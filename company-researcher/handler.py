import json
import logging
from datetime import datetime, timezone, timedelta
import boto3
import anthropic
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

deserializer = TypeDeserializer()
ssm = boto3.client('ssm', region_name='us-east-1')

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('companies')

def is_research_needed(company_name):
    response = table.get_item(Key={'company_name': company_name})
    if 'Item' not in response:
        return True
    last_updated = response['Item'].get('last_updated')
    if not last_updated:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last_updated)
    return age > timedelta(days=30)

def get_parameter(name):
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response['Parameter']['Value']

def deserialize_item(dynamo_item):
    return {k: deserializer.deserialize(v) for k, v in dynamo_item.items()}

def increment_job_count(company_name):
    table.update_item(
        Key={'company_name': company_name},
        UpdateExpression='ADD job_count :inc',
        ExpressionAttributeValues={':inc': 1}
    )

def build_prompt(company_name, company_location, job_title):
    return f"""
<company>
  name: {company_name}
  location: {company_location}
</company>

<role>
  title: {job_title}
</role>

<candidate_preferences>
  work_style: remote, hybrid
  company_size: startup, mid-size
  culture: engineering-driven, collaborative
  role_focus: individual contributor
  additional_preferences: Strong mentorship opportunities, new technologies, encourages innovation and curiousity
</candidate_preferences>
"""

SYSTEM_PROMPT = """You are an expert company research agent helping a job seeker evaluate 
potential employers. You understand that culture fit matters as much 
as role fit.

Given a company name, job title, and candidate preferences your job is to:
1. Locate the company's web presence
2. Research the company thoroughly
3. Assess fit against the candidate's preferences
4. Return concise, structured findings

Return your findings in the following format exactly.
Do not include any text outside of these fields.

company_name: {company name}
website: {company website URL}
industry: {industry}
company_size: {approximate employee count or range}
summary: {3-5 sentence overview of the company, their product, 
  mission, and trajectory}
culture_notes:
  - {culture observation}
  - {add as many observations as are relevant, minimum 2}
recent_news: {1-2 sentences on any notable recent developments, 
  or "nothing significant found" if none}
hiring_reputation: {2-3 sentences on candidate experience, interview 
  process reputation, or Glassdoor sentiment, or "insufficient data 
  found" if nothing reliable could be sourced}
candidate_fit_score: {1-10}
candidate_fit_reasoning: {2-3 sentences explaining the score 
  against the candidate's stated preferences}
research_confidence: {low | medium | high}"""

def lambda_handler(event, context):
    logger.info(f"Event received: {json.dumps(event)}")

    records = event if isinstance(event, list) else event.get("Records", [])
    for record in records:
        if record["eventName"] != "INSERT":
            continue

        raw_item = record["dynamodb"]["NewImage"]
        job = deserialize_item(raw_item)

        company_name = job["company"]
        company_location = job["location"]
        job_title = job["positionName"]

        increment_job_count(company_name)

        if not is_research_needed(company_name):
          logger.info(f"Skipping research for {company_name} — recent data exists")
          return

        logger.info(f"Researching company: {company_name}")

        api_key = get_parameter('anthropic-api-key')
        client = anthropic.Anthropic(api_key=api_key)

        user_prompt = build_prompt(company_name, company_location, job_title)

        # Your research logic here
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        logger.info(f"Research complete for: {company_name}")
        
        response_text = ""
        for block in response.content:
            if block.type == "text":
                response_text += block.text
                break

        company_information = {}
        culture_section = False
        for line in response_text.splitlines():
            stripped = line.strip()

            if stripped.startswith("- ") and culture_section:
                company_information['culture_notes'].append(stripped[2:].strip())
                continue
            parts = line.split(": ", 1)
            if len(parts) == 2:
                key = parts[0].strip()
                value = parts[1].strip()
                if key == 'culture_notes':
                    company_information['culture_notes'] = []
                    culture_section = True
                elif key == 'candidate_fit_score':
                    company_information[key] = int(value)
                else:
                    culture_section = False
                    company_information[key] = value

        valid_confidence = {'low', 'medium', 'high'}
        if company_information.get('research_confidence') not in valid_confidence:
            company_information['research_confidence'] = 'low'

        try:
            existing = table.get_item(Key={'company_name': company_name}).get('Item', {})
            current_count = existing.get('job_count', 0)

            table.put_item(Item={
                'company_name': company_information.get('company_name', company_name),
                'website': company_information.get('website', 'N/A'),
                'industry': company_information.get('industry', 'N/A'),
                'company_size': company_information.get('company_size', 'N/A'),
                'summary': company_information.get('summary', 'N/A'),
                'culture_notes': company_information.get('culture_notes', []),
                'recent_news': company_information.get('recent_news', 'N/A'),
                'hiring_reputation': company_information.get('hiring_reputation', 'N/A'),
                'candidate_fit_score': company_information.get('candidate_fit_score', 0),
                'candidate_fit_reasoning': company_information.get('candidate_fit_reasoning', 'N/A'),
                'research_confidence': company_information.get('research_confidence', 'low'),
                'last_updated': datetime.now(timezone.utc).isoformat(),
                'job_count': current_count
            })
            logger.info(f"Company information stored for: {company_name}")
        except Exception as e:
            logger.error(f"Error storing company information for {company_name}: {e}")
        
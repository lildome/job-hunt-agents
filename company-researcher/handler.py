import json
import logging
import time
from datetime import datetime, timezone, timedelta
import boto3
import anthropic
from anthropic import RateLimitError
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

deserializer = TypeDeserializer()
ssm = boto3.client('ssm', region_name='us-east-1')

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
companies_table = dynamodb.Table('companies')
profiles_table = dynamodb.Table('candidate_profiles')
jobs_table = dynamodb.Table('jobs')
mappings_table = dynamodb.Table('company_name_mappings')

def is_research_needed(company_id):
    response = companies_table.get_item(Key={'id': company_id})
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

def get_preferences():
    profile = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item', {})
    return profile.get('preferences', {})

def build_prompt(company_name, company_location, job_title, preferences):
    prefs_lines = "\n".join(f"  {k}: {v}" for k, v in preferences.items()) if preferences else ""
    return f"""
<company>
  name: {company_name}
  location: {company_location}
</company>
<role>
  title: {job_title}
</role>
<candidate_preferences>
{prefs_lines}
</candidate_preferences>
"""

SYSTEM_PROMPT = """You are an expert company research agent helping a job seeker evaluate
potential employers. You understand that culture fit matters as much
as role fit.

Given a company name, job title, and candidate preferences your job is to:
1. The provided company name is a best attempt at a canonical name, 
  but if your research reveals a more accurate canonical name use that instead 
  and note in in the canonical_company_name field in your response.
2. Locate the company's web presence
3. Research the company thoroughly
4. Assess fit against the candidate's preferences
5. Return concise, structured findings

Return your findings in the following format exactly.
Do not include any text outside of these fields.

company_name: {company name}
canonical_company_name: {the canonical public-facing brand or parent company
  name. Use the provided pre-pass result unless research reveals a more
  accurate name}
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
    logger.info(f"Job received: {event['job_id']}")
    job_id = event['job_id']
    job_response = jobs_table.get_item(Key={'id': job_id})
    if 'Item' not in job_response:
        raise ValueError(f"Job {job_id} not found in jobs table")
    job = job_response['Item']

    company_id = job.get("company_id")
    company_location = job.get("location", "")
    job_title = job.get("positionName", "")
    job_id = job.get("id")

    if not company_id:
        raise ValueError(f"Job {job_id} has no company_id field")
    

    company_response = companies_table.get_item(Key={'id': company_id})
    if 'Item' not in company_response:
        raise ValueError(f"Company {company_id} not found in companies table")
    
    company = company_response['Item']

    if not is_research_needed(company_id):
        logger.info(f"Skipping research for {company_id} — recent data exists")
        return

    logger.info(f"Researching company: {company_id} for job: {job_id}")

    company_name = company.get('company_name')

    api_key = get_parameter('anthropic-api-key')
    client = anthropic.Anthropic(api_key=api_key, max_retries=8)

    preferences = get_preferences()
    user_prompt = build_prompt(company_name, company_location, job_title, preferences)

    api_kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )
    try:
        response = client.messages.create(**api_kwargs)
    except RateLimitError:
        logger.warning(f"Rate limited researching {company_name} — waiting 65s before retry")
        time.sleep(65)
        response = client.messages.create(**api_kwargs)

    logger.info(f"Research complete for: {company_id} for job: {job_id}")

    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text

    company_information = {}
    culture_section = False
    for line in response_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("- ") and culture_section:
            company_information['culture_notes'].append(stripped[2:].strip())
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            key = parts[0].strip()
            value = parts[1].strip()
            if key == 'culture_notes':
                company_information['culture_notes'] = []
                culture_section = True
            elif key == 'candidate_fit_score':
                try:
                    company_information[key] = int(value.split('/')[0].strip())
                except (ValueError, IndexError):
                    logger.warning(f"Could not parse candidate_fit_score: {value!r}")
                    company_information[key] = 0
            else:
                culture_section = False
                company_information[key] = value

    valid_confidence = {'low', 'medium', 'high'}
    if company_information.get('research_confidence') not in valid_confidence:
        company_information['research_confidence'] = 'low'

    resolved_canonical = company_information.get('canonical_company_name', company_name)

    try:
        if resolved_canonical != company_name:
            logger.info(f"Resolved canonical name for {company_name}: {resolved_canonical}")
            companies_table.update_item(
                Key={'id': company_id},
                UpdateExpression='SET #aliases = list_append(if_not_exists(#aliases, :empty), :new)',
                ExpressionAttributeValues={':new': [company_name], ':empty': []},
            )
            companies_table.update_item(
                Key={'id': company_id},
                UpdateExpression='SET company_name = :cn',
                ExpressionAttributeValues={':cn': resolved_canonical}
            )
            mappings_table.put_item(Item={
                'raw_name': company_name,
                'company_id': company_id
            })
        
        update_fields={
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
        }
        for key, value in update_fields.items():
            if value is not None:
                companies_table.update_item(
                    Key={'id': company_id},
                    UpdateExpression=f'SET {key} = :val',
                    ExpressionAttributeValues={':val': value}
                )

        logger.info(f"Company information stored for: {resolved_canonical}")
    except Exception as e:
        logger.error(f"Error storing company information for {company_name}: {e}")

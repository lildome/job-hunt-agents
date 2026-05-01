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
table = dynamodb.Table('companies')
profiles_table = dynamodb.Table('candidate_profiles')
jobs_table = dynamodb.Table('jobs')
mappings_table = dynamodb.Table('company_name_mappings')

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

def get_preferences():
    profile = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item', {})
    return profile.get('preferences', {})

def build_prompt(company_name, company_location, job_title, preferences,
                 pre_pass_canonical_name=None, pre_pass_confidence=None, pre_pass_reasoning=None):
    prefs_lines = "\n".join(f"  {k}: {v}" for k, v in preferences.items()) if preferences else ""
    canonical_block = ""
    if pre_pass_canonical_name:
        canonical_block = f"""
<canonical_name_resolution>
  canonical_company_name: {pre_pass_canonical_name}
  confidence: {pre_pass_confidence}
  reasoning: {pre_pass_reasoning}
</canonical_name_resolution>
"""
    return f"""
<company>
  name: {company_name}
  location: {company_location}
</company>
{canonical_block}
<role>
  title: {job_title}
</role>

<candidate_preferences>
{prefs_lines}
</candidate_preferences>
"""

PRE_PASS_SYSTEM_PROMPT = """You are a company name resolution agent. Your job is to determine the
canonical public-facing brand or parent company name from a raw scraped
company name.

If you are confident from your own knowledge that the name is a subsidiary,
legal entity, or regional arm of a larger brand, return the canonical name
directly. If you are uncertain, perform a single web search to verify before
responding.

Return your findings in exactly this format. No text outside these fields.

canonical_company_name: {the canonical public-facing brand or parent company name}
confidence: {high | low}
reasoning: {one sentence explaining the resolution, or why confidence is low}"""

SYSTEM_PROMPT = """You are an expert company research agent helping a job seeker evaluate
potential employers. You understand that culture fit matters as much
as role fit.

Given a company name, job title, and candidate preferences your job is to:
1. Use the provided canonical company name as the basis for all research.
   If during research you discover evidence that a more accurate canonical
   name exists, use that instead.
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
    logger.info(f"Event received: {json.dumps(event)}")

    records = event if isinstance(event, list) else event.get("Records", [])
    for record in records:
        if record["eventName"] != "INSERT":
            continue

        raw_item = record["dynamodb"]["NewImage"]
        job = deserialize_item(raw_item)

        company_name = job.get("company")
        company_location = job.get("location", "")
        job_title = job.get("positionName", "")
        job_id = job.get("id")

        if not company_name:
            logger.warning("Record missing company field — skipping")
            continue

        increment_job_count(company_name)

        # Layer 1: mapping table lookup — no LLM cost
        mapping = mappings_table.get_item(Key={'raw_name': company_name}).get('Item')
        if mapping:
            canonical_name = mapping['canonical_company_name']
            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET canonical_company_name = :cn',
                ExpressionAttributeValues={':cn': canonical_name}
            )
            table.update_item(
                Key={'company_name': canonical_name},
                UpdateExpression='SET aliases = list_append(if_not_exists(aliases, :empty), :new)',
                ExpressionAttributeValues={':new': [company_name], ':empty': []}
            )
            logger.info(f"Mapping found for {company_name} → {canonical_name}, skipping research")
            continue

        if not is_research_needed(company_name):
            logger.info(f"Skipping research for {company_name} — recent data exists")
            continue

        logger.info(f"Researching company: {company_name}")

        api_key = get_parameter('anthropic-api-key')
        client = anthropic.Anthropic(api_key=api_key, max_retries=8)

        # Layer 2: pre-pass LLM call to resolve canonical name
        pre_pass_kwargs = dict(
            model="claude-sonnet-4-6",
            max_tokens=200,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=PRE_PASS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"<raw_company_name>\n{company_name}\n</raw_company_name>"}]
        )
        try:
            pre_pass_response = client.messages.create(**pre_pass_kwargs)
        except RateLimitError:
            logger.warning(f"Rate limited on pre-pass for {company_name} — waiting 65s before retry")
            time.sleep(65)
            pre_pass_response = client.messages.create(**pre_pass_kwargs)

        pre_pass_text = ""
        for block in pre_pass_response.content:
            if block.type == "text":
                pre_pass_text += block.text

        pre_pass_info = {}
        for line in pre_pass_text.splitlines():
            parts = line.split(":", 1)
            if len(parts) == 2:
                pre_pass_info[parts[0].strip()] = parts[1].strip()

        pre_pass_canonical_name = pre_pass_info.get('canonical_company_name', company_name)
        pre_pass_confidence = pre_pass_info.get('confidence', 'low')
        pre_pass_reasoning = pre_pass_info.get('reasoning', '')

        if pre_pass_confidence == 'high':
            existing_under_canonical = table.get_item(Key={'company_name': pre_pass_canonical_name}).get('Item')
            if existing_under_canonical:
                mappings_table.put_item(Item={
                    'raw_name': company_name,
                    'canonical_company_name': pre_pass_canonical_name
                })
                table.update_item(
                    Key={'company_name': pre_pass_canonical_name},
                    UpdateExpression='SET aliases = list_append(if_not_exists(aliases, :empty), :new)',
                    ExpressionAttributeValues={':new': [company_name], ':empty': []}
                )
                jobs_table.update_item(
                    Key={'id': job_id},
                    UpdateExpression='SET canonical_company_name = :cn',
                    ExpressionAttributeValues={':cn': pre_pass_canonical_name}
                )
                logger.info(f"Pre-pass resolved {company_name} → {pre_pass_canonical_name}, existing record found, skipping full research")
                continue

        # Layer 3: full research pass
        preferences = get_preferences()
        user_prompt = build_prompt(
            company_name, company_location, job_title, preferences,
            pre_pass_canonical_name=pre_pass_canonical_name,
            pre_pass_confidence=pre_pass_confidence,
            pre_pass_reasoning=pre_pass_reasoning
        )

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

        logger.info(f"Research complete for: {company_name}")

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
            existing_canonical = table.get_item(Key={'company_name': resolved_canonical}).get('Item')
            if existing_canonical:
                table.update_item(
                    Key={'company_name': resolved_canonical},
                    UpdateExpression='SET aliases = list_append(if_not_exists(aliases, :empty), :new)',
                    ExpressionAttributeValues={':new': [company_name], ':empty': []}
                )
            else:
                current_count = existing_canonical.get('job_count', 0) if existing_canonical else 0
                table.put_item(Item={
                    'company_name': resolved_canonical,
                    'aliases': [company_name],
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

            mappings_table.put_item(Item={
                'raw_name': company_name,
                'canonical_company_name': resolved_canonical
            })
            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET canonical_company_name = :cn',
                ExpressionAttributeValues={':cn': resolved_canonical}
            )
            logger.info(f"Company information stored for: {resolved_canonical}")
        except Exception as e:
            logger.error(f"Error storing company information for {company_name}: {e}")

import json
import logging
import boto3
import anthropic
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

deserializer = TypeDeserializer()
ssm = boto3.client('ssm', region_name='us-east-1')

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('jobs')

def get_parameter(name):
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response['Parameter']['Value']

def deserialize_item(dynamo_item):
    return {k: deserializer.deserialize(v) for k, v in dynamo_item.items()}

def build_prompt(job_description):
    return f"""
<job_description>
  {job_description}
</job_description>
"""

SYSTEM_PROMPT = """You are a job description summariser.

Emphasis Scoring guide:
    high — explicitly required, listed as must have, or mentioned multiple times 
    mid — preferred or beneficial but not strictly required 
    low — briefly mentioned or listed as nice to have

Given a job description you will return your findings in the following format exactly.
Do not include any text outside of these fields.

job_title: {job title}
job_summary: {3-5 sentence summary of job role and responsibilities}
education_requirements: 
    - {requirement} | {low/mid/high}
    - {add as many requirements as required}
experience_requirements: 
    - {requirement } | {low/mid/high}
    - {add as many requirements as required}
skill_requirements: 
    - {requirement } | {low/mid/high}
    - {add as many requirements as required}
salary: {salary in whatever format the description provided it in or “not specified” if not mentioned}
red_flags: {note anything that might be viewed negatively by a prospective employee or “none identified” if nothing stands out}"""

def lambda_handler(event, context):
    logger.info(f"Event received: {json.dumps(event)}")

    for record in event["Records"]:
        if record["eventName"] != "INSERT":
            continue

        raw_item = record["dynamodb"]["NewImage"]
        job = deserialize_item(raw_item)

        job_description = job.get("description")

        logger.info(f"Summarising job description for: {job.get('positionName')} at {job.get('company')}")

        api_key = get_parameter('anthropic-api-key')
        client = anthropic.Anthropic(api_key=api_key)

        user_prompt = build_prompt(job_description)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        logger.info(f"Summarisation complete for: {job.get('positionName')} at {job.get('company')}")
        
        response_text = ""
        for block in response.content:
            if block.type == "text":
                response_text += block.text
                break

        summary = {}
        list_section = ""
        for line in response_text.splitlines():
            stripped = line.strip()

            if stripped.startswith("- ") and list_section:
                requirement, confidence = stripped[2:].rsplit("|", 1)
                summary[list_section].append({
                    "requirement": requirement.strip(),
                    "confidence": confidence.strip()
                })
                continue
            parts = line.split(": ", 1)
            if len(parts) == 2:
                key = parts[0].strip()
                value = parts[1].strip()
                if key == 'education_requirements' or key == 'experience_requirements' or key == 'skill_requirements':
                    summary[key] = []
                    list_section = key
                else:
                    list_section = ""
                    summary[key] = value

        try:
            table.update_item(
                Key={'id': job['id']},
                UpdateExpression="SET #summary = :summary",
                ExpressionAttributeNames={
                    "#summary": "summary"
                },
                ExpressionAttributeValues={
                    ":summary": summary
                }
            )
            logger.info(f"Job summary stored for: {job.get('positionName')} at {job.get('company')}")
        except Exception as e:
            logger.error(f"Error storing job summary for {job.get('positionName')} at {job.get('company')}: {e}")
        
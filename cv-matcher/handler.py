import json
import logging
import boto3
import anthropic

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client('ssm', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
profiles_table = dynamodb.Table('candidate_profiles')

def get_parameter(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

api_key = get_parameter('anthropic-api-key')
anthropic_client = anthropic.Anthropic(api_key=api_key)

SYSTEM_PROMPT = """You are an expert CV-to-job-description matching specialist.
Your task is to evaluate how well a candidate's CV matches a given job's requirements.

You will be given:
- A structured job summary with emphasis-scored requirements (high/mid/low)
- The candidate's CV broken into sections

Weighting rules:
- High emphasis requirements: most important for the match score
- Mid emphasis requirements: moderately important
- Low emphasis requirements: minor weight

Return your findings in exactly this format. No text outside these fields.

match_score: {integer 1-10}
match_summary: {4-6 sentences explaining the score, citing specific CV evidence for or against key requirements, weighted by emphasis score}"""

def build_prompt(job_summary: dict, cv: dict) -> str:
    return f"""
<job_summary>
{json.dumps(job_summary, indent=2)}
</job_summary>

<candidate_cv>
{json.dumps(cv, indent=2)}
</candidate_cv>
"""

def lambda_handler(event, context):
    job_id = event['job_id']
    logger.info(f"Matching CV for job_id: {job_id}")

    job_response = jobs_table.get_item(Key={'id': job_id})
    if 'Item' not in job_response:
        raise ValueError(f"Job {job_id} not found in jobs table")
    job = job_response['Item']

    if 'summary' not in job:
        raise ValueError(f"Job {job_id} has no summary — run job-summariser first")

    cv_response = profiles_table.get_item(Key={'profile_id': 'primary'})
    if 'Item' not in cv_response:
        raise ValueError("Candidate profile not found — insert 'primary' record into candidate_profiles")
    cv = cv_response['Item']

    user_prompt = build_prompt(job['summary'], cv)

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )

    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text
            break

    match_score = None
    match_summary = None
    for line in response_text.splitlines():
        if line.startswith("match_score:"):
            try:
                match_score = int(line.split(":", 1)[1].strip())
            except ValueError:
                logger.error(f"Could not parse match_score from: {line}")
        elif line.startswith("match_summary:"):
            match_summary = line.split(":", 1)[1].strip()

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression="SET match_score = :score, match_summary = :summary",
        ExpressionAttributeValues={
            ':score': match_score,
            ':summary': match_summary
        }
    )

    logger.info(f"Match complete for job_id: {job_id}, score: {match_score}")
    return {"job_id": job_id, "match_score": match_score}

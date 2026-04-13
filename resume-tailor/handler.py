import json
import logging
import boto3
import anthropic

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client('ssm', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
companies_table = dynamodb.Table('companies')
profiles_table = dynamodb.Table('candidate_profiles')

def get_parameter(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

api_key = get_parameter('anthropic-api-key')
anthropic_client = anthropic.Anthropic(api_key=api_key)

SYSTEM_PROMPT = """You are an expert CV writer helping tailor a candidate's resume for a specific job application.

CRITICAL RULES:
- You may ONLY use achievements, experiences, and skills that appear in the provided CV.
- Do NOT invent, infer, or hallucinate any content not present in the CV.
- You may reorder, re-emphasise, and reword existing content to better match the job.
- Weight your emphasis toward requirements marked as 'high', then 'mid', then 'low'.
- Output a complete tailored resume in clean markdown.

SUMMARY RULES:
- Write in a direct, confident, and human voice using first person.
- Avoid buzzwords, superlatives, and vague claims — every sentence must be grounded in something specific from the CV.
- Do not write like a recruiter — write like the candidate describing themselves plainly.
- Weave in a natural signal from the candidate's preferences, particularly around learning, growth, and the type of environment they're looking for.
- Length: 4-5 sentences.

Structure your output exactly as follows:
# [Full Name]
## Summary
[Tailored summary following the summary rules above, drawing from the CV and candidate preferences]
## Experience
[Experience entries remain in chronological order — reorder and re-emphasise bullets within each role to lead with those most relevant to the job]
## Skills
[Skills reordered and grouped to lead with those matching high-emphasis requirements]
## Projects
[Project entries reordered/re-emphasised to lead with most relevant bullets]
## Education
[Education section unchanged]"""

def build_prompt(job: dict, company: dict, cv: dict) -> str:
    preferences = cv.get('preferences', {})
    preferences_block = ""
    if preferences:
        preferences_block = f"""
<candidate_preferences>
{json.dumps(preferences, indent=2)}
</candidate_preferences>
"""

    return f"""
<job_summary>
{json.dumps(job.get('summary', {}), indent=2)}
</job_summary>

<company_context>
culture_notes: {json.dumps(company.get('culture_notes', []))}
candidate_fit_reasoning: {company.get('candidate_fit_reasoning', 'N/A')}
</company_context>
{preferences_block}
<candidate_cv>
{json.dumps(cv, indent=2)}
</candidate_cv>
"""

def lambda_handler(event, context):
    job_id = event['job_id']
    logger.info(f"Tailoring resume for job_id: {job_id}")

    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        raise ValueError(f"Job {job_id} not found")
    if 'summary' not in job:
        raise ValueError(f"Job {job_id} has no summary — run job-summariser first")

    company = companies_table.get_item(
        Key={'company_name': job.get('company', '')}
    ).get('Item', {})

    cv = profiles_table.get_item(
        Key={'profile_id': 'primary'}
    ).get('Item', {})

    user_prompt = build_prompt(job, company, cv)

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )

    tailored_resume = ""
    for block in response.content:
        if block.type == "text":
            tailored_resume += block.text
            break

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression="SET tailored_resume = :resume",
        ExpressionAttributeValues={':resume': tailored_resume}
    )

    logger.info(f"Resume tailored for job_id: {job_id}")
    return {"job_id": job_id, "tailored_resume": tailored_resume}

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

SYSTEM_PROMPT = """You are an expert cover letter writer. You write compelling, authentic cover letters grounded strictly in the candidate's real experience.

CRITICAL RULES:
- Use ONLY experiences and achievements present in the provided CV.
- Do NOT fabricate results, metrics, or accomplishments not in the CV.
- Mirror the company's tone where evident from culture notes.
- Structure: opening hook → relevant experience → company-specific motivation → call to action.
- Target length: 250-350 words."""

CRITIQUE_PROMPT = """You are a senior hiring manager reviewing a cover letter for a {job_title} role at {company_name}.
Identify the top 2-3 weaknesses and suggest specific improvements. Be direct and concise."""

def build_initial_prompt(job: dict, company: dict, cv: dict) -> str:
    return f"""
<job_context>
{json.dumps(job.get('summary', {}), indent=2)}
</job_context>

<company_context>
culture_notes: {json.dumps(company.get('culture_notes', []))}
recent_news: {company.get('recent_news', 'N/A')}
</company_context>

<candidate_cv>
{json.dumps(cv, indent=2)}
</candidate_cv>

Write a cover letter for this role.
"""

def run_critique(draft: str, job_title: str, company_name: str) -> str:
    system = CRITIQUE_PROMPT.format(job_title=job_title, company_name=company_name)
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": draft}]
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""

def lambda_handler(event, context):
    job_id = event['job_id']
    mode = event.get('mode', 'autonomous')
    feedback = event.get('feedback')
    conversation_history = event.get('conversation_history', [])

    logger.info(f"Generating cover letter for job_id: {job_id}, mode: {mode}")

    job = jobs_table.get_item(Key={'id': job_id}).get('Item', {})
    if not job:
        raise ValueError(f"Job {job_id} not found")

    company_name = job.get('company', '')
    company = companies_table.get_item(Key={'company_name': company_name}).get('Item', {})
    cv = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item', {})

    if mode == 'guided':
        if not conversation_history:
            messages = [{"role": "user", "content": build_initial_prompt(job, company, cv)}]
        else:
            messages = conversation_history + [
                {"role": "user", "content": feedback or "Please revise the cover letter."}
            ]

        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=messages
        )
        draft = ""
        for block in response.content:
            if block.type == "text":
                draft += block.text
                break

        updated_history = messages + [{"role": "assistant", "content": draft}]
        logger.info(f"Guided draft generated for job_id: {job_id}")

        return {
            "job_id": job_id,
            "mode": "guided",
            "draft": draft,
            "conversation_history": updated_history,
            "complete": False
        }

    else:  # autonomous: draft → critique → revised final
        initial_prompt = build_initial_prompt(job, company, cv)
        messages = [{"role": "user", "content": initial_prompt}]

        # Turn 1: initial draft
        response1 = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=messages
        )
        draft1 = ""
        for block in response1.content:
            if block.type == "text":
                draft1 += block.text
                break

        # Critique pass
        job_title = job.get('summary', {}).get('job_title', job.get('positionName', ''))
        critique = run_critique(draft1, job_title, company_name)

        # Turn 2: revised final incorporating critique
        messages2 = messages + [
            {"role": "assistant", "content": draft1},
            {"role": "user", "content": f"A hiring manager gave this critique:\n\n{critique}\n\nRevise the cover letter to address these points."}
        ]
        response2 = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=messages2
        )
        final_draft = ""
        for block in response2.content:
            if block.type == "text":
                final_draft += block.text
                break

        jobs_table.update_item(
            Key={'id': job_id},
            UpdateExpression="SET cover_letter = :cl",
            ExpressionAttributeValues={':cl': final_draft}
        )

        logger.info(f"Autonomous cover letter generated for job_id: {job_id}")
        return {
            "job_id": job_id,
            "mode": "autonomous",
            "cover_letter": final_draft,
            "critique": critique
        }

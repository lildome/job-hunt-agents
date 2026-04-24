import json
import logging
from datetime import datetime, timezone
import boto3
import anthropic

from prompts import AUTONOMOUS_SYSTEM_PROMPT
from renderer import render_cover_letter_pdf
from filename import build_filename

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = "dprofico-job-hunt-artifacts"

s3_client = boto3.client("s3", region_name="us-east-1")
ssm = boto3.client('ssm', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
companies_table = dynamodb.Table('companies')
profiles_table = dynamodb.Table('candidate_profiles')

def get_parameter(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

api_key = get_parameter('anthropic-api-key')
anthropic_client = anthropic.Anthropic(api_key=api_key)

GUIDED_SYSTEM_PROMPT = """You are an expert cover letter writer. You write compelling, authentic cover letters grounded strictly in the candidate's real experience.

CRITICAL RULES:
- Use ONLY experiences and achievements present in the provided CV.
- Do NOT fabricate results, metrics, or accomplishments not in the CV.
- Mirror the company's tone where evident from culture notes.
- Structure: opening hook → relevant experience → company-specific motivation → call to action.
- Target length: 250-350 words."""


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


def _build_autonomous_user_prompt(job: dict, company: dict, cv: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")

    preferences = cv.get("preferences", {})
    pref_block = ""
    if preferences:
        pref_block = f"\n<candidate_preferences>\n{json.dumps(preferences, indent=2)}\n</candidate_preferences>\n"

    return (
        f"<today>{today}</today>\n\n"
        f"<job_summary>\n{json.dumps(job.get('summary', {}), indent=2)}\n</job_summary>\n\n"
        f"<company_context>\n"
        f"company_name: {job.get('company', '')}\n"
        f"culture_notes: {json.dumps(company.get('culture_notes', []))}\n"
        f"recent_news: {company.get('recent_news', 'N/A')}\n"
        f"candidate_fit_reasoning: {company.get('candidate_fit_reasoning', 'N/A')}\n"
        f"</company_context>\n"
        f"{pref_block}"
        f"<candidate_cv>\n{json.dumps(cv, indent=2)}\n</candidate_cv>"
    )


def _validate_cover_letter(data: dict) -> None:
    required = ("sender", "date", "recipient", "salutation",
                "body_paragraphs", "closing", "signature")
    for key in required:
        if key not in data:
            raise ValueError(f"Cover letter JSON missing required field: {key}")

    for f in ("name", "location", "email", "phone"):
        if f not in data["sender"]:
            raise ValueError(f"Cover letter sender missing field: {f}")

    for f in ("name", "company"):
        if f not in data["recipient"]:
            raise ValueError(f"Cover letter recipient missing field: {f}")

    if not isinstance(data["body_paragraphs"], list) or not data["body_paragraphs"]:
        raise ValueError("body_paragraphs must be a non-empty list")
    for i, p in enumerate(data["body_paragraphs"]):
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"body_paragraphs[{i}] must be a non-empty string")


def _parse_json(raw: str, label: str):
    """Parse a JSON response from the LLM, tolerating common formatting issues."""
    text = raw.strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]

    if text.endswith("```"):
        text = text.rsplit("```", 1)[0].rstrip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(
            f"JSON parse failure in {label}: no JSON object found in output\n"
            f"Raw output:\n{raw}"
        )
    text = text[start:end + 1]

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"JSON parse failure in {label}: {e}\nRaw output:\n{raw}"
        ) from e


def _upload_to_s3(job_id: str, pdf_bytes: bytes, filename: str) -> str:
    """Upload PDF bytes to S3. Returns the S3 object key."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    key = f"cover-letters/{job_id}/{timestamp}-{filename}"
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=pdf_bytes,
        ContentType='application/pdf',
        ContentDisposition=f'attachment; filename="{filename}"',
    )
    return key


def _generate_presigned_url(key: str, expires_in: int = 86400) -> str:
    """Generate a time-limited URL for fetching an S3 object."""
    return s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': S3_BUCKET, 'Key': key},
        ExpiresIn=expires_in,
    )


def render_markdown(data: dict) -> str:
    lines = []
    sender = data["sender"]
    lines.append(sender["name"])
    lines.append(sender["location"])
    lines.append(f"{sender['email']} · {sender['phone']}")
    lines.append("")
    lines.append(data["date"])
    lines.append("")
    recipient = data["recipient"]
    lines.append(recipient["name"])
    lines.append(recipient["company"])
    lines.append("")
    lines.append(data["salutation"])
    lines.append("")
    for p in data["body_paragraphs"]:
        lines.append(p)
        lines.append("")
    lines.append(data["closing"])
    lines.append("")
    lines.append(data["signature"])
    return "\n".join(lines)


def lambda_handler(event, context):
    job_id = event['job_id']
    mode = event.get('mode', 'autonomous')
    feedback = event.get('feedback')
    conversation_history = event.get('conversation_history', [])

    logger.info(f"Generating cover letter for job_id: {job_id}, mode: {mode}")

    if mode == 'guided':
        job = jobs_table.get_item(Key={'id': job_id}).get('Item', {})
        if not job:
            raise ValueError(f"Job {job_id} not found")

        company_name = job.get('company', '')
        company = companies_table.get_item(Key={'company_name': company_name}).get('Item', {})
        cv = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item', {})

        if not conversation_history:
            messages = [{"role": "user", "content": build_initial_prompt(job, company, cv)}]
        else:
            messages = conversation_history + [
                {"role": "user", "content": feedback or "Please revise the cover letter."}
            ]

        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=GUIDED_SYSTEM_PROMPT,
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

    else:  # autonomous
        jobs_table.update_item(
            Key={'id': job_id},
            UpdateExpression="SET cover_letter_status = :s, cover_letter_started_at = :t REMOVE cover_letter_error",
            ExpressionAttributeValues={
                ':s': 'generating',
                ':t': datetime.now(timezone.utc).isoformat(),
            }
        )

        try:
            job = jobs_table.get_item(Key={'id': job_id}).get('Item')
            if not job:
                raise ValueError(f"Job {job_id} not found")

            company_name = job.get('company', '')
            company = companies_table.get_item(Key={'company_name': company_name}).get('Item', {})
            cv = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item', {})

            user_prompt = _build_autonomous_user_prompt(job, company, cv)

            logger.info("Running single-call CoT generation for job_id: %s", job_id)
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=AUTONOMOUS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = ""
            for block in response.content:
                if block.type == "text":
                    raw = block.text
                    break

            cover_letter_data = _parse_json(raw, "cover letter generation")
            _validate_cover_letter(cover_letter_data)
            logger.info("Cover letter JSON parsed and validated")

            pdf_bytes = render_cover_letter_pdf(cover_letter_data)

            role = job.get("positionName", "")
            filename = build_filename(company_name, role)
            s3_key = _upload_to_s3(job_id, pdf_bytes, filename)
            pdf_url = _generate_presigned_url(s3_key)
            logger.info("PDF uploaded to S3: %s", s3_key)

            markdown = render_markdown(cover_letter_data)

            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression=(
                    "SET cover_letter_data = :data, cover_letter = :cl, "
                    "cover_letter_pdf_key = :key, cover_letter_status = :s"
                ),
                ExpressionAttributeValues={
                    ':data': cover_letter_data,
                    ':cl': markdown,
                    ':key': s3_key,
                    ':s': 'done',
                }
            )

            logger.info("Autonomous cover letter generated for job_id: %s", job_id)
            return {
                "job_id": job_id,
                "cover_letter": markdown,
                "cover_letter_pdf_url": pdf_url,
            }

        except Exception as e:
            logger.exception("cover-letter-generator failed for job_id %s", job_id)
            try:
                jobs_table.update_item(
                    Key={'id': job_id},
                    UpdateExpression="SET cover_letter_status = :s, cover_letter_error = :e",
                    ExpressionAttributeValues={
                        ':s': 'failed',
                        ':e': str(e)[:500],
                    }
                )
            except Exception:
                logger.exception("Failed to write failure status to DDB")
            raise

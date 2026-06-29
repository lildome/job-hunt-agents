import json
import logging
from datetime import datetime, timezone
import boto3
import anthropic

from prompts import AUTONOMOUS_SYSTEM_PROMPT, REVISION_INSTRUCTION
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


def _build_autonomous_user_prompt(job: dict, company: dict, cv: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")

    preferences = cv.get("preferences", {})
    pref_block = ""
    if preferences:
        pref_block = f"\n<candidate_preferences>\n{json.dumps(preferences, indent=2)}\n</candidate_preferences>\n"

    return (
        f"<today>{today}</today>\n\n"
        f"<job_summary>\n{json.dumps(job.get('analysis', {}).get('summary', {}), indent=2)}\n</job_summary>\n\n"
        f"<company_context>\n"
        f"company_name: {job.get('company', '')}\n"
        f"culture_notes: {json.dumps(company.get('culture_notes', []))}\n"
        f"recent_news: {company.get('recent_news', 'N/A')}\n"
        f"candidate_fit_reasoning: {company.get('candidate_fit_reasoning', 'N/A')}\n"
        f"</company_context>\n"
        f"{pref_block}"
        f"<candidate_cv>\n{json.dumps(cv, indent=2)}\n</candidate_cv>"
    )


def _build_revision_user_prompt(job: dict, company: dict, cv: dict,
                                 existing_letter: dict, feedback: str) -> str:
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")

    preferences = cv.get("preferences", {})
    pref_block = ""
    if preferences:
        pref_block = f"\n<candidate_preferences>\n{json.dumps(preferences, indent=2)}\n</candidate_preferences>\n"

    return (
        f"<today>{today}</today>\n\n"
        f"<job_summary>\n{json.dumps(job.get('analysis', {}).get('summary', {}), indent=2)}\n</job_summary>\n\n"
        f"<company_context>\n"
        f"company_name: {job.get('company', '')}\n"
        f"culture_notes: {json.dumps(company.get('culture_notes', []))}\n"
        f"recent_news: {company.get('recent_news', 'N/A')}\n"
        f"candidate_fit_reasoning: {company.get('candidate_fit_reasoning', 'N/A')}\n"
        f"</company_context>\n"
        f"{pref_block}"
        f"<candidate_cv>\n{json.dumps(cv, indent=2)}\n</candidate_cv>\n\n"
        f"<existing_cover_letter>\n{json.dumps(existing_letter, indent=2)}\n</existing_cover_letter>\n\n"
        f"<user_feedback>\n{feedback}\n</user_feedback>\n\n"
        f"{REVISION_INSTRUCTION}"
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
        ContentDisposition=f'inline; filename="{filename}"',
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


def _finalise_cover_letter(job_id: str, raw_response: str, job: dict,
                            company_name: str, label: str) -> dict:
    """Common finalisation: parse, validate, render, upload, persist, return."""
    cover_letter_data = _parse_json(raw_response, label)
    _validate_cover_letter(cover_letter_data)
    logger.info("Cover letter JSON parsed and validated (%s)", label)

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

    return {
        "job_id": job_id,
        "cover_letter": markdown,
        "cover_letter_pdf_url": pdf_url,
    }


def lambda_handler(event, context):
    job_id = event['job_id']
    mode = event.get('mode', 'generate')
    feedback = event.get('feedback')

    logger.info("Cover letter request: job_id=%s, mode=%s", job_id, mode)

    if mode not in ('generate', 'revise'):
        raise ValueError(
            f"Invalid mode '{mode}'. Expected 'generate' or 'revise'. "
            "The 'autonomous' and 'guided' modes have been removed."
        )

    if mode == 'revise' and not feedback:
        raise ValueError("Mode 'revise' requires non-empty 'feedback' field")

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression="SET cover_letter_status = :s, cover_letter_started_at = :t REMOVE cover_letter_error, cover_letter_pdf_url_cached, cover_letter_pdf_url_generated_at",
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
        company_id = job.get('company_id')
        company = (
            companies_table.get_item(Key={'id': company_id}).get('Item', {})
            if company_id else {}
        )
        cv = profiles_table.get_item(
            Key={'profile_id': 'primary'}
        ).get('Item', {})

        if mode == 'generate':
            user_prompt = _build_autonomous_user_prompt(job, company, cv)
            label = "cover letter generation"
        else:  # mode == 'revise'
            existing = job.get('cover_letter_data')
            if not existing:
                raise ValueError(
                    f"No existing cover letter to revise for job {job_id}. "
                    "Generate one first before requesting revision."
                )
            user_prompt = _build_revision_user_prompt(
                job, company, cv, existing, feedback
            )
            label = "cover letter revision"

        logger.info("Running LLM call for %s", label)
        response = anthropic_client.messages.create(
            model="claude-opus-4-8",
            thinking={"type": "adaptive"},
            max_tokens=2048,
            system=AUTONOMOUS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = ""
        for block in response.content:
            if block.type == "text":
                raw = block.text
                break

        return _finalise_cover_letter(job_id, raw, job, company_name, label)

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

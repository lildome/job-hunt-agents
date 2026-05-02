import copy
import json
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field

import boto3
import anthropic

from prompts import TAILOR_SYSTEM_PROMPT, REPAIR_SYSTEM_PROMPT
from measure import simulate_wrap, is_orphan, describe_orphan
from renderer import render_pdf, get_bullet_layout_params, get_summary_layout_params
from filename import build_filename

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = "dprofico-job-hunt-artifacts"

s3_client = boto3.client("s3", region_name="us-east-1")
ssm = boto3.client("ssm", region_name="us-east-1")
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
jobs_table = dynamodb.Table("jobs")
companies_table = dynamodb.Table("companies")
profiles_table = dynamodb.Table("candidate_profiles")


def _get_parameter(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


api_key = _get_parameter("anthropic-api-key")
anthropic_client = anthropic.Anthropic(api_key=api_key)


# ─── Schema validation ────────────────────────────────────────────────────────

def _validate_resume(data: dict) -> None:
    for key in ("name", "contact", "summary", "experience", "skills", "education", "projects"):
        if key not in data:
            raise ValueError(f"Resume JSON missing required field: {key}")

    for f in ("location", "phone", "email", "linkedin", "github"):
        if f not in data["contact"]:
            raise ValueError(f"Resume contact missing field: {f}")

    for i, job in enumerate(data["experience"]):
        for f in ("company", "role", "period", "bullets"):
            if f not in job:
                raise ValueError(f"experience[{i}] missing field: {f}")

    for i, sg in enumerate(data["skills"]):
        for f in ("category", "items"):
            if f not in sg:
                raise ValueError(f"skills[{i}] missing field: {f}")

    for f in ("institution", "year", "degrees"):
        if f not in data["education"]:
            raise ValueError(f"education missing field: {f}")

    for i, proj in enumerate(data["projects"]):
        for f in ("title", "subtitle", "bullets"):
            if f not in proj:
                raise ValueError(f"projects[{i}] missing field: {f}")


# ─── Prompt builders ─────────────────────────────────────────────────────────

def _build_tailor_user_prompt(job: dict, company: dict, cv: dict) -> str:
    preferences = cv.get("preferences", {})
    pref_block = ""
    if preferences:
        pref_block = f"\n<candidate_preferences>\n{json.dumps(preferences, indent=2)}\n</candidate_preferences>\n"

    return (
        f"<job_summary>\n{json.dumps(job.get('summary', {}), indent=2)}\n</job_summary>\n\n"
        f"<company_context>\n"
        f"culture_notes: {json.dumps(company.get('culture_notes', []))}\n"
        f"candidate_fit_reasoning: {company.get('candidate_fit_reasoning', 'N/A')}\n"
        f"</company_context>\n"
        f"{pref_block}"
        f"<candidate_cv>\n{json.dumps(cv, indent=2)}\n</candidate_cv>"
    )


def _build_repair_user_prompt(
    job: dict,
    company: dict,
    cv: dict,
    resume: dict,
    orphans: list["OrphanReport"],
) -> str:
    context = _build_tailor_user_prompt(job, company, cv)

    # Group orphans by section key so we can show full section context
    orphan_by_path = {o.path: o for o in orphans}

    # Collect sections that have at least one orphan
    sections: dict[str, list["OrphanReport"]] = {}
    for o in orphans:
        if o.path == "summary":
            sections.setdefault("summary", []).append(o)
        else:
            m = re.match(r"^(experience|projects)\[(\d+)\]", o.path)
            if m:
                section_key = f"{m.group(1)}[{m.group(2)}]"
                sections.setdefault(section_key, []).append(o)

    blocks = []
    for section_key, section_orphans in sections.items():
        if section_key == "summary":
            o = section_orphans[0]
            header = "Section: summary"
            body = f'  {resume["summary"]}'
            if o.path in orphan_by_path:
                body += f"  [FLAGGED - orphan: {o.metric_description}]"
            paths_line = "Paths to repair: summary"
            blocks.append(f"{header}\n{body}\n{paths_line}")
        else:
            m = re.match(r"^(experience|projects)\[(\d+)\]$", section_key)
            if not m:
                continue
            section_name, idx_str = m.group(1), m.group(2)
            idx = int(idx_str)
            entry = resume[section_name][idx]

            if section_name == "experience":
                label = f"{entry['company']}, {entry['role']}"
            else:
                label = f"{entry['title']}"

            header = f"Section: {section_key}.bullets ({label})"
            bullet_lines = []
            orphaned_paths = []
            for j, b in enumerate(entry["bullets"]):
                path = f"{section_name}[{idx}].bullets[{j}]"
                flag = ""
                if path in orphan_by_path:
                    flag = f"  [FLAGGED - orphan: {orphan_by_path[path].metric_description}]"
                    orphaned_paths.append(path)
                bullet_lines.append(f"  [{j}] {b}{flag}")

            paths_line = "Paths to repair: " + ", ".join(orphaned_paths)
            blocks.append(header + "\n" + "\n".join(bullet_lines) + "\n" + paths_line)

    repair_sections = "\n\n".join(blocks)
    return f"{context}\n\n<items_to_repair>\n{repair_sections}\n</items_to_repair>"


# ─── Orphan detection ─────────────────────────────────────────────────────────

@dataclass
class OrphanReport:
    path: str
    lines: list[str] = field(repr=False)
    metric_description: str


def _detect_orphans(resume: dict) -> list[OrphanReport]:
    orphans: list[OrphanReport] = []
    bullet_params = get_bullet_layout_params()
    summary_params = get_summary_layout_params()

    # Keys for is_orphan / describe_orphan (no hanging_indent)
    bullet_measure = {k: v for k, v in bullet_params.items() if k != "hanging_indent"}
    summary_measure = {k: v for k, v in summary_params.items() if k != "hanging_indent"}

    # Summary
    lines = simulate_wrap(resume["summary"], **summary_params)
    if is_orphan(lines, **summary_measure):
        orphans.append(OrphanReport(
            path="summary",
            lines=lines,
            metric_description=describe_orphan(lines, **summary_measure),
        ))

    # Experience bullets
    for i, job in enumerate(resume["experience"]):
        for j, bullet in enumerate(job["bullets"]):
            text = "•\xa0\xa0" + bullet
            lines = simulate_wrap(text, **bullet_params)
            if is_orphan(lines, **bullet_measure):
                orphans.append(OrphanReport(
                    path=f"experience[{i}].bullets[{j}]",
                    lines=lines,
                    metric_description=describe_orphan(lines, **bullet_measure),
                ))

    # Project bullets
    for i, proj in enumerate(resume["projects"]):
        for j, bullet in enumerate(proj["bullets"]):
            text = "•\xa0\xa0" + bullet
            lines = simulate_wrap(text, **bullet_params)
            if is_orphan(lines, **bullet_measure):
                orphans.append(OrphanReport(
                    path=f"projects[{i}].bullets[{j}]",
                    lines=lines,
                    metric_description=describe_orphan(lines, **bullet_measure),
                ))

    return orphans


# ─── Repair application ───────────────────────────────────────────────────────

_BULLET_PATH_RE = re.compile(r"^(experience|projects)\[(\d+)\]\.bullets\[(\d+)\]$")


def apply_repairs(resume: dict, repairs: list[dict]) -> dict:
    """Apply each {path, text} repair to a deep copy of resume."""
    result = copy.deepcopy(resume)
    for repair in repairs:
        path = repair.get("path", "")
        text = repair.get("text", "")
        if path == "summary":
            result["summary"] = text
            continue
        m = _BULLET_PATH_RE.match(path)
        if m:
            section, idx, bidx = m.group(1), int(m.group(2)), int(m.group(3))
            try:
                result[section][idx]["bullets"][bidx] = text
            except (IndexError, KeyError):
                logger.warning("Repair path %s out of bounds, skipping", path)
        else:
            logger.warning("Unknown repair path %s, skipping", path)
    return result


# ─── Markdown renderer (backward compat) ─────────────────────────────────────

def render_markdown(resume: dict) -> str:
    lines = [f"# {resume['name']}", ""]

    contact = resume["contact"]
    parts = [v.strip() for v in [
        contact.get("location", ""), contact.get("phone", ""),
        contact.get("email", ""), contact.get("linkedin", ""),
        contact.get("github", ""),
    ] if v.strip()]
    lines += [" · ".join(parts), ""]

    lines += ["## Summary", resume["summary"], ""]

    lines.append("## Experience")
    for job in resume["experience"]:
        lines.append(f"### {job['company']} — {job['role']} ({job['period']})")
        for b in job["bullets"]:
            lines.append(f"- {b}")
        lines.append("")

    lines.append("## Skills")
    for sg in resume["skills"]:
        lines.append(f"**{sg['category']}**: {', '.join(sg['items'])}")
    lines.append("")

    lines.append("## Projects")
    for proj in resume["projects"]:
        lines.append(f"### {proj['title']} — {proj['subtitle']}")
        for b in proj["bullets"]:
            lines.append(f"- {b}")
        lines.append("")

    lines.append("## Education")
    edu = resume["education"]
    lines.append(f"**{edu['institution']}** ({edu['year']})")
    for degree in edu["degrees"]:
        lines.append(f"- {degree}")

    return "\n".join(lines)


# ─── LLM helpers ─────────────────────────────────────────────────────────────

def _llm_call(system: str, user: str, max_tokens: int = 4096) -> str:
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("LLM returned no text block")


def _parse_json(raw: str, label: str):
    """Parse a JSON response from the LLM, tolerating common formatting issues.

    Handles three known LLM output quirks:
    1. Markdown code fence wrapping (```json ... ```)
    2. Preamble or postamble text outside the JSON object
    3. Unescaped control characters (newlines, tabs) inside string values

    Fail fast: raises ValueError with full raw output if the normalised text
    still can't be parsed. Does NOT retry the LLM call.
    """
    text = raw.strip()

    # Strip opening markdown code fence if present
    if text.startswith("```"):
        # Drop everything up to and including the first newline ("```json\n" or just "```\n")
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]

    # Strip closing markdown code fence if present
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0].rstrip()

    # Find the JSON object boundaries — strips any preamble or trailing commentary
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(
            f"JSON parse failure in {label}: no JSON object found in output\n"
            f"Raw output:\n{raw}"
        )
    text = text[start:end + 1]

    # strict=False tolerates unescaped control characters inside string values
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"JSON parse failure in {label}: {e}\nRaw output:\n{raw}"
        ) from e
    
# ─── S3 Helpers ───────────────────────────────────────────────────────────────

def _upload_to_s3(job_id: str, pdf_bytes: bytes, filename: str) -> str:
    """Upload PDF bytes to S3. Returns the S3 object key."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    key = f"resumes/{job_id}/{timestamp}-{filename}"
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

# ─── Lambda entry point ───────────────────────────────────────────────────────

def lambda_handler(event, context):
    job_id = event["job_id"]
    logger.info("Tailoring resume for job_id: %s", job_id)

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression="SET tailored_resume_status = :s, tailored_resume_started_at = :t REMOVE tailored_resume_error, tailored_resume_pdf_url_cached, tailored_resume_pdf_url_generated_at",
        ExpressionAttributeValues={
            ':s': 'generating',
            ':t': datetime.now(timezone.utc).isoformat(),
        }
    )

    try:
        # Load data from DynamoDB
        job = jobs_table.get_item(Key={"id": job_id}).get("Item")
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if "summary" not in job:
            raise ValueError(f"Job {job_id} has no summary — run job-summariser first")

        company = companies_table.get_item(
            Key={"company_name": job.get("company", "")}
        ).get("Item", {})

        cv = profiles_table.get_item(Key={"profile_id": "primary"}).get("Item", {})

        # Pass 1: tailor
        logger.info("Pass 1: tailoring resume")
        tailor_user = _build_tailor_user_prompt(job, company, cv)
        raw_json = _llm_call(TAILOR_SYSTEM_PROMPT, tailor_user)
        resume_data = _parse_json(raw_json, "tailor pass")
        _validate_resume(resume_data)
        logger.info("Pass 1 complete — schema valid")

        # Pass 2: detect orphans
        orphans = _detect_orphans(resume_data)
        if orphans:
            logger.info("Pass 2: %d orphan(s) detected: %s",
                        len(orphans), [o.path for o in orphans])
        else:
            logger.info("Pass 2: no orphans detected")

        # Pass 3: repair (only if orphans found)
        if orphans:
            repair_user = _build_repair_user_prompt(job, company, cv, resume_data, orphans)
            raw_repairs = _llm_call(REPAIR_SYSTEM_PROMPT, repair_user, max_tokens=2048)
            repairs_data = _parse_json(raw_repairs, "repair pass")
            repairs = repairs_data.get("repairs", [])
            resume_data = apply_repairs(resume_data, repairs)
            logger.info("Pass 3: applied %d repair(s)", len(repairs))

        # Pass 4: final render
        pdf_bytes = render_pdf(resume_data)

        # Check for remaining orphans (warn only — do not block)
        remaining = _detect_orphans(resume_data)
        if remaining:
            logger.warning(
                "Remaining orphan(s) after repair pass: %s",
                [o.path for o in remaining],
            )

        # Build outputs
        markdown = render_markdown(resume_data)

        # Upload PDF to S3
        role = job.get("positionName", "")
        company_name = job.get("company", "")
        filename = build_filename(company_name, role)
        s3_key = _upload_to_s3(job_id, pdf_bytes, filename)

        url = _generate_presigned_url(s3_key)
        jobs_table.update_item(
            Key={'id': job_id},
            UpdateExpression="SET tailored_resume_data = :data, tailored_resume = :md, tailored_resume_pdf_key = :key, tailored_resume_status = :s",
            ExpressionAttributeValues={
                ':data': resume_data,
                ':md': markdown,
                ':key': s3_key,
                ':s': 'done',
            }
        )

        logger.info("Resume tailored and stored for job_id: %s", job_id)

        return {
            "job_id": job_id,
            "tailored_resume": markdown,
            "tailored_resume_pdf_url": url,
        }

    except Exception as e:
        logger.exception("resume-tailor failed for job_id %s", job_id)
        try:
            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression="SET tailored_resume_status = :s, tailored_resume_error = :e",
                ExpressionAttributeValues={
                    ':s': 'failed',
                    ':e': str(e)[:500],
                }
            )
        except Exception:
            logger.exception("Failed to write failure status to DDB")
        raise

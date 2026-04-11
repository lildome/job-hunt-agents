"""
Local prompt inspector for job-summariser.

Loads jobs from tests/fixtures/melbourne_se_jobs.json, runs the exact same
system prompt and parsing logic as job-summariser/handler.py, and prints the
raw Claude response alongside the parsed summary — flagging any missing sections.

Usage (from repo root):
  python3 tests/test_summariser_prompt.py          # all jobs
  python3 tests/test_summariser_prompt.py 2        # job at index 2
"""

import json
import sys
import boto3
import anthropic

FIXTURE_PATH = 'tests/fixtures/melbourne_se_jobs.json'

# ── Exact copies from job-summariser/handler.py ──────────────────────────────

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
salary: {salary in whatever format the description provided it in or "not specified" if not mentioned}
red_flags: {note anything that might be viewed negatively by a prospective employee or "none identified" if nothing stands out}"""


def parse_summary(response_text):
    """Exact copy of the parsing loop from job-summariser/handler.py."""
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
        parts = line.split(":", 1)
        if len(parts) == 2:
            key = parts[0].strip()
            value = parts[1].strip()
            if key in ('education_requirements', 'experience_requirements', 'skill_requirements'):
                summary[key] = []
                list_section = key
            else:
                list_section = ""
                summary[key] = value
    return summary

# ─────────────────────────────────────────────────────────────────────────────

RED   = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def check(label, value):
    if isinstance(value, list):
        count = len(value)
        status = f"{GREEN}{count} items{RESET}" if count > 0 else f"{RED}MISSING (0 items){RESET}"
        print(f"  {label}: {status}")
        return count > 0
    else:
        present = bool(value and value.strip())
        status = f"{GREEN}{repr(value)}{RESET}" if present else f"{RED}MISSING{RESET}"
        print(f"  {label}: {status}")
        return present

def run_job(job, idx, client):
    print(f"\n{'='*70}")
    print(f"{BOLD}[{idx}] {job.get('positionName')} @ {job.get('company')}{RESET}")
    print(f"     {job.get('location')} | {job.get('url', '')}")
    print(f"{'='*70}")

    description = job.get('description', '')
    if not description:
        print(f"{RED}  ERROR: no description field{RESET}")
        return

    print(f"\n{BOLD}--- Calling Claude ---{RESET}")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(description)}]
    )

    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text
            break

    print(f"\n{BOLD}--- Raw Claude Response ---{RESET}")
    print(response_text)

    summary = parse_summary(response_text)

    print(f"\n{BOLD}--- Parsed Summary ---{RESET}")
    all_ok = True
    all_ok &= check("job_title", summary.get("job_title"))
    all_ok &= check("job_summary", summary.get("job_summary"))
    all_ok &= check("education_requirements", summary.get("education_requirements", []))
    all_ok &= check("experience_requirements", summary.get("experience_requirements", []))
    all_ok &= check("skill_requirements", summary.get("skill_requirements", []))
    check("salary", summary.get("salary"))
    check("red_flags", summary.get("red_flags"))

    if all_ok:
        print(f"\n  {GREEN}✓ All sections present{RESET}")
    else:
        print(f"\n  {RED}✗ One or more sections missing — check raw response above{RESET}")

    return summary

def main():
    with open(FIXTURE_PATH) as f:
        jobs = json.load(f)

    # Determine which jobs to test
    if len(sys.argv) > 1:
        try:
            indices = [int(sys.argv[1])]
        except ValueError:
            print(f"Usage: python3 {sys.argv[0]} [job_index]")
            sys.exit(1)
    else:
        indices = list(range(len(jobs)))

    print(f"Loaded {len(jobs)} jobs from {FIXTURE_PATH}")
    print(f"Testing job(s): {indices}")

    # Init Anthropic client
    ssm = boto3.client('ssm', region_name='us-east-1')
    api_key = ssm.get_parameter(Name='anthropic-api-key', WithDecryption=True)['Parameter']['Value']
    client = anthropic.Anthropic(api_key=api_key)

    results = {}
    for idx in indices:
        if idx >= len(jobs):
            print(f"{RED}Index {idx} out of range (0–{len(jobs)-1}){RESET}")
            continue
        results[idx] = run_job(jobs[idx], idx, client)

    # Summary table
    print(f"\n{'='*70}")
    print(f"{BOLD}SUMMARY{RESET}")
    list_keys = ['education_requirements', 'experience_requirements', 'skill_requirements']
    for idx, summary in results.items():
        if summary is None:
            continue
        job = jobs[idx]
        missing = [k for k in list_keys if not summary.get(k)]
        status = f"{RED}MISSING: {missing}{RESET}" if missing else f"{GREEN}OK{RESET}"
        print(f"  [{idx}] {job.get('positionName','?')[:45]} — {status}")

if __name__ == '__main__':
    main()

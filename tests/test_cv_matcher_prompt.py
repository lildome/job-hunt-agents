"""
Local prompt inspector for cv-matcher.

Generates (or loads cached) job summaries from tests/fixtures/melbourne_se_jobs.json,
fetches the candidate CV from DynamoDB, then runs the exact same system prompt and
parsing logic as cv-matcher/handler.py — printing the raw Claude response and
parsed result so any missing/truncated fields are immediately visible.

Summaries are cached to tests/fixtures/job_summaries.json to avoid re-running the
summariser on every cv-matcher test iteration.

Usage (from repo root):
  python3 tests/test_cv_matcher_prompt.py            # test job at index 0
  python3 tests/test_cv_matcher_prompt.py 2          # test job at index 2
  python3 tests/test_cv_matcher_prompt.py --refresh  # re-generate all summaries first
"""

import json
import sys
import time
import boto3
import anthropic
from anthropic import RateLimitError

FIXTURE_PATH   = 'tests/fixtures/melbourne_se_jobs.json'
SUMMARIES_PATH = 'tests/fixtures/job_summaries.json'

# ── Summariser logic (exact copy from job-summariser/handler.py) ──────────────

SUMMARISER_SYSTEM_PROMPT = """You are a job description summariser.

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

def build_summariser_prompt(job_description):
    return f"""
<job_description>
  {job_description}
</job_description>
"""

def parse_summary(response_text):
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

def generate_summaries(jobs, client):
    summaries = {}
    for i, job in enumerate(jobs):
        print(f"  Summarising [{i}] {job.get('positionName')} @ {job.get('company')}...")
        api_kwargs = dict(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SUMMARISER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_summariser_prompt(job['description'])}]
        )
        try:
            response = client.messages.create(**api_kwargs)
        except RateLimitError:
            print("    Rate limited — waiting 65s...")
            time.sleep(65)
            response = client.messages.create(**api_kwargs)

        response_text = ""
        for block in response.content:
            if block.type == "text":
                response_text += block.text
                break
        summaries[i] = parse_summary(response_text)

    with open(SUMMARIES_PATH, 'w') as f:
        json.dump(summaries, f, indent=2)
    print(f"  Saved summaries to {SUMMARIES_PATH}")
    return summaries

# ── CV Matcher logic (exact copy from cv-matcher/handler.py) ──────────────────

CV_MATCHER_SYSTEM_PROMPT = """You are an expert CV-to-job-description matching specialist.
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

def build_cv_prompt(job_summary, cv):
    return f"""
<job_summary>
{json.dumps(job_summary, indent=2)}
</job_summary>

<candidate_cv>
{json.dumps(cv, indent=2)}
</candidate_cv>
"""

def parse_match(response_text):
    match_score = None
    match_summary = None
    for line in response_text.splitlines():
        if line.startswith("match_score:"):
            try:
                match_score = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("match_summary:"):
            match_summary = line.split(":", 1)[1].strip()
    return match_score, match_summary

# ─────────────────────────────────────────────────────────────────────────────

RED   = "\033[91m"
GREEN = "\033[92m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def main():
    refresh = '--refresh' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    try:
        idx = int(args[0]) if args else 0
    except ValueError:
        print(f"Usage: python3 {sys.argv[0]} [job_index] [--refresh]")
        sys.exit(1)

    with open(FIXTURE_PATH) as f:
        jobs = json.load(f)

    ssm = boto3.client('ssm', region_name='us-east-1')
    api_key = ssm.get_parameter(Name='anthropic-api-key', WithDecryption=True)['Parameter']['Value']
    client = anthropic.Anthropic(api_key=api_key)

    # Load or generate summaries
    summaries = {}
    if not refresh:
        try:
            with open(SUMMARIES_PATH) as f:
                raw = json.load(f)
                summaries = {int(k): v for k, v in raw.items()}
            print(f"Loaded cached summaries from {SUMMARIES_PATH}")
        except FileNotFoundError:
            pass

    if refresh or not summaries:
        print("Generating summaries (this will call Claude once per job)...")
        summaries = generate_summaries(jobs, client)

    if idx not in summaries:
        print(f"{RED}No summary for job index {idx}. Run with --refresh or choose 0–{len(summaries)-1}{RESET}")
        sys.exit(1)

    # Fetch CV from DynamoDB
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    profiles_table = dynamodb.Table('candidate_profiles')
    cv_response = profiles_table.get_item(Key={'profile_id': 'primary'})
    if 'Item' not in cv_response:
        print(f"{RED}ERROR: No 'primary' record in candidate_profiles table{RESET}")
        sys.exit(1)
    cv = cv_response['Item']

    job = jobs[idx]
    summary = summaries[idx]

    print(f"\n{'='*70}")
    print(f"{BOLD}[{idx}] {job.get('positionName')} @ {job.get('company')}{RESET}")
    print(f"{'='*70}")

    print(f"\n{BOLD}--- Job Summary (input) ---{RESET}")
    print(json.dumps(summary, indent=2))

    print(f"\n{BOLD}--- Calling Claude ---{RESET}")
    api_kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=CV_MATCHER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_cv_prompt(summary, cv)}]
    )
    try:
        response = client.messages.create(**api_kwargs)
    except RateLimitError:
        print("Rate limited — waiting 65s...")
        time.sleep(65)
        response = client.messages.create(**api_kwargs)

    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text
            break

    print(f"\n{BOLD}--- Raw Claude Response ---{RESET}")
    print(response_text)

    match_score, match_summary = parse_match(response_text)

    print(f"\n{BOLD}--- Parsed Result ---{RESET}")
    score_ok = match_score is not None
    summary_ok = bool(match_summary and match_summary.strip())

    score_str = f"{GREEN}{match_score}{RESET}" if score_ok else f"{RED}MISSING{RESET}"
    print(f"  match_score:   {score_str}")

    if summary_ok:
        print(f"  match_summary: {GREEN}{repr(match_summary[:80])}...{RESET}")
    else:
        print(f"  match_summary: {RED}MISSING{RESET}")

    if score_ok and summary_ok:
        print(f"\n  {GREEN}✓ Both fields parsed{RESET}")
    else:
        print(f"\n  {RED}✗ One or more fields missing — check raw response above{RESET}")

if __name__ == '__main__':
    main()

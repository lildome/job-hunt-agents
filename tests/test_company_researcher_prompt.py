"""
Local prompt inspector for company-researcher.

Loads companies from the jobs in tests/fixtures/melbourne_se_jobs.json,
runs the exact same system prompt and parsing logic as
company-researcher/handler.py, and prints the raw Claude response
alongside the parsed result — flagging any missing fields.

Usage (from repo root):
  python3 tests/test_company_researcher_prompt.py          # all companies
  python3 tests/test_company_researcher_prompt.py 0        # company at index 0
"""

import json
import sys
import boto3
import anthropic

FIXTURE_PATH = 'tests/fixtures/melbourne_se_jobs.json'

# ── Exact copies from company-researcher/handler.py ──────────────────────────

def build_prompt(company_name, company_location, job_title):
    return f"""
<company>
  name: {company_name}
  location: {company_location}
</company>

<role>
  title: {job_title}
</role>

<candidate_preferences>
  work_style: remote, hybrid
  company_size: startup, mid-size
  culture: engineering-driven, collaborative
  role_focus: individual contributor
  additional_preferences: Strong mentorship opportunities, new technologies, encourages innovation and curiousity
</candidate_preferences>
"""

SYSTEM_PROMPT = """You are an expert company research agent helping a job seeker evaluate
potential employers. You understand that culture fit matters as much
as role fit.

Given a company name, job title, and candidate preferences your job is to:
1. Locate the company's web presence
2. Research the company thoroughly
3. Assess fit against the candidate's preferences
4. Return concise, structured findings

Return your findings in the following format exactly.
Do not include any text outside of these fields.

company_name: {company name}
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


def parse_company(response_text):
    """Exact copy of the parsing loop from company-researcher/handler.py."""
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
                    company_information[key] = int(value)
                except ValueError:
                    company_information[key] = value
            else:
                culture_section = False
                company_information[key] = value
    return company_information

# ─────────────────────────────────────────────────────────────────────────────

RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

REQUIRED_FIELDS = [
    'company_name', 'website', 'industry', 'company_size',
    'summary', 'culture_notes', 'recent_news', 'hiring_reputation',
    'candidate_fit_score', 'candidate_fit_reasoning', 'research_confidence',
]

def check(label, value):
    if isinstance(value, list):
        count = len(value)
        status = f"{GREEN}{count} items{RESET}" if count > 0 else f"{RED}MISSING (0 items){RESET}"
        print(f"  {label}: {status}")
        return count > 0
    else:
        present = bool(value is not None and str(value).strip())
        status = f"{GREEN}{repr(value)}{RESET}" if present else f"{RED}MISSING{RESET}"
        print(f"  {label}: {status}")
        return present

def run_company(company_name, company_location, job_title, idx, client):
    print(f"\n{'='*70}")
    print(f"{BOLD}[{idx}] {company_name} ({company_location}){RESET}")
    print(f"     Role: {job_title}")
    print(f"{'='*70}")

    print(f"\n{BOLD}--- Calling Claude ---{RESET}")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(company_name, company_location, job_title)}]
    )

    print(f"\n{BOLD}--- Response Blocks ---{RESET}")
    for i, block in enumerate(response.content):
        print(f"  block[{i}]: type={block.type}")

    # Concatenate all text blocks (web_search responses arrive as many small fragments)
    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text

    print(f"\n{BOLD}--- Raw Claude Response ---{RESET}")
    print(response_text)

    result = parse_company(response_text)

    print(f"\n{BOLD}--- Parsed Result ---{RESET}")
    all_ok = True
    for field in REQUIRED_FIELDS:
        ok = check(field, result.get(field, [] if field == 'culture_notes' else None))
        all_ok = all_ok and ok

    if all_ok:
        print(f"\n  {GREEN}✓ All fields present{RESET}")
    else:
        print(f"\n  {RED}✗ One or more fields missing — check raw response above{RESET}")

    return result

def main():
    with open(FIXTURE_PATH) as f:
        jobs = json.load(f)

    # Deduplicate companies, preserve order
    seen = set()
    companies = []
    for job in jobs:
        name = job.get('company', '')
        if name and name not in seen:
            seen.add(name)
            companies.append({
                'company': name,
                'location': job.get('location', ''),
                'positionName': job.get('positionName', ''),
            })

    if len(sys.argv) > 1:
        try:
            indices = [int(sys.argv[1])]
        except ValueError:
            print(f"Usage: python3 {sys.argv[0]} [company_index]")
            sys.exit(1)
    else:
        indices = list(range(len(companies)))

    print(f"Found {len(companies)} unique companies in fixture")
    print(f"Testing company/companies: {indices}")

    ssm = boto3.client('ssm', region_name='us-east-1')
    api_key = ssm.get_parameter(Name='anthropic-api-key', WithDecryption=True)['Parameter']['Value']
    client = anthropic.Anthropic(api_key=api_key)

    results = {}
    for idx in indices:
        if idx >= len(companies):
            print(f"{RED}Index {idx} out of range (0–{len(companies)-1}){RESET}")
            continue
        c = companies[idx]
        results[idx] = run_company(c['company'], c['location'], c['positionName'], idx, client)

    print(f"\n{'='*70}")
    print(f"{BOLD}SUMMARY{RESET}")
    for idx, result in results.items():
        if result is None:
            continue
        c = companies[idx]
        missing = [f for f in REQUIRED_FIELDS
                   if not result.get(f) and not (f == 'culture_notes' and result.get(f) == [])]
        missing_culture = result.get('culture_notes') == [] or 'culture_notes' not in result
        if missing_culture and 'culture_notes' not in missing:
            missing.append('culture_notes')
        status = f"{RED}MISSING: {missing}{RESET}" if missing else f"{GREEN}OK{RESET}"
        print(f"  [{idx}] {c['company'][:40]} — {status}")

if __name__ == '__main__':
    main()

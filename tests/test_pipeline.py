"""
End-to-end integration test for the full pipeline.

Flow under test:
  DynamoDB INSERT → EventBridge Pipes →
    Pipe A: company-researcher Lambda (writes to companies table)
    Pipe B: Step Functions → job-summariser → cv-matcher (writes to jobs table)

What it does:
  1. Inserts a realistic test job into the jobs table
  2. Waits up to 3 minutes for all fields to be populated
  3. Asserts summary, match_score, match_summary are written to the jobs table
  4. Asserts a company record was written to the companies table
  5. Cleans up both test records

Run from repo root:
  python3 tests/test_pipeline.py
"""

import json
import uuid
import sys
import time
import boto3

REGION = 'us-east-1'
JOBS_TABLE = 'jobs'
COMPANIES_TABLE = 'companies'
POLL_INTERVAL = 15   # seconds between DynamoDB checks
MAX_WAIT = 180       # seconds before giving up

dynamodb = boto3.resource('dynamodb', region_name=REGION)
jobs_table = dynamodb.Table(JOBS_TABLE)
companies_table = dynamodb.Table(COMPANIES_TABLE)

TEST_JOB = {
    'positionName': 'Junior Software Engineer',
    'company': 'Atlassian',
    'location': 'Remote',
    'source': 'test',
    'status': 'new',
    'scrapedAt': '2026-04-07T00:00:00Z',
    'postingDate': '2026-04-01T00:00:00Z',
    'salary': '$90,000 - $110,000',
    'url': 'https://example.com/jobs/123',
    'description': (
        "We are hiring a Junior Software Engineer to join our platform team. "
        "You will build and maintain Python-based backend services running on Linux servers. "
        "Required: 1-2 years Python experience, Linux/Unix administration, Bash scripting, Git. "
        "Preferred: experience with Ansible or similar IaC tools, SQL or NoSQL databases, CI/CD concepts. "
        "Nice to have: familiarity with AWS services. "
        "Education: Bachelor's degree in Computer Science or equivalent. "
        "We offer mentorship, flexible remote work, and a collaborative engineering culture."
    )
}

def insert_test_job(job_id):
    item = {**TEST_JOB, 'id': job_id}
    jobs_table.put_item(Item=item)
    print(f"  [setup] Inserted test job: {job_id} (company: {TEST_JOB['company']})")

def poll_job(job_id, required_fields, label):
    print(f"  [wait]  Polling for {label}...")
    elapsed = 0
    while elapsed < MAX_WAIT:
        item = jobs_table.get_item(Key={'id': job_id}).get('Item', {})
        if all(item.get(f) for f in required_fields):
            return item
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        print(f"          {elapsed}s elapsed — fields so far: {[f for f in required_fields if item.get(f)]}")
    return None

def poll_company(company_name):
    print(f"  [wait]  Polling for company record: {company_name}...")
    elapsed = 0
    while elapsed < MAX_WAIT:
        item = companies_table.get_item(Key={'company_name': company_name}).get('Item', {})
        if item.get('summary'):  # partial stub exists before research completes, wait for summary
            return item
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        print(f"          {elapsed}s elapsed — not yet written")
    return None

def assert_job(item):
    errors = []

    # Summary (from job-summariser)
    summary = item.get('summary')
    if not summary or not isinstance(summary, dict):
        errors.append("summary is missing or not a dict")
    else:
        for key in ('job_title', 'job_summary', 'skill_requirements'):
            if not summary.get(key):
                errors.append(f"summary.{key} is missing")

    # Match (from cv-matcher)
    match_score = item.get('match_score')
    if match_score is None:
        errors.append("match_score is missing")
    else:
        try:
            if not (1 <= int(match_score) <= 10):
                errors.append(f"match_score out of range: {match_score}")
        except (ValueError, TypeError):
            errors.append(f"match_score is not a number: {match_score}")

    if not item.get('match_summary'):
        errors.append("match_summary is missing")

    return errors

def assert_company(item):
    errors = []
    if not item.get('summary'):
        errors.append("company.summary is missing")
    if not item.get('research_confidence'):
        errors.append("company.research_confidence is missing")
    if item.get('candidate_fit_score') is None:
        errors.append("company.candidate_fit_score is missing")
    return errors

def cleanup(job_id, company_name):
    jobs_table.delete_item(Key={'id': job_id})
    companies_table.delete_item(Key={'company_name': company_name})
    print(f"  [cleanup] Deleted test job and company record")

def run():
    job_id = f"test-{uuid.uuid4()}"
    company_name = TEST_JOB['company']
    print(f"\nRunning end-to-end pipeline test (job_id: {job_id})")
    print(f"Waiting up to {MAX_WAIT}s for pipeline to complete...\n")

    try:
        insert_test_job(job_id)

        # Poll for all job fields (summariser + matcher both need to complete)
        job_item = poll_job(
            job_id,
            required_fields=['summary', 'match_score', 'match_summary'],
            label='summary + match_score + match_summary'
        )

        # Poll for company record
        company_item = poll_company(company_name)

        errors = []

        if job_item is None:
            errors.append("Timed out waiting for job fields — pipeline may have stalled")
        else:
            errors += assert_job(job_item)

        if company_item is None:
            errors.append("Timed out waiting for company record — company-researcher may have stalled")
        else:
            errors += assert_company(company_item)

        if errors:
            print("\nFAIL")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            print(f"\n  job_title    : {job_item['summary'].get('job_title')}")
            print(f"  match_score  : {job_item['match_score']}")
            print(f"  match_summary: {str(job_item['match_summary'])[:120]}...")
            print(f"  company      : {company_item.get('company_name')} (fit: {company_item.get('candidate_fit_score')})")
            print("\nPASS")

    finally:
        cleanup(job_id, company_name)

if __name__ == '__main__':
    run()

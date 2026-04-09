"""
Integration test for the API Gateway + api Lambda.

Tests:
  1. GET /jobs — returns a list
  2. GET /jobs/{id} — returns job + company
  3. PUT /jobs/{id}/status — updates status field
  4. POST /jobs/{id}/resume — invokes resume-tailor (checks tailored_resume returned)
  5. POST /jobs/{id}/cover-letter — invokes cover-letter-generator (checks cover_letter returned)

Run from repo root:
  python3 tests/test_api.py

Requires:
  API_BASE env var or edit the constant below.
"""

import json
import os
import sys
import uuid
import boto3
import urllib.request
import urllib.error

API_BASE = os.environ.get('API_BASE', 'https://nvv4c6g5jl.execute-api.us-east-1.amazonaws.com/prod')
REGION = 'us-east-1'
JOBS_TABLE = 'jobs'

dynamodb = boto3.resource('dynamodb', region_name=REGION)
jobs_table = dynamodb.Table(JOBS_TABLE)

def load_fixture(filename):
    with open(f'tests/fixtures/{filename}') as f:
        return json.load(f)

def insert_test_job(job_id):
    item = {
        'id': job_id,
        'positionName': 'Junior Software Engineer',
        'company': 'Atlassian',
        'location': 'Remote',
        'source': 'test',
        'status': 'new',
        'description': 'Test job description.',
        'summary': load_fixture('sample_summary.json')
    }
    jobs_table.put_item(Item=item)
    print(f"  [setup] Inserted test job: {job_id}")

def http(method, path, body=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def cleanup(job_id):
    jobs_table.delete_item(Key={'id': job_id})
    print(f"  [cleanup] Deleted test job: {job_id}")

def run():
    job_id = f"test-{uuid.uuid4()}"
    errors = []
    print(f"\nRunning API integration test (job_id: {job_id})")
    print(f"  API_BASE: {API_BASE}\n")

    try:
        insert_test_job(job_id)

        # 1. GET /jobs
        print("  [test] GET /jobs")
        status, body = http('GET', '/jobs')
        if status != 200:
            errors.append(f"GET /jobs returned {status}")
        elif not isinstance(body, list):
            errors.append(f"GET /jobs did not return a list: {type(body)}")
        else:
            print(f"         OK — {len(body)} jobs returned")

        # 2. GET /jobs/{id}
        print(f"  [test] GET /jobs/{job_id}")
        status, body = http('GET', f'/jobs/{job_id}')
        if status != 200:
            errors.append(f"GET /jobs/{{id}} returned {status}: {body}")
        elif not body.get('job'):
            errors.append("GET /jobs/{id} response missing 'job' key")
        else:
            print(f"         OK — positionName: {body['job'].get('positionName')}")

        # 3. PUT /jobs/{id}/status
        print(f"  [test] PUT /jobs/{job_id}/status")
        status, body = http('PUT', f'/jobs/{job_id}/status', {'status': 'reviewed'})
        if status != 200:
            errors.append(f"PUT /jobs/{{id}}/status returned {status}: {body}")
        else:
            item = jobs_table.get_item(Key={'id': job_id}).get('Item', {})
            if item.get('status') != 'reviewed':
                errors.append(f"Status not updated in DynamoDB: {item.get('status')}")
            else:
                print(f"         OK — status updated to 'reviewed'")

        # 4. POST /jobs/{id}/resume
        print(f"  [test] POST /jobs/{job_id}/resume")
        status, body = http('POST', f'/jobs/{job_id}/resume')
        if status != 200:
            errors.append(f"POST /jobs/{{id}}/resume returned {status}: {body}")
        elif not body.get('tailored_resume'):
            errors.append("resume response missing 'tailored_resume'")
        else:
            print(f"         OK — tailored_resume length: {len(body['tailored_resume'])} chars")

        # 5. POST /jobs/{id}/cover-letter
        print(f"  [test] POST /jobs/{job_id}/cover-letter")
        status, body = http('POST', f'/jobs/{job_id}/cover-letter', {'mode': 'autonomous'})
        if status != 200:
            errors.append(f"POST /jobs/{{id}}/cover-letter returned {status}: {body}")
        elif not body.get('cover_letter'):
            errors.append("cover-letter response missing 'cover_letter'")
        else:
            print(f"         OK — cover_letter length: {len(body['cover_letter'])} chars")

    finally:
        cleanup(job_id)

    if errors:
        print("\nFAIL")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\nPASS")

if __name__ == '__main__':
    run()

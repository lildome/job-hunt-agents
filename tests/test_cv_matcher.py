"""
Integration test for cv-matcher Lambda.

What it does:
  1. Inserts a test job item with a pre-built summary into the jobs table
  2. Invokes cv-matcher Lambda synchronously
  3. Reads the item back from DynamoDB
  4. Asserts match_score (int 1-10) and match_summary (non-empty string) are written
  5. Cleans up the test item

Run from repo root:
  python tests/test_cv_matcher.py
"""

import json
import uuid
import sys
from decimal import Decimal
import boto3

REGION = 'us-east-1'
JOBS_TABLE = 'jobs'
FUNCTION_NAME = 'cv-matcher'

dynamodb = boto3.resource('dynamodb', region_name=REGION)
lambda_client = boto3.client('lambda', region_name=REGION)
jobs_table = dynamodb.Table(JOBS_TABLE)

def load_fixture(filename):
    with open(f'tests/fixtures/{filename}') as f:
        return json.load(f)

def insert_test_job(job_id):
    item = {
        'id': job_id,
        'positionName': 'Junior Software Engineer',
        'company': 'Test Corp',
        'location': 'Remote',
        'source': 'test',
        'status': 'new',
        'description': 'Test job description.',
        'summary': load_fixture('sample_summary.json')
    }
    jobs_table.put_item(Item=item)
    print(f"  [setup] Inserted test job: {job_id}")

def invoke_cv_matcher(job_id):
    payload = json.dumps({"job_id": job_id}).encode()
    response = lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType='RequestResponse',
        Payload=payload
    )
    result = json.loads(response['Payload'].read())
    if 'errorMessage' in result:
        raise RuntimeError(f"Lambda error: {result['errorMessage']}")
    return result

def assert_results(job_id):
    response = jobs_table.get_item(Key={'id': job_id})
    item = response.get('Item', {})

    errors = []

    match_score = item.get('match_score')
    if match_score is None:
        errors.append("match_score is missing")
    else:
        try:
            score = int(match_score)
            if not (1 <= score <= 10):
                errors.append(f"match_score out of range: {score}")
        except (ValueError, TypeError):
            errors.append(f"match_score is not a number: {match_score}")

    match_summary = item.get('match_summary')
    if not match_summary or not isinstance(match_summary, str):
        errors.append("match_summary is missing or empty")

    return errors, item

def cleanup(job_id):
    jobs_table.delete_item(Key={'id': job_id})
    print(f"  [cleanup] Deleted test job: {job_id}")

def run():
    job_id = f"test-{uuid.uuid4()}"
    print(f"\nRunning cv-matcher integration test (job_id: {job_id})")

    try:
        insert_test_job(job_id)

        print("  [invoke] Calling cv-matcher Lambda...")
        result = invoke_cv_matcher(job_id)
        print(f"  [invoke] Response: {result}")

        print("  [assert] Checking DynamoDB output...")
        errors, item = assert_results(job_id)

        if errors:
            print("\nFAIL")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            print(f"  match_score : {item['match_score']}")
            print(f"  match_summary: {item['match_summary'][:120]}...")
            print("\nPASS")

    finally:
        cleanup(job_id)

if __name__ == '__main__':
    run()

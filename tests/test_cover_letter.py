"""
Integration test for cover-letter-generator Lambda (autonomous mode).

What it does:
  1. Inserts a test job with a pre-built summary into the jobs table
  2. Invokes cover-letter-generator Lambda in autonomous mode
  3. Asserts cover_letter is written to jobs table and is non-empty
  4. Asserts critique is returned in the Lambda response
  5. Cleans up the test item

Run from repo root:
  python3 tests/test_cover_letter.py
"""

import json
import uuid
import sys
import boto3

REGION = 'us-east-1'
JOBS_TABLE = 'jobs'
FUNCTION_NAME = 'cover-letter-generator'

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
        'company': 'Atlassian',
        'location': 'Remote',
        'source': 'test',
        'status': 'new',
        'description': 'Test job description.',
        'summary': load_fixture('sample_summary.json')
    }
    jobs_table.put_item(Item=item)
    print(f"  [setup] Inserted test job: {job_id}")

def invoke_cover_letter(job_id):
    payload = json.dumps({"job_id": job_id, "mode": "autonomous"}).encode()
    response = lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType='RequestResponse',
        Payload=payload
    )
    result = json.loads(response['Payload'].read())
    if 'errorMessage' in result:
        raise RuntimeError(f"Lambda error: {result['errorMessage']}")
    return result

def assert_results(job_id, result):
    errors = []

    # Check Lambda response fields
    if not result.get('cover_letter'):
        errors.append("cover_letter missing from Lambda response")
    if not result.get('critique'):
        errors.append("critique missing from Lambda response")

    # Check DynamoDB write
    item = jobs_table.get_item(Key={'id': job_id}).get('Item', {})
    cover_letter = item.get('cover_letter')
    if not cover_letter or not isinstance(cover_letter, str):
        errors.append("cover_letter is missing or empty in DynamoDB")
    elif len(cover_letter) < 100:
        errors.append(f"cover_letter suspiciously short ({len(cover_letter)} chars)")

    return errors, item

def cleanup(job_id):
    jobs_table.delete_item(Key={'id': job_id})
    print(f"  [cleanup] Deleted test job: {job_id}")

def run():
    job_id = f"test-{uuid.uuid4()}"
    print(f"\nRunning cover-letter-generator integration test (job_id: {job_id})")

    try:
        insert_test_job(job_id)

        print("  [invoke] Calling cover-letter-generator Lambda (autonomous mode)...")
        result = invoke_cover_letter(job_id)
        print(f"  [invoke] Response received (cover_letter length: {len(result.get('cover_letter', ''))} chars)")

        print("  [assert] Checking output...")
        errors, item = assert_results(job_id, result)

        if errors:
            print("\nFAIL")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            preview = item['cover_letter'][:300].replace('\n', ' ')
            print(f"  preview: {preview}...")
            print(f"  critique: {str(result.get('critique', ''))[:150]}...")
            print("\nPASS")

    finally:
        cleanup(job_id)

if __name__ == '__main__':
    run()

"""
Integration test for resume-tailor Lambda.

What it does:
  1. Inserts a test job with a pre-built summary into the jobs table
  2. Invokes resume-tailor Lambda synchronously
  3. Asserts tailored_resume is written to jobs table and contains expected sections
  4. Cleans up the test item

Run from repo root:
  python3 tests/test_resume_tailor.py
"""

import json
import uuid
import sys
import boto3

REGION = 'us-east-1'
JOBS_TABLE = 'jobs'
FUNCTION_NAME = 'resume-tailor'

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

def invoke_resume_tailor(job_id):
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
    item = jobs_table.get_item(Key={'id': job_id}).get('Item', {})
    errors = []

    resume = item.get('tailored_resume')
    if not resume or not isinstance(resume, str):
        errors.append("tailored_resume is missing or empty")
        return errors, item

    # Check expected markdown sections are present
    for section in ('## Summary', '## Experience', '## Skills', '## Education'):
        if section not in resume:
            errors.append(f"tailored_resume missing section: {section}")

    # Should be grounded in real CV content
    if 'JVN Communications' not in resume and 'Rowan University' not in resume:
        errors.append("tailored_resume does not appear to reference CV content")

    return errors, item

def cleanup(job_id):
    jobs_table.delete_item(Key={'id': job_id})
    print(f"  [cleanup] Deleted test job: {job_id}")

def run():
    job_id = f"test-{uuid.uuid4()}"
    print(f"\nRunning resume-tailor integration test (job_id: {job_id})")

    try:
        insert_test_job(job_id)

        print("  [invoke] Calling resume-tailor Lambda...")
        result = invoke_resume_tailor(job_id)
        print(f"  [invoke] Response received (resume length: {len(result.get('tailored_resume', ''))} chars)")

        print("  [assert] Checking output...")
        errors, item = assert_results(job_id)

        if errors:
            print("\nFAIL")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            # Print first 300 chars of resume as a sanity check
            preview = item['tailored_resume'][:300].replace('\n', ' ')
            print(f"  preview: {preview}...")
            print("\nPASS")

    finally:
        cleanup(job_id)

if __name__ == '__main__':
    run()

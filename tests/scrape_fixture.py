"""
One-time fixture generator — fetches 5 Melbourne Software Engineer jobs from Apify
and saves them to tests/fixtures/melbourne_se_jobs.json.

Run from repo root:
  python3 tests/scrape_fixture.py
"""

import json
import uuid
import boto3
from apify_client import ApifyClient

FIXTURE_PATH = 'tests/fixtures/melbourne_se_jobs.json'
ACTOR_ID = 'hMvNSpz3JnHgl5jkh'
KEYS_TO_EXTRACT = ['salary', 'positionName', 'company', 'location', 'url',
                   'scrapedAt', 'postingDateParsed', 'description']

def get_parameter(name):
    ssm = boto3.client('ssm', region_name='us-east-1')
    return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

def main():
    print("Fetching Apify API key from SSM...")
    apify_token = get_parameter('apify-api-key')
    client = ApifyClient(apify_token)

    run_input = {
        "position": "Software Engineer",
        "location": "Melbourne",
        "country": "AU",
        "maxItemsPerSearch": 5
    }
    print(f"Running Apify actor with: {run_input}")
    run = client.actor(ACTOR_ID).call(run_input=run_input)

    jobs = []
    for listing in client.dataset(run['defaultDatasetId']).iterate_items():
        job = {key: listing.get(key) for key in KEYS_TO_EXTRACT}
        job['postingDate'] = job.pop('postingDateParsed', None)
        job['id'] = str(uuid.uuid4())
        job['status'] = 'new'
        job['source'] = 'indeed'
        jobs.append(job)

    with open(FIXTURE_PATH, 'w') as f:
        json.dump(jobs, f, indent=2, default=str)

    print(f"\nSaved {len(jobs)} jobs to {FIXTURE_PATH}:")
    for i, j in enumerate(jobs):
        print(f"  [{i}] {j.get('positionName')} @ {j.get('company')} ({j.get('location')})")

if __name__ == '__main__':
    main()

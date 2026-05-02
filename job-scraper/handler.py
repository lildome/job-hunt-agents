import json
import logging
import boto3
from apify_client import ApifyClient
from scrapers.indeed_scraper import scrape_indeed
from scrapers.linkedin_scraper import scrape_linkedin

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('jobs')

ssm_client = boto3.client('ssm', region_name='us-east-1')

_apify_client = None

def get_apify_client():
    global _apify_client
    if _apify_client is None:
        token = ssm_client.get_parameter(Name='apify-api-key')['Parameter']['Value']
        _apify_client = ApifyClient(token)
    return _apify_client

def lambda_handler(event, context):
    client = get_apify_client()

    job_board = event.get('job_board')

    if job_board == 'indeed':
        result = scrape_indeed(client, event['run_input'])
    elif job_board == 'linkedin':
        result = scrape_linkedin(client, event['search_params'], event.get('count', 50))
    else:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': f'Unknown or missing job_board: {job_board}'})
        }

    for item in result:
        try:
            table.put_item(Item=item)
            item['dbInsertSuccess'] = True
        except Exception as e:
            logger.error(f"Error inserting item into DynamoDB: {e}")
            item['dbInsertSuccess'] = False
            continue

    return {
        'statusCode': 200,
        'body': json.dumps(result)
    }

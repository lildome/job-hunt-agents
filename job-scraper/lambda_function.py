import json
import boto3
from apify_client import ApifyClient
from scrapers.indeed_scraper import scrape_indeed

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('jobs')

ssm_client = boto3.client('ssm', region_name='us-east-1')

apify_token = None

try:
    apify_token = ssm_client.get_parameter(Name='apify-api-key')['Parameter']['Value']
except Exception as e:
    print(f"Error retrieving Apify API key from SSM: {e}")
    apify_token = None
    exit(1)

client = ApifyClient(apify_token)

def lambda_handler(event, context):

    result = {}

    if event['job_board'] == 'indeed':
        run_input = event['run_input']
        result = scrape_indeed(client, run_input)

    for item in result:
        try:
            table.put_item(Item=item)
            item['dbInsertSuccess'] = True
        except Exception as e:
            print(f"Error inserting item into DynamoDB: {e}")
            item['dbInsertSuccess'] = False
            continue

    return {
        'statusCode': 200,
        'body': json.dumps(result)
    }

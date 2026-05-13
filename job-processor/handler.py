import json
import logging
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Default read timeout is 60s — cv-matcher can take 65s+ when rate limited
lambda_client = boto3.client('lambda', region_name='us-east-1',
                             config=Config(read_timeout=300, connect_timeout=10))
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')


def claim_job(job_id):
    """Atomically transition analysis.status to 'summarising' if pipeline hasn't started.
    Returns False if the job is already being processed or has been processed."""
    try:
        jobs_table.update_item(
            Key={'id': job_id},
            UpdateExpression='SET analysis = :start',
            ConditionExpression='attribute_not_exists(analysis) OR analysis.#s = :pending OR analysis.#s = :failed',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':start': {'status': 'summarising'},
                ':pending': 'pending',
                ':failed': 'failed',
            }
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise

def invoke(function_name, payload):
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload).encode()
    )
    result = json.loads(response['Payload'].read())
    if result and 'errorMessage' in result:
        raise RuntimeError(f"{function_name} failed: {result['errorMessage']}")
    return result


def lambda_handler(event, context):
    for sqs_record in event['Records']:
        body = json.loads(sqs_record['body'])
        job_id = body['job_id']

        if not claim_job(job_id):
            logger.info(f"Skipping job {job_id} — already claimed or complete")
            continue

        logger.info(f"Processing job_id: {job_id}")

        try:
            # Step 1: Summarise
            logger.info(f"Step 1/3: Summarising job {job_id}")
            invoke('job-summariser', {'job_id': job_id})

            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET analysis.status = :update',
                ExpressionAttributeValues={':update': "researching"}
            )

            # Step 2: Company research
            logger.info(f"Step 2/3: Researching company for job {job_id}")
            invoke('company-researcher', {'job_id': job_id})

            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET analysis.status = :update',
                ExpressionAttributeValues={':update': "matching"}
            )

            # Step 3: CV match
            logger.info(f"Step 3/3: Matching CV for job {job_id}")
            invoke('cv-matcher', {'job_id': job_id})

            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET analysis.status = :update',
                ExpressionAttributeValues={':update': "complete"}
            )
        except Exception as e:
            error_msg = str(e)[:500]
            logger.error(f"Error processing job {job_id}: {error_msg}")
            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET analysis.#s = :status, analysis.#e = :error',
                ExpressionAttributeNames={'#s': 'status', '#e': 'error'},
                ExpressionAttributeValues={':status': 'failed', ':error': error_msg}
            )

        logger.info(f"Processing complete for job_id: {job_id}")

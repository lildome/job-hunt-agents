import json
import logging
import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
deserializer = TypeDeserializer()


def deserialize_item(dynamo_item):
    return {k: deserializer.deserialize(v) for k, v in dynamo_item.items()}


def claim_job(job_id):
    """Atomically transition status 'new' → 'processing'. Returns False if already claimed."""
    try:
        jobs_table.update_item(
            Key={'id': job_id},
            UpdateExpression='SET #s = :processing',
            ConditionExpression='#s = :new',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':processing': 'processing', ':new': 'new'}
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise


def complete_job(job_id):
    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression='SET #s = :complete',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':complete': 'complete'}
    )


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
        records = json.loads(sqs_record['body'])
        if not isinstance(records, list):
            records = [records]

        for record in records:
            if record.get('eventName') != 'INSERT':
                continue

            job = deserialize_item(record['dynamodb']['NewImage'])
            job_id = job['id']

            if not claim_job(job_id):
                logger.info(f"Skipping job {job_id} — already claimed or not a real job")
                continue

            logger.info(f"Processing job_id: {job_id} ({job.get('company')} — {job.get('positionName')})")

            # Step 1: Summarise
            logger.info(f"Step 1/3: Summarising job {job_id}")
            invoke('job-summariser', records)

            # Step 2: Company research
            logger.info(f"Step 2/3: Researching company for job {job_id}")
            invoke('company-researcher', records)

            # Step 3: CV match
            logger.info(f"Step 3/3: Matching CV for job {job_id}")
            invoke('cv-matcher', {'job_id': job_id})

            complete_job(job_id)
            logger.info(f"Processing complete for job_id: {job_id}")

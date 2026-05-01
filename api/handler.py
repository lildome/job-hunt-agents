import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PIN = "Job Hunt PIN 159075"
S3_BUCKET = "dprofico-job-hunt-artifacts"
STALE_THRESHOLD = timedelta(minutes=5)

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
lambda_client = boto3.client('lambda', region_name='us-east-1')
s3_client = boto3.client('s3', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
companies_table = dynamodb.Table('companies')
sessions_table = dynamodb.Table('sessions')
profiles_table = dynamodb.Table('candidate_profiles')

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,X-Session-Token',
    'Access-Control-Allow-Methods': 'GET,PUT,POST,OPTIONS',
    'Content-Type': 'application/json'
}

def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': CORS_HEADERS,
        'body': json.dumps(body, default=str)
    }

def validate_token(headers):
    token = (headers or {}).get('X-Session-Token') or (headers or {}).get('x-session-token')
    if not token:
        return 'absent'
    item = sessions_table.get_item(Key={'token': token}).get('Item')
    if not item or int(item.get('expires_at', 0)) <= int(time.time()):
        return 'invalid'
    return 'valid'

def post_auth(body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON'})

    pin = data.get('pin', '')
    if not secrets.compare_digest(pin, PIN):
        return response(401, {'error': 'Invalid PIN'})

    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + 28800  # 8 hours
    sessions_table.put_item(Item={'token': token, 'expires_at': expires_at})
    logger.info("New session created")
    return response(200, {'token': token})

def _build_generation_status(job: dict, prefix: str) -> dict:
    status = job.get(f'{prefix}_status', 'none')
    started_at = job.get(f'{prefix}_started_at')
    error = job.get(f'{prefix}_error')
    pdf_key = job.get(f'{prefix}_pdf_key')

    if status == 'generating' and started_at:
        try:
            started = datetime.fromisoformat(started_at)
            if datetime.now(timezone.utc) - started > STALE_THRESHOLD:
                status = 'failed'
                error = error or 'Generation appears to have timed out'
        except ValueError:
            pass

    pdf_url = None
    if status == 'done' and pdf_key:
        pdf_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': pdf_key},
            ExpiresIn=86400,
        )

    return {
        'status': status,
        'pdf_url': pdf_url,
        'started_at': started_at,
        'error': error if status == 'failed' else None,
    }


def invoke_lambda(function_name, payload):
    result = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload).encode()
    )
    return json.loads(result['Payload'].read())

def get_jobs(event, auth):
    if auth == 'invalid':
        return response(401, {'error': 'Unauthorized'})

    params = event.get('queryStringParameters') or {}
    status_filter = params.get('status')
    min_score = params.get('min_score')

    result = jobs_table.scan()
    items = result.get('Items', [])

    if status_filter:
        items = [i for i in items if i.get('status') == status_filter]

    if min_score and auth == 'valid':
        try:
            threshold = int(min_score)
            items = [i for i in items if int(i.get('match_score', 0)) >= threshold]
        except (ValueError, TypeError):
            pass

    jobs = [
        {
            'id': i.get('id'),
            'positionName': i.get('positionName'),
            'company': i.get('company'),
            'location': i.get('location'),
            'match_score': i.get('match_score'),
            'status': i.get('status'),
            'scrapedAt': i.get('scrapedAt'),
            'salary': i.get('summary', {}).get('salary') if isinstance(i.get('summary'), dict) else None,
        }
        for i in items
    ]
    jobs.sort(key=lambda x: int(x.get('match_score') or 0), reverse=True)

    if auth == 'absent':
        for job in jobs:
            job.pop('match_score', None)
            job.pop('match_summary', None)

    return response(200, jobs)

def get_job(job_id, auth):
    if auth == 'invalid':
        return response(401, {'error': 'Unauthorized'})

    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    company_name = job.get('company', '')
    canonical_name = job.get('canonical_company_name', company_name)
    company = companies_table.get_item(Key={'company_name': canonical_name}).get('Item', {})

    if auth == 'absent':
        for field in ('match_score', 'match_summary', 'tailored_resume', 'cover_letter'):
            job.pop(field, None)
        for field in ('candidate_fit_score', 'candidate_fit_reasoning'):
            company.pop(field, None)

    return response(200, {
        'job': job,
        'company': company,
        'resume': _build_generation_status(job, 'tailored_resume'),
        'cover_letter': _build_generation_status(job, 'cover_letter'),
    })

def update_job_status(job_id, body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    new_status = data.get('status')
    if not new_status:
        return response(400, {'error': 'status field required'})

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression='SET #s = :s',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': new_status}
    )
    return response(200, {'job_id': job_id, 'status': new_status})

def start_scrape(body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    position = data.get('position', '').strip()
    if not position:
        return response(400, {'error': 'position is required'})

    run_input = {'position': position}
    if data.get('location'):
        run_input['location'] = data['location'].strip()
    if data.get('country'):
        run_input['country'] = data['country'].strip()
    if data.get('maxItemsPerSearch'):
        try:
            run_input['maxItemsPerSearch'] = int(data['maxItemsPerSearch'])
        except (ValueError, TypeError):
            return response(400, {'error': 'maxItemsPerSearch must be an integer'})

    # Fire-and-forget — scraping takes minutes
    lambda_client.invoke(
        FunctionName='job-scraper',
        InvocationType='Event',
        Payload=json.dumps({'job_board': 'indeed', 'run_input': run_input}).encode()
    )
    logger.info(f"Scrape triggered: {run_input}")
    return response(202, {'message': 'Scrape started', 'run_input': run_input})

def tailor_resume(job_id):
    lambda_client.invoke(
        FunctionName='resume-tailor',
        InvocationType='Event',
        Payload=json.dumps({'job_id': job_id}).encode()
    )
    return response(202, {'status': 'generating'})

def generate_cover_letter(job_id, body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    mode = data.get('mode', 'generate')
    feedback = data.get('feedback')

    if mode not in ('generate', 'revise'):
        return response(400, {
            'error': f"Invalid mode '{mode}'. Expected 'generate' or 'revise'."
        })

    if mode == 'revise' and (not feedback or not feedback.strip()):
        return response(400, {
            'error': "Mode 'revise' requires non-empty 'feedback' field"
        })

    payload = {'job_id': job_id, 'mode': mode}
    if feedback:
        payload['feedback'] = feedback

    lambda_client.invoke(
        FunctionName='cover-letter-generator',
        InvocationType='Event',
        Payload=json.dumps(payload).encode()
    )
    return response(202, {'status': 'generating'})

def get_profile():
    try:
        item = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item')
        if not item:
            return response(404, {'error': 'Profile not found'})
        item.pop('profile_id', None)
        item.pop('preferences', None)
        return response(200, item)
    except Exception as e:
        logger.error(f"get_profile error: {e}")
        return response(500, {'error': 'Internal server error'})


def get_generation_status(job_id):
    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    return response(200, {
        'resume': _build_generation_status(job, 'tailored_resume'),
        'cover_letter': _build_generation_status(job, 'cover_letter'),
    })


def lambda_handler(event, context):
    method = event.get('httpMethod', '')
    path = event.get('path', '')
    body = event.get('body')
    headers = event.get('headers') or {}

    logger.info(f"{method} {path}")

    # OPTIONS preflight — no auth required
    if method == 'OPTIONS':
        return response(200, {})

    # POST /auth — no auth required
    if method == 'POST' and path == '/auth':
        return post_auth(body)

    auth = validate_token(headers)

    # GET /jobs
    if method == 'GET' and path == '/jobs':
        return get_jobs(event, auth)

    # GET /jobs/{id}
    m = re.match(r'^/jobs/([^/]+)$', path)
    if m and method == 'GET':
        return get_job(m.group(1), auth)

    # GET /jobs/{id}/generation-status — no auth required
    m = re.match(r'^/jobs/([^/]+)/generation-status$', path)
    if m and method == 'GET':
        return get_generation_status(m.group(1))

    # GET /profile — no auth required
    if method == 'GET' and path == '/profile':
        return get_profile()

    # Write/action routes — require valid token
    if auth != 'valid':
        return response(401, {'error': 'Unauthorized'})

    # PUT /jobs/{id}/status
    m = re.match(r'^/jobs/([^/]+)/status$', path)
    if m and method == 'PUT':
        return update_job_status(m.group(1), body)

    # POST /jobs/{id}/resume
    m = re.match(r'^/jobs/([^/]+)/resume$', path)
    if m and method == 'POST':
        return tailor_resume(m.group(1))

    # POST /jobs/{id}/cover-letter
    m = re.match(r'^/jobs/([^/]+)/cover-letter$', path)
    if m and method == 'POST':
        return generate_cover_letter(m.group(1), body)

    # POST /scrape
    if method == 'POST' and path == '/scrape':
        return start_scrape(body)

    return response(404, {'error': f'Route not found: {method} {path}'})

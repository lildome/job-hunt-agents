import json
import logging
import re
import secrets
import time
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PIN = "Job Hunt PIN 159075"

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
lambda_client = boto3.client('lambda', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
companies_table = dynamodb.Table('companies')
sessions_table = dynamodb.Table('sessions')

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
        return False
    item = sessions_table.get_item(Key={'token': token}).get('Item')
    if not item:
        return False
    return int(item.get('expires_at', 0)) > int(time.time())

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

def invoke_lambda(function_name, payload):
    result = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload).encode()
    )
    return json.loads(result['Payload'].read())

def get_jobs(event):
    params = event.get('queryStringParameters') or {}
    status_filter = params.get('status')
    min_score = params.get('min_score')

    result = jobs_table.scan()
    items = result.get('Items', [])

    if status_filter:
        items = [i for i in items if i.get('status') == status_filter]

    if min_score:
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
    return response(200, jobs)

def get_job(job_id):
    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    company_name = job.get('company', '')
    company = companies_table.get_item(Key={'company_name': company_name}).get('Item', {})

    return response(200, {'job': job, 'company': company})

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
    result = invoke_lambda('resume-tailor', {'job_id': job_id})
    if 'errorMessage' in result:
        return response(500, {'error': result['errorMessage']})
    return response(200, result)

def generate_cover_letter(job_id, body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    payload = {
        'job_id': job_id,
        'mode': data.get('mode', 'autonomous'),
        'feedback': data.get('feedback'),
        'conversation_history': data.get('conversation_history', [])
    }
    result = invoke_lambda('cover-letter-generator', payload)
    if 'errorMessage' in result:
        return response(500, {'error': result['errorMessage']})
    return response(200, result)

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

    # All other routes require a valid session token
    if not validate_token(headers):
        return response(401, {'error': 'Unauthorized'})

    # GET /jobs
    if method == 'GET' and path == '/jobs':
        return get_jobs(event)

    # GET /jobs/{id}
    m = re.match(r'^/jobs/([^/]+)$', path)
    if m and method == 'GET':
        return get_job(m.group(1))

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

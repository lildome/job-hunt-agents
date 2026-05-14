import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PIN = "Job Hunt PIN 159075"
S3_BUCKET = "dprofico-job-hunt-artifacts"
STALE_THRESHOLD = timedelta(minutes=5)

JOB_PROCESSING_QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/052732928292/job-processing-queue'
JOB_URL_INGESTION_QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/052732928292/job-url-ingestion-queue'
JOB_SCREENING_QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/052732928292/job-screening-queue'

LINKEDIN_REMOTE_VALUES = {'onsite', 'remote', 'hybrid'}
LINKEDIN_POSTED_WITHIN_VALUES = {'day', '3days', 'week', 'month'}

VALID_STATUSES = {'new', 'applied', 'interviewing', 'offer', 'rejected'}
APPLIED_STATUSES = ['applied', 'interviewing', 'offer', 'rejected']
VALID_BUCKETS = {'screened', 'analysed', 'applied', 'archive'}

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
lambda_client = boto3.client('lambda', region_name='us-east-1')
s3_client = boto3.client('s3', region_name='us-east-1')
sqs = boto3.client('sqs', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
companies_table = dynamodb.Table('companies')
sessions_table = dynamodb.Table('sessions')
profiles_table = dynamodb.Table('candidate_profiles')

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,X-Session-Token',
    'Access-Control-Allow-Methods': 'GET,PUT,POST,DELETE,OPTIONS',
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
        cached_url = job.get(f'{prefix}_pdf_url_cached')
        cached_at_str = job.get(f'{prefix}_pdf_url_generated_at')
        use_cache = False
        if cached_url and cached_at_str:
            try:
                cached_at = datetime.fromisoformat(cached_at_str)
                if datetime.now(timezone.utc) - cached_at < timedelta(hours=12):
                    use_cache = True
            except (ValueError, TypeError):
                pass

        if use_cache:
            pdf_url = cached_url
        else:
            pdf_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': pdf_key},
                ExpiresIn=86400,
            )
            jobs_table.update_item(
                Key={'id': job['id']},
                UpdateExpression='SET #u = :u, #g = :g',
                ExpressionAttributeNames={
                    '#u': f'{prefix}_pdf_url_cached',
                    '#g': f'{prefix}_pdf_url_generated_at',
                },
                ExpressionAttributeValues={
                    ':u': pdf_url,
                    ':g': datetime.now(timezone.utc).isoformat(),
                },
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

def _bucket_filter_expr(bucket):
    not_archived = Attr('archived_at').not_exists()
    not_failed = Attr('ingestion_status').not_exists() | Attr('ingestion_status').ne('failed')

    if bucket == 'screened':
        return (
            not_archived & not_failed &
            (Attr('analysis.status').not_exists() | Attr('analysis.status').ne('complete'))
        )
    if bucket == 'analysed':
        return (
            not_archived & not_failed &
            Attr('analysis.status').exists() &
            Attr('analysis.status').eq('complete') &
            ~Attr('status').is_in(APPLIED_STATUSES)
        )
    if bucket == 'applied':
        return (
            not_archived & not_failed &
            Attr('status').is_in(APPLIED_STATUSES)
        )
    if bucket == 'archive':
        return Attr('archived_at').exists() & not_failed


def _scan_all(filter_expr, select=None):
    kwargs = {'FilterExpression': filter_expr}
    if select:
        kwargs['Select'] = select
    result = jobs_table.scan(**kwargs)
    if select == 'COUNT':
        total = result.get('Count', 0)
        while 'LastEvaluatedKey' in result:
            result = jobs_table.scan(**kwargs, ExclusiveStartKey=result['LastEvaluatedKey'])
            total += result.get('Count', 0)
        return total
    items = result.get('Items', [])
    while 'LastEvaluatedKey' in result:
        result = jobs_table.scan(**kwargs, ExclusiveStartKey=result['LastEvaluatedKey'])
        items.extend(result.get('Items', []))
    return items


def _attach_company_info(items):
    company_ids = list({j['company_id'] for j in items if j.get('company_id')})
    id_to_items = {}
    for i in range(0, len(company_ids), 100):
        batch = company_ids[i:i + 100]
        resp = dynamodb.batch_get_item(
            RequestItems={
                'companies': {
                    'Keys': [{'id': cid} for cid in batch],
                    'ProjectionExpression': 'id, canonical_name, candidate_fit_score',
                }
            }
        )
        for item in resp.get('Responses', {}).get('companies', []):
            id_to_items[item['id']] = (item.get('canonical_name'), item.get('candidate_fit_score', None))
    for j in items:
        j['canonical_name'] = id_to_items.get(j.get('company_id'), (None, None))[0]
        j['candidate_fit_score'] = id_to_items.get(j.get('company_id'), (None, None))[1]


def get_jobs(event, auth):
    if auth == 'invalid':
        return response(401, {'error': 'Unauthorized'})

    params = event.get('queryStringParameters') or {}
    bucket = params.get('bucket')

    if not bucket:
        return response(400, {'error': "bucket query parameter is required"})
    if bucket not in VALID_BUCKETS:
        return response(400, {'error': f"Invalid bucket '{bucket}'. Must be one of: screened, analysed, applied, archive"})

    items = _scan_all(_bucket_filter_expr(bucket))

    for item in items:
        item.pop('description', None)
        if auth == 'absent':
            item.pop('analysis', None)
            item.pop('screening', None)
            for key in list(item.keys()):
                if key.startswith('tailored_resume_') or key.startswith('cover_letter_'):
                    del item[key]

    _attach_company_info(items)

    if auth == 'absent':
        for item in items:
            item.pop('candidate_fit_score', None)

    counts = {b: _scan_all(_bucket_filter_expr(b), select='COUNT') for b in VALID_BUCKETS}

    return response(200, {'jobs': items, 'counts': counts})

def get_job(job_id, auth):
    if auth == 'invalid':
        return response(401, {'error': 'Unauthorized'})

    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    company_id = job.get('company_id')
    company = {}
    if company_id:
        company = companies_table.get_item(Key={'id': company_id}).get('Item', {})

    if auth == 'absent':
        job.pop('analysis', None)
        for key in list(job.keys()):
            if key.startswith('tailored_resume_') or key.startswith('cover_letter_'):
                del job[key]
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

    if new_status == 'archived':
        return response(400, {'error': "Use POST /jobs/{id}/archive to archive a job"})

    if new_status not in VALID_STATUSES:
        return response(400, {'error': f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"})

    now = datetime.now(timezone.utc).isoformat()
    update_expr = 'SET #s = :s, #sc = :sc'
    expr_names = {'#s': 'status', '#sc': 'status_changed_at'}
    expr_values = {':s': new_status, ':sc': now}

    if new_status == 'rejected':
        update_expr += ', #aa = :aa'
        expr_names['#aa'] = 'archived_at'
        expr_values[':aa'] = now

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
    return response(200, {'job_id': job_id, 'status': new_status})


def archive_job(job_id):
    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression='SET archived_at = :t',
        ExpressionAttributeValues={':t': datetime.now(timezone.utc).isoformat()},
    )
    return response(200, {'job_id': job_id})


def restore_job(job_id):
    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    jobs_table.update_item(
        Key={'id': job_id},
        UpdateExpression='REMOVE archived_at',
    )
    return response(200, {'job_id': job_id})

def start_indeed_scrape(body):
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

    lambda_client.invoke(
        FunctionName='job-scraper',
        InvocationType='Event',
        Payload=json.dumps({'job_board': 'indeed', 'run_input': run_input}).encode()
    )
    logger.info(f"Indeed scrape triggered: {run_input}")
    return response(202, {'message': 'Scrape started', 'run_input': run_input})

def start_linkedin_scrape(body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    keywords = data.get('keywords', '').strip()
    if not keywords:
        return response(400, {'error': 'keywords is required'})

    location = data.get('location', '').strip()
    if not location:
        return response(400, {'error': 'location is required'})

    search_params = {'keywords': keywords, 'location': location}

    if 'remote' in data:
        if data['remote'] not in LINKEDIN_REMOTE_VALUES:
            return response(400, {'error': f"Invalid 'remote' value. Allowed: {sorted(LINKEDIN_REMOTE_VALUES)}"})
        search_params['remote'] = data['remote']

    if 'posted_within' in data:
        if data['posted_within'] not in LINKEDIN_POSTED_WITHIN_VALUES:
            return response(400, {'error': f"Invalid 'posted_within' value. Allowed: {sorted(LINKEDIN_POSTED_WITHIN_VALUES)}"})
        search_params['posted_within'] = data['posted_within']

    count = 50
    if 'count' in data:
        try:
            count = int(data['count'])
        except (ValueError, TypeError):
            return response(400, {'error': 'count must be an integer'})

    lambda_client.invoke(
        FunctionName='job-scraper',
        InvocationType='Event',
        Payload=json.dumps({
            'job_board': 'linkedin',
            'search_params': search_params,
            'count': count,
        }).encode()
    )
    logger.info(f"LinkedIn scrape triggered: {search_params} count={count}")
    return response(202, {'message': 'Scrape started', 'search_params': search_params, 'count': count})

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


def _filename_from_key(key: str) -> str:
    # Keys are {prefix}/{job_id}/{timestamp}-{filename}; strip the timestamp prefix
    basename = key.split('/')[-1]
    return basename.split('-', 1)[-1]


def get_resume_download(job_id):
    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    pdf_key = job.get('tailored_resume_pdf_key')
    if not pdf_key:
        return response(404, {'error': 'Resume PDF not yet generated'})

    filename = _filename_from_key(pdf_key)
    url = s3_client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': S3_BUCKET,
            'Key': pdf_key,
            'ResponseContentDisposition': f'attachment; filename="{filename}"',
        },
        ExpiresIn=300,
    )
    return response(200, {'url': url})


def get_cover_letter_download(job_id):
    job = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not job:
        return response(404, {'error': f'Job {job_id} not found'})

    pdf_key = job.get('cover_letter_pdf_key')
    if not pdf_key:
        return response(404, {'error': 'Cover letter PDF not yet generated'})

    filename = _filename_from_key(pdf_key)
    url = s3_client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': S3_BUCKET,
            'Key': pdf_key,
            'ResponseContentDisposition': f'attachment; filename="{filename}"',
        },
        ExpiresIn=300,
    )
    return response(200, {'url': url})


def trigger_full_analysis(body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) == 0:
        return response(400, {'error': "'ids' must be a non-empty list"})

    accepted = []
    rejected = []
    any_enqueue_succeeded = False

    for job_id in ids:
        item = jobs_table.get_item(Key={'id': job_id}).get('Item')

        if not item:
            logger.info(f"full-analysis: {job_id} not found")
            rejected.append({'job_id': job_id, 'reason': 'not_found'})
            continue

        analysis_status = (item.get('analysis') or {}).get('status')

        if analysis_status in ('summarising', 'researching', 'matching'):
            logger.info(f"full-analysis: {job_id} in_progress ({analysis_status})")
            rejected.append({'job_id': job_id, 'reason': 'in_progress', 'current_status': analysis_status})
            continue

        if analysis_status == 'complete':
            logger.info(f"full-analysis: {job_id} already_complete")
            rejected.append({'job_id': job_id, 'reason': 'already_complete'})
            continue

        # analysis missing, pending, or failed — accept and enqueue
        try:
            sqs.send_message(
                QueueUrl=JOB_PROCESSING_QUEUE_URL,
                MessageBody=json.dumps({'job_id': job_id}),
            )
            accepted.append(job_id)
            any_enqueue_succeeded = True
        except Exception as e:
            logger.error(f"full-analysis: failed to enqueue {job_id}: {e}")
            rejected.append({'job_id': job_id, 'reason': 'enqueue_failed'})

    all_failed = len(accepted) == 0 and any(r['reason'] == 'enqueue_failed' for r in rejected)
    status_code = 500 if all_failed else 200
    return response(status_code, {'accepted': accepted, 'rejected': rejected})


def _dispatch_retry(job):
    """Determine the retry action for a job and execute it.
    Returns ('accepted', None) or ('rejected', reason_string).
    """
    job_id = job['id']

    # Path 1: failed ingestion — delete record, re-enqueue URL
    if job.get('ingestion_status') == 'failed':
        url = job.get('url')
        if not url:
            return ('rejected', 'missing_url')
        try:
            jobs_table.delete_item(Key={'id': job_id})
        except Exception as e:
            logger.error(f"retry: delete failed for ingestion {job_id}: {e}")
            return ('rejected', 'delete_failed')
        try:
            sqs.send_message(
                QueueUrl=JOB_URL_INGESTION_QUEUE_URL,
                MessageBody=json.dumps({'url': url}),
            )
        except Exception as e:
            logger.error(f"retry: enqueue failed for ingestion {job_id} (record already deleted): {e}")
            return ('rejected', 'enqueue_failed')
        return ('accepted', None)

    analysis_status = (job.get('analysis') or {}).get('status')
    has_screening = bool(job.get('screening'))

    # Path 2: no screening block — re-trigger screening
    if not has_screening:
        try:
            sqs.send_message(
                QueueUrl=JOB_SCREENING_QUEUE_URL,
                MessageBody=json.dumps({'job_id': job_id}),
            )
        except Exception as e:
            logger.error(f"retry: enqueue failed for screening {job_id}: {e}")
            return ('rejected', 'enqueue_failed')
        return ('accepted', None)

    # Path 3: screened, no analysis attempted — not stuck
    if analysis_status is None:
        return ('rejected', 'nothing_to_retry')

    # Path 4: already complete
    if analysis_status == 'complete':
        return ('rejected', 'already_complete')

    # Path 5: in-flight (stuck) — reset to failed before enqueueing
    if analysis_status in ('summarising', 'researching', 'matching'):
        try:
            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET analysis.#s = :s',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':s': 'failed'},
            )
        except Exception as e:
            logger.error(f"retry: failed to reset analysis status for {job_id}: {e}")
            return ('rejected', 'enqueue_failed')

    # Paths 5, 6 (failed), 7 (pending) — enqueue to processing queue
    try:
        sqs.send_message(
            QueueUrl=JOB_PROCESSING_QUEUE_URL,
            MessageBody=json.dumps({'job_id': job_id}),
        )
    except Exception as e:
        logger.error(f"retry: enqueue failed for processing {job_id}: {e}")
        return ('rejected', 'enqueue_failed')

    return ('accepted', None)


def retry_jobs(body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) == 0:
        return response(400, {'error': "'ids' must be a non-empty list"})

    accepted = []
    rejected = []
    any_enqueue_succeeded = False

    for job_id in ids:
        item = jobs_table.get_item(Key={'id': job_id}).get('Item')

        if not item:
            rejected.append({'job_id': job_id, 'reason': 'not_found'})
            continue

        outcome, reason = _dispatch_retry(item)

        if outcome == 'accepted':
            accepted.append(job_id)
            any_enqueue_succeeded = True
        else:
            rejected.append({'job_id': job_id, 'reason': reason})

    all_failed = len(accepted) == 0 and any(
        r['reason'] in ('enqueue_failed', 'delete_failed') for r in rejected
    )
    status_code = 500 if all_failed else 200
    return response(status_code, {'accepted': accepted, 'rejected': rejected})


def delete_failed_ingestion(job_id):
    item = jobs_table.get_item(Key={'id': job_id}).get('Item')
    if not item:
        return response(404, {'error': f'Job {job_id} not found'})

    if item.get('ingestion_status') != 'failed':
        return response(400, {'error': 'This endpoint only deletes failed-ingestion records'})

    jobs_table.delete_item(Key={'id': job_id})
    return response(200, {'job_id': job_id})


def is_valid_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
    except Exception:
        return False


def find_duplicate_job(url):
    """Returns job_id of a non-failed duplicate, or None if no duplicate exists."""
    result = jobs_table.query(
        IndexName='url-index',
        KeyConditionExpression=Key('url').eq(url),
        FilterExpression=Attr('ingestion_status').ne('failed') | Attr('ingestion_status').not_exists(),
    )
    items = result.get('Items', [])
    return items[0]['id'] if items else None


def ingest_from_urls(body):
    try:
        data = json.loads(body or '{}')
    except json.JSONDecodeError:
        return response(400, {'error': 'Invalid JSON body'})

    urls = data.get('urls')
    if not isinstance(urls, list) or len(urls) == 0:
        return response(400, {'error': "'urls' must be a non-empty list"})
    if not all(isinstance(u, str) for u in urls):
        return response(400, {'error': "'urls' must be a list of strings"})

    accepted = []
    rejected = []
    any_enqueue_succeeded = False

    for url in urls:
        if not is_valid_url(url):
            rejected.append({'url': url, 'reason': 'malformed_url'})
            continue

        existing_id = find_duplicate_job(url)
        if existing_id:
            rejected.append({'url': url, 'reason': 'duplicate', 'existing_job_id': existing_id})
            continue

        try:
            sqs.send_message(
                QueueUrl=JOB_URL_INGESTION_QUEUE_URL,
                MessageBody=json.dumps({'url': url}),
            )
            accepted.append(url)
            any_enqueue_succeeded = True
        except Exception as e:
            logger.error(f"from-url: failed to enqueue {url}: {e}")
            rejected.append({'url': url, 'reason': 'enqueue_failed'})

    all_failed = len(accepted) == 0 and any(r['reason'] == 'enqueue_failed' for r in rejected)
    status_code = 500 if all_failed else 200
    return response(status_code, {'accepted': accepted, 'rejected': rejected})


def get_failed_ingestions(auth):
    if auth == 'invalid':
        return response(401, {'error': 'Unauthorized'})

    filter_expr = Attr('ingestion_status').eq('failed')
    items = _scan_all(filter_expr)

    result = [
        {
            'id': item['id'],
            'url': item.get('url'),
            'ingestion_error': item.get('ingestion_error'),
            'scrapedAt': item.get('scrapedAt'),
        }
        for item in items
    ]
    result.sort(key=lambda x: x.get('scrapedAt', ''), reverse=True)

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

    auth = validate_token(headers)

    # GET /jobs
    if method == 'GET' and path == '/jobs':
        return get_jobs(event, auth)

    # GET /jobs/failed-ingestions
    if method == 'GET' and path == '/jobs/failed-ingestions':
        return get_failed_ingestions(auth)

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

    # DELETE /jobs/{id}
    m = re.match(r'^/jobs/([^/]+)$', path)
    if m and method == 'DELETE':
        return delete_failed_ingestion(m.group(1))

    # PUT /jobs/{id}/status
    m = re.match(r'^/jobs/([^/]+)/status$', path)
    if m and method == 'PUT':
        return update_job_status(m.group(1), body)

    # POST /jobs/{id}/archive
    m = re.match(r'^/jobs/([^/]+)/archive$', path)
    if m and method == 'POST':
        return archive_job(m.group(1))

    # POST /jobs/{id}/restore
    m = re.match(r'^/jobs/([^/]+)/restore$', path)
    if m and method == 'POST':
        return restore_job(m.group(1))

    # POST /jobs/{id}/resume
    m = re.match(r'^/jobs/([^/]+)/resume$', path)
    if m and method == 'POST':
        return tailor_resume(m.group(1))

    # POST /jobs/{id}/cover-letter
    m = re.match(r'^/jobs/([^/]+)/cover-letter$', path)
    if m and method == 'POST':
        return generate_cover_letter(m.group(1), body)

    # POST /jobs/full-analysis
    if method == 'POST' and path == '/jobs/full-analysis':
        return trigger_full_analysis(body)

    # POST /jobs/retry
    if method == 'POST' and path == '/jobs/retry':
        return retry_jobs(body)

    # POST /jobs/from-url
    if method == 'POST' and path == '/jobs/from-url':
        return ingest_from_urls(body)

    # POST /scrape/indeed
    if method == 'POST' and path == '/scrape/indeed':
        return start_indeed_scrape(body)

    # POST /scrape/linkedin
    if method == 'POST' and path == '/scrape/linkedin':
        return start_linkedin_scrape(body)

    # GET /jobs/{id}/resume/download
    m = re.match(r'^/jobs/([^/]+)/resume/download$', path)
    if m and method == 'GET':
        return get_resume_download(m.group(1))

    # GET /jobs/{id}/cover-letter/download
    m = re.match(r'^/jobs/([^/]+)/cover-letter/download$', path)
    if m and method == 'GET':
        return get_cover_letter_download(m.group(1))

    return response(404, {'error': f'Route not found: {method} {path}'})

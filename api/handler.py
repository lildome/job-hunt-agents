import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import boto3
from boto3.dynamodb.conditions import Key, Attr
from boto3.dynamodb.types import TypeSerializer
from dedup import is_duplicate, classify_type, normalise_posting_date

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PIN = "Job Hunt PIN 159075"
S3_BUCKET = "dprofico-job-hunt-artifacts"
STALE_THRESHOLD = timedelta(minutes=5)
PENDING_SCREENING_STUCK_AFTER = timedelta(minutes=10)

JOB_PROCESSING_QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/052732928292/job-processing-queue'
JOB_URL_INGESTION_QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/052732928292/job-url-ingestion-queue'
JOB_SCREENING_QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/052732928292/job-screening-queue'

LINKEDIN_REMOTE_VALUES = {'onsite', 'remote', 'hybrid'}
LINKEDIN_POSTED_WITHIN_VALUES = {'day', '3days', 'week', 'month'}

# Seek accepts the same enum values for these fields; reuse the LinkedIn sets.
SEEK_REMOTE_VALUES = LINKEDIN_REMOTE_VALUES
SEEK_POSTED_WITHIN_VALUES = LINKEDIN_POSTED_WITHIN_VALUES

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

_serializer = TypeSerializer()

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
        # Screened: analysis has never been attempted (no analysis block, or status 'pending').
        # Once any analysis state is reached (summarising/researching/matching/complete/failed),
        # the job moves to Analysed.
        return (
            not_archived & not_failed &
            (Attr('analysis.status').not_exists() | Attr('analysis.status').eq('pending'))
        )
    if bucket == 'analysed':
        # Analysed: analysis has been initiated. Includes in-flight (summarising/researching/matching),
        # complete, and failed. Excludes applied (those move to the Applied bucket).
        return (
            not_archived & not_failed &
            Attr('analysis.status').exists() &
            Attr('analysis.status').is_in(['summarising', 'researching', 'matching', 'complete', 'failed']) &
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

def start_seek_scrape(body):
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
        if data['remote'] not in SEEK_REMOTE_VALUES:
            return response(400, {'error': f"Invalid 'remote' value. Allowed: {sorted(SEEK_REMOTE_VALUES)}"})
        search_params['remote'] = data['remote']

    if 'posted_within' in data:
        if data['posted_within'] not in SEEK_POSTED_WITHIN_VALUES:
            return response(400, {'error': f"Invalid 'posted_within' value. Allowed: {sorted(SEEK_POSTED_WITHIN_VALUES)}"})
        search_params['posted_within'] = data['posted_within']

    if 'state' in data and data['state']:
        search_params['state'] = data['state'].strip()
    if 'postCode' in data and data['postCode']:
        search_params['postCode'] = str(data['postCode']).strip()
    if 'radius' in data and data['radius']:
        try:
            search_params['radius'] = int(data['radius'])
        except (ValueError, TypeError):
            return response(400, {'error': 'radius must be an integer'})

    count = 100
    if 'count' in data:
        try:
            count = int(data['count'])
        except (ValueError, TypeError):
            return response(400, {'error': 'count must be an integer'})

    lambda_client.invoke(
        FunctionName='job-scraper',
        InvocationType='Event',
        Payload=json.dumps({
            'job_board': 'seek',
            'search_params': search_params,
            'count': count,
        }).encode()
    )
    logger.info(f"Seek scrape triggered: {search_params} count={count}")
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

    # Path 2: no screening block — re-trigger screening.
    # job-screener consumes a DynamoDB-stream INSERT shape, not {job_id}; send the
    # serialized job as a synthetic stream record (see _to_stream_record).
    if not has_screening:
        try:
            sqs.send_message(
                QueueUrl=JOB_SCREENING_QUEUE_URL,
                MessageBody=json.dumps(_to_stream_record(job), default=str),
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


def _to_stream_record(job: dict) -> dict:
    """Rebuild the DynamoDB-stream INSERT shape that job-screener expects.

    job-screener reads record['dynamodb']['NewImage'] and runs it through a
    boto3 TypeDeserializer. The real EventBridge Pipe delivers NewImage in
    DynamoDB-JSON ({"id": {"S": "..."}}), but a resource-level scan() (and the
    get_item in _dispatch_retry) returns already-deserialized Python values
    ({"id": "..."}). So we re-serialize the item to reconstruct the wire shape
    the screener deserializes.

    Only eventName and dynamodb.NewImage are populated — those are the only
    fields the screener touches.
    """
    new_image = {k: _serializer.serialize(v) for k, v in job.items()}
    return {
        'eventName': 'INSERT',
        'dynamodb': {'NewImage': new_image},
    }


def _is_stuck_screening(job: dict) -> bool:
    """A job is stuck in the screening pass if any of the following hold:
      - It has no screening block at all.
      - Its screening explicitly failed.
      - Its screening has been 'pending' for longer than PENDING_SCREENING_STUCK_AFTER,
        which means the screener Lambda almost certainly died silently
        (unhandled exceptions write a 'failed' status before re-raising, so
        permanent 'pending' means the container died before that could run).
      - Its screening is 'pending' with no started_at timestamp at all. These are
        pre-timestamp records; since this endpoint is run manually, treat them as
        stuck and let the human decide when to invoke.

    Failed-ingestion records are never screened. 'complete' is done.
    """
    if job.get('ingestion_status') == 'failed':
        return False
    screening = job.get('screening')
    if not screening:
        return True

    status = screening.get('status')
    if status == 'failed':
        return True
    if status == 'pending':
        started_at = screening.get('started_at')
        if not started_at:
            return True
        try:
            started = datetime.fromisoformat(started_at)
        except (ValueError, TypeError):
            return True
        return datetime.now(timezone.utc) - started > PENDING_SCREENING_STUCK_AFTER

    return False


def rescreen_stuck_jobs():
    """Bulk-requeue every job stuck in the screening pass.

    Scans the jobs table, selects jobs that are missing a screening block or whose
    screening failed (excluding failed-ingestion records), and re-enqueues each one
    to job-screening-queue as a synthetic DynamoDB-stream INSERT record. The screener
    is idempotent for these jobs because they never reached screening.status=complete,
    so company job_count is not double-counted.

    Returns {accepted, rejected, count} consistent with the other bulk endpoints.
    """
    not_archived = Attr('archived_at').not_exists()
    not_failed_ingestion = (
        Attr('ingestion_status').not_exists() | Attr('ingestion_status').ne('failed')
    )
    # Broaden to "anything not 'complete'" — the precise stuck definition (failed,
    # missing, or stale-pending) is enforced in _is_stuck_screening on the Python side.
    not_complete = (
        Attr('screening').not_exists() | Attr('screening.status').ne('complete')
    )
    filter_expr = not_archived & not_failed_ingestion & not_complete

    items = _scan_all(filter_expr)

    # Defensive second pass in Python — guarantees the exact stuck definition even if
    # the scan filter is loosened later, and drops anything without an id.
    stuck = [j for j in items if j.get('id') and _is_stuck_screening(j)]

    accepted = []
    rejected = []

    # SQS send_message_batch takes at most 10 entries per call.
    for i in range(0, len(stuck), 10):
        chunk = stuck[i:i + 10]
        entries = []
        entry_id_to_job_id = {}
        for n, job in enumerate(chunk):
            entry_id = str(n)
            entry_id_to_job_id[entry_id] = job['id']
            entries.append({
                'Id': entry_id,
                'MessageBody': json.dumps(_to_stream_record(job), default=str),
            })

        try:
            resp = sqs.send_message_batch(
                QueueUrl=JOB_SCREENING_QUEUE_URL,
                Entries=entries,
            )
        except Exception as e:
            logger.error(f"rescreen-stuck: batch send failed: {e}")
            for job in chunk:
                rejected.append({'job_id': job['id'], 'reason': 'enqueue_failed'})
            continue

        for ok in resp.get('Successful', []):
            accepted.append(entry_id_to_job_id[ok['Id']])
        for bad in resp.get('Failed', []):
            jid = entry_id_to_job_id.get(bad['Id'], 'unknown')
            logger.error(f"rescreen-stuck: enqueue failed for {jid}: {bad.get('Message')}")
            rejected.append({'job_id': jid, 'reason': 'enqueue_failed'})

    all_failed = len(accepted) == 0 and len(rejected) > 0
    status_code = 500 if all_failed else 200
    return response(status_code, {
        'accepted': accepted,
        'rejected': rejected,
        'count': len(accepted),
    })


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


def _survivor_rank(job: dict) -> tuple:
    """Lower tuple → higher priority survivor. Ties broken by earliest scrapedAt."""
    analysis_complete = 0 if (job.get('analysis') or {}).get('status') == 'complete' else 1
    has_applied_status = 0 if job.get('status') in ('applied', 'interviewing', 'offer', 'rejected') else 1
    has_work = 0 if any(
        k.startswith('tailored_resume_') or k.startswith('cover_letter_')
        for k in job
    ) else 1
    scraped = job.get('scrapedAt', '')
    return (analysis_complete, has_applied_status, has_work, scraped)


def _build_comparison(job: dict) -> dict:
    screening = job.get('screening') or {}
    return {
        'title': job.get('positionName', ''),
        'match_score': screening.get('match_score', 0),
        'source': job.get('source', ''),
        'posting_date': job.get('postingDate'),
    }


def _cluster_jobs(jobs: list) -> list:
    """Group jobs into duplicate clusters. Returns list of {'survivor': job, 'members': [job, ...]}
    for clusters that have at least one member. Survivor selection is applied after initial grouping."""
    # Sort ascending by scrapedAt so earlier records are processed first
    sorted_jobs = sorted(jobs, key=lambda j: j.get('scrapedAt', ''))

    # clusters: list of {'head': job, 'members': [job, ...], 'all': [job, ...]}
    clusters = []

    for job in sorted_jobs:
        comp = _build_comparison(job)
        matched_cluster = None
        for cluster in clusters:
            head_comp = _build_comparison(cluster['head'])
            dupe, _ = is_duplicate(comp, head_comp)
            if dupe:
                matched_cluster = cluster
                break
        if matched_cluster is not None:
            matched_cluster['members'].append(job)
            matched_cluster['all'].append(job)
        else:
            clusters.append({'head': job, 'members': [], 'all': [job]})

    result = []
    for cluster in clusters:
        if not cluster['members']:
            continue
        # Re-rank all jobs in cluster to pick true survivor
        all_jobs = cluster['all']
        all_jobs_ranked = sorted(all_jobs, key=_survivor_rank)
        survivor = all_jobs_ranked[0]
        members = [j for j in all_jobs_ranked if j is not survivor]
        result.append({'survivor': survivor, 'members': members})

    return result


def _compute_sighting(member: dict, survivor: dict) -> dict:
    """Build a sighting record for a member being folded into survivor."""
    member_comp = _build_comparison(member)
    survivor_comp = _build_comparison(survivor)
    _, match_method = is_duplicate(member_comp, survivor_comp)
    type_val = classify_type(member_comp, survivor_comp)
    score_delta = member_comp['match_score'] - survivor_comp['match_score']
    return {
        'source': member.get('source'),
        'url': member.get('url'),
        'postingDate': member.get('postingDate'),
        'scrapedAt': member.get('scrapedAt'),
        'screening': member.get('screening'),
        'type': type_val,
        'match_method': match_method,
        'score_delta': score_delta,
        'folded_at': datetime.now(timezone.utc).isoformat(),
        'times_seen': 1,
    }


def _type2_listing_updates(survivor: dict, members: list) -> dict:
    """Return url/source/postingDate/scrapedAt from the most recent Type 2 member."""
    type2_members = []
    for member in members:
        mc = _build_comparison(member)
        sc = _build_comparison(survivor)
        if classify_type(mc, sc) == 2:
            type2_members.append(member)
    if not type2_members:
        return {}
    # Pick freshest by postingDate, falling back to scrapedAt
    freshest = max(
        type2_members,
        key=lambda m: (
            normalise_posting_date(m.get('postingDate'), m.get('source', '')) or '',
            m.get('scrapedAt', ''),
        ),
    )
    updates = {}
    for field in ('url', 'source', 'postingDate', 'scrapedAt'):
        val = freshest.get(field)
        if val is not None:
            updates[field] = val
    return updates


def dedup_preview():
    """Dry-run bulk dedup: returns proposed clusters without writing anything."""
    filter_expr = (
        Attr('screening.status').eq('complete') &
        (Attr('ingestion_status').not_exists() | Attr('ingestion_status').ne('failed')) &
        Attr('duplicate_of').not_exists()
    )
    jobs = _scan_all(filter_expr)
    logger.info(f"dedup-preview: {len(jobs)} eligible jobs")

    # Group by company_id
    by_company: dict = {}
    for job in jobs:
        cid = job.get('company_id', '__none__')
        by_company.setdefault(cid, []).append(job)

    # Look up canonical names in one batch
    company_ids = [cid for cid in by_company if cid != '__none__']
    cid_to_name = {}
    for i in range(0, len(company_ids), 100):
        batch = company_ids[i:i + 100]
        resp = dynamodb.batch_get_item(
            RequestItems={
                'companies': {
                    'Keys': [{'id': cid} for cid in batch],
                    'ProjectionExpression': 'id, canonical_name',
                }
            }
        )
        for item in resp.get('Responses', {}).get('companies', []):
            cid_to_name[item['id']] = item.get('canonical_name')

    clusters_out = []
    for cid, company_jobs in by_company.items():
        clusters = _cluster_jobs(company_jobs)
        for cluster in clusters:
            survivor = cluster['survivor']
            members = cluster['members']
            s_screening = survivor.get('screening') or {}
            survivor_shape = {
                'id': survivor['id'],
                'positionName': survivor.get('positionName'),
                'source': survivor.get('source'),
                'postingDate': survivor.get('postingDate'),
                'scrapedAt': survivor.get('scrapedAt'),
                'match_score': s_screening.get('match_score'),
            }
            members_shape = []
            for member in members:
                m_screening = member.get('screening') or {}
                mc = _build_comparison(member)
                sc = _build_comparison(survivor)
                _, match_method = is_duplicate(mc, sc)
                type_val = classify_type(mc, sc)
                score_delta = mc['match_score'] - sc['match_score']
                members_shape.append({
                    'id': member['id'],
                    'positionName': member.get('positionName'),
                    'source': member.get('source'),
                    'postingDate': member.get('postingDate'),
                    'scrapedAt': member.get('scrapedAt'),
                    'match_score': m_screening.get('match_score'),
                    'type': type_val,
                    'match_method': match_method,
                    'score_delta': score_delta,
                })
            clusters_out.append({
                'company_id': cid if cid != '__none__' else None,
                'canonical_name': cid_to_name.get(cid),
                'survivor': survivor_shape,
                'members': members_shape,
                'survivor_field_updates': _type2_listing_updates(survivor, members),
            })

    total_duplicates = sum(len(c['members']) for c in clusters_out)
    logger.info(f"dedup-preview: {len(clusters_out)} clusters, {total_duplicates} duplicates")
    return response(200, {
        'clusters': clusters_out,
        'cluster_count': len(clusters_out),
        'total_duplicates': total_duplicates,
    })


def dedup_apply():
    """Execute bulk dedup merges: fold members into survivors and mark them duplicate_of."""
    filter_expr = (
        Attr('screening.status').eq('complete') &
        (Attr('ingestion_status').not_exists() | Attr('ingestion_status').ne('failed')) &
        Attr('duplicate_of').not_exists()
    )
    jobs = _scan_all(filter_expr)
    logger.info(f"dedup-apply: {len(jobs)} eligible jobs")

    by_company: dict = {}
    for job in jobs:
        cid = job.get('company_id', '__none__')
        by_company.setdefault(cid, []).append(job)

    now = datetime.now(timezone.utc).isoformat()
    accepted = []
    rejected = []
    clusters_applied = 0

    for cid, company_jobs in by_company.items():
        clusters = _cluster_jobs(company_jobs)
        for cluster in clusters:
            survivor = cluster['survivor']
            members = cluster['members']
            survivor_id = survivor['id']

            existing_sightings = list(survivor.get('sightings') or [])

            new_sightings = []
            for member in members:
                sighting = _compute_sighting(member, survivor)
                # Self-dedup: same source + same normalised posting date → bump times_seen
                norm_date = normalise_posting_date(sighting.get('postingDate'), sighting.get('source', ''))
                merged = False
                for existing in existing_sightings:
                    ex_norm = normalise_posting_date(existing.get('postingDate'), existing.get('source', ''))
                    if existing.get('source') == sighting.get('source') and ex_norm == norm_date and norm_date is not None:
                        existing['times_seen'] = existing.get('times_seen', 1) + 1
                        merged = True
                        break
                if not merged:
                    new_sightings.append(sighting)

            all_sightings = existing_sightings + new_sightings

            # Type 2 listing-field updates on survivor
            field_updates = _type2_listing_updates(survivor, members)

            # Build UpdateExpression for survivor
            update_parts = ['#sightings = :sightings']
            expr_names = {'#sightings': 'sightings'}
            expr_values = {':sightings': all_sightings}

            for field in ('url', 'source', 'postingDate', 'scrapedAt'):
                if field in field_updates:
                    placeholder = f'#{field}'
                    expr_names[placeholder] = field
                    expr_values[f':{field}'] = field_updates[field]
                    update_parts.append(f'{placeholder} = :{field}')

            try:
                jobs_table.update_item(
                    Key={'id': survivor_id},
                    UpdateExpression='SET ' + ', '.join(update_parts),
                    ExpressionAttributeNames=expr_names,
                    ExpressionAttributeValues=expr_values,
                )
            except Exception as e:
                logger.error(f"dedup-apply: failed to update survivor {survivor_id}: {e}")
                for member in members:
                    rejected.append({'job_id': member['id'], 'reason': 'survivor_update_failed'})
                continue

            # Mark each member as duplicate_of survivor
            for member in members:
                member_id = member['id']
                try:
                    jobs_table.update_item(
                        Key={'id': member_id},
                        UpdateExpression='SET duplicate_of = :sid',
                        ExpressionAttributeValues={':sid': survivor_id},
                    )
                    accepted.append(member_id)
                except Exception as e:
                    logger.error(f"dedup-apply: failed to mark {member_id} as duplicate: {e}")
                    rejected.append({'job_id': member_id, 'reason': 'mark_failed'})

            # TODO: job_count on company records may be overcounted on existing data
            # (each member was counted when first screened). A future pass could recompute.

            clusters_applied += 1

    all_failed = len(accepted) == 0 and any(
        r['reason'] in ('survivor_update_failed', 'mark_failed') for r in rejected
    )
    status_code = 500 if all_failed else 200
    logger.info(f"dedup-apply: {clusters_applied} clusters, {len(accepted)} folded, {len(rejected)} failed")
    return response(status_code, {
        'accepted': accepted,
        'rejected': rejected,
        'count': len(accepted),
        'clusters_applied': clusters_applied,
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

    # POST /jobs/rescreen-stuck
    if method == 'POST' and path == '/jobs/rescreen-stuck':
        return rescreen_stuck_jobs()

    # POST /jobs/from-url
    if method == 'POST' and path == '/jobs/from-url':
        return ingest_from_urls(body)

    # POST /jobs/dedup-preview
    if method == 'POST' and path == '/jobs/dedup-preview':
        return dedup_preview()

    # POST /jobs/dedup-apply
    if method == 'POST' and path == '/jobs/dedup-apply':
        return dedup_apply()

    # POST /scrape/indeed
    if method == 'POST' and path == '/scrape/indeed':
        return start_indeed_scrape(body)

    # POST /scrape/linkedin
    if method == 'POST' and path == '/scrape/linkedin':
        return start_linkedin_scrape(body)

    # POST /scrape/seek
    if method == 'POST' and path == '/scrape/seek':
        return start_seek_scrape(body)

    # GET /jobs/{id}/resume/download
    m = re.match(r'^/jobs/([^/]+)/resume/download$', path)
    if m and method == 'GET':
        return get_resume_download(m.group(1))

    # GET /jobs/{id}/cover-letter/download
    m = re.match(r'^/jobs/([^/]+)/cover-letter/download$', path)
    if m and method == 'GET':
        return get_cover_letter_download(m.group(1))

    return response(404, {'error': f'Route not found: {method} {path}'})

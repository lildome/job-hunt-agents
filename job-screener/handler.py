import json
import logging
import time
import uuid
import random
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError

from dedup import is_duplicate, classify_type, normalise_posting_date

logger = logging.getLogger()
logger.setLevel(logging.INFO)

deserializer = TypeDeserializer()
ssm = boto3.client('ssm', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')
companies_table = dynamodb.Table('companies')
mappings_table = dynamodb.Table('company_name_mappings')
profiles_table = dynamodb.Table('candidate_profiles')

def _get_parameter(name):
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response['Parameter']['Value']

gemini_client = genai.Client(api_key=_get_parameter('gemini-api-key'))

SCREENING_SYSTEM_PROMPT = """You are helping a software engineer screen scraped job listings to decide which are worth investigating further. Your job is to produce a fast first-pass score for each job — fast enough that every scraped job can be processed cheaply, accurate enough that the candidate can confidently skip the jobs you flag as poor fits.

Jobs that score well on this screen will be escalated to a deeper analysis pass. Jobs that score poorly will be discarded. The candidate is looking at this from their own perspective, not a recruiter's: gaps in their background are real signal, not concerns to talk around.

You will receive a brief summary of the candidate's background and the full job description.

You will produce two scores, both on a 1-10 integer scale.

The match score measures how well the candidate's technical background fits what this role requires: their stack, their specific skills, and their experience level relative to the experience the job description asks for. Match is a strict technical question.

The recommendation score answers a different question: should the candidate spend further effort investigating this job. This includes the technical match but also factors that don't fit cleanly into match: the role's title and seniority signal (IC vs lead, junior vs staff), work environment signals visible in the job description (team size, company stage, greenfield vs maintenance, process-heavy vs flexible), anything visible about location or remote arrangements, and the model's overall read of whether this is the kind of role the candidate would actually want, not just be qualified for.

These two scores will often differ, and divergence is expected output, not a sign you've done something wrong. A high match with a low recommendation is normal — strong technical fit for a role the candidate wouldn't accept. The reverse is also normal — a stretch role at a company doing work the candidate would find energising.

The candidate's CV summary was written specifically for this screening task, not for company-facing use. Stated gaps are real gaps. Stated preferences and the kinds of work the candidate finds energising or draining reflect what they actually want. Read the summary at face value when scoring.

Use the full 1-10 range. Most scraped jobs will not be strong fits for this candidate, and the score distribution should reflect that — bunching everything in the 6-8 range makes the screen useless.

Scores in the 7-10 range mean clear positive: strong technical alignment and the kind of role the candidate would actually pursue. Use 9-10 sparingly, only for genuinely exceptional fits. The candidate should be confident escalating these to full analysis.

Scores in the 4-6 range mean ambiguous: real signal in both directions, where the answer genuinely depends on details a deeper analysis would surface. The candidate may or may not escalate these.

Scores in the 1-3 range mean clear negative: substantive gaps in the technical match or significant misalignment with what the candidate is looking for. The candidate should feel comfortable skipping these without further investigation.

The divergence reasoning field is always populated, but its length depends on how far the scores diverged.

When the two scores differ by 1 point or less, treat them as aligned. Write one short sentence on why both scores landed where they did. No need to elaborate.

When the two scores differ by 2 points or more, the divergence is substantive. Identify the specific factor that caused the gap — the one signal in the job description or the candidate summary that pushed one score noticeably higher or lower than the other. Naming the factor matters more than describing the divergence.

You are also responsible for resolving the company's canonical name. The canonical name is the public-facing brand or parent company name, not the legal entity or regional arm. You do not have web search access — use only your existing knowledge.

Output a confidence level alongside the canonical name: high when you're certain (well-known company, obvious resolution), mid when your resolution is reasonable but you can't fully verify it, low when the company is unfamiliar to you. When confidence is low, return the raw scraped company name unchanged.

Return a single JSON object with the following fields, in this order. Produce the fields in the order given. In particular, write role_summary first, before scoring: articulating what the role is before judging it produces more grounded scores.

role_summary (string, 4-5 sentences): a plain, factual description of what this role is and its major responsibilities — enough that the candidate can decide whether the job is worth a deeper look without opening the original listing. Describe the role neutrally, as though the candidate did not exist: what the team or function does, the core responsibilities, the seniority and scope, and any defining characteristics of the work itself. Do NOT evaluate fit, reference the candidate's background, or justify the scores — that is the job of the reasoning fields below. This field answers "what is this job"; the reasoning fields answer "is it right for this candidate." Do not reproduce the job description; summarise it.
match_score (integer 1-10): the technical match score per the rules above.
match_reasoning (string, one paragraph): the reasoning for the match score, citing specific elements of the candidate's background and the role's requirements.
recommendation_score (integer 1-10): the recommendation score per the rules above.
recommendation_reasoning (string, one paragraph): the reasoning for the recommendation score, including factors beyond technical match.
divergence_reasoning (string, one to two sentences): per the divergence rules above — short when scores are aligned, substantive when they diverge.
canonical_company_name (string): the resolved canonical name, or the raw scraped name if confidence is low.
canonical_name_confidence (one of "low", "mid", "high"): the confidence level of the canonical name resolution.

Output JSON only. No preamble, no trailing commentary.
Every field must be populated, including divergence_reasoning when scores align.
Don't speculate about candidate background not stated in the summary.
Don't describe your evaluation process — produce the output directly."""

SCREENING_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "role_summary": types.Schema(type=types.Type.STRING),
        "match_score": types.Schema(type=types.Type.INTEGER, minimum=1, maximum=10),
        "match_reasoning": types.Schema(type=types.Type.STRING),
        "recommendation_score": types.Schema(type=types.Type.INTEGER, minimum=1, maximum=10),
        "recommendation_reasoning": types.Schema(type=types.Type.STRING),
        "divergence_reasoning": types.Schema(type=types.Type.STRING),
        "canonical_company_name": types.Schema(type=types.Type.STRING),
        "canonical_name_confidence": types.Schema(
            type=types.Type.STRING,
            enum=["low", "mid", "high"],
        ),
    },
    required=[
        "role_summary",
        "match_score",
        "match_reasoning",
        "recommendation_score",
        "recommendation_reasoning",
        "divergence_reasoning",
        "canonical_company_name",
        "canonical_name_confidence",
    ],
    property_ordering=[
        "role_summary",
        "match_score",
        "match_reasoning",
        "recommendation_score",
        "recommendation_reasoning",
        "divergence_reasoning",
        "canonical_company_name",
        "canonical_name_confidence",
    ],
)

_cv_summary_text = None


def get_cv_summary_text():
    global _cv_summary_text
    if _cv_summary_text is None:
        profile = profiles_table.get_item(Key={'profile_id': 'primary'}).get('Item', {})
        _cv_summary_text = profile.get('cv_summary_text', '')
    return _cv_summary_text


def deserialize_item(dynamo_item):
    return {k: deserializer.deserialize(v) for k, v in dynamo_item.items()}


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

            existing_screening = job.get('screening', {})
            if isinstance(existing_screening, dict) and existing_screening.get('status') == 'complete':
                logger.info(f"Skipping {job_id} — screening already complete")
                continue

            raw_company_name = job.get('company', '')
            position_name = job.get('positionName', '')
            location = job.get('location', '')
            description = job.get('description', '')

            logger.info(f"Screening job {job_id}: {position_name} at {raw_company_name}")

            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET screening = :s',
                ExpressionAttributeValues={':s': {
                    'status': 'pending',
                    'started_at': datetime.now(timezone.utc).isoformat(),
                }}
            )

            try:
                cv_summary_text = get_cv_summary_text()

                user_message = f"""<candidate_summary>
{cv_summary_text}
</candidate_summary>

<job>
positionName: {position_name}
company: {raw_company_name}
location: {location}
description: {description}
</job>
"""

                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SCREENING_RESPONSE_SCHEMA,
                    system_instruction=SCREENING_SYSTEM_PROMPT,
                )
                
                for attempt in range(3):
                    try:
                        response = gemini_client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=user_message,
                            config=config,
                        )
                        break  # Success, exit retry loop
                    except ClientError as e:
                        if e.code == 429 and attempt < 2:  # Rate limit exceeded
                            logger.warning(f"Rate limited screening {position_name} at {raw_company_name} — waiting 65s before retry")
                            time.sleep(65)
                        else:
                            raise
                        response = gemini_client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=user_message,
                            config=config,
                        )
                    except ServerError as e:
                        if e.code == 503 and attempt < 2:
                            wait = 30 * (2 ** attempt) + random.uniform(0, 5)
                            logger.warning(f"Model overloaded for {position_name} at {raw_company_name} — waiting {wait:.1f}s before retry (attempt {attempt + 1}/3)")
                            time.sleep(wait)
                        else:
                            raise

                result = json.loads(response.text)
                model_canonical = result['canonical_company_name']

                # Step 4a: raw name in mappings table
                raw_was_mapped = False
                company_id = None

                mapping = mappings_table.get_item(Key={'raw_name': raw_company_name}).get('Item')
                if mapping:
                    company_id = mapping['company_id']
                    raw_was_mapped = True
                    logger.info(f"Mapping found for raw name {raw_company_name!r} → {company_id}")

                # Step 4b: model's canonical name in companies GSI
                if company_id is None:
                    gsi_result = companies_table.query(
                        IndexName='canonical-name-index',
                        KeyConditionExpression=Key('canonical_name').eq(model_canonical),
                    )
                    items = gsi_result.get('Items', [])
                    if items:
                        company_id = items[0]['id']
                        logger.info(f"GSI found company for {model_canonical!r} → {company_id}")

                # Step 4c: model's canonical name in mappings table
                if company_id is None:
                    mapping = mappings_table.get_item(Key={'raw_name': model_canonical}).get('Item')
                    if mapping:
                        company_id = mapping['company_id']
                        logger.info(f"Mapping found for canonical name {model_canonical!r} → {company_id}")

                if company_id is None:
                    # Step 4d-create: no existing company found
                    company_id = f"comp_{uuid.uuid4()}"
                    companies_table.put_item(Item={
                        'id': company_id,
                        'canonical_name': model_canonical,
                        'aliases': [raw_company_name],
                        'job_count': 1,
                    })
                    mappings_table.put_item(Item={
                        'raw_name': raw_company_name,
                        'company_id': company_id,
                    })
                    logger.info(f"Created new company {company_id} for {model_canonical!r}")
                else:
                    # Step 4d-existing: update existing company record
                    existing = companies_table.get_item(Key={'id': company_id}).get('Item', {})
                    aliases = existing.get('aliases', [])

                    if raw_company_name not in aliases:
                        companies_table.update_item(
                            Key={'id': company_id},
                            UpdateExpression=(
                                'ADD job_count :inc '
                                'SET aliases = list_append(if_not_exists(aliases, :empty), :new)'
                            ),
                            ExpressionAttributeValues={
                                ':inc': 1,
                                ':new': [raw_company_name],
                                ':empty': [],
                            },
                        )
                    else:
                        companies_table.update_item(
                            Key={'id': company_id},
                            UpdateExpression='ADD job_count :inc',
                            ExpressionAttributeValues={':inc': 1},
                        )

                    if not raw_was_mapped:
                        mappings_table.put_item(Item={
                            'raw_name': raw_company_name,
                            'company_id': company_id,
                        })

                    logger.info(f"Updated company {company_id} for {raw_company_name!r}")

                screening = {
                    'status': 'complete',
                    'role_summary': result['role_summary'],
                    'canonical_name_confidence': result['canonical_name_confidence'],
                    'match_score': result['match_score'],
                    'match_reasoning': result['match_reasoning'],
                    'recommendation_score': result['recommendation_score'],
                    'recommendation_reasoning': result['recommendation_reasoning'],
                    'divergence_reasoning': result['divergence_reasoning'],
                }

                # --- Duplicate detection ---
                # Query all same-company jobs that have a complete screening and are
                # not themselves folded duplicates. Sort by scrapedAt asc so
                # first-seen wins deterministically.
                gsi_jobs_result = jobs_table.query(
                    IndexName='company-id-index',
                    KeyConditionExpression=Key('company_id').eq(company_id),
                )
                candidates = [
                    j for j in gsi_jobs_result.get('Items', [])
                    if j['id'] != job_id
                    and isinstance(j.get('screening'), dict)
                    and j['screening'].get('status') == 'complete'
                    and 'duplicate_of' not in j
                ]
                candidates.sort(key=lambda j: j.get('scrapedAt', ''))

                incoming_dict = {
                    'title': position_name,
                    'match_score': result['match_score'],
                    'source': job.get('source'),
                    'posting_date': job.get('postingDate'),
                }

                survivor = None
                match_method = None
                for cand in candidates:
                    cand_dict = {
                        'title': cand.get('positionName', ''),
                        'match_score': cand['screening']['match_score'],
                        'source': cand.get('source'),
                        'posting_date': cand.get('postingDate'),
                    }
                    dup, method = is_duplicate(incoming_dict, cand_dict)
                    if dup:
                        survivor = cand
                        survivor_dict = cand_dict
                        match_method = method
                        break

                if survivor is None:
                    # Step 3a — new canonical job. job_count was already bumped in step 4d above.
                    jobs_table.update_item(
                        Key={'id': job_id},
                        UpdateExpression='SET screening = :s, company_id = :c',
                        ExpressionAttributeValues={':s': screening, ':c': company_id}
                    )
                    logger.info(
                        f"Screening complete for {job_id}: "
                        f"match={result['match_score']}, rec={result['recommendation_score']}"
                    )
                else:
                    # Step 3b — fold into survivor: undo the job_count bump
                    companies_table.update_item(
                        Key={'id': company_id},
                        UpdateExpression='ADD job_count :dec',
                        ExpressionAttributeValues={':dec': -1},
                    )

                    dup_type = classify_type(incoming_dict, survivor_dict)
                    score_delta = abs(result['match_score'] - survivor['screening']['match_score'])
                    now_iso = datetime.now(timezone.utc).isoformat()

                    new_sighting = {
                        'id': job_id,
                        'positionName': position_name,
                        'location': location,
                        'url': job.get('url'),
                        'source': job.get('source'),
                        'postingDate': job.get('postingDate'),
                        'scrapedAt': job.get('scrapedAt'),
                        'screening': screening,
                        'type': dup_type,
                        'match_method': match_method,
                        'score_delta': score_delta,
                        'folded_at': now_iso,
                        'times_seen': 1,
                    }

                    # Self-dedup pass: check if this is a re-scrape of an already-folded sighting
                    existing_sightings = survivor.get('sightings', [])
                    incoming_norm_date = normalise_posting_date(
                        job.get('postingDate'), job.get('source', '')
                    )
                    merged_into_existing = False
                    for existing_sighting in existing_sightings:
                        if existing_sighting.get('source') == job.get('source'):
                            sighting_norm_date = normalise_posting_date(
                                existing_sighting.get('postingDate'),
                                existing_sighting.get('source', ''),
                            )
                            if incoming_norm_date and sighting_norm_date == incoming_norm_date:
                                # Re-scrape of an already-folded sighting — increment times_seen
                                existing_sighting['times_seen'] = existing_sighting.get('times_seen', 1) + 1
                                merged_into_existing = True
                                logger.info(
                                    f"Job {job_id} is re-scrape of existing sighting in survivor "
                                    f"{survivor['id']} — incrementing times_seen"
                                )
                                break

                    if not merged_into_existing:
                        existing_sightings = existing_sightings + [new_sighting]

                    # Build survivor update expression
                    if dup_type == 2:
                        # Update listing-pointer fields to reflect the freshest live listing
                        survivor_update_expr = (
                            'SET sightings = :sightings, '
                            '#url = :url, #source = :source, '
                            'postingDate = :postingDate, scrapedAt = :scrapedAt'
                        )
                        survivor_update_values = {
                            ':sightings': existing_sightings,
                            ':url': job.get('url'),
                            ':source': job.get('source'),
                            ':postingDate': job.get('postingDate'),
                            ':scrapedAt': job.get('scrapedAt'),
                        }
                        jobs_table.update_item(
                            Key={'id': survivor['id']},
                            UpdateExpression=survivor_update_expr,
                            ExpressionAttributeNames={'#url': 'url', '#source': 'source'},
                            ExpressionAttributeValues=survivor_update_values,
                        )
                    else:
                        # Type 1 — same posting, update sightings only
                        jobs_table.update_item(
                            Key={'id': survivor['id']},
                            UpdateExpression='SET sightings = :sightings',
                            ExpressionAttributeValues={':sightings': existing_sightings},
                        )

                    # Mark incoming job as folded; do NOT write a screening block
                    # TODO: _bucket_filter_expr in api/handler.py should also add
                    #   Attr('duplicate_of').not_exists() for robustness. The API's
                    #   get_jobs/get_job may also want to surface sightings from survivor records.
                    jobs_table.update_item(
                        Key={'id': job_id},
                        UpdateExpression='SET duplicate_of = :d, company_id = :c',
                        ExpressionAttributeValues={':d': survivor['id'], ':c': company_id},
                    )
                    logger.info(
                        f"Job {job_id} folded into survivor {survivor['id']} "
                        f"(type={dup_type}, method={match_method}, delta={score_delta})"
                    )

            except Exception as e:
                error_msg = str(e)[:500]
                logger.error(f"Screening failed for {job_id}: {error_msg}")
                jobs_table.update_item(
                    Key={'id': job_id},
                    UpdateExpression='SET screening = :s',
                    ExpressionAttributeValues={':s': {'status': 'failed', 'error': error_msg}}
                )
                raise

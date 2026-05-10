import json
import logging
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer
from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted

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

# NOTE: Free-tier Gemini prompts may be used by Google for model training.
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

Return a single JSON object with the following fields, in this order:

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
        "match_score",
        "match_reasoning",
        "recommendation_score",
        "recommendation_reasoning",
        "divergence_reasoning",
        "canonical_company_name",
        "canonical_name_confidence",
    ],
    property_ordering=[
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

            company = job.get('company', '')
            position_name = job.get('positionName', '')
            location = job.get('location', '')
            description = job.get('description', '')

            logger.info(f"Screening job {job_id}: {position_name} at {company}")

            jobs_table.update_item(
                Key={'id': job_id},
                UpdateExpression='SET screening = :s',
                ExpressionAttributeValues={':s': {'status': 'pending'}}
            )

            try:
                mapping_already_existed = False
                canonical_company_name = None
                canonical_name_confidence = None

                mapping = mappings_table.get_item(Key={'raw_name': company}).get('Item')
                if mapping:
                    canonical_company_name = mapping['canonical_company_name']
                    canonical_name_confidence = 'high'
                    mapping_already_existed = True
                    logger.info(f"Mapping found for {company} → {canonical_company_name}")

                cv_summary_text = get_cv_summary_text()

                user_message = f"""<candidate_summary>
{cv_summary_text}
</candidate_summary>

<job>
positionName: {position_name}
company: {company}
location: {location}
description: {description}
</job>
"""

                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SCREENING_RESPONSE_SCHEMA,
                    system_instruction=SCREENING_SYSTEM_PROMPT,
                )

                try:
                    response = gemini_client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=user_message,
                        config=config,
                    )
                except ResourceExhausted:
                    logger.warning(f"Rate limited screening {position_name} at {company} — waiting 65s before retry")
                    time.sleep(65)
                    response = gemini_client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=user_message,
                        config=config,
                    )

                result = json.loads(response.text)

                if mapping_already_existed:
                    pass
                else:
                    model_confidence = result['canonical_name_confidence']
                    model_canonical = result['canonical_company_name']
                    canonical_name_confidence = model_confidence

                    if model_confidence in ('high', 'mid'):
                        canonical_company_name = model_canonical
                        mappings_table.put_item(Item={
                            'raw_name': company,
                            'canonical_company_name': canonical_company_name,
                        })
                        companies_table.update_item(
                            Key={'company_name': canonical_company_name},
                            UpdateExpression=(
                                'ADD job_count :inc '
                                'SET aliases = list_append(if_not_exists(aliases, :empty), :new)'
                            ),
                            ExpressionAttributeValues={
                                ':inc': 1,
                                ':new': [company],
                                ':empty': [],
                            },
                        )
                    else:
                        # confidence is 'low' — use raw scraped name per prompt's fallback rule
                        if model_canonical != company:
                            logger.warning(
                                f"Model returned low-confidence name {model_canonical!r} for {company!r}, using raw name"
                            )
                        canonical_company_name = company

                screening = {
                    'status': 'complete',
                    'canonical_company_name': canonical_company_name,
                    'canonical_name_confidence': canonical_name_confidence,
                    'match_score': result['match_score'],
                    'match_reasoning': result['match_reasoning'],
                    'recommendation_score': result['recommendation_score'],
                    'recommendation_reasoning': result['recommendation_reasoning'],
                    'divergence_reasoning': result['divergence_reasoning'],
                }

                jobs_table.update_item(
                    Key={'id': job_id},
                    UpdateExpression='SET screening = :s',
                    ExpressionAttributeValues={':s': screening}
                )
                logger.info(
                    f"Screening complete for {job_id}: "
                    f"match={result['match_score']}, rec={result['recommendation_score']}"
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

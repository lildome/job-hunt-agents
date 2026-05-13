import json
import logging
import time
import uuid
from datetime import datetime, timezone

import boto3
import requests
from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client('ssm', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
jobs_table = dynamodb.Table('jobs')

EXTRACTION_SYSTEM_PROMPT = """You are extracting structured job posting data from raw HTML or text scraped from a job listing page. The page may be a careers site, an ATS portal, a job board listing, or any other page hosting a job description.

Your job is to populate the following fields from what you can see on the page. If a field is not present or you cannot determine it confidently, return an empty string for that field.

positionName: the job title as it appears on the page.
company: the hiring company's name. Look for it in the page header, the URL, the metadata, or the body of the description.
location: the job's location as stated. May be a city, region, country, "Remote", "Hybrid", or similar. If multiple locations are listed, pick the primary one.
description: the full job description text. Include the responsibilities, requirements, and any "about the role" or "about us" content if present on the same page. Strip navigation chrome, cookie banners, footers, and unrelated content. Preserve paragraph breaks as newlines.

Output JSON only. No preamble, no trailing commentary."""

EXTRACTION_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "positionName": types.Schema(type=types.Type.STRING),
        "company": types.Schema(type=types.Type.STRING),
        "location": types.Schema(type=types.Type.STRING),
        "description": types.Schema(type=types.Type.STRING),
    },
    required=["positionName", "company", "location", "description"],
    property_ordering=["positionName", "company", "location", "description"],
)

_gemini_client = None


def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = ssm.get_parameter(Name='gemini-api-key', WithDecryption=True)['Parameter']['Value']
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def write_failed(url, error_message):
    job_id = f"job_{uuid.uuid4()}"
    jobs_table.put_item(Item={
        "id": job_id,
        "url": url,
        "source": "manual",
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "ingestion_status": "failed",
        "ingestion_error": error_message[:500],
    })
    logger.warning(f"Ingestion failed for {url}: {error_message}")
    return job_id


def lambda_handler(event, context):
    record = event['Records'][0]
    body = json.loads(record['body'])
    url = body['url']

    logger.info(f"Ingesting URL: {url}")

    # Fetch
    try:
        resp = requests.get(
            url,
            timeout=20,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; JobHuntAgent/1.0)'},
            allow_redirects=True,
        )
    except Exception as e:
        write_failed(url, f"Fetch error: {e}")
        return

    if not (200 <= resp.status_code < 300):
        write_failed(url, f"HTTP {resp.status_code}")
        return

    page_text = resp.text
    if len(page_text) > 500_000:
        page_text = page_text[:500_000]

    # Extract via Gemini
    client = get_gemini_client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=EXTRACTION_RESPONSE_SCHEMA,
        system_instruction=EXTRACTION_SYSTEM_PROMPT,
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=page_text,
            config=config,
        )
    except ResourceExhausted:
        logger.warning(f"Rate limited ingesting {url} — waiting 65s before retry")
        time.sleep(65)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=page_text,
            config=config,
        )

    extracted = json.loads(response.text)

    position_name = extracted.get("positionName", "").strip()
    description = extracted.get("description", "").strip()

    if not position_name and not description:
        write_failed(url, "no job content extracted")
        return

    job_id = f"job_{uuid.uuid4()}"
    jobs_table.put_item(Item={
        "id": job_id,
        "positionName": extracted["positionName"],
        "company": extracted["company"],
        "location": extracted["location"],
        "description": extracted["description"],
        "url": url,
        "source": "manual",
        "status": "new",
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "ingestion_status": "success",
    })

    logger.info(
        f"Successfully ingested {url} as {job_id}: "
        f"{extracted['positionName']} @ {extracted['company']}"
    )
    
    
import logging
import uuid
from apify_client import ApifyClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def scrape_indeed(apify_client: ApifyClient, run_input: dict) -> list:
    try:
        run = apify_client.actor("hMvNSpz3JnHgl5jkh").call(run_input=run_input)
    except Exception as e:
        logger.error(f"Error scraping Indeed: {e}")
        return []

    keys_to_extract = ['salary', 'positionName', 'company', 'location', 'url', 'scrapedAt', 'postingDateParsed', 'description']
    extracted_listings = []
    for listing in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
        extracted_run = {key: listing[key] for key in keys_to_extract if key in listing.keys()}
        extracted_run['postingDate'] = extracted_run['postingDateParsed']
        del extracted_run['postingDateParsed']
        extracted_run['id'] = f"job_{uuid.uuid4()}"
        extracted_run['status'] = 'new'
        extracted_run['source'] = 'indeed'
        extracted_listings.append(extracted_run)
        logger.info(listing.keys())
    return extracted_listings

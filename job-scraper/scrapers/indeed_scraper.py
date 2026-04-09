from apify_client import ApifyClient
import uuid
from datetime import datetime

def scrape_indeed (apify_client : ApifyClient, run_input : dict) -> dict:
    try:
        run = apify_client.actor("hMvNSpz3JnHgl5jkh").call(run_input=run_input)
    except Exception as e:
        print(f"Error occurred while scraping Indeed: {e}")
        return {"error": str(e)}
    

    keys_to_extract = ['salary', 'positionName', 'company', 'location', 'url', 'scrapedAt', 'postingDateParsed', 'description']
    extracted_listings = []
    for listing in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
        extracted_run = {key: listing[key] for key in keys_to_extract if key in listing.keys()}
        extracted_run['postingDate'] = extracted_run['postingDateParsed']
        del extracted_run['postingDateParsed']
        extracted_run['id'] = str(uuid.uuid4())
        extracted_run['status'] = 'new'
        extracted_run['source'] = 'indeed'
        extracted_listings.append(extracted_run)
        print(listing.keys())
    return extracted_listings
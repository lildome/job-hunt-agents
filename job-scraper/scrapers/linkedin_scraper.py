from apify_client import ApifyClient
from datetime import datetime, timezone
from urllib.parse import urlencode
import uuid

LINKEDIN_PARAM_MAP = {
    "keywords":      ("keywords", lambda v: v),
    "location":      ("location", lambda v: v),
    "remote":        ("f_WT",    {"onsite": "1", "remote": "2", "hybrid": "3"}),
    "posted_within": ("f_TPR",   {"day": "r86400", "3days": "r259200", "week": "r604800", "month": "r2592000"}),
    "sort_by":       ("sortBy",  {"date": "DD", "relevance": "R"}),
}


def build_linkedin_search_url(search_params: dict) -> str:
    unknown = set(search_params) - set(LINKEDIN_PARAM_MAP)
    if unknown:
        valid = ", ".join(sorted(LINKEDIN_PARAM_MAP))
        raise ValueError(f"Unknown search param(s): {unknown}. Valid params: {valid}")

    if "keywords" not in search_params:
        raise ValueError("'keywords' is required in search_params")
    if "location" not in search_params:
        raise ValueError("'location' is required in search_params")

    params = dict(search_params)
    params.setdefault("sort_by", "date")

    query = {}
    for friendly, value in params.items():
        linkedin_key, transformer = LINKEDIN_PARAM_MAP[friendly]
        if isinstance(transformer, dict):
            if value not in transformer:
                allowed = ", ".join(transformer)
                raise ValueError(f"Invalid value '{value}' for '{friendly}'. Allowed: {allowed}")
            query[linkedin_key] = transformer[value]
        else:
            query[linkedin_key] = transformer(value)

    return f"https://www.linkedin.com/jobs/search/?{urlencode(query)}"


def scrape_linkedin(apify_client: ApifyClient, search_params: dict, count: int) -> list:
    try:
        search_url = build_linkedin_search_url(search_params)
    except ValueError as e:
        print(f"Invalid LinkedIn search params: {e}")
        return []

    run_input = {
        "urls": [search_url],
        "count": count,
        "scrapeCompany": False,
    }

    try:
        run = apify_client.actor("curious_coder/linkedin-jobs-scraper").call(run_input=run_input)
    except Exception as e:
        print(f"Error occurred while scraping LinkedIn: {e}")
        return []

    results = []
    for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
        results.append({
            "positionName": item.get("title", ""),
            "company":      item.get("companyName", ""),
            "location":     item.get("location", ""),
            "url":          item.get("link", ""),
            "salary":       item.get("salary", ""),
            "description":  item.get("descriptionText", ""),
            "postingDate":  item.get("postedAt", ""),
            "scrapedAt":    datetime.now(timezone.utc).isoformat(),
            "id":           str(uuid.uuid4()),
            "status":       "new",
            "source":       "linkedin",
        })

    return results


if __name__ == "__main__":
    sample = {
        "keywords": "software engineer",
        "location": "Melbourne, Australia",
        "remote": "remote",
        "posted_within": "day",
    }
    url = build_linkedin_search_url(sample)
    print(url)

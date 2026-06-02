import logging
import uuid
from datetime import datetime, timezone

from apify_client import ApifyClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ACTOR_ID = "websift/seek-job-scraper"

# Map our friendly enum values to Seek's expected values.
WORK_ARRANGEMENT_MAP = {
    "onsite": "On-site",
    "remote": "Remote",
    "hybrid": "Hybrid",
}

# Map LinkedIn-style enum to Seek's number-of-days field.
POSTED_WITHIN_MAP = {
    "day": 1,
    "3days": 3,
    "week": 7,
    "month": 30,
}


def _build_run_input(search_params: dict, count: int) -> dict:
    """Translate our friendly search params into the actor's expected input shape.

    Only fields the caller actually supplied are passed through, so the actor
    uses its own defaults for anything we don't specify (rather than us guessing
    at what 'no preference' looks like for radius, work arrangement, etc).
    """
    run_input = {
        "searchTerm": search_params["keywords"],
        "location": search_params["location"],
        "maxResults": count,
        "sortBy": "date",
    }

    if "remote" in search_params:
        # Validated upstream in the API layer, but defend here too.
        if search_params["remote"] not in WORK_ARRANGEMENT_MAP:
            raise ValueError(
                f"Invalid 'remote' value: {search_params['remote']}. "
                f"Allowed: {sorted(WORK_ARRANGEMENT_MAP)}"
            )
        run_input["workArrangements"] = [WORK_ARRANGEMENT_MAP[search_params["remote"]]]

    if "posted_within" in search_params:
        if search_params["posted_within"] not in POSTED_WITHIN_MAP:
            raise ValueError(
                f"Invalid 'posted_within' value: {search_params['posted_within']}. "
                f"Allowed: {sorted(POSTED_WITHIN_MAP)}"
            )
        run_input["dateRange"] = POSTED_WITHIN_MAP[search_params["posted_within"]]

    if "state" in search_params and search_params["state"]:
        run_input["state"] = search_params["state"]
    if "postCode" in search_params and search_params["postCode"]:
        run_input["postCode"] = search_params["postCode"]
    if "radius" in search_params and search_params["radius"]:
        run_input["radius"] = search_params["radius"]

    return run_input


def _resolve_company(item: dict) -> str:
    """Prefer the hiring company profile, fall back to the advertiser.

    Seek frequently lists the advertiser as a recruitment agency and the
    companyProfile.name as 'N/A' when the actual hiring company isn't named.
    Treat the literal string 'N/A' and missing/empty as equivalent.
    """
    company_profile = item.get("companyProfile") or {}
    profile_name = company_profile.get("name")
    if profile_name and profile_name != "N/A":
        return profile_name
    advertiser = item.get("advertiser") or {}
    return advertiser.get("name", "")


def _resolve_description(item: dict) -> str:
    """Join the sections array with newlines.

    sections is the full description already broken into plain-text snippets
    in reading order — no HTML, no parsing required. Falls back to empty
    string if absent.
    """
    content = item.get("content") or {}
    sections = content.get("sections") or []
    return "\n".join(s for s in sections if s)


def _resolve_location(item: dict) -> str:
    """Use displayLocation ('City STATE') to match Indeed's format."""
    location_info = item.get("joblocationInfo") or {}
    return location_info.get("displayLocation", "")


def scrape_seek(apify_client: ApifyClient, search_params: dict, count: int) -> list:
    try:
        run_input = _build_run_input(search_params, count)
    except ValueError as e:
        logger.error(f"Invalid Seek search params: {e}")
        return []

    try:
        run = apify_client.actor(ACTOR_ID).call(run_input=run_input)
    except Exception as e:
        logger.error(f"Error scraping Seek: {e}")
        return []

    results = []
    for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
        results.append({
            "positionName": item.get("title", ""),
            "company": _resolve_company(item),
            "location": _resolve_location(item),
            "url": item.get("jobLink", ""),
            "salary": item.get("salary", ""),
            "description": _resolve_description(item),
            "postingDate": item.get("listedAt", ""),
            "scrapedAt": datetime.now(timezone.utc).isoformat(),
            "id": f"job_{uuid.uuid4()}",
            "status": "new",
            "source": "seek",
        })

    return results

from datetime import date, datetime

MATCH_SCORE_PROXIMITY = 1          # scores must be within this many points to be a duplicate


def normalise_title(title: str) -> str:
    return title.lower().strip()


def normalise_posting_date(raw, source: str) -> 'str | None':
    if not raw:
        return None
    try:
        s = str(raw).strip()
        # Try ISO format first (covers Indeed's postingDateParsed and Seek's listedAt)
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ',
                    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(s[:len(fmt) + 6], fmt).date().isoformat()
            except ValueError:
                pass
        # fromisoformat handles most ISO variants in Python 3.7+
        parsed = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return parsed.date().isoformat()
    except Exception:
        return None


def titles_match(norm_a: str, norm_b: str) -> bool:
    return norm_a == norm_b


def is_duplicate(incoming: dict, candidate: dict) -> 'tuple[bool, str | None]':
    norm_a = normalise_title(incoming['title'])
    norm_b = normalise_title(candidate['title'])

    if not titles_match(norm_a, norm_b):
        return (False, None)

    if abs(incoming['match_score'] - candidate['match_score']) > MATCH_SCORE_PROXIMITY:
        return (False, None)

    return (True, 'exact')


def classify_type(incoming: dict, candidate: dict) -> int:
    if incoming.get('source') != candidate.get('source'):
        return 2

    date_a = normalise_posting_date(incoming.get('posting_date'), incoming.get('source', ''))
    date_b = normalise_posting_date(candidate.get('posting_date'), candidate.get('source', ''))

    if date_a is None or date_b is None:
        return 2

    return 1 if date_a == date_b else 2

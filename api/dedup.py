import re
import string
from datetime import date, datetime

MATCH_SCORE_PROXIMITY = 1          # scores must be within this many points to be a duplicate
FUZZY_CONTAINMENT_THRESHOLD = 0.8  # token-containment ratio above which fuzzy titles match

_SENIORITY_TOKENS = {
    "senior", "staff", "lead", "principal", "junior", "graduate",
    "sr", "jr", "entry", "mid", "associate", "intern",
}

_LOCATION_SUFFIX_RE = re.compile(
    r'[\-–—,]\s*[A-Z][^,\-–—]*$',
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r'\([^)]*\)')


def normalise_title(title: str) -> str:
    # strip parenthetical qualifiers before anything else
    t = _PAREN_RE.sub('', title)
    # strip trailing location suffix (after -, –, —, or ,)
    t = _LOCATION_SUFFIX_RE.sub('', t)
    # lowercase
    t = t.lower()
    # remove punctuation (but keep spaces)
    t = t.translate(str.maketrans('', '', string.punctuation))
    # collapse whitespace
    t = ' '.join(t.split())
    return t


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


def token_containment(norm_title_a: str, norm_title_b: str) -> float:
    tokens_a = set(norm_title_a.split())
    tokens_b = set(norm_title_b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    return len(intersection) / min(len(tokens_a), len(tokens_b))


def titles_match(norm_a: str, norm_b: str) -> bool:
    if norm_a == norm_b:
        return True
    return token_containment(norm_a, norm_b) >= FUZZY_CONTAINMENT_THRESHOLD


def is_duplicate(incoming: dict, candidate: dict) -> 'tuple[bool, str | None]':
    norm_a = normalise_title(incoming['title'])
    norm_b = normalise_title(candidate['title'])

    if not titles_match(norm_a, norm_b):
        return (False, None)

    if abs(incoming['match_score'] - candidate['match_score']) > MATCH_SCORE_PROXIMITY:
        return (False, None)

    method = 'exact' if norm_a == norm_b else 'fuzzy'
    return (True, method)


def classify_type(incoming: dict, candidate: dict) -> int:
    if incoming.get('source') != candidate.get('source'):
        return 2

    date_a = normalise_posting_date(incoming.get('posting_date'), incoming.get('source', ''))
    date_b = normalise_posting_date(candidate.get('posting_date'), candidate.get('source', ''))

    if date_a is None or date_b is None:
        return 2

    return 1 if date_a == date_b else 2

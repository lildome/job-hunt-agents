import unicodedata
import string
import re

ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def _clean_item(item: str | None) -> str:
    if item is None:
        return ""
    item = item.strip()
    # Normalize unicode characters to their closest ASCII representation
    item = unicodedata.normalize('NFKD', item).encode('ascii', 'ignore').decode('ascii')

    # Replace path seperators with spaces
    item = item.replace("/", " ").replace("\\", " ")

    # Remove illegal characters for filenames
    item = ILLEGAL_CHARS.sub('', item)

    # Strip leading and trailing whitespace and punctation
    item = item.strip(string.whitespace + string.punctuation)

    # Replace multiple spaces with a single underscore
    item = re.sub(r'\s+', '_', item)

    # Strip leading and trailing underscores
    return item.strip("_")

def _truncate_item(item: str, max_length: int) -> str:
    if len(item) <= max_length:
        return item

    item = item[:max_length]
    for i in range(len(item)-1, -1, -1):
        if item[i] == "_":
            return item[:i].rstrip("_")
    return item[:max_length].rstrip("_")

def _truncate_to_fit(company: str, role: str, max_length: int = 120, min_length: int = 15) -> tuple[str, str]:
    # Calculate how many characters we need to remove
    SUFFIX = "_Cover_Letter.pdf"  # includes leading underscore
    total_length = len(company) + 1 + len(role) + len(SUFFIX)
    excess_length = total_length - max_length

    if excess_length <= 0:
        return company, role

    role_budget = max_length - len(SUFFIX) - len(company) - 1
    if role_budget < min_length:  # Ensure we have at least `min_length` characters for the role
        role_budget = min_length
        company_budget = max_length - len(SUFFIX) - role_budget - 1
    else:
        company_budget = len(company)

    truncated_company = _truncate_item(company, company_budget)
    truncated_role = _truncate_item(role, role_budget)

    return truncated_company, truncated_role


def build_filename(company: str | None, role: str | None) -> str:
    # Remove non-alphanumeric characters and replace spaces with underscores
    company = _clean_item(company)
    role = _clean_item(role)

    if company and role:
        company, role = _truncate_to_fit(company, role)

    filename = "_".join([s for s in [company, role, "Cover_Letter.pdf"] if s])
    return filename

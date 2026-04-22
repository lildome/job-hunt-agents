import re
import html as _html

from reportlab.pdfbase import pdfmetrics


def simulate_wrap(
    text: str,
    font_name: str,
    font_size: float,
    content_width: float,
    *,
    hanging_indent: float = 0.0,
) -> list[str]:
    """Greedy word-wrap using ReportLab's pdfmetrics.stringWidth.

    Strips HTML tags and normalises HTML entities to plain-text equivalents
    before measuring. &nbsp; becomes a regular space (not \\xa0).

    hanging_indent: first line uses content_width; continuation lines use
    (content_width - hanging_indent).
    """
    # Normalise &nbsp; before html.unescape so it becomes a regular space
    clean = text.replace("&nbsp;", " ").replace("&#160;", " ")
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = _html.unescape(clean)
    # Replace any non-breaking spaces that slipped through
    clean = clean.replace("\xa0", " ")

    words = clean.split()
    if not words:
        return [""]

    def _w(s: str) -> float:
        return pdfmetrics.stringWidth(s, font_name, font_size)

    space_w = _w(" ")
    lines: list[str] = []
    current_words: list[str] = []
    current_w = 0.0
    first_line = True

    for word in words:
        word_w = _w(word)
        available = content_width if first_line else (content_width - hanging_indent)

        if current_words:
            candidate_w = current_w + space_w + word_w
            if candidate_w > available:
                lines.append(" ".join(current_words))
                current_words = [word]
                current_w = word_w
                first_line = False
            else:
                current_words.append(word)
                current_w = candidate_w
        else:
            # Start of a new line — always place the word even if it overflows
            current_words = [word]
            current_w = word_w

    if current_words:
        lines.append(" ".join(current_words))

    return lines


def is_orphan(
    lines: list[str],
    font_name: str,
    font_size: float,
    content_width: float,
    *,
    width_pct_threshold: float = 0.40,
    word_threshold: int = 2,
) -> bool:
    """Return True if the wrapped line list has an orphaned final line."""
    if len(lines) <= 1:
        return False

    last = lines[-1]
    last_w = pdfmetrics.stringWidth(last, font_name, font_size)
    pct = last_w / content_width if content_width > 0 else 0
    words = last.split()

    return pct < width_pct_threshold or len(words) <= word_threshold


def describe_orphan(
    lines: list[str],
    font_name: str,
    font_size: float,
    content_width: float,
) -> str:
    """Produce the human-readable flag string for the repair prompt.
    Example: 'final line "hardware." is 1 word, 12% width'
    """
    last = lines[-1]
    last_w = pdfmetrics.stringWidth(last, font_name, font_size)
    pct = int(round(last_w / content_width * 100)) if content_width > 0 else 0
    words = last.split()
    word_count = len(words)
    noun = "word" if word_count == 1 else "words"
    return f'final line "{last}" is {word_count} {noun}, {pct}% width'

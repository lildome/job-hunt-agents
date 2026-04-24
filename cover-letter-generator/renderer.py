"""ReportLab PDF renderer for the cover letter."""

import io
import logging

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT


logger = logging.getLogger(__name__)


# ─── Palette ───────────────────────────────────────────────────────────────────

PAGE_BG      = colors.HexColor("#FAF9F7")
TEXT_PRIMARY = colors.HexColor("#1A2E1E")
TEXT_SECOND  = colors.HexColor("#2D5A36")
TEXT_MUTED   = colors.HexColor("#6B8470")
ACCENT       = colors.HexColor("#4A9959")
RULE         = colors.HexColor("#C8D4C8")


# ─── Page geometry (US Letter: 612 × 792 pt) ───────────────────────────────────

PAGE_W, PAGE_H = LETTER

MARGIN_T = 0.45 * inch
MARGIN_B = 0.40 * inch
MARGIN_L = 0.50 * inch
MARGIN_R = 0.50 * inch

CONTENT_WIDTH: float = PAGE_W - MARGIN_L - MARGIN_R


# ─── Typography ────────────────────────────────────────────────────────────────

FONT_REG  = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

BODY_FONT_SIZE: float = 10.0
BODY_LEADING:   float = 14.0


def _style(name, **kwargs):
    base = dict(name=name, fontName=FONT_REG, fontSize=BODY_FONT_SIZE,
                leading=BODY_LEADING, textColor=TEXT_PRIMARY, alignment=TA_LEFT)
    base.update(kwargs)
    return ParagraphStyle(**base)


S_SENDER_NAME = _style("sender_name", fontName=FONT_BOLD, fontSize=11, leading=14, textColor=TEXT_PRIMARY)
S_SENDER_META = _style("sender_meta", fontSize=9, leading=11, textColor=TEXT_MUTED)
S_DATE        = _style("date",        fontSize=10, leading=13, textColor=TEXT_MUTED)
S_RECIPIENT   = _style("recipient",   fontSize=10, leading=13, textColor=TEXT_PRIMARY)
S_SALUTATION  = _style("salutation",  fontSize=10, leading=13, textColor=TEXT_PRIMARY)
S_BODY        = _style("body",        fontSize=10, leading=14, textColor=TEXT_PRIMARY)
S_CLOSING     = _style("closing",     fontSize=10, leading=13, textColor=TEXT_PRIMARY)
S_SIGNATURE   = _style("signature",   fontSize=10, leading=13, textColor=TEXT_PRIMARY)


# ─── Drawing primitives ────────────────────────────────────────────────────────

def _draw_paragraph(c, text, x, y, width, st):
    """Draw a Paragraph. y is the TOP. Returns the height drawn."""
    p = Paragraph(text, st)
    _, h = p.wrap(width, PAGE_H)
    p.drawOn(c, x, y - h)
    return h


# ─── Public API ───────────────────────────────────────────────────────────────

def render_cover_letter_pdf(data: dict) -> bytes:
    """Render the cover letter dict to a single-page PDF. Returns the PDF bytes."""
    required = ("sender", "date", "recipient", "salutation",
                "body_paragraphs", "closing", "signature")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"cover letter dict missing required keys: {missing}")

    sender = data["sender"]
    recipient = data["recipient"]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.setTitle(f"{sender.get('name', '')} — Cover Letter")
    c.setAuthor(sender.get("name", ""))

    c.setFillColor(PAGE_BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    x = MARGIN_L
    w = CONTENT_WIDTH
    y = PAGE_H - MARGIN_T

    # Sender block — name bold, then meta lines
    name_h = _draw_paragraph(c, sender.get("name", ""), x, y, w, S_SENDER_NAME)
    y -= name_h

    for field in ("location", "email", "phone"):
        val = sender.get(field, "").strip()
        if val:
            h = _draw_paragraph(c, val, x, y, w, S_SENDER_META)
            y -= h

    # Blank line
    y -= BODY_LEADING

    # Date
    h = _draw_paragraph(c, data["date"], x, y, w, S_DATE)
    y -= h

    # Blank line
    y -= BODY_LEADING

    # Recipient block
    recip_name = recipient.get("name", "").strip()
    recip_company = recipient.get("company", "").strip()
    if recip_name:
        h = _draw_paragraph(c, recip_name, x, y, w, S_RECIPIENT)
        y -= h
    if recip_company:
        h = _draw_paragraph(c, recip_company, x, y, w, S_RECIPIENT)
        y -= h

    # Slightly larger blank line before salutation
    y -= BODY_LEADING * 1.2

    # Salutation
    h = _draw_paragraph(c, data["salutation"], x, y, w, S_SALUTATION)
    y -= h

    # Blank line
    y -= BODY_LEADING

    # Body paragraphs
    for para in data["body_paragraphs"]:
        h = _draw_paragraph(c, para, x, y, w, S_BODY)
        y -= h + 6

    # Blank line
    y -= BODY_LEADING - 6  # already consumed 6 from last para loop

    # Closing
    h = _draw_paragraph(c, data["closing"], x, y, w, S_CLOSING)
    y -= h

    # Two blank lines (signature space)
    y -= BODY_LEADING * 2

    # Signature
    _draw_paragraph(c, data["signature"], x, y, w, S_SIGNATURE)

    if y < MARGIN_B:
        logger.warning("Cover letter content overflows one page (y=%.1f pt)", y)

    c.showPage()
    c.save()

    return buf.getvalue()

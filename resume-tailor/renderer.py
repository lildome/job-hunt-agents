"""ReportLab PDF renderer for the tailored resume."""

import io

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT


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
FONT_OBL  = "Helvetica-Oblique"

BULLET_FONT_SIZE: float  = 9.0
SUMMARY_FONT_SIZE: float = 10.0
BULLET_INDENT: float     = 12.0   # leftIndent / hanging indent for bullets


def _style(name, **kwargs):
    base = dict(name=name, fontName=FONT_REG, fontSize=9.5, leading=13,
                textColor=TEXT_PRIMARY, alignment=TA_LEFT)
    base.update(kwargs)
    return ParagraphStyle(**base)


S_NAME       = _style("name",       fontName=FONT_BOLD, fontSize=24, leading=26, textColor=TEXT_PRIMARY)
S_CONTACT    = _style("contact",    fontSize=9, leading=12, textColor=TEXT_SECOND)
S_SECTION    = _style("section",    fontName=FONT_BOLD, fontSize=10.5, leading=13,
                      textColor=TEXT_PRIMARY, spaceBefore=0, spaceAfter=0)
S_SUMMARY    = _style("summary",    fontSize=SUMMARY_FONT_SIZE, leading=14, textColor=TEXT_PRIMARY)
S_JOB_HEAD   = _style("job_head",   fontName=FONT_BOLD, fontSize=10.5, leading=13, textColor=TEXT_PRIMARY)
S_JOB_META   = _style("job_meta",   fontSize=9.5, leading=12, textColor=TEXT_SECOND)
S_BULLET     = _style("bullet",     fontSize=BULLET_FONT_SIZE, leading=12, textColor=TEXT_PRIMARY,
                      leftIndent=BULLET_INDENT, firstLineIndent=-BULLET_INDENT)
S_SKILL_CAT  = _style("skill_cat",  fontName=FONT_BOLD, fontSize=9.5, leading=12, textColor=TEXT_SECOND)
S_SKILL_LIST = _style("skill_list", fontSize=9.5, leading=12.5, textColor=TEXT_PRIMARY)
S_EDU_INST   = _style("edu_inst",   fontName=FONT_BOLD, fontSize=10.5, leading=13, textColor=TEXT_PRIMARY)
S_EDU_BODY   = _style("edu_body",   fontSize=10, leading=13, textColor=TEXT_PRIMARY)


# ─── Layout helpers ────────────────────────────────────────────────────────────

def get_bullet_layout_params() -> dict:
    """Params for simulate_wrap / is_orphan calls on bullet items."""
    return {
        "font_name": FONT_REG,
        "font_size": BULLET_FONT_SIZE,
        "content_width": CONTENT_WIDTH,
        "hanging_indent": BULLET_INDENT,
    }


def get_summary_layout_params() -> dict:
    """Params for simulate_wrap / is_orphan calls on the summary."""
    return {
        "font_name": FONT_REG,
        "font_size": SUMMARY_FONT_SIZE,
        "content_width": CONTENT_WIDTH,
        "hanging_indent": 0.0,
    }


# ─── Drawing primitives ────────────────────────────────────────────────────────

def _draw_paragraph(c, text, x, y, width, st):
    """Draw a Paragraph. y is the TOP. Returns the height drawn."""
    p = Paragraph(text, st)
    _, h = p.wrap(width, PAGE_H)
    p.drawOn(c, x, y - h)
    return h


def _draw_section_heading(c, text, x, y, width):
    """Section heading + thin full-width rule beneath. Returns total height consumed."""
    h = _draw_paragraph(c, text.upper(), x, y, width, S_SECTION)
    rule_y = y - h - 3
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(x, rule_y, x + width, rule_y)
    return h + 8  # heading + rule gap + spacing below


# ─── Section renderers ────────────────────────────────────────────────────────

def _draw_header(c, y_top, resume):
    x = MARGIN_L
    w = CONTENT_WIDTH
    y = y_top

    c.setFillColor(TEXT_PRIMARY)
    c.setFont(FONT_BOLD, 24)
    c.drawString(x, y - 22, resume["name"])
    y -= 30

    contact = resume["contact"]
    sep = "&nbsp;&nbsp;·&nbsp;&nbsp;"
    parts = [v.strip() for v in [
        contact.get("location", ""),
        contact.get("phone", ""),
        contact.get("email", ""),
        contact.get("linkedin", ""),
        contact.get("github", ""),
    ] if v.strip()]
    contact_line = sep.join(parts)
    h = _draw_paragraph(c, contact_line, x, y, w, S_CONTACT)
    y -= h + 10

    c.setStrokeColor(ACCENT)
    c.setLineWidth(0.8)
    c.line(x, y, x + w, y)

    return y - 12


def _draw_summary(c, y_top, resume):
    x = MARGIN_L
    w = CONTENT_WIDTH
    y = y_top

    h = _draw_section_heading(c, "Summary", x, y, w)
    y -= h
    body_h = _draw_paragraph(c, resume["summary"], x, y, w, S_SUMMARY)
    return y - body_h - 10


def _draw_experience(c, y_top, resume):
    x = MARGIN_L
    w = CONTENT_WIDTH
    y = y_top

    h = _draw_section_heading(c, "Experience", x, y, w)
    y -= h

    for job in resume["experience"]:
        period_w = c.stringWidth(job["period"], FONT_REG, 9.5)
        c.setFont(FONT_REG, 9.5)
        c.setFillColor(TEXT_SECOND)
        c.drawString(x + w - period_w, y - 11, job["period"])

        company_p = Paragraph(f"<b>{job['company']}</b>", S_JOB_HEAD)
        company_h = company_p.wrap(w - period_w - 12, PAGE_H)[1]
        company_p.drawOn(c, x, y - company_h)
        y -= company_h + 1

        role_h = _draw_paragraph(c, job["role"], x, y, w, S_JOB_META)
        y -= role_h + 5

        for b in job["bullets"]:
            text = "•&nbsp;&nbsp;" + b
            bh = _draw_paragraph(c, text, x, y, w, S_BULLET)
            y -= bh + 2

        y -= 6

    return y - 2


def _draw_projects(c, y_top, resume):
    x = MARGIN_L
    w = CONTENT_WIDTH
    y = y_top

    h = _draw_section_heading(c, "Projects", x, y, w)
    y -= h

    for proj in resume["projects"]:
        title_p = Paragraph(f"<b>{proj['title']}</b>", S_JOB_HEAD)
        title_h = title_p.wrap(w, PAGE_H)[1]
        title_p.drawOn(c, x, y - title_h)
        y -= title_h + 1

        sub_h = _draw_paragraph(c, proj["subtitle"], x, y, w, S_JOB_META)
        y -= sub_h + 5

        for b in proj["bullets"]:
            text = "•&nbsp;&nbsp;" + b
            bh = _draw_paragraph(c, text, x, y, w, S_BULLET)
            y -= bh + 2

    return y - 6


def _draw_skills(c, y_top, resume):
    x = MARGIN_L
    w = CONTENT_WIDTH
    y = y_top

    h = _draw_section_heading(c, "Skills", x, y, w)
    y -= h

    skills = resume["skills"]
    col_w = (w - 16) / 2
    col2_x = x + col_w + 16

    for i in range(0, len(skills), 2):
        row = skills[i: i + 2]
        row_max_h = 0

        for j, sg in enumerate(row):
            col_x = x if j == 0 else col2_x
            label_w = 78
            list_x = col_x + label_w
            list_w = col_w - label_w

            label_h = _draw_paragraph(c, sg["category"], col_x, y, label_w, S_SKILL_CAT)
            list_h  = _draw_paragraph(c, " · ".join(sg["items"]), list_x, y, list_w, S_SKILL_LIST)
            row_max_h = max(row_max_h, label_h, list_h)

        y -= row_max_h + 5

    return y - 6


def _draw_education(c, y_top, resume):
    x = MARGIN_L
    w = CONTENT_WIDTH
    y = y_top

    h = _draw_section_heading(c, "Education", x, y, w)
    y -= h

    edu = resume["education"]
    year_w = c.stringWidth(edu["year"], FONT_REG, 9.5)
    c.setFont(FONT_REG, 9.5)
    c.setFillColor(TEXT_SECOND)
    c.drawString(x + w - year_w, y - 11, edu["year"])

    inst_p = Paragraph(edu["institution"], S_EDU_INST)
    inst_h = inst_p.wrap(w - year_w - 12, PAGE_H)[1]
    inst_p.drawOn(c, x, y - inst_h)
    y -= inst_h + 3

    for degree in edu["degrees"]:
        deg_h = _draw_paragraph(c, f"<b>{degree}</b>", x, y, w, S_EDU_BODY)
        y -= deg_h + 1

    return y - 10


# ─── Public API ───────────────────────────────────────────────────────────────

def render_pdf(resume: dict) -> bytes:
    """Render the resume dict to a single-page PDF. Returns the PDF bytes."""
    required = {"name", "contact", "summary", "experience", "skills", "education", "projects"}
    missing = required - resume.keys()
    if missing:
        raise ValueError(f"resume dict missing required keys: {missing}")

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.setTitle(f"{resume['name']} — Resume")
    c.setAuthor(resume["name"])

    c.setFillColor(PAGE_BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    y = PAGE_H - MARGIN_T
    y = _draw_header(c, y, resume)
    y = _draw_summary(c, y, resume)
    y = _draw_experience(c, y, resume)
    y = _draw_skills(c, y, resume)
    y = _draw_education(c, y, resume)
    y = _draw_projects(c, y, resume)

    c.showPage()
    c.save()

    return buf.getvalue()

"""
Arabic patient-report PDF builder (RTL, deterministic).
=======================================================
Mirrors report_builder.build_patient_report but renders the ARABIC patient block (already
translated + gated by services.arabic) right-to-left. reportlab has no native Arabic shaping
or bidi, so every Arabic string is run through `_shape_ar` (arabic_reshaper → LTR-isolate the
embedded clinical tokens → python-bidi get_display) before it reaches a Paragraph.

CRITICAL (verified empirically): without LTR-isolation, "6 mm" reverses/splits in the RTL flow
while the screen looks fine. `_isolate_ltr` wraps every Latin/number run (level labels, mm
values, FLAIR/T2, figure refs) in LRE…PDF embedding marks so they stay intact. python-bidi
0.4.2 (the pinned pure-Python release) does NOT support LRI/PDI isolates — embedding marks are
the compatible mechanism.

Fields already in English (a gate fallback) are rendered LTR, left-aligned, untouched — so a
mixed report is never mangled by forcing RTL onto an English sentence.

⚠️  Requires an Arabic-capable TTF. Ships flag-dark; the Electron bundle MUST include an OFL
    font (Noto Naskh Arabic). Without a font the build still succeeds (no crash) but Arabic
    glyphs won't embed — caught by the human render gate, never shipped silently.
"""

from pathlib import Path
from typing import Optional
import logging
import os
import re

logger = logging.getLogger("mika.report_builder_ar")

try:
    from core.palette import CERTAINTY_COLOR, CERTAINTY_ORDER, normalize_certainty
    from prompts.i18n_glossary import PDF_SECTIONS_AR, CERTAINTY_AR, CONFIDENCE_AR
except ImportError:  # pragma: no cover
    from backend.core.palette import CERTAINTY_COLOR, CERTAINTY_ORDER, normalize_certainty
    from backend.prompts.i18n_glossary import PDF_SECTIONS_AR, CERTAINTY_AR, CONFIDENCE_AR

INK = (0.059, 0.090, 0.165)
MUTED = (0.278, 0.333, 0.412)
ACCENT = (0.145, 0.388, 0.922)
CONF_COLOR = {"High": (0.145, 0.388, 0.922), "Moderate": (0.553, 0.604, 0.690), "Low": (0.620, 0.659, 0.722)}
LOGO_BG = (0.0, 0.012, 0.055)
SLATE_LT = (0.580, 0.639, 0.722)
HAIRLINE = (0.85, 0.86, 0.88)
_BRAND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "assets" / "brand"
_LOGO = _BRAND_DIR / "mika-header.png"

_AR_FONT_NAME = "MikaArabic"
_AR_RANGE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
# A Latin/number run (incl. "6 mm", "L4-L5", "FLAIR", "12.5%") kept atomic + LTR.
_LTR_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,%/\-]*[A-Za-z0-9%]|[A-Za-z0-9]")
_LRE, _PDF = "‪", "‬"   # left-to-right embedding … pop directional formatting


def _has_arabic(s: str) -> bool:
    return bool(_AR_RANGE.search(s or ""))


def _isolate_ltr(t: str) -> str:
    return _LTR_RUN.sub(lambda m: _LRE + m.group(0) + _PDF, t or "")


def _shape_ar(t: str) -> str:
    """reshape Arabic glyphs → isolate embedded LTR clinical tokens → bidi reorder for display."""
    if not _has_arabic(t):
        return t or ""               # pure English fallback: leave LTR, untouched
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(_isolate_ltr(t)))


def _register_font() -> str:
    """Register an Arabic TTF; return the font name to use (falls back to Helvetica if none)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        os.environ.get("MIKA_AR_FONT"),
        str(Path(__file__).resolve().parents[2] / "frontend" / "assets" / "fonts" / "NotoNaskhArabic-Regular.ttf"),
        "C:/Windows/Fonts/arial.ttf",   # dev fallback (Arial has Arabic glyphs); NOT the shipped font
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(_AR_FONT_NAME, path))
                return _AR_FONT_NAME
            except Exception as exc:   # pragma: no cover
                logger.warning("Arabic font %s failed to register: %s", path, exc)
    logger.warning("No Arabic font found — Arabic glyphs will NOT render (bundle Noto Naskh Arabic). "
                   "This must be caught by the human render gate before shipping.")
    return "Helvetica"


def build_patient_report_ar(ar_patient: dict, figures_dir, out_pdf) -> str:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, HRFlowable, KeepTogether,
    )
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT
    from reportlab.lib.utils import ImageReader

    figures_dir = Path(figures_dir)
    out_pdf = str(out_pdf)
    font = _register_font()
    if not isinstance(ar_patient, dict):
        ar_patient = {}

    def c(rgb):
        return colors.Color(*rgb)

    styles = getSampleStyleSheet()
    SECTION = ParagraphStyle("SECTION", parent=styles["Normal"], fontName=font,
                             fontSize=12, leading=18, textColor=c(ACCENT), spaceBefore=14,
                             spaceAfter=6, alignment=TA_RIGHT)
    BIG = ParagraphStyle("BIG", parent=styles["Normal"], fontName=font, fontSize=15, leading=24,
                         textColor=c(INK), spaceAfter=4, alignment=TA_RIGHT, wordWrap="RTL")
    BODY = ParagraphStyle("BODY", parent=styles["Normal"], fontName=font, fontSize=11, leading=18,
                          textColor=c(INK), spaceAfter=4, alignment=TA_RIGHT, wordWrap="RTL")
    BODY_LTR = ParagraphStyle("BODYL", parent=BODY, alignment=TA_LEFT, wordWrap=None)
    CAP = ParagraphStyle("CAP", parent=styles["Normal"], fontName=font, fontSize=9, leading=14,
                         textColor=c(MUTED), spaceAfter=10, alignment=TA_RIGHT, wordWrap="RTL")
    SMALL = ParagraphStyle("SMALL", parent=styles["Normal"], fontName=font, fontSize=8.5,
                           leading=13, textColor=c(MUTED), alignment=TA_RIGHT, wordWrap="RTL")

    def P(text, style=BODY):
        """Shape + direction-correct: Arabic → RTL style; an English fallback → LTR."""
        text = str(text or "")
        if text and not _has_arabic(text):
            ltr = ParagraphStyle("x", parent=style, alignment=TA_LEFT, wordWrap=None)
            return Paragraph(text, ltr)
        return Paragraph(_shape_ar(text), style)

    def bullets(items, style=BODY):
        if isinstance(items, str):
            items = [items] if items.strip() else []
        elif not isinstance(items, (list, tuple)):
            items = [items] if items else []
        return [P(str(it), style) for it in (items or []) if str(it).strip()]

    def _img(fig_name, max_w=6.6 * inch, max_h=6.2 * inch):
        if not fig_name:
            return None
        p = figures_dir / Path(str(fig_name)).name
        if not p.exists():
            return None
        try:
            iw, ih = ImageReader(str(p)).getSize()
            scale = min(max_w / iw, max_h / ih)
            return Image(str(p), width=iw * scale, height=ih * scale)
        except Exception:
            return None

    flow = []
    # Branded header (logo asset — not mirrored; a flipped logo is wrong) with RTL caption.
    logo_cell, lh = "", 1.25 * inch
    if _LOGO.exists():
        try:
            liw, lih = ImageReader(str(_LOGO)).getSize()
            logo_cell = Image(str(_LOGO), width=lh * liw / lih, height=lh)
        except Exception:
            logo_cell = ""
    DESC = ParagraphStyle("DC", parent=styles["Normal"], fontName=font, fontSize=9, leading=14,
                          textColor=c(SLATE_LT), alignment=TA_LEFT)
    band_h = lh + 0.30 * inch
    header = Table([[P("تقرير تحليل\nالصور", DESC), logo_cell]],
                   colWidths=[2.2 * inch, 4.5 * inch], rowHeights=[band_h])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), c(LOGO_BG)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 16), ("RIGHTPADDING", (-1, 0), (-1, 0), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(header)
    flow.append(Spacer(1, 7))
    flow.append(HRFlowable(width="100%", thickness=1.4, color=c(ACCENT), spaceAfter=10))

    # 1. Summary
    flow.append(P(PDF_SECTIONS_AR["summary"], SECTION))
    answer = Table([[P(ar_patient.get("bottom_line", ""), BIG)]], colWidths=[6.6 * inch])
    answer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), c((0.93, 0.955, 1.0))),
        ("LINEAFTER", (0, 0), (0, -1), 3, c(ACCENT)),   # accent bar on the RIGHT (RTL start edge)
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    flow.append(answer)
    if ar_patient.get("key_points"):
        flow.append(Spacer(1, 6))
        flow.extend(bullets(ar_patient["key_points"]))

    # 2. Confidence
    conf = ar_patient.get("confidence") if isinstance(ar_patient.get("confidence"), dict) else {}
    if conf and (conf.get("label") or conf.get("note")):
        flow.append(P(PDF_SECTIONS_AR["confidence"], SECTION))
        key = conf.get("label_key", "Moderate")
        flow.append(P(f"<b>{conf.get('label','')}</b> — {conf.get('note','')}", BODY))

    # 3. Findings
    findings = ar_patient.get("findings") or []
    if isinstance(findings, dict):
        findings = [findings]
    header_pending = True
    for f in findings:
        if not isinstance(f, dict):
            continue
        block = []
        if header_pending:
            block.append(P(PDF_SECTIONS_AR["findings"], SECTION))
            block.append(P(PDF_SECTIONS_AR["legend"], SMALL))
            block.append(Spacer(1, 8))
            header_pending = False
        cert_key = normalize_certainty(f.get("certainty_key") or f.get("certainty"))
        cert_col = CERTAINTY_COLOR.get(cert_key, MUTED)          # color by ENGLISH tier
        cert_text = f.get("certainty") or CERTAINTY_AR.get(cert_key, "")
        # RTL: certainty chip on the LEFT, finding text (right-aligned) on the RIGHT.
        row = Table([[Paragraph(f'<font color="white"><b>{_shape_ar(cert_text)}</b></font>', SMALL),
                      P(f.get("plain", ""), BODY)]],
                    colWidths=[1.5 * inch, 5.1 * inch])
        row.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), c(cert_col)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (0, 0), "CENTER"),
            ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, 0), (0, 0), 8),
        ]))
        block.append(row)
        im = _img(f.get("figure"))
        if im is not None:
            block.append(Spacer(1, 6)); block.append(im)
            if f.get("caption"):
                block.append(P(f["caption"], CAP))
        block.append(Spacer(1, 14))
        flow.append(KeepTogether(block))

    # 4. Change over time
    cot = ar_patient.get("change_over_time") if isinstance(ar_patient.get("change_over_time"), dict) else None
    if cot and (cot.get("points") or cot.get("plain")):
        flow.append(P(PDF_SECTIONS_AR["change_over_time"], SECTION))
        flow.extend(bullets(cot.get("points")) or [P(cot.get("plain", ""))])
        im = _img(cot.get("figure"))
        if im is not None:
            flow.append(Spacer(1, 4)); flow.append(im)

    # 5. What this may mean
    if ar_patient.get("what_it_means"):
        flow.append(P(PDF_SECTIONS_AR["what_it_means"], SECTION))
        flow.extend(bullets(ar_patient["what_it_means"]))

    # 6. Notes
    if ar_patient.get("worth_flagging"):
        flow.append(P(PDF_SECTIONS_AR["notes"], SECTION))
        flow.extend(bullets(ar_patient["worth_flagging"]))

    # Disclaimer (frozen Arabic)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=c((0.85, 0.85, 0.85)), spaceAfter=6))
    flow.append(P(ar_patient.get("disclaimer", ""), SMALL))
    flow.append(Spacer(1, 4))
    flow.append(P(PDF_SECTIONS_AR["clinician_note"], SMALL))

    def _decorate(canvas, doc_):
        canvas.saveState()
        w, _h = LETTER
        canvas.setStrokeColor(c(HAIRLINE)); canvas.setLineWidth(0.6)
        canvas.line(0.9 * inch, 0.62 * inch, w - 0.9 * inch, 0.62 * inch)
        canvas.setFont(font, 7.5); canvas.setFillColor(c(MUTED))
        canvas.drawString(0.9 * inch, 0.46 * inch, "MIKA")
        canvas.drawRightString(w - 0.9 * inch, 0.46 * inch, f"{doc_.page}")
        canvas.restoreState()

    SimpleDocTemplate(out_pdf, pagesize=LETTER,
                      leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                      topMargin=0.7 * inch, bottomMargin=0.85 * inch,
                      title="MIKA — تقرير تحليل الصور").build(
                          flow, onFirstPage=_decorate, onLaterPages=_decorate)
    return out_pdf

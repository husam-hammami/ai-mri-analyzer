"""
Patient-first report builder (deterministic).
=============================================
Renders the USER-FACING PDF from a patient-report dict (plain-language answer + simple
confidence + findings with proof images). The clinical rigor (tiers, intensity checks,
self-audit, reconciliation mechanics) stays in summary.json / a separate clinician file and
is NEVER shown here. This guarantees a consistent patient-first report on every run, instead
of relying on the model's prose discipline.

What the user sees, in order:
  1. THE BOTTOM LINE   - one or two plain sentences: what the scan shows.
  2. HOW SURE WE ARE   - one simple confidence score/label.
  3. WHAT WE FOUND     - plain findings, each with its proof image + a simple certainty word.
  4. WHAT CHANGED      - plain longitudinal summary (if prior studies) + figure.
  5. WHAT THIS MEANS   - plain, non-prescriptive next-step pointers.
  6. WORTH FLAGGING    - optional plain notes (e.g. a record discrepancy).
  7. Disclaimer.

patient dict schema (all plain language; produced by the analysis step):
{
  "patient": {"name","age","sex"},
  "study": {"body_part","modality","date","comparison"},
  "bottom_line": "1-2 plain sentences",
  "confidence": {"label":"High|Moderate|Low","score": 0-100 (optional), "note":"one plain line"},
  "findings": [{"plain":"...","certainty":"Confirmed|Likely|Possible","figure":"fileX.png","caption":"plain"}],
  "change_over_time": {"plain":"...","figure":"figure4_longitudinal.png"} (optional),
  "what_it_means": ["plain bullet", ...],
  "worth_flagging": ["plain note", ...] (optional),
  "disclaimer": "..."
}
"""

from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

import logging

logger = logging.getLogger("mika.report_builder")

try:  # shared certainty palette lives in core so the report chips, on-image marks and legend never drift
    from core.palette import CERTAINTY_COLOR, CERTAINTY_ORDER
except ImportError:  # pragma: no cover - import path when launched from backend/
    from backend.core.palette import CERTAINTY_COLOR, CERTAINTY_ORDER

# Single-accent brand palette (§7.9) — matches the on-screen Read (#2563EB), no teal/amber/grey.
INK = (0.059, 0.090, 0.165)        # slate-ink #0F172A
MUTED = (0.278, 0.333, 0.412)      # slate #475569
ACCENT = (0.145, 0.388, 0.922)     # #2563EB (the only accent)
# CERTAINTY_COLOR is imported from core.palette (the single source of truth).
CONF_COLOR = {"High": (0.145, 0.388, 0.922), "Moderate": (0.553, 0.604, 0.690), "Low": (0.620, 0.659, 0.722)}

# Brand header — the EXACT MIKA lockup asset (cropped from frontend/assets/logo.png), not a re-render.
LOGO_BG = (0.0, 0.012, 0.055)       # logo.png's own navy, so the asset blends into the band seamlessly
SLATE_LT = (0.580, 0.639, 0.722)    # #94A3B8 — secondary text on the navy header
HAIRLINE = (0.85, 0.86, 0.88)
_BRAND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "assets" / "brand"
_LOGO = _BRAND_DIR / "mika-header.png"   # exact lockup (mark + wordmark + tagline), real brand pixels


def build_patient_report(patient: dict, figures_dir, out_pdf) -> str:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, HRFlowable, KeepTogether,
    )
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.utils import ImageReader

    figures_dir = Path(figures_dir)
    out_pdf = str(out_pdf)

    def c(rgb):
        return colors.Color(*rgb)

    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=styles["Title"], fontName="Helvetica-Bold",
                        fontSize=20, leading=24, textColor=c(INK), spaceAfter=2, alignment=TA_LEFT)
    SUB = ParagraphStyle("SUB", parent=styles["Normal"], fontName="Helvetica",
                         fontSize=10, leading=14, textColor=c(MUTED), spaceAfter=10)
    SECTION = ParagraphStyle("SECTION", parent=styles["Normal"], fontName="Helvetica-Bold",
                             fontSize=12, leading=16, textColor=c(ACCENT), spaceBefore=14, spaceAfter=6)
    BIG = ParagraphStyle("BIG", parent=styles["Normal"], fontName="Helvetica",
                         fontSize=15, leading=21, textColor=c(INK), spaceAfter=4)
    BODY = ParagraphStyle("BODY", parent=styles["Normal"], fontName="Helvetica",
                          fontSize=11, leading=16, textColor=c(INK), spaceAfter=4)
    CAP = ParagraphStyle("CAP", parent=styles["Normal"], fontName="Helvetica-Oblique",
                         fontSize=9, leading=12, textColor=c(MUTED), spaceAfter=10)
    SMALL = ParagraphStyle("SMALL", parent=styles["Normal"], fontName="Helvetica",
                           fontSize=8.5, leading=12, textColor=c(MUTED))
    BULLET = ParagraphStyle("BULLET", parent=BODY, leftIndent=16, bulletIndent=2,
                            spaceBefore=1, spaceAfter=3)

    def bullets(items, style=BULLET):
        # Belt-and-suspenders (Fix 2): a string here would iterate CHARACTER-BY-CHARACTER (the
        # garbled-impression incident). Coerce any non-list shape to a list first.
        if isinstance(items, str):
            items = [items] if items.strip() else []
        elif not isinstance(items, (list, tuple)):
            items = [items] if items else []
        out = []
        for it in (items or []):
            out.append(Paragraph(str(it), style, bulletText="•"))
        return out

    # A wrong top-level shape skips sections rather than crashing the render.
    if not isinstance(patient, dict):
        patient = {}
    flow = []
    pat = patient.get("patient") if isinstance(patient.get("patient"), dict) else {}
    study = patient.get("study") if isinstance(patient.get("study"), dict) else {}

    # Branded header — the EXACT MIKA logo lockup (real asset from frontend/assets/logo.png) on its navy.
    DESC = ParagraphStyle("DC", parent=styles["Normal"], fontName="Helvetica",
                          fontSize=9, leading=12, textColor=c(SLATE_LT), alignment=2)  # right-aligned
    logo_cell = ""
    if _LOGO.exists():
        try:
            liw, lih = ImageReader(str(_LOGO)).getSize()
            lh = 1.25 * inch   # the wide lockup was squeezed to 0.8" → tiny/illegible; give it height
            logo_cell = Image(str(_LOGO), width=lh * liw / lih, height=lh)
        except Exception:
            logo_cell = ""

    header = Table([[logo_cell, Paragraph("Imaging analysis<br/>report", DESC)]],
                   colWidths=[4.5 * inch, 2.2 * inch])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), c(LOGO_BG)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 14), ("RIGHTPADDING", (-1, 0), (-1, 0), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    flow.append(header)
    flow.append(Spacer(1, 7))
    bits = [b for b in (study.get("body_part"), study.get("modality"), study.get("date")) if b]
    sub = " · ".join(([pat["name"]] if pat.get("name") else []) + bits +
                     ([study["comparison"]] if study.get("comparison") else []))
    if sub:
        flow.append(Paragraph(sub, SUB))
    flow.append(HRFlowable(width="100%", thickness=1.4, color=c(ACCENT), spaceAfter=10))

    # 1. SUMMARY
    flow.append(Paragraph("Summary", SECTION))
    answer_tbl = Table([[Paragraph(patient.get("bottom_line", ""), BIG)]], colWidths=[6.6 * inch])
    answer_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), c((0.93, 0.955, 1.0))),
        ("LINEBEFORE", (0, 0), (0, -1), 3, c(ACCENT)),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    flow.append(answer_tbl)
    # key points as bullets right under the bottom line
    kp = patient.get("key_points", [])
    if kp:
        flow.append(Spacer(1, 6))
        flow.extend(bullets(kp))

    # 2. HOW SURE WE ARE
    conf = patient.get("confidence", {})
    if not isinstance(conf, dict):
        conf = {}
    if conf:
        label = conf.get("label", "Moderate")
        score = conf.get("score")
        chip = label + (f"  -  {int(score)}%" if isinstance(score, (int, float)) else "")
        conf_block = [Paragraph("Confidence", SECTION)]
        chip_tbl = Table([[Paragraph(f'<b>{chip}</b>', BODY), Paragraph(conf.get("note", ""), BODY)]],
                         colWidths=[1.7 * inch, 4.9 * inch])
        chip_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), c(CONF_COLOR.get(label, MUTED))),
            ("TEXTCOLOR", (0, 0), (0, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 10), ("TOPPADDING", (0, 0), (0, 0), 8),
            ("BOTTOMPADDING", (0, 0), (0, 0), 8), ("LEFTPADDING", (1, 0), (1, 0), 12),
        ]))
        # White text in the colored cell: re-render label as white paragraph
        chip_white = ParagraphStyle("cw", parent=BODY, textColor=colors.white, fontName="Helvetica-Bold")
        chip_tbl._cellvalues[0][0] = Paragraph(chip, chip_white)
        conf_block.append(chip_tbl)
        flow.append(KeepTogether(conf_block))

    def _img(fig_name, max_w=6.6 * inch, max_h=3.7 * inch):
        if not fig_name:
            return None
        p = figures_dir / Path(fig_name).name   # basename: the model sometimes writes a 'report/foo.png' prefix
        if not p.exists():
            return None
        try:
            iw, ih = ImageReader(str(p)).getSize()
            scale = min(max_w / iw, max_h / ih)
            return Image(str(p), width=iw * scale, height=ih * scale)
        except Exception:
            return None

    # 3. WHAT WE FOUND
    findings = patient.get("findings", [])
    if isinstance(findings, dict):
        findings = [findings]
    elif not isinstance(findings, list):
        findings = []
    if findings:
        findings_header_pending = True
        for f in findings:
            if not isinstance(f, dict):   # skip a malformed finding rather than crash
                continue
            block = []
            if findings_header_pending:   # keep the section header with its first finding (no orphan)
                block.append(Paragraph("Findings", SECTION))
                findings_header_pending = False
                # Colour → certainty legend (so the reader isn't guessing what the colours mean).
                present = [k for k in CERTAINTY_ORDER
                           if any(isinstance(g, dict) and g.get("certainty") == k for g in findings)]
                if present:
                    cells = [[Paragraph(f'<font color="white"><b>{k}</b></font>', SMALL) for k in present]]
                    legend = Table(cells, colWidths=[(6.6 * inch) / len(present)] * len(present))
                    lstyle = [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
                        ("LEFTPADDING", (0, 0), (-1, 0), 6), ("RIGHTPADDING", (0, 0), (-1, 0), 6),
                    ]
                    for i, k in enumerate(present):
                        lstyle.append(("BACKGROUND", (i, 0), (i, 0), c(CERTAINTY_COLOR.get(k, MUTED))))
                    legend.setStyle(TableStyle(lstyle))
                    block.append(Paragraph("Colour shows how certain each finding is:", SMALL))
                    block.append(Spacer(1, 4))
                    block.append(legend)
                    block.append(Spacer(1, 12))
            cert = f.get("certainty", "")
            cert_col = CERTAINTY_COLOR.get(cert, MUTED)
            row = Table([[Paragraph(f.get("plain", ""), BULLET, bulletText="•"),
                          Paragraph(f'<font color="white"><b>{cert}</b></font>', SMALL)]],
                        colWidths=[5.1 * inch, 1.5 * inch])
            row.setStyle(TableStyle([
                ("BACKGROUND", (1, 0), (1, 0), c(cert_col)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("TOPPADDING", (1, 0), (1, 0), 8), ("BOTTOMPADDING", (1, 0), (1, 0), 8),
                ("LEFTPADDING", (1, 0), (1, 0), 8), ("RIGHTPADDING", (1, 0), (1, 0), 8),
                ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 12),
            ]))
            block.append(row)
            im = _img(f.get("figure"), max_h=6.2 * inch)   # proof figures legible (was capped at 3.7")
            if im is not None:
                block.append(Spacer(1, 6))
                block.append(im)
                if f.get("caption"):
                    block.append(Paragraph(f["caption"], CAP))
            elif f.get("figure"):
                # A finding declared a proof figure that isn't on disk — never drop it silently;
                # surface it (a finding with no visual evidence is the worst silent degradation).
                logger.warning("Finding proof figure %r missing on disk — rendering finding "
                               "without its image", f.get("figure"))
                block.append(Spacer(1, 4))
                block.append(Paragraph("<i>(proof image unavailable)</i>", CAP))
            block.append(Spacer(1, 14))
            flow.append(KeepTogether(block))

    # 3a. FOCUSED EVIDENCE REVIEW
    focused = patient.get("cv_supported_explanations", [])
    if isinstance(focused, dict):
        focused = [focused]
    elif not isinstance(focused, list):
        focused = []
    if focused:
        flow.append(Paragraph("Focused evidence review", SECTION))
        for item in focused:
            if isinstance(item, dict):
                text = item.get("plain") or item.get("explanation") or ""
            else:
                text = str(item or "")
            if text:
                flow.append(Paragraph(escape(str(text)), BODY))
        flow.append(Spacer(1, 4))

    # 3b. REFERENCE-ASSISTED REVIEW
    ref_review = patient.get("reference_reconciliation") or patient.get("reconciliation")
    if isinstance(ref_review, dict) and (ref_review.get("summary") or ref_review.get("items")):
        flow.append(Paragraph("Reference-assisted review", SECTION))
        if ref_review.get("summary"):
            flow.append(Paragraph(escape(str(ref_review["summary"])), BODY))
        items = ref_review.get("items") or []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if not isinstance(item, dict):
                continue
            label = escape(str(item.get("label") or item.get("status") or "Needs review"))
            explanation = escape(str(item.get("explanation") or ""))
            reference = escape(str(item.get("reference") or ""))
            mika = escape(str(item.get("mika") or ""))
            block = [
                Paragraph(f"<b>{label}</b>", BODY),
                Paragraph(explanation, BODY),
            ]
            if reference:
                block.append(Paragraph(f"<b>Uploaded report (report-derived):</b> {reference}", SMALL))
            if mika:
                block.append(Paragraph(f"<b>MIKA independent read:</b> {mika}", SMALL))
            block.append(Spacer(1, 6))
            flow.append(KeepTogether(block))

    # 4. WHAT CHANGED OVER TIME
    cot = patient.get("change_over_time")
    if not isinstance(cot, dict):
        cot = None
    if cot and (cot.get("points") or cot.get("plain")):
        flow.append(Paragraph("Change over time", SECTION))
        if cot.get("points"):
            flow.extend(bullets(cot["points"]))
        elif cot.get("plain"):
            flow.append(Paragraph(cot["plain"], BODY))
        im = _img(cot.get("figure"))
        if im is not None:
            flow.append(Spacer(1, 4)); flow.append(im)

    # 5. WHAT THIS MEANS
    wim = patient.get("what_it_means", [])
    if wim:
        flow.append(Paragraph("What this may mean", SECTION))
        flow.extend(bullets(wim))

    # 6. WORTH FLAGGING
    wf = patient.get("worth_flagging", [])
    if wf:
        flow.append(Paragraph("Notes", SECTION))
        flow.extend(bullets(wf))

    # Footer / disclaimer
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=c((0.85, 0.85, 0.85)), spaceAfter=6))
    flow.append(Paragraph(patient.get("disclaimer", ""), SMALL))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph("A detailed technical version is available for the referring clinician.", SMALL))

    def _decorate(canvas, doc_):          # branded footer + page number on every page
        canvas.saveState()
        w, _h = LETTER
        canvas.setStrokeColor(c(HAIRLINE)); canvas.setLineWidth(0.6)
        canvas.line(0.9 * inch, 0.62 * inch, w - 0.9 * inch, 0.62 * inch)
        canvas.setFont("Helvetica", 7.5); canvas.setFillColor(c(MUTED))
        canvas.drawString(0.9 * inch, 0.46 * inch, "MIKA  ·  Clinical Imaging Intelligence")
        canvas.drawRightString(w - 0.9 * inch, 0.46 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    SimpleDocTemplate(out_pdf, pagesize=LETTER,
                      leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                      topMargin=0.7 * inch, bottomMargin=0.85 * inch,
                      title="MIKA — Imaging analysis report").build(
                          flow, onFirstPage=_decorate, onLaterPages=_decorate)
    return out_pdf

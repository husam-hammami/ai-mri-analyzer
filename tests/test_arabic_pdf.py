"""Arabic PDF builder tests — the silent-corruption surface (reshape+bidi token integrity) and
a render smoke check. No claude; reportlab + arabic-reshaper + python-bidi only."""
from pathlib import Path

import pytest

from services.report_builder_ar import _shape_ar, _has_arabic, build_patient_report_ar


def test_clinical_tokens_survive_reshape_bidi():
    # The skeptic's catch: "6 mm" silently breaks under bidi unless LTR-isolated.
    s = "تضيّق في L4-L5 بمقدار 6 mm و 12 mm، FLAIR"
    out = _shape_ar(s)
    for tok in ("L4-L5", "6 mm", "12 mm", "FLAIR"):
        assert tok in out, f"clinical token {tok!r} corrupted by reshape+bidi"


def test_english_fallback_is_left_untouched():
    en = "No canal narrowing."
    assert _shape_ar(en) == en
    assert not _has_arabic(en)


def _ar_block():
    return {
        "bottom_line": "يوجد انتفاق قرصي خفيف في L4-L5.",
        "key_points": ["هذا أمر شائع."],
        "findings": [{
            "plain": "يوجد انتفاق قرصي خفيف في L4-L5.",
            "certainty": "مُرجَّح", "certainty_key": "Likely", "figure": "", "caption": "مستوى L4-L5.",
        }],
        "what_it_means": ["ناقش هذا مع طبيبك."],
        "confidence": {"label": "متوسطة", "label_key": "Moderate", "score": 70, "note": "بناءً على الصور."},
        "disclaimer": "هذا التقرير لا يُغني عن طبيب أشعة معتمد.",
    }


def test_build_ar_pdf_smoke(tmp_path):
    out = tmp_path / "report_ar.pdf"
    p = build_patient_report_ar(_ar_block(), tmp_path, out)
    assert Path(p).exists() and Path(p).stat().st_size > 800


def test_ar_pdf_renders_pages(tmp_path):
    fitz = pytest.importorskip("fitz")  # PyMuPDF — the plan's render gate
    out = tmp_path / "report_ar.pdf"
    build_patient_report_ar(_ar_block(), tmp_path, out)
    doc = fitz.open(str(out))
    assert doc.page_count >= 1
    doc.close()

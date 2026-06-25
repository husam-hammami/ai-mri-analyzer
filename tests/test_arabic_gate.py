"""Unit tests for the Arabic presentation layer's deterministic safety core (services/arabic.py).

The translator (the one `claude -p` pass) is fully mocked — these tests cover the gate,
recognizers, glossary rendering, English-fallback, and the sidecar staleness fingerprint.
No network, no claude, no deps beyond stdlib + the glossary.
"""
import pytest

from services import arabic
from prompts.i18n_glossary import (
    GRADE_AR, CERTAINTY_AR, CONFIDENCE_AR, REPORT_DISCLAIMER_AR,
)


# ── gate_ar_field ─────────────────────────────────────────────────────────────────────────
def test_gate_keeps_faithful_translation():
    en = "There is mild disc bulge."
    ar = "يوجد انتفاق قرصي خفيف."          # mild → خفيف, no numbers/negation/laterality
    assert arabic.gate_ar_field(en, ar) == ar


def test_gate_blocks_new_number():
    en = "There is a 6 mm disc bulge."
    ar = "يوجد انتفاق قرصي 6 mm و 4 mm."    # invented 4 mm
    assert arabic.gate_ar_field(en, ar) == en


def test_gate_preserves_existing_number():
    en = "There is a 6 mm disc bulge."
    ar = "يوجد انتفاق قرصي 6 mm."
    assert arabic.gate_ar_field(en, ar) == ar


def test_gate_blocks_dropped_negation():
    en = "No canal narrowing."
    ar = "يوجد تضيّق في القناة."             # negation dropped / inverted
    assert arabic.gate_ar_field(en, ar) == en


def test_gate_keeps_preserved_negation():
    en = "No canal narrowing."
    ar = "لا يوجد تضيّق في القناة."          # negation preserved
    assert arabic.gate_ar_field(en, ar) == ar


def test_gate_blocks_laterality_flip():
    en = "Left neural foraminal narrowing."
    ar = "تضيّق ثقبي عصبي أيمن."             # left → right
    assert arabic.gate_ar_field(en, ar) == en


def test_gate_keeps_correct_laterality():
    en = "Left neural foraminal narrowing."
    ar = "تضيّق ثقبي عصبي أيسر."
    assert arabic.gate_ar_field(en, ar) == ar


def test_gate_blocks_grade_drift_marked_to_mild():
    en = "There is marked canal narrowing."
    ar = "يوجد تضيّق خفيف في القناة."        # marked → mild — the dominant risk
    assert arabic.gate_ar_field(en, ar) == en


def test_gate_keeps_correct_grade():
    en = "There is marked canal narrowing."
    ar = "يوجد تضيّق ملحوظ في القناة."       # marked → ملحوظ
    assert arabic.gate_ar_field(en, ar) == ar


def test_gate_deny_by_default_on_unmapped_grade():
    # 'massive' is recognised but deliberately left unmapped → must fall back to English
    en = "There is massive effusion."
    ar = "يوجد انصباب كبير."                 # any Arabic — gate denies because 'massive' is unmapped
    assert arabic.gate_ar_field(en, ar) == en


def test_gate_blocks_other_grade_introduced():
    en = "There is a disc bulge."            # no grade in English
    ar = "يوجد انتفاق قرصي شديد."            # 'severe' grade invented
    assert arabic.gate_ar_field(en, ar) == en


def test_gate_compound_grade_not_split():
    en = "There is mild-to-moderate stenosis."
    ar = "يوجد تضيّق خفيف إلى متوسط."        # the compound Arabic term
    assert arabic.gate_ar_field(en, ar) == ar


def test_gate_empty_translation_falls_back():
    assert arabic.gate_ar_field("Something.", "   ") == "Something."


# ── glossary-keyed rendering ────────────────────────────────────────────────────────────────
def test_certainty_ar_maps_tier():
    assert arabic.certainty_ar("Likely") == CERTAINTY_AR["Likely"]
    assert arabic.certainty_ar("probable") == CERTAINTY_AR["Likely"]   # synonym → Likely


def test_certainty_ar_unknown_defaults_possible():
    assert arabic.certainty_ar("banana") == CERTAINTY_AR["Possible"]


def test_confidence_label_ar():
    assert arabic.confidence_label_ar("Moderate") == CONFIDENCE_AR["Moderate"]


# ── build_ar_patient (translator mocked) ─────────────────────────────────────────────────────
_FAITHFUL = {
    "There is mild disc bulge.": "يوجد انتفاق قرصي خفيف.",
    "A disc bulge is described.": "يُوصف انتفاق قرصي.",
    "There is mild L4-L5 disc bulge.": "يوجد انتفاق قرصي خفيف في L4-L5.",
    "L4-L5 level.": "مستوى L4-L5.",
    "This is common.": "هذا أمر شائع.",
    "Based on the images.": "بناءً على الصور.",
}


def _patient_en():
    return {
        "bottom_line": "There is mild disc bulge.",
        "key_points": ["A disc bulge is described."],
        "findings": [{
            "plain": "There is mild L4-L5 disc bulge.",
            "certainty": "Likely", "figure": "f.png", "caption": "L4-L5 level.",
        }],
        "what_it_means": ["This is common."],
        "confidence": {"label": "Moderate", "score": 70, "note": "Based on the images."},
    }


def test_build_ar_patient_faithful():
    out = arabic.build_ar_patient(_patient_en(), translator=lambda ts: [_FAITHFUL[t] for t in ts])
    assert out["bottom_line"] == _FAITHFUL["There is mild disc bulge."]
    f = out["findings"][0]
    assert f["plain"] == _FAITHFUL["There is mild L4-L5 disc bulge."]
    assert f["certainty"] == CERTAINTY_AR["Likely"]   # Arabic display word
    assert f["certainty_key"] == "Likely"             # English tier → PDF color
    assert f["figure"] == "f.png"
    assert out["disclaimer"] == REPORT_DISCLAIMER_AR  # frozen, not translated
    assert out["confidence"]["label"] == CONFIDENCE_AR["Moderate"]


def test_build_ar_patient_falls_back_to_english_on_corrupt_field():
    # translator drifts the grade on the bottom line (mild → severe) → that field shows English
    bad = dict(_FAITHFUL)
    bad["There is mild disc bulge."] = "يوجد انتفاق قرصي شديد."   # severe
    out = arabic.build_ar_patient(_patient_en(), translator=lambda ts: [bad[t] for t in ts])
    assert out["bottom_line"] == "There is mild disc bulge."      # English fallback
    assert out["findings"][0]["plain"] == _FAITHFUL["There is mild L4-L5 disc bulge."]  # unaffected


def test_build_ar_patient_degrades_when_translator_fails():
    def boom(ts):
        raise RuntimeError("claude unavailable")
    out = arabic.build_ar_patient(_patient_en(), translator=boom)
    assert out.get("_degraded") is True


# ── sidecar staleness fingerprint ─────────────────────────────────────────────────────────────
def test_sidecar_roundtrip_and_staleness(tmp_path):
    pat = _patient_en()
    ar_block = arabic.build_ar_patient(pat, translator=lambda ts: [_FAITHFUL[t] for t in ts])
    arabic.write_sidecar(tmp_path, pat, ar_block)
    # same English → cache hit
    assert arabic.read_sidecar(tmp_path, pat) == ar_block
    # English report changed (e.g. reconcile rewrote a finding) → sidecar treated as absent
    pat2 = _patient_en()
    pat2["bottom_line"] = "There is moderate disc bulge now."
    assert arabic.read_sidecar(tmp_path, pat2) is None


def test_invalidate_sidecar(tmp_path):
    pat = _patient_en()
    arabic.write_sidecar(tmp_path, pat, {"x": 1})
    assert arabic.sidecar_path(tmp_path).exists()
    arabic.invalidate_sidecar(tmp_path)
    assert not arabic.sidecar_path(tmp_path).exists()

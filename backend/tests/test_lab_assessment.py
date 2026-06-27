"""
Pure-Python unit tests for the lab ASSESSMENT layer (lab_reader.compose_assessment) + the analyte
normalization + the clarity-floor backend↔frontend sync. NO Claude / network.

Load-bearing properties (these are why the plan passed bulletproof):
  - The named condition is DERIVED from the flagged-marker pattern (deterministic trigger); the model
    proposal is advisory only and can never add a condition the markers don't support.
  - A red-flag / serious diagnosis can NEVER surface; treatment terms never appear.
  - A null assessment NEVER softens the verdict (the page keeps the honest grouped verdict).
  - The clarity flag-floor literal in frontend/index.html stays == CLARITY_FLAG_FLOOR (no shared import
    is possible for a no-build single-file SPA, so a scrape test enforces it).

Run:  python -m pytest backend/tests/test_lab_assessment.py
  or: python backend/tests/test_lab_assessment.py
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.lab_reader import (  # noqa: E402
    CLARITY_FLAG_FLOOR,
    _CONDITION_ADVICE,
    _CONDITION_WHITELIST,
    _REDFLAG_TERMS,
    _TREATMENT_TERMS,
    compose_assessment,
    normalize_analyte_key,
)


def _r(plain_name, analyte_key, status, *, confidence="Confirmed", clarity=0.95, analyte_raw=None):
    return {
        "plain_name": plain_name, "analyte_raw": analyte_raw or plain_name,
        "analyte_key": analyte_key, "value": "1", "unit": "x", "ref_range_text": "0-2",
        "range_type": "two_sided_numeric", "status": status, "severity_phrase": "",
        "confidence": confidence, "plain_meaning": "", "clarity": clarity,
        "page_index": 0, "source_text": f"{plain_name} 1",
    }


# ── deterministic trigger ──

def test_anemia_from_low_hemoglobin_no_model_proposal():
    """The condition is DERIVED from markers — it surfaces with NO model proposal at all."""
    res = [_r("Hemoglobin", "hemoglobin", "low")]
    a = compose_assessment(res, {}, None)
    assert a and a["condition_key"] == "anemia", a
    assert a["name_en"] and a["name_ar"] and a["explanation_en"] and a["explanation_ar"]
    assert "Hemoglobin" in a["supporting"]


def test_iron_deficiency_outranks_plain_anemia():
    """Low Hgb + low MCV + low ferritin → the more-specific iron-deficiency anemia wins (priority)."""
    res = [_r("Hemoglobin", "hemoglobin", "low"), _r("MCV", "mcv", "low"), _r("Ferritin", "ferritin", "low")]
    a = compose_assessment(res, {}, None)
    assert a["condition_key"] == "iron_deficiency_anemia", a
    # supporting markers are the flagged analytes that matched the pattern
    assert "Hemoglobin" in a["supporting"] and ("MCV" in a["supporting"] or "Ferritin" in a["supporting"])


def test_high_cholesterol_from_high_ldl():
    res = [_r("LDL cholesterol", "ldl", "high")]
    a = compose_assessment(res, {}, None)
    assert a["condition_key"] == "high_cholesterol", a


def test_presence_is_deterministic_across_calls():
    """Same input → same surfaced condition every call (reproducible; no RNG, no model dependence)."""
    res = [_r("Hemoglobin", "hemoglobin", "low"), _r("MCV", "mcv", "low"), _r("Ferritin", "ferritin", "low")]
    out = {compose_assessment(res, {}, None)["condition_key"] for _ in range(5)}
    assert out == {"iron_deficiency_anemia"}, out


# ── regression: analyte mis-normalization (the false "low platelet count" + false anemia bugs) ──

def test_mpv_is_not_platelet_count():
    """'Platelet size (MPV)' must normalize to its own key, NOT the platelet-COUNT key — a low platelet
    SIZE must never surface as 'a low platelet count' (the size↔count category error)."""
    assert normalize_analyte_key("MPV", "Platelet size (MPV)") == "mpv"
    assert normalize_analyte_key("Platelet Count", "Platelet Count") == "platelets"
    # low MPV alone (via normalization fallback) → never the low-platelet-count condition
    a = compose_assessment([_r("Platelet size (MPV)", "", "low", analyte_raw="MPV")], {}, None)
    assert a is None or a["condition_key"] != "low_platelets", a


def test_mch_is_not_hemoglobin():
    """MCH/MCHC names contain 'hemoglobin' but must normalize to 'mch' — else they fake a low Hgb."""
    assert normalize_analyte_key("MCH", "Hemoglobin per cell (MCH)") == "mch"
    assert normalize_analyte_key("MCHC", "Red-cell hemoglobin concentration (MCHC)") == "mch"
    assert normalize_analyte_key("Hemoglobin", "Hemoglobin") == "hemoglobin"  # real Hgb still maps right


def test_microcytic_without_low_hemoglobin_is_not_anemia():
    """Low MCV + low MCH with a NORMAL hemoglobin → the microcytic pattern, NOT anemia (which would
    overclaim a low Hgb the patient doesn't have). Forces the normalization fallback (analyte_key='')."""
    res = [_r("Red-cell size (MCV)", "", "low", analyte_raw="MCV"),
           _r("Hemoglobin per cell (MCH)", "", "low", analyte_raw="MCH")]
    a = compose_assessment(res, {}, None)
    assert a and a["condition_key"] == "microcytic_hypochromic", a
    assert "anemia" not in a["name_en"].lower()


def test_iron_deficiency_anemia_still_wins_when_hemoglobin_low():
    """When hemoglobin IS low too, the microcytic pattern defers to iron-deficiency anemia (priority)."""
    res = [_r("Hemoglobin", "", "low", analyte_raw="Hemoglobin"),
           _r("Red-cell size (MCV)", "", "low", analyte_raw="MCV"),
           _r("Hemoglobin per cell (MCH)", "", "low", analyte_raw="MCH")]
    a = compose_assessment(res, {}, None)
    assert a and a["condition_key"] == "iron_deficiency_anemia", a


# ── model is advisory only ──

def test_model_cannot_add_unsupported_condition():
    """The model proposes a condition the markers don't support → IGNORED (returns None here)."""
    res = [_r("Sodium", "sodium", "normal")]  # nothing flagged
    a = compose_assessment(res, {}, {"proposed_condition": "iron-deficiency anemia",
                                     "supporting_analytes": ["Sodium"], "model_confidence": "probable"})
    assert a is None, a


def test_model_proposal_cannot_demote_higher_priority_condition():
    """Markers support BOTH iron-deficiency anemia (prio 90) and plain anemia (prio 50). Even if the
    model proposes the generic 'anemia', the deterministic priority wins — presence/primary is a pure
    function of the markers (the model is the lowest tie-break only)."""
    res = [_r("Hemoglobin", "hemoglobin", "low"), _r("MCV", "mcv", "low"), _r("Ferritin", "ferritin", "low")]
    a = compose_assessment(res, {}, {"proposed_condition": "anemia",
                                     "supporting_analytes": ["Hemoglobin"], "model_confidence": "probable"})
    assert a["condition_key"] == "iron_deficiency_anemia", a


def test_model_proposal_breaks_ties_within_supported_candidates():
    """When the markers support a condition, the model proposal is allowed only as a tie-break; it can
    never override the deterministic derivation to something unsupported."""
    res = [_r("Hemoglobin", "hemoglobin", "low")]  # supports 'anemia' only
    a = compose_assessment(res, {}, {"proposed_condition": "leukemia",  # red-flag → ignored entirely
                                     "supporting_analytes": ["Hemoglobin"], "model_confidence": "probable"})
    assert a["condition_key"] == "anemia", a


# ── red-flag / treatment safety ──

def test_redflag_proposal_never_surfaces_a_redflag_condition():
    res = [_r("White cell count", "wbc", "high")]
    a = compose_assessment(res, {}, {"proposed_condition": "leukemia", "supporting_analytes": ["White cell count"],
                                     "model_confidence": "probable"})
    # The derived condition is the plain 'high white-cell count', NEVER 'leukemia'.
    assert a["condition_key"] == "high_white_cells", a
    assert "leukemia" not in (a["name_en"] + a["explanation_en"]).lower()


def test_whitelist_table_has_no_redflag_or_treatment_terms():
    """Guards the curated table itself: no canonical name/explanation (EN or AR) may contain a red-flag
    or treatment/drug term."""
    for entry in _CONDITION_WHITELIST:
        blob = " ".join(str(entry.get(f, "")) for f in ("name_en", "name_ar", "expl_en", "expl_ar")).lower()
        for term in _REDFLAG_TERMS + _TREATMENT_TERMS:
            assert term not in blob, (entry["key"], term)


def test_advice_notes_have_no_redflag_or_drug_terms():
    """The curated 'what can help' notes are general lifestyle info only — never a drug, dose, or
    red-flag term (e.g. no 'supplement', 'medication', 'statin')."""
    for key, adv in _CONDITION_ADVICE.items():
        blob = (str(adv.get("en", "")) + " " + str(adv.get("ar", ""))).lower()
        for term in _REDFLAG_TERMS + _TREATMENT_TERMS:
            assert term not in blob, (key, term)


# ── null / fallback ──

def test_no_flags_returns_none():
    res = [_r("Sodium", "sodium", "normal"), _r("Potassium", "potassium", "normal")]
    assert compose_assessment(res, {}, None) is None


def test_possible_tier_does_not_trigger_a_condition():
    """A Possible-tier abnormal is below the flag bar (matches compose_verdict) → no condition."""
    res = [_r("Hemoglobin", "hemoglobin", "low", confidence="Possible")]
    assert compose_assessment(res, {}, None) is None


def test_low_clarity_does_not_trigger_a_condition():
    res = [_r("Hemoglobin", "hemoglobin", "low", clarity=0.3)]
    assert compose_assessment(res, {}, None) is None


# ── analyte_key fallback ──

def test_analyte_key_fallback_from_name():
    """A flagged result with NO analyte_key still matches via the shared normalization slug table."""
    res = [{"plain_name": "Haemoglobin", "analyte_raw": "HGB", "analyte_key": "", "value": "8",
            "unit": "g/dL", "ref_range_text": "12-15", "range_type": "two_sided_numeric", "status": "low",
            "severity_phrase": "", "confidence": "Confirmed", "plain_meaning": "", "clarity": 0.95,
            "page_index": 0, "source_text": "HGB 8"}]
    a = compose_assessment(res, {}, None)
    assert a and a["condition_key"] == "anemia", a


def test_normalize_analyte_key_basics():
    assert normalize_analyte_key("Haemoglobin") == "hemoglobin"
    assert normalize_analyte_key("HGB") == "hemoglobin"
    assert normalize_analyte_key("25-hydroxyvitamin D") == "vitamin_d"
    assert normalize_analyte_key("LDL-C") == "ldl"
    assert normalize_analyte_key("Total iron binding capacity") == "tibc"  # not 'iron'
    assert normalize_analyte_key("Something unmapped") == ""


# ── G6: clarity-floor backend↔frontend sync (no shared import possible for a no-build SPA) ──

def test_clarity_floor_in_sync_with_frontend():
    """The frontend lab `flagged` filter must use the SAME clarity floor as the backend. Scrape the
    JS constant from index.html and assert equality (the only sync mechanism for a single-file SPA)."""
    index = os.path.join(_REPO, "frontend", "index.html")
    with open(index, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"LAB_CLARITY_FLAG_FLOOR\s*=\s*([0-9.]+)", html)
    assert m, "LAB_CLARITY_FLAG_FLOOR not found in frontend/index.html"
    assert float(m.group(1)) == CLARITY_FLAG_FLOOR, (m.group(1), CLARITY_FLAG_FLOOR)


def _run_plain():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} lab assessment tests passed.")


if __name__ == "__main__":
    _run_plain()

"""
Pure-Python unit tests for the lab verdict SAFETY GATE (lab_reader.compose_verdict).
NO Claude / network. The non-negotiable: a genuinely flagged report must NEVER yield a reassuring
verdict, and a clean all-normal read must yield the SCOPED (non-absolute) line.

Run:  python -m pytest backend/tests/test_lab_verdict_gate.py
  or: python backend/tests/test_lab_verdict_gate.py   (plain asserts, no pytest needed)
"""

import os
import sys

# Make `services` / `prompts` importable whether run from repo root or backend/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.lab_reader import compose_verdict  # noqa: E402


# ── fixtures ──

def _result(status="normal", confidence="Confirmed", clarity=0.95, range_type="two_sided_numeric"):
    return {
        "plain_name": "Test", "analyte_raw": "Test", "value": "1", "unit": "x",
        "ref_range_text": "0-2", "range_type": range_type, "status": status,
        "severity_phrase": "", "confidence": confidence, "plain_meaning": "",
        "clarity": clarity, "page_index": 0, "source_text": "Test 1 (0-2)",
    }


def _signals(extraction_confidence=0.93, analytes_parsed=None, render_quality="clear", n_results=0):
    if analytes_parsed is None:
        analytes_parsed = n_results
    return {
        "extraction_confidence": extraction_confidence,
        "analytes_parsed": analytes_parsed,
        "render_quality": render_quality,
    }


# ── tests ──

def test_clear_high_flag_never_reassures():
    """A clear Confirmed high flag must yield FEW/SEVERAL — never ALL_CLEAN/NONE_FLAGGED_PARTIAL."""
    results = [
        _result(status="high", confidence="Confirmed", clarity=0.95),
        _result(status="normal", confidence="Confirmed", clarity=0.95),
    ]
    sig = _signals(extraction_confidence=0.93, n_results=2)
    v = compose_verdict(results, sig, lang="en")
    assert v["verdict_key"] in ("FEW", "SEVERAL"), v
    assert v["verdict_key"] not in ("ALL_CLEAN", "NONE_FLAGGED_PARTIAL", "NEUTRAL")
    assert v["flagged_count"] == 1
    # FEW with n==1 pluralises to the singular.
    assert "1 thing worth a look" in v["takeaway"], v["takeaway"]


def test_several_flags():
    results = [_result(status="high", confidence="Confirmed", clarity=0.95) for _ in range(4)]
    v = compose_verdict(results, _signals(n_results=4), lang="en")
    assert v["verdict_key"] == "SEVERAL", v
    assert v["flagged_count"] == 4


def test_low_extraction_confidence_still_surfaces_confident_flag():
    """Low OVERALL extraction confidence must NOT bury a confidently-read abnormal — that was the real
    false-negative (a clear low Hemoglobin hidden behind a vague NEUTRAL line). A Confirmed/Likely flag
    drives the headline (FEW/SEVERAL); the separate confidence pill carries the 'low confidence' caveat.
    Only an UNREADABLE render (test below) overrides flags."""
    results = [_result(status="high", confidence="Confirmed", clarity=0.95)]
    v = compose_verdict(results, _signals(extraction_confidence=0.5, n_results=1), lang="en")
    assert v["verdict_key"] == "FEW", v
    assert v["flagged_count"] == 1
    assert v["confidence"] == "low"  # the caveat still surfaces in the confidence pill


def test_low_parsed_ratio_forces_neutral():
    # 10 results on the report, but only 5 parsed -> parsed_ratio 0.5 < 0.7.
    results = [_result(status="normal", clarity=0.95) for _ in range(10)]
    sig = _signals(extraction_confidence=0.95, analytes_parsed=5)
    v = compose_verdict(results, sig, lang="en")
    assert v["verdict_key"] == "NEUTRAL", v


def test_unreadable_render_forces_neutral():
    results = [_result(status="high", confidence="Confirmed", clarity=0.95)]
    sig = _signals(extraction_confidence=0.95, render_quality="unreadable", n_results=1)
    v = compose_verdict(results, sig, lang="en")
    assert v["verdict_key"] == "NEUTRAL", v


def test_fully_clean_high_quality_is_scoped_all_clean():
    results = [_result(status="normal", confidence="Confirmed", clarity=0.95) for _ in range(5)]
    sig = _signals(extraction_confidence=0.93, analytes_parsed=5)
    v = compose_verdict(results, sig, lang="en")
    assert v["verdict_key"] == "ALL_CLEAN", v
    # Scoped, NOT an absolute "everything looks normal".
    assert "not a clean bill of health" in v["takeaway"], v["takeaway"]
    assert "everything looks normal" not in v["takeaway"].lower()


def test_n_zero_but_low_clarity_analyte_is_partial_not_clean():
    """n==0 (no qualifying flags) but a low-clarity row present -> NONE_FLAGGED_PARTIAL, not ALL_CLEAN."""
    results = [
        _result(status="normal", confidence="Confirmed", clarity=0.95),
        _result(status="normal", confidence="Possible", clarity=0.4),  # not fully clean/clear
    ]
    sig = _signals(extraction_confidence=0.93, analytes_parsed=2)
    v = compose_verdict(results, sig, lang="en")
    assert v["verdict_key"] == "NONE_FLAGGED_PARTIAL", v
    assert v["flagged_count"] == 0


def test_possible_only_flags():
    """An abnormal value read only with Possible confidence is excluded from the flag set (tier gate),
    so a lone Possible-abnormal degrades to the partial template, never reassurance."""
    results = [
        _result(status="high", confidence="Possible", clarity=0.95),
        _result(status="normal", confidence="Confirmed", clarity=0.95),
    ]
    sig = _signals(extraction_confidence=0.93, analytes_parsed=2)
    v = compose_verdict(results, sig, lang="en")
    # Possible-tier abnormal is NOT a qualifying flag (confidence gate), so n==0 -> partial, not clean.
    assert v["verdict_key"] == "NONE_FLAGGED_PARTIAL", v
    assert v["verdict_key"] != "ALL_CLEAN"


def test_likely_abnormal_is_a_qualifying_flag():
    """A Likely-tier abnormal IS a qualifying flag (only Possible-tier is filtered out), so a single
    Likely low yields FEW (max_tier Likely), never POSSIBLE_ONLY/reassurance."""
    results = [_result(status="low", confidence="Likely", clarity=0.95)]
    v = compose_verdict(results, _signals(n_results=1), lang="en")
    assert v["verdict_key"] == "FEW", v
    assert v["flagged_count"] == 1


def test_en_and_ar_both_return_nonempty_fixed_strings():
    """Same (results, signals) through EN and AR must BOTH resolve to a non-empty fixed string
    (cross-language determinism). Covers several keys."""
    cases = [
        # (results, signals)
        ([_result(status="high", confidence="Confirmed", clarity=0.95)], _signals(n_results=1)),  # FEW
        ([_result(status="high", confidence="Confirmed", clarity=0.95) for _ in range(4)], _signals(n_results=4)),  # SEVERAL
        ([_result(status="normal", clarity=0.95) for _ in range(5)], _signals(analytes_parsed=5)),  # ALL_CLEAN
        ([_result(status="normal", confidence="Possible", clarity=0.4)], _signals(extraction_confidence=0.4, n_results=1)),  # NEUTRAL (n==0, shaky read)
    ]
    for results, sig in cases:
        en = compose_verdict(results, sig, lang="en")
        ar = compose_verdict(results, sig, lang="ar")
        assert en["verdict_key"] == ar["verdict_key"], (en["verdict_key"], ar["verdict_key"])
        assert isinstance(en["takeaway"], str) and en["takeaway"].strip(), en
        assert isinstance(ar["takeaway"], str) and ar["takeaway"].strip(), ar
        # AR must NOT be the English string (it's a real fixed Arabic glossary entry).
        if en["verdict_key"] != "FEW":  # FEW shares the numeric n; still distinct text below
            assert ar["takeaway"] != en["takeaway"], ar["verdict_key"]


def test_no_absolute_all_normal_string_anywhere():
    """Hard guard: no verdict key produces an absolute 'everything looks normal' reassurance."""
    from services.lab_reader import _TEMPLATES_EN, _TEMPLATES_AR
    for tpl in list(_TEMPLATES_EN.values()) + list(_TEMPLATES_AR.values()):
        assert "everything looks normal" not in tpl.lower()


def _run_plain():
    """Run every test_* with plain asserts when pytest isn't invoked."""
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} lab verdict-gate tests passed.")


if __name__ == "__main__":
    _run_plain()

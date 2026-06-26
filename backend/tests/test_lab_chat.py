"""
Pure-Python unit tests for the lab CHAT safety gate (services.lab_chat). NO Claude / network — the
`ask_claude` transport is monkeypatched. The non-negotiable: the chat is a SECOND consumer of the
condition, so it is gated by ANSWER-REPLACEMENT (not a token-strip). An adversarial model answer that
names a red-flag diagnosis, a treatment/dose, or an off-whitelist condition MUST be replaced wholesale
with the (correct-language) safe template.

Run:  python -m pytest backend/tests/test_lab_chat.py
  or: python backend/tests/test_lab_chat.py
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services import lab_chat  # noqa: E402
from services.lab_chat import (  # noqa: E402
    _SAFE_TEMPLATE, _detect_lang, _gate_answer, build_lab_context, answer_lab_question,
)


# A report whose surfaced condition is iron-deficiency anemia.
_REPORT = {
    "overall": {
        "takeaway": "2 things worth a look.",
        "assessment": {
            "condition_key": "iron_deficiency_anemia",
            "name_en": "iron-deficiency anemia", "name_ar": "فقر الدم الناتج عن نقص الحديد",
            "explanation_en": "The blood is low on iron.", "explanation_ar": "الدم منخفض في الحديد.",
            "supporting": ["Hemoglobin", "Ferritin"],
        },
    },
    "patient": {"name": "Jane Doe", "age": "34", "sex": "Female"},
    "results": [
        {"plain_name": "Hemoglobin", "value": "10.6", "unit": "g/dL", "ref_range_text": "12-15",
         "status": "low", "severity_phrase": "a bit low", "plain_meaning": "low oxygen-carrying level"},
        {"plain_name": "Ferritin", "value": "8", "unit": "ng/mL", "ref_range_text": "30-300",
         "status": "low", "severity_phrase": "low", "plain_meaning": "low iron stores"},
    ],
}


def _patch(answer):
    """Monkeypatch ask_claude to return a fixed (answer, is_error=False)."""
    lab_chat.ask_claude = lambda prompt, **kw: (answer, False)


# ── context ──

def test_context_includes_results_and_surfaced_condition():
    ctx = build_lab_context(_REPORT)
    assert "iron-deficiency anemia" in ctx
    assert "Hemoglobin" in ctx and "Ferritin" in ctx
    assert "10.6" in ctx


# ── language detection ──

def test_detect_lang():
    assert _detect_lang("ما معنى هذه النتيجة؟") == "ar"
    assert _detect_lang("what does this mean?") == "en"


# ── the gate (pure) ──

def test_gate_passes_clean_answer_about_surfaced_condition():
    ans = "Your iron-deficiency anemia means your blood is low on iron, which can make you tired."
    out, replaced = _gate_answer(ans, "iron_deficiency_anemia", "en")
    assert not replaced and out == ans


def test_gate_allows_anemia_substring_of_surfaced_name():
    """'anemia' appears inside the surfaced 'iron-deficiency anemia' → not an off-whitelist trip."""
    ans = "This is a form of anemia; your hemoglobin reads a little low."
    out, replaced = _gate_answer(ans, "iron_deficiency_anemia", "en")
    assert not replaced, out


def test_gate_replaces_redflag():
    out, replaced = _gate_answer("This could be leukemia and you should worry.", "iron_deficiency_anemia", "en")
    assert replaced and out == _SAFE_TEMPLATE["en"]


def test_gate_replaces_treatment():
    out, replaced = _gate_answer("You should take an iron supplement every day.", "iron_deficiency_anemia", "en")
    assert replaced and out == _SAFE_TEMPLATE["en"]


def test_gate_replaces_offwhitelist_condition():
    """The chat may discuss ONLY the surfaced condition; naming a different whitelist condition trips."""
    out, replaced = _gate_answer("This also looks like high cholesterol to me.", "iron_deficiency_anemia", "en")
    assert replaced and out == _SAFE_TEMPLATE["en"]


def test_gate_replaces_with_arabic_template_for_arabic():
    out, replaced = _gate_answer("This could be cancer.", "iron_deficiency_anemia", "ar")
    assert replaced and out == _SAFE_TEMPLATE["ar"]


# ── the gate must SEE Arabic (pure-Arabic, no Latin crutch — the masked-hole regression) ──

def test_gate_replaces_pure_arabic_redflag():
    out, replaced = _gate_answer("قد يكون هذا سرطانًا خبيثًا.", "iron_deficiency_anemia", "ar")
    assert replaced and out == _SAFE_TEMPLATE["ar"]


def test_gate_replaces_pure_arabic_treatment():
    out, replaced = _gate_answer("ينبغي أن تتناول مكمّل الحديد كل يوم.", "iron_deficiency_anemia", "ar")
    assert replaced and out == _SAFE_TEMPLATE["ar"]


def test_gate_replaces_pure_arabic_offwhitelist_condition():
    out, replaced = _gate_answer("يبدو أيضًا أن لديك ارتفاع الكوليسترول.", "iron_deficiency_anemia", "ar")
    assert replaced and out == _SAFE_TEMPLATE["ar"]


def test_gate_allows_pure_arabic_surfaced_condition():
    out, replaced = _gate_answer("نتائجك تشير إلى نقص الحديد، والهيموغلوبين منخفض قليلاً.", "iron_deficiency_anemia", "ar")
    assert not replaced, out


# ── end-to-end with mocked transport ──

def test_answer_redflag_neutralized_en():
    _patch("Honestly this pattern can indicate leukemia.")
    with tempfile.TemporaryDirectory() as d:
        text, err = answer_lab_question("abcd1234", _REPORT, "could this be cancer?", [],
                                        model="opus", effort="low", timeout_s=5, data_dir=d)
    assert not err and text == _SAFE_TEMPLATE["en"]


def test_answer_treatment_neutralized():
    _patch("Take 325 mg of ferrous sulfate twice daily.")
    with tempfile.TemporaryDirectory() as d:
        text, err = answer_lab_question("abcd1234", _REPORT, "what should I take?", [],
                                        model="opus", effort="low", timeout_s=5, data_dir=d)
    assert not err and text == _SAFE_TEMPLATE["en"]


def test_answer_arabic_adversarial_gets_arabic_safe_template():
    _patch("قد يكون هذا سرطانًا خبيثًا في الدم.")   # PURE Arabic — no Latin crutch
    with tempfile.TemporaryDirectory() as d:
        text, err = answer_lab_question("abcd1234", _REPORT, "هل يمكن أن يكون سرطانًا؟", [],
                                        model="opus", effort="low", timeout_s=5, data_dir=d)
    assert not err and text == _SAFE_TEMPLATE["ar"]


def test_answer_benign_passes_through():
    benign = "Your hemoglobin reads a little low, which can leave you feeling tired."
    _patch(benign)
    with tempfile.TemporaryDirectory() as d:
        text, err = answer_lab_question("abcd1234", _REPORT, "what does my hemoglobin mean?", [],
                                        model="opus", effort="low", timeout_s=5, data_dir=d)
    assert not err and text == benign


def _run_plain():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} lab chat tests passed.")


if __name__ == "__main__":
    _run_plain()

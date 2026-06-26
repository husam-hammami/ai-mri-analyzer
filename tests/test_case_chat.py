"""Unit tests for the case-chat service (services/case_chat.py).

The live `claude -p` call can't be self-verified in-session (nested-session hang), so it's mocked here.
These cover the pure functions + the deterministic backstops that ARE the safety floor.
"""
import json
import re

import pytest

from services import case_chat


def _uncal_report():
    return {
        # the REAL on-disk shape: calibration_status is a DICT, not a string (regression for the .lower() crash)
        "study": {"body_part": "Lower-back spine", "modality": "MRI",
                  "calibration_status": {"calibrated": False, "basis": "image export; no PixelSpacing"}},
        "patient": {"findings": [{"plain": "mild disc bulge at L4-L5", "certainty": "Likely",
                                  "caption": "the lowest discs"}],
                    "bottom_line": "There is likely a mild disc bulge.",
                    "what_it_means": ["Often causes no lasting trouble."],
                    "confidence": {"label": "Moderate", "score": 70, "note": "Based on the images."}},
    }


# ── build_context ─────────────────────────────────────────────────────────────────────────
def test_build_context_lists_findings_certainty_confidence():
    ctx = case_chat.build_context(_uncal_report())
    assert "Lower-back spine" in ctx and "MRI" in ctx
    assert "mild disc bulge at L4-L5" in ctx and "Likely" in ctx
    assert "OVERALL CONFIDENCE: Moderate" in ctx
    assert "WHAT IT MEANS" in ctx


def test_calibration_label_handles_dict_and_string():
    assert case_chat._calibration_label({"calibration_status": {"calibrated": False}}) == "uncalibrated"
    assert case_chat._calibration_label({"calibration_status": {"calibrated": True}}) == "calibrated"
    assert case_chat._calibration_label({"calibration_status": "DICOM-calibrated"}) == "DICOM-calibrated"
    assert case_chat._calibration_label({}) == "unknown"


def test_build_context_does_not_crash_on_dict_calibration():
    ctx = case_chat.build_context(_uncal_report())   # calibration_status is a dict — must not raise
    assert "calibration: uncalibrated" in ctx


def test_build_context_falls_back_to_agent_summary_patient():
    # The real on-disk shape on most studies: top-level patient sparse, data under agent.summary.patient.
    report = {
        "study": {"body_part": "Brain", "modality": "MRI", "calibration_status": "UNCALIBRATED"},
        "patient": {},
        "agent": {"summary": {"patient": {
            "findings": [{"plain": "small lesion in the right frontal lobe", "certainty": "Confirmed"}],
            "bottom_line": "A small spot is described."}}},
    }
    ctx = case_chat.build_context(report)
    assert "small lesion in the right frontal lobe" in ctx and "Confirmed" in ctx and "Brain" in ctx


# ── build_prompt ──────────────────────────────────────────────────────────────────────────
def test_build_prompt_contains_rules_report_and_history():
    ctx = "STUDY: Brain · MRI\nFINDING 1 [Likely]: x"
    prompt = case_chat.build_prompt(ctx, [{"role": "user", "text": "hi"},
                                          {"role": "assistant", "text": "hello"}], "what now?")
    assert "Use ONLY the report above" in prompt and ctx in prompt
    assert "Q: hi" in prompt and "A: hello" in prompt and prompt.rstrip().endswith("Q: what now?\nA:")
    assert "Help first" in prompt  # the help-first rule (now rule 4) is present


# ── scope invariant (no tool/file access) ───────────────────────────────────────────────────
def test_ask_claude_command_has_no_tool_or_permission_flags(monkeypatch):
    captured = {}
    monkeypatch.setattr(case_chat, "_resolve_claude_bin", lambda *a, **k: "claude")

    class _Proc:
        stdout = '{"result":"hi","is_error":false}'
        returncode = 0

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(case_chat.subprocess, "run", _fake_run)
    text, err = case_chat.ask_claude("q", model="opus", effort="low", timeout_s=5)
    assert "--add-dir" not in captured["cmd"]
    assert "--permission-mode" not in captured["cmd"]
    assert err is False and text == "hi"


# ── mm-backstop (token-strip, not nuke) ─────────────────────────────────────────────────────
def test_mm_backstop_strips_token_but_keeps_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(case_chat, "ask_claude",
                        lambda *a, **k: ("Your scan shows a mild bulge of about 4 mm pressing out. "
                                         "It's a common finding.", False))
    text, err = case_chat.answer_case_question("abcdef01", _uncal_report(), "is it serious?", [],
                                               model="opus", effort="low", timeout_s=5, data_dir=str(tmp_path))
    assert err is False
    assert not re.search(r"\d+(\.\d+)?\s*mm", text)   # no mm token survives on an uncalibrated study
    assert "common finding" in text                   # the rest of the helpful answer is preserved


def test_mm_passes_through_when_calibrated(monkeypatch, tmp_path):
    rep = _uncal_report()
    rep["study"]["calibration_status"] = {"calibrated": True, "basis": "DICOM PixelSpacing"}
    monkeypatch.setattr(case_chat, "ask_claude", lambda *a, **k: ("The canal narrows to about 8 mm.", False))
    text, err = case_chat.answer_case_question("abcdef01", rep, "q", [], model="opus", effort="low",
                                               timeout_s=5, data_dir=str(tmp_path))
    assert "8 mm" in text


def test_empty_model_output_is_error(monkeypatch, tmp_path):
    monkeypatch.setattr(case_chat, "ask_claude", lambda *a, **k: ("", False))
    _t, err = case_chat.answer_case_question("abcdef01", _uncal_report(), "q", [], model="opus",
                                             effort="low", timeout_s=5, data_dir=str(tmp_path))
    assert err is True


# ── persistence ─────────────────────────────────────────────────────────────────────────────
def test_persist_turn_caps_history(tmp_path):
    for i in range(20):
        case_chat._persist_turn(str(tmp_path), "abcdef01", f"q{i}", f"a{i}", max_turns=12)
    log = json.loads((tmp_path / "abcdef01" / "chat.json").read_text(encoding="utf-8"))
    assert len(log) == 24                       # 12 pairs (user+assistant) kept
    assert log[-1] == {"role": "assistant", "text": "a19", "ts": log[-1]["ts"]}

"""Endpoint-level tests for the case-chat wiring in app.py. claude is mocked (the live call hangs in-session)."""
import asyncio
import json

import pytest
from fastapi import HTTPException

import app as mika_app
from services import case_chat


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


JOB = "abcdef01"


def _write_disk_report(tmp_path):
    summary = {
        "study_description": "Lumbar spine MRI",
        "patient": {
            "study": {"body_part": "Lower-back spine", "modality": "MRI"},
            "bottom_line": "There is likely a mild disc bulge.",
            "findings": [{"plain": "mild disc bulge at L4-L5", "certainty": "Likely", "caption": "the level"}],
            "confidence": {"label": "Moderate", "score": 70, "note": "Based on the images."},
        },
    }
    jd = tmp_path / JOB
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "report.json").write_text(json.dumps({"job_id": JOB, "agent": {"summary": summary}}), encoding="utf-8")


def _req(question="What does this mean?", history=None):
    return mika_app.ChatRequest(question=question, history=history or [])


def test_flag_off_post_is_404(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", False)
    _write_disk_report(mika_app.DATA_DIR)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mika_app.case_chat_endpoint(JOB, _req()))
    assert ei.value.status_code == 404


def test_availability_reflects_flag(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", False)
    assert asyncio.run(mika_app.chat_availability()) == {"enabled": False}
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", True)
    assert asyncio.run(mika_app.chat_availability()) == {"enabled": True}


def test_happy_path(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", True)
    monkeypatch.setattr(case_chat, "ask_claude",
                        lambda *a, **k: ("Your scan shows a mild disc bulge — a common finding. "
                                         "Discuss this with your doctor.", False))
    _write_disk_report(mika_app.DATA_DIR)
    resp = asyncio.run(mika_app.case_chat_endpoint(JOB, _req("Is this serious?")))
    assert "mild disc bulge" in resp.answer and "doctor" in resp.answer
    # the turn was persisted
    log = json.loads((mika_app.DATA_DIR / JOB / "chat.json").read_text(encoding="utf-8"))
    assert log[0]["role"] == "user" and log[1]["role"] == "assistant"


def test_empty_question_400(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", True)
    _write_disk_report(mika_app.DATA_DIR)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mika_app.case_chat_endpoint(JOB, _req("   ")))
    assert ei.value.status_code == 400


def test_too_long_question_413(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", True)
    _write_disk_report(mika_app.DATA_DIR)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mika_app.case_chat_endpoint(JOB, _req("x" * (mika_app.CHAT_MAX_Q + 1))))
    assert ei.value.status_code == 413


def test_no_report_404(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", True)   # flag on, but no report on disk
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mika_app.case_chat_endpoint(JOB, _req()))
    assert ei.value.status_code == 404


def test_bad_job_id_404(monkeypatch):
    monkeypatch.setattr(mika_app, "CHAT_ENABLED", True)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mika_app.case_chat_endpoint("../etc", _req()))
    assert ei.value.status_code == 404

"""Endpoint-level tests for the Arabic layer wiring in app.py.

No claude: the live translate path can't be self-verified in-session (nested hang), so these
tests pre-write a sidecar (or use a mock translator) and verify the FLAG GATING, the additive
`?lang=ar` attach, freshness, and that the English path is untouched when off.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import app as mika_app
from services import arabic


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
        "findings": [{"text": "L4-L5 disc bulge.", "tier": "B", "figure": "proof"}],
        "impression": ["Likely L4-L5 disc bulge."],
        "patient": {
            "study": {"body_part": "Lower-back spine", "modality": "MRI"},
            "bottom_line": "A lower-back disc is described.",
            "key_points": ["A disc bulge is described."],
            "findings": [{"plain": "A disc bulge is described.",
                          "certainty": "Likely", "figure": "proof.png", "caption": "the level"}],
            "what_it_means": ["This is common."],
            "confidence": {"label": "Moderate", "score": 70, "note": "Based on the images."},
        },
    }
    jd = tmp_path / JOB
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "report.json").write_text(json.dumps({"job_id": JOB, "agent": {"summary": summary}}), encoding="utf-8")
    payload = mika_app._normalize_loaded_report(JOB, json.loads((jd / "report.json").read_text()))
    return jd, payload["patient"]


def _seed_sidecar(jd, patient_en):
    ar_block = arabic.build_ar_patient(patient_en, translator=lambda ts: ["نص عربي" for _ in ts])
    arabic.write_sidecar(jd, patient_en, ar_block)
    return ar_block


def test_flag_off_post_ar_is_404(monkeypatch):
    monkeypatch.setattr(mika_app, "AR_ENABLED", False)
    _write_disk_report(mika_app.DATA_DIR)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mika_app.generate_arabic_report(JOB))
    assert ei.value.status_code == 404


def test_flag_off_get_lang_ar_returns_plain_english(monkeypatch):
    monkeypatch.setattr(mika_app, "AR_ENABLED", False)
    _write_disk_report(mika_app.DATA_DIR)
    out = asyncio.run(mika_app.get_report(JOB, lang="ar"))
    assert "ar" not in out               # no Arabic attached when off
    assert out["patient"]["bottom_line"] == "A lower-back disc is described."


def test_flag_on_get_lang_ar_attaches_fresh_sidecar(monkeypatch):
    monkeypatch.setattr(mika_app, "AR_ENABLED", True)
    jd, patient = _write_disk_report(mika_app.DATA_DIR)
    ar_block = _seed_sidecar(jd, patient)
    out = asyncio.run(mika_app.get_report(JOB, lang="ar"))
    assert out["lang"] == "ar"
    assert out["ar"]["available"] is True
    assert out["ar"]["patient"]["disclaimer"] == ar_block["disclaimer"]
    # English remains present and untouched in the same payload
    assert out["patient"]["bottom_line"] == "A lower-back disc is described."


def test_flag_on_get_lang_ar_without_sidecar_reports_unavailable(monkeypatch):
    monkeypatch.setattr(mika_app, "AR_ENABLED", True)
    _write_disk_report(mika_app.DATA_DIR)
    out = asyncio.run(mika_app.get_report(JOB, lang="ar"))
    assert out["ar"]["available"] is False and out["ar"]["patient"] is None


def test_generate_returns_cached_without_translation(monkeypatch):
    # A fresh sidecar exists → generate must return it WITHOUT invoking the translator (claude).
    monkeypatch.setattr(mika_app, "AR_ENABLED", True)
    jd, patient = _write_disk_report(mika_app.DATA_DIR)
    ar_block = _seed_sidecar(jd, patient)

    def _boom(*a, **k):
        raise AssertionError("translator must not run when a fresh sidecar exists")
    monkeypatch.setattr(arabic, "_claude_translate", _boom)
    out = asyncio.run(mika_app.generate_arabic_report(JOB))
    assert out["status"] == "ok"
    assert out["patient"]["disclaimer"] == ar_block["disclaimer"]


def test_stale_sidecar_not_served(monkeypatch):
    monkeypatch.setattr(mika_app, "AR_ENABLED", True)
    jd, patient = _write_disk_report(mika_app.DATA_DIR)
    _seed_sidecar(jd, patient)
    # Simulate a reconcile rewrite changing the English prose → fingerprint mismatch → not served.
    patient2 = dict(patient)
    patient2["bottom_line"] = "Now the English says something different."
    assert arabic.read_sidecar(jd, patient2) is None

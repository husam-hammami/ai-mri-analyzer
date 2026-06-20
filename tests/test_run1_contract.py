import asyncio
import json

import pytest
from fastapi import BackgroundTasks

import app as mika_app
from services import agent_runner


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


def _summary():
    return {
        "study_description": "Lumbar spine MRI",
        "calibration_status": "DICOM-calibrated",
        "findings": [{"text": "L4-L5 disc bulge.", "tier": "B", "figure": "proof"}],
        "impression": ["Likely L4-L5 disc bulge."],
        "incidentals": [],
        "discrepancies": [],
        "patient": {
            "patient": {"name": "Test Patient", "age": "", "sex": ""},
            "study": {"body_part": "Lower-back spine", "modality": "MRI", "date": "", "comparison": ""},
            "bottom_line": "There is likely a lower-back disc bulge.",
            "key_points": ["A disc bulge is described."],
            "confidence": {"label": "Moderate", "score": 70, "note": "Based on the available images."},
            "findings": [{"plain": "Likely disc bulge", "certainty": "Likely", "figure": "proof", "caption": "Disc bulge"}],
            "what_it_means": ["This can match back or leg symptoms."],
            "worth_flagging": [],
            "disclaimer": "Review with a qualified clinician.",
        },
    }


def _make_completed_job(job_id="abcdef12"):
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.pdf").write_bytes(PDF_BYTES)
    (report_dir / "report_clinical.pdf").write_bytes(PDF_BYTES)
    (report_dir / "proof.png").write_bytes(b"not-a-real-png")

    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(jd / "dicom"))
    job.status = "complete"
    job.mode = "agent"
    job.progress = 100
    job.pdf_path = str(report_dir / "report.pdf")
    job.annotated_images = {"proof": str(report_dir / "proof.png")}
    job.measurements = {
        "detected_anatomy": "spine",
        "anatomy_subregion": "lumbar",
        "modality": "MR",
        "calibration_status": "DICOM-calibrated",
        "study_description": "Lumbar spine MRI",
        "agent_summary": _summary(),
    }
    job.agent = {
        "success": True,
        "pdf_available": True,
        "figures": ["proof"],
        "summary": _summary(),
        "error": None,
    }
    return job


def test_report_contract_normalizes_agent_summary_when_legacy_interpretation_empty():
    job = _make_completed_job()

    payload = mika_app._build_report_payload(job)

    for key in ("study", "patient", "clinician", "findings", "confidence", "assets", "verification"):
        assert key in payload
    assert payload["patient"]["bottom_line"] == "There is likely a lower-back disc bulge."
    assert payload["clinician"]["impression"] == ["Likely L4-L5 disc bulge."]
    assert payload["interpretation"]["source"] == "agent_summary"
    assert payload["pdf_available"] is True
    assert payload["clinical_pdf_available"] is True
    assert payload["assets"]["pdf"] == {"patient_available": True, "clinical_available": True}
    assert payload["error_code"] is None
    assert payload["progress_phase"] == "complete"


def test_pdf_routes_work_for_live_and_persisted_jobs():
    job = _make_completed_job()
    mika_app.JOBS[job.job_id] = job

    assert asyncio.run(mika_app.get_report_pdf(job.job_id)).status_code == 200
    assert asyncio.run(mika_app.get_clinical_report_pdf(job.job_id)).status_code == 200

    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    assert asyncio.run(mika_app.get_report_pdf(job.job_id)).status_code == 200
    assert asyncio.run(mika_app.get_clinical_report_pdf(job.job_id)).status_code == 200
    assert asyncio.run(mika_app.get_report_pdf_alias(job.job_id)).status_code == 200
    assert asyncio.run(mika_app.get_clinical_report_pdf_alias(job.job_id)).status_code == 200


def test_persisted_report_reloads_with_recent_studies_contract():
    job = _make_completed_job()
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job.job_id))
    assert report["study"]["body_part"] == "Lower-back spine"
    assert report["patient"]["bottom_line"]
    assert report["pdf_available"] is True
    assert report["clinical_pdf_available"] is True

    recent = asyncio.run(mika_app.list_reports())["reports"]
    assert recent[0]["job_id"] == job.job_id
    assert recent[0]["title"] == "Lumbar spine MRI"
    assert recent[0]["pdf_available"] is True
    assert recent[0]["clinical_pdf_available"] is True


def test_auth_error_is_cleared_after_successful_preflight_rerun(monkeypatch):
    job = mika_app.AnalysisJob(job_id="fedcba98", dicom_dir=str(mika_app.DATA_DIR / "fedcba98" / "dicom"))
    mika_app.JOBS[job.job_id] = job

    class SignedOutRunner:
        def readiness_probe(self):
            return {
                "ready": False,
                "connected": False,
                "auth_state": "signed_out",
                "error_code": "CLAUDE_NOT_SIGNED_IN",
                "error_message": "Sign in with Claude before starting the read.",
                "preflight": {"uses_model": False},
            }

    monkeypatch.setattr(mika_app, "AgentRunner", lambda: SignedOutRunner())
    failed = asyncio.run(mika_app.start_analysis(
        mika_app.AnalyzeRequest(job_id=job.job_id, mode="agent"),
        BackgroundTasks(),
    ))
    assert failed.status_code == 400
    assert json.loads(failed.body)["error_code"] == "CLAUDE_NOT_SIGNED_IN"
    assert job.error_code == "CLAUDE_NOT_SIGNED_IN"
    assert job.auth_state == "signed_out"

    class ReadyRunner:
        def readiness_probe(self):
            return {
                "ready": True,
                "connected": True,
                "auth_state": "connected",
                "error_code": None,
                "error_message": None,
                "preflight": {"uses_model": False},
            }

    async def noop_agent_pipeline(**kwargs):
        return None

    monkeypatch.setattr(mika_app, "AgentRunner", lambda: ReadyRunner())
    monkeypatch.setattr(mika_app, "_run_agent_pipeline", noop_agent_pipeline)
    started = asyncio.run(mika_app.start_analysis(
        mika_app.AnalyzeRequest(job_id=job.job_id, mode="agent"),
        BackgroundTasks(),
    ))

    assert started["status"] == "started"
    assert job.error_code is None
    assert job.error is None
    assert job.auth_state == "connected"


class _RunResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_claude_availability_missing_cli(monkeypatch):
    monkeypatch.delenv("MIKA_AGENT_USE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agent_runner.shutil, "which", lambda _: None)
    monkeypatch.setattr(agent_runner.os.path, "exists", lambda _: False)

    info = agent_runner.AgentRunner().availability()

    assert info["connected"] is False
    assert info["auth_state"] == "missing_cli"
    assert info["error_code"] == "CLAUDE_CLI_MISSING"


def test_claude_availability_signed_out_and_signed_in(monkeypatch):
    monkeypatch.delenv("MIKA_AGENT_USE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agent_runner.shutil, "which", lambda _: "C:/Claude/claude.exe")

    def signed_out_run(args, **kwargs):
        if args[1] == "--version":
            return _RunResult(stdout="claude 1.0.0")
        return _RunResult(stderr="not logged in", returncode=1)

    monkeypatch.setattr(agent_runner.subprocess, "run", signed_out_run)
    signed_out = agent_runner.AgentRunner().availability()
    assert signed_out["connected"] is False
    assert signed_out["auth_state"] == "signed_out"
    assert signed_out["error_code"] == "CLAUDE_NOT_SIGNED_IN"

    def signed_in_run(args, **kwargs):
        if args[1] == "--version":
            return _RunResult(stdout="claude 1.0.0")
        return _RunResult(stdout='{"loggedIn": true, "subscriptionType": "pro"}')

    monkeypatch.setattr(agent_runner.subprocess, "run", signed_in_run)
    signed_in = agent_runner.AgentRunner().availability()
    assert signed_in["connected"] is True
    assert signed_in["auth_state"] == "connected"
    assert signed_in["subscription_type"] == "pro"
    assert signed_in["ready"] is True

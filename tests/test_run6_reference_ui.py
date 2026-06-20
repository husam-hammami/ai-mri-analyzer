import asyncio
import json
from io import BytesIO

import pytest
from starlette.datastructures import UploadFile

import app as mika_app
from services.reconciliation import build_reference_reconciliation, read_reference_report_bytes


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


def _reference_text() -> str:
    return (
        "At L5-S1 there are postoperative changes on the left. The report describes "
        "left lateral recess enhancing scar/fibrosis versus residual or recurrent disc "
        "with involvement of the descending S1 nerve root."
    )


def _summary() -> dict:
    text = "No left L5-S1 abnormal enhancement, residual or recurrent disc, or S1 nerve root compression."
    return {
        "findings": [{"text": text, "tier": "B", "figure": None, "evidence_refs": ["ev001"]}],
        "impression": [text],
        "patient": {
            "bottom_line": "MIKA independent read summary.",
            "findings": [{"plain": "MIKA independent read summary.", "certainty": "Likely"}],
            "confidence": {"label": "Moderate", "score": 70, "note": ""},
            "what_it_means": [],
            "worth_flagging": [],
        },
    }


def _make_completed_job(job_id: str = "abcdef12") -> mika_app.AnalysisJob:
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.pdf").write_bytes(PDF_BYTES)
    (report_dir / "report_clinical.pdf").write_bytes(PDF_BYTES)
    summary = _summary()
    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(jd / "dicom"))
    job.status = "complete"
    job.mode = "agent"
    job.progress = 100
    job.pdf_path = str(report_dir / "report.pdf")
    job.evidence_manifest = {
        "manifest_version": 1,
        "study": {"input_type": "dicom", "modality": "MR", "anatomy": "spine", "subregion": "lumbar", "calibrated": True},
        "selected_images": [{"evidence_id": "ev001", "relative_path": "work/evidence/images/ev001.png"}],
    }
    job.measurements = {
        "detected_anatomy": "spine",
        "anatomy_subregion": "lumbar",
        "modality": "MR",
        "calibration_status": "DICOM-calibrated",
        "study_description": "Lumbar spine MRI",
        "agent_summary": summary,
    }
    job.agent = {
        "success": True,
        "pdf_available": True,
        "figures": [],
        "summary": summary,
        "error": None,
    }
    return job


def _reference_pdf_bytes() -> bytes:
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, _reference_text())
    c.save()
    return buf.getvalue()


def test_reconcile_paste_request_handling():
    job = _make_completed_job()
    mika_app._persist_report(job)

    data = asyncio.run(mika_app.reconcile_completed_report(
        mika_app.ReconcileRequest(job_id=job.job_id, reference_report_text=_reference_text())
    ))

    assert data["reconciliation"]["used"] is True
    assert data["patient"]["reference_reconciliation"]["items"]


def test_reconcile_upload_request_handling_pdf():
    job = _make_completed_job()
    mika_app._persist_report(job)
    upload = UploadFile(file=BytesIO(_reference_pdf_bytes()), filename="reference.pdf")

    data = asyncio.run(
        mika_app.reconcile_completed_report_upload(
            job_id=job.job_id,
            reference_report_text=None,
            reference_report=upload,
        )
    )

    assert data["reconciliation"]["summary"]["has_discrepancy"] is True
    assert data["clinical_pdf_available"] is True


def test_user_facing_status_labels_are_plain_language():
    rec = build_reference_reconciliation(blind_summary=_summary(), reference_text=_reference_text())

    visible_patient = {
        **rec["patient"],
        "items": [{k: v for k, v in item.items() if k != "status"} for item in rec["patient"]["items"]],
    }
    patient_text = json.dumps(visible_patient).lower()
    assert "conflicts_with_reference" not in patient_text
    assert "mika's independent read differs from the uploaded report" in patient_text
    assert "uploaded report may contain clinically important findings" in patient_text


def test_patient_and_clinician_reconciliation_pdf_sections_are_separate():
    job = _make_completed_job()
    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())

    report_dir = mika_app.DATA_DIR / job.job_id / "work" / "report"
    patient_text = read_reference_report_bytes("report.pdf", (report_dir / "report.pdf").read_bytes()).lower()
    clinical_text = read_reference_report_bytes("report.pdf", (report_dir / "report_clinical.pdf").read_bytes()).lower()

    assert "reference-assisted review" in patient_text
    assert "uploaded report" in patient_text
    assert "mika independent read" in patient_text
    assert "reference-assisted reconciliation" in clinical_text
    assert "reference target" in clinical_text
    assert "mika blind finding" in clinical_text


def test_persisted_reload_and_no_marker_creation_from_reconciliation_rows():
    job = _make_completed_job()
    before = len(job.agent["summary"]["patient"]["findings"])
    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job.job_id))

    assert len(report["patient"]["findings"]) == before
    assert report["reconciliation"]["used"] is True
    assert report["patient"]["reference_reconciliation"]["items"]

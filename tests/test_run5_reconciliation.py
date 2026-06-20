import asyncio
import json

import pytest

import app as mika_app
from services.reconciliation import (
    build_reference_reconciliation,
    extract_reference_targets,
    reconcile_reference_targets,
)


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


def _target() -> dict:
    return extract_reference_targets(_reference_text())[0]


def _summary(text: str, *, evidence_refs=None) -> dict:
    return {
        "findings": [{
            "text": text,
            "tier": "B",
            "figure": "proof",
            "evidence_refs": evidence_refs or [],
        }],
        "impression": [text],
        "patient": {
            "bottom_line": "Blind read summary.",
            "findings": [{"plain": "Blind read summary.", "certainty": "Likely"}],
            "confidence": {"label": "Moderate", "score": 70, "note": ""},
        },
    }


def _make_completed_job(job_id: str = "abcdef12") -> mika_app.AnalysisJob:
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.pdf").write_bytes(PDF_BYTES)
    (report_dir / "report_clinical.pdf").write_bytes(PDF_BYTES)
    (report_dir / "proof.png").write_bytes(b"not-a-real-png")
    summary = _summary(
        "No left L5-S1 abnormal enhancement, residual or recurrent disc, or S1 nerve root compression.",
        evidence_refs=["ev001"],
    )
    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(jd / "dicom"))
    job.status = "complete"
    job.mode = "agent"
    job.progress = 100
    job.pdf_path = str(report_dir / "report.pdf")
    job.annotated_images = {"proof": str(report_dir / "proof.png")}
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
        "figures": ["proof"],
        "summary": summary,
        "error": None,
    }
    return job


def test_reference_target_extraction_schema_for_february_p0_pattern():
    targets = extract_reference_targets(_reference_text())

    assert targets
    target = targets[0]
    for key in (
        "reference_finding",
        "anatomy",
        "level",
        "side",
        "modality_sequence_needed",
        "evidence_refs",
    ):
        assert key in target
    assert target["level"] == "L5-S1"
    assert target["side"] == "left"
    assert "postoperative lateral recess" in target["reference_finding"]


def test_reconciliation_agreement_statuses():
    target = _target()

    confirmed = reconcile_reference_targets(
        [target],
        _summary(
            "Left L5-S1 postoperative enhancing scar or recurrent disc in the lateral recess contacts the S1 nerve root.",
            evidence_refs=["ev001"],
        ),
    )[0]
    partial = reconcile_reference_targets(
        [target],
        _summary("Left L5-S1 postoperative change is present.", evidence_refs=[]),
    )[0]
    unseen = reconcile_reference_targets([target], _summary("L4-L5 mild disc bulge.", evidence_refs=["ev002"]))[0]
    conflict = reconcile_reference_targets(
        [target],
        _summary("No left L5-S1 abnormal enhancement, recurrent disc, or S1 nerve root compression.", evidence_refs=["ev003"]),
    )[0]
    cannot = reconcile_reference_targets([target], {})[0]

    assert confirmed["agreement_status"] == "confirmed"
    assert partial["agreement_status"] == "partially_supported"
    assert unseen["agreement_status"] == "not_independently_seen"
    assert conflict["agreement_status"] == "conflicts_with_reference"
    assert cannot["agreement_status"] == "cannot_assess"


def test_blind_disagreement_is_not_forced_to_confirmation():
    target = _target()

    item = reconcile_reference_targets(
        [target],
        _summary("No left L5-S1 recurrent disc or S1 nerve root compression.", evidence_refs=["ev003"]),
    )[0]

    assert item["agreement_status"] != "confirmed"
    assert item["agreement_status"] == "conflicts_with_reference"
    assert "did not independently confirm" in item["patient_explanation"]


def test_patient_and_clinician_wording_are_separated():
    rec = build_reference_reconciliation(
        blind_summary=_summary("No left L5-S1 recurrent disc or S1 nerve root compression.", evidence_refs=["ev003"]),
        reference_text=_reference_text(),
    )

    patient_text = json.dumps(rec["patient"]).lower()
    clinician_text = json.dumps(rec["clinician"]).lower()

    for jargon in ("lateral recess", "fibrosis", "foraminal", "tier"):
        assert jargon not in patient_text
    assert "clinically important" in patient_text
    assert "lateral recess" in clinician_text
    assert "residual-recurrent disc" in clinician_text


def test_persisted_reconciliation_reloads_and_recent_studies_flag_it():
    job = _make_completed_job()
    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job.job_id))
    recent = asyncio.run(mika_app.list_reports())["reports"]

    assert report["reconciliation"]["used"] is True
    assert report["patient"]["reference_reconciliation"]["items"]
    assert report["clinician"]["reference_reconciliation"]["items"]
    assert recent[0]["reference_reconciliation_available"] is True


def test_pdf_routes_available_after_persisted_reconciliation_reload():
    job = _make_completed_job()
    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    assert asyncio.run(mika_app.get_report_pdf(job.job_id)).status_code == 200
    assert asyncio.run(mika_app.get_clinical_report_pdf(job.job_id)).status_code == 200


def test_reconciliation_does_not_create_body_marker_findings():
    job = _make_completed_job()
    before = len(job.agent["summary"]["patient"]["findings"])

    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())

    after = len(job.agent["summary"]["patient"]["findings"])
    assert after == before
    assert "reference_reconciliation" in job.agent["summary"]["patient"]

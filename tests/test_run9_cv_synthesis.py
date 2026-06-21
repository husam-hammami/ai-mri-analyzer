import asyncio
import json

import pytest

import app as mika_app
from services.cv_synthesis import (
    synthesize_cv_candidate_reviews,
    upgrade_reconciliation_with_cv_supported_findings,
)
from services.reconciliation import build_reference_reconciliation, read_reference_report_bytes


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


def _candidate() -> dict:
    return {
        "candidate_id": "lumbar_l5_s1_left_prepost_lateral_recess_001",
        "anatomy": "lumbar_spine",
        "level": "L5-S1",
        "side": "left",
        "series_ids": ["s001", "s002"],
        "slice_ids": ["sl001", "sl002"],
        "candidate_type": "pre_post_contrast_lateral_recess_roi",
        "roi": {"unit": "normalized_image_fraction", "x": 0.58, "y": 0.42, "width": 0.24, "height": 0.30},
        "calibration_state": "calibrated",
        "geometry_confidence": 0.78,
        "registration_confidence": 0.94,
        "limitations": ["Localization only."],
        "evidence_refs": ["ev001", "ev002"],
        "artifact_trust": {"body_marker": False, "proof_overlay": False, "pinpoint_marker": False},
    }


def _manifest() -> dict:
    return {
        "manifest_version": 1,
        "study": {"input_type": "dicom", "modality": "MR", "calibrated": True},
        "selected_images": [{"evidence_id": "ev001", "relative_path": "work/evidence/images/ev001.png"}],
        "cv_candidates": [_candidate()],
        "cv_candidate_policy": {
            "deterministic_cv_does_not_create_findings": True,
            "marker_thresholds_still_apply": True,
        },
    }


def _supported_review(**overrides) -> dict:
    row = {
        "candidate_id": _candidate()["candidate_id"],
        "status": "supported",
        "evidence_refs_used": ["ev001", "ev002"],
        "short_reason": "Focused region is supported on reviewed evidence.",
        "patient_wording": "MIKA reviewed this focused area separately.",
        "clinician_wording": "Focused left L5-S1 lateral recess evidence is supported.",
    }
    row.update(overrides)
    return row


def _blind_summary() -> dict:
    return {
        "findings": [{
            "text": "No left L5-S1 abnormal enhancement, residual or recurrent disc, or S1 nerve root compression.",
            "tier": "B",
            "evidence_refs": ["ev003"],
        }],
        "impression": ["No left L5-S1 abnormal enhancement."],
        "patient": {
            "bottom_line": "Blind read remains separate.",
            "findings": [{"plain": "MIKA did not flag a matching abnormality.", "certainty": "Likely"}],
            "confidence": {"label": "Moderate", "score": 70, "note": ""},
            "what_it_means": ["Review the official report with your clinician."],
        },
    }


def _reference_text() -> str:
    return (
        "At L5-S1 there are postoperative changes on the left. The report describes "
        "left lateral recess enhancing scar/fibrosis versus residual or recurrent disc "
        "with involvement of the descending S1 nerve root."
    )


def _make_job(job_id: str = "abc12345") -> mika_app.AnalysisJob:
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    evidence_dir = jd / "work" / "evidence"
    report_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest()
    (evidence_dir / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    summary = _blind_summary()
    summary["cv_candidate_reviews"] = [_supported_review()]
    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(jd / "dicom"))
    job.status = "complete"
    job.mode = "agent"
    job.progress = 100
    job.evidence_manifest = {**manifest, "manifest_path": "work/evidence/evidence_manifest.json"}
    job.measurements = {
        "detected_anatomy": "spine",
        "anatomy_subregion": "lumbar",
        "modality": "MR",
        "calibration_status": "DICOM-calibrated",
        "study_description": "Synthetic lumbar MRI",
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


def test_supported_candidate_synthesis_creates_patient_and_clinician_rows():
    blind = _blind_summary()

    synthesis = synthesize_cv_candidate_reviews(
        blind_report=blind,
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[_supported_review()],
        verifier_result={},
        cv_candidate_policy=_manifest()["cv_candidate_policy"],
    )

    assert synthesis["used"] is True
    assert synthesis["clinician_findings"][0]["candidate_id"] == _candidate()["candidate_id"]
    assert synthesis["clinician_findings"][0]["evidence_refs"] == ["ev001", "ev002"]
    assert "diagnosis" in synthesis["patient_explanations"][0]["plain"]
    assert "cv_candidate" not in json.dumps(synthesis["patient_explanations"]).lower()
    assert blind["findings"][0]["text"].startswith("No left L5-S1")


def test_unsupported_candidate_reviews_do_not_become_findings():
    for status in ("not_supported", "cannot_assess", "localization_wrong"):
        synthesis = synthesize_cv_candidate_reviews(
            blind_report=_blind_summary(),
            cv_candidates=[_candidate()],
            cv_candidate_reviews=[_supported_review(status=status)],
            verifier_result={},
        )
        assert synthesis["used"] is False
        assert synthesis["clinician_findings"] == []
        assert synthesis["patient_explanations"] == []

    verifier_rejected = synthesize_cv_candidate_reviews(
        blind_report=_blind_summary(),
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[_supported_review()],
        verifier_result={"cv_candidate_reviews": [_supported_review(status="localization_wrong")]},
    )
    assert verifier_rejected["used"] is False


def test_reconciliation_conflict_can_move_to_partially_supported_by_focused_evidence():
    rec = build_reference_reconciliation(
        blind_summary=_blind_summary(),
        reference_text=_reference_text(),
        evidence_manifest=_manifest(),
    )
    assert rec["items"][0]["agreement_status"] == "conflicts_with_reference"
    synthesis = synthesize_cv_candidate_reviews(
        blind_report=_blind_summary(),
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[_supported_review()],
    )

    upgraded = upgrade_reconciliation_with_cv_supported_findings(
        rec,
        synthesis["clinician_findings"],
    )

    assert upgraded["items"][0]["agreement_status"] in {
        "partially_supported",
        "supported_by_focused_evidence",
    }
    assert upgraded["items"][0]["agreement_status"] == "partially_supported"
    assert upgraded["items"][0]["blind_read_discrepancy_preserved"] is True
    assert "focused" in json.dumps(upgraded["patient"]).lower()
    assert "scar versus recurrent disc" in upgraded["items"][0]["clinician_explanation"].lower()


def test_persisted_report_reloads_synthesized_fields_and_preserves_blind_findings():
    job = _make_job()
    original_findings = list(job.agent["summary"]["findings"])

    mika_app._rewrite_agent_summary_and_patient_pdf(job, job.agent["summary"])
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job.job_id))

    assert report["patient"]["findings"] == _blind_summary()["patient"]["findings"]
    assert report["agent"]["summary"]["findings"] == original_findings
    assert report["patient"]["cv_supported_explanations"]
    assert report["clinician"]["cv_supported_findings"]
    assert report["assets"]["cv_candidate_reviews"][0]["status"] == "supported"
    assert report["confidence"]["cv_candidate_policy"]["deterministic_cv_does_not_create_findings"] is True
    assert report["clinician"]["cv_supported_findings"][0]["artifact_trust"]["body_marker"] is False


def test_pdf_routes_include_synthesized_sections():
    job = _make_job()
    mika_app._rewrite_agent_summary_and_patient_pdf(job, job.agent["summary"])
    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())
    mika_app._persist_report(job)

    report_dir = mika_app.DATA_DIR / job.job_id / "work" / "report"
    patient_text = read_reference_report_bytes("report.pdf", (report_dir / "report.pdf").read_bytes()).lower()
    clinical_text = read_reference_report_bytes("report.pdf", (report_dir / "report_clinical.pdf").read_bytes()).lower()

    assert "focused evidence review" in patient_text
    assert "mika reviewed a focused area" in patient_text
    assert "cv-supported focused evidence" in clinical_text
    assert "reference-assisted reconciliation" in clinical_text
    assert asyncio.run(mika_app.get_report_pdf(job.job_id)).status_code == 200
    assert asyncio.run(mika_app.get_clinical_report_pdf(job.job_id)).status_code == 200

import asyncio
import json

import pytest

import app as mika_app
from services.cv_adjudication import adjudicate_cv_candidate_reviews
from services.cv_synthesis import (
    annotate_reconciliation_with_cv_adjudication,
    synthesize_cv_candidate_reviews,
    upgrade_reconciliation_with_cv_supported_findings,
)
from services.evidence_pack import EvidencePackBuilder
from services.reconciliation import build_reference_reconciliation, read_reference_report_bytes


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


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


def _review(status: str = "supported", reason: str = "Focused region is supported.") -> dict:
    return {
        "candidate_id": _candidate()["candidate_id"],
        "status": status,
        "evidence_refs_used": ["ev001", "ev002"],
        "short_reason": reason,
        "pre_post_enhancement_support": "Reviewed same-level pre/post evidence.",
        "level_side_localization": "Level and side matched candidate metadata.",
        "visible_evidence_reason": reason,
        "patient_wording": "MIKA checked this focused area separately.",
        "clinician_wording": "Focused left L5-S1 lateral recess evidence was reviewed.",
    }


def _blind_summary(reviews=None) -> dict:
    return {
        "findings": [{
            "text": "No left L5-S1 abnormal enhancement, residual or recurrent disc, or S1 nerve root compression.",
            "tier": "B",
            "evidence_refs": ["ev003"],
        }],
        "impression": ["No left L5-S1 abnormal enhancement."],
        "cv_candidate_reviews": reviews if reviews is not None else [_review()],
        "patient": {
            "bottom_line": "Blind read remains separate.",
            "findings": [{"plain": "MIKA did not flag a matching abnormality.", "certainty": "Likely"}],
            "confidence": {"label": "Moderate", "score": 70, "note": ""},
            "what_it_means": ["Review the official report with a clinician."],
        },
    }


def _reference_text() -> str:
    return (
        "At L5-S1 there are postoperative changes on the left. The report describes "
        "left lateral recess enhancing scar/fibrosis versus residual or recurrent disc "
        "with involvement of the descending S1 nerve root."
    )


def _make_job(job_id: str = "abc12345", summary: dict | None = None) -> mika_app.AnalysisJob:
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    evidence_dir = jd / "work" / "evidence"
    report_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.pdf").write_bytes(PDF_BYTES)
    (report_dir / "report_clinical.pdf").write_bytes(PDF_BYTES)
    manifest = _manifest()
    (evidence_dir / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    summary = summary or _blind_summary()
    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(jd / "dicom"))
    job.status = "complete"
    job.mode = "agent"
    job.progress = 100
    job.pdf_path = str(report_dir / "report.pdf")
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


def test_repeated_reviews_majority_supported_final_supported():
    rows = adjudicate_cv_candidate_reviews(
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[
            _review("supported", "Supported on first focused review."),
            _review("supported", "Supported on second focused review."),
            _review("cannot_assess", "One pass was limited."),
        ],
        evidence_bundle_id="bundle-1",
    )

    assert rows[0]["review_count"] == 3
    assert rows[0]["majority_status"] == "supported"
    assert rows[0]["final_status"] == "supported"
    assert rows[0]["disagreement"] is True


def test_split_reviews_become_unstable_and_block_synthesis():
    adjudication = adjudicate_cv_candidate_reviews(
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[
            _review("supported", "One pass supported the ROI."),
            _review("not_supported", "One pass did not support the ROI."),
        ],
    )
    synthesis = synthesize_cv_candidate_reviews(
        blind_report=_blind_summary(),
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[_review("supported")],
        cv_candidate_adjudication=adjudication,
    )

    assert adjudication[0]["final_status"] == "unstable"
    assert adjudication[0]["disagreement"] is True
    assert synthesis["used"] is False
    assert synthesis["patient_explanations"] == []
    assert synthesis["clinician_findings"] == []


def test_localization_wrong_vetoes_candidate_adjudication():
    rows = adjudicate_cv_candidate_reviews(
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[
            _review("supported", "One pass supported the ROI."),
            _review("supported", "Another pass supported the ROI."),
            _review("localization_wrong", "Level or side was wrong."),
        ],
    )

    assert rows[0]["majority_status"] == "supported"
    assert rows[0]["final_status"] == "localization_wrong"
    assert "no focused-evidence synthesis" in " ".join(rows[0]["limitations"]).lower()


def test_reconciliation_keeps_discrepancy_when_adjudication_is_unstable():
    rec = build_reference_reconciliation(
        blind_summary=_blind_summary(),
        reference_text=_reference_text(),
        evidence_manifest=_manifest(),
    )
    assert rec["items"][0]["agreement_status"] == "conflicts_with_reference"
    adjudication = adjudicate_cv_candidate_reviews(
        cv_candidates=[_candidate()],
        cv_candidate_reviews=[_review("supported"), _review("not_supported")],
    )

    annotated = annotate_reconciliation_with_cv_adjudication(rec, adjudication)
    upgraded = upgrade_reconciliation_with_cv_supported_findings(annotated, [])

    assert upgraded["items"][0]["agreement_status"] == "conflicts_with_reference"
    assert upgraded["items"][0]["focused_review_final_status"] == "unstable"
    assert "focused reviews did not agree" in upgraded["items"][0]["patient_explanation"].lower()
    assert "final_status=unstable" in upgraded["items"][0]["clinician_explanation"]


def test_persisted_reload_clears_stale_supported_text_when_adjudication_unstable():
    summary = _blind_summary(reviews=[_review("supported"), _review("not_supported")])
    summary["patient"]["cv_supported_explanations"] = [{"plain": "Stale supported focused text."}]
    summary["cv_supported_findings"] = [{"candidate_id": _candidate()["candidate_id"], "status": "supported"}]
    job = _make_job(summary=summary)

    mika_app._rewrite_agent_summary_and_patient_pdf(job, job.agent["summary"])
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job.job_id))

    assert report["cv_candidate_adjudication"][0]["final_status"] == "unstable"
    assert report["patient"]["cv_supported_explanations"] == []
    assert report["clinician"]["cv_supported_findings"] == []
    assert report["assets"]["cv_candidate_adjudication"][0]["final_status"] == "unstable"


def test_clinical_pdf_includes_adjudication_table():
    summary = _blind_summary(reviews=[_review("supported"), _review("not_supported")])
    job = _make_job(summary=summary)

    mika_app._rewrite_agent_summary_and_patient_pdf(job, job.agent["summary"])
    mika_app._apply_reference_reconciliation(job, reference_report_text=_reference_text())

    report_dir = mika_app.DATA_DIR / job.job_id / "work" / "report"
    patient_text = read_reference_report_bytes("report.pdf", (report_dir / "report.pdf").read_bytes()).lower()
    clinical_text = read_reference_report_bytes("report.pdf", (report_dir / "report_clinical.pdf").read_bytes()).lower()

    assert "cv candidate adjudication" in clinical_text
    assert "final=unstable" in clinical_text
    assert "cv candidate adjudication" not in patient_text
    assert "focused reviews did not agree" in patient_text


def test_evidence_pack_candidate_payload_is_deterministic(tmp_path):
    from test_run8_cv_pipeline import _make_lumbar_study

    study = _make_lumbar_study(tmp_path)
    first = EvidencePackBuilder(study, tmp_path / "work1").build().to_manifest()["cv_candidates"]
    second = EvidencePackBuilder(study, tmp_path / "work2").build().to_manifest()["cv_candidates"]

    assert first == second

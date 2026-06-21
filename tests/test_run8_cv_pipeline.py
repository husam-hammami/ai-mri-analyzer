import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import app as mika_app
from services.agent_runner import AgentRunner
from services.evidence_pack import EvidencePackBuilder
from services.verification import VerificationPass


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


def _write_dicom(
    path: Path,
    *,
    series_uid: str,
    study_uid: str,
    series_number: int,
    series_description: str,
    instance_number: int,
    image_orientation,
    image_position,
) -> None:
    pydicom = pytest.importorskip("pydicom")
    from pydicom.dataset import FileDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

    path.parent.mkdir(parents=True, exist_ok=True)
    meta = pydicom.Dataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = generate_uid()
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.PatientName = "Synthetic^Patient"
    ds.PatientID = "SYNTH"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = "MR"
    ds.SeriesDescription = series_description
    ds.ProtocolName = series_description
    ds.SeriesNumber = series_number
    ds.InstanceNumber = instance_number
    ds.Rows = 64
    ds.Columns = 80
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.WindowCenter = 100
    ds.WindowWidth = 200
    ds.SliceThickness = 4.0
    ds.PixelSpacing = [0.7, 0.7]
    ds.ImageOrientationPatient = image_orientation
    ds.ImagePositionPatient = image_position
    ds.SliceLocation = float(image_position[2])
    arr = (np.arange(64 * 80, dtype=np.uint16).reshape(64, 80) + instance_number) % 4096
    ds.PixelData = arr.tobytes()
    ds.save_as(str(path))


def _make_lumbar_study(tmp_path: Path) -> Path:
    pydicom = pytest.importorskip("pydicom")
    study = tmp_path / "lumbar"
    study_uid = pydicom.uid.generate_uid()
    sag_uid = pydicom.uid.generate_uid()
    pre_uid = pydicom.uid.generate_uid()
    post_uid = pydicom.uid.generate_uid()
    for i in range(1, 7):
        _write_dicom(
            study / "sag" / f"{i:03d}.dcm",
            series_uid=sag_uid,
            study_uid=study_uid,
            series_number=1,
            series_description="Sag T2 L SPINE",
            instance_number=i,
            image_orientation=[0, 1, 0, 0, 0, 1],
            image_position=[float(i), 0.0, 50.0],
        )
    for i in range(1, 21):
        z = 120.0 - (i - 1) * 4.0
        _write_dicom(
            study / "pre" / f"{i:03d}.dcm",
            series_uid=pre_uid,
            study_uid=study_uid,
            series_number=2,
            series_description="t1_vibe_fs_tra L SPINE",
            instance_number=i,
            image_orientation=[1, 0, 0, 0, 1, 0],
            image_position=[0.0, 0.0, z],
        )
        _write_dicom(
            study / "post" / f"{i:03d}.dcm",
            series_uid=post_uid,
            study_uid=study_uid,
            series_number=3,
            series_description="t1_vibe_fs_tra-CONT L SPINE",
            instance_number=i,
            image_orientation=[1, 0, 0, 0, 1, 0],
            image_position=[0.0, 0.0, z],
        )
    return study


def _candidate_manifest() -> dict:
    return {
        "manifest_version": 1,
        "study": {"input_type": "dicom", "modality": "MR", "calibrated": True},
        "series": [],
        "selected_images": [{"evidence_id": "ev001", "relative_path": "evidence/images/ev001.png"}],
        "limitations": [],
        "cv_candidates": [{
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
            "limitations": ["CV localization only."],
            "evidence_refs": ["s001:sl001", "s002:sl002"],
            "artifact_trust": {"body_marker": False, "proof_overlay": False, "pinpoint_marker": False},
        }],
        "cv_candidate_limitations": [],
    }


def test_candidate_manifest_included_in_evidence_pack(tmp_path):
    study = _make_lumbar_study(tmp_path)

    manifest = EvidencePackBuilder(study, tmp_path / "work").build().to_manifest()

    assert manifest["cv_candidates"]
    candidate = manifest["cv_candidates"][0]
    assert candidate["candidate_id"]
    assert candidate["level"] == "L5-S1"
    assert candidate["side"] == "left"
    assert candidate["roi"]["unit"] == "normalized_image_fraction"
    assert candidate["selected_evidence_refs"]
    assert all(ref.startswith("ev") for ref in candidate["selected_evidence_refs"])
    assert candidate["geometry_confidence"] < 0.80
    assert candidate["artifact_trust"]["body_marker"] is False
    assert candidate["artifact_trust"]["proof_overlay"] is False


def test_agent_prompt_includes_cv_candidate_review_instructions(tmp_path):
    manifest_path = tmp_path / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(_candidate_manifest()), encoding="utf-8")

    prompt = AgentRunner()._build_prompt(
        study_dir=tmp_path / "study",
        out_dir=tmp_path / "work" / "report",
        anatomy="spine",
        modality="MR",
        evidence_manifest_path=str(manifest_path),
    )

    assert "CV EVIDENCE CANDIDATES" in prompt
    assert "cv_candidate_reviews" in prompt
    for status in ("supported", "not_supported", "cannot_assess", "localization_wrong"):
        assert status in prompt
    assert "Do not upgrade a CV candidate into a confirmed finding" in prompt


def test_verifier_preserves_candidate_review_statuses():
    rows = VerificationPass._normalize_cv_candidate_reviews([
        {"candidate_id": "a", "status": "supported", "evidence_refs_used": "ev001"},
        {"candidate_id": "b", "status": "not_supported"},
        {"candidate_id": "c", "status": "cannot_assess"},
        {"candidate_id": "d", "status": "localization_wrong"},
        {"candidate_id": "e", "status": "made_up"},
    ])

    assert [row["status"] for row in rows] == [
        "supported",
        "not_supported",
        "cannot_assess",
        "localization_wrong",
        "cannot_assess",
    ]
    assert rows[0]["evidence_refs_used"] == ["ev001"]


def test_report_persistence_reloads_cv_candidates_and_reviews(tmp_path):
    job_id = "abc12345"
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    evidence_dir = jd / "work" / "evidence"
    report_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.pdf").write_bytes(PDF_BYTES)
    (report_dir / "report_clinical.pdf").write_bytes(PDF_BYTES)
    manifest = _candidate_manifest()
    (evidence_dir / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    summary = {
        "findings": [{"text": "Blind technical finding.", "tier": "B"}],
        "impression": ["Blind impression."],
        "cv_candidate_reviews": [{
            "candidate_id": manifest["cv_candidates"][0]["candidate_id"],
            "status": "not_supported",
            "evidence_refs_used": ["ev001"],
            "short_reason": "Synthetic candidate was reviewed separately.",
            "patient_wording": "MIKA checked this focused area separately.",
            "clinician_wording": "Candidate localization reviewed separately from final findings.",
        }],
        "patient": {
            "bottom_line": "Blind read remains separate.",
            "findings": [{"plain": "Blind patient finding", "certainty": "Likely"}],
            "confidence": {"label": "Moderate"},
        },
    }
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
        "agent_summary": summary,
    }
    job.agent = {"success": True, "pdf_available": True, "summary": summary, "error": None}

    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job_id))

    assert report["cv_candidates"][0]["candidate_id"] == manifest["cv_candidates"][0]["candidate_id"]
    assert report["cv_candidate_reviews"][0]["status"] == "not_supported"
    assert report["clinician"]["cv_candidate_reviews"][0]["status"] == "not_supported"
    assert len(report["patient"]["findings"]) == 1


def test_marker_suppression_when_candidate_confidence_below_threshold(tmp_path):
    manifest = _candidate_manifest()
    candidate = manifest["cv_candidates"][0]

    assert candidate["geometry_confidence"] < 0.80
    assert candidate["artifact_trust"] == {
        "body_marker": False,
        "proof_overlay": False,
        "pinpoint_marker": False,
    }


def test_jpg_export_does_not_create_cv_candidates(tmp_path):
    study = tmp_path / "jpg"
    study.mkdir()
    Image.fromarray(np.full((64, 64), 120, dtype=np.uint8)).save(study / "slice.jpg")

    manifest = EvidencePackBuilder(study, tmp_path / "work").build().to_manifest()

    assert manifest["study"]["input_type"] == "image_export"
    assert manifest["study"]["calibrated"] is False
    assert manifest["cv_candidates"] == []
    assert any("image export" in note.lower() for note in manifest["cv_candidate_limitations"])

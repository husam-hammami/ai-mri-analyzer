import asyncio
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image

import app as mika_app
from services.artifacts import ArtifactQaGate, ArtifactRegistry
from services.evidence_pack import EvidencePackBuilder


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


def _write_png(path: Path, value: int = 120, size: tuple[int, int] = (64, 64)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((size[1], size[0]), value, dtype=np.uint8)
    arr[:, ::4] = min(255, value + 30)
    Image.fromarray(arr).save(path)


def _write_jpg(path: Path, value: int = 120) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((64, 64), value, dtype=np.uint8)
    arr[16:48, 16:48] = min(255, value + 40)
    Image.fromarray(arr).save(path)


def _write_dicom(
    path: Path,
    *,
    series_uid: str,
    series_description: str,
    instance_number: int,
    pixel_spacing: bool = True,
    modality: str = "MR",
) -> None:
    pydicom = pytest.importorskip("pydicom")
    from pydicom.dataset import FileDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

    path.parent.mkdir(parents=True, exist_ok=True)
    file_meta = pydicom.Dataset()
    file_meta.MediaStorageSOPClassUID = MRImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.PatientName = "Synthetic^Patient"
    ds.PatientID = "SYNTHETIC"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = series_uid
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.Modality = modality
    ds.SeriesDescription = series_description
    ds.InstanceNumber = instance_number
    ds.Rows = 32
    ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.WindowCenter = 100
    ds.WindowWidth = 200
    ds.SliceThickness = 4.0
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.SliceLocation = float(instance_number)
    if pixel_spacing:
        ds.PixelSpacing = [0.7, 0.7]
    arr = (np.arange(32 * 32, dtype=np.uint16).reshape(32, 32) + instance_number) % 256
    ds.PixelData = arr.tobytes()
    ds.save_as(str(path))


def _summary_with_trustless_proof() -> dict:
    return {
        "findings": [{"text": "L4-L5 disc bulge.", "tier": "B", "figure": "proof", "evidence_refs": []}],
        "impression": ["Likely L4-L5 disc bulge."],
        "patient": {
            "bottom_line": "There is likely a lower-back disc bulge.",
            "findings": [{"plain": "Likely disc bulge at L4-L5", "certainty": "Likely", "figure": "proof", "caption": "Disc bulge", "evidence_refs": []}],
            "confidence": {"label": "Moderate", "score": 70, "note": "Based on the available images."},
        },
    }


def _make_completed_job(job_id: str = "abcdef12") -> mika_app.AnalysisJob:
    jd = mika_app.DATA_DIR / job_id
    report_dir = jd / "work" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.pdf").write_bytes(PDF_BYTES)
    (report_dir / "report_clinical.pdf").write_bytes(PDF_BYTES)
    _write_png(report_dir / "proof.png")

    summary = _summary_with_trustless_proof()
    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(jd / "dicom"))
    job.status = "complete"
    job.mode = "agent"
    job.progress = 100
    job.pdf_path = str(report_dir / "report.pdf")
    job.annotated_images = {"proof": str(report_dir / "proof.png")}
    job.evidence_manifest = {
        "manifest_version": 1,
        "study": {"input_type": "dicom", "modality": "MR", "anatomy": "spine", "subregion": "lumbar", "calibrated": True},
        "series": [],
        "selected_images": [{"evidence_id": "ev001", "relative_path": "work/evidence/images/ev001.png"}],
        "limitations": [],
        "manifest_path": "work/evidence/evidence_manifest.json",
    }
    ev_dir = jd / "work" / "evidence" / "images"
    _write_png(ev_dir / "ev001.png")
    (jd / "work" / "evidence" / "evidence_manifest.json").write_text(json.dumps(job.evidence_manifest), encoding="utf-8")
    job.artifact_registry = {"manifest_version": 1, "artifacts": []}
    job.artifact_qa = {"status": "limited", "artifact_count": 1}
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


def test_evidence_pack_classifies_calibrated_dicom(tmp_path):
    study = tmp_path / "study"
    uid = "1.2.826.0.1.3680043.8.498.1"
    for i in range(1, 4):
        _write_dicom(study / f"img_{i:03d}.dcm", series_uid=uid, series_description="Sag T2", instance_number=i)

    pack = EvidencePackBuilder(study, tmp_path / "work").build()
    manifest = pack.to_manifest()

    assert manifest["study"]["input_type"] == "dicom"
    assert manifest["study"]["modality"] == "MR"
    assert manifest["study"]["calibrated"] is True
    assert len(manifest["selected_images"]) == 3
    assert manifest["series"][0]["pixel_spacing"] == [0.7, 0.7]
    assert all((tmp_path / "work" / item["relative_path"]).is_file() for item in manifest["selected_images"])


def test_evidence_pack_classifies_image_exports_as_uncalibrated(tmp_path):
    study = tmp_path / "image_export"
    for i in range(5):
        _write_jpg(study / f"slice_{i:03d}.jpg", 90 + i)

    manifest = EvidencePackBuilder(study, tmp_path / "work").build().to_manifest()

    assert manifest["study"]["input_type"] == "image_export"
    assert manifest["study"]["calibrated"] is False
    assert manifest["study"]["calibration_reason"]
    assert len(manifest["selected_images"]) == 5


def test_evidence_pack_selects_40_to_80_images_and_excludes_localizers(tmp_path):
    pydicom = pytest.importorskip("pydicom")
    study = tmp_path / "large"
    diag_a = pydicom.uid.generate_uid()
    diag_b = pydicom.uid.generate_uid()
    scout = pydicom.uid.generate_uid()
    for i in range(1, 46):
        _write_dicom(study / "sag" / f"{i:03d}.dcm", series_uid=diag_a, series_description="Sag T2", instance_number=i)
        _write_dicom(study / "ax" / f"{i:03d}.dcm", series_uid=diag_b, series_description="Ax T2", instance_number=i)
    for i in range(1, 13):
        _write_dicom(study / "localizer" / f"{i:03d}.dcm", series_uid=scout, series_description="Localizer", instance_number=i)

    manifest = EvidencePackBuilder(study, tmp_path / "work").build().to_manifest()

    selected = manifest["selected_images"]
    assert 40 <= len(selected) <= 80
    assert len(selected) == 80
    assert not any(item["is_localizer"] for item in selected)
    assert manifest["study"]["localizer_excluded_count"] == 12


def test_artifact_gate_suppresses_proof_and_marker_without_evidence(tmp_path):
    work = tmp_path / "work"
    _write_png(work / "report" / "proof.png")
    evidence_manifest = {
        "selected_images": [{"evidence_id": "ev001", "relative_path": "evidence/images/ev001.png"}],
        "study": {"calibrated": True},
    }
    summary = _summary_with_trustless_proof()
    registry = ArtifactRegistry(work)
    registry.add_visual(
        kind="proof_image",
        path=work / "report" / "proof.png",
        anatomy="spine",
        marker_type="pinpoint",
        evidence_ids=[],
    )

    qa = ArtifactQaGate(work, evidence_manifest=evidence_manifest).run(registry, summary)

    finding = summary["patient"]["findings"][0]
    assert qa["status"] == "limited"
    assert finding["figure"] == ""
    assert finding["trust"]["proof_image"] is False
    assert finding["trust"]["body_map_marker"] is False
    assert finding["location_trusted"] is False


def test_report_contract_contains_run1_fields_and_run2_assets_after_reload():
    job = _make_completed_job()
    mika_app._run_artifact_qa(job)
    mika_app._persist_report(job)

    report_path = mika_app.DATA_DIR / job.job_id / "report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["assets"].pop("evidence", None)
    payload["assets"].pop("artifacts", None)
    payload["assets"].pop("artifact_qa", None)
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    mika_app.JOBS.clear()

    report = asyncio.run(mika_app.get_report(job.job_id))

    for key in ("study", "patient", "clinician", "findings", "confidence", "assets", "verification"):
        assert key in report
    assert report["pdf_available"] is True
    assert report["clinical_pdf_available"] is True
    assert report["assets"]["pdf"] == {"patient_available": True, "clinical_available": True}
    assert report["assets"]["evidence"]["study"]["input_type"] == "dicom"
    assert "artifacts" in report["assets"]
    assert "artifact_qa" in report["assets"]
    assert report["findings"][0]["trust"]["proof_image"] is False


def test_api_status_and_report_return_json_with_no_store_headers():
    job = _make_completed_job("1234abcd")
    mika_app._persist_report(job)
    mika_app.JOBS.clear()

    async def request_paths():
        transport = httpx.ASGITransport(app=mika_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get(f"/api/status/{job.job_id}")
            report = await client.get(f"/api/report/{job.job_id}")
            return status, report

    status, report = asyncio.run(request_paths())

    assert status.status_code == 200
    assert report.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert report.headers["Cache-Control"] == "no-store"
    assert status.headers["content-type"].startswith("application/json")
    assert report.headers["content-type"].startswith("application/json")
    assert report.json()["job_id"] == job.job_id

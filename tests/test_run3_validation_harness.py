from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import app as mika_app
from services.agent_runner import AgentRunner, _normalize_summary
from services.artifacts import ArtifactQaGate, ArtifactRegistry
from validation.run_local_evidence_validation import ValidationCase, _validate_manifest, discover_cases


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mika_app, "DATA_DIR", tmp_path)
    mika_app.JOBS.clear()
    yield tmp_path
    mika_app.JOBS.clear()


def _write_jpg(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((48, 48), value, dtype=np.uint8)
    arr[10:30, 12:36] = min(255, value + 40)
    Image.fromarray(arr).save(path)


def _write_dicom(path: Path, series_uid: str, instance_number: int) -> None:
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
    ds.PatientID = "SYNTHETIC"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = series_uid
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = "OT"
    ds.SeriesDescription = f"Converted image {instance_number}"
    ds.InstanceNumber = instance_number
    ds.Rows = 24
    ds.Columns = 24
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = (np.full((24, 24), instance_number, dtype=np.uint16)).tobytes()
    ds.save_as(str(path))


def test_xray_validation_case_matches_real_dicom_dx_shape(tmp_path):
    (tmp_path / "XRAY").mkdir()

    cases = {case.case_id: case for case in discover_cases(tmp_path)}

    xray = cases["xray_folder"]
    assert xray.label == "DICOM lumbar X-ray folder"
    assert xray.expected_input_type == "dicom"
    assert xray.expected_modality == "DX"
    assert xray.expected_calibrated is True


def test_xray_dicom_dx_manifest_passes_validation_expectations():
    case = ValidationCase(
        case_id="xray_folder",
        label="DICOM lumbar X-ray folder",
        source_path=Path("unused"),
        reference_path=None,
        expected_input_type="dicom",
        expected_modality="DX",
        expected_calibrated=True,
        expected_concepts=["xray_modality", "radiograph_dicom_calibration"],
    )
    manifest = {
        "study": {
            "input_type": "dicom",
            "modality": "DX",
            "calibrated": True,
            "series_count": 1,
            "image_count": 4,
            "localizer_excluded_count": 0,
        },
        "series": [
            {
                "series_id": "s001_lspine",
                "modality": "DX",
                "plane": "",
                "slice_count": 4,
                "pixel_spacing": [0.1, 0.1],
                "is_localizer": False,
                "representative_slice_paths": ["evidence/images/ev001.png"],
            }
        ],
        "selected_images": [{"evidence_id": "ev001", "is_localizer": False}],
        "limitations": [],
    }

    result = _validate_manifest(case, manifest)

    assert result["flags"] == []
    assert result["study"]["input_type"] == "dicom"
    assert result["study"]["modality"] == "DX"
    assert result["study"]["calibrated"] is True


def test_prepare_evidence_pack_uses_original_upload_for_image_exports(tmp_path):
    job_id = "a1b2c3d4"
    job_dir = mika_app.DATA_DIR / job_id
    upload_dir = job_dir / "upload"
    dicom_dir = job_dir / "dicom"
    for idx in range(1, 5):
        _write_jpg(upload_dir / f"slice_{idx:03d}.jpg", 80 + idx)
        _write_dicom(dicom_dir / f"converted_{idx:03d}.dcm", f"1.2.826.0.1.3680043.8.498.{idx}", idx)
    job = mika_app.AnalysisJob(job_id=job_id, dicom_dir=str(dicom_dir))
    job.measurements = {
        "detected_anatomy": "unknown",
        "anatomy_subregion": "",
        "modality": "MR",
    }

    manifest = mika_app._prepare_evidence_pack(job, str(job_dir))

    assert manifest["study"]["input_type"] == "image_export"
    assert manifest["study"]["modality"] == "MR"
    assert manifest["study"]["calibrated"] is False
    assert manifest["study"]["series_count"] == 1
    assert manifest["study"]["image_count"] == 4
    assert len(manifest["selected_images"]) == 4
    assert {item["series_id"] for item in manifest["selected_images"]} == {"s001_image_export"}


def test_artifact_gate_inherits_figure_evidence_for_patient_proof(tmp_path):
    work = tmp_path / "work"
    _write_jpg(work / "report" / "proof.png", 120)
    evidence_manifest = {
        "selected_images": [{"evidence_id": "ev001", "relative_path": "evidence/images/ev001.png"}],
        "study": {"calibrated": True},
    }
    summary = {
        "findings": [
            {
                "text": "L5-S1 finding.",
                "tier": "B",
                "figure": "proof",
                "evidence_refs": ["ev001"],
                "level": "L5-S1",
            }
        ],
        "patient": {
            "findings": [
                {
                    "plain": "Plain-language finding",
                    "certainty": "Likely",
                    "figure": "proof",
                    "caption": "Plain-language caption",
                }
            ]
        },
    }
    registry = ArtifactRegistry(work)
    registry.add_visual(
        kind="proof_image",
        path=work / "report" / "proof.png",
        anatomy="spine",
        marker_type="pinpoint",
        evidence_ids=["ev001"],
    )

    qa = ArtifactQaGate(work, evidence_manifest=evidence_manifest).run(registry, summary)

    patient_finding = summary["patient"]["findings"][0]
    assert qa["status"] == "passed"
    assert patient_finding["figure"] == "proof"
    assert patient_finding["evidence_refs"] == ["ev001"]
    assert patient_finding["trust"]["valid_evidence"] is True
    assert patient_finding["trust"]["proof_image"] is True
    assert patient_finding["trust"]["body_map_marker"] is False
    assert patient_finding["location_trusted"] is False


def test_spine_agent_prompt_requires_post_surgical_contrast_checklist(tmp_path):
    prompt = AgentRunner(timeout_s=1)._build_prompt(
        tmp_path / "study",
        tmp_path / "work" / "report",
        anatomy="spine",
        modality="MR",
    )

    assert "hemilaminectomy" in prompt
    assert "epidural fibrosis/scar" in prompt
    assert "residual or recurrent disc" in prompt
    assert "descending S1/L5 nerve root" in prompt
    assert "pre/post fat-saturated images" in prompt
    assert "sparse representative samples alone" in prompt
    assert "full axial T1/T2 and matched pre/post contrast stacks" in prompt


def test_patient_summary_uses_plain_export_limitations_without_changing_clinician_terms():
    summary = {
        "findings": [
            {
                "text": "Technical finding.",
                "tier": "C",
                "calibration_basis": "DICOM PixelSpacing absent; uncalibrated image export.",
            }
        ],
        "patient": {
            "bottom_line": "This was read from uncalibrated picture exports.",
            "confidence": {
                "label": "Moderate",
                "note": "Measurements are not calibrated.",
            },
            "findings": [
                {
                    "plain": "The exported images show a likely disc bulge.",
                    "certainty": "Likely",
                    "caption": "Uncalibrated image export; no exact measurement.",
                }
            ],
            "what_it_means": ["These were picture exports without calibration metadata."],
            "disclaimer": "This is an AI-generated analysis of an image-export MRI. Measurements are not calibrated.",
        },
    }

    normalized = _normalize_summary(summary)
    patient_text = str(normalized["patient"]).lower()

    assert "uncalibrated" not in patient_text
    assert "calibration" not in patient_text
    assert "dicom" not in patient_text
    assert "pixelspacing" not in patient_text
    assert "scale information" in patient_text
    assert normalized["findings"][0]["calibration_basis"] == "DICOM PixelSpacing absent; uncalibrated image export."

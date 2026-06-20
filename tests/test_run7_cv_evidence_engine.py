from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from core.anatomy_modules.base import (
    EvidenceCandidate,
    candidate_allows_body_marker,
    candidate_allows_pinpoint_marker,
    candidate_allows_proof_overlay,
    candidate_verifier_contract,
)
from core.anatomy_modules.lumbar_spine import LumbarSpineEvidenceModule
from core.study_graph import StudyGraphBuilder


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
    pixel_spacing: bool = True,
    rows: int = 64,
    cols: int = 80,
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
    ds.PatientID = "SYNTH"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.Modality = "MR"
    ds.SeriesDescription = series_description
    ds.ProtocolName = series_description
    ds.SeriesNumber = series_number
    ds.InstanceNumber = instance_number
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.WindowCenter = 100
    ds.WindowWidth = 200
    ds.SliceThickness = 4.0
    ds.ImageOrientationPatient = image_orientation
    ds.ImagePositionPatient = image_position
    ds.SliceLocation = float(image_position[2])
    ds.AcquisitionTime = f"120{instance_number:03d}"
    if pixel_spacing:
        ds.PixelSpacing = [0.7, 0.7]
    arr = (np.arange(rows * cols, dtype=np.uint16).reshape(rows, cols) + instance_number) % 4096
    ds.PixelData = arr.tobytes()
    ds.save_as(str(path))


def _make_lumbar_contrast_study(tmp_path: Path, *, post_orientation=None, pixel_spacing: bool = True) -> Path:
    pydicom = pytest.importorskip("pydicom")
    study = tmp_path / "lumbar"
    study_uid = pydicom.uid.generate_uid()
    sag_uid = pydicom.uid.generate_uid()
    pre_uid = pydicom.uid.generate_uid()
    post_uid = pydicom.uid.generate_uid()
    sagittal_orientation = [0, 1, 0, 0, 0, 1]
    axial_orientation = [1, 0, 0, 0, 1, 0]
    post_orientation = post_orientation or axial_orientation

    for i in range(1, 7):
        _write_dicom(
            study / "sag" / f"{i:03d}.dcm",
            series_uid=sag_uid,
            study_uid=study_uid,
            series_number=1,
            series_description="Sag T2",
            instance_number=i,
            image_orientation=sagittal_orientation,
            image_position=[float(i), 0.0, 50.0],
            pixel_spacing=pixel_spacing,
        )
    for i in range(1, 21):
        z = 120.0 - (i - 1) * 4.0
        _write_dicom(
            study / "pre" / f"{i:03d}.dcm",
            series_uid=pre_uid,
            study_uid=study_uid,
            series_number=2,
            series_description="t1_vibe_fs_tra",
            instance_number=i,
            image_orientation=axial_orientation,
            image_position=[0.0, 0.0, z],
            pixel_spacing=pixel_spacing,
        )
        _write_dicom(
            study / "post" / f"{i:03d}.dcm",
            series_uid=post_uid,
            study_uid=study_uid,
            series_number=3,
            series_description="t1_vibe_fs_tra-CONT",
            instance_number=i,
            image_orientation=post_orientation,
            image_position=[0.0, 0.0, z],
            pixel_spacing=pixel_spacing,
        )
    return study


def test_study_graph_dicom_metadata_extraction(tmp_path):
    study = _make_lumbar_contrast_study(tmp_path)

    graph = StudyGraphBuilder(study).build()

    assert graph.source_type == "dicom"
    assert graph.modality == "MR"
    assert graph.image_count == 46
    post = next(s for s in graph.series if s.description.endswith("CONT"))
    assert post.plane == "axial"
    assert post.sequence == "t1"
    assert post.contrast_phase == "post_contrast"
    assert post.pixel_spacing == (0.7, 0.7)
    assert post.slices[0].image_position_patient == (0.0, 0.0, 120.0)
    assert post.slices[0].image_orientation_patient == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    assert post.slices[0].acquisition_time


def test_plane_sequence_and_contrast_classification(tmp_path):
    study = _make_lumbar_contrast_study(tmp_path)

    graph = StudyGraphBuilder(study).build()
    by_desc = {s.description: s for s in graph.series}

    assert by_desc["Sag T2"].plane == "sagittal"
    assert by_desc["Sag T2"].sequence == "t2"
    assert by_desc["t1_vibe_fs_tra"].contrast_phase == "pre_contrast"
    assert by_desc["t1_vibe_fs_tra-CONT"].contrast_phase == "post_contrast"


def test_left_right_orientation_handling_controls_roi_side(tmp_path):
    study = _make_lumbar_contrast_study(tmp_path)
    graph = StudyGraphBuilder(study).build()
    module = LumbarSpineEvidenceModule()
    post = next(s for s in graph.series if s.description.endswith("CONT"))

    left_roi = module.lateral_recess_roi(post, "left")
    right_roi = module.lateral_recess_roi(post, "right")

    assert left_roi["x"] > right_roi["x"]

    flipped_study = _make_lumbar_contrast_study(tmp_path / "flipped", post_orientation=[-1, 0, 0, 0, 1, 0])
    flipped = StudyGraphBuilder(flipped_study).build()
    flipped_post = next(s for s in flipped.series if s.description.endswith("CONT"))
    flipped_left = module.lateral_recess_roi(flipped_post, "left")
    flipped_right = module.lateral_recess_roi(flipped_post, "right")

    assert flipped_left["x"] < flipped_right["x"]


def test_sagittal_to_axial_mapping_surfaces_l5_s1_range(tmp_path):
    study = _make_lumbar_contrast_study(tmp_path)
    graph = StudyGraphBuilder(study).build()
    module = LumbarSpineEvidenceModule()
    sag = next(s for s in graph.series if s.plane == "sagittal")
    post = next(s for s in graph.series if s.description.endswith("CONT"))

    ranges = module.map_sagittal_disc_levels_to_axial_ranges(sag, post)

    assert set(ranges) == {"L1-L2", "L2-L3", "L3-L4", "L4-L5", "L5-S1"}
    assert ranges["L5-S1"].slice_ids
    assert ranges["L5-S1"].coordinate_range[1] < ranges["L1-L2"].coordinate_range[0]
    assert ranges["L5-S1"].confidence < 0.80


def test_registration_qc_pass_and_fail(tmp_path):
    good_study = _make_lumbar_contrast_study(tmp_path)
    graph = StudyGraphBuilder(good_study).build()
    module = LumbarSpineEvidenceModule()
    pre = next(s for s in graph.series if s.description == "t1_vibe_fs_tra")
    post = next(s for s in graph.series if s.description.endswith("CONT"))
    target = [sl.slice_id for sl in post.slices[-4:]]

    passed = module.registration_qc(pre, post, target)
    assert passed.passed is True
    assert passed.confidence >= 0.90

    bad_study = _make_lumbar_contrast_study(tmp_path / "bad", post_orientation=[1, 0, 0, 0, -1, 0])
    bad_graph = StudyGraphBuilder(bad_study).build()
    bad_pre = next(s for s in bad_graph.series if s.description == "t1_vibe_fs_tra")
    bad_post = next(s for s in bad_graph.series if s.description.endswith("CONT"))
    failed = module.registration_qc(bad_pre, bad_post, [sl.slice_id for sl in bad_post.slices[-4:]])

    assert failed.passed is False
    assert any("ImageOrientationPatient" in note for note in failed.limitations)


def test_candidate_contract_serialization_and_no_cv_confirmation(tmp_path):
    study = _make_lumbar_contrast_study(tmp_path)
    graph = StudyGraphBuilder(study).build()

    candidate_set = LumbarSpineEvidenceModule().analyze(graph)
    payload = candidate_set.to_dict()
    candidate = payload["candidates"][0]

    for key in (
        "candidate_id",
        "anatomy",
        "level",
        "side",
        "series_ids",
        "slice_ids",
        "candidate_type",
        "roi",
        "calibration_state",
        "geometry_confidence",
        "registration_confidence",
        "limitations",
        "evidence_refs",
    ):
        assert key in candidate
    assert candidate["level"] == "L5-S1"
    assert candidate["side"] == "left"
    assert candidate["requires_verifier"] is True
    assert candidate["cv_claim_scope"] == "localization_only"
    assert "confirmed" not in str(payload).lower()
    assert "supported" in payload["verifier_contract"]["allowed_statuses"]


def test_uncalibrated_jpg_study_is_gated_from_precise_geometry(tmp_path):
    study = tmp_path / "jpg_export"
    study.mkdir()
    Image.fromarray(np.full((64, 64), 120, dtype=np.uint8)).save(study / "slice.jpg")

    graph = StudyGraphBuilder(study).build()
    candidate_set = LumbarSpineEvidenceModule().analyze(graph)

    assert graph.source_type == "image_export"
    assert graph.calibrated is False
    assert not candidate_set.candidates
    assert "image exports" in " ".join(candidate_set.limitations).lower()


def test_no_cv_only_confirmed_finding_or_forced_verifier_status():
    contract = candidate_verifier_contract()

    assert contract["allowed_statuses"] == ["supported", "not_supported", "cannot_assess", "localization_wrong"]
    assert "confirmed" not in str(contract).lower()
    assert any("CV localization" in rule for rule in contract["rules"])


def test_no_marker_or_overlay_when_geometry_or_registration_confidence_is_low():
    candidate = EvidenceCandidate(
        candidate_id="low",
        anatomy="lumbar_spine",
        level="L5-S1",
        side="left",
        series_ids=["s1", "s2"],
        slice_ids=["sl1", "sl2"],
        candidate_type="pre_post_contrast_lateral_recess_roi",
        roi={"unit": "normalized_image_fraction"},
        calibration_state="calibrated",
        geometry_confidence=0.70,
        registration_confidence=0.94,
        limitations=[],
        evidence_refs=["s1:sl1", "s2:sl2"],
    )

    assert candidate_allows_body_marker(candidate) is False
    assert candidate_allows_proof_overlay(candidate) is False
    assert candidate_allows_pinpoint_marker(candidate) is False

from pathlib import Path

import numpy as np
from PIL import Image

from core.study_graph import StudyGraph, StudySeries, StudySlice
from validation.l5s1_evidence_diagnostics import (
    assert_phi_safe_payload,
    classify_reference_claim_category,
    decide_l5s1_evidence_adequacy,
    plausible_l5s1_roi_grid,
    run_cross_case_harness,
    sequence_adequacy_listing,
    should_trigger_re_evaluation,
    summarize_roi_sweep_results,
)


def _series(
    series_id: str,
    description: str,
    *,
    contrast_phase: str,
    slice_count: int,
    series_number: int,
) -> StudySeries:
    orientation = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    series = StudySeries(
        series_id=series_id,
        description=description,
        modality="MR",
        plane="axial",
        sequence="t1",
        contrast_phase=contrast_phase,
        source_type="dicom",
        protocol_name=description,
        series_number=series_number,
        pixel_spacing=(0.7, 0.7),
        image_orientation_patient=orientation,
        rows=64,
        columns=80,
        slice_thickness=4.0,
    )
    for index in range(1, slice_count + 1):
        z = 120.0 - (index - 1) * 4.0
        series.slices.append(StudySlice(
            slice_id=f"{series_id}_sl{index:03d}",
            series_id=series_id,
            modality="MR",
            plane="axial",
            sequence="t1",
            contrast_phase=contrast_phase,
            source_type="dicom",
            instance_number=index,
            pixel_spacing=(0.7, 0.7),
            image_position_patient=(0.0, 0.0, z),
            image_orientation_patient=orientation,
            slice_location=z,
            rows=64,
            columns=80,
        ))
    return series


def _graph_with_broad_and_dedicated_pairs() -> StudyGraph:
    return StudyGraph(
        study_id="study_synthetic",
        source_type="dicom",
        modality="MR",
        series=[
            _series("s010_t1_vibe_fs_tra", "t1_vibe_fs_tra", contrast_phase="pre_contrast", slice_count=64, series_number=10),
            _series("s011_t1_vibe_fs_tra-cont", "t1_vibe_fs_tra-CONT", contrast_phase="post_contrast", slice_count=64, series_number=11),
            _series("s020_t1_tse_tra_l_spine", "t1_tse_tra L SPINE", contrast_phase="pre_contrast", slice_count=31, series_number=20),
            _series("s021_t1_tse_tra_l_spine_cont", "t1_tse_tra L SPINE CONT", contrast_phase="post_contrast", slice_count=31, series_number=21),
        ],
    )


def test_reference_target_category_schema():
    assert classify_reference_claim_category("At L5-S1, left S1 nerve root involvement is described.") == "nerve_root_involvement"
    assert classify_reference_claim_category("L5-S1 expected postoperative scar tissue is present.") == "expected_post_op_scar"
    assert classify_reference_claim_category("No L5-S1 target is mentioned.") == "unclear"
    assert classify_reference_claim_category("Only L4-L5 is mentioned.") == "none_stated"


def test_sequence_adequacy_listing_is_deterministic_phi_safe_and_triggers_rerun():
    graph = _graph_with_broad_and_dedicated_pairs()
    candidate = {"series_ids": ["s010_t1_vibe_fs_tra", "s011_t1_vibe_fs_tra-cont"]}

    first = sequence_adequacy_listing(graph, candidate)
    second = sequence_adequacy_listing(graph, candidate)

    assert first == second
    assert first["better_suited_sequence_found"] is True
    assert should_trigger_re_evaluation(first) is True
    assert first["better_suited_pairs"][0]["post_series_id"] == "s021_t1_tse_tra_l_spine_cont"
    assert_phi_safe_payload(first)


def test_roi_sweep_grid_is_deterministic_and_bounded():
    first = plausible_l5s1_roi_grid()
    second = plausible_l5s1_roi_grid()

    assert first == second
    assert any(row["label"] == "run12_narrowed_roi" for row in first)
    assert all(0.50 <= row["center_x"] <= 0.66 for row in first)
    assert all(0.44 <= row["center_y"] <= 0.56 for row in first)
    assert all(0.06 <= row["width"] <= 0.16 for row in first)
    assert all(0.14 <= row["height"] <= 0.26 for row in first)


def test_sweep_result_cannot_auto_promote_to_supported():
    summary = summarize_roi_sweep_results([
        {"verdict": "focal_candidate"},
        {"verdict": "focal_candidate"},
    ])

    assert summary["sweep_interpretation"] == "non_focal_broad_signal"
    assert summary["any_focal_candidate"] is False
    assert summary["auto_promote_allowed"] is False


def test_cross_case_harness_records_per_case_metrics_phi_safe(tmp_path):
    case_dir = tmp_path / "case1"
    case_dir.mkdir()
    Image.fromarray(np.full((32, 32), 100, dtype=np.uint8)).save(case_dir / "slice.jpg")

    result = run_cross_case_harness([case_dir])

    assert result["case_count"] == 1
    assert result["rows"][0]["case_id"] == "case_01"
    assert result["rows"][0]["candidate_count"] == 0
    assert_phi_safe_payload(result)


def test_phi_safe_guard_blocks_paths_and_artifacts():
    unsafe = {"raw_path": r"C:\Users\Someone\scan.png"}

    try:
        assert_phi_safe_payload(unsafe)
    except ValueError:
        pass
    else:
        raise AssertionError("PHI-safe guard should reject raw local paths and image artifacts")


def test_decision_synthesis_classifies_correct_cannot_assess_without_focal_signal():
    decision = decide_l5s1_evidence_adequacy(
        reference_category="nerve_root_involvement",
        sequence_report={"better_suited_sequence_found": False},
        roi_sweep={"summary": {"any_focal_candidate": False}},
        cross_case={"generalized": False},
    )

    assert decision["classification"] == "CORRECT cannot_assess"
    assert decision["true_miss"] is False
    assert decision["no_synthesis"] is True

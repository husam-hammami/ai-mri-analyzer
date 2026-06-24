"""Phase 2 — measure disc-level placement vs SPIDER mask ground truth.

The mask never drops a disc, so naming sacrum-up from the mask is the reference the
read's sacrum-up count is checked against. compare_spine_levels turns "looks plausible"
into "level-match 0/6, off-by-one DETECTED".
"""
import pytest

from validation.annotation_overlap import (
    compare_spine_levels,
    spider_level_ground_truth,
)

# GT: 7 discs, caudal->cranial, inferior-positive si_mm (larger = more inferior).
_GT_SI = {
    "L5-S1": 130, "L4-L5": 110, "L3-L4": 90, "L2-L3": 70,
    "L1-L2": 50, "T12-L1": 30, "T11-T12": 10,
}


def _make_gt():
    order = list(_GT_SI.keys())  # already caudal->cranial
    levels = {
        name: {"label": 250 + i, "si_mm": si, "si_extent_mm": 8.0, "voxel_count": 100}
        for i, (name, si) in enumerate(_GT_SI.items())
    }
    return {
        "levels": levels,
        "discs_caudal_to_cranial": order,
        "median_disc_spacing_mm": 20.0,
        "sacrum_si_mm": 150.0,
    }


def _pts(mapping):
    return {name: [0, si] for name, si in mapping.items()}  # [AP, SI]


def test_off_by_one_detected_on_the_known_case():
    # The read missed the faint lowest disc and named everything one level too low:
    # its "L5-S1" sits at the mask's L4-L5, the true L5-S1 was never marked.
    gt = _make_gt()
    shifted = _pts({
        "L5-S1": 110, "L4-L5": 90, "L3-L4": 70,
        "L2-L3": 50, "L1-L2": 30, "T12-L1": 10,
    })
    res = compare_spine_levels(gt, shifted, calib={"si_scale": 1.0, "si_offset": 0.0})

    assert res["registration"] == "absolute"
    assert res["off_by_one_detected"] is True
    assert res["missed_caudal_disc"] is True
    assert res["level_match_rate"] == 0.0
    assert res["per_level"]["L5-S1"]["matched_level"] == "L4-L5"


def test_correct_levels_match_and_no_off_by_one():
    gt = _make_gt()
    correct = _pts({
        "L5-S1": 130, "L4-L5": 110, "L3-L4": 90,
        "L2-L3": 70, "L1-L2": 50, "T12-L1": 30,
    })
    res = compare_spine_levels(gt, correct, calib={"si_scale": 1.0, "si_offset": 0.0})

    assert res["off_by_one_detected"] is False
    assert res["level_match_rate"] == 1.0
    assert res["mean_mm_offset"] == 0.0


def test_approximate_registration_is_honest_about_off_by_one():
    # Without an absolute (sacrum-anchored) calibration the offset can't be recovered,
    # so the verdict is NOT asserted rather than silently wrong.
    gt = _make_gt()
    shifted = _pts({
        "L5-S1": 110, "L4-L5": 90, "L3-L4": 70,
        "L2-L3": 50, "L1-L2": 30, "T12-L1": 10,
    })
    res = compare_spine_levels(gt, shifted, calib=None)

    assert res["registration"] == "approximate"
    assert res["off_by_one_detected"] is None


def test_empty_inputs_do_not_crash():
    assert compare_spine_levels({}, {})["off_by_one_detected"] is None
    assert compare_spine_levels(_make_gt(), {})["off_by_one_detected"] is None


def test_spider_ground_truth_parses_and_names_sacrum_up():
    sitk = pytest.importorskip("SimpleITK")
    np = pytest.importorskip("numpy")

    vol = np.zeros((80, 40, 40), dtype=np.int16)
    # Default sitk orientation: si_mm = -z, so the smallest z is most inferior (caudal).
    vol[8:12, 15:25, 15:25] = 8       # sacrum vertebra (low int, most caudal)
    vol[18:22, 15:25, 15:25] = 255    # L5-S1 disc (high label, just above sacrum)
    vol[38:42, 15:25, 15:25] = 253    # L4-L5
    vol[58:62, 15:25, 15:25] = 252    # L3-L4
    vol[58:62, 15:25, 15:25] = 252
    img = sitk.GetImageFromArray(vol)

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "mask.mha"
        sitk.WriteImage(img, str(mp))
        gt = spider_level_ground_truth(mp)

    assert gt["n_discs"] == 3
    assert gt["discs_caudal_to_cranial"] == ["L5-S1", "L4-L5", "L3-L4"]
    assert gt["levels"]["L5-S1"]["label"] == 255
    assert gt["levels"]["L4-L5"]["label"] == 253
    assert gt["levels"]["L3-L4"]["label"] == 252
    # the sacrum vertebra sits caudal to (more inferior than) the L5-S1 disc
    assert gt["sacrum_si_mm"] is not None
    assert gt["sacrum_si_mm"] > gt["levels"]["L5-S1"]["si_mm"]

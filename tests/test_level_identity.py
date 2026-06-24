"""Phase 3 — structure-identity verification kills the off-by-one.

Intensity verification only checks a tip sits on a disc-like pixel, never that it sits on
the CLAIMED level. verify_level_identity adds a sacrum-anchor check, and identify_levels
re-searches the band below the lowest disc to recover the faint L5-S1 the strict pass drops.
Rows increase downward, so the lowest disc is the largest row and the sacrum is below it.
"""
import numpy as np

from core.dicom_engine import DICOMEngine


def _engine_with(level_map, body_map):
    eng = DICOMEngine.__new__(DICOMEngine)  # bypass __init__; method reads only these maps
    eng.level_map = dict(level_map)
    eng.body_map = dict(body_map)
    return eng


def test_healthy_map_anchors_and_passes():
    eng = _engine_with(
        {"L5-S1": 500, "L4-L5": 440, "L3-L4": 380, "L2-L3": 320, "L1-L2": 260},
        {"S1": 540},  # gap 40 < 1.3 * 60 spacing
    )
    res = eng.verify_level_identity()
    assert res["anchored"] is True
    assert res["consecutive"] is True
    assert res["count_ok"] is True
    assert res["ok"] is True
    assert res["identity_confidence"] == "high"


def test_sacrum_gap_catches_a_dropped_lowest_disc():
    # Same disc rows, but the sacrum sits ~2 spacings below the lowest counted disc:
    # a faint L5-S1 was missed and the count shifted up by one.
    eng = _engine_with(
        {"L5-S1": 500, "L4-L5": 440, "L3-L4": 380, "L2-L3": 320, "L1-L2": 260},
        {"S1": 620},  # gap 120 ~= 2 * 60 spacing
    )
    res = eng.verify_level_identity()
    assert res["anchored"] is False
    assert res["ok"] is False
    assert res["identity_confidence"] == "low"
    assert any("faint L5-S1" in r for r in res["reasons"])


def test_irregular_spacing_flags_not_consecutive():
    eng = _engine_with(
        {"L5-S1": 500, "L4-L5": 460, "L3-L4": 440, "L2-L3": 280, "L1-L2": 260},
        {"S1": 540},
    )
    res = eng.verify_level_identity()
    assert res["consecutive"] is False
    assert any("irregular" in r for r in res["reasons"])


def test_too_few_levels_fails_count():
    eng = _engine_with({"L5-S1": 500, "L4-L5": 440}, {"S1": 540})
    res = eng.verify_level_identity()
    assert res["count_ok"] is False


def _grad_with_peaks(length, centers, height=5.0, sigma=4.0):
    x = np.arange(length)
    grad = np.zeros(length, dtype=float)
    for c in centers:
        grad += height * np.exp(-((x - c) ** 2) / (2 * sigma ** 2))
    return grad


def test_recover_faint_lowest_disc_when_gap_is_wide():
    eng = DICOMEngine.__new__(DICOMEngine)
    # discs at 100/160/220 (spacing 60); sacrum far below at 400 leaves room for one disc.
    # low-prominence gradient peaks at 250/310 pair into a center at 280 (= 220 + 60).
    grad = _grad_with_peaks(420, [250, 310])
    centers, recovered = eng._recover_faint_lowest_disc(grad, [100, 160, 220], 400)
    assert recovered is True
    assert any(abs(c - 280) <= 2 for c in centers)


def test_no_recovery_when_lowest_disc_abuts_sacrum():
    eng = DICOMEngine.__new__(DICOMEngine)
    grad = _grad_with_peaks(300, [250])
    centers, recovered = eng._recover_faint_lowest_disc(grad, [100, 160, 220], 250)
    assert recovered is False
    assert centers == [100, 160, 220]

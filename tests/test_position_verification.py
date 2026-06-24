"""Phase 4 — universal verify-or-degrade position gate.

A pinpoint only when an INDEPENDENT signal confirms the position; otherwise an honest
region band at lower certainty. Never a confident wrong pinpoint.
"""
from services.position_verification import verify_annotation_position


def test_no_localizer_is_region_band_not_pinpoint():
    res = verify_annotation_position({"level": "L5-S1", "col": 100, "row": 200})
    assert res["decision"] == "region_band"
    assert res["certainty"] == "moderate"
    assert res["agreement"] == "unavailable"


def test_frame_agreement_allows_pinpoint():
    loc = {"source": "L5-S1 cross-check", "allows_pinpoint": True, "agrees": True}
    res = verify_annotation_position({"level": "L4-L5"}, independent_localizer=loc)
    assert res["decision"] == "pinpoint"
    assert res["certainty"] == "high"
    assert res["agreement"] == "agree"


def test_frame_disagreement_vetoes_pinpoint():
    loc = {"source": "L5-S1 cross-check", "allows_pinpoint": True, "agrees": False}
    res = verify_annotation_position({"level": "L5-S1"}, independent_localizer=loc)
    assert res["decision"] == "region_band"
    assert res["agreement"] == "disagree"


def test_anchor_failure_forces_region_band_even_with_localizer():
    loc = {"source": "L5-S1 cross-check", "allows_pinpoint": True, "agrees": True}
    res = verify_annotation_position(
        {"level": "L5-S1"}, independent_localizer=loc, anchor_check={"anchored": False}
    )
    assert res["decision"] == "region_band"
    assert res["agreement"] == "anchor_failed"


def test_untrusted_localizer_cannot_pinpoint():
    loc = {"source": "cv_candidate", "allows_pinpoint": False, "agrees": True}
    res = verify_annotation_position({"level": "L5-S1"}, independent_localizer=loc)
    assert res["decision"] == "region_band"
    assert res["agreement"] == "localizer_untrusted"


def test_point_based_agreement_within_tolerance():
    loc = {"source": "cv", "allows_pinpoint": True, "level": "L5-S1", "point": [100, 200]}
    res = verify_annotation_position(
        {"level": "L5-S1", "point": [104, 203]}, independent_localizer=loc, tolerance_px=12
    )
    assert res["decision"] == "pinpoint"


def test_point_based_level_mismatch_is_disagree():
    loc = {"source": "cv", "allows_pinpoint": True, "level": "L4-L5", "point": [100, 200]}
    res = verify_annotation_position(
        {"level": "L5-S1", "point": [100, 200]}, independent_localizer=loc
    )
    assert res["decision"] == "region_band"
    assert res["agreement"] == "disagree"


def test_point_based_distance_beyond_tolerance_is_disagree():
    loc = {"source": "cv", "allows_pinpoint": True, "level": "L5-S1", "point": [100, 200]}
    res = verify_annotation_position(
        {"level": "L5-S1", "point": [100, 240]}, independent_localizer=loc, tolerance_px=12
    )
    assert res["decision"] == "region_band"
    assert res["agreement"] == "disagree"

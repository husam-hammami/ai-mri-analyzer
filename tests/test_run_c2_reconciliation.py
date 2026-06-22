"""Run C.2 reconciliation moat: prior studies, temporal deltas, op-vs-read wiring."""

from services.op_note_recon import extract_read_surgical_level_side
from services.reconciliation import build_structured_change_over_time, merge_change_over_time


def test_analyze_request_accepts_prior_studies():
    from backend import app as mika_app

    req = mika_app.AnalyzeRequest(
        job_id="deadbeef",
        mode="agent",
        prior_studies=["abc12345", r"C:\local\prior-report-dir"],
    )

    assert req.prior_studies == ["abc12345", r"C:\local\prior-report-dir"]


def test_temporal_delta_extracts_resolved_new_and_progressed():
    prior = """
    Comparison study:
    Left L5-S1 lateral recess was occupied by disc/scar tissue.
    No left L4-L5 nerve root compression.
    Right L3-L4 foraminal stenosis.
    """
    current = {
        "findings": [
            {"text": "The left L5-S1 lateral recess is decompressed after surgery."},
            {"text": "New left L4-L5 disc compression contacts the nerve root."},
            {"text": "Right L3-L4 foraminal stenosis has progressed and is severe."},
        ],
        "patient": {},
    }

    change = build_structured_change_over_time(current_summary=current, prior_reports=prior)
    statuses = {(item["level"], item["side"], item["status"]) for item in change["items"]}

    assert ("L5-S1", "left", "resolved") in statuses
    assert ("L4-L5", "left", "new") in statuses
    assert ("L3-L4", "right", "progressed") in statuses
    assert all(item["prior_text"] and item["current_text"] for item in change["items"])


def test_merge_change_over_time_preserves_existing_points():
    summary = {"patient": {"change_over_time": {"points": ["Existing comparison point."]}}}
    change = {
        "items": [{"status": "resolved", "level": "L5-S1", "side": "left"}],
        "points": ["L5-S1 left: abnormal on prior comparison -> improved now (resolved)."],
        "source": "deterministic_temporal_delta",
    }

    merge_change_over_time(summary, change)

    cot = summary["patient"]["change_over_time"]
    assert cot["points"][0] == "Existing comparison point."
    assert cot["points"][1].startswith("L5-S1 left")
    assert cot["items"] == change["items"]
    assert summary["change_over_time"]["source"] == "deterministic_temporal_delta"


def test_extract_read_surgical_level_side_ignores_unrelated_levels():
    summary = {
        "findings": [
            {"text": "There is degenerative right L4-L5 foraminal stenosis."},
            {"text": "Postoperative left L5-S1 hemilaminectomy change with enhancing scar tissue."},
        ],
        "impression": ["Post-surgical change is centered at left L5-S1."],
    }

    levels, sides = extract_read_surgical_level_side(summary)

    assert levels == ["L5-S1"]
    assert sides == ["left"]

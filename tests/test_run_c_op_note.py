"""Run C — operative-note discrepancy engine (the reconciliation moat).

Covers the March-style catches (level contradiction, complications-vs-dural-tear) and verifies
no false positives on a clean note.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.op_note_recon import (  # noqa: E402
    parse_operative_note, detect_op_note_contradictions, reconcile_op_note_with_read,
    op_note_discrepancies,
)

COMPLICATION_NOTE = """
PROCEDURE: Revision microdiscectomy at L5-S1, left.
FINDINGS: A dural tear was identified on the dorsal aspect of the S1 nerve root; dural sealant applied.
COMPLICATIONS: None.
"""

LEVEL_TYPO_NOTE = """
PROCEDURE: L4-L5 discectomy, left side.
NARRATIVE: The L5-S1 disc space was entered and a large extruded fragment removed at the L5-S1 level.
COMPLICATIONS: None.
"""

CLEAN_NOTE = """
PROCEDURE: L5-S1 left microdiscectomy.
NARRATIVE: The L5-S1 disc space was decompressed on the left. Hemostasis achieved.
COMPLICATIONS: None.
"""


def test_parse_extracts_facts():
    f = parse_operative_note(COMPLICATION_NOTE)
    assert "discectomy" in f.procedures
    assert "L5-S1" in f.levels
    assert "left" in f.sides
    assert f.complications_stated_none is True
    assert "dural tear" in f.complications_found


def test_complications_none_vs_documented_tear_flagged():
    f = parse_operative_note(COMPLICATION_NOTE)
    d = detect_op_note_contradictions(f, COMPLICATION_NOTE)
    kinds = {x.kind for x in d}
    assert "complication_mismatch" in kinds


def test_procedure_line_level_vs_narrative_flagged():
    f = parse_operative_note(LEVEL_TYPO_NOTE)
    d = detect_op_note_contradictions(f, LEVEL_TYPO_NOTE)
    assert any(x.kind == "intra_op_level" for x in d)


def test_op_note_vs_read_level_and_side():
    f = parse_operative_note(CLEAN_NOTE)  # op note at L5-S1, left
    lvl = reconcile_op_note_with_read(f, read_levels=["L4-L5"], read_sides=["left"])
    assert any(x.kind == "op_vs_read_level" for x in lvl)
    side = reconcile_op_note_with_read(f, read_levels=["L5-S1"], read_sides=["right"])
    assert any(x.kind == "op_vs_read_side" for x in side)


def test_clean_note_no_false_positives():
    f = parse_operative_note(CLEAN_NOTE)
    intra = detect_op_note_contradictions(f, CLEAN_NOTE)
    assert intra == []
    matched = reconcile_op_note_with_read(f, read_levels=["L5-S1"], read_sides=["left"])
    assert matched == []


def test_public_entry_point():
    out = op_note_discrepancies(COMPLICATION_NOTE, read_levels=["L5-S1"], read_sides=["left"])
    assert out and any("complications as none" in s for s in out)
    assert op_note_discrepancies("", read_levels=["L5-S1"]) == []
    assert op_note_discrepancies(None) == []


def test_merge_into_summary():
    from services.op_note_recon import merge_into_summary
    summary = {"discrepancies": ["pre-existing item"]}
    merge_into_summary(summary, COMPLICATION_NOTE)
    assert any("complications as none" in s for s in summary["discrepancies"])
    assert "pre-existing item" in summary["discrepancies"]
    # dedup / idempotent
    n = len(summary["discrepancies"])
    merge_into_summary(summary, COMPLICATION_NOTE)
    assert len(summary["discrepancies"]) == n
    # safe on non-dict and empty note
    assert merge_into_summary(None, COMPLICATION_NOTE) is None
    s2 = {}
    merge_into_summary(s2, "")
    assert s2.get("discrepancies") in (None, [])

"""Phase 6 — annotation completeness self-audit (lite path).

Every reportable finding has a visual, a neutral "normal for comparison" reference is
present, and there are no orphan marks (a drawn mark with no matching finding).
"""
from types import SimpleNamespace

from core.dicom_engine import DICOMEngine


def _disc(level, grade):
    return SimpleNamespace(level=level, desiccation_grade=grade)


def _engine(measurements, audit):
    eng = DICOMEngine.__new__(DICOMEngine)
    eng.disc_measurements = measurements
    eng.annotation_audit = audit
    return eng


def test_complete_when_every_finding_marked_with_reference():
    eng = _engine(
        [_disc("L4-L5", "severe"), _disc("L5-S1", "moderate"), _disc("L1-L2", "mild")],
        [
            {"level": "L4-L5", "structure": "disc_desiccated", "drawn": True},
            {"level": "L5-S1", "structure": "disc_desiccated", "drawn": True},
            {"level": None, "structure": "canal_csf", "drawn": True},  # normal reference
        ],
    )
    comp = eng._compute_annotation_completeness()
    assert comp["complete"] is True
    assert comp["unmarked_findings"] == []
    assert comp["normal_reference_present"] is True


def test_unmarked_finding_is_flagged():
    eng = _engine(
        [_disc("L4-L5", "severe"), _disc("L5-S1", "severe")],
        [
            {"level": "L4-L5", "structure": "disc_desiccated", "drawn": True},
            {"level": None, "structure": "canal_csf", "drawn": True},
        ],
    )
    comp = eng._compute_annotation_completeness()
    assert comp["unmarked_findings"] == ["L5-S1"]
    assert comp["complete"] is False


def test_missing_normal_reference_is_incomplete():
    eng = _engine(
        [_disc("L4-L5", "severe")],
        [{"level": "L4-L5", "structure": "disc_desiccated", "drawn": True}],
    )
    comp = eng._compute_annotation_completeness()
    assert comp["normal_reference_present"] is False
    assert comp["complete"] is False

"""Run A — confidence-forward guard.

Locks in the un-muzzling: the blanket confidence-suppression / undercalling / soft-contradiction
rules are gone, while the ONE real guard (no fabricated mm) and the single disclaimer remain.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from prompts import base_prompt  # noqa: E402

# Every file whose prompt/protocol text drives the read or report.
PROMPT_SOURCES = [
    BACKEND / "prompts" / "base_prompt.py",
    BACKEND / "prompts" / "spine_master.py",
    BACKEND / "services" / "agent_runner.py",
    BACKEND / "services" / "claude_interpreter.py",
    BACKEND / "services" / "verification.py",
    BACKEND / "skills" / "mri-spine-analysis" / "SKILL.md",
]

# Muzzle phrases that must NOT reappear anywhere in the read/report text.
MUZZLE_PHRASES = [
    "prefer the LESS severe",
    "choose the LESS severe",
    "LESS severe interpretation wins",
    "ALWAYS Tier C (never higher)",
    "cap confidence at Tier B",
    "may warrant further evaluation",
    "Visual-only assessment (no quantitative data)",
]


def _blob() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in PROMPT_SOURCES)


def test_muzzle_phrases_removed():
    blob = _blob()
    for phrase in MUZZLE_PHRASES:
        assert phrase not in blob, f"muzzle phrase reintroduced: {phrase!r}"


def test_measurement_guard_kept():
    # The one real limitation stays: no fabricated mm without calibration.
    rules = base_prompt.BASE_RULES
    assert "do NOT fabricate mm values" in rules
    assert "specific mm value in uncalibrated mode is a reporting error" in rules


def test_single_disclaimer_present():
    assert base_prompt.REPORT_DISCLAIMER.strip()
    assert "board-certified radiologist" in base_prompt.REPORT_DISCLAIMER


def test_confidence_forward_language_present():
    rules = base_prompt.BASE_RULES
    # Tier follows what is actually seen, not a forced cap.
    assert "Tier A even if it is qualitative" in rules
    # Discrepancies are stated, not softened.
    assert "State the difference clearly and confidently" in rules

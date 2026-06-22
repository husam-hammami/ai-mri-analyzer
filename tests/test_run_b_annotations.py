"""Run B — annotations: a finding never silently loses its visual.

When a precise arrow tip can't be intensity-verified, the engine now falls back to a labelled
REGION BAND instead of dropping the annotation entirely.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_region_band_draws_a_marker():
    from PIL import Image, ImageDraw, ImageFont
    from core.dicom_engine import DICOMEngine

    img = Image.new("RGB", (240, 240), "black")
    draw = ImageDraw.Draw(img)
    before = list(img.getdata())

    eng = DICOMEngine.__new__(DICOMEngine)  # _draw_region_band uses no instance state
    eng._draw_region_band(draw, (120, 120), "L5-S1 (approx region)", "red", "right",
                          ImageFont.load_default())

    assert list(img.getdata()) != before, "region band drew nothing"


def test_failed_tip_falls_back_not_dropped():
    src = (BACKEND / "core" / "dicom_engine.py").read_text(encoding="utf-8")
    # the region-band fallback exists ...
    assert "_draw_region_band" in src
    assert 'audit["status"] = "region_band"' in src
    # ... and the old silent-drop log is gone.
    assert "NOT drawn" not in src


def test_prompt_and_skill_agree_no_silent_drop():
    runner = (BACKEND / "services" / "agent_runner.py").read_text(encoding="utf-8")
    skill = (BACKEND / "skills" / "mri-spine-analysis" / "SKILL.md").read_text(encoding="utf-8")
    assert "DROP the annotation" not in runner
    assert "never silently\n    drop the finding's visual" in runner or "drop the finding's visual" in runner
    assert "never silently drop the finding's visual" in skill

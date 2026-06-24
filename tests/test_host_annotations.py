"""Phase 5 — agent-mode host-side render of model-emitted annotation specs.

The model writes annotations.json; the host deterministically re-renders the figures so
they always exist and follow the renderer's rules, even if the model's own draw failed.
"""
import json

from PIL import Image

from core.palette import CERTAINTY_RGB255
from services.agent_runner import AgentRunner


def test_host_render_creates_figure_from_annotations_json(tmp_path):
    Image.new("RGB", (100, 100), (0, 0, 0)).save(tmp_path / "raw.png")
    (tmp_path / "annotations.json").write_text(json.dumps([
        {
            "figure": "fig1.png", "base": "raw.png", "calibrated": True, "title": "Sag T2",
            "marks": [{"form": "circle", "center": [50, 50], "radius": 18,
                       "certainty": "Confirmed", "label": "L5-S1 disc"}],
        }
    ]), encoding="utf-8")

    runner = AgentRunner.__new__(AgentRunner)  # bypass __init__; method uses no runner state
    runner._render_host_annotations(tmp_path)

    out = tmp_path / "fig1.png"
    assert out.exists()
    assert CERTAINTY_RGB255["Confirmed"] in set(Image.open(out).convert("RGB").getdata())


def test_host_render_no_annotations_is_noop(tmp_path):
    runner = AgentRunner.__new__(AgentRunner)
    runner._render_host_annotations(tmp_path)  # no annotations.json → must not raise
    assert not (tmp_path / "fig1.png").exists()


def test_host_render_skips_entry_with_missing_base(tmp_path):
    (tmp_path / "annotations.json").write_text(json.dumps([
        {"figure": "fig1.png", "base": "nope.png",
         "marks": [{"form": "box", "bbox": [10, 10, 40, 40], "certainty": "Likely"}]}
    ]), encoding="utf-8")
    runner = AgentRunner.__new__(AgentRunner)
    runner._render_host_annotations(tmp_path)  # base missing → entry skipped, no crash
    assert not (tmp_path / "fig1.png").exists()

"""Focused disc-level localizer + the host-side snap step.

A minimal Claude-vision call out-localizes the mega-prompt agent (measured 2026-06-24); we
snap the agent's level-named marks onto its coordinates. These tests cover the pure parsing
and snapping deterministically, and the render-hook integration with the vision call mocked.
"""
import json

from PIL import Image

from services import agent_runner
from services.agent_runner import AgentRunner
from services.disc_localizer import level_token, parse_levels, snap_marks_to_levels


def test_level_token_normalizes_separators_and_case():
    assert level_token("L5-S1 disc narrowed") == "L5-S1"
    assert level_token("l4–l5") == "L4-L5"           # en-dash, lowercase
    assert level_token("T12/L1 preserved") == "T12-L1"
    assert level_token("L1-L2 preserved disc - normal") == "L1-L2"
    assert level_token("canal stenosis") is None
    assert level_token(None) is None


def test_parse_levels_tolerant_and_clamped():
    txt = 'noise before {"L5-S1":[90,300],"L4-L5":[99,259],"junk":[1,2]} trailing'
    out = parse_levels(txt, w=320, h=350)
    assert out["L5-S1"] == (90, 300)
    assert out["L4-L5"] == (99, 259)
    assert "junk" not in out                          # non-level keys dropped
    # out-of-bounds coords are clamped, not dropped
    assert parse_levels('{"L5-S1":[9999,-5]}', 320, 350)["L5-S1"] == (319, 0)
    assert parse_levels("not json", 100, 100) == {}


def test_snap_recenters_box_keeping_size():
    marks = [{"form": "box", "bbox": [10, 280, 40, 320], "label": "L5-S1 disc narrowed"},
             {"form": "leader", "center": [131, 112], "label": "canal (no level)"}]
    n = snap_marks_to_levels(marks, {"L5-S1": (96, 100)})
    assert n == 1
    x0, y0, x1, y1 = marks[0]["bbox"]
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == (96.0, 100.0)   # recentered
    assert (x1 - x0, y1 - y0) == (30, 40)                    # size preserved
    assert marks[1]["center"] == [131, 112]                  # non-level mark untouched


def test_snap_noop_without_coords():
    marks = [{"form": "box", "bbox": [1, 1, 9, 9], "label": "L5-S1"}]
    assert snap_marks_to_levels(marks, {}) == 0
    assert marks[0]["bbox"] == [1, 1, 9, 9]


def test_render_hook_snaps_level_marks_to_localizer(tmp_path, monkeypatch):
    # base image nested as the agent saves it; one level-named region box placed too low
    nested = tmp_path / "evidence" / "images"
    nested.mkdir(parents=True)
    Image.new("RGB", (100, 100), (0, 0, 0)).save(nested / "ev008.png")
    (tmp_path / "annotations.json").write_text(json.dumps([
        {"figure": "fig1.png", "base": "ev008.png", "calibrated": False,
         "marks": [{"form": "box", "bbox": [10, 80, 40, 95], "certainty": "Possible",
                    "label": "L5-S1 disc narrowed"}]}
    ]), encoding="utf-8")

    # mock the focused vision call: L5-S1 actually sits mid-image, not at the bottom
    monkeypatch.setattr(agent_runner, "localize_levels",
                        lambda *a, **k: {"L5-S1": (50, 50)})

    runner = AgentRunner.__new__(AgentRunner)
    runner.claude_bin = "claude"
    runner.model, runner.effort, runner.permission_mode = "m", "low", "default"
    runner._child_env = lambda: {}
    runner._render_host_annotations(tmp_path)

    assert (tmp_path / "fig1.png").exists()
    saved = json.loads((tmp_path / "annotations.json").read_text(encoding="utf-8"))
    x0, y0, x1, y1 = saved[0]["marks"][0]["bbox"]
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == (50.0, 50.0)    # snapped up from the sacrum

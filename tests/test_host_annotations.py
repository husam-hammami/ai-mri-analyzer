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


def test_host_render_finds_nested_base_image(tmp_path):
    # The agent nests bases under evidence/images/; the resolver must find them by recursive
    # search — the one-level lookup silently skipped them, so the gate/rendering never ran.
    nested = tmp_path / "evidence" / "images"
    nested.mkdir(parents=True)
    Image.new("RGB", (100, 100), (0, 0, 0)).save(nested / "ev008.png")
    (tmp_path / "annotations.json").write_text(json.dumps([
        {"figure": "fig1.png", "base": "ev008.png", "calibrated": False,
         "marks": [{"form": "box", "bbox": [10, 10, 40, 40], "certainty": "Possible", "label": "x"}]}
    ]), encoding="utf-8")
    runner = AgentRunner.__new__(AgentRunner)
    runner._render_host_annotations(tmp_path)
    assert (tmp_path / "fig1.png").exists()


def test_resolve_base_image_skips_ambiguous_match(tmp_path):
    # Two same-named slices under the work tree: the recursive fallback must NOT guess the first —
    # drawing marks computed for one slice onto a different series' same-named slice is a silent
    # coordinate-space mismatch. Ambiguous → None (skip, leave the model's figure).
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    Image.new("RGB", (10, 10), (0, 0, 0)).save(tmp_path / "a" / "slice_008.png")
    Image.new("RGB", (10, 10), (0, 0, 0)).save(tmp_path / "b" / "slice_008.png")
    out_dir = tmp_path / "report"
    out_dir.mkdir()
    assert AgentRunner._resolve_base_image(out_dir, "slice_008.png") is None
    # but a single unambiguous nested match still resolves
    (tmp_path / "c").mkdir()
    Image.new("RGB", (10, 10), (0, 0, 0)).save(tmp_path / "c" / "unique_042.png")
    assert AgentRunner._resolve_base_image(out_dir, "unique_042.png") is not None


def test_study_is_uncalibrated_reads_manifest(tmp_path):
    # Host calibration truth comes from the manifest, not the model's annotations.json claim.
    out_dir = tmp_path / "report"
    out_dir.mkdir()
    (tmp_path / "evidence").mkdir()
    man = tmp_path / "evidence" / "evidence_manifest.json"
    man.write_text(json.dumps({"study": {"input_type": "image_export"}}), encoding="utf-8")
    assert AgentRunner._study_is_uncalibrated(out_dir) is True
    man.write_text(json.dumps({"study": {"input_type": "dicom", "calibrated": True}}), encoding="utf-8")
    assert AgentRunner._study_is_uncalibrated(out_dir) is False
    man.unlink()
    assert AgentRunner._study_is_uncalibrated(out_dir) is False   # absent manifest → not uncalibrated


def test_render_host_annotations_static_gate_overwrites_model_figure(tmp_path):
    # #12 regression: the post-QA PDF rebuild calls this as a STATICMETHOD (no AgentRunner
    # instance) and it must be the LAST writer — re-rendering over the model's own ungated figure
    # so the gate can't be bypassed by the rebuild path.
    nested = tmp_path / "evidence" / "images"
    nested.mkdir(parents=True)
    Image.new("RGB", (200, 200), (0, 0, 0)).save(nested / "ev.png")
    Image.new("RGB", (50, 50), (255, 0, 0)).save(tmp_path / "f.png")   # model's own ungated figure
    before = (tmp_path / "f.png").read_bytes()
    (tmp_path / "annotations.json").write_text(json.dumps([
        {"figure": "f.png", "base": "ev.png", "calibrated": False,
         "marks": [{"form": "box", "bbox": [20, 20, 60, 60], "label": "L5-S1", "certainty": "Possible"}]}
    ]), encoding="utf-8")
    AgentRunner._render_host_annotations(tmp_path)   # called with no instance, as app.py does
    assert (tmp_path / "f.png").read_bytes() != before   # gate re-rendered over the model figure


def test_host_render_skips_entry_with_missing_base(tmp_path):
    (tmp_path / "annotations.json").write_text(json.dumps([
        {"figure": "fig1.png", "base": "nope.png",
         "marks": [{"form": "box", "bbox": [10, 10, 40, 40], "certainty": "Likely"}]}
    ]), encoding="utf-8")
    runner = AgentRunner.__new__(AgentRunner)
    runner._render_host_annotations(tmp_path)  # base missing → entry skipped, no crash
    assert not (tmp_path / "fig1.png").exists()

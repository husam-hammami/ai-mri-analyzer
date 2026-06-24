"""Phase 5 — deterministic, model-chosen annotation renderer.

The model emits a spec (form + short label + certainty + optional calibrated number); this
tested code renders it. Colour = certainty, numbers only when calibrated, a bad spec is
skipped not fatal, and a significance cap drops the least-significant marks (logged).
"""
import logging

from PIL import Image

from core.annotation_renderer import normalize_spec, render_all
from core.palette import CERTAINTY_RGB255

ACCENT = CERTAINTY_RGB255["Confirmed"]  # (37, 99, 235)


def _base(tmp_path, w=120, h=120):
    p = tmp_path / "base.png"
    Image.new("RGB", (w, h), (0, 0, 0)).save(p)
    return p


def _colors(path):
    return set(Image.open(path).convert("RGB").getdata())


def _has(path, color):
    return color in _colors(path)


def test_each_primitive_draws(tmp_path):
    forms = [
        {"form": "arrow", "point": [60, 60], "label": "a"},
        {"form": "circle", "center": [60, 60], "radius": 15, "label": "c"},
        {"form": "ellipse", "bbox": [40, 40, 80, 70], "label": "e"},
        {"form": "box", "bbox": [40, 40, 80, 80], "label": "b"},
        {"form": "caliper", "p0": [40, 50], "p1": [80, 50], "label": "k"},
        {"form": "leader", "point": [60, 60], "label": "l"},
    ]
    for spec in forms:
        out = tmp_path / f"{spec['form']}.png"
        res = render_all(_base(tmp_path), [spec], out, scale=2, legend=False)
        assert res["rendered"] == 1, spec["form"]
        # something other than the black background was drawn
        assert any(px != (0, 0, 0) for px in _colors(out)), spec["form"]


def test_bad_spec_is_skipped_not_fatal(tmp_path):
    specs = [
        {"form": "spiral", "point": [10, 10]},        # invalid form
        {"form": "circle"},                           # missing coords
        {"form": "caliper", "p0": [1, 1]},            # missing p1
        {"form": "box", "bbox": [10, 10, 50, 50], "label": "ok"},  # valid
    ]
    out = tmp_path / "mixed.png"
    res = render_all(_base(tmp_path), specs, out, scale=2)
    assert res["rendered"] == 1


def test_coords_clamp_into_image(tmp_path):
    spec = {"form": "circle", "center": [9999, -50], "radius": 10, "label": "x"}
    out = tmp_path / "clamp.png"
    res = render_all(_base(tmp_path), [spec], out, scale=2, legend=False)
    assert res["rendered"] == 1  # clamped, not dropped, not crashed


def test_certainty_drives_colour(tmp_path):
    out = tmp_path / "color.png"
    render_all(_base(tmp_path), [{"form": "circle", "center": [60, 60], "radius": 20,
                                  "certainty": "Confirmed"}], out, scale=2, legend=False)
    assert _has(out, ACCENT)


def test_uncalibrated_shows_no_mm_number(tmp_path):
    spec = {"form": "caliper", "p0": [30, 60], "p1": [90, 60], "number": 7.4, "units": "mm",
            "certainty": "Likely", "calibrated": False}
    out = tmp_path / "uncal.png"
    res = render_all(_base(tmp_path), [spec], out, scale=2, calibrated=False, legend=False)
    assert res["marks"][0]["number_shown"] is False


def test_calibrated_shows_number(tmp_path):
    spec = {"form": "caliper", "p0": [30, 60], "p1": [90, 60], "number": 7.4, "units": "mm",
            "certainty": "Confirmed", "calibrated": True}
    out = tmp_path / "cal.png"
    res = render_all(_base(tmp_path), [spec], out, scale=2, legend=False)
    assert res["marks"][0]["number_shown"] is True


def test_label_is_margin_placed(tmp_path):
    # a label for a centred target must sit in a margin, never on top of the target
    out = tmp_path / "label.png"
    render_all(_base(tmp_path), [{"form": "circle", "center": [60, 60], "radius": 8,
                                  "label": "L5-S1 disc", "certainty": "Confirmed"}],
               out, scale=2, legend=False)
    img = Image.open(out).convert("RGB")
    W, _ = img.size
    px = img.load()
    margin = any(px[x, y] == ACCENT for x in list(range(0, 30)) + list(range(W - 30, W))
                 for y in range(img.size[1]))
    assert margin  # label text/leader reached a margin column


def test_legend_is_rendered(tmp_path):
    out_no = tmp_path / "no_legend.png"
    out_yes = tmp_path / "legend.png"
    spec = {"form": "circle", "center": [60, 60], "radius": 6, "certainty": "Possible"}
    render_all(_base(tmp_path), [spec], out_no, scale=2, legend=False)
    render_all(_base(tmp_path), [spec], out_yes, scale=2, legend=True)
    possible = CERTAINTY_RGB255["Possible"]
    # the legend adds the certainty swatch colour the small mark alone may not contribute much of
    yes_count = sum(1 for px in Image.open(out_yes).convert("RGB").getdata() if px == possible)
    no_count = sum(1 for px in Image.open(out_no).convert("RGB").getdata() if px == possible)
    assert yes_count > no_count


def test_significance_cap_drops_least_significant_and_logs(tmp_path, caplog):
    specs = [
        {"form": "circle", "center": [30, 30], "radius": 6, "label": "keep1", "significance": 0.9},
        {"form": "circle", "center": [60, 60], "radius": 6, "label": "keep2", "significance": 0.8},
        {"form": "circle", "center": [90, 90], "radius": 6, "label": "drop_me", "significance": 0.1},
    ]
    out = tmp_path / "cap.png"
    with caplog.at_level(logging.INFO, logger="mika.annotation_renderer"):
        res = render_all(_base(tmp_path), specs, out, scale=2, max_marks=2, legend=False)
    assert res["rendered"] == 2
    assert [d["label"] for d in res["dropped"]] == ["drop_me"]
    assert any("de-clutter" in r.message for r in caplog.records)


def test_uncalibrated_pinpoint_downgrades_to_region_box():
    # A flat JPG (calibrated=False) must never get a pinpoint — circle/arrow → region box.
    c = normalize_spec({"form": "circle", "center": [60, 60], "radius": 12, "calibrated": False}, 120, 120)
    assert c["form"] == "box" and c["bbox"] is not None
    a = normalize_spec({"form": "arrow", "point": [50, 50], "calibrated": False}, 120, 120)
    assert a["form"] == "box"
    # calibrated (or unspecified) keeps the model's chosen form
    assert normalize_spec({"form": "circle", "center": [60, 60], "radius": 12, "calibrated": True}, 120, 120)["form"] == "circle"
    assert normalize_spec({"form": "circle", "center": [60, 60], "radius": 12}, 120, 120)["form"] == "circle"


def test_normalize_spec_rejects_garbage():
    assert normalize_spec({"form": "nope"}, 100, 100) is None
    assert normalize_spec("not a dict", 100, 100) is None
    ok = normalize_spec({"form": "box", "bbox": [1, 1, 9, 9]}, 100, 100)
    assert ok and ok["form"] == "box"

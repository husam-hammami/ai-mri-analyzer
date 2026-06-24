"""Deterministic annotation renderer (Pillow-only, anatomy-agnostic).

The MODEL emits an annotation spec per mark — it chooses the clearest visual FORM
(arrow / circle / ellipse / box / caliper / leader) and the short label; this tested code
RENDERS it. The renderer owns the rules that must not drift:

  * coords are BASE-image pixels — the renderer applies ``* scale``;
  * the on-mark text is ONE short line; the NUMBER carries explicit units and appears ONLY
    when calibrated (uncalibrated → qualitative, never a fabricated mm);
  * colour encodes CERTAINTY (from the shared core palette) and a legend is drawn;
  * a ``significance`` field caps marks per figure — least-significant dropped, LOGGED;
  * a bad spec is skipped, never crashes the figure.

Reasoning / ratios / Tier belong in the figure CAPTION (the PDF), not on the image.
"""
import logging
import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

try:
    from core.palette import CERTAINTY_ORDER, CERTAINTY_RGB255, certainty_rgb255, normalize_certainty
except ImportError:  # pragma: no cover - import path when launched from backend/
    from backend.core.palette import (
        CERTAINTY_ORDER, CERTAINTY_RGB255, certainty_rgb255, normalize_certainty,
    )

logger = logging.getLogger("mika.annotation_renderer")

VALID_FORMS = {"arrow", "circle", "ellipse", "box", "caliper", "leader"}

# Legibility on grayscale anatomy: every coloured stroke gets a dark HALO so it separates
# from mid-grey tissue, and label text is WHITE (certainty is shown by a colour swatch +
# the mark colour + the legend, never by tinting the text grey — which is unreadable).
HALO = (6, 8, 12)
WHITE = (245, 247, 250)


def _get_font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _num(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _pt(value, w, h):
    """Validate a [x, y] coordinate (base px) and clamp into the image."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    x, y = _num(value[0]), _num(value[1])
    if x is None or y is None:
        return None
    return (_clamp(x, 0, w - 1), _clamp(y, 0, h - 1))


def _bbox(value, w, h):
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    xs = [_num(value[0]), _num(value[2])]
    ys = [_num(value[1]), _num(value[3])]
    if any(v is None for v in xs + ys):
        return None
    x0, x1 = sorted(_clamp(x, 0, w - 1) for x in xs)
    y0, y1 = sorted(_clamp(y, 0, h - 1) for y in ys)
    return (x0, y0, x1, y1)


# A broad region box's MINIMUM half-extent as a fraction of the image, used on uncalibrated
# studies. Sized to absorb the model's coordinate error (~one disc level ≈ 11% of height in
# the studies we measured) so the box honestly covers the finding's region even when the
# centre is a level off. Full box ≈ 36% wide × 24% tall.
BROAD_BOX_W_FRAC = 0.18
BROAD_BOX_H_FRAC = 0.18


def normalize_spec(spec: dict, w: int, h: int, calibrated: Optional[bool] = None) -> Optional[dict]:
    """Coerce a model spec into a clean render spec, or None if unrenderable.

    Required: a ``form`` in VALID_FORMS and coords appropriate to that form. Everything else
    has a safe default. Never raises — a malformed spec returns None and is skipped.

    ``calibrated`` is the STUDY-level calibration, used when the spec has no per-mark flag —
    the model rarely sets it per mark, so without this the uncalibrated form-gate never fired.
    """
    if not isinstance(spec, dict):
        return None
    form = str(spec.get("form", "")).strip().lower()
    if form not in VALID_FORMS:
        return None

    center = _pt(spec.get("center") or spec.get("point"), w, h)
    bbox = _bbox(spec.get("bbox"), w, h)
    p0 = _pt(spec.get("p0"), w, h)
    p1 = _pt(spec.get("p1"), w, h)
    radius = _num(spec.get("radius"))

    # Uncalibrated images get a BROAD region box over the finding's general area, NEVER a
    # pinpoint. A flat JPG/screenshot has no scale or geometry, so any precise marker (circle,
    # arrow, tight box, caliper, leader) implies an accuracy we don't have. An honest broad box
    # beats a precise-but-wrong one (user call, 2026-06-24, after a focused localizer still
    # missed). Degrade EVERY form to a generous region box centred on the mark. Falls back to
    # the STUDY-level flag when the mark has none (the model rarely sets it per mark).
    eff_calibrated = spec.get("calibrated")
    if eff_calibrated is None:
        eff_calibrated = calibrated
    if eff_calibrated is False:
        cx = cy = hw = hh = None
        if bbox is not None:
            cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
            hw, hh = abs(bbox[2] - bbox[0]) / 2.0, abs(bbox[3] - bbox[1]) / 2.0
        elif center is not None:
            cx, cy, hw, hh = center[0], center[1], 0.0, 0.0
        elif p0 is not None and p1 is not None:
            cx, cy, hw, hh = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0, 0.0, 0.0
        if cx is not None:
            hw = max(hw, BROAD_BOX_W_FRAC * w)
            hh = max(hh, BROAD_BOX_H_FRAC * h)
            bbox = (_clamp(cx - hw, 0, w - 1), _clamp(cy - hh, 0, h - 1),
                    _clamp(cx + hw, 0, w - 1), _clamp(cy + hh, 0, h - 1))
            form = "box"

    if form in ("arrow", "leader", "circle"):
        if center is None:
            return None
        if form == "circle" and radius is None:
            radius = 14.0
    elif form in ("box", "ellipse"):
        if bbox is None:
            if center is not None and radius:
                bbox = (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
            else:
                return None
    elif form == "caliper":
        if p0 is None or p1 is None:
            return None

    out = {
        "form": form,
        "center": center,
        "bbox": bbox,
        "p0": p0,
        "p1": p1,
        "radius": float(radius) if radius else None,
        "label": str(spec.get("label") or "").strip(),
        "number": _num(spec.get("number")),
        "units": str(spec.get("units") or "").strip(),
        "certainty": normalize_certainty(spec.get("certainty")),
        "significance": _num(spec.get("significance")) if _num(spec.get("significance")) is not None else 0.5,
        "label_side": str(spec.get("label_side") or "auto").strip().lower(),
        "calibrated": spec.get("calibrated"),
    }
    return out


def _target(spec) -> tuple:
    """The point a label leader should point at."""
    if spec["center"] is not None:
        return spec["center"]
    if spec["bbox"] is not None:
        x0, y0, x1, y1 = spec["bbox"]
        return ((x0 + x1) / 2, (y0 + y1) / 2)
    if spec["p0"] and spec["p1"]:
        return ((spec["p0"][0] + spec["p1"][0]) / 2, (spec["p0"][1] + spec["p1"][1]) / 2)
    return (0, 0)


class _LabelPlacer:
    """Stack margin labels so text never overlaps anatomy or other labels."""

    def __init__(self, height: int, line_h: int):
        self.height = height
        self.line_h = line_h
        self.used = {"left": [], "right": []}

    def place(self, side: str, want_y: float) -> float:
        y = _clamp(want_y, 4, self.height - self.line_h - 4)
        bands = sorted(self.used[side])
        moved = True
        while moved:
            moved = False
            for (b0, b1) in bands:
                if b0 - self.line_h < y < b1 + 2:
                    y = b1 + 2
                    moved = True
            if y > self.height - self.line_h - 4:
                y = max(4, want_y - self.line_h)  # spill upward if the bottom is full
                break
        self.used[side].append((y, y + self.line_h))
        return y


def _halo_line(draw, pts, color, width):
    draw.line(pts, fill=HALO, width=width + max(3, width))
    draw.line(pts, fill=color, width=width)


def _halo_ellipse(draw, box, color, width):
    draw.ellipse(box, outline=HALO, width=width + 2)
    draw.ellipse(box, outline=color, width=width)


def _halo_rect(draw, box, color, width):
    draw.rectangle(box, outline=HALO, width=width + 2)
    draw.rectangle(box, outline=color, width=width)


def _draw_arrowhead(draw, start, end, color, width):
    _halo_line(draw, [start, end], color, width)
    ang = math.atan2(end[1] - start[1], end[0] - start[0])
    bl = 6 + 3 * width
    for d in (+2.5, -2.5):
        bx = end[0] - bl * math.cos(ang + d)
        by = end[1] - bl * math.sin(ang + d)
        _halo_line(draw, [(bx, by), end], color, width)


def _render_form(draw, spec, scale, color, width=2):
    """Draw the chosen primitive in SCALED display coords. Returns the leader anchor point."""
    form = spec["form"]
    tx, ty = _target(spec)
    target = (tx * scale, ty * scale)
    if form in ("circle",):
        r = (spec["radius"] or 14.0) * scale
        _halo_ellipse(draw, [target[0] - r, target[1] - r, target[0] + r, target[1] + r], color, width)
    elif form == "ellipse":
        x0, y0, x1, y1 = spec["bbox"]
        _halo_ellipse(draw, [x0 * scale, y0 * scale, x1 * scale, y1 * scale], color, width)
    elif form == "box":
        x0, y0, x1, y1 = spec["bbox"]
        _halo_rect(draw, [x0 * scale, y0 * scale, x1 * scale, y1 * scale], color, width)
    elif form == "caliper":
        a = (spec["p0"][0] * scale, spec["p0"][1] * scale)
        b = (spec["p1"][0] * scale, spec["p1"][1] * scale)
        _halo_line(draw, [a, b], color, width)
        # perpendicular end ticks
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) + math.pi / 2
        tick = 6 + 2 * width
        for pt in (a, b):
            _halo_line(draw, [(pt[0] - tick * math.cos(ang), pt[1] - tick * math.sin(ang)),
                              (pt[0] + tick * math.cos(ang), pt[1] + tick * math.sin(ang))], color, width)
    # arrow/leader heads are drawn from the label connector; nothing else here.
    return target


def render_all(
    base_image_path,
    specs,
    out_path,
    *,
    scale: int = 2,
    calibrated: bool = False,
    max_marks: Optional[int] = None,
    title: Optional[str] = None,
    legend: bool = True,
) -> dict:
    """Render model-emitted annotation specs onto a base image, deterministically.

    Returns ``{out_path, rendered, dropped, marks}``. A bad spec is skipped (logged), not
    fatal. When ``max_marks`` is set the least-significant marks are dropped and LOGGED.
    """
    base = Image.open(str(base_image_path)).convert("RGB")
    base = base.resize((base.width * scale, base.height * scale), Image.LANCZOS)
    draw = ImageDraw.Draw(base)
    W, H = base.size
    base_w, base_h = base.width // scale, base.height // scale
    font_px = max(14, 9 * scale)
    font = _get_font(font_px)
    title_font = _get_font(font_px + 3)
    line_h = font_px + 8
    stroke = max(2, scale)

    # normalize + drop invalid
    clean = []
    for raw in (specs or []):
        ns = normalize_spec(raw, base_w, base_h, calibrated=calibrated)
        if ns is None:
            logger.info("annotation spec skipped (unrenderable): %r", raw)
            continue
        clean.append(ns)

    # significance cap — keep the most significant, drop + LOG the rest (never silent)
    clean.sort(key=lambda s: s["significance"], reverse=True)
    dropped = []
    if max_marks is not None and len(clean) > max_marks:
        dropped = clean[max_marks:]
        clean = clean[:max_marks]
        for d in dropped:
            logger.info("annotation dropped for de-clutter (significance=%.2f): %s",
                        d["significance"], d["label"] or d["form"])

    placer = _LabelPlacer(H, line_h)
    used_certainties = set()
    marks_audit = []

    for spec in clean:
        try:
            color = certainty_rgb255(spec["certainty"])
            used_certainties.add(spec["certainty"])
            target = _render_form(draw, spec, scale, color, stroke)

            # number only when calibrated and a unit is given — never a fabricated mm
            show_number = (
                spec["number"] is not None and spec["units"]
                and bool(spec["calibrated"] if spec["calibrated"] is not None else calibrated)
            )
            label_text = spec["label"]
            if show_number and spec["form"] != "caliper":
                num = f"{spec['number']:g} {spec['units']}".strip()
                label_text = f"{label_text}  {num}".strip()
            if spec["form"] == "caliper" and show_number:
                mid = ((spec["p0"][0] + spec["p1"][0]) / 2 * scale, (spec["p0"][1] + spec["p1"][1]) / 2 * scale)
                _text_chip(draw, (mid[0] + 6, mid[1] - line_h), f"{spec['number']:g} {spec['units']}".strip(),
                           font, swatch=color)

            # margin-placed label with a thin leader (text never covers anatomy)
            if label_text:
                side = spec["label_side"]
                if side not in ("left", "right"):
                    side = "right" if target[0] < W / 2 else "left"
                label_w = (font_px + 6) + draw.textlength(label_text, font=font)  # swatch + text
                ly = placer.place(side, target[1])
                if side == "left":
                    lx = 6
                    anchor = (lx + label_w + 4, ly + line_h / 2)
                else:
                    lx = W - label_w - 8
                    anchor = (lx - 4, ly + line_h / 2)
                # leader / arrowhead from the label to the mark (haloed so it reads on anatomy)
                if spec["form"] == "arrow":
                    _draw_arrowhead(draw, anchor, target, color, stroke)
                else:
                    _halo_line(draw, [anchor, target], color, max(2, stroke - 1))
                _text_chip(draw, (lx, ly), label_text, font, swatch=color)
            elif spec["form"] == "arrow":
                _draw_arrowhead(draw, (target[0] + 40, target[1]), target, color, stroke)

            marks_audit.append({"form": spec["form"], "label": spec["label"],
                                "certainty": spec["certainty"], "number_shown": bool(show_number)})
        except Exception as exc:  # noqa: BLE001 - one bad mark must not kill the figure
            logger.warning("annotation mark failed to render (%s): %s", spec.get("form"), exc)

    if title:
        _text_chip(draw, (6, 4), title, title_font)
    if legend and used_certainties:
        _draw_legend(draw, used_certainties, font, W, H)

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    base.save(out_path)
    return {
        "out_path": out_path,
        "rendered": len(marks_audit),
        "dropped": [{"label": d["label"], "significance": d["significance"]} for d in dropped],
        "marks": marks_audit,
    }


def _text_chip(draw, xy, text, font, swatch=None):
    """White text on a black plate (always legible over any anatomy). An optional certainty
    colour swatch sits at the left so certainty reads without tinting the text grey."""
    x, y = xy
    th = int(getattr(font, "size", 14))
    sw = th + 6 if swatch is not None else 0
    tw = draw.textlength(text, font=font)
    draw.rectangle([x - 4, y - 3, x + sw + tw + 4, y + th + 4], fill="black")
    if swatch is not None:
        draw.rectangle([x, y + 1, x + th, y + th + 1], fill=swatch, outline=WHITE)
    draw.text((x + sw, y), text, fill=WHITE, font=font)


def _draw_legend(draw, certainties, font, W, H):
    """Colour → certainty key at the bottom-left (white text + colour swatches)."""
    items = [k for k in CERTAINTY_ORDER if k in certainties]
    th = int(getattr(font, "size", 14))
    x = 8
    y = H - th - 12
    widths = [(k, int(draw.textlength(k, font=font))) for k in items]
    total = sum(th + 6 + w + 16 for _, w in widths)
    draw.rectangle([x - 5, y - 5, x + total, y + th + 5], fill="black")
    for k, w in widths:
        draw.rectangle([x, y, x + th, y + th], fill=CERTAINTY_RGB255[k], outline=WHITE)
        draw.text((x + th + 5, y), k, fill=WHITE, font=font)
        x += th + 6 + w + 16

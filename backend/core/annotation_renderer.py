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


def normalize_spec(spec: dict, w: int, h: int) -> Optional[dict]:
    """Coerce a model spec into a clean render spec, or None if unrenderable.

    Required: a ``form`` in VALID_FORMS and coords appropriate to that form. Everything else
    has a safe default. Never raises — a malformed spec returns None and is skipped.
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


def _draw_arrowhead(draw, start, end, color, width):
    draw.line([start, end], fill=color, width=width)
    ang = math.atan2(end[1] - start[1], end[0] - start[0])
    for d in (+2.5, -2.5):
        bx = end[0] - 12 * math.cos(ang + d)
        by = end[1] - 12 * math.sin(ang + d)
        draw.line([(bx, by), end], fill=color, width=width)


def _render_form(draw, spec, scale, color, width=2):
    """Draw the chosen primitive in SCALED display coords. Returns the leader anchor point."""
    form = spec["form"]
    tx, ty = _target(spec)
    target = (tx * scale, ty * scale)
    if form in ("circle",):
        r = (spec["radius"] or 14.0) * scale
        draw.ellipse([target[0] - r, target[1] - r, target[0] + r, target[1] + r], outline=color, width=width)
    elif form == "ellipse":
        x0, y0, x1, y1 = spec["bbox"]
        draw.ellipse([x0 * scale, y0 * scale, x1 * scale, y1 * scale], outline=color, width=width)
    elif form == "box":
        x0, y0, x1, y1 = spec["bbox"]
        draw.rectangle([x0 * scale, y0 * scale, x1 * scale, y1 * scale], outline=color, width=width)
    elif form == "caliper":
        a = (spec["p0"][0] * scale, spec["p0"][1] * scale)
        b = (spec["p1"][0] * scale, spec["p1"][1] * scale)
        draw.line([a, b], fill=color, width=width)
        # perpendicular end ticks
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) + math.pi / 2
        for pt in (a, b):
            draw.line([(pt[0] - 6 * math.cos(ang), pt[1] - 6 * math.sin(ang)),
                       (pt[0] + 6 * math.cos(ang), pt[1] + 6 * math.sin(ang))], fill=color, width=width)
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
    font = _get_font(13)
    title_font = _get_font(15)
    line_h = 18

    # normalize + drop invalid
    clean = []
    for raw in (specs or []):
        ns = normalize_spec(raw, base_w, base_h)
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
            target = _render_form(draw, spec, scale, color)

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
                           font, color)

            # margin-placed label with a thin leader (text never covers anatomy)
            if label_text:
                side = spec["label_side"]
                if side not in ("left", "right"):
                    side = "right" if target[0] < W / 2 else "left"
                tw = draw.textlength(label_text, font=font)
                ly = placer.place(side, target[1])
                if side == "left":
                    lx = 6
                    anchor = (lx + tw + 4, ly + line_h / 2)
                else:
                    lx = W - tw - 8
                    anchor = (lx - 4, ly + line_h / 2)
                # leader / arrowhead from the label to the mark
                if spec["form"] == "arrow":
                    _draw_arrowhead(draw, anchor, target, color, 2)
                else:
                    draw.line([anchor, target], fill=color, width=1)
                _text_chip(draw, (lx, ly), label_text, font, color)
            elif spec["form"] == "arrow":
                _draw_arrowhead(draw, (target[0] + 40, target[1]), target, color, 2)

            marks_audit.append({"form": spec["form"], "label": spec["label"],
                                "certainty": spec["certainty"], "number_shown": bool(show_number)})
        except Exception as exc:  # noqa: BLE001 - one bad mark must not kill the figure
            logger.warning("annotation mark failed to render (%s): %s", spec.get("form"), exc)

    if title:
        _text_chip(draw, (5, 3), title, title_font, (255, 255, 255))
    if legend and used_certainties:
        _draw_legend(draw, used_certainties, font, H)

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    base.save(out_path)
    return {
        "out_path": out_path,
        "rendered": len(marks_audit),
        "dropped": [{"label": d["label"], "significance": d["significance"]} for d in dropped],
        "marks": marks_audit,
    }


def _text_chip(draw, xy, text, font, color):
    """Text on a black plate so it stays legible over any anatomy."""
    x, y = xy
    bbox = draw.textbbox((x, y), text, font=font)
    draw.rectangle([bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1], fill="black")
    draw.text((x, y), text, fill=color, font=font)


def _draw_legend(draw, certainties, font, H):
    """Colour → certainty key at the bottom-left."""
    items = [k for k in CERTAINTY_ORDER if k in certainties]
    x, y = 6, H - 20
    draw.rectangle([x - 2, y - 2, x + 2 + sum(int(draw.textlength(k, font=font)) + 26 for k in items), y + 16],
                   fill="black")
    for k in items:
        sw = CERTAINTY_RGB255[k]
        draw.rectangle([x, y, x + 12, y + 12], fill=sw)
        draw.text((x + 16, y - 1), k, fill=sw, font=font)
        x += 16 + int(draw.textlength(k, font=font)) + 12

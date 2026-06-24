"""Focused, minimal-prompt disc-level localizer.

Measured 2026-06-24: the heavily-prompted main agent overshot L5-S1 *into the sacrum* on a
real study, while a naked 3-line Claude-vision call placed every level correctly (above the
sacrum, evenly spaced) and did so consistently across four slices. The lesson: per-disc
localization degrades when buried in the mega-prompt; a focused call does it better.

So for level placement we run ONE small focused vision call on the image the agent already
chose, and SNAP the agent's level-named marks onto its coordinates. The agent picks the
right image and decides the findings; the localizer fixes only the *where* of each level.
Everything here degrades gracefully — any failure returns empty and the agent's own coords
are kept.
"""
from __future__ import annotations

import json
import logging
import subprocess
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mika.disc_localizer")

# A vertebral endplate token (T12, L1..L5, S1/S2); a disc LEVEL is two adjacent tokens.
_VERT = r"(?:T1[0-2]|T\d|L[1-5]|S[12])"
_LEVEL_RE = re.compile(rf"({_VERT})\s*[-–—/]\s*({_VERT})", re.IGNORECASE)


def level_token(text) -> Optional[str]:
    """Extract a normalized level like 'L5-S1' from a label/key, else None."""
    if not isinstance(text, str):
        return None
    m = _LEVEL_RE.search(text)
    return f"{m.group(1).upper()}-{m.group(2).upper()}" if m else None


def build_localizer_prompt(image_filename: str, w: int, h: int) -> str:
    """The minimal naked-vision prompt that out-localized the full pipeline in testing."""
    return (
        f"Read (view) the image file {image_filename}.\n\n"
        f"It is a sagittal (side-view) lumbar spine MRI, {w} px wide x {h} px tall. "
        f"Coordinates: x = column from the LEFT (0-{w - 1}), y = row from the TOP (0-{h - 1}).\n\n"
        "Give the CENTER pixel coordinate of each intervertebral disc space you can see. The "
        "lowest lumbar disc is L5-S1 - the disc space just ABOVE the sacrum (the large bone at "
        "the bottom angling backward). Count up: L5-S1, L4-L5, L3-L4, L2-L3, L1-L2, T12-L1 if visible.\n\n"
        "Return ONLY a JSON object, integer pixels in the given space, e.g.:\n"
        '{"L5-S1":[x,y],"L4-L5":[x,y],"L3-L4":[x,y],"L2-L3":[x,y],"L1-L2":[x,y]}\n'
        "Include only discs you can actually see."
    )


def parse_levels(text, w: int, h: int) -> dict:
    """Tolerant parse of the localizer reply into {level: (x, y)}, clamped in-bounds."""
    if not isinstance(text, str) or "{" not in text:
        return {}
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        raw = json.loads(text[s:e + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        lvl = level_token(k)
        if not lvl or not isinstance(v, (list, tuple)) or len(v) < 2:
            continue
        try:
            x, y = int(round(float(v[0]))), int(round(float(v[1])))
        except (TypeError, ValueError):
            continue
        out[lvl] = (max(0, min(w - 1, x)), max(0, min(h - 1, y)))
    return out


def snap_marks_to_levels(marks: list, level_coords: dict) -> int:
    """Recenter any mark whose label names a level onto the localizer's coordinate for that
    level (center/point moved, bbox shifted keeping its size). Returns the number snapped.

    Calipers (p0/p1) are left alone — snapping a measured span needs both endpoints and the
    span length is the point of a caliper.
    """
    if not level_coords or not isinstance(marks, list):
        return 0
    snapped = 0
    for m in marks:
        if not isinstance(m, dict):
            continue
        lvl = level_token(m.get("label"))
        if not lvl or lvl not in level_coords:
            continue
        cx, cy = level_coords[lvl]
        moved = False
        bbox = m.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            hw, hh = abs(bbox[2] - bbox[0]) / 2.0, abs(bbox[3] - bbox[1]) / 2.0
            m["bbox"] = [cx - hw, cy - hh, cx + hw, cy + hh]
            moved = True
        if isinstance(m.get("center"), (list, tuple)) and len(m["center"]) == 2:
            m["center"] = [cx, cy]
            moved = True
        if isinstance(m.get("point"), (list, tuple)) and len(m["point"]) == 2:
            m["point"] = [cx, cy]
            moved = True
        if not moved and "p0" not in m:   # no positional field at all → give it a center
            m["center"] = [cx, cy]
            moved = True
        snapped += 1 if moved else 0
    return snapped


def localize_levels(claude_bin, image_path, *, model, effort, permission_mode, env,
                    timeout: int = 120) -> dict:
    """Run ONE focused vision call to place disc centers on a sagittal image.

    Returns {level: (x, y)} or {} on any failure (never raises). Reuses the same headless
    `claude -p` mechanism + subscription auth as the main agent — no new dependency.
    """
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
        prompt = build_localizer_prompt(Path(image_path).name, w, h)
        cmd = [claude_bin, "-p", "--output-format", "json", "--model", model,
               "--effort", effort, "--permission-mode", permission_mode,
               "--add-dir", str(Path(image_path).parent)]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=timeout)
        text = proc.stdout or ""
        try:
            text = json.loads(text.strip()).get("result", "") or ""
        except (ValueError, AttributeError):
            pass   # not the JSON envelope — parse the raw text
        coords = parse_levels(text, w, h)
        logger.info("Disc localizer placed %d level(s) on %s", len(coords), Path(image_path).name)
        return coords
    except Exception as e:  # noqa: BLE001 — localization must never break the report
        logger.warning("Disc localizer failed (keeping agent coords): %s", e)
        return {}

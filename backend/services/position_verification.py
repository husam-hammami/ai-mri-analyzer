"""Universal annotation position gate.

A pinpoint marker is only honest when an INDEPENDENT signal confirms its position. With no
independent localizer (common for non-spine / non-DICOM), or when the localizer disagrees,
or when the spine level identity is not anchored to the sacrum, the mark degrades to an
honest region band at lower certainty instead of shipping a confident wrong pinpoint.

This is anatomy-agnostic: the caller supplies whatever independent localizer it has, and
the decision is pure data (unit-testable, no I/O).
"""
from __future__ import annotations

import math
import re
from typing import Optional

PINPOINT = "pinpoint"
REGION_BAND = "region_band"
DROP = "drop"


def _norm_level(value) -> Optional[str]:
    if not value:
        return None
    return re.sub(r"\s+", "", str(value)).upper()


def _norm_side(value) -> Optional[str]:
    if not value:
        return None
    v = str(value).strip().lower()
    if v.startswith("l"):
        return "left"
    if v.startswith("r"):
        return "right"
    return None


def _point(obj) -> Optional[tuple]:
    if not isinstance(obj, dict):
        return None
    p = obj.get("point")
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        return float(p[0]), float(p[1])
    if obj.get("col") is not None and obj.get("row") is not None:
        return float(obj["col"]), float(obj["row"])
    return None


def verify_annotation_position(
    annotation: dict,
    independent_localizer: Optional[dict] = None,
    anchor_check: Optional[dict] = None,
    tolerance_px: float = 12.0,
) -> dict:
    """Decide whether an annotation may be a pinpoint or must degrade to a region band.

    ``annotation``: {level?, side?, col/row or point}.
    ``independent_localizer``: an independent localization signal or None. It must set
        ``allows_pinpoint`` (the trust gate). It may carry either an explicit
        ``{"agrees": bool}`` (a frame-level signal, e.g. an L5-S1 cross-check) or a
        ``{level, side, point}`` to compare against the annotation.
    ``anchor_check``: a structure-identity result (e.g. verify_level_identity). If its
        ``anchored`` is False, no pinpoint is allowed — the off-by-one veto.
    Returns ``{decision, certainty, agreement, reasons}``.
    """
    reasons: list[str] = []

    # 1. Spine anchor failed → the level frame itself is suspect; never pinpoint.
    if isinstance(anchor_check, dict) and anchor_check.get("anchored") is False:
        reasons.append("level identity is not anchored to the sacrum (off-by-one risk)")
        return {"decision": REGION_BAND, "certainty": "low", "agreement": "anchor_failed", "reasons": reasons}

    # 2. No independent localizer → never pinpoint on a single source.
    if not independent_localizer:
        reasons.append("no independent localizer — a single-source mark cannot be a pinpoint")
        return {"decision": REGION_BAND, "certainty": "moderate", "agreement": "unavailable", "reasons": reasons}

    # 3. The localizer must itself be trusted to support a pinpoint.
    if not independent_localizer.get("allows_pinpoint", False):
        src = independent_localizer.get("source", "localizer")
        reasons.append(f"{src} is not trusted for a pinpoint marker")
        return {"decision": REGION_BAND, "certainty": "low", "agreement": "localizer_untrusted", "reasons": reasons}

    src = independent_localizer.get("source", "localizer")

    # 4a. Frame-level agreement signal (a yes/no confirmation of the level frame).
    if "agrees" in independent_localizer:
        if independent_localizer["agrees"]:
            reasons.append(f"{src} confirms the level frame")
            return {"decision": PINPOINT, "certainty": "high", "agreement": "agree", "reasons": reasons}
        reasons.append(f"{src} disagrees with the level frame (off-by-one veto)")
        return {"decision": REGION_BAND, "certainty": "low", "agreement": "disagree", "reasons": reasons}

    # 4b. Point / level / side comparison.
    a_level, l_level = _norm_level(annotation.get("level")), _norm_level(independent_localizer.get("level"))
    if a_level and l_level and a_level != l_level:
        reasons.append(f"level mismatch: annotation {a_level} vs localizer {l_level}")
        return {"decision": REGION_BAND, "certainty": "low", "agreement": "disagree", "reasons": reasons}
    a_side, l_side = _norm_side(annotation.get("side")), _norm_side(independent_localizer.get("side"))
    if a_side and l_side and a_side != l_side:
        reasons.append(f"side mismatch: annotation {a_side} vs localizer {l_side}")
        return {"decision": REGION_BAND, "certainty": "low", "agreement": "disagree", "reasons": reasons}
    pa, pl = _point(annotation), _point(independent_localizer)
    if pa and pl:
        dist = math.hypot(pa[0] - pl[0], pa[1] - pl[1])
        if dist > tolerance_px:
            reasons.append(f"localizer disagrees on position ({dist:.0f}px > {tolerance_px:.0f}px)")
            return {"decision": REGION_BAND, "certainty": "low", "agreement": "disagree", "reasons": reasons}

    reasons.append(f"{src} agrees on level/side/position")
    return {"decision": PINPOINT, "certainty": "high", "agreement": "agree", "reasons": reasons}

"""Shared certainty palette — ONE source of truth.

Color encodes CERTAINTY (not severity). The on-image annotation marks, the figure legend,
and the patient-report certainty chips all read from here so they can never drift apart.
RGB is stored as floats in 0-1 (what report_builder's reportlab colors expect); a 0-255
helper is provided for Pillow rendering.
"""
from typing import Optional

# Certainty -> RGB (0-1). Confirmed = full accent #2563EB; Likely = reduced accent;
# Possible = neutral slate. A normal "reference" mark is muted gray (neutral, not a claim).
CERTAINTY_COLOR = {
    "Confirmed": (0.145, 0.388, 0.922),   # full accent #2563EB
    "Likely":    (0.451, 0.557, 0.863),   # reduced-opacity accent
    "Possible":  (0.553, 0.604, 0.690),   # neutral slate
    "Reference": (0.278, 0.333, 0.412),   # muted slate — "normal for comparison"
}
MUTED = (0.278, 0.333, 0.412)

# Order for legends (most to least certain; Reference last).
CERTAINTY_ORDER = ["Confirmed", "Likely", "Possible", "Reference"]

_SYNONYMS = {
    "confirmed": "Confirmed", "definite": "Confirmed", "tier a": "Confirmed", "high": "Confirmed",
    "likely": "Likely", "probable": "Likely", "tier b": "Likely", "moderate": "Likely",
    "possible": "Possible", "suggestive": "Possible", "tier c": "Possible", "low": "Possible",
    "reference": "Reference", "normal": "Reference", "normal for comparison": "Reference",
}


def normalize_certainty(value) -> str:
    """Map a free-text certainty word to a canonical palette key (defaults to Possible)."""
    if not value:
        return "Possible"
    return _SYNONYMS.get(str(value).strip().lower(), "Possible")


def rgb255(rgb01) -> tuple:
    """Convert an (r, g, b) 0-1 tuple to 0-255 ints for Pillow."""
    return tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb01)


CERTAINTY_RGB255 = {k: rgb255(v) for k, v in CERTAINTY_COLOR.items()}


def certainty_rgb255(value) -> tuple:
    """Pillow 0-255 color for a (possibly free-text) certainty word."""
    return CERTAINTY_RGB255[normalize_certainty(value)]

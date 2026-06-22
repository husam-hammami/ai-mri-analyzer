"""Operative-note discrepancy engine (deterministic, the reconciliation moat).

Parses an operative / surgical note into structured facts and surfaces contradictions:
  (a) WITHIN the note  — the level named on the procedure line vs the level the narrative
      describes; a "complications: none" statement vs a complication documented in the body.
  (b) BETWEEN the note and MIKA's independent image read — level / side mismatches.

These are exactly the catches that made the March reconciliation strong (the L4-L5 vs L5-S1
level contradiction; "complications: none" alongside an undocumented dural tear). Pure text
logic — no image read. This module only FLAGS discrepancies (stated plainly, confidence-forward);
it never edits the blind read. PHI (the raw note) is not stored — only the discrepancy statements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from services.reconciliation import LEVEL_RE, SIDE_RE  # reuse the shared patterns

PROCEDURE_TERMS = {
    "discectomy": ("discectomy", "microdiscectomy", "diskectomy"),
    "laminectomy": ("laminectomy", "hemilaminectomy", "laminotomy"),
    "fusion": ("fusion", "tlif", "plif", "alif", "arthrodesis", "interbody"),
    "decompression": ("decompression", "foraminotomy"),
}
_ALL_PROC_TERMS = tuple(t for terms in PROCEDURE_TERMS.values() for t in terms)

COMPLICATION_TERMS = (
    "dural tear", "durotomy", "dural defect", "csf leak", "cerebrospinal fluid leak",
    "pseudomeningocele", "nerve root injury", "nerve injury", "hemorrhage", "hematoma", "infection",
)
COMPLICATIONS_NONE_RE = re.compile(r"complications?\s*[:\-]?\s*(none|nil|no\b|without\b)", re.I)
READ_SURGICAL_TERMS = (
    "postoperative",
    "post-operative",
    "post surgical",
    "postsurgical",
    "post-surgical",
    "operative",
    "surgery",
    "surgical",
    "laminectomy",
    "hemilaminectomy",
    "laminotomy",
    "discectomy",
    "diskectomy",
    "microdiscectomy",
    "decompression",
    "foraminotomy",
    "fusion",
)


def _norm_level(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").upper()).replace("/", "-")


@dataclass
class OpNoteFacts:
    procedures: list = field(default_factory=list)            # e.g. ["discectomy"]
    levels: list = field(default_factory=list)                # normalized, e.g. ["L5-S1"]
    sides: list = field(default_factory=list)                 # ["left"]
    complications_stated_none: bool = False
    complications_found: list = field(default_factory=list)   # terms documented in the body


@dataclass
class Discrepancy:
    kind: str           # intra_op_level | complication_mismatch | op_vs_read_level | op_vs_read_side
    detail: str         # plain, confident statement
    severity: str = "flag"


def parse_operative_note(text: str) -> OpNoteFacts:
    t = text or ""
    low = t.lower()
    procedures = sorted({name for name, terms in PROCEDURE_TERMS.items()
                         if any(term in low for term in terms)})
    levels = []
    for m in LEVEL_RE.finditer(t):
        lv = _norm_level(m.group(1))
        if lv not in levels:
            levels.append(lv)
    sides = sorted({m.group(1).lower() for m in SIDE_RE.finditer(t)})
    comps = sorted({c for c in COMPLICATION_TERMS if c in low})
    return OpNoteFacts(
        procedures=procedures, levels=levels, sides=sides,
        complications_stated_none=bool(COMPLICATIONS_NONE_RE.search(t)),
        complications_found=comps,
    )


def _iter_summary_text(summary: Any) -> Iterable[str]:
    if isinstance(summary, dict):
        for key in (
            "findings",
            "findings_by_level",
            "findings_by_region",
            "impression",
            "discrepancies",
            "post_surgical_assessment",
        ):
            yield from _iter_summary_text(summary.get(key))
        patient = summary.get("patient")
        if isinstance(patient, dict):
            yield from _iter_summary_text(patient.get("findings"))
            yield from _iter_summary_text(patient.get("bottom_line"))
            yield from _iter_summary_text(patient.get("key_points"))
        for key in ("text", "plain", "caption", "finding", "description"):
            value = summary.get(key)
            if isinstance(value, str) and value.strip():
                yield value
    elif isinstance(summary, list):
        for item in summary:
            yield from _iter_summary_text(item)
    elif isinstance(summary, str) and summary.strip():
        yield summary


def extract_read_surgical_level_side(summary: Any) -> tuple[list[str], list[str]]:
    """Extract the read's post-surgical level/side facts for op-note comparison.

    Only rows that mention surgery/postoperative anatomy are treated as surgical-level
    evidence. This keeps an unrelated degenerative finding elsewhere from triggering an
    op-vs-read mismatch.
    """
    levels: list[str] = []
    sides: list[str] = []
    for text in _iter_summary_text(summary):
        low = text.lower()
        if not any(term in low for term in READ_SURGICAL_TERMS):
            continue
        for match in LEVEL_RE.finditer(text):
            level = _norm_level(match.group(1))
            if level not in levels:
                levels.append(level)
        for match in SIDE_RE.finditer(text):
            side = match.group(1).lower()
            if side not in sides:
                sides.append(side)
    return levels, sides


def _procedure_line_level(text: str):
    """Return (level named on the procedure line, [levels described elsewhere in the note])."""
    proc_level = None
    narr_levels = []
    for ln in (text or "").splitlines():
        low = ln.lower()
        m = LEVEL_RE.search(ln)
        lvn = _norm_level(m.group(1)) if m else None
        is_proc_line = ("procedure" in low or any(p in low for p in _ALL_PROC_TERMS))
        if is_proc_line and lvn and proc_level is None:
            proc_level = lvn
        elif lvn and lvn not in narr_levels:
            narr_levels.append(lvn)
    return proc_level, narr_levels


def detect_op_note_contradictions(facts: OpNoteFacts, raw_text: str) -> list:
    out = []
    if facts.complications_stated_none and facts.complications_found:
        out.append(Discrepancy(
            kind="complication_mismatch",
            detail=("The operative note records complications as none, but the body documents "
                    + ", ".join(facts.complications_found) + "."),
            severity="important",
        ))
    proc_level, narr_levels = _procedure_line_level(raw_text)
    if proc_level and narr_levels and proc_level not in narr_levels:
        out.append(Discrepancy(
            kind="intra_op_level",
            detail=(f"The procedure is named at {proc_level}, but the operative narrative describes "
                    f"{', '.join(narr_levels)} — verify the operative level."),
            severity="important",
        ))
    return out


def reconcile_op_note_with_read(facts: OpNoteFacts, read_levels=None, read_sides=None) -> list:
    out = []
    rl = {_norm_level(x) for x in (read_levels or []) if x}
    if facts.levels and rl and not (set(facts.levels) & rl):
        out.append(Discrepancy(
            kind="op_vs_read_level",
            detail=(f"The operative note is at {', '.join(facts.levels)}, but the image read's "
                    f"surgical-level findings are at {', '.join(sorted(rl))}."),
        ))
    rs = {s.lower() for s in (read_sides or []) if s}
    if facts.sides and rs and not (set(facts.sides) & rs):
        out.append(Discrepancy(
            kind="op_vs_read_side",
            detail=(f"The operative note is {', '.join(facts.sides)}-sided, but the read describes "
                    f"{', '.join(sorted(rs))}."),
        ))
    return out


def op_note_discrepancies(surgical_notes: Optional[str], read_levels=None, read_sides=None) -> list:
    """Public entry point: structured op-note discrepancy statements (PHI-safe strings) for the
    reconciliation section. Returns [] if no note. Stated confidently — flags, never edits the read."""
    if not surgical_notes or not surgical_notes.strip():
        return []
    facts = parse_operative_note(surgical_notes)
    found = detect_op_note_contradictions(facts, surgical_notes)
    found += reconcile_op_note_with_read(facts, read_levels, read_sides)
    return [d.detail for d in found]


def merge_into_summary(summary, surgical_notes, read_levels=None, read_sides=None):
    """Append deterministic op-note discrepancies into summary['discrepancies'] (deduped, order
    preserved). Mutates and returns the summary dict; no-op on a non-dict or empty note."""
    if not isinstance(summary, dict):
        return summary
    extra = op_note_discrepancies(surgical_notes, read_levels, read_sides)
    if not extra:
        return summary
    existing = summary.get("discrepancies")
    if not isinstance(existing, list):
        existing = [] if existing in (None, "") else [str(existing)]
    seen = {str(x).strip().lower() for x in existing}
    for s in extra:
        key = s.strip().lower()
        if key not in seen:
            existing.append(s)
            seen.add(key)
    summary["discrepancies"] = existing
    return summary

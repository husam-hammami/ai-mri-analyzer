"""
Reference-assisted discrepancy reconciliation.

This module intentionally does not alter the blind image read. It extracts structured
targets from a user-provided reference report, compares those targets with MIKA's
independent findings, and returns a separate reconciliation section.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.sax.saxutils import escape


AGREEMENT_STATUSES = {
    "confirmed",
    "supported_by_focused_evidence",
    "partially_supported",
    "not_independently_seen",
    "conflicts_with_reference",
    "cannot_assess",
}

MAX_REFERENCE_REPORT_BYTES = 25 * 1024 * 1024
REFERENCE_SUFFIXES = {".pdf", ".txt", ".md", ".rtf"}

LEVEL_RE = re.compile(r"\b([CLT]\d{1,2}\s*[-/]\s*[CLT]?\d{1,2}|L\d\s*[-/]\s*S\d)\b", re.I)
SIDE_RE = re.compile(r"\b(left|right|bilateral)\b", re.I)

POSTSURGICAL_TERMS = (
    "postoperative",
    "post-operative",
    "post surgical",
    "postsurgical",
    "post-surgical",
    "surgery",
    "laminectomy",
    "hemilaminectomy",
    "discectomy",
    "microdiscectomy",
)
SCAR_DISC_TERMS = (
    "scar",
    "fibrosis",
    "fibrotic",
    "enhancement",
    "residual",
    "recurrent",
    "recurrence",
    "disc",
    "disk",
)
LATERAL_RECESS_TERMS = ("lateral recess", "subarticular")
FORAMINAL_TERMS = ("foraminal", "foramen", "foramina")
NERVE_ROOT_TERMS = (
    "nerve root",
    "root",
    "descending",
    "exiting",
    "s1",
    "l5",
    "impingement",
    "encasement",
    "contact",
    "displacement",
    "compression",
)
NEGATION_TERMS = (
    "no ",
    "without ",
    "not ",
    "absent",
    "negative for",
    "free of",
    "does not show",
    "did not show",
    "no definite",
    "no evidence of",
)
TEMPORAL_IMPROVED_TERMS = (
    "improved",
    "decreased",
    "less",
    "resolved",
    "decompressed",
    "removed",
    "relieved",
    "widely patent",
)
TEMPORAL_PROGRESS_TERMS = (
    "worse",
    "worsened",
    "progressed",
    "progression",
    "increased",
    "larger",
    "new",
    "severe",
    "marked",
)
TEMPORAL_ABNORMAL_TERMS = (
    "stenosis",
    "narrowing",
    "compression",
    "impingement",
    "encasement",
    "contact",
    "disc",
    "disk",
    "herniation",
    "protrusion",
    "extrusion",
    "scar",
    "fibrosis",
    "enhancement",
    "occupied",
    "effacement",
    "lateral recess",
    "foraminal",
)


class ReferenceInputError(ValueError):
    """Raised when a requested local reference report cannot be read safely."""


@dataclass
class ReferenceTarget:
    reference_finding: str
    anatomy: str
    level: str
    side: str
    modality_sequence_needed: str
    evidence_refs: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)


@dataclass
class ReconciliationItem:
    reference_finding: str
    mika_blind_finding: str
    anatomy: str
    level: str
    side: str
    modality_sequence_needed: str
    evidence_refs: list[str]
    agreement_status: str
    patient_explanation: str
    clinician_explanation: str


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    t = text.lower()
    return any(term in t for term in terms)


def _normalize_level(level: str) -> str:
    value = _norm(level).upper().replace(" ", "").replace("/", "-")
    value = re.sub(r"^([CLT])(\d+)-(\d+)$", r"\1\2-\1\3", value)
    return value


def _sentence_window(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.;:\n])\s+", text)
    return [_norm(c) for c in chunks if _norm(c)]


def read_reference_report_text(reference_path: str | Path) -> str:
    """Read a local reference report without copying it into the job directory."""
    path = Path(reference_path).expanduser()
    if not path.exists() or not path.is_file():
        raise ReferenceInputError("Reference report path does not exist or is not a file.")
    suffix = path.suffix.lower()
    if suffix not in REFERENCE_SUFFIXES:
        raise ReferenceInputError("Reference report must be a PDF or text file.")
    try:
        if path.stat().st_size > MAX_REFERENCE_REPORT_BYTES:
            raise ReferenceInputError("Reference report is too large to process safely.")
    except OSError as exc:
        raise ReferenceInputError(f"Could not inspect reference report: {exc}") from exc

    if suffix == ".pdf":
        return _read_pdf_text(path)

    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ReferenceInputError(f"Could not read reference report: {exc}") from exc


def read_reference_report_bytes(filename: str, data: bytes) -> str:
    """Extract reference text from an uploaded file without writing it to disk."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in REFERENCE_SUFFIXES:
        raise ReferenceInputError("Reference report must be a PDF or text file.")
    if len(data) > MAX_REFERENCE_REPORT_BYTES:
        raise ReferenceInputError("Reference report is too large to process safely.")
    if suffix == ".pdf":
        return _read_pdf_text_from_bytes(data)
    return data.decode("utf-8-sig", errors="replace")


def _read_pdf_text(path: Path) -> str:
    data = path.read_bytes()
    return _extract_pdf_text(data=data, source_path=path)


def _read_pdf_text_from_bytes(data: bytes) -> str:
    return _extract_pdf_text(data=data, source_path=None)


def _extract_pdf_text(*, data: bytes, source_path: Optional[Path]) -> str:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional install
        fitz_error = exc
    else:
        try:
            if source_path:
                doc = fitz.open(str(source_path))
            else:
                doc = fitz.open(stream=data, filetype="pdf")
            with doc:
                return "\n".join(doc.load_page(i).get_text("text") for i in range(min(25, doc.page_count)))
        except Exception as exc:
            raise ReferenceInputError(f"Could not extract text from reference PDF: {exc}") from exc

    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        pass
    else:  # pragma: no cover - depends on optional install
        try:
            reader = PdfReader(str(source_path) if source_path else BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:25])
        except Exception as exc:
            raise ReferenceInputError(f"Could not extract text from reference PDF: {exc}") from exc

    try:
        return _read_pdf_text_basic(data)
    except Exception as exc:
        raise ReferenceInputError(
            "Could not extract text from reference PDF. Try pasting the report text instead."
        ) from fitz_error or exc


def _read_pdf_text_basic(data: bytes) -> str:
    """Best-effort text extraction for simple text-layer PDFs when no PDF library is installed."""
    parts: list[str] = []
    for match in re.finditer(rb"(<<.*?>>)?\s*stream\r?\n(.*?)\r?\nendstream", data, re.S):
        dictionary = match.group(1) or b""
        stream = match.group(2)
        if b"FlateDecode" in dictionary:
            try:
                stream = zlib.decompress(stream)
            except Exception:
                continue
        try:
            parts.append(stream.decode("latin-1", errors="ignore"))
        except Exception:
            continue
    if not parts:
        parts.append(data.decode("latin-1", errors="ignore"))
    text = "\n".join(_pdf_text_strings(part) for part in parts)
    return _norm(text)


def _pdf_text_strings(content: str) -> str:
    strings: list[str] = []
    for raw in re.findall(r"\((?:\\.|[^\\()])*\)\s*Tj", content):
        strings.append(_decode_pdf_literal(raw.rsplit(")", 1)[0][1:]))
    for array in re.findall(r"\[(.*?)\]\s*TJ", content, flags=re.S):
        for raw in re.findall(r"\((?:\\.|[^\\()])*\)", array):
            strings.append(_decode_pdf_literal(raw[1:-1]))
        for raw in re.findall(r"<([0-9A-Fa-f\s]+)>", array):
            try:
                strings.append(bytes.fromhex(re.sub(r"\s+", "", raw)).decode("utf-16-be", errors="ignore"))
            except Exception:
                try:
                    strings.append(bytes.fromhex(re.sub(r"\s+", "", raw)).decode("latin-1", errors="ignore"))
                except Exception:
                    pass
    return " ".join(s for s in strings if s)


def _decode_pdf_literal(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in {"n", "r", "t", "b", "f"}:
            return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}[token]
        if token in {"\\", "(", ")"}:
            return token
        if re.fullmatch(r"[0-7]{1,3}", token):
            return chr(int(token, 8))
        return token

    return re.sub(r"\\([nrtbf\\()]|[0-7]{1,3})", repl, value)


def extract_reference_targets(reference_text: str) -> list[dict[str, Any]]:
    """Extract structured, PHI-safe reference targets from report/context text."""
    text = _norm(reference_text)
    if not text:
        return []

    targets: list[ReferenceTarget] = []
    lower = text.lower()
    l5s1_present = "l5-s1" in lower or "l5/s1" in lower or "l5 s1" in lower
    feb_p0_present = (
        l5s1_present
        and "left" in lower
        and _contains_any(lower, POSTSURGICAL_TERMS)
        and (
            _contains_any(lower, LATERAL_RECESS_TERMS)
            or _contains_any(lower, SCAR_DISC_TERMS)
            or _contains_any(lower, NERVE_ROOT_TERMS)
        )
    )
    if feb_p0_present:
        targets.append(ReferenceTarget(
            reference_finding=(
                "The reference report describes left L5-S1 postoperative lateral recess/scar "
                "or residual-recurrent disc pattern with nerve-root involvement."
            ),
            anatomy="spine",
            level="L5-S1",
            side="left",
            modality_sequence_needed=(
                "Lumbar MRI axial and sagittal T1/T2 plus post-contrast fat-suppressed images"
            ),
            concepts=[
                "post_surgical",
                "scar_or_residual_recurrent_disc",
                "lateral_recess",
                "nerve_root",
            ],
        ))

    seen = {_norm_key(t.reference_finding) for t in targets}
    for sentence in _sentence_window(text):
        level_match = LEVEL_RE.search(sentence)
        if not level_match:
            continue
        sentence_lower = sentence.lower()
        if not (
            _contains_any(sentence_lower, SCAR_DISC_TERMS)
            or _contains_any(sentence_lower, LATERAL_RECESS_TERMS)
            or _contains_any(sentence_lower, FORAMINAL_TERMS)
            or _contains_any(sentence_lower, NERVE_ROOT_TERMS)
        ):
            continue
        level = _normalize_level(level_match.group(1))
        side_match = SIDE_RE.search(sentence)
        side = side_match.group(1).lower() if side_match else ""
        concepts = _concepts_for_text(sentence_lower)
        target = ReferenceTarget(
            reference_finding=_make_reference_finding(sentence, level=level, side=side, concepts=concepts),
            anatomy="spine" if level.startswith(("L", "T", "C")) else "unknown",
            level=level,
            side=side,
            modality_sequence_needed=_sequence_need_for_level(level),
            concepts=concepts,
        )
        key = _norm_key(target.reference_finding)
        if key not in seen:
            targets.append(target)
            seen.add(key)

    return [asdict(t) for t in targets]


def _concepts_for_text(text: str) -> list[str]:
    concepts = []
    if _contains_any(text, POSTSURGICAL_TERMS):
        concepts.append("post_surgical")
    if _contains_any(text, SCAR_DISC_TERMS):
        concepts.append("scar_or_residual_recurrent_disc")
    if _contains_any(text, LATERAL_RECESS_TERMS):
        concepts.append("lateral_recess")
    if _contains_any(text, FORAMINAL_TERMS):
        concepts.append("foraminal")
    if _contains_any(text, NERVE_ROOT_TERMS):
        concepts.append("nerve_root")
    return concepts


def _make_reference_finding(sentence: str, *, level: str, side: str, concepts: list[str]) -> str:
    if level == "L5-S1" and side == "left" and {"post_surgical", "scar_or_residual_recurrent_disc"} & set(concepts):
        return (
            "The reference report describes left L5-S1 postoperative lateral recess/scar "
            "or residual-recurrent disc pattern with nerve-root involvement."
        )
    side_part = f"{side} " if side else ""
    concept_part = ", ".join(c.replace("_", " ") for c in concepts) or "reported abnormality"
    return f"The reference report describes {side_part}{level} {concept_part}."


def _sequence_need_for_level(level: str) -> str:
    if level.startswith(("L", "T", "C")):
        return "Cross-sectional spine MRI sequences matched to the reported level and side"
    return "Relevant diagnostic imaging sequences for the reported anatomy"


def _iter_report_text(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key in (
            "findings",
            "findings_by_level",
            "findings_by_region",
            "impression",
            "incidentals",
            "discrepancies",
            "post_surgical_assessment",
            "bottom_line",
            "key_points",
            "what_it_means",
        ):
            yield from _iter_report_text(value.get(key))
        patient = value.get("patient")
        if isinstance(patient, dict):
            yield from _iter_report_text(patient)
        for key in ("text", "plain", "caption", "finding", "description", "summary"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                yield text
    elif isinstance(value, list):
        for item in value:
            yield from _iter_report_text(item)
    elif isinstance(value, str) and value.strip():
        yield value


def _temporal_state(text: str) -> str:
    low = text.lower()
    negated = _contains_any(low, NEGATION_TERMS)
    if _contains_any(low, TEMPORAL_IMPROVED_TERMS):
        return "improved"
    if negated and _contains_any(low, TEMPORAL_ABNORMAL_TERMS):
        return "absent"
    if _contains_any(low, TEMPORAL_PROGRESS_TERMS):
        return "progressed"
    if _contains_any(low, TEMPORAL_ABNORMAL_TERMS):
        return "abnormal"
    return "mentioned"


def extract_temporal_findings(source: Any, *, source_label: str = "") -> list[dict[str, Any]]:
    """Extract PHI-safe level/side finding rows for longitudinal comparison."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for text in _iter_report_text(source):
        for sentence in _sentence_window(text):
            level_match = LEVEL_RE.search(sentence)
            if not level_match:
                continue
            low = sentence.lower()
            if not (
                _contains_any(low, TEMPORAL_ABNORMAL_TERMS)
                or _contains_any(low, TEMPORAL_IMPROVED_TERMS)
                or _contains_any(low, TEMPORAL_PROGRESS_TERMS)
            ):
                continue
            level = _normalize_level(level_match.group(1))
            side_match = SIDE_RE.search(sentence)
            side = side_match.group(1).lower() if side_match else ""
            concepts = _concepts_for_text(low)
            state = _temporal_state(sentence)
            key = (level, side, ",".join(concepts), state)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "level": level,
                "side": side,
                "key": f"{level}|{side or 'unspecified'}",
                "concepts": concepts,
                "state": state,
                "source_label": source_label,
                "text": sentence,
            })
    return rows


def _temporal_status(prior_state: str, current_state: str) -> Optional[str]:
    prior_abnormal = prior_state in {"abnormal", "progressed", "mentioned"}
    current_abnormal = current_state in {"abnormal", "progressed", "mentioned"}
    if prior_abnormal and current_state in {"absent", "improved"}:
        return "resolved"
    if prior_state in {"absent", "improved"} and current_abnormal:
        return "new"
    if prior_abnormal and current_state == "progressed":
        return "progressed"
    if prior_state == "abnormal" and current_state == "abnormal":
        return None
    return None


def build_structured_change_over_time(
    *,
    current_summary: dict[str, Any],
    prior_reports: Optional[str] = None,
    prior_summaries: Optional[list[dict[str, Any]]] = None,
    prior_label: str = "prior",
    current_label: str = "current",
) -> dict[str, Any]:
    """Compare prior and current spine findings by level+side.

    The function is intentionally conservative: it only emits new/resolved/progressed
    rows when both timepoints contain a level-keyed finding. It does not read or copy
    prior images, and it never edits the blind read.
    """
    prior_rows: list[dict[str, Any]] = []
    if prior_reports:
        prior_rows.extend(extract_temporal_findings(prior_reports, source_label=prior_label))
    for idx, summary in enumerate(prior_summaries or []):
        prior_rows.extend(extract_temporal_findings(summary, source_label=f"{prior_label} {idx + 1}"))
    current_rows = extract_temporal_findings(current_summary or {}, source_label=current_label)
    if not prior_rows or not current_rows:
        return {"used": bool(prior_rows or current_rows), "items": [], "points": [], "source": "deterministic_temporal_delta"}

    by_prior: dict[str, list[dict[str, Any]]] = {}
    by_current: dict[str, list[dict[str, Any]]] = {}
    for row in prior_rows:
        by_prior.setdefault(row["key"], []).append(row)
    for row in current_rows:
        by_current.setdefault(row["key"], []).append(row)

    items: list[dict[str, Any]] = []
    for key in sorted(set(by_prior) & set(by_current)):
        for prior in by_prior[key]:
            current = max(
                by_current[key],
                key=lambda row: len(set(row.get("concepts") or []) & set(prior.get("concepts") or [])),
            )
            status = _temporal_status(prior.get("state", ""), current.get("state", ""))
            if not status:
                continue
            side = f"{prior.get('side')} " if prior.get("side") else ""
            point = (
                f"{prior.get('level')} {side}".strip()
                + f": {prior.get('state')} on prior comparison -> {current.get('state')} now ({status})."
            )
            items.append({
                "status": status,
                "level": prior.get("level", ""),
                "side": prior.get("side", ""),
                "key": key,
                "prior_state": prior.get("state", ""),
                "current_state": current.get("state", ""),
                "prior_text": prior.get("text", ""),
                "current_text": current.get("text", ""),
                "point": point,
            })

    return {
        "used": bool(items),
        "source": "deterministic_temporal_delta",
        "items": items,
        "points": [item["point"] for item in items],
    }


def merge_change_over_time(summary: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """Merge deterministic temporal rows into the patient change_over_time block."""
    if not isinstance(summary, dict) or not change or not change.get("items"):
        return summary
    patient = dict(summary.get("patient") or {})
    existing = patient.get("change_over_time")
    if not isinstance(existing, dict):
        existing = {}
    points = list(existing.get("points") or [])
    seen = {str(p).strip().lower() for p in points}
    for point in change.get("points") or []:
        key = str(point).strip().lower()
        if key and key not in seen:
            points.append(str(point))
            seen.add(key)
    existing["points"] = points
    existing["items"] = change.get("items") or []
    existing["source"] = change.get("source")
    patient["change_over_time"] = existing
    summary["patient"] = patient
    summary["change_over_time"] = change
    return summary


def build_reference_reconciliation(
    *,
    blind_summary: dict[str, Any],
    reference_text: Optional[str] = None,
    reference_path: Optional[str | Path] = None,
    evidence_manifest: Optional[dict[str, Any]] = None,
    source_label: str = "Reference report",
) -> dict[str, Any]:
    if reference_text is None and reference_path:
        reference_text = read_reference_report_text(reference_path)
    targets = extract_reference_targets(reference_text or "")
    items = reconcile_reference_targets(targets, blind_summary, evidence_manifest=evidence_manifest)
    counts = Counter(item["agreement_status"] for item in items)
    has_discrepancy = any(
        item["agreement_status"] in {"not_independently_seen", "conflicts_with_reference", "cannot_assess"}
        for item in items
    )
    patient_items = [
        {
            "status": item["agreement_status"],
            "label": _patient_status_label(item["agreement_status"]),
            "reference": _patient_reference_phrase(item),
            "mika": _patient_mika_phrase(item),
            "explanation": item["patient_explanation"],
        }
        for item in items
    ]
    clinician_items = [
        {
            "status": item["agreement_status"],
            "reference": item["reference_finding"],
            "mika": item["mika_blind_finding"],
            "level": item["level"],
            "side": item["side"],
            "evidence_refs": item["evidence_refs"],
            "modality_sequence_needed": item["modality_sequence_needed"],
            "explanation": item["clinician_explanation"],
        }
        for item in items
    ]
    return {
        "used": bool(reference_text or reference_path),
        "source_type": "reference_report",
        "source_label": source_label,
        "targets": targets,
        "items": items,
        "summary": {
            "target_count": len(targets),
            "item_count": len(items),
            "counts": dict(counts),
            "has_discrepancy": has_discrepancy,
        },
        "patient": {
            "heading": "Reference-assisted review",
            "summary": _patient_reconciliation_summary(items),
            "items": patient_items,
        },
        "clinician": {
            "heading": "Reference-assisted reconciliation",
            "summary": _clinician_reconciliation_summary(items),
            "items": clinician_items,
        },
    }


def reconcile_reference_targets(
    targets: list[dict[str, Any]],
    blind_summary: dict[str, Any],
    *,
    evidence_manifest: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    rows = list(_blind_rows(blind_summary))
    items: list[ReconciliationItem] = []
    for target in targets:
        status, row, score = _status_for_target(target, rows)
        evidence_refs = _row_evidence_refs(row, evidence_manifest) if row else []
        target = dict(target)
        reference_finding = target.get("reference_finding", "")
        mika_blind_finding = (
            row.get("text", "") if row and row.get("text")
            else "MIKA's blind image read did not independently report this reference target."
        )
        item = ReconciliationItem(
            reference_finding=reference_finding,
            mika_blind_finding=mika_blind_finding,
            anatomy=target.get("anatomy", ""),
            level=target.get("level", ""),
            side=target.get("side", ""),
            modality_sequence_needed=target.get("modality_sequence_needed", ""),
            evidence_refs=evidence_refs,
            agreement_status=status,
            patient_explanation=_patient_explanation(status, target),
            clinician_explanation=_clinician_explanation(status, target, row, score),
        )
        items.append(item)
    return [asdict(item) for item in items]


def _blind_rows(summary: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if not isinstance(summary, dict):
        return
    findings = summary.get("findings") or []
    if isinstance(findings, dict):
        findings = [findings]
    for idx, finding in enumerate(findings):
        if isinstance(finding, dict):
            text = _norm(finding.get("text") or finding.get("plain") or finding.get("finding"))
            if text:
                row = dict(finding)
                row["text"] = text
                row["_index"] = idx
                yield row
        elif isinstance(finding, str) and finding.strip():
            yield {"text": finding.strip(), "_index": idx}

    impression = summary.get("impression") or []
    if isinstance(impression, str):
        impression = [impression]
    for idx, line in enumerate(impression):
        if isinstance(line, str) and line.strip():
            yield {"text": line.strip(), "_index": idx, "_source": "impression"}


def _status_for_target(target: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, Optional[dict[str, Any]], int]:
    if not rows:
        return "cannot_assess", None, 0
    scored = [(_score_row(target, row), row) for row in rows]
    conflicts = [(score, row) for score, row in scored if score["conflict"] and score["level_side"] >= 1]
    if conflicts:
        best = max(conflicts, key=lambda x: (x[0]["level_side"], x[0]["concept_count"]))
        return "conflicts_with_reference", best[1], best[0]["total"]

    supports = [(score, row) for score, row in scored if score["concept_count"] > 0 and score["level_side"] >= 1]
    if not supports:
        return "not_independently_seen", None, 0
    best_score, best_row = max(supports, key=lambda x: (x[0]["total"], x[0]["concept_count"]))
    target_concepts = set(target.get("concepts") or [])
    strong = (
        best_score["level_match"]
        and (not target.get("side") or best_score["side_match"])
        and best_score["concept_count"] >= max(1, min(3, len(target_concepts)))
        and bool(_row_evidence_refs(best_row, None))
    )
    if strong:
        return "confirmed", best_row, best_score["total"]
    return "partially_supported", best_row, best_score["total"]


def _score_row(target: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    text = row.get("text", "")
    lower = text.lower()
    target_level = _normalize_level(target.get("level", ""))
    side = str(target.get("side", "")).lower()
    level_match = bool(target_level and target_level in _normalize_level(text))
    side_match = bool(side and side in lower)
    concepts = set(target.get("concepts") or [])
    row_concepts = set(_concepts_for_text(lower))
    concept_count = len(concepts & row_concepts)
    conflict = _contains_any(lower, NEGATION_TERMS) and (
        concept_count > 0
        or _contains_any(lower, SCAR_DISC_TERMS)
        or _contains_any(lower, NERVE_ROOT_TERMS)
        or _contains_any(lower, FORAMINAL_TERMS)
    )
    level_side = int(level_match) + int(side_match or not side)
    return {
        "level_match": level_match,
        "side_match": side_match or not side,
        "level_side": level_side,
        "concept_count": concept_count,
        "conflict": conflict,
        "total": level_side * 3 + concept_count,
    }


def _row_evidence_refs(row: Optional[dict[str, Any]], evidence_manifest: Optional[dict[str, Any]]) -> list[str]:
    if not row:
        return []
    refs = row.get("evidence_refs") or row.get("evidence") or []
    if isinstance(refs, str):
        refs = [refs]
    if refs:
        return [str(ref) for ref in refs if str(ref).strip()]
    figure = row.get("figure")
    if figure:
        return [str(figure)]
    return []


def _patient_status_label(status: str) -> str:
    return {
        "confirmed": "MIKA also saw this",
        "supported_by_focused_evidence": "Supported by focused review",
        "partially_supported": "MIKA saw part of this",
        "not_independently_seen": "MIKA did not independently see this report finding",
        "conflicts_with_reference": "MIKA's independent read differs from the uploaded report",
        "cannot_assess": "MIKA could not assess this",
    }.get(status, "Needs review")


def _patient_reconciliation_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "A reference report was provided, but MIKA could not extract a structured target from it."
    if any(item["agreement_status"] in {"conflicts_with_reference", "not_independently_seen"} for item in items):
        return (
            "MIKA compared the uploaded report with its independent image read. At least one item "
            "differs: the uploaded report may contain clinically important findings that MIKA did "
            "not independently confirm. Please review both with a radiologist or spine clinician."
        )
    return "MIKA compared the uploaded report with its independent image read and lists the agreement below."


def _patient_reference_phrase(item: dict[str, Any]) -> str:
    side = f"{item.get('side')} " if item.get("side") else ""
    level = item.get("level") or "the reported area"
    return f"The uploaded report describes a {side}{level} post-surgery area that may affect a nerve."


def _patient_mika_phrase(item: dict[str, Any]) -> str:
    status = item.get("agreement_status")
    if status == "supported_by_focused_evidence":
        return "MIKA's focused image review supported the same area, while the exact cause still needs clinician review."
    if status == "confirmed":
        return "MIKA's independent image read also supported this report finding."
    if status == "partially_supported":
        return "MIKA's independent image read supported part of this report finding, but not every detail."
    if status in {"not_independently_seen", "conflicts_with_reference"}:
        return "MIKA's independent image read did not independently confirm this report finding."
    return "MIKA could not reliably compare this report finding with the independent read."


def _clinician_reconciliation_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Reference report supplied; no structured reconciliation target extracted."
    return "Reference targets were compared against the blind structured findings without overwriting the blind read."


def _patient_explanation(status: str, target: dict[str, Any]) -> str:
    level = target.get("level") or "the reported area"
    side = f"{target.get('side')} " if target.get("side") else ""
    if status == "confirmed":
        return f"The reference report and MIKA's blind read both point to the {side}{level} area."
    if status == "partially_supported":
        return (
            f"MIKA saw part of what the reference report describes near {side}{level}, but it did not "
            "fully match every detail."
        )
    if status == "conflicts_with_reference":
        return (
            f"The uploaded report describes a {side}{level} post-surgery area that may affect a nerve. "
            "MIKA's independent read differs from the uploaded report and did not independently confirm "
            "this item. The uploaded report may contain clinically important findings, so please review "
            "both reports with a radiologist or spine clinician."
        )
    if status == "not_independently_seen":
        return (
            f"The uploaded report describes a {side}{level} post-surgery area that may affect a nerve. "
            "MIKA's independent read did not independently confirm this item. The uploaded report may "
            "contain clinically important findings, so please review both reports with a radiologist or "
            "spine clinician."
        )
    return "MIKA could not reliably compare this reference finding with the available blind read."


def _clinician_explanation(status: str, target: dict[str, Any], row: Optional[dict[str, Any]], score: int) -> str:
    level = target.get("level") or "unspecified level"
    side = target.get("side") or "unspecified side"
    sequence = target.get("modality_sequence_needed") or "relevant diagnostic sequences"
    if status == "confirmed":
        return (
            f"Blind MIKA finding supports the reference target at {side} {level} with evidence refs. "
            f"Review {sequence} for final clinical correlation."
        )
    if status == "partially_supported":
        return (
            f"Blind MIKA finding partially overlaps the reference target at {side} {level}, but the "
            f"target is not fully independently confirmed. Evidence score={score}. Review {sequence}."
        )
    if status == "conflicts_with_reference":
        blind = row.get("text", "") if row else "No matching blind finding."
        return (
            f"Reference target: {target.get('reference_finding')}. Blind MIKA read conflicts or negates "
            f"the target: {blind}. Do not treat the reference target as confirmed by MIKA; radiologist/"
            f"spine clinician review is required. Review {sequence}."
        )
    if status == "not_independently_seen":
        return (
            f"Reference target: {target.get('reference_finding')}. No independent blind MIKA finding "
            f"matched the target at {side} {level}. Do not upgrade to confirmed. Review {sequence}."
        )
    return f"Reference target at {side} {level} could not be assessed from the available blind summary."


def build_clinical_reconciliation_report(summary: dict[str, Any], reconciliation: dict[str, Any], out_pdf: str | Path) -> str:
    """Build a clinician PDF that preserves blind findings and adds a reconciliation table."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    out_pdf = str(out_pdf)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, alignment=TA_LEFT)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12, leading=16, spaceBefore=12)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#475569"))

    story: list[Any] = [
        Paragraph("MIKA Clinical Report", h1),
        Paragraph("Blind image read plus separate focused-evidence and reference-assisted sections.", small),
        Spacer(1, 8),
        Paragraph("Blind image read", h2),
        Paragraph(
            "The findings below are MIKA's independent image read. The reference-assisted section does not "
            "convert unsupported reference targets into confirmed image findings.",
            body,
        ),
    ]
    findings = summary.get("findings") or []
    if isinstance(findings, dict):
        findings = [findings]
    rows = [[Paragraph("<b>Tier</b>", small), Paragraph("<b>Blind finding</b>", small), Paragraph("<b>Evidence</b>", small)]]
    for finding in findings[:20]:
        if not isinstance(finding, dict):
            continue
        rows.append([
            Paragraph(escape(str(finding.get("tier") or "")), body),
            Paragraph(escape(str(finding.get("text") or "")), body),
            Paragraph(escape(", ".join(str(x) for x in (finding.get("evidence_refs") or [])) or str(finding.get("figure") or "")), body),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("", body), Paragraph("No structured blind findings available.", body), Paragraph("", body)])
    story.append(_table(rows, [0.6 * inch, 4.7 * inch, 1.5 * inch]))

    adjudication_rows_data = summary.get("cv_candidate_adjudication") or []
    if isinstance(adjudication_rows_data, dict):
        adjudication_rows_data = [adjudication_rows_data]
    if adjudication_rows_data:
        story.extend([
            Spacer(1, 8),
            Paragraph("CV candidate adjudication", h2),
            Paragraph(
                "Rows below combine repeated focused candidate reviews. Only final_status=supported "
                "is eligible for focused-evidence synthesis.",
                body,
            ),
        ])
        adj_rows = [[
            Paragraph("<b>Candidate</b>", small),
            Paragraph("<b>Reviews</b>", small),
            Paragraph("<b>Final status</b>", small),
            Paragraph("<b>Reason summary / limitations</b>", small),
        ]]
        for item in adjudication_rows_data[:20]:
            if not isinstance(item, dict):
                continue
            status_text = (
                f"majority={item.get('majority_status') or 'none'}; "
                f"final={item.get('final_status') or 'cannot_assess'}; "
                f"disagreement={bool(item.get('disagreement'))}"
            )
            reasons = str(item.get("reasons_summary") or "")
            limitations = "; ".join(str(x) for x in (item.get("limitations") or []))
            adj_rows.append([
                Paragraph(escape(str(item.get("candidate_id") or "")), body),
                Paragraph(escape(f"{item.get('review_count', 0)}; statuses={item.get('statuses') or []}"), body),
                Paragraph(escape(status_text), body),
                Paragraph(escape("; ".join(x for x in (reasons, limitations) if x)), body),
            ])
        if len(adj_rows) > 1:
            story.append(_table(adj_rows, [1.45 * inch, 1.4 * inch, 1.4 * inch, 2.55 * inch]))

    cv_rows_data = summary.get("cv_supported_findings") or []
    if isinstance(cv_rows_data, dict):
        cv_rows_data = [cv_rows_data]
    if cv_rows_data:
        story.extend([
            Spacer(1, 8),
            Paragraph("CV-supported focused evidence", h2),
            Paragraph(
                "Rows below are CV-localized, Claude-supported evidence. They are not deterministic diagnoses "
                "and do not create body-map markers or proof overlays unless trust gates pass.",
                body,
            ),
        ])
        cv_rows = [[
            Paragraph("<b>Candidate</b>", small),
            Paragraph("<b>Location</b>", small),
            Paragraph("<b>Status / refs</b>", small),
            Paragraph("<b>Limitations</b>", small),
        ]]
        for item in cv_rows_data[:20]:
            if not isinstance(item, dict):
                continue
            location = " ".join(str(x) for x in (item.get("side"), item.get("level")) if x)
            confidence = (
                f"geometry={item.get('geometry_confidence', 'n/a')}; "
                f"registration={item.get('registration_confidence', 'n/a')}"
            )
            refs = ", ".join(str(x) for x in (item.get("evidence_refs") or []))
            limitations = "; ".join(str(x) for x in (item.get("limitations") or []))
            cv_rows.append([
                Paragraph(escape(str(item.get("candidate_id") or "")), body),
                Paragraph(escape(location), body),
                Paragraph(escape(f"{item.get('status', '')}; refs: {refs}; {confidence}"), body),
                Paragraph(escape(limitations), body),
            ])
        if len(cv_rows) > 1:
            story.append(_table(cv_rows, [1.45 * inch, 1.0 * inch, 2.0 * inch, 2.35 * inch]))

    story.extend([
        Spacer(1, 8),
        Paragraph("Reference-assisted reconciliation", h2),
        Paragraph(
            "Statuses compare the reference report against the blind image read. Conflicts and targets not "
            "independently seen by MIKA require clinical review.",
            body,
        ),
    ])
    rec_rows = [[
        Paragraph("<b>Status</b>", small),
        Paragraph("<b>Reference target (report-derived)</b>", small),
        Paragraph("<b>MIKA blind finding</b>", small),
        Paragraph("<b>Evidence refs</b>", small),
    ]]
    for item in reconciliation.get("items") or []:
        rec_rows.append([
            Paragraph(escape(str(item.get("agreement_status") or "")), body),
            Paragraph(escape(str(item.get("reference_finding") or "")), body),
            Paragraph(escape(str(item.get("mika_blind_finding") or "")), body),
            Paragraph(escape(", ".join(str(x) for x in (item.get("evidence_refs") or []))), body),
        ])
    if len(rec_rows) == 1:
        rec_rows.append([
            Paragraph("cannot_assess", body),
            Paragraph("No structured reference targets were extracted.", body),
            Paragraph("", body),
            Paragraph("", body),
        ])
    story.append(_table(rec_rows, [1.0 * inch, 2.35 * inch, 2.35 * inch, 1.1 * inch]))

    SimpleDocTemplate(
        out_pdf,
        pagesize=LETTER,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title="MIKA Clinical Report",
    ).build(story)
    return out_pdf


def _table(rows: list[list[Any]], widths: list[float]) -> Any:
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table

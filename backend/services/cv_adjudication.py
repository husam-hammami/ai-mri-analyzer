"""
Adjudication policy for repeated CV candidate reviews.

Deterministic CV candidates are localization prompts only. This module combines one
or more Claude/verifier candidate review rows into a separate adjudication artifact
so downstream synthesis never cherry-picks a favorable focused review.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Optional


CV_REVIEW_STATUSES = {"supported", "not_supported", "cannot_assess", "localization_wrong", "unstable"}
CV_ADJUDICATION_FINAL_STATUSES = CV_REVIEW_STATUSES | {"discordant"}


def normalize_cv_review_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in CV_REVIEW_STATUSES else "cannot_assess"


def adjudicate_cv_candidate_reviews(
    *,
    cv_candidates: Optional[list[dict[str, Any]]] = None,
    cv_candidate_reviews: Optional[list[dict[str, Any]]] = None,
    existing_adjudication: Optional[list[dict[str, Any]]] = None,
    evidence_bundle_id: str = "",
) -> list[dict[str, Any]]:
    """Return one adjudication row per reviewed candidate.

    Policy:
    - localization_wrong vetoes synthesis for that candidate.
    - unstable from any reviewer keeps the final state unstable.
    - a strict majority can set the final status if no veto applies.
    - a split without strict majority is unstable.
    - only final_status == supported can be synthesized downstream.
    """
    candidates = {
        str(c.get("candidate_id")): c
        for c in (cv_candidates or [])
        if isinstance(c, dict) and c.get("candidate_id")
    }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cv_candidate_reviews or []:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        grouped[candidate_id].append(row)

    if not grouped and existing_adjudication:
        return _normalize_existing_adjudication(
            existing_adjudication,
            candidates=candidates,
            evidence_bundle_id=evidence_bundle_id,
        )

    adjudicated: list[dict[str, Any]] = []
    for candidate_id in sorted(grouped):
        candidate = candidates.get(candidate_id, {})
        reviews = grouped[candidate_id]
        statuses = [normalize_cv_review_status(row.get("status")) for row in reviews]
        counts = Counter(statuses)
        majority_status = _majority_status(counts, len(statuses))
        final_status = _final_status(counts, len(statuses), majority_status)
        disagreement = len(set(statuses)) > 1
        reasons = _reason_summary(reviews, counts)
        limitations = _limitations(candidate, final_status, disagreement)
        adjudicated.append({
            "candidate_id": candidate_id,
            "review_count": len(statuses),
            "statuses": statuses,
            "status_counts": dict(sorted(counts.items())),
            "majority_status": majority_status,
            "final_status": final_status,
            "disagreement": disagreement,
            "reasons_summary": reasons,
            "evidence_bundle_id": evidence_bundle_id,
            "limitations": limitations,
            "level": _clean(candidate.get("level") or _first_value(reviews, "level")),
            "side": _clean(candidate.get("side") or _first_value(reviews, "side")),
            "candidate_type": _clean(candidate.get("candidate_type") or _first_value(reviews, "candidate_type")),
            "evidence_refs": _evidence_refs(candidate, reviews),
            "artifact_trust": _dict(candidate.get("artifact_trust") or _first_value(reviews, "artifact_trust")),
        })
    return adjudicated


def adjudication_by_candidate(rows: Optional[list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("candidate_id")): row
        for row in (rows or [])
        if isinstance(row, dict) and row.get("candidate_id")
    }


def candidate_is_adjudicated_supported(candidate_id: str, rows: Optional[list[dict[str, Any]]]) -> bool:
    row = adjudication_by_candidate(rows).get(str(candidate_id or ""))
    return bool(row and str(row.get("final_status") or "").lower() == "supported")


def _final_status(counts: Counter, review_count: int, majority_status: str) -> str:
    if review_count <= 0:
        return "cannot_assess"
    if counts.get("localization_wrong"):
        return "localization_wrong"
    if counts.get("unstable"):
        return "unstable"
    if set(counts) == {"cannot_assess"}:
        return "cannot_assess"
    if majority_status:
        return majority_status
    return "unstable"


def _majority_status(counts: Counter, review_count: int) -> str:
    if not counts or review_count <= 0:
        return ""
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return ""
    status, count = top[0]
    return status if count > review_count / 2 else ""


def _reason_summary(reviews: list[dict[str, Any]], counts: Counter) -> str:
    reasons: list[str] = []
    for row in reviews:
        reason = _clean(row.get("short_reason") or row.get("reason") or row.get("clinician_wording"))
        if reason and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return " | ".join(reasons[:5])
    if counts:
        return "Status counts: " + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    return "No focused review rows were available."


def _limitations(candidate: dict[str, Any], final_status: str, disagreement: bool) -> list[str]:
    out = [str(v) for v in (candidate.get("limitations") or []) if str(v).strip()]
    if disagreement:
        out.append("Repeated focused reviews disagreed; do not cherry-pick a favorable row.")
    if final_status != "supported":
        out.append("Final adjudication is not supported; no focused-evidence synthesis, marker, or proof overlay is allowed.")
    out.append("CV candidate adjudication does not overwrite the blind image read.")
    return sorted(set(out))


def _normalize_existing_adjudication(
    rows: list[dict[str, Any]],
    *,
    candidates: dict[str, dict[str, Any]],
    evidence_bundle_id: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        candidate = candidates.get(candidate_id, {})
        statuses = row.get("statuses")
        if not isinstance(statuses, list):
            statuses = [row.get("final_status") or row.get("majority_status") or "cannot_assess"]
        statuses = [normalize_cv_review_status(status) for status in statuses]
        final_status = str(row.get("final_status") or "").strip().lower()
        if final_status not in CV_ADJUDICATION_FINAL_STATUSES:
            final_status = normalize_cv_review_status(final_status)
        out.append({
            "candidate_id": candidate_id,
            "review_count": int(row.get("review_count") or len(statuses) or 0),
            "statuses": statuses,
            "status_counts": _dict(row.get("status_counts")) or dict(Counter(statuses)),
            "majority_status": normalize_cv_review_status(row.get("majority_status")) if row.get("majority_status") else "",
            "final_status": final_status,
            "disagreement": bool(row.get("disagreement")),
            "reasons_summary": _clean(row.get("reasons_summary")),
            "evidence_bundle_id": _clean(row.get("evidence_bundle_id") or evidence_bundle_id),
            "limitations": [str(v) for v in (row.get("limitations") or []) if str(v).strip()],
            "level": _clean(row.get("level") or candidate.get("level")),
            "side": _clean(row.get("side") or candidate.get("side")),
            "candidate_type": _clean(row.get("candidate_type") or candidate.get("candidate_type")),
            "evidence_refs": [str(v) for v in (row.get("evidence_refs") or candidate.get("evidence_refs") or []) if str(v).strip()],
            "artifact_trust": _dict(row.get("artifact_trust") or candidate.get("artifact_trust")),
        })
    return out


def _first_value(rows: list[dict[str, Any]], key: str) -> Any:
    for row in rows:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return ""


def _evidence_refs(candidate: dict[str, Any], reviews: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for raw in [candidate.get("selected_evidence_refs"), candidate.get("evidence_refs")] + [
        row.get("evidence_refs_used") or row.get("evidence_refs") for row in reviews
    ]:
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for ref in raw:
            val = str(ref).strip()
            if val and val not in refs:
                refs.append(val)
    return refs


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())

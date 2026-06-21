"""
Safe synthesis for CV-localized candidate reviews.

The deterministic CV module can localize and measure candidate regions, but it must
not create diagnostic findings by itself. This module only turns Claude/verifier
supported candidate reviews into separate report additions that remain traceable to
the blind read, the candidate review, and the candidate metadata.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Optional


SUPPORTED_RECON_STATUS = "supported_by_focused_evidence"
CV_REVIEW_STATUSES = {"supported", "not_supported", "cannot_assess", "localization_wrong"}


def synthesize_cv_candidate_reviews(
    *,
    blind_report: Optional[dict[str, Any]] = None,
    cv_candidates: Optional[list[dict[str, Any]]] = None,
    cv_candidate_reviews: Optional[list[dict[str, Any]]] = None,
    verifier_result: Optional[dict[str, Any]] = None,
    cv_candidate_policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return safe report additions derived from supported CV candidate reviews.

    The original blind findings are intentionally not modified. A row is eligible
    only when the candidate review is supported, evidence refs exist, and any
    verifier row for the same candidate does not reject the localization.
    """
    del blind_report  # Reserved for future contradiction analysis; do not mutate it.
    candidates = {
        str(c.get("candidate_id")): c
        for c in (cv_candidates or [])
        if isinstance(c, dict) and c.get("candidate_id")
    }
    verifier_status = _verifier_status_by_candidate(verifier_result)
    policy = dict(cv_candidate_policy or {})
    policy.setdefault("deterministic_cv_does_not_create_findings", True)
    policy.setdefault("supported_review_required", True)
    policy.setdefault("marker_thresholds_still_apply", True)

    clinician_rows: list[dict[str, Any]] = []
    patient_rows: list[dict[str, Any]] = []
    for review in cv_candidate_reviews or []:
        if not isinstance(review, dict):
            continue
        candidate_id = str(review.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        status = str(review.get("status") or "").strip().lower()
        if status not in CV_REVIEW_STATUSES:
            status = "cannot_assess"
        if status != "supported":
            continue
        verifier_status_for_candidate = verifier_status.get(candidate_id)
        if verifier_status_for_candidate and verifier_status_for_candidate != "supported":
            continue
        candidate = candidates.get(candidate_id, {})
        refs = _evidence_refs(review, candidate)
        if not refs:
            continue

        level = _clean(review.get("level") or candidate.get("level"))
        side = _clean(review.get("side") or candidate.get("side")).lower()
        limitations = _limitations(candidate, policy)
        concepts = _candidate_concepts(review, candidate)
        patient_text = _patient_explanation(level=level, side=side)
        clinician_text = _clinician_explanation(
            review=review,
            candidate=candidate,
            refs=refs,
            limitations=limitations,
            level=level,
            side=side,
        )
        artifact_trust = _dict(candidate.get("artifact_trust") or review.get("artifact_trust"))
        row = {
            "candidate_id": candidate_id,
            "source": "cv_localized_claude_supported",
            "label": "CV-localized, Claude-supported evidence",
            "status": "supported",
            "level": level,
            "side": side,
            "candidate_type": _clean(review.get("candidate_type") or candidate.get("candidate_type")),
            "series_ids": _string_list(candidate.get("series_ids")),
            "slice_ids": _string_list(candidate.get("slice_ids")),
            "roi": _dict(candidate.get("roi")),
            "evidence_refs": refs,
            "calibration_state": _clean(candidate.get("calibration_state")),
            "geometry_confidence": candidate.get("geometry_confidence"),
            "registration_confidence": candidate.get("registration_confidence"),
            "limitations": limitations,
            "artifact_trust": artifact_trust,
            "patient_explanation": patient_text,
            "clinician_explanation": clinician_text,
            "concepts": concepts,
        }
        clinician_rows.append(row)
        patient_rows.append({
            "candidate_id": candidate_id,
            "heading": "Focused evidence review",
            "plain": patient_text,
            "level": level,
            "side": side,
            "source": "focused_image_review",
        })

    return {
        "used": bool(clinician_rows),
        "clinician_findings": clinician_rows,
        "patient_explanations": patient_rows,
        "policy": policy,
    }


def upgrade_reconciliation_with_cv_supported_findings(
    reconciliation: Optional[dict[str, Any]],
    cv_supported_findings: Optional[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Reflect focused CV-supported evidence in reference-assisted reconciliation.

    This updates the reconciliation section only. It preserves the blind finding text
    and records that a prior blind-read conflict or miss still needs clinical review.
    """
    rec = copy.deepcopy(reconciliation or {})
    findings = [f for f in (cv_supported_findings or []) if isinstance(f, dict)]
    if not rec or not findings or not rec.get("items"):
        return rec

    targets = rec.get("targets") or []
    updated_items: list[dict[str, Any]] = []
    for idx, raw_item in enumerate(rec.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        target = targets[idx] if idx < len(targets) and isinstance(targets[idx], dict) else item
        match = _match_focused_finding(target, findings)
        if match and item.get("agreement_status") in {
            "conflicts_with_reference",
            "not_independently_seen",
            "cannot_assess",
            "partially_supported",
        }:
            old_status = str(item.get("agreement_status") or "")
            new_status = _focused_reconciliation_status(target, match)
            item["agreement_status"] = new_status
            item["focused_evidence_status"] = match.get("status", "supported")
            item["focused_evidence_candidate_id"] = match.get("candidate_id", "")
            item["focused_evidence_refs"] = match.get("evidence_refs", [])
            item["focused_evidence_note"] = match.get("clinician_explanation", "")
            item["blind_read_discrepancy_preserved"] = old_status in {
                "conflicts_with_reference",
                "not_independently_seen",
                "cannot_assess",
            }
            item["patient_explanation"] = _focused_patient_reconciliation_explanation(item, match, old_status)
            item["clinician_explanation"] = _focused_clinician_reconciliation_explanation(
                item,
                match,
                old_status,
                new_status,
            )
        updated_items.append(item)

    rec["items"] = updated_items
    _refresh_reconciliation_sections(rec)
    return rec


def _verifier_status_by_candidate(verifier_result: Optional[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(verifier_result, dict):
        return out
    for row in verifier_result.get("cv_candidate_reviews") or []:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        status = str(row.get("status") or "").strip().lower()
        out[candidate_id] = status if status in CV_REVIEW_STATUSES else "cannot_assess"
    return out


def _evidence_refs(review: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    refs = review.get("evidence_refs_used") or review.get("evidence_refs") or candidate.get("evidence_refs") or []
    return _string_list(refs)


def _limitations(candidate: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    vals = _string_list(candidate.get("limitations"))
    if candidate.get("calibration_state"):
        vals.append(f"Calibration state: {candidate.get('calibration_state')}")
    vals.append("CV localization only; pathology classification remains Claude/verifier reviewed.")
    if policy.get("marker_thresholds_still_apply"):
        vals.append("Body-map markers, proof overlays, and pinpoint markers still require trust thresholds.")
    return sorted(set(v for v in vals if v))


def _candidate_concepts(review: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    text = " ".join(
        _clean(v).lower()
        for v in (
            review.get("candidate_type"),
            candidate.get("candidate_type"),
        )
    )
    concepts = set()
    if "lateral" in text and "recess" in text:
        concepts.add("lateral_recess")
    if "post" in text or "operative" in text or "surgical" in text:
        concepts.add("post_surgical")
    if "foram" in text:
        concepts.add("foraminal")
    return sorted(concepts)


def _patient_explanation(*, level: str, side: str) -> str:
    location = " ".join(x for x in (side, level) if x).strip() or "the focused area"
    return (
        f"MIKA reviewed a focused area at {location}. This may line up with important "
        "image evidence, but it is not a diagnosis and should still be reviewed with "
        "your clinician or radiologist."
    )


def _clinician_explanation(
    *,
    review: dict[str, Any],
    candidate: dict[str, Any],
    refs: list[str],
    limitations: list[str],
    level: str,
    side: str,
) -> str:
    location = " ".join(x for x in (side, level) if x).strip() or "unspecified location"
    return (
        "CV-localized, Claude-supported evidence at "
        f"{location}. Candidate id {review.get('candidate_id')}; "
        f"candidate type {_clean(review.get('candidate_type') or candidate.get('candidate_type')) or 'unspecified'}; "
        f"evidence refs {', '.join(refs)}. "
        "This does not make a deterministic diagnosis. "
        f"Limitations: {'; '.join(limitations)}"
    )


def _match_focused_finding(target: dict[str, Any], findings: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    target_level = _normalize_level(target.get("level", ""))
    target_side = _clean(target.get("side")).lower()
    for finding in findings:
        if _normalize_level(finding.get("level", "")) != target_level:
            continue
        finding_side = _clean(finding.get("side")).lower()
        if target_side and finding_side and target_side != finding_side:
            continue
        return finding
    return None


def _focused_reconciliation_status(target: dict[str, Any], finding: dict[str, Any]) -> str:
    target_concepts = set(target.get("concepts") or [])
    focused_concepts = set(finding.get("concepts") or [])
    unresolved = target_concepts - focused_concepts
    if unresolved & {"scar_or_residual_recurrent_disc", "nerve_root", "foraminal"}:
        return "partially_supported"
    return SUPPORTED_RECON_STATUS


def _focused_patient_reconciliation_explanation(
    item: dict[str, Any],
    finding: dict[str, Any],
    old_status: str,
) -> str:
    del item
    location = " ".join(x for x in (finding.get("side"), finding.get("level")) if x).strip() or "the reported area"
    earlier = (
        "MIKA's earlier independent read differed from the uploaded report, but "
        if old_status == "conflicts_with_reference"
        else "MIKA's earlier independent read did not fully show this item, but "
    )
    return (
        f"{earlier}a focused review looked at {location} and found supporting image evidence. "
        "This still does not prove the exact cause, and the uploaded report should be reviewed "
        "with your clinician or radiologist."
    )


def _focused_clinician_reconciliation_explanation(
    item: dict[str, Any],
    finding: dict[str, Any],
    old_status: str,
    new_status: str,
) -> str:
    return (
        f"Reference target previously reconciled as {old_status}. Focused CV-localized, "
        f"Claude-supported evidence overlaps the target at {finding.get('side')} {finding.get('level')} "
        f"with refs {', '.join(finding.get('evidence_refs') or [])}. Reclassified as {new_status}; "
        "preserve the blind-read discrepancy and do not infer scar versus recurrent disc or nerve-root "
        "encasement unless directly supported by the reviewed images."
    )


def _refresh_reconciliation_sections(rec: dict[str, Any]) -> None:
    items = [i for i in rec.get("items") or [] if isinstance(i, dict)]
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("agreement_status") or "cannot_assess")
        counts[status] = counts.get(status, 0) + 1
    has_discrepancy = any(
        item.get("agreement_status") in {"conflicts_with_reference", "not_independently_seen", "cannot_assess"}
        or item.get("blind_read_discrepancy_preserved")
        for item in items
    )
    summary = dict(rec.get("summary") or {})
    summary.update({
        "item_count": len(items),
        "counts": counts,
        "has_discrepancy": has_discrepancy,
        "focused_evidence_used": any(item.get("focused_evidence_status") == "supported" for item in items),
    })
    rec["summary"] = summary
    rec["patient"] = {
        "heading": "Reference-assisted review",
        "summary": _patient_summary(items),
        "items": [_patient_item(item) for item in items],
    }
    rec["clinician"] = {
        "heading": "Reference-assisted reconciliation",
        "summary": "Reference targets were compared against the blind read and focused CV-supported evidence without overwriting the blind read.",
        "items": [_clinician_item(item) for item in items],
    }


def _patient_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": item.get("agreement_status", "cannot_assess"),
        "label": _patient_status_label(item.get("agreement_status", "")),
        "reference": _patient_reference_phrase(item),
        "mika": _patient_mika_phrase(item),
        "explanation": item.get("patient_explanation", ""),
    }


def _clinician_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": item.get("agreement_status", "cannot_assess"),
        "reference": item.get("reference_finding", ""),
        "mika": item.get("mika_blind_finding", ""),
        "level": item.get("level", ""),
        "side": item.get("side", ""),
        "evidence_refs": item.get("evidence_refs", []),
        "focused_evidence_refs": item.get("focused_evidence_refs", []),
        "focused_evidence_candidate_id": item.get("focused_evidence_candidate_id", ""),
        "modality_sequence_needed": item.get("modality_sequence_needed", ""),
        "explanation": item.get("clinician_explanation", ""),
    }


def _patient_status_label(status: str) -> str:
    return {
        SUPPORTED_RECON_STATUS: "Supported by focused review",
        "confirmed": "MIKA also saw this",
        "partially_supported": "MIKA saw part of this",
        "not_independently_seen": "MIKA did not independently see this report finding",
        "conflicts_with_reference": "MIKA's independent read differs from the uploaded report",
        "cannot_assess": "MIKA could not assess this",
    }.get(status, "Needs review")


def _patient_summary(items: list[dict[str, Any]]) -> str:
    if any(item.get("focused_evidence_status") == "supported" for item in items):
        return (
            "MIKA compared the uploaded report with its independent image read and a focused "
            "image review. Some focused evidence may support the reported area, but the exact "
            "meaning still needs clinician or radiologist review."
        )
    if any(item.get("agreement_status") in {"conflicts_with_reference", "not_independently_seen"} for item in items):
        return (
            "MIKA compared the uploaded report with its independent image read. At least one item "
            "differs and should be reviewed with a radiologist or clinician."
        )
    return "MIKA compared the uploaded report with its independent image read and lists the agreement below."


def _patient_reference_phrase(item: dict[str, Any]) -> str:
    side = f"{item.get('side')} " if item.get("side") else ""
    level = item.get("level") or "the reported area"
    return f"The uploaded report describes a {side}{level} post-surgery area that may affect a nerve."


def _patient_mika_phrase(item: dict[str, Any]) -> str:
    status = item.get("agreement_status")
    if status == SUPPORTED_RECON_STATUS:
        return "MIKA's focused image review supported the same area, while the exact cause still needs clinician review."
    if status == "confirmed":
        return "MIKA's independent image read also supported this report finding."
    if status == "partially_supported":
        return "MIKA supported part of this report finding, but not every detail."
    if status in {"not_independently_seen", "conflicts_with_reference"}:
        return "MIKA's independent image read did not independently confirm this report finding."
    return "MIKA could not reliably compare this report finding with the independent read."


def _normalize_level(value: Any) -> str:
    text = _clean(value).upper().replace(" ", "").replace("/", "-")
    return re.sub(r"^([CLT])(\d+)-(\d+)$", r"\1\2-\1\3", text)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}

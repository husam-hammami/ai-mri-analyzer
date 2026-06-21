"""
Diagnostic-only L5-S1 evidence adequacy tools.

This module is not part of the live patient read path. It measures whether a
deterministic lumbar contrast candidate is limited by sequence selection,
localization, or genuinely non-focal subtraction signal. It does not classify
scar versus recurrent disc, nerve-root encasement, or final pathology.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from core.anatomy_modules.lumbar_spine import (
    LumbarSpineEvidenceModule,
    _difference_array,
    _physical_slice_distance_mm,
    _read_slice_array,
    _registration_metric,
    _shift_array,
)
from core.study_graph import StudyGraph, StudyGraphBuilder, StudySeries, StudySlice, normalize_contrast_pair_key


REFERENCE_CATEGORIES = (
    "expected_post_op_scar",
    "recurrent_or_residual_disc",
    "discrete_enhancing_abnormality",
    "nerve_root_involvement",
    "none_stated",
    "unclear",
)

DECISION_CORRECT = "CORRECT cannot_assess"
DECISION_RECOVERABLE = "RECOVERABLE (evidence selection)"
DECISION_LOCALIZATION_LIMITED = "LOCALIZATION-LIMITED"

RUN12_ROI_SPEC = {
    "center_x": 0.59,
    "center_y": 0.50,
    "width": 0.10,
    "height": 0.20,
    "label": "run12_narrowed_roi",
}


@dataclass(frozen=True)
class RoiSpec:
    center_x: float
    center_y: float
    width: float
    height: float
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_x": round(self.center_x, 3),
            "center_y": round(self.center_y, 3),
            "width": round(self.width, 3),
            "height": round(self.height, 3),
            "label": self.label,
        }


def classify_reference_claim_category(text: str) -> str:
    """Return a single PHI-safe category for the left L5-S1 reference target."""
    low = " ".join(str(text or "").lower().split())
    if not low:
        return "unclear"
    if not re.search(r"l5\s*[-/]?\s*s1|l5/s1|l5-s1", low):
        return "none_stated"

    # Priority is clinical specificity for this diagnostic decision. A report can
    # mention disc/scar terms broadly; nerve-root language is the strongest target.
    if re.search(r"nerve\s*root|s1\s*root|l5\s*root|radicul|imping|encroach|compress", low):
        return "nerve_root_involvement"
    if re.search(r"recurrent|residual", low) and re.search(r"\bdisc\b|disk", low):
        return "recurrent_or_residual_disc"
    if re.search(r"focal|nodular|masslike", low) and re.search(r"enhanc", low):
        return "discrete_enhancing_abnormality"
    if re.search(r"scar|fibrosis|fibrotic|granulation", low):
        return "expected_post_op_scar"
    if re.search(r"enhanc", low):
        return "discrete_enhancing_abnormality"
    return "unclear"


def sequence_adequacy_listing(graph: StudyGraph, candidate: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """List PHI-safe series/pair suitability for the bounded L5-S1 contrast question."""
    selected_series = set(str(v) for v in ((candidate or {}).get("series_ids") or []))
    module = LumbarSpineEvidenceModule()
    diagnostic = [s for s in graph.series if not s.is_localizer]
    axial_t1 = [s for s in diagnostic if s.plane == "axial" and s.sequence == "t1"]
    pres = [s for s in axial_t1 if s.contrast_phase == "pre_contrast"]
    posts = [s for s in axial_t1 if s.contrast_phase == "post_contrast"]

    series_rows = []
    for series in sorted(diagnostic, key=lambda s: (s.series_number or 9999, s.series_id)):
        role = _sequence_role(series)
        series_rows.append({
            "series_id": series.series_id,
            "label": _phi_safe_series_label(series),
            "plane": series.plane,
            "sequence": series.sequence,
            "contrast_phase": series.contrast_phase,
            "slice_count": series.slice_count,
            "calibrated": series.calibrated,
            "candidate_selected": series.series_id in selected_series,
            "answer_role": role,
        })

    pair_rows = []
    for pre in pres:
        for post in posts:
            geometry = _prepost_geometry_summary(pre, post)
            score = _pair_suitability_score(pre, post, geometry)
            pair_rows.append({
                "pre_series_id": pre.series_id,
                "post_series_id": post.series_id,
                "pre_label": _phi_safe_series_label(pre),
                "post_label": _phi_safe_series_label(post),
                "pair_key_match": normalize_contrast_pair_key(pre.description) == normalize_contrast_pair_key(post.description),
                "same_geometry": geometry["same_geometry"],
                "overlap_slice_pairs": geometry["overlap_slice_pairs"],
                "mean_pair_distance_mm": geometry["mean_pair_distance_mm"],
                "max_pair_distance_mm": geometry["max_pair_distance_mm"],
                "candidate_selected": pre.series_id in selected_series and post.series_id in selected_series,
                "suitability_score": score,
                "suitability": _suitability_label(score, geometry),
            })
    pair_rows = sorted(pair_rows, key=lambda row: (-row["suitability_score"], row["pre_series_id"], row["post_series_id"]))

    selected_pair = next((row for row in pair_rows if row["candidate_selected"]), None)
    selected_score = float(selected_pair["suitability_score"]) if selected_pair else -1.0
    better = [
        row for row in pair_rows
        if not row["candidate_selected"]
        and row["same_geometry"]
        and float(row["suitability_score"]) > selected_score + 0.08
    ]

    candidate_set = module.analyze(graph).to_dict()
    candidate_count = len(candidate_set.get("candidates") or [])
    return {
        "series": series_rows,
        "matched_prepost_pairs": pair_rows,
        "selected_pair": selected_pair,
        "better_suited_sequence_found": bool(better),
        "better_suited_pairs": better,
        "candidate_count": candidate_count,
        "phi_safe": True,
    }


def should_trigger_re_evaluation(sequence_report: dict[str, Any]) -> bool:
    return bool((sequence_report or {}).get("better_suited_sequence_found"))


def plausible_l5s1_roi_grid() -> list[dict[str, Any]]:
    """Return deterministic anatomically plausible left-sided ROI specs."""
    specs: list[RoiSpec] = []
    for center_x in (0.55, 0.59, 0.63):
        for center_y in (0.46, 0.50, 0.54):
            for width in (0.08, 0.10, 0.14):
                for height in (0.16, 0.20, 0.24):
                    label = "run12_narrowed_roi" if (
                        abs(center_x - RUN12_ROI_SPEC["center_x"]) < 1e-6
                        and abs(center_y - RUN12_ROI_SPEC["center_y"]) < 1e-6
                        and abs(width - RUN12_ROI_SPEC["width"]) < 1e-6
                        and abs(height - RUN12_ROI_SPEC["height"]) < 1e-6
                    ) else ""
                    specs.append(RoiSpec(center_x, center_y, width, height, label))
    return [spec.to_dict() for spec in specs]


def run_roi_sweep(graph: StudyGraph, candidate: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Measure focal subtraction signal across plausible ROIs without promoting findings."""
    module = LumbarSpineEvidenceModule()
    candidate_payload = candidate
    if candidate_payload is None:
        candidate_set = module.analyze(graph).to_dict()
        candidates = candidate_set.get("candidates") or []
        candidate_payload = candidates[0] if candidates else None
    if not candidate_payload:
        return _empty_roi_sweep("No CV candidate was available for ROI sweep.")

    by_id = graph.series_by_id()
    series_ids = list(candidate_payload.get("series_ids") or [])
    if len(series_ids) < 2:
        return _empty_roi_sweep("Candidate did not include both pre and post series IDs.")
    pre = by_id.get(series_ids[0])
    post = by_id.get(series_ids[1])
    if not pre or not post:
        return _empty_roi_sweep("Candidate series IDs were not found in the StudyGraph.")

    post_slice_ids = [
        slice_id for slice_id in (candidate_payload.get("slice_ids") or [])
        if str(slice_id).startswith(post.series_id)
    ]
    if not post_slice_ids:
        post_slice_ids = [sl.slice_id for sl in post.slices]
    registration = module.registration_qc(pre, post, post_slice_ids)
    if not registration.passed or not registration.slice_pairs:
        return _empty_roi_sweep("Registration did not pass; ROI sweep cannot evaluate difference maps.")

    rows = []
    for spec in plausible_l5s1_roi_grid():
        pair_metrics = []
        focal_pairs = 0
        for pre_slice, post_slice, _distance in registration.slice_pairs:
            metric = _roi_pair_signal(pre_slice, post_slice, spec)
            if metric:
                pair_metrics.append(metric)
                if metric["focal_pair"]:
                    focal_pairs += 1
        verdict = _roi_verdict(focal_pairs, len(pair_metrics))
        rows.append({
            "roi": spec,
            "pair_count": len(pair_metrics),
            "focal_pair_count": focal_pairs,
            "max_ratio": round(max((m["ratio"] for m in pair_metrics), default=0.0), 3),
            "mean_ratio": round(float(np.mean([m["ratio"] for m in pair_metrics])) if pair_metrics else 0.0, 3),
            "max_contrast": round(max((m["contrast"] for m in pair_metrics), default=0.0), 3),
            "verdict": verdict,
        })

    summary = summarize_roi_sweep_results(rows)
    run12_row = next((row for row in rows if row["roi"].get("label") == "run12_narrowed_roi"), None)
    return {
        "roi_count": len(rows),
        "pair_count": len(registration.slice_pairs),
        "registration_confidence": registration.confidence,
        "mean_pair_distance_mm": registration.mean_pair_distance_mm,
        "max_pair_distance_mm": registration.max_pair_distance_mm,
        "difference_map_allowed": registration.difference_map_allowed,
        "rows": rows,
        "summary": summary,
        "run12_roi_result": run12_row,
        "phi_safe": True,
    }


def summarize_roi_sweep_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("verdict") or "unknown") for row in rows)
    total = max(1, len(rows))
    focal_rows = int(counts.get("focal_candidate", 0))
    focal_fraction = focal_rows / total
    if focal_rows and focal_fraction <= 0.35:
        interpretation = "localized_candidate_signal"
        localized_focal = True
    elif focal_rows:
        interpretation = "non_focal_broad_signal"
        localized_focal = False
    else:
        interpretation = "non_focal_all_rois"
        localized_focal = False
    return {
        "verdict_counts": dict(sorted(counts.items())),
        "focal_candidate_row_fraction": round(focal_fraction, 3),
        "sweep_interpretation": interpretation,
        "any_focal_candidate": localized_focal,
        "auto_promote_allowed": False,
        "reason": "ROI sweep is diagnostic-only and cannot create or upgrade a supported finding.",
    }


def run_cross_case_harness(case_dirs: Iterable[str | Path]) -> dict[str, Any]:
    rows = []
    for index, raw_path in enumerate(case_dirs, start=1):
        path = Path(raw_path)
        row: dict[str, Any] = {"case_index": index, "case_id": f"case_{index:02d}"}
        try:
            graph = StudyGraphBuilder(path).build()
            candidate_set = LumbarSpineEvidenceModule().analyze(graph).to_dict()
            candidates = candidate_set.get("candidates") or []
            candidate = candidates[0] if candidates else {}
            sweep = run_roi_sweep(graph, candidate) if candidate else _empty_roi_sweep("No candidate generated.")
            row.update({
                "source_type": graph.source_type,
                "modality": graph.modality,
                "series_count": len(graph.series),
                "candidate_count": len(candidates),
                "geometry_confidence": candidate.get("geometry_confidence"),
                "registration_confidence": candidate.get("registration_confidence"),
                "roi_any_focal_candidate": bool((sweep.get("summary") or {}).get("any_focal_candidate")),
                "roi_verdict_counts": (sweep.get("summary") or {}).get("verdict_counts", {}),
            })
        except Exception as exc:
            row.update({
                "candidate_count": 0,
                "error_type": type(exc).__name__,
            })
        rows.append(row)
    return {
        "case_count": len(rows),
        "rows": rows,
        "generalized": len(rows) >= 2,
        "phi_safe": True,
    }


def decide_l5s1_evidence_adequacy(
    *,
    reference_category: str,
    sequence_report: dict[str, Any],
    roi_sweep: dict[str, Any],
    cross_case: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ref = reference_category if reference_category in REFERENCE_CATEGORIES else "unclear"
    sweep_summary = (roi_sweep or {}).get("summary") or {}
    any_focal = bool(sweep_summary.get("any_focal_candidate"))
    better_sequence = bool((sequence_report or {}).get("better_suited_sequence_found"))
    generalized = bool((cross_case or {}).get("generalized"))

    if better_sequence and any_focal:
        classification = DECISION_RECOVERABLE
        true_miss = True
        reason = "A better-suited sequence or ROI yielded reproducible focal signal; fix evidence selection before ML."
    elif any_focal and generalized:
        classification = DECISION_LOCALIZATION_LIMITED
        true_miss = False
        reason = "Focal signal is plausible but localization confidence is limited across cases; segmentation ML may be justified."
    else:
        classification = DECISION_CORRECT
        true_miss = False
        if (sweep_summary.get("sweep_interpretation") == "non_focal_broad_signal"):
            reason = (
                "Subtraction signal appears broadly across plausible ROIs rather than as a localized focal candidate; "
                "cannot_assess is the conservative result."
            )
        else:
            reason = (
                "No reproducible localized focal signal was found across plausible ROIs on the available matched "
                "pre/post sequence, or the reference category is expected postoperative scar."
            )

    return {
        "classification": classification,
        "reference_category": ref,
        "true_miss": true_miss,
        "ml_justified": classification == DECISION_LOCALIZATION_LIMITED,
        "reason": reason,
        "no_synthesis": True,
    }


def assert_phi_safe_payload(payload: Any) -> None:
    """Raise if diagnostic output contains obvious raw paths, images, PDFs, or report text markers."""
    text = json.dumps(payload, sort_keys=True, default=str)
    unsafe_patterns = (
        r"[A-Za-z]:\\",
        r"/Users/",
        r"\\\\",
        r"\.(dcm|dicom|ima|png|jpg|jpeg|pdf)\b",
        r"Patient(Name|ID)|Accession|DOB|Date of Birth",
    )
    for pattern in unsafe_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise ValueError(f"PHI-unsafe diagnostic payload matched pattern: {pattern}")


def _empty_roi_sweep(reason: str) -> dict[str, Any]:
    return {
        "roi_count": 0,
        "pair_count": 0,
        "rows": [],
        "summary": {
            "verdict_counts": {},
            "any_focal_candidate": False,
            "auto_promote_allowed": False,
            "reason": reason,
        },
        "phi_safe": True,
    }


def _sequence_role(series: StudySeries) -> str:
    if series.plane == "axial" and series.sequence == "t1" and series.contrast_phase == "post_contrast":
        return "post_contrast_axial_candidate"
    if series.plane == "axial" and series.sequence == "t1" and series.contrast_phase == "pre_contrast":
        return "pre_contrast_axial_context"
    if series.contrast_phase == "post_contrast":
        return "post_contrast_non_axial_context"
    return "context_or_not_applicable"


def _phi_safe_series_label(series: StudySeries) -> str:
    descriptors = []
    desc = f"{series.description} {series.protocol_name}".lower()
    for token in ("tse", "vibe", "fs", "stir", "tirm", "sag", "tra", "cor"):
        if token in desc and token not in descriptors:
            descriptors.append(token)
    if "l spine" in desc or "lumbar" in desc:
        descriptors.append("lumbar")
    base = "_".join(v for v in (series.plane, series.sequence, series.contrast_phase) if v)
    suffix = "_".join(descriptors) if descriptors else "series"
    return f"{base}_{suffix}_{series.slice_count}sl"


def _prepost_geometry_summary(pre: StudySeries, post: StudySeries) -> dict[str, Any]:
    if not pre.slices or not post.slices:
        return {"same_geometry": False, "overlap_slice_pairs": 0, "mean_pair_distance_mm": None, "max_pair_distance_mm": None}
    distances = []
    for post_slice in post.slices:
        nearest = _nearest_by_distance(pre.slices, post_slice)
        if nearest is None:
            continue
        distances.append(nearest)
    accepted = [d for d in distances if d <= 3.0]
    same_geometry = (
        pre.rows == post.rows
        and pre.columns == post.columns
        and pre.pixel_spacing == post.pixel_spacing
        and pre.image_orientation_patient == post.image_orientation_patient
        and len(accepted) >= max(1, min(3, len(post.slices)))
    )
    return {
        "same_geometry": bool(same_geometry),
        "overlap_slice_pairs": len(accepted),
        "mean_pair_distance_mm": round(float(np.mean(accepted)), 3) if accepted else None,
        "max_pair_distance_mm": round(float(max(accepted)), 3) if accepted else None,
    }


def _nearest_by_distance(slices: list[StudySlice], target: StudySlice) -> Optional[float]:
    distances = [
        _physical_slice_distance_mm(slice_obj, target)
        for slice_obj in slices
    ]
    distances = [float(d) for d in distances if d is not None]
    return min(distances) if distances else None


def _pair_suitability_score(pre: StudySeries, post: StudySeries, geometry: dict[str, Any]) -> float:
    score = 0.0
    if geometry.get("same_geometry"):
        score += 0.35
    if post.plane == "axial" and post.sequence == "t1" and post.contrast_phase == "post_contrast":
        score += 0.30
    desc = f"{pre.description} {post.description} {pre.protocol_name} {post.protocol_name}".lower()
    if "tse" in desc:
        score += 0.12
    if "vibe" in desc:
        score += 0.04
    if "fs" in desc or "fat" in desc:
        score += 0.08
    if "l spine" in desc or "lumbar" in desc:
        score += 0.08
    if 10 <= post.slice_count <= 40:
        score += 0.07
    elif post.slice_count > 50:
        score -= 0.03
    return round(max(0.0, min(1.0, score)), 3)


def _suitability_label(score: float, geometry: dict[str, Any]) -> str:
    if not geometry.get("same_geometry"):
        return "not_matched"
    if score >= 0.85:
        return "high"
    if score >= 0.70:
        return "moderate"
    return "limited"


def _roi_pair_signal(pre_slice: StudySlice, post_slice: StudySlice, roi_spec: dict[str, Any]) -> dict[str, Any]:
    pre = _read_slice_array(pre_slice)
    post = _read_slice_array(post_slice)
    if pre is None or post is None or pre.shape != post.shape:
        return {}
    reg = _registration_metric(pre_slice, post_slice)
    if not reg.get("passed"):
        return {}
    dy, dx = [int(v) for v in (reg.get("translation_pixels") or [0, 0])]
    registered_post = _shift_array(post, dy, dx)
    diff = _difference_array(pre, registered_post)
    y0, y1, x0, x1 = _roi_bounds(diff.shape, roi_spec)
    roi = diff[y0:y1, x0:x1]
    if roi.size == 0:
        return {}
    mask = np.ones(diff.shape, dtype=bool)
    mask[y0:y1, x0:x1] = False
    background = diff[mask]
    roi_p95 = float(np.percentile(roi, 95))
    bg_p95 = float(np.percentile(background, 95)) if background.size else 0.0
    ratio = roi_p95 / (bg_p95 + 1e-6)
    contrast = roi_p95 - bg_p95
    focal_pair = bool(roi_p95 >= 0.12 and ratio >= 1.35 and contrast >= 0.05)
    return {
        "ratio": round(float(ratio), 3),
        "contrast": round(float(contrast), 3),
        "roi_p95": round(roi_p95, 3),
        "background_p95": round(bg_p95, 3),
        "focal_pair": focal_pair,
    }


def _roi_bounds(shape: tuple[int, ...], roi_spec: dict[str, Any]) -> tuple[int, int, int, int]:
    rows, cols = int(shape[0]), int(shape[1])
    width = float(roi_spec["width"])
    height = float(roi_spec["height"])
    cx = float(roi_spec["center_x"])
    cy = float(roi_spec["center_y"])
    x0 = int(round((cx - width / 2.0) * cols))
    y0 = int(round((cy - height / 2.0) * rows))
    x1 = int(round((cx + width / 2.0) * cols))
    y1 = int(round((cy + height / 2.0) * rows))
    x0 = max(0, min(cols - 1, x0))
    y0 = max(0, min(rows - 1, y0))
    x1 = max(x0 + 1, min(cols, x1))
    y1 = max(y0 + 1, min(rows, y1))
    return y0, y1, x0, x1


def _roi_verdict(focal_pairs: int, pair_count: int) -> str:
    if pair_count <= 0:
        return "insufficient"
    if focal_pairs >= 2:
        return "focal_candidate"
    if focal_pairs == 1:
        return "single_slice_signal"
    return "non_focal"

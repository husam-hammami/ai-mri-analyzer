"""
Deterministic lumbar spine evidence module.

This module localizes candidate evidence only. It does not decide whether
postoperative tissue represents scar, residual/recurrent disc, or nerve-root
encasement; that remains a Claude/verifier responsibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import pydicom
except Exception:  # pragma: no cover - optional runtime dependency
    pydicom = None

from core.anatomy_modules.base import EvidenceCandidate, EvidenceCandidateSet
from core.study_graph import StudyGraph, StudySeries, StudySlice, normalize_contrast_pair_key

logger = logging.getLogger("mika.lumbar_evidence")

LUMBAR_DISC_LEVELS = ("L1-L2", "L2-L3", "L3-L4", "L4-L5", "L5-S1")
SLICE_PAIR_MAX_DISTANCE_MM = 3.0
REGISTRATION_SEARCH_PIXELS = 6
REGISTRATION_MIN_CORRELATION = 0.88
SAGITTAL_DISC_X_FRACTION = (0.28, 0.65)
SAGITTAL_L5S1_ROW_FRACTION = (0.54, 0.82)
AXIAL_PROJECTION_WINDOW_SLICES = 2


@dataclass
class LevelSliceRange:
    level: str
    slice_ids: list[str]
    evidence_refs: list[str]
    coordinate_range: Optional[tuple[float, float]]
    confidence: float
    limitations: list[str] = field(default_factory=list)


@dataclass
class RegistrationQC:
    pre_series_id: str
    post_series_id: str
    passed: bool
    confidence: float
    matched_slice_ids: list[str] = field(default_factory=list)
    matched_evidence_refs: list[str] = field(default_factory=list)
    slice_pairs: list[tuple[StudySlice, StudySlice, float]] = field(default_factory=list, repr=False)
    pair_metrics: list[dict] = field(default_factory=list)
    registration_metrics: list[dict] = field(default_factory=list)
    difference_map_allowed: bool = False
    mean_pair_distance_mm: Optional[float] = None
    max_pair_distance_mm: Optional[float] = None
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pre_series_id": self.pre_series_id,
            "post_series_id": self.post_series_id,
            "passed": self.passed,
            "confidence": self.confidence,
            "matched_slice_ids": self.matched_slice_ids,
            "matched_evidence_refs": self.matched_evidence_refs,
            "pair_metrics": self.pair_metrics,
            "registration_metrics": self.registration_metrics,
            "difference_map_allowed": self.difference_map_allowed,
            "mean_pair_distance_mm": self.mean_pair_distance_mm,
            "max_pair_distance_mm": self.max_pair_distance_mm,
            "limitations": self.limitations,
        }


class LumbarSpineEvidenceModule:
    module_id = "lumbar_spine"

    def __init__(
        self,
        *,
        proof_bundle_dir: Optional[str | Path] = None,
        proof_relative_prefix: str = "evidence/cv_proof",
    ):
        self.proof_bundle_dir = Path(proof_bundle_dir) if proof_bundle_dir else None
        self.proof_relative_prefix = proof_relative_prefix.strip("/\\")

    def analyze(self, study_graph: StudyGraph) -> EvidenceCandidateSet:
        limitations = list(study_graph.limitations)
        if study_graph.source_type != "dicom":
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + [
                    "Cannot generate lumbar geometry candidates from image exports; DICOM orientation, position, and PixelSpacing are absent."
                ],
            )
        if "MR" not in (study_graph.modality or ""):
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + ["Lumbar contrast evidence module only runs on MR DICOM studies."],
            )

        diagnostic = [s for s in study_graph.series if not s.is_localizer]
        sagittal = self.detect_sagittal_lumbar_mr_series(diagnostic)
        axial = self.detect_axial_lumbar_mr_series(diagnostic)
        if not sagittal:
            limitations.append("No sagittal lumbar MR series with usable metadata was detected; level localization is limited.")
        if not axial:
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + ["No axial lumbar MR series with usable metadata was detected."],
            )

        pair = self.detect_matching_pre_post_axial_series(axial)
        if not pair:
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + [
                    "No same-geometry axial T1 pre/post contrast pair was detected; postoperative enhancement comparison cannot be localized."
                ],
            )
        pre_series, post_series = pair
        level_ranges = self.map_sagittal_disc_levels_to_axial_ranges(sagittal[0] if sagittal else None, post_series)
        l5s1 = level_ranges.get("L5-S1")
        if not l5s1 or not l5s1.slice_ids:
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + [
                    "Could not map L5-S1 to axial slices from DICOM physical coordinates; candidate suppressed."
                ],
            )

        registration = self.registration_qc(pre_series, post_series, l5s1.slice_ids)
        if not registration.passed:
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + [
                    "Pre/post contrast registration failed QC; postoperative enhancement candidate is cannot-assess.",
                    *registration.limitations,
                ],
            )

        roi = self.lateral_recess_roi(post_series, side="left")
        adjacent_refs = self.adjacent_slice_refs(post_series, l5s1.slice_ids)
        proof_bundle = self.build_candidate_proof_bundle(
            candidate_id="lumbar_l5_s1_left_prepost_lateral_recess_001",
            registration=registration,
            roi=roi,
            level="L5-S1",
            side="left",
        )
        calibration_state = "calibrated" if pre_series.calibrated and post_series.calibrated else "uncalibrated"
        if calibration_state != "calibrated":
            return EvidenceCandidateSet(
                module=self.module_id,
                limitations=limitations + [
                    "Matched axial series are missing PixelSpacing; precise geometry-derived candidate suppressed."
                ],
            )

        candidate = EvidenceCandidate(
            candidate_id="lumbar_l5_s1_left_prepost_lateral_recess_001",
            anatomy="lumbar_spine",
            level="L5-S1",
            side="left",
            series_ids=[pre_series.series_id, post_series.series_id],
            slice_ids=registration.matched_slice_ids,
            candidate_type="pre_post_contrast_lateral_recess_roi",
            roi=roi,
            calibration_state=calibration_state,
            geometry_confidence=l5s1.confidence,
            registration_confidence=registration.confidence,
            limitations=[
                "CV localized a left L5-S1 lateral recess / postoperative-bed ROI only.",
                "CV does not classify scar versus residual/recurrent disc and does not assess nerve-root encasement.",
                "Claude/verifier must decide supported, not_supported, cannot_assess, localization_wrong, or unstable.",
                *l5s1.limitations,
                *registration.limitations,
            ],
            evidence_refs=sorted(set([*l5s1.evidence_refs, *registration.matched_evidence_refs])),
            physical_pair_distances=registration.pair_metrics,
            registration_qc=registration.to_dict(),
            adjacent_slice_refs=adjacent_refs,
            proof_bundle=proof_bundle,
            contrast_timing=self.contrast_timing(pre_series, post_series),
            bounded_question=(
                "Does this focused evidence support abnormal enhancing tissue in the left "
                "L5-S1 lateral recess/operative-bed region?"
            ),
        )
        return EvidenceCandidateSet(
            module=self.module_id,
            candidates=[candidate],
            limitations=limitations,
        )

    def detect_sagittal_lumbar_mr_series(self, series: list[StudySeries]) -> list[StudySeries]:
        return sorted(
            [
                s for s in series
                if s.modality == "MR" and s.plane == "sagittal" and s.slice_count >= 3
            ],
            key=lambda s: (0 if s.sequence in {"t2", "t1", "stir"} else 1, s.series_number or 9999, s.description),
        )

    def detect_axial_lumbar_mr_series(self, series: list[StudySeries]) -> list[StudySeries]:
        return sorted(
            [
                s for s in series
                if s.modality == "MR" and s.plane == "axial" and s.slice_count >= 2
            ],
            key=lambda s: (0 if s.sequence == "t1" else 1, s.series_number or 9999, s.description),
        )

    def detect_matching_pre_post_axial_series(self, axial_series: list[StudySeries]) -> Optional[tuple[StudySeries, StudySeries]]:
        pairs: list[tuple[float, StudySeries, StudySeries]] = []
        pres = [s for s in axial_series if s.sequence == "t1" and s.contrast_phase == "pre_contrast"]
        posts = [s for s in axial_series if s.sequence == "t1" and s.contrast_phase == "post_contrast"]
        for pre in pres:
            for post in posts:
                key_score = 0.15 if normalize_contrast_pair_key(pre.description) == normalize_contrast_pair_key(post.description) else 0.0
                qc = self.registration_qc(pre, post, [sl.slice_id for sl in post.slices])
                if qc.passed:
                    pairs.append((qc.confidence + key_score, pre, post))
        if not pairs:
            return None
        _, pre, post = sorted(pairs, key=lambda item: item[0], reverse=True)[0]
        return pre, post

    def map_sagittal_disc_levels_to_axial_ranges(
        self,
        sagittal_series: Optional[StudySeries],
        axial_series: StudySeries,
    ) -> dict[str, LevelSliceRange]:
        slices = _sorted_slices_by_axis(axial_series)
        limitations: list[str] = []
        if len(slices) < 5:
            ordered = axial_series.sorted_slices()
            slices = ordered if len(ordered) >= 5 else []
            limitations.append("Axial slices lack complete physical coordinates; instance-number fallback used.")
        if len(slices) < 5:
            return {}

        base_conf = 0.55
        if sagittal_series is not None:
            base_conf += 0.12
        if all(sl.superior_inferior_position is not None for sl in slices):
            base_conf += 0.08
        if axial_series.pixel_spacing and axial_series.image_orientation_patient:
            base_conf += 0.03
        if len(slices) >= 15:
            base_conf += 0.03
        # This is deterministic localization, not vertebral segmentation; keep below proof-marker trust.
        confidence = min(0.78, round(base_conf, 2))
        if confidence < 0.80:
            limitations.append("Level assignment uses DICOM geometry with approximate lumbar binning; verifier must reject wrong-level localization.")

        out: dict[str, LevelSliceRange] = {}
        n = len(slices)
        projected_l5s1 = self.project_l5s1_from_sagittal(sagittal_series, axial_series)
        for level_idx, level in enumerate(LUMBAR_DISC_LEVELS):
            start = int(round(level_idx * n / len(LUMBAR_DISC_LEVELS)))
            end = int(round((level_idx + 1) * n / len(LUMBAR_DISC_LEVELS)))
            group = slices[start:max(start + 1, end)]
            if level == "L5-S1" and projected_l5s1 is not None:
                out[level] = projected_l5s1
                continue
            if level == "L5-S1" and len(group) > 6:
                keep = max(5, int(round(len(group) * 0.45)))
                group = group[:keep]
                terminal_limitations = list(limitations) + [
                    "L5-S1 target uses the superior terminal-bin slices to avoid caudal pelvic-tail over-selection."
                ]
            else:
                terminal_limitations = list(limitations)
            coords = [sl.superior_inferior_position for sl in group if sl.superior_inferior_position is not None]
            out[level] = LevelSliceRange(
                level=level,
                slice_ids=[sl.slice_id for sl in group],
                evidence_refs=[sl.evidence_ref for sl in group],
                coordinate_range=(min(coords), max(coords)) if coords else None,
                confidence=confidence,
                limitations=terminal_limitations,
            )
        return out

    def project_l5s1_from_sagittal(
        self,
        sagittal_series: Optional[StudySeries],
        axial_series: StudySeries,
    ) -> Optional[LevelSliceRange]:
        if sagittal_series is None:
            return None
        axial_normal = _slice_normal(axial_series.image_orientation_patient)
        if axial_normal is None:
            return None
        sagittal_slices = [
            sl for sl in sagittal_series.slices
            if sl.image_position_patient is not None and sl.image_orientation_patient is not None
        ]
        if not sagittal_slices:
            return None
        sagittal_slices = sorted(
            sagittal_slices,
            key=lambda sl: (
                _slice_axis_position(sl, sagittal_series.image_orientation_patient) is None,
                _slice_axis_position(sl, sagittal_series.image_orientation_patient) or 0.0,
                sl.instance_number or 0,
                sl.slice_id,
            ),
        )
        mid_slice = sagittal_slices[len(sagittal_slices) // 2]
        arr = _read_slice_array(mid_slice)
        if arr is None:
            return None
        detected = _detect_l5s1_sagittal_disc_row(arr)
        if detected is None:
            return None
        row, col, detection_confidence, detection_note = detected
        point = _patient_point_from_pixel(mid_slice, row=row, column=col)
        if point is None:
            return None
        target_projection = float(np.dot(point, axial_normal))
        near = _slices_near_axis_projection(axial_series, target_projection, AXIAL_PROJECTION_WINDOW_SLICES)
        if not near:
            return None
        nearest_distance = min(abs(position - target_projection) for _sl, position in near)
        if nearest_distance > max(8.0, float(axial_series.slice_thickness or 0.0) * 2.0):
            return None
        group = [sl for sl, _position in near]
        coords = [_slice_axis_position(sl, axial_series.image_orientation_patient) for sl in group]
        coords = [float(c) for c in coords if c is not None]
        confidence = min(0.78, round(0.70 + 0.08 * detection_confidence, 2))
        limitations = [
            "L5-S1 target projected from a central sagittal disc-band estimate into the axial stack.",
            "Sagittal projection is deterministic localization only; verifier must reject wrong-level localization.",
            detection_note,
        ]
        return LevelSliceRange(
            level="L5-S1",
            slice_ids=[sl.slice_id for sl in group],
            evidence_refs=[sl.evidence_ref for sl in group],
            coordinate_range=(min(coords), max(coords)) if coords else None,
            confidence=confidence,
            limitations=limitations,
        )

    def registration_qc(self, pre_series: StudySeries, post_series: StudySeries, target_post_slice_ids: list[str]) -> RegistrationQC:
        limitations: list[str] = []
        if pre_series.rows != post_series.rows or pre_series.columns != post_series.columns:
            limitations.append("Pre/post rows or columns differ.")
        if not _spacing_close(pre_series.pixel_spacing, post_series.pixel_spacing):
            limitations.append("Pre/post PixelSpacing differs or is missing.")
        if not _orientation_close(pre_series.image_orientation_patient, post_series.image_orientation_patient):
            limitations.append("Pre/post ImageOrientationPatient differs or is missing.")

        post_targets = [sl for sl in post_series.slices if sl.slice_id in set(target_post_slice_ids)]
        matched: list[tuple[StudySlice, StudySlice, float]] = []
        pair_metrics: list[dict] = []
        for post_slice in post_targets:
            pre_slice, distance = _nearest_slice_by_physical_distance(pre_series.slices, post_slice)
            metric = {
                "post_slice_id": post_slice.slice_id,
                "post_evidence_ref": post_slice.evidence_ref,
                "pre_slice_id": pre_slice.slice_id if pre_slice else "",
                "pre_evidence_ref": pre_slice.evidence_ref if pre_slice else "",
                "distance_mm": round(float(distance), 3) if distance is not None else None,
                "accepted": bool(pre_slice is not None and distance is not None and distance <= SLICE_PAIR_MAX_DISTANCE_MM),
                "threshold_mm": SLICE_PAIR_MAX_DISTANCE_MM,
            }
            pair_metrics.append(metric)
            if metric["accepted"]:
                matched.append((pre_slice, post_slice, float(distance)))  # type: ignore[arg-type]
        if not matched:
            limitations.append("No overlapping pre/post slices at the target level within physical distance threshold.")
        if len(matched) < max(1, min(3, len(post_targets))):
            limitations.append("Too few overlapping pre/post slices for reliable same-geometry comparison.")

        registration_metrics: list[dict] = []
        difference_map_allowed = False
        if matched and not limitations:
            registration_metrics = [_registration_metric(pre, post) for pre, post, _distance in matched]
            failed_metrics = [m for m in registration_metrics if not m.get("passed")]
            if failed_metrics:
                limitations.append("Translation registration QC failed; subtraction/difference maps are suppressed.")
            else:
                difference_map_allowed = True

        passed = not limitations
        distances = [distance for _pre, _post, distance in matched]
        confidence = 0.0
        if passed:
            coverage = len(matched) / max(1, len(post_targets))
            mean_distance = float(np.mean(distances)) if distances else SLICE_PAIR_MAX_DISTANCE_MM
            distance_score = max(0.0, 1.0 - mean_distance / max(SLICE_PAIR_MAX_DISTANCE_MM, 0.001))
            reg_score = min((m.get("confidence", 0.0) for m in registration_metrics), default=0.85)
            confidence = min(0.96, round(0.70 + 0.12 * coverage + 0.08 * distance_score + 0.06 * reg_score, 2))
        return RegistrationQC(
            pre_series_id=pre_series.series_id,
            post_series_id=post_series.series_id,
            passed=passed,
            confidence=confidence,
            matched_slice_ids=[sl.slice_id for pair in matched for sl in pair[:2]],
            matched_evidence_refs=[sl.evidence_ref for pair in matched for sl in pair[:2]],
            slice_pairs=matched,
            pair_metrics=pair_metrics,
            registration_metrics=registration_metrics,
            difference_map_allowed=difference_map_allowed,
            mean_pair_distance_mm=round(float(np.mean(distances)), 3) if distances else None,
            max_pair_distance_mm=round(float(max(distances)), 3) if distances else None,
            limitations=limitations,
        )

    def adjacent_slice_refs(self, series: StudySeries, target_slice_ids: list[str]) -> list[str]:
        ordered = series.sorted_slices()
        target_set = set(target_slice_ids)
        refs: list[str] = []
        for idx, sl in enumerate(ordered):
            if sl.slice_id not in target_set:
                continue
            for neighbor_idx in (idx - 1, idx + 1):
                if 0 <= neighbor_idx < len(ordered):
                    ref = ordered[neighbor_idx].evidence_ref
                    if ref not in refs:
                        refs.append(ref)
        return refs

    def contrast_timing(self, pre_series: StudySeries, post_series: StudySeries) -> dict:
        return {
            "pre_acquisition_time": pre_series.acquisition_time or "",
            "post_acquisition_time": post_series.acquisition_time or "",
            "pre_series_time": pre_series.series_time or "",
            "post_series_time": post_series.series_time or "",
            "pre_contrast_bolus_agent_present": bool(pre_series.contrast_bolus_agent),
            "post_contrast_bolus_agent_present": bool(post_series.contrast_bolus_agent),
            "pre_contrast_bolus_start_time": pre_series.contrast_bolus_start_time or "",
            "post_contrast_bolus_start_time": post_series.contrast_bolus_start_time or "",
        }

    def build_candidate_proof_bundle(
        self,
        *,
        candidate_id: str,
        registration: RegistrationQC,
        roi: dict,
        level: str,
        side: str,
    ) -> dict:
        if not self.proof_bundle_dir:
            return {
                "bundle_id": f"{candidate_id}_proof",
                "status": "not_generated",
                "visibility": "internal_candidate_review_only",
                "trusted_for_patient_ui": False,
                "limitations": ["No proof bundle output directory was configured."],
            }
        pairs = _pairs_from_registration(registration)
        if not pairs:
            return {
                "bundle_id": f"{candidate_id}_proof",
                "status": "not_generated",
                "visibility": "internal_candidate_review_only",
                "trusted_for_patient_ui": False,
                "limitations": ["No accepted pre/post slice pairs were available for proof bundle generation."],
            }
        selected = _central_with_neighbors(pairs)
        images: list[dict] = []
        limitations: list[str] = []
        self.proof_bundle_dir.mkdir(parents=True, exist_ok=True)
        for idx, pair in enumerate(selected, start=1):
            pre = pair["pre_slice"]
            post = pair["post_slice"]
            pre_arr = _read_slice_array(pre)
            post_arr = _read_slice_array(post)
            if pre_arr is None or post_arr is None:
                limitations.append(f"Could not read pixels for proof pair {idx}; image omitted.")
                continue
            reg_metric = _registration_metric(pre, post) if registration.difference_map_allowed else {"passed": False}
            shift = tuple(reg_metric.get("translation_pixels") or (0, 0))
            registered_post = _shift_array(post_arr, int(shift[0]), int(shift[1])) if reg_metric.get("passed") else post_arr
            for kind, arr, sl in (
                ("pre_slice", pre_arr, pre),
                ("post_slice", post_arr, post),
            ):
                rel = self._save_proof_image(
                    candidate_id=candidate_id,
                    kind=kind,
                    index=idx,
                    arr=arr,
                    roi=roi,
                    level=level,
                    side=side,
                    evidence_ref=sl.evidence_ref,
                    registration_metric=reg_metric if kind == "post_slice" else {},
                )
                images.append({
                    "kind": kind,
                    "relative_path": rel,
                    "evidence_ref": sl.evidence_ref,
                    "label": "localization candidate, not diagnosis",
                    "trusted_for_patient_ui": False,
                })
            if registration.difference_map_allowed and reg_metric.get("passed"):
                diff = _difference_array(pre_arr, registered_post)
                rel = self._save_proof_image(
                    candidate_id=candidate_id,
                    kind="registered_difference",
                    index=idx,
                    arr=diff,
                    roi=roi,
                    level=level,
                    side=side,
                    evidence_ref=f"{pre.evidence_ref}|{post.evidence_ref}",
                    registration_metric=reg_metric,
                )
                images.append({
                    "kind": "registered_difference",
                    "relative_path": rel,
                    "evidence_ref": f"{pre.evidence_ref}|{post.evidence_ref}",
                    "label": "registered difference, localization candidate, not diagnosis",
                    "trusted_for_patient_ui": False,
                })
        return {
            "bundle_id": f"{candidate_id}_proof",
            "status": "generated" if images else "not_generated",
            "visibility": "internal_candidate_review_only",
            "trusted_for_patient_ui": False,
            "bounded_question": (
                "Does this focused evidence support abnormal enhancing tissue in the "
                "left L5-S1 lateral recess/operative-bed region?"
            ),
            "images": images,
            "slice_pair_refs": [
                {
                    "pre": pair["pre_slice"].evidence_ref,
                    "post": pair["post_slice"].evidence_ref,
                    "distance_mm": pair["distance_mm"],
                    "role": pair["role"],
                }
                for pair in selected
            ],
            "limitations": sorted(set([*limitations, *registration.limitations])),
        }

    def _save_proof_image(
        self,
        *,
        candidate_id: str,
        kind: str,
        index: int,
        arr: np.ndarray,
        roi: dict,
        level: str,
        side: str,
        evidence_ref: str,
        registration_metric: dict,
    ) -> str:
        image = Image.fromarray(_normalize_uint8(arr)).convert("RGB")
        draw = ImageDraw.Draw(image)
        pb = roi.get("pixel_bounds") or {}
        x = int(pb.get("x") or 0)
        y = int(pb.get("y") or 0)
        w = int(pb.get("width") or 0)
        h = int(pb.get("height") or 0)
        if w > 0 and h > 0:
            draw.rectangle([x, y, x + w, y + h], outline=(255, 230, 0), width=3)
        label = f"{side} {level} ROI - localization candidate, not diagnosis"
        if registration_metric:
            label += f" | reg {registration_metric.get('confidence', 0):.2f}"
        font = ImageFont.load_default()
        draw.rectangle([0, 0, image.width, 18], fill=(0, 0, 0))
        draw.text((4, 4), label[:110], fill=(255, 255, 255), font=font)
        draw.text((4, max(22, image.height - 14)), str(evidence_ref)[:110], fill=(255, 255, 0), font=font)
        safe_kind = kind.replace(" ", "_")
        filename = f"{candidate_id}_{index:02d}_{safe_kind}.png"
        out_path = self.proof_bundle_dir / filename
        image.save(out_path)
        return f"{self.proof_relative_prefix}/{filename}".replace("\\", "/")

    def lateral_recess_roi(self, series: StudySeries, side: str) -> dict:
        cols = max(1, int(series.columns or 1))
        rows = max(1, int(series.rows or 1))
        left_on_high_columns = _patient_left_on_high_columns(series)
        if side.lower() == "left":
            x_center = 0.59 if left_on_high_columns else 0.41
        else:
            x_center = 0.41 if left_on_high_columns else 0.59
        width = 0.10
        height = 0.20
        x = max(0.02, min(0.98 - width, x_center - width / 2))
        y = 0.40
        return {
            "unit": "normalized_image_fraction",
            "x": round(x, 3),
            "y": round(y, 3),
            "width": round(width, 3),
            "height": round(height, 3),
            "pixel_bounds": {
                "x": int(round(x * cols)),
                "y": int(round(y * rows)),
                "width": int(round(width * cols)),
                "height": int(round(height * rows)),
            },
            "target": "left lateral recess / postoperative bed",
            "side_basis": "ImageOrientationPatient row direction; DICOM LPS x-positive is patient left.",
        }


def _spacing_close(a: Optional[tuple[float, float]], b: Optional[tuple[float, float]], tolerance_mm: float = 0.05) -> bool:
    if not a or not b:
        return False
    return all(abs(float(x) - float(y)) <= tolerance_mm for x, y in zip(a, b))


def _orientation_close(a, b, tolerance: float = 0.02) -> bool:
    if not a or not b:
        return False
    return all(abs(float(x) - float(y)) <= tolerance for x, y in zip(a, b))


def _slice_axis_position(slice_obj: StudySlice, orientation=None) -> Optional[float]:
    if slice_obj.image_position_patient is None:
        return slice_obj.superior_inferior_position
    normal = _slice_normal(orientation or slice_obj.image_orientation_patient)
    if normal is None:
        return slice_obj.superior_inferior_position
    try:
        return float(np.dot(np.array(slice_obj.image_position_patient, dtype=float), normal))
    except Exception:
        return slice_obj.superior_inferior_position


def _sorted_slices_by_axis(series: StudySeries) -> list[StudySlice]:
    positioned = [
        (sl, _slice_axis_position(sl, series.image_orientation_patient))
        for sl in series.slices
    ]
    with_position = [(sl, pos) for sl, pos in positioned if pos is not None]
    if len(with_position) < 5:
        return []
    # Superior-to-inferior for lumbar binning. For oblique axial stacks this must use
    # slice-normal projection, not raw z, or broad volumes can select pelvic-tail slices.
    return [
        sl for sl, _pos in sorted(
            with_position,
            key=lambda item: (-(item[1] or 0.0), item[0].instance_number or 0, item[0].slice_id),
        )
    ]


def _slices_near_axis_projection(
    series: StudySeries,
    target_projection: float,
    window_slices: int,
) -> list[tuple[StudySlice, float]]:
    ordered = [
        (sl, _slice_axis_position(sl, series.image_orientation_patient))
        for sl in series.slices
    ]
    ordered = [(sl, float(pos)) for sl, pos in ordered if pos is not None]
    if not ordered:
        return []
    ordered = sorted(ordered, key=lambda item: (item[1], item[0].instance_number or 0, item[0].slice_id))
    nearest_idx, _nearest = min(
        enumerate(ordered),
        key=lambda item: (abs(item[1][1] - target_projection), item[1][0].instance_number or 0, item[1][0].slice_id),
    )
    start = max(0, nearest_idx - window_slices)
    end = min(len(ordered), nearest_idx + window_slices + 1)
    return sorted(
        ordered[start:end],
        key=lambda item: (-(item[1]), item[0].instance_number or 0, item[0].slice_id),
    )


def _patient_point_from_pixel(slice_obj: StudySlice, *, row: int, column: int) -> Optional[np.ndarray]:
    if slice_obj.image_position_patient is None or slice_obj.image_orientation_patient is None or not slice_obj.pixel_spacing:
        return None
    try:
        ipp = np.array(slice_obj.image_position_patient, dtype=float)
        row_cosine = np.array(slice_obj.image_orientation_patient[:3], dtype=float)
        column_cosine = np.array(slice_obj.image_orientation_patient[3:6], dtype=float)
        row_spacing, column_spacing = float(slice_obj.pixel_spacing[0]), float(slice_obj.pixel_spacing[1])
        return ipp + float(column) * column_spacing * row_cosine + float(row) * row_spacing * column_cosine
    except Exception:
        return None


def _detect_l5s1_sagittal_disc_row(arr: np.ndarray) -> Optional[tuple[int, int, float, str]]:
    if arr.ndim > 2:
        arr = arr[arr.shape[0] // 2]
    if arr.size == 0:
        return None
    image = _normalize_float(arr)
    rows, cols = image.shape[:2]
    if rows < 32 or cols < 32:
        return None
    x0 = max(0, int(round(cols * SAGITTAL_DISC_X_FRACTION[0])))
    x1 = min(cols, int(round(cols * SAGITTAL_DISC_X_FRACTION[1])))
    y0 = max(0, int(round(rows * SAGITTAL_L5S1_ROW_FRACTION[0])))
    y1 = min(rows, int(round(rows * SAGITTAL_L5S1_ROW_FRACTION[1])))
    if x1 <= x0 or y1 <= y0:
        return None
    profile = image[:, x0:x1].mean(axis=1)
    window = max(5, min(13, (rows // 36) * 2 + 1))
    kernel = np.ones(window, dtype="float32") / float(window)
    smoothed = np.convolve(profile, kernel, mode="same")
    search = smoothed[y0:y1]
    if search.size < 5:
        return None
    threshold = float(np.percentile(search, 55))
    radius = max(3, window)
    expected_fraction = 0.68
    peaks: list[tuple[float, int, float]] = []
    for row in range(y0 + radius, y1 - radius):
        local = smoothed[row - radius:row + radius + 1]
        value = float(smoothed[row])
        if value < threshold or value < float(local.max()):
            continue
        shoulder = min(float(local[:radius].min()), float(local[radius + 1:].min()))
        prominence = max(0.0, value - shoulder)
        expected_penalty = abs((row / max(rows, 1)) - expected_fraction) * 0.18
        score = value + 0.35 * prominence - expected_penalty
        peaks.append((score, row, prominence))
    if peaks:
        score, row, prominence = sorted(peaks, key=lambda item: (item[0], item[2], -abs(item[1] - int(rows * expected_fraction))), reverse=True)[0]
        confidence = max(0.35, min(1.0, 0.55 + prominence))
        note = "Sagittal L5-S1 row selected from a lower-lumbar disc-band intensity peak."
    else:
        row = int(round(rows * expected_fraction))
        score = float(smoothed[row])
        confidence = 0.30
        note = "Sagittal L5-S1 row used lower-lumbar fallback because no clear disc-band peak was found."
    column = int(round((x0 + x1) / 2))
    return int(row), int(column), float(confidence), note


def _nearest_slice_by_physical_distance(
    slices: list[StudySlice],
    target: StudySlice,
) -> tuple[Optional[StudySlice], Optional[float]]:
    candidates: list[tuple[float, int, str, StudySlice]] = []
    for sl in slices:
        distance = _physical_slice_distance_mm(sl, target)
        if distance is None:
            continue
        candidates.append((distance, sl.instance_number or 0, sl.slice_id, sl))
    if candidates:
        distance, _instance, _slice_id, sl = sorted(candidates, key=lambda item: (item[0], item[1], item[2]))[0]
        return sl, distance
    if target.instance_number is not None:
        for sl in sorted(slices, key=lambda item: (item.instance_number or 0, item.slice_id)):
            if sl.instance_number == target.instance_number:
                return sl, 0.0
    return None, None


def _physical_slice_distance_mm(a: StudySlice, b: StudySlice) -> Optional[float]:
    if a.image_position_patient is not None and b.image_position_patient is not None:
        avec = np.array(a.image_position_patient, dtype=float)
        bvec = np.array(b.image_position_patient, dtype=float)
        normal = _slice_normal(b.image_orientation_patient or a.image_orientation_patient)
        if normal is not None:
            return float(abs(np.dot(avec - bvec, normal)))
        return float(np.linalg.norm(avec - bvec))
    if a.superior_inferior_position is not None and b.superior_inferior_position is not None:
        return float(abs(float(a.superior_inferior_position) - float(b.superior_inferior_position)))
    return None


def _slice_normal(orientation) -> Optional[np.ndarray]:
    if not orientation:
        return None
    try:
        vals = [float(x) for x in orientation]
        row = np.array(vals[:3], dtype=float)
        col = np.array(vals[3:6], dtype=float)
        normal = np.cross(row, col)
        norm = float(np.linalg.norm(normal))
        if norm <= 0:
            return None
        return normal / norm
    except Exception:
        return None


def _patient_left_on_high_columns(series: StudySeries) -> bool:
    orientation = series.image_orientation_patient
    if not orientation:
        return True
    try:
        row_direction = np.array([float(x) for x in orientation[:3]], dtype=float)
        # DICOM LPS: +x is patient left. Pixel columns move along the row direction.
        return float(row_direction[0]) >= 0
    except Exception:
        return True


def _registration_metric(pre_slice: StudySlice, post_slice: StudySlice) -> dict:
    pre = _read_slice_array(pre_slice)
    post = _read_slice_array(post_slice)
    base = {
        "pre_slice_id": pre_slice.slice_id,
        "post_slice_id": post_slice.slice_id,
        "pre_evidence_ref": pre_slice.evidence_ref,
        "post_evidence_ref": post_slice.evidence_ref,
        "translation_pixels": [0, 0],
        "correlation": None,
        "mse": None,
        "confidence": 0.0,
        "passed": False,
    }
    if pre is None or post is None:
        base["limitation"] = "Slice pixels could not be read for registration QC."
        return base
    if pre.shape != post.shape:
        base["limitation"] = "Pre/post slice pixel arrays have different shapes."
        return base
    pre_n = _normalize_float(pre)
    post_n = _normalize_float(post)
    best = {"score": -2.0, "mse": float("inf"), "dy": 0, "dx": 0}
    for dy in range(-REGISTRATION_SEARCH_PIXELS, REGISTRATION_SEARCH_PIXELS + 1):
        for dx in range(-REGISTRATION_SEARCH_PIXELS, REGISTRATION_SEARCH_PIXELS + 1):
            shifted = _shift_array(post_n, dy, dx)
            score = _corrcoef(pre_n, shifted)
            mse = float(np.mean((pre_n - shifted) ** 2))
            if (score, -mse, -abs(dy) - abs(dx)) > (best["score"], -best["mse"], -abs(best["dy"]) - abs(best["dx"])):
                best = {"score": score, "mse": mse, "dy": dy, "dx": dx}
    confidence = max(0.0, min(1.0, (best["score"] + 1.0) / 2.0))
    base.update({
        "translation_pixels": [int(best["dy"]), int(best["dx"])],
        "correlation": round(float(best["score"]), 4),
        "mse": round(float(best["mse"]), 5),
        "confidence": round(float(confidence), 3),
        "passed": bool(best["score"] >= REGISTRATION_MIN_CORRELATION),
    })
    if not base["passed"]:
        base["limitation"] = "Pre/post image correlation below registration QC threshold."
    return base


def _read_slice_array(slice_obj: StudySlice) -> Optional[np.ndarray]:
    if pydicom is None or not slice_obj.path:
        return None
    try:
        ds = pydicom.dcmread(str(slice_obj.path), force=True)
        arr = ds.pixel_array.astype("float32")
        if arr.ndim > 2:
            arr = arr[arr.shape[0] // 2]
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
        intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
        return arr * slope + intercept
    except Exception:
        return None


def _normalize_float(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr, dtype="float32")
    low, high = np.percentile(arr, [1, 99])
    if high <= low:
        high = float(arr.max()) if arr.size else 1.0
        low = float(arr.min()) if arr.size else 0.0
    return np.clip((arr - low) / (high - low + 1e-6), 0.0, 1.0)


def _normalize_uint8(arr: np.ndarray) -> np.ndarray:
    return (_normalize_float(arr) * 255.0).astype("uint8")


def _corrcoef(a: np.ndarray, b: np.ndarray) -> float:
    av = a.ravel().astype("float32")
    bv = b.ravel().astype("float32")
    av = av - float(av.mean())
    bv = bv - float(bv.mean())
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denom <= 1e-8:
        return 0.0
    return float(np.dot(av, bv) / denom)


def _shift_array(arr: np.ndarray, dy: int, dx: int) -> np.ndarray:
    out = np.zeros_like(arr)
    src_y0 = max(0, -dy)
    src_y1 = arr.shape[0] - max(0, dy)
    src_x0 = max(0, -dx)
    src_x1 = arr.shape[1] - max(0, dx)
    dst_y0 = max(0, dy)
    dst_y1 = dst_y0 + max(0, src_y1 - src_y0)
    dst_x0 = max(0, dx)
    dst_x1 = dst_x0 + max(0, src_x1 - src_x0)
    if src_y1 > src_y0 and src_x1 > src_x0:
        out[dst_y0:dst_y1, dst_x0:dst_x1] = arr[src_y0:src_y1, src_x0:src_x1]
    return out


def _difference_array(pre: np.ndarray, registered_post: np.ndarray) -> np.ndarray:
    return np.maximum(_normalize_float(registered_post) - _normalize_float(pre), 0.0)


def _pairs_from_registration(registration: RegistrationQC) -> list[dict]:
    pairs = [
        {
            "pre_slice": pre,
            "post_slice": post,
            "distance_mm": round(float(distance), 3),
            "role": "target",
        }
        for pre, post, distance in registration.slice_pairs
    ]
    return sorted(
        pairs,
        key=lambda item: (
            _slice_axis_position(item["post_slice"]) is None,
            -(_slice_axis_position(item["post_slice"]) or 0.0),
            item["post_slice"].instance_number or 0,
            item["post_slice"].slice_id,
        ),
    )


def _central_with_neighbors(pairs: list[dict]) -> list[dict]:
    if len(pairs) <= 3:
        for item in pairs:
            item["role"] = "target"
        return pairs
    center = len(pairs) // 2
    selected = []
    for role, idx in (("adjacent_above", center - 1), ("target", center), ("adjacent_below", center + 1)):
        if 0 <= idx < len(pairs):
            item = dict(pairs[idx])
            item["role"] = role
            selected.append(item)
    return selected

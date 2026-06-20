"""
Deterministic lumbar spine evidence module.

This module localizes candidate evidence only. It does not decide whether
postoperative tissue represents scar, residual/recurrent disc, or nerve-root
encasement; that remains a Claude/verifier responsibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from core.anatomy_modules.base import EvidenceCandidate, EvidenceCandidateSet
from core.study_graph import StudyGraph, StudySeries, StudySlice, normalize_contrast_pair_key

logger = logging.getLogger("mika.lumbar_evidence")

LUMBAR_DISC_LEVELS = ("L1-L2", "L2-L3", "L3-L4", "L4-L5", "L5-S1")


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
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pre_series_id": self.pre_series_id,
            "post_series_id": self.post_series_id,
            "passed": self.passed,
            "confidence": self.confidence,
            "matched_slice_ids": self.matched_slice_ids,
            "matched_evidence_refs": self.matched_evidence_refs,
            "limitations": self.limitations,
        }


class LumbarSpineEvidenceModule:
    module_id = "lumbar_spine"

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
                "Claude/verifier must decide supported, not_supported, cannot_assess, or localization_wrong.",
                *l5s1.limitations,
            ],
            evidence_refs=sorted(set([*l5s1.evidence_refs, *registration.matched_evidence_refs])),
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
        slices = [sl for sl in axial_series.sorted_slices() if sl.superior_inferior_position is not None]
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
        for level_idx, level in enumerate(LUMBAR_DISC_LEVELS):
            start = int(round(level_idx * n / len(LUMBAR_DISC_LEVELS)))
            end = int(round((level_idx + 1) * n / len(LUMBAR_DISC_LEVELS)))
            group = slices[start:max(start + 1, end)]
            coords = [sl.superior_inferior_position for sl in group if sl.superior_inferior_position is not None]
            out[level] = LevelSliceRange(
                level=level,
                slice_ids=[sl.slice_id for sl in group],
                evidence_refs=[sl.evidence_ref for sl in group],
                coordinate_range=(min(coords), max(coords)) if coords else None,
                confidence=confidence,
                limitations=list(limitations),
            )
        return out

    def registration_qc(self, pre_series: StudySeries, post_series: StudySeries, target_post_slice_ids: list[str]) -> RegistrationQC:
        limitations: list[str] = []
        if pre_series.rows != post_series.rows or pre_series.columns != post_series.columns:
            limitations.append("Pre/post rows or columns differ.")
        if not _spacing_close(pre_series.pixel_spacing, post_series.pixel_spacing):
            limitations.append("Pre/post PixelSpacing differs or is missing.")
        if not _orientation_close(pre_series.image_orientation_patient, post_series.image_orientation_patient):
            limitations.append("Pre/post ImageOrientationPatient differs or is missing.")

        post_targets = [sl for sl in post_series.slices if sl.slice_id in set(target_post_slice_ids)]
        matched: list[tuple[StudySlice, StudySlice]] = []
        for post_slice in post_targets:
            pre_slice = _nearest_slice_by_position(pre_series.slices, post_slice)
            if pre_slice is not None:
                matched.append((pre_slice, post_slice))
        if not matched:
            limitations.append("No overlapping pre/post slices at the target level.")
        if len(matched) < max(1, min(3, len(post_targets))):
            limitations.append("Too few overlapping pre/post slices for reliable same-geometry comparison.")

        passed = not limitations
        confidence = 0.0
        if passed:
            coverage = len(matched) / max(1, len(post_targets))
            confidence = min(0.94, round(0.82 + 0.12 * coverage, 2))
        return RegistrationQC(
            pre_series_id=pre_series.series_id,
            post_series_id=post_series.series_id,
            passed=passed,
            confidence=confidence,
            matched_slice_ids=[sl.slice_id for pair in matched for sl in pair],
            matched_evidence_refs=[sl.evidence_ref for pair in matched for sl in pair],
            limitations=limitations,
        )

    def lateral_recess_roi(self, series: StudySeries, side: str) -> dict:
        cols = max(1, int(series.columns or 1))
        rows = max(1, int(series.rows or 1))
        left_on_high_columns = _patient_left_on_high_columns(series)
        if side.lower() == "left":
            x_center = 0.70 if left_on_high_columns else 0.30
        else:
            x_center = 0.30 if left_on_high_columns else 0.70
        width = 0.24
        height = 0.30
        x = max(0.02, min(0.98 - width, x_center - width / 2))
        y = 0.42
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


def _nearest_slice_by_position(slices: list[StudySlice], target: StudySlice, tolerance_mm: float = 3.0) -> Optional[StudySlice]:
    target_pos = target.superior_inferior_position
    if target_pos is None:
        if target.instance_number is None:
            return None
        for sl in slices:
            if sl.instance_number == target.instance_number:
                return sl
        return None
    with_pos = [sl for sl in slices if sl.superior_inferior_position is not None]
    if not with_pos:
        return None
    nearest = min(with_pos, key=lambda sl: abs(float(sl.superior_inferior_position) - float(target_pos)))
    if abs(float(nearest.superior_inferior_position) - float(target_pos)) <= tolerance_mm:
        return nearest
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

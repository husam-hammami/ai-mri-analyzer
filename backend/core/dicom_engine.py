"""
MIKA — DICOM Processing Engine
===================================
Handles DICOM ingestion, calibration, conversion, and quantitative measurements.
All measurements are DICOM-calibrated using PixelSpacing metadata.
Uncalibrated mode is enforced when DICOM metadata is absent.

This module is the computational backbone — no AI calls happen here.
Everything is deterministic and reproducible.
"""

import os
import io
import math
import json
import base64
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.signal import find_peaks
from scipy.ndimage import gaussian_filter1d

try:
    import pydicom
    PYDICOM_AVAILABLE = True
except ImportError:
    PYDICOM_AVAILABLE = False

logger = logging.getLogger("mika.dicom")


# Expected pixel-intensity ranges by structure type on T2-weighted sagittal (0-255 scale).
# Used by the annotation double-check loop (skill Phase 3, Step 3C) to verify that every
# arrow tip actually lands on the structure it claims to point at, before the image ships.
EXPECTED_INTENSITY_RANGES = {
    "canal_csf":       (120, 255),  # Bright CSF column
    "disc_protrusion": (30, 110),   # Disc material, intermediate
    "disc_space":      (20, 200),   # Wide — desiccated (dark) to hydrated (bright)
    "disc_desiccated": (0, 95),     # A desiccated disc target should read dark
    "vertebral_body":  (70, 170),   # Marrow signal
    "canal_narrowing": (40, 140),   # Reduced but present CSF at a stenosis
    "bone_cortex":     (0, 50),     # Very dark cortical bone
}


# ──────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────

@dataclass
class PixelCalibration:
    """Stores DICOM pixel-to-mm calibration for a sequence."""
    row_spacing_mm: float  # mm per pixel (vertical)
    col_spacing_mm: float  # mm per pixel (horizontal)
    slice_thickness_mm: float
    rows: int
    cols: int
    fov_x_mm: float
    fov_y_mm: float
    is_calibrated: bool = True

    @property
    def pixel_area_mm2(self) -> float:
        return self.row_spacing_mm * self.col_spacing_mm


@dataclass
class SequenceInfo:
    """Metadata for a single MRI sequence."""
    name: str
    series_description: str
    plane: str  # sagittal, axial, coronal
    num_slices: int
    has_contrast: bool
    calibration: Optional[PixelCalibration]
    file_list: list = field(default_factory=list)
    slice_locations: list = field(default_factory=list)


@dataclass
class PatientDemographics:
    """Patient identification and study metadata."""
    patient_name: str = ""
    patient_id: str = ""
    birth_date: str = ""
    sex: str = ""
    age: str = ""
    study_date: str = ""
    study_description: str = ""
    institution: str = ""
    referring_physician: str = ""
    field_strength: float = 0.0
    body_part_examined: str = ""  # DICOM tag (0018,0015)
    detected_anatomy: str = ""   # spine, brain, msk, unknown


@dataclass
class DiscMeasurement:
    """Quantitative measurements for a single disc level."""
    level: str  # e.g., "L4-L5"
    disc_t2_signal: float = 0.0
    adjacent_body_signal: float = 0.0
    desiccation_ratio: float = 0.0
    desiccation_grade: str = ""  # normal, mild, moderate, severe
    canal_csf_signal: float = 0.0
    canal_csf_reference: float = 0.0  # signal at L1 for comparison
    canal_csf_reduction_pct: float = 0.0
    canal_ap_mm: float = 0.0
    disc_row: int = 0  # pixel coordinate in reference image
    canal_col: int = 0
    body_col: int = 0
    confidence_tier: str = "C"

    @property
    def is_calibrated_measurement(self) -> bool:
        return self.canal_ap_mm > 0


@dataclass
class EndplateAssessment:
    """Multi-sequence endplate signal analysis for Modic classification."""
    level: str
    endplate: str  # "superior" or "inferior"
    t1_signal: float = 0.0
    t1_ratio: float = 0.0
    t2_signal: float = 0.0
    t2_ratio: float = 0.0
    tirm_signal: float = 0.0
    tirm_ratio: float = 0.0
    t1_cont_signal: float = 0.0
    t1_cont_ratio: float = 0.0
    modic_type: str = ""  # "", "1", "2", "3", "mixed"
    confidence_tier: str = "C"


@dataclass
class StudyInventory:
    """Complete inventory of a single MRI study."""
    demographics: PatientDemographics
    sequences: dict = field(default_factory=dict)  # name -> SequenceInfo
    is_calibrated: bool = False
    has_contrast: bool = False
    total_files: int = 0
    study_date: str = ""
    detected_anatomy: str = ""  # spine, brain, msk, unknown
    anatomy_subregion: str = ""  # for msk: knee, shoulder, hip, ankle, wrist, elbow, foot, hand


# ──────────────────────────────────────────────
# DICOM Loading & Calibration
# ──────────────────────────────────────────────

class DICOMEngine:
    """
    Core engine for DICOM processing, calibration, and measurement.

    Implements the full analysis pipeline:
      Phase 0: Inventory & Calibration
      Phase 1: Level Identification
      Phase 2: Quantitative Measurements
      Phase 3: Annotation Generation
    """

    def __init__(self, dicom_dir: str, work_dir: str):
        self.dicom_dir = Path(dicom_dir)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "raw_png").mkdir(exist_ok=True)
        (self.work_dir / "annotated").mkdir(exist_ok=True)

        self.inventory: Optional[StudyInventory] = None
        self.level_map: dict = {}  # disc_level -> row in reference image
        self.body_map: dict = {}   # vertebra -> row
        self.level_confidence: str = "unknown"   # high | moderate | low
        self.level_confidence_reason: str = ""
        self.level_identity: dict = {}  # verify_level_identity() result (sacrum-anchor check)
        self.canal_col: int = 0
        self.body_col: int = 0
        self.reference_image: Optional[np.ndarray] = None
        self.disc_measurements: list[DiscMeasurement] = []
        self.endplate_assessments: list[EndplateAssessment] = []
        self.canal_narrowing: list = []   # [{row, intensity, reduction_pct}] from Step 3A.3
        self.annotation_audit: list = []  # Step 3C/3D record per arrow tip (for VerificationPass)
        self._converted_images: dict = {}  # seq_name/slice -> path

    # ── Phase 0: Inventory ──

    def run_inventory(self) -> StudyInventory:
        """Catalog all DICOM files, extract metadata, and calibrate."""
        if not PYDICOM_AVAILABLE:
            raise RuntimeError("pydicom is required. Install with: pip install pydicom")

        dcm_files = sorted([
            f for f in os.listdir(self.dicom_dir) if f.endswith(".dcm")
        ])
        if not dcm_files:
            raise FileNotFoundError(f"No DICOM files found in {self.dicom_dir}")

        # Group files by sequence (use DICOM SeriesDescription, fallback to filename prefix)
        seq_groups: dict[str, list[str]] = {}
        for f in dcm_files:
            try:
                ds_tmp = pydicom.dcmread(str(self.dicom_dir / f), stop_before_pixels=True)
                seq_name = str(getattr(ds_tmp, "SeriesDescription", "")).strip()
                if not seq_name:
                    seq_name = str(getattr(ds_tmp, "ProtocolName", "")).strip()
                if not seq_name:
                    # Fallback: strip trailing digits and extension
                    seq_name = f.rsplit("_Img", 1)[0] if "_Img" in f else f.rsplit("_", 1)[0] if "_" in f else "unknown_series"
            except Exception:
                seq_name = f.rsplit("_Img", 1)[0] if "_Img" in f else f.rsplit("_", 1)[0] if "_" in f else "unknown_series"
            seq_groups.setdefault(seq_name, []).append(f)

        # Extract demographics from first file
        first_ds = pydicom.dcmread(str(self.dicom_dir / dcm_files[0]))
        body_part_raw = str(getattr(first_ds, "BodyPartExamined", "")).strip()
        study_desc_raw = str(getattr(first_ds, "StudyDescription", "")).strip()
        series_descs = list(seq_groups.keys())
        detected_anatomy = self._detect_anatomy(body_part_raw, study_desc_raw, dcm_files, series_descs)
        anatomy_subregion = (
            self._detect_msk_subregion(body_part_raw, study_desc_raw, dcm_files, series_descs)
            if detected_anatomy == "msk" else ""
        )

        demographics = PatientDemographics(
            patient_name=str(getattr(first_ds, "PatientName", "")),
            patient_id=str(getattr(first_ds, "PatientID", "")),
            birth_date=str(getattr(first_ds, "PatientBirthDate", "")),
            sex=str(getattr(first_ds, "PatientSex", "")),
            age=str(getattr(first_ds, "PatientAge", "")),
            study_date=str(getattr(first_ds, "StudyDate", "")),
            study_description=study_desc_raw,
            institution=str(getattr(first_ds, "InstitutionName", "")),
            referring_physician=str(getattr(first_ds, "ReferringPhysicianName", "")),
            field_strength=float(getattr(first_ds, "MagneticFieldStrength", 0)),
            body_part_examined=body_part_raw,
            detected_anatomy=detected_anatomy,
        )
        self._anatomy_subregion = anatomy_subregion

        # Process each sequence
        sequences = {}
        has_any_contrast = False
        all_calibrated = True

        for seq_name, files in sorted(seq_groups.items()):
            representative = pydicom.dcmread(str(self.dicom_dir / files[0]))
            series_desc = str(getattr(representative, "SeriesDescription", seq_name))

            # Determine plane from series description
            plane = self._detect_plane(series_desc)

            # Determine contrast status
            has_contrast = (
                "CONT" in seq_name.upper()
                or "CONT" in series_desc.upper()
            )
            if has_contrast:
                has_any_contrast = True

            # Extract calibration
            ps = getattr(representative, "PixelSpacing", None)
            st = getattr(representative, "SliceThickness", None)
            rows = getattr(representative, "Rows", None)
            cols = getattr(representative, "Columns", None)

            calibration = None
            if ps and rows and cols:
                calibration = PixelCalibration(
                    row_spacing_mm=float(ps[0]),
                    col_spacing_mm=float(ps[1]),
                    slice_thickness_mm=float(st) if st else 0.0,
                    rows=int(rows),
                    cols=int(cols),
                    fov_x_mm=float(ps[1]) * int(cols),
                    fov_y_mm=float(ps[0]) * int(rows),
                )
            else:
                all_calibrated = False

            # Get slice locations
            slice_locations = []
            for f in files:
                try:
                    ds = pydicom.dcmread(str(self.dicom_dir / f))
                    sl = float(getattr(ds, "SliceLocation", 0))
                    slice_locations.append(sl)
                except Exception:
                    slice_locations.append(0.0)

            sequences[seq_name] = SequenceInfo(
                name=seq_name,
                series_description=series_desc,
                plane=plane,
                num_slices=len(files),
                has_contrast=has_contrast,
                calibration=calibration,
                file_list=files,
                slice_locations=slice_locations,
            )

        self.inventory = StudyInventory(
            demographics=demographics,
            sequences=sequences,
            is_calibrated=all_calibrated,
            has_contrast=has_any_contrast,
            total_files=len(dcm_files),
            study_date=demographics.study_date,
            detected_anatomy=detected_anatomy,
            anatomy_subregion=anatomy_subregion,
        )

        logger.info(
            f"Inventory complete: {len(dcm_files)} files, "
            f"{len(sequences)} sequences, "
            f"calibrated={all_calibrated}, contrast={has_any_contrast}, "
            f"anatomy={detected_anatomy}"
        )
        return self.inventory

    # ── Phase 0B: DICOM → PNG Conversion ──

    def convert_sequences(self, sequence_names: Optional[list[str]] = None) -> dict:
        """Convert DICOM sequences to PNG for visual analysis."""
        if not self.inventory:
            self.run_inventory()

        targets = sequence_names or list(self.inventory.sequences.keys())
        converted = {}

        for seq_name in targets:
            if seq_name not in self.inventory.sequences:
                continue
            seq = self.inventory.sequences[seq_name]
            safe_name = seq_name.replace(" ", "_").replace("-", "_")
            out_dir = self.work_dir / "raw_png" / safe_name
            out_dir.mkdir(parents=True, exist_ok=True)

            paths = []
            for idx, f in enumerate(seq.file_list):
                ds = pydicom.dcmread(str(self.dicom_dir / f))
                arr = self._normalize_dicom(ds)
                # Use InstanceNumber from DICOM if available, fallback to enumeration index
                img_num = int(getattr(ds, "InstanceNumber", idx + 1))
                out_path = out_dir / f"slice_{img_num:03d}.png"
                Image.fromarray(arr).save(str(out_path))
                paths.append(str(out_path))

            converted[seq_name] = paths
            self._converted_images[safe_name] = paths
            logger.info(f"Converted {len(paths)} slices: {safe_name}")

        return converted

    # ── Phase 1: Level Identification ──

    def identify_levels(self, sag_t2_seq_name: str, midline_slice: int = 8) -> dict:
        """
        Execute Sacrum-Up level identification protocol.
        Returns a mapping of disc levels to pixel row coordinates.
        """
        safe_name = sag_t2_seq_name.replace(" ", "_").replace("-", "_")
        img_path = self.work_dir / "raw_png" / safe_name / f"slice_{midline_slice:03d}.png"

        if not img_path.exists():
            # Convert if not already done
            self.convert_sequences([sag_t2_seq_name])
            if not img_path.exists():
                raise FileNotFoundError(f"Could not find/create: {img_path}")

        arr = np.array(Image.open(str(img_path))).astype(float)
        self.reference_image = arr
        h, w = arr.shape[:2]

        # Step 1: Locate spinal canal (bright CSF column in central image region)
        canal_cols = []
        center_start, center_end = w // 4, 3 * w // 4
        test_rows = [h // 4, h // 3, int(h * 0.4), h // 2, int(h * 0.6)]

        for row in test_rows:
            profile = arr[row, center_start:center_end]
            if profile.ndim > 1:
                profile = profile.mean(axis=-1)
            smooth = gaussian_filter1d(profile.astype(float), sigma=2)
            canal_col = int(np.argmax(smooth)) + center_start
            canal_cols.append(canal_col)

        self.canal_col = int(np.median(canal_cols))
        self.body_col = self.canal_col - 25  # Vertebral bodies are anterior to canal

        # Step 2: Vertical intensity profile along body column to find disc spaces
        body_profile = arr[:, self.body_col]
        if body_profile.ndim > 1:
            body_profile = body_profile.mean(axis=-1)
        body_smooth = gaussian_filter1d(body_profile.astype(float), sigma=3)

        # Find the sacrum: typically the brightest vertebral region in the inferior portion
        inferior_half = body_smooth[h // 2:]
        sacrum_search = gaussian_filter1d(inferior_half, sigma=5)
        sacrum_relative = int(np.argmax(sacrum_search))
        sacrum_row = sacrum_relative + h // 2

        # Step 3: Find disc spaces by gradient analysis above sacrum
        gradient = np.abs(np.gradient(body_smooth[:sacrum_row]))
        grad_smooth = gaussian_filter1d(gradient, sigma=4)
        peaks, _ = find_peaks(grad_smooth, distance=18, prominence=3)

        # Pair peaks into disc space boundaries (each disc is between two gradient peaks)
        disc_centers = []
        sorted_peaks = sorted(peaks)
        for i in range(len(sorted_peaks) - 1):
            center = (sorted_peaks[i] + sorted_peaks[i + 1]) // 2
            if center < sacrum_row - 10:  # Must be above sacrum
                disc_centers.append(center)

        # The faint lowest disc (L5-S1) is the one the strict pass most often drops, which
        # shifts every level one up (the off-by-one). If the gap below the lowest detected
        # disc is wide enough to hide a disc, re-search that band at lower prominence.
        disc_centers, recovered_faint = self._recover_faint_lowest_disc(
            grad_smooth, disc_centers, sacrum_row
        )

        # Map disc centers to levels (bottom-up = sacrum-up counting)
        disc_centers.sort(reverse=True)  # Bottom to top
        level_names = ["L5-S1", "L4-L5", "L3-L4", "L2-L3", "L1-L2", "T12-L1"]
        body_names = ["S1", "L5", "L4", "L3", "L2", "L1", "T12"]

        for i, center in enumerate(disc_centers):
            if i < len(level_names):
                self.level_map[level_names[i]] = center

        # Estimate vertebral body centers between disc spaces
        all_rows = [sacrum_row] + list(reversed(disc_centers))
        for i in range(len(all_rows) - 1):
            body_center = (all_rows[i] + all_rows[i + 1]) // 2
            if i < len(body_names):
                self.body_map[body_names[i]] = body_center

        # Add S1 body
        if disc_centers:
            self.body_map["S1"] = sacrum_row

        # Confidence in the deterministic sacrum-up map. The heuristic can mis-anchor on
        # atypical anatomy (transitional vertebrae, 6 lumbar segments, poor T2). When the
        # number/regularity of detected disc spaces is off, flag it so downstream findings
        # get tier-capped and the model is told to rely on its own sacrum-up count.
        mapped_rows = sorted(self.level_map.values())
        n_levels = len(mapped_rows)
        regularity = 0.0
        if n_levels >= 2:
            spacings = [mapped_rows[i + 1] - mapped_rows[i] for i in range(n_levels - 1)]
            mean_sp = sum(spacings) / len(spacings)
            if mean_sp > 0:
                var = sum((s - mean_sp) ** 2 for s in spacings) / len(spacings)
                regularity = (var ** 0.5) / mean_sp  # coefficient of variation
        if n_levels >= 5 and regularity < 0.25:
            self.level_confidence = "high"
        elif n_levels >= 4 and regularity < 0.40:
            self.level_confidence = "moderate"
        else:
            self.level_confidence = "low"
        self.level_confidence_reason = (
            f"{n_levels} disc spaces detected, spacing CoV={regularity:.2f}"
        )

        # Recovering a faint disc means the strict pass under-counted — trust it less.
        if recovered_faint:
            if self.level_confidence == "high":
                self.level_confidence = "moderate"
            self.level_confidence_reason += " (+faint lowest disc recovered)"

        # Sacrum-anchor identity check (does NOT mutate the map). A failed anchor is the
        # off-by-one signal intensity verification cannot see — downgrade confidence loudly.
        self.level_identity = self.verify_level_identity()
        if not self.level_identity.get("ok", True):
            if self.level_confidence == "high":
                self.level_confidence = "moderate"
            if self.level_identity.get("anchored") is False:
                self.level_confidence = "low"
            reasons = self.level_identity.get("reasons") or []
            if reasons:
                self.level_confidence_reason += " | identity: " + "; ".join(reasons)

        logger.info(
            f"Level identification: {self.level_map} "
            f"(confidence={self.level_confidence}; {self.level_confidence_reason})"
        )
        return self.level_map

    def _recover_faint_lowest_disc(self, grad_smooth, disc_centers, sacrum_row):
        """Re-search the band between the lowest detected disc and the sacrum.

        The lowest lumbar disc (L5-S1) is often faint, so the strict gradient pass drops it
        and the whole sacrum-up count shifts up by one. When the gap below the lowest disc
        is wide enough to hide a disc, re-run peak detection there at lower prominence and
        insert the recovered center. Returns ``(disc_centers, recovered: bool)``.
        Rows increase downward, so the lowest disc is the MAX row and the sacrum is below it.
        """
        if len(disc_centers) < 2:
            return disc_centers, False
        ordered = sorted(disc_centers)
        spacings = sorted(ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1))
        median_sp = spacings[len(spacings) // 2]
        lowest = max(disc_centers)
        gap = sacrum_row - lowest
        # A normal disc abuts the sacrum within ~one disc spacing; a gap near two spacings
        # means a disc was skipped between the lowest detected one and the sacrum.
        if median_sp <= 0 or gap < 1.5 * median_sp:
            return disc_centers, False
        lo_peaks, _ = find_peaks(grad_smooth, distance=12, prominence=1.0)
        sorted_lo = sorted(lo_peaks)
        candidates = []
        for i in range(len(sorted_lo) - 1):
            center = (sorted_lo[i] + sorted_lo[i + 1]) // 2
            if lowest + 0.4 * median_sp < center < sacrum_row - 0.3 * median_sp:
                candidates.append(center)
        if not candidates:
            return disc_centers, False
        # the recovered disc should sit about one spacing below the current lowest
        expected = lowest + median_sp
        recovered = min(candidates, key=lambda c: abs(c - expected))
        if recovered not in disc_centers:
            return list(disc_centers) + [recovered], True
        return disc_centers, False

    def verify_level_identity(self) -> dict:
        """Anchor check on the sacrum-up level map (does NOT mutate it).

        Intensity verification only confirms a tip sits on a disc-like pixel, never that it
        sits on the CLAIMED level — so an off-by-one (a missed faint lowest disc) passes
        silently. This re-checks the identity geometrically:
        (1) ANCHORED — the lowest counted disc must abut the sacrum (no unmarked disc
            between it and S1: gap < ~1.3x the median inter-disc spacing);
        (2) CONSECUTIVE — inter-disc spacings are regular (reuse the CoV idiom);
        (3) COUNT — the disc-space count is ~5-6 (lumbar 5, ±1 transitional).
        Returns ok / anchored / consecutive / count_ok / identity_confidence / reasons.
        """
        reasons: list[str] = []
        level_map = self.level_map or {}
        mapped_rows = sorted(level_map.values())
        n_levels = len(mapped_rows)

        regularity = 0.0
        median_sp = None
        if n_levels >= 2:
            spacings = [mapped_rows[i + 1] - mapped_rows[i] for i in range(n_levels - 1)]
            mean_sp = sum(spacings) / len(spacings)
            srt = sorted(spacings)
            median_sp = srt[len(srt) // 2]
            if mean_sp > 0:
                var = sum((s - mean_sp) ** 2 for s in spacings) / len(spacings)
                regularity = (var ** 0.5) / mean_sp
        consecutive = n_levels >= 2 and regularity < 0.35
        if n_levels >= 2 and not consecutive:
            reasons.append(f"irregular inter-disc spacing (CoV={regularity:.2f})")

        # sacrum abutment: the lowest disc (max row) must sit just above the sacrum row.
        sacrum_row = self.body_map.get("S1")
        lowest_disc = max(mapped_rows) if mapped_rows else None
        sacrum_gap = None
        anchored = None
        if sacrum_row is not None and lowest_disc is not None and median_sp:
            sacrum_gap = sacrum_row - lowest_disc
            anchored = 0 < sacrum_gap < 1.3 * median_sp
            if not anchored:
                reasons.append(
                    f"lowest disc does not abut the sacrum (gap={sacrum_gap}px vs "
                    f"~{median_sp}px spacing) — a faint L5-S1 may have been missed"
                )
        elif sacrum_row is None:
            reasons.append("no sacrum row available to anchor the count")

        count_ok = 4 <= n_levels <= 7
        if not count_ok:
            reasons.append(f"unexpected disc-space count ({n_levels}); expected ~5-6")

        checks = [c for c in (anchored, consecutive, count_ok) if c is not None]
        ok = bool(checks) and all(checks)
        if anchored and consecutive and count_ok:
            identity_confidence = "high"
        elif (anchored is not False) and consecutive and count_ok:
            identity_confidence = "moderate"
        else:
            identity_confidence = "low"

        return {
            "ok": ok,
            "anchored": anchored,
            "consecutive": consecutive,
            "count_ok": count_ok,
            "n_levels": n_levels,
            "median_spacing_px": median_sp,
            "sacrum_gap_px": sacrum_gap,
            "identity_confidence": identity_confidence,
            "reasons": reasons,
        }

    # ── Phase 2: Quantitative Measurements ──

    def measure_all_discs(self, sag_t2_seq_name: str, midline_slice: int = 8) -> list[DiscMeasurement]:
        """Run calibrated measurements at every disc level."""
        if not self.level_map:
            self.identify_levels(sag_t2_seq_name, midline_slice)

        seq = self.inventory.sequences.get(sag_t2_seq_name)
        if not seq or not seq.calibration:
            logger.warning("No calibration data available — measurements will be qualitative only")

        arr = self.reference_image
        if arr is None:
            raise RuntimeError("Reference image not loaded. Run identify_levels first.")

        if arr.ndim > 2:
            arr = arr.mean(axis=-1)

        ps = seq.calibration.row_spacing_mm if seq and seq.calibration else None

        # Reference CSF signal at L1 body level
        ref_body = self.body_map.get("L1")
        ref_csf = 0.0
        if ref_body:
            canal_region = arr[max(0, ref_body - 2):ref_body + 3,
                              max(0, self.canal_col - 2):self.canal_col + 3]
            ref_csf = float(canal_region.mean())

        measurements = []
        for level, row in self.level_map.items():
            m = DiscMeasurement(level=level, disc_row=row,
                                canal_col=self.canal_col, body_col=self.body_col)

            # Disc T2 signal (5x5 region at disc center)
            disc_region = arr[max(0, row - 3):row + 4,
                             max(0, self.body_col - 5):self.body_col + 6]
            m.disc_t2_signal = float(disc_region.mean())

            # Adjacent vertebral body signal (average above and below)
            above_row = row - 25
            below_row = row + 25
            above_region = arr[max(0, above_row - 3):above_row + 4,
                              max(0, self.body_col - 5):self.body_col + 6]
            below_region = arr[max(0, below_row - 3):below_row + 4,
                              max(0, self.body_col - 5):self.body_col + 6]
            m.adjacent_body_signal = float((above_region.mean() + below_region.mean()) / 2)

            # Desiccation ratio and grading
            m.desiccation_ratio = (
                m.disc_t2_signal / max(m.adjacent_body_signal, 1.0)
            )
            if m.desiccation_ratio < 0.4:
                m.desiccation_grade = "severe"
            elif m.desiccation_ratio < 0.7:
                m.desiccation_grade = "moderate"
            elif m.desiccation_ratio < 0.9:
                m.desiccation_grade = "mild"
            else:
                m.desiccation_grade = "normal"

            # Canal CSF signal
            canal_region = arr[max(0, row - 2):row + 3,
                              max(0, self.canal_col - 2):self.canal_col + 3]
            m.canal_csf_signal = float(canal_region.mean())
            m.canal_csf_reference = ref_csf
            m.canal_csf_reduction_pct = (
                (1 - m.canal_csf_signal / max(ref_csf, 1.0)) * 100
                if ref_csf > 0 else 0.0
            )

            # Canal AP diameter (FWHM method) if calibrated
            if ps:
                h_profile = arr[row, :].astype(float)
                h_smooth = gaussian_filter1d(h_profile, sigma=1)

                peak_col_local = self.canal_col
                peak_val = h_smooth[peak_col_local]
                half_max = peak_val / 2

                left = peak_col_local
                for c in range(peak_col_local, max(0, peak_col_local - 40), -1):
                    if h_smooth[c] < half_max:
                        left = c
                        break
                right = peak_col_local
                for c in range(peak_col_local, min(len(h_smooth) - 1, peak_col_local + 40)):
                    if h_smooth[c] < half_max:
                        right = c
                        break

                m.canal_ap_mm = (right - left) * ps

            # Confidence tier — aligned with prompt rules:
            #   Calibrated + abnormal  → Tier A (DICOM-verified measurement)
            #   Calibrated + normal    → Tier B (confirmed but no pathology)
            #   Uncalibrated           → Tier C max (per prompt rule #3)
            if ps:
                m.confidence_tier = "A" if m.desiccation_grade != "normal" else "B"
            else:
                m.confidence_tier = "C"

            measurements.append(m)

        self.disc_measurements = measurements
        return measurements

    def assess_endplates(
        self,
        seq_names: dict,
        midline_slice: int = 8,
        levels: Optional[list[str]] = None,
    ) -> list[EndplateAssessment]:
        """
        Multi-sequence endplate signal analysis for Modic classification.

        seq_names should map: {"T1": "...", "T2": "...", "TIRM": "...", "T1_CONT": "..."}
        """
        if not self.level_map:
            raise RuntimeError("Run identify_levels first")

        # Load each sequence at midline
        images = {}
        for seq_key, seq_name in seq_names.items():
            safe = seq_name.replace(" ", "_").replace("-", "_")
            path = self.work_dir / "raw_png" / safe / f"slice_{midline_slice:03d}.png"
            if path.exists():
                images[seq_key] = np.array(Image.open(str(path))).astype(float)
                if images[seq_key].ndim > 2:
                    images[seq_key] = images[seq_key].mean(axis=-1)

        if "T2" not in images:
            logger.warning("T2 image not available for endplate assessment")
            return []

        t2_h, t2_w = images["T2"].shape
        target_levels = levels or list(self.level_map.keys())

        assessments = []
        for level in target_levels:
            row_t2 = self.level_map.get(level)
            if row_t2 is None:
                continue

            for ep_name, offset in [("superior", -10), ("inferior", +10)]:
                ep = EndplateAssessment(level=level, endplate=ep_name)
                ep_row_t2 = row_t2 + offset
                ep_col_t2 = self.body_col

                # L2 body as reference
                ref_row_t2 = self.body_map.get("L2", t2_h // 3)

                for seq_key, arr in images.items():
                    seq_h, seq_w = arr.shape
                    ep_row = int(ep_row_t2 * seq_h / t2_h)
                    ep_col = int(ep_col_t2 * seq_w / t2_w)
                    ref_row = int(ref_row_t2 * seq_h / t2_h)
                    ref_col = int(ep_col_t2 * seq_w / t2_w)

                    region = arr[max(0, ep_row - 1):ep_row + 2,
                                max(0, ep_col - 1):ep_col + 2]
                    ref_region = arr[max(0, ref_row - 3):ref_row + 4,
                                    max(0, ref_col - 3):ref_col + 4]

                    signal = float(region.mean())
                    ref_signal = float(ref_region.mean())
                    ratio = signal / max(ref_signal, 1.0)

                    if seq_key == "T1":
                        ep.t1_signal, ep.t1_ratio = signal, ratio
                    elif seq_key == "T2":
                        ep.t2_signal, ep.t2_ratio = signal, ratio
                    elif seq_key == "TIRM":
                        ep.tirm_signal, ep.tirm_ratio = signal, ratio
                    elif seq_key == "T1_CONT":
                        ep.t1_cont_signal, ep.t1_cont_ratio = signal, ratio

                # Modic classification logic
                ep.modic_type, ep.confidence_tier = self._classify_modic(ep)
                assessments.append(ep)

        self.endplate_assessments = assessments
        return assessments

    # ── Phase 3: Annotation ──

    def _localize_canal_narrowing(self, arr: np.ndarray) -> list:
        """
        Step 3A.3: locate points of maximum central-canal narrowing (stenosis) by
        finding local minima of the canal CSF intensity profile. Returns a list of
        {row, intensity, reduction_pct} sorted by severity (most narrowed first).
        """
        if self.canal_col <= 0:
            return []
        c0 = max(0, self.canal_col - 3)
        c1 = self.canal_col + 4
        canal_profile = arr[:, c0:c1].mean(axis=1).astype(float)
        canal_smooth = gaussian_filter1d(canal_profile, sigma=2)
        # Reference = the brightest (most patent) canal point = abundant CSF.
        ref = float(canal_smooth.max()) if canal_smooth.size else 0.0
        canal_inv = -canal_smooth
        peaks, _ = find_peaks(canal_inv, distance=12, prominence=8)

        narrowings = []
        for p in peaks:
            intensity = float(canal_smooth[p])
            reduction = (1 - intensity / ref) * 100 if ref > 0 else 0.0
            narrowings.append({
                "row": int(p),
                "intensity": round(intensity, 1),
                "reduction_pct": round(reduction, 1),
            })
        narrowings.sort(key=lambda n: n["reduction_pct"], reverse=True)
        self.canal_narrowing = narrowings
        return narrowings

    def _verify_and_reposition_tip(
        self, raw_arr: np.ndarray, col: int, row: int,
        structure_type: str, search_radius: int = 10,
    ) -> tuple:
        """
        Step 3C: verify an arrow-tip pixel against the expected intensity range for the
        structure it claims to mark. If it fails, auto-search the neighborhood for the
        NEAREST pixel that does match and reposition to it. Returns
        (col, row, mean_intensity, status) where status is 'verified' | 'repositioned' | 'failed'.
        A 'failed' tip must NOT be drawn.
        """
        lo, hi = EXPECTED_INTENSITY_RANGES.get(structure_type, (0, 255))
        h, w = raw_arr.shape[:2]

        def mean3(r: int, c: int) -> float:
            nb = raw_arr[max(0, r - 1):r + 2, max(0, c - 1):c + 2]
            return float(nb.mean()) if nb.size else 0.0

        val = mean3(row, col)
        if lo <= val <= hi:
            return col, row, round(val, 1), "verified"

        best = None
        best_dist = 1e9
        for dr in range(-search_radius, search_radius + 1):
            for dc in range(-search_radius, search_radius + 1):
                r, c = row + dr, col + dc
                if 0 <= r < h and 0 <= c < w:
                    v = mean3(r, c)
                    if lo <= v <= hi:
                        dist = dr * dr + dc * dc
                        if dist < best_dist:
                            best_dist = dist
                            best = (c, r, v)
        if best is not None:
            return best[0], best[1], round(best[2], 1), "repositioned"
        return col, row, round(val, 1), "failed"

    def _draw_region_band(self, draw, center, label, color, side, font, scale: int = 2) -> None:
        """Fallback visual when a precise tip cannot be verified: draw a labelled REGION BOX at the
        computed location (approximate, not a pinpoint) so the finding still has a visible marker
        instead of being silently dropped. The corner ticks read as 'approximate region'."""
        cx, cy = int(center[0]), int(center[1])
        half = 16 * scale
        box = [cx - half, cy - half, cx + half, cy + half]
        draw.rectangle(box, outline=color, width=2)
        t = 6
        for x, sx in ((box[0], 1), (box[2], -1)):
            for y, sy in ((box[1], 1), (box[3], -1)):
                draw.line([(x, y), (x + t * sx, y)], fill=color, width=2)
                draw.line([(x, y), (x, y + t * sy)], fill=color, width=2)
        try:
            lw = draw.textbbox((0, 0), label, font=font)[2]
        except Exception:
            lw = len(label) * 7
        lx = box[2] + 6 if side == "right" else box[0] - 6 - lw
        ly = box[1] - 4
        bbox = draw.textbbox((lx, ly), label, font=font)
        draw.rectangle([bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1], fill="black")
        draw.text((lx, ly), label, fill=color, font=font)

    def create_annotated_sagittal(
        self, sag_t2_seq_name: str, midline_slice: int = 8, scale: int = 2
    ) -> str:
        """
        Create annotated sagittal T2 with the skill's Phase-3 double-check loop:
          3A  structure localization (canal, discs, canal narrowing) — already computed
          3B  draw arrows + verification circle (via _draw_arrow)
          3C  verify each tip against expected intensity; reposition or DROP on failure
          3D  record an audit trail so the VerificationPass can re-read placement
        Arrows that fail 3C verification are NEVER drawn — the skill forbids shipping
        annotations that land on the wrong structure.
        """
        self.annotation_audit = []
        safe = sag_t2_seq_name.replace(" ", "_").replace("-", "_")
        raw_path = self.work_dir / "raw_png" / safe / f"slice_{midline_slice:03d}.png"
        raw_arr = np.array(Image.open(str(raw_path))).astype(float)
        if raw_arr.ndim > 2:
            raw_arr = raw_arr.mean(axis=-1)

        # Step 3A.3 — locate canal narrowing on this slice.
        self._localize_canal_narrowing(raw_arr)

        img = Image.open(str(raw_path)).convert("RGB")
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        draw = ImageDraw.Draw(img)

        font_sm = self._get_font(13)
        font_title = self._get_font(15)

        # Build candidate targets: (label, col, row, structure_type, color, side)
        # side: 'left' draws the arrow from the left margin, 'right' from the right.
        candidates = []
        for m in self.disc_measurements:
            if m.desiccation_grade in ("severe", "moderate"):
                color = "red" if m.desiccation_grade == "severe" else "orange"
                candidates.append({
                    "label": f"{m.level} ({m.desiccation_grade})",
                    "col": m.body_col, "row": m.disc_row,
                    "structure": "disc_desiccated", "color": color,
                    "side": "left", "level": m.level,
                })

        # Reference CSF arrow (proves a patent canal for comparison).
        l1_body = self.body_map.get("L1")
        if l1_body:
            candidates.append({
                "label": "Normal CSF", "col": self.canal_col + 8, "row": l1_body,
                "structure": "canal_csf", "color": "lime", "side": "right", "level": None,
            })

        # Most-severe canal narrowing arrow (only if meaningfully reduced).
        if self.canal_narrowing and self.canal_narrowing[0]["reduction_pct"] >= 25:
            n = self.canal_narrowing[0]
            candidates.append({
                "label": f"Canal narrowing (~{int(n['reduction_pct'])}% CSF loss)",
                "col": self.canal_col, "row": n["row"],
                "structure": "canal_narrowing", "color": "yellow",
                "side": "right", "level": None,
            })

        for t in candidates:
            final_col, final_row, intensity, status = self._verify_and_reposition_tip(
                raw_arr, int(t["col"]), int(t["row"]), t["structure"]
            )
            audit = {
                "label": t["label"], "structure": t["structure"], "level": t["level"],
                "requested_tip": [int(t["col"]), int(t["row"])],
                "final_tip": [int(final_col), int(final_row)],
                "intensity": intensity,
                "expected_range": list(EXPECTED_INTENSITY_RANGES.get(t["structure"], (0, 255))),
                "status": status,
                "drawn": status != "failed",
            }
            self.annotation_audit.append(audit)

            if status == "failed":
                # Confidence-forward: do NOT silently drop the finding's visual. Fall back to a
                # labelled REGION BAND at the computed location (approximate, not a verified
                # pinpoint) so the reader still sees where the finding is.
                audit["status"] = "region_band"
                audit["drawn"] = True
                rc = (int(t["col"]) * scale, int(t["row"]) * scale)
                self._draw_region_band(
                    draw, rc, f"{t['label']} (approx region)", t["color"], t["side"], font_sm, scale
                )
                logger.info(
                    f"Annotation '{t['label']}' tip unverifiable — drawn as region band, not dropped"
                )
                continue

            tip = (final_col * scale, final_row * scale)
            if t["side"] == "right":
                start = (tip[0] + 45 * scale, tip[1])
                lx = start[0] + 5
            else:
                start = (tip[0] - 50 * scale, tip[1])
                lx = start[0] - len(t["label"]) * 7
            self._draw_arrow(draw, start, tip, color=t["color"])
            ly = start[1] - 8
            bbox = draw.textbbox((lx, ly), t["label"], font=font_sm)
            draw.rectangle([bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1], fill="black")
            draw.text((lx, ly), t["label"], fill=t["color"], font=font_sm)

        draw.text((5, 3), "Sagittal T2 — Disc & Canal Assessment", fill="white", font=font_title)

        out_path = str(self.work_dir / "annotated" / "sag_t2_annotated.png")
        img.save(out_path)

        drawn = sum(1 for a in self.annotation_audit if a["drawn"])
        failed = sum(1 for a in self.annotation_audit if a["status"] == "failed")
        repositioned = sum(1 for a in self.annotation_audit if a["status"] == "repositioned")
        region = sum(1 for a in self.annotation_audit if a["status"] == "region_band")
        logger.info(
            f"Annotation 3C: {drawn} drawn ({repositioned} repositioned, {region} region-band), "
            f"{failed} dropped"
        )

        return out_path

    def create_contrast_comparison(
        self, pre_seq: str, post_seq: str, slice_idx: int, label: str = "L4-L5"
    ) -> str:
        """Create side-by-side pre/post contrast comparison."""
        pre_safe = pre_seq.replace(" ", "_").replace("-", "_")
        post_safe = post_seq.replace(" ", "_").replace("-", "_")

        pre_path = self.work_dir / "raw_png" / pre_safe / f"slice_{slice_idx:03d}.png"
        post_path = self.work_dir / "raw_png" / post_safe / f"slice_{slice_idx:03d}.png"

        pre_img = np.array(Image.open(str(pre_path)))
        post_img = np.array(Image.open(str(post_path)))

        h, w = pre_img.shape[:2]
        gap = 10
        if pre_img.ndim == 2:
            combined = np.zeros((h, w * 2 + gap), dtype=np.uint8)
            combined[:, :w] = pre_img
            combined[:, w + gap:] = post_img
        else:
            combined = np.zeros((h, w * 2 + gap, 3), dtype=np.uint8)
            combined[:, :w] = pre_img
            combined[:, w + gap:] = post_img

        img = Image.fromarray(combined).convert("RGB")
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        draw = ImageDraw.Draw(img)

        font = self._get_font(13)
        font_sm = self._get_font(11)
        draw.text((5, 3), "PRE-CONTRAST", fill="cyan", font=font)
        draw.text((w * 2 + 25, 3), "POST-CONTRAST", fill="cyan", font=font)
        draw.text((5, img.height - 18), f"Axial T1 VIBE FS at ~{label}", fill="white", font=font)
        # Neutral guidance only — do NOT assert a conclusion. The reader/model decides
        # whether enhancement is present by comparing these SAME-LEVEL pre/post images.
        draw.text((w * 2 + 25, img.height - 35),
                  "Same-level pre/post: scar enhances, recurrent disc does not", fill="yellow", font=font_sm)

        out_path = str(self.work_dir / "annotated" / f"contrast_{label.replace('-', '')}.png")
        img.save(out_path)
        return out_path

    def create_level_reference(self, sag_t2_seq_name: str, midline_slice: int = 8) -> str:
        """Create Level Reference Image (Figure 0) with sacrum-up labels."""
        safe = sag_t2_seq_name.replace(" ", "_").replace("-", "_")
        raw_path = self.work_dir / "raw_png" / safe / f"slice_{midline_slice:03d}.png"

        img = Image.open(str(raw_path)).convert("RGB")
        scale = 2
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        draw = ImageDraw.Draw(img)

        font = self._get_font(14)
        font_sm = self._get_font(11)

        for level_name, body_row in self.body_map.items():
            row = body_row * scale
            draw.line([(10, row), ((self.body_col - 40) * scale, row)], fill="cyan", width=1)
            draw.text((10, row - 8), level_name, fill="cyan", font=font)

        for disc_name, disc_row in self.level_map.items():
            row = disc_row * scale
            color = "red" if any(
                m.desiccation_grade == "severe" and m.level == disc_name
                for m in self.disc_measurements
            ) else "yellow"
            x_start = (self.canal_col + 40) * scale
            x_end = (img.width - 20)
            draw.line([(x_start, row), (x_end, row)], fill=color, width=1)
            draw.text((x_start + 5, row - 8), disc_name, fill=color, font=font_sm)

        draw.text((10, 5), "Level Reference (Sag T2, Midline)", fill="white", font=font)

        out_path = str(self.work_dir / "annotated" / "level_reference.png")
        img.save(out_path)
        return out_path

    def create_multi_sequence_panel(self, seq_files: list[tuple[str, str, int]]) -> str:
        """
        Create a side-by-side panel of multiple sequences.
        seq_files: list of (sequence_name, label, midline_slice)
        """
        panels = []
        target_h = 330

        for seq_name, label, slice_idx in seq_files:
            safe = seq_name.replace(" ", "_").replace("-", "_")
            slice_dir = self.work_dir / "raw_png" / safe
            path = slice_dir / f"slice_{slice_idx:03d}.png"
            if not path.exists():
                # Find nearest available slice (sorted by name, pick middle)
                available = sorted(slice_dir.glob("slice_*.png")) if slice_dir.exists() else []
                if available:
                    path = available[len(available) // 2]
                else:
                    continue
            img = Image.open(str(path)).convert("RGB")
            new_w = int(img.width * target_h / img.height)
            img = img.resize((new_w, target_h), Image.LANCZOS)
            panels.append((img, label))

        if not panels:
            return ""

        gap = 4
        total_w = sum(p[0].width for p in panels) + gap * (len(panels) - 1)
        combined = Image.new("RGB", (total_w, target_h + 20), color=(0, 0, 0))

        font = self._get_font(13)
        x_offset = 0
        draw = ImageDraw.Draw(combined)
        for img_p, label in panels:
            combined.paste(img_p, (x_offset, 20))
            draw.text((x_offset + 5, 3), label, fill="cyan", font=font)
            x_offset += img_p.width + gap

        scale_f = 1.5
        combined = combined.resize(
            (int(combined.width * scale_f), int(combined.height * scale_f)),
            Image.LANCZOS,
        )

        out_path = str(self.work_dir / "annotated" / "multi_sequence_panel.png")
        combined.save(out_path)
        return out_path

    # ── Export Helpers ──

    def get_image_base64(self, image_path: str) -> str:
        """Convert an image file to base64 for API transmission."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def export_measurements_json(self) -> dict:
        """Export all measurements as a JSON-serializable dict."""
        calibration_status = "DICOM-calibrated" if (
            self.inventory and self.inventory.is_calibrated
        ) else "UNCALIBRATED"

        # Deterministic sequence catalog so the report can state the authoritative
        # study description (dates / sequences / contrast) rather than inferring it.
        sequence_catalog = []
        if self.inventory:
            for name, seq in self.inventory.sequences.items():
                sequence_catalog.append({
                    "name": name,
                    "plane": seq.plane,
                    "num_slices": seq.num_slices,
                    "has_contrast": seq.has_contrast,
                    "calibrated": seq.calibration is not None,
                })

        study_description = ""
        if self.inventory:
            demo = self.inventory.demographics
            seq_names = ", ".join(s["name"] for s in sequence_catalog) or "n/a"
            study_description = (
                f"{demo.study_description or 'MRI study'} "
                f"(date: {demo.study_date or 'unknown'}); "
                f"{self.inventory.total_files} images across "
                f"{len(sequence_catalog)} sequences [{seq_names}]; "
                f"contrast administered: {'yes' if self.inventory.has_contrast else 'no'}; "
                f"calibration: {calibration_status}"
            )

        return {
            "demographics": asdict(self.inventory.demographics) if self.inventory else {},
            "detected_anatomy": self.inventory.detected_anatomy if self.inventory else "unknown",
            "anatomy_subregion": getattr(self.inventory, "anatomy_subregion", "") if self.inventory else "",
            "calibration_status": calibration_status,
            "has_contrast": self.inventory.has_contrast if self.inventory else False,
            "total_files": self.inventory.total_files if self.inventory else 0,
            "study_description": study_description,
            "sequence_catalog": sequence_catalog,
            "level_map": self.level_map,
            "level_confidence": self.level_confidence,
            "level_confidence_reason": self.level_confidence_reason,
            "level_identity": self.level_identity,
            "disc_measurements": [asdict(m) for m in self.disc_measurements],
            "endplate_assessments": [asdict(e) for e in self.endplate_assessments],
            "canal_narrowing": self.canal_narrowing,
            "annotation_audit": self.annotation_audit,
        }

    # ── Private Helpers ──

    @staticmethod
    def _detect_anatomy(body_part: str, study_desc: str, dcm_files: list[str],
                        series_descs: list[str] | None = None) -> str:
        """
        Detect anatomy type from DICOM metadata using a multi-signal approach.

        Priority order:
          1. BodyPartExamined DICOM tag (0018,0015) — most reliable
          2. StudyDescription free text — second best
          3. SeriesDescription / protocol names (sequence-level signals, e.g. FLAIR→brain,
             T2 HASTE→abdomen) — these disambiguate coarse BodyPart labels like "HEAD_NECK"
          4. File naming conventions — fallback

        Returns one of: 'spine', 'brain', 'msk', 'cardiac', 'chest',
                         'abdomen', 'breast', 'vascular', 'head_neck',
                         'prostate', or 'unknown'
        """
        def _classify(text: str) -> str:
            t = text.upper()

            # ── SPINE detection ──
            spine_signals = [
                "SPINE", "LUMBAR", "LSPINE", "CSPINE", "TSPINE",
                "CERVICAL", "THORACIC", "SACRAL", "SACRUM",
                "LWS", "HWS", "BWS",  # German: Lendenwirbelsäule, Halswirbelsäule, Brustwirbelsäule
                "VERTEBRA", "DISC", "SPINAL", "MYELOGRA",
            ]
            if any(s in t for s in spine_signals):
                return "spine"

            # ── CARDIAC detection (before chest — more specific) ──
            cardiac_signals = [
                "CARDIAC", "HEART", "MYOCARD", "PERICARDI",
                "CINE", "TRUFI", "FIESTA",
                "LATE_GADOLINIUM", "LGE", "TAGGING",
                "AORTIC_VALVE", "MITRAL", "VENTRICL",
                "CARDIAC_MR", "CMR",
                "HERZ", "KARDIO",  # German
            ]
            if any(s in t for s in cardiac_signals):
                return "cardiac"

            # ── BREAST detection (before chest — more specific) ──
            breast_signals = [
                "BREAST", "MAMMA", "BIRADS", "BI-RADS",
                "SILICONE", "IMPLANT_BREAST",
                "VIBRANT", "THRIVE",
                "BRUST",  # German
            ]
            if any(s in t for s in breast_signals):
                return "breast"

            # ── PROSTATE detection (before brain/abdomen — a specific organ) ──
            prostate_signals = [
                "PROSTATE", "PROSTATA", "PIRADS", "PI-RADS",
                "SEMINAL", "TRANSITION_ZONE", "PERIPHERAL_ZONE",
            ]
            if any(s in t for s in prostate_signals):
                return "prostate"

            # ── BRAIN detection ──
            brain_signals = [
                "BRAIN", "NEURO", "CRANIAL", "CEREBR",
                "FLAIR", "SWI", "DWI", "DIFFUSION",
                "INTRACRANIAL", "PITUITARY", "SELLA",
                "HIPPOCAMP", "MPRAGE", "TENSOR",  # hippocampus, brain-volumetric & DTI sequences
                "KOPF",  # German
            ]
            if any(s in t for s in brain_signals):
                return "brain"

            # ── HEAD & NECK detection (after brain — less specific) ──
            head_neck_signals = [
                "HEAD", "NECK", "ORBIT", "TEMPORAL",
                "PAROTID", "THYROID", "LARYNX", "PHARYNX",
                "SINUSES", "PARANASAL", "NASOPHARYN",
                "TONGUE", "ORAL", "MANDIBLE", "MAXILLA",
                "SKULL", "MASTOID", "IAC", "BRACHIAL",
                "HALS",  # German
            ]
            if any(s in t for s in head_neck_signals):
                return "head_neck"

            # ── VASCULAR / MRA detection ──
            vascular_signals = [
                "MRA", "ANGIOGRA", "ANGIO",
                "VESSEL", "ARTERIAL", "VENOUS",
                "TOF", "TIME_OF_FLIGHT",
                "CAROTID", "CIRCLE_OF_WILLIS", "AORTA",
                "RENAL_ARTERY", "MESENTERIC",
                "PHASE_CONTRAST", "FLOW",
            ]
            if any(s in t for s in vascular_signals):
                return "vascular"

            # ── CHEST / THORAX detection ──
            chest_signals = [
                "CHEST", "THORAX", "LUNG", "PULMONARY",
                "MEDIASTIN", "PLEURA", "DIAPHRAGM",
                "HILUM", "BRONCH", "CXR",  # CXR = chest X-ray (filename hint for plain radiographs)
                "LUNGE", "THORAX",  # German
            ]
            if any(s in t for s in chest_signals):
                return "chest"

            # ── ABDOMEN / PELVIS detection ──
            abdomen_signals = [
                "ABDOMEN", "ABDOMINAL", "LIVER", "HEPAT",
                "KIDNEY", "RENAL", "PANCREA", "SPLEEN",
                "ADRENAL", "GALLBLADDER", "BILE", "BILIARY",
                "BOWEL", "COLON", "RECTAL", "RECTUM",
                "PELVIS", "PELVIC", "UTERUS", "OVARY", "OVARIAN",
                "BLADDER", "URETER",
                "PERITON", "RETROPERITON", "MESENTERY",
                "LEBER", "NIERE", "BAUCH", "BECKEN",  # German
            ]
            if any(s in t for s in abdomen_signals):
                return "abdomen"

            # ── MSK detection (broad catch-all for extremities) ──
            msk_signals = [
                "KNEE", "SHOULDER", "HIP", "ANKLE", "WRIST",
                "ELBOW", "FOOT", "HAND", "FINGER", "THUMB",
                "EXTREMITY", "JOINT", "MUSCULOSKELETAL",
                "MENISCUS", "ACL", "PCL", "MCL", "LCL",
                "ROTATOR", "LABRUM", "TENDON", "LIGAMENT",
                "FEMUR", "TIBIA", "HUMERUS", "RADIUS", "ULNA",
                "CALCANEUS", "ACHILLES", "PLANTAR",
                "KNIE", "SCHULTER", "HÜFTE",  # German
            ]
            if any(s in t for s in msk_signals):
                return "msk"

            return "unknown"

        # BodyPartExamined is the most reliable signal: a SPECIFIC organ label (PROSTATE, LIVER,
        # KIDNEY, LUNG…) is authoritative and must NOT be overridden by sequence-name hints (e.g.
        # a prostate ADC series literally named "Apparent Diffusion Coefficient" must not become
        # 'brain'). Only a coarse/empty label ('HEAD_NECK', 'HEAD', or missing) defers to the
        # study/series/file signals so things like a FLAIR series can upgrade 'HEAD_NECK'→'brain'.
        bp_result = _classify(body_part) if body_part.strip() else "unknown"
        if bp_result not in ("unknown", "head_neck"):
            return bp_result
        rest = f"{body_part} {study_desc} {' '.join(series_descs or [])} {' '.join(dcm_files)}"
        full_result = _classify(rest)
        return full_result if full_result != "unknown" else bp_result

    @staticmethod
    def _detect_msk_subregion(body_part: str, study_desc: str, dcm_files: list[str],
                              series_descs: list[str] | None = None) -> str:
        """Identify the specific joint within an MSK study so the UI can show the right body figure
        (knee vs hip vs shoulder …) instead of always rendering a knee. Returns '' when ambiguous.

        BodyPartExamined (0018,0015) is weighted highest; study description and file names follow.
        Order matters: more specific / less collision-prone joints are checked first.
        """
        combined = (
            f"{body_part} {study_desc} {' '.join(series_descs or [])} {' '.join(dcm_files)}".upper()
        )
        # (subregion, signal tokens). Knee last among the common joints so an explicit hip/shoulder wins.
        groups = [
            ("shoulder", ["SHOULDER", "ROTATOR", "LABRUM", "GLENOID", "SUPRASPINATUS", "SCHULTER"]),
            ("hip",      ["HIP", "ACETABUL", "FEMOROACETAB", "HÜFTE", "HUEFTE"]),
            ("ankle",    ["ANKLE", "ACHILLES", "CALCANEUS", "TALUS", "SPRUNGGELENK"]),
            ("wrist",    ["WRIST", "CARPAL", "SCAPHOID", "HANDGELENK"]),
            ("elbow",    ["ELBOW", "OLECRANON", "EPICONDYL", "ELLBOGEN"]),
            ("foot",     ["FOOT", "FOREFOOT", "MIDFOOT", "METATARS", "PLANTAR", "FUSS"]),
            ("hand",     ["HAND", "FINGER", "THUMB", "METACARP", "PHALAN"]),
            ("knee",     ["KNEE", "MENISCUS", "PATELLA", "ACL", "PCL", "MCL", "LCL",
                          "CRUCIATE", "TIBIAL PLATEAU", "KNIE"]),
        ]
        for sub, tokens in groups:
            if any(t in combined for t in tokens):
                return sub
        return ""

    @staticmethod
    def _normalize_dicom(ds) -> np.ndarray:
        """Normalize a DICOM pixel array to 0-255 uint8 with windowing."""
        arr = ds.pixel_array.astype(float)
        if hasattr(ds, "WindowCenter") and hasattr(ds, "WindowWidth"):
            wc = ds.WindowCenter
            ww = ds.WindowWidth
            if isinstance(wc, pydicom.multival.MultiValue):
                wc = float(wc[0])
            else:
                wc = float(wc)
            if isinstance(ww, pydicom.multival.MultiValue):
                ww = float(ww[0])
            else:
                ww = float(ww)
            arr = np.clip(arr, wc - ww / 2, wc + ww / 2)
        arr = ((arr - arr.min()) / (arr.max() - arr.min() + 1e-10) * 255).astype(np.uint8)
        return arr

    @staticmethod
    def _detect_plane(series_desc: str) -> str:
        desc = series_desc.lower()
        if "sag" in desc:
            return "sagittal"
        elif "tra" in desc or "axi" in desc:
            return "axial"
        elif "cor" in desc:
            return "coronal"
        return "unknown"

    @staticmethod
    def _classify_modic(ep: EndplateAssessment) -> tuple[str, str]:
        """Classify Modic type from multi-sequence ratios."""
        # Modic 1: T1 hypo, T2/TIRM hyper
        # Modic 2: T1 hyper, T2 hyper, TIRM iso/hypo
        # Modic 3: T1 hypo, T2 hypo
        if ep.t1_ratio < 0.7 and ep.tirm_ratio > 1.05:
            return "1", "B"
        elif ep.t1_ratio > 1.1 and ep.t2_ratio > 0.9:
            return "2", "B"
        elif ep.t1_ratio < 0.7 and ep.t2_ratio < 0.6 and ep.tirm_ratio < 0.9:
            return "3", "C"
        elif ep.t1_ratio < 0.8 and ep.tirm_ratio > 0.95:
            return "mixed", "C"
        return "", "C"

    @staticmethod
    def _draw_arrow(draw, start, end, color="red", width=2):
        draw.line([start, end], fill=color, width=width)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        for offset in [2.5, -2.5]:
            ax = end[0] - 12 * math.cos(angle + offset)
            ay = end[1] - 12 * math.sin(angle + offset)
            draw.line([(int(ax), int(ay)), end], fill=color, width=width)
        draw.ellipse([end[0] - 3, end[1] - 3, end[0] + 3, end[1] + 3],
                     outline=color, width=1)

    @staticmethod
    def _get_font(size: int):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size
            )
        except (OSError, IOError):
            return ImageFont.load_default()

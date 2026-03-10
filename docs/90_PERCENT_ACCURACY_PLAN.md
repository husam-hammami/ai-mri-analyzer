# MIKA 90% Accuracy Pipeline — Implementation Plan

## Executive Summary

Current MIKA accuracy: ~35-40% (estimated from blind testing).
Target: 90%+ validated accuracy with world-class radiology reports.

**Root cause of current low accuracy:** The pipeline sends 4 compressed images
(middle slices only) to Claude, discarding 95%+ of diagnostic data. Claude is
intelligent enough — the engineering is the bottleneck.

**Strategy:** Maximize what Claude sees, enhance subtle findings with classical
image processing, let Claude reason like a radiologist, engineer precise output,
validate ruthlessly against ground truth.

**Total models needed:** ONE — Claude Opus 4.6.

---

## Architecture: Before vs. After

### CURRENT (v3.0) — The Bottleneck Pipeline
```
200 DICOM slices uploaded
    ↓
DICOMEngine selects midline slice per sequence
    ↓
Creates 3-4 annotated PNG images
    ↓
Sends 4 images + measurements JSON to Claude
    ↓
Claude interprets from minimal data → ~35-40% accuracy
```

### NEW (v4.0) — The Full-Study Pipeline
```
All DICOM slices uploaded
    ↓
StudyOrganizer: sort by sequence, plane, position, label everything
    ↓
VisionEnhancer: multi-window, cross-sequence panels, difference maps
    ↓
BatchSender: ALL images to Claude, organized by sequence, high quality
    ↓
MasterPrompt: systematic search + diagnostic criteria + grading tables
    ↓
Claude analyzes FULL study → structured findings with slice references
    ↓
VerificationPass: senior attending review catches errors
    ↓
AnnotationEngine: pixel-accurate overlays from DICOM calibration
    ↓
ReportGenerator: ACR-standard structured report
    ↓
~85-92% validated accuracy
```

---

## Module Overview

| Module | File | Purpose | Dependencies |
|--------|------|---------|--------------|
| StudyOrganizer | `backend/core/study_organizer.py` | Sort, label, organize all DICOM data | pydicom |
| VisionEnhancer | `backend/core/vision_enhancer.py` | Multi-window, diff maps, symmetry | numpy, scipy, PIL |
| BatchSender | `backend/services/batch_sender.py` | Send all images to Claude efficiently | anthropic |
| MasterPrompts | `backend/prompts/` | Expert radiology prompts per anatomy | — |
| VerificationPass | `backend/services/verification.py` | Self-review second pass | anthropic |
| AnnotationEngine | `backend/core/annotation_engine.py` | Pixel-accurate overlays | PIL, numpy |
| ReportGenerator | `backend/core/report_generator.py` | ACR-standard structured reports | reportlab |
| ValidationFramework | `backend/validation/` | Ground truth comparison & metrics | numpy, scipy |

---

## PHASE 1: PIPELINE REBUILD (Week 1-2)
### Target: 70-75% accuracy

---

### Module 1: StudyOrganizer

**Problem:** Current DICOMEngine selects one midline slice per sequence.
Claude sees 4 images from a study with 200+ slices.

**Solution:** Organize ALL slices with complete metadata labels so Claude
receives a radiologist-quality organized study.

#### File: `backend/core/study_organizer.py`

```python
"""
StudyOrganizer — Organize DICOM study for maximum Claude comprehension.

Instead of selecting a few slices, organize ALL slices by:
  1. Sequence type (T1, T2, FLAIR, DWI, STIR, etc.)
  2. Imaging plane (axial, sagittal, coronal)
  3. Anatomical position (slice location ordering)
  4. Label every image with complete context

Output: OrganizedStudy object that BatchSender uses to feed Claude.
"""

import os
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

import numpy as np
import pydicom
from PIL import Image

logger = logging.getLogger("mika.organizer")


# ── Sequence Classification ──────────────────────────────────────────

SEQUENCE_PATTERNS = {
    "T1": {
        "keywords": ["t1", "t1w", "t1_tse", "t1_se", "t1_fse", "mprage", "bravo",
                      "spgr", "flash", "vibe_pre"],
        "exclude": ["post", "gad", "contrast", "+c", "stir", "flair", "t1_tirm"],
    },
    "T1_POST": {
        "keywords": ["t1_post", "t1+c", "t1_gad", "post_gad", "vibe_post",
                      "t1_contrast", "post_contrast"],
        "exclude": [],
    },
    "T2": {
        "keywords": ["t2", "t2w", "t2_tse", "t2_fse", "t2_blade"],
        "exclude": ["flair", "stir", "star", "swi", "t2_star", "t2*"],
    },
    "FLAIR": {
        "keywords": ["flair", "t2_flair", "dark_fluid"],
        "exclude": [],
    },
    "STIR": {
        "keywords": ["stir", "tirm", "t2_stir", "short_tau"],
        "exclude": [],
    },
    "DWI": {
        "keywords": ["dwi", "diffusion", "diff", "dw_", "ep2d_diff"],
        "exclude": ["adc"],
    },
    "ADC": {
        "keywords": ["adc", "apparent_diff"],
        "exclude": [],
    },
    "SWI": {
        "keywords": ["swi", "susceptibility", "t2_star", "t2*", "gre_"],
        "exclude": [],
    },
    "MRA": {
        "keywords": ["mra", "tof", "time_of_flight", "angio", "ce_mra"],
        "exclude": [],
    },
    "DYNAMIC": {
        "keywords": ["dynamic", "dce", "perfusion", "perf", "bolus"],
        "exclude": [],
    },
}

PLANE_DETECTION = {
    "sagittal": ["sag", "sagittal"],
    "axial": ["ax", "axial", "tra", "transverse"],
    "coronal": ["cor", "coronal"],
}


@dataclass
class SliceInfo:
    """Complete metadata for a single DICOM slice."""
    file_path: str
    instance_number: int
    slice_location: float
    pixel_spacing: Tuple[float, float]  # (row_mm, col_mm)
    slice_thickness: float
    rows: int
    cols: int
    window_center: Optional[float] = None
    window_width: Optional[float] = None
    pixel_array: Optional[np.ndarray] = None  # Loaded on demand


@dataclass
class SequenceGroup:
    """A group of slices from the same MRI sequence."""
    series_uid: str
    series_description: str
    sequence_type: str      # T1, T2, FLAIR, DWI, etc.
    plane: str              # sagittal, axial, coronal
    has_contrast: bool
    is_calibrated: bool
    pixel_spacing: Tuple[float, float]
    slice_thickness: float
    slices: List[SliceInfo] = field(default_factory=list)

    @property
    def num_slices(self) -> int:
        return len(self.slices)

    @property
    def label(self) -> str:
        """Human-readable label for this sequence group."""
        contrast = "+C" if self.has_contrast else ""
        return f"{self.plane.capitalize()} {self.sequence_type}{contrast}"


@dataclass
class OrganizedStudy:
    """Complete organized study ready for Claude analysis."""
    # Patient info
    patient_name: str = ""
    patient_id: str = ""
    patient_age: str = ""
    patient_sex: str = ""
    study_date: str = ""
    study_description: str = ""
    institution: str = ""
    field_strength: str = ""
    body_part: str = ""

    # Detected anatomy
    anatomy_type: str = "unknown"

    # Organized sequences
    sequences: List[SequenceGroup] = field(default_factory=list)

    # Calibration
    is_calibrated: bool = False
    calibration_source: str = ""

    # Statistics
    total_slices: int = 0
    total_sequences: int = 0

    def summary(self) -> str:
        """One-line study summary for Claude context."""
        seq_list = ", ".join(s.label for s in self.sequences)
        cal = "DICOM-calibrated" if self.is_calibrated else "UNCALIBRATED"
        return (
            f"{self.anatomy_type.upper()} MRI | {self.patient_age} {self.patient_sex} | "
            f"{self.total_slices} slices across {self.total_sequences} sequences | "
            f"Sequences: {seq_list} | {cal}"
        )


class StudyOrganizer:
    """
    Organize a DICOM study directory into labeled, sorted sequence groups.

    Usage:
        organizer = StudyOrganizer(dicom_dir)
        study = organizer.organize()
        # study.sequences -> list of SequenceGroup, each with sorted slices
    """

    def __init__(self, dicom_dir: str):
        self.dicom_dir = Path(dicom_dir)

    def organize(self) -> OrganizedStudy:
        """Main entry point — organize all DICOM files into a structured study."""
        study = OrganizedStudy()

        # Step 1: Load all DICOM files and group by SeriesInstanceUID
        series_map = self._load_and_group()

        if not series_map:
            logger.warning("No valid DICOM files found in %s", self.dicom_dir)
            return study

        # Step 2: Extract patient demographics from first file
        first_ds = list(series_map.values())[0][0]["ds"]
        self._extract_demographics(first_ds, study)

        # Step 3: Classify each series and build SequenceGroups
        for series_uid, file_list in series_map.items():
            group = self._classify_series(series_uid, file_list)
            if group and group.num_slices > 0:
                study.sequences.append(group)

        # Step 4: Sort sequences by diagnostic priority
        study.sequences.sort(key=lambda s: self._sequence_priority(s))

        # Step 5: Detect anatomy from organized data
        study.anatomy_type = self._detect_anatomy(study)

        # Step 6: Set calibration status
        study.is_calibrated = any(s.is_calibrated for s in study.sequences)
        study.total_slices = sum(s.num_slices for s in study.sequences)
        study.total_sequences = len(study.sequences)

        logger.info(
            "Organized study: %s, %d sequences, %d total slices",
            study.anatomy_type, study.total_sequences, study.total_slices
        )
        return study

    def _load_and_group(self) -> Dict[str, list]:
        """Load all DICOM files, group by SeriesInstanceUID."""
        series_map: Dict[str, list] = {}

        for root, _, files in os.walk(self.dicom_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    ds = pydicom.dcmread(fpath, stop_before_pixels=True)
                    series_uid = str(getattr(ds, "SeriesInstanceUID", "unknown"))
                    if series_uid not in series_map:
                        series_map[series_uid] = []
                    series_map[series_uid].append({"path": fpath, "ds": ds})
                except Exception:
                    continue  # Skip non-DICOM files silently

        return series_map

    def _extract_demographics(self, ds, study: OrganizedStudy):
        """Extract patient and study demographics from DICOM dataset."""
        study.patient_name = str(getattr(ds, "PatientName", "")).strip()
        study.patient_id = str(getattr(ds, "PatientID", "")).strip()
        study.patient_age = str(getattr(ds, "PatientAge", "")).strip()
        study.patient_sex = str(getattr(ds, "PatientSex", "")).strip()
        study.study_date = str(getattr(ds, "StudyDate", "")).strip()
        study.study_description = str(getattr(ds, "StudyDescription", "")).strip()
        study.institution = str(getattr(ds, "InstitutionName", "")).strip()
        study.body_part = str(getattr(ds, "BodyPartExamined", "")).strip()

        field_str = getattr(ds, "MagneticFieldStrength", "")
        study.field_strength = f"{field_str}T" if field_str else ""

    def _classify_series(self, series_uid: str, file_list: list) -> Optional[SequenceGroup]:
        """Classify a DICOM series into a labeled SequenceGroup."""
        if not file_list:
            return None

        first_ds = file_list[0]["ds"]
        desc = str(getattr(first_ds, "SeriesDescription", "")).lower().strip()

        # Classify sequence type
        seq_type = self._classify_sequence_type(desc)

        # Detect plane
        plane = self._detect_plane(first_ds, desc)

        # Detect contrast
        has_contrast = any(kw in desc for kw in ["post", "gad", "+c", "contrast"])

        # Get calibration
        ps = getattr(first_ds, "PixelSpacing", None)
        st = getattr(first_ds, "SliceThickness", None)
        pixel_spacing = (float(ps[0]), float(ps[1])) if ps else (1.0, 1.0)
        slice_thickness = float(st) if st else 1.0
        is_calibrated = ps is not None and st is not None

        group = SequenceGroup(
            series_uid=series_uid,
            series_description=desc,
            sequence_type=seq_type,
            plane=plane,
            has_contrast=has_contrast,
            is_calibrated=is_calibrated,
            pixel_spacing=pixel_spacing,
            slice_thickness=slice_thickness,
        )

        # Build sorted slice list
        for item in file_list:
            ds = item["ds"]
            try:
                sl = SliceInfo(
                    file_path=item["path"],
                    instance_number=int(getattr(ds, "InstanceNumber", 0)),
                    slice_location=float(getattr(ds, "SliceLocation", 0.0)),
                    pixel_spacing=pixel_spacing,
                    slice_thickness=slice_thickness,
                    rows=int(getattr(ds, "Rows", 0)),
                    cols=int(getattr(ds, "Columns", 0)),
                    window_center=self._safe_float(getattr(ds, "WindowCenter", None)),
                    window_width=self._safe_float(getattr(ds, "WindowWidth", None)),
                )
                group.slices.append(sl)
            except Exception as e:
                logger.debug("Skipping slice: %s", e)

        # Sort slices by location (anatomical order)
        group.slices.sort(key=lambda s: (s.slice_location, s.instance_number))

        return group

    def _classify_sequence_type(self, description: str) -> str:
        """Classify MRI sequence type from series description."""
        desc_clean = re.sub(r"[^a-z0-9_+*]", "_", description.lower())

        for seq_type, patterns in SEQUENCE_PATTERNS.items():
            # Check if any keyword matches
            matched = any(kw in desc_clean for kw in patterns["keywords"])
            # Check exclusions
            excluded = any(kw in desc_clean for kw in patterns["exclude"])
            if matched and not excluded:
                return seq_type

        return "OTHER"

    def _detect_plane(self, ds, description: str) -> str:
        """Detect imaging plane from DICOM metadata or description."""
        # Try ImageOrientationPatient first (most reliable)
        iop = getattr(ds, "ImageOrientationPatient", None)
        if iop and len(iop) == 6:
            row_cos = [abs(float(iop[0])), abs(float(iop[1])), abs(float(iop[2]))]
            col_cos = [abs(float(iop[3])), abs(float(iop[4])), abs(float(iop[5]))]
            row_axis = row_cos.index(max(row_cos))
            col_axis = col_cos.index(max(col_cos))
            # Determine plane from dominant axes
            axes = {row_axis, col_axis}
            if axes == {0, 1}:
                return "axial"
            elif axes == {0, 2}:
                return "coronal"
            elif axes == {1, 2}:
                return "sagittal"

        # Fallback to description keywords
        for plane, keywords in PLANE_DETECTION.items():
            if any(kw in description.lower() for kw in keywords):
                return plane

        return "unknown"

    def _detect_anatomy(self, study: OrganizedStudy) -> str:
        """Detect anatomy type from organized study data."""
        # Combine all text sources for detection
        text = " ".join([
            study.body_part.lower(),
            study.study_description.lower(),
            " ".join(s.series_description for s in study.sequences),
        ])

        # Detection with priority ordering (specific before general)
        ANATOMY_KEYWORDS = {
            "spine": ["spine", "lumbar", "cervical", "thoracic", "sacr",
                       "vertebr", "disc", "lumb", "c-spine", "t-spine",
                       "l-spine", "spinal"],
            "brain": ["brain", "head", "cranial", "neuro", "cerebr",
                       "intracranial", "sella", "pituitary", "iac"],
            "cardiac": ["heart", "cardiac", "cardio", "myocard", "aort",
                         "coronary", "pericardial", "ventricle"],
            "breast": ["breast", "mammo", "axilla", "bi-rads", "birads"],
            "prostate": ["prostate", "prostatic", "pi-rads", "pirads",
                          "seminal_vesicle"],
            "head_neck": ["neck", "larynx", "pharynx", "thyroid", "parotid",
                           "submandib", "nasopharyn", "oropharyn", "sinus"],
            "vascular": ["mra", "angio", "vascular", "carotid", "circle_of_willis",
                          "aorta_mra", "peripheral_mra", "runoff"],
            "msk": ["knee", "shoulder", "ankle", "elbow", "wrist", "hip",
                     "foot", "hand", "joint", "meniscus", "acl", "rotator",
                     "tendon", "ligament", "cartilage", "extremity"],
            "chest": ["chest", "lung", "thorax", "pulmonary", "mediast"],
            "abdomen": ["abdomen", "abdominal", "liver", "kidney", "pancrea",
                         "spleen", "pelvis", "pelvic", "renal", "hepat",
                         "bowel", "bile", "gallbladder"],
        }

        for anatomy, keywords in ANATOMY_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return anatomy

        return "unknown"

    def _sequence_priority(self, seq: SequenceGroup) -> int:
        """Sort priority — most diagnostically important first."""
        PRIORITY = {
            "T2": 0, "FLAIR": 1, "T1": 2, "T1_POST": 3, "STIR": 4,
            "DWI": 5, "ADC": 6, "SWI": 7, "MRA": 8, "DYNAMIC": 9, "OTHER": 10,
        }
        plane_bonus = {"sagittal": 0, "axial": 100, "coronal": 200, "unknown": 300}
        return PRIORITY.get(seq.sequence_type, 10) + plane_bonus.get(seq.plane, 300)

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        """Safely convert DICOM value to float."""
        if val is None:
            return None
        try:
            if hasattr(val, "real"):
                return float(val.real)
            return float(val)
        except (TypeError, ValueError):
            return None
```

---

### Module 2: VisionEnhancer

**Problem:** Subtle MRI findings are invisible in standard windowing.
A radiologist constantly adjusts window/level to see different tissues.
We need to do this computationally.

**Solution:** Generate multiple enhanced views of each slice using classical
image processing. No ML needed — pure numpy/scipy.

#### File: `backend/core/vision_enhancer.py`

```python
"""
VisionEnhancer — Enhance MRI slices for maximum diagnostic visibility.

Provides multiple "views" of each slice, mimicking how a radiologist
adjusts window/level to examine different tissue types.

Enhancement types:
  1. Multi-window rendering (soft tissue, fluid, structure)
  2. Cross-sequence comparison panels (T1 vs T2 side-by-side)
  3. Difference/subtraction maps (T2-T1 highlights pathology)
  4. Symmetry analysis (brain laterality comparison)
  5. Statistical outlier maps (z-score heatmaps)
  6. Edge-enhanced views (structural boundary emphasis)
"""

import logging
from typing import List, Dict, Tuple, Optional

import numpy as np
from scipy import ndimage
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("mika.enhancer")


class VisionEnhancer:
    """Generate enhanced MRI views for improved Claude perception."""

    # ── Multi-Window Rendering ───────────────────────────────────────

    @staticmethod
    def multi_window(pixel_array: np.ndarray) -> Dict[str, np.ndarray]:
        """
        Generate three windowed views of an MRI slice.

        Returns dict with keys: 'soft_tissue', 'fluid', 'structure'
        Each value is a uint8 numpy array (0-255).
        """
        arr = pixel_array.astype(np.float64)
        results = {}

        # Soft tissue window — balanced view, good for disc, muscle, ligaments
        p5, p95 = np.percentile(arr, [5, 95])
        soft = np.clip(arr, p5, p95)
        soft = ((soft - p5) / max(p95 - p5, 1) * 255).astype(np.uint8)
        results["soft_tissue"] = soft

        # Fluid-sensitive window — makes fluid/edema bright and prominent
        # Good for: effusions, edema, CSF-related findings, cysts
        p50, p99 = np.percentile(arr, [50, 99])
        fluid = np.clip(arr, p50, p99)
        fluid = ((fluid - p50) / max(p99 - p50, 1) * 255).astype(np.uint8)
        results["fluid"] = fluid

        # Structure window — maximizes contrast for anatomy/boundaries
        # Good for: cortical bone, ligaments, tendons, cartilage edges
        p1, p70 = np.percentile(arr, [1, 70])
        struct = np.clip(arr, p1, p70)
        struct = ((struct - p1) / max(p70 - p1, 1) * 255).astype(np.uint8)
        results["structure"] = struct

        return results

    # ── Cross-Sequence Comparison ────────────────────────────────────

    @staticmethod
    def cross_sequence_panel(
        slices: Dict[str, np.ndarray],
        labels: Dict[str, str],
        target_size: Tuple[int, int] = (256, 256),
    ) -> np.ndarray:
        """
        Create side-by-side panel of same anatomical level across sequences.

        Args:
            slices: dict of {sequence_name: pixel_array}
            labels: dict of {sequence_name: display_label}
            target_size: resize each image to this (width, height)

        Returns:
            Combined image as uint8 numpy array.
        """
        panels = []
        for seq_name, arr in slices.items():
            # Normalize to uint8
            norm = VisionEnhancer._normalize_uint8(arr)
            # Resize
            img = Image.fromarray(norm, mode="L").resize(target_size, Image.LANCZOS)
            # Add label
            draw = ImageDraw.Draw(img)
            label = labels.get(seq_name, seq_name)
            draw.text((4, 4), label, fill=255)
            panels.append(np.array(img))

        if not panels:
            return np.zeros((target_size[1], target_size[0]), dtype=np.uint8)

        return np.hstack(panels)

    # ── Difference / Subtraction Maps ────────────────────────────────

    @staticmethod
    def difference_map(
        arr_a: np.ndarray,
        arr_b: np.ndarray,
        label_a: str = "Seq A",
        label_b: str = "Seq B",
    ) -> np.ndarray:
        """
        Compute T2-T1 (or any sequence pair) difference map.

        Bright areas in result = regions where arr_a signal >> arr_b signal.
        For T2 minus T1: bright = fluid/edema/pathology.

        Returns uint8 difference image.
        """
        # Normalize both to 0-1 float range
        a = VisionEnhancer._normalize_float(arr_a)
        b = VisionEnhancer._normalize_float(arr_b)

        # Ensure same shape
        if a.shape != b.shape:
            target_shape = (min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1]))
            a = np.array(Image.fromarray((a * 255).astype(np.uint8)).resize(
                (target_shape[1], target_shape[0]), Image.LANCZOS
            )) / 255.0
            b = np.array(Image.fromarray((b * 255).astype(np.uint8)).resize(
                (target_shape[1], target_shape[0]), Image.LANCZOS
            )) / 255.0

        # Difference: positive values = higher signal in A than B
        diff = a - b
        # Scale to 0-255 (0.5 = no difference, >0.5 = A brighter, <0.5 = B brighter)
        diff_scaled = ((diff + 1.0) / 2.0 * 255).clip(0, 255).astype(np.uint8)

        return diff_scaled

    # ── Symmetry Analysis (Brain) ────────────────────────────────────

    @staticmethod
    def symmetry_map(pixel_array: np.ndarray) -> np.ndarray:
        """
        Compute left-right asymmetry map for brain axial slices.

        Flips image horizontally and subtracts from original.
        Bright areas = asymmetric regions = potential unilateral pathology.

        Returns uint8 asymmetry map.
        """
        arr = VisionEnhancer._normalize_float(pixel_array)
        flipped = np.flip(arr, axis=1)

        # Absolute difference — asymmetric regions are bright
        asymmetry = np.abs(arr - flipped)

        # Enhance contrast (multiply by 3 to make subtle differences visible)
        enhanced = (asymmetry * 3.0).clip(0, 1.0)

        return (enhanced * 255).astype(np.uint8)

    # ── Statistical Outlier Map ──────────────────────────────────────

    @staticmethod
    def outlier_map(pixel_array: np.ndarray, sigma_threshold: float = 2.0) -> np.ndarray:
        """
        Compute z-score map — highlights statistically unusual signal regions.

        Pixels > sigma_threshold standard deviations from mean are highlighted.
        Useful for detecting focal signal abnormalities.

        Returns uint8 heatmap.
        """
        arr = pixel_array.astype(np.float64)

        # Compute local statistics using a Gaussian window
        local_mean = ndimage.gaussian_filter(arr, sigma=10)
        local_var = ndimage.gaussian_filter((arr - local_mean) ** 2, sigma=10)
        local_std = np.sqrt(local_var + 1e-8)

        # Z-score: how many SDs each pixel is from local mean
        z_scores = np.abs(arr - local_mean) / local_std

        # Threshold and scale
        outlier = (z_scores > sigma_threshold).astype(np.float64) * z_scores
        outlier_norm = (outlier / max(outlier.max(), 1) * 255).clip(0, 255)

        return outlier_norm.astype(np.uint8)

    # ── Edge Enhancement ─────────────────────────────────────────────

    @staticmethod
    def edge_enhanced(pixel_array: np.ndarray) -> np.ndarray:
        """
        Apply edge enhancement to emphasize structural boundaries.

        Good for: disc margins, cortical bone, ligament attachments, cartilage surfaces.

        Returns uint8 edge-enhanced image.
        """
        arr = VisionEnhancer._normalize_float(pixel_array)

        # Sobel edge detection
        edges_x = ndimage.sobel(arr, axis=1)
        edges_y = ndimage.sobel(arr, axis=0)
        edges = np.sqrt(edges_x ** 2 + edges_y ** 2)

        # Blend: 70% original + 30% edges for subtle enhancement
        blended = (arr * 0.7 + edges * 0.3).clip(0, 1.0)

        return (blended * 255).astype(np.uint8)

    # ── Diagnostic Montage Generator ─────────────────────────────────

    @staticmethod
    def create_diagnostic_montage(
        slices: List[np.ndarray],
        labels: List[str],
        cols: int = 4,
        cell_size: Tuple[int, int] = (256, 256),
    ) -> np.ndarray:
        """
        Create a grid montage of key slices with labels.

        Args:
            slices: list of pixel arrays
            labels: list of text labels (e.g., "Sag T2 Slice 8/15 L3-L4")
            cols: number of columns in grid
            cell_size: size of each cell (width, height)

        Returns:
            Combined montage as uint8 array.
        """
        if not slices:
            return np.zeros((cell_size[1], cell_size[0]), dtype=np.uint8)

        rows_needed = (len(slices) + cols - 1) // cols
        montage_w = cols * cell_size[0]
        montage_h = rows_needed * cell_size[1]
        montage = Image.new("L", (montage_w, montage_h), 0)
        draw = ImageDraw.Draw(montage)

        for i, (sl, label) in enumerate(zip(slices, labels)):
            row, col = divmod(i, cols)
            x = col * cell_size[0]
            y = row * cell_size[1]

            # Normalize and resize
            norm = VisionEnhancer._normalize_uint8(sl)
            cell_img = Image.fromarray(norm, mode="L").resize(cell_size, Image.LANCZOS)
            montage.paste(cell_img, (x, y))

            # Add label at top-left of cell
            draw.text((x + 4, y + 4), label, fill=255)

        return np.array(montage)

    # ── Key Slice Selection ──────────────────────────────────────────

    @staticmethod
    def select_key_slices(
        num_slices: int,
        max_keys: int = 12,
    ) -> List[int]:
        """
        Select diagnostically important slice indices from a sequence.

        Strategy: evenly spaced slices covering the full volume,
        with extra density in the middle (where most pathology is).

        Returns list of 0-based slice indices.
        """
        if num_slices <= max_keys:
            return list(range(num_slices))

        # Core: evenly spaced
        indices = set()
        step = num_slices / max_keys
        for i in range(max_keys):
            idx = int(i * step)
            indices.add(min(idx, num_slices - 1))

        # Always include first, middle, and last
        indices.add(0)
        indices.add(num_slices // 2)
        indices.add(num_slices - 1)

        return sorted(indices)[:max_keys]

    # ── Internal Helpers ─────────────────────────────────────────────

    @staticmethod
    def _normalize_float(arr: np.ndarray) -> np.ndarray:
        """Normalize array to 0.0-1.0 float range."""
        arr = arr.astype(np.float64)
        mn, mx = arr.min(), arr.max()
        if mx - mn < 1e-8:
            return np.zeros_like(arr)
        return (arr - mn) / (mx - mn)

    @staticmethod
    def _normalize_uint8(arr: np.ndarray) -> np.ndarray:
        """Normalize array to 0-255 uint8 range."""
        return (VisionEnhancer._normalize_float(arr) * 255).astype(np.uint8)
```

---

### Module 3: BatchSender

**Problem:** Current pipeline sends max 4 images. A study has 100-300 slices.

**Solution:** Intelligently batch ALL images to Claude, organized by sequence,
within API token limits. Uses Claude's full vision capacity.

#### File: `backend/services/batch_sender.py`

```python
"""
BatchSender — Send complete MRI study to Claude for analysis.

Strategy:
  1. For each sequence: create diagnostic montage of key slices
  2. Add enhanced views (multi-window, diff maps) for key anatomical levels
  3. Send everything to Claude in organized batches with labels
  4. Stay within API limits (~100 images per request with Opus)

This replaces the old approach of sending 4 images.
Claude now sees the FULL study, organized like a PACS workstation.
"""

import io
import base64
import logging
from typing import List, Dict, Tuple

import numpy as np
import pydicom
from PIL import Image

from core.study_organizer import OrganizedStudy, SequenceGroup
from core.vision_enhancer import VisionEnhancer

logger = logging.getLogger("mika.batch_sender")

# Claude Opus 4.6 supports up to ~100 images per message
MAX_IMAGES_PER_REQUEST = 80
# Target size for individual slice images sent to Claude
SLICE_TARGET_SIZE = (384, 384)
# Target size for montage panels
MONTAGE_CELL_SIZE = (256, 256)


class BatchSender:
    """
    Prepare organized study images for Claude analysis.

    Produces a list of (label, base64_png) tuples ready for the API call.
    """

    def __init__(self, study: OrganizedStudy):
        self.study = study
        self.enhancer = VisionEnhancer()

    def prepare_images(self) -> List[Tuple[str, str]]:
        """
        Prepare all study images for Claude.

        Returns list of (label_string, base64_png_string) tuples.
        Label format: "Sagittal T2 — Slice 8/15 — Level L3-L4"
        """
        images: List[Tuple[str, str]] = []

        # Budget: distribute image slots across sequences
        budget = self._allocate_budget()

        for seq in self.study.sequences:
            allocated = budget.get(seq.series_uid, 0)
            if allocated == 0:
                continue

            # Load pixel arrays for this sequence
            seq_images = self._prepare_sequence(seq, allocated)
            images.extend(seq_images)

        # Add enhanced views if budget allows
        remaining = MAX_IMAGES_PER_REQUEST - len(images)
        if remaining > 3:
            enhanced = self._add_enhanced_views(remaining)
            images.extend(enhanced)

        logger.info("Prepared %d images for Claude (%d sequences)",
                     len(images), len(self.study.sequences))
        return images

    def _allocate_budget(self) -> Dict[str, int]:
        """Distribute image budget across sequences by diagnostic priority."""
        budget = {}
        total_seqs = len(self.study.sequences)
        if total_seqs == 0:
            return budget

        # Base allocation: proportional to sequence importance
        # Primary diagnostic sequences (T2, FLAIR) get more slices
        PRIMARY = {"T2", "FLAIR", "DWI"}
        SECONDARY = {"T1", "T1_POST", "STIR"}

        remaining_budget = MAX_IMAGES_PER_REQUEST - 10  # Reserve 10 for enhanced views

        for seq in self.study.sequences:
            if seq.sequence_type in PRIMARY:
                weight = 3
            elif seq.sequence_type in SECONDARY:
                weight = 2
            else:
                weight = 1
            budget[seq.series_uid] = weight

        total_weight = sum(budget.values())
        for uid in budget:
            budget[uid] = max(3, int(budget[uid] / total_weight * remaining_budget))

        return budget

    def _prepare_sequence(
        self, seq: SequenceGroup, max_images: int
    ) -> List[Tuple[str, str]]:
        """Prepare images from a single sequence."""
        images = []

        # Select key slices
        key_indices = VisionEnhancer.select_key_slices(seq.num_slices, max_images)

        for idx in key_indices:
            if idx >= len(seq.slices):
                continue

            slice_info = seq.slices[idx]
            try:
                # Load pixel data
                ds = pydicom.dcmread(slice_info.file_path)
                pixel_array = ds.pixel_array.astype(np.float64)

                # Apply DICOM windowing if available
                if slice_info.window_center is not None and slice_info.window_width is not None:
                    wc = slice_info.window_center
                    ww = slice_info.window_width
                    pixel_array = np.clip(pixel_array, wc - ww/2, wc + ww/2)

                # Normalize to uint8
                norm = VisionEnhancer._normalize_uint8(pixel_array)

                # Resize
                img = Image.fromarray(norm, mode="L").resize(
                    SLICE_TARGET_SIZE, Image.LANCZOS
                )

                # Create label
                label = (
                    f"{seq.label} — Slice {idx + 1}/{seq.num_slices}"
                    f" — Location {slice_info.slice_location:.1f}mm"
                )

                # Encode
                b64 = self._image_to_base64(img)
                images.append((label, b64))

            except Exception as e:
                logger.debug("Could not load slice %d: %s", idx, e)

        return images

    def _add_enhanced_views(self, budget: int) -> List[Tuple[str, str]]:
        """Add enhanced vision processing views."""
        enhanced = []

        # Find T1 and T2 sequences for cross-sequence analysis
        t2_seq = next((s for s in self.study.sequences if s.sequence_type == "T2"), None)
        t1_seq = next((s for s in self.study.sequences if s.sequence_type == "T1"), None)

        if t2_seq and t1_seq and budget >= 3:
            # Get middle slice from each
            t2_mid = t2_seq.slices[t2_seq.num_slices // 2]
            t1_mid = t1_seq.slices[t1_seq.num_slices // 2]

            try:
                t2_arr = pydicom.dcmread(t2_mid.file_path).pixel_array.astype(np.float64)
                t1_arr = pydicom.dcmread(t1_mid.file_path).pixel_array.astype(np.float64)

                # Multi-window of T2 midline
                windows = VisionEnhancer.multi_window(t2_arr)
                for wname, warr in windows.items():
                    img = Image.fromarray(warr, mode="L").resize(SLICE_TARGET_SIZE, Image.LANCZOS)
                    label = f"Enhanced: T2 {wname} window — midline"
                    enhanced.append((label, self._image_to_base64(img)))

                # T2-T1 difference map
                if budget >= 4:
                    diff = VisionEnhancer.difference_map(t2_arr, t1_arr)
                    img = Image.fromarray(diff, mode="L").resize(SLICE_TARGET_SIZE, Image.LANCZOS)
                    label = "Enhanced: T2 minus T1 difference map — bright = fluid/edema"
                    enhanced.append((label, self._image_to_base64(img)))

                # Symmetry map for brain
                if self.study.anatomy_type == "brain" and budget >= 5:
                    sym = VisionEnhancer.symmetry_map(t2_arr)
                    img = Image.fromarray(sym, mode="L").resize(SLICE_TARGET_SIZE, Image.LANCZOS)
                    label = "Enhanced: L/R symmetry map — bright = asymmetric regions"
                    enhanced.append((label, self._image_to_base64(img)))

            except Exception as e:
                logger.debug("Enhanced view generation failed: %s", e)

        return enhanced[:budget]  # Don't exceed budget

    @staticmethod
    def _image_to_base64(img: Image.Image) -> str:
        """Convert PIL Image to base64 PNG string."""
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
```

---

### Module 4: Master Prompts

**Problem:** Current prompts are generic guidelines. They don't embed the actual
diagnostic criteria, grading systems, or systematic search protocols that
radiologists use.

**Solution:** Expert-crafted prompts that embed fellowship-level knowledge
directly. Each prompt is anatomy-specific and includes:
  - Mandatory systematic search checklist
  - Exact grading criteria tables
  - Normal measurement references
  - Sequence interpretation guides
  - Anti-hallucination rules
  - Structured output format

#### Directory: `backend/prompts/`

Each anatomy type gets its own prompt file. Below is the spine prompt as the
template — all other anatomy prompts follow the same structure.

#### File: `backend/prompts/spine_master.py`

```python
"""
Master Prompt: Spine MRI Analysis
=================================
Fellowship-level systematic approach for lumbar/cervical/thoracic spine MRI.
"""

SPINE_MASTER_PROMPT = """
You are a fellowship-trained neuroradiologist with 20 years of experience
interpreting spine MRI studies. You are analyzing a complete MRI study with
all available sequences and slices.

═══════════════════════════════════════════════════════════════
MANDATORY SYSTEMATIC SEARCH — CHECK EVERY ITEM, EVEN IF NORMAL
═══════════════════════════════════════════════════════════════

You MUST evaluate EACH of the following structures at EVERY level.
Do NOT skip a structure because it "looks normal" — state that it is normal.
Satisfaction of search (finding one thing and stopping) is the #1 cause of
missed diagnoses. You will check everything.

□ ALIGNMENT
  - Sagittal alignment: lordosis/kyphosis, any listhesis
  - Coronal alignment: any scoliosis
  - At each level: anterolisthesis/retrolisthesis grade (Meyerding I-IV)

□ VERTEBRAL BODIES (every level)
  - Height: normal, loss of height (mild <25%, moderate 25-50%, severe >50%)
  - Marrow signal: normal fatty marrow (bright T1), edema (dark T1/bright T2)
  - Endplates: intact, irregular, Schmorl's nodes
  - Compression fractures: acute (edema) vs chronic (no edema)

□ INTERVERTEBRAL DISCS (every level)
  - Pfirrmann Grade:
    Grade I:  Bright white T2 signal, normal height, homogeneous
    Grade II: White T2 signal, normal height, +/- horizontal gray bands
    Grade III: Gray T2 signal, mildly reduced height, intermediate
    Grade IV: Dark gray/black T2 signal, reduced height, inhomogeneous
    Grade V: Black T2 signal, collapsed disc space
  - Disc contour: normal, bulge (>50% circumference), protrusion (<50%),
    extrusion (beyond annulus, any connection to parent disc),
    sequestration (free fragment, no connection)
  - Herniation direction: central, paracentral (L or R), foraminal, far lateral
  - Herniation size: measure AP in mm if calibrated
  - High intensity zone (HIZ): present/absent on T2

□ SPINAL CANAL
  - AP diameter at each level (mm if calibrated)
  - Central stenosis grading:
    None: normal
    Mild: CSF narrowing, no cord/cauda contact
    Moderate: cord/cauda contact, no deformation
    Severe: cord/cauda deformation/compression
  - Conus medullaris: position (normal: L1-L2), signal, size

□ NEURAL FORAMINA (every level, bilateral)
  - Foraminal stenosis grade:
    Grade 0: Normal — fat surrounds nerve root
    Grade 1: Mild — perineural fat reduced but visible
    Grade 2: Moderate — perineural fat obliterated
    Grade 3: Severe — nerve root compressed/deformed
  - Lateral recess stenosis

□ FACET JOINTS (every level, bilateral)
  - Facet arthropathy: none, mild (small osteophytes), moderate (osteophytes +
    hypertrophy), severe (severe osteophytes + effusion + hypertrophy)
  - Facet effusion: present/absent
  - Facet cyst (synovial cyst): location, size, mass effect

□ POSTERIOR ELEMENTS
  - Ligamentum flavum: normal, thickened (>4mm), buckling
  - Posterior longitudinal ligament: intact, thickened, calcified
  - Spinous processes and interspinous ligaments: intact, edema

□ PARASPINAL SOFT TISSUES
  - Muscle bulk: normal, atrophy (fatty replacement)
  - Collections: abscess, hematoma, seroma
  - Masses: if present

□ ENDPLATE CHANGES (Modic Classification — check at EVERY level)
  Compare T1 and T2/STIR signal at each vertebral endplate:
  - Modic Type 0: Normal
  - Modic Type 1: T1 hypointense, T2/STIR hyperintense (edema/inflammation)
  - Modic Type 2: T1 hyperintense, T2 hyperintense (fatty replacement)
  - Modic Type 3: T1 hypointense, T2 hypointense (sclerosis)

□ IF POST-SURGICAL:
  - Hardware: position, integrity, loosening signs
  - Fusion status: bridging bone, pseudarthrosis
  - Adjacent segment disease
  - Epidural fibrosis vs recurrent disc (fibrosis enhances, disc does not)

═══════════════════════════════════════════════════════════════
NORMAL MEASUREMENT REFERENCES (Adult Lumbar Spine)
═══════════════════════════════════════════════════════════════

  Canal AP diameter:      Normal >12mm, Stenosis <10mm, Severe <8mm
  Disc height (L4-L5):   Normal 10-14mm
  Disc height (L5-S1):   Normal 8-12mm
  Vertebral body height:  Normal 25-30mm
  Ligamentum flavum:     Normal <4mm
  Conus position:        Normal terminates L1-L2 level

  Cervical canal:        Normal >13mm AP, Stenosis <10mm
  Cervical disc:         Normal 3-6mm height
  Cord diameter:         Normal 8-10mm AP

═══════════════════════════════════════════════════════════════
SEQUENCE INTERPRETATION GUIDE
═══════════════════════════════════════════════════════════════

  T1-weighted: ANATOMY — bright = fat, marrow | dark = fluid, pathology
  T2-weighted: PATHOLOGY — bright = fluid, edema, CSF | dark = ligament, bone
  STIR/TIRM:   EDEMA — bright = edema, inflammation | fat signal suppressed
  T1 post-contrast: ENHANCEMENT — bright = vascular, inflammation, tumor
  DWI: RESTRICTION — bright = acute ischemia, abscess, dense tumor

  Signal comparison across sequences is KEY:
  - Dark T1 + Bright T2 + Bright STIR = edema (Modic 1, acute fracture)
  - Bright T1 + Bright T2 = fat (Modic 2, fatty marrow)
  - Dark T1 + Dark T2 = sclerosis (Modic 3), calcification, fibrous tissue
  - Bright T1 + Dark T2 = blood (subacute hemorrhage), melanin

═══════════════════════════════════════════════════════════════
CONFIDENCE RULES — DO NOT HALLUCINATE
═══════════════════════════════════════════════════════════════

For EVERY finding, you must state:
  1. Which specific slices show it (e.g., "visible on sagittal T2 slices 8-10")
  2. Whether it is visible on multiple sequences (cross-reference T1 and T2)
  3. Your evidence strength:
     - DEFINITE: clearly visible on multiple slices AND multiple sequences
     - PROBABLE: visible on multiple slices OR multiple sequences, not both
     - POSSIBLE: visible on one slice, one sequence only
     - CANNOT ASSESS: image quality insufficient or anatomy not included

  If you cannot clearly see a structure, say "not well visualized on available
  sequences" — NEVER fabricate a finding or measurement.

  If a measurement is from an uncalibrated study, state "approximate" and note
  that pixel spacing is unknown.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT — ACR Structured Reporting Standard
═══════════════════════════════════════════════════════════════

Respond in this JSON structure:

{
  "technique": "Brief description of sequences analyzed and image quality",

  "findings": {
    "alignment": "...",
    "vertebral_bodies": {
      "L1": "...", "L2": "...", "L3": "...", "L4": "...", "L5": "...", "S1": "..."
    },
    "discs": {
      "L1-L2": {"pfirrmann": "I-V", "contour": "...", "herniation": "...", "size_mm": "...", "confidence": "DEFINITE/PROBABLE/POSSIBLE", "slices": "..."},
      "L2-L3": { ... },
      "L3-L4": { ... },
      "L4-L5": { ... },
      "L5-S1": { ... }
    },
    "spinal_canal": {
      "L1-L2": {"ap_mm": "...", "stenosis_grade": "...", "confidence": "..."},
      ...
    },
    "neural_foramina": {
      "L3-L4_left": {"grade": "0-3", "confidence": "..."},
      "L3-L4_right": {"grade": "0-3", "confidence": "..."},
      ...
    },
    "facet_joints": {
      "L4-L5_left": {"arthropathy": "...", "effusion": "...", "confidence": "..."},
      ...
    },
    "endplates": {
      "L4_inferior": {"modic_type": "0/1/2/3", "confidence": "...", "slices": "..."},
      "L5_superior": {"modic_type": "0/1/2/3", "confidence": "...", "slices": "..."},
      ...
    },
    "conus": "position and signal",
    "posterior_elements": "...",
    "paraspinal": "...",
    "post_surgical": "... (if applicable)",
    "incidental_findings": ["..."]
  },

  "impression": [
    "1. Most critical finding with grade and confidence",
    "2. Second most critical finding",
    "3. ...",
    "4. Additional findings"
  ],

  "recommendations": [
    "Clinical correlation / follow-up recommendations if any"
  ]
}
"""
```

**Note:** Similar master prompts would be created for each anatomy type:
- `backend/prompts/brain_master.py` — Fazekas grading, tumor classification, vascular territories
- `backend/prompts/msk_master.py` — Cartilage grading, ligament assessment, effusion grading
- `backend/prompts/cardiac_master.py` — Segment analysis, wall motion, tissue characterization
- `backend/prompts/breast_master.py` — BI-RADS lexicon, enhancement kinetics, morphology descriptors
- `backend/prompts/prostate_master.py` — PI-RADS v2.1, zone-specific criteria, lesion scoring
- `backend/prompts/chest_master.py` — Lung-RADS adaptation, mediastinal assessment
- `backend/prompts/abdomen_master.py` — LI-RADS for liver, organ-by-organ systematic search
- `backend/prompts/vascular_master.py` — Stenosis grading (NASCET/ECST), aneurysm classification
- `backend/prompts/head_neck_master.py` — TNM-relevant anatomy, cranial nerve assessment

Each prompt follows the same structure:
1. Mandatory systematic search checklist
2. Grading criteria tables
3. Normal measurement references
4. Sequence interpretation guide
5. Confidence rules
6. ACR-standard output format

---

### Module 5: VerificationPass

**Problem:** Claude sometimes overcalls findings, hallucinates confidence,
or misses anatomy it didn't systematically check.

**Solution:** A second Claude call where it acts as a senior attending
reviewing a resident's report. Cheap (~$0.02) and catches the exact
errors that drop accuracy.

#### File: `backend/services/verification.py`

```python
"""
VerificationPass — Senior Attending Review of Claude's initial report.

This is the self-check mechanism. After Claude produces its initial
analysis, this module sends the report back with a review prompt that
catches:
  1. Contradictions between findings
  2. Overcalled confidence (stated "DEFINITE" without multi-sequence evidence)
  3. Anatomy that wasn't systematically examined
  4. Measurements outside physiological range
  5. Missing standard components (impression, recommendations)
"""

VERIFICATION_PROMPT = """
You are a senior attending neuroradiologist reviewing a radiology report
produced by a resident. Your job is QUALITY CONTROL — find and fix errors.

THE ORIGINAL REPORT IS BELOW. Review it against these quality criteria:

═══════════════════════════════════════════════════
QUALITY CONTROL CHECKLIST
═══════════════════════════════════════════════════

□ COMPLETENESS CHECK
  - Was every anatomical structure in the systematic search examined?
  - If a structure was NOT mentioned, it was likely NOT checked — flag this.
  - Are there levels that were skipped? (e.g., L1-L2 and L2-L3 missing?)

□ CONFIDENCE VALIDATION
  - Does every "DEFINITE" finding cite multiple slices AND multiple sequences?
  - Does every "PROBABLE" finding cite at least multiple slices OR sequences?
  - Are any "DEFINITE" findings that only reference a single slice?
    → Downgrade to "PROBABLE" or "POSSIBLE"

□ CONTRADICTION CHECK
  - Are there findings that contradict each other?
    e.g., "normal disc height" but "Grade IV desiccation" (Grade IV = reduced height)
  - Are measurements internally consistent?

□ PHYSIOLOGICAL RANGE CHECK
  - Disc heights should be 2-16mm (anything outside = likely error)
  - Canal AP diameter should be 5-25mm
  - Vertebral body height should be 15-35mm
  - If a measurement seems impossible, flag it

□ IMPRESSION QUALITY
  - Does the impression list findings in order of clinical significance?
  - Are actionable findings clearly highlighted?
  - Does it include confidence levels?

□ NORMAL VARIANT AWARENESS
  - Could any reported "findings" actually be normal variants?
    e.g., disc bulge at L5-S1 is extremely common and often incidental
    e.g., mild facet arthropathy is near-universal over age 40
  - Flag findings that might be overcalled

═══════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════

1. Review the report below
2. List any issues found (contradictions, overcalls, gaps, impossible values)
3. Produce a CORRECTED version of the JSON report with:
   - Confidence levels adjusted where evidence is insufficient
   - Missing structures added as "not well visualized" if they weren't checked
   - Contradictions resolved
   - Impossible measurements flagged as "measurement uncertain"
4. Add a "quality_notes" field listing what you corrected

ORIGINAL REPORT:
"""
```

---

### Module 6: Precision Annotation Engine

**Problem:** Current annotations use approximate pixel positions. Need
pixel-perfect overlays using real DICOM spatial calibration.

**Solution:** New annotation engine that:
  - Uses PixelSpacing for real mm measurements
  - Draws measurement lines with mm labels
  - Creates comparison panels (T1 vs T2)
  - Highlights findings with semi-transparent overlays
  - Follows radiology annotation standards

#### File: `backend/core/annotation_engine.py`

```python
"""
AnnotationEngine — Pixel-accurate radiology annotations.

Uses DICOM PixelSpacing for real millimeter measurements.
Creates annotated proof images following radiology standards:
  - Measurement lines with mm values
  - Finding arrows with labels
  - Region highlights (semi-transparent)
  - Cross-sequence comparison panels
"""

import io
import base64
import logging
from typing import List, Tuple, Optional, Dict

import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("mika.annotations")


@dataclass
class Finding:
    """A single finding to annotate."""
    label: str              # e.g., "L4-L5 disc protrusion"
    slice_idx: int          # Which slice this finding is on
    region: Tuple[int, int, int, int]  # (x1, y1, x2, y2) pixel bounding box
    confidence: str         # DEFINITE, PROBABLE, POSSIBLE
    measurement_mm: Optional[float] = None  # Size in mm if measured


class AnnotationEngine:
    """Create pixel-accurate annotated proof images."""

    # Color scheme for confidence levels
    COLORS = {
        "DEFINITE": (255, 80, 80),     # Red — definite findings
        "PROBABLE": (255, 180, 50),    # Orange — probable
        "POSSIBLE": (100, 180, 255),   # Blue — possible
        "NORMAL": (80, 200, 80),       # Green — normal reference
    }

    def annotate_slice(
        self,
        pixel_array: np.ndarray,
        findings: List[Finding],
        pixel_spacing: Tuple[float, float],
        title: str = "",
    ) -> Image.Image:
        """
        Create annotated image with findings overlay.

        Args:
            pixel_array: raw MRI slice data
            findings: list of Finding objects to annotate
            pixel_spacing: (row_mm, col_mm) from DICOM
            title: image title (e.g., "Sagittal T2 — L4-L5 level")

        Returns:
            Annotated PIL Image in RGB.
        """
        # Normalize to uint8 grayscale
        norm = self._normalize(pixel_array)

        # Convert to RGB for colored annotations
        img = Image.fromarray(norm, mode="L").convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

        # Add title bar
        if title:
            draw.rectangle([(0, 0), (img.width, 22)], fill=(0, 0, 0, 180))
            draw.text((6, 4), title, fill=(255, 255, 255))

        # Draw each finding
        for finding in findings:
            color = self.COLORS.get(finding.confidence, self.COLORS["POSSIBLE"])
            x1, y1, x2, y2 = finding.region

            # Semi-transparent highlight
            overlay_color = color + (40,)  # 40/255 alpha
            draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=2,
                           fill=overlay_color)

            # Label with confidence
            label = f"{finding.label} [{finding.confidence}]"
            if finding.measurement_mm is not None:
                label += f" {finding.measurement_mm:.1f}mm"
            draw.text((x1, y1 - 14), label, fill=color)

            # Measurement line if applicable
            if finding.measurement_mm is not None:
                mid_y = (y1 + y2) // 2
                draw.line([(x1, mid_y), (x2, mid_y)], fill=color, width=2)
                # Tick marks
                draw.line([(x1, mid_y - 4), (x1, mid_y + 4)], fill=color, width=2)
                draw.line([(x2, mid_y - 4), (x2, mid_y + 4)], fill=color, width=2)

        # Scale bar (bottom right)
        self._add_scale_bar(draw, img.size, pixel_spacing)

        return img

    def create_comparison_panel(
        self,
        images: Dict[str, np.ndarray],
        labels: Dict[str, str],
        findings: Optional[List[Finding]] = None,
    ) -> Image.Image:
        """Create side-by-side sequence comparison panel."""
        panels = []
        cell_w, cell_h = 300, 300

        for seq_name, arr in images.items():
            norm = self._normalize(arr)
            panel = Image.fromarray(norm, mode="L").convert("RGB")
            panel = panel.resize((cell_w, cell_h), Image.LANCZOS)
            draw = ImageDraw.Draw(panel)
            label = labels.get(seq_name, seq_name)
            draw.rectangle([(0, 0), (cell_w, 20)], fill=(0, 0, 0, 200))
            draw.text((6, 3), label, fill=(255, 255, 255))
            panels.append(panel)

        if not panels:
            return Image.new("RGB", (cell_w, cell_h), (0, 0, 0))

        # Stitch horizontally
        total_w = cell_w * len(panels)
        combined = Image.new("RGB", (total_w, cell_h), (0, 0, 0))
        for i, panel in enumerate(panels):
            combined.paste(panel, (i * cell_w, 0))

        return combined

    def _add_scale_bar(
        self,
        draw: ImageDraw.Draw,
        img_size: Tuple[int, int],
        pixel_spacing: Tuple[float, float],
    ):
        """Add a calibration scale bar to the image."""
        row_mm, col_mm = pixel_spacing
        if col_mm <= 0:
            return

        # 10mm scale bar
        bar_px = int(10.0 / col_mm)
        if bar_px < 10 or bar_px > img_size[0] // 2:
            return

        x_start = img_size[0] - bar_px - 10
        y = img_size[1] - 15

        draw.line([(x_start, y), (x_start + bar_px, y)], fill=(255, 255, 255), width=2)
        draw.line([(x_start, y - 3), (x_start, y + 3)], fill=(255, 255, 255), width=2)
        draw.line([(x_start + bar_px, y - 3), (x_start + bar_px, y + 3)],
                  fill=(255, 255, 255), width=2)
        draw.text((x_start + bar_px // 2 - 10, y - 14), "10mm",
                  fill=(255, 255, 255))

    @staticmethod
    def _normalize(arr: np.ndarray) -> np.ndarray:
        """Normalize array to uint8."""
        arr = arr.astype(np.float64)
        mn, mx = arr.min(), arr.max()
        if mx - mn < 1e-8:
            return np.zeros(arr.shape, dtype=np.uint8)
        return ((arr - mn) / (mx - mn) * 255).astype(np.uint8)
```

---

## PHASE 2: ENHANCED VISION PROCESSING (Week 2-3)
### Target: Push from 75% to 85%

Phase 2 is already implemented in Module 2 (VisionEnhancer) above.
The integration flow:

1. StudyOrganizer produces organized sequences
2. VisionEnhancer generates enhanced views:
   - `multi_window()` — 3 windowed views per key slice
   - `cross_sequence_panel()` — T1|T2|STIR side-by-side at matching levels
   - `difference_map()` — T2-T1 subtraction highlighting pathology
   - `symmetry_map()` — Brain L/R asymmetry detection
   - `outlier_map()` — Z-score heatmap of unusual signal
   - `edge_enhanced()` — Structural boundary emphasis
3. BatchSender includes enhanced views alongside standard slices
4. Claude receives both raw images AND enhanced views with labels

The key insight: these enhancements make SUBTLE findings VISIBLE.
Claude's limitation is perception, not reasoning. If we make subtle
signal changes visually obvious, Claude can reason about them.

---

## PHASE 3: VALIDATION FRAMEWORK (Week 3-4)
### Target: Measure actual accuracy, iterate to 85-90%

#### File: `backend/validation/validator.py`

```python
"""
Validation Framework — Measure MIKA accuracy against ground truth.

Supported datasets:
  - SPIDER (spine): 447 studies with expert vertebra + disc annotations
  - BraTS (brain): 2000+ studies with tumor segmentation masks
  - fastMRI (knee): 1500+ studies with radiologist reports
  - TCIA collections: Various anatomy types with pathology-confirmed diagnoses

Metrics:
  - Per-finding sensitivity (did we detect it?)
  - Per-finding specificity (did we avoid false positives?)
  - Per-finding accuracy (overall correctness)
  - Confidence calibration (when we say DEFINITE, are we right?)
  - Report completeness (did we check everything?)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from pathlib import Path

import numpy as np

logger = logging.getLogger("mika.validation")


@dataclass
class GroundTruth:
    """Ground truth findings for a single study."""
    study_id: str
    anatomy_type: str
    findings: List[Dict]  # Each: {"type": "disc_herniation", "level": "L4-L5", ...}
    grades: Dict[str, str]  # e.g., {"L4-L5_pfirrmann": "IV", ...}
    measurements: Dict[str, float]  # e.g., {"L4-L5_herniation_mm": 5.2}


@dataclass
class ValidationResult:
    """Results from validating one study."""
    study_id: str
    true_positives: List[str]   # Findings correctly detected
    false_negatives: List[str]  # Findings missed
    false_positives: List[str]  # Findings hallucinated
    grade_matches: int          # Grading matches
    grade_mismatches: int       # Grading errors
    confidence_calibration: Dict[str, Dict]  # Per confidence level


@dataclass
class ValidationSummary:
    """Aggregate validation metrics across all studies."""
    total_studies: int = 0
    sensitivity: float = 0.0        # True positive rate
    specificity: float = 0.0        # True negative rate
    accuracy: float = 0.0           # Overall accuracy
    grade_accuracy: float = 0.0     # Grading system accuracy
    confidence_calibration: Dict[str, float] = field(default_factory=dict)
    per_finding_type: Dict[str, Dict] = field(default_factory=dict)
    failure_modes: List[str] = field(default_factory=list)


class Validator:
    """
    Validate MIKA reports against ground truth datasets.

    Usage:
        validator = Validator()
        validator.load_ground_truth("spine", "path/to/spider_annotations.json")
        results = validator.validate_batch(mika_reports)
        summary = validator.summarize(results)
        print(summary.accuracy)  # e.g., 0.87
    """

    def __init__(self):
        self.ground_truths: Dict[str, GroundTruth] = {}

    def load_ground_truth(self, dataset_name: str, annotations_path: str):
        """Load ground truth annotations from a dataset."""
        path = Path(annotations_path)
        if not path.exists():
            raise FileNotFoundError(f"Annotations not found: {path}")

        with open(path) as f:
            data = json.load(f)

        for study in data.get("studies", []):
            gt = GroundTruth(
                study_id=study["id"],
                anatomy_type=study.get("anatomy", "unknown"),
                findings=study.get("findings", []),
                grades=study.get("grades", {}),
                measurements=study.get("measurements", {}),
            )
            self.ground_truths[gt.study_id] = gt

        logger.info("Loaded %d ground truth studies from %s",
                     len(data.get("studies", [])), dataset_name)

    def validate_single(
        self, study_id: str, mika_report: Dict
    ) -> Optional[ValidationResult]:
        """Validate a single MIKA report against ground truth."""
        gt = self.ground_truths.get(study_id)
        if not gt:
            logger.warning("No ground truth for study %s", study_id)
            return None

        result = ValidationResult(
            study_id=study_id,
            true_positives=[],
            false_negatives=[],
            false_positives=[],
            grade_matches=0,
            grade_mismatches=0,
            confidence_calibration={},
        )

        # Extract MIKA findings
        mika_findings = self._extract_findings(mika_report)

        # Compare findings
        gt_finding_set = {self._finding_key(f) for f in gt.findings}
        mika_finding_set = {self._finding_key(f) for f in mika_findings}

        for f_key in gt_finding_set:
            if f_key in mika_finding_set:
                result.true_positives.append(f_key)
            else:
                result.false_negatives.append(f_key)

        for f_key in mika_finding_set:
            if f_key not in gt_finding_set:
                result.false_positives.append(f_key)

        # Compare grades
        for grade_key, gt_grade in gt.grades.items():
            mika_grade = self._extract_grade(mika_report, grade_key)
            if mika_grade and mika_grade.upper() == gt_grade.upper():
                result.grade_matches += 1
            else:
                result.grade_mismatches += 1

        return result

    def summarize(self, results: List[ValidationResult]) -> ValidationSummary:
        """Compute aggregate metrics from validation results."""
        summary = ValidationSummary(total_studies=len(results))

        total_tp = sum(len(r.true_positives) for r in results)
        total_fn = sum(len(r.false_negatives) for r in results)
        total_fp = sum(len(r.false_positives) for r in results)
        total_grade_match = sum(r.grade_matches for r in results)
        total_grade_mismatch = sum(r.grade_mismatches for r in results)

        # Sensitivity = TP / (TP + FN)
        if total_tp + total_fn > 0:
            summary.sensitivity = total_tp / (total_tp + total_fn)

        # Precision = TP / (TP + FP) — proxy for specificity in this context
        if total_tp + total_fp > 0:
            specificity_proxy = total_tp / (total_tp + total_fp)
            summary.specificity = specificity_proxy

        # Overall accuracy
        total_decisions = total_tp + total_fn + total_fp
        if total_decisions > 0:
            summary.accuracy = total_tp / total_decisions

        # Grade accuracy
        total_grades = total_grade_match + total_grade_mismatch
        if total_grades > 0:
            summary.grade_accuracy = total_grade_match / total_grades

        # Identify failure modes (most common false negatives)
        fn_counts: Dict[str, int] = {}
        for r in results:
            for fn in r.false_negatives:
                finding_type = fn.split("::")[0] if "::" in fn else fn
                fn_counts[finding_type] = fn_counts.get(finding_type, 0) + 1

        summary.failure_modes = sorted(fn_counts.keys(),
                                         key=lambda k: fn_counts[k], reverse=True)

        return summary

    @staticmethod
    def _finding_key(finding: Dict) -> str:
        """Create comparable key from a finding dict."""
        f_type = finding.get("type", "unknown")
        level = finding.get("level", finding.get("location", ""))
        return f"{f_type}::{level}"

    @staticmethod
    def _extract_findings(report: Dict) -> List[Dict]:
        """Extract comparable findings list from MIKA report."""
        findings = []
        report_findings = report.get("findings", {})

        # Extract disc findings
        for level, data in report_findings.get("discs", {}).items():
            if isinstance(data, dict):
                contour = data.get("contour", "").lower()
                if contour and contour not in ("normal", "none", ""):
                    findings.append({"type": f"disc_{contour}", "level": level})

        # Extract stenosis findings
        for level, data in report_findings.get("spinal_canal", {}).items():
            if isinstance(data, dict):
                grade = data.get("stenosis_grade", "").lower()
                if grade and grade not in ("none", "normal", ""):
                    findings.append({"type": "stenosis", "level": level})

        # Extract endplate findings
        for level, data in report_findings.get("endplates", {}).items():
            if isinstance(data, dict):
                modic = data.get("modic_type", "0")
                if str(modic) != "0":
                    findings.append({"type": f"modic_{modic}", "level": level})

        return findings

    @staticmethod
    def _extract_grade(report: Dict, grade_key: str) -> Optional[str]:
        """Extract a specific grade from MIKA report."""
        parts = grade_key.split("_", 1)
        if len(parts) < 2:
            return None
        level, grade_type = parts[0], parts[1]

        findings = report.get("findings", {})

        if "pfirrmann" in grade_type:
            disc_data = findings.get("discs", {}).get(level, {})
            return disc_data.get("pfirrmann") if isinstance(disc_data, dict) else None

        if "modic" in grade_type:
            ep_data = findings.get("endplates", {}).get(level, {})
            return str(ep_data.get("modic_type", "")) if isinstance(ep_data, dict) else None

        return None
```

#### Validation Execution Script: `backend/validation/run_validation.py`

```python
"""
Run validation against ground truth datasets.

Usage:
    python -m validation.run_validation --dataset spider --data-dir ./test_data/spider
"""

import argparse
import json
import logging
from pathlib import Path

from validation.validator import Validator, ValidationSummary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mika.validate")


def print_report(summary: ValidationSummary):
    """Print formatted validation report."""
    print("\n" + "=" * 60)
    print("  MIKA VALIDATION REPORT")
    print("=" * 60)
    print(f"  Studies validated:    {summary.total_studies}")
    print(f"  Finding sensitivity:  {summary.sensitivity:.1%}")
    print(f"  Finding specificity:  {summary.specificity:.1%}")
    print(f"  Overall accuracy:     {summary.accuracy:.1%}")
    print(f"  Grading accuracy:     {summary.grade_accuracy:.1%}")
    print()

    if summary.failure_modes:
        print("  TOP FAILURE MODES (most commonly missed):")
        for i, mode in enumerate(summary.failure_modes[:10], 1):
            print(f"    {i}. {mode}")

    print()
    if summary.accuracy >= 0.90:
        print("  ✅ TARGET MET: 90%+ accuracy achieved")
    else:
        gap = 0.90 - summary.accuracy
        print(f"  ❌ GAP TO TARGET: {gap:.1%} improvement needed")
        print(f"     Focus on top failure modes above for targeted improvement")

    print("=" * 60)
```

---

## PHASE 4: FINAL OPTIMIZATION (Week 4-5)
### Target: Close remaining gap to 90%

### Strategy: Iterate based on validation data

After Phase 3 validation reveals specific failure modes:

**Step 1: Targeted Prompt Additions**

For each failure mode, add explicit guidance to the master prompt.
Example: if Modic changes are being missed:

```
Add to SPINE_MASTER_PROMPT:

ATTENTION — MODIC CHANGES FREQUENTLY MISSED:
On EVERY sagittal slice, STOP and examine the vertebral endplates.
Look at the bone immediately above and below each disc. Compare:
  - T1 signal of endplate vs. mid-vertebral body signal
  - T2/STIR signal of endplate vs. mid-vertebral body signal
If the endplate signal differs from the vertebral body center,
classify the Modic type. This is a commonly missed finding —
examine EVERY endplate at EVERY level even if everything else looks normal.
```

**Step 2: Enhanced Preprocessing for Specific Findings**

For findings that resist prompt optimization, add targeted preprocessing:

```python
# Example: Endplate-focused crops for Modic change detection
def create_endplate_crops(pixel_array, level_map, pixel_spacing):
    """Generate zoomed, contrast-enhanced endplate crops."""
    crops = []
    for level, row_px in level_map.items():
        # Crop 30mm above and below the disc level
        mm_range = 30
        px_range = int(mm_range / pixel_spacing[0])
        y1 = max(0, row_px - px_range)
        y2 = min(pixel_array.shape[0], row_px + px_range)

        crop = pixel_array[y1:y2, :]

        # Extreme windowing for endplate visibility
        p10, p90 = np.percentile(crop, [10, 90])
        enhanced = np.clip(crop, p10, p90)
        enhanced = ((enhanced - p10) / (p90 - p10) * 255).astype(np.uint8)

        crops.append((level, enhanced))
    return crops
```

**Step 3: Scope Gating for Unreliable Findings**

If specific finding types cannot reach 85%+ after optimization:

```json
{
  "finding_type": "facet_arthropathy",
  "validated_accuracy": 0.72,
  "status": "LIMITED",
  "report_language": "Facet joint assessment: Limited confidence on current
    imaging protocol. Clinical and CT correlation recommended for definitive
    facet joint evaluation."
}
```

Honest limitation > false confidence. Radiologists respect systems that
know what they don't know.

---

## ANATOMY ROLLOUT SCHEDULE

Do NOT launch all 10 simultaneously. Each anatomy type goes through
its own validation cycle:

| Order | Anatomy | Validation Dataset | Target | Timeline |
|-------|---------|-------------------|--------|----------|
| 1 | Spine | SPIDER (447 studies) | 90% | Week 3-4 |
| 2 | Brain | BraTS (2000+ studies) | 90% | Week 4-5 |
| 3 | MSK (Knee) | fastMRI (1500+ studies) | 88% | Week 5-6 |
| 4 | Prostate | TCIA ProstateX | 85% | Week 6-7 |
| 5 | Breast | Duke Breast MRI | 85% | Week 7-8 |
| 6-10 | Remaining | TCIA collections | 85% | Week 8-12 |

Each anatomy type must achieve its target accuracy before going live.
No exceptions.

---

## COST ESTIMATE

| Item | Cost |
|------|------|
| Claude API (development + testing) | ~$150-250 |
| Claude API (validation runs, ~500 studies) | ~$100-200 |
| Ground truth datasets | Free (open access) |
| GPU rental (only if Phase 4 needs detection model) | ~$50-100 |
| **Total** | **$300-550** |

---

## FILE STRUCTURE — NEW vs. MODIFIED

### New Files
```
backend/
├── core/
│   ├── study_organizer.py          # Module 1: Organize DICOM study
│   ├── vision_enhancer.py          # Module 2: Enhanced image processing
│   ├── annotation_engine.py        # Module 6: Pixel-accurate annotations
│   └── report_generator.py         # Module 7: ACR-standard reports
├── services/
│   ├── batch_sender.py             # Module 3: Send all images to Claude
│   └── verification.py             # Module 5: Self-review pass
├── prompts/
│   ├── __init__.py
│   ├── spine_master.py             # Module 4: Spine master prompt
│   ├── brain_master.py             # Brain master prompt
│   ├── msk_master.py               # MSK master prompt
│   ├── cardiac_master.py           # Cardiac master prompt
│   ├── breast_master.py            # Breast master prompt
│   ├── prostate_master.py          # Prostate master prompt
│   ├── chest_master.py             # Chest master prompt
│   ├── abdomen_master.py           # Abdomen master prompt
│   ├── vascular_master.py          # Vascular master prompt
│   └── head_neck_master.py         # Head & neck master prompt
└── validation/
    ├── __init__.py
    ├── validator.py                # Module 8: Ground truth comparison
    └── run_validation.py           # Validation execution script
```

### Modified Files
```
backend/
├── app.py                          # Rewire pipeline to use new modules
├── core/
│   └── dicom_engine.py             # Keep measurement code, remove old slice selection
└── services/
    └── claude_interpreter.py       # Replace with BatchSender + MasterPrompts
```

### Preserved (No Changes)
```
backend/
├── core/
│   └── format_converter.py         # Multi-format input still needed
frontend/
└── index.html                      # UI already supports all anatomy types
docs/
└── FINETUNING_AND_TRAINING_PLAN.md # Future enhancement path
```

---

## SUCCESS CRITERIA

Before presenting to radiologists, ALL must be true:

- [ ] Spine accuracy ≥ 90% validated against SPIDER dataset
- [ ] Brain accuracy ≥ 90% validated against BraTS dataset
- [ ] Every finding includes slice references and confidence level
- [ ] No hallucinated measurements (flagged or omitted when uncertain)
- [ ] Reports follow ACR structured reporting format
- [ ] Annotations are pixel-accurate with mm scale bars
- [ ] System explicitly states limitations for low-confidence findings
- [ ] Validation report with metrics available for review

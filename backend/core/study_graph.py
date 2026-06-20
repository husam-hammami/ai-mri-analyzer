"""
StudyGraph: DICOM/image-export metadata inventory for evidence modules.

This layer is deterministic and non-diagnostic. It records geometry and source
metadata that downstream anatomy modules can use to localize candidate evidence.
Raw file paths are retained only in memory and are intentionally omitted from
serialized contracts.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import pydicom
except Exception:  # pragma: no cover - dependency may be absent in stripped envs
    pydicom = None

logger = logging.getLogger("mika.study_graph")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
DICOM_EXTS = {"", ".dcm", ".ima", ".dicom"}
LOCALIZER_TERMS = ("localizer", "locator", "scout", "survey", "topogram", "3-plane", "three plane")
CONTRAST_TERMS = ("post", "cont", "contrast", "gad", "gadolinium", "gd", "+c", "ce")


def safe_slug(text: str, fallback: str = "item", max_len: int = 48) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "").strip("_").lower()
    return (cleaned[:max_len] or fallback)


def hashed_id(text: str, prefix: str) -> str:
    digest = hashlib.sha1((text or prefix).encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def is_localizer_name(name: str) -> bool:
    low = (name or "").lower()
    return any(term in low for term in LOCALIZER_TERMS)


def _float_tuple(value, length: int) -> Optional[tuple[float, ...]]:
    if value is None:
        return None
    try:
        vals = [float(x) for x in value]
        if len(vals) < length:
            return None
        return tuple(vals[:length])
    except Exception:
        return None


def _safe_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def detect_plane_from_orientation(orientation, fallback_text: str = "") -> str:
    """Classify plane from DICOM ImageOrientationPatient, with text fallback."""
    try:
        vals = [float(x) for x in orientation]
        if len(vals) >= 6:
            row = np.array(vals[:3], dtype=float)
            col = np.array(vals[3:6], dtype=float)
            normal = np.abs(np.cross(row, col))
            if float(np.linalg.norm(normal)) > 0:
                axis = int(np.argmax(normal))
                return ("sagittal", "coronal", "axial")[axis]
    except Exception:
        pass

    text = (fallback_text or "").lower()
    if "sag" in text:
        return "sagittal"
    if any(k in text for k in ("ax", "tra", "transverse")):
        return "axial"
    if "cor" in text:
        return "coronal"
    return "unknown"


def classify_sequence(text: str, modality: str = "") -> str:
    low = (text or "").lower()
    mod = (modality or "").upper()
    if mod in {"DX", "CR", "RF", "XA", "MG"}:
        return "radiograph"
    if "stir" in low or "tirm" in low:
        return "stir"
    if "flair" in low:
        return "flair"
    if "adc" in low:
        return "adc"
    if "dwi" in low or "diff" in low:
        return "dwi"
    if "t2" in low:
        return "t2"
    if "t1" in low or "vibe" in low or "mprage" in low:
        return "t1"
    if "pd" in low:
        return "pd"
    return "unknown"


def classify_contrast_phase(text: str, sequence: str = "", contrast_agent: str = "") -> str:
    low = (text or "").lower()
    agent = (contrast_agent or "").strip()
    if any(term in low for term in ("pre", "noncontrast", "non_contrast", "without")):
        return "pre_contrast"
    if agent or any(term in low for term in CONTRAST_TERMS):
        return "post_contrast"
    if sequence == "t1":
        return "pre_contrast"
    return "unknown"


def normalize_contrast_pair_key(description: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (description or "").lower()).strip("_")
    for term in ("post", "pre", "cont", "contrast", "gad", "gadolinium", "gd", "ce"):
        key = re.sub(rf"(^|_){term}(_|$)", "_", key)
    return re.sub(r"_{2,}", "_", key).strip("_")


@dataclass
class StudySlice:
    slice_id: str
    series_id: str
    modality: str
    plane: str
    sequence: str
    contrast_phase: str
    source_type: str
    instance_number: Optional[int] = None
    pixel_spacing: Optional[tuple[float, float]] = None
    image_position_patient: Optional[tuple[float, float, float]] = None
    image_orientation_patient: Optional[tuple[float, float, float, float, float, float]] = None
    slice_location: Optional[float] = None
    rows: Optional[int] = None
    columns: Optional[int] = None
    acquisition_time: str = ""
    content_time: str = ""
    series_time: str = ""
    contrast_bolus_agent: str = ""
    contrast_bolus_start_time: str = ""
    path: Optional[Path] = field(default=None, repr=False, compare=False)

    @property
    def calibrated(self) -> bool:
        return bool(self.pixel_spacing and self.source_type == "dicom")

    @property
    def superior_inferior_position(self) -> Optional[float]:
        if self.image_position_patient is not None:
            return self.image_position_patient[2]
        return self.slice_location

    @property
    def evidence_ref(self) -> str:
        return f"{self.series_id}:{self.slice_id}"

    def to_dict(self) -> dict:
        return {
            "slice_id": self.slice_id,
            "series_id": self.series_id,
            "modality": self.modality,
            "plane": self.plane,
            "sequence": self.sequence,
            "contrast_phase": self.contrast_phase,
            "source_type": self.source_type,
            "instance_number": self.instance_number,
            "pixel_spacing": list(self.pixel_spacing) if self.pixel_spacing else None,
            "image_position_patient": list(self.image_position_patient) if self.image_position_patient else None,
            "image_orientation_patient": list(self.image_orientation_patient) if self.image_orientation_patient else None,
            "slice_location": self.slice_location,
            "rows": self.rows,
            "columns": self.columns,
            "acquisition_time": self.acquisition_time,
            "content_time": self.content_time,
            "series_time": self.series_time,
            "contrast_bolus_agent_present": bool(self.contrast_bolus_agent),
            "contrast_bolus_start_time": self.contrast_bolus_start_time,
            "calibrated": self.calibrated,
        }


@dataclass
class StudySeries:
    series_id: str
    description: str
    modality: str
    plane: str
    sequence: str
    contrast_phase: str
    source_type: str
    slices: list[StudySlice] = field(default_factory=list)
    protocol_name: str = ""
    series_number: Optional[int] = None
    series_uid_hash: str = ""
    pixel_spacing: Optional[tuple[float, float]] = None
    image_orientation_patient: Optional[tuple[float, float, float, float, float, float]] = None
    rows: Optional[int] = None
    columns: Optional[int] = None
    slice_thickness: Optional[float] = None
    acquisition_time: str = ""
    series_time: str = ""
    contrast_bolus_agent: str = ""
    contrast_bolus_start_time: str = ""
    is_localizer: bool = False

    @property
    def calibrated(self) -> bool:
        return bool(self.pixel_spacing and self.source_type == "dicom")

    @property
    def slice_count(self) -> int:
        return len(self.slices)

    def sorted_slices(self) -> list[StudySlice]:
        def key(sl: StudySlice):
            pos = sl.superior_inferior_position
            if pos is None:
                return (1, sl.instance_number or 0, sl.slice_id)
            # Superior to inferior for lumbar level binning.
            return (0, -float(pos), sl.instance_number or 0, sl.slice_id)

        return sorted(self.slices, key=key)

    def to_dict(self) -> dict:
        return {
            "series_id": self.series_id,
            "description": self.description,
            "protocol_name": self.protocol_name,
            "series_number": self.series_number,
            "series_uid_hash": self.series_uid_hash,
            "modality": self.modality,
            "plane": self.plane,
            "sequence": self.sequence,
            "contrast_phase": self.contrast_phase,
            "source_type": self.source_type,
            "slice_count": self.slice_count,
            "pixel_spacing": list(self.pixel_spacing) if self.pixel_spacing else None,
            "image_orientation_patient": list(self.image_orientation_patient) if self.image_orientation_patient else None,
            "rows": self.rows,
            "columns": self.columns,
            "slice_thickness": self.slice_thickness,
            "acquisition_time": self.acquisition_time,
            "series_time": self.series_time,
            "contrast_bolus_agent_present": bool(self.contrast_bolus_agent),
            "contrast_bolus_start_time": self.contrast_bolus_start_time,
            "calibrated": self.calibrated,
            "is_localizer": self.is_localizer,
            "slices": [sl.to_dict() for sl in self.slices],
        }


@dataclass
class StudyGraph:
    study_id: str
    source_type: str
    modality: str
    series: list[StudySeries] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    graph_version: int = 1

    @property
    def calibrated(self) -> bool:
        diagnostic = [s for s in self.series if not s.is_localizer]
        return bool(diagnostic) and all(s.calibrated for s in diagnostic)

    @property
    def image_count(self) -> int:
        return sum(s.slice_count for s in self.series)

    def series_by_id(self) -> dict[str, StudySeries]:
        return {s.series_id: s for s in self.series}

    def to_dict(self) -> dict:
        return {
            "graph_version": self.graph_version,
            "study_id": self.study_id,
            "source_type": self.source_type,
            "modality": self.modality,
            "calibrated": self.calibrated,
            "series_count": len(self.series),
            "image_count": self.image_count,
            "limitations": self.limitations,
            "series": [s.to_dict() for s in self.series],
        }


class StudyGraphBuilder:
    def __init__(self, study_dir: str | Path):
        self.study_dir = Path(study_dir)

    def build(self) -> StudyGraph:
        dcm_files = self._dicom_files()
        if dcm_files:
            return self._build_dicom_graph(dcm_files)
        return self._build_image_export_graph(self._image_files())

    def _dicom_files(self) -> list[Path]:
        if pydicom is None:
            return []
        files: list[Path] = []
        for path in sorted(self.study_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in DICOM_EXTS:
                continue
            try:
                pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
                files.append(path)
            except Exception:
                continue
        return files

    def _image_files(self) -> list[Path]:
        return [p for p in sorted(self.study_dir.rglob("*")) if p.is_file() and p.suffix.lower() in IMAGE_EXTS]

    def _build_dicom_graph(self, files: list[Path]) -> StudyGraph:
        grouped: dict[str, list[tuple[Path, object]]] = {}
        study_uid = ""
        for path in files:
            try:
                ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
            except Exception:
                continue
            study_uid = study_uid or str(getattr(ds, "StudyInstanceUID", "") or self.study_dir)
            key = str(getattr(ds, "SeriesInstanceUID", "") or getattr(ds, "SeriesDescription", "") or path.parent)
            grouped.setdefault(key, []).append((path, ds))

        series_out: list[StudySeries] = []
        sorted_groups = sorted(
            grouped.items(),
            key=lambda kv: (
                _safe_int(getattr(kv[1][0][1], "SeriesNumber", None)) or 9999,
                str(getattr(kv[1][0][1], "SeriesDescription", kv[0])),
            ),
        )
        for idx, (uid, entries) in enumerate(sorted_groups, start=1):
            first = entries[0][1]
            description = str(getattr(first, "SeriesDescription", "") or getattr(first, "ProtocolName", "") or f"Series {idx}")
            protocol = str(getattr(first, "ProtocolName", "") or "")
            text = f"{description} {protocol}"
            modality = str(getattr(first, "Modality", "") or "").upper()
            orientation = _float_tuple(getattr(first, "ImageOrientationPatient", None), 6)
            plane = detect_plane_from_orientation(orientation, text)
            sequence = classify_sequence(text, modality)
            contrast_agent = str(getattr(first, "ContrastBolusAgent", "") or "")
            contrast_phase = classify_contrast_phase(text, sequence, contrast_agent)
            spacing_vals = _float_tuple(getattr(first, "PixelSpacing", None), 2)
            spacing = (spacing_vals[0], spacing_vals[1]) if spacing_vals else None
            sid = f"s{idx:03d}_{safe_slug(description, 'series')}"
            series = StudySeries(
                series_id=sid,
                description=description,
                protocol_name=protocol,
                series_number=_safe_int(getattr(first, "SeriesNumber", None)),
                series_uid_hash=hashed_id(uid, "series"),
                modality=modality,
                plane=plane,
                sequence=sequence,
                contrast_phase=contrast_phase,
                source_type="dicom",
                pixel_spacing=spacing,
                image_orientation_patient=orientation,
                rows=_safe_int(getattr(first, "Rows", None)),
                columns=_safe_int(getattr(first, "Columns", None)),
                slice_thickness=_safe_float(getattr(first, "SliceThickness", None)),
                acquisition_time=str(getattr(first, "AcquisitionTime", "") or ""),
                series_time=str(getattr(first, "SeriesTime", "") or ""),
                contrast_bolus_agent=contrast_agent,
                contrast_bolus_start_time=str(getattr(first, "ContrastBolusStartTime", "") or ""),
                is_localizer=is_localizer_name(description),
            )
            ordered = sorted(entries, key=lambda item: (_safe_int(getattr(item[1], "InstanceNumber", None)) or 0, str(item[0])))
            for slice_idx, (path, ds) in enumerate(ordered, start=1):
                sl_text = f"{getattr(ds, 'SeriesDescription', description)} {getattr(ds, 'ProtocolName', protocol)}"
                sl_orientation = _float_tuple(getattr(ds, "ImageOrientationPatient", None), 6) or orientation
                sl_plane = detect_plane_from_orientation(sl_orientation, sl_text)
                sl_sequence = classify_sequence(sl_text, modality)
                sl_agent = str(getattr(ds, "ContrastBolusAgent", "") or contrast_agent or "")
                sl = StudySlice(
                    slice_id=f"{sid}_sl{slice_idx:03d}",
                    series_id=sid,
                    modality=modality,
                    plane=sl_plane,
                    sequence=sl_sequence,
                    contrast_phase=classify_contrast_phase(sl_text, sl_sequence, sl_agent),
                    source_type="dicom",
                    instance_number=_safe_int(getattr(ds, "InstanceNumber", None)),
                    pixel_spacing=(_float_tuple(getattr(ds, "PixelSpacing", None), 2) or spacing),
                    image_position_patient=_float_tuple(getattr(ds, "ImagePositionPatient", None), 3),
                    image_orientation_patient=sl_orientation,
                    slice_location=_safe_float(getattr(ds, "SliceLocation", None)),
                    rows=_safe_int(getattr(ds, "Rows", None)),
                    columns=_safe_int(getattr(ds, "Columns", None)),
                    acquisition_time=str(getattr(ds, "AcquisitionTime", "") or ""),
                    content_time=str(getattr(ds, "ContentTime", "") or ""),
                    series_time=str(getattr(ds, "SeriesTime", "") or ""),
                    contrast_bolus_agent=sl_agent,
                    contrast_bolus_start_time=str(getattr(ds, "ContrastBolusStartTime", "") or ""),
                    path=path,
                )
                series.slices.append(sl)
            series_out.append(series)

        modalities = sorted({s.modality for s in series_out if s.modality})
        limitations: list[str] = []
        if any(s.is_localizer for s in series_out):
            limitations.append("Localizer/scout series are present and should not be used for precise diagnostic localization.")
        if any(not s.calibrated for s in series_out if not s.is_localizer):
            limitations.append("One or more diagnostic DICOM series lack PixelSpacing; precise measurements are confidence-capped.")
        return StudyGraph(
            study_id=hashed_id(study_uid or str(self.study_dir), "study"),
            source_type="dicom",
            modality=modalities[0] if len(modalities) == 1 else ("/".join(modalities) if modalities else "unknown"),
            series=series_out,
            limitations=limitations,
        )

    def _build_image_export_graph(self, files: list[Path]) -> StudyGraph:
        series_groups: dict[str, list[Path]] = {}
        for path in files:
            key = path.parent.name if path.parent != self.study_dir else "image_export"
            series_groups.setdefault(key, []).append(path)

        series_out: list[StudySeries] = []
        for idx, (name, group) in enumerate(sorted(series_groups.items()), start=1):
            sid = f"s{idx:03d}_{safe_slug(name, 'images')}"
            series = StudySeries(
                series_id=sid,
                description=name,
                modality="OT",
                plane=detect_plane_from_orientation(None, name),
                sequence=classify_sequence(name, "OT"),
                contrast_phase="unknown",
                source_type="image_export",
                is_localizer=is_localizer_name(name),
            )
            for slice_idx, path in enumerate(sorted(group), start=1):
                series.slices.append(StudySlice(
                    slice_id=f"{sid}_sl{slice_idx:03d}",
                    series_id=sid,
                    modality="OT",
                    plane=series.plane,
                    sequence=series.sequence,
                    contrast_phase="unknown",
                    source_type="image_export",
                    instance_number=None,
                    path=path,
                ))
            series_out.append(series)

        limitations = ["Image-export study: no trustworthy DICOM geometry, orientation, or PixelSpacing metadata is available."]
        return StudyGraph(
            study_id=hashed_id(str(self.study_dir), "study"),
            source_type="image_export",
            modality="OT",
            series=series_out,
            limitations=limitations,
        )

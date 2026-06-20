"""
EvidencePack: PHI-safe study evidence manifest and representative image selection.

This module does not diagnose. It inventories the available study files, renders or
copies representative images into the job work directory, and writes a manifest that
the agent can cite by stable evidence IDs.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger("mika.evidence")

try:
    import pydicom
except Exception:  # pragma: no cover - exercised when dependency is absent
    pydicom = None


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
LOCALIZER_TERMS = ("localizer", "locator", "scout", "survey", "topogram", "3-plane", "three plane")
TARGET_MIN_IMAGES = 40
TARGET_MAX_IMAGES = 80
MAX_IMAGE_DIM = 1400


@dataclass
class EvidenceImage:
    evidence_id: str
    series_id: str
    image_index: int
    instance_number: Optional[int]
    relative_path: str
    plane: str = ""
    slice_location: Optional[float] = None
    is_localizer: bool = False
    source_ref: str = ""


@dataclass
class EvidenceSeries:
    series_id: str
    series_uid: str
    name: str
    modality: str
    plane: str
    sequence_label: str
    slice_count: int
    pixel_spacing: Optional[list[float]] = None
    slice_thickness: Optional[float] = None
    orientation_laterality_notes: str = ""
    is_localizer: bool = False
    representative_slice_paths: list[str] = field(default_factory=list)


@dataclass
class EvidencePack:
    manifest_version: int
    study: dict
    series: list[EvidenceSeries]
    selected_images: list[EvidenceImage]
    limitations: list[str]
    manifest_path: str

    def to_manifest(self) -> dict:
        return {
            "manifest_version": self.manifest_version,
            "study": self.study,
            "series": [asdict(s) for s in self.series],
            "selected_images": [asdict(i) for i in self.selected_images],
            "limitations": self.limitations,
        }


def safe_id(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "").strip("_")
    return (cleaned[:48] or fallback).lower()


def is_localizer_name(name: str) -> bool:
    low = (name or "").lower()
    return any(term in low for term in LOCALIZER_TERMS)


def _contrast_pair_key(name: str) -> Optional[str]:
    """Return a normalized key for axial pre/post contrast MR series that should be sampled together."""
    low = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    if not low:
        return None
    key = low
    key = re.sub(r"(^|_)(post|cont|contrast|gd|gad)(_|$)", "_", key)
    key = re.sub(r"_{2,}", "_", key).strip("_")
    return key or None


def detect_plane_from_orientation(orientation, fallback_text: str = "") -> str:
    text = (fallback_text or "").lower()
    if "sag" in text:
        return "sagittal"
    if any(k in text for k in ("ax", "tra", "transverse")):
        return "axial"
    if "cor" in text:
        return "coronal"
    try:
        vals = [float(x) for x in orientation]
        row = np.array(vals[:3])
        col = np.array(vals[3:])
        normal = np.abs(np.cross(row, col))
        axis = int(np.argmax(normal))
        return ("sagittal", "coronal", "axial")[axis]
    except Exception:
        return ""


def _window_array(ds) -> np.ndarray:
    arr = ds.pixel_array.astype("float32")
    if arr.ndim > 2:
        if arr.ndim == 3 and arr.shape[-1] in (3, 4):
            return arr[..., :3].astype("uint8")
        arr = arr[arr.shape[0] // 2]
    slope = float(getattr(ds, "RescaleSlope", 1) or 1)
    intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
    arr = arr * slope + intercept
    center = getattr(ds, "WindowCenter", None)
    width = getattr(ds, "WindowWidth", None)
    if isinstance(center, (list, tuple)):
        center = center[0]
    if isinstance(width, (list, tuple)):
        width = width[0]
    try:
        if center is not None and width is not None and float(width) > 0:
            c = float(center)
            w = float(width)
            lo, hi = c - w / 2, c + w / 2
        else:
            lo, hi = np.percentile(arr, 1), np.percentile(arr, 99)
    except Exception:
        lo, hi = np.percentile(arr, 1), np.percentile(arr, 99)
    arr = np.clip((arr - lo) / (hi - lo + 1e-6), 0, 1) * 255.0
    return arr.astype("uint8")


def _save_image_copy(src: Path, dest: Path) -> None:
    img = Image.open(src)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_IMAGE_DIM:
        ratio = MAX_IMAGE_DIM / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)


class EvidencePackBuilder:
    def __init__(
        self,
        study_dir: str | Path,
        work_dir: str | Path,
        target_min: int = TARGET_MIN_IMAGES,
        target_max: int = TARGET_MAX_IMAGES,
    ):
        self.study_dir = Path(study_dir)
        self.work_dir = Path(work_dir)
        self.target_min = target_min
        self.target_max = target_max
        self.out_dir = self.work_dir / "evidence"
        self.image_dir = self.out_dir / "images"

    def build(self) -> EvidencePack:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        dcm_files = self._dicom_files()
        image_files = self._image_files()
        if dcm_files:
            pack = self._build_dicom_pack(dcm_files)
        else:
            pack = self._build_image_export_pack(image_files)
        manifest_path = self.out_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(pack.to_manifest(), indent=2), encoding="utf-8")
        pack.manifest_path = str(manifest_path)
        logger.info(
            "EvidencePack built: input=%s images=%d selected=%d manifest=%s",
            pack.study.get("input_type"),
            pack.study.get("image_count"),
            len(pack.selected_images),
            manifest_path,
        )
        return pack

    def _dicom_files(self) -> list[Path]:
        if not pydicom:
            return []
        files = []
        for path in sorted(self.study_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() in IMAGE_EXTS:
                continue
            if path.suffix.lower() not in ("", ".dcm", ".ima", ".dicom"):
                continue
            try:
                pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
                files.append(path)
            except Exception:
                continue
        return files

    def _image_files(self) -> list[Path]:
        return [p for p in sorted(self.study_dir.rglob("*")) if p.is_file() and p.suffix.lower() in IMAGE_EXTS]

    def _select_indices(self, count: int, n_select: int) -> list[int]:
        if n_select >= count:
            return list(range(count))
        if n_select <= 0:
            return []
        if n_select == 1:
            return [count // 2]
        return sorted({min(count - 1, round(i * (count - 1) / (n_select - 1))) for i in range(n_select)})

    def _target_count(self, diagnostic_count: int, total_count: int) -> int:
        if diagnostic_count <= self.target_max:
            return diagnostic_count
        if diagnostic_count >= self.target_min:
            return min(self.target_max, diagnostic_count)
        return min(self.target_max, total_count)

    def _allocation(self, groups: list[tuple[str, int, bool]], target: int) -> dict[str, int]:
        if target <= 0:
            return {}
        diagnostic = [(sid, n) for sid, n, localizer in groups if not localizer and n > 0]
        fallback = [(sid, n) for sid, n, _ in groups if n > 0]
        active = diagnostic or fallback
        alloc = {sid: 0 for sid, _, _ in groups}
        remaining = target
        for sid, _ in active:
            if remaining <= 0:
                break
            alloc[sid] = 1
            remaining -= 1
        total = sum(n for _, n in active)
        for sid, n in active:
            if remaining <= 0:
                break
            share = max(0, min(n - alloc[sid], math.floor(remaining * (n / max(total, 1)))))
            alloc[sid] += share
            remaining -= share
        while remaining > 0:
            changed = False
            for sid, n in active:
                if remaining <= 0:
                    break
                if alloc[sid] < n:
                    alloc[sid] += 1
                    remaining -= 1
                    changed = True
            if not changed:
                break
        return alloc

    def _dicom_priority(self, series: EvidenceSeries) -> float:
        """Priority weight for choosing representative DICOM evidence.

        Real lumbar validation showed that evenly sampling every series can under-represent
        the axial/post-contrast stacks where lateral recess, foramen, and nerve-root findings
        are decided. This preserves the 40-80 evidence cap while giving those stacks denser
        coverage than broad sagittal/coronal context series.
        """
        if series.is_localizer:
            return 0.0
        name = (series.name or series.sequence_label or "").lower()
        plane = (series.plane or "").lower()
        modality = (series.modality or "").upper()
        priority = 1.0
        if plane == "axial":
            priority += 2.0
        elif plane == "sagittal":
            priority += 0.35
        elif plane == "coronal":
            priority += 0.15
        if modality == "MR":
            if plane == "axial" and any(k in name for k in ("tra", "ax", "transverse")):
                priority += 1.0
            if plane == "axial" and any(k in name for k in ("t2", "t1")):
                priority += 0.75
            if plane == "axial" and any(k in name for k in ("fs", "fat", "vibe", "cont", "contrast", "post")):
                priority += 1.25
            if plane == "axial" and any(k in name for k in ("vibe", "post", "cont", "contrast")):
                priority += 1.25
            if "l spine" in name or "lumbar" in name:
                priority += 0.5 if plane == "axial" else 0.15
        return priority

    def _dicom_allocation(self, series: list[EvidenceSeries], groups: list[tuple[str, int, bool]], target: int) -> dict[str, int]:
        if target <= 0:
            return {}
        by_id = {s.series_id: s for s in series}
        active = [(sid, n) for sid, n, localizer in groups if not localizer and n > 0]
        fallback = [(sid, n) for sid, n, _ in groups if n > 0]
        chosen = active or fallback
        alloc = {sid: 0 for sid, _, _ in groups}
        if not chosen:
            return alloc
        if sum(n for _, n in chosen) <= target:
            for sid, n in chosen:
                alloc[sid] = n
            return alloc

        remaining = target
        # Keep at least one representative from every diagnostic series before weighting.
        for sid, _ in chosen:
            if remaining <= 0:
                break
            alloc[sid] = 1
            remaining -= 1

        weighted = []
        for sid, n in chosen:
            priority = self._dicom_priority(by_id.get(sid, EvidenceSeries(sid, sid, sid, "", "", "", n)))
            weighted.append((sid, n, max(0.1, priority), n * max(0.1, priority)))
        total_weight = sum(w for _, _, _, w in weighted) or 1.0
        for sid, n, _priority, weight in weighted:
            if remaining <= 0:
                break
            share = max(0, min(n - alloc[sid], math.floor(remaining * (weight / total_weight))))
            alloc[sid] += share
            remaining -= share

        # Fill leftovers by priority so dense axial contrast stacks get the extra slices.
        while remaining > 0:
            changed = False
            for sid, n, priority, _weight in sorted(weighted, key=lambda item: item[2], reverse=True):
                if remaining <= 0:
                    break
                if alloc[sid] < n:
                    alloc[sid] += 1
                    remaining -= 1
                    changed = True
            if not changed:
                break

        self._align_contrast_pairs(series, alloc, target)
        return alloc

    def _align_contrast_pairs(self, series: list[EvidenceSeries], alloc: dict[str, int], target: int) -> None:
        """Keep same-size axial pre/post contrast siblings sampled at matching indices."""
        pair_groups: dict[tuple[str, int], list[EvidenceSeries]] = {}
        paired_sids: set[str] = set()
        for s in series:
            if (s.plane or "").lower() != "axial" or not s.slice_count:
                continue
            key = _contrast_pair_key(s.name or s.sequence_label)
            if not key:
                continue
            pair_groups.setdefault((key, s.slice_count), []).append(s)

        for (_key, _count), siblings in pair_groups.items():
            if len(siblings) < 2:
                continue
            names = [(s.name or s.sequence_label or "").lower() for s in siblings]
            has_contrast = any(any(t in name for t in ("cont", "contrast", "post", "gd", "gad")) for name in names)
            has_plain = any(not any(t in name for t in ("cont", "contrast", "post", "gd", "gad")) for name in names)
            if not (has_contrast and has_plain):
                continue
            pair_count = min(siblings[0].slice_count, max(alloc.get(s.series_id, 0) for s in siblings))
            for s in siblings:
                alloc[s.series_id] = max(alloc.get(s.series_id, 0), pair_count)
                paired_sids.add(s.series_id)

        total = sum(alloc.values())
        if total <= target:
            return
        priorities = {s.series_id: self._dicom_priority(s) for s in series}
        # Trim from the lowest-priority non-paired series first, preserving one image each.
        candidates = sorted(
            [s for s in series if alloc.get(s.series_id, 0) > 1],
            key=lambda s: (s.series_id in paired_sids, priorities.get(s.series_id, 1.0)),
        )
        while total > target and candidates:
            changed = False
            for s in candidates:
                if total <= target:
                    break
                if alloc.get(s.series_id, 0) > 1:
                    alloc[s.series_id] -= 1
                    total -= 1
                    changed = True
            if not changed:
                break
        for (_key, _count), siblings in pair_groups.items():
            sids = [s.series_id for s in siblings if s.series_id in paired_sids]
            if len(sids) < 2:
                continue
            paired_count = min(alloc.get(sid, 0) for sid in sids)
            for sid in sids:
                alloc[sid] = paired_count

    def _build_dicom_pack(self, files: list[Path]) -> EvidencePack:
        grouped: dict[str, list[tuple[Path, object]]] = {}
        for path in files:
            try:
                ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
            except Exception:
                continue
            uid = str(getattr(ds, "SeriesInstanceUID", "") or getattr(ds, "SeriesDescription", "") or "unknown")
            grouped.setdefault(uid, []).append((path, ds))

        series_out: list[EvidenceSeries] = []
        selected: list[EvidenceImage] = []
        groups_for_alloc = []
        sorted_groups = sorted(grouped.items(), key=lambda kv: str(getattr(kv[1][0][1], "SeriesDescription", kv[0])))
        for idx, (uid, entries) in enumerate(sorted_groups, start=1):
            first = entries[0][1]
            name = str(getattr(first, "SeriesDescription", "") or getattr(first, "ProtocolName", "") or f"Series {idx}")
            sid = f"s{idx:03d}_{safe_id(name, 'series')}"
            localizer = is_localizer_name(name)
            groups_for_alloc.append((sid, len(entries), localizer))
            ps = getattr(first, "PixelSpacing", None)
            spacing = [float(ps[0]), float(ps[1])] if ps and len(ps) >= 2 else None
            thickness = getattr(first, "SliceThickness", None)
            plane = detect_plane_from_orientation(getattr(first, "ImageOrientationPatient", None), name)
            modality = str(getattr(first, "Modality", "") or "")
            notes = []
            if getattr(first, "PatientOrientation", None):
                notes.append(f"patient_orientation={list(first.PatientOrientation)}")
            if getattr(first, "ImageOrientationPatient", None):
                notes.append("orientation_from_dicom")
            series_out.append(EvidenceSeries(
                series_id=sid,
                series_uid=safe_id(uid, sid),
                name=name,
                modality=modality,
                plane=plane,
                sequence_label=name,
                slice_count=len(entries),
                pixel_spacing=spacing,
                slice_thickness=float(thickness) if thickness else None,
                orientation_laterality_notes="; ".join(notes),
                is_localizer=localizer,
            ))

        diagnostic_count = sum(n for _, n, localizer in groups_for_alloc if not localizer)
        target = self._target_count(diagnostic_count, len(files))
        alloc = self._dicom_allocation(series_out, groups_for_alloc, target)
        series_by_id = {s.series_id: s for s in series_out}
        evidence_n = 0

        for idx, (_uid, entries) in enumerate(sorted_groups, start=1):
            first = entries[0][1]
            name = str(getattr(first, "SeriesDescription", "") or getattr(first, "ProtocolName", "") or f"Series {idx}")
            sid = f"s{idx:03d}_{safe_id(name, 'series')}"
            ordered = sorted(entries, key=lambda e: int(getattr(e[1], "InstanceNumber", 0) or 0))
            indices = self._select_indices(len(ordered), alloc.get(sid, 0))
            for image_index in indices:
                path, meta = ordered[image_index]
                try:
                    ds = pydicom.dcmread(str(path), force=True)
                    arr = _window_array(ds)
                    evidence_n += 1
                    evidence_id = f"ev{evidence_n:03d}"
                    dest = self.image_dir / f"{evidence_id}.png"
                    Image.fromarray(arr).save(dest)
                    rel = dest.relative_to(self.work_dir).as_posix()
                    plane = detect_plane_from_orientation(getattr(ds, "ImageOrientationPatient", None), name)
                    item = EvidenceImage(
                        evidence_id=evidence_id,
                        series_id=sid,
                        image_index=image_index + 1,
                        instance_number=int(getattr(ds, "InstanceNumber", image_index + 1) or image_index + 1),
                        relative_path=rel,
                        plane=plane,
                        slice_location=float(getattr(ds, "SliceLocation", 0)) if getattr(ds, "SliceLocation", None) is not None else None,
                        is_localizer=is_localizer_name(name),
                        source_ref=f"{sid}:{image_index + 1}",
                    )
                    selected.append(item)
                    series_by_id[sid].representative_slice_paths.append(rel)
                except Exception as e:
                    logger.warning("Could not render evidence image %s: %s", path.name, e)

        modalities = sorted({s.modality for s in series_out if s.modality})
        calibrated_series = [s for s in series_out if s.pixel_spacing]
        study = {
            "modality": modalities[0] if len(modalities) == 1 else ("/".join(modalities) if modalities else ""),
            "anatomy": "unknown",
            "subregion": "",
            "calibrated": bool(series_out) and len(calibrated_series) == len(series_out),
            "calibration_reason": (
                "PixelSpacing present on every DICOM series"
                if series_out and len(calibrated_series) == len(series_out)
                else "One or more DICOM series are missing PixelSpacing"
            ),
            "input_type": "dicom",
            "series_count": len(series_out),
            "image_count": len(files),
            "selected_image_count": len(selected),
            "localizer_excluded_count": max(0, len(files) - diagnostic_count),
        }
        limitations = []
        if any(s.is_localizer for s in series_out):
            limitations.append("Obvious localizer/scout series were excluded from diagnostic evidence unless needed as fallback.")
        if not study["calibrated"]:
            limitations.append("At least one DICOM series lacks PixelSpacing; precise measurements must not be inferred for that series.")
        return EvidencePack(1, study, series_out, selected, limitations, "")

    def _build_image_export_pack(self, files: list[Path]) -> EvidencePack:
        series_groups: dict[str, list[Path]] = {}
        for path in files:
            key = path.parent.name if path.parent != self.study_dir else "image_export"
            series_groups.setdefault(key, []).append(path)

        series_out: list[EvidenceSeries] = []
        selected: list[EvidenceImage] = []
        groups = []
        for idx, (name, group) in enumerate(sorted(series_groups.items()), start=1):
            sid = f"s{idx:03d}_{safe_id(name, 'images')}"
            localizer = is_localizer_name(name)
            groups.append((sid, len(group), localizer))
            series_out.append(EvidenceSeries(
                series_id=sid,
                series_uid=sid,
                name=name,
                modality="OT",
                plane=detect_plane_from_orientation(None, name),
                sequence_label=name,
                slice_count=len(group),
                pixel_spacing=None,
                slice_thickness=None,
                orientation_laterality_notes="image export; no DICOM orientation/laterality metadata",
                is_localizer=localizer,
            ))

        diagnostic_count = sum(n for _, n, localizer in groups if not localizer)
        alloc = self._allocation(groups, self._target_count(diagnostic_count, len(files)))
        evidence_n = 0
        series_by_id = {s.series_id: s for s in series_out}
        for idx, (name, group) in enumerate(sorted(series_groups.items()), start=1):
            sid = f"s{idx:03d}_{safe_id(name, 'images')}"
            ordered = sorted(group)
            for image_index in self._select_indices(len(ordered), alloc.get(sid, 0)):
                src = ordered[image_index]
                evidence_n += 1
                evidence_id = f"ev{evidence_n:03d}"
                dest = self.image_dir / f"{evidence_id}.png"
                try:
                    _save_image_copy(src, dest)
                except Exception:
                    shutil.copy2(src, dest)
                rel = dest.relative_to(self.work_dir).as_posix()
                selected.append(EvidenceImage(
                    evidence_id=evidence_id,
                    series_id=sid,
                    image_index=image_index + 1,
                    instance_number=None,
                    relative_path=rel,
                    plane=series_by_id[sid].plane,
                    is_localizer=is_localizer_name(name),
                    source_ref=f"{sid}:{image_index + 1}",
                ))
                series_by_id[sid].representative_slice_paths.append(rel)

        study = {
            "modality": "OT",
            "anatomy": "unknown",
            "subregion": "",
            "calibrated": False,
            "calibration_reason": "Image exports do not carry DICOM PixelSpacing metadata",
            "input_type": "image_export",
            "series_count": len(series_out),
            "image_count": len(files),
            "selected_image_count": len(selected),
            "localizer_excluded_count": max(0, len(files) - diagnostic_count),
        }
        limitations = ["Image-export study: uncalibrated unless external scale metadata is later supplied."]
        return EvidencePack(1, study, series_out, selected, limitations, "")


def load_manifest(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def manifest_text_summary(manifest: dict) -> str:
    study = manifest.get("study") or {}
    series = manifest.get("series") or []
    selected = manifest.get("selected_images") or []
    lines = [
        "EVIDENCE PACK SUMMARY",
        f"- input_type: {study.get('input_type')}",
        f"- modality: {study.get('modality') or 'unknown'}",
        f"- calibrated: {study.get('calibrated')} ({study.get('calibration_reason')})",
        f"- series_count: {study.get('series_count')}; image_count: {study.get('image_count')}; selected: {len(selected)}",
    ]
    for s in series[:20]:
        lines.append(
            f"- {s.get('series_id')}: {s.get('name')} | {s.get('modality')} | {s.get('plane') or 'unknown plane'} | "
            f"{s.get('slice_count')} images | localizer={s.get('is_localizer')}"
        )
    if len(series) > 20:
        lines.append(f"- ... {len(series) - 20} additional series omitted from text summary")
    if manifest.get("limitations"):
        lines.append("Limitations:")
        lines.extend(f"- {x}" for x in manifest["limitations"])
    return "\n".join(lines)

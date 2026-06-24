"""
Deterministic annotation-overlap check (no Claude, no credits)
==============================================================
Objective, free verification of whether MIKA's reported/annotated lesion location
actually points at the TRUE lesion, by comparing against a pixel-level ground-truth
segmentation mask (a TCIA DICOM-SEG).

What it does
------------
1. Pulls the ground-truth lesion mask for a mask-bearing study from the TCIA NBIA
   REST API (anonymous, no login), parses the DICOM-SEG, and extracts:
     - lesion centroid in patient space (LPS mm)
     - the gland (Prostate) centroid, so side is judged RELATIVE TO THE GLAND
       midline (the whole gland is offset from x=0 — comparing to 0 is wrong)
     - segmented slice z-positions, voxel count, approximate volume (cc) and
       longest in-plane extent (mm) via PixelSpacing / SliceThickness
     - derived side (LEFT/RIGHT) and AP position (anterior/posterior)
2. Reads MIKA's reported localization for the same study from the validation cache:
     - cache/<name>/second_read.json : second_read.extreme_focus.location + .slice
                                       and structured_score.region
     - cache/<name>/summary.json     : patient.bottom_line
3. Emits a verdict: GT location vs MIKA-reported location, with side match (check/x)
   and zone/AP match (check/x). If a NUMERIC MIKA coordinate is ever present
   (annotation_coords), it also computes centroid-to-mask distance (mm) and
   inside-mask yes/no. Today MIKA reports text + slice only, so the computable
   signal is side / zone / AP / slice agreement.

The mask extraction is exposed as a reusable function:
    lesion_ground_truth(collection, patient_id, study_uid_suffix) -> dict
so other mask-bearing datasets (MSD / SPIDER / BraTS / LIDC) can be wired in later.

Run from backend/ :
    python -m validation.annotation_overlap                       # prostate default
    python -m validation.annotation_overlap --study tcia-qin-prostate-tumor
    python -m validation.annotation_overlap --json results.json   # also write JSON
    python -m validation.annotation_overlap --no-download         # use a cached mask only
"""
import argparse
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import pydicom

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
MASK_CACHE = HERE / "_mask_cache"          # downloaded SEG DICOMs (gitignored)

NBIA_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
_UA = {"User-Agent": "MIKA-validation-annotation-overlap/1.0"}

# Registry of mask-bearing studies. Add MSD/SPIDER/BraTS/LIDC entries here later;
# each just needs a loader that yields the same ground-truth dict shape.
TCIA_STUDIES = {
    "tcia-qin-prostate-tumor": {
        "collection": "QIN-PROSTATE-Repeatability",
        "patient_id": "PCAMPMRI-00012",
        "study_uid_suffix": "629694",
        # which SEG series to use (Modality==SEG + this text in SeriesDescription)
        "seg_series_desc": "Apparent Diffusion Coefficient",
        "lesion_label": "Lesion",
        "gland_label": "Prostate",
    },
}


# --------------------------------------------------------------------------- #
# TCIA NBIA REST helpers (anonymous)
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        return resp.read()


def _list_series(collection: str, patient_id: str) -> list:
    url = NBIA_BASE + "/getSeries?" + urllib.parse.urlencode(
        {"Collection": collection, "PatientID": patient_id}
    )
    return json.loads(_get(url, timeout=60).decode("utf-8"))


def _download_series_zip(series_uid: str) -> bytes:
    url = NBIA_BASE + "/getImage?" + urllib.parse.urlencode({"SeriesInstanceUID": series_uid})
    return _get(url, timeout=240)


def _pick_seg_series(rows: list, seg_series_desc: str, study_uid_suffix: str) -> dict:
    for r in rows:
        if (
            r.get("Modality") == "SEG"
            and seg_series_desc.lower() in (r.get("SeriesDescription") or "").lower()
            and (r.get("StudyInstanceUID") or "").endswith(study_uid_suffix)
        ):
            return r
    raise LookupError(
        f"No SEG series found (desc~'{seg_series_desc}', study...{study_uid_suffix})"
    )


def _seg_dataset_from_zip(raw_zip: bytes) -> pydicom.dataset.FileDataset:
    """A SEG series ZIP may have multiple files; use the one with BOTH a
    SegmentSequence and a PerFrameFunctionalGroupsSequence (the first may be empty)."""
    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    chosen = None
    for name in zf.namelist():
        try:
            ds = pydicom.dcmread(io.BytesIO(zf.read(name)), force=True)
        except Exception:  # noqa: BLE001 - skip LICENSE / non-DICOM members
            continue
        if hasattr(ds, "SegmentSequence") and hasattr(ds, "PerFrameFunctionalGroupsSequence"):
            chosen = ds
    if chosen is None:
        raise ValueError("SEG ZIP had no file with SegmentSequence + PerFrameFunctionalGroupsSequence")
    return chosen


# --------------------------------------------------------------------------- #
# SEG parsing -> per-segment patient-space geometry
# --------------------------------------------------------------------------- #
def _parse_seg(ds) -> dict:
    """Parse a multi-frame DICOM-SEG into per-segment centroid (LPS mm), voxel
    count, slice z-set, and an approx longest in-plane extent (mm)."""
    segnum = {int(q.SegmentNumber): str(q.SegmentLabel) for q in ds.SegmentSequence}
    arr = ds.pixel_array  # (frames, rows, cols), values 0/1
    if arr.ndim == 2:      # single frame edge case
        arr = arr[None, ...]

    shared = ds.SharedFunctionalGroupsSequence[0]
    pm = shared.PixelMeasuresSequence[0]
    ps = [float(x) for x in pm.PixelSpacing]           # [row(y) spacing, col(x) spacing]
    try:
        slice_thk = float(pm.SliceThickness)
    except Exception:  # noqa: BLE001
        slice_thk = None
    iop = [float(x) for x in shared.PlaneOrientationSequence[0].ImageOrientationPatient]
    xd = np.array(iop[:3])   # row direction (cols increase)
    yd = np.array(iop[3:])   # col direction (rows increase)

    pts = defaultdict(list)        # segnum -> [(patient_pt, n_pixels), ...]
    voxels = defaultdict(int)      # segnum -> total pixels
    zset = defaultdict(set)        # segnum -> {z mm}
    max_extent = defaultdict(float)  # segnum -> longest in-plane extent (mm)

    for i in range(arr.shape[0]):
        pf = ds.PerFrameFunctionalGroupsSequence[i]
        segn = int(pf.SegmentIdentificationSequence[0].ReferencedSegmentNumber)
        ipp = np.array([float(x) for x in pf.PlanePositionSequence[0].ImagePositionPatient])
        m = arr[i] > 0
        n = int(m.sum())
        if n == 0:
            continue
        ys, xs = np.nonzero(m)
        cx, cy = xs.mean(), ys.mean()
        # patient point = origin + col_index*col_spacing*row_dir + row_index*row_spacing*col_dir
        pt = ipp + cx * ps[1] * xd + cy * ps[0] * yd
        pts[segn].append((pt, n))
        voxels[segn] += n
        zset[segn].add(round(float(ipp[2]), 2))
        # in-plane extent on this frame (bbox diagonal in mm), keep the max
        w = (xs.max() - xs.min() + 1) * ps[1]
        h = (ys.max() - ys.min() + 1) * ps[0]
        max_extent[segn] = max(max_extent[segn], float(np.hypot(w, h)))

    out = {}
    for segn, label in segnum.items():
        P = pts.get(segn)
        if not P:
            out[segn] = {"label": label, "present": False}
            continue
        tot = sum(n for _, n in P)
        centroid = sum(p * n for p, n in P) / tot
        vol_cc = None
        if slice_thk:
            vol_cc = voxels[segn] * ps[0] * ps[1] * slice_thk / 1000.0
        out[segn] = {
            "label": label,
            "present": True,
            "centroid_lps_mm": [round(float(v), 2) for v in centroid],
            "voxel_count": voxels[segn],
            "slice_z_mm": sorted(zset[segn]),
            "n_slices": len(zset[segn]),
            "approx_volume_cc": (round(vol_cc, 4) if vol_cc is not None else None),
            "approx_longest_extent_mm": round(max_extent[segn], 1),
            "pixel_spacing_mm": ps,
            "slice_thickness_mm": slice_thk,
        }
    return {"segments_by_label": segnum, "geometry": out}


def _find_segn(segments_by_label: dict, want: str) -> Optional[int]:
    want_l = want.lower()
    for num, label in segments_by_label.items():
        if want_l in label.lower():
            return num
    return None


# --------------------------------------------------------------------------- #
# Public: reusable ground-truth extractor
# --------------------------------------------------------------------------- #
def lesion_ground_truth(
    collection: str,
    patient_id: str,
    study_uid_suffix: str,
    seg_series_desc: str = "Apparent Diffusion Coefficient",
    lesion_label: str = "Lesion",
    gland_label: str = "Prostate",
    allow_download: bool = True,
) -> dict:
    """Extract the ground-truth lesion from a TCIA DICOM-SEG.

    Returns a dict with: lesion centroid (LPS mm), segmented slice z-positions,
    voxel count + approx volume/extent, and side+AP RELATIVE TO THE GLAND centroid.
    Reusable for any TCIA mask-bearing study; other dataset families (MSD/SPIDER/
    BraTS/LIDC) can implement the same return shape via their own loader.
    """
    MASK_CACHE.mkdir(parents=True, exist_ok=True)
    cache_dcm = MASK_CACHE / f"{collection}_{patient_id}_{study_uid_suffix}_{seg_series_desc[:8]}.dcm".replace(" ", "_")

    ds = None
    if cache_dcm.exists():
        try:
            ds = pydicom.dcmread(str(cache_dcm), force=True)
            if not (hasattr(ds, "SegmentSequence") and hasattr(ds, "PerFrameFunctionalGroupsSequence")):
                ds = None
        except Exception:  # noqa: BLE001
            ds = None

    if ds is None:
        if not allow_download:
            raise FileNotFoundError(f"No cached SEG at {cache_dcm} and --no-download set")
        rows = _list_series(collection, patient_id)
        seg_row = _pick_seg_series(rows, seg_series_desc, study_uid_suffix)
        raw = _download_series_zip(seg_row["SeriesInstanceUID"])
        ds = _seg_dataset_from_zip(raw)
        try:
            ds.save_as(str(cache_dcm), write_like_original=True)
        except Exception:  # noqa: BLE001 - caching is best-effort
            pass

    parsed = _parse_seg(ds)
    seg_by_label = parsed["segments_by_label"]
    geom = parsed["geometry"]

    lesion_n = _find_segn(seg_by_label, lesion_label)
    gland_n = _find_segn(seg_by_label, gland_label)
    if lesion_n is None or not geom[lesion_n]["present"]:
        raise LookupError(f"No present '{lesion_label}' segment in SEG (have: {seg_by_label})")

    lesion = geom[lesion_n]
    result = {
        "source": "TCIA DICOM-SEG",
        "collection": collection,
        "patient_id": patient_id,
        "study_uid_suffix": study_uid_suffix,
        "segments": seg_by_label,
        "lesion": lesion,
    }

    # Side + AP relative to the GLAND centroid (NOT relative to x=0).
    lc = lesion["centroid_lps_mm"]
    if gland_n is not None and geom[gland_n]["present"]:
        gc = geom[gland_n]["centroid_lps_mm"]
        result["gland_centroid_lps_mm"] = gc
        dx = lc[0] - gc[0]   # LPS +x = patient LEFT
        dy = lc[1] - gc[1]   # LPS +y = posterior
        result["offset_from_gland_mm"] = {"x_left": round(dx, 2), "y_posterior": round(dy, 2)}
        result["side"] = "left" if dx > 0 else "right"
        result["ap"] = "posterior" if dy > 0 else "anterior"
        result["gland_relative"] = True
    else:
        result["side"] = None
        result["ap"] = None
        result["gland_relative"] = False
        result["note"] = "No gland segment present; side/AP cannot be judged relative to gland midline."

    # PZ classification heuristic for prostate: PZ is the posterolateral gland;
    # a lesion that is lateral (off-midline) AND posterior to gland center is PZ.
    if result.get("gland_relative"):
        lateral = abs(result["offset_from_gland_mm"]["x_left"]) >= 3.0
        posterior = result["offset_from_gland_mm"]["y_posterior"] > 0
        result["zone_guess"] = (
            "peripheral zone" if (lateral and posterior) else "non-PZ / central-transition"
        )
        if lateral:
            ap_word = {"posterior": "posterolateral", "anterior": "anterolateral"}.get(
                result["ap"], result["ap"]
            )
        else:
            ap_word = result["ap"]
        result["location_text"] = " ".join(
            x for x in [result["side"], ap_word, result["zone_guess"]] if x
        )
    return result


# --------------------------------------------------------------------------- #
# SPIDER spine level ground truth (multi-label .mha mask)
# --------------------------------------------------------------------------- #
# Caudal->cranial disc-level names. The SPIDER mask never drops a disc, so naming
# from the mask (lowest disc abutting the sacrum = L5-S1) is the reference the read's
# sacrum-up count is checked against.
SPINE_LEVELS_CAUDAL_UP = [
    "L5-S1", "L4-L5", "L3-L4", "L2-L3", "L1-L2",
    "T12-L1", "T11-T12", "T10-T11", "T9-T10", "T8-T9", "T7-T8",
]
# In the SPIDER masks, intervertebral discs carry high label values (>=200), the
# spinal canal sits ~100-130, and vertebrae are low integers (1-25). The floor cleanly
# separates discs from canal/vertebrae without hard-coding per-case label numbers.
SPIDER_DISC_LABEL_FLOOR = 200


def _canon_level_index(name: str) -> Optional[int]:
    """Caudal->cranial index of a disc level name, or None if unrecognized."""
    try:
        return SPINE_LEVELS_CAUDAL_UP.index((name or "").strip().upper().replace(" ", ""))
    except ValueError:
        return None


def spider_level_ground_truth(mask_path, disc_label_floor: int = SPIDER_DISC_LABEL_FLOOR) -> dict:
    """Parse a SPIDER multi-label .mha mask into per-disc-level ground truth.

    Discs are ordered by superior-inferior position and named sacrum-up FROM THE MASK
    (the caudal-most disc, abutting the sacrum, is L5-S1). Centroids are returned in
    patient LPS mm and as an inferior-positive ``si_mm`` axis so the read's level
    placement can be checked for the off-by-one that intensity-only verification misses.

    Returns a flat dict (no dataclass, matching this module's interchange style)::

        {source, mask_path, spacing_mm, n_discs, levels:{name:{label, centroid_lps_mm,
         si_mm, voxel_count, si_extent_mm}}, discs_caudal_to_cranial, sacrum_si_mm,
         median_disc_spacing_mm}
    """
    try:
        import SimpleITK as sitk  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("SimpleITK is required to parse SPIDER .mha masks") from exc

    img = sitk.ReadImage(str(mask_path))
    arr = sitk.GetArrayFromImage(img)  # (z, y, x) integer labels
    spacing = [float(s) for s in (img.GetSpacing() or (1.0, 1.0, 1.0))]  # (x, y, z)
    z_spacing = spacing[2] if len(spacing) > 2 else 1.0

    def _si_mm(centroid_zyx) -> float:
        # image index is (x, y, z) = (axis2, axis1, axis0); +z is superior in LPS, so
        # inferior-positive si_mm = -pz makes the caudal-most disc the largest value.
        cz, cy, cx = float(centroid_zyx[0]), float(centroid_zyx[1]), float(centroid_zyx[2])
        px, py, pz = img.TransformContinuousIndexToPhysicalPoint([cx, cy, cz])
        return -float(pz), [round(float(px), 2), round(float(py), 2), round(float(pz), 2)]

    labels = [int(v) for v in np.unique(arr) if int(v) != 0]
    discs = []
    for label in labels:
        if label < disc_label_floor:
            continue
        idx = np.argwhere(arr == label)  # rows of (z, y, x)
        if idx.size == 0:
            continue
        centroid = idx.mean(axis=0)
        si_mm, lps = _si_mm(centroid)
        si_extent_mm = float(idx[:, 0].max() - idx[:, 0].min()) * z_spacing
        discs.append({
            "label": label,
            "centroid_lps_mm": lps,
            "si_mm": round(si_mm, 2),
            # absolute SI voxel index (array axis 0) — the converted-DICOM row maps 1:1 to
            # this, so an engine level_map can be compared in this frame with no registration.
            "si_row": round(float(centroid[0]), 1),
            "voxel_count": int(len(idx)),
            "si_extent_mm": round(si_extent_mm, 1),
        })

    # Caudal-most disc (largest inferior-positive si_mm) is L5-S1; name upward from there.
    discs.sort(key=lambda d: d["si_mm"], reverse=True)
    levels: dict[str, dict] = {}
    order: list[str] = []
    for i, disc in enumerate(discs):
        name = SPINE_LEVELS_CAUDAL_UP[i] if i < len(SPINE_LEVELS_CAUDAL_UP) else f"disc_{i}"
        disc["level"] = name
        levels[name] = disc
        order.append(name)

    spacings = [order and abs(levels[order[i]]["si_mm"] - levels[order[i + 1]]["si_mm"])
                for i in range(len(order) - 1)]
    spacings = [s for s in spacings if s]
    median_spacing = float(sorted(spacings)[len(spacings) // 2]) if spacings else None

    # Most-inferior vertebra centroid = sacrum/S1 region — an absolute caudal anchor.
    sacrum_si_mm = None
    sacrum_si_row = None
    vert = [int(v) for v in labels if 0 < int(v) < 100]
    for label in vert:
        idx = np.argwhere(arr == label)
        if idx.size == 0:
            continue
        centroid = idx.mean(axis=0)
        si_mm, _ = _si_mm(centroid)
        if sacrum_si_mm is None or si_mm > sacrum_si_mm:
            sacrum_si_mm = round(si_mm, 2)
            sacrum_si_row = round(float(centroid[0]), 1)

    return {
        "source": "spider_mask",
        "mask_path": str(mask_path),
        "spacing_mm": [round(s, 3) for s in spacing],
        "n_discs": len(discs),
        "levels": levels,
        "discs_caudal_to_cranial": order,
        "sacrum_si_mm": sacrum_si_mm,
        "sacrum_si_row": sacrum_si_row,
        "median_disc_spacing_mm": round(median_spacing, 2) if median_spacing else None,
    }


def _annotation_si(point) -> Optional[float]:
    """Pull the SI coordinate from an annotation point.

    Accepts nucleus_points ``[AP, SI]`` (SI = index 1), a single row value, or a scalar.
    """
    if isinstance(point, (list, tuple)):
        if len(point) >= 2:
            return float(point[1])      # [AP, SI]
        if len(point) == 1:
            return float(point[0])
        return None
    try:
        return float(point)
    except (TypeError, ValueError):
        return None


def compare_spine_levels(gt: dict, annotation_points: dict, calib: Optional[dict] = None) -> dict:
    """Compare the read's per-level annotation points against SPIDER mask ground truth.

    ``annotation_points`` maps a claimed level name to its point (nucleus_points ``[AP,
    SI]`` or a ``level_map`` row). ``calib`` carries the registration from the point's SI
    units to the mask's inferior-positive ``si_mm``::

        {"si_scale": a, "si_offset": b}   # si_mm = a * si_units + b  (absolute frame)

    When no scale is given the SI axes are aligned by median disc spacing (best effort),
    and the result is flagged ``registration="approximate"`` with lower confidence — the
    off-by-one verdict is only asserted when the registration is absolute or the
    sacrum-anchored "missed caudal disc" signal fires (both independent of the read's own
    naming, so the check is not circular).

    Per level: ``si_mm``, ``mm_offset`` (to the same-named GT disc), ``inside_disc``,
    ``matched_level`` (nearest GT disc) and ``level_match``. Run level: ``mean_mm_offset``,
    ``level_match_rate``, ``off_by_one_detected``, plus ``reasons``.
    """
    calib = calib or {}
    levels = gt.get("levels") or {}
    if not levels or not annotation_points:
        return {
            "per_level": {},
            "mean_mm_offset": None,
            "level_match_rate": None,
            "off_by_one_detected": None,
            "registration": "none",
            "reasons": ["no ground-truth levels or no annotation points"],
        }

    # Model discs, caudal->cranial (larger SI is more inferior, like the mask).
    model = []
    for name, point in annotation_points.items():
        si = _annotation_si(point)
        if si is not None:
            model.append((name, si))
    model.sort(key=lambda t: t[1], reverse=True)
    if not model:
        return {
            "per_level": {}, "mean_mm_offset": None, "level_match_rate": None,
            "off_by_one_detected": None, "registration": "none",
            "reasons": ["annotation points had no usable SI coordinate"],
        }

    gt_order = gt.get("discs_caudal_to_cranial") or list(levels.keys())
    gt_si = [levels[n]["si_mm"] for n in gt_order]

    # --- registration: si_mm = a * si_units + b ---
    reasons = []
    if "si_scale" in calib:
        a = float(calib["si_scale"])
        b = float(calib.get("si_offset", 0.0))
        registration = "absolute"
    else:
        # Best-effort: match median spacing, align the means under the caudal-aligned
        # hypothesis. Off-by-one stays uncertain on this path (flagged below).
        model_sp = _median_step([s for _, s in model])
        gt_sp = gt.get("median_disc_spacing_mm") or _median_step(gt_si)
        a = (gt_sp / model_sp) if (model_sp and gt_sp) else 1.0
        n = min(len(model), len(gt_si))
        b = (sum(gt_si[:n]) / n) - a * (sum(s for _, s in model[:n]) / n) if n else 0.0
        registration = "approximate"
        reasons.append("no absolute SI calibration — registration is approximate")

    per_level: dict[str, dict] = {}
    offsets = []
    matches = 0
    shifts = []
    for name, si_units in model:
        si_mm = a * si_units + b
        # nearest GT disc by SI
        nearest = min(gt_order, key=lambda gn: abs(levels[gn]["si_mm"] - si_mm))
        nearest_si = levels[nearest]["si_mm"]
        level_match = (name.strip().upper().replace(" ", "") == nearest.upper().replace(" ", ""))
        if level_match:
            matches += 1
        ci_claim, ci_near = _canon_level_index(name), _canon_level_index(nearest)
        if ci_claim is not None and ci_near is not None:
            shifts.append(ci_near - ci_claim)
        # mm offset to the SAME-named GT disc when present, else to the nearest
        same = levels.get(name) or levels.get(name.strip().upper().replace(" ", ""))
        if same is not None:
            mm_off = abs(si_mm - same["si_mm"])
        else:
            mm_off = abs(si_mm - nearest_si)
        offsets.append(mm_off)
        half_extent = max((levels[nearest].get("si_extent_mm") or 0) / 2.0, 1.0)
        per_level[name] = {
            "si_mm": round(si_mm, 2),
            "matched_level": nearest,
            "level_match": level_match,
            "mm_offset": round(mm_off, 2),
            "inside_disc": abs(si_mm - nearest_si) <= half_extent,
        }

    level_match_rate = matches / len(model)
    mean_mm_offset = round(sum(offsets) / len(offsets), 2) if offsets else None

    # --- off-by-one verdict ---
    # (1) absolute frame: a consistent ±1 canonical shift across most discs.
    consistent_one = False
    if shifts:
        from collections import Counter
        common, count = Counter(shifts).most_common(1)[0]
        consistent_one = abs(common) == 1 and count >= (len(shifts) + 1) // 2
        if consistent_one:
            reasons.append(f"claimed levels are shifted {common:+d} vs nearest mask disc")
    # (2) sacrum-anchored: a real GT disc sits caudal to the model's caudal-most point
    #     by ~a disc height — i.e. the lowest disc was never marked. Independent of naming.
    missed_caudal = False
    model_caudal_si = a * model[0][1] + b
    gt_caudal_si = max(gt_si)
    step = gt.get("median_disc_spacing_mm") or _median_step(gt_si) or 0
    if step and (gt_caudal_si - model_caudal_si) >= 0.5 * step:
        missed_caudal = True
        reasons.append("a mask disc sits below the lowest annotated level (missed caudal disc)")

    if registration == "absolute":
        off_by_one = bool(consistent_one or missed_caudal)
    else:
        # approximate registration cannot align an absolute offset; only the
        # sacrum-anchored missed-caudal signal is trustworthy here.
        off_by_one = bool(missed_caudal) if missed_caudal else (consistent_one or None)

    return {
        "per_level": per_level,
        "mean_mm_offset": mean_mm_offset,
        "level_match_rate": round(level_match_rate, 3),
        "level_match_kN": f"{matches}/{len(model)}",
        "off_by_one_detected": off_by_one,
        "missed_caudal_disc": missed_caudal,
        "registration": registration,
        "n_gt_discs": len(gt_si),
        "n_annotated": len(model),
        "reasons": reasons,
    }


def _median_step(values) -> Optional[float]:
    """Median absolute gap between consecutive sorted values (disc spacing)."""
    vs = sorted(float(v) for v in values)
    steps = [abs(vs[i + 1] - vs[i]) for i in range(len(vs) - 1)]
    if not steps:
        return None
    steps.sort()
    return steps[len(steps) // 2]


# --------------------------------------------------------------------------- #
# MIKA reported localization (from the validation cache)
# --------------------------------------------------------------------------- #
def _read_json(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def mika_reported(study_name: str) -> dict:
    """Pull MIKA's reported lesion localization from cache/<name>/."""
    d = CACHE / study_name
    sr = _read_json(d / "second_read.json") or {}
    summ = _read_json(d / "summary.json") or {}

    extreme = (sr.get("second_read") or {}).get("extreme_focus") or {}
    structured = sr.get("structured_score") or {}
    out = {
        "study_name": study_name,
        "has_second_read": bool(sr),
        "has_summary": bool(summ),
        "extreme_focus_location": extreme.get("location"),
        "extreme_focus_slice": extreme.get("slice"),
        "structured_region": structured.get("region"),
        "bottom_line": (summ.get("patient") or {}).get("bottom_line"),
        # numeric cross-series coordinate, if MIKA ever persists one:
        "annotation_coords": summ.get("annotation_coords") or sr.get("annotation_coords"),
    }
    # the best single textual localization MIKA produced
    out["reported_text"] = (
        out["extreme_focus_location"]
        or out["structured_region"]
        or out["bottom_line"]
        or ""
    )
    return out


# --------------------------------------------------------------------------- #
# Matching GT vs MIKA text
# --------------------------------------------------------------------------- #
def _side_in_text(text: str) -> Optional[str]:
    t = (text or "").lower()
    has_left = bool(re.search(r"\bleft\b", t))
    has_right = bool(re.search(r"\bright\b", t))
    if has_left and not has_right:
        return "left"
    if has_right and not has_left:
        return "right"
    return None  # ambiguous / bilateral / none


def _ap_in_text(text: str) -> Optional[str]:
    t = (text or "").lower()
    if "posterolateral" in t or "posterior" in t:
        return "posterior"
    if "anterolateral" in t or "anterior" in t:
        return "anterior"
    return None


def _zone_in_text(text: str) -> Optional[str]:
    t = (text or "").lower()
    if "peripheral zone" in t or re.search(r"\bpz\b", t) or "posterolateral" in t:
        return "peripheral zone"
    if "transition zone" in t or re.search(r"\btz\b", t):
        return "transition zone"
    if "central" in t:
        return "central"
    return None


def compare(gt: dict, mika: dict) -> dict:
    text = mika.get("reported_text", "")
    gt_side, gt_ap, gt_zone = gt.get("side"), gt.get("ap"), gt.get("zone_guess")
    m_side, m_ap, m_zone = _side_in_text(text), _ap_in_text(text), _zone_in_text(text)

    def _match(a, b):
        if a is None or b is None:
            return None  # not computable
        return a == b

    side_match = _match(gt_side, m_side)
    ap_match = _match(gt_ap, m_ap)
    zone_match = _match(gt_zone, m_zone)

    # numeric overlap only when a real coordinate is present
    numeric = None
    coords = mika.get("annotation_coords")
    if coords and isinstance(coords, dict) and "patient_mm" in coords:
        try:
            p = np.array([float(x) for x in coords["patient_mm"]])
            c = np.array([float(x) for x in gt["lesion"]["centroid_lps_mm"]])
            dist = float(np.linalg.norm(p - c))
            half = (gt["lesion"].get("approx_longest_extent_mm") or 0) / 2.0
            numeric = {
                "centroid_to_report_mm": round(dist, 2),
                "inside_mask": dist <= max(half, 1.0),
            }
        except Exception:  # noqa: BLE001
            numeric = None

    # overall verdict on the computable signals
    computable = [v for v in (side_match, ap_match, zone_match) if v is not None]
    if numeric is not None:
        verdict = "MATCH (numeric)" if numeric["inside_mask"] else "MISS (numeric)"
    elif not computable:
        verdict = "NOT COMPUTABLE (no parseable MIKA location)"
    elif all(computable):
        verdict = "MATCH"
    elif any(computable):
        verdict = "PARTIAL"
    else:
        verdict = "MISS"

    return {
        "gt_location": gt.get("location_text") or f"{gt_side} {gt_ap}",
        "mika_location": text,
        "gt": {"side": gt_side, "ap": gt_ap, "zone": gt_zone},
        "mika": {"side": m_side, "ap": m_ap, "zone": m_zone},
        "side_match": side_match,
        "ap_match": ap_match,
        "zone_match": zone_match,
        "numeric": numeric,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _mk(flag) -> str:
    if flag is None:
        return "n/a"
    return "MATCH" if flag else "MISMATCH"


def render_report(study_name: str, gt: dict, mika: dict, cmp: dict) -> str:
    L = gt["lesion"]
    off = gt.get("offset_from_gland_mm", {})
    lines = []
    lines.append("=" * 72)
    lines.append(f"ANNOTATION-OVERLAP CHECK  (deterministic, no Claude)  -  {study_name}")
    lines.append("=" * 72)
    lines.append("")
    lines.append("GROUND TRUTH (TCIA DICOM-SEG lesion mask)")
    lines.append(f"  collection/patient : {gt['collection']} / {gt['patient_id']}")
    lines.append(f"  segments in mask   : {gt['segments']}")
    lines.append(f"  lesion centroid LPS: {L['centroid_lps_mm']} mm")
    if gt.get("gland_centroid_lps_mm"):
        lines.append(f"  gland centroid LPS : {gt['gland_centroid_lps_mm']} mm")
        lines.append(
            f"  lesion vs gland    : {off.get('x_left')} mm "
            f"{'LEFT' if (off.get('x_left') or 0) > 0 else 'RIGHT'}, "
            f"{off.get('y_posterior')} mm "
            f"{'POSTERIOR' if (off.get('y_posterior') or 0) > 0 else 'ANTERIOR'}"
        )
    lines.append(f"  segmented slices   : z = {L['slice_z_mm']} mm ({L['n_slices']} slice(s))")
    lines.append(
        f"  size               : {L['voxel_count']} voxels, "
        f"~{L['approx_volume_cc']} cc, longest in-plane ~{L['approx_longest_extent_mm']} mm"
    )
    lines.append(f"  => GT LOCATION     : {gt.get('location_text')}")
    lines.append("")
    lines.append("MIKA REPORTED (from validation cache)")
    lines.append(f"  extreme_focus.loc  : {mika.get('extreme_focus_location')}")
    lines.append(f"  extreme_focus.slice: {mika.get('extreme_focus_slice')}")
    lines.append(f"  structured.region  : {mika.get('structured_region')}")
    lines.append(f"  annotation_coords  : {mika.get('annotation_coords')}  (numeric voxel/patient coord)")
    lines.append("")
    lines.append("COMPARISON")
    lines.append(f"  side   GT={cmp['gt']['side']!s:<10} MIKA={cmp['mika']['side']!s:<10} -> {_mk(cmp['side_match'])}")
    lines.append(f"  AP     GT={cmp['gt']['ap']!s:<10} MIKA={cmp['mika']['ap']!s:<10} -> {_mk(cmp['ap_match'])}")
    lines.append(f"  zone   GT={cmp['gt']['zone']!s:<10} MIKA={cmp['mika']['zone']!s:<10} -> {_mk(cmp['zone_match'])}")
    if cmp["numeric"] is not None:
        n = cmp["numeric"]
        lines.append(
            f"  numeric: centroid-to-report = {n['centroid_to_report_mm']} mm, "
            f"inside-mask = {'YES' if n['inside_mask'] else 'NO'}"
        )
    else:
        lines.append("  numeric: n/a (MIKA persists text+slice only, no cross-series voxel coord)")
    lines.append("")
    lines.append(f"  VERDICT: {cmp['verdict']}")
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_study(study_name: str, allow_download: bool = True) -> dict:
    cfg = TCIA_STUDIES.get(study_name)
    if not cfg:
        raise SystemExit(
            f"Unknown study '{study_name}'. Known mask-bearing studies: {list(TCIA_STUDIES)}"
        )
    gt = lesion_ground_truth(
        cfg["collection"], cfg["patient_id"], cfg["study_uid_suffix"],
        seg_series_desc=cfg["seg_series_desc"], lesion_label=cfg["lesion_label"],
        gland_label=cfg["gland_label"], allow_download=allow_download,
    )
    mika = mika_reported(study_name)
    cmp = compare(gt, mika)
    return {"study": study_name, "ground_truth": gt, "mika_reported": mika, "comparison": cmp}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic lesion annotation-overlap check (no Claude).")
    ap.add_argument("--study", default="tcia-qin-prostate-tumor",
                    help="study key (default: tcia-qin-prostate-tumor)")
    ap.add_argument("--json", default=None, help="also write the full result to this JSON path")
    ap.add_argument("--no-download", action="store_true",
                    help="use a cached SEG mask only; do not hit the TCIA API")
    args = ap.parse_args(argv)

    result = run_study(args.study, allow_download=not args.no_download)
    print(render_report(args.study, result["ground_truth"], result["mika_reported"], result["comparison"]))

    if args.json:
        outp = Path(args.json)
        if not outp.is_absolute():
            outp = HERE / outp
        outp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nWrote {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

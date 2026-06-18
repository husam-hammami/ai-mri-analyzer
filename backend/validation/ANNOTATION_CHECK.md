# Annotation-Overlap Check (`annotation_overlap.py`)

A **deterministic, free** verification (no Claude, no credits) that answers one
objective question:

> Does MIKA's reported/annotated lesion location actually point at the **real**
> lesion, as defined by a pixel-level ground-truth segmentation mask?

Run from `backend/`:

```bash
python -m validation.annotation_overlap                       # prostate (default)
python -m validation.annotation_overlap --study tcia-qin-prostate-tumor --json out.json
python -m validation.annotation_overlap --no-download         # use the cached mask only
```

## What it measures

1. **Ground truth** — pulls the lesion mask for a mask-bearing study from the TCIA
   NBIA REST API (anonymous, no login), parses the DICOM-SEG, and extracts:
   - lesion **centroid** in patient space (LPS mm)
   - the **gland (Prostate) centroid**, so side is judged **relative to the gland
     midline** — the whole gland is offset from `x=0`, so comparing the lesion's
     `x` to `0` is wrong; we compare it to the gland centroid
   - segmented **slice z-positions**, **voxel count**, approx **volume (cc)** and
     longest in-plane **extent (mm)** via PixelSpacing / SliceThickness
   - derived **side** (LEFT/RIGHT, LPS `+x` = patient LEFT) and **AP**
     (anterior/posterior, LPS `+y` = posterior), plus a PZ/zone guess
2. **MIKA reported** — reads MIKA's localization for the same study from the
   validation cache:
   - `cache/<name>/second_read.json` → `second_read.extreme_focus.location` + `.slice`
     and `structured_score.region`
   - `cache/<name>/summary.json` → `patient.bottom_line`
3. **Verdict** — GT location vs MIKA-reported location, with **side match**,
   **AP match**, and **zone match** (each MATCH / MISMATCH / n/a). If a numeric
   MIKA coordinate is ever present, it additionally computes
   **centroid-to-report distance (mm)** and **inside-mask yes/no**.

The mask extractor is exposed as a reusable function so other mask-bearing datasets
(MSD / SPIDER / BraTS / LIDC) can be added later:

```python
from validation.annotation_overlap import lesion_ground_truth
gt = lesion_ground_truth("QIN-PROSTATE-Repeatability", "PCAMPMRI-00012", "629694")
```

## Verified result — `tcia-qin-prostate-tumor`

```
GROUND TRUTH (TCIA DICOM-SEG lesion mask)
  segments in mask   : {1:'Normal', 2:'Peripheral zone of the prostate', 3:'Lesion', 4:'Prostate'}
  lesion centroid LPS: [-5.04, 54.21, 23.47] mm
  gland centroid LPS : [-15.98, 47.73, 25.7] mm
  lesion vs gland    : 10.94 mm LEFT, 6.48 mm POSTERIOR
  segmented slices   : z = [21.27, 25.27] mm (2 slice(s))
  size               : 33 voxels, ~0.0653 cc, longest in-plane ~6.7 mm
  => GT LOCATION     : left posterolateral peripheral zone

MIKA REPORTED
  extreme_focus.loc  : left posterolateral peripheral zone (mid-to-apex)
  structured.region  : left posterolateral peripheral zone (mid-to-apex)

COMPARISON
  side   GT=left            MIKA=left            -> MATCH
  AP     GT=posterior       MIKA=posterior       -> MATCH
  zone   GT=peripheral zone MIKA=peripheral zone -> MATCH
  VERDICT: MATCH
```

(`~0.065 cc` matches the labeled ground truth `TumorROI_PZ_1 ~0.065 cc`.)

## Honest limitation

This currently verifies **side / AP / zone / slice agreement** from MIKA's *text*
localization — **not** pixel-perfect arrow-tip overlap.

A true "the annotation arrow tip lands inside the lesion mask" check requires a
**numeric cross-series coordinate** for the annotation. Today MIKA only:

- renders an arrow onto a PNG (pixel coordinates of that PNG), and
- emits a **text** location plus a per-series **slice** reference
  (e.g. `ADC z=-2.73 (series s07 idx 17)`).

It does **not** persist the annotation's voxel/patient-space coordinate in
`summary.json`. Without that, the centroid-to-mask distance and inside-mask test
cannot be computed automatically — the tool falls back to text side/zone matching
(and prints `numeric: n/a`).

## Recommendation — add `annotation_coords` to MIKA output

Make MIKA persist, for each lesion annotation, the voxel/patient coordinate it
arrowed, so this check becomes fully automatic and pixel-exact for **every** study
(not just the curated text comparison). Suggested field in `summary.json`
(mirror it in `second_read.json` for second-reader annotations):

```json
"annotation_coords": [
  {
    "lesion_id": "L1",
    "series": "s07",            // SeriesInstanceUID or the engine's series key
    "slice_index": 17,          // index within that series
    "sop_instance_uid": "1.2.…",// the exact annotated frame (preferred)
    "pixel_xy": [142, 98],      // arrow tip in that slice's pixel grid
    "patient_mm": [-5.0, 54.0, 23.5]  // LPS mm (derived from ImagePositionPatient
                                      //  + ImageOrientationPatient + PixelSpacing)
  }
]
```

With `patient_mm` present, `compare()` automatically reports
`centroid-to-report distance (mm)` and `inside-mask yes/no` against the GT mask.
The conversion is exactly the inverse of what this module already does to turn the
mask's pixel centroids into patient mm (`ipp + cx*ps[col]*xdir + cy*ps[row]*ydir`),
so the producing code can reuse that math.

## Notes

- Downloaded SEG masks are cached under `backend/validation/_mask_cache/`
  (gitignored). `--no-download` reuses the cache and never touches the network.
- Deterministic only: this tool never calls Claude / the agent and spends no credits.

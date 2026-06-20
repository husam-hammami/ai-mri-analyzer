# Run 7 CV Evidence Engine Report

Date: 2026-06-21
Baseline: `e74bdf1 productize reference assisted review`

## What Changed

- Added `StudyGraph`, a deterministic metadata model for studies, series, and slices.
- Captured modality, plane, sequence, contrast phase, PixelSpacing, ImagePositionPatient, ImageOrientationPatient, SliceLocation, InstanceNumber, source type, and acquisition/contrast timing when available.
- Added an anatomy module interface with a structured `EvidenceCandidateSet` contract.
- Added the first anatomy module: `LumbarSpineEvidenceModule`.
- Added same-geometry pre/post axial T1 contrast registration QC.
- Added a calibrated candidate ROI contract for left L5-S1 lateral recess / postoperative-bed localization.
- Added verifier contract statuses: `supported`, `not_supported`, `cannot_assess`, and `localization_wrong`.
- Added trust helpers so body markers, pinpoint markers, and proof overlays stay suppressed unless geometry and registration confidence are high enough.

## February Validation

Expected:

- The engine should surface an inspectable left L5-S1 postoperative/lateral-recess pre/post contrast candidate, or explain exactly why it cannot assess.
- The engine must not force confirmation of scar, residual/recurrent disc, or nerve-root encasement.
- Markers should remain suppressed if geometry confidence is below the trust threshold.

Actual:

- The private February contrast lumbar MRI DICOM study was read by metadata only.
- StudyGraph detected DICOM MR, calibrated geometry, 14 total series, 13 diagnostic series, and 362 images.
- The lumbar module detected 5 sagittal MR series and 5 axial MR series.
- One calibrated candidate was surfaced:
  - candidate: left L5-S1 pre/post contrast lateral-recess ROI
  - geometry confidence: `0.78`
  - registration confidence: `0.94`
  - evidence refs: 26 safe series/slice refs
- Body-map marker, proof overlay, and pinpoint marker were all suppressed because geometry confidence is below the high-confidence marker threshold.

Status:

- The Run 7 success criterion is met: the February target region is now surfaced as inspectable evidence without creating an unsupported confirmed finding.
- The candidate remains localization-only and requires Claude/verifier review.

## Remaining Limitations

- L5-S1 level assignment currently uses DICOM coordinates plus approximate lumbar binning, not vertebral segmentation. This is intentionally capped below the marker trust threshold.
- The module does not classify scar versus residual/recurrent disc.
- The module does not assess nerve-root encasement.
- The module does not generate proof overlays for candidates below the trust threshold.
- JPG/image-export studies remain gated out of precise geometry candidates.

## What ML Is Now Justified

No diagnosis classifier is justified by this run. The deterministic engine surfaced the target ROI. Future ML may be justified only if additional validation proves deterministic geometry is insufficient for:

- robust lumbar level identification in transitional anatomy,
- vertebral/disc segmentation for higher level confidence,
- nerve-root or postoperative soft-tissue segmentation after labeled data exists.

## Verification Commands

Set private paths outside the repository before running local validation:

```powershell
$env:FEB_DICOM_DIR = "<private February DICOM folder outside repo>"
```

Focused tests:

```powershell
C:\Users\husam\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_run7_cv_evidence_engine.py
```

Metadata-only real-study validation:

```powershell
$env:PYTHONPATH = "$PWD\backend"
C:\Users\husam\AppData\Local\Programs\Python\Python311\python.exe <metadata-only validation snippet>
```

Full verification for this commit:

```powershell
C:\Users\husam\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q
C:\Users\husam\AppData\Local\Programs\Python\Python311\python.exe -m py_compile backend\core\study_graph.py backend\core\anatomy_modules\base.py backend\core\anatomy_modules\lumbar_spine.py
git diff --check
git diff --cached --check
```

## PHI Safety

No medical images, PDFs, reports, screenshots, generated PHI, raw validation outputs, or raw medical files were copied into this repository. This report contains only PHI-safe counts, confidence values, and contract behavior.

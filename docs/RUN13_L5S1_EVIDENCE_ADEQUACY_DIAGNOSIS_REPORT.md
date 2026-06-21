# Run 13 L5-S1 Evidence Adequacy Diagnosis Report

Baseline: `df877cb improve lumbar evidence quality`

Run 13 diagnosed whether the February left L5-S1 `cannot_assess` result is correct or
a recoverable evidence gap. This run added diagnostic instrumentation only. It did not
wire ROI sweep, cross-case harnessing, or any new behavior into the live patient read path.
No medical images, PDFs, screenshots, raw reports, raw Claude outputs, generated PHI, or
real-study artifacts were copied into the repo.

## Reference Claim Category

PHI-safe category extracted from the local reference report:

- `nerve_root_involvement`

No raw report text is stored in this repository.

## Sequence Adequacy

The February StudyGraph contains 13 non-localizer diagnostic series. PHI-safe sequence
labels:

| Series class | Role |
| --- | --- |
| sagittal T2 myelo, 2 slices | context only |
| sagittal T2 lumbar TSE, 17 slices | context only |
| sagittal T1 pre-contrast lumbar TSE, 17 slices | context only |
| sagittal STIR/TIRM lumbar, 17 slices | context only |
| axial T2 lumbar TSE, 31 slices | context only |
| axial T1 pre-contrast TSE, 31 slices | pre-contrast axial context, no matched post pair |
| sagittal T1 pre-contrast FS/TSE, 17 slices | context only |
| coronal STIR/TIRM, 23 slices | context only |
| axial T1 VIBE FS pre-contrast, 64 slices | selected pre-contrast axial candidate pair |
| axial T1 VIBE FS post-contrast, 64 slices | selected post-contrast axial candidate pair |
| sagittal T1 post-contrast TSE, 17 slices | post-contrast non-axial context |
| axial T1 pre-contrast TSE FS lumbar, 31 slices | pre-contrast axial context, no matched post pair |
| coronal T1 post-contrast TSE, 23 slices | post-contrast non-axial context |

Matched pre/post pair result:

- Selected pair: axial T1 VIBE FS pre/post, 64 slices each.
- Same geometry: true.
- Overlapping slice pairs: 64.
- Mean/max pair distance: 0.0 mm / 0.0 mm.
- Suitability: moderate.
- Better-suited matched post-contrast axial sequence found: false.
- Re-evaluation from alternate sequence triggered: false.

Conclusion: the evidence module did not miss a more appropriate matched axial post-contrast
sequence. Dedicated axial TSE pre-contrast stacks exist, but there is no matched dedicated
post-contrast axial TSE pair for the bounded pre/post subtraction question.

## ROI Sweep

Diagnostic sweep details:

- ROI specs tested: 81.
- Slice pairs per ROI: 5.
- Pair distance: 0.0 mm mean / 0.0 mm max.
- Registration confidence: 0.96.
- Difference maps allowed by QC: true.
- Run 12 narrowed ROI was included in the sweep.
- Sweep output is diagnostic only and cannot auto-promote a candidate.

Distribution:

| Metric | Result |
| --- | --- |
| ROI rows with reproducible subtraction signal | 81 / 81 |
| Localized focal candidate after cross-ROI interpretation | false |
| Sweep interpretation | `non_focal_broad_signal` |
| Auto-promote allowed | false |

Interpretation: the Run 12 narrowing did not hide a focal true-positive. The opposite pattern
appeared: subtraction signal was broad across plausible ROIs, so it is non-specific rather than
a localized left L5-S1 lateral-recess enhancement target.

## Cross-Case Harness

No additional local postoperative lumbar contrast DICOM cases were supplied through
`RUN13_ADDITIONAL_CASES`.

- Cross-case case count: 0.
- Generalized beyond February: false.
- Current localization-ceiling claim remains N=1.

## Decision

Classification:

- `CORRECT cannot_assess`

Reason:

- The reference category is clinically important (`nerve_root_involvement`), so the discrepancy
  remains important.
- However, no better matched axial post-contrast sequence was missed.
- The ROI sweep did not find a localized focal enhancement target; it found broad non-specific
  subtraction signal across all plausible ROIs.
- Therefore February is not proven to be a true MIKA miss from deterministic evidence selection.
  The correct behavior remains reference-assisted discrepancy wording plus no focused-evidence
  synthesis.

Final product behavior:

- Do not synthesize a patient focused-evidence explanation.
- Do not add a clinician CV-supported finding row.
- Do not create body-map markers, pinpoint markers, or patient proof overlays from this candidate.
- Preserve blind read, reference report, and reconciliation as separate sections.

## ML Decision

ML is not justified yet from Run 13.

If future cases show a localized focal signal that deterministic geometry repeatedly cannot place,
then ML would be justified only for segmentation/localization:

- vertebral/disc level segmentation,
- canal/lateral-recess localization,
- subtraction-artifact/fat-suppression quality scoring.

It should be validated against labeled masks such as SPIDER / RSNA LumbarDISC-style lumbar labels.
It should not be built as a scar-versus-recurrent-disc classifier or nerve-root encasement classifier
from this evidence.

## Verification Commands

```powershell
$env:PYTHONPATH='backend'
python -m pytest tests/test_run13_l5s1_diagnostics.py -q
```

```powershell
$env:RUN13_FEB_DICOM='<local February DICOM study outside repo>'
$env:RUN13_REFERENCE_PDF='<local February reference PDF outside repo>'
$env:RUN13_OUT="$env:LOCALAPPDATA\MIKA\validation\run13_l5s1_diagnostic"
python <local Run 13 diagnostic here-string using those env vars>
```

Final full verification:

```powershell
python -m pytest -q
python -m py_compile backend\validation\l5s1_evidence_diagnostics.py
git diff --check
git diff --cached --check
```

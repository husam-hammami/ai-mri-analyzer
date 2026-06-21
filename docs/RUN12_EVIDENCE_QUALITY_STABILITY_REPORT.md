# Run 12 Evidence Quality Stability Report

Baseline: `af1d209 stabilize cv candidate adjudication`

Run 12 improved the deterministic lumbar contrast candidate bundle and tested whether the focused
candidate review became repeatable on the local February contrast lumbar MRI. No medical images,
PDFs, screenshots, raw reports, raw Claude outputs, or generated PHI were copied into this repo.
All real-study artifacts stayed under the local MIKA validation directory outside the repository.

## What Changed

- Added physical pre/post slice pairing using `ImagePositionPatient` and `ImageOrientationPatient`.
- Rejected slice pairs above a 3.0 mm physical-distance threshold.
- Recorded deterministic pair metrics, mean/max pair distance, and accepted/rejected state.
- Added translation-based registration QC and suppressed difference maps when QC fails.
- Added internal candidate proof bundles for Claude/verifier review:
  - pre slice
  - post slice
  - registered difference image when QC passes
  - adjacent above/below slices
  - ROI box
  - level/side label
  - explicit `localization candidate, not diagnosis` labeling
- Expanded the candidate payload with selected evidence refs, adjacent refs, proof refs,
  pair distances, registration QC, contrast timing, and a bounded candidate question.
- Added `unstable` as an allowed candidate-review status.
- Tightened the focused-review prompt so bounded candidate review cannot create broad
  "no abnormality" statements.
- Replaced the prior terminal-volume L5-S1 bin for broad axial volumes with a sagittal disc-band
  projection into the axial stack when sagittal geometry is usable.
- Narrowed the left lateral-recess ROI so it is a focused candidate box, not a broad hemicanal box.

## P0/P1/P2 Findings

| Severity | Finding | Expected Result | Actual Result | Likely Files | Fix | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | Broad post-contrast axial volume could map L5-S1 to caudal/pelvic-tail slices. | Candidate should use physical geometry and avoid terminal tail slices. | Early Run 12 reviews repeatedly rejected the proof bundle as wrong-level/caudal. | `backend/core/anatomy_modules/lumbar_spine.py` | Added sagittal disc-band to axial-stack physical projection and normal-axis slice ordering. | `test_sagittal_projection_prevents_terminal_tail_l5_s1_selection`; real rerun moved candidate to slices 30-34. |
| P1 | ROI was too broad/central for a left lateral recess candidate. | ROI should be a bounded left lateral-recess localization candidate. | Focused reviews said the ROI was too broad or near midline. | `backend/core/anatomy_modules/lumbar_spine.py` | Narrowed ROI width/height and shifted to a focused left lateral position. | Final real N=3 rerun used ROI x=0.54, width=0.10 and produced repeatable `cannot_assess`. |
| P2 | Difference maps can pass numeric registration QC but still show non-focal subtraction/fat-suppression signal. | Claude/verifier should reject or cannot-assess instead of synthesizing. | Final reviewers agreed the proof did not support a focal enhancement claim. | `backend/core/anatomy_modules/lumbar_spine.py`, `backend/services/agent_runner.py` | Kept difference maps internal, recorded QC, and required bounded verifier judgment. | Final N=3 status distribution: `cannot_assess` x3; synthesis disabled. |
| P2 | Contrast timing/bolus metadata is incomplete in the candidate payload for this study. | Missing timing should remain a limitation, not a claim. | Candidate carries timing fields, but review still treated contrast certainty as limited. | `backend/core/anatomy_modules/lumbar_spine.py` | Persisted available timing/contrast fields without inventing missing values. | Final N=3 reasons consistently withheld support. |

No P0 remains from Run 12. The February candidate is now handled conservatively and repeatably, but
it is not supported.

## Proof Bundle Quality Checks

Final local validation bundle:

- Selected evidence images: 80.
- CV candidates: 1.
- Candidate type: left L5-S1 pre/post contrast lateral-recess ROI.
- Final projected candidate slices: pre/post matched slices 30-34.
- Internal proof bundle images: 9.
- Accepted slice pairs: 5.
- Mean pair distance: 0.0 mm.
- Max pair distance: 0.0 mm.
- Difference map allowed by registration QC: true.
- Registration confidence: 0.96.
- Geometry confidence: 0.75.
- Marker/proof trust: still below patient-facing marker/proof threshold; no body marker or patient proof overlay should be created from candidate-only evidence.

## Repeat Review Results

Run 11 baseline repeated the same candidate as:

- `cannot_assess` x2
- `supported` x1
- final adjudication: `cannot_assess`

Run 12 progression:

- Initial physical-pair/proof-bundle rerun: split (`supported`, `not_supported`, `localization_wrong`).
- ROI-only improvement: still split (`supported` x2, `localization_wrong` x1).
- Sagittal-projection level fix: `cannot_assess` x3.
- Final sagittal-projection plus narrower ROI: `cannot_assess` x3.

Final adjudicated status:

- `final_status`: `cannot_assess`
- `stability`: `stable_all_agree`
- `synthesis_allowed`: false

## February Target Status

The February left L5-S1 target is not stable as a supported finding. The evidence engine now produces
a cleaner and repeatable candidate review, but all three final focused reviews withheld support.

Exact evidence blocker:

- The matched pre/post slice geometry is clean, but the axial proof remains anatomically ambiguous
  for an exact L5-S1 lateral-recess claim.
- The visible pre/post difference is not a reproducible focal enhancement pattern inside the ROI.
- The broad post-contrast axial sequence is not enough for the verifier to classify abnormal
  enhancing tissue in the operative-bed/lateral-recess region.

MIKA must therefore keep the final focused evidence status as `cannot_assess`, suppress patient
synthesis, and preserve reference-assisted discrepancy wording rather than creating a confirmed
finding.

## Broader Accuracy And ML

Broader accuracy is not proven yet. Run 12 proves a narrower claim: deterministic pairing,
registration metadata, proof-bundle construction, and repeated candidate adjudication became more
repeatable for this candidate.

ML is now justified for anatomy/level-localization support only, especially lumbar vertebral/disc
segmentation, canal/lateral-recess localization, and subtraction-artifact quality scoring. ML is not
justified here as a scar-versus-recurrent-disc classifier or nerve-root encasement classifier; those
must remain Claude/verifier or clinician judgments unless separately validated.

## Verification Commands

Commands run with the real study path supplied as a local environment variable outside the repo:

```powershell
$env:PYTHONPATH='backend'
python -m pytest tests/test_run7_cv_evidence_engine.py tests/test_run8_cv_pipeline.py tests/test_run11_cv_adjudication.py -q
python -m pytest -q
python -m py_compile backend\core\anatomy_modules\base.py backend\core\anatomy_modules\lumbar_spine.py backend\services\evidence_pack.py backend\services\agent_runner.py
git diff --check
git diff --cached --check
```

```powershell
$env:PYTHONPATH='backend'
$env:RUN12_FEB_DICOM='<local February DICOM study outside repo>'
$env:RUN12_OUT="$env:LOCALAPPDATA\MIKA\validation\run12_evidence_quality_sagproj_roi5"
python <local Run 12 evidence generation here-string using $env:RUN12_FEB_DICOM and $env:RUN12_OUT>
python <local Run 12 focused Claude CLI N=3 here-string using $env:RUN12_FEB_DICOM and $env:RUN12_OUT>
```

Final validation used the Claude CLI subscription path with API-key environment variables removed
from the child process. No API-key fallback was used.

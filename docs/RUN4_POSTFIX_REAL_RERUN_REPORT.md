# Run 4 Post-Fix Real Rerun Report

Validation date: 2026-06-20

Scope: post-Run 3 reruns on local lumbar imaging data using the app's subscription Claude CLI
agent path only. Medical images, PDFs, raw reports, generated outputs, screenshots, and raw
validation artifacts were kept outside the repository under the local app-data validation area.
This report is PHI-safe: it contains no patient identifiers and no embedded medical images.

## Method

- Re-read `AGENTS.md` and `docs/RUN3_REAL_DATA_VALIDATION_REPORT.md`.
- Ran full post-fix app/agent reads for all four local datasets with API-key/token environment
  variables stripped.
- Compared safe concept flags against the February reference report and inspected structured
  findings/proof-artifact QA without copying raw output into the repo.
- After the February P0 persisted, implemented scoped code fixes, reran the February DICOM MRI,
  and then ran a focused image-only L5-S1 probe through the same subscription CLI path.

## Study Metrics

| Study | Agent status | Modality/anatomy | Images found | Selected evidence | Calibration | Exclusions | Report/PDF/persistence |
|---|---:|---|---:|---:|---|---:|---|
| Feb 2026 contrast lumbar MRI DICOM, first Run 4 rerun | complete | MR / spine | 362 | 80 | calibrated DICOM | 8 localizers | patient + clinical PDFs available; reload and recent studies passed |
| Feb 2026 contrast lumbar MRI DICOM, patched rerun | complete | MR / spine | 362 | 80 | calibrated DICOM | 8 localizers | patient + clinical PDFs available; reload and recent studies passed |
| Jun 2025 JPG MRI export | complete | MR / unknown before model | 168 | 80 | uncalibrated image export | 0 | patient + clinical PDFs available; reload and recent studies passed |
| Sep 2025 JPG MRI export | complete | MR / unknown before model | 317 | 80 | uncalibrated image export | 0 | patient + clinical PDFs available; reload and recent studies passed |
| Mar 2026 lumbar X-ray | complete | DX / spine | 4 | 4 | DICOM detector pixel spacing; projection magnification limitation stated | 0 | patient + clinical PDFs available; reload and recent studies passed |

## Run 3 Issue Status

| Run 3 issue | Run 4 status |
|---|---|
| P0 post-surgical L5-S1 pattern missed on February DICOM MRI | Still failing / blocker. The agent now performs the checklist, but still negates the reference target pattern. |
| P1 JPG evidence used converted synthetic DICOM series | Fixed. Both JPG studies used the original upload as one uncalibrated image-export series. |
| P1 patient proof images suppressed despite technical evidence | Fixed for the February and September completed jobs; June had one incidental figure suppressed because it had no evidence ref. |
| P1 X-ray harness expected wrong input type | Fixed. Real app/agent path treated the X-ray folder as DICOM DX, not MRI or image export. |
| P2 JPG anatomy unknown before model | Still deferred. The evidence contract is safe, but pre-model anatomy remains unknown for JPG exports. |

## Findings

### P0 - February DICOM MRI Still Disagrees With The Reference Target

Expected result: The February read should identify or explicitly reconcile the reference target:
left L5-S1 postoperative anatomy, left lateral recess soft tissue/scar-fibrosis versus
residual/recurrent disc, foraminal narrowing, descending S1/L5 root involvement, correct side,
correct level, confidence tier, and evidence refs, without a broad false "no abnormal
enhancement" statement.

Actual result: Two full app/agent reruns completed and cited evidence refs, but both still
negated the target pattern. The patched rerun restored a broad no-abnormal-enhancement statement
and did not assess residual/recurrent disc or the lateral recess as the reference requires.
A focused image-only L5-S1 probe also concluded the target pattern was absent after reviewing
the full lower axial stacks and produced one local proof image outside the repo.

Likely files: `backend/services/agent_runner.py`, `backend/services/evidence_pack.py`, future
clinical-context/reconciliation or adjudication workflow.

Proposed fix: Do not mark this issue fixed. The app should not be forced to assert the reference
finding when both blind and focused image-only agent passes conclude otherwise. This needs external
adjudication: either a radiologist/human review of the image/reference discrepancy, or a deliberate
mode that ingests the reference/prior report as clinical context and labels the output as
reference-assisted reconciliation rather than a blind image read.

Verification command/test:

- Full first rerun: `python %LOCALAPPDATA%\MIKA\validation\run4_agent\run4_real_agent_validation.py`
- Patched rerun: `python %LOCALAPPDATA%\MIKA\validation\run4_agent\run4_patched_feb_validation.py`
- Focused image-only probe: `python %LOCALAPPDATA%\MIKA\validation\run4_agent\run4_focused_l5s1_probe.py`
- Unit guards: `pytest -q tests/test_run2_evidence.py::test_evidence_pack_prioritizes_matched_axial_contrast_pairs_within_cap tests/test_run3_validation_harness.py::test_spine_agent_prompt_requires_post_surgical_contrast_checklist`

### P1 - Axial Contrast Evidence Was Too Sparse For The Failed Blind Spot

Expected result: Contrast lumbar MR evidence should preserve the 80-image cap while giving matched
pre/post axial contrast stacks enough coverage for lateral recess, foraminal, and nerve-root review.

Actual result: The first Run 4 February rerun used sparse and mismatched axial VIBE sampling. This
did not fix the P0 and made the lower-lumbar contrast blind spot too easy to miss.

Fix implemented: DICOM evidence allocation now weights axial MR, fat-suppressed, post-contrast, and
lumbar series more heavily and aligns same-size axial pre/post contrast siblings to matching slice
indices. The patched live February rerun used matched 19-slice pre/post axial VIBE coverage while
remaining capped at 80 selected evidence images.

Likely files: `backend/services/evidence_pack.py`.

Verification command/test: `pytest -q tests/test_run2_evidence.py::test_evidence_pack_prioritizes_matched_axial_contrast_pairs_within_cap`.

### P1 - JPG Patient Copy Used Technical Calibration Wording

Expected result: JPG-export reports should be honest that exact measurements are limited, but patient
mode should use plain language rather than DICOM/calibration jargon. Clinician mode may keep technical
calibration terms.

Actual result: June and September patient copy included wording such as uncalibrated/export
calibration language. The evidence contract itself was correct: image-export input, calibration false,
no precise measurements, and no uncalibrated pinpoint markers.

Fix implemented: Patient summary normalization now rewrites calibration jargon into plain export/scale
language while preserving clinician technical fields unchanged.

Likely files: `backend/services/agent_runner.py`.

Verification command/test: `pytest -q tests/test_run3_validation_harness.py::test_patient_summary_uses_plain_export_limitations_without_changing_clinician_terms`.

### P2 - June Incidental Figure Suppressed By Artifact QA

Expected result: Every patient proof figure should be backed by trusted evidence refs, or the proof
image/body marker should be suppressed.

Actual result: June artifact QA marked one incidental figure limited because it had no evidence ref.
The patient proof image and body-map marker were suppressed for that finding; other proof images
remained trusted.

Likely files: `backend/services/artifacts.py`, future incidental-reporting policy.

Proposed fix: No Run 4 code change. The existing gate behaved safely. Future work can decide whether
to remove non-evidence-linked incidental patient findings entirely.

Verification command/test: full June rerun plus artifact QA JSON outside the repo.

### P2 - JPG Anatomy Remains Unknown Before Model Read

Expected result: Safe lumbar/spine anatomy detection for JPG exports when metadata supports it.

Actual result: Both JPG studies remained `unknown` before the model read, while correctly marked as
MR image exports and uncalibrated.

Likely files: future image-export metadata/OCR/CV layer.

Proposed fix: Deferred. Do not infer anatomy from screenshots without a validated metadata/OCR/CV
step.

Verification command/test: future synthetic screenshot/sidecar test, not real PHI images.

## Passed Checks

- Subscription Claude CLI preflight was ready and `uses_api_key` was false for all real runs.
- JPG studies were handled as uncalibrated image exports, with precise measurement and pinpoint
  marker limitations.
- X-ray study was handled as DICOM DX. The report used radiograph/projection wording and did not
  use MRI sequence wording.
- Patient mode had bottom line, key points, patient findings, and explanation text.
- Clinician mode had technical findings, tiers, evidence refs, and impressions.
- Patient PDF and clinical PDF routes worked for completed persisted jobs.
- Completed reports persisted after clearing live jobs and appeared in recent studies.
- Artifact QA passed for the patched February, September, and X-ray jobs; June safely suppressed
  one non-evidence-linked incidental proof image.

## Screenshots / Visual QA

No screenshots or medical images are embedded in this report. Local proof images were inspected
textually through artifact QA metadata and local-only image files. The February focused probe
generated one local proof PNG outside the repo; it is not copied here.

## Verification Commands

- `pytest -q`
- `python -m py_compile backend/services/agent_runner.py backend/services/evidence_pack.py`
- `git diff --check`
- `git diff --cached --check`
- Browser smoke on a persisted completed job with `MIKA_DATA_DIR` pointing at the Run 4 validation app-data directory.

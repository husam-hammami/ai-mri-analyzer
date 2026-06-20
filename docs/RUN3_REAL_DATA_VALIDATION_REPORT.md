# Run 3 Real-Data Validation Report

Validation date: 2026-06-20

Scope: Run 2 evidence pipeline validation on local lumbar studies. Generated medical outputs,
derived evidence images, raw reports, and PDFs were kept outside the repository. This report is
PHI-safe: it contains no patient identifiers and no embedded medical images.

## Method

- Re-read Run 2 EvidencePack, BatchSender, Artifact QA, app integration, and validation harness.
- Ran the PHI-safe evidence harness on all four local study folders.
- Ran an isolated app upload/inventory/evidence pass on all four folders with app data outside the repo.
- Ran the subscription Claude CLI agent path on the February 2026 DICOM MRI. The CLI preflight was connected, used the subscription login, and did not use API-key fallback.
- Started one pre-fix image-export agent run for the June 2025 JPG export, then stopped the stale runner after the image-export evidence bug was proven and fixed, to avoid spending more subscription time validating old code.

## Study Metrics

| Study | App modality/anatomy | Images found | Selected evidence | Calibration | Localizers/low-value exclusions | Agent/final status |
|---|---:|---:|---:|---|---:|---|
| Feb 2026 contrast lumbar MRI DICOM | MR / spine | 362 | 80 | Calibrated DICOM, PixelSpacing present | 8 localizers excluded | Complete; artifact QA passed; patient + clinical PDFs available; persisted reload passed |
| Jun 2025 post-surgery MRI JPG export | MR / anatomy unknown before model | 168 | 80 | Uncalibrated image export | 0 | Pre-fix agent run started; stopped after stale image-export evidence behavior was proven |
| Sep 2025 pre-surgery MRI JPG export | MR / anatomy unknown before model | 317 | 80 | Uncalibrated image export | 0 | Evidence/app path validated; full post-fix agent rerun deferred |
| Mar 2026 lumbar X-rays | DX / spine | 4 | 4 | Calibrated DICOM radiographs, PixelSpacing present | 0 | Evidence/app path validated; full agent run deferred |

## Findings

### P0 - Post-Surgical L5-S1 Pattern Missed On DICOM MRI

Expected result: The February DICOM MRI read should explicitly assess post-surgical anatomy, the left lateral recess, enhancing epidural scar/fibrosis versus residual or recurrent disc material, descending nerve-root encasement/impingement, side, level, confidence tier, and evidence refs.

Actual result: The completed agent report described lower lumbar degenerative/facet changes and stated no abnormal enhancement or nerve-root compression. Compared with the supplied reference report, this missed the post-surgical left L5-S1 lateral recess/enhancing soft-tissue and residual/recurrent disc/root-impingement pattern.

Likely files: `backend/services/agent_runner.py`, vendored spine prompt behavior.

Proposed fix: Add a mandatory lumbar post-surgical contrast checklist to the spine agent prompt: inspect for hemilaminectomy/laminotomy/discectomy anatomy, distinguish enhancing epidural fibrosis/scar from non-enhancing recurrent disc, assess descending S1/L5 root encasement/displacement/impingement, and forbid “no abnormal enhancement” until operative bed, lateral recesses, foramina, and nerve roots are compared on pre/post fat-saturated images.

Verification test: `tests/test_run3_validation_harness.py::test_spine_agent_prompt_requires_post_surgical_contrast_checklist`.

Status: Fixed at prompt-contract level. A future full subscription rerun on this study should verify clinical output improvement.

### P1 - JPG Evidence Used Synthetic Converted DICOM Series Instead Of Original Upload Order

Expected result: JPG/PNG exports should be treated as uncalibrated image exports and preserve original folder/order context. They should not become one synthetic evidence series per converted DICOM screenshot.

Actual result: The real app path converted each JPG to DICOM for downstream compatibility, then EvidencePack chose those converted DICOM files first. The June and September JPG studies were therefore represented as 168 and 317 separate synthetic series before the fix.

Likely files: `backend/app.py`, `backend/services/evidence_pack.py`.

Proposed fix: When original upload images are present, build EvidencePack from the original upload folder, then carry safe inventory labels into the manifest. Keep converted DICOMs available for existing downstream processing, but do not use them as the evidence-pack source for image exports.

Verification test: `tests/test_run3_validation_harness.py::test_prepare_evidence_pack_uses_original_upload_for_image_exports`.

Status: Fixed. Post-fix real app evidence pass produced one ordered image-export series for both JPG MRI folders, selected 80 images, and kept calibration false.

### P1 - Patient Proof Images Suppressed Despite Valid Technical Evidence

Expected result: If a patient finding references a figure that has a trusted evidence-linked technical finding, the patient proof image should remain available. A precise body-map marker should still be suppressed unless location evidence exists.

Actual result: In the completed February DICOM report, all technical findings had valid `evidence_refs` and artifact QA passed, but patient findings omitted raw evidence IDs. The artifact gate suppressed every patient proof image and marked proof trust false.

Likely files: `backend/services/artifacts.py`, frontend trust contract.

Proposed fix: In ArtifactQaGate, if a patient finding lacks evidence refs but its figure maps to a trusted registry artifact, inherit the artifact evidence refs for proof-image trust. Continue suppressing body-map markers when no location evidence exists.

Verification test: `tests/test_run3_validation_harness.py::test_artifact_gate_inherits_figure_evidence_for_patient_proof`.

Status: Fixed.

### P1 - X-Ray Validation Harness Expected Wrong Input Type

Expected result: The March 2026 X-ray folder should validate as DICOM radiograph data when DICOM tags are present. DICOM `DX` with PixelSpacing should not be forced into the JPG-export/uncalibrated path.

Actual result: The Run 2 harness expected the X-ray folder to be an image export (`OT`, uncalibrated), but the real folder is DICOM `DX`, spine, PixelSpacing present, four images selected.

Likely files: `backend/validation/run_local_evidence_validation.py`.

Proposed fix: Update the local validation case expectation to DICOM `DX`, calibrated true for this dataset.

Verification test: `tests/test_run3_validation_harness.py::test_xray_validation_case_matches_real_dicom_dx_shape` and `tests/test_run3_validation_harness.py::test_xray_dicom_dx_manifest_passes_validation_expectations`.

Status: Fixed.

### P2 - JPG Anatomy Detection Remains Unknown Before Model Read

Expected result: The app should surface the JPG lumbar MRI studies as spine/lumbar when safe evidence supports that.

Actual result: The upload/conversion path detects MR modality for the JPG MRI exports, but anatomy remains `unknown` before the model read because reliable anatomy detection from screenshots is not implemented.

Likely files: `backend/core/dicom_engine.py`, `backend/core/format_converter.py`, future image-export metadata/OCR/CV layer.

Proposed fix: Defer. A robust fix likely needs a dedicated image-export metadata/OCR/CV step, which is outside this run.

Verification test: Future image-export anatomy test using synthetic non-PHI screenshots or metadata sidecars.

Status: Not fixed in Run 3.

## Report And Persistence Checks

- February DICOM: patient PDF available, clinical PDF available, report persisted, and reload after clearing live jobs succeeded.
- Artifact QA: February DICOM passed for six generated proof figures.
- Automated image safety: artifact QA found no missing, outside-workdir, unreadable, blank, or tiny proof images in the completed DICOM job.
- Patient/clinician contract: patient proof-image trust failure was found and fixed; clinician evidence refs were present.

## Notes

- No public datasets were used.
- No API-key fallback was used.
- No medical images, PDFs, raw reports, generated medical outputs, or validation artifacts were copied into this repository.

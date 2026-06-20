# MIKA Locked Hybrid Architecture Plan

## Summary
MIKA should be built as a desktop-only patient assistant that uses the user's own Claude subscription, requires no terminal use, and improves accuracy through an evidence-first architecture.

Locked pipeline:

```text
Guided Claude sign-in
-> upload/import
-> StudyGraph inventory
-> CV/measurement evidence engine
-> artifact + annotation QA
-> Claude first read
-> focused verifier
-> normalized report contract
-> patient/clinician PDFs + UI
-> real-data regression gates
```

Claude remains the reasoning and writing layer. CV/evidence logic becomes the detection, measurement, artifact, and verification backbone.

This is not a regulated diagnostic product in v1. The first production target is a patient-facing assistant that explains imaging findings, flags concerns, prepares doctor questions, and clearly states uncertainty.

## Run 1 - Stability + Zero-Terminal Claude Login
- Replace fire-and-forget Claude login with an in-app auth session.
- Support browser sign-in, polling, retry, cancel, and pasted-code fallback inside MIKA.
- Add a cheap Claude readiness probe separate from full `opus/high` reads.
- Clear stale auth errors after successful sign-in or rerun.
- Stabilize report persistence, clinical PDF download, recent studies, and normalized patient/clinician output.
- Keep the default path on the user's Claude subscription. Do not make API-key mode the normal path.

## Run 2 - Artifact + Annotation QA Layer
- Add an `ArtifactRegistry` for every generated visual:
  - body map
  - proof image
  - annotated slice
  - comparison panel
  - report figure
  - PDF figure
- Each artifact records source, linked finding id, anatomy, level, side, modality, sequence/view, calibration state, marker type, and QA status.
- Add an `ArtifactQaGate` before final report persistence:
  - no cropped labels/arrows
  - no text overlapping anatomy
  - no blank or unreadable proof images
  - no pinpoint marker on uncalibrated image exports
  - no body-map pin unless the anatomy landmark map is approved
  - no final confirmed visual claim if artifact QA failed
- Body-map markers are navigation aids, not clinical proof. Proof images carry clinical evidence.
- If artifact QA fails, hide or downgrade the artifact and surface a clear limitation.

## Run 3 - Desktop UI/UX Cleanup
- Desktop-only. No mobile scope.
- No main-page scroll on the read screen.
- Selected finding must be fully readable.
- Proof images must preserve the full annotated image with labels/arrows visible.
- Body-map illustrations should use per-anatomy sizing presets to reduce empty voids, without distorting marker geometry.
- Patient mode explains meaning and next questions.
- Clinician mode stays technical and evidence-focused.

## Run 4 - Evidence Engine Foundation
- Add `StudyGraph`: canonical model for study, series, slices, modality, plane, contrast, source type, and calibration.
- Add `EvidencePack`: selected slices, measurements, candidate findings, limitations, artifact references, and confidence caps.
- Add sequence/view classification for lumbar MRI, contrast MRI, image-export MRI, and lumbar X-ray.
- Enforce no precise measurements or Tier A confidence on uncalibrated image exports.

## Run 5 - Targeted CV/Medical Logic
- Lumbar MRI:
  - L4-L5 and L5-S1 level tracking
  - left/right tracking
  - canal, foraminal, and disc evidence
  - post-op scar vs residual/recurrent disc concern
  - left S1 root contact, displacement, or encasement
- Lumbar X-ray:
  - AP/lateral/flexion-extension view detection
  - levoscoliosis/dextroscoliosis
  - alignment/spondylolisthesis
  - disc-space narrowing at L4-L5 and L5-S1
  - instability if flexion/extension views exist
- Add CLI-based focused verifier passes before final report persistence.

## Run 6 - Wording and Report Quality Gate
- Add patient-copy rules:
  - one clear bottom-line sentence
  - short key points
  - what this may mean
  - what to ask the doctor
  - no tier letters, sequence jargon, pixel language, or false certainty
- Add clinician-copy rules:
  - technical findings
  - level/side/modality evidence
  - limitations
  - discrepancy/reconciliation section when prior reports exist
- Patient PDF and clinician PDF must be generated from the same normalized report contract.

## Run 7 - Real-Data Regression + Hardening
- Rerun the private Batch 1 studies through the real app path.
- Compare output against private reference reports/surgical context outside the repo.
- Verify UI, annotations, arrows, markers, artifacts, patient PDF, clinician PDF, and persistence after restart.
- Produce a private regression report with expected result, actual result, screenshot, severity, likely cause, and fix status.

## Public Interfaces and Contracts
- Keep existing app endpoints where possible, but make their response shape stable.
- Add normalized report fields:
  - `study`
  - `patient`
  - `clinician`
  - `findings`
  - `confidence`
  - `assets`
  - `verification`
- Add clean status/error fields:
  - `error_code`
  - `error_message`
  - `auth_state`
  - `progress_phase`
- Persist `verification.json`, evidence metadata, patient PDF, clinician PDF, and normalized `report.json`.

## Test Plan
- Unit tests:
  - report normalization
  - PDF routing
  - auth recovery
  - image-export calibration caps
  - X-ray protocol
  - verifier merge
  - artifact registry
  - artifact QA failure handling
- Visual tests:
  - no page scrollbar
  - selected finding not clipped
  - proof image visible
  - labels/arrows in bounds
  - no blank generated artifact
  - body-map pins suppressed when unapproved
- Auth tests:
  - Claude CLI missing
  - already signed in
  - browser login success
  - pasted-code fallback
  - expired auth during analysis
  - successful rerun after auth recovery
- Real-data regression remains private and references files outside the repo.

## Assumptions
- Auth model is user subscription only.
- Target is patient assistant, not regulated diagnostic product.
- No mobile work.
- No EXE packaging until these runs pass.
- No medical files or private reports are committed.
- Custom trained ML is deferred until validation data, labels, and metrics exist.

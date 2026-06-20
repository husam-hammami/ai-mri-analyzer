# Run 8 CV Pipeline Integration Report

Date: 2026-06-21
Baseline: `1b1fab9 add lumbar contrast evidence engine`

## What Changed

- Wired deterministic lumbar CV candidates into the EvidencePack manifest.
- Added `cv_candidates`, `cv_candidate_limitations`, and `cv_candidate_policy` to the manifest.
- Added a separate `CV evidence candidates` prompt section for the Claude CLI agent.
- Required Claude to return `cv_candidate_reviews` with one status per candidate:
  `supported`, `not_supported`, `cannot_assess`, or `localization_wrong`.
- Updated the verifier prompt and parser to preserve `cv_candidate_reviews`.
- Added report contract fields:
  - `cv_candidates`
  - `cv_candidate_reviews`
  - `clinician.cv_candidate_reviews`
  - `assets.cv_candidates`
- Kept patient findings and clinician findings separate from CV candidate rows.
- Added a clinician-only UI card for CV candidate review rows.
- Kept reference-assisted reconciliation separate from the blind read and from CV candidate review.

## February Validation

Validation used the local February contrast lumbar MRI DICOM study outside the repository through the subscription Claude CLI `AgentRunner` path. No API-key fallback was used.

Expected:

- The February left L5-S1 candidate should be included in the agent input.
- Claude should return a candidate status row.
- The final report should not turn deterministic CV localization into an unsupported confirmed finding.
- Marker/proof-overlay trust should stay suppressed when geometry confidence is below the marker threshold.

Actual:

- EvidencePack selected 80 evidence images from 362 DICOM images.
- EvidencePack included 1 CV candidate:
  - left L5-S1 pre/post contrast lateral-recess ROI
- The Claude CLI run completed successfully.
- `summary.json` included 1 candidate review row.
- Candidate status result: `supported`.
- The candidate remained in `cv_candidate_reviews`; it was not inserted into patient findings by deterministic code.
- Candidate marker/proof trust remained false:
  - body marker: false
  - proof overlay: false
  - pinpoint marker: false

## Final Finding Behavior

No backend code overwrites blind findings with CV candidates. The integration passes candidates as localization evidence and preserves Claude's separate candidate status. Any final finding still must come from Claude image review and evidence refs, not from the deterministic CV module alone.

Reference-assisted reconciliation remains separate and unchanged.

## Limitations

- The current candidate is still localization-only; it does not classify scar, recurrent disc, or nerve-root encasement.
- Geometry confidence for the February L5-S1 candidate remains below the marker/proof threshold, so candidate-only markers stay suppressed.
- The verifier contract is implemented and tested, but the primary app path still relies on the Claude CLI agent to write `cv_candidate_reviews` into `summary.json`.
- Image-export/JPG studies remain uncalibrated and do not receive CV candidates.

## Verification Commands

Focused Run 8 tests:

```powershell
python -m pytest -q tests\test_run8_cv_pipeline.py
```

Full test suite:

```powershell
python -m pytest -q
```

Compile changed backend modules:

```powershell
python -m py_compile backend\app.py backend\services\evidence_pack.py backend\services\agent_runner.py backend\services\verification.py
```

Diff checks:

```powershell
git diff --check
git diff --cached --check
```

HTTP smoke:

```powershell
python <synthetic ASGI report smoke>
```

## PHI Safety

No medical images, PDFs, raw reports, screenshots, generated PHI, raw validation outputs, or real-study artifacts were copied into the repository or committed. This report contains only PHI-safe counts, statuses, and contract behavior.

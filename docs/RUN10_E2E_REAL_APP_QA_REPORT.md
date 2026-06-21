# Run 10 E2E Real App QA Report

Date: 2026-06-21
Baseline: `fdcea4c synthesize supported cv evidence into reports`

## Run Summary

The full February lumbar MRI workflow was run through the real app server on a local-only desktop port with a fresh app-data validation directory outside the repository.

Workflow steps completed:

- Uploaded/imported 362 DICOM files through `/api/upload`.
- Started agent-mode analysis through `/api/analyze`.
- Built EvidencePack with 80 selected evidence images.
- Generated the deterministic lumbar CV candidate.
- Ran the Claude CLI subscription blind read path.
- Returned a `cv_candidate_reviews` row.
- Applied reference-assisted reconciliation using the local reference PDF.
- Generated patient and clinical PDFs.
- Persisted the completed report and reloaded it after backend restart.

No medical images, PDFs, screenshots, raw reports, generated PHI, or raw validation outputs were copied into the repository.

## Backend Output Checks

Expected:

- `cv_candidates` contains the left L5-S1 candidate.
- `cv_candidate_reviews` contains a status row.
- Supported candidates synthesize into patient and clinician focused-evidence fields.
- Unsupported candidates do not synthesize into final report additions.
- Blind findings remain preserved.
- Reference reconciliation only moves away from pure conflict when focused evidence is supported.
- Marker/proof/pinpoint trust remains false unless thresholds pass.

Actual:

- `cv_candidates`: 1.
- Candidate location: left L5-S1.
- `cv_candidate_reviews`: 1 row.
- Candidate review status on this fresh rerun: `not_supported`.
- `patient.cv_supported_explanations`: 0, as expected for `not_supported`.
- `clinician.cv_supported_findings`: 0, as expected for `not_supported`.
- Reference reconciliation statuses remained `conflicts_with_reference`.
- Blind findings remained present and were not overwritten.
- No unsupported CV-derived confirmed finding was created.
- Marker/proof/pinpoint trust did not leak into candidate-only evidence.
- Patient PDF route returned 200.
- Clinical PDF route returned 200.
- After backend restart, report, recent studies, patient PDF, and clinical PDF routes still returned successfully.

## UI Checks

Browser path:

- In-app Browser bootstrap failed with a local asset-path initialization error before browser controls became available.
- Playwright with local Chrome was used as the documented fallback.

Desktop viewport:

- Completed job loaded successfully.
- No resume error appeared.
- Report controls were visible.
- `Compare with my report` controls were visible.
- Patient/clinician toggle worked.
- Patient mode showed the reference-assisted review.
- Patient mode did not show a focused-evidence section because the candidate review was `not_supported`.
- Patient mode did not expose raw internal terms such as `cv_candidate`, `ROI`, `PixelSpacing`, or registration/geometry field names.
- No confusing patient-mode contradiction was detected.
- Clinician mode showed raw CV candidate review rows.
- Clinician mode showed the candidate status as `not supported`.
- Clinician mode showed reference-assisted reconciliation.
- No horizontal page overflow was detected at the tested desktop viewport.

## PDF Checks

- Patient PDF included the reference-assisted review section.
- Patient PDF did not include raw internal CV terms.
- Patient PDF did not include a focused-evidence section because no candidate was supported on this rerun.
- Clinical PDF included the reference-assisted reconciliation section.
- Clinical PDF did not include a CV-supported table because no supported CV evidence row existed on this rerun.
- Both PDF routes returned 200 before and after backend restart.

## Issues Found And Fixed

No code issue was proven during this QA pass.

The main behavioral difference from Run 9 is that the fresh Claude CLI rerun returned `not_supported` for the CV candidate instead of `supported`. The app handled that correctly by preserving the blind read and reference discrepancy, withholding focused-evidence synthesis, and avoiding unsupported markers or confirmed findings.

No code changes were made in this run.

## Issues Deferred

- Candidate review status can vary between full Claude reruns. A future validation run may compare repeated candidate-review stability, but Run 10 did not broaden CV architecture.
- The in-app Browser surface could not be used because its local browser bridge failed to initialize; Playwright fallback completed the desktop UI smoke.

## Verification Commands

Full app workflow:

```powershell
python <upload February DICOMs through /api/upload>
python <start /api/analyze in agent mode with reference PDF>
python <poll /api/status/{job_id} until complete>
```

Backend contract checks:

```powershell
python <inspect /api/report/{job_id} structured fields>
```

Persistence and PDF checks:

```powershell
python <restart backend and check /api/report, /api/reports, /pdf, /clinical-pdf>
```

Desktop UI fallback smoke:

```powershell
python <Playwright desktop smoke against http://127.0.0.1:8031/?job_id={job_id}>
```

Regression suite:

```powershell
python -m pytest -q
```

Compile check:

```powershell
python -m py_compile backend\app.py backend\services\cv_synthesis.py backend\services\reconciliation.py backend\services\report_builder.py
```

Diff checks:

```powershell
git diff --check
git diff --cached --check
```

## PHI Safety

The report intentionally contains only counts, statuses, route outcomes, and UI behavior. It does not embed or quote medical images, PDFs, screenshots, raw report text, generated PHI, or raw validation outputs.

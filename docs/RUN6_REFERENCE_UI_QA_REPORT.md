# Run 6 Reference UI QA Report

Date: 2026-06-21
Baseline: `1e8a8df add reference assisted discrepancy workflow`

## What Changed

- Added a desktop completed-read panel for `Compare with my report`.
- The panel supports selecting a local PDF/text report or pasting report text.
- Added progress, success, error, retry, cancel, and clear states.
- Added a browser-friendly multipart endpoint at `/api/reconcile/upload`.
- Kept blind findings, uploaded-report targets, and reconciliation rows separate in the UI and PDFs.
- Updated patient wording to avoid raw statuses and use plain language: MIKA's independent read may differ from the uploaded report, and both should be reviewed with a radiologist or clinician.
- Updated clinician rows to show the reference target, MIKA blind finding, level/side, evidence refs, agreement status, needed sequences, and discrepancy note.
- Recent studies now surfaces when a report has reference-assisted review.

## Browser/UI Verification

Test data:
PHI-safe synthetic completed lumbar MRI report in a temporary app data directory.

Result:
- Completed persisted job loaded at desktop viewport `1440x950`.
- `Compare with my report` controls were visible.
- Pasted report text successfully triggered reconciliation from the UI.
- Patient view showed the reference-assisted review and the plain-language discrepancy wording.
- Clinician toggle worked.
- Clinician view showed technical reconciliation rows with agreement status, evidence refs, needed sequences, and discrepancy note.
- No body-map marker appeared for reference-only reconciliation rows.
- No page-level scroll overflow was detected at the tested desktop viewport.
- Reconciliation chips and badges were not cramped.
- Patient and clinical report controls remained visible.

Browser note:
The Codex in-app Browser helper was blocked by the Windows sandbox (`CreateProcessAsUserW` access denied). Headless Playwright against the actual localhost app was used as the browser smoke fallback.

## PDF Verification

Automated regression tests verified:
- Patient PDF contains a separate `Reference-assisted review` section.
- Patient PDF labels uploaded-report content separately from `MIKA independent read`.
- Clinical PDF contains a technical `Reference-assisted reconciliation` table.
- Clinical PDF keeps blind findings separate from report-derived reference targets.
- PDF routes returned HTTP 200 before and after backend restart.

## Persistence Verification

Result:
- Completed report persisted reconciliation after refresh/reload.
- Backend restart preserved reconciliation rows.
- Recent studies returned `reference_reconciliation_available=true`.
- Patient PDF and clinical PDF routes returned HTTP 200 after restart.

## Remaining Limitations

- The reference report extractor remains best-effort for scanned/image-only PDFs; users can paste text if PDF text extraction fails.
- The reconciliation engine does not decide whether MIKA or the uploaded report is clinically correct.
- The UI flow was verified on desktop only, per Run 6 scope.

## Commands Run

- `python -m pytest -q tests/test_run5_reconciliation.py tests/test_run6_reference_ui.py`
- `python -m pytest -q`
- `python -m py_compile backend\app.py backend\services\reconciliation.py backend\services\report_builder.py`
- Headless Playwright smoke against `http://127.0.0.1:8026/?job_id=abc12345`
- HTTP persistence/PDF checks against:
  - `/api/report/abc12345`
  - `/api/reports`
  - `/api/report/abc12345/pdf`
  - `/api/report/abc12345/clinical-pdf`

## PHI Safety

No medical images, PDFs, raw reports, screenshots, generated PHI, or raw validation outputs were copied into the repository. UI smoke used synthetic non-PHI content in an untracked temporary data directory.

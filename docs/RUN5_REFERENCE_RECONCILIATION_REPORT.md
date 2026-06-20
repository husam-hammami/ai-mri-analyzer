# Run 5 Reference-Assisted Reconciliation Report

Date: 2026-06-20
Baseline: `9ad8734 prove post-fix real data reads`

## What Changed

- Added a separate reference-assisted reconciliation contract with reference target, blind MIKA finding, anatomy, level, side, needed modality/sequence, evidence refs, agreement status, patient explanation, and clinician explanation.
- Added a reference-assisted backend path that reads a local reference PDF or pasted text, extracts structured targets, compares them against the blind structured read, and stores the result without overwriting blind findings.
- Added `/api/reconcile` so a completed blind read can receive a separate reconciliation section without rerunning or changing the blind read.
- Updated durable report payloads, `report.json`, `meta.json`, recent studies, and reload normalization to preserve reconciliation results.
- Updated patient and clinician PDFs so reference-derived content is clearly separate from MIKA's blind image read.
- Updated the UI to show a `Reference-assisted review` state and separate agreement/disagreement rows without adding body-map markers.

## February P0 Handling

Expected:
MIKA must not force confirmation of the reference target when the blind image read does not independently support it. The output must preserve the blind read, preserve the reference target, and flag the discrepancy clearly.

Actual:
The February reference-assisted validation extracted structured L5-S1 left-sided reference targets from the local reference PDF and compared them against the persisted blind read. The reconciliation classified the targets as `conflicts_with_reference`, set `has_discrepancy=true`, and kept patient wording plain-language.

Status:
Handled honestly. The workflow now states that the reference report describes the clinically important postoperative left L5-S1 target, while MIKA's blind image read did not independently confirm it. It does not create an unsupported confirmed finding.

## Findings

P0: Blind-read misses/negations previously looked like final MIKA output with no explicit reference discrepancy workflow.
Expected: blind read remains visible; reference target remains visible; disagreement is flagged.
Actual: separate reconciliation section reports `conflicts_with_reference`.
Likely files: `backend/services/reconciliation.py`, `backend/app.py`, `frontend/index.html`, `backend/services/report_builder.py`.
Verification: `python -m pytest -q tests/test_run5_reconciliation.py`; in-memory February reconciliation check returned `used=True`, `target_count=3`, `has_discrepancy=True`, and all reconciliation rows as `conflicts_with_reference`.

P1: Patient-facing reference text could expose technical target language if raw reference targets are shown.
Expected: patient mode explains the meaning plainly; clinician mode preserves technical detail.
Actual: patient reconciliation items use plain phrases; clinician items keep the technical target and needed sequences.
Likely files: `backend/services/reconciliation.py`, `frontend/index.html`, `backend/services/report_builder.py`.
Verification: regression test asserts patient reconciliation JSON excludes technical terms while clinician JSON preserves technical detail.

P1: Reconciliation must survive restart and keep PDF routes available.
Expected: completed reports reload reconciliation through `report.json` or the reconciliation manifest; patient and clinician PDF routes remain available.
Actual: persisted synthetic completed jobs reload reconciliation and both PDF routes return 200.
Likely files: `backend/app.py`, `backend/services/report_builder.py`, `backend/services/reconciliation.py`.
Verification: `test_persisted_reconciliation_reloads_and_recent_studies_flag_it`; `test_pdf_routes_available_after_persisted_reconciliation_reload`.

P2: Reference-only discrepancy rows must not create body-map markers.
Expected: body markers remain tied only to trusted blind-image findings.
Actual: reconciliation is not inserted into `patient.findings` or `vm.findings`.
Likely files: `backend/app.py`, `frontend/index.html`.
Verification: `test_reconciliation_does_not_create_body_marker_findings`.

## Expected vs Actual

Expected:
- Ingest a local reference report as context.
- Extract structured target findings.
- Compare targets against MIKA blind findings.
- Keep blind and reference-assisted sections visually and contractually separate.
- Flag the February postoperative L5-S1 discrepancy without unsupported confirmation.
- Persist and reload reconciliation.

Actual:
- Local PDF extraction succeeded in the February validation path.
- Structured target extraction identified left L5-S1 reference targets.
- Agreement status was discrepancy-oriented, not confirmation-oriented.
- UI/PDF data contracts separate `findings` from `reconciliation`.
- Persistence and PDF route behavior are covered by regression tests.
- Route smoke on the completed February persisted job showed `report_recon=True`, 3 patient reconciliation rows, 3 clinician reconciliation rows, recent-studies reconciliation flag `True`, patient PDF HTTP 200, and clinical PDF HTTP 200.

## Remaining Limitations

- PDF text extraction is best-effort when no dedicated PDF text library is installed. The implementation uses PyMuPDF or pypdf when available, with a simple text-layer fallback.
- Reconciliation target extraction is currently optimized for the Run 5 lumbar postoperative discrepancy pattern and generic spine-level statements. Broader anatomy-specific extraction should be validated before expanding clinical claims.
- Reconciliation does not adjudicate which source is clinically correct. It only reports agreement, disagreement, or uncertainty and recommends clinician review.

## Verification Commands

- `python -m pytest -q tests/test_run5_reconciliation.py`
- `python -m pytest -q`
- `python -m py_compile backend\app.py backend\services\reconciliation.py backend\services\report_builder.py`
- `git diff --check`
- `git diff --cached --check`
- Local route smoke on `127.0.0.1:8015` for the completed persisted February job: `/api/reconcile`, `/api/report/{job_id}`, `/api/reports`, `/api/report/{job_id}/pdf`, and `/api/report/{job_id}/clinical-pdf`.

Note: in-app Browser automation could not be completed because the browser helper process was blocked by the sandbox (`CreateProcessAsUserW` access denied). HTTP route smoke was completed against the current backend instead.

## PHI Safety

No medical images, screenshots, raw reports, generated reports, PDFs, or raw validation outputs were copied into this repository. This report contains only PHI-safe structured outcomes.

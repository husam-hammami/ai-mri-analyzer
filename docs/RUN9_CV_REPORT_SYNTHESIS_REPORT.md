# Run 9 CV Report Synthesis Report

Date: 2026-06-21
Baseline: `e3431ee wire cv candidates into read pipeline`

## What Changed

- Added a CV-supported report synthesis layer for `cv_candidate_reviews`.
- Kept original blind findings unchanged.
- Added separate derived report fields:
  - `patient.cv_supported_explanations`
  - `clinician.cv_supported_findings`
  - `assets.cv_candidate_reviews`
  - `assets.cv_supported_findings`
  - `confidence.cv_candidate_policy`
- Added patient PDF rendering for a plain-language `Focused evidence review` section.
- Added clinical PDF rendering for a technical `CV-supported focused evidence` table.
- Updated desktop UI to show:
  - patient `Focused evidence review`
  - clinician `CV-supported focused evidence`
  - raw `CV evidence candidates` separately
- Updated reference-assisted reconciliation so a matching supported focused-evidence row can move a reference target away from a pure conflict while preserving the earlier blind-read disagreement.

## February Validation

Validation used the previous local February DICOM Claude CLI subscription output from Run 8, then applied the Run 9 synthesis and reference reconciliation path outside the repository. No API-key fallback was used. No medical images, PDFs, screenshots, raw reports, or generated raw outputs were copied into the repository.

Expected:

- The CV candidate review is available with status `supported`.
- The final report includes a safe clinician CV-supported row.
- The patient report includes a plain focused-evidence explanation.
- Reference-assisted reconciliation no longer treats the matching target as a pure conflict.
- No unsupported body-map marker, proof overlay, or pinpoint marker is created.

Actual:

- CV candidate count: 1.
- Candidate review statuses: `supported`.
- Clinician CV-supported rows: 1.
- Patient focused-evidence explanations: 1.
- Matching reference target status changed to `partially_supported`.
- Focused-evidence reconciliation flag: true.
- Patient PDF available: true.
- Clinical PDF available: true.
- Marker/proof/pinpoint trust stayed false for the candidate-supported row.

## What Was Not Claimed

- Deterministic CV did not create a confirmed finding by itself.
- Blind findings were not overwritten.
- Candidate-only evidence did not create body-map markers, pinpoint markers, or proof overlays.
- The focused candidate did not classify scar versus residual/recurrent disc.
- The focused candidate did not independently establish nerve-root encasement.
- Reference-assisted reconciliation did not claim full agreement where the focused evidence only supported the location/region.

## Remaining Limitations

- The synthesis layer depends on Claude/verifier candidate review status. Unsupported, cannot-assess, or localization-wrong rows remain excluded.
- The current deterministic lumbar candidate remains localization-only.
- The matching reference target was upgraded to `partially_supported`, not fully confirmed, because pathology details still require clinical/radiologist review.
- Browser visual verification was not required beyond HTTP/PDF route smoke for this backend/report contract change.

## Verification Commands

Focused Run 9 tests:

```powershell
python -m pytest -q tests\test_run9_cv_synthesis.py
```

Full test suite:

```powershell
python -m pytest -q
```

Compile changed backend modules:

```powershell
python -m py_compile backend\app.py backend\services\cv_synthesis.py backend\services\reconciliation.py backend\services\report_builder.py
```

Diff checks:

```powershell
git diff --check
git diff --cached --check
```

HTTP/PDF smoke:

```powershell
python <synthetic persisted report and PDF route smoke>
```

Real-study synthesis validation:

```powershell
python <local Run 8 supported-candidate reuse plus Run 9 synthesis check>
```

## PHI Safety

No medical images, PDFs, screenshots, raw reports, generated PHI, raw validation outputs, or real-study artifacts were copied into this repository or committed. This report contains only PHI-safe counts, statuses, and contract behavior.

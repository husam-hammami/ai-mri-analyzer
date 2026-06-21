# Run 11 Candidate Stability and Adjudication Report

Baseline: `7565b95 qa focused evidence workflow end to end`

## Summary

Run 11 tested why the same deterministic lumbar CV candidate could receive different Claude review statuses across real February workflow runs. The model output remained variable, so the fix is not to force a favorable status. The app now preserves each candidate review, adds a separate adjudication contract, and only synthesizes focused patient/clinician evidence when the adjudicated final status is `supported`.

No medical images, PDFs, screenshots, raw reports, raw Claude outputs, generated PHI, or generated study artifacts were copied into the repository. Real-study artifacts and raw validation outputs stayed under the local MIKA validation directory outside the repo.

## Repeat-Review Setup

- Source path: local February DICOM workflow output from the Run 10 app validation directory outside the repository.
- Candidate: one deterministic lumbar candidate, `lumbar_l5_s1_left_prepost_lateral_recess_001`.
- Focused evidence bundle: 38 selected pre/post axial evidence images from the existing EvidencePack.
- Claude path: Claude CLI subscription login, no API-key fallback.
- Repetitions: N=3 with the same prompt, same candidate payload, and same selected evidence.

## Status Distribution

Same focused prompt and same evidence, N=3:

| Status | Count |
| --- | ---: |
| `cannot_assess` | 2 |
| `supported` | 1 |

Variant checks:

| Variant | Status |
| --- | --- |
| Internal proof panel, no reference context | `unstable` |
| Same focused evidence with PHI-safe structured reference target | `cannot_assess` |

Prior real workflow context:

- Run 8 candidate status: `supported`.
- Run 10 fresh full app rerun candidate status: `not_supported`.
- Run 11 repeated focused reviews: split, majority `cannot_assess`.

## Adjudication Result

Final adjudicated status for the repeated focused review is `cannot_assess`, with disagreement recorded. This means:

- no patient focused-evidence synthesis,
- no clinician CV-supported finding row,
- no body-map marker, pinpoint marker, or proof overlay from candidate-only evidence,
- reference-assisted reconciliation remains a discrepancy unless another adjudicated-supported candidate exists.

## Evidence Payload Determinism

The synthetic DICOM regression test confirms deterministic candidate payload generation. Real validation also found a traceability issue: the CV candidate carried StudyGraph-local series IDs while the EvidencePack selected images carried EvidencePack-local series IDs. The sequence names matched, but the generated numbering did not.

Fix added:

- future candidate manifests include `selected_evidence_refs` when candidate series names match selected EvidencePack images;
- prompt text now surfaces those selected evidence IDs;
- synthesis/adjudication can use `selected_evidence_refs` as fallback evidence refs.

## What Changed

- Added `backend/services/cv_adjudication.py` with majority, split, localization-wrong veto, and non-synthesis rules.
- Added `cv_candidate_adjudication` to report normalization, persistence, assets, and clinician contract.
- Changed synthesis so only adjudicated `supported` candidates can produce patient or clinician focused-evidence rows.
- Annotated reconciliation with unstable/cannot-assess focused review context without upgrading the reference target.
- Added `unstable` to the agent/verifier candidate status vocabulary.
- Tightened candidate prompt wording to require visible-evidence reasoning, pre/post support, level/side localization, and no broad negative statements from bounded candidate review.
- Added clinician UI and clinical PDF adjudication sections.
- Added selected-evidence ref bridging in EvidencePack for candidate traceability.

## Remaining Limitations

- The proof panel did not improve consistency in this validation; it returned `unstable`.
- The simple proof panel is internal-only and is not a trusted patient UI proof overlay.
- The candidate remains localization-only. It does not classify scar versus recurrent disc or nerve-root encasement.
- N=3 was used because each focused Claude CLI review took roughly 80-134 seconds; N=5 would increase cost/runtime without changing the observed split.
- Existing old manifests do not gain `selected_evidence_refs` until a new EvidencePack is generated.

## More ML/CV Justification

More deterministic CV is justified before any diagnostic ML classifier: stronger same-level pre/post registration views, candidate-specific selected image linkage, and QC-backed proof panels. A diagnosis classifier for scar versus recurrent disc is not justified from this run because the model disagreement is still about assessability and visual support, not only classification.

## Verification Commands

Focused regression run passed:

```powershell
$env:PYTHONPATH='backend'; python -m pytest tests/test_run8_cv_pipeline.py tests/test_run9_cv_synthesis.py tests/test_run11_cv_adjudication.py -q
```

Full verification passed:

```powershell
$env:PYTHONPATH='backend'; python -m pytest -q
python -m py_compile backend/app.py backend/services/agent_runner.py backend/services/cv_adjudication.py backend/services/cv_synthesis.py backend/services/evidence_pack.py backend/services/reconciliation.py backend/services/verification.py
git diff --check
```

HTTP/Playwright smoke passed on a synthetic completed persisted job:

- `GET /` returned 200.
- `GET /api/report/{job_id}` returned 200.
- patient PDF route returned 200.
- clinical PDF route returned 200.
- browser deep link `/?job_id={job_id}` loaded the completed report.
- report controls were visible.
- clinician mode showed `CV candidate adjudication`.
- patient mode did not expose raw internal terms checked in the smoke test.

Staged diff check passed:

```powershell
git diff --cached --check
```

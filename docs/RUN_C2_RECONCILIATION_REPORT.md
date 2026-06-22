# Run C.2 - Prior Studies, Temporal Delta, Op-vs-Read Wiring

Plan: `~/.claude/plans/so-what-s-the-plan-snappy-treasure.md`. PHI-safe: no medical files,
raw reports, screenshots, generated outputs, or study artifacts were copied into the repo.

## What changed
- Added optional `prior_studies` to `AnalyzeRequest` and threaded it into the subscription
  Claude agent path as `AgentRunner.run(prior_studies=...)`.
- Added local prior-summary lookup for prior MIKA job IDs or local report/summary JSON paths.
  This reads existing local metadata only; it does not copy prior studies.
- Added deterministic `change_over_time` extraction in `services.reconciliation`:
  prior and current findings are keyed by level + side and emitted as structured
  `new`, `resolved`, or `progressed` rows.
- Merged deterministic temporal rows into the patient `change_over_time` block so existing
  report rendering and persistence can carry them.
- Wired op-note reconciliation to the read's actual post-surgical level/side extraction before
  calling `merge_into_summary(...)`, so op-vs-read level/side discrepancies can fire.

## Expected Result
- API clients can pass prior studies without breaking existing requests.
- A prior textual report or prior persisted MIKA summary can produce structured longitudinal
  rows such as resolved/new/progressed at a specific level and side.
- Surgical note contradictions now compare against the read's surgical level/side rather than
  only within-note contradictions.

## Actual Result
- Synthetic tests confirm:
  - `prior_studies` is accepted by the API request model.
  - temporal extraction emits `resolved`, `new`, and `progressed`.
  - existing `change_over_time` points are preserved when deterministic rows are added.
  - op-vs-read extraction ignores unrelated degenerative levels and returns the post-surgical
    level/side.

## E2E Confirmation
- A full live app read with operative note + prior study was not run in this step because no
  safe, PHI-free prior-study bundle was available in-repo, and real-study artifacts must stay
  outside the repo. The code path is covered by unit tests; a live confirmation should use the
  user's local data/output directory and confirm the read is non-empty and non-zero-cost.

## Verification
- `python -m pytest -q tests\test_run_c_op_note.py tests\test_run_c2_reconciliation.py`
  - Result: 11 passed.
- `python -m py_compile backend\app.py backend\services\op_note_recon.py backend\services\reconciliation.py`
  - Result: passed.

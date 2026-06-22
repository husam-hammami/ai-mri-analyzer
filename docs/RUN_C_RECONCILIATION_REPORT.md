# Run C.1 — operative-note discrepancy engine (the reconciliation moat)

Plan: `~/.claude/plans/so-what-s-the-plan-snappy-treasure.md`. PHI-safe (raw notes not stored; only
discrepancy statements surfaced).

## Why
Reconciliation is MIKA's real differentiator and the part that was genuinely correct in March — but
it was almost entirely prompt-driven (the agent had to *notice* a contradiction). This makes the
highest-value catches **deterministic**: the operative-note discrepancies the March read surfaced
(the L4-L5 vs L5-S1 level contradiction; "complications: none" alongside a documented dural tear).

## What was built (`backend/services/op_note_recon.py`)
A pure text engine, reusing reconciliation's level/side patterns:
- `parse_operative_note()` → procedures, levels, sides, "complications: none" flag, complications
  documented in the body.
- `detect_op_note_contradictions()` — WITHIN the note: "complications: none" vs a documented
  complication; the level named on the procedure line vs the level the narrative describes.
- `reconcile_op_note_with_read()` — BETWEEN note and image read: level / side mismatch.
- `op_note_discrepancies()` / `merge_into_summary()` — public entry points; merge deduped,
  confidently-worded discrepancy statements into the read's `discrepancies` field.

## Wiring
`backend/app.py` `_run_agent_pipeline` — after the agent returns, when surgical notes are present,
`merge_into_summary(result.summary, surgical_notes)` folds the deterministic op-note discrepancies
into `summary["discrepancies"]` (wrapped in try/except so reconciliation can never break a delivered
read). Currently the WITHIN-note contradictions (not dependent on read levels); op-vs-read level/side
needs read-level extraction (Run C.2).

## Verification (run here)
- `tests/test_run_c_op_note.py` — 7 tests: parses facts; flags complications-vs-tear; flags
  procedure-line-vs-narrative level; flags op-vs-read level + side; **no false positives on a clean
  note**; merge is deduped/idempotent and safe on non-dict/empty.
- `python -m pytest -q tests` → **85 passed**. `py_compile` of `app.py` + the module → OK.

## Remaining moat work — Run C.2 (touches the live pipeline / API; verify in the user's env)
- **Prior-studies API input** — `AnalyzeRequest` doesn't accept prior studies; today they're only
  passable programmatically to `agent_runner.run`.
- **Structured prior-finding extraction + temporal delta** — "present in Sep, gone in Feb" computed
  per level/side, not just narrated by the agent.
- **Op-vs-read level/side** in the live wiring — extract the read's surgical-level/side from the
  summary findings and pass to `reconcile_op_note_with_read`.
- **e2e confirmation** — that the op-note discrepancies actually appear in a rendered report needs a
  live run (the nested `claude -p` 401s here).

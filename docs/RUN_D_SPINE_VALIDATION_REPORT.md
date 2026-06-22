# Run D - Spine Validation Harness

Plan: `~/.claude/plans/so-what-s-the-plan-snappy-treasure.md`. PHI-safe: no medical
images, PDFs, raw reports, raw Claude outputs, generated PHI, SPIDER files, RSNA files,
or validation outputs were committed.

## What changed
- Added `backend/validation/spine_eval.py`.
- The harness supports:
  - SPIDER staging checks with a hard guard against repo/OneDrive dataset roots.
  - SPIDER case discovery from staged `.mha`/DICOM files.
  - Schema-tolerant radiological grading CSV/TSV parsing.
  - Optional `.mha` to synthetic DICOM conversion through SimpleITK.
  - Subscription Claude CLI reads through `AgentRunner` only.
  - Arm-tagged caches, preserving the legacy `baseline` cache path.
  - Text-only Claude label extraction from MIKA summaries.
  - Raw `{tp, fp, tn, fn}` persistence and per-finding sensitivity/specificity as `k/N`.
  - Explicit rejection of empty/instant/$0 unconfirmed reads as failed reads.
- Added tests for staging metadata, cache arm paths, SPIDER label parsing, metric math,
  read-confirmation guards, and dataset-root safety.

## Dataset Scope
- SPIDER validates lumbar MRI degeneration-style labels and segmentation-adjacent coverage:
  Pfirrmann/disc degeneration style labels, disc bulging/herniation/narrowing where present in
  grading tables, and related spine structure coverage.
- SPIDER does **not** validate contrast enhancement, neuritis, scar-vs-recurrent-disc
  distinction, postoperative lateral recess detail, or side-specific nerve-root involvement.
- RSNA LumbarDISC is staged as a gated future input only. The harness does not download it.

## Expected Result
- With SPIDER staged outside OneDrive, a pilot command can produce per-finding sensitivity and
  specificity counts, then scale up from the same cache/resume path.
- A true baseline read requires non-empty cached or newly-run summaries; instant all-zero output
  is treated as failed, not as a result.

## Actual Result
- Harness and unit tests pass.
- `python -m validation.spine_eval --stage-info` reports SPIDER staging metadata and the gated
  RSNA note.
- No first real spine accuracy number was produced in this run because SPIDER was not already
  staged at:
  - `D:\mika_datasets\SPIDER`
  - `D:\datasets\SPIDER`
  - `C:\mika_datasets\SPIDER`
  - `C:\datasets\SPIDER`
- The SPIDER image archive is about 3.45 GB. I did not bulk-download it into the repo or OneDrive,
  and I did not report an all-zero placeholder metric.

## Per-Finding Metrics
No SPIDER cases were available locally, so the first real table is pending dataset staging.
The harness will emit rows like:

| finding | sensitivity | specificity |
|---|---:|---:|
| disc_herniation | `tp/(tp+fn)` | `tn/(tn+fp)` |
| disc_bulging | `tp/(tp+fn)` | `tn/(tn+fp)` |
| disc_narrowing | `tp/(tp+fn)` | `tn/(tn+fp)` |
| pfirrmann_advanced | `tp/(tp+fn)` | `tn/(tn+fp)` |
| modic_change | `tp/(tp+fn)` | `tn/(tn+fp)` |
| spondylolisthesis | `tp/(tp+fn)` | `tn/(tn+fp)` |
| endplate_change | `tp/(tp+fn)` | `tn/(tn+fp)` |

## Exact Commands
- `python -m pytest -q tests\test_run_d_spine_eval.py`
  - Result: 6 passed.
- `python -m py_compile backend\validation\spine_eval.py`
  - Result: passed.
- From `backend`: `python -m validation.spine_eval --stage-info`
  - Result: printed SPIDER/RSNA staging metadata.

## Next Validation Command
After staging SPIDER outside OneDrive:

```powershell
cd C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\backend
python -m validation.spine_eval --spider-root D:\mika_datasets\SPIDER --limit 3
python -m validation.spine_eval --spider-root D:\mika_datasets\SPIDER --read --limit 3
```

The first command is the free pilot/detection pass. The second spends subscription Claude reads
only for uncached cases.

## ML Justification
No new ML is justified by this run. The validation harness exists to produce the first real
spine accuracy numbers; model work should wait until those numbers show a repeatable ceiling or
failure mode.

# MIKA validation harness

Cheap, fast accuracy testing. Two layers:

| Layer | Cost | What it scores |
|---|---|---|
| **Detection** (default) | **free** (no Claude) | anatomy / modality / calibration vs ground truth + sequence count |
| **Reading** (`--read`) | subscription credits, **once per study** (cached) | MIKA's read of each study (produces `summary.json`) |
| **Judge** (automatic when a read exists) | ~$0.1/study (cached) | a **semantic LLM judge** (Claude) reads the known diagnosis + MIKA's report and grades clinical equivalence — verdict (correct/partial/missed/overcall), 0-100 score, what it got right / missed / overcalled. Understands synonyms (HCC≈hepatocellular carcinoma) and the normal-control case. **Not keyword matching.** |

## Run (from `backend/`)
```bash
python -m validation.validate                    # free detection scoring only
python -m validation.validate --read             # also run agent reads (uncached) — COSTS CREDITS
python -m validation.validate --read --force     # re-run reads even if cached
python -m validation.validate --only spine       # filter by anatomy
python -m validation.validate --max-src-mb 1000  # include large studies skipped by default
```
Set `MIKA_AGENT_EFFORT=low` before `--read` to keep credit spend down. Reads are cached under
`validation/cache/<study>/summary.json`, so re-scoring is free — you only pay when the reading
pipeline itself changes (delete the cache or use `--force`).

## Ground truth
Edit `ground_truth.json`. Per study: `anatomy`/`modality`/`calibrated` (set to `null` to skip a
check) are scored free; fill `expect_findings` (each with `keywords`, matched any-of) and
`expect_absent` with the real clinical truth to get reading-recall / overcall numbers.

## Output
- Console scorecard + per-study right/wrong.
- `validation_results.json` (machine-readable) and `validation_report.md` (human-readable), both gitignored.

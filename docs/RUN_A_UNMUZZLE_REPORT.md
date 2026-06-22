# Run A — Un-muzzle the read + align the skill (confidence-forward)

Plan: `~/.claude/plans/so-what-s-the-plan-snappy-treasure.md`. PHI-safe; no patient data in this repo.

## Goal
Let the read assign the confidence it actually warrants — like a radiologist — while keeping the
ONE real guard: no fabricated millimetres. Remove the blanket confidence-suppression, undercalling
bias, soft-contradiction mandates, and forced tier caps that were burying genuine findings.

## What changed (prompt + protocol text only — no pipeline/logic rewrite)

**Stripped (the muzzle):**
- `prompts/base_prompt.py` — replaced the blanket TIER CONSTRAINTS (visual-only→B, single-sequence→B,
  incidentals→always-C) with "tier follows what you actually see; a clear finding is Tier A even if
  qualitative/single-sequence/visual-only." Removed "choose the LESS severe" (rule 6) and the
  single-sequence→B cap (rule 2). Rewrote CONTRADICTION discipline (state differences confidently,
  don't soften/retract) and INCIDENTAL FINDINGS (tier by visibility, no forced Tier-C / boilerplate).
- `prompts/spine_master.py` — removed the level-confidence→C/B caps (tier the finding by visibility;
  use "approximate level" language only for the level LABEL). Updated the few-shot discrepancy +
  incidental examples to confident wording.
- `services/agent_runner.py` — READING RIGOR no longer says "prefer the LESS severe reading"; now
  "call findings at the severity the images support; the only hard limit is measurements."
- `services/claude_interpreter.py` — removed the auto "cap confidence at Tier B for structures without
  measurements" and the soft-contradiction RULES block + example.
- `services/verification.py` — removed "the LESS severe interpretation wins"; reframed audit items
  `tier_criteria` and `contradiction_language` to confidence-forward (single-sequence/visual can be A;
  divergences stated clearly, not softened).
- `skills/mri-spine-analysis/SKILL.md` — reframed the "third danger" (confident error = asserting what
  you can't see / uncalibrated mm, NOT confidence itself), the tier caps, the contradiction language,
  the incidental rule, and the self-audit close ("state clearly what you DO see").

**Kept (the only limits):**
- No specific mm without PixelSpacing calibration; uncalibrated → qualitative + "(visual estimate — no
  calibrated measurement available)" (`base_prompt.py` MEASUREMENT RULES + anti-hallucination #3).
- "Don't describe what you cannot see" / Tier D for a genuinely missing sequence/element.
- ONE per-report disclaimer (`REPORT_DISCLAIMER`).
- Annotation machinery untouched (already computational + intensity-verified).
- CV candidate adjudication untouched — verified it never gated the holistic read; it only adds
  localization/annotation support, so the diagnosis was never the thing being suppressed.

## Verification (run here)
- New guard test `tests/test_run_a_unmuzzle.py` — asserts the muzzle phrases are gone, the measurement
  guard + disclaimer remain, and confidence-forward language is present.
- `python -m pytest -q tests` → **75 passed** (71 prior + 4 new).
- `py_compile` of all changed modules → OK.
- `git diff --check` → OK. Residual-muzzle grep over source (excluding the gitignored run cache) → NONE.

## Remaining step — must run in the user's environment (not here)
The live read uses the subscription `claude` CLI, which 401s when nested inside a Claude Code session.
So the actual validation runs on the user's machine:

```
# from the repo, with the February study path set outside the repo / OneDrive
python -m validation.<feb-read-harness>   # re-read the February lumbar study, Opus 4.8
```
**Pass criterion:** the read now states the March-correct diagnosis qualitatively and confidently —
left S1 nerve-root enhancement (neuritis), peripheral-scar / central-disc split, the level/op-note
reconciliation — WITHOUT any fabricated mm, and without the old "cannot assess / may warrant / Tier C"
hedging. Compare against the March report's diagnosis (not its measurements).

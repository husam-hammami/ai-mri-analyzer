# MIKA — Incident log

High-signal only: a REAL failure with a generalizable root cause that caused a wrong result,
data loss, an outage, or hours lost. Dedupe by failure-class. Format:
`### title [category] [severity]` · Symptom · Root-cause CLASS · Fix · Guard (what a future plan must check).

### Nested `claude -p` read hangs when launched from inside a Claude session [concurrency] [high]
- **Symptom:** `spine_eval --read` (agent mode shells a headless `claude -p`) produced figures then froze; sat dead ~8 hours, never wrote `summary.json`.
- **Root-cause class:** a headless `claude` spawned within a `claude` session deadlocks/auth-stalls (nested-runtime reentrancy).
- **Fix:** validated the deterministic engine path in-process instead; real agent reads must run from a normal terminal.
- **Guard:** any plan that calls the `--read`/`AgentRunner` path must run it in a real terminal, never nested in a session. Keep localization metrics on the deterministic-only harness.

### Single-slice spine localization is ~110 mm off on curved / off-center studies [validation] [high]
- **Symptom:** engine `identify_levels` placed L5-S1 ~110 mm from the true disc on a real SPIDER lumbar case; marks landed on the wrong half of the spine.
- **Root-cause class:** localizing on ONE 2-D sagittal slice (hardcoded `midline_slice=8`, fixed-fraction sacrum search) when the spine curves across sagittal planes and the FOV isn't LR-centered — a 2-D-projection bug, not a model gap.
- **Fix (planned):** multi-slice detection back-projected through the existing 3-D geometry; demote the single-slice path to fallback. See `docs/CV_LOCALIZATION_PLAN.md`.
- **Guard:** never localize a 3-D structure from one fixed slice index; score against ground truth under an absolute (not approximate) registration before trusting a localizer.

### Ground-truth orientation inferred from physical-axis sign was inverted [data-integrity] [med]
- **Symptom:** SPIDER GT parser named discs upside-down (L5-S1 at the cranial end), corrupting the positional metric.
- **Root-cause class:** deriving caudal direction from `-pz` (physical z sign) is unreliable across `.mha` direction matrices.
- **Fix:** name from the dataset's label convention (highest disc label = L5-S1) + sacrum anchor; orientation-independent.
- **Guard:** never infer anatomical direction from a raw physical-axis sign; anchor on a known label/landmark and add an orientation-robustness test.

### LLM-stated mm measurements are fabricated without calibration [validation] [high]
- **Symptom:** the once-celebrated "March" read was later found partly fabricated — guessed mm, eyeballed arrows, no calibration. (Recurs as: a demo's "6.2 mm" was a hardcoded literal, not measured.)
- **Root-cause class:** asking an LLM (or a label) for a precise number it did not actually measure → confident confabulation.
- **Fix:** mm comes from DICOM calibration + computed geometry, gated by `cannot_assess`/calibration_state; numbers shown only when calibrated.
- **Guard:** any "N mm" in output must trace to a measured pixel-span × real PixelSpacing; never let a model or a demo emit a precise number it didn't compute.

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

### A safety gate applied at one write site is bypassed by a second downstream writer [data-integrity] [high]
- **Symptom:** the uncalibrated→broad-box / calibrated-only-numbers figure gate was applied in the agent finalize, but a later post-QA PDF rebuild (`_rewrite_agent_summary_and_patient_pdf`) called `build_patient_report` WITHOUT re-running the gate — so the patient-facing PDF embedded the model's **ungated** figures after QA, silently defeating the fix. (Sibling: `_render_host_annotations` also silently skipped on an unresolvable base, and the renderer ignored the study-level `calibrated` flag — "fix looks applied but never runs.")
- **Root-cause class:** a transform/gate that is not the LAST writer of the artifact it guards; a second code path re-emits the raw artifact downstream.
- **Fix:** make the gate a single shared function and call it as the last writer before EVERY consumer (here: `AgentRunner._render_host_annotations` is static, called before both `build_patient_report` sites); add a regression test that the final artifact is gated.
- **Guard:** any output gate must be verified to run on the FINAL artifact the user sees, not just the first one produced — enumerate every writer of that artifact and confirm the gate runs after the last one. Prefer one shared gate over per-site copies.

### Non-deterministic LLM verdict mutates clinical output by default [validation] [high]
- **Symptom:** a focused-CV "supported" verdict that flips between identical Claude reruns (RUN10 QA) could upgrade reconciliation `agreement_status` (conflict → supported), so the same study run twice gave different reconciliation — on the flagship lumbar-MR path.
- **Root-cause class:** letting a single, non-reproducible model judgement mutate persisted clinical state with no cross-run stability requirement.
- **Fix:** the upgrade is OFF by default (`MIKA_CV_SYNTHESIS=1` to opt in); the blind read / reference conflict is preserved verbatim, so the default output is reproducible.
- **Guard:** never let a single-run model verdict mutate clinical output by default; require cross-rerun stability (or keep it advisory/non-mutating) before any state change.

### LLM-stated mm measurements are fabricated without calibration [validation] [high]
- **Symptom:** the once-celebrated "March" read was later found partly fabricated — guessed mm, eyeballed arrows, no calibration. (Recurs as: a demo's "6.2 mm" was a hardcoded literal, not measured.)
- **Root-cause class:** asking an LLM (or a label) for a precise number it did not actually measure → confident confabulation.
- **Fix:** mm comes from DICOM calibration + computed geometry, gated by `cannot_assess`/calibration_state; numbers shown only when calibrated.
- **Guard:** any "N mm" in output must trace to a measured pixel-span × real PixelSpacing; never let a model or a demo emit a precise number it didn't compute.

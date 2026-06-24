# MIKA — Automatic mm-accurate disc localization (lumbar spine MR)

> Reviewed ✓ (bulletproof, 2 rounds) — strategy SUFFICIENT: "classical-first, learned-only-if-needed-and-only-if-packageable is the right approach for this goal and deployment model." The 3 round-2 implementation gates are baked in as Phase 0 below.

## Goal (smallest honest claim)
Annotations land on the correct disc to ~mm accuracy, **automatically**, for **lumbar spine MR only**, validated **out-of-distribution** — and MIKA still ships as the ~500–700 MB zero-prereq Electron app. No cross-anatomy / "all imaging" promise in this deliverable.

## Why this approach (not "let Claude/an LLM localize to mm")
LLM vision is patch-downsampled, uncalibrated, and confabulates precise numbers — already burned this project (the March read fabricated mm). The ~110 mm miss was a **2-D-projection bug** (single midline slice; spine curves across sagittal planes; FOV not LR-centered), **not** a need-for-a-neural-net. MIKA already has the 3-D geometry machinery and the verify-or-degrade gate. So: fix the projection with the geometry we already have; only reach for a learned model if that baseline can't clear the bar AND it can be packaged.

## Phase 0 — make the experiment honest (BLOCKING gates, from bulletproof)
- **0.1 Rigid, computable metric.** The synthetic-DICOM converter writes no IPP/IOP, so "LPS-to-LPS" is not available. Score in the **shared array-row frame**: `mm = Δsi_row × z-spacing`. Pass `compare_spine_levels` a real `si_scale=z_spacing` (+ offset) so `registration=="absolute"` and the off-by-one verdict is trustworthy. Remove the hard-coded `calib=None` in `spine_eval.positional_accuracy` for this path; delete/feed the dead `si_scale` branch. Stop calling it "LPS-mm" until IPP/IOP exist.
- **0.2 OOD loader + n.** SPIDER is in-distribution for any learned spine model, so the gate is OOD. RSNA LumbarDISC is coordinate GT (gated) — `spider_level_ground_truth` can't parse it. Either add a coordinate-format OOD loader emitting the same `{levels:{name:{si_row, si_mm,...}}}` shape, or designate a TCIA segmentation lumbar set (reuse the existing SEG machinery). State **n** before any gate fires.

## Phase A — no-new-dependency multi-slice classical localizer (the first and maybe only commitment)
- **A1 Multi-slice detection.** Refactor `_detect_l5s1_sagittal_disc_row` to accept a slice; run the disc-band detector across the **parasagittal fan** (not the hardcoded `midline_slice=8`), back-project each detection to the array-row/3-D frame, cluster centroids, name sacrum-up. 100% numpy<2, zero packaging risk. Reuse `_recover_faint_lowest_disc` for the off-by-one; keep `compare_spine_levels`' off-by-one as the **independent** check (not the localizer's own naming — avoid circularity).
- **A2 Retire the broken path (preserve the contract).** Re-point `app.py:2931/2938/2974` (`identify_levels`/`measure_all_discs`/`create_level_reference`) at the multi-slice localizer; single-slice `midline_slice=8` becomes a fallback only. **Invariant:** the localizer MUST still populate `self.level_map` and `self.reference_image` that `measure_all_discs` depends on (don't break the spine measurement pipeline — CLAUDE.md rule 7).
- **A3 Measure** on SPIDER (in-dist) AND the OOD set, under the 0.1 rigid metric, **n stated**, on the **deterministic-only** harness (no nested `claude -p` — that hangs in-session; `--read` only in a real terminal). Report in-dist vs OOD mm-offset + level-match + off-by-one separately.
- **GATE 1:** if the classical baseline clears the agreed mm bar **OOD** → STOP. Ship spine-only, zero new dependency. Done.

## Phase B — learned model, ONLY if Phase A fails GATE 1
- **B1 Packaging spike FIRST (written go/no-go).** Export TotalSpineSeg's nnU-Net to ONNX; run via `onnxruntime` in a clean numpy==1.26.4 env; confirm mask parity with the PyTorch reference on ≥1 case. The ONNX pre/post (resample, normalize, sliding-window) is re-implemented in numpy and is where mm-accuracy can quietly die — so **validate the ONNX path, not PyTorch**, under 0.1+A3.
- **B2 If ONNX parity passes** → integrate via onnxruntime in the existing numpy<2 env (no torch, no second ABI). Add ~300–700 MB.
- **B3 If ONNX fails** → TotalSpineSeg is a **download-on-first-use optional component** (its own env), explicitly NOT bundled; base app stays ~500–700 MB. **Never two numpy ABIs on one PATH** (`ELECTRON_BUNDLING_PLAN.md` §0/§4).
- **B4 Runtime gate.** Measure onnxruntime-CPU latency/RAM on a representative volume; a preflight RAM/CPU check degrades to region-band via `position_verification` (`allows_pinpoint=False`) on low-spec laptops.

## Cross-cutting contract
- Localizer centroids → sacrum-up levels → `EvidenceCandidate`. Raising `geometry_confidence` past `TRUSTED_GEOMETRY_CONFIDENCE=0.80` (currently capped 0.78) must be an **empirically-validated mapping** from measured localization accuracy to confidence — never a flat bump, or it defeats the gate that suppresses wrong pinpoints.
- Confirm NIfTI axis/LPS frame matches MIKA's converter (the `.mha` converter already had an axis-order bug — explicit check).
- The verify-or-degrade gate, renderer, trust-gates, adjudication/synthesis remain the contract; the localizer feeds into them, never bypasses them.

## Validation summary (what "done" means)
A1 clears the OOD rigid mm bar with n stated → ship spine-only, no new dependency. Otherwise Phase B behind the packaging spike. Never ship a path that was not the path validated.

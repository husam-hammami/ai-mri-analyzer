# Deep functionality review — findings & fixes

Source: multi-agent adversarial review (workflow `wf_63cb48e9-97c`, 2026‑06‑16).
6 review dimensions (data‑contract, state machine, annotation/marker accuracy, anatomy routing,
backend correctness, certainty/safety) → 38 candidate findings → 37 per‑finding adversarial
verifications. **33 confirmed real, 4 refuted.** The synthesizer agent was interrupted; this file is
the hand‑written synthesis + the fixes applied.

## Fixed

### Annotation & UI‑marker accuracy (frontend)
- **ANNOT‑1** — landmark matcher now uses word boundaries, not substring (`pons`≠`responses`, `base`≠`baseline`).
- **ANNOT‑3** — spine level parser accepts `/` and bare‑digit second token (`L4-5`, `L4/5`, `L4/L5`, `L5/S1`).
- **ANNOT‑6** — parser recognizes the full column (C1–C7/T1–T12/L/S1); levels with no calibrated
  coordinate (cervical/thoracic) return **no pin** instead of mis‑pinning (e.g. `T11‑T12`→T12).
- **ANNOT‑2** — if a regional asset fails to load and falls back to the full body, pins are suppressed
  (region‑authored coords would land on the wrong body parts). Gated on `base === expectedReadBase`.
- **ANNOT‑5** — dense label clusters are shifted up so chips can't render below the figure.
- **AR‑1** — Wait defaults to `unknown` (diffuse whole‑figure scan), not a `spine` close‑up, before sequences resolve.

### Anatomy routing (backend + frontend)
- **AR‑2** — MSK no longer always shows a knee. `dicom_engine._detect_msk_subregion` detects the joint
  (knee/shoulder/hip/ankle/wrist/elbow/foot/hand); carried via `anatomy_subregion` through the pre‑step,
  `/sequences`, and report payload. Frontend `figureAnatomy()` routes knee→pins as before, other joints→their
  own asset with **no mis‑pins**, joints without art→whole body.

### Certainty & safety honesty (frontend)
- **CERT‑1** — agent patient block now runs `stripTier()` (plain/caption/bottom_line/arrays/cot) like the lite path → no tier letters can leak.
- **CERT‑2** — Plain Read shows an honest caution banner when `verification.status ∈ {issues_flagged, incomplete}`.
- **CERT‑3** — removed the unfounded blanket "No signs of anything urgent or dangerous were seen."
- **CERT‑4** — lite raw `quality_notes` (audit jargon / parser errors) no longer shown beside the patient confidence pill.
- **CERT‑5** — Tier D ("cannot be assessed") is dropped, never upgraded to "Possible".
- **DC‑01/DC‑07** — agent run without a patient block now falls back to the agent's technical findings + impression instead of rendering a blank Read / empty Clinician view.

### Backend correctness
- **BACKEND‑1** — calibration derived from `inv.is_calibrated` (the dataclass has no `calibration_status`; it was always "unknown").
- **BACKEND‑2** — `job.cancelled` flag honored in the agent loop + completion; a late success can't revert a cancel or send a "ready" email.
- **BACKEND‑3** — `/report/{id}/pdf` gates on `status==complete` + `pdf_available` and serves the run's recorded `pdf_path`; no stale cross‑run PDF.
- **BACKEND‑4 / DC‑04** — `impression` normalized to `list[str]` at the parse boundary (no `[object Object]` from a dict/string).
- **AR‑3** — lite pipeline writes `modality` into `job.measurements` (CT/X‑ray no longer mislabeled "MR").
- **BACKEND‑6** — failed‑inventory studies default to `unknown`, not `spine`.

### Wait / state robustness (frontend)
- **sm‑01** — resume deep‑link verifies the job exists first; a gone job shows a clear message; the poller resets on a confirmed 404 instead of freezing at 8%.
- **sm‑02** — progress is monotonic (no 8→0→8 backslide).
- **sm‑03** — completed Read survives a transient fetch failure (retry w/ backoff); only a confirmed 404 resets to Home; stream is stopped only after a successful fetch.
- **sm‑06** — sequence panel latches on the real sequence list, not on anatomy alone.
- **sm‑08** — ETA shows "Almost done…" when `eta_seconds<=0` instead of perpetually "about 1 min".

## Refuted (not real — left unchanged)
- **sm‑04** — `analyze()` r.ok unchecked: no reachable failure mode (sole caller sends a schema‑valid body; non‑JSON errors hit the catch).
- **sm‑07** — re‑upload stale poller: the state machine makes "Home rendered + live stale poller" mutually exclusive.
- **DC‑03** — "lite confidence always Moderate": premise inverted — VerificationPass runs in the **lite** pipeline and populates `quality_score`.
- **DC‑05** — change‑over‑time broken image / missing ProofPanel fallback: COT figure isn't rendered, and ProofPanel already has an onError fallback.

## Deliberately not changed
- **CERT‑6** ("Connect with Claude" in the patient flow) — spec flags it for removal *pending founder decision*,
  but the user explicitly asked to restore this button. User instruction governs; left in place.

## Open low‑severity items (noted, not fixed)
- **ANNOT‑4** — pin chip numbers follow certainty order, not top‑to‑bottom (leaders disambiguate; renumbering would break thread correspondence).
- **BACKEND‑5** — SSE keepalive is effectively inert during active progress (per‑second data frames keep the connection warm anyway).
- **BACKEND‑7** — zero‑byte/garbage uploads fail later in inventory with an opaque error rather than a clean 400 at upload.
- **DC‑02** — `vm.figures` is populated (lite) but never consumed (dead field).
- **DC‑06** — `vm.patient` carries demographics to the client but is never rendered (unused leak surface).

## Note for review
The CERT‑2 caution banner uses an orange/amber treatment (matching the existing clinician `warn` token),
which sits outside the single‑accent `#2563EB` palette. This is an intentional patient‑safety exception
(blue reads as informational, not cautionary). Switch to an accent‑only treatment if strict palette adherence is preferred.

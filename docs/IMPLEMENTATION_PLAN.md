# MIKA â€” UI/UX Redesign Implementation Plan (Final)
### The One-Body Trilogy: Home â†’ Wait â†’ Read

This is the authoritative, build-ready plan. It applies every valid review correction and resolves all contradictions in favor of (a) the locked design decisions and (b) the real backend code as it exists today in `backend/app.py`, `backend/services/agent_runner.py`, `backend/services/report_builder.py`, and `frontend/index.html`. Where a section repeats a fact, treat the statement in this document as canonical â€” earlier drafts that disagree are superseded.

---

## 1. Overview & Goals

### What we're building
MIKA is a trust-first, patient-first clinical imaging reader. A patient uploads an MRI/CT/X-ray study, leaves for ~20 minutes, and returns to a plain-language, verified radiology-style **Read**. The redesign replaces the current dark three-tab SPA with **three light screens that are three *states of the same study*** â€” the **One-Body Trilogy**: one anatomical figure rendered in Coverage â†’ Reading â†’ Resolved states.

- **HOME** (light) â€” the body as a **coverage map**. Readable regions are hollow nodes with leaders to a coverage list; the active region lit; an upload zone; founder card; trust strip. **LOCKED to `frontend/assets/mockups/home.png`.**
- **WAIT / Reading Room** (light, **claim-free**) â€” the body **being read**: one calm scan-glow over the active region, an honest phase tracker, an honest progress bar + ETA, "you can safely leave this page." **No findings, no level labels, no per-slice position.** This is the load-bearing Waitâ‰ Read differentiation.
- **READ / The Read** (light, complete & **verified**) â€” a large plain bottom-line sentence + overall-confidence pill; the body as a **findings map** with certainty-coded markers pinned to true landmarks + leaders to a certainty-ranked findings thread; a right-hand **proof panel** (real annotated slice + "What this means" + clinician detail); Plain/Clinician toggle; download/share. **LOCKED to `frontend/assets/mockups/read.png`.**

### Non-negotiable principles (locked)
1. **Findings are never shown until verified-final.** The gate is the backend `status === "complete"` â€” `/api/report/{job_id}` returns HTTP 400 (`"Analysis not complete (status: â€¦)"`, app.py:413â€“414) for any other status. The frontend physically cannot fetch findings early. The Wait is claim-free *by construction*, not by discipline.
2. **One data-driven, area-agnostic screen** â€” not ten. Anatomy + modality are auto-detected in Phase 0 (`detect_study_anatomy` / `detect_study_modality`, agent_runner.py ~462â€“465). The Read renders one shape: `{ plainSentence, certainty, proofImage, location?, measurement? }`.
3. **One body figure, three states.** The same `<BodyFigure>` instance is reused; only its state class changes. It never re-mounts between screens.
4. **Graceful degradation, never block on a missing asset.** Regional diagram â†’ full-body figure â†’ no body-map (thread + proof only).
5. **Single accent `#2563EB`.** No violet, no second hue. Certainty is **word + colour + shape**, never colour alone. Motion = CSS `@keyframes` + WAAPI `element.animate()` only (transform/opacity), no new library.
6. **Never fabricate.** No synthetic slice counts, no agent-invented coordinates, no regex-scraped "measurements." Every clinical claim carries an internal tier; the patient sees the certainty *word*, never the letter.
7. **Honest progress only.** The Wait shows only `progress`, `progress_message`, `eta_seconds`, `est_total_seconds`, `status` â€” and frames them as an *estimate*, because in agent mode `progress` is a time-based asymptotic synthesis, not measured work.

### Accuracy honesty (governs copy)
The repo's own assessment puts current accuracy at ~40â€“45%. The normal-study and empty-findings copy must be **qualified and non-dismissive**, and the legal disclaimer is rendered verbatim and is **non-dismissible** on Read.

---

## 2. Architecture & Stack Decision

### 2.1 Status-derived routing (the trust mechanism)
There is no router and no manual navigation. The active screen is a pure function of job state:

```js
function screenFor(job) {
  if (!job.id || job.status === 'pending') return 'home';
  if (job.status === 'complete')           return 'read';   // only reachable post-verification
  if (job.status === 'error')              return 'home';   // + error toast
  return 'wait';   // inventory | levels | measuring | interpreting
}
```

Because `'read'` is reachable only when the backend sets `status === 'complete'` (which agent mode does only after the agent's in-run verification + final `summary.json`, app.py:625â€“628), Locked Decision #1 is enforced by routing, not per-component guards.

### 2.2 The Front-end shell
```
AppShell (light canvas + dark navy sidebar rail + toast layer)
   route = screenFor(job)
   â”Œâ”€â”€ HomeScreen â”€â”€â”   â”Œâ”€â”€ WaitScreen â”€â”€â”   â”Œâ”€â”€ ReadScreen â”€â”€â”
   â””â”€â”€â”€â”€â”€â”€â”€â”€ BodyFigure (one instance, state machine) â”€â”€â”€â”€â”€â”€â”€â”€â”˜
        state-home        state-wait          state-read
```

### 2.3 Stack decision â€” KEEP the single-file CDN app (no build step)
**Recommendation: stay single-file** (React 18 via CDN + in-browser Babel, one `frontend/index.html`), with a disciplined internal section order. Do **not** introduce Vite/esbuild for this redesign.

**Rationale (maintainability vs no-build convention):**
- The app is genuinely small: three thin screens over ~20 shared components. Current file is ~3,640 lines; the redesign *removes* the dense spine-centric tab ReportView and adds the leaner body-figure system, landing ~4,000â€“4,500 lines â€” under the ~5,000-line readability threshold.
- No build = no toolchain drift, no `node_modules`, no CI step, for a project whose backend is Python/FastAPI and whose deploy is one static file. The founder's editâ†’refresh loop is preserved.
- The locked constraints (no new animation library, single accent, no TypeScript mandate) remove the usual justifications for a bundler â€” there's no dependency graph to bundle.
- The real risk (one unreadable blob) is mitigated by **structure**, enforced section order inside the one `<script type="text/babel">`:
  1. Design tokens / CSS (in `<style>`, brand-migrated first)
  2. Constants & maps â€” `COPY`, `TIER_MAP`, `ANATOMY_DIAGRAM`, `LANDMARKS`, `FIGURE_DESCRIPTIONS`, `API_BASE`
  3. Hooks & helpers â€” `useToast`, `useStatusStream(jobId)`, `useReducedMotion()`, `screenFor`, `buildViewModel`, `api.*`
  4. Primitives â€” `CertaintyChip`, `ProgressBar`, `Icons`, `Lightbox`
  5. Body-figure system â€” `BodyFigure`, `MarkerLayer`, `Marker`, `LeaderLine`, `ScanGlow`
  6. Shell â€” `TopBar`, `Sidebar`, `FounderCard`, `PrivacyChip`, `ConnectModal`, `ToastContainer`
  7. Screens â€” `HomeScreen`, `WaitScreen`, `ReadScreen`
  8. App (state + `screenFor` switch) + `ReactDOM.render`

**Concessions / non-concessions:**
- **Pin the React/Babel CDN URLs with SRI hashes** (supply-chain hygiene). One-time, no toolchain.
- **Do NOT check in a precompiled `app.js`.** A second copy of the app creates source-of-truth ambiguity on a no-build project. Drop this idea for v1. (If in-browser transpile cost ever matters, make it a release-only step that regenerates from source and is `.gitignore`d â€” not now.)

**Trip-wire to revisit:** if the file crosses ~5,000 lines, a second full-time dev joins, or TS/test coverage becomes mandatory â†’ migrate to Vite + a flat `components/` dir. Until then, single file is the lower-risk choice.

### 2.4 Brand-token migration (prerequisite â€” M0, before any screen)
The live `index.html` ships the *pre-redesign* palette (`--accent:#4c8dff`, a deprecated `--violet:#8b5cf6`, Inter). Locked brand: `#2563EB` (only accent), DM Sans, navy bases `#0A0F1D`/`#101B33`, light canvas `#F8FAFC`, hairline `#E5E7EB`, slate text `#475569`/`#94A3B8`.

- Repoint `--accent` family to `#2563EB`; **delete** `--violet`, `--violet-soft`, `--violet-glow` and replace every usage.
- **Delete `--tier-a/b/c/d` hue tokens** too (teal/amber/orange/grey). Certainty is encoded through `CertaintyChip` as one-accent opacity ladder + shape + word. If these tokens remain they will be reused and reintroduce a multi-hue model.
- Swap Google Fonts to **DM Sans**; drop Inter/Playfair.
- Light canvas for Home/Wait/Read; dark scheme scoped to the sidebar rail only.
- **All new CSS uses `var(--accent)` only â€” zero hardcoded accent hex.** M0 gate: grep for `#2563EB` / `#4c8dff` / `#8b5cf6` literals in new code = fail.

### 2.5 State surface (centralized in AppShell â€” no global store)
```js
const [connection, setConnection] = useState(null);        // /api/agent/availability
const [job, setJob]   = useState({ id:null, status:'pending', mode:null, progress:0,
                                    message:'', eta_seconds:null, est_total_seconds:null, error:null });
const [report, setReport]  = useState(null);               // /api/report/{id} (only when complete)
const [uiPrefs, setUiPrefs] = useState({ view:'plain' });  // plain | clinician
const [activeFinding, setActiveFinding] = useState(null);  // marker â†” thread cross-highlight
const [lightbox, setLightbox] = useState(null);
const screen = screenFor(job);                             // derived, never set directly
```
`job.mode` is **read back from the `/api/analyze` response** (agent â†’ `mode:"agent"`, app.py:329; lite â†’ `mode:"lite"`, app.py:354) and drives the Read data contract (see Â§5).

---

## 3. Shared Shell + Reusable Component Inventory

### Shell (persists across all three screens)
| Component | Responsibility | Source |
|---|---|---|
| `AppShell` | Light canvas + dark navy sidebar rail + toast layer; owns the only top-level state; computes `screen` | new |
| `TopBar` | Logo + helix mark, tagline, connection status pill | reuse, re-skin; calls `GET /api/agent/availability` |
| `Sidebar` (dark `#0A0F1D`/`#101B33`) | Brand, nav, `FounderCard`, `PrivacyChip`, "Need help?" | new |
| `FounderCard` | `assets/brand/founder-husam.png` + origin note | new (asset exists) |
| `PrivacyChip` | Trust pill â€” **copy must be truthful** (see Â§7.6 TTL) | new |
| `ConnectModal` | Subscription sign-in (`POST /api/connect`) | reuse as-is |
| `ToastContainer` + `useToast()` | Ephemeral notices, 4s auto-dismiss | reuse as-is |

### The body-figure system (trilogy core)
| Component | Responsibility |
|---|---|
| `BodyFigure` | Renders `assets/anatomy/{region}.png` in a `position:relative` stage; state via CSS class `.state-home/.state-wait/.state-read`. Graceful degradation: regional â†’ `body.png` â†’ none. `role="img"` with an `aria-label` summarizing findings; **markers are `aria-hidden`** (the thread is the accessible source of truth, Â§9). |
| `MarkerLayer` | Absolutely-positioned overlay; consumes `LANDMARKS[anatomy][landmark] = {x,y}` (normalized 0â€“1); renders `Marker` pins **only in `read` mode**. |
| `Marker` | One pin at `transform: translate(x%,y%)`; certainty encoded by opacity/ring-weight + an inner shape glyph (â—/â—/â—‹). Click â†’ scroll thread to its finding + glow. Decorative for AT. |
| `LeaderLine` | Thin SVG leader markerâ†’thread row; recomputed on resize via `ResizeObserver`; hidden on <600px. |
| `ScanGlow` | **Wait-only.** One calm blue scan-band over the active region (CSS `@keyframes` on `transform`/`opacity`; `blur` static on a child). Claim-free â€” no labels, no markers, no measurements. |

### Screen-scoped & primitives
| Component | Responsibility |
|---|---|
| `UploadDropzone` | drop / choose files / `webkitdirectory` DICOM folder; modality chips (MRIÂ·CTÂ·X-ray) + format chips (DICOMÂ·NIfTIÂ·NRRDÂ·PNG/JPGÂ·ZIP); **client pre-flight validation** (Â§10). |
| `CoverageList` | region â†’ "we can read this / coming soon", leaders to `BodyFigure` nodes |
| `PhaseTracker` | Honest stepper bound to `status` + `progress` thresholds (Â§7.3) |
| `StudySequencesPanel` | Thumbnail row of detected sequences (conditional â€” see Â§4.2 / Â§7.4) |
| `ProgressBar` | `width = progress%` + ETA; framed as estimate |
| `BottomLineBlock` | Large plain sentence + overall `ConfidencePill` |
| `FindingsThread` / `FindingRow` | Certainty-ranked rows; each is a real `<button>` with `aria-expanded`; bidirectional link to its `Marker` |
| `ProofPanel` | Annotated slice (`GET /api/images/{jobId}/{stem}`) + "What this means" + "View clinician detail" |
| `PlainClinicianToggle` | Flips view-model source; Plain default |
| `CertaintyChip` | Single source of truth: tier A/B/C/D â†’ **Confirmed/Likely/Possible** (word + accent-opacity + shape, never letter) |
| `Icons` | Inline SVG; add `share`, `download`, `bell` |

---

## 4. The Three Screens

The app root replaces `view: 'upload'|'analyzing'|'report'` with `screen: 'home'|'wait'|'read'`, derived via `screenFor(job)`. Connection (`GET /api/agent/availability`) is orthogonal to screen â€” it gates the *ability to start a run*, not which screen renders.

### 4.1 HOME â€” coverage map (LOCKED to `home.png`)
**Layout (desktop â‰¥1024px):** centered light column; hero row = `BodyCoverageMap` (left) + headline "Imaging studies, unfolded into evidence." + `UploadDropzone` (right); founder card; trust strip. On <768px â†’ single column, coverage list as a static chip wrap (leaders dropped).

**Data flow:**
```
drop/choose â†’ handleFiles â†’ POST /api/upload
  â†’ { job_id, file_count, input_format, warnings[] }   // warnings â†’ toasts
  â†’ show "Ready" + optional clinical_history field
  â†’ CTA "Read my scan" â†’ POST /api/analyze { job_id, mode:"agent", clinical_history }
  â†’ READ BACK response.mode; set job.mode; job.status flips â†’ screen='wait'
```
Anatomy is **not** known at upload; the coverage map does not pre-light a detected region until Wait.

**States:** A Empty/idle Â· B Drag-over (borderâ†’solid accent, WAAPI scale â‰¤1.02) Â· C Uploading/converting (real upload progress, not indeterminate) Â· D Ready (file_count + input_format) Â· E Upload error (retry, no jobId) Â· F With-warnings (run still allowed) Â· **G Capacity/Connect** â€” see Â§10/Â§11: if patients don't authenticate, this is an **at-capacity** state ("we're at capacity â€” leave your email and we'll run it when a slot opens"), gated on `GET /api/agent/availability`; the "Connect with Claude" developer concept is **removed from the patient flow** pending the founder decision (Â§11).

### 4.2 WAIT â€” Reading Room (claim-free; no saved mockup, build to spec)
**Hard constraints (LOCKED):** zero finding markers; zero level labels; honest progress only (no fabricated slice/level position).

**Layout (desktop):** left "What's happening" â€” `BodyFigure state="wait"` (single scan-glow), claim-free reassurance sentence, `PhaseTracker`, "You can safely leave this page â€” we'll notify you." Right â€” `StudySequencesPanel` (conditional), `ProgressBar` + ETA.

**The honest-progress reality (agent mode is the production path):**
- Agent mode is an opaque ~20-min `claude -p` subprocess (`asyncio.to_thread(runner.run, â€¦)`, app.py:577). The backend gets **nothing** until it returns. `progress` is synthesized: `frac = 1 - exp(-elapsed/(est*0.55)); progress = min(95, 8 + int(frac*87))` (app.py:590â€“591). `eta_seconds = max(0, est - elapsed)`.
- Agent mode emits only `inventory â†’ interpreting â†’ complete` (app.py:570, 626) â€” **never** `levels`/`measuring` (those are lite-only).
- **Therefore the PhaseTracker is threshold-driven on `progress` within `interpreting`** (Â§7.3), framed as *stages of the read*, never "step N of M complete."
- `detected_anatomy` is computed **inside** the opaque subprocess, so it is **not** exposed mid-run unless the synchronous inventory pre-step (Â§7.3, made **mandatory**) runs first. **Without that pre-step the scan-glow is whole-figure faint (honest), not region-localized.**
- `StudySequencesPanel` has **no data in pure agent mode** (`job.measurements["sequence_catalog"]` is populated only after the agent completes, and even then agent mode overwrites `measurements` to `{detected_anatomy, calibration_status, study_description, agent_summary}`, app.py:618â€“623 â€” there is no `sequence_catalog`). The panel is therefore **gated on the Â§7.3 inventory pre-step or lite mode**; absent that data it renders a calm placeholder â€” never a fabricated list.

**States:** A Spin-up (whole-figure faint glow, "Aligningâ€¦" active) Â· B Typical (region glow *if* anatomy known via pre-step; phase ticks; bar+ETA) Â· C Sparse study (only the real 1â€“2 sequences) Â· D Near-done (`progress â‰¥ 95` â†’ "Finishing upâ€¦", ETA â†’ "almost done") Â· E Over-ETA ("This is taking longer than usual â€” you can leave and we'll notify you"; never auto-fail) Â· F Error (`status==='error'` â†’ HOME + toast `job.error`; jobId retained for retry) Â· G Connection lost mid-run (non-blocking banner; run continues server-side) Â· **H Job expired/not-found** (deep-link to a job the in-memory `JOBS` dict no longer has, or a 404 from `/api/status`) â†’ calm "We couldn't find this analysis â€” it may have expired. Please upload again."

**`prefers-reduced-motion`:** scan-glow holds static at `opacity:0.5`; bar width is *set* not tweened; phase dot solid; `aria-live="polite"` on progress so SR users hear phase changes.

### 4.3 READ â€” The Read (LOCKED to `read.png`)

**Data source (mode-aware â€” corrected):**
- **The `interpretation`/`verification` adapter is the DEFAULT code path; the agent `patient` block is the ENHANCEMENT.** Reason: `report.agent.summary.patient` exists **only in agent mode AND only if the model emitted it** (it is free-form prompt output, agent_runner.py:419â€“432; `_finalize_patient_report` returns early if absent). Lite mode has `job.agent = {}` â†’ no `patient` block at all; its only data is `report.interpretation` (the `findings_by_*` dicts), `report.measurements`, `report.verification`.
- `buildViewModel(report)` branches on `report.mode`:
  - **agent + `agent.summary.patient` present** â†’ consume the patient block (plain, no letters).
  - **else (lite, or agent without patient block)** â†’ adapter: strip `[Tier X]` from `interpretation.impression[]` â†’ certainty word; `bottom_line` = `impression[0]`; overall confidence derived from `verification.quality_score`; findings from `findings_by_*`.

**Layout (desktop):** header = `BottomLineBlock` (large DM Sans) + `ConfidencePill`; study line (`body_part Â· modality Â· date Â· comparison?`); `PlainClinicianToggle` + Download/Share. Left = `BodyFigure state="read"` (landmark labels + certainty markers + leaders) + `FindingsThread`. Right = `ProofPanel` + `KeyPoints`/`WhatItMeans`/`WorthFlagging`/`ChangeOverTime?`. Footer = verbatim disclaimer. On <1024px proof stacks under the thread.

**Field bindings (agent patient block â€” all presence-checked):**
| Read element | Field |
|---|---|
| Bottom line | `agent.summary.patient.bottom_line` |
| Overall pill | `agent.summary.patient.confidence.{label, note}` â€” **`score` optional**, render label-only if absent |
| Study line | `agent.summary.patient.study.{body_part, modality, date, comparison}` |
| Patient meta | `agent.summary.patient.patient.{name, age, sex}` |
| Findings thread | `agent.summary.patient.findings[]` = `{plain, certainty, figure, caption}` (**no `location`/`text` today**) |
| Proof image | `findings[].figure` â†’ `GET /api/images/{job_id}/{stem}` (strip extension; both modes use this route, app.py:530/605) |
| Key points | `agent.summary.patient.key_points[]` |
| What it means | `agent.summary.patient.what_it_means[]` |
| Worth flagging | `agent.summary.patient.worth_flagging[]` (optional) |
| Change over time | `agent.summary.patient.change_over_time` â€” accept **`points[]` XOR `plain`** (prompt and builder disagree, Â§7), omit block if absent |
| Disclaimer | `agent.summary.patient.disclaimer` (verbatim, fallback to `report.disclaimer` / hardcoded `REPORT_DISCLAIMER`) |

**Clinician view** reads a *different array* â€” `agent.summary.findings[]` = `{text, tier, figure}` (top-level technical block, NOT `patient.findings[i].text`) â€” plus `report.interpretation`, `report.verification` (12-item audit, `quality_score`, `quality_notes`, `corrections`, `missed_findings`). Clinician shows the tier **letter**; Plain never does.

**States:** A Loading (skeletons) Â· B Typical (1â€“6 findings; most-certain auto-selected in proof) Â· **C Empty/normal** â€” bottom line is **qualified**: "This automated read did not flag a significant abnormality. This is not a substitute for your radiologist's report." Body-map shows labeled landmarks, no markers; disclaimer especially prominent; clinician sign-off required on this copy Â· D Dense (>6: thread grouped by certainty, scrollable; co-located markers cluster into a count badge; virtualize >20) Â· E Partial (`verification.status âˆˆ {issues_flagged, incomplete}` â†’ honest banner from `verification.quality_notes`; never invent findings; missing per-finding figure â†’ "Proof image unavailable") Â· F Fetch/parse error despite `complete` (recoverable shell: Retry re-fetch + Start over) Â· G Asset degradation (regional â†’ body â†’ no map; lite â†’ adapter) Â· **H Job expired** (404 from `/api/report`) â†’ "This analysis expired, please re-upload."

### 4.4 The Wait â‰  Read differentiation (summary)
| | WAIT | READ |
|---|---|---|
| Findings | none, ever | verified, pinned |
| Level/landmark labels | none | shown |
| Body figure | scan-glow, unmarked | labeled + certainty markers + leaders |
| Motion | *breathes* | *pins* (deliberate marker draw-on) |
| Data | only `status/progress/eta` | only post-`complete` `/api/report` |

---

## 5. Data Model â€” The Read View-Model

### 5.1 Source of truth
The Read consumes one payload, `GET /api/report/{job_id}`, and `buildViewModel(report)` normalizes it. No component reads `report` directly.

### 5.2 The view-model
```ts
ViewModel = {
  mode: "agent" | "lite",
  bottomLine: string,
  overall: { label: "High"|"Moderate"|"Low", note: string, score?: number },
  study:   { bodyPart, modality, date, comparison },
  anatomy: AnatomyKey,
  findings: Finding[],            // certainty-ranked
  keyPoints, whatItMeans, worthFlagging: string[],
  changeOverTime: { points?: string[], plain?: string, figureUrl?: string } | null,
  disclaimer: string,
}
Finding = {
  id: string,                     // `finding-${index}` (stable anchor)
  plainSentence: string,          // patient.findings[i].plain â€” verbatim phrase
  certainty: "Confirmed"|"Likely"|"Possible",
  proofImageUrl: string | null,   // resolved from .figure (Â§5.7)
  caption: string,
  location: { label, x, y } | null, // parsed landmark â†’ coordinate (Â§5.5â€“5.6); may be null
  measurement: null,              // see Â§5.2-note
}
```
**Provenance rules:** render `plainSentence`/`caption` verbatim; unknown/absent certainty â†’ treat as `"Possible"` (most conservative, never upgrade).

**Â§5.2-note â€” NO measurement chip on Read.** Do **not** regex-scrape `caption` for "7.2 mm" â€” a caption number may be a percentage, slice index, or level, and surfacing it as a measurement fabricates clinical data (CLAUDE.md rule #1). `measurement` is `null` on the patient Read. Calibrated values live in the **Clinician** view from `report.measurements.disc_measurements[]`. If a structured patient-facing measurement is ever wanted, add an explicit agent field â€” never a regex.

### 5.3 Tier â†’ certainty (immutable, one-directional)
The backend already collapses letters to words in the patient block (agent_runner.py ~436â€“437). Frontend consumes words directly.

| Tier (clinician) | Patient word | Encoding |
|---|---|---|
| A | **Confirmed** | solid `var(--accent)` + filled â— |
| B | **Likely** | reduced-opacity `var(--accent)` + half â— |
| C | **Possible** | hollow `var(--accent)` ring + open â—‹ |
| D | *(cannot-assess â€” never a patient finding)* | not shown |

`patient.confidence.label` (High/Moderate/Low) â†’ overall pill via the same ladder. **The word is always present and primary; colour + shape are reinforcement** (opacity-of-one-blue alone fails colour-blind users â€” the shape glyph + text close that gap). The clinician view *reveals* the underlying letter; Plain never exposes A/B/C/D.

### 5.4 Anatomy â†’ diagram lookup
`report.detected_anatomy` âˆˆ 10 keys. 11 assets on disk: `body, brain, knee, chest, abdomen, head-neck, hip, shoulder, vascular, breast, prostate`.
```js
const ANATOMY_DIAGRAM = {
  spine:"body.png", brain:"brain.png", chest:"chest.png", abdomen:"abdomen.png",
  breast:"breast.png", vascular:"vascular.png", head_neck:"head-neck.png", prostate:"prostate.png",
  msk:"body.png",         // refine to knee/hip/shoulder if study text names the joint
  cardiac: null,          // NO chest.png borrow â€” see Â§10 risk #3 / Â§11
};
const MSK_DIAGRAM = { knee:"knee.png", hip:"hip.png", shoulder:"shoulder.png" };
```
**Corrected:** `cardiac` (and any region without a true, clinician-approved asset) defaults to **full-body fallback or no-body-map**, NOT a borrowed `chest.png` with "left ventricle" pinned on a generic figure â€” that is the mispositioned-organ credibility risk. No anatomyâ†’diagram mapping ships **enabled** without clinician sign-off (Â§10 risk #3).

**Fallback order:** regional â†’ `body.png` (on `onerror`) â†’ no body-map (single-column thread + proof). Resolver returns `{ diagramUrl, hasBodyMap }`.

### 5.5 Landmark â†’ (x,y) coordinate map
Markers placed by `(x,y) âˆˆ [0,1]` fractions of the diagram's rendered box (responsive-safe). `LANDMARKS[anatomy][landmark]`. **Spine fractions are hand-authored, clinician-reviewable** values for T12â€“S1 matching the Read mockup.

**Corrected â€” spine marker Y does NOT come from `level_map`:** in agent mode `job.measurements` has **no `level_map`** at all (overwritten, app.py:618â€“623); in lite mode `level_map[level].sag_slice` is a **slice index** (which MRI image to show), not a Y on the stylized `body.png`. So: `level_map` is used **only** to pick which MRI slice the proof panel shows â€” **never** to position a diagram marker. Diagram markers use the hand-authored fractions. **The Read copy must call body-map level labels schematic/approximate; the proof MRI slice is the only positional source of truth.**

### 5.6 location â†’ marker (text-parsing is the day-one mechanism)
`finding.location` **does not exist in the backend today** (a finding is exactly `{plain, certainty, figure, caption}`). So:
```
1. Parse a landmark token from plainSentence, then caption:
     spine â†’ /(T12|L[1-5]|S1)(?:-(T12|L[1-5]|S1))?/i
     other â†’ longest synonym-normalized key in LANDMARKS[anatomy]
2. If token matches AND hasBodyMap AND LANDMARKS[anatomy][token] exists:
     render Marker at (x,y), styled by certainty, with a leader to #finding-${id}
3. Else location=null â†’ NO marker for that finding (graceful, Â§5.7)
```
Cross-highlight reuses the existing `getElementById(...).scrollIntoView()` pattern. Structured `location` is a **later precision upgrade** (Â§7.2), not a v1 dependency. (Consequently, M3's gate is softened: "every finding with a parseable landmark pins a marker; others render marker-less" â€” not "marker count == findings count.")

### 5.7 Graceful degradation (layered, independent)
1. Missing regional diagram â†’ `body.png`.
2. `body.png` unavailable/`onerror` â†’ drop body-map; single-column thread + proof.
3. Landmark unparseable/unmapped â†’ that finding shows in thread + proof, **no pin**; others pin normally.
4. Missing proof image (`figure` null or 404) â†’ card shows text + caption; proof panel shows quiet "proof image unavailable," never a broken `<img>`.
5. No findings â†’ bottom line + key points + what-it-means; suppress findings section (qualified copy, Â§4.3 State C).
6. No `patient` block (lite / sparse agent) â†’ render from `interpretation.impression[]`; overall confidence from `verification.quality_score`.

### 5.8 Proof image loading
```js
function proofUrl(jobId, figureFilename) {
  if (!figureFilename) return null;
  const stem = figureFilename.replace(/\.[^.]+$/, "");  // "figure_1.png" â†’ "figure_1"
  return `${API_BASE}/api/images/${jobId}/${stem}`;
}
```
- **Both modes** load via `GET /api/images/{job_id}/{stem}` â€” agent figures are registered into the same route (`job.annotated_images[Path(p).stem] = p`, app.py:605); there is no separate "report-dir filename" path.
- **Per-finding proof is agent-mode-only** (lite findings carry no `figure_N.png`). **Lite Read shows the named-figure gallery** (`level_reference, sag_t2_annotated, multi_sequence_panel, contrast_L4L5, contrast_L5S1`) rather than per-finding proof.
- `loading="lazy"`; click â†’ reuse existing `Lightbox`; `onerror` â†’ degradation layer #4. `report.figures[]` supplies gallery captions.

### 5.9 Plain / Clinician toggle
Switches which slice of the report feeds the view-model â€” same body-map, same marker geometry, no refetch, no re-layout. Plain default (patient-first). Plain reads strictly from `agent.summary.patient`; Clinician reads `agent.summary.findings[]` (`{text, tier, figure}`) + `interpretation` + `verification`, and links the PDF via `GET /api/report/{job_id}/pdf` (gated on `pdf_available`, agent-only). If the clinician block is sparse (lite) â†’ "limited clinician detail for this run."

---

## 6. Motion Specs

Animate **`transform`/`opacity` only**; keep `blur`/`filter` static on child layers; CSS `@keyframes` for loops, WAAPI `element.animate()` for triggered/data-driven moments; single accent; **no library**.

### 6.1 Tokens (extend existing `:root`)
Reuse `--ease-out`, `--ease-spring`, `--duration-fast/normal/slow`. Add: `--ease-calm: cubic-bezier(0.37,0,0.63,1)`; `--dur-breathe:4200ms`; `--dur-scan:3600ms`; `--dur-progress:800ms`; `--stagger-step:70ms`; `--draw-marker:520ms`.

### 6.2 Reduced-motion contract (NEW â€” absent today)
Add the global guard, AND read the flag **reactively** (a `change` listener, not a once-at-load const) so an OS toggle mid-session is honored:
```css
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{ animation-duration:.01ms!important; animation-iteration-count:1!important;
    transition-duration:.01ms!important; scroll-behavior:auto!important; }
}
```
```js
function useReducedMotion(){ /* matchMedia + 'change' listener â†’ boolean state */ }
// every animate(): duration: REDUCED ? 0 : <ms>; for loops, guard the whole call.
```
Rule for a trust product: reduced-motion users reach the **exact same end state instantly** â€” no information lost, only motion.

### 6.3 HOME
- **logoBreathe** (the helix node) â€” slow scale/opacity loop, "system awake."
- **regionGlow** â€” soft accent halo on the lit region; blur static on a pre-blurred child, loop animates child opacity/scale only.
- **Triggered** â€” coverage nodes + leaders fade-in stagger (reuse `fadeIn`); hover lifts node `scale(1.06)`.
- *Reduced:* logo at `scale(1)`; glow fixed `opacity:0.5`; nodes/labels instant.

### 6.4 WAIT (the honesty screen)
> **HONEST-PROGRESS CONSTRAINT (LOCKED):** motion reflects only `progress`/`status`. The bar/ETA are framed in copy as an **estimate** ("about ~X min remaining"), never a determinate work meter â€” because in agent mode the backend itself advances `progress` on a time curve. **No `setInterval` advances visuals independent of a status event.**
- **scanGlowSweep** â€” ONE glow over the active region (replaces old multi-element `scanLine`+`scanPulse`); `translateY`/`scaleY`/`opacity`; blur static.
- **advanceProgress(prev,next)** â€” WAAPI tween width prevâ†’next on each status update; **never animate backward** (set instantly if next â‰¤ prev). Note: `width` is acceptable here (isolated tiny bar, no reflow neighbors â€” matches existing `.progress-bar-fill`).
- **dotPulse** on the active phase dot (reuse); advances only on a real backend phase boundary.
- **Sequence-check stamp** â€” spring pop when a sequence is confirmed (conditional on Â§7.4 data).
- *Reduced:* glow static `opacity:0.5`; bar width set; dot solid; checks instant; `aria-live` still updates.

### 6.5 READ (confident; markers are the assertion the Wait withheld)
- **crystallizeIn** â€” bottom line â†’ pill â†’ findings (certainty order) â†’ markers; per-item `delay = i*70`; markers start after `threadCount*70` so the figure resolves from Wait's glow into Read's pinned state last.
- **drawMarker** â€” open a finding: leader `strokeDashoffset` draw â†’ pin spring scale-in â†’ proof cross-fade (three chained `animate()`).
- **Active-finding halo** â€” slow low-amplitude `dotPulse` on the open pin only; cancel on close.
- *Reduced:* all to final state instantly (0ms WAAPI fades keep code paths identical); no halo.

### 6.6 Cross-screen transition
`BodyFigure` stays mounted; only the overlay layers cross-fade (`opacity` over `--duration-slow`) as the state class changes. Same body, three states.

### 6.7 QA checklist
Compositor-only loops (verify zero layout/paint on idle); blur never animated; `will-change:transform` scoped to glow elements only; honest-progress grep (Wait reads only `progress`/`status`); reduced-motion parity; no new dependency.

---

## 7. Backend Changes & Wiring

Guiding principle: smallest possible delta; keep the spine pipeline and `claude_interpreter.py` as the single Claude integration point; dataclasses not Pydantic (except FastAPI request bodies); never fabricate.

### 7.1 What's truthful in agent mode (the constraint table)
| Wait element | Truthful in agent mode? | Source |
|---|---|---|
| Coarse progress bar | âœ… | `progress` (synthesized asymptotic, app.py:590) |
| ETA | âœ… | `eta_seconds`/`est_total_seconds` |
| Calm scan-glow | âœ… (region-localized only with Â§7.3 pre-step) | `detected_anatomy` |
| 4-step phase tracker | âš ï¸ coarse | `status` + `progress` thresholds (Â§7.3) |
| Sequence panel w/ checks | âš ï¸ only with Â§7.3 pre-step (else placeholder) | `sequence_catalog` |
| "Reading slice N of M" | âŒ never | no per-slice signal |
| Finding markers | âŒ never | not verified yet |

### 7.2 Structured `location` (optional, additive â€” precision upgrade, NOT v1-blocking)
Add **only** `location.region` + `location.landmark` (a **named** structure the agent already states in prose) to the patient findings spec inside `_build_prompt` (agent_runner.py ~419â€“432). **Do NOT add `location.norm`** â€” an agent-invented `(x,y)` is a fabricated numeric and a visual location claim; coordinates must come from the hand-authored, clinician-reviewed `LANDMARKS` table, never the model. Optional `@dataclass FindingLocation(region:str, landmark:str="")` in `report_builder.py` to coerce/drop malformed entries â€” **dataclass, not Pydantic.** Because `location` is optional and the frontend's day-one mechanism is text-parsing (Â§5.6), nothing blocks on this.

### 7.3 Phase tracker mapping + the mandatory inventory pre-step
Threshold-driven within `interpreting` (agent mode emits no `levels`/`measuring`):
- `progress < 12` â†’ *Aligning sequences* active
- `12 â‰¤ progress < 30` â†’ Aligned âœ“, *Mapping anatomy* active
- `30 â‰¤ progress < 85` â†’ first two âœ“, *Reading* active (the long honest stretch)
- `progress â‰¥ 85` â†’ Reading âœ“, *Verifying* active
- `status === "complete"` â†’ all âœ“ â†’ Read

**Mandatory (not optional) synchronous inventory pre-step:** run `DICOMEngine.run_inventory()` + anatomy/calibration detection **synchronously before** spawning the agent, persisting `detected_anatomy` and `sequence_catalog` to the job. This is what makes the first two checks **provably true** and the scan-glow **region-localized** and the sequence panel **populated**. It runs **upstream** of `measure_all_discs` â€” the spine measurement pipeline is untouched. Without it, the Wait honestly degrades to whole-figure glow + placeholder sequence panel; the plan must not promise the localized glow/sequence checks otherwise.

### 7.4 New endpoint for the sequence panel (one addition)
```
GET /api/study/{job_id}/sequences  â†’ { anatomy, modality, sequences:[{name,plane,num_slices}] }
```
Source: `job.measurements["sequence_catalog"]` + `detected_anatomy` (populated by Â§7.3 pre-step or lite inventory). If unavailable â†’ `{ "sequences": [] }` and the panel renders a placeholder â€” never fabricated.

### 7.5 The verification gate (preserve exactly)
- Gate = `status === "complete"` + a parsed final `summary.json`. `/api/report` 400s on any non-complete status (app.py:413). The frontend cannot fetch findings early.
- Agent mode: VerificationPass runs **inside** the agent (`self_audit` block); `summary.json`/PDF emitted only at the end; `_collect_outputs` reads them after the subprocess returns; only then `status="complete"` (app.py:625). No window of queryable unverified findings.
- Lite mode: `verifier.verify(...)` (Phase 6) produces `VerifiedReport.verified_findings` before `status="complete"`.
- Surface the audit on the **Read, not the Wait**: `verification.quality_score`/`quality_notes` â†’ softened confidence label / State E banner; never hide that verification was incomplete.

### 7.6 Privacy/security â€” required backend tasks (not just UI copy)
The PrivacyChip currently promises more than the system delivers (`JOBS` is in-memory; `work_dir` files persist; the existing code keeps state "as long as the process is alive"). For a trust product this is a liability. Required:
- **TTL + cleanup job:** delete `work_dir` and purge `JOBS[job_id]` after completion + a grace window. Make the PrivacyChip copy truthful ("processed on our server, deleted after N hours") OR soften the claim until cleanup ships.
- **Route authorization note:** `/api/report/{job_id}`, `/api/images/{job_id}/{name}`, `/api/report/{job_id}/pdf` are keyed by `job_id` only â€” an IDOR surface for medical images. `job_id` is a UUID(8) (unguessable enough short-term), but routes must be session-scoped and **not globally enumerable**. Flag as a launch prerequisite.
- **PHI exposure:** decide which `demographics` fields (`patient_name`, `patient_id`, `birth_date` from `export_measurements_json`) appear in the UI/PDF; default to minimal.

### 7.7 "We'll notify you" â€” corrected mechanism
A ~20-min leave-and-return flow means the tab may be discarded; browsers throttle/suspend `EventSource` in backgrounded tabs and fire **nothing** in a discarded/closed tab, so a local `new Notification` on `complete` can never run. The SSE generator (`await asyncio.sleep(1.0)` loop, app.py:401) is server-side and indifferent to tab state â€” but the client side is the problem.
- **v1: opt-in email** is the only mechanism that survives tab close â€” add `notify_email: Optional[str] = None` as a **Pydantic field on the existing `AnalyzeRequest` (BaseModel)** (consistent with the guardrail that already exempts request bodies). On `complete`/`error` in the pipeline finalizer, enqueue one email with a resume deep-link `?job_id=...`.
- **Bonus:** Browser Notifications API for users who keep the tab merely backgrounded (honest copy: "switch tabs â€” keep this one open and we'll notify you; or leave your email to close it entirely").
- **Resume + expiry:** deep-link re-attaches the stream; if `complete` â†’ straight to Read; **if the in-memory job is gone (server restart) â†’ State H** "this analysis expired, please re-upload" (never a blank Read). Document the in-memory-`JOBS` limitation explicitly.

### 7.8 SSE wiring + initial-state seeding (corrected)
Switch the Wait from 1s polling to SSE (`GET /api/status/{job_id}/stream`); keep 1s polling as fallback.
```js
const es = new EventSource(`${API_BASE}/api/status/${jobId}/stream`);
es.onmessage = e => { const s = JSON.parse(e.data); setProgress(s);
  if (s.status==="complete"){ es.close(); fetchReport(); }
  if (s.status==="error"){ es.close(); toastError(s.error); } };
es.onerror = () => { /* exponential backoff reconnect; after N fails â†’ poll /api/status */ };
```
- **Rationale (corrected):** SSE yields only when `(progress, eta_seconds, status)` changes (app.py:386â€“387) â†’ **fewer client wakeups, lower latency; server cost is unchanged** (the generator still loops 1s server-side).
- **Seed initial Wait state** from the `est_total_seconds`/`mode` known at analyze-time â€” no SSE frame arrives until the first change, so don't wait for it.
- **Reconnect/keepalive:** add a server `: ping\n\n` comment every ~15s so hospital/corporate proxies don't drop the idle stream; on reconnect re-sync from `/api/status` (SSE has no replay).

### 7.9 Other backend tasks surfaced by review
- `POST /api/event` â€” lightweight, **PHI-free** analytics sink (`job_id` only): `upload_started, analyze_started, wait_abandoned` (via `visibilitychange`/`beforeunload`), `read_viewed, proof_opened, clinician_toggle, error_shown{cause}`. Feeds the Wait-abandonment + credit-run-rate questions. **No third-party analytics SDK.**
- `POST /api/cancel/{job_id}` â€” stop a wrong-study run and free the credit; without it, at minimum the client abandons (the credit still burns â€” note the waste).
- **PDF brand migration:** `report_builder.py` hardcodes `CERTAINTY_COLOR = {Confirmed:teal, Likely:amber, Possible:gray}` and a teal `ACCENT`. The downloadable PDF will visibly contradict the single-accent Read. Re-color to the `#2563EB` opacity ladder, or document the known inconsistency.
- **`change_over_time` cleanup:** align the prompt (agent_runner.py:428 asks `{points, figure}`) with the builder docstring (report_builder.py:26 documents `{plain, figure}`). Until aligned, the client accepts either.

### 7.10 Guardrails (must not be violated)
1. Spine pipeline untouched (`identify_levels`, `measure_all_discs`, `assess_endplates`, lite Phase 1â€“3). Â§7.3 pre-step is upstream of measurements.
2. `claude_interpreter.py` stays the single Claude integration point. The `location` field is requested via the **prompt** in `agent_runner.py` (agent entry) and the existing interpreter path in lite. No new module opens its own Anthropic client. Verification stays `verifier.verify(...)`.
3. Dataclasses, not Pydantic â€” except FastAPI request bodies (`AnalyzeRequest` is the existing `BaseModel`; `notify_email` is a Pydantic field on it).
4. Never fabricate â€” no synthetic slices, no agent coordinates, no caption-scraped measurements; findings only after the gate; certainty word never letter.
5. Anti-hallucination blocks (`modality_block`, `reading_extra`, ANNOTATION PRECISION / READING RIGOR) unchanged; only delta is the additive `location` request.

---

## 8. Build Order & Milestones

M0â€“M5 = **v1 (ship-blocking)**. M6â€“M7 = **incremental** (graceful-degrade if deferred). Each milestone ends with a gate.

- **M0 â€” Shell + tokens + asset pipeline.** Token sweep (`--violet`/`--tier-*` deleted, `--accent`â†’`#2563EB`, DM Sans, light canvas). `screen` state machine + persistent navy sidebar. Lift reusable primitives. Build `<BodyFigure>` (state prop only). **Asset pipeline (gate before M1, see Â§9/Â§10 #1):** convert the 11 anatomy PNGs (1.7â€“2.4 MB each, ~24 MB) to WebP+AVIF at ~600â€“900px, <120 KB each, `<picture>` PNG fallback; lazy-load regional diagrams; eager only `body.png`; preload the detected diagram after Phase 0. *Gate:* shell renders light/brand-correct; zero `--violet`/accent-hex literals (grep clean); reduced-motion respected; **Home transfer < 500 KB**.
- **M1 â€” Home.** Coverage map + upload + founder + trust strip â†’ `home.png`. Wire uploadâ†’analyze; read back `mode`. *Gate:* visual diff vs `home.png`; real DICOM folder returns `job_id` and advances to Wait.
- **M2 â€” Wait.** Claim-free body (scan-glow), phase tracker, sequence panel (conditional), honest bar/ETA bound to SSE. Â§7.3 inventory pre-step landed so the first two checks are true. *Gate:* bar monotonic & matches backend `progress`; **automated DOM assertion: no level labels / certainty words / finding text render anywhere in Wait.**
- **M3 â€” Read.** Bottom line + overall pill; `state="read"` markers + leaders; proof panel; Plain/Clinician toggle; download/share. `buildViewModel` with **adapter as default**, patient block as enhancement. *Gate:* visual diff vs `read.png`; no A/B/C/D leak to Plain; **every finding with a parseable landmark pins a marker; others render marker-less** (softened from "marker count == findings count").
- **M4 â€” All states wired e2e.** Same `<BodyFigure>` transitions Homeâ†’Waitâ†’Read with no layout jump; markerâ†”thread both directions; error/empty/expired (State H) states; email-notify + resume deep-link; reduced-motion-safe browser notification bonus. *Gate:* one clean dogfood: upload â†’ wait (leave tab, return) â†’ read on a real spine study.
- **M5 â€” Anatomy generalization.** Drive Read purely from `{plainSentence, certainty, proofImage, location?, measurement?}` regardless of `detected_anatomy`; graceful degradation built in. *Gate:* a non-spine study (brain or knee via converted input) yields a coherent Read (thread + proof) with full-body fallback; spine unchanged.
- **M6 â€” Regional diagrams (INCREMENTAL, per-anatomy, clinician-gated).** Integrate the 10 regional diagrams + landmark maps; each ships independently; an un-mapped/unapproved region silently uses M5 fallback; `cardiac`/borrowed-asset regions stay on fallback until a true asset exists. *Gate:* per enabled region, markers land within tolerance; disabling a region cleanly reverts.
- **M7 â€” Polish (INCREMENTAL).** Motion finesse; responsive reflow incl. **380px** (markers dropped on <600px, thread primary, proof full-width, tap targets â‰¥44px); accessibility hardening if not fully done in M4; copy pass; file-size check (flag near ~5K lines). *Gate:* full visual + a11y + responsive pass green on Home and Read.

**Accessibility is M4 (first-class), not M7 polish** â€” for a disability-origin product it's table stakes (Â§9).

---

## 9. Verification & Testing

Tooling stays in-project (browse/gstack or Chrome DevTools MCP for screenshots + DOM assertions + Lighthouse); no new test framework.

1. **Visual diff vs LOCKED mockups.** After M1/M3, screenshot at the mockups' viewport and diff `home.png`/`read.png`: layout grid, single `#2563EB`, DM Sans, sidebar, upload zone, bottom-line/findings-map/proof. Wait â†’ spec checklist (claim-free, tracker order, honest bar/ETA). Material deviation from a LOCKED mockup = defect.
2. **Accessibility (first-class).** Body-map is `role="img"` + summarizing `aria-label`; **the FindingsThread is the canonical accessible representation** (markers `aria-hidden`). Each `FindingRow` is a `<button>`/`<a>` with `aria-expanded`. Certainty = **word + colour + shape** (never colour alone). `aria-live="polite"` on Wait progress. Focus moves to the new screen's `<h1>` on transition; ConnectModal focus-trap. WCAG AA contrast (slate on `#F8FAFC`, accent on light, navy sidebar) via axe/Lighthouse.
3. **`prefers-reduced-motion`.** OS flag on â†’ scan-glow/marker entrances/WAAPI reduce to static, instant; assert no `@keyframes` run; same end states.
4. **Responsive.** Test desktop + 1024/768/600/**380**. Sidebarâ†’top bar; body figure above thread; chips wrap; proof stacks; markers dropped <600px; no horizontal scroll; small WebP served on mobile.
5. **E2E dogfood.** Agent pipeline live: upload real DICOM folder â†’ Wait shows honest progress â†’ **leave tab and return** (verify state restores + notification) â†’ Read renders verified `patient` block with proof. Run on a spine study (regression) and a non-spine study (generalization + degradation). **Re-run the existing spine flow e2e** to confirm measurements/level map/proof figures are byte-for-byte unaffected.
6. **Performance budgets** (named gate): Home transfer < 500 KB, Read < 800 KB; verify lazy-load/preload via DevTools network.

---

## 10. Risks & Mitigations

1. **Image weight (~24 MB of anatomy PNGs).** â†’ M0 asset pipeline: WebP/AVIF <120 KB each, lazy-load, eager only `body.png`, preload detected diagram; named perf budgets are gates.
2. **Honest progress vs agent reality.** Agent reports coarse `inventoryâ†’interpretingâ†’complete` with an asymptotic time curve. â†’ Bind only to `status`/`progress`/`eta`; thresholds map the tracker (Â§7.3); frame ETA as estimate; never invent slice/level; finer granularity is a backend change, not faked.
3. **AI-generated anatomy-asset accuracy.** Wrong art = credibility failure; markers depend on correct art. â†’ Each diagram + landmark map is **clinician-signed-off before that region is enabled** (M6). The **proof MRI slice is the source of truth**; the marker only points. `cardiac`/borrowed-asset regions stay on full-body fallback until a true asset exists.
4. **Not breaking the spine pipeline.** â†’ Frontend consumes existing endpoints only; Â§7.3 pre-step is upstream of measurements; dedicated regression gate (Testing #5). New backend fields are additive.
5. **The ~20-min wait UX.** Highest-abandonment moment. â†’ Engineered for leaving: opt-in email notify (v1), resume deep-link, full state restore, State H for expired jobs, calm claim-free glow + honest tracker; SSE over polling; analytics on `wait_abandoned`.
6. **Subscription-credit ceiling for a free product.** Each run costs real credits (`cost_usd` in `AgentResult`). â†’ Client pre-flight validation (file count/format/size) before burning a run; an explicit **at-capacity/queued** state (separate from `error`) gated on `GET /api/agent/availability`; `POST /api/cancel` for wrong-study runs; flag queueing/quotas as a backend launch prerequisite.
7. **Privacy promise vs in-memory/on-disk reality.** â†’ Â§7.6 TTL+cleanup, truthful PrivacyChip copy, route-auth note, minimal PHI.
8. **Notification reliability.** Local Notification dies with a discarded tab. â†’ Email opt-in is v1; Notification is the keep-tab-open bonus; honest copy.
9. **SSE through hospital proxies.** â†’ `onerror` exponential-backoff reconnect â†’ poll fallback; server keepalive `: ping`; re-sync from `/api/status` on reconnect; seed initial state at analyze-time.
10. **False reassurance on normal studies** (with ~40â€“45% accuracy). â†’ Qualified, non-dismissive empty-study copy + prominent disclaimer; clinician sign-off on that copy specifically.
11. **Single-file growth.** â†’ Enforced section order; flag at ~5K lines; documented migration trip-wire.

---

## 11. Open Decisions for the Founder

1. **Patient authentication model â€” the biggest open question.** Does a *patient* "Connect with Claude," or does the run execute on the founder's subscription pool with no patient auth? The "Connect with Claude" concept is developer-facing and confusing for a patient-first product ("why do I need a Claude account to read my MRI?"). **Recommendation:** remove it from the patient flow; replace Home State G with an **at-capacity/queued** state. This plan assumes that pending your call.
2. **Notification mechanism for v1.** Email opt-in (recommended, survives tab close) vs Browser-Notification-only (free, but breaks on tab close) vs both. Email requires an SMTP/transactional sender and storing an email under medical context (opt-in only).
3. **Privacy copy vs reality.** Approve the TTL window (e.g., "deleted after N hours") and confirm the cleanup job is in scope before the PrivacyChip claims it. Which `demographics`/PHI fields may appear in UI/PDF?
4. **Anatomy-asset clinical sign-off.** Each regional diagram + landmark map needs your (or a clinician's) approval before M6 enables it. Confirm the review process and the priority order (spine first, then the two most-requested regions). Confirm `cardiac` stays on full-body fallback until a true cardiac asset exists.
5. **Longitudinal compare (deferred).** The Read's `change_over_time` block can't populate without a **second-study upload path** (today only `prior_reports` *text* exists on `AnalyzeRequest`). Defer with a minimal future hook (`prior_study_id` + a "compare to an earlier scan" affordance) or accept the block stays dead for v1?
6. **First-run founder moment (deferred).** A one-time, dismissible intro (the cauda-equina origin story) on first visit (`localStorage` flag) vs only the persistent `FounderCard`? This is the emotional core â€” worth a small dedicated moment?
7. **Lite-mode Read scope.** Agent mode is the production path and the only one with the full `patient` block. Is lite-mode Read in scope for v1 (adapter from `interpretation`, named-figure gallery, no per-finding proof), or explicitly out-of-scope until agent-only ships?
8. **PDF brand consistency.** Re-color `report_builder.py` certainty palette to single-accent now (matches the Read), or accept a documented teal/amber/grey PDF for v1?
9. **Copy ownership & i18n.** Centralized `COPY` object (recommended, the i18n seam) with ~8th-grade reading level and clinician sign-off on patient/disclaimer/normal-study copy. The backend already detects German anatomy terms (LWS, HWS, Leber, Niere, Kopf) â€” is non-English UI a near-term need or an explicit deferral?

---

**Files this plan binds to (absolute):**
`C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\frontend\index.html` Â·
`C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\backend\app.py` (analyze `mode` return 329/354; SSE 383â€“403; report 485â€“503; images 523â€“534; agent pipeline + asymptotic progress 550â€“639) Â·
`C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\backend\services\agent_runner.py` (patient block 419â€“432; tierâ†’certainty 436â€“437; figure stems; anatomy/modality detection ~462â€“465) Â·
`C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\backend\services\report_builder.py` (patient dict docstring 19â€“30; certainty palette 38â€“44) Â·
`C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\backend\services\verification.py` (gate, quality_notes) Â·
mockups `frontend\assets\mockups\{home,read}.png` Â· anatomy `frontend\assets\anatomy\{body,brain,knee,chest,abdomen,head-neck,hip,shoulder,vascular,breast,prostate}.png` Â· `frontend\assets\brand\founder-husam.png`.

---

## Amendment A — Voice & Founder Presence (supersedes mockup copy)

**1. MIKA speaks in the first person ("I"), not "we".** All system / process / reassurance / coverage copy uses MIKA's singular voice — "I'm reading your study…", "I'll notify you when it's ready", "I can read this area". Encode in the `COPY` object. The we→I sweep covers the Wait reassurance, the "notify you" lines (top bar + Wait), and the Home coverage legend.

**2. Findings stay declarative — never "I found / I diagnose".** MIKA's "I" is for what it is *doing* and for *addressing the user*. Clinical findings stay stated declaratively with certainty labels ("The main finding is…", "A disc at L5–S1 presses on a nerve root — Confirmed") to avoid over-claiming authority. The legal disclaimer is verbatim and uses third-person "MIKA".

**3. No founder photo in the sidebar.** Remove the photo founder-card. The founder origin note moves to a dedicated **About / first-run moment**, in Husam's *signed* first-person ("— Husam") — the one place "I" is the founder's, disambiguated by the signature. The sidebar carries at most a quiet text "About MIKA" link. The Wait reassurance becomes MIKA's voice (e.g. "Being thorough takes time. I'm reading your study carefully."), unsigned, no photo. `brand/founder-husam.png` is used only in the About moment, and only if the founder chooses to show it there (otherwise the MIKA mark).

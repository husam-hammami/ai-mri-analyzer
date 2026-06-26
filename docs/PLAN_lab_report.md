# PLAN — MIKA Lab-Report ("The Verdict") feature

> Forged in /warcry (2026-06-26). Design finalized in `docs/Mika_Lab_Report_Concept.md`
> (§ "✅ FINALIZED — locked for /warcry"). This plan is the build spec for /katana.
> Reviewed ✓ (bulletproof, 2 rounds) — VERDICT: SUFFICIENT. All 4 must-haves closed against the code; two
> adjacent seams (`_list_reports`, `/api/reconcile`) + the Phase-0 token-origin obligation folded in.

## Goal
A separate lab/blood-report flow in MIKA: a patient uploads a lab report (PDF or photo), Claude Opus reads it
directly (no DICOM/measurement pipeline) and the app renders "The Verdict" — one calm plain-language takeaway,
an "N to review" count, flagged-value cards, and a collapsed list of normals. **Done-when:** uploading a blood
report on the new Lab-report surface produces a Verdict page that (a) reuses the existing upload→SSE→durable-disk
pipeline, (b) renders flagged items as `.frow` cards + normals as one `.cov-row`, (c) shows the Stream hero, and
(d) **never over-soothes a genuinely flagged report** (the safety gate holds), confirmed running locally.

## Approach (and why)
**One generic lab flow grafted onto the existing pipeline at the `analyze` fork**, with the verdict tone computed
deterministically in Python (not by the model). Opus reads the rendered report page images as vision input and
returns structured per-analyte data + extraction-quality signals; Python turns those signals into the verdict and
the flagged/normal split.

**Why this approach:**
- Reuses `POST /api/upload` (job_id, JOBS cache, disk dirs), the SSE `GET /api/status/{id}/stream`, the
  `report.json`/`meta.json` persistence, and the disk-fallback read endpoints — zero new infra.
- The pre-mortem's #1 failure (over-soothing verdict) is structurally prevented by moving verdict-tone selection
  OUT of the prompt and INTO a deterministic Python gate keyed on measured signals.
- Backend-side PDF→image (PyMuPDF pixmap) gives us ONE rendered page image that serves both Opus vision and the
  proof view — so "see it on your report" shows exactly what Opus read, with no fabricated coordinates.

**Rejected alternatives:**
- *Trust the prompt for verdict tone* — rejected: Opus may not self-label uncertainty; an over-soothing line on a
  flagged report is the dangerous failure and the verdict has the least visual scaffolding. Gate must be code.
- *`claude -p` subprocess as the lab-read transport* — rejected: the headless agent sends NO inline images; it
  reads files off disk with its own Read tool via `--add-dir` (`agent_runner.py:1236-1263, 949-955`), which is an
  agentic run that (a) inherits the nested-`claude -p` hang (INCIDENTS #1) and (b) is hard to constrain to strict
  JSON. The SDK `messages.create` image-block path (`claude_interpreter.py:240-258`) is a single deterministic
  vision call AND supports **subscription auth via `auth_token`** (oauth bearer, `build_anthropic_client`,
  `claude_interpreter.py:53-58`) — so it needs no API key and matches MIKA's posture. SDK is PRIMARY; the agentic
  disk-read is the fallback only.
- *English-only verdict string + LLM Arabic translation* — rejected: re-translating a deterministic verdict
  through `arabic.build_ar_patient`'s LLM pass would re-introduce non-determinism (INCIDENTS #3) and `gate_ar_field`
  was built for imaging findings, not a tone-bound verdict. The verdict is a fixed per-language TEMPLATE keyed on
  `(n, max_tier, quality)` — see the safety gate.
- *Client-side PDF→image conversion* — rejected: the proof view must show the SAME image Opus read; rendering
  backend-side keeps Opus input and the proof crop identical and avoids fragile browser PDF handling.
- *Per-organ "lit body" hero / localization* — already dropped in the finalized concept.

## Verdict-bearing writers — the COMPLETE enumeration (INCIDENTS #1: gate must be the LAST writer)
The verdict-meaning ("am I okay?") can reach the patient through these surfaces. The Python gate must be the only
source of verdict tone on EVERY one:
1. **English on-screen / `GET /api/report` from disk** — renders `overall.takeaway` straight from the gated
   `report.json`. ✅ gated at source.
2. **Arabic report** (`MIKA_AR_ENABLED`, `?lang=ar`) — the lab verdict does NOT pass through
   `arabic.build_ar_patient`'s LLM translation. It is rendered from a fixed **Arabic template glossary** keyed on
   the same `(n, max_tier, quality)` (mirrors `arabic.py` `REPORT_DISCLAIMER_AR`/`confidence_label_ar`). Same key →
   same words in both languages; determinism preserved cross-language.
3. **Case-chat** (`MIKA_CHAT_ENABLED`) — **DISABLED for `kind:'lab'` in v1** (explicit, not an accidental 404).
   `app.py`'s chat-eligibility check returns a clean "chat not available for lab reports yet" for lab jobs, so an
   ungated `claude -p` can never free-narrate the takeaway. (Future: feed chat the gated verdict + structured
   `results[]` under a lab-specific ruleset.)
4. **Downloadable PDF** — not produced for lab in v1 (no second writer exists to bypass the gate).

## Phased steps

### Phase 0 — Transport spike (BLOCKER — do before any schema/UI work)
**Prove the lab read returns valid structured JSON from page images before building Phases 1-3.** Write a throwaway
script that renders one sample lab page → PNG, sends it through `ClaudeInterpreter`'s SDK image-block path
(`messages.create`, base64 image) authenticated with the subscription `auth_token` (no API key), and confirms it
returns parseable JSON matching the lab schema.
- **Run this from a REAL terminal/worker, never nested inside a Claude session** (INCIDENTS #2: nested `claude -p`
  hangs — and even the SDK call needs live network + a valid token, so it cannot be self-verified from inside this
  build session). The build agent does NOT execute a live read; it ships the path + a fixture and the live spike is
  a documented manual step.
- **PASS/FAIL obligation — document where the subscription token comes from.** In the default desktop posture
  (`claude /login` only, no API key, no `ANTHROPIC_AUTH_TOKEN`), `build_anthropic_client` gets NO creds and the SDK
  cannot authenticate — only the agentic `claude -p` reads its own `~/.claude` login token-free (`agent_runner.py:
  703-706`). So the SDK-primary claim holds ONLY if the packaged app actually obtains a token (e.g. surfaces
  `claude setup-token` → `ANTHROPIC_AUTH_TOKEN`). Phase 0 must record the token's origin.
- Decision tree: SDK image-block under a real `auth_token`/key → PRIMARY. **If the app has no token source → commit
  up front to the agentic `claude -p` worker-only disk-read** (render page PNGs to the job dir, `--add-dir`, instruct
  a tightly-constrained JSON-only read, parse-retry on the envelope `result`) — the path the rest of MIKA already
  uses and that authenticates in production. Run worker/terminal-only, never nested (INCIDENTS #2). Record which path
  validated. **Do not build Phases 1-3 assuming a transport the spike hasn't confirmed.**

### Phase 1 — Backend: lab read path
1. **`backend/prompts/lab_master.py`** — a single generic lab/bloodwork prompt + JSON schema. Prepend `BASE_RULES`
   (anti-hallucination, "never fabricate a value/range", "never strengthen a hedged claim"). The prompt MUST:
   - Read only what is visibly printed; if a value/unit/range is not clearly legible, mark it low-clarity, never guess.
   - Never auto-infer a reference range that is not printed on the report (`ref_range_text: null` if absent).
   - Preserve the printed unit and language verbatim; never convert units.
   - Return per-analyte: `plain_name`, `analyte_raw`, `value`, `unit`, `ref_range_text`, `range_type`
     (`two_sided_numeric`|`one_sided`|`qualitative`), `status` (`low`|`normal`|`high`|`abnormal`|`unknown`),
     `severity_phrase`, `confidence` (`Confirmed`|`Likely`|`Possible`), `plain_meaning`, `clarity` (0–1),
     `page_index`, `source_text` (the exact text it read).
   - Return read-level signals: `extraction_confidence` (0–1), `analytes_parsed`, `render_quality`
     (`clear`|`degraded`|`unreadable`), and a list of `unmapped`/uncertain analytes.
   - **NOT** return verdict prose (Python composes it).
2. **`backend/services/lab_reader.py`** — the focused lab read service:
   - `render_pages(upload_path) -> [png_path,...]`: PDF → page PNGs via PyMuPDF (`fitz.open().load_page().
     get_pixmap()`), cap at 8 pages (reject >20 with the honest "too large" error); image upload → single page.
     Normalise output paths with the existing `_rel_to_job()`/`_safe_join()` portability pattern.
   - `read_labs(job_id, page_pngs) -> dict`: send the page images + `lab_master` prompt to Claude via the
     **transport validated in Phase 0** — PRIMARY = `ClaudeInterpreter` SDK image-block path (`messages.create`,
     base64 `image_content_blocks`, `claude_interpreter.py:240-258`) under subscription `auth_token` (oauth bearer,
     no API key); fallback = SDK-with-key, then agentic `claude -p` disk-read (worker-only). Parse + `json.loads`
     the structured result and validate against the lab schema.
   - **The deterministic safety gate** (`compose_verdict(results, signals, lang) -> verdict`), the #1 hardening.
     It returns a verdict KEY; the prose is a fixed per-language **template lookup** (EN + AR glossary, mirrors
     `arabic.py REPORT_DISCLAIMER_AR`) — never an LLM string, never translated by an LLM:
     ```
     flagged = [r for r in results
                if r.status not in ('normal','unknown')
                and r.confidence in ('Confirmed','Likely')
                and r.clarity >= 0.7]
     n = len(flagged); max_tier = highest confidence tier among flagged
     all_clean = (n == 0
                  and all(r.status == 'normal' and r.clarity >= 0.7 for r in results)
                  and signals.extraction_confidence >= 0.85 and parsed_ratio >= 0.95)
     if extraction_confidence < 0.85 or parsed_ratio < 0.7 or render_quality == 'unreadable':
         key = 'NEUTRAL'        # "MIKA read your report — please review the values with your doctor."
     elif all_clean:            key = 'ALL_CLEAN'   # SCOPED, not absolute (see under-call note)
     elif n == 0:               key = 'NONE_FLAGGED_PARTIAL'  # read ok but not every analyte clean/clear
     elif max_tier == 'Possible': key = 'POSSIBLE_ONLY'
     elif n <= 2:               key = 'FEW'   # "{n} thing(s) worth a look."
     else:                      key = 'SEVERAL'
     ```
     **Close the under-call hole (INCIDENTS #4 / the CXR ~50%-sensitivity profile, memory `mika-accuracy-
     findings`).** A single conservative read can MISS a real flag → `n==0`. So the all-normal verdict is **SCOPED,
     never absolute**: `ALL_CLEAN` → *"Nothing stood out in what MIKA could read — this describes only the values on
     this report, not a clean bill of health."* There is no "Everything looks normal." absolute string. `n==0` that
     isn't fully clean/clear degrades to `NONE_FLAGGED_PARTIAL` (scoped + "share the full report with your doctor"),
     not reassurance. Verdict prose comes ONLY from these fixed per-language templates; counts ONLY from structured
     `results`. No diagnosis/treatment words anywhere. (v1 has no second-reader; the scoped wording carries the
     honesty about misses.)
   - Build the final `report.json` payload (shape in Data model below) + write `meta.json` via a **dedicated lab
     persistence path** — NOT through `_persist_report` (`app.py:1297-1352`, which is DICOM-coupled: `seqthumb*`
     thumb, `detected_anatomy`, `evidence_manifest`, `artifact_registry`, `_find_patient_pdf`). The lab path writes
     `report.json` + a lab `meta.json` (`kind:'lab'`, page-image map, no thumb/anatomy/pdf) directly via the
     low-level disk helpers, reusing only the path-portability (`_rel_to_job`/`_safe_join`) discipline. Lab runs
     produce NO annotated figures and (v1) NO downloadable report.pdf.
3. **`backend/app.py` wiring** (branch lab BEFORE every DICOM-shaped path):
   - `POST /api/upload`: accept lab uploads (PDF/PNG/JPG); no FormatConverter/DICOM for lab.
   - `POST /api/analyze`: add `kind` param (`dicom` default | `lab`). On `kind:'lab'`, run the lab_reader path on
     the worker, emitting SSE progress with lab phases ("Reading the values · finding what stands out · putting it
     in plain words"). Reuse the same job-status/SSE machinery.
   - **`GET /api/report/{id}`**: for a lab job, **early-return the lab `report.json` from disk BEFORE**
     `_build_report_payload` (`app.py:2184`) and `_normalize_loaded_report` (`app.py:638`) — both backfill DICOM
     contract fields (interpretation/measurements/anatomy/`agent.summary`) and would mangle or throw on the lab
     shape. The live response and the disk response must be the same lab payload (byte-identical).
   - **`_english_report_payload` / Arabic attach** (`app.py:2307`): for `kind:'lab'`, skip the imaging payload
     build; serve the lab payload; the Arabic verdict comes from the template glossary (see writers §2), NOT
     `build_ar_patient`.
   - **Case-chat eligibility** (`app.py:2381-2410`): return a clean "not available for lab yet" for `kind:'lab'`
     (writers §3) — never 404-ambiguous, never free-narrate.
   - **`_list_reports`** (`app.py:1441`): branch on `meta['kind']=='lab'` and build the Recent entry **from `meta`
     alone — do NOT run `_normalize_loaded_report` (DICOM-shaped) on a lab dir.** The loop's outer `try/except`
     (`app.py:1464`) would otherwise drop every report after a failing one. The frontend Recent renderer already
     tolerates a null thumb (`index.html:1867`), so a lab entry with no thumb degrades gracefully.
   - **`/api/reconcile`** (`app.py:2420`): reject `kind:'lab'` cleanly — it calls `_build_report_payload`/
     `_normalize_loaded_report` directly (outside the `_english_report_payload` early-return) and reconciliation is
     DICOM-only. Same for any other direct caller of those builders (`_rehydrate_completed_job` `app.py:1479`).
   - Persist `kind:'lab'` in `meta.json` so all the above route correctly after a restart.
   - New `GET /api/report/{id}/page/{n}` (or reuse the existing image endpoint, `_safe_join`-confined) to serve a
     rendered report page PNG for the proof view.

### Phase 2 — Frontend: the Verdict page (`frontend/index.html`)
4. **Screen states + routing** (real surgery on the one shared state machine — not a clean add):
   - `screenFor(job)` (`index.html:1272-1275`) maps purely on `job.status`: `complete→'read'`. A completed lab job
     is also `status==='complete'`, so without a discriminator it resolves to the imaging `'read'` screen. **Edit
     `screenFor` to branch on `job.kind`**: `complete && kind==='lab' → 'lab-read'`; `pending/uploading && lab →
     'lab-home'`; wait is shared. Thread `kind` into the single `job` state object on upload/analyze.
   - Preserve the `dm`/demo hook (`index.html:2976`, `screen = dm ? … : screenFor(job)`); add `?demo=lab-read` /
     `?demo=lab-home` cases so the lab screens are reachable for visual checks without a live read.
5. **Sidebar nav**: add a `navItem('lab', I.<droplet/flask>, L('Lab report'), …)` entry (new inline SVG icon).
6. **lab-home**: reuse `UploadDropzone` (`.dropzone`) with copy "Add your lab or blood report"; on upload, call
   `api.upload` then `api.analyze({job_id, kind:'lab'})`; subscribe via the existing `watch()` SSE.
7. **wait**: the existing wait screen with lab phase copy; the Stream hero may appear here as the calm artifact.
8. **lab-read — "The Verdict"** (top→bottom), all on existing primitives:
   - `.read-topnote` disclaimer (reused).
   - **Verdict header**: large slate-ink takeaway (the Python-composed verdict string) + `.opill` confidence +
     an accent **"N to review"** count token + the reassurance-ratio line ("22 of 24 checked and look normal").
   - **The Stream** hero card (aside right / below on mobile): the renamed `bloodwork (3).png` →
     `frontend/assets/brand/lab-stream.png`, `object-fit:cover`, `--accent-line` ring + `--accent-glow`, soft
     vignette. Atmospheric only; no bound data. Move `bloodwork (1)/(2).png` out of the shipped path.
   - **Flagged cards** as `.frow` rows (number, `.cchip` tier, plain name, `severity_phrase`), top card
     pre-expanded via `.frow.active`. `.frow-extra` holds:
     - "See the numbers" → `value` + `ref_range_text` + the **range bar** (renders ONLY when
       `range_type == 'two_sided_numeric'` with two numeric bounds; else plain text; clamp extreme to a labelled
       end-cap; never fabricate a tick).
     - "See it on your report" → the rendered page image (`/api/report/{id}/page/{page_index}`) with the quoted
       `source_text` above it ("MIKA read: '…'"). **v1: no bounding box** — full page + quote (honest fallback).
   - **Collapsed normals**: one `.cov-row` ("N of M checked, all normal", expandable), descriptive-not-diagnostic,
     with an "Other results" bucket for unmapped/low-clarity analytes ("could not read clearly").
9. **i18n**: add a `COPY.lab` block + `AR_UI` Arabic strings; all lab copy through `L()`; logical properties;
   numerals/units in `dir`-isolated spans.

### Phase 3 — States, verify, polish
10. Implement the four states: all-normal (no card stack, ratio line + accent check), many-flagged (honest,
    most-off-first, normals still collapse), error/unreadable (calm error register, no fake results), empty
    (dropzone). Add a `?demo=lab-read` dev gate mirroring the existing `?demo=` hook for visual checks.
11. Run MIKA locally; upload a sample blood report (use `bloodwork`-style or a real lab PDF); confirm the full
    flow end-to-end, the safety gate on a flagged report, and the all-normal calm state.

## Files & surfaces touched
- **New:** `backend/prompts/lab_master.py`, `backend/services/lab_reader.py`, `frontend/assets/brand/lab-stream.png`
  (renamed from `bloodwork (3).png`), `docs/PLAN_lab_report.md` (this).
- **Edited:** `backend/app.py` (upload/analyze `kind` branch, lab phases, page-image endpoint, meta routing),
  `backend/prompts/__init__.py` (register lab prompt if routed there), `frontend/index.html` (screen states,
  sidebar, Verdict page, range bar, proof view, `COPY.lab`/`AR_UI`).
- **Removed from shipped path:** `frontend/assets/bloodwork (1).png`, `(2).png`.

## Data & schema changes
No DB. New `report.json` shape for `kind:'lab'`:
```json
{
  "kind": "lab",
  "overall": { "takeaway": "<python-composed verdict>", "confidence": "high|moderate|low",
               "checked_count": 24, "normal_count": 22, "flagged_count": 2 },
  "results": [ { "plain_name": "Vitamin D", "analyte_raw": "25-hydroxyvitamin D",
                 "value": "18", "unit": "ng/mL", "ref_range_text": "30–100",
                 "range_type": "two_sided_numeric", "status": "low",
                 "severity_phrase": "a bit low", "confidence": "Likely",
                 "plain_meaning": "low vitamin D is common and can leave you tired",
                 "clarity": 0.92, "page_index": 0, "source_text": "Vitamin D 18 (30–100)" } ],
  "signals": { "extraction_confidence": 0.93, "analytes_parsed": 24, "render_quality": "clear" }
}
```
`meta.json` adds `kind:'lab'` and the page-image map; no `pdf_path` required.

## Test & verification strategy
- **Safety-gate unit checks** (the non-negotiable, pure-Python, NO Claude call — testable from inside this session):
  a results-fixture with a clear high flag must NEVER yield a reassuring/normal verdict; `extraction_confidence <
  0.85` or `parsed_ratio < 0.7` or `render_quality=='unreadable'` forces the neutral template; a fully-clean
  high-quality read yields the SCOPED `ALL_CLEAN` line (never an absolute "Everything looks normal"); an `n==0`
  read that isn't fully clean/clear yields `NONE_FLAGGED_PARTIAL`, not reassurance; Possible-only flags yield the
  lower-certainty line. **Run the SAME `(n, tier, quality)` keys through both the EN and AR template glossaries and
  assert both resolve to a fixed string** (verdict determinism cross-language; INCIDENTS #1/#3).
- **Writer-coverage assertion** (INCIDENTS #1): assert the lab Arabic verdict is a glossary lookup, not a
  `build_ar_patient` LLM call; assert case-chat returns the explicit "not available for lab" for `kind:'lab'`.
- **Range-bar guard:** one-sided/qualitative/None ranges render NO bar; two-sided numeric renders a bar with the
  tick in-range; off-scale clamps to an end-cap, never a fabricated position.
- **Proof view:** shows the rendered page + quoted `source_text`; never a pixel box.
- **End-to-end run (worker/terminal-only — NEVER nested in a Claude session, INCIDENTS #2):** start MIKA via
  launch.json "mika" from a real terminal; upload a lab report; walk the flow. The live Claude read can only be
  exercised this way. Inside the build session, verification is limited to: the pure-Python gate tests, the
  `?demo=lab-read`/`lab-home` visual screens (fixture data, no Claude), and static/preview audits.
- **UI audit (on the demo screens):** screenshot the Verdict, a flagged card expanded (numbers + proof), the
  scoped all-normal state, and the error state; audit for AI-tell colors / red leaking into chrome / dead voids /
  zoom breakage before declaring done.
- **Restart byte-identity:** after a completed lab run, restart the server and confirm `GET /api/report/{id}`
  returns the lab Verdict from disk **byte-identical to the live response** (proves the lab read/persist branch
  bypasses the DICOM-shaped payload builders).
- **Recent-list non-regression:** a persisted lab job appears in `GET /api/reports` AND every imaging entry still
  lists (proves `_list_reports` skips `_normalize_loaded_report` for lab and the loop doesn't abort).

## Rollout & rollback
- Single branch `feat/lab-report`; main untouched. Feature reachable via the new sidebar entry; a
  `MIKA_LAB_ENABLED` flag (default on for the branch) lets the entry be hidden if needed, mirroring the existing
  `MIKA_CHAT_ENABLED` flag pattern. Rollback = drop the branch / flag off; imaging flow is structurally unaffected
  (lab is an additive `kind` branch, never altering the DICOM path).

## Risks → mitigations
| Risk (from pre-mortem) | Mitigation in this plan |
|---|---|
| Over-soothing verdict on a flagged report | Verdict tone computed in Python from structured signals, fixed templates, gated on count + tier + quality; reassurance only at `n==0` + good quality. |
| Opus fabricates a value/unit/range from a poor photo | Prompt forbids guessing; per-analyte `clarity`; low-clarity items excluded from cards, routed to "Other results"; `ref_range_text:null` when not printed; never convert units. |
| Proof-crop false attribution | v1 shows full rendered page + quoted `source_text`, no model-supplied box; precise highlight deferred. |
| Range bar misleads on non-two-sided ranges | Bar renders only for `two_sided_numeric`; else plain text; clamp extremes; never fabricate a tick. |
| Partial read silently under-reports | `analytes_parsed` + `render_quality`; `parsed_ratio < 0.7` forces the "read most of your report" neutral template, never silent omission. |
| Missing pdf_path breaks read/meta endpoints for lab | Lab uses a dedicated persist/read path that never requires a thumb/pdf; `GET /api/reports` tolerates a lab entry with no images. |
| Implying diagnosis/treatment | No diagnosis/drug words in verdict or cards; descriptive-not-diagnostic normals; reuse the persistent disclaimer. |
| **Verdict gate bypassed by a 2nd writer (INCIDENTS #1)** | Verdict tone is a fixed per-language template keyed on `(n,tier,quality)`; Arabic = glossary lookup (no LLM translation); case-chat disabled for lab; no lab PDF. All verdict writers enumerated above. |
| **Under-call → false reassurance (INCIDENTS #4 / CXR profile)** | No absolute "all normal"; `ALL_CLEAN` is SCOPED ("what MIKA could read… not a clean bill of health"); `n==0` not-fully-clean → partial template. |
| **Claude image transport unproven (INCIDENTS #2)** | Phase 0 spike proves the SDK image-block path under subscription `auth_token` returns valid lab JSON BEFORE Phases 1-3; live read runs worker/terminal-only, never nested; gate tested with a fixture. |
| **Live-vs-disk report asymmetry / DICOM-shaped paths** | Lab read early-returns the lab payload before `_build_report_payload`/`_normalize_loaded_report`; dedicated lab persist path (not `_persist_report`); restart byte-identity test. |
| **`screenFor` collision** | `screenFor` branches on `job.kind`; `kind` threaded into job state; `?demo=lab-*` hooks preserved. |

## Out of scope (v1)
- Precise OCR-bbox proof highlighting (full-page + quote is the honest v1).
- Downloadable lab report.pdf (on-screen Verdict only).
- A reference-range database / unit-conversion (Opus uses only printed ranges, verbatim).
- Trend/longitudinal lab comparison across uploads.
- Body-system grouping beyond the optional in-drawer view at ≥20 results.

## Success criteria (restated)
1. New "Lab report" surface: upload PDF/photo → SSE wait → Verdict page, reusing the existing pipeline end-to-end.
2. Flagged items as `.frow` cards (top pre-expanded) with on-demand numbers (+ range bar where valid) and the
   page-image proof; normals collapse to one `.cov-row`.
3. The Stream hero renders contained on the light canvas; no red/amber/green in UI chrome.
4. **The safety gate holds**: a genuinely flagged report never yields a reassuring verdict; low-quality/partial
   reads fall back to the neutral template. Verified by unit checks + a manual flagged-report run.
5. MIKA runs locally with the feature intact; imaging flow unaffected; lab reports survive a restart.

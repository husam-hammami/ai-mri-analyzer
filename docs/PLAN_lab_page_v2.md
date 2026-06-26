# PLAN — Lab Report page v2 ("The Verdict" → named assessment, master/detail, scroll fix, chat)

> Forged in /warcry (2026-06-27). War-council: 5 read-only scouts (prior-art, codebase cartographer,
> pre-mortem, assessment-architecture, design/UX).
> Reviewed ✓ /eagleye (ON TRACK — plan maps 1:1 to the 6-point ask, no drift).
> Reviewed ✓ /bulletproof — VERDICT: SUFFICIENT (round 2; 5 must-haves resolved: deterministic
> marker-trigger assessment, chat answer-replacement gate, clarity-floor scrape test, server-side
> analyte_key fallback, chat-dock reflow CSS + secondaries cut; bilingual chat safe-template folded in).
> Status: APPROVED → /katana.
> Owner decision (load-bearing): the lab page may **name the likely condition directly** (still NO
> treatment/drugs/dosing). This PARTIALLY OVERRIDES the previously-locked "never name a diagnosis"
> verdict gate in `docs/Mika_Lab_Report_Concept.md` §5 and the memory `mika-lab-report-feature`.

## Goal / Done-when
Turn the patient-facing Lab Report page into an optimal, reachable, trustworthy read:
1. A real **final assessment** that names the likely condition + its supporting markers (the X and Y),
   plus a **patient header** (name/age/sex) when present on the report.
2. Numbers/proof live in a **detail pane that uses the page width**, not inflating each card.
3. A clear, compact **range indicator** (normal band + value marker + low/high direction), color-free.
4. Better per-reading **visual indicators + tasteful card decoration/motion** (brand-locked).
5. **Scroll works** — every reading is reachable at 80/100/125% zoom; nothing crops.
6. A **lab chat** to discuss the findings (mirrors imaging), grounded + safety-gated.

**Done-when:** `?demo=lab-read` renders the named assessment + demographics + master/detail + new range
bar with zero forbidden hues and zero console errors; the page scrolls (measured `scrollHeight >
clientHeight`, all flagged cards reachable) at all three zooms; lab chat opens, sends, and is gated;
the deterministic assessment + verdict gates have passing unit tests; `python -c "import app"` imports
clean. Live Opus behavior is validated on a REAL terminal post-build (cannot self-verify in-session).

## Approach (+ why) and rejected alternatives
**Assessment = "Python DERIVES the condition from the flagged-marker pattern; the model proposal is only
confirmation/coverage."** (Revised per bulletproof must-have #2 — INCIDENTS #5: a non-deterministic
model verdict must not mutate clinical output.) The deterministic trigger is the marker pattern, not the
model: if the flagged results satisfy a whitelist entry's `required` pattern, that condition is surfaced
**regardless of whether the model named it** — and a model-named condition whose pattern is NOT satisfied
is suppressed. This makes the condition's PRESENCE a pure function of `flagged` (reproducible across
reruns). The model's structured proposal is consumed ONLY to (a) extend coverage for whitelist patterns
the rule table phrases conservatively and (b) order/confirm — never as the sole trigger, and its free
prose never reaches the patient. Every surfaced claim is bounded by the whitelist + marker-presence +
red-flag exclusion + treatment-term strip, reusing the exact discipline of `compose_verdict`.

- **Rejected — "model proposes → Python validates" (model as trigger):** makes presence non-reproducible
  (run A names it, run B omits it → two different patient assessments for one study; INCIDENTS #5). We
  invert it: rules trigger, model confirms.
- **Rejected — Pure Python rules with NO model input:** fine for the trigger, but the model proposal
  still earns its place as a coverage/ordering signal layered on top of the deterministic trigger.
- **Rejected — Model freeform prose (C):** highest over-reach/false-reassurance risk; un-bounded;
  un-verifiable live (nested-`claude -p` hang). This is the dangerous path the pre-mortem flagged.
- **Rejected — extend the upload form to collect demographics:** owner wants them *from the report*; a
  form adds friction and is unasked scope. We read them from the report, display-only.

**i18n win:** the displayed condition NAME **and its explanation** both come from the whitelist canonical
templates (EN+AR), never raw model text — so the entire assessment header is deterministic and bilingual.
(This closes the AR-explanation gap; raw model `plain_explanation` is dropped entirely.)

## The assessment architecture (detail)

### Schema additions
`backend/prompts/lab_master.py` — extend `LAB_MASTER_PROMPT` + `LAB_OUTPUT_SCHEMA`:
- New top-level `patient` block (display-only; read from the report, do NOT infer):
  `{ "name": str|null, "age": str|null, "sex": str|null }`. Prompt: read these ONLY if clearly printed
  on the report header; otherwise null. NEVER guess. They are display-only and MUST NOT change any
  status classification (status stays bound to the printed range).
- New top-level `assessment` block (the model's PROPOSAL — CONFIRMATION/COVERAGE ONLY, never the
  trigger; its free prose is NOT surfaced):
  `{ "proposed_condition": str|null, "supporting_analytes": [analyte_raw strings],
  "model_confidence": "probable"|"possible"|"unconfirmed" }`.
  Prompt rules: propose a condition ONLY from clearly-abnormal printed values; supporting_analytes MUST
  be analytes you marked abnormal; NO treatment/drug/dose; NO procedure; if uncertain → null. Do NOT
  name cancer/leukemia/lymphoma/any malignancy or any acute emergency — set proposed_condition=null and
  let the values speak. (`plain_explanation` is dropped from the schema — explanations come from the
  bilingual whitelist templates, not the model.)
- Per-result: add `analyte_key` (normalized lowercase slug, e.g. "hemoglobin","hgb","haemoglobin" →
  `hemoglobin`) alongside `analyte_raw`/`plain_name`, for robust matching. **Server-side fallback
  (must-have #4):** if the model omits/garbles `analyte_key`, `_parse_lab_json` derives it from
  `analyte_raw`/`plain_name` via the SAME normalization slug table the whitelist uses — so condition
  matching never depends on the model populating the new field (otherwise the feature could go silently
  dark, undetectable in-session per the nested-hang trap).

### The deterministic gate — new `compose_assessment()` in `backend/services/lab_reader.py`
Pure Python, no Claude, unit-tested (mirrors `compose_verdict`). Inputs: the validated `results` +
`signals` + the model's `assessment` proposal (advisory) + `lang`. **The marker pattern is the trigger;
the model is advisory only.** Algorithm:
1. Compute `flagged` with the SAME filter as `compose_verdict` (status not normal/unknown, confidence
   Confirmed/Likely, clarity ≥ `CLARITY_FLAG_FLOOR`). If `flagged` is empty → return `None` (no
   condition; the verdict carries the signal).
2. **DERIVE candidates from the markers:** scan the CONDITION WHITELIST; an entry is a CANDIDATE iff its
   `required` marker pattern is satisfied by `flagged` (matched by `analyte_key` + direction). No
   candidate → `None` (silent downgrade to the honest grouped-markers verdict fallback).
3. **Pick the PRIMARY** deterministically: most markers matched, tie-broken by most-off severity, then a
   fixed whitelist priority order (stable, no RNG). v1 surfaces the PRIMARY only.
4. **Model is advisory:** if the model's `proposed_condition` maps to a whitelist entry whose pattern is
   ALSO satisfied, it may break a tie toward that entry; a model-proposed condition whose pattern is NOT
   satisfied is IGNORED. The model can never introduce a candidate the markers don't support.
5. `supporting` = the flagged analytes that satisfy the primary's pattern (by `analyte_key`); display
   their `plain_name`s (the "X and Y").
6. **Safety (inherent + defense-in-depth):** the surfaced NAME and EXPLANATION are the whitelist's
   curated canonical templates (no red-flag term, no treatment/drug by construction). Still assert the
   canonical `condition_key` is not on the red-flag list and the templates contain no treatment/drug
   term (a unit test guards the table itself).
7. Confidence phrase = a FIXED key derived from the pattern strength (not the model): "This set of
   results is consistent with …". `model_confidence` is advisory and never raises the surfaced phrase.
8. Return `{ condition_key, condition_name_en, condition_name_ar, explanation_en, explanation_ar,
   supporting: [plain_name…], confidence_phrase_key, source_indices }`. The DISPLAY name AND explanation
   are whitelist canonicals, never raw model text. (Secondary patterns are OUT of scope for v1 — primary
   only, per bulletproof must-have #5.)

### CONDITION WHITELIST (curated, value-defined; the safety control)
A table keyed by `condition_key` → `{ aliases:[…], required: <marker pattern over analyte_key+direction>,
name_en, name_el(plain), name_ar, explanation_en_template, explanation_ar_template }`. Initial set
(plain names, no jargon, no disease that isn't directly value-defined):
- `anemia` — low hemoglobin.
- `iron_deficiency_anemia` — low hemoglobin AND (low MCV OR low MCH OR low ferritin/iron).
- `low_b12` — low vitamin B12. `low_folate` — low folate.
- `high_cholesterol` — high LDL OR high total cholesterol OR high triglycerides.
- `high_blood_sugar` — high fasting glucose OR high HbA1c.
- `underactive_thyroid` — high TSH (± low T4). `overactive_thyroid` — low TSH (± high T4).
- `low_vitamin_d` — low 25-OH vitamin D.
- `reduced_kidney_function` — low eGFR OR high creatinine.
- `elevated_liver_enzymes` — high ALT OR high AST.
- `low_white_cells` / `high_white_cells` — plainly named (NEVER "leukemia").
**Red-flag exclusion list** (can never be surfaced even if proposed): cancer, malignancy, leukemia,
lymphoma, myeloma, tumor, sepsis, and any term not in the whitelist. These degrade to the grouped
fallback + an honest "some results are clearly outside the printed range — please review them with your
doctor soon" (no naming, but NOT falsely reassuring).

### Fallback when assessment is `None`
Keep the existing `compose_verdict` headline (FEW/SEVERAL/etc.) as the takeaway, and render the flagged
markers grouped — i.e. today's behavior. The assessment is ADDITIVE; the verdict gate is untouched and
remains the sole writer of `overall.takeaway`/counts.

### `build_lab_payload` / persistence
- Add `overall.assessment` (the `compose_assessment` output or null) and top-level `patient` to the
  payload. `compose_assessment` runs in the SAME place `compose_verdict` runs (one writer, server-side).
- `persist_lab_report`: store `patient` in **report.json only**. `meta.json` MUST NOT carry name/sex/age
  (it backs the Recent-list index → PII leak). Keep meta as today (verdict_key/counts/pages).

## Demographics (display-only)
- Read from the report by the model (`patient` block). Display in a new header meta row when any field
  present; hide entirely when all null (no empty header).
- NEVER used to compute/override status — printed ranges remain the only basis (anti sex/age misread).
- Frontend renders only on the open report (not in Recent list).

## Layout / scroll (frontend/index.html — single-file React SPA, inline)
### Scroll fix (point 5) — the real bug
`.page-inner { overflow:hidden }` (≈188) and the lab read element is `className="page-inner
page-lab-read"`. Give the lab variant its own scroll: `.page-lab-read { overflow-y:auto;
overscroll-behavior:contain; scrollbar-gutter:stable; }` and remove inner sticky traps that fight it
(`.lab-side { position:sticky; top:0 }` → keep sticky but WITHIN the now-scrollable page, verify it
doesn't trap). Verify with `scrollHeight > clientHeight` and that the LAST flagged card + drawers are
reachable at 80/100/125%. (Cartographer + design scout agree; this is the minimal robust fix.)

### Master/detail (points 2 & 3)
Restructure `LabReadScreen` (≈3290) results area into the imaging `thread + proof` pattern:
- Header zone (full width): assessment + demographics + confidence pill.
- Workspace grid: LEFT = flagged results list (`.lab-frow` rows as master, selected-state via
  `.frow.active`), RIGHT = **detail pane** showing the selected result's numbers + range bar +
  "see it on your report" proof. Default selection = the primary flagged result.
- The bloodstream `.lab-stream` visual moves to the detail-pane empty/default backdrop OR a slim header
  accent (keep low-risk; decorative only, never over proof).
- Normals/Other drawers below the workspace, full width.
- Mobile (≤920px): collapse to single column; the detail pane becomes in-card expand under the selected
  row (`.frow-extra` in place). Touch targets ≥44px.
- Chat-dock reflow (must-have #5): there is NO `.shell.chat-open .page-lab-read` rule today (only
  `.page-read` at ≈913-914). ADD an explicit `.shell.chat-open .page-lab-read .lab-workspace
  { grid-template-columns: minmax(0,1fr) }` (collapse the master/detail to one column) so the right
  detail pane doesn't overflow under `margin-inline-end:var(--chat-w)` when chat opens. Enumerated CSS
  deliverable, not just a note.

### Range bar redesign (point 3) — `LabRangeBar` (≈3207) + `.lrange*` CSS (≈569,695)
Horizontal number-line, **accent-only**: `--surface` rail (inset line), `--accent-softer` normal band,
`--accent` value tick + glow, a small direction caret/glyph + an explicit "Low"/"High"/"Well below"/
"Well above" text label (WCAG 1.4.1 — never color-only). Fixed small height. Render ONLY for
`two_sided_numeric` with both bounds parseable + numeric value; clamp out-of-range to the rail edge with
a labelled end-cap (never fabricate a position). One-sided/qualitative → no bar, show value + a plain
"Below normal"/"High"/status text. Unit tests: two-sided→bar+tick; one-sided→no bar; qualitative→no bar.

### Visual indicators + motion (point 4) — brand-locked
- Per card: a left accent rail / glyph encoding low vs high by DIRECTION + the severity phrase (no
  red/amber/green; single blue accent only). Selected = `.frow.active` accent ring (reuse ≈674/502).
- Motion (existing tokens only, per `docs/PLAN_one_pulse_motion.md`): card entrance fade+translateY(8px)
  staggered (`--stagger-step`); detail-pane content fade; range-tick draw-in on demand
  (`--draw-marker`); `.lab-stream` float/pulse stays decorative. EVERYTHING behind
  `@media (prefers-reduced-motion:reduce)` → fades/instant only. No loops on proof/values.
- Daedalus: NOT a separate detour — the design scout's spec is grounded + on-brand; katana's built-in
  design army refines during build IF the rendered result looks generic (owner's "if needed").

## Lab chat (point 6)
Backend:
- `backend/app.py` ≈2528: remove the lab block in `case_chat_endpoint`. Route lab jobs to a lab path.
- New `build_lab_context(report)` (in `case_chat.py` or a small `lab_chat` helper): ground from the
  structured `results` (plain_name, value, unit, ref_range_text, status, severity, plain_meaning),
  `overall.takeaway`, `overall.assessment` (the SURFACED, gated condition), and `patient`. Never re-read
  raw files; never re-interpret beyond what surfaced.
- New `LAB_SYSTEM_RULES`: plain language; answer ONLY from this report's analytes + the surfaced
  assessment; MAY discuss the named condition in "consistent with" framing (consistent with the owner
  decision); NEVER treatment/medication/dose/procedure; NEVER name a NEW condition beyond the surfaced
  whitelist assessment; refuse out-of-scope ("what should I take", "do I have <other disease>"); don't
  repeat the standing disclaimer.
- Deterministic last-writer backstops — a GATE, not a token-strip (must-have #1; INCIDENTS #4: the chat
  is a second, non-deterministic consumer of the condition — it must be gated like the header, because
  "no new condition" is an OPEN vocabulary you cannot strip token-by-token):
  - **Backstop ordering:** the gates run as a short-circuit chain (first trip → replace + return) so a
    multi-violation answer can't partially survive.
  - **Red-flag answer-replacement:** if the chat answer contains ANY red-flag term (the shared
    cancer/leukemia/lymphoma/myeloma/tumor/malignancy/sepsis blocklist), discard the WHOLE answer and
    return a fixed safe template. The safe template is **BILINGUAL** — keyed by the request `lang`
    (mirror `compose_verdict`'s `_TEMPLATES_AR`), NOT an English-only string (the chat answers in the
    patient's language; an English flip on the most safety-critical output is a real EN+AR hole). EN:
    "I can't speak to that from this report — please review it with your doctor." + the AR equivalent.
  - **Off-whitelist condition suppression (positive-list):** if the answer names a whitelist condition
    that is NOT the surfaced `assessment.condition_key`, discard/replace — the chat may only discuss the
    condition the gate already surfaced for THIS report, never introduce another.
  - **Treatment/drug replacement:** same blocklist as the assessment gate → replace the answer with the
    safe template (treatment is permanently out of scope, so replacement, not strip).
  - **Grounding:** never introduce a value/marker not in `results`. Persist turns to `chat.json` as today.
  - Unit-tested with `ask_claude` MOCKED returning adversarial strings ("yes, you likely have leukemia",
    "take 325mg ferrous sulfate", and an Arabic adversarial input → AR safe template) → each backstop
    must neutralize to the (correct-language) safe template.
- Routing: the lab-chat handler reuses the already-loaded `report`/`meta` it fetched for grounding
  rather than calling `_is_lab_job` independently (avoids a second disk round-trip per turn).
- Keep `case_chat.ask_claude` transport (no tools/files, subscription `claude -p`). Same nested-hang
  caveat: live chat not self-verifiable in-session.

Frontend:
- Mount `ChatDrawer` for lab jobs too (App router ≈4180: currently `chatOpen && !dm && job.id` — already
  generic; ensure it isn't gated to imaging). Wire the lab-read "Ask MIKA" trigger (TopBar ≈2215 /
  a lab-read askrow mirroring ReadScreen ≈3541). `studyLabel` = "Lab report" (+ date if present).
- `chatAvail` already from `/api/chat/availability`; no per-kind flag needed (endpoint now serves lab).

## Phased steps (commit to main per phase — solo repo)
**Phase A — Backend assessment + demographics (pure, testable):**
1. Extend `lab_master.py` schema/prompt (`patient`, `assessment`, `analyte_key`; red-flag prohibition).
2. Add `CLARITY_FLAG_FLOOR` shared constant (single source) used by `compose_verdict` +
   `compose_assessment`; document that the frontend filter must match it.
3. Implement `compose_assessment()` + the CONDITION WHITELIST + treatment/drug blocklist (EN+AR
   canonical names). Wire into the lab pipeline next to `compose_verdict`; add to `build_lab_payload`.
4. `persist_lab_report`: persist `patient` in report.json, keep it OUT of meta.json.
5. `_parse_lab_json`: normalize/keep `patient`, `assessment`; ensure each result has `analyte_key` —
   if missing/garbled, DERIVE it server-side from `analyte_raw`/`plain_name` via the shared
   normalization slug table (must-have #4), so matching never depends on the model.
6. Unit tests:
   - `compose_assessment`: marker-pattern TRIGGER (condition surfaces from flagged markers even when the
     model proposes nothing); model-advisory (a model-proposed condition whose pattern is unsatisfied is
     IGNORED; presence is a pure function of `flagged` → same input, same output across calls); red-flag
     never surfaces; null → grouped fallback (never softens); EN+AR canonical name AND explanation;
     supporting-markers = the flagged analytes matching the pattern.
   - Table guard: assert NO whitelist canonical name/explanation (EN or AR) contains a red-flag or
     treatment/drug term (guards the curated table itself).
   - `analyte_key` fallback: a result with no `analyte_key` still matches via the slug derivation.
   - Range-bar parse (two-sided→bar; one-sided→none; qualitative→none).
   - **G6 clarity-floor sync (must-have #3):** a Python test that reads `frontend/index.html`,
     regex-extracts the lab `flagged` filter's clarity literal, and asserts it `== CLARITY_FLAG_FLOOR`.
     (The only sync mechanism possible for a no-build single-file SPA — there is no shared import.)
   - Run `pytest backend/tests/`.

**Phase B — Frontend assessment header + demographics + scroll fix:**
7. Scroll fix on `.page-lab-read`; verify reachability at 3 zooms.
8. Assessment header (named condition + supporting markers + confidence phrase) + demographics row;
   fallback to today's verdict when `assessment` null. Add all new strings to `COPY.lab` + `AR_UI`
   (EN+AR). Update `DEMO_LAB_REPORT` fixture to include `patient` + `assessment` (primary + 1 secondary).

**Phase C — Frontend master/detail + range bar + motion:**
9. Restructure results into master list + right detail pane (mobile = in-card). Selected-state, focus,
   keyboard. Chat-dock reflow to single column.
10. Range-bar redesign (accent-only, direction label, degrade rules). Card decoration + motion behind
    reduced-motion. Keep clarity floor `0.5` in the frontend `flagged` filter in sync with the backend
    constant.

**Phase D — Lab chat:**
11. Backend: remove lab block; `build_lab_context` + `LAB_SYSTEM_RULES` + backstops; route lab jobs.
12. Frontend: mount ChatDrawer + trigger for lab-read; lab `studyLabel`.
13. Tests: lab chat context builder + backstops (drug-strip, no-new-condition, no-new-value) unit-tested
    with `ask_claude` mocked. `python -c "import app"` (route count sane).

**Phase E — Verify + ship:**
14. Demo gate: `?demo=lab-read` — screenshot at 100/125%, audit for forbidden hues / crops / alignment;
    confirm scroll, master/detail, range bar, chat open. Fix, re-check.
15. Real-terminal live check (NOT in-session): upload a real CBC PDF on the running server; confirm the
    named assessment surfaces correctly, no red-flag naming, no treatment text, demographics show.
16. `/sincere` copy pass on new strings (post-build).

## Files & surfaces touched
- `backend/prompts/lab_master.py` — schema/prompt (patient, assessment, analyte_key, red-flag rule).
- `backend/services/lab_reader.py` — `compose_assessment`, the CONDITION WHITELIST (with EN+AR name +
  explanation templates + `required` marker patterns), the analyte normalization slug table (shared by
  the whitelist matcher and the `_parse_lab_json` fallback), red-flag + treatment blocklists,
  `CLARITY_FLAG_FLOOR` constant, `_parse_lab_json`, `build_lab_payload`, `persist_lab_report`.
- `backend/services/case_chat.py` (or new `lab_chat.py`) — `build_lab_context`, `LAB_SYSTEM_RULES`,
  lab backstops.
- `backend/app.py` — lab pipeline wiring (assessment), `case_chat_endpoint` lab routing (remove 2528
  block), ensure `patient` flows to the lab payload.
- `frontend/index.html` — `LabReadScreen`, `LabFlagCard`, `LabRangeBar`, new detail pane, assessment +
  demographics header (selects `condition_name_ar` by `UI_LANG`, not via `L()`), lab CSS (scroll fix on
  `.page-lab-read`, master/detail, the new `.shell.chat-open .page-lab-read .lab-workspace` reflow rule,
  range bar, motion), the lab `flagged` filter clarity literal (kept == `CLARITY_FLAG_FLOOR`),
  `COPY.lab`+`AR_UI`, `DEMO_LAB_REPORT` (with `patient` + `assessment`), ChatDrawer mount/trigger for lab.
- `backend/tests/` — `test_lab_verdict_gate.py` (intentional updates), new `test_lab_assessment.py`,
  new lab-chat tests.

## Safety gates (enumerated — bulletproof must confirm each)
- G1 Whitelist-only naming: a condition surfaces ONLY if it maps to the curated whitelist AND its marker
  pattern is present in flagged results. (compose_assessment steps 2–4.)
- G2 Red-flag exclusion: cancer/leukemia/etc. can NEVER be named; degrade to grouped + "review soon".
- G3 Treatment/drug + off-whitelist control: assessment uses curated templates (no such terms by
  construction, table-guarded). Chat applies a GATE (answer-replacement to a safe template), not a
  token-strip, for red-flag / off-whitelist-condition / treatment terms.
- G3b Deterministic presence (INCIDENTS #5): the condition's presence is a pure function of `flagged`
  (marker-pattern trigger); the model is advisory only. Same input → same surfaced assessment.
- G4 No-strengthen / grounded: supporting markers must exist in flagged; chat/assessment never invent a
  value, range, marker, or condition not present.
- G5 Verdict gate untouched: `compose_verdict` remains the sole writer of takeaway/counts; assessment is
  additive and falls back to it.
- G6 Clarity-floor sync: single backend constant; frontend filter matches; a test fails on divergence.
- G7 PII: `patient` in report.json only, never meta.json; shown only on the open report.
- G8 Demographics never alter status: classification stays bound to the printed range.
- G9 Verification trap: NO live `claude -p` (read or chat) run in-session; confidence comes from pure
  fns + demo fixtures; live check is a real-terminal step.
- G10 i18n: every new UI string in EN+AR; condition NAME displayed from whitelist canonical (bilingual);
  RTL logical properties; numbers/units dir-isolated.
- G11 No forbidden hues / no traffic-light status colors anywhere in the new UI.

## Test & verification strategy
- Pure-Python unit tests for `compose_assessment`, whitelist matching, blocklists, range-bar parse,
  clarity-floor sync, verdict-gate regressions (unreadable overrides; flags drive FEW/SEVERAL).
- Frontend visual gate via `?demo=lab-read` (no backend): screenshot + hue/crop/zoom audit.
- Lab-chat: unit-test context builder + backstops with `ask_claude` mocked.
- Import smoke: `python -c "import app"`.
- LIVE (real terminal, not in-session): real CBC + a lipid panel + an all-normal report → confirm
  correct naming, red-flag safety, no treatment text, demographics, scroll/chat.

## Rollout & rollback
- Commit per phase to `main` (solo repo). Feature stays behind existing `MIKA_LAB_ENABLED`; chat behind
  `MIKA_CHAT_ENABLED`. Rollback = revert the phase commit(s); the assessment is additive so reverting it
  restores today's verdict-only behavior with no data migration.

## Risks → mitigations (from the pre-mortem)
- Over-reach naming → G1/G2 whitelist + red-flag exclusion + marker-presence.
- False reassurance (the #1 documented failure) → assessment never softens the verdict; `None` falls
  back to the honest grouped verdict; all-normal stays scoped ("not a clean bill of health").
- Verification trap → G9; tests on pure fns + fixtures; real-terminal live step.
- Clarity desync → G6 single constant + sync test.
- PII leak → G7.
- Sex/age misread → G8 (display-only).
- Chat plumbing regression → isolate lab path; keep imaging context builder untouched; unit-test both.
- Scroll fix regresses a breakpoint → test 80/100/125% + mobile; verify last card reachable.
- Range-bar fabricated position → render only for valid two-sided; clamp+label extremes; unit tests.

## Out of scope
- Sex/age-derived reference ranges (display demographics only; ranges stay printed-only).
- SECONDARY/multi-pattern conditions ("also consistent with …") — v1 surfaces the PRIMARY condition
  only (must-have #5). Multi-pattern is a deliberate later increment.
- Trend/longitudinal comparison across reports; optimal-vs-standard dual bands.
- Fixing the pre-existing English-only render of `overall.takeaway` in AR mode (the NEW assessment
  header must select `condition_name_ar` by `UI_LANG` directly, NOT inherit that raw-render bug).
- Any treatment/medication guidance (permanently out of scope).

## Success criteria (restated)
Named, value-grounded assessment + demographics render; numbers/proof use the width via master/detail;
range bar is clear, compact, color-free, and never fabricates a position; the page scrolls with every
reading reachable at all zooms; lab chat works and is safety-gated; all safety gates G1–G11 hold; pure
unit tests + import smoke pass; live behavior validated on a real terminal.

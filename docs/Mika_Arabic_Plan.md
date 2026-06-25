# MIKA — Arabic ⇄ English toggle: implementation plan

> Forged in /warcry (5 scouts → 3-judge panel incl. a skeptic) · Reviewed ✓ (bulletproof, 3 rounds) — **SUFFICIENT**.
> R1→INSUFFICIENT (3 must-haves: deterministic grade gate · pin pure-Python `python-bidi==0.4.2` · sidecar staleness guard). R2→INSUFFICIENT (grade lexicon under-scoped → full vocabulary + deny-by-default). R3→SUFFICIENT.

**Goal.** An Arabic-speaking patient/clinician uses MIKA fully in Arabic, right-to-left: the UI chrome,
the upload/reading flow, the report (on-screen **and** the downloadable PDF), and the planned case-chat.
**Success signal:** flip the toggle and a real completed study reads end-to-end in correct, *safe* Arabic
RTL — findings, certainty, disclaimer, PDF — while English is byte-for-byte unchanged when off.

---

## Approach — A: English-canonical, Arabic as a derived presentation layer

**The one bet that makes it safe:** the agent **always reads in English**; the gated English `report.json`
is the *only* clinical source of truth. Arabic is never *generated* — it is *derived* from the English by:
1. a runtime **UI i18n dictionary** + `dir=rtl` (no build step);
2. a **fixed, human-approved Arabic glossary** for all safety-bearing strings (certainty tiers, the
   disclaimer, the "(visual estimate…)" qualifier) — keyed by the English value, the **LLM never touches them**;
3. one tight `claude -p` pass that translates **only descriptive finding prose** → a cached `report.ar` sidecar;
4. **deterministic backstops** on that sidecar that **fall back to the English sentence** on any doubt;
5. an Arabic **PDF** render (reportlab + arabic-reshaper + python-bidi + an OFL Arabic font), token-verified.

### Rejected alternatives (do not relitigate)
- **B — generate the read in Arabic (agent re-run in Arabic).** REJECTED, dangerous. It translates the
  safety *prompt* (weakens the anti-fabrication / no-mm / tier gates) and emits Arabic structured keys the
  English-string gates don't match — `palette.normalize_certainty` silently defaults unknown → "Possible",
  so an unsafe value passes *and recolors the finding* with no error. Plus the nested-`claude -p` hang, a
  full re-run cost per toggle, and it can't serve already-completed studies. Three independent disqualifiers.
- **C — UI-only Arabic, findings stay English.** NOT the endpoint (the report prose never becomes Arabic),
  but it **is Phase 1 and the permanent degradation floor**: when any backstop trips, render that field in
  English with a small "shown in English" marker rather than ship unverified Arabic.

---

## Architecture / data flow
```
Agent read ─► report.json  (ENGLISH, gated: tiers, no-mm, disclaimer — UNCHANGED)
                  │
   user toggles عربى (first time on this study) ──► POST /api/report/{job}/ar  (flag-gated, lazy)
                  ▼
   services/arabic.py:
     • translate ONLY prose fields (findings.plain, bottom_line, key_points, what_it_means, captions)
       via one `claude -p` translate-only pass  ── NO numbers/findings added, certainty/disclaimer excluded
     • DETERMINISTIC GATE (numeric parity · structural parity · negation/laterality · severity-lexicon parity)
       → per field: keep Arabic OR fall back to the English sentence  (round-trip = human-review aid, not automated)
     • render certainty + disclaimer + fixed terms from AR_GLOSSARY[english_value]  (never the LLM)
     • write report.ar.json sidecar (cached; the human-gate review artifact)
                  ▼
   GET /api/report/{job}?lang=ar  → serves report.ar.json (bound; toggling is pure presentation after)
   Frontend: dir=rtl + t() dictionary; figures/scan NOT mirrored; LTR tokens isolated (<bdi>)
   PDF: build_patient_report_ar()  (reshaper+bidi+font, token-integrity asserted, PyMuPDF-verified)
```

---

## The safety model (the heart — every judge made this the crux)
| Layer | Who produces it | Guard |
|---|---|---|
| Certainty tiers (Confirmed/Likely/Possible/Reference) | **AR_GLOSSARY[english_tier]** — fixed, human-approved | Rendered from the **structured** `certainty` field (never parsed from prose), same as today. Pure equality. |
| Patient disclaimer | translated **once**, human-approved, **frozen as `REPORT_DISCLAIMER_AR` constant** | Never per-study LLM. Byte-identical every render. |
| "(visual estimate — no calibrated measurement available)" + calibration qualifiers | AR_GLOSSARY | Fixed. |
| Finding **prose** (plain, bottom_line, what_it_means, captions) | one `claude -p` translate-only pass | The deterministic gate below, then fall-back-to-English on any miss. |
| mm / numbers | **must already exist in the English** | numeric-parity gate: Arabic may introduce **no** number/unit the English lacked (mirrors the chat plan's mm-backstop). |
| **Severity / grade** adjectives | mapped through the **fixed `GRADE_AR` lexicon** | grade parity (deterministic, **deny-by-default**): for every grade token in the English, its **fixed** Arabic term must be present in the Arabic, and the Arabic may carry **no other** grade term — and if the English contains a grade-bearing token with **no `GRADE_AR` mapping**, the field **falls back to English**. This is the **dominant** mistranslation risk: meaning lives in the adjective. The lexicon must cover the grade vocabulary the codebase actually emits, NOT just five words — `dicom_engine.py:1008` emits `"marked"`; the prompts + reconciliation prose also use `minimal/moderate/significant/extensive/trace/tiny/subtle/gross/prominent/advanced/borderline/critical/high-grade/low-grade` + compounds (`mild-to-moderate`, `moderate-to-severe`). A symmetric "no other term" check alone fails open when an out-of-lexicon grade makes **both** sides empty — hence deny-by-default. NOT left to the (non-deterministic, human-gated) round-trip. |

**Deterministic gate on `report.ar` (release blocker — run BEFORE the Arabic is shown or PDF'd):**
```python
# GRADE_AR: fixed, human-approved — EVERY grade/severity adjective the pipeline emits (mined from the codebase:
#   mild minimal moderate marked severe  small large trace tiny extensive significant subtle gross prominent
#   advanced borderline critical high-grade low-grade  + compounds mild-to-moderate moderate-to-severe …)
GRADE_AR = {"mild": "خفيف", "marked": "ملحوظ", "severe": "شديد", ...}   # superset = the recognizer

def gate_ar_field(en: str, ar: str) -> str:        # returns ar OR en (fail-safe) — FULLY DETERMINISTIC, deny-by-default
    nums = lambda s: sorted(re.findall(r"\d+(?:\.\d+)?\s*(?:mm|cm|%)?", s.lower()))
    if nums(ar) != nums(en):                        # 1) no new/changed numbers or units
        return en
    if neg_ar(ar) < neg_en(en) or lat_ar(ar) != lat_en(en):  # 2) negation + laterality preserved (cue lists)
        return en
    grades = grade_terms_en(en)                     # 3) grade parity — DENY-BY-DEFAULT
    for g in grades:                                #    every English grade must survive as its FIXED Arabic term
        if g not in GRADE_AR or GRADE_AR[g] not in ar:    #    unknown grade OR its mapping missing → English
            return en
    expected = {GRADE_AR[g] for g in grades}
    if any(t not in expected for t in grade_terms_ar(ar)):  #    no OTHER grade term introduced in the Arabic
        return en
    return ar
```
- Checks **1–3 are fully deterministic and model-independent** (numbers, negation/laterality, grade via the
  fixed lexicon, **deny-by-default**) — they ARE the automated gate. The recognizer (`grade_terms_en`) matches the
  comprehensive grade vocabulary above (longest-match for compounds); an English grade with no `GRADE_AR` mapping,
  or whose mapping is absent from the Arabic, fails the field to English — closing the "both-sides-empty" hole a
  symmetric check would leave open. The **round-trip back-translation is an OPTIONAL human-review aid** surfaced
  at the human gate, **not** an automated catch (it can't self-verify — nested `claude -p` hangs), and grade no
  longer depends on it. The *default on any check failure is English*, so an unreviewed run is never worse than
  "English sentence with Arabic labels." Latin level labels (L4–L5) + Western numerals are kept verbatim so the
  numeric diff is exact.
- **PDF token-integrity assertion (the sneaky one — screen passes while PDF corrupts):** after `arabic_reshaper`
  + `python-bidi`, extract level labels + numeric tokens and string-compare to source; wrap embedded LTR runs
  (level labels, mm, `[See Figure N]`) in LRI/PDI isolates / LRM **before** python-bidi. Verify the rendered PDF
  with **PyMuPDF** (poppler absent — see `mika-pdf-verify-with-fitz`), not blind.

---

## Phased delivery
- **P1 — UI i18n + RTL (this is "C", safe + valuable on its own).** Runtime `t(key)` dictionary (en/ar),
  `<html dir lang>` flip, convert the *reading-flow* physical CSS to logical props, **figures/scan/decorative
  scan-brackets stay physical (not mirrored)**, `<bdi>`-isolate LTR clinical tokens. No backend, no translation,
  no medical risk. Ships behind `MIKA_AR_ENABLED=0`.
- **P2 — report.ar translation + glossary + gate + Arabic PDF.** `services/arabic.py`, the
  `POST /api/report/{job}/ar` lazy endpoint, `AR_GLOSSARY`, `REPORT_DISCLAIMER_AR`, the deterministic gate,
  `build_patient_report_ar`, the new deps. Degrade-to-English floor wired. The Arabic PDF builder **mirrors the
  English builder's tolerance** for an unknown certainty value (render the raw word + neutral/MUTED color — never
  a `KeyError`, never a silent relabel; cf. `report_builder.py:231`).
- **P3 — case-chat in Arabic.** Reuse `docs/Mika_Chat_Plan.md`: the chat **reasons in English over the English
  JSON** (gates + context intact); only the user-facing turn is translated via the same glossary-protected pass
  + the same backstops. Never let chat reason natively in Arabic (= B inside chat). Add this as a constraint to
  the chat plan.
- **P4 — polish.** Arabic month names (Western numerals), date/number `Intl` formatting, the toggle's home/recent
  surfaces, untranslated-string fallback to English.

## Files & surfaces touched (additive)
- **Frontend `index.html`:** new `I18N = {en,ar}` + `t()`; extend `uiPrefs`/localStorage with `lang`; `dir=rtl`
  on root; **surgical** logical-CSS conversion (the genuine reading-flow set: `.sec-block li{padding-left}` +
  `li::before{left:4px}` → `inset-inline-start`, `.recent-date`/`.sb-soon-tag`/`.seq-check`/`.cov-dot
  {margin-left:auto}` → `margin-inline-start`, `.tier-badge{margin-right}`, `.ref-review{border-left}`,
  sidebar `border-right`); leave the `.bf-scan`/`.rv-bk.*`/`.bf-focus-glow` decorative animations physical;
  `<bdi>` around level labels/mm/figure refs; the `Ask`/toggle in the topbar. **No build step.**
- **Backend:** new `backend/services/arabic.py` (translate + gate + glossary); new
  `backend/services/report_builder_ar.py` (or an `lang` branch in `report_builder.py`); new endpoint in
  `app.py`; `backend/prompts/i18n_glossary.py` (`AR_GLOSSARY`, `REPORT_DISCLAIMER_AR`).
- **Deps:** `requirements.txt` += `arabic-reshaper` (pure-Python) and **`python-bidi==0.4.2`** — the LAST
  pure-Python (`py3-none-any`) release. ⚠️ python-bidi ≥0.5 ships a **compiled Rust/maturin wheel**
  (`Root-Is-Purelib: false`, per-target binary) that needs a matching tag for every platform at the bundled
  Python version and can break the hashed `requirements.lock --require-hashes` build
  (`ELECTRON_BUNDLING_PLAN.md:101`) — the **same failure class as the numpy pin**. So: pin `==0.4.2`, hash it,
  import `from bidi.algorithm import get_display`. Bundle one OFL font (Noto Naskh Arabic ~3 MB) — within the
  Electron size budget (`ELECTRON_BUNDLING_PLAN §2`).
- **Untouched:** `agent_runner.run()`, the English prompts/gates, the existing report/PDF path, every existing endpoint.

## Data & schema
- `report.ar.json` **sidecar** next to `report.json` (English never mutated). Per-field provenance:
  `{value, source: "translated"|"english_fallback"|"glossary"}` so the UI can mark "shown in English".
- `AR_GLOSSARY` (certainty tiers + the **full `GRADE_AR` grade lexicon** + calibration qualifiers) +
  `REPORT_DISCLAIMER_AR`: code constants, human-approved, version-stamped.
- **Staleness guard (release blocker).** The English report is **NOT immutable after completion** —
  `/api/reconcile[/upload]` rewrite `report.json` via `_rewrite_agent_summary_and_patient_pdf` →
  `_persist_report` (`app.py:2331/2364`), adding a reference-assisted-review section / new prose. So the sidecar
  stamps `src_fingerprint` = a hash of the English `patient` block it was derived from. On `GET ...?lang=ar`, if
  `src_fingerprint` ≠ the current English `patient` hash, the sidecar is treated as **absent** (lazily
  regenerated, or serve C). Also **delete/invalidate the sidecar in the reconcile rewrite path**. Closes the
  "Arabic asserts what the updated English no longer says" hole. **Hash the *rendered top-level* `patient` block**
  (`_normalized_report_sections`, `app.py:568` — the exact object the translator consumes), **not** the raw
  `summary["patient"]` sub-dict: reconcile writes `patient["reference_reconciliation"]` (`app.py:1181-1183, 1255`)
  and `cv_supported_explanations` is recomputed at GET-time (`app.py:582`), so hashing the rendered block stays
  stable for a fixed English source while still catching reconcile deltas. All three reconcile entry points
  (`app.py:2324, 2361, 2834`) funnel through `_apply_reference_reconciliation` → `_persist_report`.

## Test & verification
- **Unit (pure, deterministic):** numeric-parity gate (Arabic with an extra "4 mm" → falls back to English);
  **grade-parity gate** (English "marked" → Arabic "mild"-term → falls back to English; correct mapping → kept;
  **deny-by-default**: English grade with no `GRADE_AR` mapping → falls back to English); structural parity
  (finding count/tier/figure refs equal); negation/laterality counter
  (a dropped negation → English); glossary-keyed certainty equals the fixed Arabic; an **unknown certainty**
  renders raw word + neutral color (no `KeyError`); `REPORT_DISCLAIMER_AR` is a constant; **sidecar staleness**
  (mutated English `patient` block → fingerprint mismatch → sidecar treated as absent). **PDF token-integrity**:
  a synthetic "L4–L5 / 6 mm" line survives reshape+bidi unreversed (assert on the post-bidi string).
- **Render check:** Arabic PDF rendered with **PyMuPDF** and eyeballed for tofu / disconnected letters /
  reversed tokens (a missing-glyph failure is silent).
- **RTL layout:** measure the Read/Wait screens under `dir=rtl` (no page scroll, no void, figure not mirrored)
  — the layout-fit rules, measured not assumed.
- **Human gate (cannot be self-verified — nested-`claude -p` hangs):** a clinical reviewer (Arabic-fluent +
  the glossary) audits the frozen disclaimer + a sample of translated findings for meaning/negation/severity,
  and confirms the fail-safe (a forced gate-trip shows English). No CI/agent run substitutes.

## Rollout & rollback
- Flag-dark `MIKA_AR_ENABLED=0` → toggle hidden, endpoint 404s, English untouched. Rollback = flip the flag.
- **Degradation floor:** any field that trips the gate renders English ("shown in English" marker) — the
  product never ships unverified Arabic clinical prose. Worst case is C, which is safe.

## Risks → mitigations
| Risk | Mitigation |
|---|---|
| Translated prose drifts **severity/grade** (marked→mild) — the dominant class (findings are qualitative) | **deterministic, deny-by-default** grade parity via the full `GRADE_AR` lexicon (covers `marked/minimal/significant/extensive/…` + compounds, not just 5 words) → fall back to English on any unmapped or missing grade; round-trip is only a human-review aid, not the catch |
| Translated prose flips a **negation/laterality** | deterministic negation + laterality counters → **fall back to English**; never ship-uncertain-Arabic |
| Mistranslated certainty/disclaimer | fixed glossary + frozen `REPORT_DISCLAIMER_AR`; LLM never touches them |
| PDF silently corrupts L4–L5 / mm while screen looks fine | LTR-isolate tokens before python-bidi + post-reshape token assertion + PyMuPDF render check |
| RTL breaks the dense no-scroll layouts | surgical logical-CSS (small set); figures/scan exempt; measured under dir=rtl |
| Compiled `python-bidi` drifts the frozen numpy<2 bundle | pin `python-bidi==0.4.2` (last pure-Python) + hash; `arabic-reshaper` is pure-Python; one OFL font ~3 MB within budget |
| Stale Arabic sidecar after a post-Arabic reconcile rewrites the English | `src_fingerprint` on `report.ar.json`; mismatch → treat as absent; invalidate on the reconcile path |
| Can't self-verify the live translate call | human gate + the fail-safe-to-English default makes the unreviewed case safe |

## Out of scope
Languages beyond AR/EN (architecture doesn't preclude more); generating the read natively in Arabic (that's B);
re-running the agent on toggle. Anatomy level labels stay Latin (L4–L5) — clinical convention.

## Success criteria (restated)
1. Flag off = zero behavior change (English byte-identical; suite green).
2. Flag on, a real completed study: UI + reading flow + report (screen + PDF) + chat render correct Arabic RTL;
   certainty/disclaimer come from the glossary; **no Arabic finding asserts something the English didn't**
   (numbers, negation, severity) — gate-verified, English-fallback on doubt; figures not mirrored; PDF renders
   with no tofu/reversed clinical tokens — confirmed in the human run.
3. Additive: no edits to the read/agent pipeline; new tests green; no page scroll introduced under RTL.

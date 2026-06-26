# Implementation Plan — "One Pulse" unified reading-motion language

> **Forged in /warcry · Reviewed ✓ (bulletproof, Rev 2) — SUFFICIENT: inventory baseline corrected & render-tree-verified (21/29/0), reduced-motion gate confirmed load-bearing, evidence/marker stillness enforced. Ready for /katana.**

> Forged in /warcry. Design source: [PLAN_one_pulse_motion.md](PLAN_one_pulse_motion.md) (daedalus-approved concept).
> Build target: `frontend/index.html` (single-file React SPA, in-browser Babel, NO build step, vanilla CSS
> custom properties + `@keyframes`; desktop-locked `body{min-width:1180px}`; bilingual EN/AR with `[dir=rtl]`;
> no frontend test harness). This plan is PLAN-ONLY. Build is a separate `/katana` step.
>
> **Rev 2 (post-bulletproof).** The inventory was re-derived from the REAL file + render tree (the first cut
> mis-counted 17→21 and listed dead CSS as live). Changes: corrected baseline (21 keyframes / 29 animation
> sites); `.lab-scan*` petri-dish marked DEAD (never rendered); `--draw-marker` confirmed dead (no-op,
> dropped); `.rv-slice` split rewritten as the JSX restructure it is; verification tied to rendered
> components; no-WAAPI rule made greppable. All counts verified via grep on 2026-06-27.

## Goal
Make MIKA's lab and imaging stages feel like **one product** by unifying their motion into a shared,
token-driven "reading-motion language" — applied **by artifact role**, not blanket. Observable success
signal: on home/wait/read, lab and imaging artifacts share one easing + one reading-sweep signature + one
reduced-motion story; **no diagnostic evidence moves; no marker drifts; no filtered layer is animated; no
animation escapes `prefers-reduced-motion`.**

## Approach (winner: "Token unification, minimal-touch, CSS-only")
Unify the **feel** (easing, reading-sweep signature, ambient-float feel, reduced-motion handling) via
`:root` tokens and a shared sweep gradient; **keep each artifact's existing duration** (tokenized, not
collapsed). Fix the animated-filtered-layer jank sites with the wrapper/child split. Keep ALL motion in CSS
(entrances via class-toggle) so the existing global reduced-motion block covers everything.

**Why:** the imaging section already has good, purpose-built motion. The cohesion gap is stylistic (two
easings/sweep looks), not structural. Minimal-touch tokenization closes it at lowest regression risk.

**Rejected alternatives (do not relitigate):**
- *WAAPI one-shots / JS motion engine* — WAAPI animations are NOT caught by the CSS reduced-motion block →
  new accessibility-leak surface, plus JS in a no-build file. CSS class-toggle is simpler and auto-covered.
- *Replace imaging keyframes with One-Pulse primitives* — collapses intentional cadences (rvScan 4.8s,
  seqScan 1.7s/2.6s, focusPulse 2.0s) → high regression. Unify the feel, don't rip out the system.

---

## The population (∀ coverage gate — VERIFIED, not estimated)
Success = 100% of the enumerated motion population classified and handled, zero leaks. **Verified baseline
(2026-06-27): 21 `@keyframes`, 29 `animation:` sites, 0 WAAPI calls.** Regenerate + check with:

```bash
grep -c "@keyframes" frontend/index.html            # MUST equal 21 (baseline); a diff = uncatalogued motion
grep -oE "@keyframes +[A-Za-z0-9_]+" frontend/index.html | sort
grep -c "animation:" frontend/index.html            # MUST equal 29 (baseline)
grep -n "@media (prefers-reduced-motion" frontend/index.html
grep -nE "\.animate\(|new Animation" frontend/index.html   # MUST return NOTHING (enforces the no-WAAPI rule)
# Render-tree cross-check: every animated CSS class must map to a className that actually renders:
grep -oE "className=[\"'\`][^\"'\`]+" frontend/index.html   # diff against the animated-class list below
```

### Role classification — ALL 21 keyframes (cross-referenced against rendered `className`)
- **DECORATIVE** (float/particles/pulse OK): `labStreamFloat`, `labParticleFlow` (both on `.lab-stream*`,
  rendered by `LabStreamVisual` @2609), `regionGlow` (home `.bf-glow`, rendered by `BodyFigure` @2015).
- **STATE/reading** (shared sweep signature + meaningful pulse): `scanSweep`, `diffuseBreathe`, `focusPulse`,
  `dotPulse`, `stepPulse`, `rvScan`, `seqScan`, `labStreamPulse` — all on rendered wait-stage elements.
- **EVIDENCE-adjacent one-shot** (special handling, Step 2.3): `rvKen` (slow zoom on a real wait-stage slice).
- **DEAD — never rendered, OUT OF SCOPE (do NOT unify):** `scanSpin`, `scanRing` — referenced only by
  `.lab-scan` / `.lab-scan-dish` (CSS 537–606), the old petri-dish visual orphaned by `a179a62 Redesign lab
  report page`. **Verified: no `className="lab-scan*"` anywhere in JSX.** The live lab visual is `.lab-stream`.
  → leave untouched here; optionally delete the `.lab-scan*` block in a SEPARATE cleanup commit (out of scope).
- **NON-artifact UI** (out of scope, already covered by reduced-motion): `skel`, `toastIn`, `fadeIn`, `fadeUp`,
  `chatIn`, `modalIn`, `chatBreath` (infinite, separately guarded @931).

(3 decorative + 8 state + 1 evidence + 2 dead + 7 UI = **21** ✓)

### Anatomy coverage — ALL regions, by construction
`.bodyfig`/`.bf-scan`/`.bf-glow`/`.bf-focus` are ONE component (`BodyFigure` @2129, `className="bodyfig
state-${state}"`); the detected region only swaps the IMAGE asset (`ANATOMY_DIAGRAM` @1490: spine, brain,
chest, abdomen, breast, vascular, head_neck, prostate, msk→knee, cardiac→full-body fallback). Therefore the
unified float (home) + reading-sweep/glow (wait) apply to **all 10 anatomy types automatically** — there is
no per-anatomy work, and the existing scan-band/`bf-focus` already targets the active region per anatomy
(preserved). Coverage is class-based, not asset-by-asset.

### Forbidden-from-motion (evidence) — MUST stay still
`.proof-img-wrap img` (@747), `.clin-proof-img img` (@816), `.lab-proof-img img` (@580) — verified static. The
real slice *content* of `.rv-slice` (its filter moves to a static child; only the wrapper zooms). `.bf-markers`
never floats (renders only at `state==='read'` @2054; stays registered to anatomy). **Whitelist, not blacklist:
only the classes listed under DECORATIVE/STATE above may animate; broad selectors (`img{}`, `.card{}`) banned.**

---

## Phased steps

### Step 0 — Confirm the ONE remaining unknown (the other two are resolved here)
- ✅ **Marker draw-in: RESOLVED — dead.** `--draw-marker:520ms` (@78) is referenced nowhere (no
  `@keyframes draw-marker`, no `stroke-dashoffset`, no `.animate()`). Markers render static; there is NO draw-in
  to retime. (Step 5 drops it.)
- ✅ **`.rv-slice` classification: RESOLVED.** It is the wait-stage reading viewer (`ReadingViewer` @2583), not
  read-stage proof. Authorizes the wrapper/child split (Step 2.3).
- ⏳ **Only remaining:** confirm/document `.lab-stream.reading img{animation-duration:7s}` (@652) is a deliberate
  "reading speedup" (not a bug). Either way it's neutralized by the global reduced-motion `*` block — no shape change.

### Step 1 — Add shared motion tokens to `:root`
Namespaced, with a ":root only — shared by lab+imaging, do not override locally" comment.
- Easing: reuse `--ease-calm` (loops) + `--ease-out` (entrances). **No new easing curves** (≤2 for loops).
- Durations (alias existing values, DO NOT collapse): `--dur-float:10s`, `--dur-particle:14s`,
  `--reading-period:3.1s` (shared lab+imaging pulse); keep `--dur-scan`/`--dur-breathe`; tokenize the locals
  (rvScan 4.8s, seqScan 1.7s/2.6s, focusPulse 2.0s, rvKen 6s) as named tokens at their CURRENT values.
- Amplitude (fixed endpoint values inside keyframes — valid; custom props resolve at parse-time):
  `--float-amp:1.4%`, `--float-rot:0.35deg`, `--particle-opacity:0.38`.
- Shared sweep gradient `--pulse-band` (reused by the lab pulse and the imaging scan-band so both "reading"
  sweeps look identical).

### Step 2 — Fix the animated-filtered-layer jank (wrapper/child split) — BLOCKING
Moving layer = transform/opacity only; a static child holds the filter. **Complete set of animated+filtered
elements = exactly these 3** (re-grep `filter:`/`mix-blend-mode:` × animation sites confirms no 4th):
1. **`.lab-stream img`** (filter saturate/contrast/drop-shadow + `labStreamFloat`, @634–636): move
   `labStreamFloat` to an unfiltered wrapper; keep the filter on the static inner `<img>`. **PRESERVE the
   z-order + blend context:** the `.lab-stream` stack is `isolation:isolate` with img `z-index:1`, particles
   `z-index:2`, pulse `z-index:3`, `::after` gloss `z-index:4` (@630–654) — the new wrapper must keep the img
   layer at `z-index:1` so the particle/pulse `mix-blend-mode` still composites correctly. (Touch `LabStreamVisual` @2609.)
2. **`.lab-stream-pulse`** (`filter:blur(11px)` + `mix-blend-mode:screen` + animated, @651): animate
   opacity/transform on a wrapper; keep blur/blend on a static child (or bake blur into the gradient).
3. **`.rv-slice` — this is a JSX RESTRUCTURE, not a CSS tweak.** It is currently a bare
   `<img className="rv-slice ...">` rendered directly in `.rv-stage` (@2596); the cross-fade between slices is
   per-`<img>` `transition:opacity 1300ms` + the `i===idx?'on'` toggle (@413–414). Restructure to
   `<div className="rv-slice"><img className="rv-slice-content"/></div>` and assign:
   - wrapper `.rv-slice`: the `on` toggle + `transition:opacity 1300ms` (cross-fade) + `rvKen` zoom (transform) + `will-change`;
   - child `.rv-slice-content`: the `filter:contrast/brightness` ONLY (static).
   Confirm the `idx` toggle now targets the wrapper and the 1300ms cross-fade still works.
   **Reduced-motion end-state:** `.rv-slice.on` uses `rvKen … both` (@414); under the global block it runs once-
   instant with `both` fill → lands at `scale(1.07)`. Add `@media (prefers-reduced-motion){ .rv-slice.on{
   animation-fill-mode:none } }` so evidence sits at natural `scale(1.0)`, AND make verif #3 expect the settled
   (non-animating) transform rather than asserting "no transform ever."

### Step 3 — Unify the reading-sweep signature (WAIT stage)
Point both the lab pulse (`labStreamPulse` on `.lab-stream`, LIVE) and the imaging scan-band (`scanSweep`) at
the shared `--pulse-band` + `--ease-calm`. Lab keeps `--reading-period`; imaging keeps `--dur-scan` (durations
stay per-artifact; gradient + easing + signature unify). Retime any artifact loop not already on `--ease-calm`
to `--ease-calm` (inventory: all are, except `linear` particles, which stay `linear`). **Do NOT touch the dead
`scanSpin`/`scanRing`.**

### Step 4 — Shared ambient float on the HOME heroes (decorative only)
- Lab: `.lab-stream` on lab-home — align to `--dur-float`/`--float-amp`.
- Imaging: apply the float to the **outer `.bodyfig` wrapper on HOME only** (`BodyFigure state="home"`, which
  renders only `.bf-glow` @2047 — no markers). **NOT `.bodyfig-stage`** (carries marker transform space), **NOT
  on wait/read**. The outer `.bodyfig` has no existing transform (@221–224) so float can't cascade to markers/evidence.

### Step 5 — READ stage: stillness, no marker retime
- Evidence stays still (no change to `.proof-img-wrap`). Markers render static — **there is no draw-in animation
  to retime** (Step 0: `--draw-marker` is dead). No new infinite loops on read. Existing `fade-up` entrance
  (already `--ease-out`) is fine as the shared entrance.

### Step 6 — Reduced-motion (global block is load-bearing; token-zero is belt)
- The global `*` block (@101–105, `animation-duration:.01ms !important; animation-iteration-count:1 !important;
  transition-duration:.01ms !important`) is the LOAD-BEARING gate — it already neutralizes every CSS animation
  incl. the @652 duration override. Verified: no other `animation*` rule in the file carries `!important`, so
  nothing out-specifies it.
- Add a token-zero belt inside that block (set `--dur-float`/`--dur-particle`/`--reading-period`→0, particle
  opacity→0). Belt-and-suspenders only; do not claim it's the gate.
- Plus the `.rv-slice.on` fill override from Step 2.3. No WAAPI introduced (coverage-gate grep enforces).

### Step 7 — RTL guard
Confirm directional sweeps (`translateY`/`skewX` in `labStreamPulse`) and `linear-gradient(90deg,…)` scan-bands
do NOT auto-flip under `[dir=rtl]` (medical figures/scan-lines must not mirror). Add explicit `[dir=rtl]` rules
only if a flip is observed. Markers stay on anatomy in RTL.

---

## Files & surfaces touched
`frontend/index.html` ONLY. CSS: `:root` (~73–78), reduced-motion (101–105), figure/marker (221–266), wait
anims (239–263, 390–456), lab anims (630–661; the dead 537–606 left untouched). JSX: `LabStreamVisual` (2609),
`ReadingViewer` (2583–2605, the `.rv-slice` restructure), `BodyFigure`/`HomeScreen` (2015/2517, home-float
wrapper). No backend, data, schema, dependency, or new file.

## Test & verification strategy (motion can't be proven by a still)
Run via the in-repo chrome-devtools/preview tooling. **Sample only elements that ACTUALLY RENDER** (a dead
class like `.lab-scan` would read "frozen" trivially and give false confidence — tie the element list to
rendered components, not CSS classes):
1. **Loop liveness:** at home/wait/read (lab + imaging), `evaluate_script` samples
   `getComputedStyle(el).transform`/`opacity`/`backgroundPosition` every ~100ms for ~3s on the whitelisted
   RENDERED elements; assert values change.
2. **Reduced-motion:** emulate `prefers-reduced-motion: reduce`; resample; assert those elements FREEZE; assert
   `.rv-slice.on` sits at `scale(1.0)` (fill override); UI legible.
3. **Evidence stillness:** assert `.proof-img-wrap img`, `.clin-proof-img img`, `.lab-proof-img img` and the
   `.rv-slice` *content* child show no transform/opacity animation (allow `.rv-slice` wrapper's settled zoom end-state).
4. **Marker registration:** on HOME float, sample a (read-stage) marker's bbox vs anatomy anchor — but markers
   don't render on home, so the real check is: confirm float is on outer `.bodyfig` and `.bf-markers` is absent
   from the home DOM; on read, confirm the figure does NOT float and markers are static.
5. **Jank + no-WAAPI gate:** re-run the coverage greps — 21 keyframes / 29 animation sites / **0** `.animate(`;
   confirm no animated element carries `filter`/`mix-blend-mode` (the 3 sites are split).
6. **∀ coverage gate:** every `@keyframes`/`animation:` site is in the role table; baseline counts match; a new
   uncatalogued animation (count diff) or a new WAAPI call FAILS review.
7. **Anatomy spot-check (coverage is class-based; prove it on ≥3 regions):** via the existing `?anatomy=`
   demo override (@4112/`DEMO_BY_ANATOMY` @4548) + `?demo=wait`, confirm the unified float + reading-sweep
   render on at least **brain, spine, and a full-body fallback (cardiac)** — and that the scan-band/`bf-focus`
   still targets the correct region per anatomy. (One component, asset-swapped → all 10 covered, but a 3-region
   sample proves no anatomy renders motion-less.)

## Rollout & rollback
Single-file, CSS-dominant → rollback = `git revert` of the commit. Verify on the fresh backend (:12900) + the
static `?demo=` states before committing to main (solo repo, commit per phase).

## Risks → mitigations
| Risk | Mitigation |
|---|---|
| Animated filtered-layer jank (lab-stream ×2, rv-slice) | Wrapper/child split (Step 2) + preserve z-order/isolation; coverage grep proves no filtered element animates |
| `.rv-slice` restructure breaks the 1300ms cross-fade / zoom on real DICOM | Explicit DOM + node-by-node property assignment (Step 2.3); reduced-motion fill override; verif #2/#3 |
| Reduced-motion leak | Load-bearing global `*` block + token-zero belt + greppable no-WAAPI rule; sampling test on rendered elements |
| Marker de-registration | Float outer `.bodyfig` (home-only, no markers there); read figure stays still; markers never animate |
| Evidence animates via broad selector | Whitelist animatable classes; explicit evidence exclusion; stillness test |
| Wasting effort on dead `.lab-scan*` | Marked DEAD/out-of-scope; not unified; optional separate-commit cleanup |
| Stale inventory rides into build | Baseline counts verified (21/29/0) + render-tree cross-check is the gate |
| Breaking existing imaging cadence | Keep per-artifact durations; unify easing + sweep signature only |
| RTL mirrors a figure/sweep | Step 7 explicit `[dir=rtl]` check |

## Out of scope (explicit)
- Float/drift/particles on read-stage proof crops, real slice *content*, or the marker-bearing figure.
- The dead `.lab-scan*`/petri-dish block (537–606) — not unified here; delete in a separate cleanup commit if desired.
- Non-artifact UI motion (toast/modal/chat/skeleton/chatBreath); collapsing per-artifact durations.
- A new automated CI motion-test harness (Playwright/Puppeteer) — deliberate: no-build, Python-only tests;
  verification is the scripted chrome-devtools sampling gate above.
- **Partial/phased COVERAGE of the in-scope motion population is NOT acceptable** (∀). Depth may phase; coverage may not.

## Success criteria (restated)
1. Lab + imaging share one easing for loops + one reading-sweep signature; home heroes share one ambient float.
2. Coverage gate passes against the VERIFIED baseline (21 keyframes / 29 animation sites / 0 WAAPI); every
   animation classified + handled; render-tree cross-check clean (no dead class treated as live).
3. No filtered layer is the animated element (all 3 sites split; z-order preserved).
4. Every continuous loop freezes under `prefers-reduced-motion` (sampling on RENDERED elements proves zero
   leaks); `.rv-slice` settles at scale 1.0; reduced state legible.
5. No evidence animates; no marker drifts; existing imaging cadences preserved.

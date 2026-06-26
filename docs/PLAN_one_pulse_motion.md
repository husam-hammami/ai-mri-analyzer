# Concept: "One Pulse" — a unified reading-motion language (lab + imaging)

**Status:** daedalus-approved (REFINED from the user's "apply lab animation everywhere" request).
**Owner intent:** lab and imaging stages should feel like one product. **Approved means a shared motion
LANGUAGE, NOT pasting the bloodstream float onto medical evidence.**

## Thesis
One easing, one "reading" pulse, one ambient float, one particle treatment — defined as tokens and applied
**by artifact role, not blanket**. Decorative/brand layers may float, drift, and pulse. **Evidence never
moves; markers stay pinned.** Motion encodes state ("MIKA is reading") or rewards an action (entrance,
marker draw-in) — never decorates a diagnostic image.

## Artifact roles (the gate that decides what gets motion)
- **DECORATIVE / brand** (safe for float + particles + pulse): lab `.lab-stream` evidence artifact; the
  home anatomy hero figure (`.bodyfig` on home — it's a diagram, not proof); any side-panel brand texture.
- **STATE / reading** (gets the shared *meaningful* pulse only): the wait-stage figure scan-band and the
  slice reading-viewer — they signal "reading the active region/slice".
- **EVIDENCE** (NO continuous motion, ever): read-stage proof crops (`.proof-img-wrap`), real DICOM slice
  *content*, and the marker-bearing figure on the read. Allowed: one-shot entrance + marker draw-in.

## Shared motion tokens (define once, reuse on both lab + imaging)
- `--ease-calm` — the SINGLE continuous/ambient easing (already exists; make it the only one for loops).
- `--reading-period: ~3.1s` — the "reading" pulse cadence (from labStreamPulse).
- `--float-period: ~10s`, `--float-amp: ~1.4%` translate + `±0.35deg` rotateZ — ambient float.
- `--pulse-band` — the scan-sweep gradient (shared by lab-stream pulse and imaging scan-band).
- `--particle-*` — drift speed (~14s linear), opacity cap (≤0.38), mask — decorative layers only.

## Per-stage spec
### HOME
- Lab `.lab-stream` and imaging `.bodyfig` hero: shared **ambient float** (transform/opacity only), low
  intensity, `--float-period`/`--float-amp`, `--ease-calm`. No particles needed (calm entry).

### WAIT (the meaningful, state-encoding stage — biggest cohesion win)
- Unify the **reading pulse**: lab-stream "reading" sweep and imaging `scanSweep` become the SAME band
  gradient + `--reading-period` + `--ease-calm`. Both stages now pulse with one identical "reading"
  signature.
- Slice `reading-viewer` keeps its purpose-built scan-line (`rvScan`/`seqScan`) but **retimed to
  `--reading-period`/`--ease-calm`** so it's the same heartbeat.
- Particles/ambient texture: ONLY on the decorative lab-stream / a decorative ambient layer behind the
  imaging figure — **never over the real slice**.

### READ (evidence — stillness is the feature)
- Proof crops: **static** (zoom-on-click unchanged). No loops.
- Markers: **pinned**; keep the one-shot `draw-marker` draw-in, retimed to `--ease-calm`.
- Shared touch only: card/figure **entrance** (opacity + translateY ≤8px, 200–400ms, ease-out). No infinite
  decorative loops on read evidence.

## Motion registers (raw values for the build)
**Continuous loops**
- Ambient float (decorative): `translate3d(0, 0→-1.4%→0, 0) rotateZ(-0.35deg→0.35deg→-0.35deg)`, 10s,
  `--ease-calm`, infinite.
- Reading pulse (wait): band `opacity 0→0.95→0`, `translateY(-120%→120%) skewX(-8deg)`, 3.1s, `--ease-calm`,
  infinite, only while status === reading.
- Particle drift (decorative only): `background-position` drift, 14s linear, opacity ≤0.38, masked.

**Triggered one-shots**
- Entrance: `opacity 0→1, translateY(8px→0)`, 240ms, ease-out (WAAPI or CSS).
- Marker draw-in: existing `draw-marker` (~520ms), retimed to `--ease-calm`, fires once per marker.

## Non-negotiable guardrails (rubric G + motion-spec)
1. **Transform + opacity only.** Refactor the existing `.lab-stream img` so the moving layer is an
   **unfiltered wrapper**; keep `saturate/contrast/drop-shadow` on a static inner `<img>` (fixes the
   current "move a filtered layer" jank, and the unified system must not repeat it).
2. **Evidence never animates** (proof crops, real slice content) and **markers never float** (stay pinned).
3. **`prefers-reduced-motion`:** both registers off; particles/pulse opacity 0; entrances → instant/opacity
   only. Reduced state must stay fully legible.
4. **One easing for loops** (`--ease-calm`); ≤2 curves total (rubric G).
5. Legibility: contrast/readability of any image under an overlay must hold (WCAG D1).

## What /warcry must plan
- Extract the shared tokens; refactor lab-stream to the wrapper/child split; unify wait-stage pulse cadence
  across lab + imaging; retime slice scan-line + marker draw-in to the shared easing; add ambient float to
  the home heroes; wire one reduced-motion branch covering all of it; verify by **sampling computed
  transform/opacity over time** (a still cannot prove motion).

## Out of scope (explicitly rejected)
Float / drift / particles on read-stage proof crops, real DICOM slices, or the marker-bearing figure.

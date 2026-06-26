# MIKA Lab-Report page — UI/UX concept: "The Verdict"

> Forged in /daedalus (2026-06-26). Concept phase only — the INPUT to a later `/warcry` implementation plan.
> Status: **SELECTED** (unanimous 2-judge panel; invent-army of 4 divergent concepts).
> Separate page/flow from the imaging Read. Opus reads the parsed lab/blood report directly (already capable;
> no parsing pipeline to design). Presents ONLY what patients care about, plain language, no jargon, no prescribing.

## Thesis
A scared, non-technical patient needs one calm human sentence that answers **"am I okay?"** plus a single honest
count of **what needs attention** — *before* any list, value, or chart. Everything normal disappears into one quiet
line, never competing for the eye. Lead with language, never a number or a colour-grammar the patient must decode.

## Three-second contract
Eye lands: **takeaway sentence → confidence → "N to review" count → the one obvious action.** A patient perceives
"overall okay, two things to look at" in three seconds, with the green-light that everything else was checked and fine.

## Hero artifact — "The Stream" (generated 3D bloodstream PNG)  ← CURRENT DIRECTION
> **Pivot (user decision, 2026-06-26):** the hero is a **pre-generated detailed-3D bloodstream PNG** (red + white
> blood cells), not a CSS/SVG figure. Three deciding calls: (1) **drop per-organ localization** — the artifact is
> UNIFIED across ALL medical-report types, atmospheric not a body-map (the Verdict header + cards carry all meaning,
> so the marker↔organ linking below is no longer needed); (2) palette resolved with **maroon** blood (deep, not
> alarm-red) on dusk navy + the single blue accent; (3) it's an **image asset like the body figure**, so the earlier
> 3D/WebGL/perf objection is moot. Sits in the hero slot (portrait) or as a full-bleed backdrop behind the Verdict.
> **Image-gen prompt** ("The Stream") lives in the chat handoff / to be saved beside the asset; key craft lock = a
> single restrained cool-blue (#2563EB) rim-light on maroon cells so it reads as one product with the imaging figure.
> Drop the PNG at `frontend/assets/brand/` and wire it as the report-page hero.

**Image-gen prompt ("The Stream") — feed to the image model, drop the PNG beside the body figure:**
```
A cinematic, ultra-detailed 3D render looking into the inside of a translucent blood vessel — a calm, slow river of
blood drifting into deep space. Suspended in the flow: many red blood cells as smooth biconcave discs in deep,
desaturated MAROON (#6E1423 to #7B1E2B) with soft subsurface translucency and slightly darker rims, plus a few larger
WHITE blood cells as pale, gently glowing translucent spheres (#EAF0F8) with subtle textured surfaces. The cells flow
with real depth — crisp and detailed in the foreground, melting into soft bokeh and haze as the stream recedes into a
near-black dusk-navy void (#0A0F1D).
Lighting: a warm maroon glow from within the plasma, met by a single restrained COOL BLUE accent rim-light (#2563EB)
that catches the edges of the cells and forms a faint volumetric haze deep in the frame — the only cool color, the
signature that ties it to a premium blue-accent medical brand. Crisp near-white speculars on wet cell surfaces, gentle
god-rays through the plasma, soft particulate drift.
Mood: premium, futuristic, clinical-trust — microscopy meets a high-end Octane/Redshift product render. Calm, elegant,
alive. NOT alarming, NOT gory; maroon stays deep and sophisticated, never bright primary red.
Composition: vertical portrait, stream flowing top-to-bottom; edges/corners fall off softly into the dusk-navy void
(#0A0F1D) to composite onto a dark app surface; generous balanced negative space.
Strict palette: dusk navy #0A0F1D · maroon blood #6E1423–#7B1E2B · pale cells #EAF0F8 · one cool-blue accent #2563EB ·
near-white speculars. No other hues.
Style: photoreal 3D, volumetric, subsurface scattering, ray-traced, shallow DoF, 8k detail.
Avoid: bright primary red, gore, wounds, needles, syringes, anatomical diagrams, organs, human figures/faces, any
text/numbers/labels/watermark, UI/HUD/gauges, purple/violet/indigo/cyan/teal/neon-green, glassmorphism, cartoon/flat
illustration, clinical stock-photo look, clutter.
Aspect ratio 3:4 portrait, ~1024×1536, dusk-navy background (or transparent PNG if supported), no border.
```
(For a full-width backdrop variant: regenerate at 16:9 and change "vertical portrait, top-to-bottom" → "wide cinematic, left-to-right.")

### Parked alternative — "Inner Constellation" (the body lit from within)
> Forged in a second /daedalus invent-army (unanimous pick at the time). SUPERSEDED by The Stream above, but kept as
> the fallback if an interactive, data-driven body-map is ever wanted. Its honesty rules (Circulation-column not
> "flame the heart"; certainty=opacity) still apply to any future body artifact. Detail below is reference-only.

**Metaphor.** The SAME translucent dusk body from the imaging Read — but instead of one scan region, the ORGANS tied
to a patient's flagged lab markers softly **illuminate from inside the body's silhouette** (accent inner-glow clipped
to the organ outline, so the light reads as coming from within, not a sticker on top), each tethered by a thin accent
leader-line to a numbered value chip. A body lit from the inside at exactly the places this person's blood is talking
about. Dormant organs stay whisper-faint (~10% accent) so the lit ones own the eye; zero flags → the figure rests at
the calm home-glow baseline ("nothing flagged — your systems read normal").

**Why it won / why it's on-brand.** It is mechanically the imaging Read's figure (same full-body translucent asset
at `--bodyfig-max`, same `mk-dot`→`mk-leader`→`mk-chip` markers, same certainty-as-opacity ladder, same `bf-glow`
radial), so the two screens are unmistakably one product and a second hue is not even reachable. "Futuristic" = accent
LUMINANCE glowing from inside a translucent body (light as anatomy), never a HUD/gauge/dark-canvas/glassmorphism.

**Lab → organ map (honest).** hemoglobin/hematocrit/ferritin/iron/MCV → blood + marrow core; cholesterol/LDL/HDL/
triglycerides → **great-vessel column / "Circulation"** (NOT "flame the heart" — a lab signal must never read as a
cardiac diagnosis) [graft: The Current]; ALT/AST/ALP/bilirubin → liver; creatinine/eGFR/BUN → kidneys; glucose/HbA1c
→ pancreas/metabolic; TSH/T3/T4 → thyroid (neck); WBC/CRP → immune (marrow core + faint lymphatic wash); calcium/
vitamin D/phosphate → skeleton/long-bone. Multiple markers can share a zone — they stack with a per-organ **count
badge** ("Liver 2") and the zone takes the WORST status of its markers [graft: Constellation Read]. A marker that maps
to no zone degrades to a "general / whole-body" chip with a faint full-figure breathe — the body is never wrong, only
sometimes general.

**Two honest light axes (still monochrome).** Inner-glow OPACITY = certainty (Confirmed/Likely/Possible, the existing
ladder); inner-glow RADIUS/intensity = how far from normal (clamped deviation) [graft: Vitals Aura] — so a lit organ
says "what part of me AND how much" in pure light, before any number, with no stoplight colour. Direction (high vs low)
lives in the card text/arrow, never in a forbidden hue.

**Marker ↔ card link.** Identical to imaging, reusing `activeId` + `onMarkerClick`: click a flagged card → its organ
inner-glow intensifies, leader thickens, body holds focus on that organ; click the lit organ/chip → the card
highlights and scrolls in (`selectFinding`). Focusable zones with aria-labels ("Liver — ALT flagged, 2 of 8").

**Motion (native CSS/SVG).** Continuous: each active organ inner-glow breathes on `--dur-breathe` (4.2s) `--ease-calm`,
desynced per zone so the lit constellation shimmers like a slow heartbeat. Triggered: on reveal, organs light in a soft
**staggered head→pelvis cascade** (`--stagger-step` 70ms + `--draw-marker` 520ms leader draw) so the body "comes alive"
once, then settles; **pre-light the top flagged organ** on load so the 3-second contract lands before the cascade
[graft: Vitals Aura]. At 6–8+ flags, cap breathing to the active/hovered organ; the rest hold a static lit state to keep
the calm ceiling [graft: The Current]. `prefers-reduced-motion` → organs simply lit-or-not at final opacity, leaders
drawn, no loop/cascade.

**Buildability.** Evolve `BodyFigure` (function at `index.html:1686`, already takes `state/findings/activeId/
onMarkerClick`) with a `state="labs"` branch rendering a `<svg class="bf-organs">` overlay (analogous to `.bf-markers`).
New assets are only an `ORGAN_ZONES` table (id, normalised x/y/scale, glyph key from the existing `I.*` set, leader
anchor) + the `LAB_TO_ZONE` map. Inner-glow = the existing `bf-glow` radial clipped to an SVG mask of the organ outline.
Leaders, chips, de-collision stacking, opacity classes, hover halo, RTL (chrome flips, body NOT mirrored) all reused.
Pure SVG + CSS, no WebGL/three.js, no new deps.

## While analyzing (the wait state) — the body comes alive
Mirrors the imaging Wait (which holds the body figure during processing). The translucent dusk body is present but
DORMANT, with a calm single-accent **reading pulse** passing through it while the report is read; the MIKA coin breathes
beside three plain steps ("Reading the values · finding what stands out · putting it in plain words"). On completion the
organs light in the head→pelvis cascade — the read literally resolves into the lit body. (The uploaded document isn't
the hero, but it returns per-card as the "see it on your report" proof-crop below.) `prefers-reduced-motion` → the pulse
fades in place; the reveal is static-lit.

## The page (top to bottom)
1. **Slim disclaimer banner** — reuse the existing one-line `.read-topnote` (the persistent "discuss with your
   doctor" frame already lives here; do NOT repeat doctor-deferral in the content).
2. **The Verdict header** (the dominant artifact — typography, not a widget):
   - A large slate-ink plain-language takeaway ("Mostly reassuring. Two things are worth a look.").
   - The existing overall-confidence pill (`.opill`, High/Moderate/Low + %).
   - One accent **"N to review"** count token (accent-ringed integer); when clean → accent check + "Nothing needs
     your attention right now."
   - An explicit **reassurance ratio** line: "22 of 24 results checked and look normal" (state the green-light,
     don't merely imply it by absence). [graft: Plainspoken / Reassurance Line]
3. **Flagged-card stack** — ONLY high-confidence flagged items, priority order (most-off first). Each card:
   - Plain name headline ("Vitamin D", not "25-hydroxyvitamin D").
   - Short plain status phrase ("a bit low" / "clearly high") — never a raw delta.
   - The existing **`.cchip`** tier (Confirmed / Likely / Possible).
   - One-line plain meaning ("low vitamin D is common and can leave you tired").
   - **"See the numbers"** expands in place → raw value + reference range + the on-demand **range bar** (abstract artifact).
   - **"See it on your report"** → reveals a highlighted crop of the patient's ACTUAL uploaded report showing that
     exact value — the lab analog of imaging's "view on scan" (concrete/proof artifact). Grounds trust: the patient
     sees "18 (30–100)" lifted straight from their own page, so MIKA clearly didn't invent it. The full original
     report stays one tap away ("view full report") for anyone who wants everything.
   - **Build primitive:** render flagged cards AS the existing **`.frow`** rows (accent ring + glow, `index.html:486`)
     and the expand-in-place numbers panel as **`.frow-extra`** — the highest-reuse path, no new card family. [graft: judge 2]
   - **Pre-expand the single top flagged card on load** (reuse `.frow.active`) so the full 3-second contract is met
     with zero taps, even on mobile. [graft: Plainspoken]
4. **Collapsed normals** — one quiet row ("22 of 24 results checked, all normal", expandable; the `cov-row` register).
   Framed **descriptive, not diagnostic, scoped to what was measured**, and routes unmapped analytes to a visible
   "Other results" bucket — inoculates against over-reading "all normal" as a clean bill for an unmeasured system.
   Optional body-system grouping appears INSIDE this drawer only when results are many (~≥20). [grafts: Vital Systems]

**Spatial grammar:** mirrors the imaging Read so the two feel like one product — **Verdict header** spans the top, then
the **Inner Constellation lit body as the hero** (left/center, where the Read's figure lives) beside the **flagged
cards as the findings thread** (right, numbered to match the organ chips); collapsed normals below. 8px grid; sidebar +
topbar shell unchanged. (This corrects the earlier draft's "labs have no figure" — they DO: the lit body.) Mobile: body
stacks above the cards (figure-then-thread), verdict stays the hero, top flagged organ pre-lit, touch targets ≥44px.

## Artifact verdict (the explicit ask)
"What's the artifact since the report isn't images?" → it's a **body, not a document.** The hero is the **Inner
Constellation lit body** — the lab equivalent of the imaging scan/figure. Artifacts that earn their place:
- ✅ **Inner Constellation — the lit body** (hero): carries both the analysing state (body comes alive) and the results
  (organs lit from within + marker↔card link). This is what replaces the imaging scan/figure.
- ✅ **Verdict header** — the *meaning* artifact and the 3-second contract; costs only typography + one existing
  pill. All four concepts converged on it (the signal).
- ✅ **Range bar** (abstract) + **document proof-crop** (concrete) — the flagged card's two on-demand reveals:
  - **Range bar:** single-accent, **flagged-cards-only, on-demand** (behind "see the numbers"). A hairline
    `--surface-2` rail, normal band shaded `--accent-softer`, the value as one accent tick. Answers "how far off am I?"
    without leading with a number. Pure CSS, no chart lib. **Degrades to a plain card (no bar)** when the range isn't
    two-sided numeric (positive/negative, one-sided, off-scale); clamp extremes to a labelled "well above/below"
    end-cap — never fabricate a marker position.
  - **Document proof-crop:** a highlighted crop of the patient's own report ("see it on your report") = the lab
    "view on scan". **Build wrinkle for /warcry:** highlighting needs the value's *location*, not just its text —
    options are Claude-vision returning an approximate region, or a text-match on the rendered page; the honest
    fallback is showing the cropped row/region (or just the relevant page) rather than a precise box. Render PDFs to
    image with the existing PyMuPDF/fitz path; photos are already images.
- ❌ **Separate summary score/widget** — redundant; the header already is the summary.
- ❌ **Traffic-light status-icon set** — forces non-accent AI-tell colours (green/amber/red) MIKA forbids, and
  duplicates the `.cchip`. Status is carried by the plain phrase + tier chip + (on demand) tick position.
- ❌ **Eager body-system grouping as the lead** — a browse model that buries the 3-second answer; demoted to the
  optional in-drawer view above.

## States
- **All-normal:** Verdict reads "Everything looks normal." + accent check; no card stack; just the quiet ratio line.
  Mostly calm white space — maximally reassuring.
- **Many-flagged:** Verdict stays honest but non-alarming ("Several things stand out — here are the ones worth a
  look."); cards stack most-off-first; normals still collapse to one line so the page is never a wall of red.
- **Error (unreadable upload):** reuse the existing calm empty/error register — "MIKA couldn't read this upload
  clearly," plain why + what-next (retake photo in good light / try the PDF), one re-upload action, NO fake results.
- **Empty (pre-upload):** the existing dropzone with patient copy ("Add your lab or blood report").

## Data model Opus returns (what the page renders)
Overall plain takeaway + overall confidence → the Verdict header. `results[]` each: plain name; status (low/normal/
high) + rough severity phrase; confidence tier; one-line plain meaning; raw value + reference range (on demand).
Split: high-confidence flagged → cards; everything else → the collapsed truthful count.

## Motion
Reuse existing tokens: `--ease-out` for the in-place card expand; `--draw-marker` (520ms) + `--stagger-step` (70ms)
for the on-demand tick draw-in, so attention lands on the bar position only after the patient chose to look at numbers.
`prefers-reduced-motion` → fades only.

## RTL / i18n
Logical properties throughout (`margin-inline`, `padding-inline`, `inset-inline`) so the column and card internals
mirror cleanly for Arabic. The range bar's normal-band and value tick mirror with direction, but numerals, units and
range text are wrapped in dir-isolated spans so digits/units never reverse. All copy via the existing `L()`/`AR_UI`.

## Primary risk → mitigation
The whole 3-second contract is **generated copy** — a hedge-stripped or over-soothing verdict ("everything's fine")
on a genuinely flagged report is the dangerous failure (the Verdict has the least visual scaffolding to fall back on).
**Mitigation:** bind the verdict's tone to the count (clean → reassure; any flag → "worth a look", never "fine");
gate the verdict on the same high-confidence threshold as the cards; never imply a diagnosis or a next treatment;
fall back to a fixed neutral template when confidence is low or parsing is uncertain. This is the #1 thing `/warcry`
must harden.

## Rejected concepts (don't relitigate)
- **The Reassurance Line** (range-position lead) — leads with the bar grammar; makes the patient decode position
  before language. Its range bar survives as the on-demand artifact; its end-cap/fallback rules were grafted in.
- **Vital Systems** (system grouping lead) — a browse model, not triage; buries the 3-second answer. Its "Other
  results" catch-all + "descriptive not diagnostic" framing were grafted into the normals drawer.
- **Plainspoken Read** (narrative + progressive) — closest runner-up; correct instinct but no count/at-a-glance
  scaffolding. Its pre-expand-top-card and `.frow`-as-primitive ideas were grafted into the winner.

---

## ✅ FINALIZED — locked for /warcry (Daedalus finalize pass, 2026-06-26)
> Finalize mode against the now-present assets + the real brand contract read from `index.html`. These calls
> OVERRIDE any earlier text in this doc that conflicts. No new concept — disambiguation + hero pick + safety lock.

### Brand contract (read from `index.html`, do not violate)
Content canvas `--canvas #F8FAFC` (LIGHT), white `--surface` cards, navy sidebar (`--navy-900 #0A0F1D`), single
blue accent `--accent #2563EB` — **no other UI hues**. Motion tokens exist: `--ease-out`, `--draw-marker 520ms`,
`--stagger-step 70ms`, `--dur-breathe 4200ms`. Screen state machine: `home → wait → read` (see `index.html:1891+`).

### 1. Hero image — LOCKED: `frontend/assets/bloodwork (3).png`
Portrait bloodstream close-up — the only candidate that reads as a *stream* (flowing cells, depth/bokeh, no
container), matching "The Stream" thesis. **Rejected:** `(1)` and `(2)` are petri **dishes** — hard container rings
read literal-clinical and fight the atmospheric intent; `(2)` is also cluttered (breaks the calm ceiling).
- **Treatment (corrected for the LIGHT canvas):** the original "full-bleed dusk backdrop" assumed a dark surface
  that the content area does NOT have. So the hero is a **contained portrait card**, not a backdrop: `object-fit:
  cover`, soft inner vignette, `--accent-line` ring + `--accent-glow` shadow, one restrained blue rim via CSS to
  tie it to brand. No recolor of the source.
- **Palette honesty:** the rendered cells are bright/pink-red, not the deep maroon the earlier brief specified.
  Acceptable — it's a **photographic hero artifact** (like the founder photo / body figure), and the "single-accent,
  no other hues" rule governs UI **chrome**, not a photo. HARD RULE: red must never leak into UI chrome — no
  red/amber/green status dots or icons anywhere (the traffic-light set stays rejected; status = plain phrase +
  `.cchip` tier). Rename the asset to a clean path on build (e.g. `frontend/assets/brand/lab-stream.png`).
- Move `bloodwork (1).png` / `(2).png` out of the shipped path (unused).

### 2. Hero contradiction — RESOLVED: The Stream PNG, NOT the lit body
This doc documents two heroes. **Locked: hero = The Stream PNG.** The "Inner Constellation lit body as the hero"
(Spatial-grammar + Artifact-verdict sections) is **DROPPED**, per the user's 2026-06-26 pivot. Consequences for
/warcry — do NOT build any of: per-organ localization, `ORGAN_ZONES`, `LAB_TO_ZONE`, the `bf-organs` SVG overlay,
or marker↔card linking. The artifact is **atmospheric, unified across all report types**, carries no bound data.

### 3. Page spec — LOCKED (top → bottom), all on existing primitives
1. Slim disclaimer banner — reuse `.read-topnote` (`index.html:678`).
2. **Verdict header** — large slate-ink plain takeaway + existing `.opill` confidence pill + one accent
   **"N to review"** count token + the explicit reassurance-ratio line ("22 of 24 checked and look normal").
   Typography, not a widget.
3. **The Stream** hero card — aside (right) on desktop, below the verdict on mobile. Atmospheric only.
4. **Flagged-card stack** — render high-confidence flagged items AS `.frow` rows (`index.html:486`); the two
   on-demand reveals live in `.frow-extra` (`index.html:495`): "See the numbers" (raw value + range + range bar)
   and "See it on your report" (proof-crop). **Pre-expand the top flagged card** via `.frow.active` so the
   3-second contract lands with zero taps.
5. **Collapsed normals** — one `cov-row`-register line ("22 of 24 checked, all normal", expandable). Descriptive,
   not diagnostic; route unmapped analytes to a visible **"Other results"** bucket. Optional body-system grouping
   only INSIDE this drawer when results ≥ ~20.

**Spatial grammar (corrected):** Verdict header spans the top; below it two columns — flagged cards as the findings
thread (primary, left/center) + The Stream hero card (aside, right); collapsed normals below. Mobile: Verdict →
Stream → cards → normals. 8px grid; sidebar + topbar shell unchanged.

### 4. Range bar (on-demand, flagged-only)
Hairline `--surface-2` rail · normal band `--accent-softer` · value as one accent tick. **Degrades to no-bar** when
the range isn't two-sided numeric (positive/negative, one-sided, off-scale); clamp extremes to a labelled
"well above/below" end-cap. **Never fabricate a tick position.** Pure CSS, no chart lib.

### 5. Verdict-tone safety gate — LOCKED (the #1 /warcry hardening)
- Tone is **bound to the flagged count**: 0 flags → reassure ("Everything looks normal"); ≥1 flag → "worth a look",
  NEVER "fine" / "everything's fine" / "all good".
- Verdict is gated on the **same high-confidence threshold as the cards** — nothing reaches the verdict that isn't
  card-worthy.
- **Never** names a diagnosis or a treatment/drug, in the verdict or the cards.
- **Low parse confidence OR uncertain extraction → fixed neutral template** ("MIKA read your report — please review
  the values with your doctor."), no generated reassurance.
- Counts/severities come from the structured `results[]`, never from prose; the verdict must not strengthen a hedged
  value. (Ties to the global anti-hallucination + clinical-claim rules — never strengthen a hedged medical claim.)

### 6. Data model Opus returns (the page renders this)
`overall`: plain takeaway + confidence (high/moderate/low). `results[]` each: `plain_name`, `status`
(low|normal|high), `severity_phrase`, `confidence` (Confirmed|Likely|Possible), `plain_meaning`, `value`,
`ref_range`, optional `report_location` (for the proof-crop, approximate region OK; honest fallback = show the
cropped row/page, never a fabricated box). Split: high-confidence flagged → cards; everything else → the collapsed
truthful count.

### 7. New surface wiring (name it for /warcry)
New sidebar nav entry (**"Lab report"**) + a screen state mirroring `home → wait → read` as
**`lab-home` (empty/dropzone) → `wait` → `lab-read`**. Reuse the existing upload → SSE-progress → durable-disk
persistence pipeline (report.json/meta.json keyed by `job_id`); Opus reads the parsed report directly — **no DICOM/
measurement pipeline**. PDF uploads render to image via the existing PyMuPDF/fitz path for the proof-crop; photos
are already images. Empty state: existing dropzone, copy "Add your lab or blood report".

### States (unchanged from above, restated as locked)
all-normal (calm, no card stack, ratio line + accent check) · many-flagged (honest non-alarming, cards most-off
first, normals still collapse) · error/unreadable (calm error register, no fake results) · empty (dropzone).

> Concept: **The Verdict** (hero = **The Stream**, `bloodwork (3).png`) — finalized in this doc.

## Next step
Finalized concept → run **`/warcry`** for the implementation plan (data model + Opus prompt/schema + the page +
the verdict-tone safety gate), then bulletproof → katana. `/sincere` humanises the final copy after build.

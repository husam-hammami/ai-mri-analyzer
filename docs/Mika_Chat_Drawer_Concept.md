# MIKA Case-Chat — UI/UX concept: "Beside"

> Forged in /daedalus (2026-06-26). Refines the frontend/drawer spec of `docs/Mika_Chat_Plan.md`.
> Status: **APPROVED** (per /eagleye CONTINUE→build) — folded into the chat plan's Frontend section; build with /katana.

**Thesis.** Reframe "medical chatbot" as *a calm companion reading your scan beside you.* Not two-sided
bubbles with typing dots — a single quiet column where the patient's question is a soft prompt and MIKA's
reply is a **plain-language note anchored to the patient's own finding** (what it is → what it could mean for
you → a plain next step), tethered by the existing certainty glyph to the marker on their body figure.

## Decisions (what /katana should build)
- **Anchored answer note** (the proprietary artifact): a calm bordered note, NOT a bubble. Carries the existing
  `CertaintyGlyph` (● Confirmed / ◐ Likely / ○ Possible) + a tiny "about your <certainty> finding · <level>"
  line, and a `view on scan` chip. Soft structure — render only the sections the answer has; short answers stay
  one paragraph (mitigates rigid-3-section robotic feel).
- **Signature interaction — "peek," not "leave":** tapping `view on scan` DIMS the drawer to translucent
  (doesn't close it) and lights the body marker + swaps the proof image underneath, so the patient sees it on
  their body while the answer stays readable. Reuses `selectFinding`/`landmarkXY` (`index.html:2630/1167`).
- **Shell:** right slide-over, 380px, over the proof column (page stays no-scroll; only the thread scrolls).
  Header = MIKA mark + "Ask about your read" + the `this study only` pill + ✕. Footer = soft input
  ("Ask in your own words…") + the 3 starter chips above it. Mobile = full-screen sheet.
- **Patient question** = quiet right-aligned soft line (`you` label), light surface — deliberately understated
  vs the answer note (the answer is the focus, not a symmetrical chat).
- **Tone/posture:** unhurried, warm, reassuring. Thinking state = a slow 1.6s breathing pulse on the MIKA mark
  ("MIKA is reading your report…"), NEVER bouncing dots. Error = a calm inline line, not a red toast.
- **Palette:** MIKA's existing tokens only — single accent #2563EB, slate ink, near-white surfaces. No new
  color, no glassmorphism, no gradient. Certainty colors = the existing palette (Confirmed full / Likely
  reduced / Possible neutral).

## Motion (native CSS, MIKA's existing vars)
- Drawer in: `translateX(16px)→0` + opacity, ~240ms `--ease-out` — slides from the inline-end (flips for RTL).
- Answer note: fade + rise 8px / 200ms.
- Thinking: 1.6s opacity "breath" (0.5↔1) on the mark.
- `prefers-reduced-motion`: fades only — no slide, no pulse.

## States
empty (scope pill + 3 chips + one warm line) · thinking (breathing mark) · answered (anchored notes) ·
peek (drawer dimmed, marker lit underneath) · error (calm inline) · flag-off (no trigger at all).

## RTL
Drawer flips to the inline-start under `[dir=rtl]` (`right:auto; left:0; border-left:0; border-right:1px`);
the certainty glyph + body figure are NOT mirrored. (Chat answers in the patient's own language natively — no
translation layer; the drawer chrome uses the existing `L()`/`AR_UI` i18n.)

## Primary risk
Rigid 3-section answers feel robotic → keep sections soft; one-paragraph answers allowed.

---

## v2 — the "wow" elevation (/daedalus refine, 2026-06-26)
Fine → genuinely premium, by making the drawer feel like **MIKA itself speaking**, not a chatbot. Three moves:

1. **Brand presence — the MIKA helix coin (kills the cheap speech-bubble).** The assistant avatar is the REAL
   silver helix mark, cropped from `frontend/assets/logo.png` (the left ~620px helix) onto a small **dusk-navy
   circle** (`#0A0F1D`) so the metallic mark reads correctly on a light surface — a little "MIKA coin." Build
   crops a transparent PNG `frontend/assets/brand/mika-mark.png` (DON'T re-draw it as SVG — use the real raster,
   per house rule). Sizes: 26px in the header + answer speaker, 40px in the empty state. The **thinking state is
   this coin breathing** (1.6s opacity+scale pulse) under "MIKA is reading your report…" — branded, calm, the
   signature wow detail.

2. **Answers are beautifully-typeset NOTES, markdown-rendered.** The model returns markdown (`**bold**`, line
   breaks, "What this means for you:" / "Next step:" labels). Build a tiny inline renderer (no library): `**x**`
   → semibold ink, blank lines → paragraph spacing, `- ` → a clean bullet, and bold "Label:" leads → a quiet
   **accent-colored mini-label**. Generous leading (1.66), comfortable measure. The note carries the MIKA coin
   as the speaker at its top-left + a hairline border + 14px radius — and echoes the Read screen's own answer
   box (same single accent) so chat and report share one visual language. NO literal asterisks ever again.

3. **A warm, premium empty state + personal anchoring.** The 40px coin, a calm line ("Ask me anything about
   this scan — I'll keep it plain."), the study shown subtly in the header subtitle ("your brain MRI") so it
   feels personal/anchored, and the 3 starter chips as elegant pills. The patient's own question stays a quiet
   right-aligned bubble so the branded answer is the hero.

**Motion (native):** drawer slide-in 240ms ease-out (inline-end; RTL flips); answer note fade+rise 8px/220ms;
the coin breathes 1.6s in the thinking state; `prefers-reduced-motion` → fades only, no pulse/slide.
**Palette unchanged:** single accent `#2563EB`, dusk-navy coin, near-white surfaces — no new hue, no glass.
**Build notes for /katana:** crop the helix coin asset; add the markdown mini-renderer; swap the speaker glyph
to the coin everywhere; header subtitle = the study line; keep RTL flip + i18n + the no-scroll thread.

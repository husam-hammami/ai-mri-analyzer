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

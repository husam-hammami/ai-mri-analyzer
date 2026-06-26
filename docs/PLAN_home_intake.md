# Concept: "Start, or pick up" — home entry points for MIKA's three reads

Daedalus design pass (2026-06-26) for surfacing imaging analysis, lab reports, and "Ask MIKA"
chat on the home/landing screen, natively to the locked "The Read" design system.

## Decision
Keep the imaging **dropzone as the single focal intake instrument**. Beneath it, add a secondary
two-item rail that is honestly typed:
- **Lab report** — a second way to START a read → `onNav('lab')` (its own lab-home screen). Always shown.
- **Ask MIKA** — a way to PICK UP an existing read → `onNav('recent')`. Shown only when ≥1 completed
  read exists AND chat is enabled (`chatAvail`). No dead/disabled control.

Rejected: the interim 3-equal-card switcher above the title (demotes the instrument, competes with the
figure-as-hero, reads as the AI value-prop triplet, and dishonestly frames per-study chat as a third
upload flow).

## Why not co-equal 1:1:1
The home's body-figure + coverage columns are imaging-specific (the anatomy figure anchors the screen).
Lab cannot be visually co-dominant here without making the hero incoherent; chat has no cold start.
"Equal prominence" is delivered as **equal discoverability** (dedicated hero real estate, crafted icon
tiles), not equal pixel weight. Imaging stays the focal instrument (rubric B2 / figure-as-hero).

## Visual treatment (native to the dropzone)
Rail of two horizontal mini-cards under `<UploadDropzone>` inside `.home-hero`:
`[ accent-soft SVG icon tile ] [ title / one-line sub ] [ chevron ]`
- Reuses dropzone material: `--surface` bg, `--line` hairline, `--accent-soft` icon tile, `--r-md`.
- Lab tile = droplet icon; Ask tile = chat icon (both SVG-in-tile for consistency).
- One tier below the big dashed instrument (smaller, lighter) → focal hierarchy preserved.
- Hover: `--accent-line` border + `--surface-2` bg, 150ms color-only (no transform / no infinite motion).

## States
- Lab: always present (always a valid new read).
- Ask: present iff `chatAvail && hasRecents` (fetched via `/api/reports`). Absent otherwise.
- Mobile: rail wraps to stacked full-width items.

## Constraints honored
- No-scroll: ~56px below the dropzone (less than the switcher's ~136px above the title).
- Single blue accent, 8px scale, DM Sans, certainty colors untouched. No new effects.
- Maps to existing handlers: dropzone (onFiles/onStart), onNav('lab'), onNav('recent').

## Score (Daedalus rubric)
clarity 9 · usefulness 9 · originality 8 · brand 9 · emotion 9 · a11y 8 · responsive 8 · feasibility 9.

Built directly (small, well-specified frontend change) rather than via /warcry; verified in preview.

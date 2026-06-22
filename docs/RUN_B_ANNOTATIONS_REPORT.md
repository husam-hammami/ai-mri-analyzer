# Run B — annotations: never silently drop a finding's visual

Plan: `~/.claude/plans/so-what-s-the-plan-snappy-treasure.md`. PHI-safe.

## The problem
Annotation placement is already computational and intensity-verified (`dicom_engine.py`
`_verify_and_reposition_tip` + `EXPECTED_INTENSITY_RANGES`). But when a tip failed the 3C
intensity check after the neighborhood search, `create_annotated_sagittal` **dropped the
annotation entirely** (`continue`, "NOT drawn"). A genuine finding could thus lose its visual
because the intensity gate was strict — exactly the "incapacitating" behavior to fix.

## What changed (`backend/core/dicom_engine.py`)
- Added `_draw_region_band()` — a labelled REGION BOX (with corner ticks marking it "approximate,
  not a pinpoint").
- On a failed-verification tip, **fall back to a region band at the computed location** instead of
  dropping it. The audit row becomes `status="region_band", drawn=True`. So every finding keeps a
  visible marker; nothing is silently dropped. (A wrong *pinpoint* is still never shipped — the
  region band is honestly approximate.)
- Clearer display: figure label font 11→13, title 14→15.
- Count log now reports region-band fallbacks separately.

Aligned the instructions so the prompt + skill match the code:
- `services/agent_runner.py` ANNOTATION PRECISION — "if none matches, **fall back to a region band**…
  never silently drop the finding's visual" (was "DROP the annotation").
- `skills/mri-spine-analysis/SKILL.md` Step 3E — "Region band, don't fudge or drop."

## Verification (run here)
- `tests/test_run_b_annotations.py` — region band actually draws a marker; the silent-drop path is
  gone; prompt + skill agree.
- `python -m pytest -q tests` → **78 passed** (75 prior + 3 new). `py_compile` OK.

## Deferred to a visual check in the user's environment
- **Report formatting polish** (disclaimer→footer, figure height) — `report_builder.py` renders a
  reportlab PDF that can't be visually verified in this session. Blind layout edits are reckless;
  this should be done where the PDF can be rendered and eyeballed.
- **Intensity-range audit** — widening `EXPECTED_INTENSITY_RANGES` to match real T2/T1 histograms
  needs real DICOM (PHI, outside the repo). With the region-band fallback in place, an overly-strict
  range now degrades to a region band rather than a dropped finding, so this is lower-risk; still
  worth auditing on real studies.

# MIKA Accuracy Assessment — March 2026

## Current Estimated Accuracy: ~40-45%

This is an honest estimate, not a measured number. We do NOT have ground truth
validation yet (Module 4 of Plan C+V). All estimates below are based on blind
testing observations and architectural analysis.

---

## What Changed Since Last Assessment (~35-40%)

### Completed: Master Prompts (Module 2 of Plan C+V)
- 10 fellowship-level master prompts wired into `claude_interpreter.py`
- Each prompt now has mandatory checklists, grading tables, measurement refs
- Prompt depth went from ~200 words to 3,000-8,000 words per anatomy
- **Impact: +5-10% estimated** (Claude now knows WHAT to look for and HOW to grade)

### Still Broken: 4-Image Bottleneck (Module 1 NOT done)
The #1 accuracy killer is STILL in place. Lines 564-572 of `app.py`:
```python
# Prepare key images for Claude (up to 4 to manage token cost)
key_images = {}
for img_name in ["sag_t2_annotated", "level_reference", "multi_sequence_panel"]:
    ...
for img_name in ["contrast_L4L5", "contrast_L5S1"]:
    if ... and len(key_images) < 4:
        ...
```
Claude still sees only 4 images from a 200+ slice study. The master prompts
tell Claude to "check every level bilaterally" but Claude can't do that when
it only has a single midline sagittal slice.

### Not Built Yet
- BatchSender (Module 1) — send all images → **biggest single improvement**
- VerificationPass (Module 3) — self-review
- ValidationFramework (Module 4) — real accuracy numbers

---

## Accuracy Breakdown by Component

| Component | Status | Est. Impact | Cumulative |
|-----------|--------|-------------|------------|
| **Baseline** (4 images, generic prompts) | Was | — | ~35-40% |
| **Master Prompts** (fellowship-level) | **DONE** | +5-10% | **~40-45%** |
| **BatchSender** (all 80 images) | NOT DONE | +20-25% | ~60-70% |
| **VerificationPass** (self-review) | NOT DONE | +5-10% | ~70-80% |
| **Validation iteration** (data-driven fixes) | NOT DONE | +10-15% | ~80-90% |

### Why Master Prompts Alone Add Only ~5-10%

The prompts are excellent — they tell Claude exactly what grading criteria to use
(Pfirrmann disc grades, Lee foraminal grades, Modic classification, etc.). But:

1. **Claude can't grade what it can't see.** With 4 images (often just midline
   sagittal + a 4-panel collage), Claude has no axial views to assess foramina,
   no parasagittal views for lateral structures, no STIR to confirm Modic type 1.

2. **The checklist demands bilateral assessment.** The spine prompt says
   "assess foramina at EVERY level, bilateral." But there are no foraminal
   images in the 4-image set. Claude either guesses (hallucination) or says
   "cannot assess" (Tier D). Both hurt accuracy.

3. **Grading tables only help if Claude sees the pathology.** Knowing Pfirrmann
   grade III = "inhomogeneous, grey, intermediate signal" is useless if the disc
   isn't visible in any of the 4 images.

**Bottom line:** Master prompts turned Claude from a medical student into a
fellow — but a fellow who's only allowed to look at 4 images from the study.
The BatchSender will give the fellow the full study to read.

---

## Accuracy by Anatomy Type

| Anatomy | Current Est. | Why | Biggest Gap |
|---------|-------------|-----|-------------|
| **Spine** | **45-50%** | Has quantitative measurements + calibrated AP diameters | Still only 4 images |
| **Brain** | 30-35% | Visual-only, no measurements, limited image coverage | Missing DWI, FLAIR views |
| **MSK** | 25-35% | Visual-only, limited cross-section views | Missing PD-FS axials |
| **Cardiac** | 20-30% | No cine, no LGE assessment, minimal images | Missing all functional data |
| **Abdomen** | 25-35% | Visual-only, dynamic phases not systematically sent | Missing arterial/portal phases |
| **Breast** | 20-30% | Visual-only, kinetic curves impossible with 4 images | Missing subtraction/kinetic |
| **Prostate** | 20-30% | Visual-only, PI-RADS needs DWI + ADC + T2 | Missing multi-sequence |
| **Vascular** | 25-35% | Limited MRA coverage | Missing full MIP series |
| **Head & Neck** | 25-35% | Visual-only, deep space assessment limited | Missing post-contrast FS |
| **Chest** | 25-35% | MRI already limited vs CT for lung | Missing HASTE series |

**Note:** Spine is highest because it's the ONLY anatomy with quantitative
measurements from DICOMEngine. All others are 100% visual interpretation.

---

## What Would Move The Needle Most

### Priority 1: BatchSender (est. +20-25%)
Sending all 40-80 images instead of 4 would:
- Allow bilateral foraminal assessment (axial T2 slices)
- Enable multi-sequence cross-referencing (T1 + T2 + STIR for Modic)
- Show dynamic contrast phases (for abdomen, breast, prostate)
- Cover full anatomy (not just midline slice)
- Enable DWI assessment (for brain, prostate, body)

**This is the #1 priority. Everything else is secondary.**

### Priority 2: VerificationPass (est. +5-10%)
A second Claude call reviewing the initial report catches:
- Overcalls (artifact read as pathology)
- Grading errors (wrong Pfirrmann, wrong stenosis grade)
- Missed anatomy (regions not addressed)
- Contradictions (finding doesn't match images)

### Priority 3: ValidationFramework (enables +10-15% via iteration)
Without ground truth, we're guessing accuracy. The ValidationFramework lets us:
- Know our REAL accuracy per finding type
- See exactly where we fail (disc grading? foraminal stenosis? Modic typing?)
- Measure the impact of every change
- Iterate data-driven (fix weakest finding type first)

---

## When Will We Know Real Accuracy?

**After ValidationFramework (Module 4) is built and run against SPIDER dataset.**

Timeline estimate:
- Build BatchSender: ~3-4 days
- Build VerificationPass: ~2-3 days
- Build ValidationFramework: ~3-4 days
- Download SPIDER dataset + run first 50 studies: ~2-3 days
- **First real accuracy number: ~2 weeks from now**

Until then, all numbers in this document are estimates based on:
- Architecture analysis (what Claude can and cannot see)
- Prior blind testing observations
- Published literature on AI radiology performance

---

## Key Takeaway

**We upgraded Claude's knowledge (prompts) but haven't upgraded Claude's vision
(images). Knowledge without vision is ~40-45%. Knowledge WITH vision will be
60-70%. Knowledge + vision + self-review + iteration → 80-90%.**

The path is clear. BatchSender is next.

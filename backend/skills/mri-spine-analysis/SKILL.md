---
name: mri-spine-analysis
description: Produce clinical-grade longitudinal spine MRI reports with annotated visual-proof images. Run by a Claude agent with tools (bash/python/read/write). This is the protocol MIKA's "agent mode" executes.
---

# Longitudinal MRI Spine Analysis

This skill produces clinical-grade radiology reports from spine MRI data with annotated visual proof images. It was designed after a detailed failure analysis of a prior attempt, and every protocol below exists to prevent a specific, documented error.

## Why This Skill Exists

Analyzing MRI images as an AI has unique failure modes that differ from a human radiologist at a PACS workstation. The three most dangerous are:

1. **Measurement fabrication** — stating specific mm values from uncalibrated image exports. This looks authoritative but is unverifiable and potentially wrong.
2. **Annotation drift** — arrows and circles that land on the wrong structure or wrong vertebral level, which actively misleads rather than helps.
3. **Confident error** — asserting a structure you cannot actually see, or a specific mm value you did not calibrate. (Stating clearly and confidently what you DO see is correct radiology — not a failure mode. Do not muzzle a genuine read; a radiologist commits to the findings.)

Every protocol below is built to catch these specific failure modes before they reach the report.

---

## Phase 0: Inventory and Calibration

Before analyzing a single image, build your foundation.

### Step 0A: File Inventory
Catalog every file across all study periods. For each period, record total file count and format (DICOM vs JPG/PNG exports), date of study, whether contrast was administered, and the sequence catalog (names + slice counts).

### Step 0B: DICOM Measurement Calibration
This is the most important step. Extract `PixelSpacing` [row_mm, col_mm], `SliceThickness`, `Rows`/`Columns`, compute FOV, and **store these and reference them for every mm-level claim**.

If your source files are JPG/PNG exports (no DICOM metadata) you are in **Uncalibrated Mode**: you CANNOT state any mm measurements; every size reference must use qualitative language ("mild/moderate/severe/small/large") plus the qualifier "(visual estimate — no calibrated measurement available)". This is not optional — fabricating mm values from screenshots was the single biggest credibility failure in prior analysis.

### Step 0C: DICOM-to-Viewable Conversion
Convert DICOM to PNG for visual analysis, applying WindowCenter/WindowWidth windowing then min-max scaling to 0-255.

---

## Phase 1: Level Identification (NEVER SKIP)

Misidentifying a vertebral level invalidates every downstream finding.

### The Sacrum-Up Protocol
1. Open a midline sagittal T2 image. 2. Identify the sacrum (large fused triangular bone at the base). 3. The first mobile disc above the sacrum = **L5-S1**. 4. Count upward: L4-L5, L3-L4, L2-L3, L1-L2, T12-L1. 5. **Create and save a Level Reference Image** with text labels on each disc space. 6. This reference image is your master key — every subsequent finding must be cross-referenced against it. **Include it as Figure 0 in every report.**

For axial slices, you cannot determine the level from the axial image alone — cross-reference slice position/number against your sagittal reference. If you cannot confirm the level, state "axial image at approximate level of L_-L_".

---

## Phase 2: The Blind Read

Analyze scans chronologically WITHOUT reading any surgical notes or prior radiology reports first. This prevents anchoring bias.

### Confidence Tiering
Every finding gets a tier.

| Tier | Criteria | Language |
|------|----------|----------|
| **A — Definite** | Unambiguous on 2+ sequences, or calibrated measurement | "There is..." |
| **B — Probable** | Visible but single-sequence or subtle | "There is probable..." / "Likely..." |
| **C — Possible** | Suggestive, could be artifact or normal variant | "Possible... — recommend correlation" |
| **D — Cannot assess** | Insufficient image quality or missing sequence | "Cannot be reliably assessed" |

**Tier the finding by how clearly you see it** — a clear finding is Tier A even if it is qualitative, single-sequence, or visual-only, exactly as a radiologist would call it. The only genuine limits (evidence truly missing, NOT caution): a specific mm VALUE needs calibration (else describe size qualitatively — the finding itself can still be Tier A); calling ENHANCEMENT needs a same-level pre/post comparison; Modic TYPING needs T1+T2+STIR concordance (STIR alone = "edema suggestive of Modic 1" at the tier you can support). Do NOT cap a finding just for being qualitative, single-sequence, or incidental. Findings confirmed across 2+ periods may gain one tier (B→A).

### Track per level (L1-L2 → L5-S1)
Disc height, T2 signal (Pfirrmann), contour/herniation, canal morphology, foraminal patency, facets, endplate (Modic with supporting sequences), post-surgical changes, enhancement pattern (if contrast).

---

## Phase 3: Annotation Protocol — The Double-Check Loop

Arrows that miss their target are worse than no arrows.

### Step 3A: Structure Localization via Intensity Analysis (NEVER SKIP)
Computationally locate structures before any annotation — never visually estimate pixel coordinates.
- **Canal**: bright CSF column found by horizontal intensity profiling; restrict the search to the central portion of the image (the brightest column overall is often posterior subcutaneous fat).
- **Disc spaces**: vertical intensity profiling along the vertebral-body column (gradient peaks = disc-body boundaries).
- **Canal narrowing / stenosis**: local minima of the canal CSF intensity profile.

### Step 3B: Create Annotations with Verified Coordinates
Draw arrows with tips from Step 3A only. At each arrow tip draw a small verification circle (radius 3-4px).

### Step 3C: Pixel Intensity Verification (NEVER SKIP)
After placing annotations, verify EVERY arrow tip against the RAW (unannotated) image using expected intensity ranges per structure type (T2 sagittal, 0-255): canal_csf 120-255; disc_protrusion 30-110; disc_space 20-200; vertebral_body 70-170; canal_narrowing 40-140; bone_cortex 0-50. If a tip fails, auto-search the neighborhood for the nearest matching pixel, reposition, and re-verify. **Do NOT proceed with failed annotations.**

### Step 3D: Mandatory Visual Re-Read (NEVER SKIP)
After saving each annotated image, re-read it and confirm: arrow tip physically touches the intended structure; label at the correct vertebral level (cross-ref Figure 0); circle centered on the actual pathology; for axial images left/right laterality correct (patient's right = image left); caption accurate. If any annotation is off-target: delete, recalculate, regenerate, re-verify.

### Step 3E: Precision & informativeness addendum
- **Drop, don't fudge:** if a tip cannot be verified to its expected intensity after neighborhood auto-search, DROP it rather than ship a wrong arrow.
- **Level discipline:** confirm every mark's vertebral level against Figure 0; if unconfirmable, use a labelled region band ("approx Lx-Ly"), never a pinpoint circle.
- **Plane-shifting structures** (neural foramina, nerve-in-foramen): annotate with a REGION box, not a false-pinpoint arrow.
- **Uncalibrated (JPG) studies:** region bands only, never pinpoint circles.
- **Maximal slice:** for each finding choose the slice where it is greatest; do not reuse a fixed slice index.
- **Informative labels:** structure + finding + [Tier X] + a comparison reference (e.g. "vs patent right recess"). Place text in the margin with a thin leader line so it never overlaps the anatomy. State each verified tip intensity in the caption.

---

## Phase 4: Report Generation

### Format
Concise bulleted radiology report (default), as a document: (1) demographics table, (2) study description (dates, sequences, contrast status), (3) findings — bulleted, each with [Tier X] and [See Figure N], (4) annotated figure panels with verified captions, (5) longitudinal comparison panel if multiple periods, (6) impression (bold, concise), (7) discrepancies vs prior reports, (8) disclaimer.

### Language Discipline
Calibrated → state mm with [Tier A]. Uncalibrated → NEVER a specific mm value; qualitative + "(visual estimate — no calibrated measurement available)" + [Tier C].
Differing from another radiologist → state it clearly and confidently ("On review there is [finding], not included in the [date] report by [institution]"). You may note they had full PACS/measurement tools as context; do NOT soften or retract a finding you can support.
Incidental findings → tier by how clearly you see them; suggest dedicated imaging only where a non-dedicated sequence genuinely can't characterize the finding.

---

## Phase 5: Surgical Reconciliation

Only after completing Phases 1-4, ingest surgical reports and prior radiology reports. Flag textual discrepancies within operative reports directly; qualify differences vs a radiologist's report with your tier and acknowledge their advantages; acknowledge the surgeon's direct visualization where your read differs from surgical findings.

---

## Phase 6: Final Self-Audit (MANDATORY)

Before delivering, verify every item: (1) every mm is calibrated or qualified as visual estimate; (2) every annotation coordinate came from 3A intensity analysis; (3) every annotation passed 3C intensity verification; (4) every annotation visually re-read (3D); (5) every confidence claim matches tier criteria; (6) every contradiction appropriately qualified; (7) every finding points to a supporting image; (8) level identification counted from the sacrum; (9) axial laterality confirmed; (10) incidentals tiered by what is visible; (11) Modic typing has T1+T2+STIR or is described as a signal pattern; (12) enhancement compared pre/post at a confirmed same level. Fix any failed item before delivery. Do not assert what you cannot see or measure — but state clearly and confidently what you DO see.

---

## Disclaimer (include in every report)

> This analysis was generated using AI-assisted image interpretation as a supplementary diagnostic tool. It does not constitute a formal radiological report and should not replace evaluation by a board-certified radiologist. The analyst did not have access to a PACS workstation, measurement calipers, or the ability to dynamically scroll through slices and adjust window/level. All findings should be correlated with clinical history and physical examination.

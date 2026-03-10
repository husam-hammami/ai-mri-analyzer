# MIKA Plan C+V — Claude Agents Implementation Plan

## Mission

Reach **90%+ validated accuracy** on MRI interpretation using a lean, data-driven
approach. Build only 4 core modules, validate immediately, add complexity only when
data proves it's needed.

**Selected Plan:** Plan C+V (Maximize Claude + Validation)
**Why this plan won:** Highest certainty of improvement. The current bottleneck is
NOT Claude's intelligence — it's the pipeline sending only 4 images from a 200+ slice
study. Fix the pipeline first, measure, then iterate.

---

## The 4 Core Modules

| # | Module | What It Does | Why It Matters |
|---|--------|-------------|----------------|
| 1 | **BatchSender** | Send ALL images to Claude (80+ vs 4) | Removes the #1 accuracy killer |
| 2 | **MasterPrompts** | Fellowship-level systematic search prompts | Turns Claude into a specialist |
| 3 | **VerificationPass** | Second Claude call as senior attending review | Catches overcalls and missed findings |
| 4 | **ValidationFramework** | Compare against ground truth datasets | Gives us real accuracy numbers |

---

## Current State (Before)

```
200 DICOM slices uploaded
    |
DICOMEngine picks midline slice per sequence
    |
Creates 3-4 annotated PNGs
    |
Sends 4 images + measurements JSON to Claude
    |
Claude guesses from 2% of data --> ~35-40% accuracy
```

## Target State (After)

```
200 DICOM slices uploaded
    |
StudyOrganizer sorts ALL slices by sequence/plane/position  [EXISTING - already in 90% plan]
    |
BatchSender sends ALL images to Claude organized by priority [MODULE 1]
    |
MasterPrompt: systematic search + grading criteria           [MODULE 2]
    |
Claude analyzes FULL study --> structured findings
    |
VerificationPass: senior attending catches errors             [MODULE 3]
    |
ValidationFramework: compare vs ground truth                  [MODULE 4]
    |
Target: 85-92% validated accuracy
```

---

## Agent Skills Required

Each module maps to a Claude Agent skill. These are the capabilities the agent
needs to build each module.

### Skill 1: Backend Python Engineering
- FastAPI endpoint modification
- Async Python with background tasks
- File I/O with pydicom, numpy, PIL
- Base64 image encoding and token budgeting
- Error handling and logging

### Skill 2: Claude API Integration
- Anthropic Python SDK (messages API)
- Multi-image content blocks (base64 PNG/JPEG)
- Token counting and budget management
- Structured JSON response parsing
- System prompt engineering

### Skill 3: Medical Imaging Domain
- DICOM metadata interpretation (PixelSpacing, SliceThickness, SeriesDescription)
- MRI sequence classification (T1, T2, FLAIR, DWI, STIR, etc.)
- Imaging plane detection (axial, sagittal, coronal)
- Radiology grading systems (Pfirrmann, BI-RADS, PI-RADS, Fazekas, etc.)
- ACR reporting standards

### Skill 4: Validation & Metrics
- Ground truth dataset handling (SPIDER, BraTS, fastMRI)
- Statistical metrics (sensitivity, specificity, PPV, NPV, F1)
- Per-finding-type accuracy breakdown
- Confusion matrix generation
- Automated regression testing

---

## MODULE 1: BatchSender

### File: `backend/services/batch_sender.py`

### Problem
Lines 564-572 of `backend/app.py` hardcode a 4-image limit:
```python
for img_name in ["sag_t2_annotated", "level_reference", "multi_sequence_panel"]:
    if img_name in job.annotated_images:
        key_images[img_name] = engine.get_image_base64(job.annotated_images[img_name])
for img_name in ["contrast_L4L5", "contrast_L5S1"]:
    if img_name in job.annotated_images and len(key_images) < 4:
        key_images[img_name] = engine.get_image_base64(job.annotated_images[img_name])
```

This discards 95%+ of the diagnostic data. Claude never sees most of the study.

### Solution
Send ALL converted PNG images to Claude, organized by diagnostic priority,
with a token budget to stay within API limits.

### Implementation Steps

**Step 1: Create BatchSender class**
```
File: backend/services/batch_sender.py

class BatchSender:
    MAX_IMAGES = 80          # Claude vision limit per request
    TARGET_TOKENS = 150_000  # Leave room for prompt + response
    JPEG_QUALITY = 85        # Balance quality vs tokens

    def __init__(self, work_dir: Path, anatomy_type: str):
        self.work_dir = work_dir
        self.anatomy = anatomy_type
        self.raw_png_dir = work_dir / "raw_png"

    def collect_all_images(self) -> list[ImageEntry]:
        """Scan raw_png/ directory for all converted slices.
        Return list of ImageEntry(path, sequence_name, slice_num, plane, priority)."""

    def prioritize(self, images: list[ImageEntry]) -> list[ImageEntry]:
        """Sort by diagnostic priority per anatomy type.
        Spine: sagittal T2 first, then T1, STIR, axial T2, contrast.
        Brain: FLAIR first, then DWI, T1+C, T2, SWI.
        MSK: PD-FS first, then T2-FS, T1, post-contrast.
        Returns sorted list, trimmed to MAX_IMAGES."""

    def encode_batch(self, images: list[ImageEntry]) -> list[dict]:
        """Encode each image as base64 JPEG within token budget.
        Returns list of Claude content blocks:
        [
            {"type": "text", "text": "=== T2 Sagittal (14 slices) ==="},
            {"type": "image", "source": {"type": "base64", ...}},
            {"type": "text", "text": "Slice 7/14 - Mid sagittal"},
            {"type": "image", "source": {"type": "base64", ...}},
            ...
        ]
        Each image labeled with: sequence name, plane, slice N/total."""

    def build_message_content(self) -> list[dict]:
        """Full pipeline: collect -> prioritize -> encode -> return content blocks."""
```

**Step 2: Image priority tables per anatomy**
```
PRIORITY_ORDER = {
    "spine": ["T2_SAG", "T1_SAG", "STIR_SAG", "T2_AX", "T1_AX", "T1_CONT_SAG", "T1_CONT_AX"],
    "brain": ["FLAIR", "DWI", "T1_CONT", "T2", "SWI", "ADC", "T1"],
    "msk": ["PD_FS", "T2_FS", "T1", "T1_CONT", "T2"],
    "cardiac": ["CINE", "LGE", "T1_MAP", "T2_MAP", "PERF"],
    "chest": ["T2_HASTE", "T1_CONT", "DWI", "T1"],
    "abdomen": ["T2_FS", "DWI", "T1_CONT_PORTAL", "T1_CONT_ART", "T1_OPP", "T1_IN"],
    "breast": ["T1_CONT_SUB", "DWI", "T2", "T1_PRE"],
    "vascular": ["TOF", "CE_MRA", "T1_BB", "T2"],
    "head_neck": ["T1_CONT_FS", "T2_FS", "DWI", "T1"],
    "prostate": ["DWI", "ADC", "T2", "T1_CONT_DCE", "T1"]
}
```

**Step 3: Modify app.py to use BatchSender**
Replace the hardcoded 4-image block (lines 564-572) with:
```python
from backend.services.batch_sender import BatchSender

batch_sender = BatchSender(job.work_dir, job.anatomy_type)
image_content_blocks = batch_sender.build_message_content()
```

Then pass `image_content_blocks` directly to claude_interpreter instead of
`key_images_b64` dict.

**Step 4: Update claude_interpreter.py**
Modify `interpret()` to accept raw content blocks instead of a dict of 4 images.
The new signature:
```python
async def interpret(
    self,
    measurements_json: dict,
    image_content_blocks: list[dict],  # FROM BatchSender
    anatomy_type: str,
    clinical_history: str = None,
    prior_reports: str = None,
    surgical_notes: str = None
) -> InterpretationResult:
```

### Acceptance Criteria
- [ ] All raw PNG images in work_dir/raw_png/ are collected
- [ ] Images sorted by diagnostic priority for each anatomy type
- [ ] Each image labeled with sequence name, plane, slice position
- [ ] Total stays within 150K token budget (resize if needed)
- [ ] Claude receives 20-80 images per study (vs 4 currently)
- [ ] Non-spine anatomies get full image coverage for the first time

---

## MODULE 2: MasterPrompts

### Directory: `backend/prompts/`

### Problem
Current prompts in `claude_interpreter.py` are competent but generic. They list
what to look for but don't enforce systematic search, don't include grading
criteria tables, and don't have anti-hallucination rules.

### Solution
Fellowship-level master prompts per anatomy with:
1. **Mandatory systematic search checklist** (miss nothing)
2. **Grading criteria tables** (standardized scoring)
3. **Normal measurement references** (know what's abnormal)
4. **Sequence interpretation guide** (use each sequence correctly)
5. **Anti-hallucination rules** (when to say "cannot assess")
6. **ACR-standard output format** (structured, professional)

### Implementation Steps

**Step 1: Create prompt directory structure**
```
backend/prompts/
    __init__.py
    base_prompt.py          # Shared rules across all anatomies
    spine_master.py         # Spine-specific master prompt
    brain_master.py         # Brain-specific master prompt
    msk_master.py           # MSK-specific master prompt
    cardiac_master.py       # Cardiac-specific master prompt
    chest_master.py         # Chest-specific master prompt
    abdomen_master.py       # Abdomen-specific master prompt
    breast_master.py        # Breast-specific master prompt
    vascular_master.py      # Vascular-specific master prompt
    head_neck_master.py     # Head & neck-specific master prompt
    prostate_master.py      # Prostate-specific master prompt
```

**Step 2: Base prompt (shared rules)**
```
File: backend/prompts/base_prompt.py

BASE_RULES = """
## IDENTITY
You are a board-certified radiologist with fellowship training reviewing
a complete MRI study. You have access to ALL images from this study.

## ANALYSIS METHOD
You MUST follow the systematic search protocol below. Do NOT skip any
anatomical region even if it appears normal. Document normal findings
explicitly — they are clinically important.

## CONFIDENCE FRAMEWORK
- Tier A (Definite): Finding confirmed on 2+ sequences OR calibrated measurement
  abnormal. Language: "There is..."
- Tier B (Probable): Finding seen on 1 sequence, consistent with known pattern.
  Language: "There likely is..."
- Tier C (Possible): Suggestive finding, may be artifact or normal variant.
  Language: "Possible... recommend clinical correlation"
- Tier D (Cannot assess): Sequence not available or image quality insufficient.
  Language: "Cannot be reliably assessed due to..."

## ANTI-HALLUCINATION RULES
1. If you cannot clearly see a structure, say "not well visualized" — never invent.
2. If only one sequence shows a finding, cap at Tier B maximum.
3. If measurements are uncalibrated, cap at Tier C maximum.
4. If image quality is degraded (motion, artifact), explicitly state limitation.
5. Never report a finding you wouldn't bet your medical license on.
6. When in doubt between two grades, choose the less severe one.

## OUTPUT FORMAT
Return valid JSON matching the schema provided. Every field must be populated.
Use null for fields that cannot be assessed, never leave empty strings.
"""
```

**Step 3: Spine master prompt (example — most developed anatomy)**
```
File: backend/prompts/spine_master.py

SPINE_MASTER_PROMPT = BASE_RULES + """
## SPINE MRI — SYSTEMATIC SEARCH PROTOCOL

You are a fellowship-trained neuroradiologist. Analyze this spine MRI study
using the following mandatory checklist. Check EVERY item.

### MANDATORY CHECKLIST (do not skip any):
[ ] 1. Alignment: lordosis/kyphosis, listhesis at each level, scoliosis
[ ] 2. Vertebral bodies: height, signal, fracture, hemangioma, metastasis
[ ] 3. Disc at EACH level: height, signal (Pfirrmann grade), herniation type
[ ] 4. Central canal: AP diameter, CSF effacement, cord compression
[ ] 5. Neural foramina: bilateral at each level, fat obliteration, nerve root
[ ] 6. Facet joints: hypertrophy, effusion, cyst
[ ] 7. Ligaments: ALL/PLL thickening, ligamentum flavum hypertrophy
[ ] 8. Spinal cord/conus: signal, syrinx, cord compression, conus level
[ ] 9. Paraspinal soft tissues: masses, collections, muscle atrophy
[ ] 10. Endplates: Modic type at each level (T1 + T2 + STIR concordance)
[ ] 11. Sacrum/SI joints: if visualized
[ ] 12. Incidentals: kidneys, aorta, lymph nodes if in FOV

### GRADING CRITERIA

**Pfirrmann Disc Grading (T2 signal):**
| Grade | Structure | Signal | Height | Distinction |
|-------|-----------|--------|--------|-------------|
| I | Homogeneous, bright white | Hyperintense | Normal | Clear nucleus/annulus |
| II | Inhomogeneous, +/- bands | Hyperintense | Normal | Clear |
| III | Inhomogeneous, grey | Intermediate | Normal to slightly decreased | Unclear |
| IV | Inhomogeneous, dark grey | Hypointense | Slightly decreased | Lost |
| V | Inhomogeneous, black | Hypointense | Collapsed | Lost |

**Disc Herniation Classification:**
- Bulge: >50% circumference, symmetric
- Protrusion: <50% circumference, base wider than apex
- Extrusion: apex wider than base, may extend above/below disc
- Sequestration: free fragment, no continuity with parent disc
- Direction: central, paracentral (L/R), foraminal (L/R), extraforaminal (L/R)

**Central Canal Stenosis (AP diameter):**
- Normal: >13mm
- Mild: 10-13mm
- Moderate: 7-10mm
- Severe: <7mm

**Foraminal Stenosis (Lee grading):**
- Grade 0: Normal, perineural fat present
- Grade 1: Mild, perineural fat partially obliterated
- Grade 2: Moderate, perineural fat completely obliterated
- Grade 3: Severe, nerve root compressed/displaced

**Modic Endplate Changes:**
| Type | T1 | T2 | STIR | Pathology |
|------|-----|-----|------|-----------|
| 1 | Hypointense | Hyperintense | Hyperintense | Edema/inflammation |
| 2 | Hyperintense | Iso/hyperintense | Isointense | Fatty replacement |
| 3 | Hypointense | Hypointense | Hypointense | Sclerosis |

**Spondylolisthesis (Meyerding):**
- Grade I: 0-25% slip
- Grade II: 25-50% slip
- Grade III: 50-75% slip
- Grade IV: 75-100% slip
- Grade V: >100% (spondyloptosis)

### SEQUENCE INTERPRETATION GUIDE
- T2 Sagittal: PRIMARY — disc signal, CSF, cord signal, alignment
- T1 Sagittal: Vertebral body signal, fatty marrow, endplate Modic
- STIR/TIRM Sagittal: Edema, inflammation, acute fracture, Modic 1
- T2 Axial: Canal cross-section, foraminal detail, disc morphology
- T1 Post-contrast: Enhancement = active inflammation, tumor, infection
- DWI: Restricted diffusion = acute ischemia, abscess, highly cellular tumor

### NORMAL REFERENCE MEASUREMENTS
- Lumbar lordosis: 40-60 degrees
- Conus medullaris terminates: L1-L2 level (normal), below L2 = low-lying
- Thoracic cord diameter: 8-10mm AP
- Cervical cord diameter: 8-10mm AP at C3-C6
- Normal disc height: 8-12mm lumbar, 5-7mm cervical

### OUTPUT JSON SCHEMA
{
  "findings_by_level": {
    "L5-S1": {
      "disc": {"pfirrmann_grade": "III", "herniation_type": "protrusion",
               "herniation_direction": "paracentral_left", "height": "mildly reduced"},
      "canal": {"stenosis_grade": "moderate", "ap_diameter_mm": 9.2},
      "foramina": {"left": "grade_2", "right": "grade_1"},
      "endplates": {"superior": "modic_1", "inferior": "normal"},
      "facets": {"left": "mild hypertrophy", "right": "normal"}
    }
  },
  "alignment": {"lordosis": "maintained", "listhesis": null, "scoliosis": null},
  "cord_conus": {"signal": "normal", "conus_level": "L1", "compression": false},
  "paraspinal": {"findings": "no significant abnormality"},
  "incidentals": [],
  "impression": [
    "1. L5-S1 left paracentral disc protrusion with moderate central canal stenosis...",
    "2. ..."
  ],
  "confidence_summary": {
    "tier_a": ["L5-S1 moderate central stenosis (measured AP 9.2mm)"],
    "tier_b": ["L5-S1 Modic 1 endplate changes"],
    "tier_c": [],
    "tier_d": []
  }
}
"""
```

**Step 4: Build remaining anatomy prompts**
Each anatomy gets the same structure:
- Mandatory systematic checklist
- Grading criteria tables (BI-RADS for breast, PI-RADS for prostate, etc.)
- Normal reference measurements
- Sequence interpretation guide
- Output JSON schema

Priority order for prompt development (matching validation data availability):
1. Spine (SPIDER dataset available)
2. Brain (BraTS dataset available)
3. MSK/Knee (fastMRI dataset available)
4. Remaining 7 anatomies (TCIA collections)

**Step 5: Update claude_interpreter.py**
Replace inline prompt strings with imports from prompts/ directory:
```python
from backend.prompts.spine_master import SPINE_MASTER_PROMPT
from backend.prompts.brain_master import BRAIN_MASTER_PROMPT
# etc.

PROMPT_MAP = {
    "spine": SPINE_MASTER_PROMPT,
    "brain": BRAIN_MASTER_PROMPT,
    # ...
}
```

### Acceptance Criteria
- [ ] base_prompt.py with shared rules used by all anatomies
- [ ] spine_master.py with complete systematic checklist + grading tables
- [ ] brain_master.py with complete systematic checklist + grading tables
- [ ] msk_master.py with complete systematic checklist + grading tables
- [ ] All prompts enforce anti-hallucination rules
- [ ] All prompts define explicit JSON output schemas
- [ ] claude_interpreter.py imports from prompts/ directory

---

## MODULE 3: VerificationPass

### File: `backend/services/verification.py`

### Problem
Current pipeline: one Claude call, output goes directly to report. No review.
A single-pass system will always have overcalls (false positives from artifacts)
and missed findings.

### Solution
Second Claude call where it acts as a **senior attending radiologist** reviewing
the initial report against the same images. This catches:
- Contradictions (finding doesn't match the images)
- Overcalls (artifact interpreted as pathology)
- Missed anatomy (regions not addressed in the report)
- Grading errors (wrong Pfirrmann, wrong stenosis grade)
- Language imprecision (vague or ambiguous statements)

### Implementation Steps

**Step 1: Create VerificationPass class**
```
File: backend/services/verification.py

class VerificationPass:
    def __init__(self, client: anthropic.Anthropic):
        self.client = client

    async def verify(
        self,
        initial_report: dict,           # JSON from first Claude call
        image_content_blocks: list,      # Same images sent to first call
        measurements_json: dict,         # Same measurements
        anatomy_type: str
    ) -> VerifiedReport:
        """
        Send the initial report + all images to Claude as senior attending.
        Returns VerifiedReport with:
          - verified_findings: corrected/confirmed findings
          - corrections: list of what was changed and why
          - missed_findings: anything the first pass missed
          - quality_score: 0-100 self-assessed confidence
        """

    def _build_verification_prompt(self, anatomy_type: str) -> str:
        """Build the senior attending review prompt."""
```

**Step 2: Verification prompt template**
```
VERIFICATION_PROMPT = """
## ROLE
You are a senior attending radiologist with 20 years of experience.
A junior colleague has produced the report below. Your job is to
VERIFY every finding against the actual images.

## THE INITIAL REPORT
{initial_report_json}

## YOUR REVIEW CHECKLIST
For EACH finding in the report:
1. Can you see this finding in the images? (yes/no/uncertain)
2. Is the grading correct? (correct/upgrade/downgrade)
3. Is the anatomical location correct? (correct/incorrect)
4. Is the confidence tier appropriate? (correct/too high/too low)

Then check for MISSED findings:
5. Are there any abnormalities visible in the images NOT in the report?
6. Are all anatomical regions addressed? (check systematic checklist)
7. Are incidentals noted?

## QUALITY CONTROL
8. Are any findings contradicted by the measurements data?
9. Are measurement-based findings correctly calibrated?
10. Is the impression consistent with the detailed findings?

## OUTPUT
Return JSON:
{
    "verified_findings": { ... corrected version of findings ... },
    "corrections": [
        {"finding": "L4-L5 disc", "action": "downgraded", "reason": "Signal is Pfirrmann II not III"},
        {"finding": "L3-L4 foraminal stenosis", "action": "removed", "reason": "Not visible on axial images"}
    ],
    "missed_findings": [
        {"finding": "T12 compression fracture", "tier": "B", "reason": "Visible on sagittal T1, ~25% height loss"}
    ],
    "quality_score": 82,
    "quality_notes": "Good systematic coverage. Minor grading discrepancies corrected."
}
"""
```

**Step 3: Integrate into pipeline**
In `app.py`, after the initial Claude interpretation:
```python
# EXISTING: First pass interpretation
initial_result = await interpreter.interpret(...)

# NEW: Verification pass
verifier = VerificationPass(claude_client)
verified_result = await verifier.verify(
    initial_report=initial_result.raw_json,
    image_content_blocks=image_content_blocks,
    measurements_json=measurements_json,
    anatomy_type=job.anatomy_type
)

# Use verified_result for the final report
job.interpretation = verified_result.verified_findings
job.corrections = verified_result.corrections
job.quality_score = verified_result.quality_score
```

**Step 4: Merge logic**
When VerificationPass returns corrections:
- If a finding is confirmed: keep as-is
- If a finding is downgraded: update tier and language
- If a finding is removed: move to "reviewed and excluded" section
- If a new finding is added: include with verification tier
- The final report includes a "Quality Assurance" section noting the review

### Token Cost
- First pass: ~150K input tokens (images + prompt)
- Verification pass: ~160K input tokens (images + prompt + initial report)
- Total: ~310K tokens per study
- At Claude Opus pricing: ~$4-6 per study
- Acceptable for clinical-grade accuracy

### Acceptance Criteria
- [ ] VerificationPass class with verify() method
- [ ] Senior attending prompt with 10-point review checklist
- [ ] Corrections tracked with finding, action, and reason
- [ ] Missed findings caught and added to report
- [ ] Quality score (0-100) assigned per report
- [ ] Pipeline integration: first pass -> verification -> final report
- [ ] Final report includes "Quality Assurance" section

---

## MODULE 4: ValidationFramework

### Directory: `backend/validation/`

### Problem
We don't know our real accuracy. All estimates are guesses. Without ground truth
comparison, we can't know what's working, what's failing, or where to invest
improvement effort.

### Solution
Automated comparison of MIKA reports against annotated datasets with per-finding
metrics. This is the module that turns "we think it's 70%" into "we measured 78.3%
sensitivity for disc herniations and 45.2% for foraminal stenosis — fix foraminal
detection next."

### Implementation Steps

**Step 1: Ground truth datasets**
```
Available public datasets with annotations:

| Dataset | Anatomy | Studies | Annotations | Source |
|---------|---------|---------|-------------|--------|
| SPIDER  | Spine   | 447     | Disc grades, stenosis, Modic | spider.grand-challenge.org |
| BraTS   | Brain   | 2000+   | Tumor segmentation, grade | synapse.org/brats |
| fastMRI | Knee    | 1500+   | Meniscal tears, ligaments | fastmri.med.nyu.edu |
| TCIA    | Various | 10000+  | Varies by collection | cancerimagingarchive.net |

Start with: SPIDER (spine, 447 studies, detailed annotations)
```

**Step 2: Create validation directory structure**
```
backend/validation/
    __init__.py
    validator.py            # Core validation engine
    ground_truth.py         # Ground truth loader for each dataset
    metrics.py              # Sensitivity, specificity, accuracy calculations
    report_comparator.py    # Compare MIKA JSON output vs ground truth
    run_validation.py       # CLI script to run validation batches
```

**Step 3: Validator core class**
```
File: backend/validation/validator.py

class ValidationFramework:
    def __init__(self, dataset_path: Path, anatomy_type: str):
        self.dataset_path = dataset_path
        self.anatomy = anatomy_type
        self.ground_truth = GroundTruthLoader(dataset_path, anatomy_type)
        self.results = []

    async def validate_study(self, study_id: str) -> StudyValidation:
        """Run MIKA pipeline on one study, compare to ground truth.
        Returns per-finding comparison."""

    async def validate_batch(self, n_studies: int = 50) -> BatchValidation:
        """Run validation on N random studies from the dataset.
        Returns aggregate metrics."""

    def compute_metrics(self) -> ValidationMetrics:
        """Compute sensitivity, specificity, PPV, NPV, F1 per finding type."""

    def generate_report(self) -> str:
        """Human-readable validation report with tables and recommendations."""
```

**Step 4: Metrics calculator**
```
File: backend/validation/metrics.py

class MetricsCalculator:
    def per_finding_metrics(self, predictions: list, ground_truth: list) -> dict:
        """For each finding type (disc_herniation, stenosis, etc.):
        - True Positives: MIKA found it AND ground truth confirms
        - False Positives: MIKA found it BUT ground truth says normal
        - False Negatives: MIKA missed it BUT ground truth confirms
        - True Negatives: MIKA said normal AND ground truth confirms

        Returns:
        {
            "disc_herniation": {
                "sensitivity": 0.82,    # TP / (TP + FN)
                "specificity": 0.91,    # TN / (TN + FP)
                "ppv": 0.87,            # TP / (TP + FP)
                "npv": 0.88,            # TN / (TN + FN)
                "f1": 0.84,
                "accuracy": 0.87,
                "n_cases": 124
            },
            "canal_stenosis": { ... },
            "foraminal_stenosis": { ... },
            ...
        }"""

    def overall_accuracy(self, per_finding: dict) -> float:
        """Weighted average accuracy across all finding types."""

    def confusion_matrix(self, finding_type: str) -> dict:
        """TP, FP, FN, TN counts for one finding type."""
```

**Step 5: Report comparator**
```
File: backend/validation/report_comparator.py

class ReportComparator:
    def compare(self, mika_json: dict, ground_truth: dict, anatomy: str) -> Comparison:
        """Map MIKA output fields to ground truth annotation fields.

        For spine (SPIDER dataset):
        - mika.findings_by_level.L4-L5.disc.pfirrmann_grade vs gt.disc_grades.L4-L5
        - mika.findings_by_level.L4-L5.canal.stenosis_grade vs gt.canal_stenosis.L4-L5
        - mika.findings_by_level.L4-L5.disc.herniation_type vs gt.herniation.L4-L5

        Returns list of Finding comparisons:
        [
            Finding(type="disc_grade", level="L4-L5", predicted="III", actual="III", match=True),
            Finding(type="canal_stenosis", level="L4-L5", predicted="moderate", actual="mild", match=False),
            ...
        ]
        """
```

**Step 6: CLI validation runner**
```
File: backend/validation/run_validation.py

"""
Usage:
    python -m backend.validation.run_validation --dataset spider --n 50 --anatomy spine
    python -m backend.validation.run_validation --dataset brats --n 25 --anatomy brain

Output:
    Writes validation report to docs/validation_results/YYYY-MM-DD_spine_50.md
    with per-finding accuracy tables and recommendations for improvement.
"""
```

### Acceptance Criteria
- [ ] SPIDER dataset downloaded and ground truth loader working
- [ ] Per-finding metrics: sensitivity, specificity, PPV, NPV, F1
- [ ] Report comparator maps MIKA JSON to ground truth fields
- [ ] CLI runner processes N studies and generates report
- [ ] Validation report shows per-finding-type breakdown
- [ ] Report identifies weakest finding types for targeted improvement
- [ ] Automated: can re-run after any pipeline change to measure impact

---

## Execution Timeline

### Week 1: Pipeline Rewire + Spine Master Prompt
**Agent Tasks:**
1. Create `backend/services/batch_sender.py` with full BatchSender class
2. Create `backend/prompts/base_prompt.py` with shared rules
3. Create `backend/prompts/spine_master.py` with complete spine master prompt
4. Modify `backend/app.py` to use BatchSender instead of 4-image hardcoded block
5. Modify `backend/services/claude_interpreter.py` to accept content blocks
6. Test on 3 spine studies — verify Claude receives all images
7. Compare output quality: old pipeline (4 images) vs new (all images)

**Expected outcome:** Immediate accuracy jump from ~35% to ~55-65% just from
sending all images with better prompts.

### Week 2: Verification Pass + Validation Framework
**Agent Tasks:**
1. Create `backend/services/verification.py` with VerificationPass
2. Integrate verification into pipeline (app.py)
3. Create `backend/validation/` directory with all files
4. Download SPIDER dataset (447 spine studies)
5. Build ground truth loader for SPIDER format
6. Run first validation batch: 50 spine studies
7. Generate first real accuracy report

**Expected outcome:** First REAL accuracy number. Verification pass adds ~5-10%
accuracy by catching overcalls.

### Week 3: Measure and Iterate
**Agent Tasks:**
1. Read validation report — identify weakest finding types
2. If disc grading is weak: enhance Pfirrmann criteria in prompt
3. If stenosis is weak: add measurement calibration instructions
4. If foraminal stenosis is weak: add axial image emphasis
5. Re-run validation on same 50 studies — measure improvement
6. Create brain_master.py prompt (for brain validation next)

**Expected outcome:** Data-driven improvements push accuracy to ~75-80%.

### Week 4: Brain + MSK Expansion
**Agent Tasks:**
1. Download BraTS dataset for brain validation
2. Create brain_master.py with complete neuro checklist
3. Run brain validation batch
4. Download fastMRI for MSK validation
5. Create msk_master.py with complete MSK checklist
6. Run MSK validation batch

**Expected outcome:** 3 anatomy types with real accuracy numbers.

### Week 5-6: Targeted Optimization
**Agent Tasks:**
1. Based on validation data, identify specific failure modes
2. For each failure mode: is it a prompt issue, image quality issue, or Claude limitation?
3. Prompt issues: refine the specific section of the master prompt
4. Image quality issues: add VisionEnhancer (from 90% plan) for that specific finding
5. Claude limitations: add targeted measurement support
6. Re-validate after each change to confirm improvement
7. Build remaining anatomy prompts (cardiac, chest, abdomen, breast, vascular, head_neck, prostate)

**Expected outcome:** Spine accuracy approaches 85-90%. Other anatomies at 70-80%.

---

## Decision Points

After each validation run, make a data-driven decision:

```
IF accuracy >= 90% for an anatomy:
    DONE. Move to next anatomy.

IF accuracy 80-89%:
    Analyze failure modes. Likely prompt refinement only.
    DO NOT add new modules.

IF accuracy 70-79%:
    Analyze failure modes.
    IF most failures are "missed subtle findings":
        Add VisionEnhancer for that finding type (targeted, not global).
    IF most failures are "overcalls":
        Strengthen anti-hallucination rules in prompt.
    IF most failures are "grading disagreements":
        Add more explicit grading criteria + examples to prompt.

IF accuracy < 70%:
    Something is fundamentally wrong.
    Check: Are images being sent correctly?
    Check: Is the prompt being used?
    Check: Is the output being parsed correctly?
    Debug before optimizing.
```

---

## File Changes Summary

### New Files to Create
```
backend/services/batch_sender.py      # Module 1: Send all images to Claude
backend/prompts/__init__.py            # Module 2: Prompt package
backend/prompts/base_prompt.py         # Module 2: Shared rules
backend/prompts/spine_master.py        # Module 2: Spine master prompt
backend/prompts/brain_master.py        # Module 2: Brain master prompt
backend/prompts/msk_master.py          # Module 2: MSK master prompt
backend/prompts/cardiac_master.py      # Module 2: Cardiac master prompt
backend/prompts/chest_master.py        # Module 2: Chest master prompt
backend/prompts/abdomen_master.py      # Module 2: Abdomen master prompt
backend/prompts/breast_master.py       # Module 2: Breast master prompt
backend/prompts/vascular_master.py     # Module 2: Vascular master prompt
backend/prompts/head_neck_master.py    # Module 2: Head & neck master prompt
backend/prompts/prostate_master.py     # Module 2: Prostate master prompt
backend/services/verification.py       # Module 3: VerificationPass
backend/validation/__init__.py         # Module 4: Validation package
backend/validation/validator.py        # Module 4: Core validation engine
backend/validation/ground_truth.py     # Module 4: Ground truth loader
backend/validation/metrics.py          # Module 4: Metrics calculator
backend/validation/report_comparator.py # Module 4: Report comparator
backend/validation/run_validation.py   # Module 4: CLI validation runner
```

### Existing Files to Modify
```
backend/app.py                         # Replace 4-image block with BatchSender
backend/services/claude_interpreter.py # Accept content blocks, import master prompts
```

### Files NOT Modified (kept as-is)
```
backend/core/dicom_engine.py           # Still does inventory, measurements, annotations
backend/core/format_converter.py       # Still handles multi-format input
frontend/index.html                    # No frontend changes needed
```

---

## Success Metrics

| Metric | Current | Week 2 Target | Week 4 Target | Final Target |
|--------|---------|---------------|---------------|--------------|
| Spine sensitivity | ~35% | 65% | 80% | 90% |
| Spine specificity | ~50% | 75% | 85% | 90% |
| Brain sensitivity | ~25% | - | 70% | 85% |
| MSK sensitivity | ~25% | - | 70% | 85% |
| Images sent to Claude | 4 | 40-80 | 40-80 | 40-80 |
| Verified reports | 0% | 100% | 100% | 100% |
| Real accuracy known | No | Yes (spine) | Yes (3 types) | Yes (all) |

---

## Cost Analysis

| Component | Tokens per Study | Cost per Study |
|-----------|-----------------|----------------|
| First pass (all images + master prompt) | ~150K input + ~4K output | ~$2.50 |
| Verification pass (images + report + review) | ~160K input + ~2K output | ~$2.70 |
| **Total per study** | **~316K tokens** | **~$5.20** |

For validation runs:
- 50 studies = ~$260
- 447 studies (full SPIDER) = ~$2,325

This is investment in quality. Once we reach 90%, each study costs $5.20 for
radiologist-quality interpretation.

---

## Key Principle

**Build 4 things. Measure. Then decide what's next based on data.**

Do NOT pre-build VisionEnhancer, AnnotationEngine, ReportGenerator, or
StudyOrganizer until validation data proves they're needed. The 90% accuracy
plan (docs/90_PERCENT_ACCURACY_PLAN.md) has complete code for all 8 modules
if we need them later — but we probably won't need most of them.

The fastest path to 90% is:
1. Let Claude see everything (BatchSender)
2. Tell Claude exactly how to analyze it (MasterPrompts)
3. Have Claude check its own work (VerificationPass)
4. Measure ruthlessly (ValidationFramework)
5. Fix only what the data says is broken

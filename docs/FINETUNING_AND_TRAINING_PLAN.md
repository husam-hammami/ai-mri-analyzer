# MIKA Fine-Tuning & Training Plan

> **Version:** 1.0
> **Date:** March 10, 2026
> **Status:** Implementation-ready
> **Estimated Total Duration:** 6-8 weeks
> **Estimated Total Cost:** $500-2,000 (cloud GPU + dataset access)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Overview](#2-architecture-overview)
3. [Phase 1: Data Collection & Preparation](#3-phase-1-data-collection--preparation)
4. [Phase 2: RAG System (Quick Win)](#4-phase-2-rag-system-quick-win)
5. [Phase 3: Model Selection & Fine-Tuning](#5-phase-3-model-selection--fine-tuning)
6. [Phase 4: Training Pipeline](#6-phase-4-training-pipeline)
7. [Phase 5: Evaluation & Benchmarking](#7-phase-5-evaluation--benchmarking)
8. [Phase 6: Integration with MIKA](#8-phase-6-integration-with-mika)
9. [Phase 7: Deployment & Serving](#9-phase-7-deployment--serving)
10. [Infrastructure & Cost Estimates](#10-infrastructure--cost-estimates)
11. [File Structure](#11-file-structure)
12. [Implementation Checklist](#12-implementation-checklist)

---

## 1. Executive Summary

### Goal

Build a custom MRI interpretation model that can replace or complement Claude Opus in MIKA's pipeline, reducing per-query API costs by 90%+ while maintaining diagnostic accuracy across Spine, Brain, and MSK anatomies.

### Strategy: Three-Phase Approach

| Phase | What | Timeline | Cost | Impact |
|-------|------|----------|------|--------|
| **Phase 2: RAG** | Add radiology knowledge retrieval to Claude prompts | Week 1-2 | ~$50 | Better accuracy, same API costs |
| **Phase 3-4: Fine-Tune** | Train MedGemma 4B on MRI instruction data | Week 3-6 | ~$200-500 | 90% API cost reduction |
| **Phase 5-7: Deploy** | Serve fine-tuned model, benchmark, integrate | Week 6-8 | ~$100-500 | Production-ready hybrid system |

### Target Architecture

```
DICOM Upload
    |
    v
DICOMEngine (existing, unchanged)
    |-- Measurements JSON (spine quantitative data)
    |-- Key Images (up to 4 annotated PNGs)
    |
    v
+---------------------------+     +---------------------------+
| Fine-Tuned MedGemma 4B   |     | Claude Opus 4.6           |
| (Primary Interpreter)     |     | (Fallback / Validator)    |
|                           |     |                           |
| - Local or cloud GPU      |     | - API call only when:     |
| - < 5 second inference    |     |   * Confidence < threshold|
| - $0 per query            |     |   * Complex/ambiguous case|
| - Same JSON output schema |     |   * User requests 2nd     |
+---------------------------+     |     opinion               |
    |                             +---------------------------+
    v                                 |
    +-------- Report JSON <-----------+
```

---

## 2. Architecture Overview

### Current MIKA Pipeline (What We're Extending)

```
backend/
├── app.py                         # FastAPI + pipeline orchestration
│   ├── POST /api/upload           # DICOM file upload
│   ├── POST /api/analyze          # Starts async pipeline
│   ├── GET  /api/status/{id}      # Poll progress
│   ├── GET  /api/report/{id}      # Get JSON report
│   └── _run_analysis_pipeline()   # 5-phase orchestrator
│
├── core/
│   └── dicom_engine.py            # Layer 1: Deterministic measurements
│       ├── run_inventory()        # DICOM cataloging + anatomy detection
│       ├── identify_levels()      # Sacrum-up vertebral counting
│       ├── measure_all_discs()    # Quantitative disc/canal measurements
│       ├── assess_endplates()     # Modic classification
│       └── create_*()             # Annotation image generation
│
└── services/
    └── claude_interpreter.py      # Layer 2: AI interpretation
        ├── SPINE_SYSTEM_PROMPT    # Anatomy-specific prompts
        ├── BRAIN_SYSTEM_PROMPT
        ├── MSK_SYSTEM_PROMPT
        ├── GENERIC_SYSTEM_PROMPT
        └── interpret()            # Claude API call
```

### Key Integration Points

The fine-tuned model must accept the same input and produce the same output as `ClaudeInterpreter`:

**Input (`InterpretationRequest`):**
```python
@dataclass
class InterpretationRequest:
    measurements_json: dict              # Quantitative data from DICOMEngine
    key_images_b64: dict                 # {label: base64_png} (max 4 images)
    clinical_history: Optional[str]
    surgical_notes: Optional[str]
    prior_reports: Optional[str]
    anatomy_type: str                    # "spine" | "brain" | "msk" | "unknown"
```

**Output (`ClinicalInterpretation`):**
```python
@dataclass
class ClinicalInterpretation:
    anatomy_type: str
    findings_by_level: dict              # Spine: {level: {disc, canal, foramina, endplates, facets}}
    findings_by_region: dict             # Brain: {cerebral_hemispheres, white_matter, ventricles, ...}
    findings_by_structure: dict          # MSK: {osseous, cartilage, ligaments, tendons, ...}
    alignment: str
    conus: str
    enhancement_pattern: str
    diffusion_findings: str
    joint_effusion: str
    bone_marrow: str
    identified_anatomy: str
    incidentals: str
    impression: list[str]
    confidence_summary: dict             # {tier_a: [], tier_b: [], tier_c: [], tier_d: []}
    raw_response: str
    model_used: str
    input_tokens: int
    output_tokens: int
```

**Confidence Tier System (must be preserved in fine-tuned model):**

| Tier | Criteria | Language |
|------|----------|----------|
| A | DICOM-calibrated measurement, confirmed on 2+ sequences | "There is..." |
| B | Single-sequence finding, visual-only assessment | "There likely is..." |
| C | Suggestive, could be artifact, uncalibrated max | "Possible... recommend correlation" |
| D | Insufficient data | "Cannot be reliably assessed" |

---

## 3. Phase 1: Data Collection & Preparation

### 3.1 Target Datasets

#### Priority 1: Spine (MIKA's strongest anatomy)

| Dataset | Size | Content | Access | URL |
|---------|------|---------|--------|-----|
| **SPIDER** | 447 studies (T1+T2 sag) | Lumbar segmentation masks for vertebrae, discs, canal | Free (CC BY 4.0) | https://huggingface.co/datasets/cdoswald/SPIDER |
| **RSNA Lumbar Spine** | Competition dataset | Degenerative conditions with labels | Free | https://imaging.rsna.org/dataset/6 |
| **TCIA Spine Collections** | 1,246 subjects (9 datasets) | Multi-sequence spine with pathology | Free (registration) | https://www.cancerimagingarchive.net/ |
| **Lumbosacral Open-Access** | 14 subjects (CISS, DESS, T2-TSE) | High-res nerve root anatomy | Free | https://www.nature.com/articles/s41597-024-03919-4 |
| **Lumbar Spine Mendeley** | Annotated lumbar MRI | Various pathologies | Free (CC BY) | https://data.mendeley.com/datasets/k57fr854j2/2 |

#### Priority 2: Brain (Most available data)

| Dataset | Size | Content | Access | URL |
|---------|------|---------|--------|-----|
| **BraTS 2024/2025** | 2,000+ cases | Glioma T1/T1-Gd/T2/FLAIR with segmentation | Free (Synapse registration) | https://www.synapse.org/Synapse:syn64153130 |
| **fastMRI Brain** | 6,970 studies | T1, T2, FLAIR from 3T/1.5T | Free (NYU agreement) | https://fastmri.med.nyu.edu/ |
| **IXI Dataset** | 600 normal subjects | T1, T2, PD, MRA, DTI | Free (CC BY-SA 3.0) | https://brain-development.org/ixi-dataset/ |
| **UK Biobank Brain** | 100,000+ subjects | T1, T2-FLAIR, fMRI, DWI, SWI | Application required | https://www.ukbiobank.ac.uk/ |

#### Priority 3: Musculoskeletal

| Dataset | Size | Content | Access | URL |
|---------|------|---------|--------|-----|
| **fastMRI Knee** | Large dataset | Knee MRI with radiologist annotations | Free (NYU agreement) | https://fastmri.med.nyu.edu/ |
| **TCIA MSK Collections** | 4 knee-focused datasets | Various MSK pathology | Free (registration) | https://www.cancerimagingarchive.net/ |

#### Multi-Anatomy (for pre-training)

| Dataset | Size | Content | Access | URL |
|---------|------|---------|--------|-----|
| **RadMD** | 16M+ 2D/3D scans | Multi-modal radiology with text | Free | https://huggingface.co/datasets/chaoyi-wu/RadFM_data_csv |
| **PMC-15M** | 15M figure-caption pairs | Biomedical literature images | Free | Used by BiomedCLIP/LLaVA-Med |

### 3.2 Data Download Script

Create `training/scripts/download_datasets.py`:

```python
"""
Dataset Download Manager for MIKA Training Pipeline
Downloads and organizes MRI datasets from public sources.
"""
import os
import requests
from pathlib import Path
from huggingface_hub import snapshot_download
from tcia_utils import nbia

# Base directories
DATA_ROOT = Path("training/data/raw")
SPIDER_DIR = DATA_ROOT / "spider"
BRATS_DIR = DATA_ROOT / "brats"
IXI_DIR = DATA_ROOT / "ixi"
FASTMRI_DIR = DATA_ROOT / "fastmri"
TCIA_DIR = DATA_ROOT / "tcia"


def download_spider():
    """Download SPIDER lumbar spine dataset from HuggingFace."""
    print("[1/5] Downloading SPIDER dataset...")
    snapshot_download(
        repo_id="cdoswald/SPIDER",
        repo_type="dataset",
        local_dir=str(SPIDER_DIR),
        allow_patterns=["*.mha", "*.json", "*.csv"],
    )
    print(f"  -> SPIDER saved to {SPIDER_DIR}")


def download_ixi():
    """Download IXI brain MRI dataset (T1, T2)."""
    print("[2/5] Downloading IXI dataset...")
    IXI_DIR.mkdir(parents=True, exist_ok=True)
    base_url = "http://biomedic.doc.ic.ac.uk/brain-development/downloads/IXI"
    for modality in ["IXI-T1", "IXI-T2"]:
        url = f"{base_url}/{modality}.tar"
        out_path = IXI_DIR / f"{modality}.tar"
        if not out_path.exists():
            print(f"  Downloading {modality}...")
            resp = requests.get(url, stream=True)
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
    print(f"  -> IXI saved to {IXI_DIR}")


def download_tcia_collections():
    """Download selected TCIA collections for spine and MSK."""
    print("[3/5] Downloading TCIA collections...")
    TCIA_DIR.mkdir(parents=True, exist_ok=True)

    collections = [
        # Spine
        {"collection": "UPENN-GBM", "modality": "MR", "body_part": "BRAIN", "limit": 100},
        {"collection": "Vestibular-Schwannoma-SEG", "modality": "MR", "limit": 50},
        {"collection": "Soft-tissue-Sarcoma", "modality": "MR", "limit": 50},
    ]

    for coll in collections:
        name = coll["collection"]
        out_dir = TCIA_DIR / name
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Downloading {name}...")
        try:
            series = nbia.getSeries(collection=name, modality=coll.get("modality", "MR"))
            if series:
                series_uids = [s["SeriesInstanceUID"] for s in series[:coll.get("limit", 50)]]
                nbia.downloadSeries(series_uids, path=str(out_dir))
        except Exception as e:
            print(f"  WARNING: Could not download {name}: {e}")

    print(f"  -> TCIA saved to {TCIA_DIR}")


def download_brats():
    """
    BraTS requires Synapse registration.
    Prints instructions for manual download.
    """
    print("[4/5] BraTS dataset requires manual download:")
    print("  1. Register at https://www.synapse.org/Synapse:syn64153130")
    print("  2. Accept data use agreement")
    print("  3. Download and extract to:", BRATS_DIR)
    print("  4. Expected structure: brats/{subject_id}/{T1,T1ce,T2,FLAIR}.nii.gz")
    BRATS_DIR.mkdir(parents=True, exist_ok=True)


def download_fastmri():
    """
    fastMRI requires NYU Data Sharing Agreement.
    Prints instructions for manual download.
    """
    print("[5/5] fastMRI dataset requires NYU agreement:")
    print("  1. Apply at https://fastmri.med.nyu.edu/")
    print("  2. Download brain and knee subsets")
    print("  3. Extract to:", FASTMRI_DIR)
    FASTMRI_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    download_spider()
    download_ixi()
    download_tcia_collections()
    download_brats()
    download_fastmri()
    print("\n=== Download complete ===")
    print(f"Total data root: {DATA_ROOT}")
```

### 3.3 Data Preparation Pipeline

Create `training/scripts/prepare_dataset.py`:

```python
"""
Converts raw MRI datasets into instruction-tuning format for VLM fine-tuning.

Output format (JSONL):
{
    "id": "spider_001_sag_t2",
    "image": ["path/to/slice_001.png", "path/to/slice_010.png"],
    "anatomy_type": "spine",
    "conversations": [
        {
            "from": "human",
            "value": "<image>\n<image>\n## Pre-Computed Measurements\n```json\n{...}\n```\n\nAnalyze this lumbar spine MRI..."
        },
        {
            "from": "gpt",
            "value": "{\"findings_by_level\": {...}, \"impression\": [...], ...}"
        }
    ]
}
"""
import json
import pydicom
import numpy as np
from pathlib import Path
from PIL import Image
from dataclasses import dataclass, asdict


@dataclass
class TrainingSample:
    id: str
    image_paths: list[str]
    anatomy_type: str
    measurements_json: dict
    clinical_history: str
    ground_truth_report: str  # Structured JSON from radiologist or generated


# ─── Stage 1: DICOM → PNG + Metadata ───────────────────────────────────

def dicom_to_png(dicom_dir: Path, output_dir: Path) -> dict:
    """
    Convert a DICOM series to PNG slices with extracted metadata.
    Returns metadata dict with calibration, sequence info, demographics.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "pixel_spacing": None,
        "slice_thickness": None,
        "series_description": "",
        "body_part": "",
        "slices": [],
    }

    dcm_files = sorted(dicom_dir.glob("*.dcm"))
    for i, dcm_path in enumerate(dcm_files):
        ds = pydicom.dcmread(str(dcm_path))

        # Extract metadata from first file
        if i == 0:
            ps = getattr(ds, "PixelSpacing", None)
            metadata["pixel_spacing"] = [float(ps[0]), float(ps[1])] if ps else None
            metadata["slice_thickness"] = float(getattr(ds, "SliceThickness", 0))
            metadata["series_description"] = str(getattr(ds, "SeriesDescription", ""))
            metadata["body_part"] = str(getattr(ds, "BodyPartExamined", ""))

        # Normalize pixel data to 8-bit PNG
        arr = ds.pixel_array.astype(np.float32)
        if hasattr(ds, "WindowCenter") and hasattr(ds, "WindowWidth"):
            wc = float(ds.WindowCenter) if not isinstance(ds.WindowCenter, pydicom.multival.MultiValue) else float(ds.WindowCenter[0])
            ww = float(ds.WindowWidth) if not isinstance(ds.WindowWidth, pydicom.multival.MultiValue) else float(ds.WindowWidth[0])
            arr = np.clip((arr - (wc - ww / 2)) / ww * 255, 0, 255)
        else:
            arr = ((arr - arr.min()) / max(arr.max() - arr.min(), 1) * 255)

        img_num = int(getattr(ds, "InstanceNumber", i + 1))
        out_path = output_dir / f"slice_{img_num:03d}.png"
        Image.fromarray(arr.astype(np.uint8)).save(str(out_path))
        metadata["slices"].append(str(out_path))

    return metadata


# ─── Stage 2: Generate Instruction-Tuning Pairs ────────────────────────

def create_instruction_pair(
    sample: TrainingSample,
    system_prompt_template: str,
) -> dict:
    """
    Create a single instruction-tuning conversation pair
    in the LLaVA/MedGemma training format.
    """
    # Build the human message (mirrors MIKA's ClaudeInterpreter format)
    image_tags = "\n".join(["<image>"] * len(sample.image_paths))

    calibration = "DICOM-calibrated" if sample.measurements_json.get("calibration_status") else "Visual-only"

    human_message = f"""{image_tags}
## Pre-Computed Measurements (Calibration: {calibration})
```json
{json.dumps(sample.measurements_json, indent=2)}
```

## Clinical History
{sample.clinical_history or "No clinical history provided."}

## Task
Analyze the provided MRI images and measurements. Produce clinical findings with [Tier X] confidence tags.
Return valid JSON matching the {sample.anatomy_type} output schema."""

    return {
        "id": sample.id,
        "image": sample.image_paths,
        "conversations": [
            {"from": "human", "value": human_message},
            {"from": "gpt", "value": sample.ground_truth_report},
        ],
    }


# ─── Stage 3: Report Generation via Claude ──────────────────────────────

def generate_ground_truth_with_claude(
    image_paths: list[str],
    measurements: dict,
    anatomy_type: str,
    api_key: str,
) -> str:
    """
    Use Claude Opus to generate ground-truth structured reports
    for training data. This bootstraps the training dataset using
    MIKA's existing interpreter on real MRI data.

    NOTE: This is a one-time data generation step, not inference.
    The goal is to create (image, report) pairs for fine-tuning.
    """
    import anthropic
    import base64

    client = anthropic.Anthropic(api_key=api_key)

    # Reuse MIKA's existing prompt system
    from services.claude_interpreter import get_system_prompt

    content_blocks = []

    # Add measurements
    content_blocks.append({
        "type": "text",
        "text": f"## Pre-Computed Measurements\n```json\n{json.dumps(measurements, indent=2)}\n```",
    })

    # Add images (max 4)
    for img_path in image_paths[:4]:
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content_blocks.append({"type": "text", "text": f"\n### Image: {Path(img_path).stem}\n"})
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        })

    content_blocks.append({
        "type": "text",
        "text": "Analyze and produce JSON with [Tier X] confidence tags.",
    })

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        system=get_system_prompt(anatomy_type),
        messages=[{"role": "user", "content": content_blocks}],
    )

    return response.content[0].text


# ─── Stage 4: Dataset Assembly ──────────────────────────────────────────

def assemble_training_dataset(
    raw_data_dir: Path,
    output_path: Path,
    anatomy_type: str,
    api_key: str = None,
    use_claude_for_reports: bool = True,
    max_samples: int = None,
):
    """
    Full pipeline: raw MRI data -> instruction-tuning JSONL.

    Steps:
    1. Convert DICOM to PNG
    2. Extract measurements (if spine, run DICOMEngine)
    3. Generate structured reports (via Claude or existing labels)
    4. Format as instruction-tuning pairs
    5. Write to JSONL
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    samples = []
    subject_dirs = sorted(raw_data_dir.iterdir())

    if max_samples:
        subject_dirs = subject_dirs[:max_samples]

    for i, subject_dir in enumerate(subject_dirs):
        if not subject_dir.is_dir():
            continue

        print(f"  [{i+1}/{len(subject_dirs)}] Processing {subject_dir.name}...")

        # Step 1: Convert to PNG
        png_dir = raw_data_dir.parent / "processed" / anatomy_type / subject_dir.name
        metadata = dicom_to_png(subject_dir, png_dir)

        if not metadata["slices"]:
            continue

        # Step 2: Select key slices (mid-volume + quartiles)
        n_slices = len(metadata["slices"])
        key_indices = [
            n_slices // 4,
            n_slices // 2,
            3 * n_slices // 4,
        ]
        key_images = [metadata["slices"][i] for i in key_indices if i < n_slices]

        # Step 3: Build measurements dict
        measurements = {
            "demographics": {"body_part_examined": metadata["body_part"]},
            "detected_anatomy": anatomy_type,
            "calibration_status": "DICOM-calibrated" if metadata["pixel_spacing"] else "UNCALIBRATED",
            "pixel_spacing_mm": metadata["pixel_spacing"],
            "slice_thickness_mm": metadata["slice_thickness"],
            "series_description": metadata["series_description"],
            "num_slices": n_slices,
        }

        # Step 4: Generate report
        if use_claude_for_reports and api_key:
            report = generate_ground_truth_with_claude(
                key_images, measurements, anatomy_type, api_key
            )
        else:
            report = json.dumps({
                "findings_by_region": {"note": "Label pending manual annotation"},
                "impression": ["Unlabeled sample"],
                "confidence_summary": {"tier_d": ["All findings"]},
            })

        # Step 5: Create training sample
        sample = TrainingSample(
            id=f"{anatomy_type}_{subject_dir.name}",
            image_paths=key_images,
            anatomy_type=anatomy_type,
            measurements_json=measurements,
            clinical_history="",
            ground_truth_report=report,
        )

        pair = create_instruction_pair(sample, "")
        samples.append(pair)

    # Write JSONL
    with open(output_path, "w") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")

    print(f"  -> Wrote {len(samples)} samples to {output_path}")
    return len(samples)
```

### 3.4 Data Augmentation

Create `training/scripts/augment_data.py`:

```python
"""
MRI-safe data augmentation strategies.
Only applies augmentations that preserve diagnostic features.
"""
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, map_coordinates
import random


def augment_mri_slice(img_array: np.ndarray, seed: int = None) -> np.ndarray:
    """
    Apply random MRI-safe augmentations to a single slice.

    Safe augmentations (preserve diagnostic features):
    - Small rotation (max 10 degrees)
    - Horizontal flip (for bilateral anatomy only)
    - Brightness/contrast jitter (small range)
    - Gaussian noise (low sigma)
    - Elastic deformation (subtle)
    - Bias field simulation

    UNSAFE (never apply):
    - Aggressive color jitter (changes signal characteristics)
    - Vertical flip (anatomically incorrect)
    - Large rotations (> 15 degrees)
    - Cutout/erasing (removes diagnostic regions)
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    arr = img_array.astype(np.float32).copy()

    # 1. Small rotation (50% chance, max +/- 10 degrees)
    if random.random() < 0.5:
        angle = random.uniform(-10, 10)
        from scipy.ndimage import rotate
        arr = rotate(arr, angle, reshape=False, order=1, mode="constant", cval=0)

    # 2. Brightness jitter (60% chance, +/- 10%)
    if random.random() < 0.6:
        factor = random.uniform(0.9, 1.1)
        arr = arr * factor

    # 3. Contrast jitter (40% chance, +/- 15%)
    if random.random() < 0.4:
        mean_val = arr.mean()
        factor = random.uniform(0.85, 1.15)
        arr = (arr - mean_val) * factor + mean_val

    # 4. Gaussian noise (50% chance, low sigma)
    if random.random() < 0.5:
        sigma = random.uniform(1, 5)
        noise = np.random.normal(0, sigma, arr.shape)
        arr = arr + noise

    # 5. Bias field simulation (30% chance)
    if random.random() < 0.3:
        h, w = arr.shape[:2]
        x = np.linspace(-1, 1, w)
        y = np.linspace(-1, 1, h)
        xx, yy = np.meshgrid(x, y)
        # Random low-frequency bias field
        bias = 1 + 0.1 * (
            random.uniform(-1, 1) * xx
            + random.uniform(-1, 1) * yy
            + random.uniform(-0.5, 0.5) * xx * yy
        )
        if arr.ndim == 2:
            arr = arr * bias
        else:
            arr = arr * bias[:, :, np.newaxis]

    # 6. Elastic deformation (20% chance, subtle)
    if random.random() < 0.2:
        arr = _elastic_deformation(arr, alpha=15, sigma=3)

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return arr


def _elastic_deformation(image, alpha=15, sigma=3):
    """Subtle elastic deformation for MRI augmentation."""
    shape = image.shape[:2]
    dx = gaussian_filter(np.random.randn(*shape) * alpha, sigma)
    dy = gaussian_filter(np.random.randn(*shape) * alpha, sigma)

    y, x = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), indexing="ij")
    indices = [np.clip(y + dy, 0, shape[0] - 1), np.clip(x + dx, 0, shape[1] - 1)]

    if image.ndim == 2:
        return map_coordinates(image, indices, order=1, mode="reflect")
    else:
        channels = []
        for c in range(image.shape[2]):
            channels.append(map_coordinates(image[:, :, c], indices, order=1, mode="reflect"))
        return np.stack(channels, axis=-1)
```

### 3.5 Data Volume Targets

| Phase | Pairs Needed | Source | Method |
|-------|-------------|--------|--------|
| **Proof of concept** | 1,000-5,000 | SPIDER + IXI + TCIA | Claude-generated reports |
| **Minimum viable** | 10,000-50,000 | Above + fastMRI + BraTS | Claude + existing labels |
| **Production quality** | 50,000-120,000 | All datasets combined | Claude + manual review |

**Important: Data distribution targets:**
- 40% Spine (MIKA's primary strength)
- 30% Brain (most available data)
- 20% MSK (knee, shoulder, hip)
- 10% Other/Generic (abdomen, cardiac, etc.)

---

## 4. Phase 2: RAG System (Quick Win)

### 4.1 Overview

Before fine-tuning, add a Retrieval-Augmented Generation (RAG) layer to MIKA's existing Claude pipeline. This improves accuracy immediately at minimal cost.

### 4.2 Architecture

```
User uploads MRI
        |
        v
DICOMEngine (unchanged)
        |
        v
+--- RAG Retrieval (NEW) ---+
| 1. Detect anatomy type     |
| 2. Extract key features    |
|    (measurements, findings) |
| 3. Query vector DB for     |
|    similar cases + relevant |
|    radiology literature     |
| 4. Inject top-5 references |
|    into Claude prompt       |
+----------------------------+
        |
        v
Claude Opus (existing, enhanced prompt)
        |
        v
Report JSON
```

### 4.3 Implementation

Create `backend/services/rag_engine.py`:

```python
"""
RAG engine for enhancing Claude's MRI interpretation
with relevant radiology knowledge and similar cases.
"""
import json
import numpy as np
from pathlib import Path
from dataclasses import dataclass

# Use a lightweight embedding model
# Options: sentence-transformers, OpenAI ada-002, or Anthropic voyage
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # 384-dim, fast, free


@dataclass
class RAGDocument:
    id: str
    text: str
    metadata: dict           # {anatomy, pathology, source, ...}
    embedding: np.ndarray


class RAGEngine:
    def __init__(self, index_path: str = "backend/data/rag_index"):
        self.index_path = Path(index_path)
        self.documents: list[RAGDocument] = []
        self._embedder = None

    @property
    def embedder(self):
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(EMBEDDING_MODEL)
        return self._embedder

    def build_index(self, knowledge_dir: str):
        """
        Build vector index from radiology knowledge base.

        Sources to index:
        1. Radiology teaching cases (structured findings + diagnosis)
        2. ACR Appropriateness Criteria excerpts
        3. Anatomy-specific assessment protocols
        4. Common pathology descriptions with typical MRI appearances
        """
        knowledge_path = Path(knowledge_dir)
        for json_file in knowledge_path.glob("**/*.json"):
            with open(json_file) as f:
                docs = json.load(f)
            for doc in docs:
                embedding = self.embedder.encode(doc["text"])
                self.documents.append(RAGDocument(
                    id=doc["id"],
                    text=doc["text"],
                    metadata=doc.get("metadata", {}),
                    embedding=embedding,
                ))
        self._save_index()

    def retrieve(self, query: str, anatomy_type: str, top_k: int = 5) -> list[dict]:
        """
        Retrieve most relevant documents for a given query + anatomy.
        """
        query_embedding = self.embedder.encode(query)

        # Filter by anatomy type first
        candidates = [
            d for d in self.documents
            if d.metadata.get("anatomy", "any") in (anatomy_type, "any")
        ]

        # Cosine similarity
        scores = []
        for doc in candidates:
            sim = np.dot(query_embedding, doc.embedding) / (
                np.linalg.norm(query_embedding) * np.linalg.norm(doc.embedding)
            )
            scores.append((sim, doc))

        scores.sort(key=lambda x: x[0], reverse=True)
        return [
            {"text": doc.text, "score": float(sim), "metadata": doc.metadata}
            for sim, doc in scores[:top_k]
        ]

    def format_context(self, results: list[dict]) -> str:
        """Format retrieved documents for injection into Claude prompt."""
        if not results:
            return ""
        lines = ["\n## Reference Knowledge (Retrieved from Radiology Literature)\n"]
        for i, r in enumerate(results, 1):
            source = r["metadata"].get("source", "Unknown")
            lines.append(f"### Reference {i} (Source: {source}, Relevance: {r['score']:.2f})")
            lines.append(r["text"])
            lines.append("")
        return "\n".join(lines)

    def _save_index(self):
        self.index_path.mkdir(parents=True, exist_ok=True)
        # Save embeddings and metadata for fast reload
        np.save(
            str(self.index_path / "embeddings.npy"),
            np.array([d.embedding for d in self.documents]),
        )
        with open(self.index_path / "documents.json", "w") as f:
            json.dump(
                [{"id": d.id, "text": d.text, "metadata": d.metadata} for d in self.documents],
                f,
            )
```

### 4.4 Knowledge Base Content to Index

Create `training/knowledge_base/` with:

1. **`spine_pathology.json`** - Disc desiccation grades, stenosis classification (Schizas), Modic typing criteria, spondylolisthesis grading
2. **`brain_pathology.json`** - Tumor grading (WHO), white matter disease patterns, stroke imaging criteria, hemorrhage classification
3. **`msk_pathology.json`** - Meniscal tear classification, ligament grading, cartilage scoring (Outerbridge), bone marrow edema patterns
4. **`assessment_protocols.json`** - Systematic assessment checklists per anatomy type
5. **`teaching_cases.json`** - Classic presentation patterns with expected findings

---

## 5. Phase 3: Model Selection & Fine-Tuning

### 5.1 Primary Model: MedGemma 4B

**Why MedGemma 4B:**
- Proven on brain MRI (accuracy: 33% -> 89% after LoRA fine-tuning)
- Single-GPU training (RTX 4090 or A100)
- Open weights (research + commercial use)
- Multi-image support (inherits from Gemma 3)
- Smallest model with medical pre-training
- Well-documented fine-tuning tutorials exist

**HuggingFace:** `google/medgemma-4b-it`

### 5.2 Fallback Model: RadFM

**Why RadFM as fallback:**
- Native 3D volume support (critical for MRI sequences)
- Trained on 180K 3D radiology scans
- Multi-image input natively
- Better for cases where slice context matters

**HuggingFace:** `chaoyi-wu/RadFM`

### 5.3 Model Comparison

| Feature | MedGemma 4B | RadFM | LLaVA-Med 7B |
|---------|------------|-------|--------------|
| Parameters | 4B | ~7B | 7B |
| Multi-image | Yes | Yes (native 3D) | Limited |
| MRI pre-training | Yes (fine-tuned) | Yes (180K 3D scans) | Partial |
| LoRA VRAM | 24GB (1x 4090) | 80GB (1x A100) | 80GB (1x A100) |
| QLoRA VRAM | 16GB (1x 3090) | 80GB (1x A100) | 24GB (1x 4090) |
| License | Open | Research | Research |
| Fine-tune ease | Easy | Moderate | Easy |
| **Priority** | **#1** | **#2** | **#3** |

### 5.4 Fine-Tuning Configuration

**MedGemma 4B with QLoRA:**

Create `training/configs/medgemma_qlora.yaml`:

```yaml
# MedGemma 4B QLoRA Fine-Tuning Configuration
# Estimated: 4-8 hours on 1x A100 80GB with 10K samples

model:
  name: "google/medgemma-4b-it"
  dtype: "bfloat16"
  load_in_4bit: true
  bnb_4bit_compute_dtype: "bfloat16"
  bnb_4bit_quant_type: "nf4"
  use_double_quant: true

lora:
  r: 64                            # LoRA rank (higher = more capacity)
  lora_alpha: 128                  # Scaling factor
  lora_dropout: 0.05
  target_modules:
    - "q_proj"
    - "k_proj"
    - "v_proj"
    - "o_proj"
    - "gate_proj"
    - "up_proj"
    - "down_proj"
  bias: "none"
  task_type: "CAUSAL_LM"

training:
  output_dir: "training/checkpoints/medgemma-4b-mika"
  num_train_epochs: 3
  per_device_train_batch_size: 2
  per_device_eval_batch_size: 2
  gradient_accumulation_steps: 8   # Effective batch = 16
  learning_rate: 2.0e-4
  weight_decay: 0.01
  warmup_ratio: 0.03
  lr_scheduler_type: "cosine"
  logging_steps: 10
  save_steps: 500
  eval_steps: 500
  save_total_limit: 3
  fp16: false
  bf16: true
  max_grad_norm: 0.3
  group_by_length: true
  dataloader_num_workers: 4
  remove_unused_columns: false

data:
  train_file: "training/data/instruction_tuning/train.jsonl"
  eval_file: "training/data/instruction_tuning/eval.jsonl"
  max_seq_length: 4096
  image_resolution: 512            # Resize images to 512x512

wandb:
  project: "mika-finetuning"
  run_name: "medgemma-4b-qlora-v1"
```

---

## 6. Phase 4: Training Pipeline

### 6.1 Training Script

Create `training/scripts/train_medgemma.py`:

```python
"""
Fine-tune MedGemma 4B on MIKA's MRI instruction-tuning dataset.

Usage:
    python training/scripts/train_medgemma.py \
        --config training/configs/medgemma_qlora.yaml \
        --data_dir training/data/instruction_tuning \
        --output_dir training/checkpoints/medgemma-4b-mika

Prerequisites:
    pip install torch transformers peft bitsandbytes accelerate
    pip install trl datasets pillow wandb
"""
import os
import json
import yaml
import torch
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
    TrainingArguments,
)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from trl import SFTTrainer, SFTConfig
from datasets import load_dataset


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def setup_model(config: dict):
    """Load MedGemma 4B with 4-bit quantization for QLoRA."""
    model_name = config["model"]["name"]

    # Quantization config
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )

    # Load model
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map="auto",
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
    )

    # Load tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Prepare for QLoRA
    model = prepare_model_for_kbit_training(model)

    # Apply LoRA
    lora_config = LoraConfig(
        r=config["lora"]["r"],
        lora_alpha=config["lora"]["lora_alpha"],
        lora_dropout=config["lora"]["lora_dropout"],
        target_modules=config["lora"]["target_modules"],
        bias=config["lora"]["bias"],
        task_type=config["lora"]["task_type"],
    )
    model = get_peft_model(model, lora_config)

    # Print trainable parameters
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"Trainable params: {trainable:,} / {total:,} ({100 * trainable / total:.2f}%)")

    return model, tokenizer


def format_conversation(example: dict, tokenizer) -> str:
    """
    Format a training example into the chat template format
    that MedGemma expects.
    """
    messages = []
    for turn in example["conversations"]:
        role = "user" if turn["from"] == "human" else "assistant"
        messages.append({"role": role, "content": turn["value"]})

    # Apply the model's chat template
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=False,
    )
    return text


def main(args):
    config = load_config(args.config)

    print("=" * 60)
    print("MIKA Fine-Tuning Pipeline")
    print(f"Model: {config['model']['name']}")
    print(f"Config: {args.config}")
    print("=" * 60)

    # 1. Load model + tokenizer
    print("\n[1/4] Loading model...")
    model, tokenizer = setup_model(config)

    # 2. Load dataset
    print("\n[2/4] Loading dataset...")
    dataset = load_dataset(
        "json",
        data_files={
            "train": config["data"]["train_file"],
            "eval": config["data"]["eval_file"],
        },
    )
    print(f"  Train: {len(dataset['train'])} samples")
    print(f"  Eval:  {len(dataset['eval'])} samples")

    # 3. Format conversations
    print("\n[3/4] Formatting conversations...")

    def preprocess(example):
        example["text"] = format_conversation(example, tokenizer)
        return example

    dataset = dataset.map(preprocess, num_proc=4)

    # 4. Train
    print("\n[4/4] Starting training...")
    training_config = config["training"]

    training_args = SFTConfig(
        output_dir=training_config["output_dir"],
        num_train_epochs=training_config["num_train_epochs"],
        per_device_train_batch_size=training_config["per_device_train_batch_size"],
        per_device_eval_batch_size=training_config["per_device_eval_batch_size"],
        gradient_accumulation_steps=training_config["gradient_accumulation_steps"],
        learning_rate=training_config["learning_rate"],
        weight_decay=training_config["weight_decay"],
        warmup_ratio=training_config["warmup_ratio"],
        lr_scheduler_type=training_config["lr_scheduler_type"],
        logging_steps=training_config["logging_steps"],
        save_steps=training_config["save_steps"],
        eval_steps=training_config["eval_steps"],
        eval_strategy="steps",
        save_total_limit=training_config["save_total_limit"],
        bf16=training_config["bf16"],
        max_grad_norm=training_config["max_grad_norm"],
        group_by_length=training_config["group_by_length"],
        dataloader_num_workers=training_config["dataloader_num_workers"],
        remove_unused_columns=training_config["remove_unused_columns"],
        max_seq_length=config["data"]["max_seq_length"],
        dataset_text_field="text",
        report_to="wandb" if config.get("wandb") else "none",
        run_name=config.get("wandb", {}).get("run_name", "mika-training"),
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset["eval"],
        processing_class=tokenizer,
    )

    trainer.train()

    # Save final model
    final_path = Path(training_config["output_dir"]) / "final"
    trainer.save_model(str(final_path))
    tokenizer.save_pretrained(str(final_path))
    print(f"\n=== Training complete! Model saved to {final_path} ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MIKA Fine-Tuning Pipeline")
    parser.add_argument("--config", required=True, help="Path to YAML config")
    parser.add_argument("--data_dir", default="training/data/instruction_tuning")
    parser.add_argument("--output_dir", default=None)
    args = parser.parse_args()
    main(args)
```

### 6.2 Training Data Split Strategy

```
Total dataset: N samples
├── train.jsonl  (85%)   — Training set
├── eval.jsonl   (10%)   — Validation (monitored during training)
└── test.jsonl   (5%)    — Held-out test set (never seen during training)

Stratified by:
├── anatomy_type (spine: 40%, brain: 30%, msk: 20%, other: 10%)
├── pathology (normal: 30%, mild: 25%, moderate: 25%, severe: 20%)
└── sequence_type (T1: 25%, T2: 30%, FLAIR: 15%, multi-seq: 30%)
```

Create `training/scripts/split_dataset.py`:

```python
"""Split instruction-tuning JSONL into train/eval/test with stratification."""
import json
import random
from pathlib import Path
from collections import defaultdict


def split_dataset(input_path: str, output_dir: str, seed: int = 42):
    random.seed(seed)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # Load all samples
    with open(input_path) as f:
        samples = [json.loads(line) for line in f]

    # Group by anatomy type for stratification
    by_anatomy = defaultdict(list)
    for s in samples:
        anatomy = s.get("id", "").split("_")[0]  # e.g., "spine_001" -> "spine"
        by_anatomy[anatomy].append(s)

    train, val, test = [], [], []

    for anatomy, group in by_anatomy.items():
        random.shuffle(group)
        n = len(group)
        n_test = max(1, int(n * 0.05))
        n_val = max(1, int(n * 0.10))

        test.extend(group[:n_test])
        val.extend(group[n_test : n_test + n_val])
        train.extend(group[n_test + n_val :])

    # Shuffle each split
    random.shuffle(train)
    random.shuffle(val)
    random.shuffle(test)

    # Write
    for name, data in [("train", train), ("eval", val), ("test", test)]:
        path = output / f"{name}.jsonl"
        with open(path, "w") as f:
            for s in data:
                f.write(json.dumps(s) + "\n")
        print(f"  {name}: {len(data)} samples -> {path}")

    print(f"\nTotal: {len(samples)} -> train:{len(train)} eval:{len(val)} test:{len(test)}")
```

---

## 7. Phase 5: Evaluation & Benchmarking

### 7.1 Evaluation Metrics

| Metric | What It Measures | Target |
|--------|-----------------|--------|
| **JSON Schema Validity** | Does output match expected schema? | > 95% |
| **Finding Detection Rate** | Correct pathology identification vs ground truth | > 80% |
| **Tier Accuracy** | Correct confidence tier assignment | > 85% |
| **False Positive Rate** | Findings reported that don't exist | < 10% |
| **False Negative Rate** | Real findings missed | < 15% |
| **BLEU/ROUGE** | Text similarity to reference reports | BLEU > 0.3 |
| **Clinical Concordance** | Agreement with expert radiologist assessment | > 75% |
| **Inference Latency** | Time per analysis | < 10 seconds |

### 7.2 Evaluation Script

Create `training/scripts/evaluate.py`:

```python
"""
Evaluate fine-tuned model against held-out test set.
Compares model output to ground truth on multiple metrics.
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class EvalResult:
    sample_id: str
    anatomy_type: str
    json_valid: bool
    schema_valid: bool
    findings_detected: list[str]
    findings_missed: list[str]
    false_positives: list[str]
    tier_accuracy: float
    inference_time_ms: float
    impression_bleu: float


def evaluate_json_validity(response: str) -> tuple[bool, Optional[dict]]:
    """Check if model output is valid JSON matching MIKA schema."""
    try:
        # Handle markdown-wrapped JSON
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]

        parsed = json.loads(response.strip())

        # Check required fields
        required_fields = ["impression", "confidence_summary"]
        for field in required_fields:
            if field not in parsed:
                return False, None

        return True, parsed
    except (json.JSONDecodeError, IndexError):
        return False, None


def evaluate_findings(predicted: dict, ground_truth: dict, anatomy: str) -> dict:
    """Compare predicted findings against ground truth."""
    # Extract impression items as finding indicators
    pred_impressions = set(
        item.lower().strip()
        for item in predicted.get("impression", [])
    )
    gt_impressions = set(
        item.lower().strip()
        for item in ground_truth.get("impression", [])
    )

    # Simple keyword-based overlap (could be replaced with semantic similarity)
    detected = pred_impressions & gt_impressions
    missed = gt_impressions - pred_impressions
    false_pos = pred_impressions - gt_impressions

    return {
        "detected": list(detected),
        "missed": list(missed),
        "false_positives": list(false_pos),
        "detection_rate": len(detected) / max(len(gt_impressions), 1),
        "false_positive_rate": len(false_pos) / max(len(pred_impressions), 1),
    }


def evaluate_tier_accuracy(predicted: dict, ground_truth: dict) -> float:
    """Check if confidence tiers are correctly assigned."""
    pred_tiers = predicted.get("confidence_summary", {})
    gt_tiers = ground_truth.get("confidence_summary", {})

    correct = 0
    total = 0

    for tier in ["tier_a", "tier_b", "tier_c", "tier_d"]:
        pred_items = set(str(x).lower() for x in pred_tiers.get(tier, []))
        gt_items = set(str(x).lower() for x in gt_tiers.get(tier, []))
        correct += len(pred_items & gt_items)
        total += len(gt_items)

    return correct / max(total, 1)


def run_evaluation(
    model_path: str,
    test_file: str,
    output_file: str,
    use_vllm: bool = False,
):
    """
    Run full evaluation suite on test set.
    Can evaluate either local model or Claude API.
    """
    results = []

    with open(test_file) as f:
        test_samples = [json.loads(line) for line in f]

    print(f"Evaluating {len(test_samples)} test samples...")

    for i, sample in enumerate(test_samples):
        print(f"  [{i+1}/{len(test_samples)}] {sample['id']}")

        # Get ground truth
        gt_response = sample["conversations"][-1]["value"]
        gt_valid, gt_parsed = evaluate_json_validity(gt_response)

        if not gt_valid:
            print(f"    WARNING: Invalid ground truth for {sample['id']}")
            continue

        # Run inference
        start_time = time.time()
        # NOTE: Replace with actual inference call
        # predicted_response = model.generate(sample["conversations"][0]["value"])
        predicted_response = ""  # Placeholder
        inference_time = (time.time() - start_time) * 1000

        # Evaluate
        pred_valid, pred_parsed = evaluate_json_validity(predicted_response)

        if pred_valid and pred_parsed:
            anatomy = sample["id"].split("_")[0]
            findings = evaluate_findings(pred_parsed, gt_parsed, anatomy)
            tier_acc = evaluate_tier_accuracy(pred_parsed, gt_parsed)
        else:
            findings = {"detected": [], "missed": [], "false_positives": [], "detection_rate": 0}
            tier_acc = 0

        result = EvalResult(
            sample_id=sample["id"],
            anatomy_type=sample["id"].split("_")[0],
            json_valid=pred_valid,
            schema_valid=pred_valid and pred_parsed is not None,
            findings_detected=findings["detected"],
            findings_missed=findings["missed"],
            false_positives=findings["false_positives"],
            tier_accuracy=tier_acc,
            inference_time_ms=inference_time,
            impression_bleu=0.0,  # TODO: Add BLEU calculation
        )
        results.append(result)

    # Aggregate metrics
    n = len(results)
    metrics = {
        "total_samples": n,
        "json_validity_rate": sum(r.json_valid for r in results) / max(n, 1),
        "schema_validity_rate": sum(r.schema_valid for r in results) / max(n, 1),
        "mean_detection_rate": sum(
            len(r.findings_detected) / max(len(r.findings_detected) + len(r.findings_missed), 1)
            for r in results
        ) / max(n, 1),
        "mean_false_positive_rate": sum(
            len(r.false_positives) / max(len(r.findings_detected) + len(r.false_positives), 1)
            for r in results
        ) / max(n, 1),
        "mean_tier_accuracy": sum(r.tier_accuracy for r in results) / max(n, 1),
        "mean_inference_time_ms": sum(r.inference_time_ms for r in results) / max(n, 1),
        "p95_inference_time_ms": sorted([r.inference_time_ms for r in results])[int(n * 0.95)] if n > 0 else 0,
    }

    # Write results
    with open(output_file, "w") as f:
        json.dump({
            "metrics": metrics,
            "per_sample": [
                {
                    "id": r.sample_id,
                    "anatomy": r.anatomy_type,
                    "json_valid": r.json_valid,
                    "detection_rate": len(r.findings_detected) / max(len(r.findings_detected) + len(r.findings_missed), 1),
                    "tier_accuracy": r.tier_accuracy,
                    "inference_ms": r.inference_time_ms,
                }
                for r in results
            ],
        }, f, indent=2)

    print(f"\n{'='*60}")
    print("EVALUATION RESULTS")
    print(f"{'='*60}")
    for k, v in metrics.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print(f"\nResults saved to {output_file}")

    return metrics
```

### 7.3 Blind Test Protocol

Replicate the blind testing methodology used in this session:

```
1. Download pathology DICOM from TCIA (known diagnosis)
2. Anonymize metadata (strip StudyDescription, PatientName, etc.)
3. Feed generic clinical history (e.g., "Adult patient with headaches")
4. Run through MIKA pipeline
5. Compare MIKA's top differential against known ground truth
6. Score: exact match (2 pts), in top-3 (1 pt), missed (0 pts)
```

Target: **Top-1 accuracy > 60%, Top-3 accuracy > 80%** on blind test cases.

---

## 8. Phase 6: Integration with MIKA

### 8.1 New Interpreter Class

Create `backend/services/local_interpreter.py`:

```python
"""
Local model interpreter for MIKA.
Drop-in replacement for ClaudeInterpreter using fine-tuned MedGemma.
Implements the same InterpretationRequest -> ClinicalInterpretation interface.
"""
import json
import base64
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from services.claude_interpreter import (
    InterpretationRequest,
    ClinicalInterpretation,
    get_system_prompt,
)


class LocalModelInterpreter:
    """
    Interprets MRI studies using a locally-served fine-tuned model.
    Uses the same interface as ClaudeInterpreter for drop-in replacement.
    """

    def __init__(
        self,
        model_endpoint: str = "http://localhost:8001/v1",  # vLLM OpenAI-compatible endpoint
        model_name: str = "mika-medgemma-4b",
        timeout: int = 60,
        confidence_threshold: float = 0.7,  # Below this, escalate to Claude
    ):
        self.model_endpoint = model_endpoint
        self.model_name = model_name
        self.timeout = timeout
        self.confidence_threshold = confidence_threshold

    def interpret(self, request: InterpretationRequest) -> ClinicalInterpretation:
        """
        Run interpretation using local fine-tuned model.
        Returns same ClinicalInterpretation as ClaudeInterpreter.
        """
        import requests as http_requests

        # Build message content (same format as ClaudeInterpreter)
        system_prompt = get_system_prompt(request.anatomy_type)
        user_content = self._build_user_message(request)

        # Call local model via OpenAI-compatible API (vLLM)
        start = time.time()
        response = http_requests.post(
            f"{self.model_endpoint}/chat/completions",
            json={
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 8000,
                "temperature": 0.1,  # Low temperature for clinical consistency
            },
            timeout=self.timeout,
        )
        elapsed_ms = (time.time() - start) * 1000

        if response.status_code != 200:
            raise RuntimeError(f"Local model error: {response.status_code} - {response.text}")

        result = response.json()
        raw_text = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})

        # Parse JSON (same logic as ClaudeInterpreter)
        interpretation = self._parse_response(
            raw_text, request.anatomy_type, usage, elapsed_ms
        )

        return interpretation

    def _build_user_message(self, request: InterpretationRequest) -> str:
        """Build user message in same format as Claude interpreter."""
        parts = []

        # Measurements
        calibration = request.measurements_json.get("calibration_status", "Visual-only")
        parts.append(
            f"## Pre-Computed Measurements (Calibration: {calibration})\n"
            f"```json\n{json.dumps(request.measurements_json, indent=2)}\n```\n"
            f"[Warning: Use as-is, do not fabricate alternative values]"
        )

        # Images (as base64 references for vision model)
        for label, b64_data in request.key_images_b64.items():
            parts.append(f"\n### Image: {label}\n[image: {label}]")

        # Clinical context
        if request.clinical_history:
            parts.append(f"\n## Clinical History\n{request.clinical_history}")
        if request.surgical_notes:
            parts.append(f"\n## Surgical Notes\n{request.surgical_notes}")
        if request.prior_reports:
            parts.append(f"\n## Prior Reports\n{request.prior_reports}")

        # Task
        parts.append(
            "\n## Task\n"
            "Analyze measurements and images. Produce clinical findings with [Tier X] tags.\n"
            "Return valid JSON matching the specified format."
        )

        return "\n".join(parts)

    def _parse_response(
        self, raw_text: str, anatomy_type: str, usage: dict, elapsed_ms: float
    ) -> ClinicalInterpretation:
        """Parse model response into ClinicalInterpretation (same as Claude)."""
        json_str = raw_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]

        try:
            parsed = json.loads(json_str.strip())
        except json.JSONDecodeError:
            parsed = {}

        return ClinicalInterpretation(
            anatomy_type=anatomy_type,
            findings_by_level=parsed.get("findings_by_level", {}),
            findings_by_region=parsed.get("findings_by_region", {}),
            findings_by_structure=parsed.get("findings_by_structure", {}),
            alignment=parsed.get("alignment", ""),
            conus=parsed.get("conus", ""),
            enhancement_pattern=parsed.get("enhancement_pattern", ""),
            diffusion_findings=parsed.get("diffusion_findings", ""),
            joint_effusion=parsed.get("joint_effusion", ""),
            bone_marrow=parsed.get("bone_marrow", ""),
            identified_anatomy=parsed.get("identified_anatomy", ""),
            incidentals=parsed.get("incidentals", ""),
            impression=parsed.get("impression", []),
            confidence_summary=parsed.get("confidence_summary", {}),
            post_surgical_assessment=parsed.get("post_surgical_assessment", ""),
            raw_response=raw_text,
            model_used=f"mika-medgemma-4b (local, {elapsed_ms:.0f}ms)",
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )

    def should_escalate_to_claude(self, interpretation: ClinicalInterpretation) -> bool:
        """
        Determine if this case should be escalated to Claude for a second opinion.
        Returns True if the local model's confidence is too low.
        """
        tier_d = interpretation.confidence_summary.get("tier_d", [])
        tier_c = interpretation.confidence_summary.get("tier_c", [])
        total_findings = sum(
            len(v) for v in interpretation.confidence_summary.values()
        )

        if total_findings == 0:
            return True  # No findings at all -> escalate

        low_confidence_ratio = (len(tier_c) + len(tier_d)) / max(total_findings, 1)
        return low_confidence_ratio > (1 - self.confidence_threshold)
```

### 8.2 Hybrid Interpreter (Orchestrates Both Models)

Create `backend/services/hybrid_interpreter.py`:

```python
"""
Hybrid interpreter that uses local model as primary
and Claude as fallback for complex cases.
"""
from services.claude_interpreter import ClaudeInterpreter, InterpretationRequest, ClinicalInterpretation
from services.local_interpreter import LocalModelInterpreter


class HybridInterpreter:
    """
    Two-tier interpretation system:
    1. Local fine-tuned model (fast, free)
    2. Claude Opus (accurate, paid) - only for complex/low-confidence cases
    """

    def __init__(
        self,
        local_endpoint: str = "http://localhost:8001/v1",
        claude_api_key: str = None,
        always_use_claude: bool = False,
        confidence_threshold: float = 0.7,
    ):
        self.local = LocalModelInterpreter(
            model_endpoint=local_endpoint,
            confidence_threshold=confidence_threshold,
        )
        self.claude = ClaudeInterpreter(api_key=claude_api_key) if claude_api_key else None
        self.always_use_claude = always_use_claude

    def interpret(self, request: InterpretationRequest) -> ClinicalInterpretation:
        """
        Interpret MRI using hybrid approach:
        1. Run local model first
        2. If confidence is low, escalate to Claude
        3. Return best interpretation
        """
        # Step 1: Local model
        local_result = self.local.interpret(request)

        # Step 2: Check if Claude is needed
        if self.always_use_claude:
            needs_claude = True
        else:
            needs_claude = self.local.should_escalate_to_claude(local_result)

        if needs_claude and self.claude:
            print(f"  [Hybrid] Escalating to Claude (low confidence from local model)")
            claude_result = self.claude.interpret(request)
            claude_result.model_used = f"claude-opus-4-6 (escalated from local)"
            return claude_result

        return local_result
```

### 8.3 Pipeline Integration

Modify `backend/app.py` to support interpreter selection:

```python
# In _run_analysis_pipeline(), replace:
#   interpreter = ClaudeInterpreter(api_key=api_key)
# With:

INTERPRETER_MODE = os.environ.get("MIKA_INTERPRETER", "claude")
# Options: "claude" (default), "local", "hybrid"

if INTERPRETER_MODE == "local":
    from services.local_interpreter import LocalModelInterpreter
    interpreter = LocalModelInterpreter(
        model_endpoint=os.environ.get("MIKA_LOCAL_MODEL_URL", "http://localhost:8001/v1"),
    )
elif INTERPRETER_MODE == "hybrid":
    from services.hybrid_interpreter import HybridInterpreter
    interpreter = HybridInterpreter(
        local_endpoint=os.environ.get("MIKA_LOCAL_MODEL_URL", "http://localhost:8001/v1"),
        claude_api_key=api_key,
        confidence_threshold=float(os.environ.get("MIKA_CONFIDENCE_THRESHOLD", "0.7")),
    )
else:  # "claude" (default, current behavior)
    interpreter = ClaudeInterpreter(api_key=api_key)
```

---

## 9. Phase 7: Deployment & Serving

### 9.1 vLLM Inference Server

Create `training/scripts/serve_model.py`:

```python
"""
Serve fine-tuned MedGemma 4B via vLLM with OpenAI-compatible API.

Usage:
    # Option 1: Direct vLLM
    python -m vllm.entrypoints.openai.api_server \
        --model training/checkpoints/medgemma-4b-mika/final \
        --port 8001 \
        --dtype bfloat16 \
        --max-model-len 4096 \
        --gpu-memory-utilization 0.9

    # Option 2: This script (with health checks)
    python training/scripts/serve_model.py \
        --model-path training/checkpoints/medgemma-4b-mika/final \
        --port 8001
"""
import argparse
import subprocess
import sys
import time
import requests


def wait_for_server(port: int, timeout: int = 120):
    """Wait for vLLM server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"http://localhost:{port}/health")
            if resp.status_code == 200:
                print(f"Server ready on port {port}")
                return True
        except requests.ConnectionError:
            pass
        time.sleep(2)
    print(f"Server failed to start within {timeout}s")
    return False


def main(args):
    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", args.model_path,
        "--served-model-name", "mika-medgemma-4b",
        "--port", str(args.port),
        "--dtype", "bfloat16",
        "--max-model-len", "4096",
        "--gpu-memory-utilization", "0.9",
        "--trust-remote-code",
    ]

    if args.quantization:
        cmd.extend(["--quantization", args.quantization])

    print(f"Starting vLLM server: {' '.join(cmd)}")
    process = subprocess.Popen(cmd)

    if wait_for_server(args.port):
        print(f"\nMIKA model serving at http://localhost:{args.port}/v1")
        print(f"OpenAI-compatible endpoint: POST /v1/chat/completions")
        print(f"Model name: mika-medgemma-4b")
        process.wait()
    else:
        process.terminate()
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--quantization", default=None, help="e.g., awq, gptq")
    main(parser.parse_args())
```

### 9.2 Docker Deployment

Create `training/docker/Dockerfile.inference`:

```dockerfile
FROM vllm/vllm-openai:latest

# Copy model weights
COPY training/checkpoints/medgemma-4b-mika/final /model

# Set environment
ENV MODEL_PATH=/model
ENV PORT=8001

# Expose port
EXPOSE 8001

# Start vLLM server
CMD python -m vllm.entrypoints.openai.api_server \
    --model $MODEL_PATH \
    --served-model-name mika-medgemma-4b \
    --port $PORT \
    --dtype bfloat16 \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.9 \
    --trust-remote-code
```

### 9.3 RunPod Serverless Deployment (Cost-Effective)

Create `training/scripts/deploy_runpod.py`:

```python
"""
Deploy fine-tuned model to RunPod Serverless.
Pay-per-use: $0 when idle, ~$1.39/hr when processing.

Steps:
1. Upload model to HuggingFace Hub (private repo)
2. Create RunPod serverless endpoint with vLLM worker
3. Update MIKA's MIKA_LOCAL_MODEL_URL to point to RunPod endpoint
"""

RUNPOD_TEMPLATE = {
    "name": "mika-medgemma-inference",
    "imageName": "runpod/worker-vllm:stable-cuda12.1.0",
    "env": {
        "MODEL_NAME": "YOUR_HF_ORG/mika-medgemma-4b",  # Private HF repo
        "MAX_MODEL_LENGTH": "4096",
        "DTYPE": "bfloat16",
        "GPU_MEMORY_UTILIZATION": "0.9",
        "DEFAULT_BATCH_SIZE": "1",
        "MODEL_REVISION": "main",
    },
    "gpuTypeId": "NVIDIA RTX A6000",  # 48GB VRAM, $0.76/hr
    "minWorkers": 0,                  # Scale to zero when idle
    "maxWorkers": 3,                  # Max concurrent requests
    "idleTimeout": 300,               # 5 min idle -> scale down
}
```

---

## 10. Infrastructure & Cost Estimates

### 10.1 Training Costs

| Scenario | GPU | Provider | Time | Cost |
|----------|-----|----------|------|------|
| MedGemma 4B + QLoRA (5K samples) | 1x RTX 4090 | RunPod | 2-4 hrs | **$2-4** |
| MedGemma 4B + QLoRA (10K samples) | 1x A100 80GB | RunPod | 4-8 hrs | **$6-11** |
| MedGemma 4B + LoRA (50K samples) | 1x A100 80GB | RunPod | 12-24 hrs | **$17-33** |
| RadFM + LoRA (50K samples) | 2x A100 80GB | RunPod | 24-48 hrs | **$67-134** |
| Full fine-tune 4B (50K samples) | 4x A100 80GB | Lambda | 12-24 hrs | **$62-124** |

### 10.2 Data Generation Costs (Claude API for ground truth)

| Samples | Claude API Calls | Est. Tokens | Cost |
|---------|-----------------|-------------|------|
| 5,000 | 5,000 | ~15M input + 7M output | ~$225 |
| 10,000 | 10,000 | ~30M input + 14M output | ~$450 |
| 50,000 | 50,000 | ~150M input + 70M output | ~$2,250 |

**Cost optimization:** Generate 5K samples with Claude, then use the fine-tuned model to generate additional training data (self-training / distillation).

### 10.3 Inference Costs (Production)

| Setup | Per-Query Cost | Monthly (100 queries/day) | Latency |
|-------|---------------|---------------------------|---------|
| **Claude Opus API** | ~$0.05-0.10 | $150-300 | 5-15s |
| **RunPod Serverless** | ~$0.01-0.02 | $30-60 | 3-8s |
| **Dedicated A6000** | ~$0.003 | $547 (24/7) or ~$100 (on-demand) | 2-5s |
| **Local RTX 4090** | $0 (hardware cost) | $0 | 2-5s |

### 10.4 Recommended Budget Allocation

| Phase | Item | Budget |
|-------|------|--------|
| Phase 1 | Dataset downloads + storage | $0-50 |
| Phase 2 | RAG index build + sentence-transformers | $0-10 |
| Phase 3 | Claude API for ground truth (5K samples) | $200-250 |
| Phase 4 | Training GPU (3-5 runs on RunPod) | $50-100 |
| Phase 5 | Evaluation GPU + blind test data | $20-50 |
| Phase 6 | Integration testing | $0-20 |
| Phase 7 | RunPod serverless first month | $30-100 |
| **Total** | | **$300-580** |

---

## 11. File Structure

```
training/
├── configs/
│   ├── medgemma_qlora.yaml          # QLoRA training config
│   ├── medgemma_lora.yaml           # Full LoRA training config (later)
│   └── radfm_qlora.yaml             # RadFM config (if needed)
│
├── scripts/
│   ├── download_datasets.py         # Dataset download manager
│   ├── prepare_dataset.py           # DICOM -> instruction-tuning JSONL
│   ├── augment_data.py              # MRI-safe augmentation
│   ├── split_dataset.py             # Train/eval/test split
│   ├── train_medgemma.py            # QLoRA training script
│   ├── evaluate.py                  # Evaluation & benchmarking
│   ├── serve_model.py               # vLLM inference server
│   └── deploy_runpod.py             # RunPod serverless deployment
│
├── data/
│   ├── raw/                         # Downloaded datasets
│   │   ├── spider/
│   │   ├── brats/
│   │   ├── ixi/
│   │   ├── fastmri/
│   │   └── tcia/
│   ├── processed/                   # Converted PNGs + metadata
│   │   ├── spine/
│   │   ├── brain/
│   │   └── msk/
│   └── instruction_tuning/          # Final training data
│       ├── train.jsonl
│       ├── eval.jsonl
│       └── test.jsonl
│
├── knowledge_base/                  # RAG reference data
│   ├── spine_pathology.json
│   ├── brain_pathology.json
│   ├── msk_pathology.json
│   └── assessment_protocols.json
│
├── checkpoints/                     # Saved model weights
│   └── medgemma-4b-mika/
│       └── final/
│
├── docker/
│   └── Dockerfile.inference         # vLLM inference container
│
└── results/                         # Evaluation outputs
    ├── eval_v1.json
    └── blind_test_v1.json

backend/
├── services/
│   ├── claude_interpreter.py        # (existing) Claude Opus integration
│   ├── local_interpreter.py         # (NEW) Local model integration
│   ├── hybrid_interpreter.py        # (NEW) Hybrid orchestrator
│   └── rag_engine.py                # (NEW) RAG retrieval engine
└── ...
```

---

## 12. Implementation Checklist

### Week 1-2: Data & RAG

- [ ] Create `training/` directory structure
- [ ] Implement `download_datasets.py` — download SPIDER, IXI, and TCIA collections
- [ ] Implement `prepare_dataset.py` — DICOM to instruction-tuning format
- [ ] Implement `augment_data.py` — MRI-safe augmentation pipeline
- [ ] Implement `split_dataset.py` — stratified train/eval/test split
- [ ] Generate 1,000 ground-truth reports using Claude API (proof of concept)
- [ ] Implement `rag_engine.py` — vector search with sentence-transformers
- [ ] Build knowledge base JSON files (spine, brain, MSK pathology)
- [ ] Integrate RAG into existing Claude pipeline
- [ ] Test RAG-enhanced Claude on 10 sample cases

### Week 3-4: Fine-Tuning

- [ ] Install training dependencies (torch, transformers, peft, bitsandbytes, trl)
- [ ] Create `medgemma_qlora.yaml` training config
- [ ] Implement `train_medgemma.py` — QLoRA training script
- [ ] Generate 5,000 instruction-tuning pairs (Claude API)
- [ ] Run first training job on RunPod (A100 80GB)
- [ ] Monitor with Weights & Biases
- [ ] Iterate: adjust learning rate, LoRA rank, epochs
- [ ] Train final model on 10K+ samples

### Week 5-6: Evaluation & Integration

- [ ] Implement `evaluate.py` — multi-metric evaluation suite
- [ ] Run evaluation on held-out test set
- [ ] Conduct 10 blind tests (known pathology, anonymized DICOM)
- [ ] Compare local model vs Claude on same cases
- [ ] Implement `local_interpreter.py` — vLLM integration
- [ ] Implement `hybrid_interpreter.py` — fallback orchestration
- [ ] Add `MIKA_INTERPRETER` environment variable to `app.py`
- [ ] End-to-end test: upload DICOM -> local model -> report

### Week 7-8: Deployment

- [ ] Implement `serve_model.py` — vLLM server wrapper
- [ ] Create `Dockerfile.inference` for containerized deployment
- [ ] Deploy to RunPod Serverless (or local GPU)
- [ ] Load test: 50 concurrent analyses
- [ ] Monitor inference latency and memory usage
- [ ] Set up Claude fallback for low-confidence cases
- [ ] Production readiness review
- [ ] Update MIKA documentation

### Ongoing: Continuous Improvement

- [ ] Collect user feedback on report quality
- [ ] Expand training data to 50K+ samples
- [ ] Add new anatomy types (cardiac, abdomen, chest)
- [ ] Fine-tune on MIKA-specific blind test failures
- [ ] A/B test local model vs Claude in production
- [ ] Consider RadFM for 3D volume support
- [ ] Explore model distillation (train smaller model from larger)

---

## Appendix A: Required Python Dependencies

```
# Training dependencies (install on GPU machine)
torch>=2.1.0
transformers>=4.38.0
peft>=0.8.0
bitsandbytes>=0.42.0
accelerate>=0.26.0
trl>=0.7.0
datasets>=2.17.0
wandb>=0.16.0
sentence-transformers>=2.3.0
huggingface-hub>=0.20.0

# Data preparation
pydicom>=2.4.4
numpy>=1.26.0
scipy>=1.12.0
Pillow>=10.2.0
tcia_utils>=0.5.0
SimpleITK>=2.3.0
nibabel>=5.2.0

# Inference serving
vllm>=0.3.0

# Evaluation
rouge-score>=0.1.2
nltk>=3.8.1
```

## Appendix B: Environment Variables

```bash
# Interpreter mode: "claude" (default) | "local" | "hybrid"
export MIKA_INTERPRETER="hybrid"

# Local model endpoint (vLLM server)
export MIKA_LOCAL_MODEL_URL="http://localhost:8001/v1"

# Confidence threshold for Claude escalation (0.0-1.0)
export MIKA_CONFIDENCE_THRESHOLD="0.7"

# Claude API key (for hybrid mode fallback)
export ANTHROPIC_API_KEY="sk-ant-..."

# Weights & Biases (for training monitoring)
export WANDB_API_KEY="..."
export WANDB_PROJECT="mika-finetuning"
```

## Appendix C: Quick Start Commands

```bash
# 1. Download datasets
python training/scripts/download_datasets.py

# 2. Prepare instruction-tuning data
python training/scripts/prepare_dataset.py \
    --raw-dir training/data/raw/spider \
    --output training/data/instruction_tuning/all.jsonl \
    --anatomy spine \
    --use-claude --api-key $ANTHROPIC_API_KEY

# 3. Split data
python training/scripts/split_dataset.py \
    --input training/data/instruction_tuning/all.jsonl \
    --output-dir training/data/instruction_tuning

# 4. Train model (on GPU machine)
python training/scripts/train_medgemma.py \
    --config training/configs/medgemma_qlora.yaml

# 5. Evaluate
python training/scripts/evaluate.py \
    --model-path training/checkpoints/medgemma-4b-mika/final \
    --test-file training/data/instruction_tuning/test.jsonl \
    --output training/results/eval_v1.json

# 6. Serve model
python training/scripts/serve_model.py \
    --model-path training/checkpoints/medgemma-4b-mika/final \
    --port 8001

# 7. Run MIKA with local model
MIKA_INTERPRETER=hybrid MIKA_LOCAL_MODEL_URL=http://localhost:8001/v1 \
    python server.py
```

# MIKA — AI Medical MRI Analyzer

## Project Overview
MIKA is a clinical-grade MRI interpretation system that uses Claude Opus 4.6
to analyze medical MRI studies across 10 anatomy types. It processes DICOM files
(and NIfTI, NRRD, PNG/JPG, ZIP), extracts quantitative measurements, and sends
images + data to Claude for structured radiology reports.

## Architecture

```
Frontend (React SPA)  →  FastAPI Backend  →  Claude Opus 4.6
   index.html              app.py              Anthropic API
                           ├── core/
                           │   ├── dicom_engine.py      # DICOM processing, measurements, annotations
                           │   └── format_converter.py   # NIfTI/NRRD/image → DICOM conversion
                           ├── services/
                           │   ├── claude_interpreter.py  # Claude API integration + prompts
                           │   └── batch_sender.py        # [PLANNED] Send all images to Claude
                           ├── prompts/                   # [PLANNED] Master prompt library
                           └── validation/                # [PLANNED] Ground truth validation
```

## Pipeline Flow
1. User uploads DICOM/NIfTI/NRRD/images via frontend
2. `FormatConverter` normalizes all formats to DICOM
3. `DICOMEngine` runs inventory (anatomy detection, calibration, sequence classification)
4. `DICOMEngine` converts all slices to PNG (`work_dir/raw_png/`)
5. For spine: quantitative measurements (disc signal, canal diameter, endplates)
6. Annotation images created (level reference, multi-sequence panel, etc.)
7. `ClaudeInterpreter` sends images + measurements to Claude Opus 4.6
8. Claude returns structured JSON (findings, impression, confidence tiers)
9. Frontend displays results with SSE progress streaming

## Critical Bottleneck (Being Fixed)
**Current:** Only 4 images sent to Claude (lines 564-572 of app.py)
**Target:** Send ALL images (40-80) via BatchSender module
This is the #1 accuracy improvement.

## 10 Supported Anatomy Types
spine, brain, msk, cardiac, chest, abdomen, breast, vascular, head_neck, prostate

Each has a dedicated system prompt and JSON output schema.

## Key Technical Details

### DICOM Calibration
- PixelSpacing tag (0028,0030) provides mm/pixel ratio
- Calibrated studies get Tier A confidence for measurements
- Uncalibrated studies capped at Tier C

### Confidence Tier Framework
- **Tier A**: Confirmed on 2+ sequences or calibrated measurement → "There is..."
- **Tier B**: Single sequence, visual-only → "There likely is..."
- **Tier C**: Suggestive, may be artifact → "Possible..."
- **Tier D**: Cannot assess → "Cannot be reliably assessed"

### Spine Measurements (most developed)
- Disc T2 signal intensity and desiccation ratio
- Canal CSF reduction % and AP diameter (mm)
- Endplate signal ratios (T1, T2, STIR) for Modic classification
- Level identification via sacrum-up protocol

### Non-Spine Anatomies
- Currently NO quantitative measurements (visual-only, Tier B max)
- Will benefit most from sending all images (BatchSender)

## Code Conventions
- Python 3.10+, FastAPI, async endpoints
- Type hints on all function signatures
- Dataclasses for data models (not Pydantic)
- Logging via `logging.getLogger("mika.<module>")`
- DICOM processing via pydicom, image processing via PIL/numpy/scipy
- Claude API via `anthropic` Python SDK
- All file paths use `pathlib.Path`

## Environment
- `ANTHROPIC_API_KEY` — required
- `MIKA_DATA_DIR` — working directory (default: `./data`)
- Server: `uvicorn app:app --host 0.0.0.0 --port 8000` from `backend/`

## Current Implementation Plan
See `docs/PLAN_CV_CLAUDE_AGENTS.md` for the active plan.
4 modules being built: BatchSender, MasterPrompts, VerificationPass, ValidationFramework.
The full 8-module plan is in `docs/90_PERCENT_ACCURACY_PLAN.md` as backup.

## Testing
- Run server: use launch.json config named "mika"
- Test data in `test_data/` directory
- Validation framework (planned) will use SPIDER, BraTS, fastMRI datasets

## Important Rules for Agents
1. Never fabricate medical measurements — use only what DICOMEngine computes
2. All radiology prompts must include anti-hallucination rules
3. Every finding needs a confidence tier (A/B/C/D)
4. New modules go in appropriate subdirectory (core/, services/, prompts/, validation/)
5. Keep claude_interpreter.py as the single integration point with Claude API
6. Master prompts live in backend/prompts/ — one file per anatomy
7. Don't break the existing spine measurement pipeline when adding features

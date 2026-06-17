# MIKA — AI Medical Imaging Analyzer

## Project Overview
MIKA is a clinical-grade medical-imaging interpretation system that uses Claude Opus
to analyze studies across 10 anatomy types AND all common modalities (MR, CT, X-ray,
ultrasound, mammography, PET — not MR only). It processes DICOM files (and NIfTI, NRRD,
PNG/JPG, ZIP), extracts quantitative measurements, and sends images + data to Claude for
structured radiology reports.

### Auth — runs on the user's Claude subscription (no API key)
The default "agent" pipeline shells out to the installed `claude` CLI in headless mode
(`claude -p --output-format json`), authenticated by the user's normal Claude login
(`claude /login` / subscription). No Anthropic API key and no extra Python auth library are
required — "just sign in and shoot". The `anthropic` SDK is an OPTIONAL, lazily-imported
fallback used only by the "lite" pipeline when an API key/token is explicitly provided.
Sign-in status is surfaced in the sidebar; `/api/connect` launches the browser login.

### Durable persistence (reports never disappear)
Every completed study is written to disk under a stable per-user data dir
(`%LOCALAPPDATA%\MIKA\data` on Windows; `MIKA_DATA_DIR` overrides) as `report.json` +
`meta.json`. The in-memory `JOBS` dict is only a hot cache — all report/image/pdf/status
endpoints fall back to disk, so a finished study is always retrievable by `job_id` after a
restart. `GET /api/reports` indexes them for the "Recent studies" screen.

### Security (loopback desktop posture)
Binds `127.0.0.1` by default; CORS pinned to an allow-list (no wildcard+credentials);
job_ids validated (`^[0-9a-f]{8}$`); image/pdf paths confined to the job dir (anti
path-traversal); ZIP extraction guarded against zip-slip; uploads size-capped + filename
-sanitized; CSP + security headers on the app shell.

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

# CLAUDE.md — SpineAI MRI Lumbar Spine Analyzer

## Project Overview

SpineAI is a medical imaging application that analyzes lumbar spine MRI DICOM studies. It uses a two-layer architecture: a deterministic computational engine for measurements, and Claude Opus for clinical narrative interpretation.

**Important:** This is a research/educational tool, not for clinical diagnosis.

## Repository Structure

```
backend/
  app.py                    # FastAPI server, API routes, job management
  core/
    dicom_engine.py         # DICOM processing engine (~850 lines, deterministic)
  services/
    claude_interpreter.py   # Claude API integration for clinical narratives
  models/
    __init__.py             # Dataclass definitions (measurements, calibration, etc.)
  api/
    __init__.py             # API route definitions
frontend/
  index.html               # Single-file React app (CDN-loaded, no build step)
requirements.txt            # Pinned Python dependencies
run.sh                      # Launch script
```

## Tech Stack

- **Backend:** Python 3.10+, FastAPI, Uvicorn
- **Frontend:** React 18 via CDN with Babel standalone (no build tooling)
- **DICOM Processing:** pydicom, numpy, scipy, Pillow
- **AI Integration:** Anthropic Python SDK (`anthropic==0.43.0`)
- **Reports:** reportlab for PDF generation

## Quick Start

```bash
# Install dependencies and start the server
bash run.sh

# Or with API key as argument
bash run.sh sk-ant-...
```

The server runs on `http://localhost:8000` with `--reload` enabled.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key for interpretation layer |
| `SPINEAI_DATA_DIR` | No | Output directory (default: `./data`) |

## Architecture

### Layer 1 — Computational Engine (`dicom_engine.py`)

Deterministic processing with no AI involvement:
- **Phase 0:** DICOM inventory & calibration from PixelSpacing metadata
- **Phase 1:** Vertebral level identification (sacrum-up protocol)
- **Phase 2:** Quantitative measurements (disc T2 ratios, canal CSF, AP diameter)
- **Phase 3:** Annotation generation with pixel-intensity verification

### Layer 2 — AI Interpretation (`claude_interpreter.py`)

- Receives pre-computed measurements + key images as base64
- Uses Claude for structured clinical narrative generation
- Returns JSON with findings, impression, and confidence tiers

### Confidence Tier System

- **Tier A:** Definite — high-quality calibrated data
- **Tier B:** Probable — adequate data with minor limitations
- **Tier C:** Possible — limited data or uncalibrated measurements
- **Tier D:** Cannot assess — insufficient data

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/upload` | Upload DICOM files |
| `POST` | `/api/analyze` | Start analysis pipeline |
| `GET` | `/api/status/{job_id}` | Poll job progress |
| `GET` | `/api/status/{job_id}/stream` | SSE progress stream |
| `GET` | `/api/report/{job_id}` | Get completed report |
| `GET` | `/api/images/{job_id}/{name}` | Serve annotated images |
| `GET` | `/` | Serve frontend |

## Key Data Models

Defined as Python dataclasses in `backend/models/__init__.py`:
- `PixelCalibration`, `SequenceInfo`, `PatientDemographics`
- `DiscMeasurement`, `EndplateAssessment`
- `StudyInventory`, `ClinicalInterpretation`

## Conventions

### Code Style
- **Python:** snake_case for functions/variables, CamelCase for classes
- **Frontend:** CamelCase for React components
- **Medical terminology:** Standard radiology terms (desiccation, foramina, CSF, canal AP)
- **Disc levels:** L1-L2 through L5-S1, counted bottom-up from sacrum

### Safety Constraints
- All measurements derived from DICOM PixelSpacing metadata only
- Arrow placement verified against pixel intensities
- Sacrum-up counting prevents level misidentification
- Uncalibrated measurements capped at Tier C confidence

## Testing & Linting

No test suite or linting configuration exists yet. There is no CI/CD pipeline configured.

## Data Directory Layout

Processing artifacts are stored per job:
```
{SPINEAI_DATA_DIR}/{job_id}/
  dicom/       # Uploaded DICOM files
  work/
    raw_png/   # Converted image slices
    annotated/ # Proof images with annotations
```

# SpineAI — MRI Lumbar Spine Analyzer

AI-assisted MRI lumbar spine analysis powered by **Claude Opus 4.6**. Upload DICOM files, get a clinical-grade radiology report with annotated proof images, DICOM-calibrated measurements, and confidence-tiered findings.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109-green)
![Claude](https://img.shields.io/badge/Claude-Opus%204.6-purple)
![License](https://img.shields.io/badge/License-MIT-yellow)

## How It Works

SpineAI splits the analysis into two layers:

**Layer 1 — Computational Engine** (deterministic, no AI):
- DICOM ingestion and PixelSpacing calibration
- Sacrum-up vertebral level identification
- Quantitative measurements: disc T2 signal ratios, canal CSF intensity, AP diameters
- Multi-sequence endplate analysis for Modic classification
- Annotated proof image generation with pixel intensity verification

**Layer 2 — Clinical Interpretation** (Claude Opus 4.6 via Anthropic API):
- Receives pre-computed measurements + key MRI images
- Produces structured clinical findings with confidence tiers (A/B/C/D)
- Generates impression, post-surgical assessment, and incidental findings
- Every finding is traceable to a specific image and measurement

## Quick Start

```bash
# Clone the repo
git clone https://github.com/husam-hammami/spineai-mri-analyzer.git
cd spineai-mri-analyzer

# Install dependencies
pip install -r requirements.txt

# Run the server
cd backend
python -m uvicorn app:app --host 0.0.0.0 --port 8000

# Open http://localhost:8000 in your browser
```

Or use the launch script:
```bash
bash run.sh
```

You'll need an [Anthropic API key](https://console.anthropic.com/) — enter it in the UI when prompted.

## Architecture

```
spineai-mri-analyzer/
├── backend/
│   ├── core/
│   │   └── dicom_engine.py      # DICOM processing, calibration, measurements
│   ├── services/
│   │   └── claude_interpreter.py # Claude Opus 4.6 API integration
│   ├── api/
│   ├── models/
│   └── app.py                   # FastAPI server & pipeline orchestration
├── frontend/
│   └── index.html               # React UI (PACS-inspired dark theme)
├── requirements.txt
├── run.sh
└── README.md
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/upload` | Upload DICOM files |
| `POST` | `/api/analyze` | Start analysis pipeline |
| `GET` | `/api/status/{job_id}` | Check progress (poll) |
| `GET` | `/api/status/{job_id}/stream` | Real-time progress (SSE) |
| `GET` | `/api/report/{job_id}` | Get completed report |
| `GET` | `/api/images/{job_id}/{name}` | Get annotated proof images |

## Confidence Tier System

Every finding includes a confidence tag:

| Tier | Criteria | Language |
|------|----------|----------|
| **A — Definite** | Confirmed on 2+ sequences or DICOM-calibrated | "There is..." |
| **B — Probable** | Single-sequence or subtle finding | "There likely is..." |
| **C — Possible** | Suggestive, could be artifact | "Possible... recommend correlation" |
| **D — Cannot assess** | Insufficient data | "Cannot be reliably assessed" |

## Key Safety Constraints

- **No fabricated measurements**: Every mm value comes from DICOM PixelSpacing metadata. If no calibration data exists, the system enforces qualitative-only language.
- **Annotation verification**: Every arrow placement is verified against raw pixel intensities before inclusion.
- **Sacrum-up counting**: Vertebral levels are always counted upward from the sacrum to prevent level misidentification.
- **Not a diagnostic tool**: This is a supplementary analysis aid. It does not replace evaluation by a board-certified radiologist.

## Disclaimer

This software is for **research and educational purposes only**. It does not constitute a medical device and has not been cleared by the FDA or any regulatory body. It should not be used for clinical diagnosis. All findings should be correlated with clinical history and reviewed by a qualified radiologist.

## License

MIT

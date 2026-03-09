"""
SpineAI — FastAPI Application Server
======================================
REST API for DICOM upload, analysis pipeline orchestration,
and report delivery. Serves the React frontend as static files.

Architecture:
  POST /api/upload          → Upload DICOM files
  POST /api/analyze         → Run full analysis pipeline
  GET  /api/status/{job_id} → Check analysis progress (SSE)
  GET  /api/report/{job_id} → Get completed report
  GET  /api/images/{job_id}/{name} → Get annotated images
  GET  /                    → Serve React frontend
"""

import os
import uuid
import json
import shutil
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.dicom_engine import DICOMEngine
from services.claude_interpreter import (
    ClaudeInterpreter,
    InterpretationRequest,
    ClinicalInterpretation,
)

# ── Configuration ──

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("spineai.api")

DATA_DIR = Path(os.environ.get("SPINEAI_DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Application ──

app = FastAPI(
    title="SpineAI",
    description="AI-Assisted MRI Lumbar Spine Analysis powered by Claude Opus 4.6",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── In-Memory Job Store ──

class AnalysisJob:
    def __init__(self, job_id: str, dicom_dir: str):
        self.job_id = job_id
        self.dicom_dir = dicom_dir
        self.work_dir = str(DATA_DIR / job_id / "work")
        self.status = "pending"  # pending, inventory, levels, measuring, interpreting, complete, error
        self.progress = 0  # 0-100
        self.progress_message = ""
        self.engine: Optional[DICOMEngine] = None
        self.interpretation: Optional[ClinicalInterpretation] = None
        self.measurements: Optional[dict] = None
        self.annotated_images: dict = {}
        self.error: Optional[str] = None
        self.created_at = datetime.utcnow().isoformat()

JOBS: dict[str, AnalysisJob] = {}


# ── Request/Response Models ──

class AnalyzeRequest(BaseModel):
    job_id: str
    api_key: Optional[str] = None
    clinical_history: Optional[str] = None
    surgical_notes: Optional[str] = None
    prior_reports: Optional[str] = None


class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: int
    progress_message: str
    created_at: str
    error: Optional[str] = None


class ReportResponse(BaseModel):
    job_id: str
    demographics: dict
    measurements: dict
    interpretation: dict
    annotated_images: list
    calibration_status: str


# ── API Endpoints ──

@app.post("/api/upload")
async def upload_dicom(files: list[UploadFile] = File(...)):
    """Upload DICOM files and create a new analysis job."""
    job_id = str(uuid.uuid4())[:8]
    job_dir = DATA_DIR / job_id / "dicom"
    job_dir.mkdir(parents=True, exist_ok=True)

    file_count = 0
    for file in files:
        if file.filename and (file.filename.endswith(".dcm") or file.filename.endswith(".DCM")):
            dest = job_dir / file.filename
            with open(str(dest), "wb") as f:
                content = await file.read()
                f.write(content)
            file_count += 1

    if file_count == 0:
        shutil.rmtree(str(DATA_DIR / job_id))
        raise HTTPException(400, "No DICOM (.dcm) files found in upload")

    job = AnalysisJob(job_id=job_id, dicom_dir=str(job_dir))
    JOBS[job_id] = job

    logger.info(f"Upload complete: job={job_id}, files={file_count}")
    return {"job_id": job_id, "file_count": file_count}


@app.post("/api/analyze")
async def start_analysis(request: AnalyzeRequest, background_tasks: BackgroundTasks):
    """Start the full analysis pipeline in the background."""
    job = JOBS.get(request.job_id)
    if not job:
        raise HTTPException(404, f"Job {request.job_id} not found")

    if job.status not in ("pending", "error"):
        raise HTTPException(400, f"Job is already {job.status}")

    api_key = request.api_key or ANTHROPIC_API_KEY
    if not api_key:
        raise HTTPException(400, "Anthropic API key required (pass in request or set ANTHROPIC_API_KEY env var)")

    job.status = "inventory"
    job.progress = 0

    background_tasks.add_task(
        _run_analysis_pipeline,
        job=job,
        api_key=api_key,
        clinical_history=request.clinical_history,
        surgical_notes=request.surgical_notes,
        prior_reports=request.prior_reports,
    )

    return {"job_id": job.job_id, "status": "started"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    """Get current analysis status (poll or SSE)."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    return JobStatus(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        progress_message=job.progress_message,
        created_at=job.created_at,
        error=job.error,
    )


@app.get("/api/status/{job_id}/stream")
async def stream_status(job_id: str):
    """Server-Sent Events stream for real-time progress updates."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    async def event_generator():
        last_progress = -1
        while True:
            if job.progress != last_progress or job.status in ("complete", "error"):
                data = json.dumps({
                    "status": job.status,
                    "progress": job.progress,
                    "message": job.progress_message,
                    "error": job.error,
                })
                yield f"data: {data}\n\n"
                last_progress = job.progress

            if job.status in ("complete", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/report/{job_id}")
async def get_report(job_id: str):
    """Get the completed analysis report."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    if job.status != "complete":
        raise HTTPException(400, f"Analysis not complete (status: {job.status})")

    interpretation_dict = {}
    if job.interpretation:
        interpretation_dict = {
            "findings_by_level": job.interpretation.findings_by_level,
            "alignment": job.interpretation.alignment,
            "conus": job.interpretation.conus,
            "post_surgical_assessment": job.interpretation.post_surgical_assessment,
            "incidentals": job.interpretation.incidentals,
            "impression": job.interpretation.impression,
            "confidence_summary": job.interpretation.confidence_summary,
            "model_used": job.interpretation.model_used,
            "tokens": {
                "input": job.interpretation.input_tokens,
                "output": job.interpretation.output_tokens,
            },
        }

    return {
        "job_id": job_id,
        "demographics": job.measurements.get("demographics", {}) if job.measurements else {},
        "measurements": job.measurements or {},
        "interpretation": interpretation_dict,
        "annotated_images": list(job.annotated_images.keys()),
        "calibration_status": (
            job.measurements.get("calibration_status", "unknown")
            if job.measurements else "unknown"
        ),
    }


@app.get("/api/images/{job_id}/{image_name}")
async def get_image(job_id: str, image_name: str):
    """Serve an annotated proof image."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    image_path = job.annotated_images.get(image_name)
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(404, f"Image {image_name} not found")

    return FileResponse(image_path, media_type="image/png")


# ── Analysis Pipeline ──

async def _run_analysis_pipeline(
    job: AnalysisJob,
    api_key: str,
    clinical_history: Optional[str] = None,
    surgical_notes: Optional[str] = None,
    prior_reports: Optional[str] = None,
):
    """Execute the full analysis pipeline in the background."""
    try:
        engine = DICOMEngine(job.dicom_dir, job.work_dir)
        job.engine = engine

        # Phase 0: Inventory
        job.status = "inventory"
        job.progress = 5
        job.progress_message = "Cataloging DICOM files and extracting calibration..."
        inventory = engine.run_inventory()

        # Identify key sequences by their series descriptions
        sag_t2 = _find_sequence(inventory, ["t2_tse_sag", "t2_sag"], plane="sagittal", contrast=False)
        sag_t1 = _find_sequence(inventory, ["t1_tse_sag"], plane="sagittal", contrast=False, exclude=["FS", "fs", "CONT"])
        sag_tirm = _find_sequence(inventory, ["tirm", "stir"], plane="sagittal", contrast=False)
        sag_t1_cont = _find_sequence(inventory, ["t1_tse_sag"], plane="sagittal", contrast=True)
        ax_t2 = _find_sequence(inventory, ["t2_tse_tra", "t2_tra"], plane="axial", contrast=False)
        ax_vibe_pre = _find_sequence(inventory, ["vibe_fs_tra", "vibe_tra"], plane="axial", contrast=False)
        ax_vibe_post = _find_sequence(inventory, ["vibe_fs_tra", "vibe_tra"], plane="axial", contrast=True)

        if not sag_t2:
            raise RuntimeError("Could not identify sagittal T2 sequence — required for analysis")

        # Phase 0B: Convert key sequences
        job.progress = 15
        job.progress_message = "Converting DICOM to viewable format..."
        seqs_to_convert = [s for s in [sag_t2, sag_t1, sag_tirm, sag_t1_cont, ax_t2, ax_vibe_pre, ax_vibe_post] if s]
        engine.convert_sequences(seqs_to_convert)

        # Phase 1: Level identification
        job.status = "levels"
        job.progress = 25
        job.progress_message = "Identifying vertebral levels (sacrum-up protocol)..."

        # Find the best midline slice (middle of the sagittal stack)
        seq_info = inventory.sequences[sag_t2]
        midline = seq_info.num_slices // 2
        engine.identify_levels(sag_t2, midline)

        # Phase 2: Measurements
        job.status = "measuring"
        job.progress = 40
        job.progress_message = "Running DICOM-calibrated measurements at all disc levels..."
        engine.measure_all_discs(sag_t2, midline)

        # Endplate assessment
        job.progress = 50
        job.progress_message = "Assessing endplate signal across multiple sequences..."
        seq_map = {}
        if sag_t1:
            seq_map["T1"] = sag_t1
        if sag_t2:
            seq_map["T2"] = sag_t2
        if sag_tirm:
            seq_map["TIRM"] = sag_tirm
        if sag_t1_cont:
            seq_map["T1_CONT"] = sag_t1_cont

        if len(seq_map) >= 2:
            engine.assess_endplates(seq_map, midline, levels=["L4-L5", "L5-S1", "L3-L4"])

        # Phase 3: Annotations
        job.progress = 60
        job.progress_message = "Creating annotated proof images with intensity verification..."

        level_ref = engine.create_level_reference(sag_t2, midline)
        job.annotated_images["level_reference"] = level_ref

        sag_annotated = engine.create_annotated_sagittal(sag_t2, midline)
        job.annotated_images["sag_t2_annotated"] = sag_annotated

        # Multi-sequence panel
        panel_seqs = []
        if sag_t2:
            panel_seqs.append((sag_t2, "T2", midline))
        if sag_t1:
            panel_seqs.append((sag_t1, "T1", midline))
        if sag_tirm:
            panel_seqs.append((sag_tirm, "TIRM", midline))
        if sag_t1_cont:
            panel_seqs.append((sag_t1_cont, "T1+C", midline))

        if panel_seqs:
            panel = engine.create_multi_sequence_panel(panel_seqs)
            if panel:
                job.annotated_images["multi_sequence_panel"] = panel

        # Contrast comparison
        if ax_vibe_pre and ax_vibe_post:
            seq_pre = inventory.sequences[ax_vibe_pre]
            num_slices = seq_pre.num_slices
            # Sample slices at ~60% and ~35% through the stack for L4-L5 and L5-S1
            l45_slice = int(num_slices * 0.55)
            l5s1_slice = int(num_slices * 0.35)

            try:
                c1 = engine.create_contrast_comparison(ax_vibe_pre, ax_vibe_post, l45_slice, "L4-L5")
                job.annotated_images["contrast_L4L5"] = c1
            except Exception as e:
                logger.warning(f"Could not create L4-L5 contrast comparison: {e}")

            try:
                c2 = engine.create_contrast_comparison(ax_vibe_pre, ax_vibe_post, l5s1_slice, "L5-S1")
                job.annotated_images["contrast_L5S1"] = c2
            except Exception as e:
                logger.warning(f"Could not create L5-S1 contrast comparison: {e}")

        # Export measurements
        job.measurements = engine.export_measurements_json()

        # Phase 4: Claude interpretation
        job.status = "interpreting"
        job.progress = 75
        job.progress_message = "Claude Opus 4.6 is analyzing findings..."

        interpreter = ClaudeInterpreter(api_key=api_key)

        # Prepare key images for Claude (up to 4 to manage token cost)
        key_images = {}
        for img_name in ["sag_t2_annotated", "level_reference", "multi_sequence_panel"]:
            if img_name in job.annotated_images:
                key_images[img_name] = engine.get_image_base64(job.annotated_images[img_name])

        request = InterpretationRequest(
            measurements_json=job.measurements,
            key_images_b64=key_images,
            clinical_history=clinical_history,
            surgical_notes=surgical_notes,
            prior_reports=prior_reports,
        )

        job.progress = 85
        job.progress_message = "Receiving clinical interpretation from Claude Opus 4.6..."
        interpretation = interpreter.interpret(request)
        job.interpretation = interpretation

        # Complete
        job.status = "complete"
        job.progress = 100
        job.progress_message = "Analysis complete"
        logger.info(f"Job {job.job_id} complete")

    except Exception as e:
        logger.exception(f"Analysis failed for job {job.job_id}")
        job.status = "error"
        job.error = str(e)
        job.progress_message = f"Error: {str(e)}"


def _find_sequence(
    inventory,
    keywords: list[str],
    plane: str = "",
    contrast: bool = False,
    exclude: Optional[list[str]] = None,
) -> Optional[str]:
    """Find a sequence by keyword matching in series description."""
    exclude = exclude or []
    for name, seq in inventory.sequences.items():
        desc = seq.series_description.lower()
        name_lower = name.lower()

        if plane and seq.plane != plane:
            continue
        if contrast and not seq.has_contrast:
            continue
        if not contrast and seq.has_contrast:
            continue

        if any(ex.lower() in name_lower for ex in exclude):
            continue

        if any(kw.lower() in desc or kw.lower() in name_lower for kw in keywords):
            return name

    return None


# ── Serve Frontend ──

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text())
    return HTMLResponse(content="<h1>SpineAI — Frontend not found</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

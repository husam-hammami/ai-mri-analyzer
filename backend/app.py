"""
MIKA — AI Medical MRI Analyzer — FastAPI Server
==================================================
Multi-anatomy AI-powered MRI analysis platform.
Supports: Spine, Brain, MSK, Cardiac, Chest, Abdomen/Pelvis,
          Breast, Vascular/MRA, Head & Neck, and Prostate studies.

Input Formats: DICOM (.dcm), NIfTI (.nii/.nii.gz), NRRD (.nrrd),
               Standard images (PNG/JPG/TIFF), ZIP archives.

Architecture:
  POST /api/upload          → Upload imaging files (any supported format)
  POST /api/analyze         → Run full analysis pipeline
  GET  /api/status/{job_id} → Check analysis progress (SSE)
  GET  /api/report/{job_id} → Get completed report
  GET  /api/images/{job_id}/{name} → Get annotated images
  GET  /                    → Serve React frontend
"""

import os
import re
import uuid
import json
import time
import math
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
from core.format_converter import FormatConverter
from services.claude_interpreter import (
    ClaudeInterpreter,
    InterpretationRequest,
    ClinicalInterpretation,
)
from services.batch_sender import BatchSender
from services.verification import VerificationPass
from services.agent_runner import AgentRunner
try:
    from backend.prompts.base_prompt import REPORT_DISCLAIMER
except ImportError:
    from prompts.base_prompt import REPORT_DISCLAIMER

# Human-readable descriptions for annotated proof figures (used for figure numbering
# and the figure inventory passed to the verification self-audit).
FIGURE_DESCRIPTIONS = {
    "level_reference": "Level Reference (sagittal midline, sacrum-up) — master key for vertebral levels",
    "sag_t2_annotated": "Annotated sagittal T2 — disc desiccation & canal assessment",
    "multi_sequence_panel": "Multi-sequence comparison panel",
    "contrast_L4L5": "Pre/post-contrast comparison at approximately L4-L5",
    "contrast_L5S1": "Pre/post-contrast comparison at approximately L5-S1",
}


def _build_figure_blocks(engine, annotated_images: dict):
    """
    Convert the job's annotated images into numbered Claude content blocks plus a
    figure inventory. The Level Reference is always FIGURE 0 (the skill's master key);
    remaining figures are numbered 1, 2, 3, ... in insertion order.
    """
    blocks, inventory = [], []
    ordered = []
    if "level_reference" in annotated_images:
        ordered.append(("level_reference", 0))
    next_n = 1
    for name in annotated_images:
        if name == "level_reference":
            continue
        ordered.append((name, next_n))
        next_n += 1

    for name, number in ordered:
        path = annotated_images[name]
        desc = FIGURE_DESCRIPTIONS.get(name, name)
        label = f"FIGURE {number} — {desc}"
        inventory.append({"figure": f"Figure {number}", "name": name, "description": desc})
        try:
            b64 = engine.get_image_base64(path)
            blocks.append({"type": "text", "text": f"\n=== {label} ===\n"})
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })
        except Exception as e:
            logger.warning(f"Could not encode figure {name}: {e}")
    return blocks, inventory

# ── Configuration ──

logging.basicConfig(level=getattr(logging, os.environ.get("MIKA_LOG_LEVEL", "INFO").upper(), logging.INFO))
logger = logging.getLogger("mika.api")


def _default_data_dir() -> Path:
    """Resolve the durable data directory where every study + report is kept.

    Reports must survive a server restart, a different working directory, and app updates,
    so we default to a STABLE per-user location (never the volatile CWD-relative './data'):
      • explicit override:  MIKA_DATA_DIR / SPINEAI_DATA_DIR
      • Windows:            %LOCALAPPDATA%\\MIKA\\data
      • macOS/Linux:        $XDG_DATA_HOME/MIKA/data  or  ~/.mika/data
    In the bundled desktop build this is set to Electron's userData/data.
    """
    override = os.environ.get("MIKA_DATA_DIR") or os.environ.get("SPINEAI_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "MIKA" / "data"
    return Path.home() / ".mika" / "data"


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"MIKA data directory: {DATA_DIR}")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# A job_id is uuid4()[:8] — validate before it ever touches a filesystem path (anti path-traversal).
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")
# Cap a single upload so a malicious/accidental client can't exhaust disk (medical studies are big,
# but 2 GB is a generous ceiling for one study). Overridable for large multi-series CT/PET.
MAX_UPLOAD_BYTES = int(os.environ.get("MIKA_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))

# ── Application ──

app = FastAPI(
    title="MIKA — AI Medical Imaging Analyzer",
    description="MIKA: Multi-modality, multi-anatomy AI imaging analysis — MR, CT, X-ray, ultrasound, mammography, PET across Spine, Brain, MSK, Cardiac, Chest, Abdomen, Breast, Vascular, Head & Neck, Prostate. Accepts DICOM, NIfTI, NRRD, PNG/JPG, ZIP. Runs on your Claude subscription.",
    version="3.0.0",
)

# MIKA serves its own frontend (same origin) and, in the desktop build, runs on localhost only.
# A wildcard origin + credentials is both invalid and an exfiltration risk for medical data, so we
# pin to an explicit allow-list (localhost by default; override with MIKA_ALLOWED_ORIGINS for a hosted
# deployment) and never reflect credentials cross-origin.
_origins_env = os.environ.get("MIKA_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()] or [
    "http://localhost:8000", "http://127.0.0.1:8000",
    "http://localhost:5173", "http://127.0.0.1:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def _csrf_origin_guard(request, call_next):
    """CORS stops a cross-origin page from READING our responses, but it does NOT stop the browser from
    SENDING a state-changing request (e.g. a hidden cross-site form POST to /api/connect could pop a
    Claude sign-in window). For mutating methods we therefore reject any request whose Origin is present
    and is neither same-origin (any localhost port — so the Electron random-port build still works) nor in
    the allow-list. Requests with no Origin (same-origin GETs, the desktop app's own calls, curl) pass."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin:
            try:
                from urllib.parse import urlparse
                same_origin = urlparse(origin).netloc == request.headers.get("host", "")
            except Exception:
                same_origin = False
            if not same_origin and origin not in ALLOWED_ORIGINS:
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    return await call_next(request)


# ── In-Memory Job Store ──

class AnalysisJob:
    def __init__(self, job_id: str, dicom_dir: str):
        self.job_id = job_id
        self.dicom_dir = dicom_dir
        self.work_dir = str(DATA_DIR / job_id / "work")
        self.status = "pending"  # pending, inventory, levels, measuring, interpreting, complete, error
        self.progress = 0  # 0-100
        self.progress_message = ""
        self.eta_seconds: Optional[int] = None      # live estimate of time remaining
        self.est_total_seconds: Optional[int] = None  # calibrated total estimate for this run
        self.engine: Optional[DICOMEngine] = None
        self.interpretation: Optional[ClinicalInterpretation] = None
        self.measurements: Optional[dict] = None
        self.annotated_images: dict = {}
        self.annotation_audit: list = []      # per-arrow 3C/3D audit from the engine
        self.verification: dict = {}          # 12-item audit + corrections + quality
        self.mode: str = "lite"               # "agent" | "lite"
        self.active_sequence: Optional[str] = None  # live "now reading X", when the agent reports it
        self.active_region: Optional[str] = None     # live "now inspecting <level/region>", when reported
        self.agent: dict = {}                 # agent-mode result (pdf path, figures, summary)
        self.pdf_path: Optional[str] = None   # server-side path to the generated PDF (not exposed to client)
        self.cancelled: bool = False          # user requested cancel — honored by the agent loop + completion
        self.error: Optional[str] = None
        self.created_at = datetime.utcnow().isoformat()

JOBS: dict[str, AnalysisJob] = {}


# ── Durable persistence (so reports & images never "disappear" on restart) ──
#
# Each job owns a folder DATA_DIR/<job_id>/ holding its uploads, work files, and — once complete —
# two manifests written to disk:
#   report.json : the exact payload GET /api/report returns (served verbatim after a restart)
#   meta.json   : a small index entry (status, anatomy, title, date, thumb, image map, pdf path)
# The in-memory JOBS dict is only a hot cache of the live run; DISK is the source of truth, so a
# completed study is always retrievable by job_id even after the process restarts or the cache is gone.

def _job_dir(job_id: str) -> Path:
    return DATA_DIR / job_id


def _validate_job_id(job_id: str) -> str:
    """Reject anything that isn't our uuid4()[:8] shape before it touches a filesystem path."""
    if not job_id or not JOB_ID_RE.match(job_id):
        raise HTTPException(404, "Job not found")
    return job_id


def _rel_to_job(job_id: str, path: str) -> str:
    """Path relative to the job dir, stored with FORWARD slashes so the manifest is portable across
    OSes (a Windows-written 'work\\report\\x.png' would be a single literal filename on POSIX)."""
    try:
        return Path(path).resolve().relative_to(_job_dir(job_id).resolve()).as_posix()
    except Exception:
        return path


def _safe_join(job_id: str, rel: str) -> Optional[Path]:
    """Resolve a manifest-relative path and guarantee it points to a FILE strictly inside the job dir
    (anti-traversal). Normalizes any backslashes so manifests written on Windows resolve on POSIX too."""
    base = _job_dir(job_id).resolve()
    try:
        p = (base / str(rel).replace("\\", "/")).resolve()
    except Exception:
        return None
    # Strict descendant + regular file: never hand back the job dir itself or a directory.
    if base in p.parents and p.is_file():
        return p
    return None


def _persist_report(job: "AnalysisJob") -> None:
    """Write report.json + meta.json for a completed job so it survives a restart. Non-fatal."""
    try:
        jd = _job_dir(job.job_id)
        jd.mkdir(parents=True, exist_ok=True)
        payload = _build_report_payload(job)
        (jd / "report.json").write_text(json.dumps(payload, default=str), encoding="utf-8")

        images = {name: _rel_to_job(job.job_id, p) for name, p in (job.annotated_images or {}).items()}
        # Cover thumbnail for the Recent list: prefer a real study slice, else the first figure.
        thumb = next((n for n in images if n.startswith("seqthumb")), None) or next(iter(images), None)
        m = job.measurements or {}
        meta = {
            "job_id": job.job_id,
            "status": job.status,
            "mode": job.mode,
            "created_at": job.created_at,
            "completed_at": datetime.utcnow().isoformat(),
            "detected_anatomy": m.get("detected_anatomy", "unknown"),
            "anatomy_subregion": m.get("anatomy_subregion", ""),
            "modality": m.get("modality", ""),
            "title": payload.get("study_description") or m.get("study_description") or "",
            "thumb": thumb,
            "images": images,
            "pdf": _rel_to_job(job.job_id, job.pdf_path) if job.pdf_path else None,
            "pdf_available": bool(job.agent.get("pdf_available")) if job.agent else False,
        }
        (jd / "meta.json").write_text(json.dumps(meta, default=str), encoding="utf-8")
        logger.info(f"Persisted report for job {job.job_id} ({len(images)} images)")
    except Exception as e:
        logger.warning(f"Could not persist report for job {job.job_id}: {e}")


def _load_meta(job_id: str) -> Optional[dict]:
    try:
        p = _job_dir(job_id) / "meta.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8-sig"))  # tolerate a BOM from any writer
    except Exception as e:
        logger.warning(f"Could not read meta for {job_id}: {e}")
    return None


def _load_report(job_id: str) -> Optional[dict]:
    try:
        p = _job_dir(job_id) / "report.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8-sig"))  # tolerate a BOM from any writer
    except Exception as e:
        logger.warning(f"Could not read report for {job_id}: {e}")
    return None


def _list_reports() -> list[dict]:
    """Index every completed study on disk for the Recent list (durable, not browser-only)."""
    out = []
    try:
        for d in DATA_DIR.iterdir():
            if not d.is_dir() or not JOB_ID_RE.match(d.name):
                continue
            meta = _load_meta(d.name)
            if not meta:
                continue
            out.append({
                "job_id": meta.get("job_id", d.name),
                "status": meta.get("status", "complete"),
                "title": meta.get("title", ""),
                "detected_anatomy": meta.get("detected_anatomy", "unknown"),
                "anatomy_subregion": meta.get("anatomy_subregion", ""),
                "modality": meta.get("modality", ""),
                "created_at": meta.get("created_at", ""),
                "completed_at": meta.get("completed_at", ""),
                "thumb": meta.get("thumb"),
                "pdf_available": meta.get("pdf_available", False),
            })
    except Exception as e:
        logger.warning(f"Could not list reports: {e}")
    out.sort(key=lambda r: r.get("completed_at") or r.get("created_at") or "", reverse=True)
    return out


# ── Request/Response Models ──

class AnalyzeRequest(BaseModel):
    job_id: str
    mode: str = "agent"   # "agent" = run the skill via Claude Code (subscription, definitive
                          # PDF report) | "lite" = fast structured-JSON pipeline (needs a key/token)
    api_key: Optional[str] = None
    auth_token: Optional[str] = None  # bearer/OAuth token (e.g. subscription via Claude Code)
    clinical_history: Optional[str] = None
    surgical_notes: Optional[str] = None
    prior_reports: Optional[str] = None
    notify_email: Optional[str] = None   # §7.7: opt-in "we'll email you when it's ready" (survives tab close)


class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: int
    progress_message: str
    created_at: str
    error: Optional[str] = None
    eta_seconds: Optional[int] = None
    est_total_seconds: Optional[int] = None
    active_sequence: Optional[str] = None
    active_region: Optional[str] = None


class ReportResponse(BaseModel):
    job_id: str
    demographics: dict
    measurements: dict
    interpretation: dict
    annotated_images: list
    calibration_status: str
    detected_anatomy: str


# ── API Endpoints ──

SUPPORTED_EXTENSIONS = {
    ".dcm", ".DCM", ".ima", ".dicom",             # DICOM
    ".nii", ".gz",                                  # NIfTI (.nii, .nii.gz)
    ".nrrd", ".nhdr",                               # NRRD
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",  # Images
    ".zip",                                         # Archives
}


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    """
    Upload medical imaging files and create a new analysis job.
    Supports: DICOM (.dcm), NIfTI (.nii/.nii.gz), NRRD (.nrrd),
              images (PNG/JPG/TIFF), and ZIP archives containing any of these.
    """
    job_id = str(uuid.uuid4())[:8]
    upload_dir = DATA_DIR / job_id / "upload"
    dicom_dir = DATA_DIR / job_id / "dicom"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dicom_dir.mkdir(parents=True, exist_ok=True)

    file_count = 0
    has_non_dicom = False
    total_bytes = 0

    for file in files:
        if not file.filename:
            continue

        # Sanitize: strip any directory components and unsafe characters so a crafted filename
        # (e.g. "..\\..\\evil.dcm") can never escape the upload directory on write.
        raw = os.path.basename(file.filename.replace("\\", "/"))
        fname = re.sub(r"[^A-Za-z0-9_.\-]", "_", raw) or "file"
        fname_lower = fname.lower()
        ext = Path(fname_lower).suffix

        # Handle .nii.gz double extension
        is_nifti_gz = fname_lower.endswith(".nii.gz")

        is_supported = ext in SUPPORTED_EXTENSIONS or is_nifti_gz
        # Also accept extensionless files (potential DICOM from PACS)
        if not is_supported and ext == "":
            is_supported = True

        if not is_supported:
            continue

        is_dicom = ext in {".dcm", ".ima", ".dicom"} or ext == ""

        # Stream to disk in 1 MB chunks so a single huge file is never buffered whole in RAM, and the
        # size cap is enforced DURING the read (not after the entire body is in memory).
        dest_dir = dicom_dir if is_dicom else upload_dir
        dest = dest_dir / fname
        with open(str(dest), "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    f.close()
                    shutil.rmtree(str(DATA_DIR / job_id), ignore_errors=True)
                    raise HTTPException(
                        413, f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per-study limit."
                    )
                f.write(chunk)
        file_count += 1

        if not is_dicom:
            has_non_dicom = True

    if file_count == 0:
        shutil.rmtree(str(DATA_DIR / job_id))
        raise HTTPException(
            400,
            "No supported files found. Upload DICOM (.dcm), NIfTI (.nii/.nii.gz), "
            "NRRD (.nrrd), images (PNG/JPG/TIFF), or ZIP archives.",
        )

    # If we have non-DICOM files, run the format converter
    input_format = "dicom"
    conversion_warnings = []
    if has_non_dicom:
        converter = FormatConverter(str(upload_dir), str(dicom_dir))
        result = converter.convert()
        input_format = result.input_format
        conversion_warnings = result.warnings

        if not result.success:
            shutil.rmtree(str(DATA_DIR / job_id))
            raise HTTPException(400, f"Format conversion failed: {result.error}")

        file_count = result.num_files
        logger.info(f"Format conversion: {result.input_format} -> DICOM, {result.num_slices} slices")

    job = AnalysisJob(job_id=job_id, dicom_dir=str(dicom_dir))
    JOBS[job_id] = job

    logger.info(f"Upload complete: job={job_id}, format={input_format}, files={file_count}")
    response = {"job_id": job_id, "file_count": file_count, "input_format": input_format}
    if conversion_warnings:
        response["warnings"] = conversion_warnings
    return response


@app.get("/api/agent/availability")
async def agent_availability():
    """Report whether the app is connected to Claude (real `claude auth status` check)."""
    return AgentRunner().availability()


@app.post("/api/connect")
async def connect_claude(console: bool = False):
    """
    'Connect with Claude' button: launch the browser sign-in to the user's own Claude
    account (desktop/EXE build). The UI then polls /api/agent/availability until connected.
    """
    from services.agent_runner import trigger_claude_login
    return trigger_claude_login(console=console)


@app.post("/api/analyze")
async def start_analysis(request: AnalyzeRequest, background_tasks: BackgroundTasks):
    """Start the analysis pipeline in the background (agent mode by default)."""
    _validate_job_id(request.job_id)
    job = JOBS.get(request.job_id)
    if not job:
        raise HTTPException(404, f"Job {request.job_id} not found")

    if job.status not in ("pending", "error"):
        raise HTTPException(400, f"Job is already {job.status}")

    mode = (request.mode or "agent").lower()
    job.mode = mode

    if mode == "agent":
        # Agent mode runs the skill via Claude Code on your subscription — no API key needed.
        avail = AgentRunner().availability()
        if not avail.get("claude_cli_found"):
            raise HTTPException(
                400,
                "Agent mode needs the Claude Code CLI installed and logged in on your "
                "subscription. Install it (npm i -g @anthropic-ai/claude-code), run "
                "`claude` once to log in, then retry — or use mode='lite' with an API key.",
            )
        job.status = "inventory"
        job.progress = 0
        background_tasks.add_task(
            _run_agent_pipeline,
            job=job,
            api_key=request.api_key or "",          # per-user credential from sign-in; else host login
            auth_token=request.auth_token or "",
            clinical_history=request.clinical_history,
            surgical_notes=request.surgical_notes,
            prior_reports=request.prior_reports,
            notify_email=request.notify_email,
        )
        return {"job_id": job.job_id, "status": "started", "mode": "agent"}

    # ── lite mode (fast structured-JSON pipeline) ──
    api_key = request.api_key or ANTHROPIC_API_KEY
    auth_token = request.auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    has_env_profile = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not (api_key or auth_token or has_env_profile):
        raise HTTPException(
            400,
            "lite mode needs an Anthropic credential. Provide api_key or auth_token, or "
            "set ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN — or use mode='agent' to run on "
            "your Claude subscription with no API key.",
        )

    job.status = "inventory"
    job.progress = 0
    background_tasks.add_task(
        _run_analysis_pipeline,
        job=job,
        api_key=api_key,
        auth_token=auth_token,
        clinical_history=request.clinical_history,
        surgical_notes=request.surgical_notes,
        prior_reports=request.prior_reports,
    )
    return {"job_id": job.job_id, "status": "started", "mode": "lite"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    """Get current analysis status (poll or SSE)."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        # Restart / cache miss: a finished study still lives on disk — report it complete so the
        # frontend resumes straight to the Read instead of showing "job not found".
        meta = _load_meta(job_id)
        if meta:
            st = meta.get("status", "complete")
            return JobStatus(
                job_id=job_id, status=st, progress=100 if st == "complete" else 0,
                progress_message="Report ready" if st == "complete" else (meta.get("error") or ""),
                created_at=meta.get("created_at", ""), error=meta.get("error"),
            )
        raise HTTPException(404, f"Job {job_id} not found")

    return JobStatus(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        progress_message=job.progress_message,
        created_at=job.created_at,
        error=job.error,
        eta_seconds=job.eta_seconds,
        est_total_seconds=job.est_total_seconds,
        active_sequence=job.active_sequence,
        active_region=job.active_region,
    )


@app.get("/api/status/{job_id}/stream")
async def stream_status(job_id: str):
    """Server-Sent Events stream for real-time progress updates."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        # No live run: if a finished report exists on disk, emit one terminal event and close.
        meta = _load_meta(job_id)
        if meta:
            st = meta.get("status", "complete")
            async def _one():
                yield f"data: {json.dumps({'status': st, 'progress': 100 if st == 'complete' else 0, 'message': 'Report ready', 'error': meta.get('error')})}\n\n"
            return StreamingResponse(_one(), media_type="text/event-stream")
        raise HTTPException(404, f"Job {job_id} not found")

    async def event_generator():
        last = None
        since_ping = 0.0
        while True:
            snapshot = (job.progress, job.eta_seconds, job.status, job.active_sequence, job.active_region, job.progress_message)
            if snapshot != last or job.status in ("complete", "error"):
                data = json.dumps({
                    "status": job.status,
                    "progress": job.progress,
                    "message": job.progress_message,
                    "error": job.error,
                    "eta_seconds": job.eta_seconds,
                    "est_total_seconds": job.est_total_seconds,
                    "active_sequence": job.active_sequence,
                    "active_region": job.active_region,
                })
                yield f"data: {data}\n\n"
                last = snapshot
                since_ping = 0.0

            if job.status in ("complete", "error"):
                break
            await asyncio.sleep(1.0)
            # §7.8 keepalive: emit an SSE comment every ~15s so idle proxies don't drop the stream
            since_ping += 1.0
            if since_ping >= 15.0:
                yield ": ping\n\n"
                since_ping = 0.0

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _build_report_payload(job: "AnalysisJob") -> dict:
    """Assemble the full report payload for a completed job. Used by GET /api/report and by
    _persist_report (which writes it to disk so the same payload is served after a restart)."""
    interpretation_dict = {}
    if job.interpretation:
        interp = job.interpretation
        interpretation_dict = {
            "anatomy_type": interp.anatomy_type,
            # Spine
            "findings_by_level": interp.findings_by_level,
            "alignment": interp.alignment,
            "conus": interp.conus,
            "post_surgical_assessment": interp.post_surgical_assessment,
            # Brain / Chest / Head-Neck / Breast / Generic region-based
            "findings_by_region": interp.findings_by_region,
            "enhancement_pattern": interp.enhancement_pattern,
            "diffusion_findings": interp.diffusion_findings,
            # MSK / Cardiac structure-based
            "findings_by_structure": interp.findings_by_structure,
            "joint_effusion": interp.joint_effusion,
            "bone_marrow": interp.bone_marrow,
            "wall_motion": interp.wall_motion,
            "tissue_characterization": interp.tissue_characterization,
            # Abdomen organ-based
            "findings_by_organ": interp.findings_by_organ,
            # Vascular vessel-based
            "findings_by_vessel": interp.findings_by_vessel,
            "vascular_territory": interp.vascular_territory,
            "flow_assessment": interp.flow_assessment,
            # Prostate zone-based
            "findings_by_zone": interp.findings_by_zone,
            "dominant_lesion": interp.dominant_lesion,
            "pirads_category": interp.pirads_category,
            "extraprostatic_extension": interp.extraprostatic_extension,
            # Breast-specific
            "background_parenchymal_enhancement": interp.background_parenchymal_enhancement,
            "kinetic_assessment": interp.kinetic_assessment,
            "birads_category": interp.birads_category,
            # Head-Neck-specific
            "cranial_nerves": interp.cranial_nerves,
            # Generic
            "identified_anatomy": interp.identified_anatomy,
            # Shared
            "incidentals": interp.incidentals,
            "discrepancies": interp.discrepancies,
            "impression": interp.impression,
            "confidence_summary": interp.confidence_summary,
            "model_used": interp.model_used,
            "tokens": {
                "input": interp.input_tokens,
                "output": interp.output_tokens,
            },
        }

    detected_anatomy = (
        job.measurements.get("detected_anatomy", "unknown")
        if job.measurements else "unknown"
    )
    anatomy_subregion = (
        job.measurements.get("anatomy_subregion", "")
        if job.measurements else ""
    )

    # Map each finding's [See Figure N] to a real image so the report stays traceable.
    figures = []
    fig_n = 0
    if "level_reference" in job.annotated_images:
        figures.append({"figure": 0, "name": "level_reference",
                        "description": FIGURE_DESCRIPTIONS.get("level_reference", "")})
    for name in job.annotated_images:
        if name == "level_reference":
            continue
        fig_n += 1
        figures.append({"figure": fig_n, "name": name,
                        "description": FIGURE_DESCRIPTIONS.get(name, name)})

    return {
        "job_id": job.job_id,
        "demographics": job.measurements.get("demographics", {}) if job.measurements else {},
        "study_description": job.measurements.get("study_description", "") if job.measurements else "",
        "measurements": job.measurements or {},
        "interpretation": interpretation_dict,
        "annotated_images": list(job.annotated_images.keys()),
        "figures": figures,
        "verification": job.verification or {"status": "not_run"},
        "calibration_status": (
            job.measurements.get("calibration_status", "unknown")
            if job.measurements else "unknown"
        ),
        "detected_anatomy": detected_anatomy,
        "anatomy_subregion": anatomy_subregion,
        "disclaimer": REPORT_DISCLAIMER,
        "mode": job.mode,
        "agent": job.agent,
        "pdf_available": bool(job.agent.get("pdf_available")),
    }


@app.get("/api/report/{job_id}")
async def get_report(job_id: str):
    """Get the completed analysis report — from the live job, or rehydrated from disk after a restart
    so a finished study is never lost just because the server bounced."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if job:
        if job.status != "complete":
            raise HTTPException(400, f"Analysis not complete (status: {job.status})")
        return _build_report_payload(job)
    disk = _load_report(job_id)
    if disk:
        return disk
    raise HTTPException(404, "Report not found")


@app.get("/api/reports")
async def list_reports():
    """Durable index of all completed studies on this machine (backs the 'Recent studies' screen).
    Reads DATA_DIR manifests — survives restarts and browser-storage clears."""
    return {"reports": _list_reports()}


@app.get("/api/report/{job_id}/pdf")
async def get_report_pdf(job_id: str):
    """Serve the agent-generated PDF report (agent mode), from the live job or from disk."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        # Restart / cache miss: serve the durable PDF recorded in the on-disk manifest.
        meta = _load_meta(job_id)
        if meta and meta.get("pdf_available") and meta.get("pdf"):
            p = _safe_join(job_id, meta["pdf"])
            if p:
                return FileResponse(str(p), media_type="application/pdf", filename="mika_report.pdf")
        raise HTTPException(404, "No PDF report available for this job")
    # Only serve a PDF the CURRENT run actually produced — never a stale file from a prior run of the
    # same job_id. Gate on the live result, then use the authoritative path recorded for this run. (BACKEND-3)
    if job.status != "complete" or not (job.agent and job.agent.get("pdf_available")):
        raise HTTPException(404, "No PDF report available for this job")
    pdf_path = job.pdf_path
    if not pdf_path or not os.path.exists(pdf_path):
        # Fallback only if the in-flight path wasn't recorded (older job): recover from the report dir.
        candidate = Path(job.work_dir) / "report"
        pdfs = sorted(candidate.glob("*.pdf")) if candidate.exists() else []
        pdf_path = str(pdfs[0]) if pdfs else None
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(404, "No PDF report available for this job")
    # Never serve a file outside this job's own directory.
    if not _safe_join(job_id, _rel_to_job(job_id, pdf_path)):
        raise HTTPException(404, "No PDF report available for this job")
    return FileResponse(pdf_path, media_type="application/pdf", filename="mika_report.pdf")


def _safe_image_name(image_name: str) -> bool:
    """The {image_name} segment is only ever used as a lookup KEY into a server-built map (live dict or
    the on-disk manifest), and the resolved path is re-confined by _safe_join — so we don't need a tight
    charset (the agent names its own figures, sometimes with spaces/parens/non-ASCII). We only reject the
    few things that could form a traversal: separators, '..', empties, and absurd lengths."""
    if not image_name or len(image_name) > 200:
        return False
    if "/" in image_name or "\\" in image_name or ".." in image_name or "\x00" in image_name:
        return False
    return True


@app.get("/api/images/{job_id}/{image_name}")
async def get_image(job_id: str, image_name: str):
    """Serve an annotated proof image — from the live job, or rehydrated from the on-disk manifest
    after a restart. Every path is resolved and confirmed to stay inside the job directory."""
    _validate_job_id(job_id)
    if not _safe_image_name(image_name):
        raise HTTPException(404, "Image not found")
    job = JOBS.get(job_id)
    image_path = job.annotated_images.get(image_name) if job else None
    if not image_path:
        # Restart / cache miss: resolve from the durable manifest (relative path inside the job dir).
        rel = (_load_meta(job_id) or {}).get("images", {}).get(image_name)
        if rel:
            p = _safe_join(job_id, rel)
            image_path = str(p) if p else None
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(404, "Image not found")
    safe = _safe_join(job_id, _rel_to_job(job_id, image_path))
    if not safe:
        raise HTTPException(404, "Image not found")
    low = str(safe).lower()
    media = "image/jpeg" if low.endswith((".jpg", ".jpeg")) else "image/webp" if low.endswith(".webp") else "image/png"
    return FileResponse(str(safe), media_type=media)


@app.get("/api/study/{job_id}/sequences")
async def get_sequences(job_id: str):
    """Detected anatomy + sequence catalog for the Wait panel (§7.4).
    Populated by the §7.3 inventory pre-step (agent) or the lite inventory; never fabricated."""
    if not JOB_ID_RE.match(job_id or ""):
        return {"sequences": []}
    job = JOBS.get(job_id)
    if job:
        m = job.measurements or {}
        return {
            "anatomy": m.get("detected_anatomy", "unknown"),
            "anatomy_subregion": m.get("anatomy_subregion", ""),
            "modality": m.get("modality", "MR"),
            "sequences": m.get("sequence_catalog", []),
        }
    meta = _load_meta(job_id) or {}
    return {
        "anatomy": meta.get("detected_anatomy", "unknown"),
        "anatomy_subregion": meta.get("anatomy_subregion", ""),
        "modality": meta.get("modality", "MR"),
        "sequences": [],
    }


class EventIn(BaseModel):
    job_id: Optional[str] = None
    event: str
    detail: Optional[str] = None


@app.post("/api/event")
async def log_event(ev: EventIn):
    """Lightweight, PHI-free analytics sink (§7.9): job_id + event name only.
    No third-party SDK; just structured server logs for funnel/abandonment questions."""
    logger.info(f"event={ev.event} job={ev.job_id or '-'} detail={(ev.detail or '')[:80]}")
    return {"ok": True}


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Best-effort cancel for a wrong-study run (§7.9). The agent runs in a worker thread and
    cannot be force-killed cleanly mid-subprocess, so we mark intent; an early/pending job stops
    immediately. The credit for an in-flight agent run may still be spent — surfaced to the client."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    if job.status in ("complete", "error"):
        return {"job_id": job_id, "status": job.status, "cancelled": False, "note": "already finished"}
    job.cancelled = True   # honored by _run_agent_pipeline's loop + completion guard so a late success can't revert it
    job.status = "error"
    job.error = "Cancelled by user"
    job.progress_message = "Cancelled"
    return {"job_id": job_id, "status": "error", "cancelled": True,
            "note": "Marked cancelled. An in-flight subscription run may still consume its credit."}


# ── Analysis Pipeline ──

# Calibrated from measured runs (~34 min for a 3-study high-effort run). Used to drive the
# live time-remaining estimate; the bar only completes when the agent actually returns.
_EFFORT_FACTOR = {"low": 0.5, "medium": 0.7, "high": 1.0, "xhigh": 1.3, "max": 1.6}


def _estimate_agent_seconds(n_studies: int = 1, effort: str = "high") -> int:
    base = 900 + 400 * max(1, n_studies)
    est = base * _EFFORT_FACTOR.get(effort, 1.0)
    return int(min(max(est, 600), 5400))


def _slice_thumbnails(file_list, dest_dir: str, key: str, max_n: int = 5, width: int = 360):
    """Convert a few evenly-spaced slices of a sequence into small grayscale PNG thumbnails for the
    Wait viewer (real study images, percentile-windowed). Returns [(stem, path)]. Best-effort: any
    failure yields fewer/zero thumbnails and the viewer simply falls back — never fabricated."""
    files = [f for f in (file_list or []) if f]
    if not files:
        return []
    try:
        import numpy as np
        import pydicom
        from PIL import Image
    except Exception:
        return []
    n = len(files)
    idxs = sorted({int(i * (n - 1) / (max_n - 1)) for i in range(max_n)}) if n >= max_n else list(range(n))
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    out = []
    for j, i in enumerate(idxs):
        try:
            ds = pydicom.dcmread(str(files[i]), force=True)
            a = ds.pixel_array.astype("float32")
            if a.ndim > 2:                      # multi-frame / RGB → take a representative 2-D plane
                a = a[a.shape[0] // 2] if a.ndim == 3 and a.shape[2] not in (3, 4) else a.mean(axis=-1)
            lo, hi = np.percentile(a, 1), np.percentile(a, 99)
            a = np.clip((a - lo) / (hi - lo + 1e-6), 0, 1) * 255.0
            im = Image.fromarray(a.astype("uint8")).convert("L")
            w, h = im.size
            if w > width:
                im = im.resize((width, int(h * width / w)), Image.LANCZOS)
            stem = f"seqthumb_{key}_{j}"
            p = Path(dest_dir) / f"{stem}.png"
            im.save(str(p))
            out.append((stem, str(p)))
        except Exception:
            continue
    return out


def _notify_email(email: Optional[str], job_id: str, status: str) -> None:
    """§7.7: best-effort 'your read is ready' email with a resume deep-link. Sends via SMTP only
    if MIKA_SMTP_HOST is configured; otherwise logs intent. Non-fatal — never breaks the pipeline.
    Email is stored only transiently for this one notification (medical-context, opt-in only)."""
    if not email:
        return
    base = os.environ.get("MIKA_PUBLIC_URL", "").rstrip("/")
    link = f"{base}/?job_id={job_id}" if base else f"(open MIKA and your study {job_id})"
    subject = "Your MIKA read is ready" if status == "complete" else "Your MIKA study needs attention"
    body = (f"Your imaging read is ready. Open it here: {link}\n\nThis is an automated message from MIKA."
            if status == "complete" else
            f"We hit a problem analyzing your study. Please reopen MIKA to retry: {link}")
    host = os.environ.get("MIKA_SMTP_HOST")
    if not host:
        logger.info(f"[notify] would email {email}: {subject} ({link})")
        return
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = os.environ.get("MIKA_SMTP_FROM", "no-reply@mika.local")
        msg["To"] = email
        msg["Subject"] = subject
        msg.set_content(body)
        port = int(os.environ.get("MIKA_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            user, pw = os.environ.get("MIKA_SMTP_USER"), os.environ.get("MIKA_SMTP_PASS")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        logger.info(f"[notify] emailed {email} ({status})")
    except Exception as e:
        logger.warning(f"[notify] email failed for {email}: {e}")


async def _run_agent_pipeline(
    job: AnalysisJob,
    api_key: str = "",
    auth_token: str = "",
    clinical_history: Optional[str] = None,
    surgical_notes: Optional[str] = None,
    prior_reports: Optional[str] = None,
    notify_email: Optional[str] = None,
):
    """
    Agent mode: run the mri-spine-analysis skill via Claude Code (your subscription),
    exactly the way cowork produced the definitive PDF report.
    """
    try:
        # Study root holds the original uploaded files (dicom/ and upload/).
        study_root = str(Path(job.dicom_dir).parent)

        # ── §7.3 Mandatory synchronous inventory pre-step ──
        # Detect anatomy + catalog sequences BEFORE the opaque agent subprocess so the Wait
        # can show the real region (not a hardcoded "spine") and a truthful sequence list.
        # This runs UPSTREAM of measure_all_discs — the spine measurement pipeline is untouched.
        # Honest default: if inventory fails we don't know the region — 'unknown' degrades the Wait
        # to the whole-figure glow rather than mislabeling a non-spine study as spine (BACKEND-6).
        detected_anatomy = "unknown"
        anatomy_subregion = ""
        try:
            pre_engine = DICOMEngine(job.dicom_dir, job.work_dir)
            inv = pre_engine.run_inventory()
            detected_anatomy = inv.detected_anatomy or "unknown"
            anatomy_subregion = getattr(inv, "anatomy_subregion", "") or ""
            seq_catalog = []
            thumb_dir = str(Path(job.work_dir) / "seqthumbs")
            for nm, s in (inv.sequences or {}).items():
                key = ("".join(c for c in nm if c.isalnum())[:24]) or f"seq{len(seq_catalog)}"
                thumbs = _slice_thumbnails(getattr(s, "file_list", []), thumb_dir, key)
                for stem, path in thumbs:
                    job.annotated_images[stem] = path     # served via /api/images/{job}/{stem}
                seq_catalog.append({
                    "name": (getattr(s, "series_description", "") or nm),
                    "plane": getattr(s, "plane", "") or "",
                    "num_slices": getattr(s, "num_slices", None),
                    "slice_stems": [stem for stem, _ in thumbs],   # real per-sequence slice thumbnails
                })
            try:
                from services.agent_runner import detect_study_modality
                detected_modality = detect_study_modality(job.dicom_dir)
            except Exception:
                detected_modality = "MR"
            job.measurements = {
                "detected_anatomy": detected_anatomy,
                "anatomy_subregion": anatomy_subregion,
                "modality": detected_modality,
                "sequence_catalog": seq_catalog,
                # StudyInventory exposes is_calibrated (bool), not calibration_status — derive the label.
                "calibration_status": "DICOM-calibrated" if getattr(inv, "is_calibrated", False) else "UNCALIBRATED",
            }
            logger.info(f"Inventory pre-step: anatomy={detected_anatomy}{'/'+anatomy_subregion if anatomy_subregion else ''}, sequences={len(seq_catalog)}")
        except Exception as e:
            logger.warning(f"Inventory pre-step failed (Wait degrades to whole-figure glow): {e}")

        runner = AgentRunner(api_key=api_key, auth_token=auth_token)
        est = _estimate_agent_seconds(n_studies=1, effort=runner.effort)
        job.est_total_seconds = est
        job.eta_seconds = est
        job.status = "interpreting"
        job.progress = 8
        job.progress_message = "Launching analysis on your subscription..."

        # Run the blocking CLI off the event loop, and tick a live, honest ETA while it runs:
        # the bar advances asymptotically toward 95% over the calibrated estimate and only
        # completes when the agent actually returns (so it never lies about being done).
        agent_task = asyncio.ensure_future(asyncio.to_thread(
            runner.run,
            study_dir=study_root,
            work_dir=job.work_dir,
            clinical_history=clinical_history,
            surgical_notes=surgical_notes,
            prior_reports=prior_reports,
        ))
        start = time.monotonic()
        while True:
            done, _ = await asyncio.wait({agent_task}, timeout=2.0)
            elapsed = time.monotonic() - start
            job.eta_seconds = max(0, int(est - elapsed))
            frac = 1.0 - math.exp(-elapsed / (est * 0.55))   # asymptotic, never reaches 1
            job.progress = min(95, 8 + int(frac * 87))
            # Honest live readout: if the agent has written progress.json (see _build_prompt),
            # surface its real per-step note + current sequence; otherwise leave the message blank
            # (the ETA + phase stepper carry it) — never invent a per-slice position.
            note, active_seq, active_reg = None, None, None
            try:
                pf = Path(job.work_dir) / "progress.json"
                if pf.exists():
                    pj = json.loads(pf.read_text(encoding="utf-8"))
                    note = (pj.get("note") or "").strip() or None
                    active_seq = (pj.get("active_sequence") or "").strip() or None
                    active_reg = (pj.get("region") or "").strip() or None
            except Exception:
                pass
            if not job.cancelled:
                job.active_sequence = active_seq
                job.active_region = active_reg
                job.progress_message = note or ("Wrapping up…" if job.eta_seconds == 0 else "")
            if done or job.cancelled:
                break
        # User cancelled mid-run: leave the 'error/Cancelled' state intact and do NOT overwrite it
        # with a late success or fire a "ready" email. The subprocess can't be force-killed, so its
        # work may still finish in the background, but we discard the result. (BACKEND-2)
        if job.cancelled:
            logger.info(f"Agent job {job.job_id} was cancelled — discarding any late result")
            return
        result = agent_task.result()
        job.eta_seconds = 0

        job.progress = 96
        # Expose the agent's figures via the existing image route.
        for p in result.figures:
            job.annotated_images[Path(p).stem] = p

        job.pdf_path = result.pdf_path or None   # authoritative path for this run (BACKEND-3); not exposed to client
        job.agent = {
            "success": result.success,
            "pdf_available": bool(result.pdf_path),
            "figures": [Path(p).stem for p in result.figures],
            "summary": result.summary,
            "result_text": result.result_text,
            "num_turns": result.num_turns,
            "cost_usd": result.cost_usd,
            "error": result.error,
        }
        # Best-effort: surface the agent's summary in the standard report fields.
        # Preserve the anatomy + sequence catalog detected by the §7.3 pre-step.
        pre = job.measurements or {}
        job.measurements = {
            "detected_anatomy": pre.get("detected_anatomy", detected_anatomy),
            "anatomy_subregion": pre.get("anatomy_subregion", anatomy_subregion),
            "modality": pre.get("modality", "MR"),
            "sequence_catalog": pre.get("sequence_catalog", []),
            "calibration_status": (result.summary or {}).get("calibration_status", pre.get("calibration_status", "unknown")),
            "study_description": (result.summary or {}).get("study_description", ""),
            "agent_summary": result.summary,
        }

        if result.success:
            job.status = "complete"
            job.progress = 100
            job.progress_message = "Agent analysis complete — PDF report ready"
            logger.info(f"Agent job {job.job_id} complete: pdf ready, {len(result.figures)} figures")
            _persist_report(job)   # durable: survives restart, indexes into Recent studies
            _notify_email(notify_email, job.job_id, "complete")
        else:
            job.status = "error"
            job.error = result.error or "Agent run failed without producing a report"
            job.progress_message = f"Agent error: {job.error}"
            _notify_email(notify_email, job.job_id, "error")
    except Exception as e:
        logger.exception(f"Agent pipeline failed for job {job.job_id}")
        job.status = "error"
        job.error = str(e)
        job.progress_message = f"Error: {str(e)}"
        _notify_email(notify_email, job.job_id, "error")


async def _run_analysis_pipeline(
    job: AnalysisJob,
    api_key: str,
    auth_token: str = "",
    clinical_history: Optional[str] = None,
    surgical_notes: Optional[str] = None,
    prior_reports: Optional[str] = None,
):
    """Execute the full analysis pipeline in the background."""
    try:
        # Rough total for the time-remaining bar (lite is image+API bound, ~3 min).
        # The frontend derives remaining from progress when eta_seconds is not set live.
        job.est_total_seconds = 180
        engine = DICOMEngine(job.dicom_dir, job.work_dir)
        job.engine = engine

        # Phase 0: Inventory & Anatomy Detection
        job.status = "inventory"
        job.progress = 5
        job.progress_message = "Cataloging DICOM files and detecting anatomy type..."
        inventory = engine.run_inventory()
        detected_anatomy = inventory.detected_anatomy
        try:
            from services.agent_runner import detect_study_modality
            detected_modality = detect_study_modality(job.dicom_dir)
        except Exception:
            detected_modality = "MR"
        logger.info(f"Detected anatomy: {detected_anatomy}, modality: {detected_modality}")
        job.progress_message = f"Detected {detected_anatomy.upper() if detected_anatomy != 'unknown' else 'GENERAL'} MRI study — identifying sequences..."

        # Identify key sequences by their series descriptions
        sag_t2 = _find_sequence(inventory, ["t2_tse_sag", "t2_sag", "sag_t2"], plane="sagittal", contrast=False)
        sag_t1 = _find_sequence(inventory, ["t1_tse_sag", "t1_sag", "sag_t1"], plane="sagittal", contrast=False, exclude=["FS", "fs", "CONT"])
        sag_tirm = _find_sequence(inventory, ["tirm", "stir", "flair_sag"], plane="sagittal", contrast=False)
        sag_t1_cont = _find_sequence(inventory, ["t1_tse_sag", "t1_sag"], plane="sagittal", contrast=True)
        ax_t2 = _find_sequence(inventory, ["t2_tse_tra", "t2_tra", "tra_t2", "ax_t2"], plane="axial", contrast=False)
        ax_vibe_pre = _find_sequence(inventory, ["vibe_fs_tra", "vibe_tra"], plane="axial", contrast=False)
        ax_vibe_post = _find_sequence(inventory, ["vibe_fs_tra", "vibe_tra"], plane="axial", contrast=True)

        # For brain studies, also look for brain-specific sequences
        ax_flair = _find_sequence(inventory, ["flair", "dark_fluid"], plane="axial", contrast=False)
        ax_dwi = _find_sequence(inventory, ["dwi", "diffusion", "ep2d_diff"], plane="axial", contrast=False)
        ax_swi = _find_sequence(inventory, ["swi", "suscept"], plane="axial", contrast=False)

        # Spine requires sagittal T2 for quantitative analysis. Even without a Sag T2
        # match, a spine study must still get a Level Reference (Figure 0) from the
        # best-available sagittal (T1/STIR), per the skill's "NEVER SKIP" level ID.
        is_spine_quant = detected_anatomy == "spine" and sag_t2
        sag_for_levels = sag_t2 or sag_t1 or sag_tirm
        do_levels = detected_anatomy == "spine" and sag_for_levels is not None
        if detected_anatomy == "spine" and not sag_t2:
            logger.warning(
                "Spine study detected but no sagittal T2 found — quantitative measurements "
                "skipped (tiers capped); Level Reference still generated from best-available "
                f"sagittal ({sag_for_levels})."
            )

        # Phase 0B: Convert key sequences
        job.progress = 15
        job.progress_message = "Converting DICOM to viewable format..."
        all_seqs = [sag_t2, sag_t1, sag_tirm, sag_t1_cont, ax_t2, ax_vibe_pre, ax_vibe_post, ax_flair, ax_dwi, ax_swi]
        seqs_to_convert = [s for s in all_seqs if s]
        # If no named sequences matched, convert all available sequences
        if not seqs_to_convert:
            seqs_to_convert = list(inventory.sequences.keys())
        engine.convert_sequences(seqs_to_convert)

        midline = None

        # ── Spine pipeline ──
        if do_levels:
            # Phase 1: Level identification (sacrum-up) — always when a sagittal exists.
            job.status = "levels"
            job.progress = 25
            job.progress_message = "Identifying vertebral levels (sacrum-up protocol)..."

            seq_info = inventory.sequences[sag_for_levels]
            midline = seq_info.num_slices // 2
            engine.identify_levels(sag_for_levels, midline)

            if is_spine_quant:
                # Phase 2: Measurements (requires true Sag T2)
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
            else:
                job.status = "measuring"
                job.progress = 40
                job.progress_message = (
                    "Sag T2 not found — Level Reference produced; visual interpretation "
                    "only (measurement tiers capped)."
                )
        else:
            # Non-spine or no sagittal: skip quantitative pipeline
            job.status = "measuring"
            job.progress = 40
            job.progress_message = f"Preparing {detected_anatomy.upper()} study for visual interpretation..."

        # Phase 3: Annotations (anatomy-aware)
        job.progress = 60
        job.progress_message = "Creating annotated proof images..."

        if do_levels and midline is not None:
            # Level Reference (Figure 0) — always for spine when levels were identified.
            level_ref = engine.create_level_reference(sag_for_levels, midline)
            job.annotated_images["level_reference"] = level_ref

        if is_spine_quant and midline is not None:
            # Annotated sagittal with the full Phase-3 double-check loop (Sag T2 only).
            sag_annotated = engine.create_annotated_sagittal(sag_t2, midline)
            job.annotated_images["sag_t2_annotated"] = sag_annotated
            job.annotation_audit = engine.annotation_audit

        # Multi-sequence panel (useful for all anatomy types)
        panel_seqs = []
        if sag_t2 and midline is not None:
            panel_seqs.append((sag_t2, "T2 Sag", midline))
        if sag_t1 and midline is not None:
            panel_seqs.append((sag_t1, "T1 Sag", midline))
        if sag_tirm and midline is not None:
            panel_seqs.append((sag_tirm, "TIRM Sag", midline))
        if sag_t1_cont and midline is not None:
            panel_seqs.append((sag_t1_cont, "T1+C Sag", midline))

        # Brain-specific panels
        if ax_flair:
            flair_info = inventory.sequences[ax_flair]
            flair_mid = flair_info.num_slices // 2
            panel_seqs.append((ax_flair, "FLAIR Ax", flair_mid))
        if ax_dwi:
            dwi_info = inventory.sequences[ax_dwi]
            dwi_mid = dwi_info.num_slices // 2
            panel_seqs.append((ax_dwi, "DWI Ax", dwi_mid))
        if ax_swi:
            swi_info = inventory.sequences[ax_swi]
            swi_mid = swi_info.num_slices // 2
            panel_seqs.append((ax_swi, "SWI Ax", swi_mid))

        # Axial T2 (useful for all)
        if ax_t2:
            ax_info = inventory.sequences[ax_t2]
            ax_mid = ax_info.num_slices // 2
            panel_seqs.append((ax_t2, "T2 Ax", ax_mid))

        # Fallback: if no named sequences matched, use the first available sequence
        if not panel_seqs and inventory.sequences:
            for seq_name, seq_info in inventory.sequences.items():
                mid_slice = seq_info.num_slices // 2
                panel_seqs.append((seq_name, seq_info.series_description or seq_name, mid_slice))
                if len(panel_seqs) >= 4:
                    break

        if panel_seqs:
            panel = engine.create_multi_sequence_panel(panel_seqs[:4])  # Max 4 panels
            if panel:
                job.annotated_images["multi_sequence_panel"] = panel

        # Contrast comparison (spine-specific)
        if is_spine_quant and ax_vibe_pre and ax_vibe_post:
            seq_pre = inventory.sequences[ax_vibe_pre]
            num_slices = seq_pre.num_slices
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
        # export_measurements_json omits modality — carry the detected value so the Wait/sequences
        # panel labels CT/CR/X-ray studies correctly instead of always defaulting to "MR". (AR-3)
        job.measurements["modality"] = detected_modality

        # Phase 4: Claude interpretation
        job.status = "interpreting"
        job.progress = 75
        anatomy_label = {
            "spine": "Spine", "brain": "Neuroimaging", "msk": "Musculoskeletal",
            "cardiac": "Cardiac", "chest": "Chest", "abdomen": "Abdomen/Pelvis",
            "breast": "Breast", "vascular": "Vascular/MRA", "head_neck": "Head & Neck",
            "prostate": "Prostate",
        }.get(detected_anatomy, "MRI")
        job.progress_message = f"Claude Opus is analyzing {anatomy_label} findings..."

        interpreter = ClaudeInterpreter(api_key=api_key, auth_token=auth_token)

        # ── BatchSender: Send ALL images to Claude (replaces 4-image bottleneck) ──
        batch_sender = BatchSender(
            work_dir=Path(job.work_dir),
            anatomy_type=detected_anatomy,
        )
        image_content_blocks, image_count = batch_sender.build_message_content()
        logger.info(f"BatchSender: {image_count} images prepared for Claude")

        # Numbered annotated proof figures (Figure 0 = Level Reference) + figure inventory.
        annotated_blocks, figure_inventory = _build_figure_blocks(engine, job.annotated_images)

        # Combine: raw study images + numbered proof figures
        all_image_blocks = image_content_blocks + annotated_blocks

        # ── Phase 2: BLIND READ — no surgical notes / prior reports (anti-anchoring) ──
        blind_request = InterpretationRequest(
            measurements_json=job.measurements,
            image_content_blocks=all_image_blocks,
            clinical_history=clinical_history,   # indication/symptoms only — not a prior read
            surgical_notes=None,
            prior_reports=None,
            anatomy_type=detected_anatomy,
            modality=detected_modality,
        )

        job.progress = 78
        job.progress_message = f"Blind read — Claude analyzing {image_count} images independently..."
        interpretation = interpreter.interpret(blind_request)
        logger.info(
            f"Blind read complete: {interpretation.input_tokens} input, "
            f"{interpretation.output_tokens} output tokens"
        )

        # ── Phase 5: RECONCILIATION — only now ingest surgical notes / prior reports ──
        if surgical_notes or prior_reports:
            job.progress = 84
            job.progress_message = "Reconciling against surgical notes / prior reports..."
            try:
                recon = interpreter.reconcile(
                    blind_report={
                        "impression": interpretation.impression,
                        "findings_by_level": interpretation.findings_by_level,
                        "incidentals": interpretation.incidentals,
                    },
                    image_content_blocks=all_image_blocks,
                    surgical_notes=surgical_notes,
                    prior_reports=prior_reports,
                    anatomy_type=detected_anatomy,
                )
                if recon.get("discrepancies"):
                    interpretation.discrepancies = recon["discrepancies"]
                if recon.get("post_surgical_assessment"):
                    interpretation.post_surgical_assessment = recon["post_surgical_assessment"]
                interpretation.input_tokens += recon.get("input_tokens", 0)
                interpretation.output_tokens += recon.get("output_tokens", 0)
                logger.info(f"Reconciliation complete: {len(interpretation.discrepancies)} discrepancies")
            except Exception as e:
                logger.warning(f"Reconciliation pass failed: {e}")

        # ── Phase 6: MANDATORY 12-ITEM SELF-AUDIT (spine-aware) ──
        job.progress = 90
        job.progress_message = "Final self-audit (12-item) by senior attending..."

        try:
            verifier = VerificationPass(api_key=api_key, auth_token=auth_token)

            # Build initial report JSON for the audit
            initial_report = {}
            if interpretation.findings_by_level:
                initial_report["findings_by_level"] = interpretation.findings_by_level
            if interpretation.findings_by_region:
                initial_report["findings_by_region"] = interpretation.findings_by_region
            if interpretation.findings_by_structure:
                initial_report["findings_by_structure"] = interpretation.findings_by_structure
            if interpretation.findings_by_organ:
                initial_report["findings_by_organ"] = interpretation.findings_by_organ
            if interpretation.findings_by_vessel:
                initial_report["findings_by_vessel"] = interpretation.findings_by_vessel
            if interpretation.findings_by_zone:
                initial_report["findings_by_zone"] = interpretation.findings_by_zone
            initial_report["impression"] = interpretation.impression
            initial_report["confidence_summary"] = interpretation.confidence_summary
            initial_report["incidentals"] = interpretation.incidentals
            if interpretation.discrepancies:
                initial_report["discrepancies"] = interpretation.discrepancies

            verified = verifier.verify(
                initial_report=initial_report,
                image_content_blocks=all_image_blocks,
                measurements_json=job.measurements,
                anatomy_type=detected_anatomy,
                annotation_audit=job.annotation_audit,
                figure_inventory=figure_inventory,
                prior_reports=prior_reports,
                surgical_notes=surgical_notes,
            )

            # Apply verified findings back to interpretation (only keys actually returned)
            vf = verified.verified_findings or {}
            for key in ("findings_by_level", "findings_by_region", "findings_by_structure",
                        "findings_by_organ", "findings_by_vessel", "findings_by_zone",
                        "impression", "confidence_summary", "incidentals", "discrepancies"):
                if key in vf:
                    setattr(interpretation, key, vf[key])

            # Merge missed findings the attending caught INTO the impression (don't discard).
            if verified.missed_findings:
                if not isinstance(interpretation.impression, list):
                    interpretation.impression = [str(interpretation.impression)]
                for mf in verified.missed_findings:
                    tier = mf.get("tier", "C")
                    interpretation.impression.append(
                        f"[Added on audit] {mf.get('finding','')} [Tier {tier}] — {mf.get('reason','')}"
                    )

            # Determine an honest verification status (engine 3C failures are hard signals).
            engine_annotation_failed = any(
                a.get("status") == "failed" for a in (job.annotation_audit or [])
            )
            if not verified.parsed_ok:
                v_status = "incomplete"
            elif verified.audit_failures or engine_annotation_failed:
                v_status = "issues_flagged"
            else:
                v_status = "passed"

            job.verification = {
                "status": v_status,
                "quality_score": verified.quality_score,
                "quality_notes": verified.quality_notes,
                "audit": verified.audit,
                "audit_failures": verified.audit_failures,
                "annotation_review": verified.annotation_review,
                "corrections": verified.corrections,
                "missed_findings": verified.missed_findings,
                "annotation_3c_failed": engine_annotation_failed,
            }

            interpretation.input_tokens += verified.input_tokens
            interpretation.output_tokens += verified.output_tokens

            logger.info(
                f"Self-audit complete: status={v_status}, "
                f"quality_score={verified.quality_score}, "
                f"failures={verified.audit_failures}, "
                f"corrections={len(verified.corrections)}, "
                f"missed={len(verified.missed_findings)}"
            )
        except Exception as e:
            logger.warning(f"Self-audit pass failed (report flagged unverified): {e}")
            job.verification = {"status": "incomplete", "quality_notes": str(e)}

        job.interpretation = interpretation

        # Complete
        job.status = "complete"
        job.progress = 100
        job.progress_message = "Analysis complete"
        logger.info(f"Job {job.job_id} complete — {image_count} images analyzed with verification")
        _persist_report(job)   # durable: survives restart, indexes into Recent studies

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
ASSETS_DIR = FRONTEND_DIR / "assets"

# Serve static assets (logo, images, etc.)
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

# Security headers for the served app shell. CSP allows the CDN React/Babel/fonts the single-file
# frontend depends on, and same-origin XHR/SSE/images; everything else is denied. Framing is blocked.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"), headers=_SECURITY_HEADERS)
    return HTMLResponse(content="<h1>MIKA — Frontend not found</h1>", headers=_SECURITY_HEADERS)


if __name__ == "__main__":
    import uvicorn
    # Bind to loopback by default — MIKA is a local desktop app and must not expose medical data on
    # the network. Override with MIKA_HOST=0.0.0.0 only for an intentional, secured hosted deployment.
    host = os.environ.get("MIKA_HOST", "127.0.0.1")
    port = int(os.environ.get("MIKA_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)

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
from contextlib import asynccontextmanager

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
from services.evidence_pack import EvidencePackBuilder, manifest_text_summary
from services.artifacts import ArtifactQaGate, ArtifactRegistry
from services.reconciliation import (
    ReferenceInputError,
    MAX_REFERENCE_REPORT_BYTES,
    build_clinical_reconciliation_report,
    build_reference_reconciliation,
    read_reference_report_bytes,
)
from services.cv_synthesis import (
    synthesize_cv_candidate_reviews,
    upgrade_reconciliation_with_cv_supported_findings,
)
from services.agent_runner import (
    AgentRunner,
    AUTH_MANAGER,
    _normalize_summary,
    DEFAULT_TIMEOUT_S as AGENT_TIMEOUT_S,
)
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

@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    """Fix 1/Fix 3 startup: validate the read environment, then reconcile any jobs left
    non-terminal by a previous process that died mid-read (salvage finished reports, mark the
    rest as interrupted). Runs once before the server accepts requests."""
    env = check_env()
    if env["ok"]:
        logger.info(f"check_env OK — {env['versions']}")
    else:
        logger.warning(f"check_env reported a problem at startup: {env.get('import_error') or env.get('mismatches')}")
    _warn_if_onedrive_data_dir()
    try:
        handled = _reconcile_jobs_on_boot()
        if handled:
            logger.info(f"Boot reconciliation handled {handled} interrupted/unpersisted job(s)")
    except Exception:
        logger.exception("Boot reconciliation failed")
    yield


app = FastAPI(
    title="MIKA — AI Medical Imaging Analyzer",
    description="MIKA: Multi-modality, multi-anatomy AI imaging analysis — MR, CT, X-ray, ultrasound, mammography, PET across Spine, Brain, MSK, Cardiac, Chest, Abdomen, Breast, Vascular, Head & Neck, Prostate. Accepts DICOM, NIfTI, NRRD, PNG/JPG, ZIP. Runs on your Claude subscription.",
    version="3.0.0",
    lifespan=_lifespan,
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
                response = JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
                response.headers["Cache-Control"] = "no-store"
                return response
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
        self.evidence_manifest: dict = {}
        self.artifact_registry: dict = {}
        self.artifact_qa: dict = {}
        self.reconciliation: dict = {}
        self.mode: str = "lite"               # "agent" | "lite"
        self.active_sequence: Optional[str] = None  # live "now reading X", when the agent reports it
        self.active_region: Optional[str] = None     # live "now inspecting <level/region>", when reported
        self.agent: dict = {}                 # agent-mode result (pdf path, figures, summary)
        self.pdf_path: Optional[str] = None   # server-side path to the generated PDF (not exposed to client)
        self.cancelled: bool = False          # user requested cancel — honored by the agent loop + completion
        self.truncated: bool = False          # read hit the time cap with partial output (Fix 4)
        self.heartbeat_ts: float = time.time()  # last on-disk heartbeat (Fix 1 watchdog/boot recovery)
        self.error: Optional[str] = None
        self.error_code: Optional[str] = None
        self.auth_state: Optional[str] = None
        self.progress_phase: str = self.status
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


def _find_clinical_pdf(job_id: str, job: Optional["AnalysisJob"] = None) -> Optional[Path]:
    """Return this job's preserved clinician PDF, if the agent produced one."""
    candidates = []
    if job and getattr(job, "pdf_path", None):
        candidates.append(Path(job.pdf_path).with_name("report_clinical.pdf"))
    if job and getattr(job, "work_dir", None):
        candidates.append(Path(job.work_dir) / "report" / "report_clinical.pdf")
    try:
        meta = _load_meta(job_id)
        if meta and meta.get("clinical_pdf"):
            candidates.append(_job_dir(job_id) / str(meta["clinical_pdf"]).replace("\\", "/"))
    except Exception:
        pass
    candidates.append(_job_dir(job_id) / "work" / "report" / "report_clinical.pdf")

    seen = set()
    for candidate in candidates:
        try:
            resolved = Path(candidate).resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if not resolved.is_file():
            continue
        safe = _safe_join(job_id, _rel_to_job(job_id, str(resolved)))
        if safe:
            return safe
    return None


def _find_patient_pdf(job_id: str, job: Optional["AnalysisJob"] = None) -> Optional[Path]:
    """Return the patient-facing report.pdf for live or persisted jobs."""
    candidates = []
    if job and getattr(job, "pdf_path", None):
        p = Path(job.pdf_path)
        if p.name == "report.pdf":
            candidates.append(p)
        candidates.append(p.with_name("report.pdf"))
    if job and getattr(job, "work_dir", None):
        candidates.append(Path(job.work_dir) / "report" / "report.pdf")
    try:
        meta = _load_meta(job_id)
        if meta and meta.get("pdf"):
            candidates.append(_job_dir(job_id) / str(meta["pdf"]).replace("\\", "/"))
    except Exception:
        pass
    candidates.append(_job_dir(job_id) / "work" / "report" / "report.pdf")

    seen = set()
    for candidate in candidates:
        try:
            resolved = Path(candidate).resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.name != "report.pdf" or not resolved.is_file():
            continue
        safe = _safe_join(job_id, _rel_to_job(job_id, str(resolved)))
        if safe:
            return safe
    return None


def _coerce_list(v) -> list:
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _coerce_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _fill_missing_dict_values(base, fallback: dict) -> dict:
    out = dict(base) if isinstance(base, dict) else {}
    for key, value in fallback.items():
        if out.get(key) in (None, "", [], {}):
            out[key] = value
    return out


def _normalized_progress_phase(status: str, phase: Optional[str]) -> str:
    if not phase or (phase == "pending" and status != "pending"):
        return status
    return phase


def _summary_for_job(job: "AnalysisJob") -> dict:
    raw = ((job.agent or {}).get("summary")
           or ((job.measurements or {}).get("agent_summary") if job.measurements else None)
           or {})
    return _normalize_summary(raw)


def _interpretation_from_summary(summary: dict) -> dict:
    if not summary:
        return {}
    patient = _coerce_dict(summary.get("patient"))
    confidence = _coerce_dict(patient.get("confidence"))
    return {
        "findings": _coerce_list(summary.get("findings")),
        "impression": _coerce_list(summary.get("impression")),
        "incidentals": _coerce_list(summary.get("incidentals")),
        "discrepancies": _coerce_list(summary.get("discrepancies")),
        "confidence_summary": summary.get("confidence_summary") or confidence.get("note") or "",
        "model_used": (summary.get("model_used") or (summary.get("self_audit") or {}).get("model")),
        "tokens": summary.get("tokens") or {},
        "source": "agent_summary",
    }


def _normalized_findings_from_summary(summary: dict) -> list[dict]:
    patient_block = _coerce_dict(summary.get("patient"))
    patient_findings = _coerce_list(patient_block.get("findings"))
    technical_findings = _coerce_list(summary.get("findings"))

    findings = []
    for idx, finding in enumerate(patient_findings, start=1):
        if isinstance(finding, dict):
            findings.append({
                "id": f"patient-{idx}",
                "audience": "patient",
                "plain": finding.get("plain") or finding.get("text") or "",
                "certainty": finding.get("certainty", ""),
                "figure": finding.get("figure"),
                "caption": finding.get("caption", ""),
                "evidence_refs": _finding_evidence_refs(finding),
                "trust": _coerce_dict(finding.get("trust")),
                "location_trusted": finding.get("location_trusted"),
            })
        elif isinstance(finding, str) and finding.strip():
            findings.append({
                "id": f"patient-{idx}",
                "audience": "patient",
                "plain": finding.strip(),
                "certainty": "",
                "figure": None,
                "caption": "",
            })

    for idx, finding in enumerate(technical_findings, start=1):
        if isinstance(finding, dict):
            findings.append({
                "id": f"clinician-{idx}",
                "audience": "clinician",
                "text": finding.get("text") or finding.get("plain") or "",
                "tier": finding.get("tier") or finding.get("confidence_tier"),
                "figure": finding.get("figure"),
                "caption": finding.get("caption", ""),
                "evidence_refs": _finding_evidence_refs(finding),
                "series": finding.get("series"),
                "image": finding.get("image"),
                "plane": finding.get("plane"),
                "side": finding.get("side"),
                "level_or_region": finding.get("level_or_region") or finding.get("level") or finding.get("region"),
                "calibration_basis": finding.get("calibration_basis"),
                "trust": _coerce_dict(finding.get("trust")),
                "location_trusted": finding.get("location_trusted"),
            })
        elif isinstance(finding, str) and finding.strip():
            findings.append({
                "id": f"clinician-{idx}",
                "audience": "clinician",
                "text": finding.strip(),
                "tier": None,
                "figure": None,
                "caption": "",
            })
    return findings


def _normalized_report_sections(
    *,
    job: "AnalysisJob",
    summary: dict,
    interpretation_dict: dict,
    figures: list,
    detected_anatomy: str,
    anatomy_subregion: str,
    calibration_status: str,
    patient_pdf: Optional[Path],
    clinical_pdf: Optional[Path],
) -> dict:
    m = job.measurements or {}
    patient_block = _coerce_dict(summary.get("patient"))
    patient_study = _coerce_dict(patient_block.get("study"))
    patient_demographics = _coerce_dict(patient_block.get("patient")) or m.get("demographics", {})
    technical_findings = _coerce_list(summary.get("findings"))
    patient_findings = _coerce_list(patient_block.get("findings"))
    findings = _normalized_findings_from_summary(summary)
    reconciliation = (
        _coerce_dict(summary.get("reconciliation"))
        or _coerce_dict(getattr(job, "reconciliation", {}))
        or _coerce_dict((job.agent or {}).get("reconciliation") if getattr(job, "agent", None) else {})
    )
    cv_candidates = _cv_candidates_from_manifest(job.evidence_manifest)
    cv_candidate_reviews = _cv_candidate_reviews_for_report(summary, job.verification or {}, cv_candidates)
    cv_synthesis = _cv_synthesis_for_report(
        summary=summary,
        verification=job.verification or {},
        cv_candidates=cv_candidates,
        cv_candidate_reviews=cv_candidate_reviews,
        evidence_manifest=job.evidence_manifest or {},
    )
    reconciliation = upgrade_reconciliation_with_cv_supported_findings(
        reconciliation,
        cv_synthesis.get("clinician_findings") or [],
    )

    confidence = _coerce_dict(patient_block.get("confidence"))
    if not confidence:
        confidence = {
            "label": "",
            "score": None,
            "note": interpretation_dict.get("confidence_summary", ""),
        }
    confidence = dict(confidence)
    confidence["cv_candidate_policy"] = cv_synthesis.get("policy") or _cv_candidate_policy_from_manifest(job.evidence_manifest)
    progress_phase = _normalized_progress_phase(job.status, getattr(job, "progress_phase", None))

    study = {
        "body_part": patient_study.get("body_part") or detected_anatomy,
        "modality": patient_study.get("modality") or m.get("modality", ""),
        "date": patient_study.get("date", ""),
        "comparison": patient_study.get("comparison", ""),
        "description": m.get("study_description", "") or summary.get("study_description", ""),
        "detected_anatomy": detected_anatomy,
        "anatomy_subregion": anatomy_subregion,
        "calibration_status": calibration_status,
    }

    return {
        "study": study,
        "patient": {
            "demographics": patient_demographics,
            "bottom_line": patient_block.get("bottom_line", ""),
            "key_points": _coerce_list(patient_block.get("key_points")),
            "findings": patient_findings,
            "confidence": confidence,
            "change_over_time": _coerce_dict(patient_block.get("change_over_time")),
            "what_it_means": _coerce_list(patient_block.get("what_it_means")),
            "worth_flagging": _coerce_list(patient_block.get("worth_flagging")),
            "reference_reconciliation": (
                _coerce_dict(patient_block.get("reference_reconciliation"))
                or _coerce_dict(reconciliation.get("patient"))
            ),
            "cv_candidate_review": _coerce_dict(patient_block.get("cv_candidate_review")),
            "cv_supported_explanations": cv_synthesis.get("patient_explanations") or _coerce_list(patient_block.get("cv_supported_explanations")),
            "disclaimer": patient_block.get("disclaimer") or REPORT_DISCLAIMER,
        },
        "clinician": {
            "findings": technical_findings,
            "impression": _coerce_list(summary.get("impression")) or _coerce_list(interpretation_dict.get("impression")),
            "incidentals": _coerce_list(summary.get("incidentals")) or _coerce_list(interpretation_dict.get("incidentals")),
            "discrepancies": _coerce_list(summary.get("discrepancies")) or _coerce_list(interpretation_dict.get("discrepancies")),
            "confidence_summary": summary.get("confidence_summary") or interpretation_dict.get("confidence_summary", ""),
            "calibration_status": calibration_status,
            "reference_reconciliation": _coerce_dict(reconciliation.get("clinician")),
            "cv_candidate_reviews": cv_candidate_reviews,
            "cv_supported_findings": cv_synthesis.get("clinician_findings") or _coerce_list(summary.get("cv_supported_findings")),
        },
        "findings": findings,
        "cv_candidates": cv_candidates,
        "cv_candidate_reviews": cv_candidate_reviews,
        "confidence": confidence,
        "assets": {
            "figures": figures,
            "images": list((job.annotated_images or {}).keys()),
            "pdf": {
                "patient_available": bool(patient_pdf),
                "clinical_available": bool(clinical_pdf),
            },
            "evidence": job.evidence_manifest or {},
            "cv_candidates": cv_candidates,
            "cv_candidate_reviews": cv_candidate_reviews,
            "cv_supported_findings": cv_synthesis.get("clinician_findings") or [],
            "artifacts": job.artifact_registry or {},
            "artifact_qa": job.artifact_qa or {},
            "reconciliation": reconciliation,
        },
        "reconciliation": reconciliation,
        "status": job.status,
        "error_code": getattr(job, "error_code", None),
        "error_message": getattr(job, "error", None),
        "auth_state": getattr(job, "auth_state", None),
        "progress_phase": progress_phase,
    }


def _normalize_loaded_report(job_id: str, payload: dict) -> dict:
    """Backfill Run 1 contract fields for reports written by older builds."""
    out = dict(payload or {})
    summary = _normalize_summary(
        ((out.get("agent") or {}).get("summary")
         or (out.get("measurements") or {}).get("agent_summary")
         or {})
    )
    m = out.get("measurements") or {}
    patient_block = _coerce_dict(summary.get("patient"))
    patient_study = _coerce_dict(patient_block.get("study"))
    patient_findings = _coerce_list(patient_block.get("findings"))
    technical_findings = _coerce_list(summary.get("findings"))
    confidence = _coerce_dict(patient_block.get("confidence"))
    normalized_findings = _normalized_findings_from_summary(summary)
    patient_pdf = _find_patient_pdf(job_id)
    clinical_pdf = _find_clinical_pdf(job_id)
    evidence = _read_job_json(job_id, "work/evidence/evidence_manifest.json")
    artifact_registry = _read_job_json(job_id, "work/artifacts/artifact_registry.json")
    artifact_qa = _read_job_json(job_id, "work/artifacts/artifact_qa.json")
    cv_candidates = _cv_candidates_from_manifest(evidence or (out.get("assets") or {}).get("evidence") or {})
    if not cv_candidates:
        cv_candidates = [dict(c) for c in _coerce_list(out.get("cv_candidates")) if isinstance(c, dict)]
    cv_candidate_reviews = _cv_candidate_reviews_for_report(
        summary,
        _coerce_dict(out.get("verification")),
        cv_candidates,
    ) or _normalize_cv_candidate_reviews(out.get("cv_candidate_reviews"), cv_candidates)
    reconciliation = (
        _coerce_dict(out.get("reconciliation"))
        or _coerce_dict(summary.get("reconciliation"))
        or _read_job_json(job_id, "work/reconciliation/reconciliation.json")
    )
    if reconciliation:
        cv_synthesis = _cv_synthesis_for_report(
            summary=summary,
            verification=_coerce_dict(out.get("verification")),
            cv_candidates=cv_candidates,
            cv_candidate_reviews=cv_candidate_reviews,
            evidence_manifest=evidence or (out.get("assets") or {}).get("evidence") or {},
        )
        reconciliation = upgrade_reconciliation_with_cv_supported_findings(
            reconciliation,
            cv_synthesis.get("clinician_findings") or [],
        )
        summary["reconciliation"] = reconciliation
        patient_block["reference_reconciliation"] = (
            _coerce_dict(patient_block.get("reference_reconciliation"))
            or _coerce_dict(reconciliation.get("patient"))
        )
        out["reconciliation"] = reconciliation
    else:
        cv_synthesis = _cv_synthesis_for_report(
            summary=summary,
            verification=_coerce_dict(out.get("verification")),
            cv_candidates=cv_candidates,
            cv_candidate_reviews=cv_candidate_reviews,
            evidence_manifest=evidence or (out.get("assets") or {}).get("evidence") or {},
        )
    patient_block["cv_supported_explanations"] = (
        cv_synthesis.get("patient_explanations")
        or _coerce_list(patient_block.get("cv_supported_explanations"))
    )
    summary["cv_supported_findings"] = (
        cv_synthesis.get("clinician_findings")
        or _coerce_list(summary.get("cv_supported_findings"))
        or _coerce_list(out.get("cv_supported_findings"))
    )
    confidence = dict(confidence or {})
    confidence["cv_candidate_policy"] = (
        cv_synthesis.get("policy")
        or _coerce_dict((out.get("confidence") or {}).get("cv_candidate_policy"))
        or _cv_candidate_policy_from_manifest(evidence)
    )
    out["pdf_available"] = bool(out.get("pdf_available") or patient_pdf)
    out["clinical_pdf_available"] = bool(out.get("clinical_pdf_available") or clinical_pdf)
    out.setdefault("error_code", None)
    out.setdefault("error_message", out.get("error"))
    out.setdefault("auth_state", None)
    out["progress_phase"] = _normalized_progress_phase(out.get("status", "complete"), out.get("progress_phase"))
    if "interpretation" not in out or not out.get("interpretation"):
        out["interpretation"] = _interpretation_from_summary(summary)
    if all(k in out for k in ("study", "patient", "clinician", "findings", "confidence", "assets")):
        out["study"] = _fill_missing_dict_values(out.get("study"), {
            "body_part": patient_study.get("body_part") or out.get("detected_anatomy") or m.get("detected_anatomy", "unknown"),
            "modality": patient_study.get("modality") or m.get("modality", ""),
            "date": patient_study.get("date", ""),
            "comparison": patient_study.get("comparison", ""),
            "description": out.get("study_description", "") or m.get("study_description", "") or summary.get("study_description", ""),
            "detected_anatomy": out.get("detected_anatomy") or m.get("detected_anatomy", "unknown"),
            "anatomy_subregion": out.get("anatomy_subregion") or m.get("anatomy_subregion", ""),
            "calibration_status": out.get("calibration_status") or m.get("calibration_status", "unknown"),
        })
        out["patient"] = _fill_missing_dict_values(out.get("patient"), {
            "demographics": _coerce_dict(patient_block.get("patient")) or out.get("demographics", {}),
            "bottom_line": patient_block.get("bottom_line", ""),
            "key_points": _coerce_list(patient_block.get("key_points")),
            "findings": patient_findings,
            "confidence": confidence,
            "change_over_time": _coerce_dict(patient_block.get("change_over_time")),
            "what_it_means": _coerce_list(patient_block.get("what_it_means")),
            "worth_flagging": _coerce_list(patient_block.get("worth_flagging")),
            "reference_reconciliation": _coerce_dict(patient_block.get("reference_reconciliation")),
            "cv_candidate_review": _coerce_dict(patient_block.get("cv_candidate_review")),
            "cv_supported_explanations": _coerce_list(patient_block.get("cv_supported_explanations")),
            "disclaimer": patient_block.get("disclaimer") or out.get("disclaimer") or REPORT_DISCLAIMER,
        })
        out["clinician"] = _fill_missing_dict_values(out.get("clinician"), {
            "findings": technical_findings,
            "impression": _coerce_list(summary.get("impression")) or _coerce_list((out.get("interpretation") or {}).get("impression")),
            "incidentals": _coerce_list(summary.get("incidentals")),
            "discrepancies": _coerce_list(summary.get("discrepancies")),
            "confidence_summary": summary.get("confidence_summary") or (out.get("interpretation") or {}).get("confidence_summary", ""),
            "calibration_status": out.get("calibration_status") or m.get("calibration_status", "unknown"),
            "reference_reconciliation": _coerce_dict(reconciliation.get("clinician")) if reconciliation else {},
            "cv_candidate_reviews": cv_candidate_reviews,
            "cv_supported_findings": _coerce_list(summary.get("cv_supported_findings")),
        })
        if not out.get("findings") and normalized_findings:
            out["findings"] = normalized_findings
        if not out.get("confidence") and confidence:
            out["confidence"] = confidence
        else:
            out["confidence"] = dict(out.get("confidence") or {})
            out["confidence"]["cv_candidate_policy"] = confidence.get("cv_candidate_policy")
        assets = dict(out.get("assets") or {})
        pdf_assets = dict(assets.get("pdf") or {})
        pdf_assets["patient_available"] = bool(pdf_assets.get("patient_available") or out["pdf_available"])
        pdf_assets["clinical_available"] = bool(pdf_assets.get("clinical_available") or out["clinical_pdf_available"])
        assets["pdf"] = pdf_assets
        assets.setdefault("evidence", evidence)
        assets.setdefault("cv_candidates", cv_candidates)
        assets.setdefault("cv_candidate_reviews", cv_candidate_reviews)
        assets.setdefault("cv_supported_findings", _coerce_list(summary.get("cv_supported_findings")))
        assets.setdefault("artifacts", artifact_registry)
        assets.setdefault("artifact_qa", artifact_qa)
        assets.setdefault("reconciliation", reconciliation)
        out["assets"] = assets
        out["cv_candidates"] = cv_candidates
        out["cv_candidate_reviews"] = cv_candidate_reviews
        out["cv_supported_findings"] = _coerce_list(summary.get("cv_supported_findings"))
        return out

    out["study"] = _fill_missing_dict_values(out.get("study"), {
        "body_part": patient_study.get("body_part") or out.get("detected_anatomy", "unknown"),
        "modality": patient_study.get("modality") or m.get("modality", ""),
        "date": patient_study.get("date", ""),
        "comparison": patient_study.get("comparison", ""),
        "description": out.get("study_description", "") or m.get("study_description", ""),
        "detected_anatomy": out.get("detected_anatomy", "unknown"),
        "anatomy_subregion": out.get("anatomy_subregion", ""),
        "calibration_status": out.get("calibration_status", "unknown"),
    })
    out["patient"] = _fill_missing_dict_values(out.get("patient"), {
        "demographics": _coerce_dict(patient_block.get("patient")) or out.get("demographics", {}),
        "bottom_line": patient_block.get("bottom_line", ""),
        "key_points": _coerce_list(patient_block.get("key_points")),
        "findings": patient_findings,
        "confidence": confidence,
        "change_over_time": _coerce_dict(patient_block.get("change_over_time")),
        "what_it_means": _coerce_list(patient_block.get("what_it_means")),
        "worth_flagging": _coerce_list(patient_block.get("worth_flagging")),
        "reference_reconciliation": _coerce_dict(patient_block.get("reference_reconciliation")),
        "cv_candidate_review": _coerce_dict(patient_block.get("cv_candidate_review")),
        "cv_supported_explanations": _coerce_list(patient_block.get("cv_supported_explanations")),
        "disclaimer": patient_block.get("disclaimer") or out.get("disclaimer") or REPORT_DISCLAIMER,
    })
    out["clinician"] = _fill_missing_dict_values(out.get("clinician"), {
        "findings": technical_findings,
        "impression": _coerce_list(summary.get("impression")) or _coerce_list((out.get("interpretation") or {}).get("impression")),
        "incidentals": _coerce_list(summary.get("incidentals")),
        "discrepancies": _coerce_list(summary.get("discrepancies")),
        "confidence_summary": summary.get("confidence_summary") or (out.get("interpretation") or {}).get("confidence_summary", ""),
        "calibration_status": out.get("calibration_status", "unknown"),
        "reference_reconciliation": _coerce_dict(reconciliation.get("clinician")) if reconciliation else {},
        "cv_candidate_reviews": cv_candidate_reviews,
        "cv_supported_findings": _coerce_list(summary.get("cv_supported_findings")),
    })
    if not out.get("findings"):
        out["findings"] = normalized_findings
    out["confidence"] = dict(out.get("confidence") or confidence or {})
    out["confidence"]["cv_candidate_policy"] = confidence.get("cv_candidate_policy")
    out.setdefault("assets", {
        "figures": out.get("figures", []),
        "images": out.get("annotated_images", []),
        "pdf": {
            "patient_available": out["pdf_available"],
            "clinical_available": out["clinical_pdf_available"],
        },
        "evidence": evidence,
        "cv_candidates": cv_candidates,
        "cv_candidate_reviews": cv_candidate_reviews,
        "cv_supported_findings": _coerce_list(summary.get("cv_supported_findings")),
        "artifacts": artifact_registry,
        "artifact_qa": artifact_qa,
        "reconciliation": reconciliation,
    })
    out["assets"].setdefault("evidence", evidence)
    out["assets"].setdefault("cv_candidates", cv_candidates)
    out["assets"].setdefault("cv_candidate_reviews", cv_candidate_reviews)
    out["assets"].setdefault("cv_supported_findings", _coerce_list(summary.get("cv_supported_findings")))
    out["assets"].setdefault("artifacts", artifact_registry)
    out["assets"].setdefault("artifact_qa", artifact_qa)
    out["assets"].setdefault("reconciliation", reconciliation)
    out["cv_candidates"] = cv_candidates
    out["cv_candidate_reviews"] = cv_candidate_reviews
    out["cv_supported_findings"] = _coerce_list(summary.get("cv_supported_findings"))
    return out


def _read_job_json(job_id: str, rel: str) -> dict:
    try:
        path = _safe_join(job_id, rel)
        if path:
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return {}


def _prepare_evidence_pack(job: "AnalysisJob", study_root: Optional[str] = None) -> dict:
    """Build and persist the Run 2 PHI-safe evidence manifest for this job."""
    root = Path(study_root) if study_root else Path(job.dicom_dir)
    try:
        # If the upload started as image exports, keep the calibration cap explicit even
        # if the converter wrapped them as DICOM for downstream compatibility.
        upload_dir = _job_dir(job.job_id) / "upload"
        has_image_exports = any(
            p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
            for p in upload_dir.rglob("*")
        ) if upload_dir.exists() else False
        evidence_root = upload_dir if has_image_exports else root
        builder = EvidencePackBuilder(evidence_root, job.work_dir)
        pack = builder.build()
        manifest = pack.to_manifest()
        manifest["manifest_path"] = _rel_to_job(job.job_id, pack.manifest_path)
        if has_image_exports:
            manifest["study"]["input_type"] = "image_export"
            manifest["study"]["calibrated"] = False
            manifest["study"]["calibration_reason"] = "Original upload was an image export without trustworthy scale metadata"
            manifest["cv_candidates"] = []
            manifest["cv_candidate_limitations"] = ["CV geometry candidates disabled for uncalibrated image-export uploads."]
            if "Image-export upload: precise measurements and pinpoint markers are disabled." not in manifest["limitations"]:
                manifest["limitations"].append("Image-export upload: precise measurements and pinpoint markers are disabled.")

        # Carry known inventory labels into the manifest, without PHI.
        m = job.measurements or {}
        if m:
            manifest["study"]["anatomy"] = m.get("detected_anatomy", manifest["study"].get("anatomy", "unknown"))
            manifest["study"]["subregion"] = m.get("anatomy_subregion", manifest["study"].get("subregion", ""))
            manifest["study"]["modality"] = m.get("modality", manifest["study"].get("modality", ""))
        Path(pack.manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if job.measurements is not None:
            job.measurements["cv_candidates"] = manifest.get("cv_candidates", [])
            job.measurements["cv_candidate_limitations"] = manifest.get("cv_candidate_limitations", [])
        job.evidence_manifest = manifest
        return manifest
    except Exception as e:
        logger.warning(f"EvidencePack build failed for {job.job_id}: {e}")
        job.evidence_manifest = {
            "manifest_version": 1,
            "study": {
                "input_type": "unknown",
                "modality": (job.measurements or {}).get("modality", ""),
                "anatomy": (job.measurements or {}).get("detected_anatomy", "unknown"),
                "subregion": (job.measurements or {}).get("anatomy_subregion", ""),
                "calibrated": False,
                "calibration_reason": "EvidencePack build failed",
                "series_count": 0,
                "image_count": 0,
                "selected_image_count": 0,
            },
            "series": [],
            "selected_images": [],
            "cv_candidates": [],
            "cv_candidate_limitations": [f"EvidencePack build failed: {type(e).__name__}"],
            "limitations": [f"EvidencePack build failed: {type(e).__name__}"],
        }
        return job.evidence_manifest


def _evidence_manifest_path(job: "AnalysisJob") -> Optional[str]:
    rel = (job.evidence_manifest or {}).get("manifest_path")
    if not rel:
        p = Path(job.work_dir) / "evidence" / "evidence_manifest.json"
        return str(p) if p.exists() else None
    p = _safe_join(job.job_id, rel)
    return str(p) if p else None


def _finding_evidence_refs(finding: dict) -> list[str]:
    val = finding.get("evidence_refs") or finding.get("evidence_ids") or finding.get("evidence_ref")
    if isinstance(val, list):
        return [str(v) for v in val if str(v).strip()]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


CV_CANDIDATE_STATUSES = {"supported", "not_supported", "cannot_assess", "localization_wrong"}


def _cv_candidates_from_manifest(manifest: Optional[dict]) -> list[dict]:
    return [dict(c) for c in _coerce_list((manifest or {}).get("cv_candidates")) if isinstance(c, dict)]


def _normalize_cv_candidate_reviews(value, candidates: Optional[list[dict]] = None) -> list[dict]:
    candidate_map = {
        str(c.get("candidate_id")): c
        for c in (candidates or [])
        if isinstance(c, dict) and c.get("candidate_id")
    }
    out = []
    for row in _coerce_list(value):
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in CV_CANDIDATE_STATUSES:
            status = "cannot_assess"
        refs = row.get("evidence_refs_used") or row.get("evidence_refs") or []
        if isinstance(refs, str):
            refs = [refs] if refs.strip() else []
        elif not isinstance(refs, list):
            refs = []
        candidate = candidate_map.get(candidate_id, {})
        out.append({
            "candidate_id": candidate_id,
            "status": status,
            "evidence_refs_used": [str(ref) for ref in refs if str(ref).strip()],
            "short_reason": str(row.get("short_reason") or row.get("reason") or "").strip(),
            "patient_wording": str(row.get("patient_wording") or "").strip(),
            "clinician_wording": str(row.get("clinician_wording") or "").strip(),
            "level": row.get("level") or candidate.get("level", ""),
            "side": row.get("side") or candidate.get("side", ""),
            "candidate_type": row.get("candidate_type") or candidate.get("candidate_type", ""),
            "geometry_confidence": candidate.get("geometry_confidence"),
            "registration_confidence": candidate.get("registration_confidence"),
            "artifact_trust": _coerce_dict(candidate.get("artifact_trust")),
        })
    return out


def _cv_candidate_reviews_for_report(summary: dict, verification: dict, candidates: list[dict]) -> list[dict]:
    sources = [
        summary.get("cv_candidate_reviews") if isinstance(summary, dict) else None,
        verification.get("cv_candidate_reviews") if isinstance(verification, dict) else None,
    ]
    for source in sources:
        rows = _normalize_cv_candidate_reviews(source, candidates)
        if rows:
            return rows
    return []


def _cv_candidate_policy_from_manifest(manifest: Optional[dict]) -> dict:
    return _coerce_dict((manifest or {}).get("cv_candidate_policy"))


def _cv_synthesis_for_report(
    *,
    summary: dict,
    verification: dict,
    cv_candidates: list[dict],
    cv_candidate_reviews: list[dict],
    evidence_manifest: Optional[dict],
) -> dict:
    synthesis = synthesize_cv_candidate_reviews(
        blind_report=summary,
        cv_candidates=cv_candidates,
        cv_candidate_reviews=cv_candidate_reviews,
        verifier_result=verification or {},
        cv_candidate_policy=_cv_candidate_policy_from_manifest(evidence_manifest),
    )
    if not synthesis.get("used") and isinstance(summary, dict):
        existing_patient = _coerce_list((_coerce_dict(summary.get("patient"))).get("cv_supported_explanations"))
        existing_clinician = _coerce_list(summary.get("cv_supported_findings"))
        if existing_patient or existing_clinician:
            synthesis["patient_explanations"] = existing_patient
            synthesis["clinician_findings"] = existing_clinician
            synthesis["used"] = bool(existing_clinician or synthesis["patient_explanations"])
    return synthesis


def _summary_with_cv_synthesis(job: "AnalysisJob", summary: dict) -> tuple[dict, dict]:
    summary = dict(summary or {})
    cv_candidates = _cv_candidates_from_manifest(job.evidence_manifest)
    cv_candidate_reviews = _cv_candidate_reviews_for_report(summary, job.verification or {}, cv_candidates)
    synthesis = _cv_synthesis_for_report(
        summary=summary,
        verification=job.verification or {},
        cv_candidates=cv_candidates,
        cv_candidate_reviews=cv_candidate_reviews,
        evidence_manifest=job.evidence_manifest or {},
    )
    patient = dict(summary.get("patient") or {})
    patient["cv_supported_explanations"] = synthesis.get("patient_explanations") or []
    summary["patient"] = patient
    summary["cv_candidate_reviews"] = cv_candidate_reviews
    summary["cv_supported_findings"] = synthesis.get("clinician_findings") or []
    return summary, synthesis


def _summary_figure_evidence(summary: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    patient_findings = ((summary.get("patient") or {}).get("findings") or [])
    clinician_findings = summary.get("findings") or []
    for finding in list(patient_findings) + list(clinician_findings):
        if not isinstance(finding, dict):
            continue
        fig = finding.get("figure") or finding.get("file")
        refs = _finding_evidence_refs(finding)
        if fig and refs:
            out.setdefault(Path(str(fig)).stem, [])
            out[Path(str(fig)).stem].extend(refs)
    return {k: sorted(set(v)) for k, v in out.items()}


def _artifact_kind(name: str) -> str:
    low = name.lower()
    if low.startswith("seqthumb"):
        return "reference_image"
    if "body" in low and "map" in low:
        return "body_map"
    if "panel" in low or "contrast" in low or "comparison" in low:
        return "comparison_panel"
    if "annotated" in low or "level_reference" in low:
        return "annotated_slice"
    return "proof_image"


def _run_artifact_qa(job: "AnalysisJob") -> None:
    summary = _summary_for_job(job)
    registry = ArtifactRegistry(job.work_dir)
    fig_evidence = _summary_figure_evidence(summary)
    m = job.measurements or {}
    anatomy = m.get("detected_anatomy", "unknown")
    modality = m.get("modality", "")
    calibration_state = "calibrated" if (job.evidence_manifest or {}).get("study", {}).get("calibrated") else "uncalibrated"
    for name, path in (job.annotated_images or {}).items():
        registry.add_visual(
            kind=_artifact_kind(name),
            path=path,
            source="generated",
            linked_finding_id=None,
            anatomy=anatomy,
            modality=modality,
            sequence_view=name,
            calibration_state=calibration_state,
            marker_type="region" if calibration_state == "uncalibrated" else "pinpoint",
            evidence_ids=fig_evidence.get(Path(name).stem, []),
        )
    gate = ArtifactQaGate(job.work_dir, evidence_manifest=job.evidence_manifest)
    qa = gate.run(registry, summary)
    job.artifact_registry = registry.to_manifest()
    job.artifact_qa = qa
    if job.agent:
        job.agent["summary"] = summary
        job.agent["artifact_qa"] = qa
    if job.measurements is not None:
        job.measurements["artifact_qa"] = qa
    _rewrite_agent_summary_and_patient_pdf(job, summary)


def _rewrite_agent_summary_and_patient_pdf(job: "AnalysisJob", summary: dict) -> None:
    if not summary:
        return
    summary, cv_synthesis = _summary_with_cv_synthesis(job, summary)
    reconciliation = (
        _coerce_dict(getattr(job, "reconciliation", {}))
        or _coerce_dict(summary.get("reconciliation"))
        or _coerce_dict((job.agent or {}).get("reconciliation") if getattr(job, "agent", None) else {})
    )
    if reconciliation:
        reconciliation = upgrade_reconciliation_with_cv_supported_findings(
            reconciliation,
            cv_synthesis.get("clinician_findings") or [],
        )
        summary["reconciliation"] = reconciliation
        patient = dict(summary.get("patient") or {})
        patient["reference_reconciliation"] = reconciliation.get("patient") or {}
        summary["patient"] = patient
        job.reconciliation = reconciliation
    if job.agent is not None:
        job.agent["summary"] = summary
        if reconciliation:
            job.agent["reconciliation"] = reconciliation
    if job.measurements is not None:
        job.measurements["agent_summary"] = summary
    report_dir = Path(job.work_dir) / "report"
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"Could not rewrite gated summary.json: {e}")
    patient = summary.get("patient")
    if not patient:
        return
    try:
        try:
            from backend.services.report_builder import build_patient_report
        except ImportError:
            from services.report_builder import build_patient_report
        build_patient_report(patient, report_dir, report_dir / "report.pdf")
        job.pdf_path = str(report_dir / "report.pdf")
    except Exception as e:
        logger.warning(f"Could not rebuild patient PDF after artifact QA: {e}")
    if cv_synthesis.get("used") or reconciliation:
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            build_clinical_reconciliation_report(summary, reconciliation or {}, report_dir / "report_clinical.pdf")
        except Exception as e:
            logger.warning(f"Could not rebuild clinical PDF after CV synthesis: {e}")


def _apply_reference_reconciliation(
    job: "AnalysisJob",
    *,
    reference_report_path: Optional[str] = None,
    reference_report_text: Optional[str] = None,
) -> dict:
    """Attach reference-assisted reconciliation without modifying the blind read findings."""
    if not (reference_report_path or reference_report_text):
        return {}
    summary, cv_synthesis = _summary_with_cv_synthesis(job, _summary_for_job(job))
    reconciliation = build_reference_reconciliation(
        blind_summary=summary,
        reference_text=reference_report_text,
        reference_path=reference_report_path,
        evidence_manifest=job.evidence_manifest or {},
    )
    reconciliation = upgrade_reconciliation_with_cv_supported_findings(
        reconciliation,
        cv_synthesis.get("clinician_findings") or [],
    )
    if not reconciliation.get("used"):
        return {}

    summary = dict(summary or {})
    patient = dict(summary.get("patient") or {})
    patient["reference_reconciliation"] = reconciliation.get("patient") or {}
    summary["patient"] = patient
    summary["reconciliation"] = reconciliation

    job.reconciliation = reconciliation
    if job.agent is not None:
        job.agent["summary"] = summary
        job.agent["reconciliation"] = reconciliation
        job.agent["pdf_available"] = bool(job.agent.get("pdf_available") or job.pdf_path)
    if job.measurements is not None:
        job.measurements["agent_summary"] = summary

    rec_dir = Path(job.work_dir) / "reconciliation"
    report_dir = Path(job.work_dir) / "report"
    try:
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / "reconciliation.json").write_text(json.dumps(reconciliation, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not persist reconciliation manifest for {job.job_id}: {e}")

    _rewrite_agent_summary_and_patient_pdf(job, summary)
    if job.agent is not None:
        job.agent["pdf_available"] = bool(job.agent.get("pdf_available") or job.pdf_path)
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        build_clinical_reconciliation_report(summary, reconciliation, report_dir / "report_clinical.pdf")
    except Exception as e:
        logger.warning(f"Could not rebuild clinical PDF with reconciliation for {job.job_id}: {e}")
    return reconciliation


def _persist_report(job: "AnalysisJob") -> None:
    """Write report.json + meta.json for a completed job so it survives a restart. Non-fatal."""
    try:
        jd = _job_dir(job.job_id)
        jd.mkdir(parents=True, exist_ok=True)
        payload = _build_report_payload(job)
        (jd / "report.json").write_text(json.dumps(payload, default=str), encoding="utf-8")

        images = {name: _rel_to_job(job.job_id, p) for name, p in (job.annotated_images or {}).items()}
        patient_pdf = _find_patient_pdf(job.job_id, job)
        clinical_pdf = _find_clinical_pdf(job.job_id, job)
        # Cover thumbnail for the Recent list: prefer a real study slice, else the first figure.
        thumb = next((n for n in images if n.startswith("seqthumb")), None) or next(iter(images), None)
        m = job.measurements or {}
        progress_phase = _normalized_progress_phase(job.status, getattr(job, "progress_phase", None))
        meta = {
            "job_id": job.job_id,
            "status": job.status,
            "mode": job.mode,
            "created_at": job.created_at,
            "completed_at": datetime.utcnow().isoformat(),
            "detected_anatomy": m.get("detected_anatomy", "unknown"),
            "anatomy_subregion": m.get("anatomy_subregion", ""),
            "modality": m.get("modality", ""),
            "title": (
                (payload.get("study") or {}).get("description")
                or (payload.get("study") or {}).get("body_part")
                or payload.get("study_description")
                or m.get("study_description")
                or ""
            ),
            "thumb": thumb,
            "images": images,
            "pdf": _rel_to_job(job.job_id, str(patient_pdf)) if patient_pdf else None,
            "pdf_available": bool(patient_pdf),
            "clinical_pdf": _rel_to_job(job.job_id, str(clinical_pdf)) if clinical_pdf else None,
            "clinical_pdf_available": bool(clinical_pdf),
            "evidence_manifest": (job.evidence_manifest or {}).get("manifest_path"),
            "artifact_registry": (job.artifact_qa or {}).get("registry_path"),
            "artifact_qa_status": (job.artifact_qa or {}).get("status"),
            "reference_reconciliation_available": bool((payload.get("reconciliation") or {}).get("used")),
            "error_code": getattr(job, "error_code", None),
            "error_message": getattr(job, "error", None),
            "auth_state": getattr(job, "auth_state", None),
            "progress_phase": progress_phase,
            "truncated": bool(getattr(job, "truncated", False)),
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
            raw_report = _load_report(d.name) or {}
            report = _normalize_loaded_report(d.name, raw_report) if raw_report else {}
            study = report.get("study") or {}
            out.append({
                "job_id": meta.get("job_id", d.name),
                "status": meta.get("status", "complete"),
                "title": meta.get("title") or study.get("description") or study.get("body_part") or report.get("study_description") or "",
                "detected_anatomy": meta.get("detected_anatomy") or study.get("detected_anatomy") or "unknown",
                "anatomy_subregion": meta.get("anatomy_subregion") or study.get("anatomy_subregion") or "",
                "modality": meta.get("modality") or study.get("modality") or "",
                "created_at": meta.get("created_at", ""),
                "completed_at": meta.get("completed_at", ""),
                "thumb": meta.get("thumb"),
                "pdf_available": bool(meta.get("pdf_available") or _find_patient_pdf(d.name)),
                "clinical_pdf_available": bool(meta.get("clinical_pdf_available") or _find_clinical_pdf(d.name)),
                "reference_reconciliation_available": bool(
                    meta.get("reference_reconciliation_available")
                    or (report.get("reconciliation") or {}).get("used")
                ),
                "error_code": meta.get("error_code"),
                "error_message": meta.get("error_message"),
                "auth_state": meta.get("auth_state"),
                "progress_phase": _normalized_progress_phase(meta.get("status", "complete"), meta.get("progress_phase")),
            })
    except Exception as e:
        logger.warning(f"Could not list reports: {e}")
    out.sort(key=lambda r: r.get("completed_at") or r.get("created_at") or "", reverse=True)
    return out


# ── Fix 3: runtime environment enforcement ──
#
# ABI-critical pins — these MUST match requirements.txt / requirements.lock. numpy<2 is HARD:
# scipy 1.12.0 is built against the numpy 1.26 ABI, so numpy 2.x crashes every read (the real F2
# incident, where a stray `pip install` pulled numpy 2.x and broke every read until it was pinned back).
def _rehydrate_completed_job(job_id: str) -> Optional["AnalysisJob"]:
    raw = _load_report(job_id)
    if not raw:
        return None
    report = _normalize_loaded_report(job_id, raw)
    job = AnalysisJob(job_id=job_id, dicom_dir=str(_job_dir(job_id) / "dicom"))
    job.status = report.get("status", "complete")
    job.progress_phase = _normalized_progress_phase(job.status, report.get("progress_phase"))
    job.progress = 100 if job.status == "complete" else 0
    job.mode = report.get("mode", "agent")
    job.measurements = report.get("measurements") or {}
    job.agent = report.get("agent") or {}
    job.verification = report.get("verification") or {}
    job.reconciliation = report.get("reconciliation") or {}
    assets = report.get("assets") or {}
    job.evidence_manifest = assets.get("evidence") or _read_job_json(job_id, "work/evidence/evidence_manifest.json")
    job.artifact_registry = assets.get("artifacts") or _read_job_json(job_id, "work/artifacts/artifact_registry.json")
    job.artifact_qa = assets.get("artifact_qa") or _read_job_json(job_id, "work/artifacts/artifact_qa.json")
    patient_pdf = _find_patient_pdf(job_id)
    if patient_pdf:
        job.pdf_path = str(patient_pdf)
    meta = _load_meta(job_id) or {}
    for name, rel in (meta.get("images") or {}).items():
        p = _safe_join(job_id, rel)
        if p:
            job.annotated_images[name] = str(p)
    return job


EXPECTED_VERSIONS = {"numpy": "1.26.4", "scipy": "1.12.0", "pydicom": "2.4.4", "Pillow": "10.2.0"}


def check_env() -> dict:
    """Confirm the read pipeline still imports AND that the ABI-critical deps match their pins.
    Catches an out-of-band `pip install` that drifted numpy/scipy after boot. Non-raising —
    returns a structured result; callers decide whether to fail the read or just surface it."""
    versions, mismatches, import_error = {}, [], None
    try:
        import numpy, scipy, pydicom, PIL
        from scipy.signal import find_peaks            # the exact read-path imports (dicom_engine.py:24-25)
        from scipy.ndimage import gaussian_filter1d
        versions = {"numpy": numpy.__version__, "scipy": scipy.__version__,
                    "pydicom": pydicom.__version__, "Pillow": PIL.__version__}
        if int(str(numpy.__version__).split(".")[0]) >= 2:
            mismatches.append(f"numpy {numpy.__version__} is >=2 — scipy {scipy.__version__} needs the numpy 1.26 ABI")
        for pkg, want in EXPECTED_VERSIONS.items():
            got = versions.get(pkg)
            if got and got != want:
                mismatches.append(f"{pkg} {got} != pinned {want}")
    except Exception as e:
        import_error = f"{type(e).__name__}: {e}"
    return {"ok": import_error is None and not mismatches,
            "versions": versions, "mismatches": mismatches, "import_error": import_error}


def _assert_env_for_read() -> None:
    """Cheap per-read guard: a mid-session dependency drift fails the job cleanly with an
    actionable message instead of an opaque crash deep in the pipeline."""
    env = check_env()
    if not env["ok"]:
        detail = env["import_error"] or "; ".join(env["mismatches"])
        raise RuntimeError(f"environment changed — restart MIKA ({detail})")


def _classify_run_error(message: Optional[str]) -> str:
    text = (message or "").lower()
    if any(token in text for token in ("not logged in", "not signed in", "login", "auth status", "authentication")):
        return "CLAUDE_NOT_SIGNED_IN"
    if "claude code cli not found" in text or "not installed" in text:
        return "CLAUDE_CLI_MISSING"
    if "environment changed" in text:
        return "ENVIRONMENT_CHANGED"
    if "timed out" in text:
        return "AGENT_TIMEOUT"
    return "AGENT_RUN_FAILED"


def _warn_if_onedrive_data_dir() -> None:
    """Deferred item: a OneDrive-synced data dir can lock files mid-read (an F1 trigger). Warn
    only — never a hard gate."""
    try:
        if "onedrive" in str(DATA_DIR).lower():
            logger.warning("MIKA_DATA_DIR appears to be inside OneDrive — cloud file locks/syncs can "
                           "interrupt a read. Prefer a local disk path (set MIKA_DATA_DIR).")
    except Exception:
        pass


# ── Fix 1: disk heartbeat, dead-worker recovery, and watchdog ──
#
# Reads run in-process via BackgroundTasks, so a *process* kill (OOM, OneDrive lock, OS reap)
# bypasses the try/except and freezes the job at a non-terminal status forever. The defenses:
#   1. each progress tick writes status.json with a wall-clock heartbeat (a killed worker leaves it stale)
#   2. boot reconciliation salvages a finished-but-unpersisted report, or marks an interrupted one error
#   3. a watchdog flips a live job stuck past the timeout+margin to error so SSE/status terminate honestly
WATCHDOG_MARGIN_S = int(os.environ.get("MIKA_WATCHDOG_MARGIN_S", "120"))


def _write_status_file(
    job_id: str,
    status: str,
    progress: int,
    message: str,
    heartbeat_ts: float,
    truncated: bool = False,
    error_code: Optional[str] = None,
    auth_state: Optional[str] = None,
    progress_phase: Optional[str] = None,
) -> None:
    """Write DATA_DIR/<job>/status.json (the on-disk heartbeat). Non-fatal."""
    try:
        jd = _job_dir(job_id)
        jd.mkdir(parents=True, exist_ok=True)
        (jd / "status.json").write_text(json.dumps({
            "job_id": job_id, "status": status, "progress": progress,
            "message": message, "heartbeat_ts": heartbeat_ts, "truncated": truncated,
            "error_code": error_code, "auth_state": auth_state,
            "progress_phase": _normalized_progress_phase(status, progress_phase),
        }), encoding="utf-8")
    except Exception as e:
        logger.debug(f"status.json write failed for {job_id}: {e}")


def _write_status_heartbeat(job: "AnalysisJob") -> None:
    """Stamp a fresh heartbeat for a live job and mirror its state to disk."""
    ts = time.time()
    job.heartbeat_ts = ts
    _write_status_file(
        job.job_id,
        job.status,
        job.progress,
        job.progress_message or "",
        ts,
        bool(getattr(job, "truncated", False)),
        getattr(job, "error_code", None),
        getattr(job, "auth_state", None),
        getattr(job, "progress_phase", job.status),
    )


def _load_status(job_id: str) -> Optional[dict]:
    try:
        p = _job_dir(job_id) / "status.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8-sig"))  # tolerate a BOM from any writer
    except Exception as e:
        logger.warning(f"Could not read status for {job_id}: {e}")
    return None


def _salvage_report(job_id: str, out_dir: Path, pdfs: list, summary: dict, truncated: bool = False) -> None:
    """F5: a previous process finished a DELIVERABLE report (PDF + a real patient summary on disk)
    but died before persisting it. Reconstruct a minimal completed job from the artifacts and
    persist it via the existing _persist_report so the finished study becomes retrievable.
    The caller owns the deliverability gate (_has_patient) — mirroring the live success path so a
    clinical-PDF-only / patient-less crash is never promoted to 'complete' on the recovery path."""
    job = AnalysisJob(job_id=job_id, dicom_dir=str(_job_dir(job_id) / "dicom"))
    job.mode = "agent"
    job.status = "complete"
    job.progress = 100
    job.truncated = bool(truncated)   # preserve the Fix 4 "may be incomplete" signal across recovery
    pngs = sorted(out_dir.glob("*.png"))
    for p in pngs:
        job.annotated_images[p.stem] = str(p)
    seqthumb_dir = _job_dir(job_id) / "work" / "seqthumbs"
    if seqthumb_dir.exists():
        for p in sorted(seqthumb_dir.glob("*.png")):
            job.annotated_images.setdefault(p.stem, str(p))   # for the Recent-studies thumbnail
    job.pdf_path = str(pdfs[0])  # report.pdf sorts before report_clinical.pdf
    top_study = summary.get("study") if isinstance(summary.get("study"), dict) else {}
    job.agent = {
        "success": True, "pdf_available": True,
        "figures": [p.stem for p in pngs], "summary": summary,
        "result_text": "", "num_turns": 0, "cost_usd": 0.0, "error": None,
        "salvaged": True, "truncated": job.truncated,
    }
    job.measurements = {
        "detected_anatomy": "unknown", "anatomy_subregion": "",
        "modality": top_study.get("modality", ""),
        "calibration_status": summary.get("calibration_status", "unknown"),
        "study_description": summary.get("study_description", ""),
        "agent_summary": summary,
    }
    _persist_report(job)


def _reconcile_job_from_disk(job_id: str) -> Optional[str]:
    """Bring one job dir to an honest terminal state. Idempotent.
      • already persisted (report.json + meta.json) → no-op
      • finished PDF + a REAL patient summary on disk, never persisted → salvage → 'complete'
      • a non-terminal status.json, or partial/patient-less artifacts → mark 'error' (interrupted)
    Salvage uses the SAME deliverability gate as the live success path (Fix 4): a clinical-PDF-only
    / patient-less crash is never promoted to 'complete' on the recovery path — it falls through to
    'error' instead. Returns the resulting terminal status, or None if nothing was done."""
    jd = _job_dir(job_id)
    if (jd / "report.json").exists() and (jd / "meta.json").exists():
        return None
    out_dir = jd / "work" / "report"
    summary_file = out_dir / "summary.json"
    pdfs = sorted(out_dir.glob("*.pdf")) if out_dir.exists() else []
    st = _load_status(job_id)
    prior_truncated = bool(st.get("truncated")) if st else False
    has_artifacts = bool(pdfs) and summary_file.exists()
    if has_artifacts:
        try:
            summary = _normalize_summary(json.loads(summary_file.read_text(encoding="utf-8-sig")))
        except Exception:
            summary = _normalize_summary({})
        if AgentRunner._has_patient(summary):   # deliverable only — same gate as the live run
            try:
                _salvage_report(job_id, out_dir, pdfs, summary, truncated=prior_truncated)
                _write_status_file(job_id, "complete", 100, "Report recovered after restart",
                                   time.time(), truncated=prior_truncated)
                logger.info(f"Salvaged finished report for job {job_id}")
                return "complete"
            except Exception as e:
                logger.warning(f"Salvage failed for {job_id}: {e}")
    # Not salvageable: a non-terminal status.json, OR artifacts that fail the deliverable gate
    # (partial/patient-less). Either way the read did not finish a real report → mark it interrupted.
    if (st and st.get("status") not in (None, "complete", "error")) or has_artifacts:
        _write_status_file(job_id, "error", (st or {}).get("progress", 0),
                           "read was interrupted — please re-run", time.time(),
                           truncated=prior_truncated)
        logger.info(f"Marked interrupted job {job_id} as error")
        return "error"
    return None


def _reconcile_jobs_on_boot() -> int:
    """At startup JOBS is empty and any worker that owned an in-flight read is gone, so every
    non-terminal job on disk is orphaned. Salvage or mark each. Returns the count handled."""
    handled = 0
    try:
        for d in DATA_DIR.iterdir():
            if not d.is_dir() or not JOB_ID_RE.match(d.name):
                continue
            if _reconcile_job_from_disk(d.name):
                handled += 1
    except Exception as e:
        logger.warning(f"Boot reconciliation scan failed: {e}")
    return handled


def _maybe_expire_job(job: "AnalysisJob") -> bool:
    """Watchdog: a live job that's stayed non-terminal past the agent timeout + margin without a
    fresh heartbeat is treated as dead — flip it to error so SSE/status terminate honestly instead
    of looping at ~95% forever. Returns True if it expired the job."""
    if job.status in ("complete", "error"):
        return False
    if time.time() - (job.heartbeat_ts or 0) <= AGENT_TIMEOUT_S + WATCHDOG_MARGIN_S:
        return False
    job.status = "error"
    job.error = job.error or "read timed out — please re-run"
    job.error_code = job.error_code or "AGENT_TIMEOUT"
    job.progress_phase = "error"
    job.progress_message = "Read timed out"
    _write_status_heartbeat(job)
    logger.warning(f"Watchdog expired stuck job {job.job_id} (no heartbeat for >{AGENT_TIMEOUT_S + WATCHDOG_MARGIN_S}s)")
    return True


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
    reference_report_path: Optional[str] = None
    reference_report_text: Optional[str] = None
    notify_email: Optional[str] = None   # §7.7: opt-in "we'll email you when it's ready" (survives tab close)

class ReconcileRequest(BaseModel):
    job_id: str
    reference_report_path: Optional[str] = None
    reference_report_text: Optional[str] = None


class AuthStartRequest(BaseModel):
    mode: str = "browser"  # browser | code


class AuthCodeRequest(BaseModel):
    code: str


class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: int
    progress_message: str
    created_at: str
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    auth_state: Optional[str] = None
    progress_phase: Optional[str] = None
    eta_seconds: Optional[int] = None
    est_total_seconds: Optional[int] = None
    active_sequence: Optional[str] = None
    active_region: Optional[str] = None
    truncated: bool = False   # Fix 4: read hit the time cap with partial output — re-run recommended


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


@app.get("/api/agent/preflight")
async def agent_preflight():
    """Cheap readiness check before a full opus/high analysis."""
    return AgentRunner().readiness_probe()


@app.get("/health")
async def health():
    """Fix 3: liveness + read-environment check. 200 when the read pipeline imports and the
    ABI-critical deps match their pins; 503 (degraded) with the specific mismatch otherwise."""
    env = check_env()
    return JSONResponse(
        {"status": "ok" if env["ok"] else "degraded", **env},
        status_code=200 if env["ok"] else 503,
    )


@app.post("/api/connect")
async def connect_claude(request: Optional[AuthStartRequest] = None, console: bool = False):
    """
    Start an in-app Claude auth session. Browser sign-in is the default; code mode is
    available for environments where the CLI asks for a pasted code.
    """
    mode = "code" if console else ((request.mode if request else "browser") or "browser")
    return AUTH_MANAGER.start(mode=mode)


@app.get("/api/connect/{session_id}")
async def poll_claude_connect(session_id: str):
    return AUTH_MANAGER.poll(session_id)


@app.post("/api/connect/{session_id}/retry")
async def retry_claude_connect(session_id: str, request: Optional[AuthStartRequest] = None):
    mode = (request.mode if request else "browser") or "browser"
    return AUTH_MANAGER.retry(session_id, mode=mode)


@app.post("/api/connect/{session_id}/cancel")
async def cancel_claude_connect(session_id: str):
    return AUTH_MANAGER.cancel(session_id)


@app.post("/api/connect/{session_id}/code")
async def submit_claude_code(session_id: str, request: AuthCodeRequest):
    return AUTH_MANAGER.submit_code(session_id, request.code)


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
    job.error = None
    job.error_code = None
    job.progress_message = ""
    job.progress_phase = "queued"

    if mode == "agent":
        # Agent mode runs the skill via Claude Code on your subscription — no API key needed.
        avail = AgentRunner().readiness_probe()
        job.auth_state = avail.get("auth_state")
        if not avail.get("ready"):
            job.status = "error"
            job.progress = 0
            job.progress_phase = "auth"
            job.error_code = avail.get("error_code") or "CLAUDE_NOT_READY"
            job.error = avail.get("error_message") or "Claude is not ready. Sign in and retry."
            job.progress_message = job.error
            _write_status_heartbeat(job)
            return JSONResponse(
                {
                    "detail": job.error,
                    "job_id": job.job_id,
                    "status": "error",
                    "error_code": job.error_code,
                    "error_message": job.error,
                    "auth_state": job.auth_state,
                    "preflight": avail.get("preflight"),
                },
                status_code=400,
            )
        if not avail.get("ready") and not avail.get("claude_cli_found"):
            raise HTTPException(
                400,
                "Agent mode needs the Claude Code CLI installed and logged in on your "
                "subscription. Install it (npm i -g @anthropic-ai/claude-code), run "
                "`claude` once to log in, then retry — or use mode='lite' with an API key.",
            )
        job.status = "inventory"
        job.progress = 0
        job.progress_phase = "inventory"
        job.auth_state = "connected"
        background_tasks.add_task(
            _run_agent_pipeline,
            job=job,
            api_key=request.api_key or "",          # per-user credential from sign-in; else host login
            auth_token=request.auth_token or "",
            clinical_history=request.clinical_history,
            surgical_notes=request.surgical_notes,
            prior_reports=request.prior_reports,
            reference_report_path=request.reference_report_path,
            reference_report_text=request.reference_report_text,
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
    job.progress_phase = "inventory"
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
        # No live run. First bring the on-disk job to an honest terminal state (Fix 1): salvage a
        # finished-but-unpersisted report, or mark an interrupted one as error.
        _reconcile_job_from_disk(job_id)
        meta = _load_meta(job_id)
        if meta:
            st = meta.get("status", "complete")
            return JobStatus(
                job_id=job_id, status=st, progress=100 if st == "complete" else 0,
                progress_message="Report ready" if st == "complete" else (meta.get("error") or ""),
                created_at=meta.get("created_at", ""), error=meta.get("error"),
                error_code=meta.get("error_code"),
                error_message=meta.get("error_message") or meta.get("error"),
                auth_state=meta.get("auth_state"),
                progress_phase=_normalized_progress_phase(st, meta.get("progress_phase")),
                truncated=bool(meta.get("truncated")),
            )
        disk = _load_status(job_id)
        if disk:
            st = disk.get("status", "error")
            return JobStatus(
                job_id=job_id, status=st, progress=disk.get("progress", 0),
                progress_message=disk.get("message", ""), created_at="",
                error=disk.get("message") if st == "error" else None,
                error_code=disk.get("error_code"),
                error_message=disk.get("message") if st == "error" else None,
                auth_state=disk.get("auth_state"),
                progress_phase=_normalized_progress_phase(st, disk.get("progress_phase")),
                truncated=bool(disk.get("truncated")),
            )
        raise HTTPException(404, f"Job {job_id} not found")

    _maybe_expire_job(job)   # watchdog: close out a stuck live job before reporting
    return JobStatus(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        progress_message=job.progress_message,
        created_at=job.created_at,
        error=job.error,
        error_code=getattr(job, "error_code", None),
        error_message=job.error,
        auth_state=getattr(job, "auth_state", None),
        progress_phase=_normalized_progress_phase(job.status, getattr(job, "progress_phase", None)),
        eta_seconds=job.eta_seconds,
        est_total_seconds=job.est_total_seconds,
        active_sequence=job.active_sequence,
        active_region=job.active_region,
        truncated=bool(getattr(job, "truncated", False)),
    )


@app.get("/api/status/{job_id}/stream")
async def stream_status(job_id: str):
    """Server-Sent Events stream for real-time progress updates."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        # No live run: reconcile the on-disk job (Fix 1), then emit one terminal event and close.
        _reconcile_job_from_disk(job_id)
        payload = None
        meta = _load_meta(job_id)
        if meta:
            st = meta.get("status", "complete")
            payload = {"status": st, "progress": 100 if st == "complete" else 0,
                       "message": "Report ready" if st == "complete" else (meta.get("error") or ""),
                       "error": meta.get("error"),
                       "error_code": meta.get("error_code"),
                       "error_message": meta.get("error_message") or meta.get("error"),
                       "auth_state": meta.get("auth_state"),
                       "progress_phase": _normalized_progress_phase(st, meta.get("progress_phase")),
                       "truncated": bool(meta.get("truncated"))}
        else:
            disk = _load_status(job_id)
            if disk:
                st = disk.get("status", "error")
                payload = {"status": st, "progress": disk.get("progress", 0),
                           "message": disk.get("message", ""),
                           "error": disk.get("message") if st == "error" else None,
                           "error_code": disk.get("error_code"),
                           "error_message": disk.get("message") if st == "error" else None,
                           "auth_state": disk.get("auth_state"),
                           "progress_phase": _normalized_progress_phase(st, disk.get("progress_phase")),
                           "truncated": bool(disk.get("truncated"))}
        if payload is not None:
            async def _one():
                yield f"data: {json.dumps(payload)}\n\n"
            return StreamingResponse(_one(), media_type="text/event-stream")
        raise HTTPException(404, f"Job {job_id} not found")

    async def event_generator():
        last = None
        since_ping = 0.0
        while True:
            _maybe_expire_job(job)   # watchdog: terminate a stuck live stream honestly
            snapshot = (job.progress, job.eta_seconds, job.status, job.active_sequence, job.active_region, job.progress_message, job.truncated)
            if snapshot != last or job.status in ("complete", "error"):
                data = json.dumps({
                    "status": job.status,
                    "progress": job.progress,
                    "message": job.progress_message,
                    "error": job.error,
                    "error_code": getattr(job, "error_code", None),
                    "error_message": job.error,
                    "auth_state": getattr(job, "auth_state", None),
                    "progress_phase": _normalized_progress_phase(job.status, getattr(job, "progress_phase", None)),
                    "eta_seconds": job.eta_seconds,
                    "est_total_seconds": job.est_total_seconds,
                    "active_sequence": job.active_sequence,
                    "active_region": job.active_region,
                    "truncated": job.truncated,
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
    summary = _summary_for_job(job)
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
    elif summary:
        interpretation_dict = _interpretation_from_summary(summary)

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
    patient_pdf = _find_patient_pdf(job.job_id, job)
    clinical_pdf = _find_clinical_pdf(job.job_id, job)
    reconciliation = (
        _coerce_dict(getattr(job, "reconciliation", {}))
        or _coerce_dict(summary.get("reconciliation"))
        or _coerce_dict((job.agent or {}).get("reconciliation") if getattr(job, "agent", None) else {})
    )

    payload = {
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
        "reconciliation": reconciliation,
        "pdf_available": bool(patient_pdf),
        "clinical_pdf_available": bool(clinical_pdf),
        "truncated": bool(getattr(job, "truncated", False)),   # Fix 4: read may be incomplete — re-run recommended
    }
    payload.update(_normalized_report_sections(
        job=job,
        summary=summary,
        interpretation_dict=interpretation_dict,
        figures=figures,
        detected_anatomy=detected_anatomy,
        anatomy_subregion=anatomy_subregion,
        calibration_status=payload["calibration_status"],
        patient_pdf=patient_pdf,
        clinical_pdf=clinical_pdf,
    ))
    return payload


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
        return _normalize_loaded_report(job_id, disk)
    raise HTTPException(404, "Report not found")


@app.get("/api/reports")
async def list_reports():
    """Durable index of all completed studies on this machine (backs the 'Recent studies' screen).
    Reads DATA_DIR manifests — survives restarts and browser-storage clears."""
    return {"reports": _list_reports()}


@app.post("/api/reconcile")
async def reconcile_completed_report(request: ReconcileRequest):
    """Add a separate reference-assisted review to an already completed blind read."""
    _validate_job_id(request.job_id)
    if not (request.reference_report_path or request.reference_report_text):
        raise HTTPException(400, "Provide a reference report path or reference report text.")
    job = JOBS.get(request.job_id) or _rehydrate_completed_job(request.job_id)
    if not job:
        raise HTTPException(404, "Report not found")
    if job.status != "complete":
        raise HTTPException(400, f"Analysis not complete (status: {job.status})")
    try:
        _apply_reference_reconciliation(
            job,
            reference_report_path=request.reference_report_path,
            reference_report_text=request.reference_report_text,
        )
    except ReferenceInputError as e:
        raise HTTPException(400, str(e))
    _persist_report(job)
    return _build_report_payload(job)


@app.post("/api/reconcile/upload")
async def reconcile_completed_report_upload(
    job_id: str = Form(...),
    reference_report_text: Optional[str] = Form(None),
    reference_report: Optional[UploadFile] = File(None),
):
    """Browser-friendly reference upload/paste path for completed blind reads."""
    _validate_job_id(job_id)
    text_parts: list[str] = []
    if reference_report_text and reference_report_text.strip():
        text_parts.append(reference_report_text.strip())
    if reference_report and reference_report.filename:
        data = await reference_report.read(MAX_REFERENCE_REPORT_BYTES + 1)
        try:
            text_parts.append(read_reference_report_bytes(reference_report.filename, data))
        except ReferenceInputError as e:
            raise HTTPException(400, str(e))
    if not text_parts:
        raise HTTPException(400, "Upload a reference report PDF/text file or paste report text.")

    job = JOBS.get(job_id) or _rehydrate_completed_job(job_id)
    if not job:
        raise HTTPException(404, "Report not found")
    if job.status != "complete":
        raise HTTPException(400, f"Analysis not complete (status: {job.status})")
    try:
        _apply_reference_reconciliation(job, reference_report_text="\n\n".join(text_parts))
    except ReferenceInputError as e:
        raise HTTPException(400, str(e))
    _persist_report(job)
    return _build_report_payload(job)


@app.get("/api/report/{job_id}/pdf")
async def get_report_pdf(job_id: str):
    """Serve the patient-facing PDF report, from the live job or from disk."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        # Restart / cache miss: serve the durable PDF recorded in the on-disk manifest.
        meta = _load_meta(job_id)
        if meta and meta.get("pdf_available") and meta.get("pdf"):
            p = _safe_join(job_id, meta["pdf"])
            if p and p.name == "report.pdf":
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
        patient_pdf = candidate / "report.pdf"
        pdf_path = str(patient_pdf) if patient_pdf.exists() else None
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(404, "No PDF report available for this job")
    if Path(pdf_path).name != "report.pdf":
        raise HTTPException(404, "No patient PDF report available for this job")
    # Never serve a file outside this job's own directory.
    if not _safe_join(job_id, _rel_to_job(job_id, pdf_path)):
        raise HTTPException(404, "No PDF report available for this job")
    return FileResponse(pdf_path, media_type="application/pdf", filename="mika_report.pdf")


@app.get("/api/report/{job_id}/clinical-pdf")
async def get_clinical_report_pdf(job_id: str):
    """Serve the preserved clinician/technical PDF, if the agent produced it."""
    _validate_job_id(job_id)
    job = JOBS.get(job_id)
    if job and job.status != "complete":
        raise HTTPException(400, f"Analysis not complete (status: {job.status})")
    pdf_path = _find_clinical_pdf(job_id, job)
    if not pdf_path:
        raise HTTPException(404, "No clinician PDF report available for this job")
    return FileResponse(str(pdf_path), media_type="application/pdf", filename="mika_clinical_report.pdf")


@app.get("/api/reports/{job_id}/pdf")
async def get_report_pdf_alias(job_id: str):
    return await get_report_pdf(job_id)


@app.get("/api/reports/{job_id}/clinical-pdf")
async def get_clinical_report_pdf_alias(job_id: str):
    return await get_clinical_report_pdf(job_id)


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
    job.error_code = "CANCELLED"
    job.progress_phase = "cancelled"
    job.progress_message = "Cancelled"
    _write_status_heartbeat(job)   # Fix 1: terminal heartbeat so boot recovery sees a clean error, not "interrupted"
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
    reference_report_path: Optional[str] = None,
    reference_report_text: Optional[str] = None,
    notify_email: Optional[str] = None,
):
    """
    Agent mode: run the mri-spine-analysis skill via Claude Code (your subscription),
    exactly the way cowork produced the definitive PDF report.
    """
    try:
        _assert_env_for_read()   # Fix 3: fail cleanly if deps drifted mid-session (numpy/scipy ABI)
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

        evidence_manifest = _prepare_evidence_pack(job, study_root)

        runner = AgentRunner(api_key=api_key, auth_token=auth_token)
        est = _estimate_agent_seconds(n_studies=1, effort=runner.effort)
        job.est_total_seconds = est
        job.eta_seconds = est
        job.status = "interpreting"
        job.progress_phase = "interpreting"
        job.progress = 8
        job.progress_message = "Launching analysis on your subscription..."
        _write_status_heartbeat(job)   # Fix 1: first on-disk heartbeat (boot recovery can detect a kill from here on)

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
            evidence_manifest_path=_evidence_manifest_path(job),
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
            _write_status_heartbeat(job)   # Fix 1: refresh the on-disk heartbeat each tick
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
        job.truncated = bool(getattr(result, "truncated", False))   # Fix 4: ran past the time cap with partial output
        job.agent = {
            "success": result.success,
            "pdf_available": bool(result.pdf_path),
            "figures": [Path(p).stem for p in result.figures],
            "summary": result.summary,
            "result_text": result.result_text,
            "num_turns": result.num_turns,
            "cost_usd": result.cost_usd,
            "error": result.error,
            "truncated": job.truncated,
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
            "evidence_pack": {
                "study": (evidence_manifest or {}).get("study", {}),
                "selected_image_count": len((evidence_manifest or {}).get("selected_images", [])),
                "cv_candidate_count": len((evidence_manifest or {}).get("cv_candidates", [])),
                "cv_candidates": (evidence_manifest or {}).get("cv_candidates", []),
                "cv_candidate_limitations": (evidence_manifest or {}).get("cv_candidate_limitations", []),
                "limitations": (evidence_manifest or {}).get("limitations", []),
            },
            "cv_candidates": (evidence_manifest or {}).get("cv_candidates", []),
            "cv_candidate_limitations": (evidence_manifest or {}).get("cv_candidate_limitations", []),
        }
        _run_artifact_qa(job)
        if result.success and (reference_report_path or reference_report_text):
            try:
                _apply_reference_reconciliation(
                    job,
                    reference_report_path=reference_report_path,
                    reference_report_text=reference_report_text,
                )
            except ReferenceInputError as e:
                job.status = "error"
                job.error = str(e)
                job.error_code = "REFERENCE_REPORT_ERROR"
                job.progress_phase = "error"
                job.progress_message = str(e)
                _write_status_heartbeat(job)
                _notify_email(notify_email, job.job_id, "error")
                return

        if result.success:
            job.status = "complete"
            job.progress_phase = "complete"
            job.error = None
            job.error_code = None
            job.auth_state = "connected"
            job.progress = 100
            job.progress_message = (
                "Analysis complete — this read may be incomplete; re-run recommended"
                if job.truncated else "Agent analysis complete — PDF report ready"
            )
            logger.info(f"Agent job {job.job_id} complete: pdf ready, {len(result.figures)} figures"
                        f"{' (TRUNCATED)' if job.truncated else ''}")
            _persist_report(job)   # durable: survives restart, indexes into Recent studies
            _write_status_heartbeat(job)   # Fix 1: terminal heartbeat
            _notify_email(notify_email, job.job_id, "complete")
        else:
            job.status = "error"
            job.error = result.error or "Agent run failed without producing a report"
            job.error_code = _classify_run_error(job.error)
            job.auth_state = "signed_out" if job.error_code == "CLAUDE_NOT_SIGNED_IN" else job.auth_state
            job.progress_phase = "error"
            job.progress_message = f"Agent error: {job.error}"
            _write_status_heartbeat(job)   # Fix 1: terminal heartbeat
            _notify_email(notify_email, job.job_id, "error")
    except Exception as e:
        logger.exception(f"Agent pipeline failed for job {job.job_id}")
        job.status = "error"
        job.error = str(e)
        job.error_code = _classify_run_error(job.error)
        job.auth_state = "signed_out" if job.error_code == "CLAUDE_NOT_SIGNED_IN" else job.auth_state
        job.progress_phase = "error"
        job.progress_message = f"Error: {str(e)}"
        _write_status_heartbeat(job)   # Fix 1: terminal heartbeat
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
        _assert_env_for_read()   # Fix 3: fail cleanly if deps drifted mid-session (numpy/scipy ABI)
        # Rough total for the time-remaining bar (lite is image+API bound, ~3 min).
        # The frontend derives remaining from progress when eta_seconds is not set live.
        job.est_total_seconds = 180
        engine = DICOMEngine(job.dicom_dir, job.work_dir)
        job.engine = engine

        # Phase 0: Inventory & Anatomy Detection
        job.status = "inventory"
        job.progress = 5
        job.progress_message = "Cataloging DICOM files and detecting anatomy type..."
        _write_status_heartbeat(job)   # Fix 1: first on-disk heartbeat (boot recovery can detect a kill from here on)
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
        evidence_manifest = _prepare_evidence_pack(job, str(Path(job.dicom_dir).parent))
        job.measurements["evidence_pack"] = {
            "study": (evidence_manifest or {}).get("study", {}),
            "selected_image_count": len((evidence_manifest or {}).get("selected_images", [])),
            "cv_candidate_count": len((evidence_manifest or {}).get("cv_candidates", [])),
            "cv_candidates": (evidence_manifest or {}).get("cv_candidates", []),
            "cv_candidate_limitations": (evidence_manifest or {}).get("cv_candidate_limitations", []),
            "limitations": (evidence_manifest or {}).get("limitations", []),
        }
        job.measurements["cv_candidates"] = (evidence_manifest or {}).get("cv_candidates", [])
        job.measurements["cv_candidate_limitations"] = (evidence_manifest or {}).get("cv_candidate_limitations", [])

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
            evidence_manifest=evidence_manifest,
        )
        image_content_blocks, image_count = batch_sender.build_message_content()
        if evidence_manifest:
            image_content_blocks.insert(0, {"type": "text", "text": manifest_text_summary(evidence_manifest)})
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
                "cv_candidate_reviews": verified.cv_candidate_reviews,
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
        _run_artifact_qa(job)

        # Complete
        job.status = "complete"
        job.progress = 100
        job.progress_message = "Analysis complete"
        logger.info(f"Job {job.job_id} complete — {image_count} images analyzed with verification")
        _persist_report(job)   # durable: survives restart, indexes into Recent studies
        _write_status_heartbeat(job)   # Fix 1: terminal heartbeat

    except Exception as e:
        logger.exception(f"Analysis failed for job {job.job_id}")
        job.status = "error"
        job.error = str(e)
        job.progress_message = f"Error: {str(e)}"
        _write_status_heartbeat(job)   # Fix 1: terminal heartbeat


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
    "Cache-Control": "no-store",
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

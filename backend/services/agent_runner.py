"""
AgentRunner — run the mri-spine-analysis SKILL via Claude Code (headless), the same way
cowork produced the definitive report.
=============================================================================
Instead of reimplementing the skill as a fixed Python pipeline (the "lite" mode in
app.py), this drives the Claude Code CLI in print/headless mode with tools (bash, python,
read, write). The agent loads the skill, does its own slice-by-slice analysis, places and
verifies annotations, performs multi-study longitudinal comparison, and authors a PDF
report — i.e. it *is* the skill, not an approximation of it.

Auth: by default this uses Claude Code's stored login (your Claude subscription). The
child process has ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN stripped so the subscription
OAuth login is used — the app "only needs the subscription." Set MIKA_AGENT_USE_API_KEY=1
to instead let an env API key through (metered billing).
"""

import os
import json
import shutil
import logging
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

try:
    from services.evidence_pack import cv_candidate_text_summary, load_manifest, manifest_text_summary
except ImportError:
    from backend.services.evidence_pack import cv_candidate_text_summary, load_manifest, manifest_text_summary

logger = logging.getLogger("mika.agent")

# Vendored skill shipped with the app (self-contained — no dependency on a plugin session).
SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "mri-spine-analysis" / "SKILL.md"

DEFAULT_MODEL = os.environ.get("MIKA_AGENT_MODEL", "opus")
DEFAULT_PERMISSION_MODE = os.environ.get("MIKA_AGENT_PERMISSION_MODE", "bypassPermissions")
DEFAULT_TIMEOUT_S = int(os.environ.get("MIKA_AGENT_TIMEOUT_S", "3600"))  # 60 min (max-effort runs are slow)
DEFAULT_EFFORT = os.environ.get("MIKA_AGENT_EFFORT", "high")  # low|medium|high|xhigh|max

ANATOMY_LABELS = {
    "spine": "spine", "brain": "brain / neuro", "msk": "musculoskeletal", "cardiac": "cardiac",
    "chest": "chest", "abdomen": "abdomen / pelvis", "breast": "breast", "vascular": "vascular / MRA",
    "head_neck": "head & neck", "prostate": "prostate", "unknown": "medical imaging",
}

# DICOM Modality tag (0008,0060) -> plain label. Used so a CT / X-ray / ultrasound study is
# read with the right physics instead of being forced through MRI sequence logic (T1/T2/STIR).
MODALITY_LABELS = {
    "MR": "MRI", "CT": "CT", "CR": "X-ray (radiograph)", "DX": "X-ray (radiograph)",
    "RF": "fluoroscopy", "XA": "angiography (X-ray)", "MG": "mammography (X-ray)",
    "US": "ultrasound", "PT": "PET", "NM": "nuclear medicine", "OT": "image",
}
# How to read each non-MR modality (anti-hallucination: do not invent MR signal on non-MR data).
MODALITY_READING = {
    "CT": ("Base findings on CT attenuation (Hounsfield units), reviewing bone and soft-tissue "
           "windows; describe density (hypo-/iso-/hyperdense), calcification, fat, fluid, gas, and "
           "contrast enhancement if a contrast phase is present."),
    "CR": ("Base findings on radiographic density: alignment, cortical integrity, lucency vs "
           "sclerosis, joint spaces, soft-tissue gas/swelling. This is a 2D projection — depth and "
           "soft-tissue detail are limited."),
    "DX": ("Base findings on radiographic density: alignment, cortical integrity, lucency vs "
           "sclerosis, joint spaces, soft-tissue gas/swelling. This is a 2D projection — depth and "
           "soft-tissue detail are limited."),
    "MG": ("Base findings on mammographic density: masses, asymmetries, architectural distortion, "
           "and calcification morphology/distribution; report a BI-RADS category."),
    "XA": ("Base findings on vascular opacification: stenosis, occlusion, aneurysm, flow."),
    "US": ("Base findings on echogenicity (an-/hypo-/hyperechoic), through-transmission, and "
           "Doppler flow if present."),
    "PT": ("Base findings on tracer uptake (e.g. SUV) and its anatomical correlate."),
    "NM": ("Base findings on radiotracer distribution and focal uptake."),
    "OT": ("The source was a plain image file with no DICOM modality tag. FIRST identify the "
           "actual modality from the image itself (MRI, CT, radiograph/X-ray, ultrasound, etc.), "
           "then read and report using that modality's features. Do not assume MRI."),
}


def _resolve_claude_bin(claude_bin: Optional[str] = None) -> Optional[str]:
    binp = claude_bin or os.environ.get("MIKA_CLAUDE_BIN") or shutil.which("claude") or "claude"
    return shutil.which(binp) or (binp if os.path.exists(binp) else None)


def _subscription_auth_env() -> dict:
    env = dict(os.environ)
    # The normal MIKA path is the user's Claude subscription login, not API-key billing.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _classify_auth_result(returncode: int, stdout: str, stderr: str) -> dict:
    raw = (stdout or "").strip()
    err = (stderr or "").strip()
    combined = f"{raw}\n{err}".strip()
    info = {
        "connected": False,
        "auth_state": "signed_out",
        "subscription_type": None,
        "error_code": "CLAUDE_NOT_SIGNED_IN",
        "error_message": "Sign in with Claude before starting the read.",
    }
    brace = raw.find("{")
    if brace >= 0:
        try:
            data = json.loads(raw[brace:])
            logged_in = bool(data.get("loggedIn") or data.get("logged_in") or data.get("authenticated"))
            if logged_in:
                return {
                    "connected": True,
                    "auth_state": "connected",
                    "subscription_type": data.get("subscriptionType") or data.get("subscription_type"),
                    "error_code": None,
                    "error_message": None,
                }
            msg = data.get("error") or data.get("message")
            if msg:
                info["error_message"] = str(msg)
        except Exception:
            pass
    low = combined.lower()
    if any(token in low for token in ("not logged in", "not signed in", "login required", "please log in")):
        return info
    if "logged in" in low or "authenticated" in low or "signed in" in low:
        return {
            **info,
            "connected": True,
            "auth_state": "connected",
            "error_code": None,
            "error_message": None,
        }
    if returncode != 0 and combined:
        info["error_message"] = combined[:500]
    return info


def detect_study_modality(study_dir) -> str:
    """Read the DICOM Modality tag (0008,0060) from a few files so routing/prompts match the
    actual modality. Returns a code like 'MR', 'CT', 'CR', 'DX', 'US', ... or 'MR' as the
    default (this app is MRI-first; converted NIfTI/NRRD/image imports carry no real modality)."""
    study = Path(study_dir)
    try:
        import pydicom
    except Exception:
        return "MR"
    counts: dict[str, int] = {}
    try:
        for f in list(study.rglob("*.dcm"))[:12]:
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                mod = str(getattr(ds, "Modality", "")).strip().upper()
                if mod:
                    counts[mod] = counts.get(mod, 0) + 1
            except Exception:
                pass
    except Exception:
        pass
    if not counts:
        return "MR"
    # Most common real modality wins.
    return max(counts.items(), key=lambda kv: kv[1])[0]


@dataclass
class ClaudeAuthSession:
    session_id: str
    mode: str
    claude_bin: str
    started_at: float
    state: str = "pending"
    message: str = ""
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    needs_code: bool = False
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    stdout_lines: list = field(default_factory=list, repr=False)
    stderr_lines: list = field(default_factory=list, repr=False)


class ClaudeAuthSessionManager:
    """In-memory auth sessions for the desktop login flow."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClaudeAuthSession] = {}
        self._lock = threading.Lock()

    def _append_pipe(self, session: ClaudeAuthSession, pipe, attr: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break
                with self._lock:
                    getattr(session, attr).append(line)
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _availability(self, claude_bin: Optional[str] = None) -> dict:
        return AgentRunner(claude_bin=claude_bin).readiness_probe()

    def _snapshot(self, session: ClaudeAuthSession, availability: Optional[dict] = None) -> dict:
        availability = availability or self._availability(session.claude_bin)
        connected = bool(availability.get("connected"))
        if connected:
            session.state = "connected"
            session.error_code = None
            session.error_message = None
            session.needs_code = False
            session.message = "Claude is connected."
            self._terminate(session)
        elif session.state not in ("cancelled", "error", "code_required"):
            proc = session.process
            output = "\n".join(session.stdout_lines + session.stderr_lines)
            low = output.lower()
            if session.mode == "code" or any(token in low for token in ("paste", "code", "one-time", "verification")):
                session.state = "code_required"
                session.needs_code = True
                session.message = "Paste the Claude sign-in code to finish connecting."
            if proc and proc.poll() is not None and session.state not in ("code_required", "connected"):
                session.state = "error"
                session.error_code = availability.get("error_code") or "CLAUDE_AUTH_FAILED"
                session.error_message = availability.get("error_message") or output[-500:] or "Claude sign-in did not complete."
                session.message = session.error_message
        return {
            "session_id": session.session_id,
            "started": session.state not in ("error", "cancelled"),
            "mode": session.mode,
            "auth_state": session.state,
            "connected": connected,
            "needs_code": session.needs_code,
            "message": session.message,
            "error_code": session.error_code or availability.get("error_code"),
            "error_message": session.error_message or availability.get("error_message"),
            "subscription_type": availability.get("subscription_type"),
            "availability": availability,
        }

    def _terminate(self, session: ClaudeAuthSession) -> None:
        proc = session.process
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass

    def start(self, mode: str = "browser", claude_bin: Optional[str] = None) -> dict:
        mode = "code" if mode in ("code", "console") else "browser"
        resolved = _resolve_claude_bin(claude_bin)
        if not resolved:
            return {
                "started": False,
                "auth_state": "missing_cli",
                "connected": False,
                "error_code": "CLAUDE_CLI_MISSING",
                "error_message": "Claude is not installed or bundled with this MIKA build.",
                "message": "Claude is not installed or bundled with this MIKA build.",
            }

        availability = self._availability(resolved)
        if availability.get("connected"):
            session = ClaudeAuthSession(
                session_id=str(uuid.uuid4())[:8],
                mode=mode,
                claude_bin=resolved,
                started_at=time.time(),
                state="connected",
                message="Claude is already connected.",
            )
            with self._lock:
                self._sessions[session.session_id] = session
            return self._snapshot(session, availability)

        args = [resolved, "auth", "login", "--console" if mode == "code" else "--claudeai"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        session = ClaudeAuthSession(
            session_id=str(uuid.uuid4())[:8],
            mode=mode,
            claude_bin=resolved,
            started_at=time.time(),
            needs_code=mode == "code",
            state="code_required" if mode == "code" else "pending",
            message=("Paste the Claude sign-in code to finish connecting."
                     if mode == "code" else "A browser window is opening. Sign in to Claude there."),
        )
        try:
            proc = subprocess.Popen(
                args,
                env=_subscription_auth_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            session.process = proc
            if proc.stdout:
                threading.Thread(target=self._append_pipe, args=(session, proc.stdout, "stdout_lines"), daemon=True).start()
            if proc.stderr:
                threading.Thread(target=self._append_pipe, args=(session, proc.stderr, "stderr_lines"), daemon=True).start()
        except Exception as e:
            session.state = "error"
            session.error_code = "CLAUDE_AUTH_START_FAILED"
            session.error_message = str(e)
            session.message = str(e)
        with self._lock:
            self._sessions[session.session_id] = session
        return self._snapshot(session, availability)

    def poll(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {
                "session_id": session_id,
                "started": False,
                "auth_state": "expired",
                "connected": False,
                "error_code": "AUTH_SESSION_NOT_FOUND",
                "error_message": "That sign-in session expired. Start sign-in again.",
            }
        return self._snapshot(session)

    def retry(self, session_id: str, mode: str = "browser") -> dict:
        self.cancel(session_id)
        return self.start(mode=mode)

    def cancel(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {
                "session_id": session_id,
                "started": False,
                "auth_state": "cancelled",
                "connected": False,
                "message": "Sign-in cancelled.",
            }
        self._terminate(session)
        session.state = "cancelled"
        session.needs_code = False
        session.message = "Sign-in cancelled."
        return self._snapshot(session)

    def submit_code(self, session_id: str, code: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {
                "session_id": session_id,
                "started": False,
                "auth_state": "expired",
                "connected": False,
                "error_code": "AUTH_SESSION_NOT_FOUND",
                "error_message": "That sign-in session expired. Start sign-in again.",
            }
        code = (code or "").strip()
        if not code:
            session.state = "code_required"
            session.needs_code = True
            session.error_code = "AUTH_CODE_REQUIRED"
            session.error_message = "Paste the Claude sign-in code."
            return self._snapshot(session)
        if not session.process or not session.process.stdin:
            return self.retry(session_id, mode="code")
        try:
            session.process.stdin.write(code + "\n")
            session.process.stdin.flush()
            session.state = "pending"
            session.needs_code = False
            session.error_code = None
            session.error_message = None
            session.message = "Checking the pasted Claude sign-in code..."
        except Exception as e:
            session.state = "error"
            session.error_code = "AUTH_CODE_SUBMIT_FAILED"
            session.error_message = str(e)
            session.message = str(e)
        return self._snapshot(session)


AUTH_MANAGER = ClaudeAuthSessionManager()


def trigger_claude_login(claude_bin: Optional[str] = None, console: bool = False) -> dict:
    """
    Start the Claude sign-in flow: launches `claude auth login`, which opens the user's
    browser to sign in to their own Claude account. Designed for the desktop/EXE build —
    the "Connect with Claude" button calls this; the browser opens, the patient signs in,
    and the connection becomes ready. Non-blocking: returns immediately; the UI then polls
    /api/agent/availability until `connected` flips true.
    """
    return AUTH_MANAGER.start(mode="code" if console else "browser", claude_bin=claude_bin)
    binp = claude_bin or os.environ.get("MIKA_CLAUDE_BIN") or shutil.which("claude") or "claude"
    resolved = shutil.which(binp) or (binp if os.path.exists(binp) else None)
    if not resolved:
        return {"started": False,
                "error": "Claude is not installed on this computer. The MIKA app should "
                         "bundle it; if you are running from source, install Claude Code first."}
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # force the interactive subscription login
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    args = [resolved, "auth", "login", "--console" if console else "--claudeai"]
    try:
        # Detach: it opens a browser and completes its own OAuth callback.
        subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"started": True, "message": "A browser window is opening — sign in to your Claude account."}
    except Exception as e:
        return {"started": False, "error": str(e)}


def detect_study_anatomy(study_dir) -> str:
    """Best-effort anatomy detection for routing the agent (reuses the engine's detector).
    Scans DICOM headers and filenames so a brain/chest/knee/etc. upload is NOT run through
    the spine protocol."""
    study = Path(study_dir)
    try:
        from core.dicom_engine import DICOMEngine
    except ImportError:
        from backend.core.dicom_engine import DICOMEngine

    body_part, study_desc = "", ""
    try:
        import pydicom
        for f in list(study.rglob("*.dcm"))[:8]:
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                body_part = body_part or str(getattr(ds, "BodyPartExamined", "")).strip()
                study_desc = study_desc or str(getattr(ds, "StudyDescription", "")).strip()
                if body_part or study_desc:
                    break
            except Exception:
                pass
    except Exception:
        pass

    try:
        names = [p.name for p in list(study.rglob("*"))[:300]]
    except Exception:
        names = []
    try:
        return DICOMEngine._detect_anatomy(body_part, study_desc, names) or "unknown"
    except Exception:
        return "unknown"


@dataclass
class AgentResult:
    success: bool
    report_dir: str = ""
    pdf_path: Optional[str] = None
    figures: list = field(default_factory=list)     # produced PNG/figure paths
    summary: dict = field(default_factory=dict)      # parsed summary.json if the agent wrote one
    result_text: str = ""                            # the agent's final printed message
    num_turns: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None
    raw_meta: dict = field(default_factory=dict)
    truncated: bool = False                          # produced output but hit the time cap — may be incomplete (Fix 4)


def _as_list_str(v) -> list:
    """Coerce a value to a clean list[str] (drops empties). Mirrors the impression coercion at
    claude_interpreter.py:393-401."""
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str):
        return [v] if v.strip() else []
    if v:
        return [str(v)]
    return []


def _as_list_dict(v) -> list:
    """Coerce to a list[dict], dropping non-dict items so a downstream `.get(...)` can't crash.
    Non-destructive of valid (dict) findings."""
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        return [v]
    return []


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


CV_CANDIDATE_STATUSES = {"supported", "not_supported", "cannot_assess", "localization_wrong", "unstable"}


def _normalize_cv_candidate_reviews(value) -> list[dict]:
    rows = []
    for row in _as_list_dict(value):
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
        rows.append({
            "candidate_id": candidate_id,
            "status": status,
            "evidence_refs_used": [str(ref) for ref in refs if str(ref).strip()],
            "short_reason": str(row.get("short_reason") or row.get("reason") or "").strip(),
            "patient_wording": _plain_patient_string(row.get("patient_wording") or ""),
            "clinician_wording": str(row.get("clinician_wording") or "").strip(),
            "pre_post_enhancement_support": str(row.get("pre_post_enhancement_support") or "").strip(),
            "level_side_localization": str(row.get("level_side_localization") or "").strip(),
            "visible_evidence_reason": str(row.get("visible_evidence_reason") or "").strip(),
        })
    return rows


def _plain_patient_string(text: str) -> str:
    """Keep image-export limitations honest without leaking clinician calibration jargon."""
    out = str(text or "")
    replacements = [
        (r"\bMeasurements are not calibrated\.", "Exact measurements may not be reliable from these exported pictures."),
        (r"\bmeasurements are not calibrated\b", "exact measurements may not be reliable from these exported pictures"),
        (r"\buncalibrated picture exports?\b", "exported pictures without scale information"),
        (r"\buncalibrated image exports?\b", "exported pictures without scale information"),
        (r"\buncalibrated exported images?\b", "exported pictures without scale information"),
        (r"\buncalibrated\b", "without scale information"),
        (r"\bnot calibrated\b", "not based on a reliable scale"),
        (r"\bcalibrated measurements?\b", "exact measurements"),
        (r"\bcalibration\b", "scale information"),
        (r"\bDICOM\b", "scan file"),
        (r"\bPixelSpacing\b", "scale information"),
        (r"\bimage-export MRI\b", "MRI made from exported pictures"),
    ]
    for pattern, replacement in replacements:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out


def _plain_patient_copy(value):
    if isinstance(value, dict):
        return {k: _plain_patient_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_patient_copy(v) for v in value]
    if isinstance(value, str):
        return _plain_patient_string(value)
    return value


def _normalize_summary(summary) -> dict:
    """Fix 2 — shape-only normalization of the agent's summary.json so a string/null where the
    report builder expects a list/dict can't garble or crash the PDF (the char-by-char
    `impression` incident). Non-destructive of valid findings; missing keys → safe defaults.

    The patient block is what report_builder.build_patient_report renders, so it gets the full
    shape treatment; the top-level technical fields used by other consumers are coerced too.
    """
    if not isinstance(summary, dict):
        return {"patient": {}}
    s = dict(summary)

    # Top-level technical fields (clinician PDF / SPA consumers).
    s["impression"] = _as_list_str(s.get("impression"))
    s["findings"] = _as_list_dict(s.get("findings"))
    s["cv_candidate_reviews"] = _normalize_cv_candidate_reviews(s.get("cv_candidate_reviews"))
    if not isinstance(s.get("discrepancies"), list):
        s["discrepancies"] = _as_list_str(s.get("discrepancies"))

    # Patient block (rendered by report_builder — every shape here is load-bearing).
    p = _as_dict(s.get("patient"))
    p["patient"] = _as_dict(p.get("patient"))
    p["study"] = _as_dict(p.get("study"))
    p["confidence"] = _as_dict(p.get("confidence"))
    p["key_points"] = _as_list_str(p.get("key_points"))
    p["what_it_means"] = _as_list_str(p.get("what_it_means"))
    p["worth_flagging"] = _as_list_str(p.get("worth_flagging"))
    p["findings"] = _as_list_dict(p.get("findings"))
    cot = _as_dict(p.get("change_over_time"))
    cot["points"] = _as_list_str(cot.get("points"))
    p["change_over_time"] = cot
    if not isinstance(p.get("bottom_line"), str):
        p["bottom_line"] = str(p.get("bottom_line") or "")
    if not isinstance(p.get("disclaimer"), str):
        p["disclaimer"] = str(p.get("disclaimer") or "")
    s["patient"] = _plain_patient_copy(p)
    return s


class AgentRunner:
    """Run the spine skill end-to-end via Claude Code headless."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        effort: str = DEFAULT_EFFORT,
        claude_bin: Optional[str] = None,
        api_key: str = "",       # per-user credential (from sign-in); else host login
        auth_token: str = "",    # per-user subscription token (from `claude setup-token`)
    ):
        self.model = model
        self.permission_mode = permission_mode
        self.timeout_s = timeout_s
        self.effort = effort
        self.api_key = api_key
        self.auth_token = auth_token
        self.claude_bin = claude_bin or os.environ.get("MIKA_CLAUDE_BIN") or shutil.which("claude") or "claude"

    # ── Availability / self-check ──

    def availability(self) -> dict:
        """Report whether the Claude Code CLI is installed and how auth will resolve."""
        path = _resolve_claude_bin(self.claude_bin)
        info = {
            "claude_cli_found": path is not None,
            "claude_bin": path or self.claude_bin,
            "skill_present": SKILL_PATH.exists(),
            "uses_api_key": bool(os.environ.get("MIKA_AGENT_USE_API_KEY")),
            "version": None,
            "auth_mode": "subscription (Claude Code login)",
            "connected": False,           # is the host actually signed in to Claude?
            "subscription_type": None,    # e.g. "max", "pro"
            "auth_state": "missing_cli" if path is None else "signed_out",
            "error_code": "CLAUDE_CLI_MISSING" if path is None else "CLAUDE_NOT_SIGNED_IN",
            "error_message": (
                "Claude is not installed or bundled with this MIKA build."
                if path is None else "Sign in with Claude before starting the read."
            ),
        }
        if info["uses_api_key"] and os.environ.get("ANTHROPIC_API_KEY"):
            info["auth_mode"] = "api_key (metered)"
            info["connected"] = True
            info["subscription_type"] = "api"
            info["auth_state"] = "connected"
            info["error_code"] = None
            info["error_message"] = None
        if path:
            try:
                out = subprocess.run(
                    [path, "--version"], capture_output=True, text=True, timeout=20
                )
                info["version"] = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
            except Exception as e:
                info["version_error"] = str(e)
            if not info["connected"]:
                # Real, free login check (no inference): `claude auth status` returns JSON.
                try:
                    auth = subprocess.run(
                        [path, "auth", "status"], capture_output=True, text=True, timeout=20
                    )
                    auth_info = _classify_auth_result(auth.returncode, auth.stdout or "", auth.stderr or "")
                    info.update({k: v for k, v in auth_info.items() if v is not None or k in ("error_code", "error_message")})
                except Exception as e:
                    info["auth_error"] = str(e)
                    info["auth_state"] = "unknown"
                    info["error_code"] = "CLAUDE_AUTH_STATUS_FAILED"
                    info["error_message"] = str(e)
        info["ready"] = bool(info["claude_cli_found"] and info["skill_present"] and info["connected"])
        return info

    def readiness_probe(self) -> dict:
        """Cheap preflight for the UI and /api/analyze gate.

        This checks CLI presence, skill presence, and auth status only. It does not invoke
        an opus/high model run or inspect study images.
        """
        info = self.availability()
        info["preflight"] = {
            "kind": "claude_auth_status",
            "uses_model": False,
            "runs_analysis": False,
        }
        if not info.get("skill_present"):
            info["ready"] = False
            info["error_code"] = info.get("error_code") or "MIKA_SKILL_MISSING"
            info["error_message"] = f"MIKA's analysis skill is missing at {SKILL_PATH}."
        return info

    # ── Child environment ──

    def _child_env(self) -> dict:
        env = dict(os.environ)
        if self.api_key:
            # Per-user API key → Claude Code uses it (metered to that user's API account).
            env["ANTHROPIC_API_KEY"] = self.api_key
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        elif self.auth_token:
            # Per-user subscription token (from `claude setup-token`).
            env["ANTHROPIC_AUTH_TOKEN"] = self.auth_token
            env.pop("ANTHROPIC_API_KEY", None)
        elif not os.environ.get("MIKA_AGENT_USE_API_KEY"):
            # No per-user credential → fall back to the host's Claude Code login.
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def _finalize_patient_report(self, out_dir: Path, summary: dict) -> None:
        """
        Render the patient-first report.pdf from summary['patient'] (deterministic).
        The agent's own technical PDF, if any, is preserved as report_clinical.pdf.
        Failures are non-fatal — the agent's PDF is left in place.
        """
        patient = (summary or {}).get("patient")
        if not patient:
            return
        try:
            # Host-side deterministic render of any model-emitted annotation specs, so the
            # proof figures exist (and follow the renderer's rules) even if the model's own
            # draw failed. Non-fatal: a render error leaves the model's figures in place.
            self._render_host_annotations(out_dir)
            try:
                from backend.services.report_builder import build_patient_report
            except ImportError:
                from services.report_builder import build_patient_report
            agent_pdf = out_dir / "report.pdf"
            if agent_pdf.exists():
                try:
                    agent_pdf.replace(out_dir / "report_clinical.pdf")
                except Exception:
                    pass
            build_patient_report(patient, out_dir, out_dir / "report.pdf")
            logger.info("Patient-first report rendered (technical version kept as report_clinical.pdf)")
        except Exception as e:
            logger.warning(f"Patient report render failed (keeping agent PDF): {e}")

    def _render_host_annotations(self, out_dir: Path) -> None:
        """Render model-emitted annotation specs deterministically (host-side fallback).

        ``annotations.json`` (when present) is a list of figure entries::

            [{"figure": "figX.png", "base": "rawY.png", "title": "...",
              "calibrated": bool, "max_marks": int, "marks": [<spec>, ...]}, ...]

        Each entry whose base image resolves is re-rendered onto ``out_dir/figure`` via the
        shared annotation_renderer (model-chosen forms, calibrated-only numbers, certainty
        colour + legend). Entries with no resolvable base are left to the model's own figure.
        """
        ann_path = out_dir / "annotations.json"
        if not ann_path.exists():
            return
        try:
            entries = json.loads(ann_path.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"annotations.json unreadable, skipping host render: {e}")
            return
        if isinstance(entries, dict):
            entries = entries.get("figures") or [entries]
        if not isinstance(entries, list):
            return
        try:
            from backend.core.annotation_renderer import render_all
        except ImportError:
            from core.annotation_renderer import render_all
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            marks = entry.get("marks") or []
            figure = entry.get("figure")
            if not figure or not marks:
                continue
            base = self._resolve_base_image(out_dir, entry.get("base") or figure)
            if base is None:
                continue
            try:
                res = render_all(
                    base, marks, out_dir / figure,
                    calibrated=bool(entry.get("calibrated")),
                    max_marks=entry.get("max_marks"),
                    title=entry.get("title"),
                )
                logger.info("Host-rendered %s: %d marks, %d dropped",
                            figure, res["rendered"], len(res["dropped"]))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Host render of {figure} failed (keeping model figure): {e}")

    @staticmethod
    def _resolve_base_image(out_dir: Path, base_name) -> Optional[Path]:
        """Find a base image for a figure: by name in out_dir, out_dir/work, or as a path."""
        if not base_name:
            return None
        candidates = [out_dir / base_name, out_dir / "work" / base_name, Path(base_name)]
        for c in candidates:
            try:
                if c.exists() and c.is_file():
                    return c
            except OSError:
                continue
        return None

    def _collect_outputs(self, out_dir: Path, result: AgentResult, require_pdf: bool) -> None:
        """Gather figures + summary.json, render the patient report, set pdf_path.
        Called on BOTH the normal and timeout paths so a run that produced output before
        the cap is never reported as a total failure."""
        if not out_dir.exists():
            return
        result.figures = [str(p) for p in sorted(out_dir.glob("*.png"))]
        summary_file = out_dir / "summary.json"
        if summary_file.exists():
            try:
                # Normalize the shape immediately (Fix 2) so a malformed summary can't reach the
                # PDF builder as a string-where-a-list-is-expected.
                result.summary = _normalize_summary(json.loads(summary_file.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"Could not parse summary.json: {e}")
        self._finalize_patient_report(out_dir, result.summary)
        # Re-glob: _finalize may have host-rendered new annotation figures from annotations.json.
        result.figures = [str(p) for p in sorted(out_dir.glob("*.png"))]
        pdfs = sorted(out_dir.glob("*.pdf"))
        result.pdf_path = str(pdfs[0]) if pdfs else None  # report.pdf sorts before report_clinical.pdf

    @staticmethod
    def _has_patient(summary: dict) -> bool:
        """True if the summary carries a real patient answer (not just the empty scaffold that
        _normalize_summary always builds). The prompt mandates a bottom_line; we also accept
        findings/key_points so a slightly-off-shape but substantive block still counts."""
        p = (summary or {}).get("patient") or {}
        if not isinstance(p, dict):
            return False
        return bool(p.get("bottom_line") or p.get("findings") or p.get("key_points"))

    @classmethod
    def _is_deliverable(cls, result: AgentResult, require_pdf: bool) -> bool:
        """A full run (require_pdf) needs BOTH a PDF AND a real patient summary (Fix 4) — a
        clinical-PDF-only run no longer passes. Focused runs (require_pdf=False) pass on figures."""
        if require_pdf:
            return (result.pdf_path is not None) and cls._has_patient(result.summary)
        return len(result.figures) > 0

    # ── Prompt ──

    def _build_prompt(
        self,
        study_dir: Path,
        out_dir: Path,
        anatomy: str = "spine",
        protocol_ref: Optional[Path] = None,
        prior_studies: Optional[list] = None,
        clinical_history: Optional[str] = None,
        surgical_notes: Optional[str] = None,
        prior_reports: Optional[str] = None,
        modality: str = "MR",
        evidence_manifest_path: Optional[str] = None,
    ) -> str:
        label = ANATOMY_LABELS.get(anatomy, "medical imaging")
        is_spine = anatomy == "spine"
        protocol_ref = protocol_ref or SKILL_PATH
        progress_path = out_dir.parent / "progress.json"   # drives the live Wait readout (honest, no fabricated counts)

        # Modality discipline: the protocols are MRI-tuned, so on a non-MR study tell the agent
        # to apply that modality's physics and NOT to fabricate MR sequences/signal.
        modality = (modality or "MR").upper()
        modality_label = MODALITY_LABELS.get(modality, modality or "imaging")
        modality_block = ""
        if modality != "MR":
            how = MODALITY_READING.get(modality, "Apply interpretation appropriate to this modality.")
            modality_block = (
                f"\nMODALITY — this is a {modality_label} study (DICOM Modality: {modality}), NOT MRI:\n"
                f"  - {how}\n"
                f"  - The protocol below is written for MRI. Use its ANATOMICAL search checklist, "
                f"grading structure, and confidence-tier discipline (these are modality-independent), "
                f"but DO NOT assume MRI pulse sequences (T1/T2/STIR/FLAIR) and DO NOT report MR signal "
                f"characteristics — they do not exist on {modality_label}. Report only what THIS modality shows.\n"
            )

        priors = ""
        if prior_studies:
            priors = "\nPRIOR STUDY DIRECTORIES (older timepoints, for longitudinal comparison):\n" + \
                     "\n".join(f"  - {p}" for p in prior_studies)
        context = ""
        if clinical_history:
            context += f"\nClinical history / indication (symptoms only — NOT a prior read):\n{clinical_history}\n"
        if surgical_notes:
            context += f"\nSurgical / operative notes (use ONLY in reconciliation, AFTER your blind read):\n{surgical_notes}\n"
        if prior_reports:
            context += f"\nPrior radiology reports (use ONLY in reconciliation, AFTER your blind read):\n{prior_reports}\n"
        evidence_block = ""
        if evidence_manifest_path:
            try:
                evidence_manifest = load_manifest(evidence_manifest_path)
                evidence_summary = manifest_text_summary(evidence_manifest)
                cv_candidate_summary = cv_candidate_text_summary(evidence_manifest)
            except Exception as e:
                evidence_summary = f"Evidence manifest could not be read before prompt assembly: {e}"
                cv_candidate_summary = ""
            evidence_block = f"""
EVIDENCE PACK - mandatory primary review set:
  Manifest path: {evidence_manifest_path}
  Selected image files are listed in that JSON manifest under selected_images[].relative_path.
{evidence_summary}

Rules for this evidence pack:
  - Treat selected evidence images as the explicit set MIKA sent for this read. State limitations
    when a structure, plane, side, or level is not represented.
  - Every technical finding in summary.json MUST include evidence_refs: ["ev###", ...] citing
    one or more selected evidence IDs from the manifest.
  - For every finding, include series/sequence, evidence image or slice, plane, side/laterality
    if assessable, level/region if assessable, confidence tier, and calibration_basis.
  - If the evidence is insufficient for a location, laterality, level, or measurement, output
    Tier D / cannot assess for that element. Do not guess and do not place a precise marker.
  - On uncalibrated image exports, do not report precise measurements and do not use pinpoint
    annotations. Use region boxes only when location evidence is adequate.
"""
            if cv_candidate_summary:
                evidence_block += f"""

CV EVIDENCE CANDIDATES - separate localization review, not findings:
{cv_candidate_summary}

Rules for CV candidates:
  - Review each candidate separately from the blind findings and write a top-level
    summary.json field named cv_candidate_reviews.
  - For every candidate, output exactly one status from:
    supported, not_supported, cannot_assess, localization_wrong, unstable.
  - Required fields per row:
    candidate_id, status, evidence_refs_used, short_reason, patient_wording, clinician_wording,
    pre_post_enhancement_support, level_side_localization, visible_evidence_reason.
  - supported means the candidate localization/ROI is visually supported by the specific images
    you reviewed and the cited refs. For pre/post contrast candidates, state whether the same-level
    pre/post comparison visibly supports enhancement or difference in pre_post_enhancement_support.
    It does NOT by itself confirm scar, recurrent disc, nerve-root encasement, or any diagnosis.
  - If a candidate includes proof_bundle images, review those internal proof images first, then
    verify against selected_evidence_refs or evidence_refs when needed. The proof bundle is a
    localization candidate only and is not patient-facing proof.
  - Answer the candidate's bounded_question directly; do not broaden it into a full radiology read.
  - not_supported means you reviewed the candidate region and did not see supporting visual evidence.
  - cannot_assess means the sequence, registration, slices, image quality, or evidence set is insufficient.
  - localization_wrong means the candidate level, side, slice, or ROI is wrong.
  - unstable means the candidate has mixed/borderline visual support or your repeated checks disagree.
  - Tie short_reason and visible_evidence_reason to visible image evidence, not general impressions.
  - State in level_side_localization whether the cited refs support the candidate level and side.
  - If a candidate is rejected or cannot be assessed, explain why in short_reason.
  - Do not make broad negative statements such as "no abnormality" or "no enhancement" from a bounded
    candidate review. If the bounded region is not supported, say only that the candidate is not supported
    or cannot be assessed from the reviewed evidence.
  - Do not upgrade a CV candidate into a confirmed finding unless the actual images independently
    support that finding and you cite image evidence. Preserve the blind read separately.
  - If artifact_trust says body_marker/proof_overlay/pinpoint_marker is false, do not create
    a body-map marker, pinpoint annotation, or proof overlay from that candidate alone.
"""

        # Anatomy-specific vs general wording (so a non-spine study is NOT forced through spine logic).
        if is_spine:
            ref_fig = "Figure 0 = Level Reference (midline, sacrum-up labels)"
            location_rule = ("Confirm the vertebral LEVEL of every mark by counting from the sacrum on "
                             "Figure 0. If you cannot confirm a level, render a labelled region band "
                             "\"approx Lx-Ly\", NOT a pinpoint circle. Never assert a level you cannot confirm.")
            shifting_rule = ("Structures whose plane shifts slice-to-slice (neural foramina, nerve-in-foramen) "
                             "-> use a REGION box, never a false-pinpoint arrow.")
            reading_extra = (
                "  - On post-contrast, EXPLICITLY evaluate the symptomatic nerve root for intrinsic "
                "enhancement vs the normal contralateral root; report neuritis ONLY if the root itself "
                "enhances above the contralateral baseline. EXPLICITLY check facet joints for enhancing "
                "synovitis. Grade canal/foraminal stenosis with explicit severity.\n"
                "  - In every lumbar study, actively inspect for prior hemilaminectomy/laminotomy/"
                "discectomy anatomy even if no surgical history is supplied. On contrast studies, "
                "distinguish enhancing epidural fibrosis/scar from non-enhancing residual or recurrent "
                "disc material in the lateral recess. Specifically assess whether enhancing or "
                "non-enhancing tissue encases, displaces, or impinges the descending S1/L5 nerve root, "
                "and cite the exact side, level, series/image evidence, and confidence tier. Do not "
                "state 'no abnormal enhancement' until the operative bed, lateral recesses, foramina, "
                "and nerve roots have been compared on pre/post fat-saturated images. Do not make a "
                "negative 'no prior surgery', 'no epidural fibrosis/scar', 'no residual/recurrent disc', "
                "or 'no root impingement' call from sagittal images or sparse representative samples "
                "alone: for lower-lumbar/L5-S1 negatives, inspect the full axial T1/T2 and matched "
                "pre/post contrast stacks, opening the raw DICOM files if the evidence pack does not "
                "contain the exact slice. If that full axial comparison is not completed, report the "
                "post-surgical/root assessment as limited rather than negative.\n")
        else:
            ref_fig = "Figure 0 = a labelled overview/reference figure that orients the rest of the figures"
            location_rule = ("Confirm the anatomical location of every mark against the labelled reference "
                             "figure (Figure 0). If you cannot confirm the location, render a labelled region "
                             "band, NOT a pinpoint circle. Never assert a location you cannot confirm.")
            shifting_rule = ("Structures whose location shifts across slices, or whose boundary is ambiguous, "
                             "-> use a REGION box, never a false-pinpoint arrow.")
            reading_extra = (
                "  - On post-contrast, evaluate abnormal enhancement appropriately for this anatomy "
                "(same-level/same-region pre vs post). Grade severity explicitly (mild/moderate/severe).\n")

        return f"""You are running a clinical-grade {label} {modality_label} analysis.

DETECTED ANATOMY: {anatomy}.  DETECTED MODALITY: {modality_label} ({modality}).
FOLLOW THIS PROTOCOL EXACTLY — read it first, then execute it:
  {protocol_ref}
Use that protocol for the systematic search, grading, and confidence tiers. Produce the
OUTPUTS specified at the bottom of THIS message (they override any output format in the protocol).
{modality_block}
PRIMARY STUDY DIRECTORY (current study — DICOM and/or images):
  {study_dir}
{evidence_block}
{priors}{context}
TOOLS: You have bash, python, read, and write. Do your own slice-by-slice analysis —
convert DICOM with windowing, run intensity profiling for landmark/structure localization
and annotation, pixel-verify annotations, and re-read each annotated image. pydicom, numpy,
scipy, and Pillow are installed; pip install matplotlib and reportlab if you need them.

LIVE PROGRESS — the patient watches a status while you work, so keep them informed (and honest):
  each time you BEGIN a new major step, OVERWRITE {progress_path} with a tiny JSON object:
    {{"active_sequence": "<the sequence you are reading right now, e.g. 'Sag T2', or ''>",
      "region": "<the level/region/structure you are EXAMINING right now — e.g. 'L4-L5', 'S1', 'frontal lobe', 'medial meniscus' — or ''>",
      "note": "<ONE short plain sentence of what you are doing now, e.g. 'Reading the sagittal T2 — checking the lumbar levels'>"}}
  Update it whenever you move to the next sequence/region/step. Write plain language (no jargon, no tier letters).
  "region" is WHERE you are looking, not a finding — it drives a live highlight, so keep it to the area under review.
  Do NOT invent slice numbers, counts, or findings you have not actually computed — report only the real step you are on.

ORDERING (critical): perform the BLIND READ on the images BEFORE reading the surgical notes
/ prior reports above; only use those in the reconciliation step.

ANNOTATION PRECISION — every annotation must be pixel-accurate AND informative:
  - Localize each structure by intensity analysis, place the tip, then VERIFY the tip's
    3x3 pixel intensity against the expected range for that structure. If it fails,
    auto-search the neighborhood and reposition; if none matches, fall back to a labelled
    REGION BAND (approximate, not a pinpoint) — never ship a wrong pinpoint, and never silently
    drop the finding's visual. Re-read every saved figure and confirm on-target.
  - {location_rule}
  - {shifting_rule}
  - On UNCALIBRATED (JPG/screenshot) studies, mark with REGION bands, not pinpoint circles.
  - For each finding choose the slice where it is MAXIMAL (scan the stack; no fixed index).
  - Keep on-image labels SHORT (one line, ~6 words): structure + finding + certainty word. Put any
    measurements, ratios, reasoning, or Tier in the figure CAPTION, not on the on-image label.
  - Place labels in the margin with a thin leader line so text never overlaps the anatomy.
  - PICK THE CLEAREST FORM per finding (you choose; the renderer draws it):
      focal point (disc, nodule, focal signal) -> circle or arrow
      an area / fuzzy boundary (region, edema, "approximate") -> box
      a linear extent or measurement (canal AP diameter, a distance) -> caliper
      a level/structure reference (label a level, point at a normal) -> leader
      UNCALIBRATED studies -> box or leader only (never a pinpoint circle/arrow)
  - COLOUR ENCODES CERTAINTY (Confirmed/Likely/Possible), not severity — the renderer colours
    the marks and draws a legend.
  - The NUMBER is shown ONLY when calibrated and carries explicit units; uncalibrated -> omit the
    number (use a qualitative word in the label), never a fabricated mm.
  - Emit a SPEC per mark and also save them to `annotations.json` so figures can be re-rendered
    deterministically. It is a list of figure entries (coords are BASE-image pixels):
      [{{"figure": "<output png>", "base": "<the raw slice PNG you drew on>", "title": "...",
         "calibrated": <true|false>, "max_marks": 6, "marks": [
           {{"form": "circle|arrow|box|caliper|leader|ellipse", "center": [col,row]
             (or "bbox":[x0,y0,x1,y1] / "p0":[..],"p1":[..]), "label": "short line",
             "number": <num or omit>, "units": "mm", "certainty": "Confirmed|Likely|Possible",
             "significance": 0.0-1.0, "label_side": "auto"}}, ... ]}}, ... ]

ANNOTATION COVERAGE — mark the RIGHT things, driven by the findings (not the pixels):
  - Every reportable finding gets exactly ONE visual at the slice where it is maximal; the
    `findings[].figure` must point to it. No unmarked key finding.
  - ALWAYS include at least one neutral "normal for comparison" reference (a preserved disc
    beside the degenerate ones, or the normal side vs the abnormal side), certainty "Possible"
    and labelled "normal for comparison" — severity should read by contrast, not by assertion.
  - No orphan marks: every drawn mark ties to a stated finding. Never annotate incidental noise
    or artifact; merge co-located findings. Use `significance` so a crowded figure drops the
    least-significant mark (the renderer logs it) rather than cluttering.

READING RIGOR — read like a radiologist who commits to the findings:
  - Work systematically per the protocol. Call each finding at the severity and confidence the
    images support; do NOT default to the less-severe reading out of caution, and do not skip
    subtle findings. The only hard limit is measurements: no specific mm value without calibration.
{reading_extra}
WRITE ALL OUTPUTS into this directory (create it):
  {out_dir}
Produce:
  1. Annotated proof figures as PNG ({ref_fig}; numbered figures for each finding).
  2. A CLINICIAN/technical PDF named `report_clinical.pdf` (the full clinical format: tiered
     findings with [Tier X] and [See Figure N], reconciliation vs prior reports, disclaimer).
     This is for the doctor — NOT the patient. The patient-facing report is generated
     automatically (see #3), so do NOT hand-write a patient PDF.
  3. A machine-readable `summary.json`. Keep the technical detail here (calibration_status,
     levels, findings [{{text, tier, figure, evidence_refs, series, image, plane, side, level_or_region, calibration_basis}}], impression, discrepancies, incidentals,
     figures [{{file, caption}}], self_audit) AND add a top-level "patient" block — this is
     what the user actually sees, so write it in PLAIN language with NO tier letters, NO pixel
     intensities, NO audit trail, NO calibration/DICOM jargon:
     If CV evidence candidates were provided, summary.json MUST also include:
     "cv_candidate_reviews": [
       {{"candidate_id": "<candidate_id>", "status": "supported|not_supported|cannot_assess|localization_wrong|unstable",
         "evidence_refs_used": ["<series/slice or evidence refs used>", ...],
         "short_reason": "why the candidate was accepted, rejected, cannot be assessed, or judged wrong",
         "pre_post_enhancement_support": "same-level pre/post comparison support or limitation",
         "level_side_localization": "why level and side are acceptable, wrong, or cannot be verified",
         "visible_evidence_reason": "specific visible evidence basis for this status",
         "patient_wording": "plain-language sentence if useful; no jargon",
         "clinician_wording": "technical localization/pathology review note"}}
     ]
     "patient": {{
       "patient": {{"name","age","sex"}},
       "study": {{"body_part" (plain, e.g. "Lower-back (lumbar) spine"), "modality" (e.g.
                 "MRI with contrast"), "date", "comparison" (e.g. "compared with your earlier scans" or "")}},
       "bottom_line": "ONE concise plain sentence - the single answer",
       "key_points": ["3-5 short bullet points summarising the result", ...],
       "confidence": {{"label": "High|Moderate|Low", "score": 0-100, "note": "one plain line"}},
       "findings": [{{"plain": "ONE short bullet (a phrase, not a paragraph)", "certainty": "Confirmed|Likely|Possible",
                     "figure": "<one figure filename>", "caption": "plain caption"}}],
       "change_over_time": {{"points": ["short bullet", ...], "figure": "<longitudinal figure>"}} (omit if single study),
       "what_it_means": ["plain patient-facing implication: symptoms this can match, why it matters, or what to discuss with the clinician; no measurements, sequence names, tier letters, calibration, or radiology jargon", ...],
       "worth_flagging": ["short plain bullet, e.g. a record discrepancy", ...] (optional),
       "disclaimer": "<the mandatory disclaimer verbatim>"
     }}
     Patient copy rules: bottom_line, key_points, findings[].plain, findings[].caption,
     confidence.note, change_over_time, what_it_means, and worth_flagging are for the patient.
     They must explain the result in plain language and should never read like a technical
     interpretation. Keep technical interpretation, differential reasoning, measurements, tiers,
     modality/sequence details, and audit language in the top-level technical fields and
     report_clinical.pdf only.
     STYLE: concise bullets everywhere except bottom_line (one sentence) - no long paragraphs.
     Write in a neutral, patient-readable register: not chatty, not alarming, no first-person
     "we/us/our", and avoid "you/your" unless needed for clarity. Map tiers to plain certainty:
     Tier A -> "Confirmed", Tier B -> "Likely", Tier C -> "Possible". Choose a sensible
     overall confidence.

When finished, print a single JSON object: {{"pdf": "<path>", "summary": "<path to summary.json>", "figures": [<paths>], "status": "complete"}}.
"""

    # ── Run ──

    def run(
        self,
        study_dir: str,
        work_dir: str,
        anatomy: Optional[str] = None,       # None -> auto-detect (spine/brain/chest/...)
        prior_studies: Optional[list] = None,
        clinical_history: Optional[str] = None,
        surgical_notes: Optional[str] = None,
        prior_reports: Optional[str] = None,
        task_prompt: Optional[str] = None,   # override the full-report prompt (focused runs)
        require_pdf: bool = True,            # focused runs succeed on figures alone
        protocol_override: Optional[str] = None,  # use this protocol text instead of the anatomy master (experiment knob; default None = live behavior)
        evidence_manifest_path: Optional[str] = None,
    ) -> AgentResult:
        study = Path(study_dir)
        work = Path(work_dir)
        out_dir = work / "report"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Route by anatomy so a non-spine study is never forced through the spine protocol.
        anatomy = anatomy or detect_study_anatomy(study)
        # Detect modality so a CT / X-ray / ultrasound study is read with the right physics
        # instead of MRI sequence logic (the protocols are MRI-tuned).
        modality = detect_study_modality(study)
        if anatomy == "spine":
            if not SKILL_PATH.exists():
                return AgentResult(success=False, report_dir=str(out_dir),
                                   error=f"Vendored spine skill not found at {SKILL_PATH}")
            protocol_ref = SKILL_PATH
        else:
            protocol_ref = work / "protocol.md"
            if protocol_override:
                # Experiment knob: follow this protocol text verbatim instead of the anatomy master
                # (e.g. a radiograph-native chest protocol in place of the MRI-tuned chest master).
                protocol_ref.write_text(protocol_override, encoding="utf-8")
            else:
                # Non-spine: write that anatomy's master prompt as the protocol the agent follows.
                try:
                    from prompts import get_master_prompt
                except ImportError:
                    from backend.prompts import get_master_prompt
                try:
                    protocol_ref.write_text(get_master_prompt(anatomy), encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Could not write protocol for {anatomy}: {e}; falling back to spine skill")
                    protocol_ref = SKILL_PATH
        logger.info(f"Agent routing: anatomy={anatomy}, modality={modality}, protocol={protocol_ref}")

        prompt = task_prompt or self._build_prompt(
            study, out_dir, anatomy, protocol_ref, prior_studies,
            clinical_history, surgical_notes, prior_reports, modality,
            evidence_manifest_path=evidence_manifest_path,
        )

        # Pass the (large) prompt via STDIN, not argv: the Windows claude.CMD shim runs
        # through cmd.exe, whose command line is capped at 8191 chars — embedding the
        # surgical notes / prior reports in argv overflows it ("command line is too long").
        cmd = [
            self.claude_bin, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--effort", self.effort,
            "--permission-mode", self.permission_mode,
            "--add-dir", str(study), "--add-dir", str(work), "--add-dir", str(Path(protocol_ref).parent),
        ]
        # Grant tool access to each prior-study directory (longitudinal comparison).
        for ps in (prior_studies or []):
            cmd += ["--add-dir", str(ps)]
        logger.info(
            f"Launching Claude Code agent (model={self.model}, perm={self.permission_mode}, "
            f"auth={'api_key' if os.environ.get('MIKA_AGENT_USE_API_KEY') else 'subscription'})"
        )

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work),
                env=self._child_env(),
                input=prompt,            # prompt delivered on stdin (see cmd note above)
                capture_output=True,
                text=True,
                encoding="utf-8",        # force UTF-8 on the pipe — Windows defaults to cp1252,
                errors="replace",        # which can't encode glyphs extracted from PDFs (e.g. )
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            # The agent often finishes its files right around the cap — collect them rather
            # than discard a usable result. A full run still requires a real patient summary
            # (Fix 4) so a clinical-PDF-only partial isn't promoted to a clean success.
            result = AgentResult(success=False, report_dir=str(out_dir),
                                 error=f"Agent timed out after {self.timeout_s}s")
            self._collect_outputs(out_dir, result, require_pdf)
            deliverable = self._is_deliverable(result, require_pdf)
            if deliverable:
                result.success = True
                result.truncated = True   # produced before the cap — flag as possibly incomplete
                result.error = (f"Agent exceeded {self.timeout_s}s but had produced a full report "
                                f"before the cap — this read may be incomplete; re-run recommended.")
            return result
        except FileNotFoundError:
            return AgentResult(success=False, report_dir=str(out_dir),
                               error=f"Claude Code CLI not found ({self.claude_bin}). Install it and run `claude` once to log in on your subscription.")

        result = AgentResult(success=False, report_dir=str(out_dir))

        # Parse the --output-format json envelope (final result + metadata).
        try:
            envelope = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
            result.raw_meta = {k: envelope.get(k) for k in ("subtype", "is_error", "duration_ms", "session_id")}
            result.result_text = envelope.get("result", "") or ""
            result.num_turns = envelope.get("num_turns", 0)
            result.cost_usd = envelope.get("total_cost_usd", 0.0) or 0.0
            agent_failed = bool(envelope.get("is_error"))
        except (json.JSONDecodeError, AttributeError):
            result.result_text = (proc.stdout or "").strip()
            agent_failed = proc.returncode != 0

        # Collect produced artifacts (figures, summary, patient-first PDF) regardless of how
        # the agent phrased its final message.
        self._collect_outputs(out_dir, result, require_pdf)

        if proc.returncode != 0 and not result.pdf_path:
            result.error = (proc.stderr or proc.stdout or "agent exited non-zero").strip()[:2000]
            return result

        # Success if the agent produced the deliverable. Full runs require a PDF AND a real
        # patient summary (Fix 4); focused runs (require_pdf=False) succeed on figures alone.
        deliverable = self._is_deliverable(result, require_pdf)
        result.success = deliverable and not agent_failed
        if not result.success and not result.error:
            if require_pdf and result.pdf_path and not self._has_patient(result.summary):
                result.error = ("Agent produced a PDF but no patient summary — the read may be "
                                "incomplete; re-run recommended.")
            elif require_pdf:
                result.error = "Agent finished but produced no PDF report. See result_text."
            else:
                result.error = "Agent finished but produced no annotated figures. See result_text."
        logger.info(
            f"Agent run done: success={result.success}, pdf={result.pdf_path}, "
            f"figures={len(result.figures)}, turns={result.num_turns}, cost=${result.cost_usd:.2f}"
        )
        return result

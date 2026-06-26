"""
MIKA — Lab / Bloodwork reader service
=====================================
The focused, DICOM-free read path for a lab/bloodwork report (PDF or photo). It renders the report
to page PNGs, hands those page images to Claude Opus via the SAME subscription `claude -p` CLI
transport the imaging agent and the case-chat already use, parses strict structured per-analyte
JSON, and then composes "The Verdict" DETERMINISTICALLY in Python (the safety gate — never an LLM
string).

This module is purely additive and shares nothing with the DICOM/imaging pipeline.

-------------------------------------------------------------------------------------------------
AUTH — subscription CLI, no token required (matches agent_runner / case_chat)
  `read_labs()` drives the installed `claude` CLI in headless mode (`claude -p --output-format json`)
  with the page PNGs granted via `--add-dir`, authenticated by the user's normal Claude login
  (`claude /login` / subscription). By default the child has ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN
  STRIPPED so the host's subscription OAuth login is used — the desktop app "only needs the
  subscription", and no token has to be surfaced. An explicit per-user `api_key` (metered) or
  subscription `auth_token` is still honoured if passed (mirrors AgentRunner._child_env), but is no
  longer required for the read to work.

INCIDENTS #2 — the live Claude call must run on a REAL worker/terminal, never nested in a Claude
session. `read_labs()` spawns `claude -p` and is NOT executed during the build/verification session
(a nested `claude -p` self-call hangs and cannot self-verify). The build ships the transport + the
deterministic gate; the live read runs from the running server worker. `compose_verdict()` and
`render_pages()` are pure / offline and ARE safe to run in-session (and are unit-tested).
-------------------------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

try:
    from backend.prompts.lab_master import LAB_MASTER_PROMPT
    from backend.services.agent_runner import _resolve_claude_bin
except ImportError:  # running with backend/ on sys.path (uvicorn app:app from backend/)
    from prompts.lab_master import LAB_MASTER_PROMPT
    from services.agent_runner import _resolve_claude_bin

logger = logging.getLogger("mika.lab")

# CLI model alias — mirrors AgentRunner's DEFAULT_MODEL ("opus"); the headless CLI resolves the alias
# to the current Opus, so we don't pin a dated id here. Overridable for testing/cost tuning.
LAB_MODEL = os.environ.get("MIKA_LAB_MODEL", "opus")
LAB_EFFORT = os.environ.get("MIKA_LAB_EFFORT", "high")
LAB_TIMEOUT_S = int(os.environ.get("MIKA_LAB_TIMEOUT_S", "600"))  # 10 min cap for a page-image read
MAX_PAGES = 8           # cap how many pages we send to Opus (cost + latency)
HARD_PAGE_LIMIT = 20    # reject reports larger than this with a clear error

_VALID_STATUS = {"low", "normal", "high", "abnormal", "unknown"}
_VALID_TIER = {"Confirmed", "Likely", "Possible"}
_VALID_RANGE_TYPE = {"two_sided_numeric", "one_sided", "qualitative"}
_VALID_RENDER = {"clear", "degraded", "unreadable"}


# ──────────────────────────────────────────────────────────────────────────────
# 1. Render the upload to page PNGs
# ──────────────────────────────────────────────────────────────────────────────

def render_pages(upload_path, out_dir) -> list[Path]:
    """Render a lab upload to page PNGs.

    PDF  -> one PNG per page via PyMuPDF (fitz) at ~150 dpi, capped at MAX_PAGES; a PDF with more
            than HARD_PAGE_LIMIT pages is rejected with a clear ValueError.
    image (png/jpg/jpeg) -> copied/used as a single page PNG.

    Returns the list of page PNG paths (page order). Paths live under `out_dir`.
    """
    upload_path = Path(upload_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not upload_path.is_file():
        raise FileNotFoundError(f"Lab upload not found: {upload_path}")

    ext = upload_path.suffix.lower()
    pages: list[Path] = []

    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise RuntimeError("PyMuPDF (fitz) is required to render lab PDFs. pip install PyMuPDF") from e
        doc = fitz.open(str(upload_path))
        try:
            n = doc.page_count
            if n > HARD_PAGE_LIMIT:
                raise ValueError(
                    f"This report has {n} pages, which is more than MIKA reads at once "
                    f"(max {HARD_PAGE_LIMIT}). Please upload a shorter lab report."
                )
            for i in range(min(n, MAX_PAGES)):
                page = doc.load_page(i)
                pix = page.get_pixmap(dpi=150)
                png = out_dir / f"page_{i}.png"
                pix.save(str(png))
                pages.append(png)
        finally:
            doc.close()
    elif ext in (".png", ".jpg", ".jpeg"):
        png = out_dir / "page_0.png"
        if ext == ".png":
            shutil.copyfile(str(upload_path), str(png))
        else:
            # Normalise JPG -> PNG so the page map and proof view are uniformly PNG.
            try:
                from PIL import Image
                with Image.open(str(upload_path)) as im:
                    im.convert("RGB").save(str(png), "PNG")
            except Exception:
                # Fall back to a raw copy under the original suffix if PIL is unavailable.
                png = out_dir / f"page_0{ext}"
                shutil.copyfile(str(upload_path), str(png))
        pages.append(png)
    else:
        raise ValueError(
            f"Unsupported lab upload type '{ext}'. Upload a PDF, PNG, or JPG lab report."
        )

    if not pages:
        raise ValueError("Could not render any pages from the lab upload.")
    return pages


# ──────────────────────────────────────────────────────────────────────────────
# 2. The live Opus read (worker/terminal-only — see INCIDENTS #2 / AUTH note above)
# ──────────────────────────────────────────────────────────────────────────────

def _lab_auth_env(api_key: str = "", auth_token: str = "") -> dict:
    """Child env for the headless `claude -p` lab read — IDENTICAL policy to AgentRunner._child_env.

    Default desktop posture (no per-user credential): strip ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN
    so the CLI uses the host's subscription OAuth login (`claude /login`). An explicit per-user
    api_key (metered) or subscription auth_token is honoured when passed. MIKA_AGENT_USE_API_KEY=1
    lets an ambient env API key through (shared with the imaging agent)."""
    env = dict(os.environ)
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    elif auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        env.pop("ANTHROPIC_API_KEY", None)
    elif not os.environ.get("MIKA_AGENT_USE_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _build_cli_prompt(page_pngs: list[Path]) -> str:
    """Fold the lab master prompt + the explicit page-image file list into a single headless prompt.
    The CLI agent reads each PNG with its Read tool (granted via --add-dir) and returns STRICT JSON."""
    file_lines = "\n".join(
        f"  - Report page {i} (page_index {i}): {p}" for i, p in enumerate(page_pngs)
    )
    return f"""{LAB_MASTER_PROMPT}

## YOUR TASK NOW
Read the following lab/blood report page image file(s) with your Read tool, in the order listed.
Pages are 0-indexed in this order — use the stated page_index for each row's `page_index`.

PAGE IMAGE FILES:
{file_lines}

Open and read EVERY page image above before answering. Then return STRICT JSON ONLY (one object
matching the schema in the rules above) and NOTHING ELSE — no preamble, no markdown prose, no code
fence commentary. Do not use any tool other than Read. Do not write any files."""


def _parse_lab_json(raw_text: str) -> dict:
    """Strip ```json fences (mirrors claude_interpreter), json.loads, and shallow-validate keys."""
    json_str = raw_text or ""
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    parsed = json.loads(json_str.strip())

    if not isinstance(parsed, dict):
        raise ValueError("Lab read did not return a JSON object")
    results = parsed.get("results")
    signals = parsed.get("signals")
    if not isinstance(results, list):
        raise ValueError("Lab read JSON missing a 'results' list")
    if not isinstance(signals, dict):
        raise ValueError("Lab read JSON missing a 'signals' object")
    # Light per-row normalisation so downstream (compose_verdict / UI) sees stable types.
    norm_results = []
    for r in results:
        if not isinstance(r, dict):
            continue
        status = r.get("status")
        if status not in _VALID_STATUS:
            status = "unknown"
        conf = r.get("confidence")
        if conf not in _VALID_TIER:
            conf = "Possible"
        rtype = r.get("range_type")
        if rtype not in _VALID_RANGE_TYPE:
            rtype = "qualitative"
        try:
            clarity = float(r.get("clarity", 0))
        except (TypeError, ValueError):
            clarity = 0.0
        try:
            page_index = int(r.get("page_index", 0))
        except (TypeError, ValueError):
            page_index = 0
        norm_results.append({
            "plain_name": r.get("plain_name") or r.get("analyte_raw") or "",
            "analyte_raw": r.get("analyte_raw") or "",
            "value": r.get("value"),
            "unit": r.get("unit"),
            "ref_range_text": r.get("ref_range_text"),
            "range_type": rtype,
            "status": status,
            "severity_phrase": r.get("severity_phrase") or "",
            "confidence": conf,
            "plain_meaning": r.get("plain_meaning") or "",
            "clarity": max(0.0, min(1.0, clarity)),
            "page_index": page_index,
            "source_text": r.get("source_text") or "",
        })

    render_quality = signals.get("render_quality")
    if render_quality not in _VALID_RENDER:
        render_quality = "degraded"
    try:
        ec = float(signals.get("extraction_confidence", 0))
    except (TypeError, ValueError):
        ec = 0.0
    try:
        analytes_parsed = int(signals.get("analytes_parsed", len(norm_results)))
    except (TypeError, ValueError):
        analytes_parsed = len(norm_results)
    norm_signals = {
        "extraction_confidence": max(0.0, min(1.0, ec)),
        "analytes_parsed": analytes_parsed,
        "render_quality": render_quality,
    }
    return {"results": norm_results, "signals": norm_signals}


def read_labs(job_id: str, page_pngs, *, api_key: str = "", auth_token: str = "") -> dict:
    """Hand the rendered page images + the lab_master prompt to Claude Opus via the subscription
    `claude -p` CLI and return the parsed, validated lab dict ({"results": [...], "signals": {...}}).

    LIVE CALL — worker/terminal-only, never nested in a Claude session (INCIDENTS #2).
    Auth mirrors the imaging agent exactly: default desktop posture uses the host's Claude
    subscription login (tokens stripped); an explicit api_key/auth_token is honoured if passed.
    No ANTHROPIC_AUTH_TOKEN is required for the read to work (the prior SDK path needed one).
    """
    page_pngs = [Path(p) for p in page_pngs]
    if not page_pngs:
        raise ValueError("read_labs called with no page images")

    binp = _resolve_claude_bin()
    if not binp:
        raise RuntimeError(
            "Claude Code CLI not found. The MIKA app should bundle it; if running from source, "
            "install Claude Code and run `claude /login` once to sign in on your subscription."
        )

    # Grant the headless agent read access to the directory the page PNGs live in.
    pages_dir = page_pngs[0].parent
    prompt = _build_cli_prompt(page_pngs)
    cmd = [
        binp, "-p",
        "--output-format", "json",
        "--model", LAB_MODEL,
        "--effort", LAB_EFFORT,
        # File tools needed (Read the page PNGs) → bypassPermissions + scoped --add-dir, exactly like
        # the imaging agent. This is intentionally NOT the no-tools chat path (chat reads no files).
        "--permission-mode", "bypassPermissions",
        "--add-dir", str(pages_dir),
    ]

    logger.info(
        f"[lab {job_id}] reading {len(page_pngs)} page image(s) via claude -p "
        f"(model={LAB_MODEL}, auth={'api_key' if api_key else ('auth_token' if auth_token else 'subscription')})"
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(pages_dir),
            input=prompt,            # prompt on stdin (Windows claude.CMD argv is capped at 8191 chars)
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_lab_auth_env(api_key, auth_token),
            timeout=LAB_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Lab read timed out after {LAB_TIMEOUT_S}s") from e
    except FileNotFoundError as e:
        raise RuntimeError(f"Claude Code CLI not found ({binp})") from e

    # Parse the --output-format json envelope (final result + error flag).
    raw_out = (proc.stdout or "").strip()
    try:
        envelope = json.loads(raw_out) if raw_out else {}
    except json.JSONDecodeError:
        envelope = {}
    if proc.returncode != 0 or envelope.get("is_error"):
        detail = (envelope.get("result") or proc.stderr or raw_out or "claude -p exited non-zero").strip()
        raise RuntimeError(f"Lab read failed: {detail[:500]}")

    raw_text = envelope.get("result", "") if envelope else raw_out
    parsed = _parse_lab_json(raw_text)
    logger.info(
        f"[lab {job_id}] parsed {len(parsed['results'])} analyte rows "
        f"(render_quality={parsed['signals']['render_quality']})"
    )
    return parsed


# ──────────────────────────────────────────────────────────────────────────────
# 3. THE DETERMINISTIC SAFETY GATE — compose_verdict (pure Python, NO Claude)
# ──────────────────────────────────────────────────────────────────────────────
#
# CRITICAL: there is NO absolute "Everything looks normal" string anywhere. A clear high/low flag
# must NEVER resolve to a reassuring key. Verdict prose is a fixed per-language TEMPLATE keyed on
# `verdict_key` — never an LLM string, never LLM-translated. The same key yields the same semantics
# in EN and AR.

_TIER_ORDER = {"Possible": 0, "Likely": 1, "Confirmed": 2}

# English templates. {n} is filled for FEW. Semantics are locked per the plan; `/sincere` may polish
# wording later but must keep the meaning (especially the scoped, non-absolute ALL_CLEAN line).
_TEMPLATES_EN = {
    "NEUTRAL": "MIKA read your report — please review the values with your doctor.",
    "ALL_CLEAN": (
        "Nothing stood out in what MIKA could read. This describes only the values on this "
        "report, not a clean bill of health."
    ),
    "NONE_FLAGGED_PARTIAL": (
        "MIKA didn't flag anything, but couldn't read all of your report clearly — please share "
        "the full report with your doctor."
    ),
    "POSSIBLE_ONLY": (
        "A few results stand out, but with lower certainty — worth discussing with your doctor."
    ),
    "FEW": "{n} {thing} worth a look.",
    "SEVERAL": "Several things stand out — here are the ones worth a look.",
}

# Arabic templates — fixed strings, mirroring arabic.py glossary style (REPORT_DISCLAIMER_AR). Same
# keys, same semantics; a clinical translator polishes wording later. NEVER an LLM translation.
_TEMPLATES_AR = {
    "NEUTRAL": "قرأت MIKA تقريرك — يُرجى مراجعة القيم مع طبيبك.",
    "ALL_CLEAN": (
        "لم يلفت أي شيء الانتباه في ما تمكنت MIKA من قراءته. هذا يصف فقط القيم الموجودة في هذا "
        "التقرير، وليس شهادة صحة كاملة."
    ),
    "NONE_FLAGGED_PARTIAL": (
        "لم تُشِر MIKA إلى أي شيء، لكنها لم تتمكن من قراءة تقريرك بالكامل بوضوح — يُرجى مشاركة "
        "التقرير الكامل مع طبيبك."
    ),
    "POSSIBLE_ONLY": (
        "تبرز بعض النتائج، لكن بدرجة يقين أقل — من المفيد مناقشتها مع طبيبك."
    ),
    "FEW": "{n} {thing} تستحق النظر.",
    "SEVERAL": "تبرز عدة نتائج — وهذه هي التي تستحق النظر.",
}

# Pluralisation token for the FEW template.
_THING_EN = {"one": "thing", "many": "things"}
_THING_AR = {"one": "نتيجة", "many": "نتائج"}


def _max_tier(flagged: list) -> Optional[str]:
    if not flagged:
        return None
    best = max(_TIER_ORDER.get(r.get("confidence"), 0) for r in flagged)
    for name, order in _TIER_ORDER.items():
        if order == best:
            return name
    return None


def _confidence_from_signals(extraction_confidence: float) -> str:
    if extraction_confidence >= 0.85:
        return "high"
    if extraction_confidence >= 0.6:
        return "moderate"
    return "low"


def compose_verdict(results: list, signals: dict, lang: str = "en") -> dict:
    """THE SAFETY GATE. Pure Python; no Claude. Map structured (results, signals) to a verdict KEY,
    then look up a fixed per-language template string. Returns:
        { verdict_key, takeaway, confidence, checked_count, normal_count, flagged_count }
    """
    results = results or []
    signals = signals or {}

    # A flag only counts if it's a real abnormal status, confidently read, and legible enough.
    flagged = [
        r for r in results
        if r.get("status") not in ("normal", "unknown")
        and r.get("confidence") in ("Confirmed", "Likely")
        and r.get("clarity", 0) >= 0.7
    ]
    n = len(flagged)

    ec = signals.get("extraction_confidence", 0)
    rq = signals.get("render_quality")
    analytes_parsed = signals.get("analytes_parsed", 0)
    parsed_ratio = analytes_parsed / max(1, len(results))

    all_clean = (
        n == 0
        and all(r.get("status") == "normal" and r.get("clarity", 0) >= 0.7 for r in results)
        and ec >= 0.85
        and parsed_ratio >= 0.95
    )

    if ec < 0.85 or parsed_ratio < 0.7 or rq == "unreadable":
        key = "NEUTRAL"
    elif all_clean:
        key = "ALL_CLEAN"
    elif n == 0:
        key = "NONE_FLAGGED_PARTIAL"
    # NOTE: per the approved plan, `flagged` already excludes Possible-tier rows (confidence gate
    # above), so `_max_tier(flagged)` is Likely/Confirmed and this branch is currently unreachable.
    # A lone Possible-abnormal therefore degrades to NONE_FLAGGED_PARTIAL (n==0), which is the SAFE
    # direction (no reassurance). Left exactly as the plan specifies — flagging only, not refactoring.
    elif _max_tier(flagged) == "Possible":
        key = "POSSIBLE_ONLY"
    elif n <= 2:
        key = "FEW"
    else:
        key = "SEVERAL"

    templates = _TEMPLATES_AR if lang == "ar" else _TEMPLATES_EN
    thing_map = _THING_AR if lang == "ar" else _THING_EN
    takeaway = templates[key]
    if key == "FEW":
        thing = thing_map["one"] if n == 1 else thing_map["many"]
        takeaway = takeaway.format(n=n, thing=thing)

    checked_count = len(results)
    normal_count = sum(1 for r in results if r.get("status") == "normal")

    return {
        "verdict_key": key,
        "takeaway": takeaway,
        "confidence": _confidence_from_signals(ec),
        "checked_count": checked_count,
        "normal_count": normal_count,
        "flagged_count": n,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4. Dedicated lab persistence (NOT _persist_report — that path is DICOM-coupled)
# ──────────────────────────────────────────────────────────────────────────────

def build_lab_payload(verdict: dict, results: list, signals: dict) -> dict:
    """The final lab report.json payload shape (see docs/PLAN_lab_report.md Data model)."""
    return {
        "kind": "lab",
        "overall": verdict,
        "results": results or [],
        "signals": signals or {},
    }


def persist_lab_report(job_id: str, data_dir, payload: dict, page_pngs, *,
                       created_at: str = "", title: str = "") -> None:
    """Write report.json + a lab meta.json into the job's data dir, using only low-level disk writes
    (json + pathlib). Does NOT call _persist_report (which backfills DICOM-coupled fields: seqthumb
    thumb, detected_anatomy, evidence_manifest, artifact_registry, patient pdf). A lab meta carries
    kind:'lab' + a page-image map, and NO thumb/anatomy/pdf.

    `data_dir` is app.py's DATA_DIR; the page map is stored forward-slash relative to the job dir
    (mirrors app._rel_to_job portability) so it resolves cross-OS.
    """
    from datetime import datetime

    data_dir = Path(data_dir)
    job_dir = data_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "report.json").write_text(
        json.dumps(payload, default=str), encoding="utf-8"
    )

    # Page-image map: {"page_0": "<rel/posix/path>", ...}, relative to the job dir for portability.
    pages_map: dict[str, str] = {}
    for i, p in enumerate(page_pngs or []):
        p = Path(p)
        try:
            rel = p.resolve().relative_to(job_dir.resolve()).as_posix()
        except Exception:
            rel = p.name
        pages_map[f"page_{i}"] = rel

    overall = payload.get("overall") or {}
    meta = {
        "job_id": job_id,
        "kind": "lab",
        "status": "complete",
        "mode": "lab",
        "created_at": created_at or datetime.utcnow().isoformat(),
        "completed_at": datetime.utcnow().isoformat(),
        "title": title or "Lab report",
        "pages": pages_map,
        "page_count": len(pages_map),
        # No thumb / detected_anatomy / pdf for lab — Recent-list build reads from meta alone.
        "thumb": None,
        "verdict_key": overall.get("verdict_key"),
        "flagged_count": overall.get("flagged_count"),
        "checked_count": overall.get("checked_count"),
        "progress_phase": "complete",
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, default=str), encoding="utf-8")
    logger.info(f"[lab {job_id}] persisted lab report ({len(pages_map)} page image(s))")

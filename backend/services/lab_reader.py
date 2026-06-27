"""
MIKA — Lab / Bloodwork reader service
=====================================
The focused, DICOM-free read path for a lab/bloodwork report (PDF or photo). It is TEXT-FIRST: a
digital PDF is read from its exact embedded text layer (the LIS-generated ground truth), handed to
Claude Opus as TEXT via the no-tools `claude -p` transport (same scope as case_chat). Only a photo
or a scanned PDF with no text layer falls back to rendering page PNGs and a VISION read. Either path
returns the same strict structured per-analyte JSON, from which "The Verdict" is composed
DETERMINISTICALLY in Python (the safety gate — never an LLM string).

WHY text-first: rasterising a PDF that already has a perfect text layer and asking a vision model to
re-read it is lossy — it misread printed reference ranges and self-scored low clarity, which the flag
gate then deleted into a false "no findings". Read the text; only fall back to vision when there is
no text to read.

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
import re
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

# Clarity flag-floor — the SINGLE source of truth, shared by compose_verdict + compose_assessment.
# MUST stay == the literal in frontend/index.html LabReadScreen `flagged` filter; the pure-Python test
# test_lab_assessment.test_clarity_floor_in_sync scrapes index.html and asserts equality. 0.5 (not 0.7):
# Opus self-scores clarity conservatively even on clean digital PDFs, so a higher floor silently dropped
# confidently-read abnormals (the false "no findings" — see mika-lab-clarity-gate-false-negative).
CLARITY_FLAG_FLOOR = 0.5


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
                # 300 dpi (not 150): dense CBC/chemistry tables in small fonts were rendering
                # degraded at 150, so Opus self-scored low clarity and misread printed reference
                # ranges (supplying remembered ranges instead). Token cost is unchanged — the model
                # resizes every image to its own pixel cap regardless of source dpi, so a sharper
                # source only yields a cleaner downscale.
                pix = page.get_pixmap(dpi=300)
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


def extract_text(upload_path) -> list[str]:
    """Pull the embedded text layer out of a digital PDF, one string per page (capped at MAX_PAGES).

    This is the PRIMARY read input: a lab PDF generated by a hospital LIS carries an exact, perfectly
    legible text layer — rasterising it to an image and asking a vision model to re-read it is lossy
    and was the cause of a real false-negative (misread reference ranges + low self-scored clarity).
    Returns [] for a photo/image upload or a scanned PDF with no text layer — the caller then falls
    back to the vision read of the rendered page images.
    """
    upload_path = Path(upload_path)
    if upload_path.suffix.lower() != ".pdf":
        return []  # images (photos) have no text layer — vision read handles those
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []
    pages: list[str] = []
    try:
        doc = fitz.open(str(upload_path))
        try:
            for i in range(min(doc.page_count, MAX_PAGES)):
                pages.append(doc.load_page(i).get_text("text") or "")
        finally:
            doc.close()
    except Exception:
        return []
    return pages


def _has_text_layer(pages_text: list[str]) -> bool:
    """True when the extracted text is a real, value-bearing report (not a scanned PDF whose text
    layer is empty or just a sparse header). Lab reports are digit-dense, so require both a minimum
    body length and a minimum digit count before trusting the text path."""
    if not pages_text:
        return False
    total = sum(len(t.strip()) for t in pages_text)
    digits = sum(c.isdigit() for t in pages_text for c in t)
    return total >= 200 and digits >= 12


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
        # analyte_key: trust the model's slug if it's a known canonical, else DERIVE it server-side from
        # analyte_raw/plain_name (must-have #4 — condition matching must not depend on the model field).
        akey = r.get("analyte_key")
        if akey not in _ANALYTE_ALIASES:
            akey = normalize_analyte_key(r.get("analyte_raw"), r.get("plain_name"))
        norm_results.append({
            "plain_name": r.get("plain_name") or r.get("analyte_raw") or "",
            "analyte_raw": r.get("analyte_raw") or "",
            "analyte_key": akey,
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

    # Demographics (display-only; read from the report header, never inferred, never used to classify).
    pat = parsed.get("patient")
    pat = pat if isinstance(pat, dict) else {}
    norm_patient = {
        "name": (pat.get("name") or None),
        "age": (pat.get("age") or None),
        "sex": (pat.get("sex") or None),
    }
    # The model's ADVISORY assessment proposal (compose_assessment treats it as a tie-break only).
    proposal = parsed.get("assessment")
    proposal = proposal if isinstance(proposal, dict) else None

    return {
        "results": norm_results,
        "signals": norm_signals,
        "patient": norm_patient,
        "assessment_proposal": proposal,
    }


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


def _build_text_prompt(pages_text: list[str]) -> str:
    """Fold the lab master prompt + the report's EXTRACTED TEXT into a single no-tools prompt. The
    model reads the text directly (no Read tool, no images) and returns the SAME strict JSON schema."""
    page_blocks = "\n\n".join(
        f"----- REPORT PAGE (page_index {i}) -----\n{(t or '').strip()}"
        for i, t in enumerate(pages_text)
    )
    return f"""{LAB_MASTER_PROMPT}

## YOUR TASK NOW (TEXT)
Below is the EXACT text extracted from this lab/blood report's own digital text layer, one block per
page (0-indexed). It is the report's own text, so transcription is reliable: read each value, unit,
and printed reference range directly from it, and set each row's `clarity` high (0.9-1.0) unless a
line is genuinely truncated or garbled. Use the stated page_index for each row's `page_index`, and
put the exact line you read into `source_text`. Classify `status` ONLY against the range printed on
that line. Keep every `plain_meaning` calm, plain, and short — one sentence a worried non-expert
understands — never alarming, never a disease name, cause, or treatment.

REPORT TEXT:
{page_blocks}

Return STRICT JSON ONLY (one object matching the schema above) and NOTHING ELSE — no preamble, no
markdown, no commentary. Do not use any tools; everything you need is in the text above."""


def read_labs_text(job_id: str, pages_text: list[str], *, api_key: str = "", auth_token: str = "") -> dict:
    """Text-first lab read: hand the report's EXTRACTED TEXT to Opus via the no-tools `claude -p`
    transport (same scope as case_chat.ask_claude — NO --add-dir, NO --permission-mode) and return
    the parsed, validated dict. Preferred over the vision read for digital PDFs: exact transcription,
    no rasterisation, no clarity penalty.

    LIVE CALL — worker/terminal-only, never nested in a Claude session (INCIDENTS #2).
    """
    if not pages_text:
        raise ValueError("read_labs_text called with no page text")

    binp = _resolve_claude_bin()
    if not binp:
        raise RuntimeError(
            "Claude Code CLI not found. The MIKA app should bundle it; if running from source, "
            "install Claude Code and run `claude /login` once to sign in on your subscription."
        )

    prompt = _build_text_prompt(pages_text)
    # SCOPE INVARIANT (mirrors case_chat): NO --add-dir AND NO --permission-mode → zero filesystem
    # reach. The report text is in the prompt; the model needs no tools.
    cmd = [binp, "-p", "--output-format", "json", "--model", LAB_MODEL, "--effort", LAB_EFFORT]
    logger.info(
        f"[lab {job_id}] TEXT read via claude -p ({len(pages_text)} page text block(s), "
        f"model={LAB_MODEL}, auth={'api_key' if api_key else ('auth_token' if auth_token else 'subscription')})"
    )
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
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
        f"[lab {job_id}] text read parsed {len(parsed['results'])} analyte rows "
        f"(render_quality={parsed['signals']['render_quality']})"
    )
    return parsed


def read_lab_report(job_id: str, upload, page_pngs, *, api_key: str = "", auth_token: str = "") -> tuple[dict, str]:
    """Dispatch the read. TEXT-FIRST: a digital PDF with a real text layer is read from its exact
    text (no vision). Photos and scanned PDFs (no usable text layer) fall back to the vision read of
    the rendered page images. Returns (parsed, mode) where mode is "text" or "vision".

    `page_pngs` are still rendered by the caller regardless of mode — they back the "See it on your
    report" proof view and the vision fallback.
    """
    pages_text = extract_text(upload)
    if _has_text_layer(pages_text):
        return read_labs_text(job_id, pages_text, api_key=api_key, auth_token=auth_token), "text"
    logger.info(f"[lab {job_id}] no usable text layer — falling back to vision read")
    return read_labs(job_id, page_pngs, api_key=api_key, auth_token=auth_token), "vision"


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
    # The confidence tier (Confirmed/Likely) already carries legibility ("Likely" = legible but
    # slightly imperfect), so the clarity floor only exists to drop near-illegible rows. It is set
    # at 0.5 (not 0.7): Opus self-scores clarity conservatively — on a clean, machine-printed digital
    # PDF it routinely returns ~0.55-0.65 — so a 0.7 floor silently deleted confidently-read abnormal
    # values (e.g. a low Hemoglobin) and yielded a false "no findings". This floor MUST stay in sync
    # with the frontend LabReadScreen `flagged` filter (index.html).
    flagged = [
        r for r in results
        if r.get("status") not in ("normal", "unknown")
        and r.get("confidence") in ("Confirmed", "Likely")
        and r.get("clarity", 0) >= CLARITY_FLAG_FLOOR
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

    if rq == "unreadable":
        # The page is genuinely illegible — trust no transcription, surface nothing (a "Confirmed"
        # flag off an unreadable page is not credible). This is the one signal that overrides flags.
        key = "NEUTRAL"
    elif n > 0:
        # Confident abnormal(s) exist — already gated to Confirmed/Likely + the clarity floor. They
        # drive the headline REGARDLESS of overall extraction confidence: a moderate-confidence read
        # must never bury real flags under the vague "please review the values" (NEUTRAL) line — that
        # is incoherent next to N flagged cards, and hides exactly what the patient needs to see. The
        # separate confidence pill + the always-on "discuss with your doctor" disclaimer carry the
        # caveat. (`flagged` excludes Possible-tier, so POSSIBLE_ONLY is unreachable here by design.)
        key = "FEW" if n <= 2 else "SEVERAL"
    elif ec < 0.85 or parsed_ratio < 0.7:
        # Nothing confidently flagged AND the read was shaky/incomplete — don't reassure.
        key = "NEUTRAL"
    elif all_clean:
        key = "ALL_CLEAN"
    else:
        key = "NONE_FLAGGED_PARTIAL"

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
# 3b. THE ASSESSMENT LAYER — compose_assessment (pure Python, NO Claude)
# ──────────────────────────────────────────────────────────────────────────────
#
# Names the likely condition for the patient (owner decision, 2026-06-27) WITHOUT becoming an unbounded
# diagnosis engine. The DETERMINISTIC TRIGGER is the flagged-marker pattern, not the model: a condition
# surfaces iff the flagged results satisfy a curated whitelist entry's pattern. The model's `assessment`
# proposal is ADVISORY ONLY (tie-break / confirmation); it can never introduce a condition the markers
# don't support, and its free prose never reaches the patient (name + explanation are the bilingual
# whitelist canonicals). PRESENCE is a pure function of `flagged` → reproducible across reruns
# (INCIDENTS #5) and bounded to a defensible value-defined set (no cancer/leukemia/etc. — _REDFLAG_TERMS).

# Normalization slug table: canonical analyte_key -> alias fragments (lowercased, alnum+space).
# Shared by the whitelist matcher AND the _parse_lab_json analyte_key fallback, so condition matching
# never depends on the model populating `analyte_key` (otherwise the feature could go silently dark, and
# the live read can't be self-verified in-session — INCIDENTS #1/#2).
_ANALYTE_ALIASES = {
    "hemoglobin": ["hemoglobin", "haemoglobin", "hgb", "hb"],
    "hematocrit": ["hematocrit", "haematocrit", "hct", "pcv"],
    "mcv": ["mean corpuscular volume", "mcv"],
    # MCH/MCHC names CONTAIN "hemoglobin", so they MUST carry aliases longer than the bare "hemoglobin"
    # key (longest-alias-wins) — otherwise "Hemoglobin per cell (MCH)" mis-maps to hemoglobin and fakes a
    # low Hgb (→ false anemia). Keep the multi-word forms ahead of the short "mch"/"mchc".
    "mch": ["mean corpuscular hemoglobin concentration", "red cell hemoglobin concentration",
            "mean corpuscular hemoglobin", "hemoglobin per cell", "red cell hemoglobin", "mchc", "mch"],
    "rdw": ["red cell distribution width", "rdw"],
    "ferritin": ["ferritin"],
    "iron": ["serum iron", "iron"],
    "tibc": ["total iron binding capacity", "tibc"],
    "transferrin_sat": ["transferrin saturation", "tsat"],
    "vitamin_b12": ["vitamin b12", "cobalamin", "b12"],
    "folate": ["folate", "folic acid"],
    "vitamin_d": ["25 hydroxyvitamin d", "25 oh vitamin d", "25 ohd", "vitamin d"],
    "ldl": ["ldl cholesterol", "low density lipoprotein", "ldl c", "ldl"],
    "hdl": ["hdl cholesterol", "high density lipoprotein", "hdl c", "hdl"],
    "total_cholesterol": ["total cholesterol", "cholesterol total", "cholesterol"],
    "triglycerides": ["triglycerides", "triglyceride", "tg"],
    "glucose": ["fasting blood sugar", "fasting glucose", "blood glucose", "glucose", "fbs"],
    "hba1c": ["glycated hemoglobin", "hemoglobin a1c", "hba1c", "a1c"],
    "tsh": ["thyroid stimulating hormone", "tsh"],
    "ft4": ["free t4", "free thyroxine", "thyroxine", "ft4", "t4"],
    "ft3": ["free t3", "ft3", "t3"],
    "creatinine": ["creatinine"],
    "egfr": ["estimated gfr", "egfr", "gfr"],
    "urea": ["blood urea nitrogen", "urea", "bun"],
    "alt": ["alanine aminotransferase", "alt", "sgpt"],
    "ast": ["aspartate aminotransferase", "ast", "sgot"],
    "alp": ["alkaline phosphatase", "alp"],
    "bilirubin": ["total bilirubin", "bilirubin"],
    "wbc": ["white blood cell", "white cell count", "leukocyte", "leucocyte", "wbc"],
    # NOTE: NO bare "platelet" alias — it greedily matched "Platelet size (MPV)" and mis-mapped a low
    # platelet SIZE to the platelet COUNT key (→ a false "low platelet count" verdict). MPV is its own key.
    "platelets": ["platelet count", "platelets", "plt"],
    "mpv": ["mean platelet volume", "platelet size", "mpv"],
    "potassium": ["potassium"],
    "sodium": ["sodium"],
    "calcium": ["calcium"],
}


def _norm_text(s) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())


def normalize_analyte_key(*names) -> str:
    """Map an analyte's printed/plain name(s) to a canonical slug, or '' if unknown. Whole-word match,
    longest-alias-wins (so 'iron' does not steal 'total iron binding capacity' — TIBC's longer alias
    scores higher)."""
    blob = " " + " ".join(_norm_text(n) for n in names if n) + " "
    best_key, best_len = "", 0
    for key, aliases in _ANALYTE_ALIASES.items():
        for a in aliases:
            if f" {a} " in blob and len(a) > best_len:
                best_key, best_len = key, len(a)
    return best_key


def _direction(r) -> str:
    """The direction a flagged result points; 'abnormal' can't be placed → not directional."""
    s = r.get("status")
    return s if s in ("low", "high") else ""


# A clause = list of (analyte_key, direction) options; satisfied if ANY option is in flagged.
# A condition's pattern = list of clauses; satisfied only if ALL clauses are satisfied.
# Higher priority wins among simultaneous matches (more-specific conditions rank above general ones).
_CONDITION_WHITELIST = [
    {
        "key": "iron_deficiency_anemia", "priority": 90,
        "aliases": ["iron deficiency anemia", "iron deficiency anaemia", "iron deficiency", "ida"],
        "pattern": [[("hemoglobin", "low")],
                    [("mcv", "low"), ("mch", "low"), ("ferritin", "low"), ("iron", "low"), ("transferrin_sat", "low")]],
        "name_en": "iron-deficiency anemia",
        "name_ar": "فقر الدم الناتج عن نقص الحديد",
        "expl_en": "This usually means the blood is low on iron, which can leave you feeling tired.",
        "expl_ar": "يعني هذا عادةً أن الدم منخفض في الحديد، وقد يسبّب الشعور بالتعب.",
    },
    {
        # Small + low-hemoglobin red cells WITHOUT a low hemoglobin (microcytosis/hypochromia on their own).
        # Ranks BELOW iron-deficiency anemia (90) so a true low-Hgb picture still wins; ABOVE the generic
        # single-marker conditions. Both MCV-low AND MCH-low required (a specific pairing, not MCV alone).
        "key": "microcytic_hypochromic", "priority": 80,
        "aliases": ["microcytic", "microcytosis", "microcytic hypochromic", "small red cells", "hypochromic"],
        "pattern": [[("mcv", "low")], [("mch", "low")]],
        "name_en": "small, low-hemoglobin red cells",
        "name_ar": "كريات دم حمراء صغيرة ومنخفضة الهيموغلوبين",
        "expl_en": "Your red cells read smaller and carry less hemoglobin than the printed range — a pattern that's usually followed up with iron studies.",
        "expl_ar": "تظهر كريات دمك الحمراء أصغر حجماً وتحمل هيموغلوبيناً أقل من النطاق المطبوع — وهو نمط تتم متابعته عادةً بفحوصات الحديد.",
    },
    {
        "key": "anemia", "priority": 50,
        "aliases": ["anemia", "anaemia"],
        "pattern": [[("hemoglobin", "low")]],
        "name_en": "anemia (a low oxygen-carrying level)",
        "name_ar": "فقر الدم (انخفاض مستوى نقل الأكسجين)",
        "expl_en": "Your blood is a little low on the part that carries oxygen.",
        "expl_ar": "دمك منخفض قليلاً في الجزء الذي يحمل الأكسجين.",
    },
    {
        "key": "low_vitamin_d", "priority": 60,
        "aliases": ["vitamin d deficiency", "low vitamin d", "hypovitaminosis d"],
        "pattern": [[("vitamin_d", "low")]],
        "name_en": "low vitamin D",
        "name_ar": "انخفاض فيتامين د",
        "expl_en": "Low vitamin D is common and can leave you feeling tired.",
        "expl_ar": "انخفاض فيتامين د شائع وقد يسبّب الشعور بالتعب.",
    },
    {
        "key": "low_vitamin_b12", "priority": 60,
        "aliases": ["vitamin b12 deficiency", "low b12", "low vitamin b12", "b12 deficiency"],
        "pattern": [[("vitamin_b12", "low")]],
        "name_en": "low vitamin B12",
        "name_ar": "انخفاض فيتامين ب12",
        "expl_en": "A low vitamin B12 level is common and worth checking.",
        "expl_ar": "انخفاض مستوى فيتامين ب12 شائع ويستحق المتابعة.",
    },
    {
        "key": "high_cholesterol", "priority": 60,
        "aliases": ["high cholesterol", "hypercholesterolemia", "dyslipidemia", "dyslipidaemia", "hyperlipidemia"],
        "pattern": [[("ldl", "high"), ("total_cholesterol", "high"), ("triglycerides", "high")]],
        "name_en": "high cholesterol",
        "name_ar": "ارتفاع الكوليسترول",
        "expl_en": "One or more cholesterol-related results read above the printed range.",
        "expl_ar": "واحدة أو أكثر من نتائج الكوليسترول لديك أعلى من النطاق المطبوع.",
    },
    {
        "key": "high_blood_sugar", "priority": 70,
        "aliases": ["high blood sugar", "hyperglycemia", "hyperglycaemia", "prediabetes", "diabetes"],
        "pattern": [[("glucose", "high"), ("hba1c", "high")]],
        "name_en": "high blood sugar",
        "name_ar": "ارتفاع سكر الدم",
        "expl_en": "Your blood-sugar reading is above the printed range.",
        "expl_ar": "قراءة سكر الدم لديك أعلى من النطاق المطبوع.",
    },
    {
        "key": "underactive_thyroid", "priority": 70,
        "aliases": ["underactive thyroid", "hypothyroid", "hypothyroidism"],
        "pattern": [[("tsh", "high")]],
        "name_en": "an underactive thyroid pattern",
        "name_ar": "نمط خمول الغدة الدرقية",
        "expl_en": "Your thyroid result reads outside the printed range in a low-activity pattern.",
        "expl_ar": "نتيجة الغدة الدرقية لديك خارج النطاق المطبوع بنمط يشير إلى نشاط منخفض.",
    },
    {
        "key": "overactive_thyroid", "priority": 70,
        "aliases": ["overactive thyroid", "hyperthyroid", "hyperthyroidism"],
        "pattern": [[("tsh", "low")]],
        "name_en": "an overactive thyroid pattern",
        "name_ar": "نمط فرط نشاط الغدة الدرقية",
        "expl_en": "Your thyroid result reads outside the printed range in a high-activity pattern.",
        "expl_ar": "نتيجة الغدة الدرقية لديك خارج النطاق المطبوع بنمط يشير إلى نشاط مرتفع.",
    },
    {
        "key": "reduced_kidney_function", "priority": 65,
        "aliases": ["reduced kidney function", "impaired kidney function", "chronic kidney disease", "renal impairment"],
        "pattern": [[("egfr", "low"), ("creatinine", "high")]],
        "name_en": "reduced kidney function",
        "name_ar": "انخفاض وظائف الكلى",
        "expl_en": "A kidney-function result reads outside the printed range.",
        "expl_ar": "إحدى نتائج وظائف الكلى خارج النطاق المطبوع.",
    },
    {
        "key": "elevated_liver_enzymes", "priority": 60,
        "aliases": ["elevated liver enzymes", "raised liver enzymes", "abnormal liver function"],
        "pattern": [[("alt", "high"), ("ast", "high")]],
        "name_en": "elevated liver enzymes",
        "name_ar": "ارتفاع إنزيمات الكبد",
        "expl_en": "A liver-enzyme result reads above the printed range.",
        "expl_ar": "إحدى نتائج إنزيمات الكبد أعلى من النطاق المطبوع.",
    },
    {
        "key": "high_white_cells", "priority": 55,
        "aliases": ["high white cell count", "leukocytosis", "raised white cells"],
        "pattern": [[("wbc", "high")]],
        "name_en": "a high white-cell count",
        "name_ar": "ارتفاع عدد خلايا الدم البيضاء",
        "expl_en": "Your white-cell count reads above the printed range.",
        "expl_ar": "عدد خلايا الدم البيضاء لديك أعلى من النطاق المطبوع.",
    },
    {
        "key": "low_white_cells", "priority": 55,
        "aliases": ["low white cell count", "leukopenia", "low white cells"],
        "pattern": [[("wbc", "low")]],
        "name_en": "a low white-cell count",
        "name_ar": "انخفاض عدد خلايا الدم البيضاء",
        "expl_en": "Your white-cell count reads below the printed range.",
        "expl_ar": "عدد خلايا الدم البيضاء لديك أقل من النطاق المطبوع.",
    },
    {
        "key": "low_platelets", "priority": 55,
        "aliases": ["low platelet count", "thrombocytopenia", "low platelets"],
        "pattern": [[("platelets", "low")]],
        "name_en": "a low platelet count",
        "name_ar": "انخفاض عدد الصفائح الدموية",
        "expl_en": "Your platelet count reads below the printed range.",
        "expl_ar": "عدد الصفائح الدموية لديك أقل من النطاق المطبوع.",
    },
]

# Red-flag terms — a surfaced assessment (and the chat) may NEVER name any of these, even if the model
# proposes one. They degrade to the honest grouped verdict. Shared with the lab-chat answer-replacement.
# Bare-substring matched (over-blocking fails SAFE — e.g. "cancerous" still trips "cancer").
_REDFLAG_TERMS = [
    "cancer", "malignan", "leukemia", "leukaemia", "lymphoma", "myeloma", "tumor", "tumour",
    "carcinoma", "metasta", "sepsis",
]

# Treatment/drug terms — never in a surfaced assessment (table-guarded) and replaced in chat output.
_TREATMENT_TERMS = [
    "milligram", "microgram", "dosage", "tablet", "capsule", "supplement", "prescrib",
    "medication", "medicine", "therapy", "treatment", "inject",
    "ferrous sulfate", "ferrous sulphate", "metformin", "insulin", "levothyroxine", "statin",
    "atorvastatin", "rosuvastatin", "simvastatin",
]

# Arabic blocklists + condition aliases — the lab chat answers in the patient's language, but _norm_text
# strips non-Latin script, so the Latin lists above are blind to an Arabic answer. These are matched
# against the RAW (Arabic-preserving) answer so an Arabic red-flag/treatment/off-whitelist mention is
# still gated (the chat is the second, non-deterministic consumer — INCIDENTS #4). Do NOT add "حديد"
# (iron) to treatment — the chat legitimately discusses iron as a marker.
_REDFLAG_TERMS_AR = ["سرطان", "خبيث", "ورم", "لمفوما", "ليمفوما", "ابيضاض", "نخاع", "إنتان", "تعفّن الدم", "تعفن الدم"]
_TREATMENT_TERMS_AR = ["دواء", "أدوية", "علاج", "جرعة", "حبوب", "حبة", "قرص", "كبسولة", "مكمّل", "مكمل", "حقن", "وصفة", "ملغ", "ميلغرام", "ميتفورمين", "إنسولين", "ستاتين", "ليفوثيروكسين"]
# Short Arabic aliases per condition_key (what a model would actually write), for the chat off-whitelist
# positive-list. Keyed by whitelist key; the surfaced condition's own aliases are allowed.
_CONDITION_AR_ALIASES = {
    "iron_deficiency_anemia": ["نقص الحديد", "فقر الدم الناتج عن نقص الحديد"],
    "anemia": ["فقر الدم"],
    "low_vitamin_d": ["نقص فيتامين د", "انخفاض فيتامين د"],
    "low_vitamin_b12": ["نقص فيتامين ب12", "انخفاض فيتامين ب12"],
    "high_cholesterol": ["ارتفاع الكوليسترول", "فرط شحميات الدم"],
    "high_blood_sugar": ["ارتفاع سكر الدم", "السكري", "ما قبل السكري"],
    "underactive_thyroid": ["خمول الغدة الدرقية", "قصور الغدة الدرقية"],
    "overactive_thyroid": ["فرط نشاط الغدة الدرقية", "فرط الغدة الدرقية"],
    "reduced_kidney_function": ["انخفاض وظائف الكلى", "قصور الكلى"],
    "elevated_liver_enzymes": ["ارتفاع إنزيمات الكبد"],
    "high_white_cells": ["ارتفاع كريات الدم البيضاء", "ارتفاع خلايا الدم البيضاء"],
    "low_white_cells": ["انخفاض كريات الدم البيضاء", "انخفاض خلايا الدم البيضاء"],
    "low_platelets": ["نقص الصفائح", "انخفاض الصفائح"],
    "microcytic_hypochromic": ["كريات حمراء صغيرة", "صغر حجم الكريات الحمراء"],
}

# CURATED, SAFE per-condition "what can help" notes — concise + reassuring general lifestyle info ONLY.
# NO drugs, NO doses, NO "this fixes it / no need to check further". For conditions where a generic
# diet/lifestyle tip could HARM or imply self-cure over a needed work-up (thyroid, kidney, white cells,
# platelets), the note is reassurance ONLY — no specific "eat/drink/do X". Curated, never LLM-generated.
_CONDITION_ADVICE = {
    "iron_deficiency_anemia": {
        "en": "Good news — this is common and very treatable. Iron-rich foods like red meat, lentils, beans, and dark leafy greens help your body rebuild its iron.",
        "ar": "خبر جيد — هذا شائع وقابل للعلاج تماماً. الأطعمة الغنية بالحديد مثل اللحم الأحمر والعدس والفاصولياء والخضروات الورقية الداكنة تساعد جسمك على تعويض الحديد."},
    "anemia": {
        "en": "This is common and usually very manageable. Iron- and vitamin-rich foods help support healthy blood.",
        "ar": "هذا شائع وقابل للتحسّن عادةً. الأطعمة الغنية بالحديد والفيتامينات تساعد على دعم صحة الدم."},
    "low_vitamin_d": {
        "en": "Very common and easy to top up — a little daily sunlight and vitamin-D-rich foods like oily fish, eggs, and fortified milk help.",
        "ar": "شائع جداً ويسهل تعويضه — القليل من ضوء الشمس يومياً والأطعمة الغنية بفيتامين د مثل الأسماك الدهنية والبيض والحليب المدعّم تساعد."},
    "low_vitamin_b12": {
        "en": "Common and easy to address — B12-rich foods like meat, fish, eggs, and dairy help.",
        "ar": "شائع ويسهل معالجته — الأطعمة الغنية بفيتامين ب12 مثل اللحوم والأسماك والبيض ومنتجات الألبان تساعد."},
    "high_cholesterol": {
        "en": "Often improves with everyday habits — more fiber from oats, beans, and vegetables, regular movement, and less fried and processed food.",
        "ar": "غالباً ما يتحسّن بعادات يومية — مزيد من الألياف من الشوفان والبقول والخضار، والحركة المنتظمة، وتقليل الأطعمة المقلية والمصنّعة."},
    "high_blood_sugar": {
        "en": "Often improves with everyday habits — more whole foods and fiber, fewer sugary drinks and refined carbs, and staying active.",
        "ar": "غالباً ما يتحسّن بعادات يومية — مزيد من الأطعمة الكاملة والألياف، وتقليل المشروبات السكرية والنشويات المكرّرة، والبقاء نشيطاً."},
    "underactive_thyroid": {
        "en": "This is common and very manageable once it's looked into.",
        "ar": "هذا شائع وقابل للتحكّم تماماً بعد المتابعة."},
    "overactive_thyroid": {
        "en": "This is common and manageable once it's looked into.",
        "ar": "هذا شائع وقابل للتحكّم بعد المتابعة."},
    "reduced_kidney_function": {
        "en": "This is often manageable, and the right next steps depend on your full picture.",
        "ar": "هذا قابل للتحكّم غالباً، وتعتمد الخطوات المناسبة على صورتك الكاملة."},
    "elevated_liver_enzymes": {
        "en": "Often improves by easing back on alcohol and very rich, fatty foods.",
        "ar": "غالباً ما يتحسّن بتقليل الكحول والأطعمة الدسمة جداً."},
    "high_white_cells": {
        "en": "This is best understood alongside your other results and how you're feeling.",
        "ar": "يُفهَم هذا بشكل أفضل مع بقية نتائجك وكيف تشعر."},
    "low_white_cells": {
        "en": "This is best understood alongside your other results and how you're feeling.",
        "ar": "يُفهَم هذا بشكل أفضل مع بقية نتائجك وكيف تشعر."},
    "low_platelets": {
        "en": "This is best understood alongside your other results and how you're feeling.",
        "ar": "يُفهَم هذا بشكل أفضل مع بقية نتائجك وكيف تشعر."},
    # Reassurance ONLY (no "eat iron") — this pattern can also be a harmless inherited trait where iron
    # isn't the cause, so point to the work-up (iron studies), never a self-treatment.
    "microcytic_hypochromic": {
        "en": "This is a common pattern and usually straightforward to look into — iron studies help tell apart the likely causes.",
        "ar": "هذا نمط شائع وعادةً ما يسهل متابعته — تساعد فحوصات الحديد على التمييز بين الأسباب المحتملة."},
}


def _contains_any(text: str, terms) -> bool:
    t = _norm_text(text)
    return any(term in t for term in terms)


def compose_assessment(results: list, signals: dict = None, proposal: Optional[dict] = None) -> Optional[dict]:
    """THE ASSESSMENT LAYER. Pure Python; no Claude. Derive the likely condition DETERMINISTICALLY from
    the flagged-marker pattern (the trigger); the model `proposal` is an advisory tie-break only.
    Returns a bilingual dict, or None → the page keeps the honest grouped verdict (never softens it).
    NEVER names a red-flag/treatment term (curated whitelist + guards)."""
    results = results or []
    flagged = [
        r for r in results
        if r.get("status") not in ("normal", "unknown")
        and r.get("confidence") in ("Confirmed", "Likely")
        and r.get("clarity", 0) >= CLARITY_FLAG_FLOOR
    ]
    if not flagged:
        return None

    # Index flagged results by (canonical analyte_key, direction).
    flagged_by_key: dict = {}
    for r in flagged:
        key = r.get("analyte_key") or normalize_analyte_key(r.get("analyte_raw"), r.get("plain_name"))
        d = _direction(r)
        if key and d:
            flagged_by_key.setdefault((key, d), r)

    def _clause_match(clause):
        for (key, d) in clause:
            r = flagged_by_key.get((key, d))
            if r is not None:
                return r
        return None

    # DERIVE candidates from the markers: an entry is a candidate iff EVERY clause is satisfied.
    candidates = []
    for entry in _CONDITION_WHITELIST:
        matched = []
        ok = True
        for clause in entry["pattern"]:
            r = _clause_match(clause)
            if r is None:
                ok = False
                break
            matched.append(r)
        if ok:
            candidates.append((entry, matched))
    if not candidates:
        return None

    # Advisory tie-break only: the model proposal can favour a candidate it named, never add one.
    proposed_keys = set()
    if isinstance(proposal, dict):
        pc = proposal.get("proposed_condition")
        if pc and not _contains_any(pc, _REDFLAG_TERMS):
            for entry, _m in candidates:
                if _contains_any(pc, entry["aliases"]):
                    proposed_keys.add(entry["key"])

    # Priority is PRIMARY and is a pure function of the markers; the model proposal is the LOWEST
    # tie-break only (it can never demote a more-specific, higher-priority derived condition). This keeps
    # the surfaced primary deterministic (INCIDENTS #5) — e.g. iron-deficiency anemia is never demoted to
    # plain "anemia" just because the model proposed the generic term.
    def _score(item):
        entry, matched = item
        return (entry["priority"], len(matched), 1 if entry["key"] in proposed_keys else 0)

    entry, _best_matched = max(candidates, key=_score)

    # Defense-in-depth: never surface a red-flag/treatment term even if the table were edited badly.
    for fld in ("name_en", "name_ar", "expl_en", "expl_ar"):
        if _contains_any(entry.get(fld, ""), _REDFLAG_TERMS) or _contains_any(entry.get(fld, ""), _TREATMENT_TERMS):
            return None

    # Supporting markers = ALL flagged results matching any (key, direction) option in the pattern
    # (richer than one-per-clause → a fuller "X and Y and Z"), in report order, deduped.
    pattern_opts = {(k, d) for clause in entry["pattern"] for (k, d) in clause}
    flagged_ids = {id(x) for x in flagged}
    supporting, source_indices, seen = [], [], set()
    for i, r in enumerate(results):
        if id(r) not in flagged_ids or id(r) in seen:
            continue
        key = r.get("analyte_key") or normalize_analyte_key(r.get("analyte_raw"), r.get("plain_name"))
        if (key, _direction(r)) in pattern_opts:
            seen.add(id(r))
            nm = r.get("plain_name") or r.get("analyte_raw") or ""
            if nm:
                supporting.append(nm)
            source_indices.append(i)

    advice = _CONDITION_ADVICE.get(entry["key"], {})
    return {
        "condition_key": entry["key"],
        "name_en": entry["name_en"],
        "name_ar": entry["name_ar"],
        "explanation_en": entry["expl_en"],
        "explanation_ar": entry["expl_ar"],
        "advice_en": advice.get("en", ""),
        "advice_ar": advice.get("ar", ""),
        "supporting": supporting,
        "lead_key": "consistent_with",
        "source_indices": source_indices,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4. Dedicated lab persistence (NOT _persist_report — that path is DICOM-coupled)
# ──────────────────────────────────────────────────────────────────────────────

def build_lab_payload(verdict: dict, results: list, signals: dict,
                      assessment: Optional[dict] = None, patient: Optional[dict] = None) -> dict:
    """The final lab report.json payload shape. `assessment` (the gated named-condition dict from
    compose_assessment, or None) attaches ADDITIVELY under `overall` — compose_verdict stays the sole
    writer of takeaway/counts. `patient` (display-only demographics) lives at the top level and is
    persisted ONLY in report.json, never in meta.json (PII out of the Recent-list index)."""
    overall = dict(verdict or {})
    overall["assessment"] = assessment   # None when no condition is confidently derivable
    return {
        "kind": "lab",
        "overall": overall,
        "results": results or [],
        "signals": signals or {},
        "patient": patient or {},
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

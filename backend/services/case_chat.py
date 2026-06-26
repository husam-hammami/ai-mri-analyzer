"""
Case chat (services/case_chat.py) — a patient asks plain questions about THEIR ONE completed study.

Self-contained, flag-dark (MIKA_CHAT_ENABLED). The chat answers ONLY from the patient's own report, in
concise plain language (what it means for them → life impact → a plain next step), and NEVER as medical
advice/diagnosis/dosing. Claude answers in whatever language the patient asks — no translation layer here.

Safety = system prompt (one line of defence) + DETERMINISTIC last-writer backstops that run AFTER generation
and can't be talked around (per docs/INCIDENTS.md: "a gate that looks applied but never fires" + "LLM-stated
mm fabricated without calibration"). The live `claude -p` call cannot be self-verified inside a Claude session
(nested-session hang) — it's a human gate; the pure functions here are unit-tested with `ask_claude` mocked.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mika.chat")

try:  # server runs from backend/ (cwd=backend); fall back to package path
    from services.agent_runner import _resolve_claude_bin
except ImportError:  # pragma: no cover - import path when launched as a package
    from backend.services.agent_runner import _resolve_claude_bin


def _chat_env() -> dict:
    """Mirror the agent's host-fallback auth: default desktop posture = the host subscription login.
    Strips ANTHROPIC_API_KEY/AUTH_TOKEN unless MIKA_AGENT_USE_API_KEY is set (then both read+chat share it)."""
    import os
    env = dict(os.environ)
    if not os.environ.get("MIKA_AGENT_USE_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def ask_claude(prompt: str, *, model: str, effort: str, timeout_s: int) -> tuple[str, bool]:
    """One-shot, NO tools/files. Returns (text, is_error). Never raises."""
    binp = _resolve_claude_bin()
    if not binp:
        return ("", True)
    # SCOPE INVARIANT (load-bearing security): NO --add-dir AND NO --permission-mode. Headless `claude -p`
    # without bypassPermissions cannot auto-approve file tools → zero filesystem reach even from the server cwd.
    # NEVER copy `--permission-mode bypassPermissions` from agent_runner for "parity" — both omissions are required.
    cmd = [binp, "-p", "--output-format", "json", "--model", model, "--effort", effort]
    assert "--add-dir" not in cmd and "--permission-mode" not in cmd, "chat must have no tool/file scope"
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=_chat_env(), timeout=timeout_s)
        env = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
        return (env.get("result", "") or "", bool(env.get("is_error")) or proc.returncode != 0)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return ("", True)


def _calibration_label(study: dict) -> str:
    """Robust calibration string. The real payload carries `calibration_status` as a DICT
    ({'calibrated': bool, 'basis': ...}); older/lite paths use a plain string. Normalize both."""
    cs = (study or {}).get("calibration_status", "") if isinstance(study, dict) else ""
    if isinstance(cs, dict):
        c = cs.get("calibrated")
        return "uncalibrated" if c is False else ("calibrated" if c else "unknown")
    return str(cs or "unknown")


# ── Context assembly — pure, unit-testable; reads only the patient-facing (plain, de-identified) report ──
def build_context(report: dict) -> str:
    """Grounding text from the NORMALIZED report payload. Belt-and-suspenders fallback through
    `agent.summary.patient` so a normal-study / lite report still grounds (top-level patient can be sparse)."""
    if not isinstance(report, dict):
        report = {}
    s = report.get("study", {}) if isinstance(report.get("study"), dict) else {}
    p = report.get("patient") or {}
    agp = (((report.get("agent") or {}).get("summary") or {}).get("patient")) or {}
    findings = p.get("findings") or agp.get("findings") or []
    bottom = p.get("bottom_line") or agp.get("bottom_line") or ""
    kpts = p.get("key_points") or agp.get("key_points") or []
    lines = [
        f"STUDY: {s.get('body_part', '?')} · {s.get('modality', '?')} · calibration: {_calibration_label(s)}",
        f"BOTTOM LINE: {bottom}",
    ]
    for i, f in enumerate(findings, 1):
        if not isinstance(f, dict):
            continue
        lines.append(f"FINDING {i} [{f.get('certainty', '')}]: {f.get('plain', '')}"
                     + (f" — {f.get('caption')}" if f.get("caption") else ""))
    if kpts:
        lines.append("KEY POINTS: " + " | ".join(str(k) for k in kpts))
    # the report's OWN patient-facing meaning + signposting, so "what it means / next steps" stays grounded
    # in the report rather than invented from general knowledge:
    wim = p.get("what_it_means") or agp.get("what_it_means") or []
    if wim:
        lines.append("WHAT IT MEANS (plain, from the report): " + " | ".join(str(w) for w in wim))
    wf = p.get("worth_flagging") or agp.get("worth_flagging") or []
    if wf:
        lines.append("WATCH-FOR / WORTH FLAGGING (from the report): " + " | ".join(str(w) for w in wf))
    c = (p.get("confidence") or agp.get("confidence") or {})
    if isinstance(c, dict):
        lines.append(f"OVERALL CONFIDENCE: {c.get('label', '')} ({c.get('score', '')}%) {c.get('note', '')}")
    return "\n".join(lines)


SYSTEM_RULES = """You are MIKA, answering a patient's questions about THIS ONE imaging study, using ONLY the report below.

REPORT
{context}

RULES
1. Use ONLY the report above. Never add a finding, measurement, or fact that is not in it. If the answer is not in the report, say: "I can't tell that from this read."
2. This study only — but HELP FIRST, don't brush off. A question about THEIR reading (incl. "is it serious?", "is this cancer?", "should I worry?") deserves a plain answer of what the report DOES and does NOT say, then defer the diagnosis itself to their doctor — never a cold "ask your doctor." The short redirect is ONLY for genuinely off-study things (other conditions, general medical questions, dosages).
3. Write for a worried non-technical person. Short everyday words, no jargon; if a medical term must appear, give its plain meaning in a few words. Be clear and reassuring without overstating.
4. You MAY explain, in plain language, what a finding means for them and how it might affect everyday life (as a possibility, not a certainty), and you MAY surface the report's own next-step pointers and any "watch-for" symptoms. NEVER recommend a specific treatment, medication, dose, or procedure, and never diagnose — their doctor decides those.
5. Match the report's certainty word exactly (Confirmed / Likely / Possible); never sound more sure than it. If the study is uncalibrated, never state a millimetre value — use the report's qualitative wording.
6. Be concise: a few short sentences, or up to three short points. When it fits the question, shape the answer as — what it is (plain) → what it could mean for you → a plain next step. No preamble.
7. End any answer about what to do with "Discuss this with your doctor"; if the report lists red-flag symptoms, name them as reasons to seek care sooner."""


def build_prompt(context: str, history: list, question: str) -> str:
    parts = [SYSTEM_RULES.format(context=context), ""]
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        parts.append(("Q: " if role == "user" else "A: ") + text)
    parts.append("Q: " + question.strip())
    parts.append("A:")
    return "\n".join(parts)


# ── Deterministic backstops (the answer's LAST writer — not prompt-only) ──────────────────────────────
_MM_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mm|millimet(?:er|re)s?)\b", re.IGNORECASE)  # 4mm/4 mm/4.5 mm/4 millimetres


def answer_case_question(job_id, report, question, history, *, model, effort, timeout_s, data_dir):
    """Generate, apply the deterministic backstops, persist, return (text, is_error)."""
    ctx = build_context(report)
    text, err = ask_claude(build_prompt(ctx, history, question), model=model, effort=effort, timeout_s=timeout_s)
    if err or not text.strip():
        return ("", True)
    # BACKSTOP 1 — uncalibrated studies must NEVER state a millimetre value (INCIDENTS.md fabricated-mm class).
    # STRIP the offending mm token(s), don't discard the whole answer — keeps a useful plain reply while
    # guaranteeing no fabricated measurement survives (nuking the answer would destroy a correct, helpful reply
    # over an incidental "5 mm slice thickness" — a usefulness regression against the feature's purpose).
    study = (report.get("study") or {}) if isinstance(report, dict) else {}
    if "uncalibrat" in _calibration_label(study).lower():
        if _MM_RE.search(text):
            text = _MM_RE.sub("a size that can't be measured exactly on this uncalibrated study", text).strip()
            if "doctor" not in text.lower():
                text += " Discuss exact measurements with your doctor."
    _persist_turn(data_dir, job_id, question, text)
    return (text, False)


def _persist_turn(data_dir, job_id: str, question: str, answer: str, *, max_turns: int = 12) -> None:
    """Append the turn to {data_dir}/{job_id}/chat.json, capped. Best-effort (never fatal)."""
    try:
        path = Path(data_dir) / job_id / "chat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        log = []
        if path.exists():
            try:
                log = json.loads(path.read_text(encoding="utf-8-sig")) or []
            except Exception:
                log = []
        ts = int(time.time())
        log.append({"role": "user", "text": question, "ts": ts})
        log.append({"role": "assistant", "text": answer, "ts": ts})
        log = log[-(max_turns * 2):]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:  # pragma: no cover - best-effort, same discipline as _persist_report
        logger.warning("Could not persist chat turn for %s: %s", job_id, e)

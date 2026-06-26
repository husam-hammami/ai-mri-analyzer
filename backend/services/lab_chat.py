"""
Lab case chat — a patient asks plain questions about THEIR ONE completed lab report.

Mirrors services.case_chat (same subscription `claude -p` transport, flag-dark via MIKA_CHAT_ENABLED)
but grounds in the STRUCTURED lab results + the gated named assessment, and applies a deterministic
ANSWER-REPLACEMENT gate (NOT a token-strip): any red-flag / off-whitelist-condition / treatment term in
the model's answer discards the WHOLE answer and returns a fixed BILINGUAL safe template. "No new
condition" is an open vocabulary you cannot strip token-by-token, so the chat is gated like the header
(INCIDENTS #4: a second, non-deterministic consumer of the condition must be gated, not trusted).

Safety: the chat may discuss ONLY the condition compose_assessment already surfaced for THIS report; it
can never introduce another condition, a red-flag diagnosis, or any treatment/dose. The live `claude -p`
call cannot be self-verified inside a Claude session (nested hang, INCIDENTS #1/#2) — the pure functions
here (`build_lab_context`, `_gate_answer`, `_detect_lang`) are unit-tested with `ask_claude` mocked.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("mika.labchat")

try:  # server runs from backend/ (cwd=backend); fall back to package path
    from services.case_chat import ask_claude, _persist_turn
    from services.lab_reader import (
        _CONDITION_WHITELIST, _CONDITION_AR_ALIASES, _REDFLAG_TERMS, _REDFLAG_TERMS_AR,
        _TREATMENT_TERMS, _TREATMENT_TERMS_AR, _norm_text,
    )
except ImportError:  # pragma: no cover - import path when launched as a package
    from backend.services.case_chat import ask_claude, _persist_turn
    from backend.services.lab_reader import (
        _CONDITION_WHITELIST, _CONDITION_AR_ALIASES, _REDFLAG_TERMS, _REDFLAG_TERMS_AR,
        _TREATMENT_TERMS, _TREATMENT_TERMS_AR, _norm_text,
    )


# Bilingual safe template — the answer-replacement output (NOT an English-only flip on the most
# safety-critical chat output; mirrors compose_verdict's _TEMPLATES_AR discipline).
_SAFE_TEMPLATE = {
    "en": "I can't speak to that from this report — please review it with your doctor.",
    "ar": "لا أستطيع الإجابة عن ذلك من هذا التقرير — يُرجى مراجعته مع طبيبك.",
}


def _detect_lang(question: str) -> str:
    """'ar' if the question contains Arabic script, else 'en' (the chat answers in the patient's
    language; the safe-template must match so the gate doesn't flip languages)."""
    return "ar" if re.search(r"[؀-ۿ]", question or "") else "en"


LAB_SYSTEM_RULES = """You are MIKA, answering a patient's questions about THIS ONE lab/blood report, using ONLY the report below.

REPORT
{context}

RULES
1. Lead with the direct answer in your FIRST sentence. 2-4 short plain sentences (~40-60 words). Answer ONLY what was asked. No preamble, no headers. Plain markdown only.
2. Use ONLY the report above — its values, statuses, and the one LIKELY CONDITION already surfaced to the patient (if any). If the answer isn't in the report, say "I can't tell that from this report." NEVER invent a value, range, marker, or finding not listed.
3. You MAY explain the one already-surfaced likely condition and which results support it, in plain "this set of results is consistent with…" language. You must NEVER name a DIFFERENT condition, and never name cancer, leukaemia, or any serious/red-flag disease — if asked "could this be cancer / X?", say plainly that this report can't answer that and to review it with their doctor.
4. NEVER give treatment, medication, a dose, a supplement, or a procedure — not even a general one. If asked what to take or do, say that's for their doctor to decide.
5. Write for a worried non-expert: short everyday words, warm, never alarming or curt. Match the report's wording; never sound more certain than it does.
6. The app already shows a persistent "discuss with your doctor" disclaimer — do NOT repeat it or close with a routine "see your doctor" sign-off; just answer."""


def build_lab_context(report: dict) -> str:
    """Grounding text from the lab report payload: the verdict, the gated assessment (if any), optional
    demographics, and every result row (plain name, value, range, status, plain meaning)."""
    if not isinstance(report, dict):
        report = {}
    overall = report.get("overall") or {}
    lines = ["LAB REPORT"]
    if overall.get("takeaway"):
        lines.append(f"VERDICT: {overall.get('takeaway')}")
    a = overall.get("assessment")
    if isinstance(a, dict) and a.get("name_en"):
        sup = ", ".join(a.get("supporting") or [])
        lines.append(
            f"LIKELY CONDITION (already surfaced to the patient — the ONLY condition you may discuss): "
            f"{a.get('name_en')}" + (f" — supported by {sup}" if sup else "")
            + (f". {a.get('explanation_en')}" if a.get("explanation_en") else "")
        )
    else:
        lines.append("LIKELY CONDITION: none surfaced — do NOT name any condition.")
    pat = report.get("patient") or {}
    demo = ", ".join(f"{k}: {pat.get(k)}" for k in ("name", "age", "sex") if pat.get(k))
    if demo:
        lines.append(f"PATIENT: {demo}")
    lines.append("RESULTS:")
    for r in (report.get("results") or [])[:60]:
        if not isinstance(r, dict):
            continue
        nm = r.get("plain_name") or r.get("analyte_raw") or "?"
        val = " ".join(str(x) for x in (r.get("value"), r.get("unit")) if x)
        rng = r.get("ref_range_text")
        status = r.get("status") or "?"
        sev = r.get("severity_phrase") or ""
        mean = r.get("plain_meaning") or ""
        line = f"- {nm}: {val}" + (f" (range {rng})" if rng else "") + f" [{status}{(' · ' + sev) if sev else ''}]"
        if mean:
            line += f" — {mean}"
        lines.append(line)
    return "\n".join(lines)


def build_lab_prompt(context: str, history: list, question: str) -> str:
    parts = [LAB_SYSTEM_RULES.format(context=context), ""]
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


def _gate_answer(answer: str, surfaced_key, lang: str) -> tuple[str, bool]:
    """The deterministic last-writer GATE (answer-replacement, not token-strip). Returns
    (safe_answer, was_replaced). Short-circuit chain: first trip → replace whole answer + return.
    Checks BOTH the Latin-normalized form (Latin lists) AND the raw Arabic-preserving form (Arabic
    lists) — _norm_text strips Arabic, so an Arabic answer must be gated against the raw text."""
    safe = _SAFE_TEMPLATE.get(lang, _SAFE_TEMPLATE["en"])
    low = " " + _norm_text(answer) + " "                 # Latin-normalized (Arabic stripped)
    raw = " " + (answer or "").lower() + " "             # preserves Arabic script

    # 1. Red-flag term (Latin OR Arabic) → replace.
    if any(t in low for t in _REDFLAG_TERMS) or any(t in raw for t in _REDFLAG_TERMS_AR):
        return safe, True
    # 2. Treatment / drug term (Latin OR Arabic) → replace.
    if any(t in low for t in _TREATMENT_TERMS) or any(t in raw for t in _TREATMENT_TERMS_AR):
        return safe, True
    # 3. Off-whitelist condition naming (positive-list): the chat may only name the surfaced condition.
    surfaced = next((e for e in _CONDITION_WHITELIST if e.get("key") == surfaced_key), None)
    surfaced_name_norm = _norm_text(surfaced["name_en"]) if surfaced else ""
    surfaced_ar = (_CONDITION_AR_ALIASES.get(surfaced_key, []) + [surfaced.get("name_ar", "")]) if surfaced else []
    surfaced_ar = " ".join(surfaced_ar)
    for e in _CONDITION_WHITELIST:
        if surfaced and e.get("key") == surfaced_key:
            continue  # the surfaced condition + its own aliases are allowed
        # English aliases against the Latin-normalized text.
        for alias in e.get("aliases", []):
            an = _norm_text(alias)
            if an and f" {an} " in low:
                # Allow an alias that is part of the surfaced condition's own name
                # (e.g. "anemia" inside the surfaced "iron-deficiency anemia").
                if surfaced_name_norm and an in surfaced_name_norm:
                    continue
                return safe, True
        # Arabic aliases against the raw text.
        for alias in _CONDITION_AR_ALIASES.get(e.get("key"), []):
            if alias and alias in raw:
                if surfaced_ar and alias in surfaced_ar:
                    continue
                return safe, True
    return answer, False


def answer_lab_question(job_id, report, question, history, *, model, effort, timeout_s, data_dir):
    """Generate, apply the deterministic answer-replacement gate, persist, return (text, is_error).
    Language is detected from the question so the safe-template matches. ask_claude is the no-tools
    subscription `claude -p` transport (worker-only; never self-verified nested in a Claude session)."""
    ctx = build_lab_context(report)
    raw, err = ask_claude(build_lab_prompt(ctx, history, question), model=model, effort=effort, timeout_s=timeout_s)
    if err or not raw.strip():
        return ("", True)
    lang = _detect_lang(question)
    surfaced_key = (((report or {}).get("overall") or {}).get("assessment") or {})
    surfaced_key = surfaced_key.get("condition_key") if isinstance(surfaced_key, dict) else None
    safe, replaced = _gate_answer(raw, surfaced_key, lang)
    if replaced:
        logger.info(f"[labchat {job_id}] answer replaced by safety gate (lang={lang})")
    _persist_turn(data_dir, job_id, question, safe)
    return (safe, False)

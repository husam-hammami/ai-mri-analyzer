"""
Arabic presentation layer for MIKA reports (English-canonical, deny-by-default).

The English `report.json` is the ONLY clinical source of truth. This module DERIVES the
Arabic view from it:
  • certainty / disclaimer / grade words come from the FIXED glossary (the LLM never makes them);
  • descriptive PROSE is translated by one `claude -p` pass, then each field passes a fully
    deterministic gate (number parity · negation/laterality preservation · deny-by-default grade
    parity). Any field that trips the gate FALLS BACK to the English sentence — "show English when
    unsure" is always safe, an inverted Arabic claim is not.

Flag-dark behind MIKA_AR_ENABLED. See docs/Mika_Arabic_Plan.md. Nested `claude -p` cannot be
self-verified inside a Claude session (it hangs) — the live translate path needs a human run; the
gate + recognizers here are pure and unit-tested with the translator mocked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("mika.arabic")

try:  # server runs from backend/ (cwd=backend); fall back to package path
    from prompts.i18n_glossary import (
        CERTAINTY_AR, CONFIDENCE_AR, GRADE_AR, GRADE_RECOGNIZER,
        EN_NEG, AR_NEG, EN_LAT_LEFT, EN_LAT_RIGHT, AR_LAT_LEFT, AR_LAT_RIGHT,
        REPORT_DISCLAIMER_AR, GLOSSARY_VERSION,
    )
except ImportError:  # pragma: no cover - import path when launched as a package
    from backend.prompts.i18n_glossary import (
        CERTAINTY_AR, CONFIDENCE_AR, GRADE_AR, GRADE_RECOGNIZER,
        EN_NEG, AR_NEG, EN_LAT_LEFT, EN_LAT_RIGHT, AR_LAT_LEFT, AR_LAT_RIGHT,
        REPORT_DISCLAIMER_AR, GLOSSARY_VERSION,
    )

try:
    from core.palette import normalize_certainty
except ImportError:  # pragma: no cover
    from backend.core.palette import normalize_certainty


# ── Deterministic token recognizers ──────────────────────────────────────────────────────
_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|ml|%)?", re.IGNORECASE)
# Grade recogniser: longest-match first so compounds ("mild-to-moderate") beat their parts.
_GRADE_WORDS = sorted(GRADE_RECOGNIZER, key=len, reverse=True)
_GRADE_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(w) for w in _GRADE_WORDS) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _numbers(s: str) -> list:
    """Sorted numeric+unit tokens (Western numerals on both EN and AR sides)."""
    return sorted(re.sub(r"\s+", "", m.group(0).lower()) for m in _NUM_RE.finditer(s or ""))


def _grade_terms_en(s: str) -> list:
    """Recognised grade adjectives in the English, longest-match (lowercased)."""
    s = (s or "").lower()
    out, i = [], 0
    for m in _GRADE_RE.finditer(s):
        if m.start() < i:   # already consumed by a longer compound
            continue
        out.append(m.group(1))
        i = m.end()
    return out


def _grade_values_in_ar(ar: str) -> list:
    ar = ar or ""
    return [v for v in GRADE_AR.values() if v in ar]


def _count_any(s: str, cues, *, lower: bool = False) -> int:
    s = (s or "").lower() if lower else (s or "")
    return sum(s.count(c) for c in cues)


def _sides_en(s: str) -> frozenset:
    s = (s or "").lower()
    sides = set()
    if _count_any(s, EN_LAT_LEFT) > 0:
        sides.add("L")
    if _count_any(s, EN_LAT_RIGHT) > 0:
        sides.add("R")
    return frozenset(sides)


def _sides_ar(s: str) -> frozenset:
    sides = set()
    if _count_any(s, AR_LAT_LEFT) > 0:
        sides.add("L")
    if _count_any(s, AR_LAT_RIGHT) > 0:
        sides.add("R")
    return frozenset(sides)


def gate_ar_field(en: str, ar: str) -> str:
    """Return `ar` only if it is a faithful translation of `en`; else fall back to `en`.

    FULLY DETERMINISTIC, deny-by-default. The default on ANY doubt is English.
    """
    en = en or ""
    ar = (ar or "").strip()
    if not ar:                                   # empty / failed translation → English
        return en
    if _numbers(ar) != _numbers(en):             # 1) no new/changed numbers or units
        return en
    if _count_any(ar, AR_NEG) < _count_any(en, EN_NEG, lower=True):   # 2a) negation not dropped
        return en
    if _sides_ar(ar) != _sides_en(en):           # 2b) laterality preserved (no flip, no drop)
        return en
    grades = _grade_terms_en(en)                 # 3) grade parity — DENY-BY-DEFAULT
    for g in grades:
        ar_term = GRADE_AR.get(g)
        if ar_term is None or ar_term not in ar:     # unmapped grade OR its Arabic missing → English
            return en
    expected = {GRADE_AR[g] for g in grades if g in GRADE_AR}
    for v in _grade_values_in_ar(ar):            # no OTHER grade term introduced in the Arabic
        if v not in expected and not any(v in e for e in expected):
            return en
    return ar


# ── Glossary-keyed (non-LLM) rendering of the safety surface ──────────────────────────────
def certainty_ar(en_certainty) -> str:
    """Fixed Arabic certainty word, keyed by the canonical English tier (never LLM)."""
    return CERTAINTY_AR[normalize_certainty(en_certainty)]


def confidence_label_ar(label) -> str:
    return CONFIDENCE_AR.get(str(label or "").strip().title(), str(label or ""))


# ── Translation orchestration (the one claude pass; isolated + mockable) ───────────────────
_TRANSLATE_INSTRUCTIONS = (
    "Translate each English string in the JSON array to Modern Standard Arabic for a worried, "
    "non-technical patient. Rules, all mandatory:\n"
    "1. Translate MEANING faithfully — never drop a negation, never change a severity/grade, "
    "never invent a number or measurement.\n"
    "2. Keep every number and unit EXACTLY as written, in Western numerals (0-9), e.g. 6 mm.\n"
    "3. Keep Latin clinical tokens verbatim (vertebral levels like L4-L5, sequence names like "
    "FLAIR/T2, abbreviations) — do NOT transliterate them.\n"
    "4. Do NOT add, merge, split, reorder, or remove items. Return a JSON array of the SAME "
    "length, same order, strings only.\n"
    "Return ONLY the JSON array."
)


def _claude_translate(texts: list) -> list:  # pragma: no cover - needs a real terminal (nested hang)
    """Default translator: one headless `claude -p` translate-only pass. Cannot be verified
    inside a Claude session (nested reads hang) — exercised only on a real run."""
    import os
    import shutil
    import subprocess

    claude_bin = shutil.which("claude") or os.environ.get("MIKA_CLAUDE_BIN")
    if not claude_bin:
        raise RuntimeError("claude CLI not found for Arabic translation")
    model = os.environ.get("MIKA_AR_MODEL", "sonnet")
    prompt = _TRANSLATE_INSTRUCTIONS + "\n\nJSON array:\n" + json.dumps(texts, ensure_ascii=False)
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
           or os.environ.get("MIKA_AGENT_USE_API_KEY")}
    proc = subprocess.run(
        [claude_bin, "-p", "--output-format", "json", "--model", model],
        input=prompt, capture_output=True, text=True, encoding="utf-8", env=env,
        timeout=int(os.environ.get("MIKA_AR_TIMEOUT", "180")),
    )
    envelope = json.loads(proc.stdout or "{}")
    if envelope.get("is_error"):
        raise RuntimeError(f"Arabic translation failed: {envelope.get('result')}")
    out = json.loads(_strip_fence(envelope.get("result", "[]")))
    if not isinstance(out, list) or len(out) != len(texts):
        raise RuntimeError("Arabic translation returned a malformed / mismatched array")
    return [str(x) for x in out]


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0]
    return s.strip()


# ── Build the Arabic patient block from the English one ───────────────────────────────────
def build_ar_patient(patient_en: dict, translator: Optional[Callable] = None) -> dict:
    """Derive the Arabic `patient` block. Prose is translated then gated (English-fallback on
    any miss); certainty/disclaimer/confidence come from the fixed glossary. Numbers, figures,
    captions' images and the structured `certainty_key` are preserved verbatim.

    `translator(list[str]) -> list[str]` is injected for tests; defaults to one claude pass.
    """
    if not isinstance(patient_en, dict):
        patient_en = {}
    translate = translator or _claude_translate

    # 1) collect every prose string into one batch; each _queue returns its index (or None if blank)
    jobs: list = []   # list of (idx, english_text)

    bottom_idx = _queue(jobs, str(patient_en.get("bottom_line") or ""))

    key_points_en = _as_list(patient_en.get("key_points"))
    kp_idx = [_queue(jobs, t) for t in key_points_en]

    findings_en = _as_list(patient_en.get("findings"))
    f_plain_idx, f_cap_idx = [], []
    for f in findings_en:
        f = f if isinstance(f, dict) else {}
        f_plain_idx.append(_queue(jobs, str(f.get("plain") or "")))
        f_cap_idx.append(_queue(jobs, str(f.get("caption") or "")))

    wim_en = _as_list(patient_en.get("what_it_means"))
    wim_idx = [_queue(jobs, t) for t in wim_en]
    wf_en = _as_list(patient_en.get("worth_flagging"))
    wf_idx = [_queue(jobs, t) for t in wf_en]

    cot_en = patient_en.get("change_over_time") if isinstance(patient_en.get("change_over_time"), dict) else {}
    cot_plain_idx = _queue(jobs, str(cot_en.get("plain") or ""))
    cot_points_en = _as_list(cot_en.get("points"))
    cot_pts_idx = [_queue(jobs, t) for t in cot_points_en]

    conf_en = patient_en.get("confidence") if isinstance(patient_en.get("confidence"), dict) else {}
    note_idx = _queue(jobs, str(conf_en.get("note") or ""))

    # 2) one translation pass; on any failure, the whole block degrades to English (the C floor)
    try:
        raw = translate([t for (_, t) in jobs]) if jobs else []
        if len(raw) != len(jobs):
            raise RuntimeError("translator returned wrong length")
    except Exception as exc:
        logger.warning("Arabic translation unavailable (%s) — serving English (degradation floor)", exc)
        return {"_lang": "ar", "_degraded": True}

    # 3) gate each field against its English source, English-fallback on any miss
    gated: list = []
    for (_, en_text), ar_text in zip(jobs, raw):
        kept = gate_ar_field(en_text, ar_text)
        gated.append(kept)

    def g(idx):
        return gated[idx] if idx is not None and 0 <= idx < len(gated) else ""

    out: dict = {
        "bottom_line": g(bottom_idx),
        "key_points": [g(i) for i in kp_idx],
        "findings": [],
        "what_it_means": [g(i) for i in wim_idx],
        "worth_flagging": [g(i) for i in wf_idx],
        "confidence": {
            "label": confidence_label_ar(conf_en.get("label")),
            "label_key": conf_en.get("label", ""),
            "score": conf_en.get("score"),
            "note": g(note_idx),
        },
        "disclaimer": REPORT_DISCLAIMER_AR,
        "demographics": patient_en.get("demographics", {}),
        "_lang": "ar",
        "_glossary_version": GLOSSARY_VERSION,
    }
    for f, pi, ci in zip(findings_en, f_plain_idx, f_cap_idx):
        f = f if isinstance(f, dict) else {}
        en_cert = f.get("certainty", "")
        out["findings"].append({
            "plain": g(pi),
            "certainty": certainty_ar(en_cert),       # Arabic display word
            "certainty_key": normalize_certainty(en_cert),  # English tier → color in the AR PDF
            "figure": f.get("figure", ""),            # image path preserved verbatim
            "caption": g(ci),
        })
    if cot_en.get("plain") or cot_points_en:
        out["change_over_time"] = {
            "plain": g(cot_plain_idx),
            "points": [g(i) for i in cot_pts_idx],
            "figure": cot_en.get("figure", ""),
        }
    return out


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _queue(jobs: list, en_text: str):
    """Append an English prose string to the batch; returns its index (or None if blank)."""
    if not (en_text and str(en_text).strip()):
        return None
    jobs.append((len(jobs), str(en_text)))
    return len(jobs) - 1


# ── Sidecar persistence + staleness fingerprint ───────────────────────────────────────────
def _translation_source(patient_en: dict) -> dict:
    """The exact English prose the Arabic is derived from — fingerprint THIS so a reconcile
    rewrite of the English `patient` block invalidates a stale sidecar (plan: staleness guard)."""
    p = patient_en if isinstance(patient_en, dict) else {}
    findings = [
        {"plain": (f or {}).get("plain", ""), "certainty": (f or {}).get("certainty", ""),
         "caption": (f or {}).get("caption", ""), "figure": (f or {}).get("figure", "")}
        for f in _as_list(p.get("findings")) if isinstance(f, dict)
    ]
    return {
        "bottom_line": p.get("bottom_line", ""),
        "key_points": _as_list(p.get("key_points")),
        "findings": findings,
        "what_it_means": _as_list(p.get("what_it_means")),
        "worth_flagging": _as_list(p.get("worth_flagging")),
        "change_over_time": p.get("change_over_time") if isinstance(p.get("change_over_time"), dict) else {},
        "confidence": p.get("confidence") if isinstance(p.get("confidence"), dict) else {},
    }


def fingerprint(patient_en: dict) -> str:
    src = json.dumps(_translation_source(patient_en), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def sidecar_path(job_dir) -> Path:
    return Path(job_dir) / "report.ar.json"


def write_sidecar(job_dir, patient_en: dict, ar_patient: dict) -> Path:
    """Atomic temp-then-move write of the Arabic sidecar, stamped with the source fingerprint."""
    path = sidecar_path(job_dir)
    blob = {
        "src_fingerprint": fingerprint(patient_en),
        "glossary_version": GLOSSARY_VERSION,
        "patient": ar_patient,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_sidecar(job_dir, patient_en: dict) -> Optional[dict]:
    """Return the cached Arabic patient block ONLY if its fingerprint still matches the current
    English source; otherwise None (treat as absent → regenerate or serve English)."""
    path = sidecar_path(job_dir)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if blob.get("src_fingerprint") != fingerprint(patient_en):
        logger.info("Arabic sidecar stale (English report changed) — ignoring %s", path)
        return None
    return blob.get("patient")


def invalidate_sidecar(job_dir) -> None:
    """Delete the Arabic sidecar (called on the reconcile rewrite path so a post-Arabic reconcile
    can't leave Arabic asserting what the updated English no longer says)."""
    try:
        sidecar_path(job_dir).unlink(missing_ok=True)
    except Exception:  # pragma: no cover
        pass

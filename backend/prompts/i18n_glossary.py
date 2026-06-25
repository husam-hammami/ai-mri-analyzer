"""
Arabic ⇄ English i18n glossary — the FIXED safety vocabulary for MIKA's Arabic layer.

English `report.json` stays the ONLY clinical source of truth. These strings are the
*derived* Arabic for the safety-bearing surface (certainty tiers, the disclaimer, grade
adjectives, the calibration qualifier). They are rendered DETERMINISTICALLY keyed by the
English value — the translation LLM never produces them. See docs/Mika_Arabic_Plan.md.

⚠️  DRAFT — every Arabic string below is a DRAFT pending sign-off by a qualified clinical
    Arabic translator (the plan's human gate). `GLOSSARY_APPROVED` stays False until then,
    and the whole Arabic layer ships flag-dark behind MIKA_AR_ENABLED=0. Do NOT treat any
    Arabic medical wording here as clinically approved.
"""

GLOSSARY_VERSION = "0.1.0-draft"
GLOSSARY_APPROVED = False  # flip to True ONLY after a clinical Arabic translator signs off

# ── Certainty tiers ─────────────────────────────────────────────────────────────────────
# Keyed by the canonical palette key (core.palette.CERTAINTY_COLOR). Rendered from the
# STRUCTURED `certainty` field, never parsed from prose — so the chip can't drift from the tier.
CERTAINTY_AR = {
    "Confirmed": "مؤكَّد",
    "Likely": "مُرجَّح",
    "Possible": "محتمل",
    "Reference": "للمقارنة",
}

# ── Grade / severity lexicon (the dominant mistranslation risk) ───────────────────────────
# EVERY grade adjective the pipeline can emit, mined from the codebase: base_prompt.py's
# mandated qualifiers (mild/moderate/severe/small/large) + dicom_engine grades ("marked",
# "moderate") + prompt/reconciliation prose. The gate is DENY-BY-DEFAULT: a recognised grade
# with no mapping here, or whose Arabic term is absent from the translation, forces English.
GRADE_AR = {
    "mild": "خفيف",
    "minimal": "ضئيل",
    "moderate": "متوسط",
    "marked": "ملحوظ",
    "severe": "شديد",
    "small": "صغير",
    "large": "كبير",
    "tiny": "متناهي الصغر",
    "trace": "أثر طفيف",
    "extensive": "واسع الامتداد",
    "significant": "جسيم",
    "subtle": "طفيف",
    "gross": "صارخ",
    "prominent": "بارز",
    "advanced": "متقدِّم",
    "borderline": "حدّي",
    "critical": "حرج",
    "high-grade": "عالي الدرجة",
    "low-grade": "منخفض الدرجة",
    "mild-to-moderate": "خفيف إلى متوسط",
    "moderate-to-severe": "متوسط إلى شديد",
}

# Recogniser SUPERSET — grade-bearing adjectives the gate watches for in the English. Any of
# these present in the English whose mapping is NOT in GRADE_AR (or whose Arabic is missing
# from the translation) → fall back to English. The extras beyond GRADE_AR keys are deliberately
# left UNMAPPED so they trip deny-by-default rather than being silently kept.
GRADE_RECOGNIZER = set(GRADE_AR) | {
    "slight", "huge", "massive", "pronounced", "progressive", "worsening", "profound",
}

# ── Negation + laterality cue lists (count-preservation checks) ───────────────────────────
# A dropped negation ("no narrowing" → "narrowing") or a flipped side is a clinical-meaning
# change the number gate can't see; these let the gate count them on both sides.
EN_NEG = ("no ", "not ", "without", "absent", "absence", "unremarkable",
          "normal", "negative", "none", "free of", "no evidence")
AR_NEG = ("لا ", "لم", "ليس", "بدون", "دون", "غياب", "سليم", "طبيعي", "خالٍ", "خالي", "انعدام")

EN_LAT_LEFT = ("left",)
EN_LAT_RIGHT = ("right",)
AR_LAT_LEFT = ("يسار", "أيسر", "اليسرى", "يسرى", "الأيسر")
AR_LAT_RIGHT = ("يمين", "أيمن", "اليمنى", "يمنى", "الأيمن")

# ── Calibration qualifier (the EXACT phrase base_prompt.py mandates in uncalibrated mode) ──
VISUAL_ESTIMATE_EN = "(visual estimate — no calibrated measurement available)"
VISUAL_ESTIMATE_AR = "(تقدير بصري — لا يتوفر قياس مُعاير)"

# ── Confidence labels (the patient confidence chip: High / Moderate / Low) ────────────────
CONFIDENCE_AR = {
    "High": "عالية",
    "Moderate": "متوسطة",
    "Low": "منخفضة",
}

# ── The patient disclaimer — translated ONCE, frozen. DRAFT until clinical sign-off. ──────
# Mirrors backend/prompts/base_prompt.py REPORT_DISCLAIMER, including the PACS/calipers/scroll
# limitation. NEVER LLM-translated per-study.
REPORT_DISCLAIMER_AR = (
    "أُنشئ هذا التحليل باستخدام تفسير الصور بمساعدة الذكاء الاصطناعي كأداة تشخيصية مساعدة. "
    "وهو لا يُعدّ تقريراً إشعاعياً رسمياً ولا يُغني عن التقييم من قبل طبيب أشعة معتمد. "
    "لم يكن لدى المحلّل وصول إلى محطة عرض الصور (PACS) أو أدوات القياس (الفرجار)، "
    "ولا القدرة على التنقّل الديناميكي بين المقاطع وضبط مستوى النافذة والتباين. "
    "ينبغي ربط جميع النتائج بالتاريخ السريري والفحص البدني."
)

# Plain UI strings reused by the Arabic PDF section headers (mirrors report_builder.py).
PDF_SECTIONS_AR = {
    "summary": "الخلاصة",
    "confidence": "درجة الثقة",
    "findings": "النتائج",
    "legend": "يدل اللون على درجة التأكد من كل نتيجة:",
    "focused_evidence": "مراجعة الأدلة المركّزة",
    "reference_review": "مراجعة مُسانَدة بمرجع",
    "change_over_time": "التغيّر عبر الزمن",
    "what_it_means": "ما الذي قد يعنيه هذا",
    "notes": "ملاحظات",
    "clinician_note": "تتوفر نسخة تقنية مفصّلة للطبيب المُحيل.",
    "shown_in_english": "(معروض بالإنجليزية)",
}

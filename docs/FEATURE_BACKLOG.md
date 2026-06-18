# MIKA — Feature & Decision Backlog

Running log of product decisions made in discussion. Status: 🔴 todo · 🟡 in progress · 🟢 done.

## Validation / trust (mostly built)
- 🟢 Accuracy harness — detection (free) + LLM-judge reading scoring. `backend/validation/`
- 🟢 Labeled ground-truth test data (TCIA/NLM/MSD, no-login). `fetch_labeled.py`
- 🟢 Second-reader sensitivity pass — recovers subtle misses, keeps specificity. `second_reader.py` (validated: prostate miss→catch, normals stayed clean)
- 🟢 Annotation mask-overlap check (prostate verified: right side/zone). `annotation_overlap.py`
- 🟡 Per-case ground-truth verification (caught the prostate mislabel) — make it standard before any published number.

## Architecture changes
- 🔴 **Wire the second-reader into the app as a GATED layer** — trigger on negative/benign/low-confidence first reads (and always for cancer-screening). Currently test-harness only, not live.
- 🔴 **Fix patient/clinical register** — today they output near-identical text with bugs. Make them genuinely different:
  - **Patient:** very simple, generalized — "what was seen" + "what it means for you" + "what to do next", minimal/no medical terms.
  - **Clinical:** technical radiology voice + correct report format (Technique / Comparison / Findings / Impression), tiers, figure refs.
  - Format/template each correctly and separately.
- 🔴 Add `annotation_coords` to `summary.json` (series, slice, pixel xy, patient mm) → makes annotation overlap pixel-exact + automatic for every study.

## New user features
- 🔴 **"Prepare questions for your doctor"** — turn findings into questions to ask (defers to physician; safe, high-value).
- 🔴 **AI chat on the findings** — scoped ONLY to this study's findings + figures; hard guardrails (no new diagnosis, no treatment/meds advice, no general medical Q&A, cites the finding, defers to doctor).
- 🔴 **Blood-test ingestion as imaging context** — labs to enrich the read (PSA↔prostate, AFP↔liver, eGFR↔contrast). NOT a standalone lab-diagnosis product.

## Go-to-market / legal
- 🔴 **Consumer-first launch**, hosted/closed SaaS (no free clinical deployment path). Pricing band: **$9–$99/report, free first read** as the hook; position as "second-opinion quality at AI price" vs $199–399 human re-reads.
- 🔴 **"Not a diagnosis" gate** — small attractive must-check acknowledgment + short privacy notice (required even though it's free; "free" ≠ no liability). Lawyer to draft.
- 🔴 Enterprise/clinical track = LATER (needs FDA SaMD + HIPAA/BAA + PACS). That bundle is what makes hospitals pay (prevents free clinical use).

## Open benchmarks to run
- 🔴 **Head-to-head vs cheap competitors** on the SAME public labeled cases — score accuracy **and reasoning quality** (add a reasoning dimension to the judge) **and** localization. Manual (their ToS/no API). This is the credible "MIKA is better" proof.
- 🔴 Bigger mask-bearing cohort (MSD/BraTS/LIDC/KiTS/SPIDER/PROSTATEx) → diagnosis accuracy + annotation accuracy in one run.

## Market context (researched, 2025–2026)
- Enterprise tools (Aidoc/Viz/Qure/Annalise/Lunit/RapidAI/Gleamer/Nanox/RadAI): narrow-to-broad **detection/triage**, clinician-facing, opaque pricing (Annalise $180k/site/yr; Viz $1,040/patient; Gleamer/Qure ~£1–$2/scan). None patient-facing.
- Consumer "explain my scan" (MIKA's real lane): AI reads **$9–$99**; human second opinions **$199–$399**. Cheap ones mostly translate report *text* or are single-modality.
- MIKA edge: all-modality + reads pixels + patient plain-language + confidence tiers + annotated proof.

# MIKA — Pre-Release Reliability Hardening Plan

> **For the implementing agent (read this first):** This is an approved, **not-yet-implemented** plan to
> harden MIKA's backend read path before public release. Implement the four fixes **in priority order**
> (Fix 1 is the gating blocker). It is **backend read-path only** — no UI or feature changes. Every fix
> below traces to a **real failure observed in testing** (see the Incident Log) — implement them even if
> you did not witness those failures; they are reproducible. Verify with the checklist at the end before
> declaring done. Do **not** weaken the "already safe" behavior noted under each section.
>
> Status: planned 2026-06-18 · scope: `backend/` · convention: Python 3.11, dataclasses (NOT Pydantic),
> `pathlib`, `logging.getLogger("mika.<module>")`.

---

## Why this exists — Incident Log (real failures from heavy testing)

These are the actual failures hit while building + running the accuracy benchmarks. Each maps to a fix.

| # | What happened | Root cause | Production-relevant? |
|---|---|---|---|
| **F1** | The read process **died mid-run repeatedly** (PID vanished, no clean exit) — the job then hangs forever. | Reads run **in-process** via FastAPI `BackgroundTasks`; a *process* kill (OOM, OneDrive file-lock, OS reap) bypasses the `try/except`. No watchdog/job-timeout/recovery exists. | **YES — the headline risk.** → Fix 1 |
| **F2** | A `pip install` (pandas/pyarrow) pulled **NumPy 2.x**, broke SciPy, and **crashed every read** until `numpy` was pinned back to 1.26.4. | Core deps are pinned in `requirements.txt`, but no lock file + no runtime version check, so an out-of-band install bypasses the pins. `dicom_engine.py:24` imports scipy **eagerly** → fails at boot. | **YES** → Fix 3 |
| **F3** | A consumer iterated the agent's `impression` field **character-by-character** (`"S \| i \| n \| g..."`) — a confirmed PDF crash/garble path. | The agent's `summary.json` sometimes emits `impression`/`findings`/`key_points` as a **string instead of a list**; agent mode does a raw `json.loads` with **zero validation**. | **YES** → Fix 2 |
| **F4** | A study read **timed out** with partial output but was served as a clean `complete`. | `agent_runner.py` promotes a timeout to `success=True` whenever a PDF exists, with no flag. | **YES** → Fix 4 |
| **F5** | The agent **finished writing** its report but the parent process died **before persisting** → report on disk, never captured (manual salvage needed). | Same root cause as F1; `_persist_report` only runs on the success path, in-process. | **YES** → Fix 1 (salvage) |
| **F6** | (Already fixed; context) detection bugs: CT-NRRD→MR, hippocampus→"hip", CXR-PNG→unknown/OT. | Heuristics ignored sequence descriptions / filename hints. | Fixed already — do not redo. |
| (n/a) | Benchmark harness wrote empty reads to cache and counted them. | This was `backend/validation/` (the test harness), **NOT the production app** — the app already blocks failed reads from persistence. | No — out of scope. |

---

## Validation context — MIKA's measured accuracy (so you implement with the right mental model)

Honest profile from the validation harness (`backend/validation/`), 2026-06-18. This is *why* Fix 2/Fix 4
matter (don't let a malformed/partial read masquerade as a confident report) and why MIKA is positioned as a
**second-reader, not a standalone diagnostician**:

- **Detection (anatomy / modality / calibration): ~100%** after the F6 fixes.
- **Reading, conspicuous lesions: strong.** On verified-truth cases (brain GBM, lung adenocarcinoma, kidney RCC,
  TB chest X-ray, brain-tumor) MIKA was correct with **0 false positives** on the normal controls. Liver HCC was
  found+named **only with the full multiphasic study** (single non-contrast series → under-call): input completeness matters.
- **CheXpert (50 chest X-rays vs radiologist-consensus labels — a private gut-check, not citable):** the headline
  "80% macro accuracy" is **misleading** (inflated by specificity on absent findings). **Real overall sensitivity ≈ 50%**
  on abnormal findings — MIKA is a **conservative under-caller** on multi-finding CXR (writes off cardiomegaly as
  "AP technique", calls abnormal films "no acute process"). Caveats: 320px downsampled images, ~5 failed reads,
  single-LLM-judge label mapping. **Does NOT support a "senior-level CXR" claim.**
- **Second-reader sensitivity pass** (`backend/validation/second_reader*.py`): recovered a verified subtle ~5mm
  prostate cancer the first read missed (missed→correct, **localization mask-verified** to the correct zone/side)
  **without overcalling** the normals. Promising but n=4 and **not wired into the app**.
- **Ground-truth lesson:** verify labels **per-case** — a "prostate miss" was actually a mislabeled case
  (collection-level "this cohort = cancer" assumption was wrong for that patient).

---

## The fixes

### Fix 1 — [BLOCKER] Recover from a dead / stuck read worker (F1, F5)
Reads run in-process via `BackgroundTasks` → `_run_agent_pipeline` (`backend/app.py` ~L523, L973). The
`try/except` (~L1128) only catches **in-process exceptions**. A killed process freezes the job at
`status="interpreting"` forever: SSE (~L608-637) loops until `complete|error` so it **never closes**
(progress stuck ~95%); a report the agent wrote to `work/report/` is **never persisted**; after restart
`JOBS` is empty and `/api/status` → **404** for a study the user waited on. No watchdog/timeout/recovery exists.

Add in `backend/app.py`:
1. **Heartbeat on disk** — on each progress tick (where it already writes `progress.json`, ~L1064), also write
   `DATA_DIR/<job>/status.json` = `{status, progress, message, heartbeat_ts, truncated}`. A killed worker leaves a stale heartbeat.
2. **Boot reconciliation (FastAPI lifespan startup)** — scan `DATA_DIR/<job>`:
   - **Salvage (fixes F5):** `work/report/*.pdf` + `summary.json` present but no `meta.json`/`report.json` → run the
     existing `_persist_report` logic from the on-disk artifacts so the finished report becomes retrievable.
   - **Mark interrupted:** `status.json` non-terminal with a stale heartbeat and no salvageable report → rewrite to
     `status="error"`, `error="read was interrupted — please re-run"`.
3. **Watchdog / job-timeout** — a supervising task (or a check inside the status/SSE handler) flips any non-terminal
   job whose heartbeat is older than `MIKA_AGENT_TIMEOUT_S + margin` to `error`, so SSE/progress terminate honestly.
   (`MIKA_AGENT_TIMEOUT_S` exists at `agent_runner.py:33`.)

### Fix 2 — [HIGH] Normalize/validate the agent `summary.json` (F3)
`_collect_outputs` (`backend/services/agent_runner.py` ~L293) does a raw `json.loads`; `report_builder.build_patient_report`
iterates `findings`/`key_points`/`what_it_means`/`change_over_time.points` as lists and treats `patient`/`confidence`
as dicts (~L85,114,154,186) → a string/null there garbles or crashes the PDF.
- **Add** `_normalize_summary(summary: dict) -> dict` in `agent_runner.py`, **shape-only, non-destructive of valid
  findings**, mirroring `claude_interpreter.py:393-401`: `patient`→dict; `findings`→list (drop non-dict items);
  `key_points`/`what_it_means`/`worth_flagging`/`impression`→list[str]; `confidence`/`study`/`change_over_time`→dict
  (with `points`→list); missing → safe defaults. Plain type-hinted function.
- **Call once** in `_collect_outputs` right after `json.loads`.
- **Belt-and-suspenders:** additive `isinstance` guards in `build_patient_report`/`bullets` — wrong shape *skips a
  section*, never crashes. Additive only; do not restructure the dense render flow (`report_builder.py` ~L48-216).

### Fix 3 — [HIGH] Enforce the environment at runtime (F2)
Imports are **eager** (`app.py:40` → `dicom_engine.py:24`), so a broken SciPy fails at **boot**. The exposure is drift
*after* boot (the ad-hoc install), which a lock file alone can't prevent.
- **`check_env()`** — assert the read pipeline imports **and** exact versions (`numpy`, `scipy`, `pydicom`, `Pillow`)
  match `requirements.txt`. Run at startup (lifespan) **and** as a cheap guard at the top of each read → a mid-session
  drift fails the job cleanly ("environment changed — restart MIKA") instead of an opaque crash. Expose `GET /health`.
- **`requirements.lock`** — full transitive freeze of a known-good venv; install from it in `run.sh`/README.
- **Bound the floating deps:** `nibabel>=5.2.0,<6`, `pynrrd>=1.0.0,<2` in `requirements.txt`.
- **Reconcile the numpy pin first:** env was validated at `numpy==1.26.4`; `requirements.txt` says `1.26.3`. Pick the one
  verified against `scipy==1.12.0`, then align the assertion + lock. See `numpy<2` constraint — it is hard.

### Fix 4 — [MED] Don't serve a timed-out / patient-less read as "complete" (F4)
`agent_runner.py` ~L531-541 promotes a timeout to `success=True` if a PDF exists, with no flag; app.py then marks it `complete`.
- Add `AgentResult.truncated` (~L161); set it on the timeout-with-partial path; carry into job + report payload + SSE as
  "this read may be incomplete — re-run recommended".
- Tighten the full-run success gate (~L537,570) to **`pdf_path AND a non-empty normalized `summary.patient``** (after Fix 2).
  Conscious behavior change (today a `report_clinical.pdf`-only run can pass); the prompt always requests a patient block
  (`agent_runner.py:428-442`), so this is expected-safe — flag it, don't tighten silently.

### Already safe — do NOT touch
- A read returning `success=False` / raising → `status="error"`, **not persisted**; `GET /api/report` hard-gates on
  `status=="complete"` (else 400); SSE/status surface the error. (`app.py` ~L1116-1128, `get_report` ~L745.)

### Dropped / deferred (per review)
- **No blind auto-retry** — it runs in the process that just died (doesn't help F1/F5) and doubles cost on deterministic
  failures. (Optional: retry only transient classes — CLI-not-found / non-zero exit with no artifacts — never timeout.)
- **OneDrive** — non-blocking startup warning if `MIKA_DATA_DIR` looks synced + a doc note to use a local disk. Never a hard gate.

---

## Critical files
- `backend/app.py` — lifespan boot reconciliation/salvage + `check_env` + `/health`; `status.json` heartbeat on tick;
  watchdog; carry `truncated`. (`_run_agent_pipeline` ~L973, `_persist_report` ~L255, status/SSE ~L561-637, `JOBS` ~L209.)
- `backend/services/agent_runner.py` — `_normalize_summary` + call in `_collect_outputs` (~L293); `AgentResult.truncated`
  (~L161); set truncated on timeout (~L531); tighten success gate (~L537,570).
- `backend/services/report_builder.py` — additive `isinstance` guards in `build_patient_report`/`bullets` (~L48-216).
- `backend/core/dicom_engine.py` — eager scipy import (L24-25): the boot-fail reason + version-assertion target.
- `requirements.txt` (bound nibabel/pynrrd) + new `requirements.lock` + `run.sh`/`README.md` (install-from-lock).
- Reuse pattern: `backend/services/claude_interpreter.py:393-401` (existing impression coercion).

## Verification checklist
1. **Dead-worker recovery (the blocker):** start a read, `kill -9` the server mid-run; restart → boot reconciliation
   marks the job `error` (stale heartbeat) OR salvages it (pdf+summary on disk) → `/api/report` serves it, no 404.
2. **Watchdog:** force a non-terminal job with an old heartbeat → status flips to `error`, SSE closes.
3. **`_normalize_summary` unit tests:** impression/findings/key_points as strings, `patient`/`confidence` as strings,
   null/missing → safe normalized dict, no raise; valid findings preserved.
4. **Render test:** malformed `summary.json` through `_collect_outputs`→`build_patient_report` → PDF renders, sections
   skipped, no char-garble.
5. **Env assertion:** boot OK + `/health` shows versions; shadow a bad numpy/scipy → `check_env` fails the read cleanly.
6. **Reproducible install:** fresh venv from `requirements.lock` → `from scipy.signal import find_peaks` imports; `py_compile` backend.
7. **Truncated path:** short `MIKA_AGENT_TIMEOUT_S` → read flagged truncated, not silent `complete`; a PDF-but-no-`patient` run → not `complete`.
8. **End-to-end:** real read on `test_data/blind_spine_sag` → clean report; a deliberately failed read → `status=error`, never served.

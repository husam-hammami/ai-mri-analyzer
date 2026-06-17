# MIKA — Production Hardening Changes

Goal: subscription login (no API key / no extra deps), all imaging modalities, durable reports
that never disappear, robust area-recognition + annotation, a real sidebar with smooth
back/forward navigation, and production-grade data security.

All backend files compile (`py_compile`) and the frontend renders error-free; the persistence,
security, and navigation changes were verified end-to-end in a real browser against a running
server.

---

## 1. Login on the user's Claude subscription — no API key, no extra library
- The default **agent pipeline already runs on the subscription**: it shells out to the installed
  `claude` CLI in headless mode (`claude -p --output-format json --model opus
  --permission-mode bypassPermissions`), with `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` stripped
  from the child env so the stored Claude login (subscription) is used. Verified live:
  `claude auth status` → `{"loggedIn":true,"subscriptionType":"max"}`; a headless call returns the
  JSON envelope MIKA parses (`.result`, `.total_cost_usd`, `.usage`).
- The `anthropic` SDK is **only imported lazily** inside `build_anthropic_client()` — the app boots
  and runs the subscription path with the SDK absent. The SDK + API key is an optional "lite"
  fallback only.
- Frontend already defaults to `mode:'agent'` and **sends no API key**. Added a sidebar
  **connection chip** ("Connected to Claude · MAX" / "Connect to Claude") wired to
  `/api/agent/availability` + `/api/connect` (browser sign-in).
- FastAPI title/description and stale "Claude Opus 4.6" copy updated; lite-mode SDK default model →
  `claude-opus-4-8` (the CLI uses the `opus` alias which auto-resolves to the latest).

## 2. All imaging modalities (not MR only)  — `core/format_converter.py`
- NIfTI/NRRD imports no longer hard-label everything `MR`: a conservative filename/description
  heuristic (`_guess_modality_from_name`) maps CT/PET/US/CR/MR; unknown still defaults to MR **but
  emits a warning** so a CT/PET study can't silently masquerade as MRI.
- CT synthetic DICOMs get default soft-tissue window tags (`WindowCenter 40 / WindowWidth 400`) so
  downstream rendering shows tissue contrast instead of a globally-normalized image.
- The agent already reads the DICOM Modality tag and applies modality-specific physics + an
  anti-hallucination block (no fabricated MR sequences on CT/X-ray/US); modality flows into the
  report and the Wait/sequences panel.

## 3. Reports & images never disappear  — `app.py`
Root cause was an **in-memory-only `JOBS` dict** + a CWD-relative `./data` dir: a restart lost every
report, and image/PDF endpoints only checked memory.
- **Durable data dir**: defaults to a stable per-user location (`%LOCALAPPDATA%\MIKA\data`,
  `MIKA_DATA_DIR` overrides) instead of `./data`.
- On completion each job writes `report.json` (the exact `GET /api/report` payload) + `meta.json`
  (status, anatomy, modality, title, thumbnail, image map, pdf path) under `DATA_DIR/<job_id>/`.
- `report`, `status`, `images`, `pdf`, and `sequences` endpoints **fall back to disk** when a job
  isn't in the live cache — so a finished study is always retrievable by `job_id` after a restart.
- New `GET /api/reports` indexes all studies on disk; the **Recent studies** screen is backed by it
  (durable — survives a refresh, a server restart, AND a browser-storage clear) with thumbnails.
- Verified: with **no live job in memory**, `report`/`status`/`image` all serve from disk (200),
  `/api/reports` lists the study, and the browser opens it from Recent into a full Read.

## 4. Area recognition, asset mapping & exact-point annotation
- Anatomy + modality detection routes every study to a sensible body figure with safe fallbacks
  (unknown/cardiac/unsupported MSK subregions → whole-body figure, never a mis-mapped one).
- **Exact "point at the issue" annotation** is produced by the agent drawing on the *actual slice*:
  the prompt requires localizing each finding by intensity analysis, placing the marker, pixel-
  verifying the tip, choosing the slice where the finding is maximal, and **dropping** any annotation
  it can't verify ("drop, don't fudge") — generalized to **all anatomies and modalities**, not just
  spine. Schematic body-figure pins are an orientation aid; the exact pointing lives on the proof
  slice.
- Honest note: "100% accuracy" on real clinical images is not literally guaranteeable. The system
  maximizes it (robust detection, modality-aware reading, pixel-verified on-slice annotation, a
  second-pass self-audit) and is transparent about uncertainty via the certainty tiers. Per-region
  schematic pin maps (e.g. hip/shoulder) remain gated on clinician sign-off rather than shipping
  unverified coordinates.

## 5. Sidebar & navigation
- Removed the dead **"Shared with me"** item (and `SharedScreen`); added **"About MIKA"** and a
  **connection-status chip**. Nav: New study · current Read · Recent studies · About — no dead-ends.
- **Browser Back/Forward now work**: route + study are mirrored into the URL as real history entries
  (pushState) with a popstate restorer; a refresh restores the same place. Verified: Home → Recent
  (`?route=recent`) → Back → Home; opening a recent study deep-links `?job_id=...`.

## 6. Production data security  — `app.py`, `core/format_converter.py`
- Binds **127.0.0.1** by default (`MIKA_HOST` to override); CORS pinned to a localhost allow-list
  with `allow_credentials=False` (was `*` + credentials).
- `job_id` validated against `^[0-9a-f]{8}$` on every endpoint; image names allow-listed; image/PDF
  paths resolved and **confined to the job directory** (anti path-traversal) — verified `..%2f..%2f`
  and `fig%2Fevil` → 404.
- **Zip-slip guard** on archive extraction (members validated to stay inside the extract dir).
- Upload **size cap** (`MIKA_MAX_UPLOAD_BYTES`, default 2 GB) + **filename sanitization** on write.
- CSP + `X-Content-Type-Options` + `X-Frame-Options: DENY` + `Referrer-Policy` on the app shell.
- Configurable log level (`MIKA_LOG_LEVEL`).

### Remaining for a *hosted, multi-user* deployment (out of scope for the local desktop build)
Per-user authentication/authorization, encryption at rest, and a PHI retention/TTL policy are
documented follow-ups — the current posture is correct for the local, single-user desktop app.

---

## Post-review fixes (adversarial multi-agent review of the diff)
A 4-dimension adversarial review (each finding double-verified) surfaced 8 real issues, all now fixed
and re-verified:
1. **(high, regression) Agent figures with spaces/parens/non-ASCII names 404'd** — the new image-name
   guard was a tight charset allow-list, but agent figure names are persisted verbatim as keys. Relaxed
   the guard to block only traversal (`/` `\` `..` / empty / overlong); the name is only a map key and
   `_safe_join` still confines the resolved path. Frontend `proofUrl` now `encodeURIComponent`s the stem.
   Verified: `/api/images/<job>/L4-L5%20herniation` → 200.
2. **(med) Manifest stored OS-specific backslash paths** — `_rel_to_job` now writes forward-slash
   (`as_posix()`) and `_safe_join` normalizes backslashes, so manifests resolve across OSes.
3. **(low) `_safe_join` could return the job dir itself** for an empty/`.` rel → 500. Now requires a
   strict descendant **file** (`p.is_file()`), never a directory.
4. **(med) Upload cap ineffective** — uploads now stream to disk in 1 MB chunks with the cap enforced
   mid-read (no whole-file RAM buffering); ZIP extraction now rejects zip bombs (member-count +
   declared-decompressed-size ceilings) before extracting.
5. **(med) No CSRF on state-changing POSTs** — added an Origin-guard middleware: cross-site POSTs are
   403'd; same-origin (any localhost port, so the Electron random-port build works) and no-Origin
   (desktop/non-browser) pass. Verified: evil-origin → 403, same-origin → allowed.
6. **(med, regression) Browser Back to Recent dropped to Home and wiped the open report** — the popstate
   handler called `goNew()` (which reset the route); it now clears the job while honoring the URL's
   route. Verified in-browser: open study → Back → lands on Recent.
7. **(med, regression) Deep-link load pushed a phantom history entry and briefly stripped `?job_id`** —
   added a mount guard so the first URL-sync no-ops until the async resume sets the job. Verified: the
   `?job_id` survives the load.
8. **(high, regression) Modality heuristic misrouted MR NIfTI/NRRD → US/PET** (`echo`→US, `pt`→PET
   checked before MR). MR-family is now checked first and the ambiguous `echo`/`pt` tokens were removed.
   Verified by unit test (incl. BIDS multi-echo names) — real MR stays MR; CT/PET/US still detected.

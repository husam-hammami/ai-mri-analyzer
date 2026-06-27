# PLAN — MIKA Electron OTA Release (Windows-first, charity/free)

> Forged in /warcry (2026-06-27). Scouts: pre-mortem + feasibility (completed); prior-art folded into
> feasibility; cartography + feature-inventory gathered directly after a mid-run process restart killed
> 3 background scouts. Status: DRAFT → /bulletproof → /katana.

## Goal / Done-when
A production, **code-signed Windows installer** of MIKA that:
1. **Bundles its Python backend** so a clean Windows machine needs no host Python (today `main.js`
   spawns host `python` — `electron/main.js:91`).
2. **Auto-updates (OTA) from GitHub Releases** via electron-updater: publish a new Release → installed
   apps download + install on quit.
3. Keeps the **private/local, BYO-Claude** posture — the only user setup is the existing "Sign in with
   Claude" flow; the app detects (never bundles) the proprietary `claude` CLI.
4. Has **every feature verified working + visually daedalus-approved + consistent across imaging/lab**
   (esp. chat) — a ∀ coverage gate, not a sample.
5. Is **free/charity-ready** to publish.

**Done-when (in-session portion):** the bundled-Python build runs the backend with no host Python;
electron-updater + `publish` config + version-bump discipline are wired; signing config is in place
(cert supplied by owner); the consistency-sweep inventory + coverage gate pass; daedalus + sincere
finalize done; all pure/demo/import/build smoke green. **Out-of-session (owner/real-machine):** acquire
the signing cert, run the clean-VM install + a real OTA cycle, publish the Release. These CANNOT be done
in a Claude session (nested `claude -p` hangs — INCIDENTS #1/#2; signing/build/live-OTA need a real box).

## Approach (clear winner — no multi-approach debate needed; the feasibility scout's constraints decided it)
- **Bundle Python via python-embeddable + vendored wheels in `extraResources/python`** (NOT PyInstaller).
  Embeddable loads pre-built numpy/scipy/PyMuPDF wheels off `sys.path` with no rebuild + lowest AV
  false-positive risk; NSIS swaps the whole app dir atomically on update so the bundled runtime updates
  for free. ~300MB unpacked. *Rejected:* PyInstaller one-dir (AV-flag risk, no win) — fallback only;
  relocatable venv (absolute-path fragility) — rejected.
- **Claude CLI = detect-and-guide, never bundle.** It's proprietary (Anthropic, all-rights-reserved) —
  redistribution is a licensing risk. Keep `shutil.which("claude")` detection; add a first-run gate that
  guides to the official installer (`winget install Anthropic.ClaudeCode` / `irm https://claude.ai/install.ps1 | iex`)
  then the existing sign-in. *Rejected:* bundling the binary; npm path (deprecated v2.1.15).
- **OTA = GitHub Releases + electron-updater** (owner-locked). `publish:{provider:github}`,
  `autoDownload:true`, `autoInstallOnAppQuit:true`, differential/blockmap updates, `perMachine:false`
  (already set — the safe choice for silent `quitAndInstall`). *Clarify in-plan:* "OTA on repo change"
  = **per published GitHub Release**, not per-commit.
- **Signing on the critical path** (electron-updater verifies the Windows signature before applying →
  unsigned = no silent update + SmartScreen). Route is an **owner decision** (see §Owner external steps).

## The consistency sweep (∀ — the spine of the goal; enumerate, don't sample)
**Inventory population = SCREENS × LANG × STATE + ENDPOINTS, plus imaging↔lab parity.** Build a
machine-checkable inventory and a coverage gate that re-runs it.

- **Screens/routes** (frontend/index.html state machine): `home`/new-scan, `wait`, `read` (plain +
  clinician), `lab-home`, `lab-wait`, `lab-read`, `recent`, About modal, Connect/sign-in modal, chat
  drawer (imaging + lab). Re-enumerate: `grep -nE "view === '|screenFor|'lab-read'|'lab-wait'" frontend/index.html`.
- **Backend endpoints**: re-enumerate `grep -nE "@app\.(get|post)" backend/app.py`; each must respond
  (schema-valid) for both imaging + lab where applicable.
- **i18n coverage (EN+AR)**: every `COPY.*`/`L('…')` string has an `AR_UI` entry. Re-enumerate:
  `grep -oE "L\('[^']+'\)" frontend/index.html | sort -u` cross-checked against the `AR_UI` map; plus the
  Electron-shell strings in `main.js` (error dialogs/title — currently English-only) must be covered.
- **imaging↔lab parity** (the consistency requirement): chat trigger/UX/scope identical pattern; wait
  screens; topbar; recent-list rendering; download/PDF (imaging has a patient PDF — confirm lab's
  parity or intentional absence). List each divergence; close or justify it.
- **States**: every surface defines empty / loading(wait) / error / unreadable / not-signed-in.
- **Coverage gate**: a checklist (one row per inventory item) with status; **100% required** — phasing
  *coverage* is banned (warcry completeness rule). Phasing *depth* (e.g. which anatomies get deep live
  validation) is allowed and must be logged, not silently dropped.

## Phased steps (commit to main per phase — solo repo)

**Phase A — Bundle Python (kills the host-Python dependency).**
1. Add `electron/scripts/fetch-python.(ps1|js)` that downloads python-embeddable (3.10/3.11 x64) +
   `pip install -r requirements.lock` into `electron/python/` (enable `import site` in the `._pth`).
2. Freeze `backend/requirements.lock` = `pip freeze` of the working env (numpy<2 enforced); commit it.
   Add a CI/preflight check: `pip install -r requirements.lock && python -c "import numpy,scipy,fitz,pydicom; assert numpy.__version__ < '2'"`.
3. `electron/package.json` build: add `{from:"python", to:"python"}` to `extraResources`; ship
   `vcruntime140.dll` (embeddable needs VC++ runtime).
4. `electron/main.js`: `resolvePython()` prefers the bundled `process.resourcesPath/python/python.exe`
   when `app.isPackaged`, else host python (dev). Keep `MIKA_PYTHON` override.
5. Backend `GET /api/version` (returns app+backend version) + a boot `check_env()` (already partly
   exists per hardening plan) asserting the numpy/scipy ABI; `main.js` surfaces a clear dialog on failure.

**Phase B — Claude-CLI first-run gate (auth consistency across ALL features).**
6. `GET /api/cli-status` → `{installed, authenticated}` (reuse `agent_runner.check_cli_auth` / `_resolve_claude_bin`).
7. Frontend first-run gate: if `!installed` → a calm panel guiding the official installer; if
   `installed && !authenticated` → the existing "Sign in with Claude" (`/api/connect`). Block reads
   until ready, on BOTH imaging and lab.
8. CONSISTENCY assertion: verify imaging read, lab read, imaging chat, lab chat ALL go through the same
   subscription `claude -p` transport (lab read = `lab_reader`, lab chat = `lab_chat`, imaging = agent +
   `case_chat`). Unit-test the transport/auth-env parity; document any divergence.

**Phase C — electron-updater OTA.**
9. Add `electron-updater` dep; `package.json` build `publish:{provider:"github",owner:"husam-hammami",repo:"ai-mri-analyzer"}`.
10. `main.js`: after the window loads, `autoUpdater.checkForUpdatesAndNotify()`; wire `update-available`
    / `update-downloaded` to a calm in-app prompt ("Update ready — restart to apply"); `autoInstallOnAppQuit:true`.
11. **Rollback/crash-loop guard**: on boot write a `_last_boot` stamp; if N crashes within M seconds
    after an update, skip auto-update once + surface a warning (don't infinite-loop a bad release).
12. **Version-match guard**: after backend ready, compare app version vs `/api/version`; mismatch →
    "restart to finish update" (don't run a half-updated pair).
13. **Data survives updates**: `MIKA_DATA_DIR = userData/data` (main.js:103) is outside the app dir →
    NSIS update preserves it; add a regression note + a boot check that the data dir is readable.

**Phase D — Signing + publish.**
14. `package.json` `win.signtoolOptions` (or the SignPath/Azure config per the owner's chosen route).
15. `.github/workflows/release.yml`: on tag `v*` → install deps + fetch-python → `electron-builder --win --publish always` → sign → Release with `latest.yml` + blockmap. (Or local `dist:win --publish always` if no CI.)
16. Version-bump discipline: bump `electron/package.json` version on every release (a check that the tag == package version).

**Phase E — Consistency sweep + finalize.**
17. Build + run the inventory coverage gate (Phase §sweep). Per-feature smoke via `?demo=*` fixtures +
    endpoint schema checks. Fix every leak to 100%.
18. **daedalus visual pass** on the running Electron window (not screenshots of dev) for cohesion
    (brand: light #F8FAFC, single blue #2563EB, no traffic-light hues, no AI-tells) across home/imaging/
    lab/chat/recent/about, EN + AR/RTL.
19. **/sincere** copy polish on any new strings (CLI-gate, update prompt, error dialogs) — EN+AR.

**Phase F — Real-machine validation (OWNER / out-of-session; the verification trap).**
20. On a clean Windows VM (no Python, no dev tools): install the signed installer; confirm backend
    starts (bundled Python), the CLI gate guides install + sign-in, an imaging read AND a lab read +
    both chats complete, EN/AR render. Then bump version, publish a Release, confirm the installed app
    auto-updates and data persists. **None of this is doable in a Claude session.**

## Files & surfaces touched
- `electron/main.js` — bundled-python resolve, autoUpdater wiring, crash-loop + version-match guards,
  CLI-status surfacing, bilingual shell strings.
- `electron/package.json` — electron-updater dep, `publish`, `extraResources/python`, signing config,
  version-bump.
- `electron/scripts/fetch-python.*` (new), `.github/workflows/release.yml` (new).
- `backend/requirements.lock` (new), `backend/app.py` — `/api/version`, `/api/cli-status`, boot
  `check_env`.
- `frontend/index.html` — first-run CLI/sign-in gate, update-ready prompt, any new EN/AR strings.
- `backend/tests/` — env-lock check, cli-status, transport-parity, inventory/i18n coverage test.
- `docs/` — this plan; a real-machine validation checklist.

## Verification strategy (be honest about what's testable here)
- **In-session (testable):** pure-function unit tests; `requirements.lock` numpy<2 import check; the
  inventory/i18n coverage grep gate; `?demo=*` visual gate (where the Preview/devtools MCP is available);
  `python -c "import app"` + `electron-builder --dir` build smoke (build only — NOT launch).
- **NOT in-session (real-terminal/owner):** launching the packaged app, the live Claude read/chat
  (nested `claude -p` hangs), code-signing, the live OTA cycle, the clean-VM test. The plan must NOT
  claim in-session verification of these (bulletproof must reject any that does).

## Safety / realism gates (from the pre-mortem — bulletproof must confirm each)
- G1 numpy<2 enforced at pack-time (requirements.lock) + boot `check_env` + CI import check.
- G2 Signed installer is a HARD dependency of silent OTA (electron-updater verifies signature).
- G3 No in-session verification of live read/chat/OTA/signing (verification trap named per step).
- G4 Update can't brick: crash-loop rollback guard + version-match guard + data-dir persistence.
- G5 Claude CLI NOT bundled (licensing); detect-and-guide only.
- G6 Existing safety properties preserved across packaging/finalize (lab verdict gate, named-assessment
  whitelist + red-flag exclusion, chat answer-replacement gate incl. Arabic, no treatment/drug/dose,
  PII out of meta.json, clarity-floor sync; imaging anti-hallucination/calibration) — re-run the 48
  backend tests after every phase; add a "final-gate re-emission" test (INCIDENTS #4) covering PDF
  re-export.
- G7 ∀ coverage gate: 100% of the inventory; phasing coverage is banned; i18n EN+AR complete incl.
  Electron-shell strings.
- G8 Auth consistency: imaging + lab read + both chats on the same subscription `claude -p` path
  (unit-tested parity).

## Risks → mitigations
- Bundled Python fails on clean machine (VC++ / ABI) → ship vcruntime; clean-VM test (Phase F); boot
  check_env with a clear dialog.
- Auto-update never fires / bricks → version-bump check, signed builds, crash-loop + version-match guards.
- Signing route unavailable (SignPath needs public repo + prior release; Azure US/Canada-only; OV cost)
  → owner decision §below; plan ships the config, owner supplies the cert/route.
- Consistency leak (a feature works in demo not live; AR half-coverage) → coverage gate + Phase F.
- Safety regression during packaging → G6 test re-run each phase.

## Rollout & rollback
Per-phase commits to main. The signed Release is the rollout unit; rollback = publish a prior Release /
the crash-loop guard reverts a bad update. Feature stays behind existing flags where applicable.

## Out of scope (deliberate)
- macOS/Linux builds (config stubs exist; Windows-first per owner).
- Hosted backend / bundled API key / cloud sync (keep local/private).
- Sex/age-derived ranges, trends (prior lab out-of-scope).
- **Coverage of the in-scope feature population is NOT out of scope** (∀ rule) — only depth of live
  multi-anatomy validation may be phased, and logged.

## Owner external steps (cannot be done in-session — surface explicitly)
1. **Signing route decision:** SignPath Foundation (free OSS — requires making the repo **public** + a
   prior release + passing their content review for a medical tool) **vs** Azure Trusted Signing (~$10/mo,
   US/Canada entity only) **vs** OV cert (~$200-400/yr). Recommend SignPath-OSS if willing to go public,
   else OV cert. ← **needs your call.**
2. Confirm GitHub repo is the publish target (`husam-hammami/ai-mri-analyzer`) + release permissions/token.
3. Run the Phase F clean-Windows-VM validation + the first real OTA cycle.

## Success criteria (restated)
Bundled-Python signed Windows installer; OTA via GitHub Releases working on a real machine; BYO-Claude
sign-in gate; 100% feature-inventory coverage + daedalus-approved + EN/AR complete; all in-session tests
green; the 48 safety tests still pass; owner external steps documented and handed off.

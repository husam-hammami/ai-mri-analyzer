# MIKA — Electron Desktop App: Bundling Plan

**Goal:** ship MIKA as a single, signed desktop installer (Windows-first; macOS/Linux via the same toolchain) where the end user installs **one** thing, signs into **their own Claude subscription**, and runs studies at **full capability** — with **zero external prerequisites**. Everything the app shells out to (the Claude Code CLI, a Python runtime + scientific libs, the analysis skill, the frontend assets) is **bundled inside the app**.

> **Revision note (independent second-pass review):** this plan was re-reviewed against the code. Architecture and §0 hold. Additions folded in below: the agent prompt itself must be edited to stop inviting `pip install` (§3.2, §9.2); the bundled-CLI launch path is the riskiest unknown and should be prototyped first (§4, §8, §9); macOS must sign the *nested* binaries, not just the shell (§5, §9.4); and new clinical-grade workstreams — PHI/telemetry, redistribution licensing, single-instance lock, long-job lifecycle (§9.11–14). Some §3 edits are pulled forward to P0/P1 (§8).

---

## 0. Why this is non-trivial (the load-bearing constraint)

MIKA's agent mode is not a self-contained Python program. At runtime it **shells out to two external binaries**:

1. **`claude` (Claude Code CLI)** — `AgentRunner` runs `claude auth login`, `claude auth status`, and `claude -p …` (`agent_runner.py`). This is a **Node.js** program.
2. **`python`** — the agent, *inside* its `claude -p` run, executes its own Python for slice-by-slice DICOM analysis and figure rendering (the skill/prompt assumes `pydicom, numpy, scipy, Pillow` importable and may `pip install matplotlib reportlab`). That child `python` resolves from **PATH**, *not* from a frozen backend.

**Consequence:** a PyInstaller "freeze" of the backend is **not sufficient** — the frozen exe is not a general `python`, so the agent's own `python foo.py` calls would fail. We must bundle a **real, relocatable CPython** with all libs and put it on the spawned process's PATH so it serves **both** the FastAPI backend **and** the agent's child Python.

Also: using a **Claude subscription** (not an API key) is **only** possible through the Claude Code CLI — so the CLI must be bundled; there is no pure-Python path to subscription-billed inference.

---

## 1. Architecture

```
┌─────────────────────────── Electron app ───────────────────────────┐
│  Main process (Node)                                                │
│   • picks a free localhost port                                     │
│   • spawns the Python sidecar:  <bundled py> -m uvicorn app:app     │
│       env: MIKA_DATA_DIR, MIKA_CLAUDE_BIN, PATH(+node,+python), …   │
│   • waits for http://127.0.0.1:<port>/ to answer                    │
│   • opens BrowserWindow → loadURL(http://127.0.0.1:<port>/)         │
│   • on quit: tree-kill the sidecar (+ any in-flight `claude` child) │
│                                                                     │
│  Renderer (Chromium)  ── just loads the existing MIKA frontend      │
│     window.location.origin == the localhost backend → /api works    │
│                                                                     │
│  Python sidecar (bundled CPython)                                   │
│     FastAPI (app.py) — serves index.html + /api + /assets           │
│     agent mode → spawns bundled `claude -p` (subscription)          │
│        which spawns bundled `python` for its analysis               │
└─────────────────────────────────────────────────────────────────────┘
```

The renderer is *just a window pointed at the local backend* — no rewrite of the frontend's networking; `API_BASE = window.location.origin` already resolves to the localhost sidecar.

---

## 2. What gets bundled (resource inventory)

Shipped under the app's `resources/` (via electron-builder `extraResources`):

| Resource | What | How produced | Approx size |
|---|---|---|---|
| `resources/python/` | Relocatable CPython 3.11 **+ venv/site-packages** with all of `requirements.txt` **+ matplotlib** | `python-build-standalone` (the distro `uv` uses) + `pip install -r requirements.txt matplotlib` into it | ~280–360 MB |
| `resources/node/` | Node.js runtime (pinned, e.g. v22) — needed to run the CLI | download official node binary for target OS | ~60–90 MB |
| `resources/claude-cli/` | `@anthropic-ai/claude-code` **pinned to a known-good version** (currently 2.1.170) installed locally | `npm i --prefix resources/claude-cli @anthropic-ai/claude-code@<pin>` | ~80–120 MB |
| `resources/backend/` | `backend/` (app.py, core/, services/, prompts/, **skills/**) | copy from repo | small |
| `resources/frontend/` | `frontend/` incl. `assets/` and **vendored** React/Babel/fonts (see §6) | copy + vendor step | ~3–5 MB |

> **Pre-install matplotlib + reportlab** into the bundled Python so the agent never needs to `pip install` (offline/no-pip safe). `reportlab` is already in `requirements.txt`; **add `matplotlib`**.

Total installer ≈ **500–700 MB** (compressed). This is expected for an Electron + scientific-Python + Node-CLI bundle; document it.

---

## 3. Repo changes required (small, additive — does not break the web/dev flow)

1. **`requirements.txt`** — add `matplotlib` (agent figure rendering; avoids runtime pip).
2. **`backend/services/agent_runner.py`** — **edit the agent prompt string.** It currently *instructs the child* to "pip install matplotlib and reportlab if you need them" (`agent_runner.py:385`; same pattern in `run_annotations.py:28`). In a bundled, possibly-offline, read-only `site-packages`, a child `pip install` will hang or error. Change the prompt to state these libs are **already installed — do NOT pip install**. Pre-provisioning the wheels (item 1) is necessary but **not sufficient**; the prompt is what actually drives the behavior.
3. **`frontend/index.html`** — vendor the 3 CDN scripts + DM Sans locally so the shell loads without internet and isn't hostage to cdnjs (see §6). Keep CDN as a fallback for the plain web/dev run, or switch to local paths unconditionally.
4. **`backend/app.py`** — already honors `MIKA_DATA_DIR`; no change needed for port (Electron passes `--port`). Add a tiny **`GET /api/preflight`** that reports `{claude_cli_found, claude_logged_in, python_libs_ok, skill_present}` so the shell can show a clear "what's missing" panel instead of a mid-run 400 (reuse `AgentRunner.availability()` + a libs import check + `SKILL_PATH.exists()`). Give it a **fast path** that does not spawn the CLI on every call — `availability()` shells out to `claude --version` + `claude auth status` with 20 s timeouts each (`agent_runner.py:218,226`), which is too heavy for a startup readiness poll.
5. **New `electron/` project** — `electron/main.js`, `electron/preload.js`, `electron/package.json` with the electron-builder config (§5).
6. **New `scripts/`** — `assemble_python.(ps1|sh)`, `fetch_node.(ps1|sh)`, `install_cli.(ps1|sh)`, `vendor_frontend.(ps1|sh)` to build `resources/` reproducibly + a `build.(ps1|sh)` that runs them then `electron-builder`.

All MIKA env knobs the sidecar honors (already in code): `MIKA_DATA_DIR`, `MIKA_CLAUDE_BIN`, `MIKA_AGENT_MODEL`, `MIKA_AGENT_EFFORT`, `MIKA_AGENT_TIMEOUT_S`, `MIKA_AGENT_PERMISSION_MODE`, `MIKA_PUBLIC_URL`, `MIKA_SMTP_*`.

---

## 4. Electron main process (sketch)

```js
// electron/main.js
const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const net = require('net'); const path = require('path'); const http = require('http');

const RES = process.resourcesPath;                       // <app>/resources at runtime
const isWin = process.platform === 'win32';
const PYDIR = path.join(RES, 'python');
const PY    = path.join(PYDIR, isWin ? 'python.exe' : 'bin/python3');
const NODE  = path.join(RES, 'node', isWin ? '' : 'bin');
const CLAUDE = path.join(RES, 'claude-cli', 'node_modules', '.bin', isWin ? 'claude.cmd' : 'claude');
const BACKEND = path.join(RES, 'backend');

function freePort(){ return new Promise(r=>{ const s=net.createServer(); s.listen(0,'127.0.0.1',()=>{const p=s.address().port; s.close(()=>r(p));}); }); }
function waitReady(url, ms=60000){ /* poll GET url until 200 or timeout */ }

let py, win;
async function start(){
  const port = await freePort();
  const env = { ...process.env,
    MIKA_DATA_DIR: path.join(app.getPath('userData'), 'data'),   // writable
    MIKA_CLAUDE_BIN: CLAUDE,
    // put bundled node + python on PATH for the agent's child `python`/`node`
    PATH: [NODE, PYDIR, process.env.PATH].filter(Boolean).join(path.delimiter),
  };
  py = spawn(PY, ['-m','uvicorn','app:app','--host','127.0.0.1','--port',String(port)],
             { cwd: BACKEND, env });
  await waitReady(`http://127.0.0.1:${port}/`);
  win = new BrowserWindow({ width:1440, height:900, show:false,
    webPreferences:{ contextIsolation:true, nodeIntegration:false, sandbox:true } });
  win.once('ready-to-show', ()=>win.show());
  // open external links (e.g. any http link) in the system browser, never in-app
  win.webContents.setWindowOpenHandler(({url})=>{ shell.openExternal(url); return {action:'deny'}; });
  win.loadURL(`http://127.0.0.1:${port}/`);
}
app.whenReady().then(start);
app.on('window-all-closed', ()=>{ killTree(py); app.quit(); });  // killTree: taskkill /T /F on Windows
```

`preload.js` can stay minimal (the renderer is the existing web app and talks to the backend over HTTP, not via IPC).

**Launch & startup caveats (second-pass review):**
- **The bundled-CLI launch path is the riskiest unknown.** Pointing `MIKA_CLAUDE_BIN` at `resources/claude-cli/node_modules/.bin/claude(.cmd)` (line 89) relies on the npm shim + `node` on PATH, and electron-builder's `extraResources` copy may not preserve the POSIX `.bin` symlink. On Windows, `subprocess` launching a `.cmd` **without `shell=True`** (`agent_runner.py:120,520`) can fail outright. **Safer:** set `MIKA_CLAUDE_BIN` to (or have the runner invoke) the bundled **`node` against the CLI's real entry JS** (the `@anthropic-ai/claude-code` `bin` target / `cli.js`), bypassing the shim. **Prototype this first** within P2 (§8).
- **`waitReady` should poll a cheap endpoint, not `/`.** `/` reads and returns the full `index.html` on every call, and first cold start imports numpy/scipy/pydicom/nibabel (can exceed a naive 60 s on slow disks / AV scanning). Poll the fast `/api/preflight` (§3.4) instead.
- **The random port is safe against the hardcoded CORS allow-list.** The allow-list pins `:8000`/`:5173` (`app.py:149-152`), but the CSRF guard explicitly permits same-origin on *any* localhost port (`app.py:167-174`) and the browser bypasses CORS for same-origin — so a random sidecar port works without touching the allow-list. (Noted so a future reader doesn't "fix" the allow-list and break nothing.)

**OAuth note:** `Connect with Claude` → `POST /api/connect` → `trigger_claude_login` spawns `claude auth login --claudeai`, which opens the user's **default browser** and completes its own localhost OAuth callback, persisting the token to `%USERPROFILE%\.claude`. Electron doesn't need to handle the OAuth window; it just must **not** override `HOME`/`USERPROFILE`, so the login persists across launches. (Optionally, intercept the login URL and `shell.openExternal` it for reliability.)

---

## 5. electron-builder config (sketch)

```jsonc
// electron/package.json (excerpt)
{
  "build": {
    "appId": "ai.mika.desktop",
    "productName": "MIKA",
    "files": ["main.js", "preload.js"],
    "extraResources": [
      { "from": "../dist-res/python",     "to": "python" },
      { "from": "../dist-res/node",       "to": "node" },
      { "from": "../dist-res/claude-cli", "to": "claude-cli" },
      { "from": "../backend",             "to": "backend",  "filter": ["**/*","!**/__pycache__/**","!**/*.pyc"] },
      { "from": "../frontend",            "to": "frontend" }
    ],
    "win": { "target": ["nsis"], "icon": "build/icon.ico", "signtoolOptions": { /* cert */ } },
    "mac": { "target": ["dmg"], "hardenedRuntime": true, "icon": "build/icon.icns" },
    "nsis": { "oneClick": false, "perMachine": false, "allowToChangeInstallationDirectory": true }
  }
}
```

> **Do NOT ship `MIKA_DATA_DIR` inside `resources/`** (read-only under Program Files) — it's set to `app.getPath('userData')/data` in main.js.
>
> **macOS:** signing the Electron shell alone is **not** enough — the *nested* bundled binaries (CPython, Node, the `claude` CLI) must each be signed, or hardened runtime (`hardenedRuntime: true` above) will refuse to launch them; otherwise add `com.apple.security.cs.disable-library-validation` / `allow-unsigned-executable-memory` entitlements. See §9.4.

---

## 6. Offline frontend assets (vendor the CDN)

The current `index.html` pulls React, ReactDOM, Babel-standalone from cdnjs and DM Sans from Google Fonts. For a desktop app these must be **local** (resilient + faster):

- Download `react.production.min.js`, `react-dom.production.min.js`, `babel.min.js` → `frontend/assets/vendor/`; self-host DM Sans `.woff2` + a local `@font-face`.
- Point the `<script>`/`<link>` tags at `/assets/vendor/...` (served by the existing `/assets` StaticFiles mount).
- **Optimization (recommended for the packaged build):** precompile the single `<script type="text/babel">` to plain JS at build time (one `app.js`, no in-browser Babel) for instant startup. This is a *release build step* for the EXE only — the dev/web source of truth stays the single-file `index.html` (so it doesn't reintroduce the source-ambiguity the web plan warned about; the precompiled file is a generated, git-ignored artifact).

---

## 7. Build pipeline (one command)

```
scripts/build:
  1. assemble_python   → dist-res/python  (relocatable CPython + pip install -r requirements.txt matplotlib)
  2. fetch_node        → dist-res/node    (pinned Node for target OS/arch)
  3. install_cli       → dist-res/claude-cli (npm i @anthropic-ai/claude-code@<pin>)
  4. vendor_frontend   → frontend/assets/vendor (+ optional precompile app.js)
  5. preflight smoke   → run dist-res/python -c "import pydicom,numpy,scipy,PIL,matplotlib,reportlab,nibabel,nrrd"
                         run dist-res/claude-cli claude --version
  6. electron-builder  → signed installer (NSIS / dmg)
```

---

## 8. Phased delivery

- **P0 — Sidecar bring-up (dev).** Electron main spawns the *system* python/claude (no bundling yet); window loads localhost; Connect + a real spine run works end-to-end inside the Electron window. *Gate: full upload→wait→read in-app.*
- **P1 — Bundle Python.** Replace system python with `dist-res/python`; verify the **agent's own** `python` resolves to it (run a non-trivial study; confirm figures render, no `pip install` needed). *Gate: works with system Python removed from PATH.*
- **P2 — Bundle Node + CLI.** Ship `dist-res/node` + `dist-res/claude-cli`; set `MIKA_CLAUDE_BIN`; verify `claude auth login` + `claude -p` run from the bundled CLI. *Gate: works on a machine with no global `claude`/node.*
- **P3 — Offline frontend + preflight.** Vendor React/Babel/fonts; add `/api/preflight` + a startup panel that blocks with a clear message if anything's missing. *Gate: app launches offline (shell), preflight green.*
- **P4 — Installer + signing.** electron-builder NSIS, code-sign (Windows cert), app icon = `mika-mark`, writable `userData/data`. *Gate: clean install on a fresh Windows VM with nothing pre-installed → Connect → run.*
- **P5 — Polish.** Auto-update (electron-updater), crash logging, "leave & we'll notify" desktop notification wired to Electron's native notifications, uninstall cleanup of `userData/data`.

**Scheduling corrections (second-pass):** the §3 repo edits are mis-scheduled if left to P3. **Pull `matplotlib` (§3.1), the prompt edit (§3.2), and a minimal `/api/preflight` (§3.4) forward into P0/P1** — the P1 gate ("figures render, no `pip install` needed") cannot be validated without them. And **prototype the bundled-CLI launch path early within P2** (the `node`-vs-shim question in §4 / §9.7 is the most probable failure) before investing in installer/signing work.

---

## 9. Risks & gotchas

1. **PyInstaller is not enough** — must bundle a real relocatable Python for the agent's child `python` (see §0). *Mitigation: python-build-standalone + PATH.*
2. **Agent `pip install` offline** — pre-install matplotlib/reportlab **and edit the prompt** (§3.2). The agent's prompt currently *tells* it to "pip install … if you need them" (`agent_runner.py:385`), so pre-provisioning alone is **not** a guarantee — a read-only/offline `site-packages` plus a prompt that invites pip can still hang/error. Remove the pip instruction and assert the libs are present.
3. **CLI version drift / contract coupling** — the runner is tightly coupled to the CLI's surface: `availability()` parses `claude auth status` JSON for `loggedIn`/`subscriptionType` (`agent_runner.py:230-233`), runs use `-p --output-format json --effort high --permission-mode bypassPermissions` (`agent_runner.py:504-508`), and login uses the `auth login`/`auth status` subcommand forms (`agent_runner.py:102,117,223`). `--effort` and `bypassPermissions` are volatile CLI surface, and a move to a slash-command `/login` form would break `trigger_claude_login`. **Pin** the version **and add a CLI-contract smoke test** to the build (assert the `auth status` JSON shape + that `--effort`/`--output-format json` parse + that `auth login` still exists) so a silent bump on rebuild fails the build, not a user's run.
4. **Code signing / SmartScreen / notarization** — an unsigned EXE triggers Windows SmartScreen + AV false-positives. An **EV** cert clears SmartScreen immediately; an **OV** cert still has to accrue reputation over time. On macOS, hardened-runtime notarization is **not enough on its own**: the *nested* bundled binaries (CPython, Node, `claude` CLI) must **each be signed**, or they fail to launch under hardened runtime (or need the `disable-library-validation` / `allow-unsigned-executable-memory` entitlements). Budget signing for every shipped binary, not just the shell.
5. **Write paths** — never write under `resources/` (read-only); `MIKA_DATA_DIR` → `userData`. TTL/cleanup of `userData/data` (ties to plan §7.6).
6. **Process tree kill** — quitting must kill the sidecar **and** any in-flight `claude` grandchild (`taskkill /T /F` on Windows); warn if a run is in progress.
7. **Windows path/space/quoting** — handled: `agent_runner` already passes the (large) prompt via **stdin** to dodge the cmd.exe 8191-char argv cap.
8. **Subscription vs API** — subscription requires the bundled CLI; API-key/lite mode is the only no-CLI path (separate billing). Keep agent mode as the default desktop path.
9. **Size** — ~500–700 MB installer; set expectations. Trim by excluding `scipy` test data, `__pycache__`, unused locales if needed.
10. **First-run OAuth** — requires the user to complete one browser sign-in; token persists in `~/.claude`. Preflight should detect not-logged-in and route to Connect.
11. **PHI / HIPAA / telemetry (clinical-tool blocker, not polish)** — the bundled Claude Code CLI writes session transcripts/logs under `~/.claude` that contain the **prompts**, and those prompts include patient clinical history + prior reports (`agent_runner.py:341-345`). The CLI also has its own usage/telemetry. For a medical product this is a PHI/BAA concern not previously raised: audit what PHI lands in `~/.claude` and in `MIKA_DATA_DIR`, document/disable CLI telemetry where possible, and **implement the (currently aspirational) TTL/cleanup of `MIKA_DATA_DIR`** — there is no retention limit in code today. Treat as its own workstream, not P5 polish.
12. **Redistribution licensing** — bundling and shipping the `@anthropic-ai/claude-code` CLI, a CPython distribution, and Node inside an installer has license/ToS implications (notably the CLI's terms around redistribution + subscription auth). Confirm redistribution rights for each bundled component before shipping a (commercial) build.
13. **Single-instance lock** — the per-launch free-port design avoids port collisions, but without `app.requestSingleInstanceLock()` two app instances spawn two sidecars writing the same `MIKA_DATA_DIR` and racing on `~/.claude`. Enforce single-instance.
14. **Long-job lifecycle** — agent timeout defaults to 3600 s (`agent_runner.py:33`). The §4 sketch's `window-all-closed → killTree(py) → quit` kills a 60-min in-progress study with no confirmation, and the in-memory `JOBS` cache is lost (only completed studies persist to disk). §9.6's "warn if a run is in progress" must actually be **implemented** — block-quit / minimize-to-tray while a job runs — since there is no mid-run resume.

---

## 10. Testing

- **Clean-VM test (the real gate):** fresh Windows with **no** Python, **no** Node, **no** `claude` → install MIKA → Connect (browser OAuth) → run a real spine DICOM study → verify Read renders with proof figures. Repeat for a brain/MSK study (generalization) and offline-shell launch.
- **Regression:** the web/dev flow (`uvicorn app:app` from `backend/`) still works unchanged (the repo changes are additive).
- **Spine pipeline untouched** — same gate as the redesign plan.
```

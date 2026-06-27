// MIKA desktop shell.
// Spawns the Python backend (uvicorn app:app) as a sidecar on a free localhost port, waits for it
// to answer, then opens a window pointed at it. The renderer is the existing MIKA web app talking
// to the backend over HTTP. Works both in dev (repo checkout) and packaged (electron-builder), and
// surfaces a clear dialog if its host prerequisites (Python, the Claude CLI) are missing.
//
// Host prerequisites (by design — MIKA runs on the user's own Claude subscription):
//   • Packaged: a bundled python-embeddable (built by electron/scripts/fetch-python.ps1) — no host
//     Python needed. Dev: Python 3.10+ with the repo-root requirements.txt installed.
//   • The Claude CLI signed in (`claude /login`) — the analysis + lab + chat reads run on it.
const { app, BrowserWindow, shell, dialog } = require('electron');
const { spawn, execFileSync } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');
const http = require('http');

const isWin = process.platform === 'win32';

// Backend lives next to this file in dev (../backend), and under resources/ when packaged.
const BACKEND = app.isPackaged
  ? path.join(process.resourcesPath, 'backend')
  : path.join(__dirname, '..', 'backend');

let py = null;
let win = null;
let starting = true;
let failed = false;            // guard against stacking two error dialogs
let intentionalQuit = false;   // user-/update-initiated quit — not a boot failure

function resolvePython() {
  // Packaged: prefer the bundled python-embeddable shipped at resources/python (built by
  // electron/scripts/fetch-python.ps1) so a clean machine needs no host Python. Explicit override
  // still wins; dev falls back to the host interpreter.
  const bundled = app.isPackaged ? path.join(process.resourcesPath, 'python', 'python.exe') : null;
  const candidates = [process.env.MIKA_PYTHON, bundled, isWin ? 'python' : 'python3', 'python'].filter(Boolean);
  for (const cand of candidates) {
    try {
      execFileSync(cand, ['--version'], { stdio: 'ignore' });
      return cand;
    } catch (_) { /* try next */ }
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

function waitReady(url, timeoutMs = 90000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (py && py.exitCode !== null) {
        reject(new Error('the backend process exited before it became ready'));
        return;
      }
      const req = http.get(url, (res) => { res.resume(); resolve(); });
      req.on('error', () => {
        if (Date.now() - start > timeoutMs) reject(new Error('backend did not become ready in time'));
        else setTimeout(tick, 400);
      });
    };
    tick();
  });
}

// Synchronous teardown — used before an OTA install so the bundled python.exe + its loaded DLLs
// (inside resources/python, the exact tree NSIS overwrites) release their handles BEFORE the
// installer runs. taskkill /F TerminateProcess has freed the handles by the time execFileSync returns.
function killTreeSync(proc) {
  if (!proc || proc.killed) return;
  try {
    if (isWin) execFileSync('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { stdio: 'ignore' });
    else proc.kill('SIGTERM');
  } catch (_) { /* best effort */ }
}

function fail(title, message) {
  if (failed) return;          // never stack two error dialogs
  failed = true;
  starting = false;
  if (!intentionalQuit) recordBootFailure(app.getVersion());   // count toward the crash-loop guard
  try { dialog.showErrorBox(title, message); } catch (e) { console.error(title, message); }
  killTreeSync(py);
  app.quit();
}

// ---- Silent OTA self-update from GitHub Releases (packaged only) + boot guards ----------------
// Ported + simplified from the Hercules reporting module. On launch (before the backend starts) we
// check the repo's Releases; if a newer build exists we download its OTA zip, VERIFY its SHA-256
// against the release's manifest.json, and swap the new backend/ (+ frontend/) into place — no
// prompt, the user just opens the app on the latest version. The bundled Python interpreter and the
// Electron shell are NOT updated this way; a release that needs either sets "requires_reinstall" in
// the manifest and we show a one-time "download the installer" notice instead.
const crypto = require('crypto');
const https = require('https');
const GITHUB_REPO = 'husam-hammami/ai-mri-analyzer';
const RELEASES_API = `https://api.github.com/repos/${GITHUB_REPO}/releases?per_page=20`;
const RELEASES_PAGE = `https://github.com/${GITHUB_REPO}/releases`;
const RESOURCES_DIR = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
const FRONTEND_DIR = path.join(RESOURCES_DIR, 'frontend');
const VERSION_FILE = path.join(RESOURCES_DIR, 'version.txt');

let otaApplied = false;     // an update was staged this launch (commit on /health-ok, roll back on fail)
let otaPriorVer = null;     // version before the staged update, for rollback
let badHandled = false;     // one-shot guard so rollback+relaunch runs once
let reinstallNotice = null; // set to a version string if a release needs a full reinstall

const stampPath = () => path.join(app.getPath('userData'), 'boot-state.json');
function readStamp() {
  try { return JSON.parse(fs.readFileSync(stampPath(), 'utf8')); } catch (_) { return { fails: 0, lastGood: null }; }
}
function writeStamp(s) {
  try { fs.mkdirSync(path.dirname(stampPath()), { recursive: true }); fs.writeFileSync(stampPath(), JSON.stringify(s)); } catch (_) { /* best effort */ }
}
function getJson(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      let body = '';
      res.on('data', (d) => { body += d; });
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch (_) { resolve(null); } });
    });
    req.on('error', () => resolve(null));
  });
}

// Secondary net under the auto-rollback: count consecutive boot FAILURES of a specific version (only
// real failures); a non-good version that fails twice points the user at the Releases page.
function recordBootFailure(version) {
  const s = readStamp();
  const fails = (s.failVersion === version ? (s.fails || 0) : 0) + 1;
  writeStamp({ fails, failVersion: version, lastGood: s.lastGood });
}
function crashLoopGuard(version) {
  const s = readStamp();
  if (s.fails >= 2 && s.failVersion === version && s.lastGood && s.lastGood !== version) {
    const r = dialog.showMessageBoxSync({
      type: 'error', title: 'MIKA update problem',
      message: `This version (v${version}) isn't starting correctly.`,
      detail: `The last working version was v${s.lastGood}. You can download and reinstall it from the Releases page.`,
      buttons: ['Open Releases page', 'Try again'], defaultId: 0, cancelId: 1,
    });
    writeStamp({ fails: 0, failVersion: null, lastGood: s.lastGood });
    if (r === 0) { shell.openExternal(RELEASES_PAGE); intentionalQuit = true; app.quit(); return false; }
  }
  return true;   // increments happen only on a real boot failure, not here
}

// --- version helpers ---
function parseVersion(v) { const m = String(v).match(/(\d+)\.(\d+)\.(\d+)/); return m ? [+m[1], +m[2], +m[3]] : [0, 0, 0]; }
function isNewer(remote, local) {
  const r = parseVersion(remote), l = parseVersion(local);
  for (let i = 0; i < 3; i++) { if (r[i] > l[i]) return true; if (r[i] < l[i]) return false; }
  return false;
}
function getLocalVersion() {
  try { if (fs.existsSync(VERSION_FILE)) return fs.readFileSync(VERSION_FILE, 'utf8').trim(); } catch (_) { /* fall through */ }
  return app.getVersion();
}

// --- https helpers (timeout + redirect-follow + stall detection; ported from Hercules) ---
function httpsGetJSON(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': 'MIKA-Desktop', 'Accept': 'application/vnd.github+json' }, timeout: 15000 }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch (_) { reject(new Error('invalid JSON')); } });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
  });
}
function downloadFile(url, dest) {
  const IDLE_MS = 30000;
  return new Promise((resolve, reject) => {
    let redirects = 0, settled = false;
    const settle = (e) => { if (settled) return; settled = true; e ? reject(e) : resolve(); };
    const follow = (u) => {
      const req = https.get(u, { headers: { 'User-Agent': 'MIKA-Desktop' }, timeout: 120000 }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          if (++redirects > 5) return settle(new Error('too many redirects'));
          res.resume(); return follow(res.headers.location);
        }
        if (res.statusCode !== 200) return settle(new Error(`HTTP ${res.statusCode}`));
        const total = parseInt(res.headers['content-length'] || '0', 10);
        let got = 0; const file = fs.createWriteStream(dest); let idle;
        const reset = () => { clearTimeout(idle); idle = setTimeout(() => res.destroy(new Error('stalled')), IDLE_MS); };
        const done = (e) => { clearTimeout(idle); file.end(() => {
          if (e) { try { fs.unlinkSync(dest); } catch (_) {} return settle(e); }
          if (total > 0 && got < total) { try { fs.unlinkSync(dest); } catch (_) {} return settle(new Error('incomplete')); }
          settle();
        }); };
        res.on('data', (c) => { got += c.length; file.write(c); reset(); });
        res.on('end', () => done()); res.on('aborted', () => done(new Error('aborted')));
        res.on('error', done); file.on('error', done); reset();
      });
      req.on('error', settle);
      req.on('timeout', function () { this.destroy(); settle(new Error('connect timeout')); });
    };
    follow(url);
  });
}
function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256'); const s = fs.createReadStream(file);
    s.on('data', (d) => h.update(d)); s.on('end', () => resolve(h.digest('hex'))); s.on('error', reject);
  });
}
function extractZip(zip, destDir) {
  execFileSync('powershell', ['-NoProfile', '-Command', `Expand-Archive -Path '${zip}' -DestinationPath '${destDir}' -Force`], { stdio: 'pipe', timeout: 120000 });
}
// dir swap with a .bak backup, kept until commit/rollback after the /health check.
function swapDir(target, incoming) {
  const bak = target + '.bak';
  try { fs.rmSync(bak, { recursive: true, force: true }); } catch (_) {}
  if (fs.existsSync(target)) fs.renameSync(target, bak);
  fs.renameSync(incoming, target);
}
function commitDir(target) { try { fs.rmSync(target + '.bak', { recursive: true, force: true }); } catch (_) {} }
function rollbackDir(target) {
  const bak = target + '.bak';
  if (!fs.existsSync(bak)) return;
  try { fs.rmSync(target, { recursive: true, force: true }); } catch (_) {}
  try { fs.renameSync(bak, target); } catch (_) { /* critical, but best effort */ }
}

// Startup update check + apply. Returns true if a newer, VERIFIED build was staged (so start() commits
// it on /health-ok or rolls it back on failure). Never throws — any problem leaves the current build.
async function checkAndApplyUpdate() {
  if (!app.isPackaged) return false;                          // dev: nothing to pull
  if (process.env.PORTABLE_EXECUTABLE_DIR) return false;      // portable can't self-update in place
  const localVer = getLocalVersion();
  let releases;
  try { releases = await httpsGetJSON(RELEASES_API); } catch (e) { console.warn('[ota] releases unreachable:', e.message); return false; }
  if (!Array.isArray(releases)) return false;
  let best = null, bestVer = localVer;
  for (const rel of releases) {
    if (rel.draft || rel.prerelease) continue;
    const m = String(rel.tag_name || '').match(/(\d+\.\d+\.\d+)/);
    if (m && isNewer(m[1], bestVer)) { bestVer = m[1]; best = rel; }
  }
  if (!best) { console.log(`[ota] up to date (v${localVer})`); return false; }
  const assets = best.assets || [];
  const manifestAsset = assets.find((a) => a.name === 'manifest.json');
  const zipAsset = assets.find((a) => a.name.endsWith('.zip'));
  if (!manifestAsset || !zipAsset) { console.warn('[ota] release missing manifest.json or .zip'); return false; }
  let manifest;
  try { manifest = await httpsGetJSON(manifestAsset.browser_download_url); } catch (e) { console.warn('[ota] manifest fetch failed:', e.message); return false; }
  if (!manifest || !manifest.sha256) { console.warn('[ota] manifest has no sha256 — refusing unverified update'); return false; }
  if (manifest.requires_reinstall) { reinstallNotice = bestVer; console.log(`[ota] v${bestVer} needs a full reinstall`); return false; }
  const tmpZip = path.join(app.getPath('temp'), `mika-ota-${bestVer}.zip`);
  try { await downloadFile(zipAsset.browser_download_url, tmpZip); } catch (e) { console.warn('[ota] download failed:', e.message); return false; }
  // INTEGRITY GATE — never extract/execute an unverified artifact (this replaces code-signing).
  const digest = await sha256File(tmpZip).catch(() => null);
  if (!digest || digest !== String(manifest.sha256).toLowerCase()) {
    console.error('[ota] sha256 mismatch — discarding update', digest, manifest.sha256);
    try { fs.unlinkSync(tmpZip); } catch (_) {}
    return false;
  }
  const stage = path.join(app.getPath('temp'), `mika-ota-${bestVer}`);
  try { fs.rmSync(stage, { recursive: true, force: true }); } catch (_) {}
  try { extractZip(tmpZip, stage); } catch (e) { console.error('[ota] extract failed:', e.message); return false; }
  const newBackend = path.join(stage, 'backend');
  if (!fs.existsSync(path.join(newBackend, 'app.py'))) { console.error('[ota] staged zip has no backend/app.py'); try { fs.rmSync(stage, { recursive: true, force: true }); } catch (_) {} return false; }
  const newFrontend = path.join(stage, 'frontend');
  try {
    otaPriorVer = localVer;
    swapDir(BACKEND, newBackend);
    if (fs.existsSync(newFrontend)) swapDir(FRONTEND_DIR, newFrontend);
    fs.writeFileSync(VERSION_FILE, bestVer, 'utf8');
    try { fs.unlinkSync(tmpZip); } catch (_) {}
    try { fs.rmSync(stage, { recursive: true, force: true }); } catch (_) {}
    console.log(`[ota] staged v${localVer} → v${bestVer} (pending /health confirm)`);
    return true;
  } catch (e) {
    console.error('[ota] swap failed, rolling back:', e.message);
    rollbackDir(BACKEND); rollbackDir(FRONTEND_DIR);
    return false;
  }
}

// A staged update that fails to boot/health: invisibly restore the previous build and relaunch.
function handleBadUpdate(reason) {
  if (badHandled) return; badHandled = true;
  console.error(`[ota] update failed (${reason}) — rolling back to v${otaPriorVer}`);
  intentionalQuit = true;
  killTreeSync(py);
  rollbackDir(BACKEND); rollbackDir(FRONTEND_DIR);
  try { if (otaPriorVer) fs.writeFileSync(VERSION_FILE, otaPriorVer, 'utf8'); } catch (_) {}
  app.relaunch(); app.exit(0);
}

async function start() {
  // Silently pull + verify a newer release BEFORE starting the backend (no prompt; the swap is
  // committed after the /health check below, or rolled back invisibly if it fails to boot).
  otaApplied = await checkAndApplyUpdate();
  const version = getLocalVersion();
  if (!crashLoopGuard(version)) return;            // repeated-failure bail-out

  if (!fs.existsSync(path.join(BACKEND, 'app.py'))) {
    return fail('MIKA could not find its backend',
      `Expected the backend at:\n${BACKEND}\n\nThe install may be incomplete — reinstall MIKA.`);
  }

  const PY = resolvePython();
  if (!PY) {
    return app.isPackaged
      ? fail('MIKA could not start its runtime',
          'The bundled Python runtime didn’t launch. The install may be incomplete, or antivirus may ' +
          'have quarantined a file.\n\nReinstall MIKA, and allow it through antivirus if prompted.')
      : fail('Python is required',
          'MIKA needs Python 3.10+ on this computer to run its analysis backend.\n\n' +
          'Install Python from python.org, then reopen MIKA. ' +
          '(Advanced: set the MIKA_PYTHON environment variable to a specific interpreter.)');
  }

  const port = await freePort();
  const tail = [];                                    // keep recent sidecar stderr for diagnostics
  const env = {
    ...process.env,
    MIKA_DATA_DIR: path.join(app.getPath('userData'), 'data'),   // writable per-user dir
    PYTHONUNBUFFERED: '1',
  };
  // --app-dir puts BACKEND on sys.path explicitly, so `import app` resolves regardless of cwd or the
  // python-embeddable's isolated ._pth mode (don't depend on uvicorn's implicit cwd insert).
  py = spawn(PY, ['-m', 'uvicorn', 'app:app', '--app-dir', BACKEND, '--host', '127.0.0.1', '--port', String(port)],
             { cwd: BACKEND, env });
  py.stdout.on('data', (d) => process.stdout.write(`[sidecar] ${d}`));
  py.stderr.on('data', (d) => { const s = `${d}`; tail.push(s); if (tail.length > 40) tail.shift(); process.stderr.write(`[sidecar] ${s}`); });
  py.on('exit', (code) => {
    console.log(`[sidecar] exited with code ${code}`);
    if (!starting) return;
    if (otaApplied) return handleBadUpdate(`backend exited (code ${code})`);   // bad update → silent rollback
    const hint = app.isPackaged
      ? 'This usually means the install is incomplete — reinstall MIKA.'
      : `Most often a missing dependency — from the repo root run:\n  ${PY} -m pip install -r requirements.txt`;
    fail('MIKA backend failed to start',
      `The Python backend exited (code ${code}). ${hint}\n\nRecent output:\n` + tail.join('').slice(-1200));
  });

  const url = `http://127.0.0.1:${port}/`;
  try {
    await waitReady(url);
  } catch (e) {
    if (otaApplied) return handleBadUpdate(e.message);   // staged update never came up → roll back
    throw e;                                             // non-update failure → outer fail()
  }
  starting = false;
  console.log(`[mika] backend ready on ${url}`);

  // Confirm the read environment is actually healthy (200), not just that the HTTP root answered.
  const health = await getJson(`http://127.0.0.1:${port}/health`);
  const healthy = !!(health && health.status === 'ok');
  if (otaApplied && !healthy) return handleBadUpdate('backend /health degraded');   // bad update → roll back
  if (healthy) {
    writeStamp({ fails: 0, failVersion: null, lastGood: version });
    if (otaApplied) { commitDir(BACKEND); commitDir(FRONTEND_DIR); console.log(`[ota] committed v${version}`); }
  } else if (!intentionalQuit) {
    recordBootFailure(version);
    console.warn('[mika] backend /health degraded:', health && (health.mismatches || health.import_error));
  }

  win = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    title: 'MIKA',
    icon: path.join(__dirname, 'build', 'icon.ico'),   // real logo raster (build/make_icon.py)
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  win.once('ready-to-show', () => win.show());
  // any external (http) link opens in the system browser, never in-app
  win.webContents.setWindowOpenHandler(({ url: u }) => { shell.openExternal(u); return { action: 'deny' }; });
  await win.loadURL(url);
  console.log('[mika] window loaded');

  // A release needing a new interpreter/shell can't be applied in place — nudge once, non-blocking.
  if (reinstallNotice) {
    dialog.showMessageBox(win, {
      type: 'info', title: 'Update available',
      message: `MIKA v${reinstallNotice} is available.`,
      detail: 'This update needs a quick reinstall. Click Download to get the latest installer.',
      buttons: ['Download', 'Later'], defaultId: 0, cancelId: 1,
    }).then((r) => { if (r.response === 0) shell.openExternal(RELEASES_PAGE); }).catch(() => {});
  }
}

// Single instance — a second launch focuses the existing window, and guarantees the startup OTA swap
// is never racing another MIKA process that holds the bundled python's files.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(start).catch((err) => {
    if (otaApplied && !badHandled) return handleBadUpdate(String((err && err.message) || err));
    fail('MIKA failed to start', String((err && err.message) || err));
  });
}

app.on('window-all-closed', () => { intentionalQuit = true; killTreeSync(py); app.quit(); });
app.on('before-quit', () => { intentionalQuit = true; killTreeSync(py); });

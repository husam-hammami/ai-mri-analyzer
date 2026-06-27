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

// ---- OTA auto-update (packaged only) + boot health / crash-loop guard ------------------------
const RELEASES_URL = 'https://github.com/husam-hammami/ai-mri-analyzer/releases';
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

// A bad OTA update can ship a build that won't boot. We count consecutive boot FAILURES of a SPECIFIC
// version (only real failures — see recordBootFailure, never a healthy boot the user happened to quit);
// once a non-good version has failed twice we stop reinstalling it and point the user at the last
// working release instead of looping forever.
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
      message: `This update (v${version}) isn't starting correctly.`,
      detail: `The last working version was v${s.lastGood}. You can download and reinstall it from the Releases page.`,
      buttons: ['Open Releases page', 'Try again'], defaultId: 0, cancelId: 1,
    });
    writeStamp({ fails: 0, failVersion: null, lastGood: s.lastGood });
    if (r === 0) { shell.openExternal(RELEASES_URL); intentionalQuit = true; app.quit(); return false; }
  }
  return true;   // increments happen only on a real boot failure, not here
}

function setupAutoUpdate() {
  if (!app.isPackaged) return;                            // dev: no published releases to pull
  if (process.env.PORTABLE_EXECUTABLE_DIR) return;        // portable build can't self-update — don't prompt
  let autoUpdater;
  try { ({ autoUpdater } = require('electron-updater')); }
  catch (_) { console.error('[updater] electron-updater not bundled — OTA disabled'); return; }
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;                // fallback if the user picks "Later"
  autoUpdater.on('update-downloaded', (info) => {
    const r = dialog.showMessageBoxSync({
      type: 'info', title: 'Update ready',
      message: `MIKA ${(info && info.version) || ''} is ready to install.`.replace('  ', ' '),
      detail: 'Restart now to apply it, or it will install automatically next time you quit MIKA.',
      buttons: ['Restart now', 'Later'], defaultId: 0, cancelId: 1,
    });
    // Tear the sidecar down SYNCHRONOUSLY first so NSIS can overwrite resources/python (held DLLs).
    if (r === 0) setImmediate(() => { intentionalQuit = true; killTreeSync(py); autoUpdater.quitAndInstall(false, true); });
  });
  autoUpdater.on('error', (e) => console.error('[updater]', (e && e.message) || e));
  autoUpdater.checkForUpdates().catch((e) => console.error('[updater] check failed:', (e && e.message) || e));
}

async function start() {
  const version = app.getVersion();
  if (!crashLoopGuard(version)) return;            // bad-update bail-out (user chose to reinstall)

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
  py = spawn(PY, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(port)],
             { cwd: BACKEND, env });
  py.stdout.on('data', (d) => process.stdout.write(`[sidecar] ${d}`));
  py.stderr.on('data', (d) => { const s = `${d}`; tail.push(s); if (tail.length > 40) tail.shift(); process.stderr.write(`[sidecar] ${s}`); });
  py.on('exit', (code) => {
    console.log(`[sidecar] exited with code ${code}`);
    if (starting) {
      const hint = app.isPackaged
        ? 'This usually means the install is incomplete — reinstall MIKA.'
        : `Most often a missing dependency — from the repo root run:\n  ${PY} -m pip install -r requirements.txt`;
      fail('MIKA backend failed to start',
        `The Python backend exited (code ${code}). ${hint}\n\nRecent output:\n` + tail.join('').slice(-1200));
    }
  });

  const url = `http://127.0.0.1:${port}/`;
  await waitReady(url);
  starting = false;
  console.log(`[mika] backend ready on ${url}`);

  // Mark this build "good" only if the read environment is actually healthy (200), not just that the
  // HTTP root answered — so an ABI-degraded OTA build isn't trusted and can trip the crash-loop guard.
  const health = await getJson(`http://127.0.0.1:${port}/health`);
  if (health && health.status === 'ok') {
    writeStamp({ fails: 0, failVersion: null, lastGood: version });
  } else if (!intentionalQuit) {
    recordBootFailure(version);
    console.warn('[mika] backend /health degraded:', health && (health.mismatches || health.import_error));
  }
  // Half-applied-update signal (rare — CI enforces the version match at release). Log, don't nag.
  if (health && health.version && health.version !== version) {
    console.warn(`[mika] version mismatch: app v${version}, backend v${health.version}`);
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
  setupAutoUpdate();
}

app.whenReady().then(start).catch((err) => {
  fail('MIKA failed to start', String(err && err.message || err));
});

app.on('window-all-closed', () => { intentionalQuit = true; killTreeSync(py); app.quit(); });
app.on('before-quit', () => { intentionalQuit = true; killTreeSync(py); });

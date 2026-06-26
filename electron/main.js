// MIKA desktop shell.
// Spawns the Python backend (uvicorn app:app) as a sidecar on a free localhost port, waits for it
// to answer, then opens a window pointed at it. The renderer is the existing MIKA web app talking
// to the backend over HTTP. Works both in dev (repo checkout) and packaged (electron-builder), and
// surfaces a clear dialog if its host prerequisites (Python, the Claude CLI) are missing.
//
// Host prerequisites (by design — MIKA runs on the user's own Claude subscription):
//   • Python 3.10+ with the backend requirements installed (see backend/requirements.txt).
//   • The Claude CLI signed in (`claude /login`) — the analysis + lab + chat reads run on it.
const { app, BrowserWindow, shell, dialog } = require('electron');
const { spawn, exec, execFileSync } = require('child_process');
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

function resolvePython() {
  // Explicit override wins; otherwise try the common interpreter names and pick the first that runs.
  const candidates = [process.env.MIKA_PYTHON, isWin ? 'python' : 'python3', 'python'].filter(Boolean);
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

function killTree(proc) {
  if (!proc || proc.killed) return;
  if (isWin) {
    try { exec(`taskkill /pid ${proc.pid} /T /F`); } catch (e) { /* best effort */ }
  } else {
    try { proc.kill('SIGTERM'); } catch (e) { /* best effort */ }
  }
}

function fail(title, message) {
  starting = false;
  try { dialog.showErrorBox(title, message); } catch (e) { console.error(title, message); }
  killTree(py);
  app.quit();
}

async function start() {
  if (!fs.existsSync(path.join(BACKEND, 'app.py'))) {
    return fail('MIKA could not find its backend',
      `Expected the backend at:\n${BACKEND}\n\nThe install may be incomplete — reinstall MIKA.`);
  }

  const PY = resolvePython();
  if (!PY) {
    return fail('Python is required',
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
      fail('MIKA backend failed to start',
        `The Python backend exited (code ${code}). Most often a missing dependency — from the ` +
        `backend folder run:\n  ${PY} -m pip install -r requirements.txt\n\nRecent output:\n` +
        tail.join('').slice(-1200));
    }
  });

  const url = `http://127.0.0.1:${port}/`;
  await waitReady(url);
  starting = false;
  console.log(`[mika] backend ready on ${url}`);

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
}

app.whenReady().then(start).catch((err) => {
  fail('MIKA failed to start', String(err && err.message || err));
});

app.on('window-all-closed', () => { killTree(py); app.quit(); });
app.on('before-quit', () => killTree(py));

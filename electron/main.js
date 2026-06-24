// MIKA desktop shell — P0 (dev sidecar bring-up).
// Spawns the SYSTEM Python backend (uvicorn app:app) as a sidecar on a free localhost port,
// waits for it to answer, then opens a window pointed at it. No bundling yet (P1/P2 add that).
// The renderer is just the existing MIKA web app talking to the backend over HTTP.
const { app, BrowserWindow, shell, dialog } = require('electron');
const { spawn, exec } = require('child_process');
const net = require('net');
const path = require('path');
const http = require('http');

const isWin = process.platform === 'win32';
const BACKEND = path.join(__dirname, '..', 'backend');
const PY = process.env.MIKA_PYTHON || 'python';   // P0: system python on PATH; bundled in P1

let py = null;
let win = null;

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

async function start() {
  const port = await freePort();
  const env = {
    ...process.env,
    MIKA_DATA_DIR: path.join(app.getPath('userData'), 'data'),   // writable per-user dir
  };
  py = spawn(PY, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(port)],
             { cwd: BACKEND, env });
  py.stdout.on('data', (d) => process.stdout.write(`[sidecar] ${d}`));
  py.stderr.on('data', (d) => process.stderr.write(`[sidecar] ${d}`));
  py.on('exit', (code) => console.log(`[sidecar] exited with code ${code}`));

  const url = `http://127.0.0.1:${port}/`;
  await waitReady(url);
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
  console.error('[mika] start failed:', err);
  try { dialog.showErrorBox('MIKA failed to start', String(err && err.message || err)); } catch (e) {}
  killTree(py);
  app.quit();
});

app.on('window-all-closed', () => { killTree(py); app.quit(); });
app.on('before-quit', () => killTree(py));

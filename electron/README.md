# MIKA Desktop (Electron)

The native desktop shell for MIKA. On launch it spawns the Python backend (`uvicorn app:app`) as a
localhost sidecar on a free port, waits for it to answer, then loads the MIKA web app in a window.
The renderer is the same single-file frontend served by the backend, so there is no separate build
step for the UI.

## Host prerequisites

MIKA runs on the user's own Claude subscription, so the host machine needs:

- **Python 3.10+** with the backend dependencies installed:
  ```sh
  cd ../backend && python -m pip install -r requirements.txt
  ```
- **The Claude CLI**, signed in once (`claude /login`). The imaging analysis, the lab/bloodwork
  read, and the case chat all run through `claude -p` on the subscription login — no API key needed.

The shell shows a clear error dialog (not a blank window) if Python is missing, if the backend
fails to import a dependency, or if the Claude CLI is not signed in (the latter surfaces inside the
app's sign-in panel).

## Run from source (dev)

```sh
npm install
npm start
```

`MIKA_PYTHON` overrides the interpreter the shell launches (defaults to `python` / `python3` on PATH).

## Build a distributable

```sh
npm run pack       # unpacked app under dist/win-unpacked/ (fast sanity check, no installer)
npm run dist:win   # Windows NSIS installer + portable .exe under dist/
npm run dist       # installer for the current OS (nsis/dmg/AppImage)
```

The build (`package.json` → `build`) copies `../backend` and `../frontend` into the app's
`resources/` directory (excluding `__pycache__`, `tests/`, `data/`, and archives). At runtime
`main.js` resolves the backend from `process.resourcesPath/backend` when packaged and from
`../backend` in dev; `app.py` finds the frontend as its sibling `../frontend`.

The frontend's React/ReactDOM/Babel and the DM Sans font are vendored under
`frontend/assets/vendor/`, so the packaged app loads with **no network/CDN dependency**.

## Bundling boundary (honest status)

This packages the MIKA app, backend source, and frontend into an installer, but it does **not** yet
embed a Python interpreter or the scientific stack (numpy/scipy/pydicom/PyMuPDF) — those are
expected on the host (see prerequisites). Freezing the backend into a standalone executable
(PyInstaller) for a zero-dependency installer is the remaining hardening step.

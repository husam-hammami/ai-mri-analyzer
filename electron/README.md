# MIKA Desktop (Electron)

The native desktop shell for MIKA. On launch it spawns the Python backend (`uvicorn app:app`) as a
localhost sidecar on a free port, waits for it to answer, then loads the MIKA web app in a window.
The renderer is the same single-file frontend served by the backend, so there is no separate build
step for the UI.

## Host prerequisites

MIKA runs on the user's own Claude subscription:

- **Python** — the PACKAGED app bundles its own python-embeddable (`scripts/fetch-python.ps1`), so an
  installed MIKA needs no host Python. To run from source (dev) you need **Python 3.10+** with the
  repo-root requirements installed:
  ```sh
  cd .. && python -m pip install -r requirements.txt
  ```
- **The Claude CLI**, signed in once (`claude /login`). The imaging analysis, the lab/bloodwork
  read, and the case chat all run through `claude -p` on the subscription login — no API key needed.
  (If the CLI isn't installed, the app's sign-in panel guides you to it.)

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
pwsh -File scripts/fetch-python.ps1   # FIRST: build the bundled python-embeddable into electron/python
npm run pack       # unpacked app under dist/win-unpacked/ (fast sanity check, no installer)
npm run dist:win   # Windows NSIS installer + portable .exe under dist/
npm run dist       # installer for the current OS (nsis/dmg/AppImage)
```
`fetch-python.ps1` must run before any build — the `python` extraResources entry requires
`electron/python` to exist.

The build (`package.json` → `build`) copies `../backend` and `../frontend` into the app's
`resources/` directory (excluding `__pycache__`, `tests/`, `data/`, and archives). At runtime
`main.js` resolves the backend from `process.resourcesPath/backend` when packaged and from
`../backend` in dev; `app.py` finds the frontend as its sibling `../frontend`.

The frontend's React/ReactDOM/Babel and the DM Sans font are vendored under
`frontend/assets/vendor/`, so the packaged app loads with **no network/CDN dependency**.

## Bundling boundary (status)

The packaged installer bundles a python-embeddable + the locked scientific stack
(numpy/scipy/pydicom/PyMuPDF/bidi) under `resources/python`, so a clean machine needs **no host
Python**. The Claude CLI is intentionally NOT bundled (it's proprietary and runs on the user's own
subscription) — the app detects it and guides the user to install + sign in. OTA auto-update ships via
`electron-updater` + GitHub Releases; the actual signed build + clean-VM/OTA validation are owner steps
(see `../docs/RELEASE_OWNER_CHECKLIST.md`).

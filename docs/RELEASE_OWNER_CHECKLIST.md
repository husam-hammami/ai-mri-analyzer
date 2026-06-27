# MIKA Windows release — owner checklist (the real-machine steps)

katana built the code/config for the bundled-Python OTA installer (Phases A–D). These steps need a
real Windows box, network, a signing cert, and a live Claude login — they **cannot** run inside a
Claude session (nested `claude -p` hangs; signing + live OTA need a real machine). Do them here.

## 0. One decision first — the signing route (gates silent OTA + SmartScreen trust)
electron-updater will only auto-apply an update whose signature matches the installed app, so an
**unsigned build cannot OTA**. Pick one:

| Route | Cost | Catch |
|---|---|---|
| **OV / EV code-signing cert (.pfx)** | ~$200–400/yr | Works with this repo as-is (set the two secrets below). EV clears SmartScreen immediately; OV warms up over installs. |
| **Azure Trusted Signing** | ~$10/mo | US/Canada entity only. Needs electron-builder ≥ 25 + a `build.win.azureSignOptions` block (not wired yet). |
| **SignPath.io OSS** | Free | Repo must go **public** + pass their medical-tool content review. Wire their Action after the build step. |

Recommended: **OV cert** if staying private; **SignPath-OSS** if you'll make the repo public.

## 1. Local build smoke (before tagging)
```pwsh
cd electron
npm install
pwsh -File scripts/fetch-python.ps1        # downloads python-embeddable + installs requirements.lock
npm run pack                                # electron-builder --dir  (unpacked, no installer)
```
Then **launch** `electron/dist/win-unpacked/MIKA.exe` and confirm: window opens, a study/lab read
runs end-to-end (needs `claude` signed in), Arabic PDF renders. This is the launch + live-Claude
check that the session could not do.

## 2. Clean-VM install test (the real proof)
On a Windows VM with **no Python and no VC++ runtime installed**:
1. Build the installer: set the signing secrets (step 0), push a tag `vX.Y.Z` (matching
   `electron/package.json` version) → the `release` workflow builds + publishes; or locally
   `npm run dist:win` with `WIN_CSC_LINK`/`WIN_CSC_KEY_PASSWORD` set.
2. Run the installer on the clean VM. Confirm MIKA launches (bundled Python works), the CLI first-run
   gate appears, and after `claude` install + sign-in a read completes.
3. Confirm data persists across a reinstall (`%APPDATA%/MIKA/data` is outside the app dir).

## 3. Live OTA cycle (do once to prove auto-update)
1. Release `vX.Y.Z`. Install it on the VM.
2. Bump `electron/package.json` **and** `backend/app.py` version to `vX.Y.(Z+1)` (the workflow
   preflight enforces they match), tag, release.
3. Open the installed app → within a minute it downloads the update → the "Update ready" dialog →
   Restart → it relaunches on the new version. Confirm the boot-state crash-loop guard by also
   testing a deliberately broken build (optional).

## 4. Verify the safety net survived packaging
The CI `release` workflow already runs `pytest backend/tests tests` (251 tests) before building, so a
green release implies no safety regression. If building locally, run it yourself first.

---
Code/config delivered in-session: `electron/scripts/fetch-python.ps1`, `requirements.lock` (+PyMuPDF/
bidi/reshaper), `electron/main.js` (bundled-python resolve + OTA + crash-loop), `electron/package.json`
(electron-updater + publish + v3.0.0), `backend/app.py` (`/health` version), `.github/workflows/release.yml`.

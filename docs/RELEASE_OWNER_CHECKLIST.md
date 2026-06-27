# MIKA Windows release — owner guide (custom OTA, no signing)

MIKA ships like the Hercules reporting module: an unsigned Windows installer + a **silent** auto-update.
There is **no code-signing and no certificate to buy**. End users never see an update prompt — they open
the app and it's already on the latest version.

## How a release works
1. **You push to `main`** (or run the `release` workflow manually).
2. CI (`.github/workflows/release.yml`) auto-versions it `3.0.<commit-count>`, runs the 251 safety tests,
   builds the bundled-Python NSIS installer, and publishes a GitHub Release with three assets:
   `MIKA-Setup-v*.exe` (installer), `MIKA-OTA-v*.zip` (the update payload), `manifest.json` (the zip's
   SHA-256).
3. **Installed apps update themselves.** On next launch each app checks the Releases feed, downloads the
   OTA zip, **verifies its SHA-256 against `manifest.json`**, swaps in the new `backend/` + `frontend/`,
   and starts — silently. If the new build fails its health check it **rolls back automatically** and the
   user keeps working on the previous version.

That's it. Day-to-day, "ship an update" = `git push`.

## First-time distribution
- Hand users `MIKA-Setup-v*.exe` from the latest Release.
- It's unsigned, so the **first** install shows Windows SmartScreen: **More info → Run anyway**. (One time.
  After that, auto-updates are silent.) If you later want to remove that one-time prompt, buy an OV/EV cert
  and set `CSC_LINK`/`CSC_KEY_PASSWORD` — but it is not required for OTA to work.

## The one rule to remember
- **Code/UI changes** (Python, prompts, the frontend) ship fully silently via OTA — just push.
- **Dependency changes** (editing `requirements.lock` / the bundled Python) can't be applied by the
  in-place OTA. The app will auto-rollback and keep running the old version safely; users get the new deps
  when they **reinstall** the latest `.exe`. (If you want, set `requires_reinstall: true` in that release's
  `manifest.json` and the app shows a one-time "download the installer" nudge.)

## Before the very first public release — validate once on a clean machine
On a Windows VM with **no Python**: run the installer, confirm MIKA launches (bundled Python works), the
Claude sign-in gate appears, and a read completes after `claude` is installed + signed in. Then cut a second
release and confirm the first machine silently updates to it. After that, trust the pipeline.

---
What the app does under the hood: `electron/main.js` `checkAndApplyUpdate()` (silent OTA + SHA-256 verify +
auto-rollback), `electron/scripts/fetch-python.ps1` (bundled runtime), `.github/workflows/release.yml`
(auto-version + build + verified release). No electron-updater, no signing.

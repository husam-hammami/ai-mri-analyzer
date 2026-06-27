<#
  fetch-python.ps1 — build a self-contained Python runtime for the MIKA bundle.

  Downloads the official Windows python-embeddable (3.11, x64), enables site-packages, installs pip,
  and pip-installs requirements.lock INTO it. electron-builder ships the result (electron/python) as
  extraResources, so the installed app needs NO host Python.

  Run this on the BUILD machine BEFORE `npm run dist:win` (needs network — ~25 MB embeddable +
  the wheels in requirements.lock; numpy/scipy/PyMuPDF/Pillow ship prebuilt cp311 win_amd64 wheels,
  so nothing compiles). Re-run only when requirements.lock or the Python version changes.

  Usage (from electron/):  pwsh -File scripts/fetch-python.ps1   [-PyVersion 3.11.9] [-Force]
#>
[CmdletBinding()]
param(
  [string]$PyVersion = "3.11.9",
  [switch]$Force
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # faster Invoke-WebRequest

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path     # electron/scripts
$elec    = Split-Path -Parent $here                            # electron
$root    = Split-Path -Parent $elec                            # repo root
$dest    = Join-Path $elec "python"                            # bundle target
$lock    = Join-Path $root "requirements.lock"
$pyMajor = ($PyVersion -split '\.')[0..1] -join ''             # "311"

if (-not (Test-Path $lock)) { throw "requirements.lock not found at $lock" }

if (Test-Path $dest) {
  if (-not $Force) { Write-Host "python/ already exists — pass -Force to rebuild. Skipping."; exit 0 }
  Write-Host "Removing existing $dest ..."; Remove-Item -Recurse -Force $dest
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# 1) embeddable runtime
$zipUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$zip    = Join-Path $env:TEMP "python-$PyVersion-embed-amd64.zip"
Write-Host "Downloading $zipUrl ..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zip
Write-Host "Extracting to $dest ..."
Expand-Archive -Path $zip -DestinationPath $dest -Force
Remove-Item $zip -Force

# 2) enable site-packages: the embeddable ships python311._pth with `#import site` commented out.
$pth = Join-Path $dest "python$pyMajor._pth"
if (-not (Test-Path $pth)) { throw "expected $pth in the embeddable — Python version mismatch?" }
$lines = Get-Content $pth | ForEach-Object { $_ -replace '^\s*#\s*import site\s*$', 'import site' }
if ($lines -notcontains 'import site') { $lines += 'import site' }
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
Set-Content -Path $pth -Value $lines -Encoding ASCII

$pyExe = Join-Path $dest "python.exe"

# 3) pip
$getPip = Join-Path $env:TEMP "get-pip.py"
Write-Host "Bootstrapping pip ..."
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
& $pyExe $getPip --no-warn-script-location
Remove-Item $getPip -Force

# 4) the locked dependency set (prebuilt wheels only — no toolchain on the build box)
Write-Host "Installing requirements.lock into the embeddable ..."
& $pyExe -m pip install --no-warn-script-location --only-binary=:all: -r $lock

# 5) verify the read pipeline imports under the BUNDLED interpreter (this is where ABI drift shows)
Write-Host "Verifying imports ..."
& $pyExe -c "import numpy,scipy,fitz,pydicom,PIL,bidi,arabic_reshaper,fastapi,uvicorn; assert numpy.__version__.startswith('1.26'), numpy.__version__; print('bundle python OK:', numpy.__version__, 'fitz', fitz.__doc__.splitlines()[0])"

# 6) verify the REAL run mode: resolve the backend app graph from the backend dir, exactly as main.js
#    launches it (`uvicorn app:app`, cwd=backend). Catches first-party deps / import-time errors that
#    the bare imports above can't see — a green here means the sidecar will actually boot.
Write-Host "Verifying backend app:app (real run mode) ..."
Push-Location (Join-Path $root "backend")
try {
  & $pyExe -c "import uvicorn.importer as u; u.import_from_string('app:app'); print('backend app:app OK')"
  if ($LASTEXITCODE -ne 0) { throw "backend app:app failed to import under the bundled interpreter" }
} finally { Pop-Location }

Write-Host "`nDone. Bundled Python is at $dest — electron-builder will ship it as resources/python."

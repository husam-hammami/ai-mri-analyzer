# Auto-resume the MIKA reading benchmark until every ground-truth study has a cached read.
# Survives the orchestrator dying mid-run: each pass re-runs validate.py, which skips already-cached
# reads (zero re-spend) and only reads what's missing. Loops until _remaining.py reports 0.
#
#   powershell -File backend/validation/run_until_done.ps1
#
$ErrorActionPreference = 'Continue'
$env:MIKA_AGENT_EFFORT = 'high'          # production-quality reads
Set-Location (Join-Path $PSScriptRoot '..')   # -> backend/
$log = Join-Path $PSScriptRoot 'read_run.log'
$maxPasses = 12

for ($i = 1; $i -le $maxPasses; $i++) {
    $remaining = (python -m validation._remaining 2>$null).Trim()
    if ($remaining -eq '0') { "[run_until_done] all reads complete after $($i-1) pass(es)." | Tee-Object -FilePath $log -Append; break }
    "[run_until_done] pass $i — $remaining study(ies) still to read..." | Tee-Object -FilePath $log -Append
    python -u -m validation.validate --read --judge-effort low *>> $log
    Start-Sleep -Seconds 5
}

$left = (python -m validation._remaining 2>$null).Trim()
if ($left -eq '0') { "[run_until_done] DONE — full scorecard in validation_report.md" } else { "[run_until_done] still $left missing after $maxPasses passes — inspect $log" }

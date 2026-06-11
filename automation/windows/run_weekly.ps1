# Weekly email automation script for Task Scheduler
# Runs: python -m automation.weekly_email
# Exit codes: 0 success, 1 failures, 2 config/lock error

# Compute repo root (two levels up from this script)
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repoRoot

# Resolve Python interpreter
# Task Scheduler runs with a limited non-interactive PATH, so we explicitly
# check the configured environment variable and virtual environment, not PATH.
$pythonExe = $null

if ($env:TRADINGAGENTS_PYTHON -and (Test-Path $env:TRADINGAGENTS_PYTHON)) {
    $pythonExe = $env:TRADINGAGENTS_PYTHON
} elseif (Test-Path (Join-Path $repoRoot ".venv\Scripts\python.exe")) {
    $pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
}

if (-not $pythonExe) {
    $logDir = Join-Path $repoRoot "automation\logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $timestamp = Get-Date -Format "yyyy-MM-dd"
    $logFile = Join-Path $logDir "task_weekly_${timestamp}.log"

    $errorMsg = "ERROR: Could not find Python interpreter. Set TRADINGAGENTS_PYTHON env var or install to .venv\Scripts\python.exe"
    Add-Content -Path $logFile -Value "=== started ===" -Encoding UTF8
    Add-Content -Path $logFile -Value $errorMsg -Encoding UTF8
    Add-Content -Path $logFile -Value "=== finished (exit 2) ===" -Encoding UTF8
    exit 2
}

# Set up logging
$logDir = Join-Path $repoRoot "automation\logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$timestamp = Get-Date -Format "yyyy-MM-dd"
$logFile = Join-Path $logDir "task_weekly_${timestamp}.log"

# Run the weekly email command
Add-Content -Path $logFile -Value "=== started ===" -Encoding UTF8
& $pythonExe -m automation.weekly_email *>&1 | Tee-Object -FilePath $logFile -Append | Out-Null
Add-Content -Path $logFile -Value "=== finished (exit $LASTEXITCODE) ===" -Encoding UTF8

exit $LASTEXITCODE

<#
    register_tasks.ps1

    Registers the TradingAgents automation tasks in Windows Task Scheduler:
      - "TradingAgents Daily"        : Mon-Fri at 17:30, runs run_daily.ps1
      - "TradingAgents Weekly Email" : Sunday at 18:00, runs run_weekly.ps1

    Usage:
      Run this script from a normal or elevated PowerShell prompt on the
      Windows machine that will execute the automation:

          .\register_tasks.ps1

    This script is idempotent and safe to re-run: each task is registered
    with -Force, which overwrites any existing task of the same name.

    Tasks are registered to run as the current user with an interactive
    logon type (no stored password is required or requested).
#>

# Compute repo root and absolute paths to the wrapper scripts
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$dailyScript = Join-Path $PSScriptRoot "run_daily.ps1"
$weeklyScript = Join-Path $PSScriptRoot "run_weekly.ps1"

# --- Common settings: retry/run when missed, time limit, battery behavior ---
$taskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# --- Daily task: Monday-Friday at 17:30 ---
$dailyAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$dailyScript`""

$dailyTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "17:30"

Register-ScheduledTask `
    -TaskName "TradingAgents Daily" `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -Settings $taskSettings `
    -Force | Out-Null

# --- Weekly task: Sunday at 18:00 ---
$weeklyAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$weeklyScript`""

$weeklyTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At "18:00"

Register-ScheduledTask `
    -TaskName "TradingAgents Weekly Email" `
    -Action $weeklyAction `
    -Trigger $weeklyTrigger `
    -Settings $taskSettings `
    -Force | Out-Null

# --- Confirmation ---
Write-Host ""
Write-Host "Registered scheduled tasks:" -ForegroundColor Green

$dailyInfo = Get-ScheduledTaskInfo -TaskName "TradingAgents Daily"
Write-Host "  - TradingAgents Daily"
Write-Host "      Script:        $dailyScript"
Write-Host "      Next run time: $($dailyInfo.NextRunTime)"

$weeklyInfo = Get-ScheduledTaskInfo -TaskName "TradingAgents Weekly Email"
Write-Host "  - TradingAgents Weekly Email"
Write-Host "      Script:        $weeklyScript"
Write-Host "      Next run time: $($weeklyInfo.NextRunTime)"

Write-Host ""
Write-Host "Note: Adjust the 17:30 trigger if your local time differs from US market close; analysis should run after close (4pm ET)."

Write-Host ""
Write-Host "To test a task immediately, run:"
Write-Host '  schtasks /Run /TN "TradingAgents Daily"'
Write-Host '  schtasks /Run /TN "TradingAgents Weekly Email"'

Write-Host ""
Write-Host "To remove a task, run:"
Write-Host '  Unregister-ScheduledTask -TaskName "TradingAgents Daily" -Confirm:$false'
Write-Host '  Unregister-ScheduledTask -TaskName "TradingAgents Weekly Email" -Confirm:$false'

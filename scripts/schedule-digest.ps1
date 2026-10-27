<#
Registers (or removes) the Windows Task Scheduler task that runs the morning
digest every day at 07:30.

In plain words:
  Same idea as schedule-sync.ps1 and schedule-remind.ps1, one more task,
  "uni-copilot digest":
    - runs:  powershell (hidden window) scripts\run-digest.ps1, which runs
             Claude Code headless (`claude -p`) with the morning-digest skill
             and appends the result to .data\digest.log
    - when:  every day at 07:30 (after the 07:00 sync has finished), weekends
             too: it's silent when nothing changed (decision 3)
    - missed it (PC off or asleep)?  It runs as soon as it can; the skill then
             marks the digest "(late, HH:MM)".
    - -Wake: also wake the PC from sleep for it (plugged in, sleep only).
    - -Model sonnet: use a lighter model for the digest (default: your default).
  Unlike sync and remind, each run uses your Claude plan (one short request).
  It only runs while you're signed in to Windows.

Usage (PowerShell, in the project folder):
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -DryRun   # show, change nothing
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1           # create or update
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -Wake     # ... and wake from sleep
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -RunNow   # create, then run once now
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -Remove   # delete the task
#>

param(
    [switch]$Wake,
    [switch]$RunNow,
    [switch]$Remove,
    [switch]$DryRun,
    [string]$Model = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "uni-copilot digest"
$Root = Split-Path -Parent $PSScriptRoot                    # the project folder (this file is in scripts\)
$Runner = Join-Path $Root "scripts\run-digest.ps1"
$Log = Join-Path $Root ".data\digest.log"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed the task '$TaskName'."
    } else {
        Write-Output "No task '$TaskName' to remove."
    }
    return
}

# The full path to claude.cmd, found now and saved in the task: a scheduled
# task may not see the same PATH as your terminal.
$Claude = (Get-Command claude.cmd -ErrorAction SilentlyContinue).Source
if (-not $Claude) {
    throw "claude.cmd not found on PATH. Is Claude Code installed (npm)?"
}

# What to run: Windows PowerShell with a hidden window, our runner script.
$runnerArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
    "-File `"$Runner`" -Claude `"$Claude`""
if ($Model) { $runnerArgs += " -Model $Model" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $runnerArgs -WorkingDirectory $Root

# When: every day at 07:30.
$trigger = New-ScheduledTaskTrigger -Daily -At "07:30"

# How: catch up after a missed run, run on battery too, never two at once,
# stop a run that hangs for 10 minutes, optionally wake the PC.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -WakeToRun:$Wake

$description = "uni-copilot: morning digest at 07:30 (Claude Code headless + morning-digest skill); " +
    "sends to Telegram only if something changed. Output: $Log. Remove with scripts\schedule-digest.ps1 -Remove."

if ($DryRun) {
    Write-Output "Would register '$TaskName':"
    Write-Output "  run:      powershell.exe $runnerArgs"
    Write-Output "  in:       $Root"
    Write-Output "  when:     daily 07:30"
    Write-Output "  catch up: yes   wake PC: $([bool]$Wake)   time limit: 10 min   model: $(if ($Model) { $Model } else { 'default' })   as user: $env:USERNAME"
    return
}

# Creates the task, or replaces it if it already exists (-Force).
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description $description -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "Registered '$TaskName'. Next run: $($info.NextRunTime). Wake PC: $([bool]$Wake). Log: $Log"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started one run now (about 1 minute). Then check: Get-Content `"$Log`" -Tail 6"
}

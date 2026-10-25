<#
Registers (or removes) the Windows Task Scheduler task that runs
`uni-copilot sync` every 2 hours from 07:00 to 23:00.

In plain words:
  Task Scheduler is Windows' built-in timetable for programs. This script
  creates one task, "uni-copilot sync", for your user account:
    - runs:  .venv\Scripts\pythonw.exe -m uni_copilot.cli --log .data\sync.log sync
             (pythonw = Python without a console window, so nothing pops up;
             the output goes to .data\sync.log instead of a screen)
    - when:  daily at 07:00, then every 2 hours until 23:00 (9 runs a day)
    - missed a run (PC off or asleep)?  It runs as soon as it can.
    - -Wake: also wake the PC from sleep for a run (plugged in, sleep only).
  It only runs while you're signed in to Windows: sync uses your saved
  browser profile in .auth\, which belongs to your user session.

Usage (PowerShell, in the project folder):
  powershell -ExecutionPolicy Bypass -File scripts\schedule-sync.ps1 -DryRun   # show, change nothing
  powershell -ExecutionPolicy Bypass -File scripts\schedule-sync.ps1           # create or update
  powershell -ExecutionPolicy Bypass -File scripts\schedule-sync.ps1 -Wake     # ... and wake from sleep
  powershell -ExecutionPolicy Bypass -File scripts\schedule-sync.ps1 -RunNow   # create, then run once now
  powershell -ExecutionPolicy Bypass -File scripts\schedule-sync.ps1 -Remove   # delete the task
#>

param(
    [switch]$Wake,
    [switch]$RunNow,
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$TaskName = "uni-copilot sync"
$Root = Split-Path -Parent $PSScriptRoot                    # the project folder (this file is in scripts\)
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Log = Join-Path $Root ".data\sync.log"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed the task '$TaskName'."
    } else {
        Write-Output "No task '$TaskName' to remove."
    }
    return
}

if (-not (Test-Path $Pythonw)) {
    throw "Not found: $Pythonw. Run 'uv sync' in the project folder first."
}

# What to run: Python without a window, our CLI, output into the log file.
$action = New-ScheduledTaskAction -Execute $Pythonw `
    -Argument "-m uni_copilot.cli --log `"$Log`" sync" `
    -WorkingDirectory $Root

# When: every day at 07:00, repeating every 2 hours for 16 hours (07:00 ... 23:00).
# The extra minute makes sure the 23:00 run is inside the window.
$trigger = New-ScheduledTaskTrigger -Daily -At "07:00"
$repeat = New-ScheduledTaskTrigger -Once -At "07:00" `
    -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Hours 16 -Minutes 1)
$trigger.Repetition = $repeat.Repetition

# How: catch up after a missed run, run on battery too, never two at once,
# stop a run that hangs for 15 minutes, optionally wake the PC.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -WakeToRun:$Wake

$description = "uni-copilot: fetch NTULearn (GET only) into the local store every 2 h, 07:00-23:00. " +
    "Output: $Log. Remove with scripts\schedule-sync.ps1 -Remove."

if ($DryRun) {
    Write-Output "Would register '$TaskName':"
    Write-Output "  run:      $Pythonw -m uni_copilot.cli --log `"$Log`" sync"
    Write-Output "  in:       $Root"
    Write-Output "  when:     daily 07:00, every 2 h until 23:00"
    Write-Output "  catch up: yes   wake PC: $([bool]$Wake)   time limit: 15 min   as user: $env:USERNAME"
    return
}

# Creates the task, or replaces it if it already exists (-Force).
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description $description -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "Registered '$TaskName'. Next run: $($info.NextRunTime). Wake PC: $([bool]$Wake). Log: $Log"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started one run now (about 1 minute). Then check: Get-Content `"$Log`" -Tail 8"
}

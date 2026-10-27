<#
Registers (or removes) the Windows Task Scheduler task that runs
`uni-copilot remind` every 30 minutes, day and night.

In plain words:
  Same idea as schedule-sync.ps1, one more task, "uni-copilot remind":
    - runs:  .venv\Scripts\pythonw.exe -m uni_copilot.cli --log .data\remind.log remind
             (no window; output goes to .data\remind.log)
    - when:  daily from 00:10, every 30 minutes until 23:40, so at :10 and
             :40 past every hour (48 runs a day). In the daytime that's just
             after a sync at :00 has finished. No quiet hours (your decision).
    - what a run does: reads the local store and, only if a reminder or the
      sync alert is due, sends ONE Telegram message. Most runs send nothing.
      It never contacts NTULearn and uses no Claude tokens.
    - missed a run (PC off or asleep)?  It runs as soon as it can, once.
    - -Wake: also wake the PC from sleep for a run (plugged in, sleep only).
  It only runs while you're signed in to Windows, like the sync task.

Usage (PowerShell, in the project folder):
  powershell -ExecutionPolicy Bypass -File scripts\schedule-remind.ps1 -DryRun   # show, change nothing
  powershell -ExecutionPolicy Bypass -File scripts\schedule-remind.ps1           # create or update
  powershell -ExecutionPolicy Bypass -File scripts\schedule-remind.ps1 -Wake     # ... and wake from sleep
  powershell -ExecutionPolicy Bypass -File scripts\schedule-remind.ps1 -RunNow   # create, then run once now
  powershell -ExecutionPolicy Bypass -File scripts\schedule-remind.ps1 -Remove   # delete the task
#>

param(
    [switch]$Wake,
    [switch]$RunNow,
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$TaskName = "uni-copilot remind"
$Root = Split-Path -Parent $PSScriptRoot                    # the project folder (this file is in scripts\)
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Log = Join-Path $Root ".data\remind.log"

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
    -Argument "-m uni_copilot.cli --log `"$Log`" remind" `
    -WorkingDirectory $Root

# When: every day at 00:10, repeating every 30 minutes for 23 h 31 min
# (00:10 ... 23:40). It stops before the next day's 00:10 start, so runs never overlap.
$trigger = New-ScheduledTaskTrigger -Daily -At "00:10"
$repeat = New-ScheduledTaskTrigger -Once -At "00:10" `
    -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 31)
$trigger.Repetition = $repeat.Repetition

# How: catch up after a missed run, run on battery too, never two at once,
# stop a run that hangs for 5 minutes, optionally wake the PC.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -WakeToRun:$Wake

$description = "uni-copilot: send due deadline reminders (24 h / 2 h) and the sync alert to Telegram, " +
    "every 30 min at :10 and :40. Reads the local store only. Output: $Log. " +
    "Remove with scripts\schedule-remind.ps1 -Remove."

if ($DryRun) {
    Write-Output "Would register '$TaskName':"
    Write-Output "  run:      $Pythonw -m uni_copilot.cli --log `"$Log`" remind"
    Write-Output "  in:       $Root"
    Write-Output "  when:     daily 00:10, every 30 min until 23:40 (48 runs a day)"
    Write-Output "  catch up: yes   wake PC: $([bool]$Wake)   time limit: 5 min   as user: $env:USERNAME"
    return
}

# Creates the task, or replaces it if it already exists (-Force).
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description $description -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "Registered '$TaskName'. Next run: $($info.NextRunTime). Wake PC: $([bool]$Wake). Log: $Log"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started one run now (a few seconds). Then check: Get-Content `"$Log`" -Tail 4"
}

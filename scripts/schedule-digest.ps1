<#
Registers (or removes) the Windows Task Scheduler task that pokes
`uni-copilot digest` every 30 minutes.

In plain words:
  "Poke often, decide in code" (28 Oct, after the 07:30 run was skipped while
  the laptop slept and Windows didn't catch it up). One task,
  "uni-copilot digest":
    - runs:  .venv\Scripts\pythonw.exe -m uni_copilot.cli --log .data\digest.log digest --claude <claude.exe>
             (pythonw = no window; the command starts Claude Code headless,
             also without a window)
    - when:  daily from 00:05, every 30 minutes until 23:35, so at :05 and :35
             (sync runs at :00, remind at :10 and :40)
    - what a poke does: `uni-copilot digest` decides. Before 07:30 or already
      done today: nothing. Otherwise, once a day: sync first if the store is
      over 3 h old, then the morning-digest skill (sends only if something
      changed). So after a night asleep the digest comes at the first poke
      after you open the laptop, with fresh data.
    - -Wake: also wake the PC from sleep for a poke (plugged in, sleep only).
  Each real digest uses your Claude plan (one short request a day); the other
  pokes use nothing. It only runs while you're signed in to Windows.

Usage (PowerShell, in the project folder):
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -DryRun   # show, change nothing
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1           # create or update
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -Wake     # ... and wake from sleep
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -RunNow   # create, then poke once now
  powershell -ExecutionPolicy Bypass -File scripts\schedule-digest.ps1 -Remove   # delete the task
A digest right now, by hand: uv run uni-copilot digest --force
#>

param(
    [switch]$Wake,
    [switch]$RunNow,
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$TaskName = "uni-copilot digest"
$Root = Split-Path -Parent $PSScriptRoot                    # the project folder (this file is in scripts\)
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
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

if (-not (Test-Path $Pythonw)) {
    throw "Not found: $Pythonw. Run 'uv sync' in the project folder first."
}

# The real claude.exe inside the npm install (next to claude.cmd), found now
# and saved in the task: a scheduled task may not see your terminal's PATH,
# and starting the .exe directly avoids a cmd.exe console window.
$Launcher = (Get-Command claude.cmd -ErrorAction SilentlyContinue).Source
if (-not $Launcher) { throw "claude.cmd not found on PATH. Is Claude Code installed (npm)?" }
$Claude = Join-Path (Split-Path $Launcher) "node_modules\@anthropic-ai\claude-code\bin\claude.exe"
if (-not (Test-Path $Claude)) { throw "Not found: $Claude" }

# What to run: Python without a window, our CLI, output into the log file.
$arguments = "-m uni_copilot.cli --log `"$Log`" digest --claude `"$Claude`""
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument $arguments -WorkingDirectory $Root

# When: every day from 00:05, every 30 minutes for 23 h 31 min (00:05 ... 23:35).
$trigger = New-ScheduledTaskTrigger -Daily -At "00:05"
$repeat = New-ScheduledTaskTrigger -Once -At "00:05" `
    -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 31)
$trigger.Repetition = $repeat.Repetition

# How: catch up after a missed poke, run on battery too, never two at once,
# stop a run after 15 minutes (a sync first + Claude), optionally wake the PC.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -WakeToRun:$Wake

$description = "uni-copilot: pokes the morning digest every 30 min at :05/:35; it runs once a day after 07:30 " +
    "(syncing first if the store is old). Output: $Log. Remove with scripts\schedule-digest.ps1 -Remove."

if ($DryRun) {
    Write-Output "Would register '$TaskName':"
    Write-Output "  run:      $Pythonw $arguments"
    Write-Output "  in:       $Root"
    Write-Output "  when:     daily 00:05, every 30 min until 23:35 (the digest itself: once a day after 07:30)"
    Write-Output "  catch up: yes   wake PC: $([bool]$Wake)   time limit: 15 min   as user: $env:USERNAME"
    return
}

# Creates the task, or replaces it if it already exists (-Force).
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description $description -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "Registered '$TaskName'. Next poke: $($info.NextRunTime). Wake PC: $([bool]$Wake). Log: $Log"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Poked once now. Then check: Get-Content `"$Log`" -Tail 8"
}

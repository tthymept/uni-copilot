<#
Runs the morning digest once: Claude Code headless with the morning-digest
skill. Task Scheduler starts this at 07:30 (see schedule-digest.ps1); you can
also run it by hand to test.

In plain words:
  `claude -p "<prompt>"` (print mode) runs ONE request with no chat window,
  prints Claude's final answer and exits. It uses your Claude plan login, the
  project's .mcp.json servers (pilot) and .claude\settings.json, from the
  project folder. Nobody is there to click "allow", so a tool that isn't
  allowed is simply refused.

  Allowed: the Skill tool and the 5 pilot tools the skill needs. Denied on top
  (a deny beats any allow, including your settings.local.json): shell, file
  edits, web access and the ntulearn login tool. The digest reads untrusted
  posts with nobody watching, so it gets exactly what it needs and no more.

  ANTHROPIC_API_KEY is cleared for this run: if it were set, `claude` would
  bill the paid API instead of your plan (project rule: no paid API).

  Output (Claude's last line: counts only) is appended to .data\digest.log,
  with a time stamp and the exit code, like the sync and remind logs.

Usage (PowerShell, in the project folder):
  powershell -ExecutionPolicy Bypass -File scripts\run-digest.ps1
  powershell -ExecutionPolicy Bypass -File scripts\run-digest.ps1 -Model sonnet
#>

param(
    [string]$Claude = "",   # full path to claude.cmd; empty = find it on PATH
    [string]$Model = ""     # e.g. "sonnet"; empty = your default model
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot                    # the project folder (this file is in scripts\)
$Log = Join-Path $Root ".data\digest.log"
$LogMaxBytes = 1MB

$Prompt = "Scheduled morning digest: nobody is at the PC. Use the morning-digest skill: check what's new " +
    "on the telegram channel and send the digest only if something changed. Never ask questions. " +
    "End with the skill's one-line result."
$Allowed = @("Skill", "mcp__pilot__whats_new", "mcp__pilot__list_events", "mcp__pilot__get_event",
    "mcp__pilot__send_telegram", "mcp__pilot__mark_seen")
$Denied = @("Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "mcp__ntulearn__login")

if (-not $Claude) {
    $Claude = (Get-Command claude.cmd -ErrorAction SilentlyContinue).Source
    if (-not $Claude) { throw "claude.cmd not found on PATH. Pass -Claude <full path>." }
}

# Start a fresh log when it's grown past 1 MB (the old one is kept as digest.log.old).
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
if ((Test-Path $Log) -and (Get-Item $Log).Length -gt $LogMaxBytes) {
    Move-Item $Log "$Log.old" -Force
}

Set-Location $Root
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
[Console]::OutputEncoding = [Text.Encoding]::UTF8           # read claude's output as UTF-8

$claudeArgs = @("-p", $Prompt, "--allowedTools") + $Allowed + @("--disallowedTools") + $Denied
if ($Model) { $claudeArgs += @("--model", $Model) }

Add-Content -Encoding utf8 $Log "=== $(Get-Date -Format 'ddd dd MMM HH:mm') SGT  morning digest (claude -p)"
try {
    & $Claude @claudeArgs 2>&1 | ForEach-Object { "$_" } | Add-Content -Encoding utf8 $Log
    $code = $LASTEXITCODE
} catch {
    Add-Content -Encoding utf8 $Log "=== CRASHED: $($_.Exception.Message)"
    $code = 2
}
Add-Content -Encoding utf8 $Log "=== exit code $code"
exit $code

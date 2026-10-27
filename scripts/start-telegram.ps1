<#
Starts the Claude Code session that answers you on Telegram.

In plain words:
  Claude Code's Telegram channel plugin (installed once with
  `/plugin install telegram@claude-plugins-official`) polls your bot and passes
  each message from you into THIS session; Claude answers with the plugin's
  `reply` tool. The session must stay open: messages only arrive while it runs.
  Close it with /exit or Ctrl+C. Restart it about once a day so its
  conversation stays short (decision 11).

  The bot token: the plugin reads TELEGRAM_BOT_TOKEN from the environment
  first. This script copies it from the project's .env into the environment
  of this one session (never printed, never written anywhere else), so you
  don't need `/telegram:configure` and the token never goes through a chat.

  "💭 Thinking…": .claude\telegram-session.json adds hooks for THIS session
  only (passed with --settings): they post "💭 Thinking…" the moment your
  message arrives and delete it before Claude's first message goes out
  (src\uni_copilot\notify\thinking.py). Other sessions don't load them.

  Only one program may read the bot's messages: while this session runs,
  `uni-copilot telegram-chat-id` gets a 409 (sending still works: reminders,
  the digest and `uni-copilot notify` are unaffected).

Usage (PowerShell, in the project folder):
  powershell -ExecutionPolicy Bypass -File scripts\start-telegram.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start-telegram.ps1 --model sonnet   # extra claude options
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot                    # the project folder (this file is in scripts\)
$EnvFile = Join-Path $Root ".env"

# TELEGRAM_BOT_TOKEN from .env (same file uni-copilot uses), unless it's already set.
if (-not $env:TELEGRAM_BOT_TOKEN) {
    if (-not (Test-Path $EnvFile)) { throw "No .env in $Root (it holds TELEGRAM_BOT_TOKEN)." }
    $line = Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -match '^\s*TELEGRAM_BOT_TOKEN\s*=' } | Select-Object -First 1
    if (-not $line) { throw "TELEGRAM_BOT_TOKEN is missing in .env." }
    $env:TELEGRAM_BOT_TOKEN = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

# Bun, which the plugin runs on. A terminal opened before Bun was installed
# (e.g. inside a VS Code that was already running) still has the old PATH, and
# then the plugin silently doesn't start (found 27 Oct). So add Bun's folder
# to this session's PATH if it's missing.
if (-not (Get-Command bun -ErrorAction SilentlyContinue)) {
    $BunDir = Join-Path $env:USERPROFILE ".bun\bin"
    if (-not (Test-Path (Join-Path $BunDir "bun.exe"))) {
        throw "Bun not found. Install it: powershell -c `"irm bun.sh/install.ps1 | iex`""
    }
    $env:Path = "$BunDir;$env:Path"
}

# Your Claude plan, never the paid API (same rule as the digest).
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

Set-Location $Root
Write-Output "Starting Claude Code with the Telegram channel in $Root (token from .env, not shown)."
Write-Output "Keep this window open; messages to the bot only arrive while it runs."
& claude --channels plugin:telegram@claude-plugins-official --settings (Join-Path $Root ".claude\telegram-session.json") @args

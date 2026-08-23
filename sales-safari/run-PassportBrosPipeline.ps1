#Requires -Version 5.1
# Stages 3-12 for the r/passportbros run, detached from any shell session.
#
# Why a scheduled task and not Start-Process: a background process launched from an ssh
# session or a tool-driven shell dies when that session ends. The first attempt on
# 2026-08-17 was killed at 1248/3011 documents that way. Task Scheduler owns the process
# instead, so nothing upstream can take it down.
#
# Registered with /it so it runs as the logged-on user. Stages 10-11 call the Claude CLI for
# world knowledge about real products, and that login belongs to this account.
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir "PassportBrosPipeline.log"

$prevEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"
# A Scheduled Task does not inherit the interactive PATH. `claude` lives in ~\.local\bin,
# so under schtasks every Claude-backed stage died with "[WinError 2] The system cannot find
# the file specified" -- which is how run pbfamily0001 shipped 25 keyword-template ideas, 0
# competitors and 25 unproven briefs while reporting status=done.
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

$Python = Join-Path $Root ".venv\Scripts\python.exe"

try {
  "=== starting $(Get-Date -Format o) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
  # Continue, not Stop: a stderr line from the child would otherwise surface as a
  # NativeCommandError and kill the wrapper on the first warning.
  $ErrorActionPreference = "Continue"
  # Run 2a926f2fad56, not b427c8326829: the first run inherited collection.max_threads=100
  # and stored 83 submissions. This one used --limit 10000 and holds 451 submissions plus the
  # same 2928 comments.
  & $Python -u -m pipeline.resume 2a926f2fad56 2>&1 | ForEach-Object { $_.ToString() } | Out-File -FilePath $LogPath -Append -Encoding utf8
  $code = $LASTEXITCODE
  $ErrorActionPreference = "Stop"
  "=== finished $(Get-Date -Format o) exitcode=$code ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
  exit $code
} finally {
  [Console]::OutputEncoding = $prevEncoding
}

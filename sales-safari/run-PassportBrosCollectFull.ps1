#Requires -Version 5.1
# Full Arctic Shift pull of r/passportbros, submissions included.
#
# Why --limit 10000: the collector caps the SUBMISSION sweep at `limit` and leaves the
# comment sweep uncapped (arcticshift_collector.py line ~343). The 2026-08-17 run inherited
# collection.max_threads = 100 and stored 83 submissions against 2928 comments, leaving 271
# threads with no opening post. Past runs through the web GUI used limit=10000, which is why
# they did not hit this.
#
# Detached under Task Scheduler on purpose: a process started from a tool-driven shell or an
# ssh session dies with that session.
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir "PassportBrosCollectFull.log"

$prevEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"

$Python = Join-Path $Root ".venv\Scripts\python.exe"

try {
  "=== starting $(Get-Date -Format o) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
  $ErrorActionPreference = "Continue"
  & $Python -u run_historical.py https://www.reddit.com/r/passportbros/ --limit 10000 2>&1 | ForEach-Object { $_.ToString() } | Out-File -FilePath $LogPath -Append -Encoding utf8
  $code = $LASTEXITCODE
  $ErrorActionPreference = "Stop"
  "=== finished $(Get-Date -Format o) exitcode=$code ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
  exit $code
} finally {
  [Console]::OutputEncoding = $prevEncoding
}

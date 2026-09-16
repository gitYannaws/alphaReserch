#Requires -Version 5.1
# Serves the Sales Safari GUI on http://localhost:8000
#
# Detached under Task Scheduler on purpose: a server started from a tool-driven shell or an
# ssh session dies when that session ends.
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir "webapp.log"

$env:PYTHONIOENCODING = "utf-8"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

"=== starting $(Get-Date -Format o) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
$ErrorActionPreference = "Continue"
& $Python -u -m uvicorn webapp.app:app --port 8000 2>&1 | ForEach-Object { $_.ToString() } | Out-File -FilePath $LogPath -Append -Encoding utf8

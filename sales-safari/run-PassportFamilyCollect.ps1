#Requires -Version 5.1
# Collect the FULL Arctic Shift archive for every real passport-bro subreddit, one after
# another, into its own run. A later step merges them into a single corpus run.
#
# --limit 100000 on every sub: the collector caps the SUBMISSION sweep at `limit` and leaves
# the comment sweep uncapped. r/thepassportbros alone holds 19,625 posts in the archive, so
# the old 10000 cap was already close to binding, and 100 (config max_threads) truncated
# r/passportbros to 83 submissions on 2026-08-17.
#
# The sub list is curated, not from the archive's prefix search. That search returns
# r/PassportHelp, r/PassportPorn and ~60 subs with 1-3 subscribers, and it MISSES
# r/thepassportbros because the name starts with "the". Snark and circlejerk subs are left
# out on purpose: they carry mockery, not first-person pain.
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir "PassportFamilyCollect.log"

$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$Python = Join-Path $Root ".venv\Scripts\python.exe"

# Ordered biggest-first so the corpus that matters most lands even if a later sub fails.
$Subs = @(
  "thepassportbros",        # the live main sub
  "Passport_Bros",          # 7,919 members
  "PassportBrosHQ",         # 2,021
  "passportbros",           # dead sub, archive stops 2025-09-04
  "PassportBrosOnTop",      # 547
  "passportbrolifestyle",   # 476
  "passportbros2",          # 441
  "Passportbro",            # 292
  "PassportGentlemen",      # 180
  "passportbrosunited",     # 153
  "PassportBrosWorldWide",  # 110
  "PassportBrosMovement",   # 94
  "passportbroslist"        # 80
)

"=== FAMILY COLLECT starting $(Get-Date -Format o) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
"subs: $($Subs -join ', ')" | Out-File -FilePath $LogPath -Append -Encoding utf8

foreach ($sub in $Subs) {
  "--- r/$sub start $(Get-Date -Format o) ---" | Out-File -FilePath $LogPath -Append -Encoding utf8
  $ErrorActionPreference = "Continue"
  & $Python -u run_historical.py "https://www.reddit.com/r/$sub/" --limit 100000 2>&1 |
      ForEach-Object { $_.ToString() } | Out-File -FilePath $LogPath -Append -Encoding utf8
  $code = $LASTEXITCODE
  "--- r/$sub done exitcode=$code $(Get-Date -Format o) ---" | Out-File -FilePath $LogPath -Append -Encoding utf8
  # A short breather between subs keeps the archive's rate limiter calm across a long sweep.
  Start-Sleep -Seconds 30
}

"=== FAMILY COLLECT finished $(Get-Date -Format o) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8

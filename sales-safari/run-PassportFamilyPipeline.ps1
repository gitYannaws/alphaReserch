#Requires -Version 5.1
# Stages 3-12 over the pooled passport-bro corpus, run pbfamily0001 (186,925 documents).
#
# The merge already ran on 2026-08-18 and is a one-shot snapshot, so it is NOT repeated
# here: scripts/merge_runs.py refuses a job id that already holds documents, and an inline
# emptiness check in PowerShell proved too fragile to be worth it. To rebuild the corpus
# after collecting more subs, pick a NEW job id:
#   .venv\Scripts\python.exe scripts\merge_runs.py --like passport --job-id pbfamily0002
#
# Every document is re-extracted, including the 56,298 processed by older runs. Those used
# the older narrow prompt and yield ~14% pains per document against ~39% today; reusing them
# would make themes from r/thepassportbros read 2.7x weaker than freshly extracted subs and
# corrupt the cross-sub comparison this pooling exists to measure.
#
# Extraction is now checkpointed (extract_seen table), so a crash resumes at the batch that
# failed instead of restarting a ~70 hour pass.
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir "PassportFamilyPipeline.log"

$env:PYTHONIOENCODING = "utf-8"
# A Scheduled Task does not inherit the interactive PATH. `claude` lives in ~\.local\bin,
# so under schtasks every Claude-backed stage died with "[WinError 2] The system cannot find
# the file specified" -- which is how run pbfamily0001 shipped 25 keyword-template ideas, 0
# competitors and 25 unproven briefs while reporting status=done.
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$Python = Join-Path $Root ".venv\Scripts\python.exe"

"=== PIPELINE starting $(Get-Date -Format o) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
# Continue, not Stop: a stderr line from the child would otherwise surface as a
# NativeCommandError and kill the wrapper on the first warning it prints.
$ErrorActionPreference = "Continue"
# --from 3 is REQUIRED here, not optional. The resume runner infers the starting stage
# from stored artifacts, and any pain at all makes it treat extraction as finished: a
# 12-document smoke test left 8 pains, so the first launch skipped stage 3 entirely and
# "completed" the whole 186,925-document corpus in 27 seconds with 0 clusters.
& $Python -u -m pipeline.resume pbfamily0001 --from 3 2>&1 |
    ForEach-Object { $_.ToString() } | Out-File -FilePath $LogPath -Append -Encoding utf8
$code = $LASTEXITCODE
"=== PIPELINE finished $(Get-Date -Format o) exitcode=$code ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
exit $code

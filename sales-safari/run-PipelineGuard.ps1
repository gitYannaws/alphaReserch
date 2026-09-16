#Requires -Version 5.1
# Restarts the pooled-corpus run if it has died. Registered on a 10-minute schedule.
# Harmless when the run is healthy: the guard exits without output.
$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
& (Join-Path $Root ".venv\Scripts\python.exe") scripts\guard_pipeline.py pbfamily0001 AlphaPassportFamilyPipeline

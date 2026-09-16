' Launches run-PipelineGuard.ps1 with no console window at all.
' The scheduled task runs as an interactive user, so powershell.exe painted a console
' box for a fraction of a second every 10 minutes even with -WindowStyle Hidden.
' WScript.Shell.Run with intWindowStyle 0 creates the process already hidden; python.exe
' and the guard's own powershell calls inherit that hidden console.
' The third argument True makes wscript wait, so the task's runtime stays accurate.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""C:\Users\almig\projects\alphaResearch\sales-safari\run-PipelineGuard.ps1""", 0, True

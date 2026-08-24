$ErrorActionPreference = "Stop"

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartupDir = [Environment]::GetFolderPath("Startup")
$Target = Join-Path $StartupDir "TmallRealtimeAgent.vbs"
$Source = Join-Path $BaseDir "start_realtime_agent_hidden.vbs"

Copy-Item -Path $Source -Destination $Target -Force
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$Target`"" -WindowStyle Hidden

Write-Host "Installed and started local realtime agent."
Write-Host "Startup item: $Target"
Write-Host "Manual and scheduled tasks are now handled by realtime_agent.py."

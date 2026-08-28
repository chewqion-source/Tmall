$ErrorActionPreference = "Stop"

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartupDir = [Environment]::GetFolderPath("Startup")
$Target = Join-Path $StartupDir "TmallRealtimeCrawler.vbs"
$LoopScript = Join-Path $BaseDir "run_realtime_scheduler_loop.ps1"

$escapedLoopScript = $LoopScript.Replace("""", """""")
$vbs = @"
Set shell = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""$escapedLoopScript"""
shell.Run cmd, 0, False
"@

Set-Content -Path $Target -Value $vbs -Encoding ASCII

Start-Process -FilePath "wscript.exe" -ArgumentList "`"$Target`"" -WindowStyle Hidden

Write-Host "Installed and started local realtime scheduler."
Write-Host "Startup item: $Target"
Write-Host "Schedule: daily 09:00-23:59, every 2 hours."

$ErrorActionPreference = "Stop"

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartupDir = [Environment]::GetFolderPath("Startup")
$Target = Join-Path $StartupDir "TmallRealtimeAgent.vbs"

$escapedBaseDir = $BaseDir.Replace("""", """""")
$vbs = @"
Set shell = CreateObject("WScript.Shell")
cmd = "cmd.exe /c cd /d ""$escapedBaseDir"" && ""D:\python\python.exe"" realtime_agent.py"
shell.Run cmd, 0, False
"@

Set-Content -Path $Target -Value $vbs -Encoding ASCII
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$Target`"" -WindowStyle Hidden

Write-Host "Installed and started local realtime agent."
Write-Host "Startup item: $Target"
Write-Host "Manual and scheduled tasks are now handled by realtime_agent.py."

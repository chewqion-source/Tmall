@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_scheduled_realtime.ps1"
exit /b %ERRORLEVEL%

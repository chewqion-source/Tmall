@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_scheduled_realtime.ps1"
exit /b %ERRORLEVEL%

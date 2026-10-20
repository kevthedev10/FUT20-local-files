@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-LocalFUT.ps1"
exit /b %errorlevel%

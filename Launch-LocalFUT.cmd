@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-LocalFUT.ps1" %*
exit /b %errorlevel%

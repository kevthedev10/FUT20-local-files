@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0CHECK-LOCAL-FUT.ps1"
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%

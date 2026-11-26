@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"

if exist "%ROOT%install_pr_bridge.ps1" goto run
echo [ERROR] install_pr_bridge.ps1 is missing.
echo Extract the complete ZIP before running this launcher.
pause
exit /b 2

:run
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ROOT%install_pr_bridge.ps1"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" echo Premiere plugin setup completed.
if not "%RC%"=="0" echo [ERROR] Premiere plugin setup returned exit code %RC%.
pause
exit /b %RC%

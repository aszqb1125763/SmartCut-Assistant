@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"

if exist "%ROOT%setup_windows.ps1" goto run
echo [ERROR] setup_windows.ps1 is missing.
echo Extract the complete ZIP before running this launcher.
pause
exit /b 2

:run
if /I "%~1"=="--check" goto check
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ROOT%setup_windows.ps1"
set "RC=%ERRORLEVEL%"
goto finish

:check
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ROOT%setup_windows.ps1" -Check
set "RC=%ERRORLEVEL%"

:finish
if "%RC%"=="0" exit /b 0
echo.
echo [ERROR] SmartCut setup returned exit code %RC%.
pause
exit /b %RC%

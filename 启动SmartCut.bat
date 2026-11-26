@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul

pushd "%~dp0" >nul 2>nul
if errorlevel 1 (
  echo [X] 无法打开 SmartCut 目录。
  pause
  exit /b 3
)
set "ROOT=%~dp0"

if exist "%ROOT%SmartCut 智能剪辑助手.exe" (
  set "TARGET=%ROOT%SmartCut 智能剪辑助手.exe"
  goto :launch_exe
)

if exist "%ROOT%智能剪辑助手.exe" (
  set "TARGET=%ROOT%智能剪辑助手.exe"
  goto :launch_exe
)

if exist "%ROOT%.venv\Scripts\pythonw.exe" (
  goto :launch_python
)

echo SmartCut 尚未安装。正在打开安装脚本…
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%setup_windows.ps1"
set "RC=%errorlevel%"
if not "%RC%"=="0" pause
goto :exit

:launch_exe
start "" "%TARGET%"
set "RC=%errorlevel%"
goto :exit

:launch_python
start "" "%ROOT%.venv\Scripts\pythonw.exe" "%ROOT%app_main.py"
set "RC=%errorlevel%"

:exit
if not defined RC set "RC=0"
popd
exit /b %RC%


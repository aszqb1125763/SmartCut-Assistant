@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist "SmartCut 智能剪辑助手.exe" (
  start "" "SmartCut 智能剪辑助手.exe"
  exit /b 0
)

if exist "智能剪辑助手.exe" (
  start "" "智能剪辑助手.exe"
  exit /b 0
)

if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "app_main.py"
  exit /b 0
)

echo SmartCut 尚未安装。正在打开安装脚本…
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
if errorlevel 1 pause
exit /b %errorlevel%

@echo off
setlocal
cd /d "%~dp0"
title SmartCut 智能剪辑助手 - 选择安装目录
chcp 65001 >nul 2>nul

echo.
echo  ==================================================
echo    SmartCut 智能剪辑助手 - 选择安装目录
echo  ==================================================
echo.
echo   会自动准备 Python、依赖、FFmpeg、当前语音模型、8B 模型和 PR 插件。
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1" -ChooseInstallDirectory %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
  echo   [X] 安装器退出码 %RC%，请查看窗口提示或 logs 文件夹。
  echo.
  pause
)
exit /b %RC%

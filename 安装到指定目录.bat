@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul

pushd "%~dp0" >nul 2>nul
if errorlevel 1 (
  echo [X] 无法打开安装包目录。请先完整解压到本地磁盘，再重新运行。
  pause
  exit /b 3
)
set "ROOT=%~dp0"
title SmartCut 智能剪辑助手 - 选择安装目录

echo.
echo  ==================================================
echo    SmartCut 智能剪辑助手 - 选择安装目录
echo  ==================================================
echo.
echo   会自动准备 Python、依赖、FFmpeg、当前语音模型、8B 模型和 PR 插件。
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%setup_windows.ps1" -ChooseInstallDirectory %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
  echo   [X] 安装器退出码 %RC%，请查看窗口提示或 logs 文件夹。
  echo.
  pause
)
popd
exit /b %RC%


@echo off
chcp 65001 >nul
echo 正在安装「智能剪辑 PR 桥」...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_pr_bridge.ps1"
echo.
pause

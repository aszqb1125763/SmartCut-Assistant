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

echo 正在安装「智能剪辑 PR 桥」...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%install_pr_bridge.ps1"
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" echo [X] PR 插件安装未完成，退出码 %RC%。
pause
popd
exit /b %RC%


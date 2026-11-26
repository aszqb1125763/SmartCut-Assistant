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

if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo [错误] 运行环境尚未安装，请先双击“安装并启动.bat”。
  pause
  set "RC=1"
  goto :exit
)
echo 正在以控制台排错模式启动 SmartCut 智能剪辑助手…
echo 关闭本窗口会同时停止软件。
echo.
"%ROOT%.venv\Scripts\python.exe" "%ROOT%app_main.py"
set "RC=%errorlevel%"
echo.
echo ---- SmartCut 已退出 ----
pause

:exit
if not defined RC set "RC=0"
popd
exit /b %RC%


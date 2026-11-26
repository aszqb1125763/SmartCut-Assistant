@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 运行环境尚未安装，请先双击“安装并启动.bat”。
  pause
  exit /b 1
)
echo 正在以控制台排错模式启动 SmartCut 智能剪辑助手…
echo 关闭本窗口会同时停止软件。
echo.
".venv\Scripts\python.exe" app_main.py
echo.
echo ---- SmartCut 已退出 ----
pause

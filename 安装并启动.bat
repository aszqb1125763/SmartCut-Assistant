@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title SmartCut 智能剪辑助手 - 一键安装
chcp 65001 >nul 2>nul

echo.
echo  ==================================================
echo    SmartCut 智能剪辑助手 - 一键安装并启动
echo  ==================================================
echo.

rem 先验证安装包，再查询或安装任何系统组件。
for %%F in (installer.py product_info.py requirements.txt constraints-runtime.txt app_main.py 启动SmartCut.bat brains\__init__.py conf\decision.json conf\coords_matrix.json conf\spec_manifest.json conf\editor_model.json) do (
  if not exist "%~dp0%%F" (
    echo   [X] 安装包不完整，缺少 %%F
    echo       请完整解压 ZIP 后再运行，不要在压缩包预览里直接双击。
    echo.
    pause
    exit /b 2
  )
)

rem 成品包优先让 EXE 自己运行安装器；不需要本机 Python，也不创建第二套环境。
if exist "%~dp0智能剪辑助手.exe" if exist "%~dp0_internal\python*.dll" (
  echo   [使用成品程序自举，无需安装 Python]
  "%~dp0智能剪辑助手.exe" --run-script "%~dp0installer.py" %*
  goto :finished
)

rem 联网开源包：优先包内运行时，其次本机 Python，最后用 winget 自动安装。
set "PY="
if exist "%~dp0runtime\python\python.exe" (
  set "PY=%~dp0runtime\python\python.exe"
  echo   [使用包内 Python 运行时]
  goto :run_installer
)

for %%V in (3.12 3.11 3.10) do (
  if not defined PY (
    for /f "delims=" %%P in ('py -%%V -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
  )
)
if not defined PY (
  for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
)
if not defined PY (
  set "CAND=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if exist "!CAND!" set "PY=!CAND!"
)

if not defined PY (
  where winget >nul 2>nul
  if errorlevel 1 goto :missing_python
  echo   未找到 Python，正在通过 winget 为当前用户安装 Python 3.12...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
  for %%V in (3.12 3.11 3.10) do (
    if not defined PY (
      for /f "delims=" %%P in ('py -%%V -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
    )
  )
  if not defined PY (
    set "CAND=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    if exist "!CAND!" set "PY=!CAND!"
  )
)

if not defined PY goto :missing_python

:run_installer
echo   Python: %PY%
echo   首次安装会自动准备依赖、FFmpeg、所选模型和 Premiere 插件。
echo.
"%PY%" "%~dp0installer.py" %*
goto :finished

:missing_python
if exist "%~dp0setup_windows.ps1" (
  echo   正在切换到 PowerShell 官方安装器备用流程...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
  exit /b %errorlevel%
)
echo.
echo   [X] 无法自动准备 Python 3.12。
echo       请从 https://www.python.org/downloads/windows/ 安装 Python 3.12，
echo       勾选 Add python.exe to PATH，再重新双击本文件。
echo.
pause
exit /b 1

:finished
set "RC=%errorlevel%"
if /I "%~1"=="--help" exit /b %RC%
if /I "%~1"=="-h" exit /b %RC%
echo.
if not "%RC%"=="0" (
  echo   [X] 安装未完成，退出码 %RC%。详情见 logs 文件夹。
  echo.
  pause
) else if /I "%~1"=="--check" (
  echo   环境体检完成。
) else (
  echo   安装完成，软件已启动。
  timeout /t 2 >nul 2>nul
)
exit /b %RC%

# -*- coding: utf-8 -*-
"""打包启动器 exe + 创建快捷方式。

产物：
  dist\\智能剪辑助手.exe        带图标、版本信息、无黑框
  桌面快捷方式 / 开始菜单快捷方式
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(BASE, ".venv", "Scripts", "python.exe")
NAME = "智能剪辑助手"
ICON = os.path.join(BASE, "assets", "icon.ico")
VER = os.path.join(BASE, "build_tools", "version_info.txt")
DIST = BASE    # exe 直接放主目录，不再塞 dist\


def run(cmd, **kw):
    print(">", " ".join(cmd))
    return subprocess.run(cmd, cwd=BASE, text=True, **kw)


def ensure_pyinstaller():
    r = run([PY, "-c", "import PyInstaller, sys; print(PyInstaller.__version__)"],
            capture_output=True)
    if r.returncode == 0:
        print("PyInstaller", r.stdout.strip())
        return True
    print("安装 PyInstaller…")
    r = run([PY, "-m", "pip", "install", "-q", "pyinstaller",
             "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    return r.returncode == 0


def build():
    if not ensure_pyinstaller():
        print("PyInstaller 安装失败")
        return False
    os.makedirs(DIST, exist_ok=True)
    cmd = [
        PY, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", NAME,
        "--icon", ICON,
        "--version-file", VER,
        "--distpath", DIST,
        "--workpath", os.path.join(BASE, "build"),
        "--specpath", os.path.join(BASE, "build"),
        os.path.join(BASE, "launcher.py"),
    ]
    r = run(cmd)
    exe = os.path.join(BASE, NAME + ".exe")
    if r.returncode != 0 or not os.path.isfile(exe):
        print("打包失败")
        return False
    print("已生成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1e6))
    return True


def shortcuts():
    """桌面 + 开始菜单快捷方式（指向 exe，带图标）。"""
    exe = os.path.join(BASE, NAME + ".exe")
    if not os.path.isfile(exe):
        print("没有 exe，跳过快捷方式")
        return
    ps = r"""
$ErrorActionPreference = 'Stop'
$exe = '%s'
$name = '%s'
$icon = '%s'
$dir = '%s'
$ws = New-Object -ComObject WScript.Shell
$targets = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)
foreach ($t in $targets) {
  $lnk = $ws.CreateShortcut((Join-Path $t ($name + '.lnk')))
  $lnk.TargetPath = $exe
  $lnk.WorkingDirectory = $dir
  $lnk.IconLocation = $icon
  $lnk.Description = '智能剪辑助手 · 自动剪辑录播'
  $lnk.Save()
  Write-Host ('  已创建 ' + (Join-Path $t ($name + '.lnk')))
}
""" % (exe, NAME, ICON, BASE)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       text=True, capture_output=True)
    print(r.stdout.strip() or r.stderr.strip())


if __name__ == "__main__":
    ok = build()
    if ok:
        shortcuts()
    sys.exit(0 if ok else 1)

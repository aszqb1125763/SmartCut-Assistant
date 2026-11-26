# -*- coding: utf-8 -*-
"""智能剪辑助手 · 启动器（打包成 exe 的入口）。

为什么用启动器而不是把整个 Python 环境塞进 exe：
  这个软件依赖 ffmpeg / llama.cpp / 模型文件 / PySide6，全套 20+ GB，
  塞进 exe 既装不下也不该每次更新都重打包。启动器的职责只有三件：
    1. 找到自己所在目录（安装目录）
    2. 找到自带的 python（.venv\\Scripts\\pythonw.exe）
    3. 拉起主程序 app_main.py（不弹黑框）
  出错时弹一个 Windows 原生对话框说清楚缺什么，而不是一闪而过。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

from product_info import PRODUCT_ID as APP_ID, PRODUCT_NAME as APP_NAME

MAIN = "app_main.py"


def _msg(title: str, text: str, error: bool = True) -> None:
    flags = 0x10 if error else 0x40          # MB_ICONERROR / MB_ICONINFORMATION
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        print("%s: %s" % (title, text), file=sys.stderr)


def app_dir() -> str:
    """找程序根目录：exe 自己所在目录，或往上最多两层。

    为什么要往上找：打包产物在 dist\\ 里，而 app_main.py / .venv 在上一层；
    用户也可能把 exe 放进子文件夹里，往上能找到就不报错。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    cur = base
    for _ in range(3):
        if os.path.isfile(os.path.join(cur, MAIN)) and os.path.isdir(
                os.path.join(cur, ".venv")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # 实在找不到就用 exe 所在目录（下面会给出明确的错误提示）
    return base


def find_python(base: str) -> str:
    """优先自带虚拟环境，其次系统 python。"""
    cands = [
        os.path.join(base, ".venv", "Scripts", "pythonw.exe"),
        os.path.join(base, ".venv", "Scripts", "python.exe"),
        os.path.join(base, "python", "pythonw.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def main() -> int:
    # 任务栏分组/图标用同一个 AppUserModelID，避免显示成 python
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

    base = app_dir()
    os.chdir(base)

    main_py = os.path.join(base, MAIN)
    if not os.path.isfile(main_py):
        _msg(APP_NAME,
             "找不到主程序 cards_ui.py。\n\n找过的位置：\n%s\n" % base
             + "\n请把「智能剪辑助手.exe」放在安装目录（和 cards_ui.py 同一层），"
               "不要单独拷到别的地方。")
        return 2

    py = find_python(base)
    if not py:
        _msg(APP_NAME,
             "找不到自带运行环境（.venv\\Scripts\\pythonw.exe）。\n\n"
             "请确认安装目录完整，或重新运行安装程序。")
        return 3

    icon = os.path.join(base, "assets", "icon.ico")
    args = [py, main_py] + sys.argv[1:]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 让主程序知道自己的安装目录（图标、配置、日志都基于它）
    env["SMARTCUT_HOME"] = base
    if os.path.isfile(icon):
        env["SMARTCUT_ICON"] = icon

    try:
        # CREATE_NO_WINDOW：不弹黑框；DETACHED 让启动器退出后主程序继续跑
        flags = 0x08000000
        subprocess.Popen(args, cwd=base, env=env, creationflags=flags,
                         close_fds=True)
    except Exception as exc:  # noqa: BLE001
        _msg(APP_NAME, "启动失败：\n%s\n\n可运行 setup_windows.ps1 -Check 体检，并查看 logs 目录。" % exc)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())

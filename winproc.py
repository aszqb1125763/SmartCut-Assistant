# -*- coding: utf-8 -*-
"""窗口/控制台隔离工具：所有"起进程"的地方统一走这里。

要点：
  · python_exe()：优先返回同目录的 pythonw.exe（无控制台）
  · run_hidden()/popen_hidden()：显式带 CREATE_NO_WINDOW + 隐藏 STARTUPINFO
这样即使用户用 python.exe 或 .bat 启动，也不会在分析/剪片时弹出黑色小窗。
"""
from __future__ import annotations

import os
import subprocess
import sys

import no_console

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def python_exe(windowless: bool = True) -> str:
    """返回不弹控制台的 Python 解释器（没有 pythonw 就退回当前解释器）。

    注意：打包后 sys.executable 就是「智能剪辑助手.exe」本身 —— 拿它当
    python 用等于每跑一次脚本就再开一个程序窗口。跑脚本请一律用下面的
    script_cmd()，它会自动换成 `exe --run-script 脚本` 的写法。
    """
    exe = sys.executable or ""
    if not windowless or os.name != "nt" or not exe:
        return exe
    base = os.path.dirname(exe)
    name = os.path.basename(exe).lower()
    if name.startswith("pythonw"):
        return exe
    cand = os.path.join(base, "pythonw.exe")
    if os.path.isfile(cand):
        return cand
    # 打包版：没有 pythonw，只能用 exe 自己（但必须配 --run-script，见 app_main）
    return exe


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _venv_pythonw() -> str:
    """找本机现成的无窗口解释器（pythonw.exe），优先用程序目录下的 .venv。"""
    if os.name != "nt":
        return ""
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.environ.get("SMARTCUT_HOME") or here
    exe_dir = os.path.dirname(os.path.abspath(sys.executable or ""))
    for base in (home, here, exe_dir):
        if not base:
            continue
        for sub in (os.path.join(".venv", "Scripts"),
                    os.path.join("runtime", "Scripts"), "Scripts"):
            p = os.path.join(base, sub, "pythonw.exe")
            if os.path.isfile(p):
                return p
    return ""


def script_cmd(script: str, args=None) -> list:
    """跑一个后台脚本的命令行。

    顺序：本机 pythonw.exe（无窗口，最稳）→ 打包版 `exe --run-script 脚本`
    → 当前解释器。绝对不能裸用打包后的 exe 当 python，那等于再开一个程序窗口。
    """
    args = [str(a) for a in (args or [])]
    pyw = _venv_pythonw()
    if pyw:
        return [pyw, "-X", "utf8", "-u", script] + args
    if is_frozen():
        return [sys.executable or "", "--run-script", script] + args
    return [sys.executable or "", "-X", "utf8", "-u", script] + args


def _hidden_kw(kw: dict, detached: bool = True, command=None) -> dict:
    # Windows 的 pythonw 会按活动代码页选择管道编码。上层统一按 UTF-8
    # 读取后台日志，因此必须在每个隐藏子进程入口固定 Python 的 I/O 编码。
    # 复制调用方传入的环境，避免给其字典或当前进程环境留下副作用。
    env = dict(kw.get("env") or os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    kw["env"] = env
    if os.name == "nt":
        kw = no_console.hidden_options(command, kw, detached=detached)
    return kw


def run_hidden(cmd, detached: bool = True, **kw):
    return subprocess.run(cmd, **_hidden_kw(kw, detached=detached, command=cmd))


def popen_hidden(cmd, detached: bool = True, **kw):
    return subprocess.Popen(cmd, **_hidden_kw(kw, detached=detached, command=cmd))


def python_run(script: str, args=None, **kw):
    """用无控制台的 python 跑一个脚本（分析、下载等后台任务）。"""
    return run_hidden(script_cmd(script, args), **kw)

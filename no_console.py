# -*- coding: utf-8 -*-
"""全局静默子进程：避免 ffmpeg / ffprobe / llama 调用时闪 cmd 窗口。

做法：在程序启动最早期把 subprocess.Popen / run 包一层，
Windows 下自动补隐藏窗口设置。只有 FFmpeg / ffprobe / llama 等媒体
工具默认脱离控制台；Python、PowerShell、cmd 等普通子进程只隐藏窗口，
避免 DETACHED_PROCESS 改变其信号和标准句柄语义。
任何模块（index/autocut/export/proofread/llm_models…）后续调用都会生效，
不用逐个文件去改。
"""
from __future__ import annotations

import os
import ntpath
import subprocess

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
PROCESS_FLAGS = CREATE_NO_WINDOW | DETACHED_PROCESS
_installed = False


def is_command_shell(command, *, executable=None, shell=False) -> bool:
    """Recognize explicit shells and Windows batch files without executing them."""
    if shell:
        return True
    value = executable
    if value is None:
        if isinstance(command, (list, tuple)):
            value = command[0] if command else ""
        else:
            value = command
    if value is None:
        return False
    value = os.fsdecode(value).lstrip()
    # Popen also accepts a Windows command-line string, including quoted paths.
    if value.startswith('"'):
        value = value[1:].split('"', 1)[0]
    elif executable is None and not isinstance(command, (list, tuple)):
        value = value.split(None, 1)[0] if value else ""
    name = ntpath.basename(value).lower()
    return (name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe"}
            or name.endswith((".bat", ".cmd")))


def _command_name(command, *, executable=None) -> str:
    value = executable
    if value is None:
        if isinstance(command, (list, tuple)):
            value = command[0] if command else ""
        else:
            value = command
    if value is None:
        return ""
    value = os.fsdecode(value).lstrip()
    if value.startswith('"'):
        value = value[1:].split('"', 1)[0]
    elif executable is None and not isinstance(command, (list, tuple)):
        value = value.split(None, 1)[0] if value else ""
    return ntpath.basename(value).lower()


def is_detached_media_tool(command, *, executable=None) -> bool:
    """Return whether the global wrapper may safely detach this executable."""
    name = _command_name(command, executable=executable)
    return (name in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}
            or name.startswith("llama-")
            or name.startswith("llama_"))


def hidden_options(command, options=None, *, detached=None):
    """Apply the shared Windows hiding policy while preserving other options."""
    kw = dict(options or {})
    if os.name != "nt":
        return kw
    flags = int(kw.get("creationflags") or 0) | CREATE_NO_WINDOW
    if is_command_shell(command, executable=kw.get("executable"),
                        shell=kw.get("shell", False)):
        # Windows PowerShell 5 may exit 0 without executing its script when
        # detached, even if stdin/stdout are piped.  Clear an inherited flag too.
        flags &= ~DETACHED_PROCESS
    elif detached is True or (detached is None and is_detached_media_tool(
            command, executable=kw.get("executable"))):
        # Explicit background helpers and known media tools may detach safely.
        flags |= DETACHED_PROCESS
    else:
        # The global wrapper must not detach arbitrary Python/helper processes.
        # They can need normal console control and inherited-handle semantics.
        flags &= ~DETACHED_PROCESS
    kw["creationflags"] = flags
    info = kw.get("startupinfo")
    if info is None:
        info = subprocess.STARTUPINFO()
        kw["startupinfo"] = info
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return kw


def install() -> bool:
    global _installed
    if _installed or os.name != "nt":
        return False
    _installed = True

    orig_popen = subprocess.Popen
    orig_run = subprocess.run

    def _flags(args, kw):
        command = args[0] if args else kw.get("args")
        return hidden_options(command, kw, detached=None)

    class _Popen(orig_popen):          # type: ignore[misc]
        def __init__(self, *a, **kw):
            super().__init__(*a, **_flags(a, kw))

    def _run(*a, **kw):
        return orig_run(*a, **_flags(a, kw))

    subprocess.Popen = _Popen            # type: ignore[assignment]
    subprocess.run = _run                # type: ignore[assignment]
    return True


def main():
    install()
    print("已启用：子进程不弹窗（CREATE_NO_WINDOW）")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""SmartCut 智能剪辑助手 · 指定目录安装器

把整套程序复制到你选的目录，然后在那个目录里建环境、装依赖、下 ffmpeg、
建桌面快捷方式。装完双击桌面「SmartCut 智能剪辑助手」就能用。

用法：
    python 安装器.py                                  # 图形界面
    python 安装器.py --dir "D:\\Apps\\SmartCut" --silent
    python 安装器.py --dir "D:\\Apps\\SmartCut" --silent --no-setup   # 只复制文件

选项：
    --dir PATH      安装到哪（默认 我的文档\\SmartCut 智能剪辑助手）
    --silent        不弹界面，命令行直接装
    --no-setup      只复制文件，不建环境（排查用）
    --no-bin        不复制 bin（ffmpeg），让安装器现下
    --no-shortcut   不建桌面快捷方式
    --launch        装完直接启动
    --force         目标目录里已经有东西也照装
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from product_info import (FULL_FEATURE_DESIGNER_URL, ORIGINAL_CREATOR_URL,
                          PRODUCT_NAME, VERSION, credits_plain_text)

BASE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = PRODUCT_NAME
PACKAGED_EXES = ("SmartCut 智能剪辑助手.exe", "智能剪辑助手.exe")

# 这些是本机运行产物/环境，不该复制给别人
SKIP_DIRS = {"models", "logs", "data", "dist", "build", "build_tools", "发布版", "工作目录",
             "__pycache__", "cache_learn", ".git", ".idea", "cache", ".pytest_cache",
             "_诊断"}
PRIVATE_CONFIGS = {"conf/settings.json", "conf/llm.json",
                   "conf/bgm.json", "conf/director_memory.json"}
REQUIRED_PROGRAM_FILES = (
    "app_main.py", "cards_ui.py", "index.py", "autocut.py", "dedupe.py", "bgm.py",
    "installer.py", "requirements.txt", "constraints-runtime.txt", "product_info.py",
    "README.md", "NOTICE.md", "启动SmartCut.bat", "brains/__init__.py",
    "brains/audio_brain.py", "conf/decision.json", "conf/coords_matrix.json",
    "conf/spec_manifest.json", "conf/editor_model.json",
)
SKIP_FILES = {".installed", ".writable",
              # 开发用，不复制给第三方（本机装包源目录里有这些）
              "dev_finish.py", "make_release.py", "selftest_track.py",
              "待改记录.md", "封存说明.md", "版本.txt"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log")


def _selected_offline_model(src):
    """Return (tier, filename) without copying private model configuration."""
    tiers = {
        "fast": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "smart": "Qwen3-8B-Q4_K_M.gguf",
        "large": "Qwen3-14B-Q4_K_M.gguf",
    }
    try:
        with open(os.path.join(src, "conf", "llm.json"),
                  encoding="utf-8") as handle:
            cfg = json.load(handle) or {}
        tier = str(cfg.get("active_tier") or
                   (cfg.get("tasks") or {}).get("paragraph") or "").lower()
        filename = str((cfg.get("models") or {}).get(tier) or tiers.get(tier) or "")
        if tier in tiers and filename and os.path.isfile(
                os.path.join(src, "models", "llm", filename)):
            return tier, filename
    except (OSError, ValueError, TypeError):
        pass
    present = []
    folder = os.path.join(src, "models", "llm")
    for tier, filename in tiers.items():
        if os.path.isfile(os.path.join(folder, filename)):
            present.append((tier, filename))
    return present[0] if len(present) == 1 else ("", "")


def log(msg=""):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 复制文件
# --------------------------------------------------------------------------
def packaged_executables(src):
    """Validate the optional onedir application before copying or launching it."""
    exes = [name for name in PACKAGED_EXES
            if os.path.isfile(os.path.join(src, name))]
    if not exes:
        return []
    internal = os.path.join(src, "_internal")
    if not os.path.isdir(internal):
        raise OSError("打包程序缺少配套 _internal，请重新下载完整安装包")
    python_dlls = [name for name in os.listdir(internal)
                   if name.lower().startswith("python")
                   and name.lower().endswith(".dll")
                   and name.lower() != "python3.dll"]
    required = exes + [os.path.join("_internal", "base_library.zip")]
    if not python_dlls:
        raise OSError("打包程序的 _internal 缺少 Python DLL")
    required += [os.path.join("_internal", name) for name in python_dlls]
    for relative in required:
        path = os.path.join(src, relative)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise OSError("打包程序文件缺失或为空：%s" % relative)
    return exes


def scan_package(src=None, with_bin=True, with_llama=True,
                 with_runtime=True, with_models=False):
    """列出要复制哪些文件（相对路径）。

    bin\\llama（本地大模型推理器，含 CUDA 运行库）**默认复制**：
    本地模式的语义分析、编剧和导演会使用它；API 模式可按需省略。
    装包体积换质量，想省空间再加 --no-llama。
    """
    src = src or BASE
    packaged = packaged_executables(src)
    _offline_tier, offline_model = _selected_offline_model(src) if with_models else ("", "")
    out = []
    for root, dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src)
        parts = rel_root.split(os.sep)
        # 只跳过顶层的 models/logs/dist… 这些目录。
        # 以前是按名字在任意层级跳过，于是 ctranslate2/models/ 这种
        # 依赖里的子包也被跳过，装出来的环境 import ctranslate2 直接崩。
        if parts == [""] or parts == ["."]:
            # with_models=True 时 models 不能被顶层过滤掉（离线版要靠它）
            _skip = SKIP_DIRS - ({"models"} if with_models else set())
            dirs[:] = [d for d in dirs if d not in _skip
                       and (not d.startswith("_internal")
                            or (d == "_internal" and packaged))
                       and "备份" not in d]
        elif parts[0] != "_internal":
            dirs[:] = [d for d in dirs if d != "__pycache__"]
        # 同理：with_models=True 时 models 这个顶层目录也要放行
        if parts[0] in (SKIP_DIRS - ({"models"} if with_models else set())):
            continue
        if not with_bin and parts[0] == "bin":
            continue
        if not with_llama and parts[:2] == ["bin", "llama"]:
            continue
        if not with_runtime and parts[0] in ("runtime", ".venv"):
            continue
        if not with_models and parts[0] == "models":
            continue
        for f in files:
            if (with_models and offline_model and parts[:2] == ["models", "llm"]
                    and f.lower().endswith(".gguf")
                    and f.lower() != offline_model.lower()):
                continue
            if parts[0] != "_internal" and (f in SKIP_FILES or f.endswith(SKIP_SUFFIX)
                                                or "备份" in f):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, src)
            if rel.startswith(".."):
                continue
            normalized = rel.replace("\\", "/")
            if normalized in PRIVATE_CONFIGS or normalized.startswith("conf/editor_model_"):
                continue
            out.append(rel)
    return sorted(out)


def _replace_packaged_files(target, staged, exes, progress):
    """Replace the entire build and retain the old one for recovery."""
    names = ("_internal",) + PACKAGED_EXES
    old_names = [name for name in names
                 if os.path.lexists(os.path.join(target, name))]
    backup = tempfile.mkdtemp(prefix="_安装备份_", dir=target) if old_names else None
    moved_old, moved_new = [], []
    try:
        for name in old_names:
            os.replace(os.path.join(target, name), os.path.join(backup, name))
            moved_old.append(name)
        for name in (["_internal"] + exes if exes else []):
            os.replace(os.path.join(staged, name), os.path.join(target, name))
            moved_new.append(name)
    except OSError as exc:
        # Return new files to staging before restoring the old matched set.
        try:
            for name in reversed(moved_new):
                os.replace(os.path.join(target, name), os.path.join(staged, name))
            for name in reversed(moved_old):
                os.replace(os.path.join(backup, name), os.path.join(target, name))
        except OSError as restore_error:
            raise OSError("替换失败且自动恢复未完成；旧版备份在 %s：%s" %
                          (backup, restore_error)) from exc
        raise OSError("替换运行程序失败，旧版已恢复；请关闭 SmartCut 后重试：%s" % exc) from exc
    if backup:
        progress("  旧版 EXE 与运行库已成套备份：%s" % backup)


def copy_package(target, with_bin=True, with_llama=True, with_runtime=True,
                 with_models=False, progress=log):
    files = scan_package(with_bin=with_bin, with_llama=with_llama,
                         with_runtime=with_runtime, with_models=with_models)
    exes = packaged_executables(BASE)
    os.makedirs(target, exist_ok=True)
    total = 0
    with tempfile.TemporaryDirectory(prefix="_安装暂存_", dir=target) as staged:
        for i, rel in enumerate(files, 1):
            s = os.path.join(BASE, rel)
            packaged = rel in exes or rel.split(os.sep)[0] == "_internal"
            d = os.path.join(staged if packaged else target, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            try:
                shutil.copy2(s, d)
                if os.path.getsize(s) != os.path.getsize(d):
                    raise OSError("复制后文件大小不一致")
                total += os.path.getsize(s)
            except OSError as exc:
                raise OSError("复制失败：%s（%s）" % (rel, exc)) from exc
            if i % 40 == 0 or i == len(files):
                progress("  已复制 %d/%d 个文件（%.0f MB）"
                         % (i, len(files), total / 1048576))
        if exes:
            packaged_executables(staged)
        _replace_packaged_files(target, staged, exes, progress)
    if with_models:
        tier, filename = _selected_offline_model(BASE)
        if tier and filename:
            tasks = {name: tier for name in (
                "proofread", "subtitle", "segment", "paragraph", "complete",
                "analyst", "writer", "editor", "director")}
            payload = {
                "models": {tier: filename},
                "active_tier": tier,
                "tasks": tasks,
                "escalate": False,
                "disable_thinking": True,
            }
            conf_dir = os.path.join(target, "conf")
            os.makedirs(conf_dir, exist_ok=True)
            with open(os.path.join(conf_dir, "llm.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            progress("  本地模型：只安装并启用 %s" % filename)
    return len(files), total


# --------------------------------------------------------------------------
# 在目标目录里跑安装器的建环境步骤
# --------------------------------------------------------------------------
def run_setup(target, extra_args=None, progress=log):
    inst = os.path.join(target, "installer.py")
    if not os.path.isfile(inst):
        progress("  [X] 目标目录里没有 installer.py，复制不完整")
        return False
    if bool(getattr(sys, "frozen", False)):
        args = [sys.executable, "--run-script", inst, "--no-launch"]
    else:
        args = [sys.executable, inst, "--no-launch"]
    args += list(extra_args or [])
    progress("  开始建环境、装依赖（首次约 3~10 分钟，需要联网）…")
    proc = subprocess.Popen(args, cwd=target, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            progress("  " + line)
            tail.append(line)
            tail = tail[-40:]
    rc = proc.wait()
    if rc != 0:
        progress("  [X] 安装脚本退出码 %d" % rc)
        return False
    return True


def has_runtime(target):
    """目标目录里有没有可用的自带运行时。"""
    py = os.path.join(target, "runtime", "python", "python.exe")
    venv = os.path.join(target, ".venv", "Scripts", "python.exe")
    return os.path.isfile(py) and os.path.isfile(venv)


def fix_runtime(target, progress=log):
    r"""把 venv 的 home 指到目标目录里的 runtime\python，并自检关键包。"""
    import subprocess
    cfg = os.path.join(target, ".venv", "pyvenv.cfg")
    home = os.path.join(target, "runtime", "python")
    txt = ("home = %s\ninclude-system-site-packages = false\n"
           "version = 3.14.4\nexecutable = %s\ncommand = portable\n"
           % (home, os.path.join(home, "python.exe")))
    try:
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(txt)
        progress("  运行时已指向：%s" % home)
    except OSError as exc:
        progress("  [X] 写不了 pyvenv.cfg：%s" % exc)
        return False
    py = os.path.join(target, ".venv", "Scripts", "python.exe")
    code = ("import faster_whisper, ctranslate2, PySide6, cv2, numpy;"
            "print('ok')")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180,
                           cwd=target,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        progress("  [X] 运行时自检跑不起来：%s" % exc)
        return False
    if r.returncode != 0 or "ok" not in (r.stdout or ""):
        progress("  [X] 运行时自检失败：%s" % ((r.stderr or r.stdout or "")[-300:]))
        return False
    progress("  运行时自检通过（关键包都能导入）")
    return True


def verify(target):
    """装完自查：关键文件在不在。"""
    miss = [f for f in REQUIRED_PROGRAM_FILES
            if not os.path.isfile(os.path.join(target, f))]
    try:
        packaged_executables(target)
    except OSError as exc:
        miss.append(str(exc))
    pyw = os.path.join(target, ".venv", "Scripts", "pythonw.exe")
    py = os.path.join(target, ".venv", "Scripts", "python.exe")
    return miss, (os.path.isfile(py) and os.path.isfile(pyw))


def launch_installed(target):
    exes = packaged_executables(target)
    if exes:
        args = [os.path.join(target, exes[0])]
    else:
        args = [os.path.join(target, ".venv", "Scripts", "pythonw.exe"),
                os.path.join(target, "app_main.py")]
    subprocess.Popen(args, cwd=target)


# --------------------------------------------------------------------------
# 主流程（界面和命令行共用）
# --------------------------------------------------------------------------
def install(target, with_bin=True, do_setup=True, no_shortcut=False,
            launch=False, force=False, with_llama=True, no_model=False,
            with_runtime=True, with_models=None, progress=log):
    target = os.path.abspath(os.path.expanduser(target))
    missing = [f for f in REQUIRED_PROGRAM_FILES
               if not os.path.isfile(os.path.join(BASE, f))]
    if missing:
        progress("[X] 安装包不完整，缺少：%s。请重新下载完整项目。" % "、".join(missing))
        return 1
    try:
        packaged_executables(BASE)
    except OSError as exc:
        progress("[X] 安装包不完整：%s" % exc)
        return 1
    if os.path.normcase(os.path.abspath(BASE)) == os.path.normcase(target):
        progress("[X] 安装目录不能就是当前这个目录（就地安装请运行 setup_windows.ps1）")
        return 1
    if os.path.isdir(target) and os.listdir(target) and not force:
        if os.path.isfile(os.path.join(target, "cards_ui.py")) or \
                os.path.isfile(os.path.join(target, "启动SmartCut.bat")):
            progress("[X] %s 里已经有一套程序了。\n"
                     "    要覆盖安装加 --force；要换目录请重新指定。" % target)
            return 1

    progress("%s %s" % (APP_NAME, VERSION))
    progress(credits_plain_text())
    progress("安装目录：%s" % target)
    progress("")
    # 包里带了模型（离线版）就自动一起装，不用额外加参数；
    # 标准包没有 models 目录，自动为 False（首次识别时再下）。
    if with_models is None:
        _m = os.path.join(BASE, "models")
        with_models = os.path.isdir(_m) and any(
            os.path.isfile(os.path.join(r, f))
            for r, _d, fs in os.walk(_m) for f in fs)

    progress("[1/3] 复制程序文件 …")
    try:
        n, size = copy_package(target, with_bin=with_bin, with_llama=with_llama,
                              with_runtime=with_runtime, with_models=with_models,
                              progress=progress)
    except OSError as exc:
        progress("[X] 安装未完成：%s" % exc)
        return 1
    progress("  共 %d 个文件，%.0f MB" % (n, size / 1048576))

    if with_runtime and has_runtime(target):
        progress("")
        progress("[2/3] 使用自带的 Python 运行时（本机不需要装 Python）…")
        if not fix_runtime(target, progress=progress):
            progress("    运行时不可用，改为尝试用本机 Python 建环境…")
            do_setup = True
        else:
            do_setup = False

    if do_setup:
        progress("")
        progress("[2/3] 建运行环境 …")
        extra = []
        if no_shortcut:
            extra.append("--no-shortcut")
        if no_model:
            extra.append("--no-model")
        if not run_setup(target, extra, progress):
            progress("")
            progress("[X] 环境没装好。可以再运行一次（会接着装），或看 logs 里的日志。")
            return 1
    else:
        progress("")
        progress("[2/3] 跳过建环境（--no-setup）")

    progress("")
    progress("[3/3] 自查 …")
    miss, has_py = verify(target)
    if miss:
        progress("  [X] 缺少文件：%s" % "、".join(miss))
        return 1
    has_packaged = bool(packaged_executables(target))
    progress("  关键文件齐全；运行环境：%s" %
             ("打包运行时已就绪" if has_packaged else "已就绪" if has_py else "未建"))

    progress("")
    progress("=" * 58)
    progress("  安装完成：%s" % target)
    progress("  以后双击桌面「%s」，或这个目录里的 启动SmartCut.bat" % APP_NAME)
    if has_py:
        progress("  自检：%s selftest_cut.py" %
                 os.path.join(target, ".venv", "Scripts", "python.exe"))
    progress("=" * 58)
    if launch and (has_py or has_packaged):
        launch_installed(target)
    return 0


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------
def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    default = os.path.join(os.path.expanduser("~"), "Documents", APP_NAME)

    root = tk.Tk()
    root.title(APP_NAME + " · 安装")
    root.geometry("800x570")
    root.minsize(720, 520)

    tk.Label(root, text=APP_NAME, font=("Microsoft YaHei", 16, "bold")).pack(
        anchor="w", padx=16, pady=(14, 2))
    tk.Label(root, text="选择安装目录，把整套程序装到这里（以后从这里启动）。",
             fg="#555").pack(anchor="w", padx=16)
    import webbrowser
    tk.Label(root, text="版本 %s · Copyright © 2026 瓜子神教大主教、不无聊的子枫哥" % VERSION,
             fg="#666").pack(anchor="w", padx=16, pady=(4, 0))
    for text, url in (("软件初版、创意提供：瓜子神教大主教", ORIGINAL_CREATOR_URL),
                      ("后续全功能完善、所有剪辑模型结构提供者：不无聊的子枫哥", FULL_FEATURE_DESIGNER_URL)):
        link = tk.Label(root, text=text, fg="#2464a3", cursor="hand2")
        link.pack(anchor="w", padx=16)
        link.bind("<Button-1>", lambda _event, address=url: webbrowser.open(address))
    tk.Label(root, text="AI辅助开发：DeepSeek、OpenAI Codex", fg="#666").pack(
        anchor="w", padx=16)

    row = tk.Frame(root)
    row.pack(fill="x", padx=16, pady=10)
    var = tk.StringVar(value=default)
    tk.Label(row, text="安装目录").pack(side="left")
    ent = tk.Entry(row, textvariable=var)
    ent.pack(side="left", fill="x", expand=True, padx=8)

    def browse():
        d = filedialog.askdirectory(title="选择安装目录", initialdir=var.get() or "C:\\")
        if d:
            var.set(os.path.join(d, APP_NAME) if os.path.basename(d).lower()
                    != APP_NAME else d)

    tk.Button(row, text="选择…", command=browse, width=8).pack(side="left")

    opt = tk.Frame(root)
    opt.pack(fill="x", padx=16)
    bin_var = tk.BooleanVar(value=os.path.isdir(os.path.join(BASE, "bin")))
    shortcut_var = tk.BooleanVar(value=True)
    launch_var = tk.BooleanVar(value=True)
    tk.Checkbutton(opt, text="附带 ffmpeg（不勾选则安装时下载）",
                   variable=bin_var).pack(side="left")
    tk.Checkbutton(opt, text="创建桌面快捷方式",
                   variable=shortcut_var).pack(side="left", padx=12)
    tk.Checkbutton(opt, text="装完直接打开",
                   variable=launch_var).pack(side="left")

    logbox = tk.Text(root, height=16, wrap="word", state="disabled",
                     bg="#111", fg="#ddd", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True, padx=16, pady=10)

    btn = tk.Button(root, text="开始安装", height=2,
                    bg="#2d7d46", fg="white", font=("Microsoft YaHei", 11, "bold"))
    btn.pack(fill="x", padx=16, pady=(0, 14))

    q = queue.Queue()

    def put(msg):
        q.put(str(msg))

    def pump():
        try:
            while True:
                msg = q.get_nowait()
                logbox.configure(state="normal")
                logbox.insert("end", msg + "\n")
                logbox.see("end")
                logbox.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(120, pump)

    def start():
        target = var.get().strip()
        if not target:
            messagebox.showwarning("还没选目录", "先选一个安装目录。")
            return
        btn.configure(state="disabled", text="正在安装…")
        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.configure(state="disabled")

        def work():
            rc = install(target, with_bin=bin_var.get(),
                         do_setup=True, no_shortcut=not shortcut_var.get(),
                         launch=False, force=False, progress=put)
            put("")
            put("完成，退出码 %d" % rc)
            def done():
                btn.configure(state="normal", text="开始安装")
                if rc == 0:
                    if messagebox.askyesno("安装完成", "装好了。\n现在打开 SmartCut 吗？"):
                        launch_installed(os.path.abspath(target))
                else:
                    messagebox.showerror("安装没完成", "看下面的日志，或再运行一次接着装。")
            root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    btn.configure(command=start)
    pump()
    root.mainloop()
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=APP_NAME + " · 指定目录安装")
    ap.add_argument("--dir", default=None, help="安装目录")
    ap.add_argument("--silent", action="store_true", help="不弹界面")
    ap.add_argument("--no-setup", action="store_true", help="只复制文件")
    ap.add_argument("--no-bin", action="store_true", help="不复制 ffmpeg")
    ap.add_argument("--with-models", action="store_true",
                    help="连带复制 models（语音模型 + 大模型，约 11 GB）")
    ap.add_argument("--no-runtime", action="store_true",
                    help="不复制自带 Python 运行时（改用本机 Python 建环境）")
    ap.add_argument("--no-llama", action="store_true",
                    help="不复制本地大模型推理器（API 模式可省略，本地模式需要它）")
    ap.add_argument("--no-model", action="store_true",
                    help="不预先下载语音模型（第一次识别时再下）")
    ap.add_argument("--no-shortcut", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    if not args.silent and not args.dir:
        return gui()
    target = args.dir or os.path.join(os.path.expanduser("~"), "Documents", APP_NAME)
    return install(target, with_bin=not args.no_bin,
                   do_setup=not args.no_setup, no_shortcut=args.no_shortcut,
                   launch=args.launch, force=args.force,
                   with_llama=not getattr(args, "no_llama", False),
                   no_model=args.no_model,
                   with_runtime=not getattr(args, "no_runtime", False),
                   with_models=(True if getattr(args, "with_models", False) else None))


if __name__ == "__main__":
    sys.exit(main())

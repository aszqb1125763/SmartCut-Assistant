# -*- coding: utf-8 -*-
"""一键打包成单进程 exe（目录版，启动快）：智能剪辑助手.exe

特点：
  · 主进程名就是「智能剪辑助手」，任务管理器不再出现 python/pythonw
  · 无控制台窗口（--noconsole）
  · 带图标、版本信息
  · 依赖（模型 / bin / 工作目录 / conf）留在外面，不重复打包，更新代码不用重打
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
OUT = os.path.join(BASE, "发布版")


def run(cmd):
    print(">", " ".join(cmd), flush=True)
    env = dict(os.environ)
    # 构建器可能从 IDE/Codex 的 PATH 继承 Poppler 等工具目录。里面有一个
    # 同名的 ICU 78 `icuuc.dll`，PyInstaller 会误判成 Qt6Core 的依赖并
    # 收进 _internal；运行时它抢在 Windows 系统 ICU 前加载，随后报
    # `ucnv_open` 入口不存在。构建环境必须排除这些外部工具链目录。
    path_parts = []
    for item in (env.get("PATH") or "").split(os.pathsep):
        low = item.lower().replace("/", "\\")
        if "\\.cache\\codex-runtimes\\" in low:
            continue
        path_parts.append(item)
    env["PATH"] = os.pathsep.join(path_parts)
    r = subprocess.run(cmd, cwd=BASE, env=env)
    return r.returncode


def _smoke_test_qt(exe: str) -> bool:
    """用刚构建的 EXE 导入 Qt，防止再次发布无法启动的产物。"""
    work = os.path.join(BASE, "build_tools", "build")
    os.makedirs(work, exist_ok=True)
    script = os.path.join(work, "_packaged_qt_smoke.py")
    marker = os.path.join(work, "_packaged_qt_smoke.ok")
    for path in (script, marker):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(
            "from PySide6 import QtCore, QtWidgets\n"
            "with open(%r, 'w', encoding='utf-8') as handle:\n"
            "    handle.write(QtCore.__version__)\n" % marker
        )
    try:
        proc = subprocess.run(
            [exe, "--run-script", script], cwd=os.path.dirname(exe),
            timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = proc.returncode == 0 and os.path.isfile(marker)
        print("打包版 Qt 冒烟：", "通过" if ok else "失败",
              "退出码=", proc.returncode)
        return ok
    except subprocess.TimeoutExpired:
        print("打包版 Qt 冒烟：超时")
        return False
    finally:
        for path in (script, marker):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def _prune_unused_qt_payload(internal: str) -> list[str]:
    """Remove Qt modules/plugins which SmartCut never imports.

    PyInstaller's Qt hook may collect optional WebEngine, PDF and virtual
    keyboard payloads even when the application only uses Widgets and
    Multimedia.  Those files add hundreds of megabytes and a large extra
    dependency surface.  Match by basename so the cleanup remains valid when
    PySide changes its internal directory layout.
    """
    blocked_files = {
        "Qt6WebEngineCore.dll", "Qt6WebEngineQuick.dll",
        "Qt6WebEngineWidgets.dll", "qpdf.dll",
        "qtvirtualkeyboardplugin.dll",
    }
    removed = []
    for root, dirs, files in os.walk(internal, topdown=False):
        normalized = root.replace("/", "\\").lower()
        for filename in files:
            if filename in blocked_files or "qtwebengine" in filename.lower():
                path = os.path.join(root, filename)
                os.remove(path)
                removed.append(os.path.relpath(path, internal))
        for dirname in dirs:
            path = os.path.join(root, dirname)
            relative = os.path.relpath(path, internal).replace("/", "\\").lower()
            if (dirname.lower() == "qtwebengineprocess"
                    or relative.endswith("translations\\qtwebengine_locales")
                    or (dirname.lower() == "resources" and "pyside6" in normalized)):
                shutil.rmtree(path, ignore_errors=True)
                removed.append(os.path.relpath(path, internal) + os.sep)
    return removed


def _check_qt_payload(internal: str) -> tuple[bool, list[str]]:
    """Verify the pruned onedir bundle still has the Qt pieces we use."""
    names = set()
    for root, _dirs, files in os.walk(internal):
        names.update(files)
    required = {"QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd",
                "QtMultimedia.pyd", "QtMultimediaWidgets.pyd"}
    forbidden = {"Qt6WebEngineCore.dll", "qpdf.dll",
                 "qtvirtualkeyboardplugin.dll"}
    issues = []
    missing = sorted(required - names)
    present = sorted(forbidden & names)
    if missing:
        issues.append("缺少 Qt 运行模块：" + "、".join(missing))
    if present:
        issues.append("仍包含未使用的 Qt 组件：" + "、".join(present))
    return not issues, issues


def main() -> int:
    if not os.path.isfile(PY):
        print("找不到 .venv python")
        return 2
    os.makedirs(OUT, exist_ok=True)

    # faster-whisper 的静音检测模型（silero_vad_v6.onnx）是包内数据文件，
    # PyInstaller 不会自动带；漏了的话打包版一进"正在识别说话内容"就
    # NoSuchFile，整条队列全失败（开发环境完全正常，11-22 踩过）。
    # 目标路径必须和 faster_whisper.utils.get_assets_path() 一致。
    fw_assets = os.path.join(BASE, ".venv", "Lib", "site-packages",
                             "faster_whisper", "assets")
    if not os.path.isfile(os.path.join(fw_assets, "silero_vad_v6.onnx")):
        print("找不到 faster_whisper\\assets\\silero_vad_v6.onnx：" + fw_assets)
        return 4

    cmd = [
        PY, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",                       # 无控制台
        "--name", NAME,
        "--icon", ICON,
        "--version-file", VER,
        "--distpath", OUT,
        "--workpath", os.path.join(BASE, "build_tools", "build"),
        "--specpath", os.path.join(BASE, "build_tools"),
        # 只带运行需要的东西；模型/bin/工作目录都在外面
        "--add-data", "%s%s%s" % (os.path.join(BASE, "assets"),
                                  os.pathsep, "assets"),
        "--add-data", "%s%s%s" % (fw_assets, os.pathsep,
                                  os.path.join("faster_whisper", "assets")),
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "PySide6.QtMultimediaWidgets",
        "--hidden-import", "faster_whisper",
        "--hidden-import", "ctranslate2",
        "--hidden-import", "av",
        "--hidden-import", "tokenizers",
        "--hidden-import", "huggingface_hub",
        "--hidden-import", "no_console",
        "--hidden-import", "winproc",
        "--hidden-import", "window_guard",
        "--hidden-import", "ui_theme",
        "--hidden-import", "ui_widgets",
        "--hidden-import", "ui_main_v2",
        "--hidden-import", "ui_queue",
        "--hidden-import", "ui_queuepanel",
        "--hidden-import", "ui_settings",
        "--hidden-import", "ui_dialogs",
        "--hidden-import", "ui_thumbs",
        "--hidden-import", "selfcheck",
        "--hidden-import", "seam_continuity",
        "--hidden-import", "paragraph_qc",
        # 主按钮和自动队列通过 autopilot 动态导入模型剪辑师；静态分析
        # 看不到这条分支，必须显式打入，否则开发版正常、EXE 才会报缺模块。
        "--hidden-import", "model_editor",
        # 自主剪辑：autoedit.py 是运行时用 --run-script 加载的，PyInstaller
        # 静态分析看不到它 import 的东西，必须显式列出来，否则 exe 里点
        # 「自主剪辑」会在 import asr / ui.llm_bridge 时 ModuleNotFoundError。
        "--hidden-import", "autoedit_qt",
        "--hidden-import", "ui_autoedit",
        "--hidden-import", "asr",
        "--hidden-import", "asr_hints",
        "--hidden-import", "neutral_text",
        "--hidden-import", "time_sense",
        "--hidden-import", "three_axis",
        "--hidden-import", "utterance",
        "--hidden-import", "ui.llm_bridge",
        "--hidden-import", "pypinyin",
        # 成片后处理也是 autoedit.py 运行时才 import 的，必须显式带进来，
        # 否则打包版一勾「烧字幕 / BGM」就 ModuleNotFoundError。
        "--hidden-import", "subtitles",
        "--hidden-import", "bgm",
        # 这些用不到，排除掉减小体积
        "--exclude-module", "matplotlib",
        "--exclude-module", "pandas",
        "--exclude-module", "scipy",
        "--exclude-module", "torch",
        "--exclude-module", "tkinter",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        # .venv 里曾为“改名 pythonw”方案装过 sitecustomize 启动钩子。
        # 正式 PyInstaller 入口已经由 app_main 负责，打进去会导致二次启动。
        "--exclude-module", "sitecustomize",
        os.path.join(BASE, "app_main.py"),
    ]
    rc = run(cmd)
    if rc != 0:
        print("打包失败，退出码", rc)
        return rc
    exe = os.path.join(OUT, NAME, NAME + ".exe")
    print("产物：", exe, "存在=", os.path.isfile(exe))
    vad = os.path.join(OUT, NAME, "_internal", "faster_whisper", "assets",
                       "silero_vad_v6.onnx")
    # 二次保险：Qt 6.12 在 Windows 上应加载 System32 的 ICU forwarder。
    # 若其他构建环境又误收了第三方同名库，明确移除并拒绝混合产物。
    internal = os.path.join(OUT, NAME, "_internal")
    removed_qt = _prune_unused_qt_payload(internal)
    if removed_qt:
        print("移除未使用 Qt 组件：%d 项" % len(removed_qt))
    qt_ok, qt_issues = _check_qt_payload(internal)
    for issue in qt_issues:
        print("Qt 载荷检查：", issue)
    removed_icu = []
    for filename in os.listdir(internal):
        low = filename.lower()
        if low == "icuuc.dll" or (low.startswith("icudt") and low.endswith(".dll")):
            os.remove(os.path.join(internal, filename))
            removed_icu.append(filename)
    if removed_icu:
        print("移除构建环境污染的 ICU：", ", ".join(sorted(removed_icu)))
    print("VAD 模型：", vad, "存在=", os.path.isfile(vad))
    bad_icu = os.path.join(internal, "icuuc.dll")
    if (not os.path.isfile(exe) or not os.path.isfile(vad)
            or os.path.exists(bad_icu) or not qt_ok):
        return 3
    if not _smoke_test_qt(exe):
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""把「智能剪辑助手.exe」做出来：它就是 Python 本体改名，直接启动界面。

原理：
  · 复制 .venv\\Scripts\\pythonw.exe → 同名目录下的 智能剪辑助手.exe
  · 在 site-packages 放一个 sitecustomize.py：当这个 exe 被双击（没有参数）时，
    自动切到程序根目录并调用 cards_ui.main()
这样任务管理器里的主进程就叫「智能剪辑助手」，不再出现 python / pythonw。
"""
from __future__ import annotations

import os
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(BASE, ".venv")
SCRIPTS = os.path.join(VENV, "Scripts")
EXE = os.path.join(SCRIPTS, "智能剪辑助手.exe")
SITE = os.path.join(VENV, "Lib", "site-packages")

# 1) 复制 pythonw.exe → 智能剪辑助手.exe
src = os.path.join(SCRIPTS, "pythonw.exe")
assert os.path.isfile(src), "找不到 pythonw.exe"
if not os.path.isfile(EXE) or os.path.getsize(EXE) != os.path.getsize(src):
    shutil.copy2(src, EXE)
print("1 已生成：", EXE, "%.1f MB" % (os.path.getsize(EXE) / 1e6))

# 2) sitecustomize.py：无参数启动时拉起界面
SC = '''# -*- coding: utf-8 -*-
"""由 install_exe.py 生成：让 智能剪辑助手.exe 双击即启动界面。

只在"本 exe 且没有任何命令行参数"时生效；子进程（index.py 等）不受影响。
"""
import os
import sys


def _smartcut_bootstrap():
    try:
        exe_path = os.path.abspath(sys.executable or "")
        exe = os.path.basename(exe_path)
        if not exe.lower().startswith("智能剪辑助手"):
            return
        # 这个钩子只属于旧的“.venv\\Scripts 里改名 pythonw”方案。
        # PyInstaller 正式版同名 exe 绝不能走这里，否则会再次执行自己。
        if os.path.basename(os.path.dirname(exe_path)).lower() != "scripts":
            return
        if len(sys.argv) > 1:
            return
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(sys.executable))))
        cards = os.path.join(root, "cards_ui.py")
        if not os.path.isfile(cards):
            return
        os.chdir(root)
        os.environ.setdefault("SMARTCUT_HOME", root)
        sys.argv = [cards, "-X", "utf8"]
        import cards_ui
        try:
            cards_ui.main()
        finally:
            os._exit(0)
    except SystemExit:
        raise
    except Exception:
        # 启动失败也把栈写进日志，别让窗口一闪就没
        try:
            import traceback
            with open(os.path.join(os.path.dirname(sys.executable),
                                   "..", "..", "logs", "exe_boot.log"),
                      "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\\n")
        except Exception:
            pass
        raise


_smartcut_bootstrap()
'''
sc_path = os.path.join(SITE, "sitecustomize.py")
old = ""
if os.path.isfile(sc_path):
    old = open(sc_path, encoding="utf-8", errors="replace").read()
if "_smartcut_bootstrap" not in old:
    with open(sc_path, "w", encoding="utf-8", newline="") as f:
        f.write(SC)
    print("2 已写 sitecustomize.py：", sc_path)
else:
    print("2 sitecustomize 已存在")

# 3) 子进程也优先用这个 exe（这样分析时的 python 子进程也显示成智能剪辑助手）
wp = os.path.join(BASE, "winproc.py")
w = open(wp, encoding="utf-8").read()
if "智能剪辑助手.exe" not in w:
    w = w.replace('''    base = os.path.dirname(exe)
    name = os.path.basename(exe).lower()
    if name.startswith("pythonw"):
        return exe
    cand = os.path.join(base, "pythonw.exe")
    return cand if os.path.isfile(cand) else exe''',
'''    base = os.path.dirname(exe)
    # 优先用改名后的主程序本体：任务管理器里也显示成「智能剪辑助手」
    app_exe = os.path.join(base, "智能剪辑助手.exe")
    if os.path.isfile(app_exe):
        return app_exe
    name = os.path.basename(exe).lower()
    if name.startswith("pythonw"):
        return exe
    cand = os.path.join(base, "pythonw.exe")
    return cand if os.path.isfile(cand) else exe''')
    open(wp, "w", encoding="utf-8", newline="").write(w)
    print("3 winproc 已优先使用 智能剪辑助手.exe")

print("完成")

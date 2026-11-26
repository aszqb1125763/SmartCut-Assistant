# -*- coding: utf-8 -*-
"""智能剪辑 · 界面：选一个视频，它自己剪。

子枫 2026-11-24：
    「把所有流程打乱重新安排，让他自己拥有相关功能，让他自己去剪辑一个试试看。」

界面只做三件事：选文件 → 看它跑 → 看成片。
所有判断都在 autoedit.py 里（模型自己决定删哪段）。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

APP = os.path.dirname(os.path.abspath(__file__))
BASE = APP
# 用能跑 faster_whisper 的解释器：优先 .venv（有 ctranslate2），
# 其次是启动本界面的解释器；runtime\python 没装转写包，不能用来跑 autoedit.py。
_VENV = os.path.join(BASE, ".venv", "Scripts", "python.exe")
PY = _VENV if os.path.isfile(_VENV) else sys.executable
DATA = os.path.join(BASE, "data")

BG = "#f4f5f7"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("智能剪辑 · 它自己剪")
        self.root.geometry("900x640")
        self.root.configure(bg=BG)
        self.proc = None
        self.lines = queue.Queue()
        self.output = None
        self._build()
        self.root.after(120, self._pump)

    def _build(self):
        top = tk.Frame(self.root, bg="#ffffff")
        top.pack(fill="x")
        tk.Label(top, text="智能剪辑", bg="#ffffff", fg="#222",
                 font=("微软雅黑", 13, "bold")).pack(side="left", padx=14,
                                                     pady=10)
        self.status = tk.Label(top, text="选一个视频开始", bg="#ffffff",
                               fg="#888", font=("微软雅黑", 9))
        self.status.pack(side="left")

        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=12, pady=8)
        tk.Button(bar, text="选择视频…", command=self.pick,
                  font=("微软雅黑", 10), relief="flat", bg="#e9eaee"
                  ).pack(side="left")
        tk.Label(bar, text="音轨", bg=BG, font=("微软雅黑", 9)).pack(
            side="left", padx=(12, 4))
        self.track = ttk.Combobox(bar, width=4, state="readonly",
                                  values=["0", "1", "2", "3", "4", "5"])
        self.track.current(0)
        self.track.pack(side="left")
        tk.Label(bar, text="目标秒数", bg=BG, font=("微软雅黑", 9)).pack(
            side="left", padx=(12, 4))
        self.target = tk.Entry(bar, width=6, font=("微软雅黑", 9))
        self.target.pack(side="left")
        self.btn = tk.Button(bar, text="▶ 开始剪", command=self.run,
                             font=("微软雅黑", 10, "bold"), relief="flat",
                             bg="#07c160", fg="#fff", state="disabled")
        self.btn.pack(side="left", padx=12)
        self.open_btn = tk.Button(bar, text="打开成片", command=self.open_file,
                                  font=("微软雅黑", 9), relief="flat",
                                  bg="#e9eaee", state="disabled")
        self.open_btn.pack(side="left")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.text = tk.Text(body, wrap="word", font=("Consolas", 10),
                            bg="#ffffff", relief="flat")
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.path = None
        self.log("流程：听（转写）→ 看（章节）→ 写剧本 → 出剪辑单 → 代码执行 → 回执")
        self.log("模型自己决定删哪段，界面上不设任何剪辑参数。")

    # -- 日志 ---------------------------------------------------------------
    def log(self, text):
        self.lines.put(text)

    def _pump(self):
        try:
            while True:
                line = self.lines.get_nowait()
                self.text.insert("end", line + "\n")
                self.text.see("end")
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._pump)
        except Exception:  # noqa: BLE001
            pass

    # -- 选择 ---------------------------------------------------------------
    def pick(self):
        path = filedialog.askopenfilename(
            title="选一个视频",
            filetypes=[("视频", "*.mp4 *.mkv *.mov *.avi"), ("全部", "*.*")])
        if not path:
            return
        self.path = path
        self.status.configure(text=os.path.basename(path))
        self.btn.configure(state="normal")
        self.log("\n已选：%s" % path)

    # -- 跑 -----------------------------------------------------------------
    def run(self):
        if not self.path:
            return
        if self.proc and self.proc.poll() is None:
            return
        cmd = [PY]
        # 打包版没有 python.exe，PY 就是「智能剪辑助手.exe」本身：
        # 不显式带 --run-script 的话，子进程会再开一个主界面而不是跑剪辑脚本。
        if "python" not in os.path.basename(PY).lower():
            cmd.append("--run-script")
        cmd += [os.path.join(APP, "autoedit.py"), self.path,
                "--track", self.track.get()]
        if self.target.get().strip():
            cmd += ["--target", self.target.get().strip()]
        self.btn.configure(state="disabled", text="剪辑中…")
        self.log("\n" + "=" * 60)

        def worker():
            self.proc = subprocess.Popen(
                cmd, cwd=BASE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace")
            for line in self.proc.stdout:
                self.lines.put(line.rstrip())
            self.proc.wait()
            self.lines.put("__DONE__")

        threading.Thread(target=worker, daemon=True).start()
        self._watch_done()

    def _watch_done(self):
        if self.proc and self.proc.poll() is None:
            self.root.after(300, self._watch_done)
            return
        self.btn.configure(state="normal", text="▶ 开始剪")
        result = os.path.join(DATA, "autoedit_last.json")
        if os.path.isfile(result):
            import json
            try:
                with open(result, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.output = data.get("out")
                self.open_btn.configure(state="normal")
                self.log("成片：%s" % self.output)
            except (OSError, ValueError):
                pass
        self.status.configure(text="完成")

    def open_file(self):
        if self.output and os.path.isfile(self.output):
            os.startfile(self.output)


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

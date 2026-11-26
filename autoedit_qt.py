# -*- coding: utf-8 -*-
"""自主剪辑 · 正式时间轴岗位工作流入口。

界面只做三件事：选文件 → 看它跑 → 看成片；autoedit.py 会统一路由到
中立分析、编剧、模型剪辑师、边界、导演、渲染和最终门禁。

为什么不用 tkinter：客户端是 PySide6 打包出来的 exe，打包时排除了 tkinter，
而且这里要和主界面同一套皮肤。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
)

import ui_theme
from memory_config import app_home

BASE = app_home()
DATA = os.path.join(BASE, "data")
LOGS = os.path.join(BASE, "logs")
C = ui_theme.C
STAGES = 6


class AutoEditDialog(QDialog):
    """一个对话框一条任务，使用与主按钮相同的正式工作流。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自主剪辑 · 正式时间轴流程")
        self.setMinimumSize(900, 640)
        self.path = ""
        self.proc = None
        self.log_path = ""
        self.log_pos = 0
        self.out_path = ""
        self._build()

        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._poll)

    # -- 界面 ---------------------------------------------------------------
    def _build(self):
        self.setStyleSheet(
            "QDialog { background: %s; }"
            "QPlainTextEdit { background: %s; border: 1px solid %s;"
            " border-radius: 4px; padding: 6px; font-family: Consolas,"
            " 'Microsoft YaHei UI'; font-size: 12px; }"
            % (C["bg"], C["panel2"], C["border"]))

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        title = QLabel("自主剪辑")
        title.setStyleSheet(
            "font-size: 15px; font-weight: 600; color: %s;" % C["text"])
        root.addWidget(title)

        hint = QLabel("流程：中立分析 → 编剧 → 时间轴模型剪辑师 → 边界精修 → "
                      "导演视听复审/返工 → 渲染 → 最终门禁。")
        hint.setStyleSheet("color: %s; font-size: 12px;" % C["text2"])
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_pick = QPushButton("选择视频…")
        self.btn_pick.clicked.connect(self._pick)
        bar.addWidget(self.btn_pick)

        self.lbl_file = QLabel("未选择")
        self.lbl_file.setStyleSheet("color: %s;" % C["text3"])
        self.lbl_file.setMinimumWidth(260)
        bar.addWidget(self.lbl_file, 1)

        bar.addWidget(QLabel("音轨"))
        self.cmb_track = QComboBox()
        self.cmb_track.addItems(["0", "1", "2", "3", "4", "5"])
        self.cmb_track.setFixedWidth(56)
        bar.addWidget(self.cmb_track)

        bar.addWidget(QLabel("目标秒数"))
        self.ed_target = QLineEdit()
        self.ed_target.setPlaceholderText("留空=尽量紧凑")
        self.ed_target.setFixedWidth(120)
        bar.addWidget(self.ed_target)

        self.btn_run = QPushButton("▶ 开始剪")
        self.btn_run.setStyleSheet(
            "QPushButton { background: %s; color: #10202e; font-weight: 600;"
            " border: none; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:hover { background: %s; }"
            "QPushButton:disabled { background: %s; color: %s; }"
            % (C["accent"], C["accent_hover"], C["card"], C["text3"]))
        self.btn_run.clicked.connect(self._start)
        bar.addWidget(self.btn_run)

        self.btn_cancel = QPushButton("停止")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        bar.addWidget(self.btn_cancel)
        root.addLayout(bar)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(4000)
        root.addWidget(self.text, 1)

        foot = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        foot.addWidget(self.bar, 1)

        self.lbl_status = QLabel("待命")
        self.lbl_status.setStyleSheet("color: %s;" % C["text2"])
        foot.addWidget(self.lbl_status)

        self.btn_open = QPushButton("打开成片")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_out)
        foot.addWidget(self.btn_open)
        root.addLayout(foot)

        self.text.appendPlainText(
            "该入口与主界面共用正式岗位流程；所有删除都绑定时间轴事件 ID。\n"
            "首次素材分析会建立共享媒体缓存，后续岗位按窗口复用。")

    # -- 选择 ---------------------------------------------------------------
    def _pick(self):
        start = os.path.dirname(self.path) if self.path else BASE
        path, _f = QFileDialog.getOpenFileName(
            self, "选一个视频", start,
            "视频 (*.mp4 *.mkv *.mov *.avi *.flv *.ts);;所有文件 (*.*)")
        if not path:
            return
        self.path = path
        self.lbl_file.setText(os.path.basename(path))
        self.lbl_file.setToolTip(path)
        self.text.appendPlainText("\n已选：%s" % path)

    # -- 跑 -----------------------------------------------------------------
    def _command(self):
        """开发目录用 .venv 的 python；打包版用 exe 自己 + --run-script。"""
        script = os.path.join(BASE, "autoedit.py")
        venv = os.path.join(BASE, ".venv", "Scripts", "python.exe")
        if os.path.isfile(venv):
            prog, args = venv, [script]
        elif "python" in os.path.basename(sys.executable).lower():
            prog, args = sys.executable, [script]
        else:
            prog, args = sys.executable, ["--run-script", script]
        args.append(self.path)
        track = self.cmb_track.currentText().strip()
        if track:
            args += ["--track", track]
        target = self.ed_target.text().strip()
        if target:
            args += ["--target", target]
        args += ["--log", self.log_path]
        return prog, args

    def _start(self):
        if not self.path:
            QMessageBox.information(self, "先选视频", "先点「选择视频…」。")
            return
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        os.makedirs(DATA, exist_ok=True)
        os.makedirs(LOGS, exist_ok=True)
        self.log_path = os.path.join(
            LOGS, "autoedit_ui_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        self.log_pos = 0
        self.out_path = ""
        self.text.clear()
        self.bar.setValue(0)
        self.btn_open.setEnabled(False)
        try:                        # 旧结果不能当成这一次的
            os.remove(os.path.join(DATA, "autoedit_last.json"))
        except OSError:
            pass

        prog, args = self._command()
        self.text.appendPlainText("执行：%s %s\n" % (os.path.basename(prog),
                                                    " ".join(args)))
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(BASE)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._drain)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._error)
        self.proc.start(prog, args)
        self.btn_run.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.lbl_status.setText("正在剪…")
        self.timer.start()

    def _drain(self):
        """日志走文件；这里只把管道读空，避免子进程被写满卡住。"""
        if self.proc is not None:
            self.proc.readAllStandardOutput()

    def _poll(self):
        if not self.log_path or not os.path.isfile(self.log_path):
            return
        try:
            with open(self.log_path, "r", encoding="utf-8",
                      errors="replace") as handle:
                handle.seek(self.log_pos)
                chunk = handle.read()
                self.log_pos = handle.tell()
        except OSError:
            return
        if not chunk:
            return
        self.text.appendPlainText(chunk.rstrip("\n"))
        self.text.verticalScrollBar().setValue(
            self.text.verticalScrollBar().maximum())
        match = re.findall(r"\[(\d)/%d\]" % STAGES, chunk)
        if match:
            stage = int(match[-1])
            self.bar.setValue(int((stage - 1) / STAGES * 100))
            self.lbl_status.setText("第 %d/%d 步" % (stage, STAGES))

    def _error(self, _err):
        self.timer.stop()
        self.lbl_status.setText("启动失败")
        self._reset_buttons()
        if self.proc is not None:
            self.text.appendPlainText("\n[启动失败] %s" % self.proc.errorString())

    def _done(self, code, _status):
        self.timer.stop()
        self._poll()
        self._reset_buttons()
        if code == 0:
            result = os.path.join(DATA, "autoedit_last.json")
            try:
                with open(result, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.out_path = str(data.get("out") or "")
            except (OSError, ValueError):
                self.out_path = ""
            self.bar.setValue(100)
            self.lbl_status.setText("完成")
            if self.out_path:
                self.text.appendPlainText("\n成片：%s" % self.out_path)
                self.btn_open.setEnabled(os.path.isfile(self.out_path))
            else:
                self.text.appendPlainText("\n完成，但没找到成片记录。")
        else:
            self.lbl_status.setText("失败（退出码 %s）" % code)
            self.text.appendPlainText("\n剪辑失败，退出码 %s。日志：%s"
                                      % (code, self.log_path))

    def _reset_buttons(self):
        self.btn_run.setEnabled(True)
        self.btn_pick.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _cancel(self):
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        pid = int(self.proc.processId() or 0)
        self.text.appendPlainText("\n[停止] 已请求终止")
        try:
            if pid:
                # /T 连 ffmpeg 子进程一起收，否则会留下半条编码
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True,
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0))
            else:
                self.proc.kill()
        except Exception:  # noqa: BLE001
            self.proc.kill()

    def _open_out(self):
        if self.out_path and os.path.isfile(self.out_path):
            os.startfile(self.out_path)      # noqa: S606

    # -- 关窗口 -------------------------------------------------------------
    def closeEvent(self, event):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            box = QMessageBox(self)
            box.setWindowTitle("还在剪")
            box.setText("剪辑还在跑。要停下来并关窗口吗？")
            yes = box.addButton("停下来关闭", QMessageBox.AcceptRole)
            box.addButton("继续跑", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is yes:
                self._cancel()
        self.timer.stop()
        super().closeEvent(event)


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    ui_theme.apply(app)
    dlg = AutoEditDialog(None)
    dlg.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

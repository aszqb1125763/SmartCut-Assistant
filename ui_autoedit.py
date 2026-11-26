# -*- coding: utf-8 -*-
"""自主剪辑 · 主窗口（旧界面样式 + 正式时间轴流程）。

子枫 2026-11-24：
    「这个软件原有的导演剪辑全部给我拆了，把全套流程换成新的这个模型。」
    「我喜欢用旧的界面样式，你可以改造啊。」

所以外观沿用旧界面：同一套标题栏、编号面板（Panel）、底部状态条（StatusBar）；
该入口现在与主界面共用正式岗位链：中立分析、编剧、时间轴模型剪辑师、
边界精修、导演视听复审/自动返工、转场、渲染、最终门禁与记忆。
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
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

import ui_theme
from memory_config import app_home
from product_info import PRODUCT_NAME as APP_NAME, VERSION, credits_plain_text
from ui_widgets import NoticeBar, Panel, StatusBar
BASE = app_home()
DATA = os.path.join(BASE, "data")
LOGS = os.path.join(BASE, "logs")
STAGES = 6


def _local_model_label():
    try:
        import llm_config
        return llm_config.selected_model_name()
    except Exception:  # noqa: BLE001
        return "当前模型"


def _probe(path):
    """时长 / 大小 / 音轨数（拿不到就留空，不挡流程）。"""
    info = {"dur": 0.0, "size": 0, "tracks": 0}
    try:
        info["size"] = os.path.getsize(path)
    except OSError:
        pass
    probe = os.path.join(BASE, "bin", "ffprobe.exe")
    if not os.path.isfile(probe):
        probe = "ffprobe"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20, creationflags=flags)
        info["dur"] = float((done.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        done = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20, creationflags=flags)
        info["tracks"] = len([x for x in (done.stdout or "").splitlines()
                              if x.strip()])
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def _load_prefs():
    """界面默认值沿用旧设置（烧字幕 / BGM / 输出目录），不另建一套配置。"""
    prefs = {"burn_subs": False, "bgm": True, "loud": True, "out_dir": ""}
    path = os.path.join(BASE, "conf", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        return prefs
    prefs["burn_subs"] = str(cfg.get("burn_subs", "0")) in ("1", "true", "True")
    prefs["bgm"] = str(cfg.get("bgm", "1")) in ("1", "true", "True")
    prefs["loud"] = str(cfg.get("loudness_norm", "1")) in ("1", "true", "True")
    prefs["out_dir"] = str(cfg.get("out_autocut") or "")
    return prefs


def _clock(seconds):
    seconds = max(0, int(seconds or 0))
    return "%02d:%02d" % (seconds // 60, seconds % 60)


class MainWindowAutoEdit(QWidget):
    """一个窗口一条任务：选视频 → 它自己剪 → 出片。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        self.setMinimumSize(1040, 680)
        self.setObjectName("root")
        self.path = ""
        self.info = {}
        self.proc = None
        self.log_path = ""
        self.log_pos = 0
        self.out_path = ""
        self._build()

        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._poll)
        self._set_running(False)

    # ------------------------------------------------------------ 标题栏
    def _build_titlebar(self):
        bar = QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet(
            "background:#181818;border-bottom:1px solid %s;" % ui_theme.C["border"])
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size:13px;font-weight:600;")
        lay.addWidget(title)

        for text, builder in (("文件", self._menu_file),
                              ("帮助", self._menu_help)):
            btn = QPushButton(text + " ▾")
            btn.setObjectName("ghost")
            btn.setFixedHeight(26)
            menu = QMenu(btn)
            builder(menu)
            btn.setMenu(menu)
            lay.addWidget(btn)

        lay.addStretch(1)
        self.lbl_env = QLabel("本地模型 · %s · 就绪" % _local_model_label())
        self.lbl_env.setStyleSheet("color:#7d7d7d;font-size:11px;")
        lay.addWidget(self.lbl_env)
        return bar

    def _menu_file(self, m):
        m.addAction("打开视频…", self.open_source)
        m.addSeparator()
        m.addAction("打开输出目录", self.open_out_dir)
        m.addAction("打开安装目录", lambda: os.startfile(BASE))
        m.addSeparator()
        m.addAction("退出", self.close)

    def _menu_help(self, m):
        m.addAction("教程 / 快速开始", lambda: self._open_doc("README.md"))
        m.addAction("教程 / Premiere 插件",
                    lambda: self._open_doc(os.path.join("docs", "Premiere插件.md")))
        m.addAction("教程 / 故障排查",
                    lambda: self._open_doc(os.path.join("docs", "故障排查.md")))
        m.addAction("打开日志目录", self.open_logs)
        m.addSeparator()
        m.addAction("关于 %s" % APP_NAME, self._about)

    def _open_doc(self, name):
        path = os.path.join(BASE, name)
        if os.path.isfile(path):
            os.startfile(path)           # noqa: S606
        else:
            QMessageBox.information(self, "找不到文档", path)

    def _about(self):
        QMessageBox.information(
            self, "关于 " + APP_NAME,
            "版本 %s\n自主剪辑 · 正式时间轴流程\n\n"
            "本地模型按事件 ID 生成剪辑单，边界岗位和导演随后用真实声音、"
            "文字与关键画面复审；最终成片未通过门禁不会交付。\n\n%s"
            % (VERSION, credits_plain_text()))

    # ------------------------------------------------------------ 主体
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_titlebar())
        self.notice = NoticeBar()
        root.addWidget(self.notice)

        body = QWidget()
        lay = QHBoxLayout(body)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self._build_left())
        lay.addWidget(self._build_center(), 1)
        lay.addWidget(self._build_right())
        root.addWidget(body, 1)

        self.status = StatusBar(self)
        root.addWidget(self.status)

    def _build_left(self):
        left = QWidget()
        left.setFixedWidth(268)
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ① 素材
        p1 = Panel("素材", "选一个视频", "①")
        self.btn_pick = QPushButton("选择视频…")
        self.btn_pick.setObjectName("primary")
        self.btn_pick.setFixedHeight(30)
        self.btn_pick.clicked.connect(self.open_source)
        p1.add_widget(self.btn_pick)
        self.lbl_file = QLabel("未选择")
        self.lbl_file.setWordWrap(True)
        self.lbl_file.setStyleSheet("color:#9a9a9a;font-size:11px;")
        p1.add_widget(self.lbl_file)
        lay.addWidget(p1)

        # ② 只有两个参数：音轨 + 目标秒数（删哪段由模型判断）
        p2 = Panel("剪成什么样", "其余交给模型", "②")
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("音轨"))
        self.cmb_track = QComboBox()
        self.cmb_track.addItems(["0", "1", "2", "3", "4", "5"])
        self.cmb_track.setFixedWidth(58)
        row.addWidget(self.cmb_track)
        row.addSpacing(8)
        row.addWidget(QLabel("目标秒数"))
        self.ed_target = QLineEdit()
        self.ed_target.setPlaceholderText("留空=紧凑")
        self.ed_target.setFixedWidth(92)
        row.addWidget(self.ed_target)
        row.addStretch(1)
        p2.add_layout(row)
        lay.addWidget(p2)

        # ③ 成片选项：旧界面里有用、又跟导演剪辑无关的几项
        p3 = Panel("成片选项", "烧字幕 / BGM / 响度", "③")
        prefs = _load_prefs()
        self.cb_burn = QCheckBox("烧字幕（用模型校对过的文字）")
        self.cb_burn.setChecked(bool(prefs.get("burn_subs")))
        p3.add_widget(self.cb_burn)
        self.cb_bgm = QCheckBox("自动配 BGM")
        self.cb_bgm.setChecked(prefs.get("bgm", True))
        p3.add_widget(self.cb_bgm)
        self.cb_loud = QCheckBox("按响度拉齐")
        self.cb_loud.setChecked(prefs.get("loud", True))
        p3.add_widget(self.cb_loud)
        self.ed_out = QLineEdit(prefs.get("out_dir", ""))
        self.ed_out.setPlaceholderText("输出目录；留空=data\\")
        self.ed_out.setFixedHeight(26)
        p3.add_widget(self.ed_out)
        self.btn_style = QPushButton("字幕样式…")
        self.btn_style.setObjectName("ghost")
        self.btn_style.setFixedHeight(24)
        self.btn_style.clicked.connect(self.open_style)
        p3.add_widget(self.btn_style)
        lay.addWidget(p3)

        # ④ 开工
        p4 = Panel("开工", "正式岗位流程自动执行", "④")
        self.btn_run = QPushButton("▶ 开始剪")
        self.btn_run.setObjectName("primary")
        self.btn_run.setFixedHeight(32)
        self.btn_run.clicked.connect(self.start)
        p4.add_widget(self.btn_run)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setFixedHeight(26)
        self.btn_stop.clicked.connect(self.stop)
        p4.add_widget(self.btn_stop)
        lay.addWidget(p4)

        # ⑤ 成片
        p5 = Panel("成片", "剪完点这里看", "⑤")
        self.lbl_out = QLabel("还没有")
        self.lbl_out.setWordWrap(True)
        self.lbl_out.setStyleSheet("color:#9a9a9a;font-size:11px;")
        p5.add_widget(self.lbl_out)
        self.btn_open = QPushButton("打开成片")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self.open_out)
        p5.add_widget(self.btn_open)
        p5.body.addStretch(1)
        lay.addWidget(p5, 1)
        return left

    def _build_center(self):
        p = Panel("模型在干什么", "听 → 章节 → 剧本 → 剪辑单 → 出片 → 回执", "")
        btn_clear = QPushButton("清空")
        btn_clear.setObjectName("ghost")
        btn_clear.setFixedHeight(20)
        btn_clear.clicked.connect(lambda: self.text.clear())
        p.head_extra.addWidget(btn_clear)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(6000)
        self.text.setStyleSheet(
            "background:#1a1a1a;border:1px solid #333;border-radius:4px;"
            "padding:8px;font-family:Consolas,'Microsoft YaHei UI';"
            "font-size:12px;")
        p.add_widget(self.text, 1)
        return p

    def _build_right(self):
        right = QWidget()
        right.setFixedWidth(300)
        lay = QVBoxLayout(right)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        p = Panel("这次怎么剪的", "模型的决定", "⑤")
        self.lbl_plan = QLabel("还没开始")
        self.lbl_plan.setWordWrap(True)
        self.lbl_plan.setStyleSheet("color:#b9b9b9;font-size:11px;")
        p.add_widget(self.lbl_plan)
        self.txt_plan = QPlainTextEdit()
        self.txt_plan.setReadOnly(True)
        self.txt_plan.setStyleSheet(
            "background:#1a1a1a;border:1px solid #333;border-radius:4px;"
            "padding:6px;font-family:Consolas,'Microsoft YaHei UI';"
            "font-size:11px;")
        p.add_widget(self.txt_plan, 1)
        lay.addWidget(p, 1)
        return right

    # ------------------------------------------------------------ 选择
    def open_source(self):
        start = os.path.dirname(self.path) if self.path else BASE
        path, _f = QFileDialog.getOpenFileName(
            self, "选一个视频", start,
            "视频 (*.mp4 *.mkv *.mov *.avi *.flv *.ts);;所有文件 (*.*)")
        if not path:
            return
        self._set_source(path)

    def _open_path(self, path):
        """从命令行/拖拽直接带进来的文件。"""
        if path and os.path.isfile(path):
            self._set_source(path)

    def _set_source(self, path):
        self.path = path
        self.info = _probe(path)
        self.lbl_file.setText(
            "%s\n时长 %s · %.0f MB · %d 条音轨"
            % (os.path.basename(path), _clock(self.info.get("dur")),
               (self.info.get("size") or 0) / 1048576.0,
               self.info.get("tracks") or 0))
        self.lbl_file.setToolTip(path)
        self.status.info("已选：%s" % os.path.basename(path))
        self.text.appendPlainText(
            "\n已选：%s（%s）" % (path, _clock(self.info.get("dur"))))

    # ------------------------------------------------------------ 跑
    def _command(self):
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
        if self.cb_burn.isChecked():
            args.append("--burn-subs")
        if self.cb_bgm.isChecked():
            args.append("--bgm")
            if self.cb_loud.isChecked():
                args.append("--loud")
        out_dir = self.ed_out.text().strip()
        if out_dir:
            args += ["--outdir", out_dir]
        args += ["--log", self.log_path]
        return prog, args

    def _set_running(self, on):
        self.btn_run.setEnabled(not on)
        self.btn_pick.setEnabled(not on)
        self.btn_stop.setEnabled(on)

    def start(self):
        if not self.path:
            self.notice.show_bar("先选一个视频", "点左边「选择视频…」", "warn")
            return
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        os.makedirs(DATA, exist_ok=True)
        os.makedirs(LOGS, exist_ok=True)
        self.notice.hide_bar()
        self.log_path = os.path.join(
            LOGS, "autoedit_ui_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        self.log_pos = 0
        self.out_path = ""
        self.text.clear()
        self.txt_plan.clear()
        self.lbl_plan.setText("模型正在判断…")
        self.btn_open.setEnabled(False)
        self.lbl_out.setText("剪完自动出现")
        try:                          # 旧结果不能当这一次的
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

        self._set_running(True)
        self.lbl_env.setText("本地模型 · %s · 工作中" % _local_model_label())
        self.status.working(True, title="自主剪辑", stage="开始…", pct=0.0,
                            on_cancel=self.stop)
        self.timer.start()

    def _drain(self):
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
        found = re.findall(r"\[(\d)/%d\]([^\n]*)" % STAGES, chunk)
        if found:
            stage = int(found[-1][0])
            label = (found[-1][1] or "").strip().strip("…").strip()
            self.status.prop_work(pct=(stage - 1) / float(STAGES),
                                  stage="%d/%d %s" % (stage, STAGES, label))

    def _error(self, _err):
        self.timer.stop()
        self._set_running(False)
        self.lbl_env.setText("本地模型 · %s · 就绪" % _local_model_label())
        self.status.working(False)
        if self.proc is not None:
            self.status.error("启动失败", self.proc.errorString())
            self.text.appendPlainText("\n[启动失败] %s" % self.proc.errorString())

    def _done(self, code, _status):
        self.timer.stop()
        self._poll()
        self._set_running(False)
        self.lbl_env.setText("本地模型 · %s · 就绪" % _local_model_label())
        self.status.working(False)
        if code != 0:
            self.status.error("剪辑失败（退出码 %s）" % code,
                              "日志：%s" % self.log_path)
            self.text.appendPlainText("\n剪辑失败，退出码 %s。日志：%s"
                                      % (code, self.log_path))
            self.lbl_plan.setText("失败")
            return
        try:
            with open(os.path.join(DATA, "autoedit_last.json"),
                      encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        self.out_path = str(data.get("out") or "")
        self._show_plan(data)
        if self.out_path and os.path.isfile(self.out_path):
            mb = os.path.getsize(self.out_path) / 1048576.0
            self.lbl_out.setText("%s（%.0f MB）"
                                 % (os.path.basename(self.out_path), mb))
            self.btn_open.setEnabled(True)
            self.status.ok("剪辑完成：%s" % os.path.basename(self.out_path),
                           "%.1f 秒 · %.0f MB"
                           % (data.get("kept") or 0, mb))
            self.text.appendPlainText("\n成片：%s" % self.out_path)
        else:
            self.status.info("完成，但没找到成片记录")
            self.text.appendPlainText("\n完成，但没找到成片记录。")

    def _show_plan(self, data):
        """把模型的剪辑单摊开：删了几段、保留几段、成片多长。"""
        if not data:
            self.lbl_plan.setText("没有记录")
            return
        keep = data.get("keep") or []
        plan = data.get("plan") or {}
        drops = plan.get("delete") or []
        total = float(data.get("total") or 0)
        kept = float(data.get("kept") or 0)
        lines = []
        lines.append("原片 %s → 成片 %s（压掉 %.0f%%）"
                     % (_clock(total), _clock(kept),
                        (1 - kept / total) * 100 if total else 0))
        lines.append("保留 %d 段，模型划掉 %d 段" % (len(keep), len(drops)))
        reason = str(plan.get("reason") or "").strip()
        if reason:
            lines.append("理由：" + reason)
        self.lbl_plan.setText("\n".join(lines[:3]))
        body = []
        for index, (a, b) in enumerate(keep, 1):
            body.append("%2d. %6.1f ~ %6.1f 秒  （%.1f 秒）"
                        % (index, float(a), float(b), float(b) - float(a)))
        self.txt_plan.setPlainText("\n".join(body) if body else "（没有保留段）")

    def stop(self):
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        self.text.appendPlainText("\n[停止] 已请求终止")
        pid = int(self.proc.processId() or 0)
        try:
            if pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True,
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0))
            else:
                self.proc.kill()
        except Exception:  # noqa: BLE001
            self.proc.kill()

    # ------------------------------------------------------------ 输出
    def open_out(self):
        if self.out_path and os.path.isfile(self.out_path):
            os.startfile(self.out_path)      # noqa: S606

    def open_out_dir(self):
        os.startfile(DATA if os.path.isdir(DATA) else BASE)   # noqa: S606

    def open_style(self):
        """字幕样式（字体/字号/描边/一行几个字）还是旧的那份配置。"""
        path = os.path.join(BASE, "conf", "subtitle_style.json")
        if os.path.isfile(path):
            os.startfile(path)            # noqa: S606
        else:
            QMessageBox.information(self, "找不到字幕样式", path)

    def open_logs(self):
        os.startfile(LOGS if os.path.isdir(LOGS) else BASE)   # noqa: S606

    # ------------------------------------------------------------ 关窗口
    def closeEvent(self, ev):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            box = QMessageBox(self)
            box.setWindowTitle("还在剪")
            box.setText("剪辑还在跑。要停下来并关窗口吗？")
            yes = box.addButton("停下来关闭", QMessageBox.AcceptRole)
            box.addButton("继续跑", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is yes:
                self.stop()
        self.timer.stop()
        super().closeEvent(ev)

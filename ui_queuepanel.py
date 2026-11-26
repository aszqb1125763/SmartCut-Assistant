# -*- coding: utf-8 -*-
"""主界面内嵌队列面板（替代原来的「① 素材」空区域）。

设计：
  · 面板头部：① 队列   N 个素材 · 已完成 M
  · 工具条：[＋ 添加] [▶ 开始] [⏸ 暂停] [清空] [⤢ 展开]
  · 默认只做素材分析；显式打开「全自动出片」才继续剧本、初剪、审片和渲染
  · 列表：每行「文件名 / 状态 / 进度条」，可拖拽排序（顺序 = 处理顺序）
  · 双击一行 = 打开这条素材去剪（卡片区加载它）
  · 拖文件进主窗口 = 加入这个队列（不自动开始）

复用 ui_queue.QueueWorker / merge_outputs，不重复实现。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

import ui_widgets as W
from ui_queue import STATUS_COLOR, STATUS_TEXT, QueueWorker, merge_outputs
from ui_widgets import Panel

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".flv", ".ts", ".avi", ".m4v", ".webm")


class QueueRowWidget(QWidget):
    """一行两排：
       第一排：序号 + 文件名（过长省略中间）
       第二排：状态 + 音轨徽标 + 时长，下面一条细进度条
    """

    def __init__(self, item: dict, idx: int, parent=None):
        super().__init__(parent)
        st = item.get("status", "wait")
        # 调色滤镜：不同状态不同的淡色底（运行中额外加左侧蓝条）
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 深色底上的实色淡调，避免 rgba 叠在白底上发灰发亮
        tint = {"run": "#1d2a3a",
                "done": "#1c2823",
                "fail": "#2e2022"}.get(st, "transparent")
        border = "border-left:3px solid #4a90d9;" if st == "run" else ""
        self.setStyleSheet(
            "QueueRowWidget{background:%s;border-radius:3px;%s}" % (tint, border))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(2)

        # ---- 第一排
        top = QHBoxLayout()
        top.setSpacing(5)
        self.num = QLabel("%d" % (idx + 1), self)
        self.num.setFixedWidth(16)
        self.num.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.num.setStyleSheet(
            "background:transparent;color:%s;font-size:12px;font-weight:700;"
            % ("#6fb0e8" if st == "run" else "#9a9a9a"))
        top.addWidget(self.num)

        full = os.path.basename(item["path"])
        name = QLabel("", self)
        name.setStyleSheet("background:transparent;font-size:12px;color:#dcdcdc;")
        name.setToolTip(full)
        try:
            from PySide6.QtGui import QFontMetrics
            fm = QFontMetrics(name.font())
            avail = max(70, (parent.width() if parent else 250) - 60)
            name.setText(fm.elidedText(full, Qt.ElideMiddle, avail))
        except Exception:
            name.setText(full)
        top.addWidget(name, 1)
        lay.addLayout(top)

        # ---- 第二排：状态 + 音轨 + 时长
        bottom = QHBoxLayout()
        bottom.setSpacing(5)
        txt = STATUS_TEXT.get(st, "")
        if st == "run" and item.get("msg"):
            txt = str(item["msg"])
        self.st = QLabel("", self)
        self.st.setStyleSheet("background:transparent;color:%s;font-size:11px;"
                              % STATUS_COLOR.get(st, "#7d7d7d"))
        self.st.setToolTip(txt)
        try:
            from PySide6.QtGui import QFontMetrics
            fm2 = QFontMetrics(self.st.font())
            self.st.setText(fm2.elidedText(txt, Qt.ElideRight, 96))
        except Exception:
            self.st.setText(txt[:10])
        bottom.addWidget(self.st)
        bottom.addStretch(1)

        tr = item.get("tracks")
        if tr:
            self.trk = QLabel("音轨 %s" % ",".join(str(x + 1) for x in tr), self)
            self.trk.setStyleSheet(
                "background:#22303f;color:#8fc0ea;border-radius:3px;"
                "padding:0px 4px;font-size:10px;")
            bottom.addWidget(self.trk)
        dur = item.get("dur") or 0
        if dur:
            dl = QLabel("%.1f 分" % (float(dur) / 60.0), self)
            dl.setStyleSheet("color:#6e6e6e;font-size:10px;")
            bottom.addWidget(dl)
        lay.addLayout(bottom)

        # ---- 进度条
        # setVisible(True) is called before the bar is added to the layout.
        # Without an explicit parent Qt creates a native top-level window first,
        # then closes it again when addWidget reparents the bar.  On Windows that
        # is the recurring 216x42 app-title popup seen during progress updates.
        self.bar = QProgressBar(self)
        self.bar.setFixedHeight(3)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(float(item.get("pct") or 0) * 1000))
        color = {"done": "#4caf72", "fail": "#d05a5a",
                 "run": "#4a90d9"}.get(st, "#3a3a3a")
        self.bar.setStyleSheet(
            "QProgressBar{background:#1a1a1a;border:none;border-radius:1px;}"
            "QProgressBar::chunk{background:%s;border-radius:1px;}" % color)
        lay.addWidget(self.bar)
        self.bar.setVisible(st != "wait")




class _PlainRunner:
    """用普通线程跑 QueueWorker：QThread 里跑这段会卡死（实测），普通线程正常。"""

    def __init__(self, worker):
        import threading
        self.w = worker
        self.t = threading.Thread(target=worker.run, daemon=True)

    def start(self):
        self.t.start()

    def isRunning(self):
        return self.t.is_alive()

    def wait(self, ms=0):
        self.t.join(timeout=(ms / 1000.0) if ms else None)
        return not self.t.is_alive()

    def stop(self):
        self.w._stop = True

    def skip_current(self):
        self.w._skip = getattr(self.w, "_idx", -1)


class QueuePanel(Panel):
    """内嵌队列面板。"""

    opened = Signal(str)          # 双击某行：要打开这条素材去剪
    expand_requested = Signal()   # 点「⤢ 展开」：开大窗口
    item_finished = Signal(str)   # 某条完成：path（V2 收到后载入卡片）
    job_started = Signal(str)             # 开始处理：第一条的文件名
    job_progress = Signal(float, str)     # 进度（-1 表示不确定）, 阶段文字
    job_finished = Signal()               # 全部结束
    merged = Signal(str)          # 合并完成

    def __init__(self, parent=None):
        super().__init__("队列", "", "①", parent)
        self.items: list[dict] = []
        self.main_keep_ratio = 0.75
        self.main_level = "medium"      # 旧链路兼容；由百分比自动派生
        self.merge_after = False        # 仅全自动模式可用；默认不合并不同素材
        self.worker = None
        self.options_provider = None
        self._page = 0
        self._last_change = time.time()
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(5000)
        self._stall_timer.timeout.connect(self._check_stall)
        self._stall_timer.start()
        self.setMinimumHeight(260)

        # 两行按钮：上面「添加 / 开始」，下面「暂停 / 清空 / 展开」，
        # 面板只有 264 宽，一行塞五个会挤成单字
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.btn_add = QPushButton("＋ 添加素材")
        self.btn_add.setFixedHeight(26)
        self.btn_start = QPushButton("▶ 开始")
        self.btn_start.setObjectName("primary")
        self.btn_start.setFixedHeight(26)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setFixedHeight(26)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("ghost")
        self.btn_clear.setFixedHeight(26)
        self.btn_expand = QPushButton("⤢ 展开")
        self.btn_expand.setObjectName("ghost")
        self.btn_expand.setFixedHeight(26)
        self.btn_expand.setToolTip("打开大窗口：单项设置、四步清单、合并结果")
        tools.addWidget(self.btn_add, 1)
        tools.addWidget(self.btn_start)
        self.add_layout(tools)

        tools2 = QHBoxLayout()
        tools2.setSpacing(6)
        self.btn_tracks = QPushButton("音轨…")
        self.btn_tracks.setFixedHeight(26)
        self.btn_tracks.setMinimumWidth(72)
        self.btn_tracks.setToolTip("给选中的这一条素材指定音轨（只影响这一条）")
        tools2.addWidget(self.btn_pause)
        tools2.addWidget(self.btn_tracks)
        tools2.addWidget(self.btn_clear)
        tools2.addStretch(1)
        tools2.addWidget(self.btn_expand)
        self.add_layout(tools2)

        auto_row = QHBoxLayout()
        self.cb_auto_workflow = QCheckBox("全自动出片（默认关闭）")
        self.cb_auto_workflow.setChecked(False)
        self.cb_auto_workflow.setToolTip(
            "关闭：只解析、切片、打标签并生成卡片；\n"
            "开启：分析后继续编剧、剪辑师初剪、导演审片和渲染。")
        auto_row.addWidget(self.cb_auto_workflow)
        auto_row.addStretch(1)
        self.add_layout(auto_row)

        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setStyleSheet(
            "QListWidget{background:#1a1a1a;border:1px solid #3a3a3a;border-radius:4px;}"
            "QListWidget::item{padding:0px;background:transparent;}"
            "QListWidget::item:selected{background:transparent;color:#dcdcdc;}"
            "QListWidget::item:hover{background:rgba(255,255,255,0.03);}")
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.sel_row = 0
        self.add_widget(self.list, 1)

        self.hint = QLabel("拖视频进窗口加入队列 · 拖行调顺序（顺序=处理顺序） · 双击查看卡片")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#6e6e6e;font-size:11px;")
        self.add_widget(self.hint)
        self._update_head()

        for b in (self.btn_add, self.btn_start, self.btn_pause, self.btn_clear,
                  self.btn_expand):
            b.setMinimumWidth(72)
        self.btn_expand.clicked.connect(self.expand_requested.emit)
        self.btn_tracks.clicked.connect(self.pick_tracks)
        self.btn_add.clicked.connect(self.add_dialog)
        self.btn_start.clicked.connect(self.start_all)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_clear.clicked.connect(self.clear)
        self.cb_auto_workflow.toggled.connect(self._auto_workflow_toggled)
        self.list.itemClicked.connect(lambda item: self._remember_row(item))
        self.list.itemDoubleClicked.connect(self._open_row)
        self.list.model().rowsMoved.connect(self._on_moved)

    # ---------------- 数据 ----------------
    def add_paths(self, paths):
        added = 0
        for p in paths:
            if not os.path.isfile(p) or not p.lower().endswith(VIDEO_EXTS):
                continue
            if any(os.path.normcase(x["path"]) == os.path.normcase(p) for x in self.items):
                continue
            dur = 0.0
            try:
                import index as indexer
                dur = float(indexer.probe_streams(p).get("duration") or 0)
            except Exception:
                pass
            self.items.append({"path": p, "status": "wait", "pct": 0.0,
                               "msg": "", "out": "", "dur": dur,
                               "level": "", "level_custom": False,
                               "keep_ratio": None,
                               "keep_ratio_custom": False,
                               "out_dir": "", "tracks": None})
            added += 1
        if added:
            self._refresh()
            self.hint.setText("拖视频进窗口加入队列 · 拖行调顺序（顺序=处理顺序） · 双击查看卡片")
        return added

    def _remember_row(self, item):
        try:
            self.sel_row = self.list.row(item)
        except Exception:
            pass

    def pick_tracks(self):
        """给当前选中的这一条素材选音轨（只影响这一条）。"""
        row = getattr(self, "sel_row", 0)
        if not (0 <= row < len(self.items)):
            self.hint.setText("先在列表里点一条素材，再选音轨")
            return
        it = self.items[row]
        try:
            import ui_dialogs
            dlg = ui_dialogs.AudioDialog(self, it["path"])
            if dlg.exec() == 1:      # QDialog.Accepted
                picks = [k for k, cb in getattr(dlg, "boxes", {}).items() if cb.isChecked()]
                if picks:
                    it["tracks"] = picks
                    self.hint.setText("已为「%s」指定音轨 %s"
                                      % (os.path.basename(it["path"]),
                                         "、".join(str(x + 1) for x in picks)))
                    self._refresh()
        except Exception as exc:  # noqa: BLE001
            self.hint.setText("音轨窗口打不开：%s" % exc)

    def add_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择素材（可多选）", "",
            "视频 (*.mp4 *.mov *.mkv *.flv *.ts *.avi *.m4v *.webm)")
        if paths:
            self.add_paths(paths)

    def _refresh(self):
        cur = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for i, it in enumerate(self.items):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, it["path"])
            item.setText("")
            item.setSizeHint(QSize(0, 54))
            self.list.addItem(item)
            # Give the row its real parent at construction time.  A parentless
            # QWidget is briefly a native top-level window on Windows; progress
            # refreshes used to flash that 216x42 window with the app title.
            row = QueueRowWidget(it, i, parent=self.list.viewport())
            self.list.setItemWidget(item, row)
        self.list.blockSignals(False)
        if 0 <= cur < self.list.count():
            self.list.setCurrentRow(cur)
        self._update_head()

    def _check_stall(self):
        """45 秒没有任何进展 → 直说可能卡在哪，而不是让用户干等。"""
        if not (self.worker and self.worker.isRunning()):
            return
        if time.time() - self._last_change > 45:
            self.hint.setText("已 45 秒没有进展：可能在加载语音模型，或卡在下载。"
                              "建议点「⏸ 暂停」后看日志。")

    def _auto_workflow_toggled(self, checked):
        """关闭全自动时同时清掉合并意图，避免下次误合并素材。"""
        if not checked:
            self.merge_after = False
        self._update_head()

    def _update_head(self):
        n = len(self.items)
        done = sum(1 for x in self.items if x["status"] == "done")
        # 开始按钮永远可点：点下去没东西可处理时给提示，而不是"点了没反应"
        self.btn_start.setEnabled(True)
        has_pending = any(x["status"] in ("wait", "fail", "stop") for x in self.items)
        if not self.items:
            self.btn_start.setToolTip("队列是空的：先把视频拖进窗口，或点「＋ 添加素材」")
        elif not has_pending:
            self.btn_start.setToolTip("队列里都处理完了，再点一次＝全部重新处理")
        else:
            if self.cb_auto_workflow.isChecked():
                self.btn_start.setToolTip(
                    "全自动：分析 → 剧本 → 初剪 → 导演审片 → 渲染输出")
            else:
                self.btn_start.setToolTip(
                    "仅分析素材并生成切片、标签和卡片，不剪辑、不出片")
        self.btn_pause.setEnabled(bool(self.worker and self.worker.isRunning()))
        try:
            lbl = getattr(self, "_cnt", None)
            if lbl is None:
                lbl = QLabel("")
                lbl.setObjectName("panel2Hint")
                self.head_extra.addWidget(lbl)
                self._cnt = lbl
            lbl.setText("%d 个素材 · 已完成 %d" % (n, done) if n else "拖视频进来")
        except Exception:
            pass

    def _find(self, path):
        for it in self.items:
            if os.path.normcase(it["path"]) == os.path.normcase(path or ""):
                return it
        return None

    def _on_moved(self, *args):
        order = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            it = self._find(item.data(Qt.UserRole)) if item else None
            if it and it not in order:
                order.append(it)
        if len(order) == len(self.items):
            self.items = order
        self._refresh()

    def _open_row(self, item):
        it = self._find(item.data(Qt.UserRole))
        if it:
            self.opened.emit(it["path"])

    # ---------------- 运行 ----------------
    def set_main_level(self, lv):
        """Compatibility wrapper for older callers."""
        import cut_strength
        self.set_main_keep_ratio(cut_strength.LEGACY_KEEP_RATIO.get(lv, 0.75))

    def set_main_keep_ratio(self, keep_ratio):
        import cut_strength
        self.main_keep_ratio = cut_strength.normalize_ratio(keep_ratio)
        self.main_level = cut_strength.level_for_ratio(self.main_keep_ratio)
        for it in self.items:
            if (it.get("status") != "run"
                    and not it.get("keep_ratio_custom", False)):
                it["keep_ratio"] = self.main_keep_ratio
                it["level"] = self.main_level

    def start_all(self):
        if self.worker and self.worker.isRunning():
            return
        pending = [x for x in self.items if x["status"] in ("wait", "fail", "stop")]
        if not pending:
            if not self.items:
                self.hint.setText("队列是空的：把视频拖进窗口，或点「＋ 添加素材」")
            else:
                # 都完成了 → 重置为等待，重新跑一遍
                for x in self.items:
                    x["status"] = "wait"
                    x["pct"] = 0.0
                    x["msg"] = ""
                self._refresh()
                self.hint.setText("已全部完成一轮，重新开始处理…")
                pending = list(self.items)
            if not pending:
                try:
                    self.job_progress.emit(-1.0, "队列是空的，先拖入素材")
                except Exception:
                    pass
                return
        # 开工前冻结百分比；运行中修改界面不会污染本轮任务。
        for x in pending:
            if not x.get("keep_ratio_custom", False):
                x["keep_ratio"] = self.main_keep_ratio
                x["level"] = self.main_level
        # 模型父目录：models（里面有 large-v3-full / large-v3-turbo-full）
        model = os.path.join(os.environ.get("SMARTCUT_HOME") or
                             os.path.dirname(os.path.abspath(__file__)),
                             "models")
        opts = {}
        if callable(self.options_provider):
            try:
                opts = dict(self.options_provider() or {})
            except Exception:
                opts = {}
        auto_workflow = bool(self.cb_auto_workflow.isChecked())
        _qr = QueueWorker(
            pending, level=self.main_level, auto_cut=auto_workflow,
            keep_ratio=self.main_keep_ratio,
            model_dir=model if os.path.isdir(model) else None,
            merge_after=auto_workflow and bool(self.merge_after),
            use_bgm=opts.get("use_bgm", True),
            burn_subs=opts.get("burn_subs", False),
            loudness_norm=opts.get("loudness_norm", True),
            output_dir=opts.get("output_dir"),
            model_size=opts.get("model_size") or opts.get("model") or "large-v3",
            edit_mode=opts.get("edit_mode") or "gameplay",
        )
        _qr.item_state.connect(self._on_state)
        _qr.item_done.connect(self._on_done)
        _qr.merge_done.connect(lambda p: self.merged.emit(p))
        _qr.all_done.connect(self._workflow_finished)
        self.worker = _PlainRunner(_qr)
        self.cb_auto_workflow.setEnabled(False)
        self.worker.start()
        action = "全自动处理" if auto_workflow else "分析素材并生成卡片"
        self.hint.setText("正在按队列顺序%s：%s" %
                          (action, os.path.basename(pending[0]["path"])))
        self.btn_pause.setEnabled(True)
        self._update_head()
        try:
            self.job_started.emit(os.path.basename(pending[0]["path"]))
        except Exception:
            pass

    def _workflow_finished(self):
        """本轮参数是启动时快照；结束后才允许改下一轮模式。"""
        self.cb_auto_workflow.setEnabled(True)
        self._update_head()
        self.job_finished.emit()

    def pause(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.btn_pause.setEnabled(False)
            self.hint.setText("正在停下当前素材…")

    def clear(self):
        if self.worker and self.worker.isRunning():
            return
        self.items = []
        self._refresh()

    @staticmethod
    def _stage_pct(text, pct):
        """index 只回文字时，按阶段给近似进度，免得进度条一直 0%。"""
        if pct is not None and pct >= 0:
            return pct
        t = str(text or "")
        for words, v in (("完成", 0.99), ("语义", 0.65),
                         ("识别", 0.45), ("抽帧", 0.12),
                         ("音量", 0.12)):
            if words in t:
                return v
        return -1.0

    def _on_state(self, idx, status, pct, text):
        if not (self.worker and 0 <= idx < len(self.worker.w.items)):
            return
        it = self._find(self.worker.w.items[idx]["path"])
        if not it:
            return
        it["status"] = status
        _p = self._stage_pct(text, pct)
        if _p is not None and _p >= 0:
            it["pct"] = _p
        it["msg"] = text or it["msg"]
        self._last_change = time.time()
        self._refresh()
        try:
            self.job_progress.emit(float(_p) if _p is not None else -1.0,
                                   str(text or ""))
        except Exception:
            pass

    def _on_done(self, idx, out):
        if not (self.worker and 0 <= idx < len(self.worker.w.items)):
            return
        it = self._find(self.worker.w.items[idx]["path"])
        if it:
            it["status"] = "done"
            it["out"] = out
            it["pct"] = 1.0
        self._refresh()
        # 通知主界面：这条任务完成，可以载入它的卡片。
        if it:
            self.item_finished.emit(it["path"])

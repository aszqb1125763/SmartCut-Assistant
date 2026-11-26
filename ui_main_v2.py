# -*- coding: utf-8 -*-
"""智能剪辑助手 · 新版主窗口（按设计稿 v3/v4/v5 实现）。

布局：标题栏 → 通知条（按需出现） → 三栏（素材/力度/输出 | 卡片 | 选中/BGM/导出）
      → 弹出式状态条（3px 细边 / 出错展开 / 成功自动缩回）

业务逻辑尽量复用现有模块：
  · 分析：index.index_video
  · 读索引：autocut.load_index
  · 导出：export.export_single / export.export_merged
  · 一条龙剪辑：autopilot.run_one
  · 设置/字幕/音轨/关于对话框：复用 cards_ui 里已有的
"""
from __future__ import annotations

import os
import sys
import time
import traceback
import uuid
from collections.abc import Mapping

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon, QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFrame, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

import ui_theme
import ui_widgets as W
from product_info import PRODUCT_NAME as APP_NAME, VERSION
from ui_widgets import CardTile, NoticeBar, Panel, StatusBar


# ---------------------------------------------------------------- 后台线程
class AnalyzeWorker(QThread):
    progress = Signal(float, str)
    done = Signal(dict, str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, path, out_dir, model_size="large-v3", model_dir=None):
        super().__init__()
        self.path = path
        self.out_dir = out_dir
        self.model_size = model_size
        self.model_dir = model_dir
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            import index as ix
            os.makedirs(self.out_dir, exist_ok=True)

            def on_status(msg):
                self.progress.emit(-1, str(msg))

            def on_progress(frac, msg=""):
                self.progress.emit(float(frac or 0), str(msg or ""))

            ix.index_video(self.path, self.out_dir,
                           model_size=self.model_size,
                           models_dir=self.model_dir,
                           on_status=on_status,
                           on_progress=on_progress,
                           should_stop=lambda: self._stop)
            if self._stop:
                self.cancelled.emit()
                return
            import autocut as ac
            data = ac.load_index(self.out_dir)
            self.done.emit(data or {}, self.out_dir)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "IndexCancelled" or self._stop:
                self.cancelled.emit()
            else:
                self.failed.emit("%s\n%s" % (exc, traceback.format_exc(limit=2)))


class CutWorker(QThread):
    log = Signal(str)
    task_started = Signal(str)
    progress = Signal(object)
    done = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    degraded = Signal(object)

    def __init__(self, src, level="medium", use_bgm=True, index_dir=None,
                 output_dir=None, burn_subs=False, loudness_norm=True,
                 mode="gameplay", task_id=None, resume=False,
                 model_editor=False, tracks=None, keep_ratio=None):
        super().__init__()
        self.src = src
        self.level = level
        self.keep_ratio = keep_ratio
        self.use_bgm = use_bgm
        self.index_dir = index_dir
        self.output_dir = output_dir
        self.burn_subs = bool(burn_subs)
        self.loudness_norm = bool(loudness_norm)
        self.mode = str(mode or "gameplay")
        self.task_id = str(task_id or "")
        self.resume = bool(resume)
        self.model_editor = bool(model_editor)
        self.tracks = list(tracks) if tracks is not None else None
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        memory = None
        try:
            import autopilot
            try:
                import memory_config
                import task_autosave
                memory_cfg = memory_config.load_config(home=_home())
                if memory_cfg.get("enabled", True):
                    memory = task_autosave.open_autosave_store(config=memory_cfg)
            except Exception as exc:  # noqa: BLE001
                self.log.emit("任务记忆不可用，本次继续但不能断点恢复：%s" % str(exc)[:80])
            if self.resume and memory is None:
                self.log.emit("原任务无法断点恢复，已改为新的完整任务。")
                self.task_id = ""
                self.resume = False
            if not self.task_id:
                self.task_id = "AUTO-%s" % uuid.uuid4().hex[:16]
            self.task_started.emit(self.task_id)
            res = autopilot.run_one(self.src, level=self.level, mode=self.mode,
                                    use_bgm=self.use_bgm,
                                    index_dir=self.index_dir,
                                    on_log=lambda s: self.log.emit(str(s)),
                                    output_dir=self.output_dir,
                                    burn_subs=self.burn_subs,
                                    loudness_norm=self.loudness_norm,
                                    tracks=self.tracks,
                                    keep_ratio=self.keep_ratio,
                                    should_cancel=lambda: self._stop,
                                     task_id=self.task_id,
                                     memory=memory,
                                     resume=bool(self.resume and memory is not None),
                                     model_editor=self.model_editor,
                                     on_progress=lambda payload: self.progress.emit(payload))
            if self._stop:
                self.cancelled.emit()
                return
            # run_one 返回 dict（out/report/minutes）；兼容旧版返回字符串
            if isinstance(res, dict):
                problems = (list(res.get("director_degraded") or [])
                            + list(res.get("render_degraded") or []))
                if problems:
                    self.log.emit("审核还有 %d 处问题没解决：成片已按降级出片"
                                  % len(problems))
                    self.degraded.emit(list(problems))
                self.done.emit(str(res.get("out") or ""))
            else:
                self.done.emit(str(res or ""))
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in ("IndexCancelled", "RenderCancelled") or self._stop:
                self.cancelled.emit()
            else:
                self.failed.emit("%s\n%s" % (exc, traceback.format_exc(limit=2)))
        finally:
            if memory is not None:
                try:
                    memory.flush()
                except Exception:
                    pass
                try:
                    memory.close()
                except Exception:
                    pass


class ExportWorker(QThread):
    done = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, src, cards, mode="single", out_dir="", out_path=""):
        super().__init__()
        self.src = src
        self.cards = cards
        self.mode = mode
        self.out_dir = out_dir
        self.out_path = out_path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            import export as ex
            if self.mode == "merge":
                ex.export_merged(self.src, self.cards, self.out_path,
                                 should_cancel=lambda: self._stop)
                if self._stop:
                    self.cancelled.emit()
                    return
                self.done.emit(self.out_path)
            else:
                os.makedirs(self.out_dir, exist_ok=True)
                ex.export_single(self.src, self.cards, self.out_dir,
                                 should_cancel=lambda: self._stop)
                if self._stop:
                    self.cancelled.emit()
                    return
                self.done.emit(self.out_dir)
        except Exception as exc:  # noqa: BLE001
            if self._stop or "取消" in str(exc):
                self.cancelled.emit()
            else:
                self.failed.emit("%s\n%s" % (exc, traceback.format_exc(limit=2)))


# ---------------------------------------------------------------- 主窗口
class MainWindowV2(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setMinimumSize(1180, 720)
        self.setObjectName("root")

        self.data = {}
        self.index_dir = ""
        self.src = ""
        self.cards_all = []
        self.cards_view = []
        self.selected = []            # 有顺序的卡片 dict
        self.tiles = {}
        self._worker = None
        self._last_out = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_titlebar())
        self.notice = NoticeBar()
        root.addWidget(self.notice)

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(8, 8, 8, 8)
        bl.setSpacing(8)
        bl.addWidget(self._build_left())
        bl.addWidget(self._build_center(), 1)
        bl.addWidget(self._build_right())
        root.addWidget(body, 1)

        self.status = StatusBar(self)
        root.addWidget(self.status)

        self.setAcceptDrops(True)
        self._install_shortcuts()
        self._load_settings_into_ui()
        self._refresh_stats()
        self._install_window_guard()
        QTimer.singleShot(300, self._run_selfcheck)

    def _background_work_running(self) -> bool:
        """Whether a task that may create native/subprocess windows is active."""
        workers = (
            getattr(self, "_worker", None),
            getattr(self, "_thumb_worker", None),
            getattr(getattr(self, "queue_panel", None), "worker", None),
        )
        for worker in workers:
            try:
                if worker is not None and worker.isRunning():
                    return True
            except (AttributeError, RuntimeError):
                continue
        return False

    def _install_window_guard(self):
        try:
            import window_guard
            self._window_guard_timer = window_guard.install_qt_timer(
                self, self._background_work_running)
        except Exception as exc:  # noqa: BLE001
            self._window_guard_timer = None
            try:
                import window_guard
                window_guard._log("窗口守卫安装失败：%s: %s" %
                                  (type(exc).__name__, exc))
            except Exception:
                pass

    # ------------------------------------------------------------ 标题栏
    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet("background:#181818;border-bottom:1px solid #3a3a3a;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size:13px;font-weight:600;")
        lay.addWidget(title)

        # 菜单
        for text, builder in (("文件", self._menu_file), ("工具", self._menu_tool),
                              ("教程", self._menu_help)):
            btn = QPushButton(text + " ▾")
            btn.setObjectName("ghost")
            btn.setFixedHeight(26)
            menu = QMenu(btn)
            builder(menu)
            btn.setMenu(menu)
            lay.addWidget(btn)

        lay.addStretch(1)
        self.btn_queue = QPushButton("队列")
        self.btn_queue.setObjectName("ghost")
        self.btn_queue.setFixedHeight(26)
        self.btn_queue.setToolTip("批量素材的分析 / 剪辑排队")
        self.btn_queue.clicked.connect(self.open_queue)
        lay.addWidget(self.btn_queue)
        self.lbl_env = QLabel("本地模型 · 显卡 · 就绪")
        self.lbl_env.setStyleSheet("color:#7d7d7d;font-size:11px;")
        lay.addWidget(self.lbl_env)
        return bar

    def _menu_file(self, m: QMenu):
        m.addAction("打开素材…", self.open_source)
        m.addAction("打开已有索引…", self.open_index_dir)
        m.addSeparator()
        m.addAction("打开导出目录", self.open_out_dir)
        m.addAction("打开安装目录", lambda: os.startfile(_home()))
        m.addSeparator()
        m.addAction("退出", self.close)

    def _menu_tool(self, m: QMenu):
        m.addAction("分析队列…", self.open_queue)
        m.addSeparator()
        m.addAction("设置…", self.open_settings)
        m.addAction("字幕 / 字幕样式…", self.open_subtitles)
        m.addAction("音轨…", self.open_tracks)
        m.addAction("本地模型存档…", self.open_models)
        m.addAction("训练…", self.open_train)
        m.addAction("自主剪辑（正式时间轴流程）…", self.open_autoedit)
        m.addAction("安装 / 更新 Premiere 工程插件…", self.install_pr_bridge)
        m.addSeparator()
        m.addAction("打开分析日志", self.open_logs)

    def _menu_help(self, m: QMenu):
        m.addAction("教程 / 快速开始", lambda: self._open_doc("README.md"))
        m.addAction("Premiere 插件教程",
                    lambda: self._open_doc(os.path.join("docs", "Premiere插件.md")))
        m.addAction("API 模型配置",
                    lambda: self._open_doc(os.path.join("docs", "API模型.md")))
        m.addAction("开发 / 用 AI 二次开发",
                    lambda: self._open_doc(os.path.join("docs", "开发与构建.md")))
        m.addAction("故障排查",
                    lambda: self._open_doc(os.path.join("docs", "故障排查.md")))
        m.addSeparator()
        m.addAction("关于 " + APP_NAME, self.open_about)

    # ------------------------------------------------------------ 左栏
    def _build_left(self) -> QWidget:
        left = QWidget()
        left.setFixedWidth(264)
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ① 队列（替代原来的素材空块）
        import ui_queuepanel
        self.queue_panel = ui_queuepanel.QueuePanel(self)
        self.queue_panel.options_provider = self._queue_options
        self.queue_panel.opened.connect(self._open_from_queue)
        self.queue_panel.expand_requested.connect(lambda: self.open_queue())
        self.queue_panel.item_finished.connect(self._on_queue_item_done)
        self.queue_panel.job_started.connect(self._queue_job_started)
        self.queue_panel.job_progress.connect(self._queue_job_progress)
        self.queue_panel.job_finished.connect(self._queue_job_finished)
        self.queue_panel.merged.connect(lambda p: self.status.ok(
            "合并完成：%s" % os.path.basename(p), ""))
        lay.addWidget(self.queue_panel, 2)

        # TaskMemory 驱动的岗位状态板。默认折叠，避免压缩低分辨率界面。
        from ui_role_status import RoleStatusBoard
        self.role_board = RoleStatusBoard(left)
        lay.addWidget(self.role_board)

        # ② 模式与比例：模式决定怎么剪，百分比决定留下多少有效内容。
        p2 = Panel("剪辑模式", "选择专项和内容保留比例", "②")
        import edit_modes
        self.mode_combo = QComboBox()
        for key, name, desc in edit_modes.choices():
            self.mode_combo.addItem(name, key)
            self.mode_combo.setItemData(self.mode_combo.count() - 1, desc, Qt.ToolTipRole)
        self.mode_combo.setFixedHeight(28)
        self.mode_combo.currentIndexChanged.connect(self._sync_main_mode)
        p2.add_widget(self.mode_combo)
        self.mode_hint = QLabel("")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setStyleSheet("color:#7d7d7d;font-size:10px;")
        p2.add_widget(self.mode_hint)
        strength = QLabel("剪辑保留比例")
        strength.setStyleSheet("color:#9a9a9a;font-size:11px;font-weight:600;")
        p2.add_widget(strength)
        ratio_row = QHBoxLayout()
        self.keep_ratio_spin = QSpinBox()
        self.keep_ratio_spin.setRange(15, 100)
        self.keep_ratio_spin.setSuffix(" %")
        self.keep_ratio_spin.setValue(75)
        self.keep_ratio_spin.setFixedWidth(92)
        self.keep_ratio_spin.setToolTip(
            "数值越低剪得越狠；百分比只统计有效内容，长时间无人说话的空段始终清理")
        self.keep_ratio_spin.valueChanged.connect(self._sync_main_keep_ratio)
        self.keep_ratio_spin.valueChanged.connect(self._refresh_stats)
        ratio_row.addWidget(self.keep_ratio_spin)
        ratio_hint = QLabel("越低越精简；空段不计入比例")
        ratio_hint.setStyleSheet("color:#777;font-size:10px;")
        ratio_row.addWidget(ratio_hint)
        ratio_row.addStretch(1)
        p2.add_layout(ratio_row)
        p2.body.addStretch(1)
        lay.addWidget(p2, 1)

        # ③ 输出
        p3 = Panel("输出", "存哪儿 / 带不带字幕", "③")
        self.out_edit = QLineEdit("")
        self.out_edit.setFixedHeight(28)
        self.out_edit.setPlaceholderText("本次覆盖目录；留空使用各操作的设置目录")
        p3.add_widget(self.out_edit)
        self.cb_burn = QCheckBox("出片时烧字幕")
        self.cb_bgm = QCheckBox("自动配 BGM")
        self.cb_bgm.setChecked(True)
        self.cb_loud = QCheckBox("按响度拉齐")
        self.cb_loud.setChecked(True)
        for cb in (self.cb_burn, self.cb_bgm, self.cb_loud):
            p3.add_widget(cb)
        self.lbl_sub = QLabel("字幕：默认样式 · 点这里改")
        self.lbl_sub.setStyleSheet("color:#9a9a9a;font-size:11px;")
        self.lbl_sub.setCursor(Qt.PointingHandCursor)
        self.lbl_sub.mousePressEvent = lambda e: self.open_subtitles()
        p3.add_widget(self.lbl_sub)
        lay.addWidget(p3, 1)
        return left

    # ------------------------------------------------------------ 中栏
    def _build_center(self) -> QWidget:
        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        bar = QWidget()
        bar.setFixedHeight(34)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索台词里的词…")
        self.search.setFixedHeight(28)
        self.search.textChanged.connect(self._apply_filter)
        bl.addWidget(self.search, 1)
        self.cb_cat = QComboBox()
        self.cb_cat.addItem("全部分类", None)
        self.cb_cat.setFixedHeight(28)
        self.cb_cat.currentIndexChanged.connect(self._apply_filter)
        bl.addWidget(self.cb_cat)
        self.cb_size = QComboBox()
        for lbl, sc in (("小图", 0.8), ("中图", 1.0), ("大图", 1.2)):
            self.cb_size.addItem(lbl, sc)
        self.cb_size.setCurrentIndex(1)
        self.cb_size.setFixedHeight(28)
        self.cb_size.currentIndexChanged.connect(lambda *_: self._relayout())
        bl.addWidget(self.cb_size)
        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("ghost")
        self.btn_all.setFixedHeight(28)
        self.btn_all.clicked.connect(self.select_all)
        bl.addWidget(self.btn_all)
        lay.addWidget(bar)

        self.card_panel = Panel("卡片", "单击选中 · 双击预览", "④")
        # 当前素材名（卡片面板头部右侧），其它地方会用 lbl_file 更新它
        self.lbl_file = QLabel("")
        self.lbl_file.setObjectName("panel2Hint")
        self.card_panel.head_extra.addWidget(self.lbl_file)
        self._cur_lbl = self.lbl_file
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.holder = QWidget()
        self.grid = QGridLayout(self.holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.setWidget(self.holder)
        # 让拖拽能穿透到窗口：viewport/holder 都接受拖放，事件转发给主窗口
        self.scroll.setAcceptDrops(True)
        self.scroll.viewport().setAcceptDrops(True)
        self.holder.setAcceptDrops(True)
        for _w in (self.scroll, self.scroll.viewport(), self.holder):
            _w.installEventFilter(self)
        self.card_panel.add_widget(self.scroll, 1)
        lay.addWidget(self.card_panel, 1)
        return center

    # ------------------------------------------------------------ 右栏
    def _build_right(self) -> QWidget:
        right = QWidget()
        right.setFixedWidth(344)
        lay = QVBoxLayout(right)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        p5 = Panel("选中片段", "这里改顺序和预览", "⑤")
        self.preview = QLabel("选中卡片后这里显示预览")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(108)
        self.preview.setStyleSheet("background:#1a1a1a;border:1px solid #3a3a3a;"
                                   "border-radius:4px;color:#7d7d7d;")
        p5.add_widget(self.preview)
        self.lbl_sel = QLabel("还没选")
        self.lbl_sel.setStyleSheet("color:#9a9a9a;font-size:11px;")
        p5.add_widget(self.lbl_sel)
        self.lbl_sel_text = QLabel("")
        self.lbl_sel_text.setWordWrap(True)
        self.lbl_sel_text.setStyleSheet("color:#b8b8b8;font-size:11px;")
        p5.add_widget(self.lbl_sel_text)
        from PySide6.QtWidgets import QListWidget, QAbstractItemView
        self.sel_list = QListWidget()
        self.sel_list.setFixedHeight(88)
        self.sel_list.setStyleSheet(
            "QListWidget{background:#1a1a1a;border:1px solid #3a3a3a;border-radius:4px;}"
            "QListWidget::item{padding:3px 6px;}")
        self.sel_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.sel_list.setDefaultDropAction(Qt.MoveAction)
        self.sel_list.model().rowsMoved.connect(self._on_sel_moved)
        p5.add_widget(self.sel_list)
        ops = QHBoxLayout()
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_rm = QPushButton("移除")
        self.btn_clear = QPushButton("清空")
        for b in (self.btn_up, self.btn_down, self.btn_rm, self.btn_clear):
            b.setObjectName("ghost")
            b.setFixedHeight(26)
            b.setEnabled(False)
            ops.addWidget(b)
        self.btn_up.clicked.connect(lambda: self._move_sel(-1))
        self.btn_down.clicked.connect(lambda: self._move_sel(1))
        self.btn_rm.clicked.connect(self._remove_sel)
        self.btn_clear.clicked.connect(self._clear_sel)
        p5.add_layout(ops)
        lay.addWidget(p5, 1)

        p6 = Panel("BGM", "1～3 分钟 · 优先段落过渡", "⑥")
        self.btn_bgm_settings = QPushButton("快速设置")
        self.btn_bgm_settings.setObjectName("ghost")
        self.btn_bgm_settings.setFixedHeight(24)
        self.btn_bgm_settings.setToolTip("设置 BGM 曲库和音量")
        self.btn_bgm_settings.clicked.connect(self.open_bgm_settings)
        p6.head_extra.addWidget(self.btn_bgm_settings)
        self.lbl_bgm = QLabel("曲库：读取中…")
        self.lbl_bgm.setWordWrap(True)
        self.lbl_bgm.setStyleSheet("color:#9a9a9a;font-size:11px;")
        p6.add_widget(self.lbl_bgm)
        # 音量直接嵌在面板里（不弹二级窗口）：点上下箭头就改，改完立刻写配置
        vol_row = QHBoxLayout()
        vol_row.setSpacing(6)
        vol_label = QLabel("音量")
        vol_label.setStyleSheet("color:#7d7d7d;font-size:11px;")
        vol_row.addWidget(vol_label)
        self.spin_bgm_vol = QDoubleSpinBox()
        self.spin_bgm_vol.setRange(-80.0, -6.0)
        self.spin_bgm_vol.setSingleStep(1.0)
        self.spin_bgm_vol.setDecimals(0)
        self.spin_bgm_vol.setSuffix(" dB")
        self.spin_bgm_vol.setFixedWidth(88)
        self.spin_bgm_vol.setToolTip("BGM 目标电平：设 -40 就真的混成 -40 dB")
        self.spin_bgm_vol.valueChanged.connect(self._on_bgm_volume_changed)
        vol_row.addWidget(self.spin_bgm_vol)
        vol_row.addStretch(1)
        p6.add_layout(vol_row)
        self.lbl_bgm2 = QLabel("接缝下压 4 dB")
        self.lbl_bgm2.setStyleSheet("color:#7d7d7d;font-size:11px;")
        p6.add_widget(self.lbl_bgm2)
        lay.addWidget(p6, 1)

        p7 = Panel("导出", "选好卡片点这里", "⑦")
        self.lbl_sel_stat = QLabel("已选 0 段 · 00:00.0")
        self.lbl_sel_stat.setStyleSheet("font-size:13px;font-weight:600;")
        p7.add_widget(self.lbl_sel_stat)
        self.lbl_est = QLabel("预计成片：打开素材后按保留比例估算")
        self.lbl_est.setStyleSheet("color:#9a9a9a;font-size:11px;")
        p7.add_widget(self.lbl_est)
        self.btn_export = QPushButton("导出选中（每段一个文件）")
        self.btn_export.setFixedHeight(30)
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(lambda: self.export_selected("single"))
        p7.add_widget(self.btn_export)
        self.btn_merge = QPushButton("按顺序合并成一条")
        self.btn_merge.setFixedHeight(30)
        self.btn_merge.setEnabled(False)
        self.btn_merge.clicked.connect(lambda: self.export_selected("merge"))
        p7.add_widget(self.btn_merge)
        self.btn_direct = QPushButton("模型剪辑（推荐）")
        self.btn_direct.setObjectName("primary")
        self.btn_direct.setFixedHeight(36)
        self.btn_direct.setEnabled(False)
        self.btn_direct.setToolTip(
            "剪辑师换成本地大模型：它看画面文字（OCR）、听台词和停顿，"
            "自己出剪辑单；边界/导演/转场/渲染仍走原来的岗位")
        self.btn_direct.clicked.connect(lambda _checked=False: self.direct_cut())
        p7.add_widget(self.btn_direct)
        tip = QLabel("看画面文字 + 听台词 → 模型出剪辑单 → 出片")
        tip.setAlignment(Qt.AlignCenter)
        tip.setStyleSheet("color:#7d7d7d;font-size:11px;")
        p7.add_widget(tip)
        lay.addWidget(p7, 1)
        return right

    # ------------------------------------------------------------ 快捷键
    def _install_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+O"), self, self.open_source)
        QShortcut(QKeySequence("Ctrl+R"), self, self.direct_cut)
        QShortcut(QKeySequence("Ctrl+E"), self, lambda: self.export_selected("single"))
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        QShortcut(QKeySequence("Esc"), self, self.status.collapse)

    # ------------------------------------------------------------ 设置
    def _cfg(self):
        try:
            import json
            p = os.path.join(_home(), "conf", "settings.json")
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _load_settings_into_ui(self):
        cfg = self._cfg()
        try:
            self.cb_burn.setChecked(str(cfg.get("burn_subs", "1")) in ("1", "true", "True"))
            self.cb_bgm.setChecked(str(cfg.get("bgm", "1")) in ("1", "true", "True"))
            import cut_strength
            self.keep_ratio_spin.setValue(cut_strength.percent(
                cut_strength.ratio_from_settings(cfg)))
            mode = str(cfg.get("edit_mode") or "gameplay")
            index = self.mode_combo.findData(mode)
            self.mode_combo.setCurrentIndex(index if index >= 0 else 0)
            self._sync_main_mode()
        except Exception:
            pass
        self._refresh_model_source_label(cfg)
        self._refresh_bgm()

    def _refresh_model_source_label(self, cfg=None):
        """Show the selected model source without ever exposing credentials."""
        try:
            import ai_api_settings
            cfg = cfg if isinstance(cfg, dict) else self._cfg()
            provider = ai_api_settings.normalize_provider(
                cfg.get("llm_provider"))
            if provider == "local":
                self.lbl_env.setText("本地模型 · 显卡 · 就绪")
                self.lbl_env.setToolTip("智能剪辑语言模型在本机运行")
                return
            labels = {key: label for key, label, _url in
                      ai_api_settings.PROVIDERS}
            model = str(cfg.get("llm_api_model") or "").strip()
            ready = bool(str(cfg.get("llm_api_base_url") or "").strip() and model)
            self.lbl_env.setText("%s · %s · %s" % (
                labels.get(provider, "AI API"), model or "未选模型",
                "已配置" if ready else "待配置"))
            self.lbl_env.setToolTip("API Key 已隐藏；在设置 → 识别与模型中修改")
        except Exception:
            self.lbl_env.setText("本地模型 · 显卡 · 就绪")

    def _refresh_bgm(self):
        try:
            import bgm
            p = os.path.join(_home(), "conf", "bgm.json")
            conf = bgm.load_conf(p)
            d = conf.get("folder") or ""
            self.cb_loud.setChecked(str(conf.get("loudness_norm", "1"))
                                    in ("1", "true", "True"))
            n = len(bgm.list_tracks(d)) if d and os.path.isdir(d) else 0
            self.lbl_bgm.setText("曲库：%s · %d 首" % (d or "（未设置）", n))
            try:
                level = "%g" % float(conf.get("level_db", -22) or -22)
            except (TypeError, ValueError):
                level = str(conf.get("level_db", -22) or -22)
            try:
                dip = "%g" % float(conf.get("dip_db", 4) or 4)
            except (TypeError, ValueError):
                dip = str(conf.get("dip_db", 4) or 4)
            self.lbl_bgm2.setText("接缝下压 %s dB" % dip)
            # 面板里的音量框跟着配置走（blockSignals 防止回写触发二次保存）
            spin = getattr(self, "spin_bgm_vol", None)
            if spin is not None:
                try:
                    spin.blockSignals(True)
                    spin.setValue(float(level))
                except (TypeError, ValueError, RuntimeError):
                    pass
                finally:
                    try:
                        spin.blockSignals(False)
                    except RuntimeError:
                        pass
            if n == 0:
                self.notice.show_bar("BGM 曲库是空的，本次出片不会加 BGM",
                                     "可以继续剪辑，也可以去设置里指定曲库目录。",
                                     "warn", "去设置", "",
                                     on_action=lambda a: self.open_bgm_settings())
        except Exception as exc:  # noqa: BLE001
            self.lbl_bgm.setText("曲库：读取失败（%s）" % str(exc)[:30])

    # ------------------------------------------------------------ 打开/分析
    def open_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择录播视频", "", "视频 (*.mp4 *.mov *.mkv *.flv *.ts *.avi *.m4v *.webm)")
        if not path:
            return
        self._reset_loaded_state()
        self.src = path
        self.lbl_file.setText(os.path.basename(path))
        self.preview.setText("已选择素材\n等待分析…")
        self._guess_index_or_analyze(path)

    def _guess_index_or_analyze(self, path):
        """有索引就直接读，没有就分析。"""
        try:
            import index as ix
            stem = os.path.splitext(os.path.basename(path))[0]
            root = os.path.join(ix.work_root(), "索引")
            cand = os.path.join(root, stem)
            if os.path.isfile(os.path.join(cand, "index.json")):
                self.load_index_dir(cand)
                return
        except Exception:
            pass
        self.start_analyze()

    def start_analyze(self, force=False):
        if not self.src:
            self.notice.show_bar("先打开一个视频", "支持 mp4 / mkv / mov，多音轨会自动挑说话那条。",
                                 "info", "打开素材", "", lambda a: self.open_source())
            return
        if self._worker and self._worker.isRunning():
            return
        try:
            import index as ix
            stem = os.path.splitext(os.path.basename(self.src))[0]
            out_dir = os.path.join(ix.work_root(), "索引", stem)
        except Exception:
            out_dir = os.path.splitext(self.src)[0] + "_索引"
        self._set_prog_visible(True)
        self._set_prog_text("准备分析…")
        self._set_open_enabled(False)
        import time as _t0
        self._an_t0 = _t0.time()
        self.status.working(True, "正在分析素材",
                            "抽帧 + 语音识别 + 语义分段 + 剧本",
                            0.0, "", on_cancel=self._cancel_analyze)

        model = os.path.join(_home(), "models")
        model_size = str(self._cfg().get("model") or "large-v3")
        self._worker = AnalyzeWorker(self.src, out_dir,
                                     model_size=model_size,
                                     model_dir=model if os.path.isdir(model) else None)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_analyzed)
        self._worker.failed.connect(self._on_analyze_failed)
        self._worker.cancelled.connect(self._on_analyze_cancelled)
        self._worker.start()

    def _on_progress(self, frac, msg):
        pct = frac if (frac is not None and frac >= 0) else None
        stage = (msg or "")[:70]
        eta = ""
        if hasattr(self, "_an_t0"):
            import time as _t
            spent = _t.time() - self._an_t0
            if pct and pct > 0.05:
                left = spent / max(pct, 0.01) - spent
                eta = "已用 %d:%02d · 预计还要 %d:%02d" % (
                    spent // 60, spent % 60, left // 60, left % 60)
        self.status.prop_work(pct=pct, stage=stage, eta=eta)
        if msg:
            self._set_prog_text(stage[:80])

    def _on_analyzed(self, data, out_dir):
        self._set_prog_visible(False)
        self._set_open_enabled(True)
        self.status.working(False)
        self.status.ok("分析完成：%d 段台词" % len((data or {}).get("segments") or []))
        self._apply_data(data, out_dir)

    def _on_analyze_failed(self, err):
        self._set_prog_visible(False)
        self._set_open_enabled(True)
        self.status.working(False)
        self.notice.show_bar("分析失败：%s" % err.splitlines()[0][:80],
                             "点「查看日志」看详细原因；常见是模型没下载完或显存不足。",
                             "error", "重新分析", "查看日志",
                             lambda a: (self.start_analyze(True) if a == "main"
                                        else self.open_logs()))

    def _on_analyze_cancelled(self):
        self._set_prog_visible(False)
        self._set_open_enabled(True)
        self.status.working(False)
        self.status.info("分析已取消", "")

    def _set_prog_text(self, text):
        w = getattr(self, "prog", None)
        if w is not None:
            w.setText(text)

    def _set_prog_visible(self, on):
        w = getattr(self, "prog", None)
        if w is not None:
            w.setVisible(on)

    def _set_open_enabled(self, on):
        w = getattr(self, "btn_open", None)
        if w is not None:
            w.setEnabled(on)

    def _cancel_analyze(self):
        w = getattr(self, "_worker", None)
        if w is not None and w.isRunning():
            w.stop()
            self.status.working(True, "正在取消分析", "等待当前步骤安全结束…",
                                on_cancel=None)

    def _run_selfcheck(self):
        """启动自检：缺 ffmpeg / 模型 / 工作目录不可写，直接说清楚。"""
        try:
            import selfcheck
            errors, warns = selfcheck.run()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("自检没跑成：%s" % exc, "", "warn")
            return
        if errors:
            self.notice.show_bar("启动自检发现 %d 个问题：%s"
                                 % (len(errors), errors[0]),
                                 "；".join(errors[1:3]) or "修好后重开即可",
                                 "error", "打开安装目录", "查看日志",
                                 lambda a: (os.startfile(_home()) if a == "main"
                                            else self.open_logs()))
        elif warns:
            self.notice.show_bar("自检提示：%s" % warns[0],
                                 "不影响使用，可忽略", "warn")

    def _queue_job_started(self, name):
        """队列开始 → 主界面显示 64px 大工作条。"""
        self._job_t0 = time.time()
        self.status.working(True, "正在分析素材", "队列：%s" % name, 0.0, "",
                            on_cancel=self._cancel_queue)
        self.role_board.bind_task(None)

    def _queue_job_progress(self, pct, stage):
        try:
            running = next((item for item in self.queue_panel.items
                            if item.get("status") == "run" and item.get("task_id")), None)
            if running:
                self.role_board.bind_task(running.get("task_id"))
        except Exception:
            pass
        if stage and "队列是空的" in stage:
            self.notice.show_bar("队列是空的", "把视频拖进窗口，或点「＋ 添加素材」。",
                                 "info")
            return
        eta = ""
        t0 = getattr(self, "_job_t0", None)
        if t0 and pct and pct > 0.03:
            spent = time.time() - t0
            left = spent / max(pct, 0.01) - spent
            eta = "已用 %d:%02d · 预计还要 %d:%02d" % (
                spent // 60, spent % 60, left // 60, left % 60)
        if pct is None or pct < 0:
            t0 = getattr(self, "_job_t0", None)
            if t0:
                spent = time.time() - t0
                eta = "已用 %d:%02d" % (spent // 60, spent % 60)
            self.status.prop_work(stage=(stage or "")[:70], eta=eta)
        else:
            self.status.prop_work(pct=pct, stage=(stage or "")[:70], eta=eta)

    def _queue_job_finished(self):
        self.status.working(False)
        self.role_board.refresh()

    def _cancel_queue(self):
        try:
            self.queue_panel.pause()
        except Exception:
            pass
        self.status.working(False)

    def _on_queue_item_done(self, path):
        """队列里某条分析完成：载入索引卡片，方便马上检查/微调。"""
        if not path:
            return
        try:
            import index as ix
            stem = os.path.splitext(os.path.basename(path))[0]
            cand = os.path.join(ix.work_root(), "索引", stem)
            if os.path.isfile(os.path.join(cand, "index.json")):
                self.load_index_dir(cand)
        except Exception:
            pass

    def _open_from_queue(self, path):
        """双击队列行：加载这条素材的索引，进入剪卡片模式。"""
        self.src = path
        self.lbl_file.setText(os.path.basename(path))
        self._guess_index_or_analyze(path)

    def _add_to_queue(self, paths):
        n = self.queue_panel.add_paths(paths) if hasattr(self, "queue_panel") else 0
        if n:
            self.status.info("已加入队列 %d 个素材" % n,
                             "拖动行调顺序，然后点「▶ 开始」", seconds=3)

    def open_index_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择索引目录（里面有 index.json）")
        if d:
            self.load_index_dir(d)

    def load_index_dir(self, d):
        try:
            import autocut as ac
            data = ac.load_index(d) or {}
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("这个索引读不出来：%s" % exc, "换一个目录，或重新分析。", "error")
            return
        if not data:
            self.notice.show_bar("索引是空的", "这个目录里没有可用的 index.json。", "error")
            return
        self._apply_data(data, d)

    def _retire_thumb_worker(self):
        """停止旧素材的缩略图任务，并保留对象到线程自然退出。"""
        w = getattr(self, "_thumb_worker", None)
        if w is not None:
            try:
                w.got.disconnect(self._on_thumb)
            except (RuntimeError, TypeError):
                pass
            try:
                if w.isRunning():
                    w.stop()
                    retired = getattr(self, "_retired_thumb_workers", None)
                    if retired is None:
                        retired = []
                        self._retired_thumb_workers = retired
                    if w not in retired:
                        retired.append(w)

                    def forget(old=w):
                        try:
                            self._retired_thumb_workers.remove(old)
                        except (AttributeError, ValueError):
                            pass
                        old.deleteLater()

                    w.finished.connect(forget)
                else:
                    w.deleteLater()
            except RuntimeError:
                pass
        self._thumb_worker = None
        self._thumb_tasks = []
        self._thumb_queued = set()

    def _reset_loaded_state(self):
        """切换素材前清掉选择、卡片、预览和旧缩略图任务。"""
        self._retire_thumb_worker()
        self.selected = []
        self.cards_all = []
        self.cards_view = []
        self.data = {}
        self.index_dir = ""
        self.spoken_sec = 0.0
        self.tiles.clear()
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.cb_cat.blockSignals(True)
        self.cb_cat.clear()
        self.cb_cat.addItem("全部分类", None)
        self.cb_cat.blockSignals(False)
        self.sel_list.clear()
        self.preview.setText("选中卡片后这里显示预览")
        self.lbl_sel.setText("还没选")
        self.lbl_sel_text.clear()
        self.lbl_file.clear()
        self.btn_direct.setEnabled(False)
        for button in (self.btn_up, self.btn_down, self.btn_rm, self.btn_clear,
                       self.btn_export, self.btn_merge):
            button.setEnabled(False)
        self._refresh_stats()

    def _apply_data(self, data, index_dir):
        self._reset_loaded_state()
        self.data = data or {}
        self.index_dir = index_dir
        src = (self.data.get("source") or "").strip()
        self.src = src
        dur = float(self.data.get("duration") or 0)
        cards = self.data.get("cards") or []
        if self.src:
            self.lbl_file.setText(os.path.basename(self.src))
            if hasattr(self, "thumb"):
                self.thumb.setText("▶  %s\n%d 张卡片" % (_fmt_min(dur), len(cards)))
        res = ""
        if self.data.get("width") and self.data.get("height"):
            res = "%dx%d" % (int(self.data["width"]), int(self.data["height"]))
        if hasattr(self, "lbl_meta"):
            self.lbl_meta.setText("%s · %d 张卡片 · %s"
                                  % (_fmt_min(dur), len(cards), res))
        if hasattr(self, "btn_reindex"):
            self.btn_reindex.setEnabled(True)
        self.btn_direct.setEnabled(True)
        # 口语时长：所有含台词的卡片加起来（用来算预估成片）
        spoken = sum(float(c.get("end", 0)) - float(c.get("start", 0))
                     for c in cards if (c.get("text") or "").strip())
        self.spoken_sec = spoken
        self._build_cards(cards if cards else (self.data.get("segments") or []))
        self._refresh_stats()

    # ------------------------------------------------------------ 卡片
    def _build_cards(self, rows):
        """兼容三种数据：index 的 cards（dict）、segments（[start,end,text]）、dict 段。"""
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self.tiles.clear()
        self.cards_all = []
        for i, sg in enumerate(rows):
            try:
                if isinstance(sg, dict):
                    st, en = float(sg.get("start") or 0), float(sg.get("end") or 0)
                    text = sg.get("text") or sg.get("title") or ""
                    cats = list(sg.get("cats") or sg.get("tags") or [])
                    if sg.get("highlight") and "高光" not in cats:
                        cats.append("高光")
                    cid = sg.get("id") or i
                elif isinstance(sg, (list, tuple)) and len(sg) >= 2:
                    st, en = float(sg[0]), float(sg[1])
                    text = str(sg[2]) if len(sg) > 2 else ""
                    cats, cid = [], i
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if en <= st:
                continue
            self.cards_all.append({"id": cid, "start": st, "end": en,
                                   "text": text.strip(), "cats": cats})
        names = sorted({str(cat) for card in self.cards_all
                        for cat in (card.get("cats") or []) if str(cat).strip()})
        self.cb_cat.blockSignals(True)
        self.cb_cat.clear()
        self.cb_cat.addItem("全部分类", None)
        for name in names:
            self.cb_cat.addItem(name, name)
        self.cb_cat.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self):
        kw = (self.search.text() or "").strip()
        cat = self.cb_cat.currentData()
        view = []
        for c in self.cards_all:
            if kw and kw not in (c.get("text") or ""):
                continue
            if cat and cat not in (c.get("cats") or []):
                continue
            view.append(c)
        self.cards_view = view
        self._page = 120
        self._rebuild_tiles(view)
        self.card_panel.head_extra.itemAt(self.card_panel.head_extra.count() - 1)
        self._update_card_hint(len(view))

    def _update_card_hint(self, n):
        lbl = getattr(self, "_hint_lbl", None)
        if lbl is None:
            lbl = QLabel("")
            lbl.setObjectName("panel2Hint")
            self.card_panel.head_extra.addWidget(lbl)
            self._hint_lbl = lbl
        lbl.setText("共 %d 段" % n)

    def _rebuild_tiles(self, cards):
        """一次只建 120 张，避免 5000 张卡把界面卡死；底部给「加载更多」。"""
        page = getattr(self, "_page", 120)
        shown = cards[:page]
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self.tiles.clear()
        cols = max(1, int((self.scroll.viewport().width() + 8) // 262))
        grid = self.grid
        for i, c in enumerate(shown):
            tile = CardTile(c)
            tile.clicked.connect(self.toggle_card)
            tile.doubleClicked.connect(self.preview_card)
            if self._is_selected(c):
                tile.set_selected(True)
            self.tiles[str(c["id"])] = tile
            grid.addWidget(tile, i // cols, i % cols)
        self._request_thumbs(shown)
        if len(cards) > len(shown):
            left = len(cards) - len(shown)
            more = QPushButton("加载更多（还有 %d 张）" % left)
            more.setFixedHeight(34)
            more.clicked.connect(self._load_more)
            grid.addWidget(more, len(shown) // cols + 1, 0, 1, cols)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)

    def _request_thumbs(self, cards):
        """给当前页没图的卡片排队抓帧（有缓存就直接贴上）。"""
        try:
            import ui_thumbs
        except Exception:
            return
        pending = []
        for c in cards:
            p = ui_thumbs.thumb_path(self.index_dir, c["id"])
            tile = self.tiles.get(str(c["id"]))
            if not tile:
                continue
            if os.path.isfile(p) and os.path.getsize(p) > 500:
                tile.set_thumb(p)
            else:
                pending.append((c["id"], c["start"]))
        if not self.src:
            return
        tasks = getattr(self, "_thumb_tasks", None)
        queued = getattr(self, "_thumb_queued", None)
        if tasks is None or queued is None:
            tasks, queued = [], set()
            self._thumb_tasks, self._thumb_queued = tasks, queued
        for cid, t in pending[:36]:
            if str(cid) in queued:
                continue
            queued.add(str(cid))
            tasks.append((cid, t))
        w = getattr(self, "_thumb_worker", None)
        if w is None or not w.isRunning():
            self._thumb_worker = ui_thumbs.ThumbWorker(self.src, self.index_dir, tasks)
            self._thumb_worker.got.connect(self._on_thumb)
            self._thumb_worker.start()

    def _on_thumb(self, cid, path):
        tile = self.tiles.get(str(cid))
        if tile:
            tile.set_thumb(path)

    def _load_more(self):
        self._page = getattr(self, "_page", 120) + 120
        self._rebuild_tiles(self.cards_view)

    def _relayout(self):
        self._rebuild_tiles(self.cards_view)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(120, self._relayout)

    # ------------------------------------------------------------ 选中
    def _is_selected(self, c):
        return any(str(x["id"]) == str(c["id"]) for x in self.selected)

    def toggle_card(self, cid):
        c = next((x for x in self.cards_all if str(x["id"]) == str(cid)), None)
        if not c:
            return
        if self._is_selected(c):
            self.selected = [x for x in self.selected if str(x["id"]) != str(c["id"])]
        else:
            self.selected.append(c)
        tile = self.tiles.get(str(cid))
        if tile:
            tile.set_selected(self._is_selected(c))
        self._refresh_stats()

    def select_all(self):
        if len(self.selected) == len(self.cards_view):
            self.selected = []
        else:
            self.selected = list(self.cards_view)
        for cid, tile in self.tiles.items():
            c = next((x for x in self.cards_all if str(x["id"]) == str(cid)), None)
            if c:
                tile.set_selected(self._is_selected(c))
        self._refresh_stats()

    def _move_sel(self, delta):
        if not self.selected:
            return
        i = max(0, self.sel_list.currentRow())
        j = min(max(i + delta, 0), len(self.selected) - 1)
        if i == j:
            return
        self.selected[i], self.selected[j] = self.selected[j], self.selected[i]
        self._refresh_stats()
        self.sel_list.setCurrentRow(j)

    def _remove_sel(self):
        if not self.selected:
            return
        i = max(0, self.sel_list.currentRow())
        c = self.selected.pop(i)
        tile = self.tiles.get(str(c["id"]))
        if tile:
            tile.set_selected(False)
        self._refresh_stats()
        self.sel_list.setCurrentRow(min(i, len(self.selected) - 1))

    def _clear_sel(self):
        self.selected = []
        for t in self.tiles.values():
            t.set_selected(False)
        self._refresh_stats()

    def _preview_audio_request(self, src):
        """Return the tracks requested for preview and whether selection is explicit.

        A per-queue-item choice belongs to that source and therefore wins over the
        global audio setting.  With no saved choice we keep the previous behavior:
        pass the index's complete stream list so every track is audible.
        """
        def normalized(value):
            if not isinstance(value, (list, tuple, set)):
                return None
            result = []
            for item in value:
                try:
                    ordinal = int(item.get("ordinal") if isinstance(item, dict) else item)
                except (TypeError, ValueError):
                    continue
                if ordinal >= 0 and ordinal not in result:
                    result.append(ordinal)
            return result or None

        try:
            wanted = os.path.normcase(os.path.abspath(src))
            for item in getattr(getattr(self, "queue_panel", None), "items", []):
                item_src = os.path.normcase(os.path.abspath(str(item.get("path") or "")))
                if item_src == wanted:
                    tracks = normalized(item.get("tracks"))
                    if tracks is not None:
                        return tracks, True
        except Exception:
            pass

        try:
            import ui_dialogs
            tracks = normalized(
                (ui_dialogs.load_render_conf().get("render") or {}).get("tracks")
            )
            if tracks is not None:
                return tracks, True
        except Exception:
            pass

        return self.data.get("audio_streams") or None, False

    def preview_card(self, cid):
        c = next((x for x in self.cards_all if str(x["id"]) == str(cid)), None)
        if not c:
            return
        self.preview.setText("▶ %.1f 秒\n%s" % (float(c["end"]) - float(c["start"]),
                                                (c.get("text") or "（无台词）")[:40]))
        self.lbl_sel.setText("%s – %s · %.1f 秒" % (_fmt(c["start"]), _fmt(c["end"]),
                                                    float(c["end"]) - float(c["start"])))
        self.lbl_sel_text.setText((c.get("text") or "")[:80])
        for b in (self.btn_up, self.btn_down, self.btn_rm):
            b.setEnabled(True)
        src = (self.data.get("source") or self.src or "").strip()
        if not os.path.isfile(src):
            self.notice.show_bar("源文件不在了", "找不到：%s" % src, "error")
            return
        worker = getattr(self, "_worker", None)
        try:
            if worker is not None and worker.isRunning():
                self.status.info("正在处理任务，完成后再预览", "", seconds=3)
                return
        except RuntimeError:
            pass
        try:
            import cards_ui
            streams, selected_only = self._preview_audio_request(src)
            dlg = cards_ui.PreviewDialog(
                src=src, card=c, parent=self,
                audio_streams=streams,
                audio_selection_explicit=selected_only,
            )
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("卡片预览打不开：%s" % exc,
                                 "可以检查源文件或音视频解码器。", "error")

    def _sync_sel_panel(self):
        """把选中列表同步到 ⑤ 面板（顺序 = 出片顺序）。"""
        try:
            self.sel_list.blockSignals(True)
            self.sel_list.clear()
            for i, c in enumerate(self.selected):
                self.sel_list.addItem("%d. %s – %s  %s" % (
                    i + 1, _fmt(c["start"]), _fmt(c["end"]),
                    (c.get("text") or "（无台词）")[:16]))
            self.sel_list.blockSignals(False)
            n = len(self.selected)
            self.lbl_sel.setText("已选 %d 段 · 共 %s"
                                 % (n, _fmt(sum(float(c["end"]) - float(c["start"])
                                                for c in self.selected))))
            has = n > 0
            for b in (self.btn_up, self.btn_down, self.btn_rm, self.btn_clear,
                      self.btn_export, self.btn_merge):
                b.setEnabled(has)
            if has:
                c = self.selected[0]
                self.preview.setText("▶ %.1f 秒\n%s" % (
                    float(c["end"]) - float(c["start"]),
                    (c.get("text") or "（无台词）")[:36]))
                self.sel_list.setCurrentRow(0)
            else:
                self.preview.setText("选中卡片后这里显示预览")
        except Exception:
            pass

    def _on_sel_moved(self, *args):
        """列表里拖动排序后，把顺序同步回 selected。"""
        try:
            order = []
            for i in range(self.sel_list.count()):
                txt = self.sel_list.item(i).text()
                idx = int(txt.split(".", 1)[0]) - 1
                if 0 <= idx < len(self.selected):
                    order.append(self.selected[idx])
            # 拖动后文本序号还是旧的，按当前行顺序重排一次即可
            if len(order) == len(self.selected):
                self.selected = order
            self._sync_sel_panel()
            self._refresh_stats()
        except Exception:
            pass

    def _refresh_stats(self):
        self._sync_sel_panel()
        n = len(self.selected)
        total = sum(float(c["end"]) - float(c["start"]) for c in self.selected)
        self.lbl_sel_stat.setText("已选 %d 段 · %s" % (n, _fmt(total)))
        has = n > 0
        for b in (self.btn_export, self.btn_merge):
            b.setEnabled(has)
        dur = float(self.data.get("duration") or 0)
        if dur > 0:
            spoken = float(getattr(self, "spoken_sec", 0) or dur * 0.75)
            keep_ratio = self.keep_ratio_value()
            estimate = min(spoken * keep_ratio, dur)
            self.lbl_est.setText(
                "目标保留 %.0f%% 有效内容，预计约 %s（无对白长空段不计）"
                % (keep_ratio * 100.0, _fmt_min(estimate)))
        else:
            self.lbl_est.setText("预计成片：打开素材后显示")

    # ------------------------------------------------------------ 动作
    def _queue_options(self):
        """队列开工时读取主界面的实时选项，而不是复制一份过期配置。"""
        cfg = self._cfg()
        return {
            "use_bgm": self.cb_bgm.isChecked(),
            "burn_subs": self.cb_burn.isChecked(),
            "loudness_norm": self.cb_loud.isChecked(),
            "output_dir": self.out_edit.text().strip(),
            "model_size": str(cfg.get("model") or "large-v3"),
            "edit_mode": self.current_mode(),
            "keep_ratio": self.keep_ratio_value(),
        }

    def current_mode(self):
        if hasattr(self, "mode_combo"):
            return str(self.mode_combo.currentData() or "gameplay")
        return "gameplay"

    def _sync_main_mode(self, *_):
        try:
            import edit_modes
            key = edit_modes.normalize(self.current_mode())
            self.mode_hint.setText(edit_modes.profile(key)["description"])
        except Exception:
            key = "gameplay"
        try:
            import cards_ui
            cfg = cards_ui.load_settings()
            cfg["edit_mode"] = key
            cards_ui.save_settings(cfg)
        except Exception:
            pass
        try:
            self._refresh_stats()
        except Exception:
            pass

    def _sync_main_keep_ratio(self, value):
        """Freeze the exact percentage into settings and pending queue rows."""
        import cut_strength
        keep_ratio = cut_strength.normalize_ratio(value)
        key = cut_strength.level_for_ratio(keep_ratio)
        try:
            if hasattr(self.queue_panel, "set_main_keep_ratio"):
                self.queue_panel.set_main_keep_ratio(keep_ratio)
            else:
                self.queue_panel.main_level = key
        except Exception:
            pass
        try:
            import cards_ui
            cfg = cards_ui.load_settings()
            cfg["keep_ratio"] = keep_ratio
            cfg["cut_level"] = key
            cards_ui.save_settings(cfg)
        except Exception:
            pass

    def keep_ratio_value(self):
        import cut_strength
        if hasattr(self, "keep_ratio_spin"):
            return cut_strength.normalize_ratio(self.keep_ratio_spin.value())
        return cut_strength.DEFAULT_KEEP_PERCENT / 100.0

    def level(self):
        import cut_strength
        return cut_strength.level_for_ratio(self.keep_ratio_value())

    def direct_cut(self, request=None):
        # QPushButton.clicked(bool) can still reach this slot through older
        # connections or callers; it is not a resume request.
        if isinstance(request, bool):
            request = None
        if not self.src:
            self.notice.show_bar("先打开素材", "导演剪辑需要原片和索引。", "info", "打开素材", "",
                                 lambda a: self.open_source())
            return
        if self._worker and self._worker.isRunning():
            return
        worker = None
        try:
            if request is None:
                output_dir = self.out_edit.text().strip() or self._configured_out_dir("out_autocut")
                tracks = None
                try:
                    import cards_ui
                    raw_tracks = str(cards_ui.load_settings().get(
                        "autocut_tracks", "") or "")
                    tracks = []
                    for value in raw_tracks.replace("，", ",").split(","):
                        value = value.strip()
                        if not value.isdigit() or int(value) <= 0:
                            continue
                        ordinal = int(value) - 1
                        if ordinal not in tracks:
                            tracks.append(ordinal)
                    if not tracks:
                        tracks = None
                except Exception:  # noqa: BLE001
                    tracks = None
                request = {
                    "src": self.src, "level": self.level(), "mode": self.current_mode(),
                    "keep_ratio": self.keep_ratio_value(),
                    "use_bgm": self.cb_bgm.isChecked(),
                    "index_dir": self.index_dir or None, "output_dir": output_dir,
                    "burn_subs": self.cb_burn.isChecked(),
                    "loudness_norm": self.cb_loud.isChecked(),
                    "tracks": tracks,
                    "model_editor": True,
                    "task_id": None, "resume": False,
                }
            elif isinstance(request, Mapping):
                request = dict(request)
            else:
                raise TypeError("剪辑请求必须是字典")

            self._cut_degraded = []
            worker = CutWorker(
                request["src"], level=request["level"], mode=request["mode"],
                use_bgm=request["use_bgm"], index_dir=request.get("index_dir"),
                output_dir=request.get("output_dir"),
                burn_subs=request.get("burn_subs", False),
                loudness_norm=request.get("loudness_norm", True),
                tracks=request.get("tracks"),
                keep_ratio=request.get("keep_ratio"),
                model_editor=request.get("model_editor", False),
                task_id=request.get("task_id"), resume=request.get("resume", False))
            worker.log.connect(lambda s: self.status.prop_work(stage=str(s)[:70]))
            worker.task_started.connect(self._on_cut_task_started)
            worker.progress.connect(self._on_cut_progress)
            worker.done.connect(self._on_cut_done)
            degraded_signal = getattr(worker, "degraded", None)
            if degraded_signal is not None and hasattr(degraded_signal, "connect"):
                degraded_signal.connect(self._on_cut_degraded)
            worker.failed.connect(self._on_cut_failed)
            worker.cancelled.connect(self._on_cut_cancelled)

            # Only show a running state after the worker has actually started.
            self._active_cut_request = request
            self._worker = worker
            worker.start()
        except Exception as exc:  # noqa: BLE001
            running = False
            if worker is not None:
                try:
                    running = worker.isRunning()
                except (AttributeError, RuntimeError):
                    pass
            if running:
                self.btn_direct.setEnabled(False)
                self._cut_progress_fraction = 0.0
                self.status.working(True, "模型剪辑中",
                                    "看画面文字 + 听台词 → 模型出剪辑单 → 出片",
                                    0.02, "", on_cancel=self._cancel_cut)
                self.notice.show_bar("剪辑启动提示", "启动时出现异常，但后台线程仍在运行：%s"
                                     % str(exc)[:120], "warn")
                return
            self._active_cut_request = None
            self._worker = None
            self._cut_progress_fraction = 0.0
            self.btn_direct.setEnabled(True)
            self.status.working(False)
            self.notice.show_bar("剪辑未能启动", "%s：%s"
                                 % (type(exc).__name__, str(exc)[:160]),
                                 "error", "查看日志", "", lambda _action: self.open_logs())
            return

        self.btn_direct.setEnabled(False)
        self._cut_progress_fraction = 0.0
        self.status.working(True, "模型剪辑中",
                            "模型读片 → 出剧本 → 初剪 → 导演审片返工 → 出片",
                            0.02, "", on_cancel=self._cancel_cut)

    def _on_cut_task_started(self, task_id):
        self.role_board.bind_task(task_id)
        request = dict(getattr(self, "_active_cut_request", {}) or {})
        request["task_id"] = str(task_id)
        self._active_cut_request = request

    def _on_cut_progress(self, payload):
        if not isinstance(payload, dict) or payload.get("stage") != "director":
            return
        total = max(0, int(payload.get("total") or 0))
        resolved = max(0, int(payload.get("resolved") or 0))
        remaining = max(0, int(payload.get("remaining") or 0))
        round_no = max(0, int(payload.get("round") or 0))
        reported = payload.get("fraction")
        try:
            pct = float(reported) if reported is not None else (
                resolved / float(total) if total else 0.0)
        except (TypeError, ValueError):
            pct = (resolved / float(total)) if total else 0.0
        pct = min(1.0, max(0.0, pct))
        self._cut_progress_fraction = pct
        stage = "发现 %d 个问题 · 已解决 %d 个 · 剩余 %d 个" % (
            total, resolved, remaining)
        if round_no:
            stage += " · 第 %d 次局部修复" % round_no
        reopened = max(0, int(payload.get("reopened") or 0))
        if reopened:
            stage += " · 复发 %d 个" % reopened
        message = str(payload.get("message") or "")
        if message:
            stage += " · " + message
        self.status.working(True, "导演正在执导返工", stage, pct, "",
                            on_cancel=self._cancel_cut)

    def _on_cut_degraded(self, problems):
        """导演没过但按降级出片：记下问题数，出片完成后提示。"""
        self._cut_degraded = list(problems or [])

    def _on_cut_done(self, out):
        self.btn_direct.setEnabled(True)
        self.status.working(False)
        self._last_out = out
        name = os.path.basename(out) if out else "成片"
        self.status.ok("出片完成：%s" % name, "")
        self._failed_cut_request = None
        problems = list(getattr(self, "_cut_degraded", None) or [])
        if problems:
            last = problems[-1]
            detail = last.get("detail") if isinstance(last, dict) else ""
            self.notice.show_bar(
                "已出片：导演还有 %d 处问题待修" % len(problems),
                "%s；成片照常保存，明细在日志里。" % (detail or ""),
                "warn", "查看日志", "", lambda a: self.open_logs())
        else:
            self.notice.hide_bar()
        self._cut_degraded = []

    def _on_cut_failed(self, err):
        self.btn_direct.setEnabled(True)
        self.status.working(False)
        request = dict(getattr(self, "_active_cut_request", {}) or {})
        request["resume"] = bool(request.get("task_id"))
        self._failed_cut_request = request
        self.notice.show_bar("剪辑失败：%s" % err.splitlines()[0][:80],
                             "导演已保留问题点；继续返工会从失败岗位恢复。",
                             "error", "继续返工", "查看日志",
                             lambda a: (self._retry_failed_cut()
                                        if a == "main" else self.open_logs()))

    def _retry_failed_cut(self):
        request = dict(getattr(self, "_failed_cut_request", {}) or {})
        if not request:
            self.direct_cut()
            return
        self.direct_cut(request=request)

    def _on_cut_cancelled(self):
        self.btn_direct.setEnabled(True)
        self.status.working(False)
        self.status.info("剪辑已取消", "")

    def _cancel_cut(self):
        w = getattr(self, "_worker", None)
        if w is not None and w.isRunning():
            w.stop()
            self.status.working(True, "正在取消剪辑", "等待当前片段安全结束…",
                                on_cancel=None)

    def _cancel_export(self):
        w = getattr(self, "_worker", None)
        if w is not None and w.isRunning():
            w.stop()
            self.status.working(True, "正在取消导出", "等待当前片段安全结束…",
                                on_cancel=None)

    def export_selected(self, mode):
        if not self.selected:
            self.notice.show_bar("还没选卡片", "在中间点几张卡片，再导出。", "info")
            return
        if self._worker and self._worker.isRunning():
            return
        kind = "out_merge" if mode == "merge" else "out_single"
        out_dir = self.out_edit.text().strip() or self._configured_out_dir(kind)
        if mode == "merge":
            out_path = os.path.join(out_dir, os.path.splitext(os.path.basename(self.src))[0]
                                    + "_合并.mp4")
        else:
            out_path = ""
        self.status.working(True, "正在导出", "准备输出片段…", 0.0, "",
                            on_cancel=self._cancel_export)
        self._worker = ExportWorker(self.src, list(self.selected), mode,
                                    out_dir=out_dir, out_path=out_path)
        self._worker.done.connect(self._on_export_done)
        self._worker.failed.connect(self._on_export_failed)
        self._worker.cancelled.connect(self._on_export_cancelled)
        self._worker.start()

    def _on_export_done(self, path):
        self.status.working(False)
        self.status.ok("导出完成：%s" % os.path.basename(path or "导出目录"))

    def _on_export_failed(self, err):
        self.status.working(False)
        self.notice.show_bar("导出失败：%s" % err.splitlines()[0][:80],
                             "查看日志。", "error")

    def _on_export_cancelled(self):
        self.status.working(False)
        self.status.info("导出已取消", "")

    def _configured_out_dir(self, kind):
        try:
            import cards_ui
            return cards_ui.resolve_out_dir(kind, self.src)
        except Exception:
            return self._default_out_dir()

    def _default_out_dir(self):
        try:
            import index as ix
            return os.path.join(ix.work_root(), "导出",
                                os.path.splitext(os.path.basename(self.src))[0])
        except Exception:
            return os.path.join(os.path.dirname(self.src), "导出")

    # ------------------------------------------------------------ 打开对话框
    def open_settings(self):
        try:
            import ui_settings
            ui_settings.SettingsDialogV2(self).exec()
        except Exception as exc:  # noqa: BLE001
            try:
                import cards_ui
                cards_ui.SettingsDialog(self).exec()
            except Exception:  # noqa: BLE001
                self.notice.show_bar("设置打不开：%s" % exc, "", "error")
                return
        self._load_settings_into_ui()

    def _on_bgm_volume_changed(self, value):
        """面板里的音量框一改就写配置（不弹二级窗口），下次出片按这个值混。"""
        import bgm
        path = os.path.join(_home(), "conf", "bgm.json")
        try:
            bgm.save_conf({"level_db": "%.0f" % float(value)}, path)
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("音量没保存：%s" % str(exc)[:80], "", "error")
            return
        self.notice.show_bar(
            "BGM 音量已设为 %.0f dB（下次出片按这个值混）" % float(value),
            "", "info")

    def open_bgm_settings(self):
        """从主界面 BGM 面板直接进入曲库和音量设置。"""
        try:
            import ui_settings
            dlg = ui_settings.SettingsDialogV2(self, initial_page=3)
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            try:
                import cards_ui
                cards_ui.SettingsDialog(self).exec()
            except Exception:  # noqa: BLE001
                self.notice.show_bar("BGM 设置打不开：%s" % exc, "", "error")
                return
        self._load_settings_into_ui()

    def open_subtitles(self):
        try:
            import cards_ui
            cards_ui.SubtitleDialog(self, self.data or {}).exec()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("字幕窗口打不开：%s" % exc, "", "error")

    def open_tracks(self):
        try:
            import ui_dialogs
            ui_dialogs.AudioDialog(self, self.src).exec()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("音轨窗口打不开：%s" % exc, "", "error")

    def open_queue(self, paths=None):
        """展开高级队列窗口（单项设置 / 四步清单 / 合并结果）。"""
        try:
            import ui_queue
            if not paths and hasattr(self, "queue_panel"):
                paths = [x["path"] for x in self.queue_panel.items]
            dlg = ui_queue.QueueDialog(self, paths=paths,
                                      panel=getattr(self, "queue_panel", None))
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("队列窗口打不开：%s" % exc, "", "error")

    # ---------------- 拖入素材 ----------------
    def _drag_overlay(self, show: bool, text: str = ""):
        """拖入提示要盖在**队列面板**上（文件就是进队列），不是卡片区。"""
        try:
            target = getattr(self, "queue_panel", None) or self
            if show:
                if getattr(self, "_drop_tip", None) is None:
                    lab = QLabel("松手加入队列", target)
                    lab.setAlignment(Qt.AlignCenter)
                    lab.setWordWrap(True)
                    lab.setStyleSheet(
                        "background:rgba(74,144,217,0.20);border:2px dashed #4a90d9;"
                        "border-radius:6px;color:#cfe3ff;font-size:14px;font-weight:600;")
                    self._drop_tip = lab
                tip = self._drop_tip
                if tip.parent() is not target:
                    tip.setParent(target)
                w = max(120, target.width() - 12)
                h = max(120, target.height() - 12)
                tip.setGeometry(6, 30, w, max(80, h - 34))
                tip.setText(text or "松手加入队列")
                tip.show()
                tip.raise_()
            else:
                tip = getattr(self, "_drop_tip", None)
                if tip is not None:
                    tip.hide()
        except Exception:
            pass

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            n = len([u for u in ev.mimeData().urls() if u.isLocalFile()])
            self._drag_overlay(True, "松手加入队列\n（%d 个文件）" % (n or 1))
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragLeaveEvent(self, ev):
        self._drag_overlay(False)
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev):
        self._drag_overlay(False)
        ev.acceptProposedAction()
        paths = [u.toLocalFile() for u in ev.mimeData().urls()
                 if u.toLocalFile().lower().endswith(
                     (".mp4", ".mov", ".mkv", ".flv", ".ts", ".avi", ".m4v", ".webm"))]
        if not paths:
            return
        # 拖进来的素材加进左栏队列：先排顺序，再点开始
        self._add_to_queue(paths)

    def open_models(self):
        try:
            import cards_ui
            import ui_dialogs
            dlg = cards_ui.ModelDialog(self)
            ui_dialogs.apply_shell(dlg, "本地模型存档",
                                   "选择一个本地大模型；所有本地岗位和字幕流程统一跟随",
                                   size=(900, 680))
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("模型窗口打不开：%s" % exc, "", "error")

    def open_autoedit(self):
        """自主剪辑：复用正式时间轴岗位、导演和交付门禁。"""
        try:
            from autoedit_qt import AutoEditDialog
            dlg = AutoEditDialog(self)
            self._autoedit_dlg = dlg      # 非模态，留引用防回收
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("自主剪辑窗口打不开：%s" % exc, "", "error")

    def open_train(self):
        try:
            import cards_ui
            import ui_dialogs
            dlg = cards_ui.TrainDialog(self)
            ui_dialogs.apply_shell(dlg, "训练 · 教它你的剪辑口味",
                                   "用你手剪过的成片和原片配对，学你的取舍",
                                   size=(960, 700))
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("训练窗口打不开：%s" % exc, "", "error")

    def open_about(self):
        try:
            import cards_ui
            cards_ui.MainWindow._show_about(self)
        except Exception:
            QMessageBox.information(self, "关于 " + APP_NAME,
                                    "版本 %s\n本地运行" % VERSION)

    def open_logs(self):
        try:
            import index as ix
            d = os.path.join(ix.work_root(), "日志")
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("打不开日志目录：%s" % exc, "", "error")

    def install_pr_bridge(self):
        try:
            import pr_bridge_installer
            pr_bridge_installer.show_installer(self, home=_home())
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("Premiere 插件安装器打不开：%s" % exc,
                                 "", "error")

    def open_out_dir(self):
        d = self.out_edit.text().strip() or self._configured_out_dir("out_autocut")
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as exc:  # noqa: BLE001
            self.notice.show_bar("打不开导出目录：%s" % exc, "", "error")

    def eventFilter(self, obj, ev):
        et = ev.type()
        from PySide6.QtCore import QEvent
        if et in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop, QEvent.DragLeave):
            try:
                if et == QEvent.DragEnter:
                    self.dragEnterEvent(ev)
                elif et == QEvent.DragMove:
                    self.dragMoveEvent(ev)
                elif et == QEvent.DragLeave:
                    self.dragLeaveEvent(ev)
                else:
                    self.dropEvent(ev)
                return True
            except Exception:
                return False
        return super().eventFilter(obj, ev)

    def closeEvent(self, ev):
        workers = []
        for w in (getattr(self, "_worker", None),
                  getattr(self, "_thumb_worker", None),
                  getattr(getattr(self, "queue_panel", None), "worker", None)):
            if w is not None and all(w is not old for old in workers):
                workers.append(w)
        workers.extend(w for w in getattr(self, "_retired_thumb_workers", [])
                       if w is not None and all(w is not old for old in workers))

        for w in workers:
            try:
                if w.isRunning():
                    w.stop()
            except Exception:
                pass

        still_running = []
        for w in workers:
            try:
                if w.isRunning() and not w.wait(4000):
                    still_running.append(w)
            except Exception:
                try:
                    if w.isRunning():
                        still_running.append(w)
                except Exception:
                    pass

        if still_running:
            ev.ignore()
            self.status.working(True, "正在安全停止后台任务",
                                "请稍候，任务退出后再关闭窗口…", on_cancel=None)
            self.notice.show_bar("还有后台任务正在收尾",
                                 "为避免成片损坏或程序崩溃，暂时不能强制关闭。",
                                 "warn")
            return
        try:
            self.role_board.shutdown()
        except Exception:
            pass
        super().closeEvent(ev)

    def _open_doc(self, name):
        p = os.path.join(_home(), name)
        if not os.path.isfile(p):
            QMessageBox.information(self, "找不到教程", "文档不存在：%s" % p)
            return
        try:
            os.startfile(p)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开教程", "%s\n%s" % (p, exc))


# ---------------------------------------------------------------- 工具函数
def _home() -> str:
    return os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))


def _fmt(t: float) -> str:
    t = max(0.0, float(t))
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def _fmt_min(sec: float) -> str:
    m = max(0.0, float(sec)) / 60.0
    return "%.1f 分钟" % m if m >= 1 else "%.0f 秒" % (m * 60)


def main():
    try:
        import no_console
        no_console.install()
    except Exception:
        pass
    app = QApplication(sys.argv)
    try:
        import cards_ui
        cards_ui._set_app_identity(app)
    except Exception:
        pass
    ui_theme.apply(app)
    w = MainWindowV2()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""队列窗口（设计稿 v4）：批量素材的分析 / 剪辑排队。

1180×760，左列表（拖拽排序 + 行内进度 + 状态色），右详情（四步清单 + 单项设置 +
已完成），顶部工具栏（全部开始/暂停/取消/清空 + 批量力度）。

队列顺序就是批量出片的处理顺序。
"""
from __future__ import annotations

import os
import shutil
import time
import traceback

from PySide6.QtCore import QSize, Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

import ui_theme
import ui_widgets as W
from product_info import PRODUCT_NAME
from ui_widgets import Panel

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".flv", ".ts", ".avi", ".m4v", ".webm")
STATUS_TEXT = {"wait": "等待中", "run": "分析中", "done": "已完成",
               "fail": "失败", "stop": "已取消"}
STATUS_COLOR = {"wait": "#7d7d7d", "run": "#4a90d9", "done": "#4caf72",
                "fail": "#d05a5a", "stop": "#a0a0a0"}


class QueueRow(QWidget):
    """队列的一行：名称、状态、进度条（设计稿要求的行内进度）。"""

    def __init__(self, item, idx, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = QLabel(os.path.basename(item["path"]), self)
        self.name.setStyleSheet("font-size:12px;")
        top.addWidget(self.name, 1)
        self.st = QLabel(STATUS_TEXT.get(item["status"], "") +
                         ((" · " + item["msg"]) if item.get("msg") and item["status"] not in ("wait",) else ""), self)
        self.st.setStyleSheet("color:%s;font-size:11px;" % STATUS_COLOR.get(item["status"], "#7d7d7d"))
        top.addWidget(self.st)
        lay.addLayout(top)
        self.bar = QProgressBar(self)
        self.bar.setFixedHeight(4)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(int(float(item.get("pct") or 0) * 100))
        if item["status"] == "done":
            self.bar.setStyleSheet("QProgressBar{background:#2a2a2a;border:none;border-radius:2px;}"
                                   "QProgressBar::chunk{background:#4caf72;border-radius:2px;}")
        elif item["status"] == "fail":
            self.bar.setStyleSheet("QProgressBar{background:#2a2a2a;border:none;border-radius:2px;}"
                                   "QProgressBar::chunk{background:#d05a5a;border-radius:2px;}")
        lay.addWidget(self.bar)
        self.bar.setVisible(item["status"] != "wait")

class QueueWorker(QThread):
    """顺序分析素材；只有显式 auto_cut=True 才继续自动出片流程。"""

    item_state = Signal(int, str, float, str)   # idx, status, pct, text
    item_done = Signal(int, str)                # idx, 输出路径
    merge_done = Signal(str)                    # 合并后的成片
    all_done = Signal()

    def __init__(self, items, level="medium", auto_cut=False, model_dir=None,
                 merge_after=False, use_bgm=True, burn_subs=False,
                 loudness_norm=True, output_dir=None, model_size="large-v3",
                 edit_mode="gameplay", keep_ratio=None):
        super().__init__()
        self.items = items
        self.level = level
        import cut_strength
        self.keep_ratio = cut_strength.normalize_ratio(
            keep_ratio, fallback=cut_strength.LEGACY_KEEP_RATIO.get(level))
        self.auto_cut = auto_cut
        self.model_dir = model_dir
        self.merge_after = merge_after
        self.use_bgm = bool(use_bgm)
        self.burn_subs = bool(burn_subs)
        self.loudness_norm = bool(loudness_norm)
        self.output_dir = output_dir
        self.model_size = str(model_size or "large-v3")
        self.edit_mode = str(edit_mode or "gameplay")
        self._stop = False
        self._skip = -1
        self.outputs = []        # 按队列顺序记录成片路径

    def stop(self):
        self._stop = True

    def skip_current(self):
        self._skip = self._idx

    def run(self):
        import os as _os, time as _t
        _dbg = bool(_os.environ.get("SC_DEBUG_QUEUE"))
        _t0 = _t.time()

        def _log(m):
            if _dbg:
                print("[qw %5.1fs] %s" % (_t.time() - _t0, m), flush=True)

        try:
            import no_console
            no_console.install()
        except Exception:
            pass
        import index as ix
        _log("import index 完成；items=%d" % len(self.items))
        for i, it in enumerate(list(self.items)):
            if self._stop:
                self.item_state.emit(i, "stop", 0, "已取消")
                continue
            self._idx = i
            self._skip = -1
            path = it["path"]
            # 开跑前先确认文件真的在（素材被移动/改名是最常见的"跑一半失败"）
            if not os.path.isfile(path):
                self.item_state.emit(
                    i, "fail", 0,
                    "源文件不在了（可能被移动/改名）：%s" % path)
                continue
            try:
                if os.path.getsize(path) < 10240:
                    self.item_state.emit(i, "fail", 0, "文件太小，可能不是视频")
                    continue
            except OSError:
                pass
            self.item_state.emit(i, "run", 0.02, "开始分析…")
            _log("进入条目 %d：%s" % (i, path))
            memory = None
            task_id = None
            try:
                stem = os.path.splitext(os.path.basename(path))[0]
                out_dir = os.path.join(ix.work_root(), "索引", stem)
                os.makedirs(out_dir, exist_ok=True)
                import cut_strength
                keep_ratio = cut_strength.normalize_ratio(
                    it.get("keep_ratio"), fallback=self.keep_ratio)
                lvl = cut_strength.level_for_ratio(keep_ratio)
                try:
                    import memory_config
                    import task_autosave
                    memory_cfg = memory_config.load_config()
                    if memory_cfg.get("enabled", True):
                        memory = task_autosave.open_autosave_store(config=memory_cfg)
                        task_id = str(it.get("task_id") or "") or memory.create_task(
                            {"sources": [os.path.abspath(path)], "queue_index": i},
                            mode=self.edit_mode, level=lvl, status="pending")
                        it["task_id"] = task_id
                        import workflow_engine
                        memory.record_stage_state(
                            task_id, "analyze", "started",
                            input_artifact_ids=[],
                            job=workflow_engine.STAGES["analyze"].job_descriptor(task_id))
                except Exception as exc:  # noqa: BLE001
                    memory = None
                    task_id = None
                    _log("任务记忆不可用：%s" % str(exc)[:80])

                def on_prog(frac, msg="", _i=i):
                    self.item_state.emit(_i, "run", float(frac or 0), str(msg or ""))

                def on_stat(msg, _i=i):
                    self.item_state.emit(_i, "run", -1, str(msg or ""))

                _log("调用 index_video：%s" % out_dir)
                ix.index_video(path, out_dir, model_size=self.model_size,
                               models_dir=self.model_dir,
                               on_status=on_stat, on_progress=on_prog,
                               should_stop=lambda: self._stop or self._skip == self._idx)
                _log("index_video 返回")
                if self._skip == i or self._stop:
                    if memory is not None and task_id:
                        memory.record_stage_state(
                            task_id, "analyze", "cancelled",
                            error="队列分析已跳过或取消")
                    self.item_state.emit(i, "stop", 0, "已跳过")
                    continue
                if memory is not None and task_id:
                    try:
                        import autocut as _autocut
                        index_data = _autocut.load_index(out_dir)
                        index_duration = float(index_data.get("duration") or 0)
                    except Exception:
                        index_duration = 0.0
                    memory.record_stage_state(
                        task_id, "analyze", "completed",
                        output_artifact_id="%s.INDEX-V1" % task_id,
                        payload={"index_dir": out_dir, "duration": index_duration},
                        metadata={"artifact_type": "index", "neutral": True})
                out = ""
                if self.auto_cut:
                    self.item_state.emit(i, "run", 0.9, "开始剪辑…")
                    import autopilot
                    _log("index_video 完成，准备剪辑")
                    want = (it.get("out_dir") or "").strip()
                    if not want:
                        want = (self.output_dir or "").strip()
                    if not want:
                        try:
                            import cards_ui
                            want = cards_ui.resolve_out_dir("out_autocut", path)
                        except Exception:
                            want = ""
                    res = autopilot.run_one(path, level=lvl, mode=self.edit_mode,
                                            keep_ratio=keep_ratio,
                                            use_bgm=self.use_bgm,
                                            index_dir=out_dir,
                                            on_log=lambda s: None,
                                            model_editor=True,
                                            tracks=it.get("tracks"),
                                            output_dir=want or None,
                                            burn_subs=self.burn_subs,
                                            loudness_norm=self.loudness_norm,
                                            should_cancel=lambda: self._stop or self._skip == self._idx,
                                            task_id=task_id, memory=memory,
                                            resume=bool(memory is not None and task_id)) or ""
                    if isinstance(res, dict):
                        out = str(res.get("out") or "")
                    else:
                        out = str(res or "")
                    # 兼容旧版 autopilot：若它没直接写到指定目录，再搬一次。
                    if out and want and os.path.isfile(out):
                        try:
                            os.makedirs(want, exist_ok=True)
                            dst = os.path.join(want, os.path.basename(out))
                            if os.path.normcase(dst) != os.path.normcase(out):
                                shutil.move(out, dst)
                                out = dst
                        except Exception:
                            pass
                    if out and os.path.isfile(out):
                        self.outputs.append(out)
                elif memory is not None and task_id:
                    memory.set_task_status(task_id, "completed", current_stage=None)
                self.item_done.emit(i, str(out or ""))
                self.item_state.emit(i, "done", 1.0, "已完成")
            except Exception as exc:  # noqa: BLE001
                import traceback as _tb
                msg = str(exc).splitlines()[0][:60]
                try:
                    import os as _os
                    base = _os.environ.get("SMARTCUT_HOME") or _os.path.dirname(
                        _os.path.dirname(_os.path.abspath(__file__)))
                    logs = _os.path.join(base, "logs")
                    _os.makedirs(logs, exist_ok=True)
                    with open(_os.path.join(logs, "queue_error.log"), "a",
                              encoding="utf-8") as _f:
                        _f.write("\n=== %s ===\n素材：%s\n%s\n"
                                 % (_os.path.basename(path), path,
                                    _tb.format_exc()))
                except Exception:
                    pass
                if memory is not None and task_id:
                    try:
                        stage_state = memory.get_stage_state(task_id, "analyze")
                        if not stage_state or stage_state.get("status") != "completed":
                            memory.record_stage_state(
                                task_id, "analyze", "failed", error=msg)
                    except Exception:
                        pass
                self.item_state.emit(i, "fail", 0, msg)
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
        # 全部处理完：按队列顺序拼成一条
        if self.merge_after and len(self.outputs) > 1 and not self._stop:
            try:
                self.item_state.emit(0, "run", -1, "正在按队列顺序合并…")
                merged = merge_outputs(self.outputs)
                if merged:
                    self.merge_done.emit(merged)
            except Exception as exc:  # noqa: BLE001
                self.item_state.emit(0, "fail", 0, "合并失败：%s" % str(exc)[:50])
        self.all_done.emit()


def merge_outputs(paths, on_log=None):
    """按给定顺序把多个成片拼成一条（顺序就是最终视频顺序）。

    先试无损 concat（同一编码参数时最快），失败再走重编码。
    """
    import subprocess
    import tempfile
    import export as ex

    ff = ex.find_ffmpeg()
    paths = [p for p in paths if p and os.path.isfile(p)]
    if len(paths) < 2:
        return paths[0] if paths else ""
    first = paths[0]
    base = os.path.dirname(first)
    out = os.path.join(base, os.path.splitext(os.path.basename(first))[0] + "_合集.mp4")

    tmpdir = tempfile.mkdtemp(prefix="merge_")
    lst = os.path.join(tmpdir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))
    try:
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", out], capture_output=True)
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 10000:
            return out
    except Exception:
        pass
    # 回退：重编码拼接
    try:
        args = [ff, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", lst,
                "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "192k", out]
        r = subprocess.run(args, capture_output=True)
        if r.returncode == 0 and os.path.isfile(out):
            return out
    except Exception:
        pass
    return ""


class QueueDialog(QDialog):
    """队列大窗口。

    给了 panel（主界面内嵌队列）时，**共享**它的 items 和 worker：
    这里改顺序/力度/音轨都作用在同一份数据上，工作状态也同步显示。
    """

    def __init__(self, parent=None, paths=None, panel=None):
        super().__init__(parent)
        self.setWindowTitle("分析队列 · " + PRODUCT_NAME)
        self.resize(1180, 760)
        self.setMinimumSize(980, 620)
        self.panel = panel
        self.items = panel.items if panel is not None else []
        self.worker = None
        try:
            import ui_theme
            ui_theme.apply()
        except Exception:
            pass
        self._build()
        if paths:
            self.add_paths(paths)
        if panel is not None:
            self.cb_autocut.setChecked(panel.cb_auto_workflow.isChecked())
            self.cb_merge.setChecked(bool(panel.merge_after))
            self.cb_autocut.toggled.connect(panel.cb_auto_workflow.setChecked)
            self.cb_merge.toggled.connect(
                lambda checked: setattr(panel, "merge_after", bool(checked)))
            self._sync_level_from_main()
            self._tick = QTimer(self)
            self._tick.setInterval(600)
            self._tick.timeout.connect(self._mirror_panel)
            self._tick.start()
            self._mirror_panel()

    # ------------------------------------------------------------ UI
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏
        head = QWidget()
        head.setFixedHeight(40)
        head.setStyleSheet("background:#181818;border-bottom:1px solid #3a3a3a;")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 0, 12, 0)
        t = QLabel("分析队列")
        t.setStyleSheet("font-size:14px;font-weight:600;")
        hl.addWidget(t)
        self.lbl_sum = QLabel("0 个素材")
        self.lbl_sum.setStyleSheet("color:#7d7d7d;font-size:11px;")
        hl.addWidget(self.lbl_sum)
        hl.addStretch(1)
        b_close = QPushButton("✕")
        b_close.setObjectName("ghost")
        b_close.setFixedHeight(26)
        b_close.clicked.connect(self.close)
        hl.addWidget(b_close)
        root.addWidget(head)

        # 工具栏
        bar = QWidget()
        bar.setFixedHeight(46)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 8, 16, 8)
        bl.setSpacing(8)
        self.btn_add = QPushButton("添加素材…")
        self.btn_start = QPushButton("全部开始")
        self.btn_start.setObjectName("primary")
        self.btn_pause = QPushButton("暂停")
        self.btn_skip = QPushButton("跳过当前")
        self.btn_clear = QPushButton("清空")
        for b in (self.btn_add, self.btn_start, self.btn_pause, self.btn_skip, self.btn_clear):
            b.setFixedHeight(28)
            bl.addWidget(b)
        bl.addStretch(1)
        self.cb_level = QSpinBox()
        self.cb_level.setRange(15, 100)
        self.cb_level.setPrefix("全部保留 ")
        self.cb_level.setSuffix(" %")
        self.cb_level.setValue(75)
        self.cb_level.setFixedHeight(28)
        bl.addWidget(self.cb_level)
        self.cb_merge = QCheckBox("合并成一个成片")
        self.cb_merge.setToolTip("按队列顺序把每条的成片拼成一条（顺序就是最终视频的播放顺序）")
        self.cb_merge.setChecked(False)
        self.cb_merge.setEnabled(False)
        bl.addWidget(self.cb_merge)
        self.cb_autocut = QCheckBox("全自动出片")
        self.cb_autocut.setToolTip(
            "关闭时只分析并生成卡片；开启后继续剧本、初剪、导演审片和渲染。")
        self.cb_autocut.setChecked(False)
        self.cb_autocut.toggled.connect(self._auto_mode_toggled)
        bl.addWidget(self.cb_autocut)
        root.addWidget(bar)

        # 主体
        body = QWidget()
        bl2 = QHBoxLayout(body)
        bl2.setContentsMargins(12, 4, 12, 8)
        bl2.setSpacing(8)

        left = Panel("队列", "拖拽行可调整顺序", "")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setStyleSheet(
            "QListWidget{background:#1a1a1a;border:1px solid #3a3a3a;border-radius:4px;}"
            "QListWidget::item{padding:0px;background:transparent;}"
            "QListWidget::item:selected{background:transparent;color:#dcdcdc;}"
            "QListWidget::item:hover{background:rgba(255,255,255,0.03);}")
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.model().rowsMoved.connect(self._on_moved)
        self.list.currentRowChanged.connect(lambda *_: self._sync_item_settings())
        left.add_widget(self.list, 1)
        drop_tip = QLabel("把视频拖进这个窗口，或点上面的「添加素材…」")
        drop_tip.setStyleSheet("color:#6e6e6e;font-size:11px;")
        left.add_widget(drop_tip)
        bl2.addWidget(left, 1)

        right = QWidget()
        right.setFixedWidth(360)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        p_cur = Panel("当前任务", "", "")
        self.lbl_cur = QLabel("空闲")
        self.lbl_cur.setStyleSheet("font-weight:600;")
        p_cur.add_widget(self.lbl_cur)
        self.lbl_cur_meta = QLabel("还没有开始")
        self.lbl_cur_meta.setStyleSheet("color:#7d7d7d;font-size:11px;")
        p_cur.add_widget(self.lbl_cur_meta)
        self.steps = {}
        for key, name in (("frames", "① 抽帧 + 音量"), ("asr", "② 语音识别"),
                          ("sem", "③ 语义分段"), ("cards", "④ 切片 + 标签")):
            lab = QLabel("○ " + name)
            lab.setStyleSheet("color:#7d7d7d;font-size:11px;")
            self.steps[key] = lab
            p_cur.add_widget(lab)
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setFixedHeight(8)
        self.prog.setTextVisible(False)
        p_cur.add_widget(self.prog)
        self.lbl_time = QLabel("")
        self.lbl_time.setStyleSheet("color:#7d7d7d;font-size:11px;")
        p_cur.add_widget(self.lbl_time)
        rl.addWidget(p_cur, 1)

        p_set = Panel("这个素材的设置", "只影响选中的这一条", "")
        self.cb_item_level = QSpinBox()
        self.cb_item_level.setRange(15, 100)
        self.cb_item_level.setPrefix("保留 ")
        self.cb_item_level.setSuffix(" %")
        import cut_strength
        _ratio_main = getattr(getattr(self, "panel", None),
                              "main_keep_ratio", None) or 0.75
        self.cb_item_level.setValue(cut_strength.percent(_ratio_main))
        self.cb_item_level.valueChanged.connect(
            lambda *_: self._apply_item_settings())
        p_set.add_widget(self.cb_item_level)
        row = QHBoxLayout()
        b_out = QPushButton("输出目录…")
        b_out.setFixedHeight(26)
        b_out.clicked.connect(self._pick_out)
        self.lbl_out = QLabel("默认")
        self.lbl_out.setStyleSheet("color:#7d7d7d;font-size:11px;")
        row.addWidget(b_out)
        row.addWidget(self.lbl_out, 1)
        p_set.add_layout(row)
        b_trk = QPushButton("音轨（只影响这一条）…")
        b_trk.setFixedHeight(26)
        b_trk.clicked.connect(self._pick_tracks)
        p_set.add_widget(b_trk)
        rl.addWidget(p_set, 1)

        p_done = Panel("已完成", "可直接打开", "")
        self.done_list = QListWidget()
        self.done_list.setFixedHeight(120)
        self.done_list.setStyleSheet(
            "QListWidget{background:#1a1a1a;border:1px solid #3a3a3a;border-radius:4px;}"
            "QListWidget::item{padding:4px 6px;}")
        p_done.add_widget(self.done_list)
        b_open = QPushButton("打开导出目录")
        b_open.setFixedHeight(26)
        b_open.clicked.connect(self._open_out)
        p_done.add_widget(b_open)
        rl.addWidget(p_done, 1)
        bl2.addWidget(right)
        root.addWidget(body, 1)

        # 底部
        foot = QWidget()
        foot.setFixedHeight(40)
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 4, 16, 8)
        tip = QLabel("队列顺序 = 批量处理顺序；单个素材的设置会覆盖上面的“全部素材用”")
        tip.setStyleSheet("color:#6e6e6e;font-size:11px;")
        fl.addWidget(tip)
        fl.addStretch(1)
        b_close2 = QPushButton("关闭")
        b_close2.setFixedHeight(28)
        b_close2.clicked.connect(self.close)
        fl.addWidget(b_close2)
        root.addWidget(foot)

        self.btn_add.clicked.connect(self.add_dialog)
        self.btn_start.clicked.connect(self._start_proxy)
        self.btn_pause.clicked.connect(self._pause_proxy)
        self.btn_skip.clicked.connect(self.skip_current)
        self.btn_clear.clicked.connect(self.clear)
        self.cb_level.valueChanged.connect(self._level_changed)
        self.setAcceptDrops(True)

    def _auto_mode_toggled(self, checked):
        """合并只属于全自动流程；关闭时清除残留勾选。"""
        running = bool(self.worker and self.worker.isRunning())
        self.cb_merge.setEnabled(bool(checked) and not running)
        if not checked:
            self.cb_merge.setChecked(False)

    # ------------------------------------------------------------ 队列数据
    def add_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择素材（可多选）", "", "视频 (*.mp4 *.mov *.mkv *.flv *.ts *.avi *.m4v *.webm)")
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        if self.panel is not None:
            self.panel.add_paths(paths)
            self._mirror_panel()
            return
        for p in paths:
            if not os.path.isfile(p):
                continue
            if any(os.path.normcase(x["path"]) == os.path.normcase(p) for x in self.items):
                continue
            self.items.append({"path": p, "status": "wait", "pct": 0.0,
                               "msg": "等待中", "out": "", "level": "",
                               "level_custom": False, "keep_ratio": None,
                               "keep_ratio_custom": False, "out_dir": ""})
        self._refresh_list()

    def _refresh_list(self):
        cur_row = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for i, it in enumerate(self.items):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, it["path"])     # 文件路径放数据里，别画成文字
            item.setText("")                          # 留空，避免和行控件叠字（重影）
            item.setSizeHint(QSize(0, 52))
            self.list.addItem(item)
            try:
                from ui_queuepanel import QueueRowWidget as _RowW
                row = _RowW(it, i, parent=self.list.viewport())
            except Exception:
                row = QueueRow(it, i, parent=self.list.viewport())
            self.list.setItemWidget(item, row)
            if it["status"] == "run":
                self.list.setCurrentRow(i)
        self.list.blockSignals(False)
        if cur_row >= 0 and cur_row < self.list.count():
            self.list.setCurrentRow(cur_row)
        n = len(self.items)
        done = sum(1 for x in self.items if x["status"] == "done")
        self.lbl_sum.setText("%d 个素材 · 已完成 %d 个" % (n, done))

    def _on_moved(self, *args):
        """拖完行后按列表当前顺序重建 self.items（顺序 = 处理顺序）。"""
        order = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            path = item.data(Qt.UserRole) if item else None
            it = self._find(path) if path else None
            if it is not None and it not in order:
                order.append(it)
        if len(order) == len(self.items):
            self.items = order
            if self.panel is not None:
                self.panel.items = list(order)
                self.panel._refresh()
        self._refresh_list()
        self._sync_item_settings()

    # ------------------------------------------------------------ 运行
    def _start_proxy(self):
        if self.panel is not None:
            self.panel.set_main_keep_ratio(self.cb_level.value())
            self.panel.cb_auto_workflow.setChecked(self.cb_autocut.isChecked())
            self.panel.merge_after = bool(self.cb_merge.isChecked())
            self.panel.start_all()
            self._mirror_panel()
        else:
            self.start_all()

    def _pause_proxy(self):
        if self.panel is not None:
            self.panel.pause()
            self._mirror_panel()
        else:
            self.pause()

    def _level_changed(self, *_):
        import cut_strength
        keep_ratio = cut_strength.normalize_ratio(self.cb_level.value())
        lv = cut_strength.level_for_ratio(keep_ratio)
        if self.panel is not None:
            self.panel.set_main_keep_ratio(keep_ratio)
        # 未开始的条目统一跟这个百分比
        for it in self.items:
            if (it.get("status") != "run"
                    and not it.get("keep_ratio_custom", False)):
                it["level"] = lv
                it["keep_ratio"] = keep_ratio

    def start_all(self):
        if self.worker and self.worker.isRunning():
            return
        pending = [x for x in self.items if x["status"] in ("wait", "fail", "stop")]
        if not pending:
            QMessageBox.information(self, "队列", "没有等待中的素材。")
            return
        model = os.path.join(os.environ.get("SMARTCUT_HOME") or
                             os.path.dirname(os.path.abspath(__file__)),
                             "models")
        try:
            import cards_ui
            _cfg = cards_ui.load_settings()
        except Exception:
            _cfg = {}
        try:
            import bgm as _bgm
            _bcfg = _bgm.load_conf()
        except Exception:
            _bcfg = {}
        self.worker = QueueWorker(
            pending,
            level=__import__("cut_strength").level_for_ratio(
                self.cb_level.value()),
            keep_ratio=self.cb_level.value(),
            auto_cut=self.cb_autocut.isChecked(),
            model_dir=model if os.path.isdir(model) else None,
            merge_after=self.cb_autocut.isChecked() and self.cb_merge.isChecked(),
            use_bgm=str(_cfg.get("bgm", "1")) in ("1", "true", "True"),
            burn_subs=str(_cfg.get("burn_subs", "0")) in ("1", "true", "True"),
            loudness_norm=str(_bcfg.get("loudness_norm", "1")) in ("1", "true", "True"),
            model_size=_cfg.get("model") or "large-v3",
            edit_mode=_cfg.get("edit_mode") or "gameplay",
        )
        self.worker.item_state.connect(self._on_item_state)
        self.worker.item_done.connect(self._on_item_done)
        self.worker.merge_done.connect(self._on_merged)
        self.worker.all_done.connect(self._standalone_run_finished)
        self.cb_autocut.setEnabled(False)
        self.cb_merge.setEnabled(False)
        self.worker.start()
        self._t0 = time.time()

    def _standalone_run_finished(self):
        self.cb_autocut.setEnabled(True)
        self.cb_merge.setEnabled(self.cb_autocut.isChecked())
        self._refresh_list()

    def pause(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.btn_pause.setText("已请求暂停…")

    def skip_current(self):
        if self.worker and self.worker.isRunning():
            self.worker.skip_current()

    def clear(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "队列", "先暂停再清空。")
            return
        self.items = []
        self._refresh_list()

    def closeEvent(self, ev):
        """独立队列窗口不能带着 QThread 直接销毁。嵌入主面板时只关视图。"""
        if self.panel is None and self.worker and self.worker.isRunning():
            self.worker.stop()
            self.btn_pause.setText("正在安全停止…")
            if not self.worker.wait(5000):
                ev.ignore()
                return
        super().closeEvent(ev)

    def _sync_level_from_main(self):
        """把主界面选的百分比显示到大窗口。"""
        ratio = getattr(self.panel, "main_keep_ratio", None)
        if ratio is None:
            return
        import cut_strength
        self.cb_level.setValue(cut_strength.percent(ratio))

    def _mirror_panel(self):
        """大窗口跟着主队列走：列表、状态、工作提示都同步。"""
        if self.panel is None:
            return
        self.items = self.panel.items
        self.worker = self.panel.worker
        if self.cb_autocut.isChecked() != self.panel.cb_auto_workflow.isChecked():
            self.cb_autocut.setChecked(self.panel.cb_auto_workflow.isChecked())
        if self.cb_merge.isChecked() != bool(self.panel.merge_after):
            self.cb_merge.setChecked(bool(self.panel.merge_after))
        running = bool(self.worker and self.worker.isRunning())
        self.cb_autocut.setEnabled(not running)
        self.cb_merge.setEnabled(not running and self.cb_autocut.isChecked())
        n = len(self.items)
        done = sum(1 for x in self.items if x["status"] == "done")
        self.lbl_sum.setText("%d 个素材 · 已完成 %d 个" % (n, done))
        self._refresh_list()
        self._follow_main_level()
        # 当前任务 / 进度
        run = next((x for x in self.items if x["status"] == "run"), None)
        if run:
            self.lbl_cur.setText(os.path.basename(run["path"]))
            self.lbl_cur_meta.setText(str(run.get("msg") or "")[:60])
            self.prog.setValue(int(float(run.get("pct") or 0) * 100))
        elif n and done == n:
            self.lbl_cur.setText("全部完成")
            self.lbl_cur_meta.setText("")

    def _find(self, path):
        for it in self.items:
            if os.path.normcase(it["path"]) == os.path.normcase(path):
                return it
        return None

    def _on_item_state(self, idx, status, pct, text):
        # idx 是 pending 列表里的下标，需要映射回 items
        if not (self.worker and 0 <= idx < len(self.worker.items)):
            return
        target = self.worker.items[idx]["path"]
        it = self._find(target)
        if not it:
            return
        it["status"] = status
        it["pct"] = pct if pct is not None else it["pct"]
        it["msg"] = text or it["msg"]
        if pct is not None and pct >= 0:
            self.prog.setValue(int(float(pct) * 100))
        if text:
            low = text
            for key, words in (("frames", ("抽帧", "音量")),
                               ("asr", ("识别", "语音", "转写")),
                               ("sem", ("语义", "分段")),
                               ("cards", ("卡片", "索引", "标签", "完成"))):
                if any(w in low for w in words):
                    for k, lab in self.steps.items():
                        base = lab.text()[2:]
                        if k == key:
                            lab.setText("▸ " + base)
                            lab.setStyleSheet("color:#4a90d9;font-size:11px;")
                        else:
                            lab.setText("○ " + base)
                            lab.setStyleSheet("color:#7d7d7d;font-size:11px;")
                    break
            self.lbl_time.setText(text[:60])
        self.lbl_cur.setText(os.path.basename(target))
        self._refresh_list()

    def _on_merged(self, path):
        try:
            self.done_list.addItem("★ 合集 → %s" % os.path.basename(path))
            self.lbl_cur.setText("合并完成")
            self.lbl_cur_meta.setText(os.path.basename(path))
        except Exception:
            pass

    def _on_item_done(self, idx, out):
        if not (self.worker and 0 <= idx < len(self.worker.items)):
            return
        target = self.worker.items[idx]["path"]
        it = self._find(target)
        if it:
            it["out"] = out
            it["status"] = "done"
        stem = os.path.splitext(os.path.basename(target))[0]
        result = os.path.basename(out) if out else "分析完成 · 卡片已生成"
        self.done_list.addItem("%s → %s" % (os.path.basename(target), result))
        self._refresh_list()

    # ------------------------------------------------------------ 单项设置
    def _follow_main_level(self):
        """主界面百分比变了 → 大窗口设置跟上。"""
        ratio = getattr(getattr(self, "panel", None), "main_keep_ratio", None)
        if ratio is None:
            return
        import cut_strength
        value = cut_strength.percent(ratio)
        if self.cb_level.value() != value:
            self.cb_level.blockSignals(True)
            self.cb_level.setValue(value)
            self.cb_level.blockSignals(False)
        self._sync_item_settings()

    def _sync_item_settings(self):
        """把选中那一行的力度/输出目录显示到右侧。"""
        try:
            row = self.list.currentRow()
            if 0 <= row < len(self.items):
                it = self.items[row]
                import cut_strength
                ratio = cut_strength.normalize_ratio(
                    it.get("keep_ratio"),
                    fallback=self.cb_level.value() / 100.0)
                self.cb_item_level.blockSignals(True)
                self.cb_item_level.setValue(cut_strength.percent(ratio))
                self.cb_item_level.blockSignals(False)
                self.lbl_out.setText(it.get("out_dir") or "默认")
        except Exception:
            pass

    def _apply_item_settings(self):
        """右侧改动写回选中那一行。"""
        try:
            row = self.list.currentRow()
            if 0 <= row < len(self.items):
                it = self.items[row]
                import cut_strength
                ratio = cut_strength.normalize_ratio(
                    self.cb_item_level.value())
                it["keep_ratio"] = ratio
                it["level"] = cut_strength.level_for_ratio(ratio)
                it["level_custom"] = True
                it["keep_ratio_custom"] = True
                if self.lbl_out.text() not in ("默认",):
                    it["out_dir"] = self.lbl_out.text()
        except Exception:
            pass

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "这个素材的输出目录")
        if d:
            self.lbl_out.setText(d)
            self._apply_item_settings()

    def _pick_tracks(self):
        row = self.list.currentRow()
        if not (0 <= row < len(self.items)):
            QMessageBox.information(self, "音轨", "先在列表里选一条素材")
            return
        it = self.items[row]
        try:
            import ui_dialogs
            dlg = ui_dialogs.AudioDialog(self, it["path"])
            if dlg.exec() == 1:      # QDialog.Accepted
                picks = [k for k, cb in getattr(dlg, "boxes", {}).items() if cb.isChecked()]
                if picks:
                    it["tracks"] = picks
                    QMessageBox.information(self, "音轨", "已为 %s 指定音轨 %s"
                                            % (os.path.basename(it["path"]),
                                               "、".join(str(x + 1) for x in picks)))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "音轨", str(exc))

    def _open_out(self):
        try:
            import index as ix
            d = os.path.join(ix.work_root(), "导出")
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    # ------------------------------------------------------------ 拖入
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls()
                 if u.toLocalFile().lower().endswith(VIDEO_EXTS)]
        if paths:
            self.add_paths(paths)


def main():
    import sys
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    ui_theme.apply(app)
    d = QueueDialog()
    d.show()
    return app.exec()


if __name__ == "__main__":
    import sys
    sys.exit(main())

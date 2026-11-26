# -*- coding: utf-8 -*-
"""
卡片界面 —— 挑片段、导出

左边是缩略图网格，每张卡片一段素材：
    双击不预览，单击选中；选好点导出，要么每段一个文件，要么拼成一条。
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
import time
import atexit

from PySide6.QtCore import Slot, QEvent, QMimeData, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QInputDialog, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QMenu, QMenuBar, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_QT_MEDIA = True
except ImportError:          # 没有 Qt 多媒体时退回系统播放器
    HAS_QT_MEDIA = False

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import export as exporter  # noqa: E402
import index as indexer  # noqa: E402
import tagger  # noqa: E402
from product_info import PRODUCT_ID, PRODUCT_NAME, VERSION, credits_html  # noqa: E402

CAT_COLORS = {
    "挖矿": ("#2b2116", "#e8b96a"),
    "合成": ("#1f2a1c", "#9ad07f"),
    "机器": ("#1b2633", "#7fc8ff"),
    "建造": ("#2a2233", "#c39bff"),
    "战斗": ("#331d1d", "#ff9c9c"),
    "探索": ("#1c2f2c", "#7fe0cf"),
    "整理": ("#262626", "#b9c3cf"),
    "闲聊": ("#2b2b1c", "#e0d68a"),
}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".flv", ".ts", ".avi", ".m4v", ".webm"}
CARD_W, CARD_H = 268, 234
THUMB_W, THUMB_H = 256, 144


def fmt(t: float) -> str:
    t = max(0.0, t)
    m, s = int(t // 60), t % 60
    return f"{m:02d}:{s:04.1f}"


def index_dir_for(src: str) -> str:
    """索引放固定工作目录，不再写源文件旁边（桌面/只读目录会拒绝访问）。"""
    stem = os.path.splitext(os.path.basename(src))[0] or "素材"
    return os.path.join(indexer.work_root(), "索引", stem)


_FAULT_LOG = None


def _close_faulthandler():
    global _FAULT_LOG
    try:
        import faulthandler
        if faulthandler.is_enabled():
            faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    if _FAULT_LOG is not None:
        try:
            _FAULT_LOG.close()
        except Exception:  # noqa: BLE001
            pass
        _FAULT_LOG = None


def _install_faulthandler():
    """装一个致命错误钩子：C 层崩（0xc0000005）时把 Python 栈写进文件。

    崩溃日志里只有 'python314.dll 0xc0000005' 这种信息，看不出是哪一行。
    faulthandler 能在进程死前把当前调用栈写出来，这是唯一能定位的手段。
    """
    try:
        import faulthandler
        logs = os.path.join(indexer.BASE_DIR, "logs")
        os.makedirs(logs, exist_ok=True)
        global _FAULT_LOG
        _FAULT_LOG = open(os.path.join(logs, "fatal.log"), "a", buffering=1,
                          encoding="utf-8")
        faulthandler.enable(file=_FAULT_LOG, all_threads=True)
        atexit.register(_close_faulthandler)
    except Exception:  # noqa: BLE001
        pass


_install_faulthandler()


def _install_crash_log():
    """把未捕获异常写进 logs\\crash_日期.log，pythonw 下也能事后查。

    pythonw 没有控制台，崩了什么都留不下，只能靠这个。
    """
    import traceback
    logs = os.path.join(indexer.BASE_DIR, "logs")
    try:
        os.makedirs(logs, exist_ok=True)
    except OSError:
        return

    def hook(exc_type, exc, tb):
        try:
            p = os.path.join(logs, "crash_%s.log" % time.strftime("%Y%m%d"))
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                traceback.print_exception(exc_type, exc, tb, file=fh)
        except Exception:  # noqa: BLE001
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


_install_crash_log()


def export_dir_for(src: str) -> str:
    stem = os.path.splitext(os.path.basename(src))[0] or "素材"
    return os.path.join(indexer.work_root(), "导出", stem)


# --------------------------------------------------------------------------
# 设置：各类导出分别指定输出文件夹
# --------------------------------------------------------------------------
SETTINGS_PATH = os.path.join(indexer.BASE_DIR, "conf", "settings.json")

# 留空 = 用默认目录（工作目录\导出\<素材名>）
DEFAULT_SETTINGS = {
    "out_single": "",
    "out_merge": "",
    "out_autocut": "",
    # 智能剪辑把素材压到原来时长的多少。0.3 = 只留 30%。
    # 实测一份人工剪辑成片留了 27%，所以默认 0.30。
    "autocut_ratio": "0.30",
    # 输出保留哪些音轨（音轨序号，从 1 起，逗号分隔）。空 = 自动去掉静音轨。
    "autocut_tracks": "",
    # 训练产物（转写缓存/中间文件）放哪。留空 = 工具目录\cache_learn
    "train_dir": "",
    # 单个卡片（片段）最长多少秒，± 是允许的浮动；重新分析后生效
    "card_max": "45",
    "card_tol": "6",
    # 音轨输出方式：mix = 选中的混成一条（默认，播放器能听到全部）
    #               tracks = 保持多条独立音轨（后期要用可分轨）
    "mix_mode": "mix",
    # 是否生成卡片缩略图。一句一张卡时有两千多张，机械盘上要 9 分钟；
    # 挑片主要看台词，需要看图时双击卡片会立刻抓那一帧。
    "thumbs": "0",
    # 用户只设置精确内容保留比例；cut_level 是自动派生的旧链路兼容字段。
    "cut_level": "medium",
    "keep_ratio": 0.75,
    # 剪辑专项：gameplay=游戏实况；general=通用兼容
    "edit_mode": "gameplay",
    # 1 = 跳过导演读片，直接用索引里已有的剧本/导演结果
    "skip_director": "0",
    # 1 = 出片时把字幕烧进画面；0 = 只生成 _字幕.srt/.ass 放在成片旁边
    "burn_subs": "0",
    # 1 = 自动查重复录制（同一段说了两遍只留更完整的一版）
    "dedupe": "1",
    # 1 = 自动配 BGM（曲库在 conf/bgm.json 里配）
    "bgm": "1",
    # 智能剪辑语言/决策模型。默认保持完全本地；API Key 只持久化，
    # 不进入状态文案、普通日志或错误摘要。
    "llm_provider": "local",
    "llm_api_base_url": "",
    "llm_api_key": "",
    "llm_api_model": "",
    "llm_api_timeout": 120,
}

OUT_KINDS = [
    ("out_single", "每段导出", "导出选中里的每一段，各存一个文件"),
    ("out_merge", "合并成一条", "把选中的片段按顺序拼成一条成片"),
    ("out_autocut", "智能剪辑", "自动剪空段后出的成片"),
]


def load_settings() -> dict:
    cfg = dict(DEFAULT_SETTINGS)
    try:
        if os.path.isfile(SETTINGS_PATH):
            with open(SETTINGS_PATH, encoding="utf-8") as handle:
                user = json.load(handle)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items()
                            if isinstance(v, str)
                            or (k in ("keep_ratio", "llm_api_timeout")
                                and isinstance(v, (int, float)))})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_settings(cfg: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def resolve_out_dir(kind: str, src: str) -> str:
    """算出这次导出该用哪个目录。

    支持占位符，方便一次设好全系列通用：
        {素材名}  → 素材文件名（不含扩展名）
        {源目录}  → 素材所在目录
    例：D:\\成片\\{素材名}  或  {源目录}\\导出
    """
    stem = os.path.splitext(os.path.basename(src))[0] or "素材"
    srcdir = os.path.dirname(os.path.abspath(src)) if src else ""
    raw = (load_settings().get(kind) or "").strip()
    if not raw:
        return export_dir_for(src)
    for a, b in (("{素材名}", stem), ("{name}", stem),
                 ("{源目录}", srcdir), ("{src_dir}", srcdir)):
        raw = raw.replace(a, b)
    return os.path.expandvars(raw)


class BurnWorker(QThread):
    """烧录字幕（要重新编码，慢）。"""
    status = Signal(str)
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, data: dict, style: dict, out_path: str):
        super().__init__()
        self.data, self.style, self.out_path = data, style, out_path

    def run(self):
        try:
            import subtitles as sd
            idx_dir = self.data.get("index_dir") or ""
            ass = os.path.join(idx_dir, "_字幕.ass") if idx_dir else self.out_path + ".ass"
            sd.write_ass(self.data, ass, self.style)
            self.status.emit("字幕文件已生成，开始烧录（要几分钟）…")
            sd.burn(self.data.get("source"), ass, self.out_path,
                    on_log=self.status.emit)
            self.ok.emit(self.out_path)
        except Exception as exc:  # noqa: BLE001
            self.err.emit("%s: %s" % (type(exc).__name__, exc))


class TrainWorker(QThread):
    """跑 learn.py，把输出实时贴到界面。"""
    status = Signal(str)
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, pairs: list, train_dir: str = ""):
        super().__init__()
        self.pairs = pairs
        self.train_dir = train_dir

    def run(self):
        try:
            import subprocess as _sp
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learn.py")
            cmd = ([sys.executable, "--run-script", script]
                   if getattr(sys, "frozen", False)
                   else [sys.executable, "-X", "utf8", script])
            try:
                import winproc as _wp
                cmd = _wp.script_cmd(script)   # 打包版会换成 exe --run-script，不开新窗口
            except Exception:
                # 打包版的 sys.executable 是 GUI 主程序，绝不能裸接 .py，
                # 否则后台任务会再次创建一个同名空壳窗口。
                pass
            if self.train_dir:
                cmd += ["--dir", self.train_dir]
            for raw, fin in self.pairs:
                cmd += ["--pair", raw, fin]
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.status.emit(line)
            if proc.wait() == 0:
                self.ok.emit("训练完成")
            else:
                self.err.emit("训练进程退出码 %d" % proc.returncode)
        except Exception as exc:  # noqa: BLE001
            self.err.emit("%s: %s" % (type(exc).__name__, exc))


class _SafeCloseDialog(QDialog):
    """关窗口前先停掉自己的后台线程，避免线程还在跑、窗口关不掉。"""

    def _own_worker(self):
        return getattr(self, "worker", None)

    def _halt(self):
        w = self._own_worker()
        if w is not None and hasattr(w, "isRunning") and w.isRunning():
            try:
                if hasattr(w, "terminate"):
                    w.terminate()
                w.wait(1500)
            except Exception:  # noqa: BLE001
                pass

    def _check_local_llm(self):
        """自检本地模型服务；缺了就在状态栏说明后果（返回缺失原因，空串=正常）。"""
        import llm_config
        from memory_config import app_home
        if llm_config.api_settings().get("enabled"):
            return ""
        base = app_home()
        exe = os.path.join(base, "bin", "llama", "llama-server.exe")
        llm_dir = os.path.join(base, "models", "llm")
        models = []
        try:
            models = [f for f in os.listdir(llm_dir) if f.lower().endswith(".gguf")]
        except OSError:
            models = []
        reason = ""
        if not os.path.isfile(exe):
            reason = "缺 bin\\llama\\llama-server.exe（%s）" % exe
        elif not models:
            reason = "缺 models\\llm\\*.gguf"
        if reason:
            self.status.setText(
                "⚠ 本地模型不可用：%s → 语义分析/剧本会跳过，剪辑只用兜底打分"
                "（用安装器补装，或把别处的 bin\\llama 和 models\\llm 拷过来）" % reason)
            try:
                import time as _t
                d = os.path.join(base, "logs")
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "ui_errors.log"), "a", encoding="utf-8") as fh:
                    fh.write("\n[%s] 本地模型自检失败：%s\n"
                             % (_t.strftime("%Y-%m-%d %H:%M:%S"), reason))
            except OSError:
                pass
        return reason
    def closeEvent(self, event):
        self._halt()
        super().closeEvent(event)

    def reject(self):
        self._halt()
        super().reject()

    def accept(self):
        self._halt()
        super().accept()


SUBTITLE_PRESETS = {
    "经典白字黑边": {"color": "#FFFFFF", "outline": 3, "outline_color": "#101010",
                "shadow": 2, "shadow_color": "#000000", "align": 2,
                "margin_v": 46, "max_chars": 18, "max_lines": 2, "size": 54},
    "醒目黄字": {"color": "#FFD84A", "outline": 4, "outline_color": "#20180B",
              "shadow": 2, "shadow_color": "#000000", "align": 2,
              "margin_v": 56, "max_chars": 14, "max_lines": 2, "size": 62},
    "大字压屏": {"color": "#FFFFFF", "outline": 5, "outline_color": "#000000",
              "shadow": 0, "shadow_color": "#000000", "align": 2,
              "margin_v": 90, "max_chars": 11, "max_lines": 2, "size": 78},
    "小字低调": {"color": "#E6EDF5", "outline": 2, "outline_color": "#0A0A0A",
              "shadow": 1, "shadow_color": "#000000", "align": 2,
              "margin_v": 30, "max_chars": 22, "max_lines": 2, "size": 42},
    "居中无描边": {"color": "#FFFFFF", "outline": 0, "outline_color": "#000000",
               "shadow": 3, "shadow_color": "#000000", "align": 5,
               "margin_v": 0, "max_chars": 16, "max_lines": 2, "size": 56},
}


class SubtitlePreview(QWidget):
    """1080p 模拟画布：按真实像素排字幕，所见即实际渲染位置。

    以前是按控件大小随手缩放，看着差不多但对不上真实位置
    （比如底部距离 46px，在预览里可能是 30 也可能 60）。
    现在固定 1920×1080 逻辑坐标，等比缩放到控件里，位置误差为 0。
    """

    CANVAS_W, CANVAS_H = 1920, 1080

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(280)
        self.setStyleSheet("background:#0a0d11;border:1px solid #26313d;"
                           "border-radius:6px;")
        self.style_conf = {}
        self.sample = "这是一句示例台词，用来看看字幕有多大、在哪、清不清楚。"

    def update_style(self, st, sample=None):
        self.style_conf = dict(st or {})
        if sample:
            self.sample = sample
        self.update()

    def _canvas_rect(self):
        """在控件里取一个居中的 16:9 区域（留边，保证比例正确）。"""
        w, h = self.width() - 24, self.height() - 40
        if w <= 0 or h <= 0:
            return 0, 0, 0, 0
        if w / 16.0 < h / 9.0:
            cw = w
            ch = w * 9 // 16
        else:
            ch = h
            cw = h * 16 // 9
        cx = (self.width() - cw) // 2
        cy = (self.height() - ch) // 2 + 8
        return cx, cy, cw, ch

    def paintEvent(self, _ev):
        from PySide6.QtGui import (QPainter, QColor, QFont, QPainterPath,
                                   QPen, QLinearGradient)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy, cw, ch = self._canvas_rect()
        if cw <= 0:
            p.end()
            return
        k = cw / float(self.CANVAS_W)      # 逻辑像素 → 屏幕像素

        # 假画面：上深下浅，白字和描边都看得清
        bg = QLinearGradient(cx, cy, cx, cy + ch)
        bg.setColorAt(0.0, QColor("#1b2430"))
        bg.setColorAt(0.55, QColor("#25303a"))
        bg.setColorAt(1.0, QColor("#33402e"))
        p.fillRect(cx, cy, cw, ch, bg)

        # 安全区参考线（中间 90%）
        p.setPen(QPen(QColor(66, 120, 170, 110),
                      1, Qt.DashLine))
        m = int(self.CANVAS_W * 0.05 * k)
        p.drawRect(cx + m, cy + m, cw - 2 * m, ch - 2 * m)

        st = self.style_conf or {}
        size = max(8, int(float(st.get("size", 54)) * k))
        f = QFont(st.get("font") or "", size)
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()

        mc = max(4, int(st.get("max_chars", 18)))
        max_lines = max(1, int(st.get("max_lines", 2)))
        txt = self.sample[: mc * max_lines]
        lines = [txt[i:i + mc] for i in range(0, len(txt), mc)][:max_lines]

        lh = fm.height() + max(2, int(6 * k))
        block_h = lh * len(lines)
        align = int(st.get("align", 2))
        mv = int(float(st.get("margin_v", 46)) * k)

        if align == 5:                       # 居中
            top = cy + (ch - block_h) // 2
        elif align == 8:                     # 顶部
            top = cy + mv
        else:                                # 底部（ASS alignment 2）
            top = cy + ch - mv - block_h

        fill = QColor(st.get("color", "#FFFFFF"))
        ocolor = QColor(st.get("outline_color", "#101010"))
        owidth = max(0, int(float(st.get("outline", 3)) * k))

        for i, ln in enumerate(lines):
            tw = fm.horizontalAdvance(ln)
            x = cx + (cw - tw) // 2
            y = top + fm.ascent() + i * lh
            path = QPainterPath()
            path.addText(x, y, f, ln)
            if owidth > 0:
                pen = QPen(ocolor)
                pen.setWidth(owidth * 2)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                p.setBrush(ocolor)
                p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawPath(path)

        # 标注：告诉玩家这是 1080p、距边多少像素
        p.setPen(QColor("#7f8b99"))
        p.setFont(QFont("", max(9, int(12 * self.height() / 400.0))))
        pos_name = {5: "垂直居中", 8: "顶部"}.get(align, "底部")
        tip = "1920×1080 模拟 · %s · 距边 %d px · 字号 %d" % (
            pos_name, int(float(st.get("margin_v", 46))), int(float(st.get("size", 54))))
        p.drawText(cx, cy - 6 if cy > 18 else cy + ch + 16, tip)
        p.end()


class SubtitleDialog(_SafeCloseDialog):
    """字幕：导出 SRT，或按固定样式烧进画面。"""

    def __init__(self, parent, data):
        super().__init__(parent)
        self.data = data
        self.setWindowTitle("字幕")
        self.setMinimumWidth(680)
        import subtitles as sd
        st = sd.load_style()

        root = QVBoxLayout(self)
        tip = QLabel("导出 SRT 给平台用；烧录字幕会把字直接压进画面（要重新编码）。")
        tip.setObjectName("muted")
        root.addWidget(tip)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.addWidget(QLabel("样式"), 0, 0)
        self.preset = QComboBox()
        self._reload_styles()
        self.preset.currentIndexChanged.connect(self._apply_preset)
        grid.addWidget(self.preset, 0, 1, 1, 2)
        _btns = QHBoxLayout()
        _b_save = QPushButton("保存为…")
        _b_save.setFixedHeight(24)
        _b_save.clicked.connect(self._save_style)
        _b_del = QPushButton("删除")
        _b_del.setFixedHeight(24)
        _b_del.clicked.connect(self._delete_style)
        _btns.addWidget(_b_save)
        _btns.addWidget(_b_del)
        grid.addLayout(_btns, 0, 3)

        grid.addWidget(QLabel("字体"), 1, 0)
        self.font = QComboBox()
        cur = st.get("font", "")
        for _f in sd.list_fonts(detail=True):
            # 下拉显示中文名，值是 ASS 要用的家族名；
            # 没有中文字形的明确标出来，免得选了以后满屏方块
            _lab = _f["label"] if _f["cjk"] else _f["label"] + "（不含中文字形）"
            self.font.addItem(_lab, _f["family"])
        _i = -1
        if cur:
            _i = self.font.findData(cur)
            if _i < 0:
                _i = self.font.findText(cur)
        if _i < 0:
            # 这台电脑没装原来的字体（换机器、拷样式文件都会遇到）：
            # 用本机现挑的默认字体，而不是落到列表第一个
            _d = sd.default_font()
            _i = self.font.findData(_d) if _d else -1
            if cur:
                self._font_note = "这台电脑没装字体「%s」，先用「%s」代替" % (
                    cur, _d or "默认")
        if _i >= 0:
            self.font.setCurrentIndex(_i)
        grid.addWidget(self.font, 1, 1, 1, 3)

        grid.addWidget(QLabel("字号"), 2, 0)
        self.size = QLineEdit(str(st.get("size", 54)))
        self.size.setFixedWidth(80)
        grid.addWidget(self.size, 2, 1)
        grid.addWidget(QLabel("每行字数"), 2, 2)
        self.mc = QLineEdit(str(st.get("max_chars", 18)))
        self.mc.setFixedWidth(80)
        grid.addWidget(self.mc, 2, 3)

        grid.addWidget(QLabel("字色"), 3, 0)
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(80, 24)
        self.color_btn.clicked.connect(lambda: self._pick_color("color"))
        grid.addWidget(self.color_btn, 3, 1)
        grid.addWidget(QLabel("描边"), 3, 2)
        self.outline = QLineEdit(str(st.get("outline", 3)))
        self.outline.setFixedWidth(80)
        grid.addWidget(self.outline, 3, 3)

        grid.addWidget(QLabel("位置"), 4, 0)
        self.pos = QComboBox()
        self.pos.addItem("底部", 2)
        self.pos.addItem("居中", 5)
        self.pos.addItem("顶部", 8)
        _idx = self.pos.findData(int(st.get("align", 2)))
        if _idx >= 0:
            self.pos.setCurrentIndex(_idx)
        self.pos.currentIndexChanged.connect(self._refresh_preview)
        grid.addWidget(self.pos, 4, 1)
        grid.addWidget(QLabel("最多行数"), 4, 2)
        self.maxlines = QLineEdit(str(st.get("max_lines", 2)))
        self.maxlines.setFixedWidth(80)
        grid.addWidget(self.maxlines, 4, 3)
        root.addLayout(grid)

        self.preview = SubtitlePreview()
        root.addWidget(self.preview, 1)

        for _w in (self.size, self.mc, self.outline, self.maxlines):
            _w.textChanged.connect(self._refresh_preview)
        self.font.currentIndexChanged.connect(self._refresh_preview)

        self._refresh_preview()

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(110)
        if getattr(self, "_font_note", ""):
            self.log.appendPlainText(self._font_note)
        root.addWidget(self.log)

        self._has_data = bool((self.data or {}).get("segments"))
        btns = QHBoxLayout()
        b_srt = QPushButton("导出 SRT…")
        b_srt.clicked.connect(self.export_srt)
        b_ass = QPushButton("导出 ASS…")
        b_ass.clicked.connect(self.export_ass)
        b_burn = QPushButton("烧录字幕…")
        b_burn.setObjectName("primary")
        b_burn.clicked.connect(self.burn)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_srt)
        btns.addWidget(b_ass)
        btns.addWidget(b_burn)
        if not self._has_data:
            for _b in (b_srt, b_ass, b_burn):
                _b.setEnabled(False)
            self.log.appendPlainText(
                "还没打开素材：现在可以调样式并「保存为…」，导出要等打开素材后才行。")
        btns.addStretch(1)
        btns.addWidget(b_close)
        root.addLayout(btns)

    def _sample_text(self):
        """预览用真实台词，比示例句更有参考价值。"""
        try:
            segs = (self.data or {}).get("segments") or []
            for it in segs[3:8]:
                t = str(it[2]).strip()
                if len(t) >= 8:
                    return t
        except Exception:  # noqa: BLE001
            pass
        return None

    def _reload_styles(self, keep=None):
        import subtitles as sd
        cur = keep or (self.preset.currentData() if self.preset.count() else None)
        self.preset.blockSignals(True)
        self.preset.clear()
        self.preset.addItem("（自定义）", None)
        for _n in sd.load_styles():
            self.preset.addItem(_n, _n)
        if cur:
            i = self.preset.findData(cur)
            if i >= 0:
                self.preset.setCurrentIndex(i)
        self.preset.blockSignals(False)

    def _save_style(self):
        import subtitles as sd
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "保存样式", "给这个样式起个名字：")
        if not ok or not name.strip():
            return
        st = self._style()
        if sd.save_named(name, st):
            self._reload_styles(keep=name.strip())
            self.log.appendPlainText("样式已保存：%s" % name.strip())
        else:
            self.log.appendPlainText("保存失败")

    def _delete_style(self):
        import subtitles as sd
        name = self.preset.currentData()
        if not name:
            return
        if sd.delete_named(name):
            self._reload_styles(keep=None)
            self.log.appendPlainText("已删除样式：%s" % name)
        else:
            self.log.appendPlainText("内置样式不能删")

    def _apply_preset(self, _i=0):
        name = self.preset.currentData()
        if not name:
            return
        import subtitles as sd
        p = sd.load_styles().get(name)
        if not p:
            return
        self.size.setText(str(p["size"]))
        self.mc.setText(str(p["max_chars"]))
        self.outline.setText(str(p["outline"]))
        self.maxlines.setText(str(p["max_lines"]))
        i = self.pos.findData(p["align"])
        if i >= 0:
            self.pos.setCurrentIndex(i)
        self._color = p["color"]
        self._paint_color_btn()
        self._refresh_preview()

    def _pick_color(self, key):
        from PySide6.QtWidgets import QColorDialog
        cur = getattr(self, "_color", self._style().get("color", "#FFFFFF"))
        c = QColorDialog.getColor(QColor(cur), self, "选字幕颜色")
        if c.isValid():
            self._color = c.name().upper()
            self._paint_color_btn()
            self._refresh_preview()

    def _paint_color_btn(self):
        c = getattr(self, "_color", "#FFFFFF")
        self.color_btn.setText(c)
        self.color_btn.setStyleSheet(
            "background:%s;color:%s;border:1px solid #3a4655;border-radius:4px;"
            % (c, "#101010" if c.upper() > "#888888" else "#FFFFFF"))

    def _refresh_preview(self, *_a):
        st = self._style()
        self.preview.update_style(st, self._sample_text())

    def _style(self):
        import subtitles as sd
        st = sd.load_style()
        st["font"] = self.font.currentData() or self.font.currentText()
        try:
            st["size"] = int(self.size.text())
        except ValueError:
            pass
        try:
            st["max_chars"] = int(self.mc.text())
        except ValueError:
            pass
        try:
            st["outline"] = int(self.outline.text())
        except ValueError:
            pass
        try:
            st["max_lines"] = int(self.maxlines.text())
        except ValueError:
            pass
        st["align"] = int(self.pos.currentData() or 2)
        if getattr(self, "_color", None):
            st["color"] = self._color
        sd.save_style(st)
        return st

    def export_ass(self):
        """单独导出 ASS：带完整样式，PR/剪映/originals 都能直接导入。"""
        import subtitles as sd
        segs = self._segments()
        if not segs:
            self.log.appendPlainText("没有逐句时间轴，先重新分析素材。")
            return
        default = os.path.join(export_dir_for(self.data.get("source", "")),
                               (self.data.get("name") or "字幕") + ".ass")
        path, _f = QFileDialog.getSaveFileName(self, "导出 ASS", default,
                                               "ASS 字幕 (*.ass)")
        if not path:
            return
        try:
            _p, n = sd.write_ass(self.data, path, self._style())
            self.log.appendPlainText("已导出 ASS：%s（%d 句）" % (_p, n))
        except Exception as exc:  # noqa: BLE001
            self.log.appendPlainText("导出失败：%s" % exc)

    def export_srt(self):
        import subtitles as sd
        stem = os.path.splitext(os.path.basename(self.data.get("source") or "字幕"))[0]
        p, _n = QFileDialog.getSaveFileName(self, "保存字幕", stem + ".srt",
                                            "字幕 (*.srt)")
        if not p:
            return
        try:
            path, n = sd.write_srt(self.data, p, self._style())
            self.log.appendPlainText("已导出 %s（%d 条）" % (path, n))
        except Exception as exc:  # noqa: BLE001
            self.log.appendPlainText("失败：%s" % exc)

    def burn(self):
        stem = os.path.splitext(os.path.basename(self.data.get("source") or "视频"))[0]
        p, _n = QFileDialog.getSaveFileName(self, "保存带字幕的视频",
                                            stem + "_字幕.mp4", "视频 (*.mp4)")
        if not p:
            return
        st = self._style()
        self.worker = BurnWorker(self.data, st, p)
        self.worker.status.connect(self.log.appendPlainText)
        self.worker.ok.connect(lambda x: self.log.appendPlainText("完成：%s" % x))
        self.worker.err.connect(lambda e: self.log.appendPlainText("失败：%s" % e))
        self.worker.start()
        self.log.appendPlainText("开始烧录…")


class TrackDialog(_SafeCloseDialog):
    """剪辑前选择保留哪些音轨。"""

    def __init__(self, parent, src):
        super().__init__(parent)
        self.src = src
        self.setWindowTitle("音轨")
        self.setMinimumWidth(620)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("勾选要保留的音轨。剪辑和导出只输出勾中的轨道。"))

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(80)
        root.addWidget(self.log)

        self.list = QListWidget()
        self.list.setFixedHeight(180)
        root.addWidget(self.list)

        btns = QHBoxLayout()
        b_play = QPushButton("试听选中")
        b_play.setToolTip("播 12 秒，听一下这条轨到底是什么声音")

        b_auto = QPushButton("全选")
        b_play.clicked.connect(self.preview_track)
        b_auto.clicked.connect(self.auto_pick)
        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self.save)
        b_close = QPushButton("取消")
        b_close.clicked.connect(self.reject)
        btns.addWidget(b_play)
        btns.addWidget(b_auto)
        btns.addStretch(1)
        btns.addWidget(b_close)
        btns.addWidget(b_save)
        root.addLayout(btns)

        self.src = src
        self.info = {}
        self.loading = True
        self.worker = TrackProbeWorker(src)
        self.worker.done.connect(self.fill)
        self.worker.err.connect(lambda e: self.log.appendPlainText("探测失败：%s" % e))
        self.worker.start()

    def fill(self, levels):
        self.loading = False
        cfg = load_settings().get("autocut_tracks", "")
        chosen = set()
        for part in (cfg or "").split(","):
            part = part.strip()
            if part.isdigit():
                chosen.add(int(part) - 1)
        if not chosen:
            # 默认全勾：不去猜哪条是静音（猜错过），让玩家自己取消不要的
            chosen = set(x["ordinal"] for x in levels)
        self.list.clear()
        for x in levels:
            txt = "音轨 %d   %s   电平 %.1f dB" % (
                x["ordinal"] + 1, x["label"], x["db"])
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, x["ordinal"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if x["ordinal"] in chosen else Qt.Unchecked)
            self.list.addItem(it)
        self.log.appendPlainText(
            "共 %d 条音轨。请自己勾选要用的（不勾就用不了），"
            "多条会混音成一条输出。" % len(levels))

    def preview_track(self):
        """播选中音轨的 12 秒，让玩家用耳朵确认是哪一条。

        光看电平看不出“这是游戏声还是我说话”，试听最直接。
        """
        it = self.list.currentItem()
        if it is None:
            self.log.appendPlainText("先在列表里选中一条音轨")
            return
        idx = int(it.data(Qt.UserRole))
        src = getattr(self, "src", "") or ""
        if not src or not os.path.isfile(src):
            self.log.appendPlainText("源文件不在了，没法试听")
            return
        import index as indexer
        import tempfile
        import subprocess
        ff = indexer.find_ffmpeg()
        out = os.path.join(indexer.temp_root(),
                           "试听_%d.wav" % (idx + 1))
        # 从 1/4 处开始取，避开开头的空白
        start = 0
        try:
            start = max(0.0, float(self.info.get("duration", 0)) * 0.25)
        except Exception:  # noqa: BLE001
            pass
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", "%.1f" % start, "-t", "12", "-i", src,
               "-map", "0:a:%d" % idx, "-ac", "2", "-ar", "44100", out]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=0x08000000)
            if os.path.isfile(out) and os.path.getsize(out) > 1000:
                os.startfile(out)          # 用系统播放器放
                self.log.appendPlainText(
                    "正在试听 音轨 %d（从 %.0f 秒开始 12 秒）" % (idx + 1, start))
            else:
                self.log.appendPlainText("这条轨抽不出声音（可能是空轨）")
        except Exception as exc:  # noqa: BLE001
            self.log.appendPlainText("试听失败：%s" % str(exc)[:80])

    def auto_pick(self):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Checked)

    def save(self):
        picked = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                picked.append(str(it.data(Qt.UserRole) + 1))
        self.picked = [int(x) - 1 for x in picked]     # 0 基，方便直接用
        cfg = load_settings()
        cfg["autocut_tracks"] = ",".join(picked)
        save_settings(cfg)
        self.log.appendPlainText("已保存：保留 %d 条音轨" % len(picked))
        self.accept()


class TrackProbeWorker(QThread):
    done = Signal(list)
    err = Signal(str)

    def __init__(self, src):
        super().__init__()
        self.src = src

    def run(self):
        try:
            import autocut
            self.done.emit(autocut.probe_track_levels(self.src))
        except Exception as exc:  # noqa: BLE001
            self.err.emit("%s: %s" % (type(exc).__name__, exc))


class DropList(QListWidget):
    """能拖文件进来的列表。拖文件夹会把它里面的视频都加进来。"""

    VEXT = (".mp4", ".mkv", ".mov", ".flv", ".ts")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(False)   # 深色主题下隔行变色会冒白带
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setWordWrap(False)

    def paths(self):
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def add_paths(self, paths):
        have = set(self.paths())
        _ = have
        added = 0
        for p in paths:
            files = []
            if os.path.isdir(p):
                for dp, _d, fs in os.walk(p):
                    for f in fs:
                        if f.lower().endswith(self.VEXT):
                            files.append(os.path.join(dp, f))
            elif p.lower().endswith(self.VEXT):
                files = [p]
            for f in files:
                if f in have:
                    continue
                it = QListWidgetItem("%s    （%s）"
                                     % (os.path.basename(f),
                                        os.path.basename(os.path.dirname(f))))
                it.setData(Qt.UserRole, f)
                it.setToolTip(f)
                self.addItem(it)
                have.add(f)
                added += 1
        return added

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            n = self.add_paths(paths)
            event.acceptProposedAction()
            parent = self.window()
            if hasattr(parent, "refresh_pairs"):
                parent.refresh_pairs()
            return
        super().dropEvent(event)


class TrainDialog(_SafeCloseDialog):
    """左边原素材、右边成品，按行配对；两边都支持拖拽。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("训练：教它你的剪辑口味")
        self.setMinimumSize(900, 620)
        self.worker = None

        root = QVBoxLayout(self)
        tip = QLabel("把文件拖进左右两边即可。**按行配对**：左 1 配右 1，左 2 配右 2。"
                     "拖文件夹会自动找出里面的视频。至少 3 组才有参考价值。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

        cols = QHBoxLayout()
        self.left = self._column(cols, "原素材（录播）", "原始录播")
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet("color:#26313d;")
        cols.addWidget(line)
        self.right = self._column(cols, "剪辑成品", "剪好的成品")
        root.addLayout(cols)

        self.pair_label = QLabel("还没有配对")
        self.pair_label.setObjectName("muted")
        root.addWidget(self.pair_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(150)
        root.addWidget(self.log)

        foot = QHBoxLayout()
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_go = QPushButton("开始训练")
        self.btn_go.setObjectName("primary")
        self.btn_go.clicked.connect(self.start)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        foot.addWidget(self.btn_stop)
        foot.addStretch(1)
        foot.addWidget(b_close)
        foot.addWidget(self.btn_go)
        try:
            import ui_theme as _th
            _p4, _b4 = _th.band("输出", "", mark="④")
            _b4.addLayout(foot)
            root.addWidget(_p4)
        except Exception:
            root.addLayout(foot)

    def _column(self, parent_layout, title, kind):
        is_left = kind == "原始录播"
        accent = "#7fc8ff" if is_left else "#8fd694"
        tag = "L" if is_left else "R"

        frame = QFrame()
        frame.setObjectName("col%d" % 0)
        frame.setStyleSheet(
            "QFrame#colBox{border:1px solid %s;border-radius:8px;background:#10161d;}"
            % accent)
        frame.setObjectName("colBox")
        box = QVBoxLayout(frame)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(6)

        head = QLabel("● %s（0）" % title)
        head.setStyleSheet("color:%s;font-weight:700;font-size:13px;" % accent)
        box.addWidget(head)

        hint = QLabel("把文件或文件夹拖到这里" if is_left
                      else "把你剪好的成品拖到这里")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color:#6b7787;border:1px dashed %s;"
                           "border-radius:6px;padding:26px 8px;" % accent)
        box.addWidget(hint)
        hint.hide()

        lst = DropList()
        lst.setMinimumHeight(250)
        lst.setStyleSheet(
            "QListWidget{background:#0b0f14;border:1px solid #26313d;border-radius:6px;}"
            "QListWidget::item{padding:7px 8px;border-bottom:1px solid #161d25;}"
            "QListWidget::item:selected{background:#1d3a55;color:#ffffff;}")
        box.addWidget(lst, 1)

        row = QHBoxLayout()
        b_add = QPushButton("添加…")
        b_add.clicked.connect(lambda _=False: self._add(lst, kind))
        b_del = QPushButton("移除")
        b_del.clicked.connect(lambda _=False: self._remove(lst))
        b_clr = QPushButton("清空")
        b_clr.clicked.connect(lambda _=False: self._clear(lst))
        for b in (b_add, b_del, b_clr):
            b.setFixedHeight(26)
            row.addWidget(b)
        row.addStretch(1)
        box.addLayout(row)

        frame.setMinimumWidth(360)
        parent_layout.addWidget(frame, 1)
        lst._head = head
        lst._hint = hint
        lst._title = title
        return lst

    # ---------- 列表操作 ----------
    def _add(self, lst, kind):
        files, _f = QFileDialog.getOpenFileNames(self, "选择%s" % kind, "",
                                                 "视频 (*.mp4 *.mkv *.mov *.flv)")
        if not files:
            return
        n = lst.add_paths(files)
        self.log.appendPlainText("已添加 %d 个%s" % (n, kind))
        self.refresh_pairs()

    def _remove(self, lst):
        for it in lst.selectedItems():
            lst.takeItem(lst.row(it))
        self.refresh_pairs()

    def _clear(self, lst):
        lst.clear()
        self.refresh_pairs()

    def refresh_pairs(self):
        nl, nr = self.left.count(), self.right.count()
        for lst, n in ((self.left, nl), (self.right, nr)):
            if hasattr(lst, "_head"):
                lst._head.setText("● %s（%d）" % (lst._title, n))
            if hasattr(lst, "_hint"):
                lst._hint.setVisible(n == 0)
        n = min(nl, nr)
        if nl == 0 and nr == 0:
            self.pair_label.setText("还没有配对")
        elif nl != nr:
            self.pair_label.setText("左 %d 个 / 右 %d 个 —— 数量不一致，"
                                    "只按前 %d 组配对（请补齐或删掉多余的）"
                                    % (nl, nr, n))
        else:
            self.pair_label.setText("已配对 %d 组，可以开始训练" % n)
        self.btn_go.setEnabled(n > 0)

    # ---------- 训练 ----------
    def start(self):
        pairs = list(zip(self.left.paths(), self.right.paths()))
        if not pairs:
            QMessageBox.information(self, "还没有配对", "左右两边各放至少一个文件。")
            return
        if len(pairs) < 3:
            self.log.appendPlainText("提示：只有 %d 组，学到的东西有限（建议 3 组以上）" % len(pairs))
        self.btn_go.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log.appendPlainText("开始训练：%d 组，转写+对齐+拟合，可能要几分钟…" % len(pairs))
        _td = load_settings().get("train_dir", "")
        self.worker = TrainWorker(pairs, train_dir=_td)
        self.worker.status.connect(self.log.appendPlainText)
        self.worker.ok.connect(self._on_trained)
        self.worker.err.connect(self._on_train_err)
        self.worker.start()

    def _on_trained(self, msg):
        self.btn_go.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log.appendPlainText(msg)
        self.log.appendPlainText("模型已更新，下次智能剪辑会用它判断。")

    def _on_train_err(self, err):
        self.btn_go.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log.appendPlainText("失败：%s" % err)

    def _stop(self):
        self._halt()
        self.log.appendPlainText("已停止训练")
        self.btn_go.setEnabled(True)
        self.btn_stop.setEnabled(False)


class ModelDownloadWorker(QThread):
    """后台下载模型档位，带进度。"""

    progress = Signal(int, int)      # 已下 MB, 总 MB
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, tier):
        super().__init__()
        self.tier = tier
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            import llm_models as models
            p = models.download(
                self.tier,
                on_progress=lambda a, b: self.progress.emit(int(a), int(b)),
                should_stop=lambda: self._stop)
            self.ok.emit(p)
        except Exception as exc:  # noqa: BLE001
            self.err.emit("%s: %s" % (type(exc).__name__, exc))


class ModelDialog(_SafeCloseDialog):
    """模型档位：看有哪些、下没下、用哪个。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("本地模型档位")
        self.setMinimumWidth(680)
        self.worker = None

        root = QVBoxLayout(self)
        tip = QLabel(self._gpu_line() + "\n这些模型都在本机跑，不联网、不上传素材。"
                     "档位越高判断越准，但占显存和硬盘。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.list = QListWidget()
        self.list.setMinimumHeight(150)
        root.addWidget(self.list, 1)

        self.bar = LiveBar()
        root.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        root.addWidget(self.status)

        foot = QHBoxLayout()
        self.btn_dl = QPushButton("下载")
        self.btn_dl.clicked.connect(self._download)
        self.btn_use = QPushButton("使用这个")
        self.btn_use.setObjectName("primary")
        self.btn_use.clicked.connect(self._use)
        self.btn_del = QPushButton("删除")
        self.btn_del.clicked.connect(self._delete)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        foot.addWidget(self.btn_del)
        foot.addStretch(1)
        foot.addWidget(b_close)
        foot.addWidget(self.btn_dl)
        foot.addWidget(self.btn_use)
        root.addLayout(foot)

        self._refresh()

    def _gpu_line(self):
        """显示显卡和显存，玩家好判断自己能扛哪个档位。"""
        try:
            import subprocess
            exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                               "System32", "nvidia-smi.exe")
            p = subprocess.run([exe, "--query-gpu=name,memory.total",
                                "--format=csv,noheader"],
                               capture_output=True, text=True, errors="replace",
                               timeout=6,
                               creationflags=0x08000000)
            line = (p.stdout or "").strip().splitlines()[0]
            name, mem = [x.strip() for x in line.split(",")]
            gb = float(mem.split()[0]) / 1024.0
            return ("你的显卡：%s · 共 %.1f GB 显存"
                    "（只需安装一个模型，所有本地岗位统一使用）" % (name, gb))
        except Exception:  # noqa: BLE001
            return "没读到显卡信息，下载前先看下面各档位的显存需求"

    def _refresh(self):
        import llm_models as models
        import llm_config
        cfg = llm_config.load()
        cur = llm_config.active_tier(cfg)
        for _c in models.CATALOG:
            models.repair(_c["tier"])      # 把没下完却占了正式文件名的降级
        self.list.clear()
        for c in models.CATALOG:
            have = models.downloaded(c["tier"])
            if have:
                st = "已下载 %.1f GB" % models.size_gb(c["tier"])
            else:
                part = models.path_of(c["tier"]) + ".part"
                if os.path.isfile(part):
                    _g = os.path.getsize(part) / (1024 ** 3)
                    _pct = (_g / c["size"] * 100) if c["size"] else 0
                    st = "下载未完成 %.1f / %.1f GB（%.0f%%）——点下载接着下" % (
                        _g, c["size"], _pct)
                else:
                    st = "未下载（约 %.1f GB）" % c["size"]
            # ★ 只标“已下载且正在用”的档位，没下完的不算
            _sel = c["tier"] == cur
            mark = "★" if (_sel and have) else ("·" if _sel else " ")
            _vr = c.get("vram", 0)
            _sp = c.get("speed", "")
            _ms = "实测" if c.get("measured") else "估算"
            perf = "显存约 %.1f GB%s · %s" % (_vr, ("（" + _ms + "）") if _sp else "", _sp)
            it = QListWidgetItem("%s %s    %s\n      占用：%s\n      %s"
                                 % (mark, c["name"], st, perf, c["note"]))
            it.setData(Qt.UserRole, c["tier"])
            self.list.addItem(it)

    def _tier(self):
        it = self.list.currentItem() or (
            self.list.item(0) if self.list.count() else None)
        return it.data(Qt.UserRole) if it else None

    def _download(self):
        import llm_models as models
        tier = self._tier()
        if not tier:
            return
        if models.downloaded(tier):
            self.status.setText("这个档位已经下载好了")
            return
        models.repair(tier)
        self.btn_dl.setEnabled(False)
        self.bar.start(marquee=False)
        self.worker = ModelDownloadWorker(tier)
        self.worker.progress.connect(self._on_prog)
        self.worker.ok.connect(self._on_ok)
        self.worker.err.connect(self._on_err)
        self.worker.start()

    def _on_prog(self, a, b):
        if b > 0:
            self.bar.setValue(int(a * 1000 / b))
        self.status.setText("下载中：%d MB / %d MB" % (a, b))

    def _on_ok(self, _path):
        self.bar.stop(finished=True)
        self.btn_dl.setEnabled(True)
        self.status.setText("下载完成，可以点「使用这个」")
        self._refresh()

    def _on_err(self, msg):
        self.bar.stop()
        self.btn_dl.setEnabled(True)
        self.status.setText("下载失败：%s" % msg[:120])

    def _use(self):
        import llm_models as models
        import llm_config
        tier = self._tier()
        if not tier:
            return
        if not models.downloaded(tier):
            QMessageBox.information(self, "还没下载", "先下载这个档位再用。")
            return
        llm_config.select_model(tier, models.info(tier)["file"])
        self._refresh()
        self.status.setText(
            "已切换：字幕、分析、编剧、剪辑和导演全流程统一使用 %s"
            % models.info(tier)["name"])

    def _delete(self):
        import llm_models as models
        tier = self._tier()
        if not tier or not models.downloaded(tier):
            return
        if QMessageBox.question(self, "删除模型",
                                "删掉这个档位的模型文件？下次要用得重新下载。") != QMessageBox.Yes:
            return
        models.delete(tier)
        self._refresh()
        self.status.setText("已删除")


class SettingsDialog(QDialog):

    def _open_style_dialog(self):
        """从设置直接调字幕样式（不需要素材）。"""
        try:
            SubtitleDialog(self, {}).exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "字幕样式打不开", str(exc))
    """设置导出目录。留空表示用默认目录。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(760)
        self.cfg = load_settings()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        tip = QLabel("各类导出可以分别指定文件夹。留空 = 输出到工作目录\\导出\\<素材名>。\n"
                     "支持占位符：{素材名} = 素材文件名，{源目录} = 素材所在目录。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        self.edits = {}
        for row, (key, label, hint) in enumerate(OUT_KINDS):
            lb = QLabel(label)
            lb.setMinimumWidth(84)
            ed = QLineEdit(self.cfg.get(key, ""))
            ed.setPlaceholderText("留空 = 默认目录")
            btn_pick = QPushButton("选择…")
            btn_pick.setFixedWidth(72)
            btn_default = QPushButton("默认")
            btn_default.setFixedWidth(60)
            btn_pick.clicked.connect(lambda _=False, e=ed: self._pick(e))
            btn_default.clicked.connect(lambda _=False, e=ed: e.setText(""))
            grid.addWidget(lb, row, 0)
            grid.addWidget(ed, row, 1)
            grid.addWidget(btn_pick, row, 2)
            grid.addWidget(btn_default, row, 3)
            hint_lb = QLabel(hint)
            hint_lb.setObjectName("muted")
            grid.addWidget(hint_lb, row, 4)
            self.edits[key] = ed
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

            # 卡片（单个片段）长度：太长会混进不相关内容
        card_row = QHBoxLayout()
        lb_c = QLabel("片段最长")
        lb_c.setMinimumWidth(84)
        self.cardmax_edit = QLineEdit(self.cfg.get("card_max", "45"))
        self.cardmax_edit.setFixedWidth(70)
        lb_t = QLabel("秒    ±")
        self.cardtol_edit = QLineEdit(self.cfg.get("card_tol", "6"))
        self.cardtol_edit.setFixedWidth(60)
        lb_t2 = QLabel("秒    重新分析后生效")
        lb_t2.setObjectName("muted")
        card_row.addWidget(lb_c)
        card_row.addWidget(self.cardmax_edit)
        card_row.addWidget(lb_t)
        card_row.addWidget(self.cardtol_edit)
        card_row.addWidget(lb_t2)
        card_row.addStretch(1)
        # 片段长度已不再需要：现在一句就是一段
        for _w in (self.cardmax_edit, self.cardtol_edit):
            _w.setEnabled(False)
            _w.setToolTip("已废弃：现在一句话就是一个片段")
        lb_c.setEnabled(False)
        lb_t.setEnabled(False)
        lb_t2.setText("（已废弃：一句一段）")
        # （片段长度已废弃，不再显示）

        mode_row = QHBoxLayout()
        lb_m = QLabel("卡片切法")
        lb_m.setMinimumWidth(84)
        self.cardmode = QComboBox()
        self.cardmode.addItem("按段落合并（推荐）", "paragraph")
        self.cardmode.addItem("一句一卡（最细）", "sentence")
        _cm = self.cfg.get("card_mode", "paragraph")
        _i = self.cardmode.findData(_cm)
        if _i >= 0:
            self.cardmode.setCurrentIndex(_i)
        lb_m2 = QLabel("切点永远落在句子边界，不会切半句")
        lb_m2.setObjectName("muted")
        mode_row.addWidget(lb_m)
        mode_row.addWidget(self.cardmode)
        mode_row.addWidget(lb_m2)
        mode_row.addStretch(1)
        # （卡片切法已固定为一句一卡，不再显示）

        edit_mode_row = QHBoxLayout()
        lb_em = QLabel("剪辑模式")
        lb_em.setMinimumWidth(84)
        self.edit_mode = QComboBox()
        import edit_modes as _edit_modes
        for key, label, desc in _edit_modes.choices():
            self.edit_mode.addItem(label, key)
            self.edit_mode.setItemData(self.edit_mode.count() - 1, desc, Qt.ToolTipRole)
        _em = self.edit_mode.findData(_edit_modes.normalize(self.cfg.get("edit_mode")))
        self.edit_mode.setCurrentIndex(max(0, _em))
        tip_em = QLabel("模式决定内容结构；力度只决定保留多少")
        tip_em.setObjectName("muted")
        edit_mode_row.addWidget(lb_em)
        edit_mode_row.addWidget(self.edit_mode)
        edit_mode_row.addWidget(tip_em)
        edit_mode_row.addStretch(1)
        root.addLayout(edit_mode_row)

        # 百分比是唯一的用户力度设置；内部档位仅用于兼容旧模型提示词。
        ratio_row = QHBoxLayout()
        lb_ratio = QLabel("剪辑保留比例")
        lb_ratio.setMinimumWidth(84)
        self.keep_ratio = QSpinBox()
        self.keep_ratio.setRange(15, 100)
        self.keep_ratio.setSuffix(" %")
        self.keep_ratio.setFixedWidth(88)
        try:
            self.keep_ratio.setValue(int(float(self.cfg.get("keep_ratio", 0.75)) * 100))
        except (TypeError, ValueError):
            self.keep_ratio.setValue(75)
        _tip_ratio = QLabel("15%~100%：越低剪得越狠；无对白长空段始终清理")
        _tip_ratio.setObjectName("muted")
        ratio_row.addWidget(lb_ratio)
        ratio_row.addWidget(self.keep_ratio)
        ratio_row.addWidget(_tip_ratio)
        ratio_row.addStretch(1)
        root.addLayout(ratio_row)

        # 智能剪辑模型来源。字段名与 API 后端约定一致；这里只负责配置，
        # 不测试连接，也绝不把 Key 写进界面日志。
        import ai_api_settings as _ai_api
        api_frame = QFrame()
        api_frame.setFrameShape(QFrame.StyledPanel)
        api_grid = QGridLayout(api_frame)
        api_grid.setContentsMargins(10, 8, 10, 8)
        api_grid.setHorizontalSpacing(8)
        api_grid.setVerticalSpacing(6)
        api_title = QLabel("智能剪辑模型")
        api_title.setStyleSheet("font-weight:600;")
        api_grid.addWidget(api_title, 0, 0, 1, 2)
        self.llm_provider = QComboBox()
        for _pid, _label, _url in _ai_api.PROVIDERS:
            self.llm_provider.addItem(_label, _pid)
        _provider = _ai_api.normalize_provider(self.cfg.get("llm_provider"))
        self.llm_provider.setCurrentIndex(max(
            0, self.llm_provider.findData(_provider)))
        self.llm_api_base_url = QLineEdit(
            str(self.cfg.get("llm_api_base_url") or ""))
        self.llm_api_base_url.setPlaceholderText(
            "例如 https://api.example.com/v1")
        self.llm_api_key = QLineEdit(str(self.cfg.get("llm_api_key") or ""))
        self.llm_api_key.setEchoMode(QLineEdit.Password)
        self.llm_api_key.setPlaceholderText("服务要求时填写；不会显示在日志中")
        self.llm_api_model = QLineEdit(
            str(self.cfg.get("llm_api_model") or ""))
        self.llm_api_model.setPlaceholderText("例如 gpt-4.1-mini / deepseek-chat")
        self.llm_api_timeout = QSpinBox()
        self.llm_api_timeout.setRange(_ai_api.MIN_TIMEOUT, _ai_api.MAX_TIMEOUT)
        self.llm_api_timeout.setSuffix(" 秒")
        self.llm_api_timeout.setValue(_ai_api.normalize_timeout(
            self.cfg.get("llm_api_timeout")))
        for _row_i, (_label, _widget) in enumerate((
                ("提供方", self.llm_provider),
                ("接口地址", self.llm_api_base_url),
                ("API Key", self.llm_api_key),
                ("模型名", self.llm_api_model),
                ("请求超时", self.llm_api_timeout)), start=1):
            _lab = QLabel(_label)
            _lab.setMinimumWidth(64)
            api_grid.addWidget(_lab, _row_i, 0)
            api_grid.addWidget(_widget, _row_i, 1)
        api_grid.setColumnStretch(1, 1)
        self._llm_last_provider = _provider
        self.llm_provider.currentIndexChanged.connect(self._sync_llm_provider)
        self._sync_llm_provider()
        root.addWidget(api_frame)
        # 目标时长：0=自动（按原片比例）；填数字=按你要的分钟数剪
        tgt_row = QHBoxLayout()
        lb_tg = QLabel("时长上限")
        lb_tg.setMinimumWidth(84)
        from PySide6.QtWidgets import QSpinBox as _QSB2
        self.target_spin = _QSB2()
        self.target_spin.setRange(0, 90)
        self.target_spin.setSuffix(" 分钟")
        self.target_spin.setSpecialValueText("不限")
        self.target_spin.setFixedWidth(110)
        try:
            self.target_spin.setValue(int(float(
                self.cfg.get("target_min", "0") or 0)))
        except (TypeError, ValueError):
            self.target_spin.setValue(0)
        tip_tg = QLabel("不限=按内容走，尽量不丢剧情（推荐）；填数字=超过这个长度才按价值裁")
        tip_tg.setObjectName("muted")
        tgt_row.addWidget(lb_tg)
        tgt_row.addWidget(self.target_spin)
        tgt_row.addWidget(tip_tg)
        tgt_row.addStretch(1)
        root.addLayout(tgt_row)
        skip_row = QHBoxLayout()
        from PySide6.QtWidgets import QCheckBox as _QCB
        self.skip_dir_chk = _QCB("跳过导演审片（仍按剧本初剪，速度更快）")
        self.skip_dir_chk.setChecked(
            str(self.cfg.get("skip_director", "0")) in ("1", "true", "True"))
        skip_row.addWidget(QLabel("导演"))
        skip_row.addWidget(self.skip_dir_chk)
        skip_row.addStretch(1)
        root.addLayout(skip_row)

        thumb_row = QHBoxLayout()
        from PySide6.QtWidgets import QCheckBox
        self.thumb_chk = QCheckBox("生成卡片缩略图")
        self.thumb_chk.setChecked(str(self.cfg.get("thumbs", "0")) not in
                                  ("0", "false", "False", ""))
        tip_th = QLabel("开启后每张卡抓一帧，一句一卡时约 9 分钟；关闭则图片区显示台词")
        tip_th.setObjectName("muted")
        thumb_row.addWidget(QLabel("缩略图"))
        thumb_row.addWidget(self.thumb_chk)
        thumb_row.addWidget(tip_th)
        thumb_row.addStretch(1)
        root.addLayout(thumb_row)

        # 出片带字幕：字幕功能本身还封着（样式对话框没开放），
        # 但「成片时间轴」已经能对上，所以至少让成片能带字幕出去。
        sub_row = QHBoxLayout()
        self.burn_chk = QCheckBox("出片时把字幕烧进画面")
        self.burn_chk.setChecked(
            str(self.cfg.get("burn_subs", "0")) in ("1", "true", "True"))
        tip_sb = QLabel("关掉也照样生成 _字幕.srt / _字幕.ass（B站、抖音可直接传）")
        tip_sb.setObjectName("muted")
        sub_row.addWidget(QLabel("字幕"))
        _b_style = QPushButton("字幕样式…")
        _b_style.setFixedHeight(24)
        _b_style.clicked.connect(self._open_style_dialog)
        sub_row.addWidget(_b_style)
        sub_row.addWidget(self.burn_chk)
        sub_row.addWidget(tip_sb)
        sub_row.addStretch(1)
        root.addLayout(sub_row)

        # 自动配 BGM：曲库可在下面直接换，电平/段长等在 conf\bgm.json
        bgm_row = QHBoxLayout()
        self.bgm_chk = QCheckBox("自动配 BGM")
        self.bgm_chk.setChecked(
            str(self.cfg.get("bgm", "1")) in ("1", "true", "True"))
        try:
            import bgm as _bgm
            _cfg_bgm = _bgm.load_conf()
        except Exception:  # noqa: BLE001
            _bgm, _cfg_bgm = None, {}
        bgm_row.addWidget(QLabel("BGM"))
        bgm_row.addWidget(self.bgm_chk)
        bgm_row.addStretch(1)
        root.addLayout(bgm_row)

        bgmdir_row = QHBoxLayout()
        lb_bd = QLabel("BGM 曲库")
        lb_bd.setMinimumWidth(84)
        self.bgmdir_edit = QLineEdit(str(_cfg_bgm.get("folder", "") or ""))
        self.bgmdir_edit.setPlaceholderText("选择一个放 BGM 的文件夹（mp3/wav/flac/m4a）")
        b_bd = QPushButton("选择…")
        b_bd.setFixedWidth(72)
        b_bd.clicked.connect(lambda: self._pick_dir(self.bgmdir_edit))
        self.bgmn_lb = QLabel("")
        self.bgmn_lb.setObjectName("muted")
        self.bgmn_lb.setMinimumWidth(64)
        bgmdir_row.addWidget(lb_bd)
        bgmdir_row.addWidget(self.bgmdir_edit, 1)
        bgmdir_row.addWidget(b_bd)
        bgmdir_row.addWidget(self.bgmn_lb)
        root.addLayout(bgmdir_row)
        self.bgmdir_edit.textChanged.connect(self._count_bgm)
        self._count_bgm()

        # BGM 音量：默认 -22 dBFS（人声约 -20，BGM 略低于人声），固定电平不闪避
        bgmvol_row = QHBoxLayout()
        lb_bv = QLabel("BGM 音量")
        lb_bv.setMinimumWidth(84)
        from PySide6.QtWidgets import QDoubleSpinBox as _QDSB
        self.bgmvol = _QDSB()
        self.bgmvol.setRange(-80.0, -6.0)
        self.bgmvol.setSingleStep(1.0)
        self.bgmvol.setDecimals(0)
        self.bgmvol.setSuffix(" dB")
        self.bgmvol.setFixedWidth(96)
        try:
            _lv = float(_cfg_bgm.get("level_db", -22) or -22)
        except (TypeError, ValueError):
            _lv = -22.0
        self.bgmvol.setValue(_lv)
        tip_bv = QLabel("数值越小越轻；默认 -22（全片固定电平，不闪避）")
        tip_bv.setObjectName("muted")
        bgmvol_row.addWidget(lb_bv)
        bgmvol_row.addWidget(self.bgmvol)
        bgmvol_row.addWidget(tip_bv)
        bgmvol_row.addStretch(1)
        root.addLayout(bgmvol_row)

        # 音量均衡：曲子本身有前奏/副歌/间奏，电平能差 6~10 dB，
        # 只乘一个系数会让副歌盖住人声。均衡是按 2 秒窗口把起伏拉平。
        bgmeven_row = QHBoxLayout()
        lb_be = QLabel("音量均衡")
        lb_be.setMinimumWidth(84)
        self.bgmeven_chk = QCheckBox("按响度拉齐（不同曲子听起来一样响）")
        self.bgmeven_chk.setChecked(
            str(_cfg_bgm.get("loudness_norm", "1")) in ("1", "true", "True"))
        tip_be = QLabel("顺带把曲内起伏拉平；关掉 = 只按 RMS 归一")
        tip_be.setObjectName("muted")
        bgmeven_row.addWidget(lb_be)
        bgmeven_row.addWidget(self.bgmeven_chk)
        bgmeven_row.addWidget(tip_be)
        bgmeven_row.addStretch(1)
        root.addLayout(bgmeven_row)

        tip_bg = QLabel("段长、循环淡化在 conf\\bgm.json 里调")
        tip_bg.setObjectName("muted")
        root.addWidget(tip_bg)

        # 训练产物目录
        traindir_row = QHBoxLayout()
        lb_td = QLabel("训练目录")
        lb_td.setMinimumWidth(84)
        self.traindir_edit = QLineEdit(self.cfg.get("train_dir", ""))
        self.traindir_edit.setPlaceholderText("留空 = 工具目录\\cache_learn")
        b_td = QPushButton("选择…")
        b_td.setFixedWidth(72)
        b_td.clicked.connect(lambda: self._pick_dir(self.traindir_edit))
        traindir_row.addWidget(lb_td)
        traindir_row.addWidget(self.traindir_edit, 1)
        traindir_row.addWidget(b_td)
        root.addLayout(traindir_row)

        # 智能剪辑的保留比例
        ratio_row = QHBoxLayout()
        lb = QLabel("剪辑保留比例")
        lb.setMinimumWidth(84)
        self.ratio_edit = QLineEdit(self.cfg.get("autocut_ratio", "0.30"))
        self.ratio_edit.setFixedWidth(90)
        tip2 = QLabel("0.30 = 只留 30% 的时长（60 分钟 → 18 分钟）；填 0 取消按时长裁剪")
        tip2.setObjectName("muted")
        ratio_row.addWidget(lb)
        ratio_row.addWidget(self.ratio_edit)
        ratio_row.addWidget(tip2)
        ratio_row.addStretch(1)
        self.ratio_edit.setEnabled(False)
        self.ratio_edit.setToolTip("已废弃：改由本地模型逐句判断该留哪些")
        tip2.setText("（已废弃：由模型判断，不再按比例截断）")
        # （保留比例已废弃，不再显示）

        btns = QHBoxLayout()
        preview = QPushButton("按当前素材预览实际路径…")
        preview.clicked.connect(self._preview)
        btns.addWidget(preview)
        btns.addStretch(1)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        root.addLayout(btns)

    def _count_bgm(self):
        """曲库换了立刻显示有几首，免得选错文件夹还不知道。"""
        try:
            import bgm as _bgm
            n = len(_bgm.list_tracks(self.bgmdir_edit.text().strip()))
            self.bgmn_lb.setText(("%d 首" % n) if n else "没有音频")
        except Exception:  # noqa: BLE001
            self.bgmn_lb.setText("")

    def _sync_llm_provider(self, *_args):
        """Enable API fields and apply a provider URL preset non-destructively."""
        import ai_api_settings
        provider = ai_api_settings.normalize_provider(
            self.llm_provider.currentData())
        previous = getattr(self, "_llm_last_provider", provider)
        current_url = self.llm_api_base_url.text().strip()
        old_default = ai_api_settings.default_base_url(previous)
        new_default = ai_api_settings.default_base_url(provider)
        if provider != previous and (not current_url or current_url == old_default):
            self.llm_api_base_url.setText(new_default)
        elif provider != "local" and not current_url and new_default:
            self.llm_api_base_url.setText(new_default)
        enabled = ai_api_settings.is_remote(provider)
        for widget in (self.llm_api_base_url, self.llm_api_key,
                       self.llm_api_model, self.llm_api_timeout):
            widget.setEnabled(enabled)
        self._llm_last_provider = provider

    def _pick_dir(self, edit):
        d = QFileDialog.getExistingDirectory(self, "选择目录", edit.text().strip() or "")
        if d:
            edit.setText(d)

    def _pick(self, edit):
        start = edit.text().strip() or ""
        if start and not os.path.isdir(start):
            start = os.path.dirname(start) if os.path.dirname(start) else ""
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if d:
            edit.setText(d)

    def _preview(self):
        src = getattr(self.parent(), "data", {}).get("source", "") or "示例素材.mp4"
        lines = []
        for key, label, _h in OUT_KINDS:
            raw = self.edits[key].text().strip()
            if raw:
                stem = os.path.splitext(os.path.basename(src))[0]
                srcdir = os.path.dirname(os.path.abspath(src))
                d = raw.replace("{素材名}", stem).replace("{name}", stem) \
                       .replace("{源目录}", srcdir).replace("{src_dir}", srcdir)
                d = os.path.expandvars(d)
            else:
                d = export_dir_for(src)
            lines.append("%s：\n    %s" % (label, d))
        QMessageBox.information(self, "实际输出路径", "\n\n".join(lines))

    def _save(self):
        # 先读出现有配置再改：以前是从空字典重新拼，
        # 没在这张表里的键（mix_mode、burn_subs…）会被整条抹掉。
        cfg = load_settings()
        for k, _l, _h in OUT_KINDS:
            cfg[k] = self.edits[k].text().strip()
        cfg["autocut_ratio"] = self.ratio_edit.text().strip() or "0.30"
        cfg["train_dir"] = self.traindir_edit.text().strip()
        def _clamp(txt, dflt, lo, hi):
            try:
                return str(max(lo, min(hi, int(float(txt)))))
            except (TypeError, ValueError):
                return dflt
        cfg["card_max"] = _clamp(self.cardmax_edit.text(), "45", 15, 300)
        cfg["card_tol"] = _clamp(self.cardtol_edit.text(), "6", 0, 60)
        cfg["card_mode"] = self.cardmode.currentData() or "paragraph"
        cfg["thumbs"] = "1" if self.thumb_chk.isChecked() else "0"
        # 百分比是唯一权威；旧 cut_level 只作为内部兼容字段自动派生。
        try:
            cfg["keep_ratio"] = round(self.keep_ratio.value() / 100.0, 2)
        except Exception:
            cfg["keep_ratio"] = 0.75
        import cut_strength
        cfg["cut_level"] = cut_strength.level_for_ratio(cfg["keep_ratio"])
        cfg["edit_mode"] = self.edit_mode.currentData() or "gameplay"
        cfg["target_min"] = str(int(self.target_spin.value()))
        cfg["skip_director"] = "1" if self.skip_dir_chk.isChecked() else "0"
        cfg["burn_subs"] = "1" if self.burn_chk.isChecked() else "0"
        cfg["bgm"] = "1" if self.bgm_chk.isChecked() else "0"
        import ai_api_settings
        provider = ai_api_settings.normalize_provider(
            self.llm_provider.currentData())
        base_url = self.llm_api_base_url.text().strip()
        model_name = self.llm_api_model.text().strip()
        if ai_api_settings.is_remote(provider):
            if not (base_url.startswith("https://") or
                    base_url.startswith("http://")):
                QMessageBox.warning(self, "设置未保存",
                                    "AI API 接口地址必须以 http:// 或 https:// 开头。")
                return
            if not model_name:
                QMessageBox.warning(self, "设置未保存", "请填写 AI API 模型名。")
                return
        cfg["llm_provider"] = provider
        cfg["llm_api_base_url"] = base_url
        cfg["llm_api_key"] = self.llm_api_key.text().strip()
        cfg["llm_api_model"] = model_name
        cfg["llm_api_timeout"] = ai_api_settings.normalize_timeout(
            self.llm_api_timeout.value())
        # BGM 曲库目录 / 音量存在 conf\bgm.json（那份配置管电平、段长等）
        try:
            import bgm as _bgm
            _cur = _bgm.load_conf()
            _patch = {}
            _fol = self.bgmdir_edit.text().strip()
            if _fol != str(_cur.get("folder", "") or ""):
                _patch["folder"] = _fol
            try:
                _vol = "%.0f" % float(self.bgmvol.value())
            except (TypeError, ValueError):
                _vol = ""
            if _vol and abs(float(_cur.get("level_db", -22) or -22)
                            - float(_vol)) > 0.01:
                _patch["level_db"] = _vol
            _ev = "1" if self.bgmeven_chk.isChecked() else "0"
            if _ev != str(_cur.get("loudness_norm", "1")):
                _patch["loudness_norm"] = _ev
            if _patch:
                _bgm.save_conf(_patch)
        except Exception:  # noqa: BLE001
            pass
        if not save_settings(cfg):
            QMessageBox.warning(self, "保存失败", "写不进 %s" % SETTINGS_PATH)
            return
        self.cfg = cfg
        self.accept()


class LiveBar(QProgressBar):
    """会动的进度条：工作时有流动光带 + 呼吸感，停下就静止。

    原来只有一条静止的蓝条，看不出程序到底在不在动，尤其卡在某个阶段时
    很像死机。这里在有任务时叠一条滚动的亮带，进度未知（marquee）时来回扫。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setTextVisible(False)
        self.setFixedHeight(10)
        self._phase = 0.0
        self._active = False
        self._marquee = False
        self._done = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setStyleSheet("QProgressBar{border:none;background:#1b232c;"
                           "border-radius:5px;}")

    def start(self, marquee: bool = False):
        self._active = True
        self._marquee = marquee
        self._done = False
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop(self, finished: bool = False):
        self._active = False
        self._timer.stop()
        self._done = finished
        if finished:
            self.setValue(1000)
        self.update()

    def setValue(self, v):
        """进度按 2% 一档显示。

        精确到 0.1% 没有意义，反而让人盯着数字怀疑卡住了；
        按 2% 走，误差 ±1% 之内，符合"大概到哪了"的预期。
        """
        try:
            v = int(v)
        except (TypeError, ValueError):
            return
        if v < 1000:
            v = (v // 20) * 20
        super().setValue(v)

    def _tick(self):
        self._phase = (self._phase + 0.035) % 1.0
        self.update()

    def paintEvent(self, _ev):
        try:
            self._paint()
        except Exception:  # noqa: BLE001
            # 进度条画不出来事小，崩溃事大
            pass

    def _paint(self):
        from PySide6.QtGui import QPainter, QColor, QLinearGradient
        if self.width() <= 0 or self.height() <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        r = h / 2.0
        # 轨道
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1b232c"))
        p.drawRoundedRect(0, 0, w, h, r, r)

        if self._marquee and self._active:
            # 进度未知：一段亮带来回扫
            band = int(w * 0.28)
            pos = int((w + band) * ((self._phase * 2) % 1.0)) - band
            grad = QLinearGradient(pos, 0, pos + band, 0)
            grad.setColorAt(0.0, QColor(47, 124, 246, 0))
            grad.setColorAt(0.5, QColor(90, 170, 255, 230))
            grad.setColorAt(1.0, QColor(47, 124, 246, 0))
            p.setBrush(grad)
            p.drawRoundedRect(pos, 0, band, h, r, r)
        else:
            frac = self.value() / 1000.0
            if frac > 0:
                fw = max(h, int(w * frac))
                color = QColor("#3ecf8e") if self._done else QColor("#2f7cf6")
                p.setBrush(color)
                p.drawRoundedRect(0, 0, fw, h, r, r)
                if self._active:
                    # 工作时有亮带在已填充部分上滚
                    band = int(fw * 0.35) or 1
                    pos = int((fw + band) * self._phase) - band
                    grad = QLinearGradient(pos, 0, pos + band, 0)
                    grad.setColorAt(0.0, QColor(255, 255, 255, 0))
                    grad.setColorAt(0.5, QColor(255, 255, 255, 90))
                    grad.setColorAt(1.0, QColor(255, 255, 255, 0))
                    p.setBrush(grad)
                    p.drawRoundedRect(max(0, pos), 0,
                                      min(band, fw - max(0, pos)), h, r, r)
        p.end()


class CardWidget(QFrame):
    def set_compact(self, on: bool = True):
        """小图模式：把台词行藏起来，只留缩略图和时间，像文件夹里的图标。"""
        self._compact = on
        for name in ("body", "tags_row"):
            w = getattr(self, name, None)
            if w is not None:
                try:
                    w.setVisible(not on)
                except Exception:  # noqa: BLE001
                    pass

    clicked = Signal(str)
    drag_out = Signal(str)
    preview = Signal(str)
    edit_request = Signal(str)

    def __init__(self, card: dict, index_dir: str, selected: bool, scale: float = 1.0):
        super().__init__()
        self.card = card
        self.cid = card["id"]
        self.scale = float(scale or 1.0)
        self._press_pos = None
        self.setObjectName("card")
        self.setFixedSize(int(CARD_W * self.scale),
                          int(CARD_H * self.scale) if self.scale >= 0.9
                          else int(THUMB_H * self.scale) + 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("on", selected)
        self._build(index_dir)
        self._apply_style()
        _full = (self.card.get("text") or "").strip()
        if _full:
            _lines = [x for x in _full.split(" ") if x.strip()]
            _tip = "\n".join(_lines[:40])
            if len(_lines) > 40:
                _tip += "\n…（共 %d 句）" % len(_lines)
            self.setToolTip(_tip)

    def _build(self, index_dir: str):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        tw, th = int(THUMB_W * self.scale), int(THUMB_H * self.scale)
        thumb = QLabel()
        thumb.setFixedSize(tw, th)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 #232a33, stop:1 #171c22);"
            "border:1px solid #2c3540; border-radius:6px;"
            "color:#cfd8e4; font-size:13px;")
        thumb.setWordWrap(True)
        thumb.setMargin(6)
        path = os.path.join(index_dir, self.card.get("thumb", "")) if self.card.get("thumb") else ""
        if path and os.path.isfile(path):
            pm = QPixmap(path)
            if not pm.isNull():
                thumb.setPixmap(pm.scaled(tw, th, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        else:
            # 没有缩略图就把台词写进图片区：一眼能读出这段在说什么
            _dur = float(self.card["end"]) - float(self.card["start"])
            _txt = (self.card.get("text") or "").strip()
            thumb.setText("%s\n%s%s" % (fmt(self.card["start"]),
                                        "%.1f 秒" % _dur,
                                        "" if _txt else "\n（无台词）"))
        lay.addWidget(thumb)

        head = QHBoxLayout()
        head.setSpacing(4)
        t = QLabel(f"{fmt(self.card['start'])} – {fmt(self.card['end'])}")
        t.setStyleSheet("color:#8b95a3; font-size:11px;")
        dur = QLabel(f"{self.card['end'] - self.card['start']:.1f}s")
        dur.setStyleSheet("color:#5c6673; font-size:11px;")
        head.addWidget(t)
        head.addStretch(1)
        # 一句话以上就标出来：卡片只显示第一句，不标的话
        # 玩家会以为“这段内容没了”，其实后面还有好几句话
        _n = len([x for x in (self.card.get("text") or "").split(" ") if x.strip()])
        if _n > 1:
            _cnt = QLabel("%d 句" % _n)
            _cnt.setStyleSheet("color:#7fc8ff; font-size:11px;")
            head.addWidget(_cnt)
        head.addWidget(dur)
        lay.addLayout(head)

        text = (self.card.get("text") or "").strip()
        title = (self.card.get("title") or "").strip()
        main = title or text
        body = QLabel(main[:26] + ("…" if len(main) > 26 else "") if main else "（这段没有台词）")
        body.setWordWrap(True)
        body.setFixedHeight(28)
        if title:
            body.setStyleSheet("color:#eaf1fa; font-weight:600;")
            body.setToolTip(text or "")
        else:
            body.setStyleSheet(
                "color:#d7dee8;" if text else "color:#5f6a77; font-style:italic;")
        lay.addWidget(body)
        self.body = body
        if not text:
            body.setVisible(False)

        tags = QHBoxLayout()
        tags.setSpacing(4)
        self.tags_row = tags
        for cat in (self.card.get("cats") or [])[:2]:
            bg, fg = CAT_COLORS.get(cat, ("#1d232a", "#9fb0c4"))
            tags.addWidget(self._tag(cat, bg, fg))
        if self.card.get("highlight"):
            tags.addWidget(self._tag("高光", "#3a2f12", "#ffd166"))
        if "画面变化大" in (self.card.get("tags") or []):
            tags.addWidget(self._tag("变化大", "#132a3a", "#7fc8ff"))
        if not text and not (self.card.get("cats") or []):
            tags.addWidget(self._tag("无台词", "#2a2f36", "#8b95a3"))
        tags.addStretch(1)
        lay.addLayout(tags)

    def _tag(self, text: str, bg: str, fg: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(
            f"background:{bg}; color:{fg}; border-radius:3px; padding:1px 5px; font-size:10px;")
        return l

    def _apply_style(self):
        self.setStyleSheet(
            """
            QFrame#card { background:#171b20; border:2px solid #2a313a; border-radius:8px; }
            QFrame#card:hover { border-color:#3d4854; }
            QFrame#card[on="true"] { border-color:#2f7cf6; background:#1a2330; }
            QLabel { background: transparent; }
            """
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is not None and (event.buttons() & Qt.LeftButton):
            moved = (event.position().toPoint() - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                self.drag_out.emit(self.cid)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            self._press_pos = None
            self.clicked.emit(self.cid)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.edit_request.emit(self.cid)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = None
            self.preview.emit(self.cid)
        super().mouseDoubleClickEvent(event)


class IndexWorker(QThread):
    status = Signal(str)
    progress = Signal(int)      # 0~1000
    ok = Signal(dict)
    err = Signal(str)

    def __init__(self, src: str, out_dir: str, model: str, models_dir: str,
                 audio_stream: int | None = None):
        super().__init__()
        self.src, self.out_dir, self.model, self.models_dir = src, out_dir, model, models_dir
        self.audio_stream = audio_stream
        self._cancel = False

    def request_cancel(self):
        self._cancel = True

    def is_cancelled(self) -> bool:
        return self._cancel

    def run(self):
        try:
            res = indexer.index_video(
                self.src, self.out_dir, model_size=self.model,
                models_dir=self.models_dir, audio_stream=self.audio_stream,
                on_status=self.status.emit,
                on_progress=lambda f: self.progress.emit(int(max(0.0, min(1.0, f)) * 1000)),
                should_stop=self.is_cancelled)
            self.ok.emit(res)
        except Exception as exc:  # noqa: BLE001
            self.err.emit(str(exc))


class AutoCutWorker(QThread):
    """自动剪辑：判空段 → 判转场 → 出片。全在后台跑，日志实时回传。"""
    status = Signal(str)
    progress = Signal(int)      # 渲染进度 0~1000
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, index_dir: str, out_path: str, ratio: float | None = None,
                 edit_mode=None):
        super().__init__()
        self.index_dir, self.out_path, self.ratio = index_dir, out_path, ratio
        self.edit_mode = edit_mode
        self._cancel = False

    # ---- 协作取消：关窗口时先让线程池把在跑的几段收尾，别硬 terminate ----
    def request_cancel(self):
        self._cancel = True

    def is_cancelled(self) -> bool:
        return self._cancel

    def run(self):
        try:
            import autocut

            data = autocut.load_index(self.index_dir)
            if "analysis" not in data:
                self.err.emit("这份索引是老格式，没有画面/音量分析数据。\n"
                              "请先点「重新索引」重建一次，再自动剪辑。")
                return
            conf = autocut.load_conf()
            import edit_modes
            _mode = edit_modes.normalize(self.edit_mode or load_settings().get("edit_mode"))
            edit_modes.apply_to_conf(conf, _mode)
            self.status.emit("剪辑模式：%s" % edit_modes.label(_mode))
            _story_mode = bool(edit_modes.profile(_mode).get("story_structure"))
            conf.setdefault("render", {})["mix_tracks"] = getattr(self, "mix_tracks", True)

            import cut_strength
            _settings = load_settings()
            _keep_ratio = cut_strength.ratio_from_settings(_settings)
            _lvl = cut_strength.level_for_ratio(_keep_ratio)
            self.status.emit("剪辑保留比例：%.0f%%（内部策略 %s）"
                             % (_keep_ratio * 100.0, _lvl))
            # 导演模式：没有剧本就先读片出剧本，再剪。
            # 以前直接进挑选，看起来像“随手选几段就输出”，没有目标。
            _script = data.get("script") or {}
            from llm_gateway import backend_signature, propagate_api_error
            _ai_backend = backend_signature()
            _script_mismatch = (str(_script.get("cut_level") or "") != _lvl
                                or _script.get("keep_ratio") != _keep_ratio
                                or _script.get("backend_signature", "local") != _ai_backend
                                or str(_script.get("mode") or "") != _mode)
            if (_story_mode and data.get("segments")
                    and (not _script.get("sections") or _script_mismatch)):
                try:
                    import script_writer
                    self.status.emit("编剧：按 %.0f%% 保留比例整理全片剧本…"
                                     % (_keep_ratio * 100.0))
                    script_writer.build(
                        os.path.join(self.index_dir, "index.json"),
                        on_log=self.status.emit, level=_lvl, mode=_mode,
                        keep_ratio=_keep_ratio)
                    data = autocut.load_index(self.index_dir)
                except Exception as exc:  # noqa: BLE001
                    propagate_api_error(exc)
                    self.status.emit("剧本没出来，退回逐句挑选：%s" % str(exc)[:60])

            _old_director = data.get("director_info") or {}
            if (str(_old_director.get("level") or "") != _lvl
                    or _old_director.get("backend_signature", "local") != _ai_backend
                    or str(_old_director.get("mode") or "") != _mode):
                data.pop("director", None)  # 禁止旧档导演结论污染当前初剪

            if (_story_mode and getattr(self, "director_first", False)
                    and not getattr(self, "skip_director", False)):
                self.status.emit("岗位流程：编剧完成后先初剪，导演将在初剪后审片和局部返工")
            duration = float(data["duration"])
            # ---- 空段：按语义判（三档都要先做这一步）----
            self.status.emit("正在找空片段…")
            cuts, report = autocut.detect_cuts(data, conf)
            _sem = autocut.detect_cuts_by_semantics(data, conf)
            if _sem is not None:
                cuts = _sem
                self.status.emit("空段（语义）：%d 处，剪掉 %.1f 分钟"
                                 % (len(_sem), sum(b - a for a, b in _sem) / 60))
            kept = [r for r in report if r.get("action") != "cut"]
            total_cut = sum(b - a for a, b in cuts)
            self.status.emit(
                "空段判定：候选 %d 处，剪掉 %.0f 秒（%.1f%%），保留 %d 处"
                % (len(report), total_cut, total_cut / max(1e-6, duration) * 100,
                   len(kept)))
            segs = autocut.keep_segments(cuts, duration)
            segs = autocut.finalize_segments(segs, autocut.load_units(data),
                                             verbose=None)
            # ---- 重复录制：同一段说了两遍只留更好的那版 ----
            if str(load_settings().get("dedupe", "1")) in ("1", "true", "True"):
                try:
                    import dedupe
                    _dups = dedupe.find_duplicates(
                        data, verbose=lambda m: self.status.emit(m.strip()))
                    if _dups:
                        _b = sum(b - a for a, b in segs)
                        segs = autocut.subtract(segs, dedupe.drop_spans(_dups))
                        _a = sum(b - a for a, b in segs)
                        self.status.emit(
                            "重复内容：%d 组，去掉 %.1f 分钟（留更完整的那版）"
                            % (len(_dups), (_b - _a) / 60))
                    else:
                        self.status.emit("重复内容：没发现")
                except Exception as exc:  # noqa: BLE001
                    self.status.emit("重复检测跳过：%s" % str(exc)[:60])
            self.status.emit("空段+去重后：%d 段，%.1f 分钟"
                             % (len(segs), sum(b - a for a, b in segs) / 60))
            # 内容筛选使用精确百分比；档位只作为旧模型策略兼容字段。
            _segs_before = sum(b - a for a, b in segs)
            segs = autocut.apply_cut_level(data, segs, conf, _lvl,
                                           on_log=self.status.emit,
                                           keep_ratio=_keep_ratio)
            if (_story_mode and segs
                    and not getattr(self, "skip_director", False)):
                try:
                    import director_critic
                    segs, _audit = director_critic.review_and_rebuild(
                        data, segs, level=_lvl, on_log=self.status.emit)
                    data["director_audit"] = _audit
                    try:
                        import json as _j
                        _audit_path = os.path.join(self.index_dir, "index.json")
                        _audit_data = _j.load(open(_audit_path, encoding="utf-8"))
                        _audit_data["director_audit"] = _audit
                        _j.dump(_audit_data, open(_audit_path, "w", encoding="utf-8"),
                                ensure_ascii=False, indent=1)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    self.status.emit("导演审片跳过：%s" % str(exc)[:60])
            self.status.emit(
                "保留比例=%.0f%%：%d 段，%.1f → %.1f 分钟"
                % (_keep_ratio * 100.0, len(segs),
                   _segs_before / 60, sum(b - a for a, b in segs) / 60))
            self.status.emit("保留 %d 段，合计 %.1f 分钟，开始判转场…"
                             % (len(segs), sum(b - a for a, b in segs) / 60))

            seams = autocut.judge_transitions(segs, data["source"], data, conf,
                                              verbose=False)
            n_cut = sum(1 for x in seams if x["kind"] == "cut")
            self.status.emit("转场：硬切 %d 处，叠化 %d 处" % (n_cut, len(seams) - n_cut))

            self.status.emit("开始出片，可能要几分钟…")
            # 字幕：索引里那份是原片时间，成片是重排过的，
            # 所以出片时把「成片时间 ← 原片时间」记下来，再按它重排字幕。
            _subs = None
            try:
                import subtitles as _subt
                _subs = _subt.get_segments(data)
            except Exception as exc:  # noqa: BLE001
                self.status.emit("字幕数据没读到（不影响出片）：%s" % str(exc)[:60])
            _burn = str(load_settings().get("burn_subs", "0")) in ("1", "true", "True")
            _bgm_on = str(load_settings().get("bgm", "1")) in ("1", "true", "True")
            _res = autocut.out_resolution(conf, data)
            _info = autocut.render(data["source"], segs, seams, self.out_path, conf,
                                   on_log=self.status.emit,
                                   on_progress=lambda f: self.progress.emit(int(f * 1000)),
                                   tracks=getattr(self, "tracks", None),
                                   cancel=self.is_cancelled,
                                   subs=_subs, sub_burn=_burn, sub_res=_res,
                                   bgm=_bgm_on)
            # 不管烧不烧，都把成片字幕文件放在成片旁边（B站/抖音可以直接传）
            if _subs and isinstance(_info, dict) and _info.get("timeline"):
                try:
                    _r = autocut.export_subs(data, self.out_path, _info["timeline"],
                                             res=_res)
                    if _r.get("srt"):
                        self.status.emit("成片字幕：%d 条（%s）"
                                         % (_r["n"], os.path.basename(_r["srt"])))
                except Exception as exc:  # noqa: BLE001
                    self.status.emit("成片字幕没导出：%s" % str(exc)[:60])
            self.ok.emit(self.out_path)
        except Exception as exc:  # noqa: BLE001
            # 取消是正常收场，不是错误；用类型名判断，免得 import 没成时
            # 在处理异常的路上再抛 NameError
            if type(exc).__name__ == "RenderCancelled":
                self.status.emit("已取消这次剪辑")
                self.err.emit("已取消")
            else:
                # 完整堆栈写日志，界面上只给一句人话+日志位置（给用户看堆栈没意义）
                _tb_tail = ""
                try:
                    import traceback as _tb
                    import time as _tm
                    _dir = os.path.join(_app_home(), "logs")
                    os.makedirs(_dir, exist_ok=True)
                    _lp = os.path.join(_dir, "ui_errors.log")
                    _txt = ("\n===== %s =====\n" % _tm.strftime("%Y-%m-%d %H:%M:%S")) + _tb.format_exc()
                    with open(_lp, "a", encoding="utf-8") as _fh:
                        _fh.write(_txt)
                    _lines = [x for x in _tb.format_exc().splitlines() if x.strip()]
                    _tb_tail = _lines[-1].strip() if _lines else ""
                except Exception:  # noqa: BLE001
                    pass
                self.err.emit("%s: %s（日志 logs\\ui_errors.log：%s）"
                              % (type(exc).__name__, exc, _tb_tail[:120]))


class ProofreadWorker(QThread):
    """字幕纠错：直接调 proofread.py，把它的输出实时贴到界面上。"""
    status = Signal(str)
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, index_dir: str):
        super().__init__()
        self.index_dir = index_dir

    def run(self):
        try:
            import subprocess as _sp
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "proofread.py")
            try:
                import winproc as _wp
                _cmd = _wp.script_cmd(script, [self.index_dir])
            except Exception:
                _cmd = ([sys.executable, "--run-script", script, self.index_dir]
                        if getattr(sys, "frozen", False)
                        else [sys.executable, "-X", "utf8", script,
                              self.index_dir])
            proc = _sp.Popen(_cmd,
                             stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.status.emit(line)
            rc = proc.wait()
            if rc == 0:
                self.ok.emit(self.index_dir)
            else:
                self.err.emit("纠错进程退出码 %d" % rc)
        except Exception as exc:  # noqa: BLE001
            self.err.emit("%s: %s" % (type(exc).__name__, exc))


class MultiCutWorker(QThread):
    """多个素材各剪一遍，再按队列顺序拼成一条。"""

    status = Signal(str)
    progress = Signal(int)
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, sources, out_path, ratio, tracks=None, mix=True,
                 edit_mode="gameplay"):
        super().__init__()
        self.sources = sources          # [(index_dir, name, tracks)]
        self.out_path = out_path
        self.ratio = ratio
        self.tracks = tracks
        self.mix = mix
        self.edit_mode = edit_mode

    def run(self):
        import autocut
        parts = []
        tmpdir = None
        try:
            import tempfile as _tf
            tmpdir = _tf.mkdtemp(prefix="multicut_")
            n = len(self.sources)
            for k, src_item in enumerate(self.sources, 1):
                idx_dir, name = src_item[0], src_item[1]
                own_tracks = src_item[2] if len(src_item) > 2 else None
                self.status.emit("剪辑 %d/%d：%s" % (k, n, name))
                data = autocut.load_index(idx_dir)
                conf = autocut.load_conf()
                import edit_modes
                edit_modes.apply_to_conf(conf, self.edit_mode)
                conf.setdefault("render", {})["mix_tracks"] = self.mix
                conf.setdefault("select", {})["enabled"] = True
                if self.ratio:
                    conf["select"]["target_ratio"] = float(self.ratio)
                cuts, _rep = autocut.detect_cuts(data, conf)
                segs = autocut.keep_segments(cuts, float(data["duration"]))
                segs = autocut.finalize_segments(segs, autocut.load_units(data),
                                                 verbose=self.status.emit)
                if conf.get("select", {}).get("enabled"):
                    target = (float(data["duration"])
                              * float(conf["select"]["target_ratio"])
                              if conf["select"].get("target_ratio") else None)
                    units = autocut.select_by_sentence(data, conf, target)
                    if units:
                        segs = autocut.intersect(segs, units)
                        self.status.emit("按句子挑：%d 段" % len(units))
                    else:
                        kept = autocut.select_cards(data, conf, target)
                        segs = autocut.intersect(
                            segs, [(float(c["start"]), float(c["end"])) for c in kept])
                if not segs:
                    self.status.emit("  这一段没有保留内容，跳过")
                    continue
                seams = autocut.judge_transitions(segs, data["source"], data, conf,
                                                  verbose=False)
                victim = os.path.join(tmpdir, "part%03d.mp4" % k)
                base = (k - 1) / n

                def _pg(f, _b=base, _n=n):
                    self.progress.emit(int((_b + f / _n) * 1000))

                autocut.render(data["source"], segs, seams, victim, conf,
                               on_log=self.status.emit, on_progress=_pg,
                               tracks=own_tracks or self.tracks)
                parts.append(victim)

            if not parts:
                self.err.emit("没有任何片段被保留，检查一下保留比例")
                return
            self.progress.emit(985)
            self.status.emit("合并 %d 段成一条…" % len(parts))
            if len(parts) == 1:
                import shutil as _sh
                _sh.copy2(parts[0], self.out_path)
            else:
                lst = os.path.join(tmpdir, "cat.txt")
                with open(lst, "w", encoding="utf-8") as fh:
                    for p in parts:
                        fh.write("file '%s'\n" % p.replace("'", "'\\''"))
                ff = autocut.indexer.find_ffmpeg()
                r = __import__("subprocess").run(
                    [ff, "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "concat", "-safe", "0", "-i", lst,
                     "-c", "copy", "-movflags", "+faststart", self.out_path],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", creationflags=autocut._NO_WIN)
                if r.returncode != 0:
                    raise RuntimeError("拼接失败：%s" % (r.stderr or "")[-600:])
            self.progress.emit(1000)
            self.ok.emit(self.out_path)
        except Exception as exc:  # noqa: BLE001
            self.err.emit("%s: %s" % (type(exc).__name__, exc))
        finally:
            if tmpdir:
                import shutil as _sh
                _sh.rmtree(tmpdir, ignore_errors=True)


class ExportWorker(QThread):
    progress = Signal(float)
    log = Signal(str)
    ok = Signal(list)
    err = Signal(str)
    done_one = Signal()          # 每个模式完成一次

    def __init__(self, mode: str, src: str, cards: list[dict], target: str,
                 fast: bool = False, audio_stream: int = 0):
        super().__init__()
        self.mode, self.src, self.cards, self.target = mode, src, cards, target
        self.fast = fast
        self.audio_stream = audio_stream

    def run(self):
        try:
            if self.mode == "single":
                files = exporter.export_single(
                    self.src, self.cards, self.target,
                    on_progress=self.progress.emit, on_log=self.log.emit,
                    fast=self.fast, audio_stream=self.audio_stream)
            else:
                files = [exporter.export_merged(
                    self.src, self.cards, self.target,
                    on_progress=self.progress.emit, on_log=self.log.emit,
                    fast=self.fast, audio_stream=self.audio_stream)]
            self.ok.emit(files)
        except Exception as exc:  # noqa: BLE001
            self.err.emit(str(exc))


class PreviewDialog(QDialog):
    """
    预览一段内容。

    两种用法：
      传 card      —— 先把这一小段切出来（秒切），再播放
      传 play_file —— 直接播放磁盘上已有的文件（比如刚导出的片段）
    """

    def __init__(self, src: str = "", card: dict | None = None,
                 audio_stream: int = 0, parent=None, play_file: str | None = None,
                 audio_streams=None, audio_selection_explicit: bool = False):
        super().__init__(parent)
        self.src = src
        self.card = card or {}
        self.audio_stream = audio_stream
        self.audio_streams = audio_streams
        self.audio_selection_explicit = bool(audio_selection_explicit)
        self.play_file = play_file
        self.clip = ""
        self._own_clip = play_file is None
        self.player = None
        self._dragging = False

        title = (f"预览 · {os.path.basename(play_file)}" if play_file
                 else f"预览 · {self.card.get('id', '')}")
        self.setWindowTitle(title)
        self.resize(900, 580)
        self._build()
        self._prepare()

    def _build(self):
        lay = QVBoxLayout(self)

        if self.play_file:
            head_text = os.path.basename(self.play_file)
        else:
            text = (self.card.get("text") or "（这段没有台词）").strip()
            head_text = (f"{fmt(self.card.get('start', 0))} – {fmt(self.card.get('end', 0))}"
                         f"　{text[:70]}")
        head = QLabel(head_text)
        head.setWordWrap(True)
        lay.addWidget(head)

        if HAS_QT_MEDIA:
            self.video = QVideoWidget(self)
            self.video.setMinimumHeight(340)
            lay.addWidget(self.video, 1)
        else:
            self.video = QLabel("这个环境没有 Qt 多媒体组件，请用下面的按钮打开。", self)
            self.video.setAlignment(Qt.AlignCenter)
            lay.addWidget(self.video, 1)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._seek_to_slider)
        lay.addWidget(self.slider)

        bar = QHBoxLayout()
        self.btn_play = QPushButton("暂停")
        self.btn_play.clicked.connect(self.toggle)
        self.btn_replay = QPushButton("重播")
        self.btn_replay.clicked.connect(self.replay)
        self.btn_sys = QPushButton("用系统播放器打开")
        self.btn_sys.clicked.connect(self.open_system)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.btn_replay)
        bar.addStretch(1)
        bar.addWidget(self.btn_sys)
        lay.addLayout(bar)

        self.tip = QLabel("空格 = 播放/暂停，ESC = 关闭")
        self.tip.setObjectName("muted")
        lay.addWidget(self.tip)

    def _prepare(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if self.play_file:
                if not os.path.isfile(self.play_file):
                    raise FileNotFoundError(self.play_file)
                self.clip = self.play_file
            else:
                tmpdir = os.path.join(indexer.temp_root(), "cards_preview")
                os.makedirs(tmpdir, exist_ok=True)
                # 每次用新文件名：上一次的播放器可能还占着旧文件，复用会撞锁
                stamp = int(time.time() * 1000)
                out = os.path.join(
                    tmpdir, f"{self.card['id']}_{int(self.card['start'])}_{stamp}.mp4")
                used_tracks = exporter._cut_preview(
                    self.src, float(self.card["start"]),
                    max(0.3, float(self.card["end"]) - float(self.card["start"])),
                    out, audio_streams=self.audio_streams,
                    fallback_audio_stream=self.audio_stream,
                )
                if len(used_tracks) > 1:
                    scope = "所选" if self.audio_selection_explicit else "全部"
                    self.tip.setText("正在同时播放%s %d 条音轨" %
                                     (scope, len(used_tracks)))
                elif len(used_tracks) == 1 and self.audio_selection_explicit:
                    self.tip.setText("正在播放所选音轨 %d" % (used_tracks[0] + 1))
                elif not used_tracks:
                    self.tip.setText("这段素材没有音轨，将无声播放")
                self.clip = out
        except Exception as exc:  # noqa: BLE001
            self.clip = ""
            self.tip.setText(f"切不出预览片段：{exc}")
        finally:
            QApplication.restoreOverrideCursor()

        if HAS_QT_MEDIA and self.clip:
            self.audio_out = QAudioOutput(self)
            self.audio_out.setVolume(0.9)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio_out)
            self.player.setVideoOutput(self.video)
            self.player.setSource(QUrl.fromLocalFile(self.clip))
            self.player.errorOccurred.connect(self._on_media_error)
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(lambda d: self.slider.setRange(0, 1000))
            self.player.playbackStateChanged.connect(self._on_state)
            # 等预览对话框进入事件循环再开播，避免部分 Qt 后端停在 LoadedMedia。
            QTimer.singleShot(0, self.player.play)

    # ---- 播放控制 ----
    def toggle(self):
        if not self.player:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def replay(self):
        if self.player:
            self.player.setPosition(0)
            self.player.play()

    def _seek_to_slider(self):
        self._dragging = False
        if self.player and self.player.duration() > 0:
            self.player.setPosition(int(self.player.duration() * self.slider.value() / 1000))

    def _on_position(self, pos):
        if self._dragging:
            return
        d = self.player.duration() if self.player else 0
        if d > 0:
            self.slider.setValue(int(pos * 1000 / d))

    def _on_state(self, state):
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setText("暂停")
        elif state == QMediaPlayer.PausedState:
            self.btn_play.setText("播放")
        else:
            self.btn_play.setText("重播")

    def _on_media_error(self, _err, msg):
        self.tip.setText(f"内置播放器打不开这个文件（{msg}），点「用系统播放器打开」")

    def _remove_later(self, path, tries: int = 6):
        """播放器释放文件句柄要一点时间，删不掉就隔一会儿重试。"""
        def attempt(left):
            try:
                os.remove(path)
            except OSError:
                if left > 0:
                    QTimer.singleShot(300, lambda: attempt(left - 1))
        attempt(tries)
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.toggle()
            return
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def open_system(self):
        if self.clip and os.path.isfile(self.clip):
            os.startfile(self.clip)  # noqa: S606

    def closeEvent(self, event):
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())      # 释放文件句柄
        if self._own_clip and self.clip:
            self._remove_later(self.clip)      # 预览用的临时片段，用完就删
        super().closeEvent(event)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(PRODUCT_NAME)
        self.resize(1080, 760)
        self.setMinimumSize(820, 600)

        self.index_dir = ""
        self.data = {}
        self.info = {}                  # 最近一次探测到的轨信息
        self.last_src = ""              # 用于"重新索引"
        self.selected: set[str] = set()
        self.order: list[str] = []      # 导出时的片段顺序，可拖动调整
        self.worker = None
        self.card_widgets: dict[str, CardWidget] = {}

        self._build_ui()
        self._apply_style()
        self.setAcceptDrops(True)
        self._drag_cache: dict[str, str] = {}

    # ---------------- 界面 ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        title = QHBoxLayout()
        name = QLabel(PRODUCT_NAME)
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        name.setFont(f)
        name.setVisible(False)
        self.file_label = QLabel("还没有打开素材")
        self.file_label.setObjectName("muted")
        title.addWidget(self.file_label)
        title.addStretch(1)
        self.track = QComboBox()
        self.track.setVisible(False)
        self.track.setMinimumWidth(220)
        self.track.addItem("音轨：自动", None)
        self.track.setToolTip("多音轨素材可以指定用哪一条。默认自动挑说话那条。")
        title.addWidget(self.track)
        self.model = QComboBox()
        self.model.setMinimumWidth(180)
        self.model.addItem("识别：准确（large-v3）", indexer.MODEL_ACCURATE)
        self.model.addItem("识别：快（turbo，字更乱）", indexer.MODEL_FAST)
        self.model.setToolTip(
            "large-v3 中文明显更准，但速度大约慢一倍；\n"
            "turbo 快，适合先快速过一遍。改完点「重新索引」生效。")
        title.addWidget(self.model)
        self.btn_reindex = QPushButton("重新索引")
        self.btn_reindex.setVisible(False)
        self.btn_reindex.clicked.connect(self.reindex)
        self.btn_reindex.setEnabled(False)
        title.addWidget(self.btn_reindex)
        self.btn_workdir = QPushButton("工作目录")
        self.btn_workdir.setVisible(False)
        self.btn_workdir.setToolTip("索引和导出的存放位置")
        self.btn_workdir.clicked.connect(self.open_work_root)
        title.addWidget(self.btn_workdir)
        self.btn_play_file = QPushButton("播放文件…")
        self.btn_play_file.setVisible(False)
        self.btn_play_file.setToolTip("用内置播放器打开任意视频，比如刚导出的片段")
        self.btn_play_file.clicked.connect(self.pick_play_file)
        title.addWidget(self.btn_play_file)
        self.btn_open = QPushButton("打开素材…")
        self.btn_open.setMinimumHeight(30)
        self.btn_open.setObjectName("primary")
        self.btn_open.clicked.connect(self.open_source)
        self.btn_load = QPushButton("打开已有索引…")
        self.btn_load.setVisible(False)
        self.btn_load.clicked.connect(self.open_index)
        title.addWidget(self.btn_open)
        try:
            import ui_theme as _th
            _p1, _b1 = _th.band("素材", "打开素材 → 自动分析", mark="①")
            _b1.addLayout(title)
            root.addWidget(_p1)
        except Exception:
            root.addLayout(title)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._bar_layout = bar          # 待会儿挪到卡片区正上方
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜台词里的词…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.search, 2)

        self.cat_filter = QComboBox()
        self.cat_filter.addItem("全部分类", None)
        self.cat_filter.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.cat_filter)

        # 卡片显示大小：像资源管理器那样，几个档位切换
        self.size_box = QComboBox()
        for label, sc in (("小图", 0.62), ("中图", 0.85), ("大图", 1.15), ("超大", 1.5)):
            self.size_box.addItem(label, sc)
        self.size_box.setCurrentIndex(1)
        self.size_box.currentIndexChanged.connect(self._on_scale_changed)
        bar.addWidget(self.size_box)

        self.filter = QComboBox()
        self.filter.addItems(["全部片段", "只有音量高光", "画面变化大", "有台词的"])
        self.filter.currentTextChanged.connect(self.refresh)
        bar.addWidget(self.filter)

        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_hi = QPushButton("只看高光")
        self.btn_all.clicked.connect(lambda: self.select_all(True))
        self.btn_none.clicked.connect(lambda: self.select_all(False))
        self.btn_hi.clicked.connect(self.select_highlights)
        bar.addWidget(self.btn_all)
        for _b in (self.btn_none, self.btn_hi):
            _b.setVisible(False)
        self.btn_retag = QPushButton("重算标题")
        self.btn_retag.setToolTip("按现有台词重新生成标题和分类（老索引也能用）")
        self.btn_retag.clicked.connect(self.retag)
        self.btn_retag.setVisible(False)
        try:
            import ui_theme as _th
            _p2, _b2 = _th.band("筛选", "搜台词 / 按分类 / 按图幅", mark="②")
            _b2.addLayout(bar)
            root.addWidget(_p2)
        except Exception:
            root.addLayout(bar)

        # ---- 素材队列（拖进来先排队，点「开始分析」才干活）----
        self.queue_panel = QWidget()
        qp = QVBoxLayout(self.queue_panel)
        qp.setContentsMargins(0, 4, 0, 4)
        qp.setSpacing(4)

        head = QHBoxLayout()
        self.queue_title = QLabel("素材队列")
        self.queue_title.setStyleSheet("font-weight:700;")
        head.addWidget(self.queue_title)
        self.queue_hint = QLabel("拖入后按顺序合成，可上下调整")
        self.queue_hint.setObjectName("muted")
        head.addWidget(self.queue_hint)
        head.addStretch(1)
        qp.addLayout(head)

        self.queue_list = QListWidget()
        self.queue_list.setFixedHeight(76)
        self.queue_list.setStyleSheet(
            "QListWidget{background:#0b0f14;border:1px solid #26313d;border-radius:6px;}"
            "QListWidget::item{padding:5px 8px;}")
        qp.addWidget(self.queue_list)

        qrow = QHBoxLayout()
        self.btn_q_up = QPushButton("上移")
        self.btn_q_down = QPushButton("下移")
        self.btn_q_del = QPushButton("移除")
        self.btn_q_clear = QPushButton("清空")
        for b in (self.btn_q_up, self.btn_q_down, self.btn_q_del, self.btn_q_clear):
            b.setFixedHeight(28)
            qrow.addWidget(b)
        self.btn_q_analyze = QPushButton("开始分析")
        self.btn_q_analyze.setObjectName("primary")
        self.btn_q_analyze.setFixedHeight(28)
        self.btn_q_tracks = QPushButton("音轨…")
        self.btn_q_tracks.setFixedHeight(28)
        self.btn_q_up.clicked.connect(lambda: self._q_move(-1))
        self.btn_q_down.clicked.connect(lambda: self._q_move(1))
        self.btn_q_del.clicked.connect(self._q_remove)
        self.btn_q_clear.clicked.connect(self._q_clear)
        self.btn_q_analyze.clicked.connect(self._q_start)
        self.btn_q_tracks.clicked.connect(self._q_tracks)
        qrow.addStretch(1)
        qrow.addWidget(self.btn_q_tracks)
        qrow.addWidget(self.btn_q_analyze)
        qp.addLayout(qrow)

        self.queue_panel.setVisible(False)
        root.addWidget(self.queue_panel)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QWidget()
        self.grid = QGridLayout(self.holder)
        self.grid.setSpacing(10)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.setWidget(self.holder)
        self.scroll.setAcceptDrops(True)
        self.scroll.viewport().setAcceptDrops(True)
        self.scroll.viewport().installEventFilter(self)
        self.holder.setAcceptDrops(True)
        self.holder.installEventFilter(self)
        root.addWidget(self.scroll, 1)
        # 搜索/筛选/全选这一行放到卡片区正上方（队列按钮之下），
        # 因为它作用的对象就是下面这些卡片，放上面反而离得远。
        try:
            root.removeItem(self._bar_layout)
            _idx = root.indexOf(self.scroll)
            if _idx >= 0:
                root.insertLayout(_idx, self._bar_layout)
            import ui_theme as _th
            _j = root.indexOf(self.scroll)
            if _j >= 0:
                root.insertWidget(_j, _th.section("③ 卡片",
                                                  "单击选中 · 双击预览 · 可拖拽排序"))
        except Exception:  # noqa: BLE001
            pass

        order_head = QHBoxLayout()
        order_lbl = QLabel("已选顺序（上下拖动可以改顺序，合并时按这个顺序出片）")
        order_lbl.setObjectName("muted")
        order_head.addWidget(order_lbl)
        order_head.addStretch(1)
        root.addLayout(order_head)

        self.order_list = QListWidget()
        self.order_list.setFixedHeight(84)
        self.order_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.order_list.setDefaultDropAction(Qt.MoveAction)
        self.order_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.order_list.model().rowsMoved.connect(self._on_order_moved)
        root.addWidget(self.order_list)

        foot = QHBoxLayout()
        self.count = QLabel("0 段")
        self.count.setObjectName("muted")
        foot.addWidget(self.count)
        self.fast = QCheckBox("快速模式")
        self.fast.setChecked(True)
        self.fast.setToolTip(
            "勾上：导出片段时直接复制原数据，几百毫秒就出来，画质无损。\n"
            "代价：切点只能落在关键帧上，开头可能提前一两秒。\n"
            "作用范围：单段导出、拖出。\n"
            "「合并成一条」不受影响——合并必须重新编码才能切准。")
        foot.addWidget(self.fast)
        foot.addStretch(1)
        extra = QHBoxLayout()
        self.btn_subs = QPushButton("字幕…")
        self.btn_tracks = QPushButton("音轨…")
        self.btn_train = QPushButton("训练…")
        for b in (self.btn_subs, self.btn_tracks, self.btn_train):
            b.setMinimumHeight(28)
            b.setVisible(False)
        extra.addStretch(1)
        self.btn_subs.clicked.connect(self.open_subtitles)
        self.btn_tracks.clicked.connect(self.open_tracks)
        self.btn_train.clicked.connect(self.open_train)
        root.addLayout(extra)

        self.btn_out_dir = QPushButton("设置…")
        self.btn_out_dir.setToolTip("设置各类导出的输出文件夹")
        self.btn_out_dir.clicked.connect(self.open_settings)
        self.btn_out_dir.setVisible(False)
        self.btn_single = QPushButton("导出 ▾")
        self.btn_single.clicked.connect(lambda: self.export("single"))
        self.btn_merge = QPushButton("合并成一条")
        self.btn_merge.setVisible(False)
        self.btn_merge.setObjectName("primary")
        self.btn_merge.clicked.connect(lambda: self.export("merge"))
        self.btn_proof = QPushButton("字幕纠错")
        self.btn_proof.setToolTip(
            "用当前选择的本地模型改正同音字和专有名词错字。\n"
            "术语表在同目录 conf\\terms.txt，改完保存即可生效。\n"
            "只会改词表里列出的写法和单字同音错误，拿不准的会跳过并列出。")
        self.btn_proof.clicked.connect(self.proofread)
        self.btn_proof.setVisible(False)
        self.btn_sub = QPushButton("字幕 ▾")
        self.btn_sub.setMinimumHeight(34)
        self.btn_sub.setToolTip("单独导出字幕，不用烧进画面")
        _sm = QMenu(self.btn_sub)
        _sm.addAction("导出 SRT（给平台上传）", lambda: self.quick_sub("srt"))
        _sm.addAction("导出 ASS（带样式）", lambda: self.quick_sub("ass"))
        _sm.addSeparator()
        _sm.addAction("样式与烧录…", self.open_subtitles)
        self.btn_sub.setMenu(_sm)

        self.btn_mixmode = QPushButton()
        self.btn_mixmode.setMinimumHeight(34)
        self.btn_mixmode.setToolTip(
            "混音输出：选中的几条音轨合成一条，播放器能直接听到全部（推荐）\n"
            "多轨输出：保留成多条独立音轨，给后期软件分轨用，普通播放器只播第一条")
        self.btn_mixmode.clicked.connect(self._toggle_mixmode)
        foot.addWidget(self.btn_sub)
        foot.addWidget(self.btn_mixmode)
        foot.addWidget(self.btn_out_dir)
        self.btn_direct = QPushButton("导演剪辑")
        self.btn_direct.setToolTip(
            "岗位流水线：中立素材分析 → 按力度写剧本 → 按剧本初剪\n"
            "→ 导演全片审查剧情和时长 → 只返工不合格位置\n"
            "勾选『跳过导演审片』时仍会按剧本初剪")
        self.btn_direct.setMinimumHeight(34)
        self.btn_direct.setObjectName("primary")
        self.btn_direct.clicked.connect(lambda: self.auto_cut(director=True))
        self.btn_autocut = QPushButton("智能剪辑")
        self.btn_autocut.setToolTip(
            "自动剪掉真正的空片段（会保住被震惊后的留白），\n"
            "再按画面/声音判转场：能硬切就硬切，接不上才叠化。\n"
            "参数在同目录 conf\\autocut.json 里可改。")
        self.btn_autocut.clicked.connect(self.auto_cut)
        for b in (self.btn_out_dir, self.btn_single, self.btn_merge,
                  self.btn_proof, self.btn_autocut):
            b.setMinimumHeight(34)

        foot.addWidget(self.btn_single)
        foot.addWidget(self.btn_merge)

        foot.addWidget(self.btn_direct)
        self.btn_autocut.setVisible(False)      # 只保留导演剪辑
        root.addLayout(foot)

        self.progress = LiveBar()
        root.addWidget(self.progress)

        self.status = QLabel("打开一个素材，或打开已有索引")
        # 启动自检：本地模型服务/模型缺失时，语义分析、剧本、导演都用不了，
        # 索引里就没有分数和剧本，剪辑会退化成兜底打分（曾经表现为中度=重度）。
        try:
            self._llm_missing = self._check_local_llm()
        except Exception:  # noqa: BLE001
            self._llm_missing = ""
        self.status.setObjectName("muted")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(64)
        self.log.setMaximumBlockCount(300)
        root.addWidget(self.status)
        root.addWidget(self.log)
        self._build_menu(root)
        try:
            self.card_scale = float(load_settings().get("card_scale", "1.0") or 1.0)
        except ValueError:
            self.card_scale = 1.0
        self._refresh_mixmode()
        self._set_busy(False)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QWidget { font-family:"Microsoft YaHei UI","Microsoft YaHei"; font-size:13px; }
            QWidget { background:#0f1318; color:#d7dee8; }
            QScrollArea { border:none; }
            QLabel#muted { color:#7c8794; }
            QCheckBox { color:#d7dee8; spacing:6px; }
            QCheckBox::indicator { width:14px; height:14px; border:1px solid #3a4450; border-radius:3px; background:#171b20; }
            QCheckBox::indicator:checked { background:#2f7cf6; border-color:#2f7cf6; }
            QLineEdit, QComboBox, QPlainTextEdit {
                background:#171b20; border:1px solid #2a313a; border-radius:6px;
                padding:5px 8px; color:#d7dee8;
            }
            QLineEdit:focus, QComboBox:focus { border-color:#2f7cf6; }
            QPushButton {
                background:#1d232a; border:1px solid #2f3842; border-radius:6px;
                padding:6px 12px; color:#d7dee8;
            }
            QPushButton:hover { background:#252d36; }
            QPushButton:disabled { color:#5b6673; border-color:#232a32; }
            QPushButton#primary { background:#2f7cf6; border:none; color:#fff; font-weight:600; }
            QPushButton#primary:hover { background:#2569d8; }
            QPushButton#primary:disabled { background:#2b3a52; color:#7d8b9e; }
            QProgressBar { border:none; border-radius:4px; background:#1d232a; }
            QProgressBar::chunk { background:#2f7cf6; border-radius:4px; }
            QComboBox QAbstractItemView { background:#171b20; color:#d7dee8; }
            """
        )

    # ---------------- 数据 ----------------
    def _warn_no_segments(self):
        """索引里有卡片但没台词：多半是分析时选错了音轨。

        这种情况会连累一串功能（卡片没标题、字幕导不出、预览没声音），
        必须让玩家知道，而不是让他自己猜。
        """
        if not (self.data or {}).get("cards"):
            return
        if self.data.get("segments"):
            return
        tr = self.data.get("audio_stream_used")
        tip = ("这份索引没有台词数据：分析时用的第 %s 条音轨可能是静音的。"
               "建议点「重新全部分析」，并在音轨里选「试听」确认有说话的那条。"
               % ((int(tr[0]) + 1) if isinstance(tr, (list, tuple)) and tr
                  else (int(tr) + 1) if isinstance(tr, int) else "?")
               if tr is not None else
               "这份索引用的是旧版本建的，缺少台词数据。点「重新全部分析」补上。")
        self.log.appendPlainText("⚠ " + tip)
        self.status.setText("⚠ " + tip[:60])

    def load_index_dir(self, index_dir: str) -> bool:
        # 记住当前索引目录：重录清单、字幕纠错等入口都要用
        self.index_dir = index_dir
        self._seg_warned = False
        path = os.path.join(index_dir, "index.json")
        if not os.path.isfile(path):
            return False
        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)
        self.index_dir = index_dir
        self.selected.clear()
        self.order.clear()
        if getattr(self, "_drag_cache", None):
            self._drag_cache.clear()
        self.file_label.setText(
            f"{os.path.basename(self.data.get('source', index_dir))} · "
            f"{indexer.tc(self.data.get('duration', 0))} · "
            f"{len(self.data['cards'])} 段")
        if not getattr(self, "out_dir", ""):
            self.out_dir = export_dir_for(self.data.get("source", "") or index_dir)
        self.last_src = self.data.get("source", "")
        self.info = {"audio_streams": self.data.get("audio_streams") or []}
        self._fill_tracks(self.info)
        used = self.data.get("audio_stream_used")
        if isinstance(used, (list, tuple)):
            used = used[0] if used else 0        # 混音时存的是列表
        if used is not None:
            try:
                pos = self.track.findData(int(used))
            except (TypeError, ValueError):
                pos = -1
            if pos >= 0:
                self.track.setCurrentIndex(pos)
        fails = self.data.get("thumb_failures") or 0
        if fails:
            self.log.appendPlainText(
                f"这张索引里有 {fails} 张卡片没取到缩略图，双击卡片可以直接预览那一段")
        self.status.setText("单击卡片选中，双击预览，选好点右下角导出")
        self.refresh()
        return True

    def cards(self) -> list[dict]:
        return self.data.get("cards", [])

    def visible_cards(self) -> list[dict]:
        q = self.search.text().strip().lower()
        mode = self.filter.currentText()
        want_cat = self.cat_filter.currentData()
        out = []
        for c in self.cards():
            if q:
                hay = ((c.get("title") or "") + " " + (c.get("text") or "")).lower()
                if q not in hay:
                    continue
            if mode == "只有音量高光" and not c.get("highlight"):
                continue
            if mode == "画面变化大" and "画面变化大" not in (c.get("tags") or []):
                continue
            if mode == "有台词的" and not (c.get("text") or "").strip():
                continue
            if want_cat and want_cat not in (c.get("cats") or []):
                continue
            out.append(c)
        return out

    def _on_scale_changed(self, _i=0):
        """切换卡片显示大小：小图/中图/大图/超大。"""
        try:
            self.card_scale = float(self.size_box.currentData() or 0.85)
        except (TypeError, ValueError):
            self.card_scale = 1.0
        cfg = load_settings()
        cfg["card_scale"] = str(self.card_scale)
        save_settings(cfg)
        self.refresh()

    def _fill_cats(self):
        """分类下拉按这份素材里实际出现的分类生成，不再写死那 8 个词。"""
        if not hasattr(self, "cat_filter"):
            return
        cur = self.cat_filter.currentData()
        names = []
        for c in self.cards():
            for x in (c.get("cats") or []):
                if x and x not in names:
                    names.append(x)
        self.cat_filter.blockSignals(True)
        self.cat_filter.clear()
        self.cat_filter.addItem("全部分类", None)
        for n in sorted(names):
            cnt = sum(1 for c in self.cards() if n in (c.get("cats") or []))
            self.cat_filter.addItem("%s（%d）" % (n, cnt), n)
        if cur:
            i = self.cat_filter.findData(cur)
            if i >= 0:
                self.cat_filter.setCurrentIndex(i)
        self.cat_filter.blockSignals(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            from PySide6.QtCore import QTimer
            if not hasattr(self, "_rs_timer"):
                self._rs_timer = QTimer(self)
                self._rs_timer.setSingleShot(True)
                self._rs_timer.timeout.connect(self.refresh)
            self._rs_timer.start(180)
        except Exception:
            pass

    def refresh(self):
        self._fill_cats()
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.card_widgets.clear()

        cards = self.visible_cards()
        scale = float(getattr(self, "card_scale", 1.0) or 1.0)
        cell_w = int(CARD_W * scale)
        cols = max(1, (self.scroll.viewport().width() - 10) // (cell_w + 10))
        for i, c in enumerate(cards):
            w = CardWidget(c, self.index_dir, c["id"] in self.selected, scale=scale)
            if scale < 0.9:
                w.set_compact(True)
            w.clicked.connect(self.toggle)
            w.drag_out.connect(self.on_drag_out)
            w.preview.connect(self.preview_card)
            w.edit_request.connect(self.edit_card_text)
            self.grid.addWidget(w, i // cols, i % cols)
            self.card_widgets[c["id"]] = w
        self._sync_order_list()
        self._update_count()

    def audio_used(self) -> int:
        """预览用哪条音轨。

        如果索引里没有台词，说明当初选的那条轨可能是静音的，
        这时改用音量最大的那条，至少能听见声音。
        """
        _u = self.data.get("audio_stream_used", 0)
        if isinstance(_u, (list, tuple)):
            _u = _u[0] if _u else 0
        try:
            used = int(_u or 0)
        except (TypeError, ValueError):
            used = 0
        if self.data.get("segments"):
            return used
        # 索引里通常没存电平，现场探一次（1~2 秒），挑最响的那条
        try:
            if not hasattr(self, "_live_lv"):
                import autocut
                self._live_lv = autocut.probe_track_levels(
                    (self.data or {}).get("source", "")) or []
            if self._live_lv:
                return int(max(self._live_lv, key=lambda x: x["db"])["ordinal"])
        except Exception:  # noqa: BLE001
            pass
        return used

    def retag(self):
        """按现有台词重新算标题和分类（老索引也能用）。"""
        if not self.cards():
            self.status.setText("先打开一个素材或索引")
            return
        n = tagger.apply_to_cards(self.cards())
        self._save_index()
        self.refresh()
        self.status.setText(f"重新生成了 {n} 张卡片的标题和分类")

    def edit_card_text(self, cid: str):
        """右键卡片改台词：识别错了就自己改，搜索和导出文件名都用它。"""
        card = next((c for c in self.cards() if c["id"] == cid), None)
        if not card:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "改这段的台词",
            "识别不准就自己改（留空 = 没台词）：",
            card.get("text") or "")
        if not ok:
            return
        card["text"] = text.strip()
        info = tagger.describe(card["text"], card.get("start", 0), card.get("end", 0),
                               card.get("tags") or [], bool(card.get("highlight")))
        card["title"], card["cats"] = info["title"], info["cats"]
        self._save_index()
        self.refresh()
        self.status.setText(f"{cid} 的台词已更新，标题：{card['title']}")

    def _save_index(self):
        if not self.index_dir:
            return
        path = os.path.join(self.index_dir, "index.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except OSError as exc:
            QMessageBox.warning(self, "存不进去", f"{path}\n{exc}")

    def preview_card(self, cid: str):
        card = next((c for c in self.cards() if c["id"] == cid), None)
        if not card:
            return
        src = self.data.get("source", "")
        if not os.path.isfile(src):
            QMessageBox.warning(self, "源文件不在了", f"找不到：\n{src}")
            return
        if self.worker and self.worker.isRunning():
            self.status.setText("正在忙，等它跑完再预览")
            return
        streams = self.data.get("audio_streams") or self.info.get("audio_streams") or None
        dlg = PreviewDialog(src, card, self.audio_used(), self,
                            audio_streams=streams)
        dlg.exec()

    def pick_play_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选一个视频播放", "", "视频 (*.mp4 *.mov *.mkv *.flv *.ts *.avi *.m4v *.webm)")
        if path:
            self.preview_file(path)

    def preview_file(self, path: str):
        """播放一个已经存在的文件（导出的片段、成片）。"""
        if not os.path.isfile(path):
            QMessageBox.warning(self, "文件不在", f"找不到：\n{path}")
            return
        dlg = PreviewDialog(parent=self, play_file=path)
        dlg.exec()

    def toggle(self, cid: str):
        if cid in self.selected:
            self.selected.discard(cid)
            self.order = [x for x in self.order if x != cid]
        else:
            self.selected.add(cid)
            if cid not in self.order:
                self.order.append(cid)
        w = self.card_widgets.get(cid)
        if w:
            w.setProperty("on", cid in self.selected)
            w.style().unpolish(w)
            w.style().polish(w)
        self._sync_order_list()
        self._update_count()

    def _sync_order_list(self):
        self.order_list.blockSignals(True)
        self.order_list.clear()
        by_id = {c["id"]: c for c in self.cards()}
        for cid in self.order:
            c = by_id.get(cid)
            if not c:
                continue
            text = (c.get("title") or c.get("text") or "").strip()
            label = (f"{fmt(c['start'])}  {text[:26]}" if text
                     else f"{fmt(c['start'])}  （无台词）")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, cid)
            self.order_list.addItem(item)
        self.order_list.blockSignals(False)

    def _on_order_moved(self, *_args):
        ids = [self.order_list.item(i).data(Qt.UserRole)
               for i in range(self.order_list.count())]
        ids = [i for i in ids if i]
        if ids == self.order:
            return
        self.order = ids
        self.selected = set(ids)
        for cid, w in self.card_widgets.items():
            on = cid in self.selected
            if bool(w.property("on")) != on:
                w.setProperty("on", on)
                w.style().unpolish(w)
                w.style().polish(w)
        self._update_count()

    def select_all(self, on: bool):
        for c in self.visible_cards():
            if on:
                self.selected.add(c["id"])
                if c["id"] not in self.order:
                    self.order.append(c["id"])
            else:
                self.selected.discard(c["id"])
                self.order = [x for x in self.order if x != c["id"]]
        self.refresh()

    def select_highlights(self):
        for c in self.cards():
            if c.get("highlight"):
                self.selected.add(c["id"])
                if c["id"] not in self.order:
                    self.order.append(c["id"])
        self.filter.setCurrentText("全部片段")
        self.refresh()

    def _update_count(self):
        if not getattr(self, "_seg_warned", False):
            self._seg_warned = True
            self._warn_no_segments()
        total = sum(c["end"] - c["start"] for c in self.selected_cards())
        self.count.setText(f"已选 {len(self.selected)} 段 · {fmt(total)}")
        has = bool(self.selected)
        self.btn_single.setEnabled(has)
        self.btn_merge.setEnabled(has)
        if hasattr(self, "btn_autocut"):
            self.btn_autocut.setEnabled(bool(has and self.data.get("source")))
        if hasattr(self, "btn_proof"):
            self.btn_proof.setEnabled(bool(has))

    def selected_cards(self) -> list[dict]:
        return [c for c in self.cards() if c["id"] in self.selected]

    # ---------------- 动作 ----------------
    def open_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择素材", "", "视频 (*.mp4 *.mov *.mkv *.flv *.ts *.avi *.m4v *.webm)")
        if path:
            self.open_source_path(path)

    # ================= 素材队列 =================
    def _queue(self):
        if not hasattr(self, "_qitems"):
            self._qitems = []          # [{"path":..., "info":..., "out":..., "ok":bool}]
        return self._qitems

    def _refresh_queue(self):
        q = self._queue()
        self.queue_list.clear()
        for i, it in enumerate(q, 1):
            mark = "✅" if it.get("ok") else "⏳"
            dur = it["info"].get("duration", 0) if it.get("info") else 0
            tk = it.get("tracks")
            tks = ("  音轨：%s" % "、".join(str(x + 1) for x in tk)) if tk else ""
            txt = "%d. %s   %s  %d 分 %.0f 秒%s" % (
                i, os.path.basename(it["path"]), mark, int(dur // 60), dur % 60, tks)
            row = QListWidgetItem(txt)
            row.setData(Qt.UserRole, i - 1)
            self.queue_list.addItem(row)
        n = len(q)
        self.queue_panel.setVisible(n > 0)
        self.queue_title.setText("素材队列（%d）" % n)
        if n > 1:
            total = sum((it["info"] or {}).get("duration", 0) for it in q)
            self.queue_hint.setText("按顺序合成一条，共 %.0f 分钟" % (total / 60))
        else:
            self.queue_hint.setText("拖入后按顺序合成，可上下调整")
        if hasattr(self, "btn_q_analyze"):
            done_n = sum(1 for it in q if it.get("ok"))
            self.btn_q_analyze.setText(
                "重新全部分析" if done_n == n and n else
                ("开始分析（%d 个待处理）" % (n - done_n) if done_n else "开始分析"))

    def _enqueue(self, paths):
        """把拖进来的视频加进队列（不自动分析）。"""
        q = self._queue()
        have = {os.path.normcase(it["path"]) for it in q}
        added = 0
        for p in paths:
            files = []
            if os.path.isdir(p):
                files = indexer.collect_videos(p)
            elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                files = [p]
            for f in files:
                if os.path.normcase(f) in have:
                    continue
                try:
                    info = indexer.probe_streams(f)
                except Exception:  # noqa: BLE001
                    info = {"duration": 0, "audio_streams": []}
                q.append({"path": f, "info": info, "out": index_dir_for(f), "ok": False})
                have.add(os.path.normcase(f))
                added += 1
        self._refresh_queue()
        return added

    def _q_move(self, delta):
        row = self.queue_list.currentRow()
        q = self._queue()
        j = row + delta
        if row < 0 or j < 0 or j >= len(q):
            return
        q[row], q[j] = q[j], q[row]
        self._refresh_queue()
        self.queue_list.setCurrentRow(j)

    def _q_remove(self):
        row = self.queue_list.currentRow()
        q = self._queue()
        if 0 <= row < len(q):
            q.pop(row)
            self._refresh_queue()

    def _q_clear(self):
        self._queue().clear()
        self._refresh_queue()

    def _q_tracks(self):
        """给队列里选中的那一个素材单独设音轨（分析用哪条、输出留哪几条）。"""
        q = self._queue()
        row = self.queue_list.currentRow()
        if row < 0 and q:
            row = 0
        if not q or row < 0 or row >= len(q):
            self.status.setText("先拖一个素材进来")
            return
        item = q[row]
        dlg = TrackDialog(self, item["path"])
        dlg.exec()
        picked = getattr(dlg, "picked", None)
        if picked is not None:
            item["tracks"] = picked
            self.log.appendPlainText(
                "%s 音轨设为：%s"
                % (os.path.basename(item["path"]),
                   "、".join("第%d条" % (t + 1) for t in picked) or "自动"))
            self._refresh_queue()

    def _q_start(self):
        """依次分析队列里的素材。"""
        q = self._queue()
        if not q:
            self.status.setText("队列是空的，先拖素材进来")
            return
        if self.worker and self.worker.isRunning():
            self.status.setText("正在忙，等当前任务跑完")
            return
        q = [it for it in q]
        self._q_run = [it for it in q if not it.get("ok")]
        if not self._q_run:
            self._q_run = list(q)          # 全部重新分析
            for it in q:
                it["ok"] = False
        self._q_pos = 0
        self._q_total = len(self._q_run)
        self.log.clear()
        self._refresh_queue()
        self._analyze_next()

    def _analyze_next(self):
        # 关键：上一次的 QThread 必须真正结束再换，否则对象被回收时
        # run() 还在收尾，会直接访问违规崩溃（0xc0000005）。
        old = getattr(self, "worker", None)
        if old is not None and old.isRunning():
            old.wait(5000)
        self.worker = None
        if self._q_pos >= self._q_total:
            for it in self._queue():
                it["ok"] = True
            self._refresh_queue()
            first = next((it for it in self._queue() if it.get("ok")), None)
            if first:
                # 先加载，再设状态和进度——load_index_dir 会清进度条和状态文字
                self.load_index_dir(first["out"])
            self._set_busy(False)
            self.progress.setValue(1000)
            self.progress.stop(finished=True)
            self.status.setText("全部分析完成：%d 个素材，可以点智能剪辑" % self._q_total)
            return
        it = self._q_run[self._q_pos]
        self._set_busy(True)
        self.status.setText("分析 %d/%d：%s"
                            % (self._q_pos + 1, self._q_total,
                               os.path.basename(it["path"])))
        models_dir = os.path.join(HERE, "models")
        _tk = it.get("tracks")
        if not _tk:
            # 队列项没记住音轨（比如先进队列后选的、或重加过素材），
            # 退回「上一次在音轨对话框里保存的选择」，别悄悄走自动挑。
            try:
                _saved = str(load_settings().get("autocut_tracks", "") or "")
                _lst = [int(x) - 1 for x in _saved.replace("，", ",").split(",")
                        if x.strip().isdigit()]
                if _lst:
                    _tk = _lst
                    it["tracks"] = list(_lst)
                    self.status.emit("本素材未单独设音轨，沿用上次选择：%s"
                                     % ",".join(str(x + 1) for x in _lst))
            except Exception:  # noqa: BLE001
                pass
        # 勾了几条就混几条去识别：只拿第一条会漏掉说话所在的那条
        self.worker = IndexWorker(it["path"], it["out"], self.model.currentData(),
                                  models_dir,
                                  audio_stream=(list(_tk) if _tk else None))
        base = self._q_pos
        total = max(1, self._q_total)

        def on_prog(v, _b=base, _t=total):
            self.progress.setValue(int((_b + v / 1000.0) / _t * 1000))

        self.worker.status.connect(self.status.setText)
        self.worker.progress.connect(on_prog)
        self._q_pending_advance = False
        self.worker.ok.connect(lambda res, _it=it: self._q_mark(_it, res))
        self.worker.err.connect(self._q_fail_one)
        self.worker.finished.connect(self._q_advance)
        self.progress.setValue(int(base / total * 1000))
        self.progress.start(marquee=False)
        self.worker.start()

    def _q_mark(self, item, res):
        item["ok"] = True
        self._q_pending_advance = True

    def _q_fail_one(self, msg):
        self.log.appendPlainText("有一个素材分析失败：%s" % msg)
        self._q_pending_advance = True

    def _q_advance(self):
        """等 worker 的 finished 到了再进下一个，避免线程还在跑就被换掉。"""
        if not getattr(self, "_q_pending_advance", False):
            return
        self._q_pending_advance = False
        self._q_pos += 1
        self._refresh_queue()
        QTimer.singleShot(0, self._analyze_next)

    def open_source_path(self, path: str):
        if not os.path.isfile(path):
            return
        self.last_src = path
        self.file_label.setText(os.path.basename(path))

        # 之前建过索引就直接用，别白等几十分钟
        if self._try_load_existing(path):
            return

        # 先把轨列出来，多音轨时让用户能选，不用猜
        try:
            self.info = indexer.probe_streams(path)
            self._fill_tracks(self.info)
            n = len(self.info.get("audio_streams") or [])
            if n > 1:
                self.log.appendPlainText(
                    f"检测到 {n} 条音轨，默认自动挑说话那条（可以手动指定后点「重新索引」）")
        except Exception as exc:  # noqa: BLE001
            self.log.appendPlainText(f"探测音轨失败：{exc}")
        self._start_index()

    def _try_load_existing(self, path: str) -> bool:
        """同一个素材如果已经建过索引，直接打开它。"""
        out_dir = index_dir_for(path)
        jp = os.path.join(out_dir, "index.json")
        if not os.path.isfile(jp):
            return False
        try:
            with open(jp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        freshness = indexer.index_freshness(data, path, index_path=jp)
        if not freshness.get("reusable"):
            self.log.appendPlainText(
                "上次索引与当前素材不再一致（%s），正在自动重建。"
                % freshness.get("reason", "素材身份不匹配"))
            return False
        if not data.get("cards"):
            return False

        if not self.load_index_dir(out_dir):
            return False
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(jp)))
        msg = f"用的是上次的索引：{len(data['cards'])} 张卡片，{stamp} 建的"
        msg += ("。素材身份已核对；要重来请点「重新索引」"
                if not freshness.get("legacy") else
                "。旧版索引已按路径和时间核对；建议有空重建一次")
        self.status.setText(msg)
        self.log.appendPlainText(msg)
        self.log.appendPlainText(f"索引位置：{out_dir}")
        return True

    def _fill_tracks(self, info: dict):
        streams = info.get("audio_streams") or []
        self.track.blockSignals(True)
        self.track.clear()
        if len(streams) > 1:
            self.track.addItem(f"音轨：自动挑（共 {len(streams)} 条）", None)
        for s in streams:
            self.track.addItem(s["label"], s["ordinal"])
        self.track.setCurrentIndex(0)
        self.track.blockSignals(False)
        self.track.setEnabled(bool(streams))
        self.btn_reindex.setEnabled(bool(self.last_src))

    def reindex(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("正在处理，等它跑完")
            return
        if not self.last_src:
            return
        self._start_index()

    def _selected_tracks(self):
        """从队列里取这个素材勾选的音轨列表（没勾就 None，让引擎自动挑）。"""
        try:
            src = getattr(self, "last_src", "")
            for it in self._queue():
                if it["path"] == src and it.get("tracks"):
                    return list(it["tracks"])
        except Exception:  # noqa: BLE001
            pass
        return None

    def _start_index(self):
        path = self.last_src
        if not path:
            return
        out_dir = index_dir_for(path)
        models_dir = os.path.join(HERE, "models")
        _sel = self._selected_tracks()
        track = _sel if _sel else self.track.currentData()

        # 重建前先把旧索引备份一份：你手动改过的台词就在里面，别被冲掉
        old = os.path.join(out_dir, "index.json")
        if os.path.isfile(old):
            try:
                os.makedirs(out_dir, exist_ok=True)
                shutil.copy2(old, os.path.join(out_dir, "index.bak.json"))
            except OSError:
                pass

        self._set_busy(True)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.progress.start()
        self.status.setText(f"正在建立索引：{os.path.basename(path)}…")
        self.log.clear()
        self.worker = IndexWorker(path, out_dir, self.model.currentData(), models_dir,
                                  audio_stream=track)
        self.worker.status.connect(self.status.setText)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.ok.connect(self._on_indexed)
        self.worker.err.connect(self._on_error)
        self.worker.start()

    # ---------------- 拖进来 ----------------
    def _classify(self, paths):
        """判断拖进来的是视频还是索引文件夹。"""
        for raw in paths:
            p = os.path.normpath(raw)
            if os.path.isdir(p):
                if os.path.isfile(os.path.join(p, "index.json")):
                    return ("index", p)
                vids = indexer.collect_videos(p)
                if vids:
                    return ("video", vids)
            elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                return ("video", p)
        return None

    def _handle_drop(self, mime) -> bool:
        if not mime.hasUrls():
            return False
        if self.worker and self.worker.isRunning():
            self.status.setText("正在忙，等当前任务跑完再拖")
            return False
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        kind = self._classify(paths)
        if not kind:
            self.status.setText("拖进来的不是视频，也不是索引文件夹")
            return False
        what, payload = kind
        if what == "index":
            self.load_index_dir(payload)
            return True
        n = self._enqueue(paths)
        if n:
            self.status.setText("已加入 %d 个素材，点右边「开始分析」才开始" % n)
            self.progress.setValue(0)
            self.progress.stop()
        else:
            self.status.setText("这些素材已经在队列里了")
        return True

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self._handle_drop(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.DragEnter, QEvent.DragMove):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return True
        elif et == QEvent.Drop:
            if self._handle_drop(event.mimeData()):
                event.acceptProposedAction()
                return True
        return super().eventFilter(obj, event)

    # ---------------- 拖出去 ----------------
    def prepare_drag_file(self, cid: str) -> str | None:
        """把一张卡片变成磁盘上的 mp4，然后才能拖到资源管理器/剪映/PR。"""
        if self.worker and self.worker.isRunning():
            self.status.setText("先等当前任务跑完再拖")
            return None
        cached = self._drag_cache.get(cid)
        if cached and os.path.isfile(cached):
            return cached
        card = next((c for c in self.cards() if c["id"] == cid), None)
        if not card:
            return None
        src = self.data.get("source", "")
        if not os.path.isfile(src):
            QMessageBox.warning(self, "源文件不在了", f"找不到：\n{src}")
            return None

        base_out = getattr(self, "out_dir", "") or export_dir_for(src)
        drag_dir = os.path.join(base_out, "拖出")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.status.setText(f"正在准备拖出的文件（{card['end'] - card['start']:.0f} 秒）…")
        QApplication.processEvents()
        try:
            files = exporter.export_single(
                src, [card], drag_dir,
                on_progress=lambda p: QApplication.processEvents(),
                fast=self.fast.isChecked(), audio_stream=self.audio_used())
            if not files:
                return None
            self._drag_cache[cid] = files[0]
            self.status.setText(f"可以拖了：{os.path.basename(files[0])}")
            return files[0]
        except Exception as exc:  # noqa: BLE001
            self.status.setText("准备拖出失败")
            QMessageBox.warning(self, "准备失败", str(exc))
            return None
        finally:
            QApplication.restoreOverrideCursor()

    def _drag_mime(self, path: str) -> QMimeData:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path)])
        return mime

    def on_drag_out(self, cid: str):
        path = self.prepare_drag_file(cid)
        if not path:
            return
        card = next((c for c in self.cards() if c["id"] == cid), {})
        drag = QDrag(self)
        drag.setMimeData(self._drag_mime(path))
        thumb = os.path.join(self.index_dir, card.get("thumb", "")) if card.get("thumb") else ""
        if thumb and os.path.isfile(thumb):
            pix = QPixmap(thumb)
            if not pix.isNull():
                pix = pix.scaledToWidth(200, Qt.SmoothTransformation)
                drag.setPixmap(pix)
                drag.setHotSpot(pix.rect().center())
        drag.exec(Qt.CopyAction)
        self.status.setText("拖完了。继续挑，或者点导出")

    def open_index(self):
        d = QFileDialog.getExistingDirectory(self, "选择索引文件夹（里面要有 index.json）")
        if not d:
            return
        if not self.load_index_dir(d):
            QMessageBox.warning(self, "没有索引", "这个文件夹里没有 index.json。")

    def _on_indexed(self, res):
        self.progress.stop(finished=True)
        self._set_busy(False)
        self.load_index_dir(res.get("index_dir") or index_dir_for(res["source"]))
        self.log.appendPlainText(f"索引完成：{len(res['cards'])} 张卡片")

    def _on_error(self, msg):
        self.progress.stop()
        self._set_busy(False)
        self.status.setText("出错了")
        hint = ""
        low = (msg or "").lower()
        if "winerror 5" in low or "拒绝访问" in msg or "permission" in low:
            hint = ("\n\n—— 这是写盘被拒（常见的三个原因）：\n"
                    "1. 目标文件夹在桌面/文档/图片里，被 Windows「受控文件夹访问」拦了\n"
                    "2. 目标盘只读或空间不足\n"
                    "3. 文件夹被别的程序占用\n\n"
                    f"索引和导出默认放在：{indexer.work_root()}\n"
                    "可以在主界面点「输出位置…」换一个地方。")
        self.log.appendPlainText(msg + hint)
        QMessageBox.warning(self, "没成功", msg + hint)

    def open_work_root(self):
        d = indexer.work_root()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)  # noqa: S606
        except OSError as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    def quick_sub(self, kind="srt"):
        """底栏直接导出字幕，省得进对话框。"""
        import subtitles as sd
        if not (self.data or {}).get("segments"):
            QMessageBox.information(
                self, "还没有字幕",
                "这份素材还没有逐句时间轴。先分析一次素材，字幕就会生成。")
            return
        name = (self.data.get("name") or "字幕").replace("/", "_")
        out_dir = export_dir_for(self.data.get("source", ""))
        os.makedirs(out_dir, exist_ok=True)
        if kind == "ass":
            default = os.path.join(out_dir, name + ".ass")
            flt = "ASS 字幕 (*.ass)"
        else:
            default = os.path.join(out_dir, name + ".srt")
            flt = "SRT 字幕 (*.srt)"
        path, _f = QFileDialog.getSaveFileName(self, "导出字幕", default, flt)
        if not path:
            return
        try:
            if kind == "ass":
                _p, n = sd.write_ass(self.data, path, sd.load_style())
            else:
                _p, n = sd.write_srt(self.data, path, sd.load_style())
            self.status.setText("已导出 %s：%d 句" % (kind.upper(), n))
            self.log.appendPlainText("字幕已导出：%s（%d 句）" % (_p, n))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导出失败", str(exc))

    def open_subtitles(self):
        # 没打开素材也允许进来调样式（预览改用示例句），只是不能导出
        SubtitleDialog(self, self.data or {}).exec()

    def open_tracks(self):
        src = (self.data or {}).get("source", "")
        if not src or not os.path.isfile(src):
            # 用弹窗提示，别只在状态栏写一行字——那样看着像“点了没反应”
            QMessageBox.information(
                self, "还没打开素材",
                "音轨信息要从源文件读，先打开一个素材（或拖进队列）。")
            return
        dlg = TrackDialog(self, src)
        dlg.exec()
        # 从菜单打开时也把结果记到队列里对应素材上，
        # 免得“选了音轨但分析时没用上”这种事再发生。
        picked = getattr(dlg, "picked", None)
        if picked is not None:
            for it in self._queue():
                if os.path.normcase(it.get("path", "")) == os.path.normcase(src):
                    it["tracks"] = list(picked)
                    self.log.appendPlainText(
                        "%s 音轨设为：%s" % (os.path.basename(src),
                        "、".join("第%d条" % (t + 1) for t in picked) or "自动"))
                    break
            self._refresh_queue()

    def open_train(self):
        TrainDialog(self).exec()

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.log.appendPlainText("设置已保存：%s" % SETTINGS_PATH)
            self._show_out_dirs()

    def _show_out_dirs(self):
        src = (self.data or {}).get("source", "")
        if not src:
            return
        parts = []
        for key, label, _h in OUT_KINDS:
            d = resolve_out_dir(key, src)
            parts.append("%s → %s" % (label, d))
        self.log.appendPlainText("当前输出目录：")
        for p in parts:
            self.log.appendPlainText("  " + p)

    def proofread(self):
        """用本地小模型纠错台词。"""
        if not self.data or not self.data.get("cards"):
            self.status.setText("先打开一个素材或索引")
            return
        idx_dir = getattr(self, "index_dir", "")
        if not idx_dir or not os.path.isfile(os.path.join(idx_dir, "index.json")):
            self.status.setText("这个索引没有目录信息，先重新索引一次")
            return

        self._set_busy(True)
        self.log.clear()
        self.status.setText("字幕纠错：正在启动本地模型…")
        self.progress.start(marquee=True)
        self.worker = ProofreadWorker(idx_dir)
        self.worker.status.connect(self.status.setText)
        self.worker.status.connect(lambda m: self.log.appendPlainText(m))
        self.worker.ok.connect(self._on_proof_done)
        self.worker.err.connect(self._on_error)
        self.worker.start()

    def _on_proof_done(self, idx_dir: str):
        self._set_busy(False)
        # 重新读一遍索引，界面上立刻能看到改后的文字
        try:
            p = os.path.join(idx_dir, "index.json")
            with open(p, encoding="utf-8") as handle:
                self.data = json.load(handle)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            self.log.appendPlainText("重新载入索引失败：%s" % exc)
        record = os.path.join(idx_dir, "订正记录.md")
        self.status.setText("字幕纠错完成")
        if os.path.isfile(record):
            self.log.appendPlainText("订正记录：%s" % record)
        QMessageBox.information(self, "纠错完成",
                                "台词已更新。\n\n逐条改动详见：\n%s" % record)

    def _mix_mode(self) -> str:
        m = (load_settings().get("mix_mode") or "mix").strip()
        return "tracks" if m == "tracks" else "mix"

    def _toggle_mixmode(self):
        m = "tracks" if self._mix_mode() == "mix" else "mix"
        cfg = load_settings()
        cfg["mix_mode"] = m
        save_settings(cfg)
        self._refresh_mixmode()
        self.status.setText("输出方式：%s" % ("多轨（分轨保留）"
                                            if m == "tracks" else "混音成一条"))

    def _refresh_mixmode(self):
        if not hasattr(self, "btn_mixmode"):
            return
        if self._mix_mode() == "tracks":
            self.btn_mixmode.setText("多轨输出")
            self.btn_mixmode.setStyleSheet("color:#ffd166;")
        else:
            self.btn_mixmode.setText("混音输出")
            self.btn_mixmode.setStyleSheet("")

    def auto_cut(self, director: bool = False):
        """一键智能剪辑：剪空段 + 判转场 + 出片。队列里有多个素材时按顺序合成一条。"""
        ready = [it for it in self._queue() if it.get("ok")]
        if len(ready) > 1:
            return self._auto_cut_multi(ready)
        if not self.data or not self.data.get("cards"):
            self.status.setText("先打开一个素材或索引")
            return
        if not hasattr(self, "index_dir") or not self.index_dir:
            self.status.setText("这个索引没有目录信息，先重新索引一次")
            return
        src = self.data.get("source", "")
        if not os.path.isfile(src):
            QMessageBox.warning(self, "源文件不在了", "找不到源文件：\n%s" % src)
            return

        out_dir = resolve_out_dir("out_autocut", src)
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(out_dir, stem + "_智能剪辑.mp4")

        self._set_busy(True)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.log.clear()
        self.status.setText("智能剪辑：正在分析…")
        # 比例已废弃：0 表示“由模型逐句判断”，不按秒数硬凑
        _ratio = 0
        _tr = load_settings().get("autocut_tracks", "")
        _tracks = [int(x) - 1 for x in (p.strip() for p in _tr.split(","))
                   if x.isdigit()] if _tr else None
        self.progress.start()
        self.worker = AutoCutWorker(
            self.index_dir, out_path, ratio=_ratio,
            edit_mode=load_settings().get("edit_mode") or "gameplay")
        _skip = str(load_settings().get("skip_director", "0")) in ("1", "true", "True")
        self.worker.director_first = bool(director)
        self.worker.skip_director = _skip
        self.worker.mix_tracks = (self._mix_mode() == "mix")
        self.worker.tracks = _tracks
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(self.status.setText)
        self.worker.status.connect(lambda m: self.log.appendPlainText(m))
        self.worker.ok.connect(self._on_autocut_done)
        self.worker.err.connect(self._on_error)
        self.worker.start()

    def _auto_cut_multi(self, ready):
        src = ready[0]["path"]
        out_dir = resolve_out_dir("out_autocut", src)
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(out_dir, "%s_等%d个素材_智能剪辑.mp4"
                                % (stem, len(ready)))
        _ratio = 0
        _tr = load_settings().get("autocut_tracks", "")
        _tracks = [int(x) - 1 for x in (p.strip() for p in _tr.split(","))
                   if x.isdigit()] if _tr else None
        self._set_busy(True)
        self.log.clear()
        self.status.setText("多素材合成：%d 个素材按队列顺序剪辑…" % len(ready))
        self.progress.setValue(0)
        self.progress.start()
        self.worker = MultiCutWorker(
            [(it["out"], os.path.basename(it["path"]), it.get("tracks")) for it in ready],
            out_path, _ratio, _tracks, mix=(self._mix_mode() == "mix"),
            edit_mode=load_settings().get("edit_mode") or "gameplay")
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(self.status.setText)
        self.worker.status.connect(lambda m: self.log.appendPlainText(m))
        self.worker.ok.connect(self._on_autocut_done)
        self.worker.err.connect(self._on_error)
        self.worker.start()

    def _reveal(self, path: str):
        """在资源管理器里选中这个文件（找不到输出时一键定位）。"""
        try:
            import subprocess as _sp
            p = os.path.abspath(path)
            if os.path.isfile(p):
                _sp.Popen(["explorer", "/select,", p])
            else:
                _sp.Popen(["explorer", os.path.dirname(p)])
        except Exception:  # noqa: BLE001
            pass

    def _on_autocut_done(self, out_path: str):
        self._set_busy(False)
        self.progress.setValue(0)
        size = os.path.getsize(out_path) / 1048576 if os.path.isfile(out_path) else 0
        self.status.setText("智能剪辑完成：%s（%.1f MB）" % (os.path.basename(out_path), size))
        self.log.appendPlainText("输出：%s" % out_path)
        # 加一个「打开文件夹」：输出目录留空时默认落在
        # %LOCALAPPDATA%\素材卡片\导出\<素材名>，很多人找不到这里。
        box = QMessageBox(self)
        box.setWindowTitle("剪辑完成")
        box.setIcon(QMessageBox.Information)
        box.setText("已输出：\n%s\n\n%.1f MB" % (out_path, size))
        b_open = box.addButton("打开文件夹", QMessageBox.ActionRole)
        box.addButton("好", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is b_open:
            self._reveal(out_path)

    def export(self, mode: str):
        by_id = {c["id"]: c for c in self.cards()}
        if mode == "merge":
            cards = [by_id[cid] for cid in self.order if cid in by_id]
        else:
            cards = sorted(self.selected_cards(), key=lambda c: c["start"])
        if not cards:
            return
        src = self.data.get("source", "")
        if not os.path.isfile(src):
            QMessageBox.warning(self, "源文件不在了", f"找不到源文件：\n{src}")
            return
        out_dir = resolve_out_dir("out_merge" if mode == "merge" else "out_single", src)
        os.makedirs(out_dir, exist_ok=True)

        if mode == "single":
            target = os.path.join(out_dir, "片段")
        else:
            stem = os.path.splitext(os.path.basename(src))[0]
            target = os.path.join(out_dir, f"{stem}_成片.mp4")
            n = 1
            while os.path.exists(target):
                target = os.path.join(out_dir, f"{stem}_成片_{n}.mp4")
                n += 1

        self._set_busy(True)
        self.progress.setValue(0)
        self.status.setText("正在导出…")
        self.worker = ExportWorker(mode, src, cards, target,
                                   fast=self.fast.isChecked(),
                                   audio_stream=self.audio_used())
        self.worker.progress.connect(lambda p: self.progress.setValue(int(p * 1000)))
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.ok.connect(self._on_exported)
        self.worker.err.connect(self._on_error)
        self.worker.start()

    def _on_exported(self, files):
        self._set_busy(False)
        self.progress.setValue(1000)
        self.status.setText(f"导出完成：{len(files)} 个文件")
        self.last_files = list(files)
        if not files:
            return
        where = os.path.dirname(files[0])
        self.log.appendPlainText(f"输出目录：{where}")

        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setText(f"已导出 {len(files)} 个文件到：\n{where}")
        btn_play = box.addButton("播放看看", QMessageBox.AcceptRole)
        btn_open = box.addButton("打开文件夹", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is btn_play:
            self.preview_file(files[0])
        elif box.clickedButton() is btn_open:
            os.startfile(where)  # noqa: S606

    def _build_menu(self, root):
        """把次要功能收进菜单栏，主界面只留高频操作。"""
        mb = QMenuBar()
        mb.setNativeMenuBar(False)

        m_file = mb.addMenu("文件")
        m_file.addAction("打开素材…", self._click(self.btn_open))
        m_file.addAction("打开已有索引…", self._click(self.btn_load))
        m_file.addSeparator()
        m_file.addAction("播放源文件…", self._click(self.btn_play_file))
        m_file.addAction("打开工作目录", self._click(self.btn_workdir))
        m_file.addSeparator()
        m_file.addAction("退出", self.close)

        m_edit = mb.addMenu("编辑")
        m_edit.addAction("全选", self._click(self.btn_all))
        m_edit.addAction("全不选", self._click(self.btn_none))
        m_edit.addSeparator()
        m_edit.addAction("只看高光", self._click(self.btn_hi))
        m_edit.addAction("重算标题", self._click(self.btn_retag))

        m_tool = mb.addMenu("工具")
        m_tool.addAction("字幕 / 字幕样式…", self._open_subtitle_dialog)
        m_tool.addAction("音轨…", self._click(self.btn_tracks))
        m_tool.addSeparator()
        m_tool.addAction("字幕纠错", self._click(self.btn_proof))
        m_tool.addAction("重新索引", self._click(self.btn_reindex))
        m_tool.addSeparator()
        m_tool.addAction("训练（教它剪辑口味）…", self.open_train)
        m_tool.addAction("自动任务：分析 → 剪辑 → 出片…", self._run_autopilot)
        m_tool.addAction("自主剪辑（正式时间轴流程）…", self._open_autoedit)
        m_tool.addSeparator()
        m_tool.addAction("重录 / 手动丢弃清单…", self._open_drop_list)
        m_tool.addAction("本地模型…", self._open_models)
        m_tool.addAction("设置…", self._click(self.btn_out_dir))

        m_help = mb.addMenu("帮助")
        m_help.addAction("教程 / 快速开始", lambda: self._open_doc("README.md"))
        m_help.addAction("安装指南", lambda: self._open_doc(os.path.join("docs", "安装指南.md")))
        m_help.addAction("Premiere 插件教程",
                         lambda: self._open_doc(os.path.join("docs", "Premiere插件.md")))
        m_help.addAction("API 模型配置",
                        lambda: self._open_doc(os.path.join("docs", "API模型.md")))
        m_help.addAction("开发 / 用 AI 二次开发",
                        lambda: self._open_doc(os.path.join("docs", "开发与构建.md")))
        m_help.addAction("故障排查", lambda: self._open_doc(os.path.join("docs", "故障排查.md")))
        m_help.addSeparator()
        m_help.addAction("打开日志目录", self._open_logs)
        m_help.addSeparator()
        m_help.addAction("功能自测…", self._run_selftest)
        m_help.addSeparator()
        m_help.addAction("关于 " + PRODUCT_NAME, self._show_about)

        root.insertWidget(0, mb)

        # 「导出 ▾」点开是一个小菜单
        try:
            self.btn_single.clicked.disconnect()
        except Exception:  # noqa: BLE001
            pass
        em = QMenu(self.btn_single)
        em.addAction("每段一个文件", lambda: self.export("single"))
        em.addAction("合并成一条", lambda: self.export("merge"))
        self.btn_single.setMenu(em)

    def _open_doc(self, name):
        """用系统默认程序打开安装目录里的文档。"""
        import os as _os
        base = _os.path.dirname(_os.path.abspath(__file__))
        path = _os.path.join(base, name)
        if not _os.path.isfile(path):
            QMessageBox.information(self, "找不到", "这个文档不在安装目录里：%s" % name)
            return
        try:
            _os.startfile(path)                    # noqa: S606  Windows 专有
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", "%s\n%s" % (path, exc))

    def _open_logs(self):
        import os as _os
        d = _os.path.join(_app_home(), "logs")
        try:
            _os.makedirs(d, exist_ok=True)
            _os.startfile(d)                       # noqa: S606
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    def _run_selftest(self):
        """在后台跑功能自测，结果贴到状态栏（不阻塞界面）。"""
        import subprocess as _sp
        import sys as _sys
        base = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(base, "selftest_cut.py")
        if not os.path.isfile(script):
            QMessageBox.information(self, "找不到", "缺少 selftest_cut.py")
            return
        try:
            try:
                import winproc as _wp
                _cmd = _wp.script_cmd(script)
            except Exception:
                _cmd = ([_sys.executable, "--run-script", script]
                        if getattr(_sys, "frozen", False)
                        else [_sys.executable, "-X", "utf8", script])
            _p = _sp.Popen(_cmd,
                           cwd=base, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                           text=True, encoding="utf-8", errors="replace",
                           creationflags=0x08000000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "跑不起来", str(exc))
            return
        self.status.setText("正在跑功能自测…（完成后会在日志目录生成结果）")
        out, _ = _p.communicate()
        tail = [ln for ln in (out or "").splitlines() if ln.strip()][-2:]
        ok = (_p.returncode == 0)
        self.status.setText(("自测通过：%s" if ok else "自测有问题：%s")
                            % (tail[-1] if tail else ""))
        try:
            d = os.path.join(base, "logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "selftest_last.txt"), "w", encoding="utf-8") as fh:
                fh.write(out or "")
        except OSError:
            pass

    def _show_about(self):
        """关于：软件名、版本、路径、依赖——像正经软件那样。"""
        try:
            import os
            from PySide6.QtGui import QPixmap
            from PySide6.QtWidgets import (QDialog, QLabel, QPushButton,
                                           QVBoxLayout, QHBoxLayout)
            dlg = QDialog(self)
            dlg.setWindowTitle("关于 " + PRODUCT_NAME)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(20, 18, 20, 14)
            lay.setSpacing(8)

            top = QHBoxLayout()
            icon = os.path.join(_app_home(), "assets", "icon.png")
            if os.path.isfile(icon):
                lab = QLabel()
                lab.setPixmap(QPixmap(icon).scaled(56, 56))
                top.addWidget(lab)
            col = QVBoxLayout()
            name = QLabel(PRODUCT_NAME)
            name.setStyleSheet("font-size:16px;font-weight:600;")
            col.addWidget(name)
            sub = QLabel("版本 %s · 本地处理，可选 API 模型" % VERSION)
            sub.setStyleSheet("color:#9a9a9a;")
            col.addWidget(sub)
            top.addLayout(col)
            top.addStretch(1)
            lay.addLayout(top)

            info = QLabel("安装目录：%s\n模型：llama.cpp + Qwen3-4B/8B/14B\n"
                          "识别：faster-whisper large-v3\n"
                          "编码：NVENC / libx264" % _app_home())
            info.setStyleSheet("color:#8a8a8a;font-size:11px;")
            lay.addWidget(info)

            credits = QLabel(credits_html())
            credits.setTextFormat(Qt.RichText)
            credits.setOpenExternalLinks(True)
            credits.setWordWrap(True)
            credits.setStyleSheet("color:#9a9a9a;font-size:11px;")
            lay.addWidget(credits)

            row = QHBoxLayout()
            row.addStretch(1)
            b_dir = QPushButton("打开安装目录")
            b_dir.clicked.connect(lambda: (os.startfile(_app_home())
                                           if os.name == "nt" else None))
            b_ok = QPushButton("好")
            b_ok.setObjectName("primary")
            b_ok.clicked.connect(dlg.accept)
            row.addWidget(b_dir)
            row.addWidget(b_ok)
            lay.addLayout(row)
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, PRODUCT_NAME,
                                    "版本 %s\n%s" % (VERSION, exc))

    def _open_subtitle_dialog(self):
        """打开字幕窗口：调样式、导出 SRT/ASS、烧录。"""
        if not getattr(self, "data", None):
            QMessageBox.information(self, "先打开素材", "字幕要按台词生成，先打开素材或索引。")
            return
        try:
            SubtitleDialog(self, self.data or {}).exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "字幕窗口打不开", str(exc))

    def _open_autoedit(self):
        """自主剪辑：复用正式时间轴岗位、导演和交付门禁。"""
        try:
            from autoedit_qt import AutoEditDialog
            dlg = AutoEditDialog(self)
            # 非模态：主界面还能继续用；留引用，避免窗口被回收
            self._autoedit_dlg = dlg
            dlg.show()
            self._autoedit_dlg.raise_()
            self._autoedit_dlg.activateWindow()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    def _run_autopilot(self):
        """自动任务：选一个素材，跑完「分析 → 剪辑计划 → 转场 → 出片」。"""
        import os as _os
        from PySide6.QtWidgets import QFileDialog
        path, _f = QFileDialog.getOpenFileName(
            self, "选一个要自动剪的素材", _app_home(),
            "视频 (*.mp4 *.mkv *.mov);;所有文件 (*.*)")
        if not path:
            return
        import cut_strength
        settings = load_settings()
        keep_ratio = cut_strength.ratio_from_settings(settings)
        if hasattr(self, "keep_ratio"):
            keep_ratio = cut_strength.normalize_ratio(self.keep_ratio.value())
        lvl = cut_strength.level_for_ratio(keep_ratio)
        mode = str(load_settings().get("edit_mode", "gameplay"))
        if hasattr(self, "edit_mode"):
            mode = self.edit_mode.currentData() or mode
        self.log.appendPlainText("自动任务开始：%s（模式 %s / 保留 %.0f%%）"
                                 % (_os.path.basename(path), mode,
                                    keep_ratio * 100.0))
        self.status.setText("自动任务运行中…")

        import threading

        def _work():
            try:
                import autopilot
                res = autopilot.run_one(
                    path, level=lvl, mode=mode, use_bgm=True,
                    model_editor=True, keep_ratio=keep_ratio,
                    on_log=lambda m: self._ui_log("   " + str(m)))
                self._ui_log("自动任务完成：%s（%.1f 分钟）"
                             % (res.get("out"), res.get("minutes", 0)))
                self._ui_log("报告：%s" % res.get("report"))
                self._ui_status("自动任务完成")
            except Exception as exc:  # noqa: BLE001
                self._ui_log("自动任务失败：%s" % exc)
                self._ui_status("自动任务失败")

        threading.Thread(target=_work, daemon=True).start()

    def _ui_log(self, text):
        """从工作线程安全地往日志框写字。"""
        try:
            from PySide6.QtCore import QMetaObject, Qt, Q_ARG
            QMetaObject.invokeMethod(self, "_append_log", Qt.QueuedConnection,
                                     Q_ARG(str, str(text)))
        except Exception:  # noqa: BLE001
            pass

    def _ui_status(self, text):
        try:
            from PySide6.QtCore import QMetaObject, Qt, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection,
                                     Q_ARG(str, str(text)))
        except Exception:  # noqa: BLE001
            pass

    @Slot(str)
    def _append_log(self, text):
        self.log.appendPlainText(text)

    @Slot(str)
    def _set_status(self, text):
        self.status.setText(text)

    def _open_drop_list(self):
        """打开当前索引的"手动丢弃清单"，并把疑似重录候选写在注释里。"""
        import os as _os
        idx = getattr(self, "index_dir", None) or getattr(self, "cur_index", None)
        if not idx or not _os.path.isdir(idx):
            QMessageBox.information(self, "先打开索引", "先分析或打开一份索引，再来标重录。")
            return
        path = _os.path.join(idx, "手动丢弃.txt")
        lines = ["# 手动丢弃清单：一行一个区间，剪辑时直接剪掉",
                 "# 写法： 40-57        或   00:40-00:57",
                 "# 空行和 # 开头会被忽略",
                 ""]
        try:
            import retakes
            import autocut as _ac
            data = _ac.load_index(idx)
            pairs = retakes.find_retakes(data)
            if pairs:
                lines.append("# ↓ 机器找的疑似重录（自己判断留哪段；不要的行删掉 # 才能生效）")
                for x in pairs[:12]:
                    lines.append("# %.1f-%.1f  或  %.1f-%.1f   （%s）"
                                 % (x["a_start"], x["a_end"], x["b_start"],
                                    x["b_end"], x.get("why", "")))
                lines.append("")
        except Exception:  # noqa: BLE001
            pass
        if not _os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        try:
            _os.startfile(path)                      # noqa: S606
            self.status.setText("清单：%s（一行一个区间，例如 40-57；保存后重新剪辑即生效）"
                                % path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", "%s\n%s" % (path, exc))
    def _open_models(self):
        try:
            ModelDialog(self).exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    def _click(self, widget):
        """给菜单项一个能触发原按钮的槽。"""
        return lambda: widget.click()

    def _set_busy(self, busy: bool):
        self.btn_open.setEnabled(not busy)
        self.btn_load.setEnabled(not busy)
        self.order_list.setEnabled(not busy)
        self.fast.setEnabled(not busy)
        self.model.setEnabled(not busy)
        self.track.setEnabled(not busy and bool(self.info.get("audio_streams")))
        self.btn_reindex.setEnabled(not busy and bool(self.last_src))
        for b in (self.btn_single, self.btn_merge, self.btn_all, self.btn_none,
                  self.btn_hi, getattr(self, "btn_autocut", None),
                  getattr(self, "btn_proof", None)):
            if b is not None:
                b.setEnabled(not busy)
        if busy:
            self.btn_single.setEnabled(False)
            self.btn_merge.setEnabled(False)
        else:
            self._update_count()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.cards():
            self.refresh()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            ret = QMessageBox.question(
                self, "还在处理",
                "正在建索引或出片，现在退出会中断。\n\n"
                "选「是」会先让正在编的几段收尾（几秒），"
                "不会留下半截文件，也不会崩。")
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            # 能协作取消的就取消，等它自己收尾。
            # 以前直接 terminate()，线程池里还有 ffmpeg 在读盘，收尾阶段会撞
            # 0xc0000005（logs/fatal.log 里那次就是），整个程序消失。
            if hasattr(self.worker, "request_cancel"):
                self.worker.request_cancel()
            if not self.worker.wait(30000):
                self.worker.terminate()      # 兜底：30 秒还不走才强杀
                self.worker.wait(3000)
        event.accept()



def _app_home() -> str:
    """安装目录（打包启动器会传 SMARTCUT_HOME）。"""
    from memory_config import app_home
    return app_home()


def _set_app_identity(app):
    """图标 + 任务栏标识 + 应用名：让它看起来像一个正经软件。"""
    import os
    try:
        from PySide6.QtGui import QIcon
        home = _app_home()
        for name in ("icon.ico", "icon.png"):
            p = os.path.join(home, "assets", name)
            if os.path.isfile(p):
                app.setWindowIcon(QIcon(p))
                break
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                PRODUCT_ID)
    except Exception:
        pass
    for attr in ("setApplicationName", "setApplicationDisplayName"):
        try:
            getattr(app, attr)(PRODUCT_NAME)
        except Exception:
            pass
    try:
        app.setOrganizationName("子枫")
    except Exception:
        pass


def _splash(app):
    """启动闪屏：自己画一张图（图标在左、字在右），不再让文字压在图标上。"""
    try:
        import os
        from PySide6.QtCore import Qt, QRect
        from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
        from PySide6.QtWidgets import QSplashScreen

        W, H = 460, 176
        pix = QPixmap(W, H)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 圆角深色底 + 描边
        p.setBrush(QColor("#1f1f1f"))
        p.setPen(QColor("#3a3a3a"))
        p.drawRoundedRect(0, 0, W - 1, H - 1, 10, 10)

        # 图标（左侧，留内边距）
        icon_path = os.path.join(_app_home(), "assets", "icon.png")
        if os.path.isfile(icon_path):
            ic = QPixmap(icon_path).scaled(104, 104, Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation)
            p.drawPixmap(28, (H - ic.height()) // 2, ic)

        # 文字（右侧）
        p.setPen(QColor("#f0f0f0"))
        f1 = QFont("Microsoft YaHei UI", 15)
        f1.setBold(True)
        p.setFont(f1)
        p.drawText(QRect(152, 44, W - 170, 30), Qt.AlignLeft | Qt.AlignVCenter,
                   "智能剪辑助手")
        p.setPen(QColor("#9a9a9a"))
        f2 = QFont("Microsoft YaHei UI", 9)
        p.setFont(f2)
        p.drawText(QRect(152, 76, W - 170, 24), Qt.AlignLeft | Qt.AlignVCenter,
                   "本地运行 · 不上传任何素材")
        p.setPen(QColor("#6f9fd0"))
        p.drawText(QRect(152, 104, W - 170, 22), Qt.AlignLeft | Qt.AlignVCenter,
                   "正在加载素材与索引…")
        # 底部细进度条（装饰，表示在启动）
        p.setBrush(QColor("#4a90d9"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(28, H - 22, 150, 3, 1.5, 1.5)
        p.setBrush(QColor("#2e2e2e"))
        p.drawRoundedRect(178, H - 22, W - 206, 3, 1.5, 1.5)
        p.end()

        sp = QSplashScreen(pix, Qt.WindowStaysOnTopHint)
        sp.show()
        app.processEvents()
        return sp
    except Exception:
        return None


_MUTEX_HANDLES = []


def _activate_existing_window() -> bool:
    """尽量把已经运行的主窗口恢复并切到前台。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @enum_proc
        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value.strip().startswith("智能剪辑助手"):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if not user32.SetForegroundWindow(hwnd):
            user32.FlashWindow(hwnd, True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _spawn_guard(mutex_name="Local\\SmartCutGUIKitRunning", activate=True) -> bool:
    """保证 GUI 单实例；后台脚本在 app_main 中会先分流，不走这里。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        h = kernel32.CreateMutexW(None, False, mutex_name)
        if not h:
            return True
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        if not already:
            _MUTEX_HANDLES.append(h)   # 活到进程退出，别让句柄被回收
            return True
        kernel32.CloseHandle(h)
        if activate:
            _activate_existing_window()
        try:
            logs = os.path.join(indexer.BASE_DIR, "logs")
            os.makedirs(logs, exist_ok=True)
            with open(os.path.join(logs, "spawn_guard.log"), "a", encoding="utf-8") as fh:
                fh.write("[%s] 拦下第二个 GUI 实例 argv=%r\n"
                         % (time.strftime("%H:%M:%S"), sys.argv))
        except OSError:
            pass
        return False
    except Exception:  # noqa: BLE001
        return True


def main():
    if not _spawn_guard():
        return 0
    argv = sys.argv[1:]
    try:
        import no_console
        no_console.install()      # 子进程不弹 cmd 窗口（ffmpeg/ffprobe/llama）
    except Exception:
        pass
    app = QApplication(sys.argv)
    _set_app_identity(app)          # 图标 / 任务栏标识 / 应用名
    _splash_handle = _splash(app)   # 启动闪屏
    try:
        import ui_theme
        ui_theme.apply(app)
    except Exception as _e:
        print('主题加载失败：', _e)
    # 绿色化：先确保工作目录在程序目录下；老版本留在 AppData 的数据搬进来
    _moved = False
    try:
        _moved = indexer.migrate_legacy_workdir()
        indexer.work_root()
    except Exception:  # noqa: BLE001
        pass
    # 上次崩溃/强关留下的本地模型服务会占着端口，让这次分析"卡住"——
    # 启动先清一遍（只杀活了很久且没人连的，正在用的不动）
    try:
        import proofread as _pf
        _pf.kill_stale_servers()
    except Exception:  # noqa: BLE001
        pass
    # 默认还是旧界面（子枫喜欢它的样子）：剪辑师已换成模型剪辑（见 _open_autoedit
    # 与 ui_main_v2 的「模型剪辑」按钮）。想试独立小窗：设 ui_version=3。
    try:
        import json as _json
        _cfg_path = os.path.join(_app_home(), "conf", "settings.json")
        _ver = "2"
        if os.path.isfile(_cfg_path):
            with open(_cfg_path, encoding="utf-8") as _f:
                _ver = str(_json.load(_f).get("ui_version", "2"))
        if os.environ.get("SMARTCUT_UI"):
            _ver = os.environ["SMARTCUT_UI"]
        if _ver == "3":
            import ui_autoedit
            print("[UI] 使用自主剪辑界面 v3")
            _w3 = ui_autoedit.MainWindowAutoEdit()
            if _splash_handle is not None:
                try:
                    _splash_handle.finish(_w3)
                except Exception:
                    pass
            _w3.show()
            if argv and os.path.isfile(argv[0]):
                _w3._open_path(argv[0])
            return app.exec()
        if _ver == "2":
            import ui_main_v2
            print("[UI] 使用新版界面 v2")
            _w2 = ui_main_v2.MainWindowV2()
            if _splash_handle is not None:
                try:
                    _splash_handle.finish(_w2)
                except Exception:
                    pass
            _w2.show()
            if argv and os.path.isdir(argv[0]):
                _w2.load_index_dir(argv[0])
            return app.exec()
    except Exception as _e:  # noqa: BLE001
        print("新版界面启动失败，回退旧界面：", _e)

    win = MainWindow()
    if _moved:
        win.log.appendPlainText("已把旧工作目录搬进程序目录：%s"
                                % indexer.work_root())
        win.status.setText("工作目录已搬到程序目录下（不再写 C 盘）")
    if argv and os.path.isdir(argv[0]):
        win.load_index_dir(argv[0])
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

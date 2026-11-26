# -*- coding: utf-8 -*-
"""新版界面组件：面板（带头部条）、弹出式状态条、提示条、卡片。

设计稿：界面设计稿_v3.md（v4/v5 修订）
  · 面板 Panel：1px 描边 + 26px 头部条（标记 + 标题 + 右侧提示）
  · 状态条 StatusBar：常态 3px 细边；出错展开常驻；成功 3 秒缩回；进度浮层
  · 通知条 NoticeBar：位于标题栏下方，独立一行，把内容整体下压，不遮挡
  · 卡片 CardTile：缩略图占位（时间码+时长）+ 台词 + 徽标
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

import ui_theme

C = ui_theme.C

# ---------------------------------------------------------------- 基础样式
STYLE = """
QFrame#panel2 { background: #232323; border: 1px solid #3a3a3a; border-radius: 5px; }
QFrame#panel2Head { background: #1b1b1b; border: none; border-bottom: 1px solid #3a3a3a; }
QLabel#panel2Title { color: #e6e6e6; font-weight: 600; font-size: 12px; }
QLabel#panel2Hint { color: #7d7d7d; font-size: 11px; }
QLabel#panel2Mark { color: #4a90d9; font-size: 11px; font-weight: 700; }

QFrame#notice { background: #342222; border: none; border-top: 1px solid #6a3a3a;
                border-bottom: 1px solid #6a3a3a; }
QFrame#noticeWarn { background: #2f2a1e; border-top: 1px solid #6a5a2a;
                    border-bottom: 1px solid #6a5a2a; }
QFrame#noticeInfo { background: #1e2a36; border-top: 1px solid #2f4a66;
                    border-bottom: 1px solid #2f4a66; }
QLabel#noticeText { color: #e6e6e6; font-size: 12px; }
QLabel#noticeSub { color: #9a9a9a; font-size: 11px; }

QFrame#bar3 { background: #2b2b2b; }
QFrame#barWork { background: #202020; border-top: 1px solid #3a3a3a; }
QFrame#barExpand { background: #262626; border-top: 1px solid #3a3a3a; }
QLabel#barText { color: #c8c8c8; font-size: 11px; }
QLabel#barDim { color: #7d7d7d; font-size: 11px; }

QFrame#toast { background: #262626; border: 1px solid #4a4a4a; border-radius: 6px; }
QLabel#toastTitle { color: #e6e6e6; font-size: 12px; font-weight: 600; }
QLabel#toastBody { color: #9a9a9a; font-size: 11px; }

QFrame#tile { background: #2b2b2b; border: 1px solid #3a3a3a; border-radius: 5px; }
QFrame#tile:hover { background: #343434; border-color: #4a4a4a; }
QFrame#tile[on="true"] { border: 2px solid #4a90d9; background: #354b63; }
QLabel#tileText { color: #dcdcdc; font-size: 12px; }
QLabel#tileTime { color: #9a9a9a; font-size: 12px; }
QLabel#tileDur { color: #6e6e6e; font-size: 11px; }
QLabel#tileThumb { background: #1c1f22; border: 1px solid #2f353c; border-radius: 4px;
                   color: #b8c2cc; font-size: 12px; }
"""


def _apply_style(w: QWidget):
    w.setStyleSheet(STYLE)


# ---------------------------------------------------------------- 面板
class Panel(QFrame):
    """设计稿里的"面板"：1px 描边 + 26px 头部条 + 内容区。"""

    def __init__(self, title: str, hint: str = "", mark: str = "", parent=None):
        super().__init__(parent)
        _apply_style(self)
        self.setObjectName("panel2")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame(self)
        head.setObjectName("panel2Head")
        head.setFixedHeight(26)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 0, 10, 0)
        hl.setSpacing(6)
        if mark:
            m = QLabel(mark, head)
            m.setObjectName("panel2Mark")
            hl.addWidget(m)
        t = QLabel(title, head)
        t.setObjectName("panel2Title")
        hl.addWidget(t)
        if hint:
            h = QLabel(hint, head)
            h.setObjectName("panel2Hint")
            hl.addWidget(h)
        hl.addStretch(1)
        self.head = head
        self.head_extra = hl                 # 头部右侧可再塞控件
        root.addWidget(head)

        body = QWidget(self)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(6)
        self.body = bl
        root.addWidget(body, 1)

    def add_widget(self, w, stretch=0):
        self.body.addWidget(w, stretch)
        return w

    def add_layout(self, lay, stretch=0):
        self.body.addLayout(lay, stretch)
        return lay


# ---------------------------------------------------------------- 通知条
class NoticeBar(QWidget):
    """标题栏下方独立一行：不遮挡内容，出现时把内容下压。"""

    action = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        _apply_style(self)
        self._kind = "error"
        self._action_callback = None
        self.setVisible(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.frame = QFrame(self)
        self.frame.setObjectName("notice")
        fl = QHBoxLayout(self.frame)
        fl.setContentsMargins(0, 0, 12, 0)
        fl.setSpacing(10)
        self.stripe = QFrame(self.frame)
        self.stripe.setFixedWidth(4)
        self.stripe.setStyleSheet("background:#d05a5a;")
        fl.addWidget(self.stripe)

        col = QVBoxLayout()
        col.setContentsMargins(0, 6, 0, 6)
        col.setSpacing(2)
        self.title = QLabel("", self.frame)
        self.title.setObjectName("noticeText")
        self.sub = QLabel("", self.frame)
        self.sub.setObjectName("noticeSub")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        fl.addLayout(col, 1)

        self.btn_main = QPushButton("", self.frame)
        self.btn_main.setObjectName("primary")
        self.btn_main.setFixedHeight(26)
        self.btn_main.clicked.connect(lambda: self.action.emit("main"))
        self.btn_sub = QPushButton("", self.frame)
        self.btn_sub.setFixedHeight(26)
        self.btn_sub.clicked.connect(lambda: self.action.emit("sub"))
        self.btn_close = QPushButton("忽略", self.frame)
        self.btn_close.setObjectName("ghost")
        self.btn_close.setFixedHeight(26)
        self.btn_close.clicked.connect(self.hide_bar)
        for b in (self.btn_main, self.btn_sub, self.btn_close):
            b.setVisible(False)
            fl.addWidget(b)
        lay.addWidget(self.frame)

    def show_bar(self, title, sub="", kind="error", main="", sub_btn="", on_action=None):
        self._kind = kind
        obj = {"error": "notice", "warn": "noticeWarn", "info": "noticeInfo"}[kind]
        color = {"error": "#d05a5a", "warn": "#d9a441", "info": "#4a90d9"}[kind]
        self.frame.setObjectName(obj)
        self.frame.setStyleSheet("")
        self.stripe.setStyleSheet("background:%s;" % color)
        self.frame.style().unpolish(self.frame)
        self.frame.style().polish(self.frame)
        self.title.setText(title)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.btn_main.setText(main)
        self.btn_main.setVisible(bool(main))
        self.btn_sub.setText(sub_btn)
        self.btn_sub.setVisible(bool(sub_btn))
        self.btn_close.setVisible(kind != "info")
        if self._action_callback is not None:
            try:
                self.action.disconnect(self._action_callback)
            except (RuntimeError, TypeError):
                pass
            self._action_callback = None
        if on_action:
            self.action.connect(on_action)
            self._action_callback = on_action
        self.setVisible(True)

    def hide_bar(self):
        self.setVisible(False)


# ---------------------------------------------------------------- 状态条
class StatusBar(QWidget):
    """弹出式状态条：常态 3px 细边；出错展开；成功 N 秒缩回；进度浮层。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        _apply_style(self)
        self._expanded = False
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self.collapse)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 浮层（进度/成功）靠右下，单独浮在内容上
        # 这个容器必须是 StatusBar 的子控件。StatusBar 通常先以 parent=None
        # 创建，再被布局接管；沿用构造参数会生成一个独立顶级窗口。Windows
        # 会把这个空容器显示成只有应用图标和标题栏的小窗，进度刷新时反复闪现。
        self.toast_holder = QWidget(self)
        _apply_style(self.toast_holder)
        self.toast_holder.setVisible(False)
        tl = QVBoxLayout(self.toast_holder)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        self.toast_holder.setFixedWidth(324)

        # 展开条
        self.bar = QFrame(self)
        self.bar.setObjectName("barExpand")
        self.bar.setVisible(False)
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(12, 5, 12, 5)
        bl.setSpacing(10)
        self.lbl = QLabel("", self.bar)
        self.lbl.setObjectName("barText")
        bl.addWidget(self.lbl)
        bl.addStretch(1)
        self.detail = QLabel("", self.bar)
        self.detail.setObjectName("barDim")
        bl.addWidget(self.detail)
        lay.addWidget(self.bar)

        # 工作条（干活时展开成 64px，比细边显眼）
        self.work = QFrame(self)
        self.work.setObjectName("barWork")
        self.work.setVisible(False)
        wl = QVBoxLayout(self.work)
        wl.setContentsMargins(14, 8, 14, 8)
        wl.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.work_title = QLabel("", self.work)
        self.work_title.setStyleSheet("font-size:13px;font-weight:600;color:#e6e6e6;")
        top.addWidget(self.work_title)
        self.work_stage = QLabel("", self.work)
        self.work_stage.setStyleSheet("color:#9a9a9a;font-size:11px;")
        top.addWidget(self.work_stage)
        top.addStretch(1)
        self.work_pct = QLabel("", self.work)
        self.work_pct.setStyleSheet("font-size:16px;font-weight:700;color:#4a90d9;")
        top.addWidget(self.work_pct)
        self.work_eta = QLabel("", self.work)
        self.work_eta.setStyleSheet("color:#9a9a9a;font-size:11px;")
        top.addWidget(self.work_eta)
        self.work_cancel = QPushButton("取消", self.work)
        self.work_cancel.setObjectName("ghost")
        self.work_cancel.setFixedHeight(24)
        self.work_cancel.clicked.connect(self._on_work_cancel)
        top.addWidget(self.work_cancel)
        wl.addLayout(top)
        self.work_bar = QProgressBar(self.work)
        self.work_bar.setFixedHeight(6)
        self.work_bar.setRange(0, 1000)
        self.work_bar.setTextVisible(False)
        self.work_bar.setStyleSheet(
            "QProgressBar{background:#1a1a1a;border:none;border-radius:3px;}"
            "QProgressBar::chunk{background:#4a90d9;border-radius:3px;}")
        wl.addWidget(self.work_bar)
        lay.addWidget(self.work)

        # 3px 细边 + 进度
        self.hair = QFrame(self)
        self.hair.setFixedHeight(3)
        self.hair.setStyleSheet("background:#2b2b2b;")
        lay.addWidget(self.hair)
        self._progress = 0.0
        self._show_progress = False

    # ---- 工作模式（大条）
    def working(self, show: bool = True, title: str = "", stage: str = "",
                pct=None, eta: str = "", on_cancel=None):
        """干活时展开成 64px 大条：一眼看到在做什么、到哪了、还要多久。"""
        self._work_cancel_cb = on_cancel
        if not show:
            self.work.setVisible(False)
            self.hair.setVisible(True)
            self._show_progress = False
            self.hair.update()
            return
        self.work.setVisible(True)
        self.hair.setVisible(False)
        self.bar.setVisible(False)
        if title:
            self.work_title.setText(title)
        if stage:
            self.work_stage.setText(stage)
        if eta:
            self.work_eta.setText(eta)
        if pct is not None:
            p = max(0.0, min(1.0, float(pct)))
            self.work_bar.setValue(int(p * 1000))
            self.work_pct.setText("%d%%" % int(p * 100))
        self.work_cancel.setVisible(bool(on_cancel))

    def prop_work(self, pct=None, stage: str = "", eta: str = ""):
        """只更新进度/阶段，不重设标题。"""
        if stage:
            self.work_stage.setText(stage)
        if eta:
            self.work_eta.setText(eta)
        if pct is not None:
            p = max(0.0, min(1.0, float(pct)))
            self.work_bar.setValue(int(p * 1000))
            self.work_pct.setText("%d%%" % int(p * 100))

    def _on_work_cancel(self):
        cb = getattr(self, "_work_cancel_cb", None)
        if cb:
            try:
                cb()
            except Exception:
                pass

    # ---- 细边上的进度
    def set_progress(self, pct: float, text: str = ""):
        self._show_progress = pct >= 0
        self._progress = max(0.0, min(1.0, float(pct)))
        if text:
            self.lbl.setText(text)
        self.hair.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        # 在细边上画进度（父控件坐标）
        if self._show_progress:
            from PySide6.QtGui import QPainter, QColor
            p = QPainter(self)
            p.fillRect(0, self.height() - 3, self.width(), 3, QColor("#2b2b2b"))
            p.fillRect(0, self.height() - 3,
                       int(self.width() * self._progress), 3, QColor("#4a90d9"))
            p.end()

    # ---- 展开/收起
    def expand(self, text: str = "", detail: str = "", auto_hide_s: float = 0):
        self.lbl.setText(text)
        self.detail.setText(detail)
        self.bar.setVisible(True)
        self._expanded = True
        self._auto_hide.stop()
        if auto_hide_s and auto_hide_s > 0:
            self._auto_hide.start(int(auto_hide_s * 1000))

    def collapse(self):
        self.bar.setVisible(False)
        self._expanded = False
        self._auto_hide.stop()

    # ---- 语义化快捷方式
    def info(self, text: str, detail: str = "", seconds: float = 4):
        self.expand(text, detail, auto_hide_s=seconds)

    def ok(self, text: str, detail: str = "", seconds: float = 3):
        self.bar.setStyleSheet("QFrame#barExpand{background:#1d2f24;"
                               "border-top:1px solid #2f5a3d;}")
        self.expand(text, detail, auto_hide_s=seconds)

    def error(self, text: str, detail: str = ""):
        self.bar.setStyleSheet("QFrame#barExpand{background:#342222;"
                               "border-top:1px solid #6a3a3a;}")
        self.expand(text, detail, auto_hide_s=0)


# ---------------------------------------------------------------- 卡片
class CardTile(QFrame):
    """卡片：缩略图占位（时间码 + 时长）+ 台词 + 徽标。"""

    clicked = Signal(str)
    doubleClicked = Signal(str)

    def __init__(self, card: dict, parent=None):
        super().__init__(parent)
        _apply_style(self)
        self.card = card
        self.cid = str(card.get("id") or card.get("start"))
        self.setObjectName("tile")
        self.setFixedSize(254, 176)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("on", False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        thumb = QLabel(self)
        thumb.setObjectName("tileThumb")
        thumb.setFixedHeight(96)
        thumb.setAlignment(Qt.AlignCenter)
        start = float(card.get("start") or 0)
        end = float(card.get("end") or 0)
        thumb.setText("%s\n%.1f 秒" % (_fmt(start), max(0.0, end - start)))
        self.thumb = thumb
        lay.addWidget(thumb)

        text = QLabel(self)
        text.setObjectName("tileText")
        text.setWordWrap(False)
        body = str(card.get("text") or card.get("title") or "").strip()
        text.setText(body[:26] + ("…" if len(body) > 26 else "") or "（无台词）")
        lay.addWidget(text)

        row = QHBoxLayout()
        row.setSpacing(4)
        dur = QLabel("%.1fs" % max(0.0, end - start), self)
        dur.setObjectName("tileDur")
        row.addWidget(dur)
        row.addStretch(1)
        for cat in (card.get("cats") or [])[:2]:
            chip = QLabel(str(cat), self)
            chip.setStyleSheet("background:#2d3026; color:#c9d8b0; border-radius:3px;"
                               "padding:1px 5px; font-size:10px;")
            row.addWidget(chip)
        lay.addLayout(row)

    def mousePressEvent(self, ev):
        self.clicked.emit(self.cid)
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        self.doubleClicked.emit(self.cid)
        super().mouseDoubleClickEvent(ev)

    def set_thumb(self, path: str):
        """把抓好的帧贴到卡面上（失败就算了，保留时间码）。"""
        try:
            from PySide6.QtGui import QPixmap
            import os
            if not os.path.isfile(path):
                return
            pix = QPixmap(path)
            if pix.isNull():
                return
            pix = pix.scaled(self.thumb.width(), self.thumb.height(),
                             Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self.thumb.setPixmap(pix)
            self.thumb.setText("")
            self.thumb.setStyleSheet("border:1px solid #2f353c;border-radius:4px;")
        except Exception:
            pass

    def set_selected(self, on: bool):
        self.setProperty("on", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)


def _fmt(t: float) -> str:
    t = max(0.0, float(t))
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))

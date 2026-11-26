# -*- coding: utf-8 -*-
"""PR 风格主题：中性灰、面板头部条、紧凑控件、PR 蓝强调色。

借鉴 Pr 的界面语言（不是照抄布局）：
  · 不用纯黑：背景 #1d1d1d，面板 #252525，卡片 #2b2b2b，描边 #3a3a3a
  · 每个区域是一个"面板"，顶部有头部条（标记 + 名字 + 右侧提示），像 PR 的源/节目/时间轴
  · 控件紧凑：12px 字、28px 行高、4px 圆角、扁平按钮（hover 才亮）
  · 强调色 PR 蓝 #4a90d9；选中项用 PR 那种深蓝底
"""
from __future__ import annotations

C = {
    "bg": "#1d1d1d",
    "panel": "#252525",
    "panel2": "#1a1a1a",
    "card": "#2b2b2b",
    "card_hover": "#343434",
    "border": "#3a3a3a",
    "border_hi": "#4a4a4a",
    "text": "#d9d9d9",
    "text2": "#a8a8a8",
    "text3": "#7d7d7d",
    "accent": "#4a90d9",
    "accent_hover": "#5aa0e8",
    "accent_dim": "#2f608f",
    "ok": "#4caf72",
    "warn": "#d9a441",
    "danger": "#d05a5a",
    "sel": "#354b63",
}

FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif'


def build_qss() -> str:
    c = C
    return f"""
* {{ font-family: {FONT}; font-size: 12px; color: {c['text']}; }}
QWidget {{ background: transparent; }}
QMainWindow, QDialog, QWidget#root {{ background: {c['bg']}; }}

QFrame#panel {{
    background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 4px;
}}
QFrame#panelHead {{
    background: {c['panel2']}; border: none; border-bottom: 1px solid {c['border']};
}}
QLabel#panelTitle {{ color: {c['text']}; font-weight: 600; font-size: 12px; }}
QLabel#panelHint {{ color: {c['text3']}; font-size: 11px; }}
QLabel#panelMark {{ color: {c['accent']}; font-size: 11px; font-weight: 700; }}

QLabel#sectionTitle {{ color: {c['text']}; font-size: 12px; font-weight: 600; }}
QLabel#sectionHint {{ color: {c['text3']}; font-size: 11px; }}
QFrame#divider {{ background: {c['border']}; max-height: 1px; border: none; }}
QFrame#vdivider {{ background: {c['border']}; max-width: 1px; border: none; }}

QPushButton {{
    background: {c['card']}; border: 1px solid {c['border']}; border-radius: 4px;
    padding: 4px 12px; min-height: 18px; color: {c['text']};
}}
QPushButton:hover {{ background: {c['card_hover']}; border-color: {c['border_hi']}; }}
QPushButton:pressed {{ background: {c['sel']}; }}
QPushButton:disabled {{ color: {c['text3']}; background: {c['panel']}; border-color: {c['border']}; }}
QPushButton#primary {{
    background: {c['accent']}; border: 1px solid {c['accent']}; color: #ffffff; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton#primary:pressed {{ background: {c['accent_dim']}; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {c['text2']}; }}
QPushButton#ghost:hover {{ background: {c['card_hover']}; color: {c['text']}; }}
QPushButton#tool {{
    background: transparent; border: 1px solid transparent; color: {c['text2']}; padding: 4px 8px;
}}
QPushButton#tool:hover {{ background: {c['card']}; color: {c['text']}; border-color: {c['border']}; }}
QPushButton#danger {{ background: transparent; border: 1px solid {c['danger']}; color: {c['danger']}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background: {c['panel2']}; border: 1px solid {c['border']};
    border-radius: 4px; padding: 4px 8px; selection-background-color: {c['accent']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {c['accent']}; }}

QComboBox {{
    background: {c['panel2']}; border: 1px solid {c['border']};
    border-radius: 4px; padding: 3px 8px; min-height: 18px;
}}
QComboBox:hover {{ border-color: {c['border_hi']}; }}
QComboBox:focus {{ border-color: {c['accent']}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {c['card']}; border: 1px solid {c['border']};
    padding: 2px; selection-background-color: {c['sel']}; outline: none;
}}

QCheckBox, QRadioButton {{ spacing: 6px; color: {c['text2']}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid {c['border_hi']}; background: {c['panel2']};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {c['accent']}; border-color: {c['accent']};
}}

QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 4px; }}
QFrame#card:hover {{ background: {c['card_hover']}; border-color: {c['border_hi']}; }}
QFrame#card[on="true"] {{ border: 2px solid {c['accent']}; background: {c['sel']}; }}

QListWidget, QTreeWidget, QTableWidget {{
    background: {c['panel2']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 2px;
}}
QListWidget::item {{ padding: 3px 6px; }}
QListWidget::item:selected {{ background: {c['sel']}; color: {c['text']}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: {c['panel2']}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #4a4a4a; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #5c5c5c; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: {c['panel2']}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #4a4a4a; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: #5c5c5c; }}

QMenuBar {{ background: {c['bg']}; border-bottom: 1px solid {c['border']}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {c['card']}; }}
QMenu {{ background: {c['card']}; border: 1px solid {c['border']}; padding: 3px; }}
QMenu::item {{ padding: 5px 20px 5px 12px; }}
QMenu::item:selected {{ background: {c['accent']}; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}

QLabel#muted, QLabel#hint {{ color: {c['text3']}; font-size: 11px; }}
QLabel#status {{ color: {c['text2']}; }}
QToolTip {{
    background: {c['card']}; color: {c['text']}; border: 1px solid {c['border_hi']};
    padding: 4px 6px;
}}
QProgressBar {{
    background: {c['panel2']}; border: 1px solid {c['border']};
    border-radius: 3px; text-align: center; color: {c['text2']};
}}
QProgressBar::chunk {{ background: {c['accent']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; top: -1px; }}
QTabBar::tab {{
    background: {c['panel2']}; color: {c['text3']};
    padding: 5px 14px; border: 1px solid {c['border']}; border-bottom: none;
}}
QTabBar::tab:selected {{
    background: {c['panel']}; color: {c['text']}; border-top: 2px solid {c['accent']};
}}
QGroupBox {{ border: 1px solid {c['border']}; margin-top: 12px; padding: 8px; }}
"""


def apply(app=None):
    from PySide6.QtWidgets import QApplication
    app = app or QApplication.instance()
    if app is None:
        return False
    qss = build_qss() + _BAND_CSS + _RADIO_CSS
    app.setStyleSheet(qss)
    return True


def panel(title, hint="", mark="", parent=None):
    """PR 式面板：头部条（标记 + 标题 + 右侧提示）+ 内容区，返回 (面板, 内容布局)。"""
    from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget
    frame = QFrame(parent)
    frame.setObjectName("panel")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    head = QFrame(frame)
    head.setObjectName("panelHead")
    head.setFixedHeight(26)
    hl = QHBoxLayout(head)
    hl.setContentsMargins(8, 0, 8, 0)
    hl.setSpacing(6)
    if mark:
        m = QLabel(mark, head)
        m.setObjectName("panelMark")
        hl.addWidget(m)
    t = QLabel(title, head)
    t.setObjectName("panelTitle")
    hl.addWidget(t)
    if hint:
        h = QLabel(hint, head)
        h.setObjectName("panelHint")
        hl.addWidget(h)
    hl.addStretch(1)
    outer.addWidget(head)
    body = QWidget(frame)
    lay = QVBoxLayout(body)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(6)
    outer.addWidget(body, 1)
    return frame, lay


def section(title, hint="", parent=None):
    """对话框里的分区标题（轻量，无边框）。"""
    from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget
    box = QWidget(parent)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 10, 0, 4)
    lay.setSpacing(4)
    row = QHBoxLayout()
    row.setSpacing(6)
    bar = QFrame(box)
    bar.setFixedSize(3, 12)
    bar.setStyleSheet("background:%s;" % C["accent"])
    row.addWidget(bar)
    t = QLabel(title, box)
    t.setObjectName("sectionTitle")
    row.addWidget(t)
    if hint:
        h = QLabel(hint, box)
        h.setObjectName("sectionHint")
        row.addWidget(h)
    row.addStretch(1)
    lay.addLayout(row)
    line = QFrame(box)
    line.setObjectName("divider")
    line.setFixedHeight(1)
    lay.addWidget(line)
    return box


def divider(parent=None, vertical=False):
    from PySide6.QtWidgets import QFrame
    f = QFrame(parent)
    f.setObjectName("vdivider" if vertical else "divider")
    if vertical:
        f.setFixedWidth(1)
    else:
        f.setFixedHeight(1)
    return f


def align_form(grid, label_width=88):
    from PySide6.QtWidgets import QLabel
    from PySide6.QtCore import Qt
    for i in range(grid.count()):
        it = grid.itemAt(i)
        w = it.widget() if it else None
        if isinstance(w, QLabel):
            w.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            w.setMinimumWidth(label_width)
    return grid


_BAND_CSS = ("""
QFrame#band { background: #232323; border: 1px solid #444444; border-radius: 5px; }
QFrame#bandHead {
    background: #1b1b1b; border: none; border-bottom: 1px solid #444444;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QLabel#bandTitle { color: #e6e6e6; font-weight: 600; font-size: 12px; }
QLabel#bandHint { color: #8a8a8a; font-size: 11px; }
QLabel#bandMark { color: #4a90d9; font-size: 12px; font-weight: 700; }
""")
_BAND_CSS = """
QFrame#band { background: #232323; border: 1px solid #444444; border-radius: 5px; }
QFrame#bandHead { background: #1b1b1b; border: none; border-bottom: 1px solid #444444;
                  border-top-left-radius: 4px; border-top-right-radius: 4px; }
QLabel#bandTitle { color: #e6e6e6; font-weight: 600; font-size: 12px; }
QLabel#bandHint { color: #8a8a8a; font-size: 11px; }
QLabel#bandMark { color: #4a90d9; font-size: 12px; font-weight: 700; }
"""


def band(title, hint="", mark="", parent=None):
    """带 26px 头部条的面板；内容区支持 addLayout（不用重挂控件）。"""
    from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget
    frame = QFrame(parent)
    frame.setObjectName("band")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    head = QFrame(frame)
    head.setObjectName("bandHead")
    head.setFixedHeight(26)
    hl = QHBoxLayout(head)
    hl.setContentsMargins(10, 0, 10, 0)
    hl.setSpacing(6)
    if mark:
        m = QLabel(mark, head)
        m.setObjectName("bandMark")
        hl.addWidget(m)
    t = QLabel(title, head)
    t.setObjectName("bandTitle")
    hl.addWidget(t)
    if hint:
        h = QLabel(hint, head)
        h.setObjectName("bandHint")
        hl.addWidget(h)
    hl.addStretch(1)
    outer.addWidget(head)

    body = QWidget(frame)
    bl = QVBoxLayout(body)
    bl.setContentsMargins(10, 8, 10, 8)
    bl.setSpacing(6)
    outer.addWidget(body, 1)
    return frame, bl

_RADIO_CSS = """
QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px;
                          border: 1px solid #4a4a4a; background: #1a1a1a; }
QRadioButton::indicator:checked { background: #4a90d9; border-color: #4a90d9; }
"""

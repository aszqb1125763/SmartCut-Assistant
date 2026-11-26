# -*- coding: utf-8 -*-
"""新版对话框：音轨选择 + 给旧对话框套新外壳。"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

import ui_theme
from product_info import PRODUCT_NAME
from ui_widgets import Panel


def _home() -> str:
    return os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))


def _conf_path() -> str:
    return os.path.join(_home(), "conf", "autocut.json")


def load_render_conf() -> dict:
    try:
        import autocut
        return autocut.load_conf()
    except Exception:
        try:
            with open(_conf_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"render": {}}


def save_tracks(tracks) -> bool:
    """把选中的音轨序号写进 conf/autocut.json（render.tracks）。"""
    try:
        import autocut
        conf = autocut.load_conf()
        conf.setdefault("render", {})["tracks"] = list(tracks) or None
        p = _conf_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


class AudioDialog(QDialog):
    """音轨：每条带电平，勾选要保留的（混音输出）。"""

    def __init__(self, parent=None, src: str = ""):
        super().__init__(parent)
        self.setWindowTitle("音轨 · " + PRODUCT_NAME)
        self.resize(640, 460)
        self.src = src
        self.levels = []
        self._build()
        if src:
            self._probe()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget()
        head.setFixedHeight(40)
        head.setStyleSheet("background:#181818;border-bottom:1px solid #3a3a3a;")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 0, 12, 0)
        t = QLabel("音轨")
        t.setStyleSheet("font-size:14px;font-weight:600;")
        hl.addWidget(t)
        hl.addStretch(1)
        b = QPushButton("✕")
        b.setObjectName("ghost")
        b.setFixedHeight(26)
        b.clicked.connect(self.reject)
        hl.addWidget(b)
        root.addWidget(head)

        body = QVBoxLayout()
        body.setContentsMargins(16, 12, 16, 8)
        body.setSpacing(8)
        self.hint = QLabel("勾选要保留的音轨；多条会混成一条（推荐）")
        self.hint.setStyleSheet("color:#7d7d7d;font-size:11px;")
        body.addWidget(self.hint)

        self.panel = Panel("音轨列表", "电平越大越可能是说话/游戏声", "")
        self.rows = QWidget()
        self.rows_lay = QVBoxLayout(self.rows)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(6)
        self.panel.add_widget(self.rows)
        body.addWidget(self.panel, 1)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#9a9a9a;font-size:11px;")
        body.addWidget(self.lbl_status)
        root.addLayout(body, 1)

        foot = QWidget()
        foot.setFixedHeight(48)
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 8, 16, 8)
        fl.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setFixedHeight(30)
        b_cancel.clicked.connect(self.reject)
        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.setFixedHeight(30)
        b_save.clicked.connect(self._save)
        fl.addWidget(b_cancel)
        fl.addWidget(b_save)
        root.addWidget(foot)

    def _probe(self):
        try:
            import autocut
            self.levels = autocut.probe_track_levels(self.src)
        except Exception as exc:  # noqa: BLE001
            self.lbl_status.setText("读音轨失败：%s" % exc)
            return
        try:
            cur = (load_render_conf().get("render") or {}).get("tracks")
        except Exception:
            cur = None
        keep = set(cur) if cur else None
        while self.rows_lay.count():
            it = self.rows_lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self.boxes = {}
        for lv in self.levels:
            ordv = int(lv.get("ordinal") or 0)
            db = float(lv.get("db") or -180)
            silent = bool(lv.get("silent"))
            cb = QCheckBox("%s   %s   %.0f dB"
                           % (lv.get("label") or ("音轨 %d" % (ordv + 1)),
                              "（几乎没声音）" if silent else "", db))
            cb.setChecked(True if keep is None else ordv in keep)
            self.boxes[ordv] = cb
            self.rows_lay.addWidget(cb)
        self.rows_lay.addStretch(1)
        self.lbl_status.setText("共 %d 条音轨" % len(self.levels))

    def _save(self):
        picks = [k for k, cb in getattr(self, "boxes", {}).items() if cb.isChecked()]
        if not picks:
            self.lbl_status.setText("至少要留一条音轨")
            return
        if save_tracks(picks):
            self.accept()
        else:
            self.lbl_status.setText("保存失败：写入 conf/autocut.json 出错")


def apply_shell(dlg: QDialog, title: str, hint: str = "", size=(860, 620)):
    """给旧对话框套一层新外壳：标题、说明、尺寸。"""
    try:
        dlg.setWindowTitle("%s · %s" % (title, PRODUCT_NAME))
        dlg.resize(*size)
        lay = dlg.layout()
        if lay is None:
            return dlg
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 10, 14, 4)
        bl.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("font-size:14px;font-weight:600;")
        bl.addWidget(t)
        if hint:
            h = QLabel(hint)
            h.setStyleSheet("color:#7d7d7d;font-size:11px;")
            bl.addWidget(h)
        lay.insertWidget(0, box)
    except Exception:
        pass
    return dlg

# -*- coding: utf-8 -*-
"""Compact, embedded role status board backed by TaskMemory."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)


ROLE_ORDER = ("analyst", "writer", "editor", "boundary", "director", "renderer")
ROLE_LABELS = {
    "analyst": "分析员",
    "writer": "编剧",
    "editor": "剪辑师",
    "boundary": "边界师",
    "director": "导演",
    "renderer": "渲染员",
}
STATUS_LABELS = {
    "pending": "等待",
    "ready": "就绪",
    "running": "工作中",
    "blocked": "受阻",
    "waiting": "等待",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "取消",
    "interrupted": "中断",
    "paused": "暂停",
}
STATUS_COLORS = {
    "pending": "#777777",
    "ready": "#8fb8dc",
    "running": "#4a90d9",
    "blocked": "#d99a4a",
    "waiting": "#9a9a9a",
    "completed": "#4caf72",
    "failed": "#d05a5a",
    "cancelled": "#a0a0a0",
    "interrupted": "#d99a4a",
    "paused": "#d99a4a",
}
STAGE_LABELS = {
    "analyze": "素材分析",
    "writer": "编写剧本",
    "editor": "初剪",
    "boundary": "音画边界",
    "director": "导演审片",
    "transition": "转场与配乐",
    "renderer": "渲染",
    "finalize": "成片确认",
}


def role_view(role: str, state: dict | None) -> dict:
    """Map a TaskMemory role row to stable display values."""
    state = state or {}
    status = str(state.get("status") or "pending").strip().lower()
    try:
        progress = max(0.0, min(1.0, float(state.get("progress") or 0.0)))
    except (TypeError, ValueError):
        progress = 0.0
    artifact = str(state.get("output_artifact_path") or
                   state.get("output_artifact_id") or "").strip()
    error = str(state.get("error_text") or state.get("blocked_by") or "").strip()
    unit = str(state.get("current_unit") or "").strip()
    detail = error or artifact or unit
    return {
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status or "未知"),
        "color": STATUS_COLORS.get(status, "#9a9a9a"),
        "progress": progress,
        "progress_text": "%d%%" % int(progress * 100 + 0.5),
        "artifact": artifact,
        "error": error,
        "detail": detail,
    }


def learning_view(path: str | None) -> dict:
    """Read the compact director debrief embedded in a review artifact."""
    result = {"visible": False, "text": "", "tooltip": "", "lessons": 0}
    path = str(path or "").strip()
    if not path or not os.path.isfile(path) or not path.lower().endswith(".json"):
        return result
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return result
    director = ((payload.get("evidence") or {}).get("director") or
                payload.get("director") or {})
    debrief = director.get("debrief") or {}
    if not debrief:
        return result
    lessons = list(debrief.get("lessons") or [])
    candidate_count = sum(row.get("result") == "candidate" for row in lessons)
    task_only_count = sum(row.get("result") == "task_only" for row in lessons)
    discovered = int(debrief.get("issues_discovered") or 0)
    resolved = int(debrief.get("issues_resolved") or 0)
    remaining = int(debrief.get("remaining_serious") or 0)
    text = "导演复盘  发现 %d · 解决 %d · 剩余 %d · 候选经验 %d" % (
        discovered, resolved, remaining, candidate_count)
    details = [str(row.get("rule") or "") for row in lessons if row.get("rule")]
    if task_only_count:
        details.append("%d 条仅用于当前任务" % task_only_count)
    return {"visible": True, "text": text, "tooltip": "\n".join(details),
            "lessons": len(lessons)}


class _RoleRow(QFrame):
    def __init__(self, role: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.setFixedHeight(24)
        self.setStyleSheet("QFrame{background:transparent;border:none;}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(5)

        self.dot = QLabel("●", self)
        self.dot.setFixedWidth(9)
        layout.addWidget(self.dot)
        self.name = QLabel(ROLE_LABELS.get(role, role), self)
        self.name.setFixedWidth(42)
        self.name.setStyleSheet("color:#c8c8c8;font-size:11px;")
        layout.addWidget(self.name)
        self.status = QLabel("等待", self)
        self.status.setFixedWidth(42)
        self.status.setStyleSheet("color:#777777;font-size:10px;")
        layout.addWidget(self.status)
        self.progress = QLabel("0%", self)
        self.progress.setFixedWidth(30)
        self.progress.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.progress.setStyleSheet("color:#858585;font-size:10px;")
        layout.addWidget(self.progress)
        self.detail = QLabel("", self)
        self.detail.setStyleSheet("color:#777777;font-size:10px;")
        self.detail.setMinimumWidth(0)
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.detail, 1)

    def apply_state(self, state: dict | None):
        view = role_view(self.role, state)
        self.dot.setStyleSheet("color:%s;font-size:9px;" % view["color"])
        self.status.setText(view["status_label"])
        self.status.setStyleSheet("color:%s;font-size:10px;" % view["color"])
        self.progress.setText(view["progress_text"])
        detail = view["detail"]
        if view["error"]:
            detail = "错误：" + view["error"]
            self.detail.setStyleSheet("color:#d06a6a;font-size:10px;")
        elif view["artifact"]:
            detail = "产物：" + view["artifact"]
            self.detail.setStyleSheet("color:#777777;font-size:10px;")
        else:
            self.detail.setStyleSheet("color:#777777;font-size:10px;")
        self.detail.setText(detail)
        self.setToolTip(detail)


class RoleStatusBoard(QFrame):
    """Embedded status board. It never derives state from UI button state."""

    def __init__(self, parent=None, *, store_factory=None, config_loader=None,
                 poll_ms: int = 1000, auto_start: bool = True):
        super().__init__(parent)
        self.setObjectName("roleStatusBoard")
        self.setStyleSheet(
            "QFrame#roleStatusBoard{background:#202020;border:1px solid #383838;"
            "border-radius:4px;}"
        )
        self._store_factory = store_factory
        self._config_loader = config_loader
        self._store = None
        self._bound_task_id = None
        self._auto_select = True
        self._enabled = None
        self._config_key = None
        self._shutdown = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget(self)
        header.setFixedHeight(28)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(7, 0, 7, 0)
        header_layout.setSpacing(5)
        self.toggle = QToolButton(header)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setText("岗位状态")
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setAutoRaise(True)
        self.toggle.setStyleSheet("color:#d0d0d0;font-size:11px;font-weight:600;")
        self.toggle.clicked.connect(self._toggle_expanded)
        header_layout.addWidget(self.toggle)
        self.summary = QLabel("正在读取任务记忆…", header)
        self.summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.summary.setStyleSheet("color:#777777;font-size:10px;")
        header_layout.addWidget(self.summary, 1)
        root.addWidget(header)

        self.detail_panel = QWidget(self)
        body = QVBoxLayout(self.detail_panel)
        body.setContentsMargins(5, 2, 5, 5)
        body.setSpacing(0)
        self.task_line = QLabel("", self.detail_panel)
        self.task_line.setStyleSheet("color:#6f6f6f;font-size:10px;padding:1px 4px 3px 4px;")
        body.addWidget(self.task_line)
        self.rows = {}
        for role in ROLE_ORDER:
            row = _RoleRow(role, self.detail_panel)
            self.rows[role] = row
            body.addWidget(row)
        self.learning_line = QLabel("", self.detail_panel)
        self.learning_line.setStyleSheet(
            "color:#8fb8dc;font-size:10px;padding:4px 4px 1px 4px;")
        self.learning_line.setWordWrap(True)
        self.learning_line.setVisible(False)
        body.addWidget(self.learning_line)
        root.addWidget(self.detail_panel)
        self.detail_panel.setVisible(False)

        self.timer = QTimer(self)
        self.timer.setInterval(max(100, int(poll_ms)))
        self.timer.timeout.connect(self.refresh)
        if auto_start:
            self.timer.start()
        self.refresh()

    @property
    def task_id(self):
        return self._bound_task_id

    def _load_config(self):
        if self._config_loader is not None:
            return dict(self._config_loader() or {})
        import memory_config
        return memory_config.load_config()

    def _open_store(self, config):
        if self._store_factory is not None:
            return self._store_factory(config=config)
        import task_memory
        return task_memory.open_store(config=config)

    def _close_store(self):
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    def _ensure_store(self) -> bool:
        config = self._load_config()
        enabled = bool(config.get("enabled", True))
        config_key = (enabled, str(config.get("root") or "").strip())
        if self._config_key is not None and config_key != self._config_key:
            self._close_store()
        self._config_key = config_key
        self._enabled = enabled
        if not enabled:
            self._close_store()
            return False
        if self._store is None:
            self._store = self._open_store(config)
        return True

    def _toggle_expanded(self):
        self.set_expanded(not self.detail_panel.isVisible())

    def set_expanded(self, expanded: bool):
        expanded = bool(expanded)
        self.detail_panel.setVisible(expanded)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)

    def bind_task(self, task_id: str | None):
        task_id = str(task_id or "").strip() or None
        self._auto_select = task_id is None
        if task_id != self._bound_task_id:
            self._bound_task_id = task_id
        self.refresh()

    @staticmethod
    def _latest_task(store):
        getter = getattr(store, "list_unfinished_tasks", None)
        if not callable(getter):
            getter = getattr(store, "list_recoverable_tasks", None)
        tasks = list(getter() or []) if callable(getter) else []
        if not tasks:
            return None
        return max(tasks, key=lambda item: str(item.get("updated_at") or ""))

    @staticmethod
    def _role_state(store, task_id, role):
        getter = getattr(store, "role_state", None)
        if not callable(getter):
            getter = getattr(store, "get_role_status", None)
        return getter(task_id, role) if callable(getter) else None

    def _show_unavailable(self, text: str):
        self.summary.setText(text)
        self.task_line.setText(text)
        for row in self.rows.values():
            row.apply_state(None)
        self.learning_line.clear()
        self.learning_line.setVisible(False)

    def refresh(self):
        if self._shutdown:
            return
        try:
            if not self._ensure_store():
                self._show_unavailable("任务记忆未启用")
                return
            task = None
            if self._bound_task_id:
                task = self._store.get_task(self._bound_task_id)
            if task is None and self._auto_select:
                task = self._latest_task(self._store)
                self._bound_task_id = str(task.get("task_id") or "") if task else None
            if not task:
                if self._bound_task_id and not self._auto_select:
                    self._show_unavailable("等待任务状态建立…")
                else:
                    self._show_unavailable("暂无进行中的任务")
                return

            task_id = str(task.get("task_id") or self._bound_task_id or "")
            self._bound_task_id = task_id or None
            stage = str(task.get("current_stage") or "").strip()
            status = str(task.get("status") or "pending").strip().lower()
            stage_text = STAGE_LABELS.get(stage, stage or STATUS_LABELS.get(status, status))
            role_states = {}
            for role in ROLE_ORDER:
                state = self._role_state(self._store, task_id, role)
                if state and state.get("output_artifact_id"):
                    artifact_getter = getattr(self._store, "get_artifact", None)
                    if callable(artifact_getter):
                        artifact = artifact_getter(state["output_artifact_id"])
                        if artifact and artifact.get("path"):
                            state = dict(state)
                            state["output_artifact_path"] = artifact["path"]
                role_states[role] = state or {}
                self.rows[role].apply_state(state)
            director_state = role_states.get("director") or {}
            director_artifact = str(director_state.get("output_artifact_path") or "")
            learned = learning_view(director_artifact)
            self.learning_line.setText(learned["text"])
            self.learning_line.setToolTip(learned["tooltip"])
            self.learning_line.setVisible(bool(learned["visible"]))
            active = [role_view(role, state) for role, state in role_states.items()
                      if str(state.get("status") or "") == "running"]
            if active:
                top = max(active, key=lambda item: item["progress"])
                self.summary.setText("%s · %s" % (top["role_label"], top["progress_text"]))
            else:
                if learned["visible"] and learned["lessons"]:
                    self.summary.setText("导演完成 · 学到 %d 条" % learned["lessons"])
                else:
                    self.summary.setText(stage_text or STATUS_LABELS.get(status, status))
            short_id = task_id if len(task_id) <= 28 else task_id[:25] + "…"
            self.task_line.setText("%s · %s" % (stage_text or "任务", short_id))
            self.task_line.setToolTip(task_id)
        except Exception as exc:  # noqa: BLE001
            self._show_unavailable("状态读取失败：%s" % str(exc)[:36])

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self.timer.stop()
        self._close_store()


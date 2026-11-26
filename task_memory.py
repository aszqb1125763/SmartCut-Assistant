# -*- coding: utf-8 -*-
"""Persistent task board and recovery journal for the editing workflow."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import memory_config


ROLES = ("analyst", "writer", "editor", "boundary", "director", "renderer")
LEVELS = ("light", "medium", "heavy")
TASK_STATUSES = (
    "pending", "running", "paused", "interrupted", "failed", "completed", "cancelled",
)
ROLE_STATUSES = (
    "pending", "ready", "running", "blocked", "waiting", "failed", "completed",
    "cancelled", "interrupted",
)
JOB_STATUSES = (
    "pending", "ready", "running", "blocked", "failed", "completed", "cancelled",
    "interrupted",
)
TERMINAL_TASK_STATUSES = ("completed", "cancelled")
STAGE_ROLES = {
    "analyze": "analyst",
    "writer": "writer",
    "editor": "editor",
    "boundary": "boundary",
    "director": "director",
    "transition": "renderer",
    "renderer": "renderer",
    "finalize": "renderer",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("source_manifest_json", "dependencies_json", "resource_json",
                "known_problems_json", "required_actions_json", "payload_json",
                "state_json"):
        if key in data:
            data[key[:-5] if key.endswith("_json") else key] = _decode(data.pop(key), {})
    return data


def validate_role(role: str) -> str:
    value = str(role or "").strip().lower()
    if value not in ROLES:
        raise ValueError("未知岗位：%s" % role)
    return value


def validate_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    try:
        import edit_modes
        value = edit_modes.ALIASES.get(value, value)
        if value not in edit_modes.MODES:
            raise ValueError("未知剪辑模式：%s" % mode)
        return value
    except ImportError:
        if not _ID_RE.fullmatch(value):
            raise ValueError("非法剪辑模式：%s" % mode)
        return value


def validate_level(level: str) -> str:
    value = str(level or "").strip().lower()
    if value not in LEVELS:
        raise ValueError("未知剪辑力度：%s" % level)
    return value


def _validate(value: str, allowed: tuple[str, ...], label: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise ValueError("未知%s：%s" % (label, value))
    return text


def _validate_id(value: str, label: str = "ID") -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError("非法%s：%s" % (label, value))
    return text


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex)


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    level TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_manifest_json TEXT NOT NULL,
    active_script_id TEXT,
    active_cut_id TEXT,
    active_boundary_id TEXT,
    active_review_id TEXT,
    approved_render_input_id TEXT
);
CREATE TABLE IF NOT EXISTS role_status (
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    current_unit TEXT,
    input_artifact_id TEXT,
    output_artifact_id TEXT,
    blocked_by TEXT,
    checkpoint_id TEXT,
    error_text TEXT,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (task_id, role),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    unit_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    dependencies_json TEXT NOT NULL,
    input_artifact_id TEXT,
    output_artifact_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    resource_json TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    source_artifact_id TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    input_artifact_id TEXT,
    output_artifact_id TEXT,
    summary TEXT,
    known_problems_json TEXT NOT NULL,
    required_actions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rework_orders (
    order_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    target_artifact_id TEXT,
    target_unit_id TEXT,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    resolved_by_artifact_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    path TEXT NOT NULL,
    state_json TEXT NOT NULL,
    artifact_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events (
    sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    role TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS autosave_operations (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_task_type ON artifacts(task_id, artifact_type, version);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_rework_task_status ON rework_orders(task_id, status);
"""


class TaskMemory:
    """Single-writer task board with WAL, batching and crash-recovery queries."""

    def __init__(self, root: str | os.PathLike | None = None, config: dict | None = None):
        self.root = os.path.abspath(os.fspath(root) if root is not None
                                    else memory_config.ensure_root(config=config))
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, "task_board.sqlite")
        self._lock = threading.RLock()
        self._batch_depth = 0
        self._closed = False
        self._conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            job_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for name, ddl in (
                ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("error_text", "TEXT"),
            ):
                if name not in job_columns:
                    self._conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, ddl))
            self._conn.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.flush()
        self.close()

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("task memory is closed")

    def _commit_if_needed(self):
        if self._batch_depth == 0:
            self._conn.commit()

    @contextlib.contextmanager
    def batch(self):
        """Commit several state/event writes as one durable SQLite transaction."""
        with self._lock:
            self._ensure_open()
            outer = self._batch_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._batch_depth += 1
        try:
            yield self
        except Exception:
            with self._lock:
                self._batch_depth -= 1
                if outer:
                    self._conn.rollback()
            raise
        else:
            with self._lock:
                self._batch_depth -= 1
                if outer:
                    self._conn.commit()

    def flush(self):
        with self._lock:
            self._ensure_open()
            self._conn.commit()
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.DatabaseError:
                pass

    def integrity_check(self) -> dict:
        """Check SQLite consistency, foreign keys and registered artifact paths."""
        with self._lock:
            self._ensure_open()
            database = [row[0] for row in self._conn.execute("PRAGMA integrity_check").fetchall()]
            foreign_keys = [tuple(row) for row in self._conn.execute("PRAGMA foreign_key_check").fetchall()]
            paths = [row[0] for row in self._conn.execute("SELECT path FROM artifacts").fetchall()]
        missing = [path for path in paths if path and not os.path.exists(path)]
        return {
            "ok": database == ["ok"] and not foreign_keys,
            "database": database,
            "foreign_key_errors": foreign_keys,
            "missing_artifacts": missing,
        }

    @staticmethod
    def _assert_external_destination(source: str, destination: str) -> None:
        source = os.path.normcase(os.path.abspath(source))
        destination = os.path.normcase(os.path.abspath(destination))
        try:
            common = os.path.commonpath((source, destination))
        except ValueError:
            return
        if common == source:
            raise ValueError("目标目录不能位于现有记忆目录内部")
        if source == destination:
            raise ValueError("目标目录不能与现有记忆目录相同")

    def backup_to(self, destination: str | os.PathLike) -> str:
        """Create an atomic, consistent copy of the whole memory root."""
        target = os.path.abspath(os.fspath(destination))
        self._assert_external_destination(self.root, target)
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(target):
            if not os.path.isdir(target) or os.listdir(target):
                raise FileExistsError("备份目标必须不存在或为空：%s" % target)
            os.rmdir(target)
        stage = os.path.join(parent, ".%s.tmp-%s" % (
            os.path.basename(target), uuid.uuid4().hex))
        os.makedirs(stage)
        try:
            with self._lock:
                self._ensure_open()
                self._conn.commit()
                backup_db = os.path.join(stage, "task_board.sqlite")
                with contextlib.closing(sqlite3.connect(backup_db)) as dest_conn:
                    self._conn.backup(dest_conn)
                for entry in os.scandir(self.root):
                    if entry.name in {"task_board.sqlite", "task_board.sqlite-wal",
                                      "task_board.sqlite-shm"}:
                        continue
                    out = os.path.join(stage, entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        shutil.copytree(entry.path, out)
                    elif entry.is_file(follow_symlinks=False):
                        shutil.copy2(entry.path, out)
                manifest = {
                    "created_at": _now(),
                    "source": self.root,
                    "integrity": self.integrity_check(),
                }
                with open(os.path.join(stage, "backup_manifest.json"), "w", encoding="utf-8") as fh:
                    json.dump(manifest, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
            os.replace(stage, target)
            return target
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._conn.commit()
                self._conn.close()
                self._closed = True

    def task_dir(self, task_id: str) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        return os.path.join(self.root, "tasks", task_id)

    def _prepare_task_dirs(self, task_id: str):
        base = self.task_dir(task_id)
        for relative in ("shared", "artifacts", "checkpoints"):
            os.makedirs(os.path.join(base, relative), exist_ok=True)
        for role in ROLES:
            os.makedirs(os.path.join(base, "roles", role), exist_ok=True)

    def _event_locked(self, task_id: str, event_type: str, payload=None,
                      role: str | None = None, applied: bool = True) -> str:
        event_id = _new_id("EVT")
        self._conn.execute(
            "INSERT INTO events(event_id,task_id,role,event_type,payload_json,created_at,applied) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_id, task_id, role, str(event_type), _json(payload or {}), _now(), int(applied)),
        )
        return event_id

    def append_event(self, task_id: str, event_type: str, payload=None,
                     role: str | None = None, applied: bool = True) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        role = validate_role(role) if role is not None else None
        with self._lock:
            self._ensure_open()
            event_id = self._event_locked(task_id, event_type, payload, role, applied)
            self._commit_if_needed()
            return event_id

    def apply_buffered_operations(self, operations) -> dict:
        """Apply autosave proxy operations exactly once in one transaction.

        ``operation_id`` is committed in the same SQLite transaction as the
        requested mutation.  A crash after commit but before spool cleanup can
        therefore replay safely: the durable marker makes the retry a no-op.
        Only the small, state-oriented API used by the autosave proxy is
        accepted here; file-producing and ID-returning operations stay
        synchronous on :class:`TaskMemory`.
        """
        allowed = {
            "append_event",
            "record_stage_state",
            "set_task_status",
            "update_role_status",
        }
        rows = list(operations or [])
        applied_ids = []
        skipped_ids = []
        with self.batch():
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("autosave operation must be a dict")
                operation_id = _validate_id(row.get("operation_id"), "自动保存操作 ID")
                method_name = str(row.get("method") or "").strip()
                if method_name not in allowed:
                    raise ValueError("不允许缓冲的方法：%s" % method_name)
                with self._lock:
                    exists = self._conn.execute(
                        "SELECT 1 FROM autosave_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                if exists is not None:
                    skipped_ids.append(operation_id)
                    continue
                args = row.get("args") or []
                kwargs = row.get("kwargs") or {}
                if not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
                    raise TypeError("autosave args/kwargs malformed")
                getattr(self, method_name)(*args, **kwargs)
                with self._lock:
                    self._conn.execute(
                        "INSERT INTO autosave_operations(operation_id,operation_type,applied_at) "
                        "VALUES(?,?,?)",
                        (operation_id, method_name, _now()),
                    )
                applied_ids.append(operation_id)
        return {
            "applied": applied_ids,
            "skipped": skipped_ids,
            "total": len(rows),
        }

    def create_task(self, source_manifest, mode="gameplay", level="medium",
                    task_id: str | None = None, status="pending") -> str:
        task_id = _validate_id(task_id or _new_id("TASK"), "任务 ID")
        mode = validate_mode(mode)
        level = validate_level(level)
        status = _validate(status, TASK_STATUSES, "任务状态")
        self._prepare_task_dirs(task_id)
        now = _now()
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "INSERT INTO tasks(task_id,mode,level,status,current_stage,created_at,updated_at,"
                "source_manifest_json) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, mode, level, status, None, now, now, _json(source_manifest or {})),
            )
            for role in ROLES:
                self._conn.execute(
                    "INSERT INTO role_status(task_id,role,status,progress,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (task_id, role, "pending", 0.0, now),
                )
            self._event_locked(task_id, "task.created", {
                "mode": mode, "level": level, "source_manifest": source_manifest or {},
            })
            self._commit_if_needed()
        return task_id

    def get_task(self, task_id: str) -> dict | None:
        task_id = _validate_id(task_id, "任务 ID")
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return _row(row)

    def list_unfinished_tasks(self) -> list[dict]:
        marks = ",".join("?" for _ in TERMINAL_TASK_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status NOT IN (%s) ORDER BY updated_at" % marks,
                TERMINAL_TASK_STATUSES,
            ).fetchall()
        return [_row(row) for row in rows]

    def set_task_status(self, task_id: str, status: str,
                        current_stage: str | None = None, **pointers) -> None:
        task_id = _validate_id(task_id, "任务 ID")
        status = _validate(status, TASK_STATUSES, "任务状态")
        allowed = {
            "active_script_id", "active_cut_id", "active_boundary_id",
            "active_review_id", "approved_render_input_id",
        }
        unknown = set(pointers) - allowed
        if unknown:
            raise ValueError("未知任务成果指针：%s" % ", ".join(sorted(unknown)))
        fields = ["status=?", "current_stage=?", "updated_at=?"]
        values = [status, current_stage, _now()]
        for key, value in pointers.items():
            fields.append("%s=?" % key)
            values.append(value)
        values.append(task_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET %s WHERE task_id=?" % ",".join(fields), values,
            )
            if cur.rowcount != 1:
                raise KeyError("任务不存在：%s" % task_id)
            self._event_locked(task_id, "task.status", {
                "status": status, "current_stage": current_stage, **pointers,
            })
            self._commit_if_needed()

    def role_state(self, task_id: str, role: str) -> dict | None:
        role = validate_role(role)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM role_status WHERE task_id=? AND role=?", (task_id, role),
            ).fetchone()
        return _row(row)

    def update_role_status(self, task_id: str, role: str, status: str,
                           progress: float | None = None, current_unit: str | None = None,
                           input_artifact_id: str | None = None,
                           output_artifact_id: str | None = None,
                           blocked_by: str | None = None,
                           checkpoint_id: str | None = None,
                           error_text: str | None = None) -> None:
        task_id = _validate_id(task_id, "任务 ID")
        role = validate_role(role)
        status = _validate(status, ROLE_STATUSES, "岗位状态")
        if progress is not None:
            progress = float(progress)
            if not 0.0 <= progress <= 1.0:
                raise ValueError("岗位进度必须在 0~1 之间")
        with self._lock:
            old = self._conn.execute(
                "SELECT * FROM role_status WHERE task_id=? AND role=?", (task_id, role),
            ).fetchone()
            if old is None:
                raise KeyError("任务或岗位不存在：%s/%s" % (task_id, role))
            values = {
                "progress": old["progress"] if progress is None else progress,
                "current_unit": current_unit,
                "input_artifact_id": input_artifact_id,
                "output_artifact_id": output_artifact_id,
                "blocked_by": blocked_by,
                "checkpoint_id": checkpoint_id or old["checkpoint_id"],
                "error_text": error_text,
            }
            self._conn.execute(
                "UPDATE role_status SET status=?,progress=?,current_unit=?,input_artifact_id=?,"
                "output_artifact_id=?,blocked_by=?,checkpoint_id=?,error_text=?,updated_at=?,"
                "row_version=row_version+1 WHERE task_id=? AND role=?",
                (status, values["progress"], values["current_unit"],
                 values["input_artifact_id"], values["output_artifact_id"],
                 values["blocked_by"], values["checkpoint_id"], values["error_text"],
                 _now(), task_id, role),
            )
            self._event_locked(task_id, "role.status", {
                "status": status, "progress": values["progress"],
                "current_unit": current_unit, "blocked_by": blocked_by,
                "error_text": error_text,
            }, role=role)
            self._commit_if_needed()

    def enqueue_job(self, task_id: str, role: str, unit_id: str | None = None,
                    dependencies=None, input_artifact_id: str | None = None,
                    priority: int = 0, resources=None, job_id: str | None = None) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        role = validate_role(role)
        job_id = _validate_id(job_id or _new_id("JOB"), "工作 ID")
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(job_id,task_id,role,unit_id,status,priority,dependencies_json,"
                "input_artifact_id,resource_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, task_id, role, unit_id, "pending", int(priority),
                 _json(dependencies or []), input_artifact_id, _json(resources or {}), now, now),
            )
            self._event_locked(task_id, "job.created", {"job_id": job_id, "unit_id": unit_id}, role)
            self._commit_if_needed()
        return job_id

    @staticmethod
    def _stage_role(stage: str) -> str:
        stage = str(stage or "").strip().lower()
        if stage not in STAGE_ROLES:
            raise ValueError("未知工作流阶段：%s" % stage)
        return STAGE_ROLES[stage]

    def get_stage_state(self, task_id: str, stage: str) -> dict | None:
        """WorkflowMemory-compatible durable stage lookup."""
        task_id = _validate_id(task_id, "任务 ID")
        stage = str(stage or "").strip().lower()
        self._stage_role(stage)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE task_id=? AND unit_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id, stage),
            ).fetchone()
        if row is None:
            return None
        data = _row(row)
        data["stage"] = stage
        data["payload"] = _decode(row["payload_json"] if "payload_json" in row.keys() else None)
        data["metadata"] = _decode(row["metadata_json"] if "metadata_json" in row.keys() else None, {})
        data["error"] = row["error_text"] if "error_text" in row.keys() else None
        return data

    def record_stage_state(self, task_id: str, stage: str, status: str, **fields) -> None:
        """Persist one workflow transition across task, job, role and event views."""
        task_id = _validate_id(task_id, "任务 ID")
        stage = str(stage or "").strip().lower()
        role = self._stage_role(stage)
        raw_status = str(status or "").strip().lower()
        job_status = {"started": "running"}.get(raw_status, raw_status)
        job_status = _validate(job_status, JOB_STATUSES, "工作状态")
        role_status = job_status
        job = fields.get("job") if isinstance(fields.get("job"), dict) else {}
        dependencies = job.get("dependencies") or []
        resources = job.get("resources") or {}
        input_ids = fields.get("input_artifact_ids") or []
        output_id = fields.get("output_artifact_id")
        payload = fields.get("payload")
        metadata = fields.get("metadata") or {}
        error_text = fields.get("error")
        job_id = "%s.%s" % (task_id, stage)
        now = _now()
        with self._lock:
            task = self._conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError("任务不存在：%s" % task_id)
            existing = self._conn.execute(
                "SELECT job_id,attempt FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO jobs(job_id,task_id,role,unit_id,status,priority,dependencies_json,"
                    "input_artifact_id,output_artifact_id,attempt,resource_json,payload_json,"
                    "metadata_json,error_text,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, task_id, role, stage, job_status, 0, _json(dependencies),
                     input_ids[0] if input_ids else None, output_id,
                     1 if job_status == "running" else 0, _json(resources), _json(payload),
                     _json(metadata), error_text, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status=?,dependencies_json=?,input_artifact_id=?,"
                    "output_artifact_id=?,attempt=attempt+?,resource_json=?,payload_json=?,"
                    "metadata_json=?,error_text=?,updated_at=? WHERE job_id=?",
                    (job_status, _json(dependencies), input_ids[0] if input_ids else None,
                     output_id, 1 if raw_status == "started" else 0, _json(resources),
                     _json(payload), _json(metadata), error_text, now, job_id),
                )
            progress = 1.0 if job_status == "completed" else 0.0
            current = self._conn.execute(
                "SELECT row_version,checkpoint_id FROM role_status WHERE task_id=? AND role=?",
                (task_id, role),
            ).fetchone()
            if current is None:
                raise KeyError("任务岗位不存在：%s/%s" % (task_id, role))
            self._conn.execute(
                "UPDATE role_status SET status=?,progress=?,current_unit=?,input_artifact_id=?,"
                "output_artifact_id=?,error_text=?,updated_at=?,row_version=row_version+1 "
                "WHERE task_id=? AND role=?",
                (role_status, progress, stage, input_ids[0] if input_ids else None,
                 output_id, error_text, now, task_id, role),
            )
            if job_status == "failed":
                task_status = "failed"
            elif job_status == "cancelled":
                task_status = "cancelled"
            elif stage == "finalize" and job_status == "completed":
                task_status = "completed"
            else:
                task_status = "running"
            self._conn.execute(
                "UPDATE tasks SET status=?,current_stage=?,updated_at=? WHERE task_id=?",
                (task_status, stage, now, task_id),
            )
            event_fields = dict(fields)
            event_fields["stage"] = stage
            event_fields["status"] = raw_status
            self._event_locked(task_id, "stage.%s" % raw_status, event_fields, role)
            self._commit_if_needed()

    def update_job(self, job_id: str, status: str, output_artifact_id: str | None = None,
                   lease_owner: str | None = None, lease_until: str | None = None,
                   increment_attempt: bool = False) -> None:
        job_id = _validate_id(job_id, "工作 ID")
        status = _validate(status, JOB_STATUSES, "工作状态")
        with self._lock:
            job = self._conn.execute("SELECT task_id,role FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError("工作不存在：%s" % job_id)
            self._conn.execute(
                "UPDATE jobs SET status=?,output_artifact_id=?,lease_owner=?,lease_until=?,"
                "attempt=attempt+?,updated_at=? WHERE job_id=?",
                (status, output_artifact_id, lease_owner, lease_until,
                 1 if increment_attempt else 0, _now(), job_id),
            )
            self._event_locked(job["task_id"], "job.status", {
                "job_id": job_id, "status": status, "output_artifact_id": output_artifact_id,
            }, job["role"])
            self._commit_if_needed()

    def register_artifact(self, task_id: str, role: str, artifact_type: str, path: str,
                          version: int = 1, source_artifact_id: str | None = None,
                          status: str = "active", artifact_id: str | None = None,
                          sha256: str | None = None, activate: bool = False) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        role = validate_role(role)
        artifact_id = _validate_id(artifact_id or _new_id("ART"), "成果 ID")
        artifact_type = str(artifact_type or "").strip().lower()
        if not _ID_RE.fullmatch(artifact_type):
            raise ValueError("非法成果类型：%s" % artifact_type)
        version = int(version)
        if version < 1:
            raise ValueError("成果版本必须大于 0")
        full_path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if sha256 is None and os.path.isfile(full_path):
            digest = hashlib.sha256()
            with open(full_path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            sha256 = digest.hexdigest()
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,role,artifact_type,version,path,sha256,"
                "source_artifact_id,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, task_id, role, artifact_type, version, full_path, sha256,
                 source_artifact_id, _now(), str(status)),
            )
            if activate:
                pointer = {
                    "script": "active_script_id", "cut": "active_cut_id",
                    "cut_plan": "active_cut_id", "boundary": "active_boundary_id",
                    "review": "active_review_id", "render_input": "approved_render_input_id",
                }.get(artifact_type)
                if pointer:
                    self._conn.execute(
                        "UPDATE tasks SET %s=?,updated_at=? WHERE task_id=?" % pointer,
                        (artifact_id, _now(), task_id),
                    )
            self._event_locked(task_id, "artifact.created", {
                "artifact_id": artifact_id, "type": artifact_type, "version": version,
                "path": full_path, "source_artifact_id": source_artifact_id,
            }, role)
            self._commit_if_needed()
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,),
            ).fetchone()
        return _row(row)

    def revise_artifact(self, artifact_id: str, path: str,
                        source_artifact_id: str | None = None,
                        sha256: str | None = None, activate: bool = True) -> str:
        """Update a stable role artifact after a resumed local rework."""
        artifact_id = _validate_id(artifact_id, "成果 ID")
        full_path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if sha256 is None and os.path.isfile(full_path):
            digest = hashlib.sha256()
            with open(full_path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            sha256 = digest.hexdigest()
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id,role,artifact_type,version FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise KeyError("成果不存在：%s" % artifact_id)
            version = int(row["version"] or 1) + 1
            self._conn.execute(
                "UPDATE artifacts SET version=?,path=?,sha256=?,source_artifact_id=?,"
                "created_at=?,status='active' WHERE artifact_id=?",
                (version, full_path, sha256, source_artifact_id, _now(), artifact_id),
            )
            if activate:
                pointer = {
                    "script": "active_script_id", "cut": "active_cut_id",
                    "cut_plan": "active_cut_id", "boundary": "active_boundary_id",
                    "review": "active_review_id", "render_input": "approved_render_input_id",
                }.get(row["artifact_type"])
                if pointer:
                    self._conn.execute(
                        "UPDATE tasks SET %s=?,updated_at=? WHERE task_id=?" % pointer,
                        (artifact_id, _now(), row["task_id"]),
                    )
            self._event_locked(row["task_id"], "artifact.revised", {
                "artifact_id": artifact_id, "version": version, "path": full_path,
                "source_artifact_id": source_artifact_id,
            }, row["role"])
            self._commit_if_needed()
        return artifact_id

    def record_handoff(self, task_id: str, from_role: str, to_role: str,
                       input_artifact_id: str | None = None,
                       output_artifact_id: str | None = None, summary: str = "",
                       known_problems=None, required_actions=None) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        from_role = validate_role(from_role)
        to_role = validate_role(to_role)
        handoff_id = _new_id("HANDOFF")
        with self._lock:
            self._conn.execute(
                "INSERT INTO handoffs(handoff_id,task_id,from_role,to_role,input_artifact_id,"
                "output_artifact_id,summary,known_problems_json,required_actions_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (handoff_id, task_id, from_role, to_role, input_artifact_id,
                 output_artifact_id, summary, _json(known_problems or []),
                 _json(required_actions or []), _now()),
            )
            self._event_locked(task_id, "handoff.created", {
                "handoff_id": handoff_id, "from": from_role, "to": to_role,
            }, from_role)
            self._commit_if_needed()
        return handoff_id

    def create_rework_order(self, task_id: str, from_role: str, to_role: str,
                            operation: str, reason: str,
                            target_artifact_id: str | None = None,
                            target_unit_id: str | None = None) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        from_role = validate_role(from_role)
        to_role = validate_role(to_role)
        order_id = _new_id("REWORK")
        with self._lock:
            self._conn.execute(
                "INSERT INTO rework_orders(order_id,task_id,from_role,to_role,target_artifact_id,"
                "target_unit_id,operation,reason,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order_id, task_id, from_role, to_role, target_artifact_id, target_unit_id,
                 str(operation), str(reason), "open", _now()),
            )
            self._event_locked(task_id, "rework.created", {
                "order_id": order_id, "to": to_role, "operation": operation,
                "target_unit_id": target_unit_id,
            }, from_role)
            self._commit_if_needed()
        return order_id

    def resolve_rework_order(self, order_id: str, artifact_id: str) -> None:
        order_id = _validate_id(order_id, "返工单 ID")
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id,to_role FROM rework_orders WHERE order_id=?", (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError("返工单不存在：%s" % order_id)
            self._conn.execute(
                "UPDATE rework_orders SET status='resolved',resolved_by_artifact_id=?,resolved_at=? "
                "WHERE order_id=?",
                (artifact_id, _now(), order_id),
            )
            self._event_locked(row["task_id"], "rework.resolved", {
                "order_id": order_id, "artifact_id": artifact_id,
            }, row["to_role"])
            self._commit_if_needed()

    def list_rework_orders(self, task_id: str, status: str | None = None) -> list[dict]:
        task_id = _validate_id(task_id, "任务 ID")
        params = [task_id]
        where = "task_id=?"
        if status is not None:
            where += " AND status=?"
            params.append(str(status))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rework_orders WHERE %s ORDER BY created_at,order_id" % where,
                tuple(params),
            ).fetchall()
        return [_row(row) for row in rows]

    def resolve_open_rework_orders(self, task_id: str, artifact_id: str) -> int:
        """Resolve all outstanding director orders after a successful review."""
        task_id = _validate_id(task_id, "任务 ID")
        artifact_id = _validate_id(artifact_id, "成果 ID")
        with self._lock:
            rows = self._conn.execute(
                "SELECT order_id,to_role FROM rework_orders "
                "WHERE task_id=? AND status='open' ORDER BY created_at,order_id",
                (task_id,),
            ).fetchall()
            now = _now()
            for row in rows:
                self._conn.execute(
                    "UPDATE rework_orders SET status='resolved',resolved_by_artifact_id=?,"
                    "resolved_at=? WHERE order_id=?",
                    (artifact_id, now, row["order_id"]),
                )
                self._event_locked(task_id, "rework.resolved", {
                    "order_id": row["order_id"], "artifact_id": artifact_id,
                }, row["to_role"])
            self._commit_if_needed()
        return len(rows)

    def save_checkpoint(self, task_id: str, role: str, state,
                        artifact_id: str | None = None,
                        checkpoint_id: str | None = None) -> str:
        task_id = _validate_id(task_id, "任务 ID")
        role = validate_role(role)
        checkpoint_id = _validate_id(checkpoint_id or _new_id("CHK"), "检查点 ID")
        folder = os.path.join(self.task_dir(task_id), "checkpoints")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, checkpoint_id + ".json")
        payload = {
            "checkpoint_id": checkpoint_id, "task_id": task_id, "role": role,
            "artifact_id": artifact_id, "created_at": _now(), "state": state,
        }
        fd, temp_path = tempfile.mkstemp(prefix=".checkpoint.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        with self._lock:
            self._conn.execute(
                "INSERT INTO checkpoints(checkpoint_id,task_id,role,path,state_json,artifact_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (checkpoint_id, task_id, role, path, _json(state), artifact_id, payload["created_at"]),
            )
            self._conn.execute(
                "UPDATE role_status SET checkpoint_id=?,updated_at=?,row_version=row_version+1 "
                "WHERE task_id=? AND role=?",
                (checkpoint_id, _now(), task_id, role),
            )
            self._event_locked(task_id, "checkpoint.saved", {
                "checkpoint_id": checkpoint_id, "artifact_id": artifact_id, "path": path,
            }, role)
            self._commit_if_needed()
        return checkpoint_id

    def latest_checkpoint(self, task_id: str, role: str | None = None) -> dict | None:
        task_id = _validate_id(task_id, "任务 ID")
        params = [task_id]
        where = "task_id=?"
        if role is not None:
            role = validate_role(role)
            where += " AND role=?"
            params.append(role)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE %s ORDER BY created_at DESC LIMIT 1" % where,
                params,
            ).fetchone()
        return _row(row)

    def events(self, task_id: str, after_sequence: int = 0) -> list[dict]:
        task_id = _validate_id(task_id, "任务 ID")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id=? AND sequence_no>? ORDER BY sequence_no",
                (task_id, int(after_sequence)),
            ).fetchall()
        return [_row(row) for row in rows]

    def mark_running_interrupted(self) -> list[str]:
        """Mark sessions left running by an unclean shutdown as recoverable."""
        with self.batch():
            with self._lock:
                rows = self._conn.execute(
                    "SELECT task_id,current_stage FROM tasks WHERE status='running'"
                ).fetchall()
                ids = [row["task_id"] for row in rows]
                now = _now()
                for row in rows:
                    task_id = row["task_id"]
                    self._conn.execute(
                        "UPDATE tasks SET status='interrupted',updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                    self._conn.execute(
                        "UPDATE role_status SET status='interrupted',updated_at=?,row_version=row_version+1 "
                        "WHERE task_id=? AND status='running'",
                        (now, task_id),
                    )
                    self._event_locked(task_id, "task.recovered_after_unclean_shutdown", {
                        "previous_stage": row["current_stage"],
                    })
        return ids


def open_store(root: str | os.PathLike | None = None, config: dict | None = None) -> TaskMemory:
    return TaskMemory(root=root, config=config)


def migrate_root(old_root: str | os.PathLike, new_root: str | os.PathLike) -> str:
    """Copy a memory root consistently; callers switch config only after success."""
    source = os.path.abspath(os.fspath(old_root))
    target = os.path.abspath(os.fspath(new_root))
    TaskMemory._assert_external_destination(source, target)
    with TaskMemory(root=source) as store:
        check = store.integrity_check()
        if not check["ok"]:
            raise sqlite3.DatabaseError("现有记忆库完整性检查失败：%s" % check["database"])
        return store.backup_to(target)

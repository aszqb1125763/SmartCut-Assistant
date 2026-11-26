# -*- coding: utf-8 -*-
"""Buffered, crash-recoverable proxy for :mod:`task_memory`.

The workflow emits many small state changes.  Writing every one as its own
SQLite transaction creates unnecessary disk pressure, while keeping them only
in memory loses the active task on a crash.  ``TaskAutosave`` bridges the two:

* every buffered operation is first appended to a small JSONL spool;
* one writer thread commits a batch at ``journal_flush_ms`` intervals;
* terminal stage/task states force an immediate durable flush;
* the database records the stable operation id in the same transaction;
* an unclean shutdown replays the spool without duplicating mutations;
* active role state is periodically written as an ordinary TaskMemory
  checkpoint.

ID-returning and file-producing TaskMemory methods stay synchronous.  Before
delegating them the proxy drains its buffer, preserving call order.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path


BUFFERED_METHODS = {
    "append_event",
    "record_stage_state",
    "set_task_status",
    "update_role_status",
}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
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


def _json_safe(value):
    """Return a detached JSON-compatible representation for the disk spool."""
    return json.loads(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str))


class AutosaveError(RuntimeError):
    """A buffered write could not be committed."""


class TaskAutosave:
    """TaskMemory-compatible single-writer autosave proxy."""

    def __init__(self, backend, config=None, *, journal_flush_ms=None,
                 checkpoint_seconds=None, spool_path=None, owns_backend=False,
                 start=True):
        if backend is None:
            raise ValueError("backend is required")
        self.backend = backend
        cfg = dict(config or {})
        self.journal_flush_ms = max(
            100, int(journal_flush_ms if journal_flush_ms is not None
                     else cfg.get("journal_flush_ms", 1000)))
        self.checkpoint_seconds = max(
            1, int(checkpoint_seconds if checkpoint_seconds is not None
                   else cfg.get("checkpoint_seconds", 120)))
        root = os.path.abspath(os.fspath(getattr(backend, "root", os.getcwd())))
        self.spool_path = os.path.abspath(os.fspath(
            spool_path or os.path.join(root, "task_autosave.spool.jsonl")))
        self.owns_backend = bool(owns_backend)

        self._condition = threading.Condition(threading.RLock())
        self._pending = []
        self._checkpoint_states = {}
        self._force_flush = False
        self._stop = False
        self._closed = False
        self._writer = None
        self._last_error = None
        self._error_generation = 0
        self._next_checkpoint = time.monotonic() + self.checkpoint_seconds
        self._stats = {
            "enqueued": 0,
            "batches": 0,
            "operations_applied": 0,
            "operations_skipped": 0,
            "spool_appends": 0,
            "spool_compactions": 0,
            "checkpoints": 0,
            "errors": 0,
            "replayed": 0,
        }
        self._load_spool()
        if start:
            self.start()

    @property
    def root(self):
        return self.backend.root

    @property
    def db_path(self):
        return getattr(self.backend, "db_path", None)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("task autosave is closed")
            if self._writer is not None and self._writer.is_alive():
                return self
            self._stop = False
            self._writer = threading.Thread(
                target=self._writer_loop,
                name="TaskAutosaveWriter",
                daemon=True,
            )
            self._writer.start()
            if self._pending:
                self._force_flush = True
                self._condition.notify_all()
        return self

    def _load_spool(self):
        path = Path(self.spool_path)
        if not path.is_file():
            return
        recovered = []
        seen = set()
        errors = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        operation = json.loads(line)
                        operation_id = str(operation.get("operation_id") or "")
                        method = str(operation.get("method") or "")
                        if not operation_id or method not in BUFFERED_METHODS:
                            raise ValueError("invalid autosave operation")
                        if operation_id not in seen:
                            recovered.append(operation)
                            seen.add(operation_id)
                    except Exception as exc:  # noqa: BLE001
                        errors.append("line %d: %s" % (line_number, exc))
        except OSError as exc:
            errors.append(str(exc))
        self._pending.extend(recovered)
        self._stats["replayed"] = len(recovered)
        for operation in recovered:
            self._remember_checkpoint_state(operation)
        if errors:
            self._set_error("spool_replay", RuntimeError("; ".join(errors[:3])))

    def _append_spool_locked(self, operation, durable=False):
        parent = os.path.dirname(self.spool_path)
        os.makedirs(parent, exist_ok=True)
        with open(self.spool_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(
                operation, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str))
            handle.write("\n")
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        self._stats["spool_appends"] += 1

    def _rewrite_spool_locked(self):
        parent = os.path.dirname(self.spool_path)
        os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".task_autosave.", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for operation in self._pending:
                    handle.write(json.dumps(
                        operation, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), default=str))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.spool_path)
            self._stats["spool_compactions"] += 1
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _operation(method, args, kwargs):
        return {
            "operation_id": "ASOP-%s" % uuid.uuid4().hex,
            "method": method,
            "args": _json_safe(list(args)),
            "kwargs": _json_safe(dict(kwargs)),
            "queued_at": time.time(),
        }

    @staticmethod
    def _operation_state(operation):
        method = operation["method"]
        args = operation.get("args") or []
        kwargs = operation.get("kwargs") or {}
        task_id = str(args[0]) if args else ""
        role = None
        status = None
        stage = None
        if method == "record_stage_state" and len(args) >= 3:
            stage = str(args[1])
            status = str(args[2])
            role = STAGE_ROLES.get(stage)
        elif method == "update_role_status" and len(args) >= 3:
            role = str(args[1])
            status = str(args[2])
        elif method == "set_task_status" and len(args) >= 2:
            status = str(args[1])
            stage = kwargs.get("current_stage")
            if stage is None and len(args) >= 3:
                stage = args[2]
            role = STAGE_ROLES.get(str(stage or ""))
        if not task_id or not role:
            return None
        return {
            "task_id": task_id,
            "role": role,
            "stage": stage,
            "status": status,
            "operation_id": operation["operation_id"],
            "method": method,
            "fields": kwargs,
        }

    def _remember_checkpoint_state(self, operation):
        state = self._operation_state(operation)
        if state is not None:
            self._checkpoint_states[state["task_id"]] = state

    @staticmethod
    def _is_terminal(method, args, kwargs):
        if method == "record_stage_state" and len(args) >= 3:
            return str(args[2]).lower() in TERMINAL_STATES
        if method == "update_role_status" and len(args) >= 3:
            return str(args[2]).lower() in TERMINAL_STATES
        if method == "set_task_status" and len(args) >= 2:
            return str(args[1]).lower() in TERMINAL_STATES
        return False

    def _enqueue(self, method, args, kwargs):
        operation = self._operation(method, args, kwargs)
        terminal = self._is_terminal(method, operation["args"], operation["kwargs"])
        with self._condition:
            if self._closed:
                raise RuntimeError("task autosave is closed")
            self._append_spool_locked(operation, durable=terminal)
            self._pending.append(operation)
            self._remember_checkpoint_state(operation)
            self._stats["enqueued"] += 1
            if terminal:
                self._force_flush = True
            self._condition.notify_all()
        if terminal:
            self.flush()
        return operation["operation_id"]

    def append_event(self, task_id, event_type, payload=None,
                     role=None, applied=True):
        return self._enqueue(
            "append_event", (task_id, event_type),
            {"payload": payload, "role": role, "applied": applied})

    def record_stage_state(self, task_id, stage, status, **fields):
        self._enqueue("record_stage_state", (task_id, stage, status), fields)

    def set_task_status(self, task_id, status, current_stage=None, **pointers):
        kwargs = {"current_stage": current_stage, **pointers}
        self._enqueue("set_task_status", (task_id, status), kwargs)

    def update_role_status(self, task_id, role, status, **fields):
        self._enqueue("update_role_status", (task_id, role, status), fields)

    def _set_error(self, phase, exc):
        with self._condition:
            self._last_error = {
                "phase": str(phase),
                "type": type(exc).__name__,
                "message": str(exc),
                "time": time.time(),
            }
            self._error_generation += 1
            self._stats["errors"] += 1
            self._condition.notify_all()

    def last_error(self):
        with self._condition:
            return dict(self._last_error) if self._last_error else None

    def stats(self):
        with self._condition:
            data = dict(self._stats)
            data.update({
                "pending": len(self._pending),
                "writer_alive": bool(self._writer and self._writer.is_alive()),
                "closed": self._closed,
            })
            return data

    def _checkpoint_snapshot(self):
        with self._condition:
            return [dict(value) for value in self._checkpoint_states.values()]

    def _save_periodic_checkpoints(self, force=False):
        now = time.monotonic()
        if not force and now < self._next_checkpoint:
            return
        self._next_checkpoint = now + self.checkpoint_seconds
        for state in self._checkpoint_snapshot():
            try:
                self.backend.save_checkpoint(
                    state["task_id"], state["role"],
                    {"autosave": state, "pending_operations": len(self._pending)})
                with self._condition:
                    self._stats["checkpoints"] += 1
            except Exception as exc:  # noqa: BLE001
                self._set_error("checkpoint", exc)

    def _writer_loop(self):
        interval = self.journal_flush_ms / 1000.0
        next_flush = time.monotonic() + interval
        while True:
            with self._condition:
                while True:
                    now = time.monotonic()
                    due = bool(self._pending) and (
                        self._force_flush or now >= next_flush or self._stop)
                    checkpoint_due = now >= self._next_checkpoint
                    if due or checkpoint_due or (self._stop and not self._pending):
                        break
                    timeout = min(next_flush, self._next_checkpoint) - now
                    self._condition.wait(max(0.01, timeout))
                if self._stop and not self._pending:
                    break
                batch = list(self._pending) if due else []
                self._force_flush = False

            if batch:
                batch_ids = {row["operation_id"] for row in batch}
                try:
                    report = self.backend.apply_buffered_operations(batch)
                except Exception as exc:  # noqa: BLE001
                    self._set_error("database_batch", exc)
                    if self._stop:
                        break
                    next_flush = time.monotonic() + interval
                    continue
                with self._condition:
                    self._pending = [row for row in self._pending
                                     if row["operation_id"] not in batch_ids]
                    self._stats["batches"] += 1
                    self._stats["operations_applied"] += len(report.get("applied") or [])
                    self._stats["operations_skipped"] += len(report.get("skipped") or [])
                    self._last_error = None
                    try:
                        self._rewrite_spool_locked()
                    except Exception as exc:  # noqa: BLE001
                        # The database transaction is already durable.  Leaving
                        # the old spool is safe because operation ids are
                        # idempotent on the next launch.
                        self._last_error = {
                            "phase": "spool_compaction",
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "time": time.time(),
                        }
                        self._error_generation += 1
                        self._stats["errors"] += 1
                    self._condition.notify_all()
                next_flush = time.monotonic() + interval

            self._save_periodic_checkpoints(force=False)

        with self._condition:
            self._condition.notify_all()

    def flush(self, timeout=30.0):
        """Synchronously drain operations queued before this call."""
        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._condition:
            target_ids = {row["operation_id"] for row in self._pending}
            if target_ids:
                if self._writer is None or not self._writer.is_alive():
                    self.start()
                start_error_generation = self._error_generation
                self._force_flush = True
                self._condition.notify_all()
                while target_ids.intersection(
                        row["operation_id"] for row in self._pending):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("自动保存刷新超时")
                    self._condition.wait(min(remaining, 0.5))
                    if self._error_generation > start_error_generation:
                        error = self._last_error or {}
                        raise AutosaveError(
                            "自动保存失败（%s）：%s" % (
                                error.get("phase", "unknown"),
                                error.get("message", "unknown error")))
        self.backend.flush()

    def replay_pending(self, timeout=30.0):
        """Force startup spool replay and return the remaining operation count."""
        self.flush(timeout=timeout)
        return self.stats()["pending"]

    def close(self, timeout=30.0):
        if self._closed:
            return
        error = None
        try:
            self.flush(timeout=timeout)
            self._save_periodic_checkpoints(force=True)
            self.backend.flush()
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            with self._condition:
                self._stop = True
                self._force_flush = True
                self._condition.notify_all()
            writer = self._writer
            if writer is not None:
                writer.join(timeout=max(0.1, float(timeout)))
                if writer.is_alive() and error is None:
                    error = TimeoutError("自动保存线程未能安全退出")
            self._closed = True
            if self.owns_backend:
                try:
                    self.backend.close()
                except Exception as exc:  # noqa: BLE001
                    if error is None:
                        error = exc
        if error is not None:
            raise error

    safe_stop = close

    def __getattr__(self, name):
        """Drain buffered state before invoking other TaskMemory operations."""
        target = getattr(self.backend, name)
        if not callable(target):
            return target

        def delegated(*args, **kwargs):
            self.flush()
            return target(*args, **kwargs)

        return delegated


def open_autosave_store(root=None, config=None, **kwargs):
    """Open TaskMemory wrapped in a started, owning autosave proxy."""
    import task_memory
    backend = task_memory.open_store(root=root, config=config)
    return TaskAutosave(
        backend, config=config, owns_backend=True, **kwargs)


__all__ = [
    "AutosaveError", "BUFFERED_METHODS", "TaskAutosave",
    "open_autosave_store",
]

# -*- coding: utf-8 -*-
"""Persistent, partitioned long-term memory for editing roles.

This database is deliberately separate from :mod:`task_memory`.  TaskMemory is
the recoverable journal for one active job; this module stores reusable lessons
only after their evidence and lifecycle have been made explicit.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import memory_config


LIFECYCLES = ("candidate", "validated", "active", "deprecated", "rejected")
SCOPES = ("global", "game", "project")
ROLES = ("analyst", "writer", "editor", "boundary", "director", "renderer")
STYLE_CATEGORIES = frozenset(("style", "style_habit", "editing_habit"))
FACT_CATEGORIES = frozenset(("fact", "hard_rule"))
SHARING_POLICIES = ("none", "facts_only", "validated", "all")
PROMOTION_POLICIES = ("manual", "validated_only", "automatic")
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    content_type TEXT NOT NULL,
    role TEXT NOT NULL,
    category TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    partition_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    source_task_id TEXT,
    source_plan_sha256 TEXT,
    confidence REAL NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    validation_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    activated_at TEXT,
    deprecated_at TEXT,
    CHECK (lifecycle IN ('candidate','validated','active','deprecated','rejected')),
    CHECK (scope_type IN ('global','game','project')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (observation_count >= 1),
    CHECK (validation_count >= 0),
    CHECK (success_count >= 0),
    CHECK (failure_count >= 0)
);
CREATE TABLE IF NOT EXISTS memory_sources (
    source_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    source_task_id TEXT,
    source_plan_sha256 TEXT,
    evidence_refs_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    passed INTEGER,
    auto_learning_eligible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE,
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);
CREATE TABLE IF NOT EXISTS memory_relations (
    source_memory_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_memory_id, target_memory_id, relation_type),
    FOREIGN KEY (source_memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE,
    FOREIGN KEY (target_memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE,
    CHECK (source_memory_id <> target_memory_id),
    CHECK (weight >= 0.0 AND weight <= 1.0)
);
CREATE TABLE IF NOT EXISTS memory_applications (
    application_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    source_plan_sha256 TEXT,
    outcome TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(memory_id, source_task_id),
    FOREIGN KEY (memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE,
    CHECK (outcome IN ('success','failure')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);
CREATE INDEX IF NOT EXISTS idx_memory_partition
    ON memories(mode, content_type, role, lifecycle);
CREATE INDEX IF NOT EXISTS idx_memory_scope
    ON memories(scope_type, scope_key, lifecycle);
CREATE INDEX IF NOT EXISTS idx_memory_category
    ON memories(category, lifecycle, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_sources_memory
    ON memory_sources(memory_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations(target_memory_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_memory_applications_memory
    ON memory_applications(memory_id, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _decode(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _token(value, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _TOKEN_RE.fullmatch(text):
        raise ValueError("非法%s：%s" % (label, value))
    return text


def _optional_id(value, label: str):
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError("非法%s：%s" % (label, value))
    return text


def _plan_sha(value):
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("计划 SHA-256 必须是 64 位十六进制")
    return text


def _confidence(value) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("记忆置信度必须在 0~1 之间")
    return number


def _refs(values) -> list[str]:
    return sorted(set(str(value).strip() for value in (values or [])
                      if str(value).strip()))


def _row(row: sqlite3.Row | None):
    if row is None:
        return None
    data = dict(row)
    data["content"] = _decode(data.pop("content_json"), {})
    data["evidence_refs"] = _decode(data.pop("evidence_refs_json"), [])
    return data


def _partition(mode, content_type, role, lifecycle):
    return "%s/%s/%s/%s" % (mode, content_type, role, lifecycle)


def _memory_identity(mode, content_type, role, category, scope_type,
                     scope_key, summary, content):
    canonical = _json({
        "mode": mode, "content_type": content_type, "role": role,
        "category": category, "scope_type": scope_type,
        "scope_key": scope_key, "summary": " ".join(summary.split()),
        "content": content,
    })
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest, "MEM-" + digest[:24]


class DirectorMemoryStore:
    """SQLite-backed catalog with lifecycle and cross-partition relations."""

    def __init__(self, root=None, config=None):
        self.config = dict(config or memory_config.load_config())
        base = (os.path.abspath(os.fspath(root)) if root is not None
                else memory_config.resolve_root(self.config))
        self.root = os.path.join(base, "long_term")
        self.backup_dir = os.path.join(self.root, "backups")
        self.db_path = os.path.join(self.root, "director_memory.sqlite")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._closed = False
        self._conn = sqlite3.connect(
            self.db_path, timeout=30.0, check_same_thread=False,
            isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        # sqlite3.executescript manages its own transaction boundary even when
        # isolation_level=None; run schema creation first, then use our normal
        # transaction wrapper for metadata writes.
        self._conn.executescript(SCHEMA)
        with self.transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','2')")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("导演长期记忆库已关闭")

    @contextlib.contextmanager
    def transaction(self):
        """Nested, rollback-safe transaction used by all compound mutations."""
        with self._lock:
            self._ensure_open()
            outer = self._transaction_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield self
            except Exception:
                self._transaction_depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._transaction_depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                self._conn.close()
                self._closed = True

    def flush(self):
        with self._lock:
            self._ensure_open()
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    @staticmethod
    def _lifecycle(passed, eligible, promotion_policy):
        policy = str(promotion_policy or "validated_only").strip().lower()
        if policy not in PROMOTION_POLICIES:
            raise ValueError("未知晋升策略：%s" % promotion_policy)
        if not (passed is True and eligible is True):
            return "candidate"
        if policy == "automatic":
            return "active"
        if policy == "validated_only":
            return "validated"
        return "candidate"

    def record_observation(self, *, mode, content_type, role, category,
                           summary, content, confidence,
                           evidence_refs=None, source_task_id=None,
                           source_plan_sha256=None, scope_type="global",
                           scope_key="", passed=None,
                           auto_learning_eligible=False,
                           promotion_policy=None, relation_ids=None) -> str:
        """Insert or merge one evidence-backed memory observation.

        Automatic promotion is fail-closed: without both a passed director
        review and ``auto_learning_eligible=True``, the memory remains a
        candidate regardless of the configured policy.
        """
        mode = _token(mode, "模式")
        content_type = _token(content_type, "内容类型")
        role = _token(role, "岗位")
        if role not in ROLES:
            raise ValueError("未知岗位：%s" % role)
        category = _token(category, "记忆类别")
        scope_type = _token(scope_type, "作用域")
        if scope_type not in SCOPES:
            raise ValueError("未知作用域：%s" % scope_type)
        scope_key = str(scope_key or "").strip()
        if scope_type != "global" and not scope_key:
            raise ValueError("游戏或项目作用域必须提供 scope_key")
        if scope_type == "global":
            scope_key = ""
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("记忆摘要不能为空")
        content_value = json.loads(_json(content))
        confidence = _confidence(confidence)
        evidence_refs = _refs(evidence_refs)
        source_task_id = _optional_id(source_task_id, "来源任务 ID")
        source_plan_sha256 = _plan_sha(source_plan_sha256)
        policy = (promotion_policy or self.config.get("candidate_promotion")
                  or "validated_only")
        lifecycle = self._lifecycle(
            passed, bool(auto_learning_eligible), policy)
        dedupe_key, memory_id = _memory_identity(
            mode, content_type, role, category, scope_type, scope_key,
            summary, content_value)
        now = _now()
        source_key = _json({
            "memory_id": memory_id, "task": source_task_id,
            "plan": source_plan_sha256, "refs": evidence_refs,
            "confidence": confidence, "passed": passed,
            "eligible": bool(auto_learning_eligible),
        })
        source_id = "SRC-" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:24]
        with self.transaction():
            # Stage replay after a crash must not reward or punish the same
            # observation twice.  The source row is the idempotency key.
            if self._conn.execute(
                    "SELECT 1 FROM memory_sources WHERE source_id=?", (source_id,),
            ).fetchone() is not None:
                return memory_id
            existing = self._conn.execute(
                "SELECT * FROM memories WHERE dedupe_key=?", (dedupe_key,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO memories(memory_id,dedupe_key,mode,content_type,role,category,"
                    "lifecycle,scope_type,scope_key,partition_key,summary,content_json,"
                    "evidence_refs_json,source_task_id,source_plan_sha256,confidence,"
                    "observation_count,validation_count,success_count,failure_count,"
                    "created_at,updated_at,activated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, dedupe_key, mode, content_type, role, category,
                     lifecycle, scope_type, scope_key,
                     _partition(mode, content_type, role, lifecycle), summary,
                     _json(content_value), _json(evidence_refs), source_task_id,
                     source_plan_sha256, confidence, 1,
                     1 if passed is not None else 0,
                     1 if passed is True else 0, 1 if passed is False else 0,
                     now, now, now if lifecycle == "active" else None))
            else:
                memory_id = existing["memory_id"]
                observations = int(existing["observation_count"]) + 1
                merged_confidence = ((float(existing["confidence"])
                                      * int(existing["observation_count"]) + confidence)
                                     / observations)
                merged_refs = _refs(
                    (_decode(existing["evidence_refs_json"], []) or []) + evidence_refs)
                current_lifecycle = existing["lifecycle"]
                rank = {"candidate": 0, "validated": 1, "active": 2,
                        "deprecated": -1, "rejected": -2}
                if current_lifecycle in ("deprecated", "rejected"):
                    merged_lifecycle = current_lifecycle
                else:
                    merged_lifecycle = (lifecycle if rank[lifecycle] > rank[current_lifecycle]
                                        else current_lifecycle)
                self._conn.execute(
                    "UPDATE memories SET lifecycle=?,partition_key=?,evidence_refs_json=?,"
                    "source_task_id=?,source_plan_sha256=?,confidence=?,observation_count=?,"
                    "validation_count=validation_count+?,success_count=success_count+?,"
                    "failure_count=failure_count+?,updated_at=?,"
                    "activated_at=COALESCE(activated_at,?) WHERE memory_id=?",
                    (merged_lifecycle,
                     _partition(mode, content_type, role, merged_lifecycle),
                     _json(merged_refs), source_task_id or existing["source_task_id"],
                     source_plan_sha256 or existing["source_plan_sha256"],
                     merged_confidence, observations,
                     1 if passed is not None else 0,
                     1 if passed is True else 0, 1 if passed is False else 0,
                     now, now if merged_lifecycle == "active" else None, memory_id))

            self._conn.execute(
                "INSERT INTO memory_sources(source_id,memory_id,source_task_id,"
                "source_plan_sha256,evidence_refs_json,confidence,passed,"
                "auto_learning_eligible,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (source_id, memory_id, source_task_id, source_plan_sha256,
                 _json(evidence_refs), confidence,
                 None if passed is None else int(bool(passed)),
                 int(bool(auto_learning_eligible)), now))
            for target_id in relation_ids or []:
                self.add_relation(memory_id, target_id, "supports", weight=confidence)
        return memory_id

    def get(self, memory_id: str):
        memory_id = _optional_id(memory_id, "记忆 ID")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM memories WHERE memory_id=?", (memory_id,),
            ).fetchone()
        return _row(row)

    def sources(self, memory_id: str) -> list[dict]:
        memory_id = _optional_id(memory_id, "记忆 ID")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_sources WHERE memory_id=? ORDER BY created_at,source_id",
                (memory_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence_refs"] = _decode(item.pop("evidence_refs_json"), [])
            result.append(item)
        return result

    def _reinforced_lifecycle(self, current, success_count, failure_count):
        """Move reusable lessons through a conservative evidence lifecycle."""
        total = max(0, int(success_count)) + max(0, int(failure_count))
        success_rate = (float(success_count) / total) if total else 0.0
        if current in ("deprecated", "rejected"):
            return current
        if failure_count >= 2 and success_rate < 0.5:
            return "rejected" if current == "candidate" else "deprecated"
        if current == "active":
            return current
        policy = str(self.config.get("candidate_promotion") or
                     "validated_only").strip().lower()
        if policy == "manual":
            return current
        active_successes = 2 if policy == "automatic" else 3
        validated_successes = 1 if policy == "automatic" else 2
        if success_count >= active_successes and success_rate >= 0.8:
            return "active"
        if success_count >= validated_successes and success_rate >= 0.75:
            return "validated"
        return current

    def reinforce(self, memory_ids, *, source_task_id, success,
                  source_plan_sha256=None, evidence_refs=None, metadata=None,
                  confidence=1.0) -> list[dict]:
        """Record one final-output reward or penalty per memory and task.

        Replaying the same task is idempotent.  If a resumed task changes from
        failure to success (or vice versa), the previous application outcome is
        replaced instead of being counted twice.
        """
        source_task_id = _optional_id(source_task_id, "来源任务 ID")
        if source_task_id is None:
            raise ValueError("强化学习必须提供来源任务 ID")
        source_plan_sha256 = _plan_sha(source_plan_sha256)
        evidence_refs = _refs(evidence_refs)
        metadata_value = json.loads(_json(metadata or {}))
        confidence = _confidence(confidence)
        outcome = "success" if bool(success) else "failure"
        ids = []
        for value in memory_ids or []:
            memory_id = _optional_id(value, "记忆 ID")
            if memory_id and memory_id not in ids:
                ids.append(memory_id)
        if not ids:
            return []

        now = _now()
        with self.transaction():
            for memory_id in ids:
                row = self._conn.execute(
                    "SELECT * FROM memories WHERE memory_id=?", (memory_id,),
                ).fetchone()
                if row is None:
                    continue
                application_id = "APP-" + hashlib.sha256(
                    (memory_id + "|" + source_task_id).encode("utf-8")
                ).hexdigest()[:24]
                prior = self._conn.execute(
                    "SELECT * FROM memory_applications WHERE memory_id=? AND source_task_id=?",
                    (memory_id, source_task_id),
                ).fetchone()
                if prior is not None and prior["outcome"] == outcome:
                    merged_refs = _refs(
                        (_decode(prior["evidence_refs_json"], []) or []) + evidence_refs)
                    merged_metadata = dict(_decode(prior["metadata_json"], {}) or {})
                    merged_metadata.update(metadata_value)
                    self._conn.execute(
                        "UPDATE memory_applications SET source_plan_sha256=COALESCE(?,source_plan_sha256),"
                        "evidence_refs_json=?,metadata_json=?,confidence=?,updated_at=? "
                        "WHERE application_id=?",
                        (source_plan_sha256, _json(merged_refs), _json(merged_metadata),
                         confidence, now, prior["application_id"]),
                    )
                    continue

                success_count = int(row["success_count"])
                failure_count = int(row["failure_count"])
                if prior is None:
                    if success:
                        success_count += 1
                    else:
                        failure_count += 1
                    self._conn.execute(
                        "INSERT INTO memory_applications(application_id,memory_id,source_task_id,"
                        "source_plan_sha256,outcome,evidence_refs_json,metadata_json,confidence,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (application_id, memory_id, source_task_id,
                         source_plan_sha256, outcome, _json(evidence_refs),
                         _json(metadata_value), confidence, now, now),
                    )
                else:
                    if prior["outcome"] == "success":
                        success_count = max(0, success_count - 1)
                    else:
                        failure_count = max(0, failure_count - 1)
                    if success:
                        success_count += 1
                    else:
                        failure_count += 1
                    self._conn.execute(
                        "UPDATE memory_applications SET source_plan_sha256=?,outcome=?,"
                        "evidence_refs_json=?,metadata_json=?,confidence=?,updated_at=? "
                        "WHERE application_id=?",
                        (source_plan_sha256, outcome, _json(evidence_refs),
                         _json(metadata_value), confidence, now, prior["application_id"]),
                    )

                lifecycle = self._reinforced_lifecycle(
                    row["lifecycle"], success_count, failure_count)
                old_confidence = float(row["confidence"])
                if success:
                    learned_confidence = old_confidence + (
                        0.08 * confidence * (1.0 - old_confidence))
                else:
                    learned_confidence = old_confidence * (1.0 - 0.25 * confidence)
                learned_confidence = max(0.0, min(1.0, learned_confidence))
                self._conn.execute(
                    "UPDATE memories SET lifecycle=?,partition_key=?,confidence=?,"
                    "validation_count=validation_count+1,success_count=?,failure_count=?,"
                    "updated_at=?,activated_at=CASE WHEN ?='active' THEN "
                    "COALESCE(activated_at,?) ELSE activated_at END,"
                    "deprecated_at=CASE WHEN ?='deprecated' THEN ? ELSE deprecated_at END "
                    "WHERE memory_id=?",
                    (lifecycle,
                     _partition(row["mode"], row["content_type"], row["role"], lifecycle),
                     learned_confidence, success_count, failure_count, now,
                     lifecycle, now, lifecycle, now, memory_id),
                )
        result = []
        for memory_id in ids:
            row = self.get(memory_id)
            if row is not None:
                result.append(row)
        return result

    def applications(self, memory_id: str) -> list[dict]:
        memory_id = _optional_id(memory_id, "记忆 ID")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT * FROM memory_applications WHERE memory_id=? "
                "ORDER BY created_at,application_id", (memory_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence_refs"] = _decode(item.pop("evidence_refs_json"), [])
            item["metadata"] = _decode(item.pop("metadata_json"), {})
            result.append(item)
        return result

    def transition_lifecycle(self, memory_id, lifecycle, *, manual=False,
                             passed=False, auto_learning_eligible=False):
        memory_id = _optional_id(memory_id, "记忆 ID")
        lifecycle = _token(lifecycle, "生命周期")
        if lifecycle not in LIFECYCLES:
            raise ValueError("未知生命周期：%s" % lifecycle)
        with self.transaction():
            row = self._conn.execute(
                "SELECT * FROM memories WHERE memory_id=?", (memory_id,),
            ).fetchone()
            if row is None:
                raise KeyError("记忆不存在：%s" % memory_id)
            current = row["lifecycle"]
            allowed = {
                "candidate": {"validated", "rejected"},
                "validated": {"active", "deprecated", "rejected"},
                "active": {"deprecated", "rejected"},
                "deprecated": {"validated", "rejected"},
                "rejected": set(),
            }
            if lifecycle != current and lifecycle not in allowed[current]:
                raise ValueError("不允许的记忆状态迁移：%s -> %s" % (current, lifecycle))
            if (not manual and lifecycle in ("validated", "active")
                    and not (passed is True and auto_learning_eligible is True)):
                raise PermissionError("自动晋升要求审片通过且允许自动学习")
            now = _now()
            self._conn.execute(
                "UPDATE memories SET lifecycle=?,partition_key=?,updated_at=?,"
                "activated_at=CASE WHEN ?='active' THEN COALESCE(activated_at,?) ELSE activated_at END,"
                "deprecated_at=CASE WHEN ?='deprecated' THEN ? ELSE deprecated_at END "
                "WHERE memory_id=?",
                (lifecycle, _partition(row["mode"], row["content_type"], row["role"], lifecycle),
                 now, lifecycle, now, lifecycle, now, memory_id))
        return self.get(memory_id)

    def add_relation(self, source_memory_id, target_memory_id, relation_type,
                     *, weight=1.0, metadata=None, bidirectional=False):
        source_memory_id = _optional_id(source_memory_id, "源记忆 ID")
        target_memory_id = _optional_id(target_memory_id, "目标记忆 ID")
        relation_type = _token(relation_type, "关系类型")
        weight = _confidence(weight)
        if source_memory_id == target_memory_id:
            raise ValueError("记忆不能关联自身")
        now = _now()
        with self.transaction():
            existing = self._conn.execute(
                "SELECT memory_id FROM memories WHERE memory_id IN (?,?)",
                (source_memory_id, target_memory_id),
            ).fetchall()
            if len(existing) != 2:
                raise KeyError("关系两端的记忆必须先存在")
            self._conn.execute(
                "INSERT INTO memory_relations(source_memory_id,target_memory_id,relation_type,"
                "weight,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source_memory_id,target_memory_id,relation_type) DO UPDATE SET "
                "weight=excluded.weight,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (source_memory_id, target_memory_id, relation_type, weight,
                 _json(metadata or {}), now, now))
            if bidirectional:
                self._conn.execute(
                    "INSERT INTO memory_relations(source_memory_id,target_memory_id,relation_type,"
                    "weight,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_memory_id,target_memory_id,relation_type) DO UPDATE SET "
                    "weight=excluded.weight,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (target_memory_id, source_memory_id, relation_type, weight,
                     _json(metadata or {}), now, now))

    def related(self, memory_id, relation_type=None, *, incoming=True, outgoing=True):
        memory_id = _optional_id(memory_id, "记忆 ID")
        relation_type = (_token(relation_type, "关系类型")
                         if relation_type is not None else None)
        clauses, params = [], []
        if outgoing:
            clauses.append("r.source_memory_id=?")
            params.append(memory_id)
        if incoming:
            clauses.append("r.target_memory_id=?")
            params.append(memory_id)
        if not clauses:
            return []
        sql = ("SELECT r.* FROM memory_relations r WHERE (" + " OR ".join(clauses) + ")")
        if relation_type:
            sql += " AND r.relation_type=?"
            params.append(relation_type)
        sql += " ORDER BY r.relation_type,r.source_memory_id,r.target_memory_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = _decode(item.pop("metadata_json"), {})
            other_id = (item["target_memory_id"]
                        if item["source_memory_id"] == memory_id
                        else item["source_memory_id"])
            item["memory"] = self.get(other_id)
            result.append(item)
        return result

    def query(self, *, mode, content_type, role=None, categories=None,
              lifecycles=("active",), project_id=None, game_id=None,
              cross_mode_sharing=None, limit=100) -> list[dict]:
        """Query the current partition plus explicitly permitted shared memory."""
        mode = _token(mode, "模式")
        content_type = _token(content_type, "内容类型")
        role = _token(role, "岗位") if role is not None else None
        categories = [_token(value, "记忆类别") for value in (categories or [])]
        lifecycles = tuple(_token(value, "生命周期") for value in lifecycles)
        if not lifecycles or any(value not in LIFECYCLES for value in lifecycles):
            raise ValueError("查询生命周期无效")
        sharing = str(cross_mode_sharing or self.config.get("cross_mode_sharing")
                      or "facts_only").strip().lower()
        if sharing not in SHARING_POLICIES:
            raise ValueError("未知跨模式共享策略：%s" % sharing)
        scope_clauses = ["scope_type='global'"]
        scope_params = []
        if game_id:
            scope_clauses.append("(scope_type='game' AND scope_key=?)")
            scope_params.append(str(game_id))
        if project_id:
            scope_clauses.append("(scope_type='project' AND scope_key=?)")
            scope_params.append(str(project_id))
        marks = ",".join("?" for _ in lifecycles)
        sql = ("SELECT * FROM memories WHERE content_type=? AND lifecycle IN (%s) "
               "AND (%s)" % (marks, " OR ".join(scope_clauses)))
        params = [content_type, *lifecycles, *scope_params]
        if role:
            sql += " AND role=?"
            params.append(role)
        if categories:
            sql += " AND category IN (%s)" % ",".join("?" for _ in categories)
            params.extend(categories)
        if sharing == "none":
            sql += " AND mode=?"
            params.append(mode)
        elif sharing == "facts_only":
            sql += (" AND (mode=? OR (category IN ('fact','hard_rule') "
                    "AND lifecycle IN ('validated','active')))" )
            params.append(mode)
        elif sharing == "validated":
            sql += (" AND (mode=? OR (category NOT IN ('style','style_habit','editing_habit') "
                    "AND lifecycle IN ('validated','active')))" )
            params.append(mode)
        else:  # all still keeps style habits mode-local.
            sql += (" AND (mode=? OR category NOT IN "
                    "('style','style_habit','editing_habit'))")
            params.append(mode)
        sql += " ORDER BY CASE lifecycle WHEN 'active' THEN 0 WHEN 'validated' THEN 1 ELSE 2 END, confidence DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(1000, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row(row) for row in rows]

    def integrity_check(self) -> dict:
        with self._lock:
            self._ensure_open()
            quick = [row[0] for row in self._conn.execute("PRAGMA quick_check").fetchall()]
            foreign = [tuple(row) for row in self._conn.execute(
                "PRAGMA foreign_key_check").fetchall()]
        return {"ok": quick == ["ok"] and not foreign,
                "quick_check": quick, "foreign_key_errors": foreign}

    def backup(self, destination=None) -> str:
        """Create a consistent SQLite backup and atomically publish it."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = os.path.abspath(os.fspath(
            destination or os.path.join(
                self.backup_dir, "director_memory-%s.sqlite" % stamp)))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".director_memory_backup.", suffix=".sqlite",
            dir=os.path.dirname(destination))
        os.close(fd)
        try:
            with self._lock:
                self._ensure_open()
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                target = sqlite3.connect(temp_path)
                try:
                    self._conn.backup(target)
                    target.commit()
                    check = target.execute("PRAGMA quick_check").fetchone()[0]
                    if check != "ok":
                        raise RuntimeError("长期记忆备份完整性检查失败：%s" % check)
                finally:
                    target.close()
            os.replace(temp_path, destination)
            return destination
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise


def open_store(root=None, config=None) -> DirectorMemoryStore:
    return DirectorMemoryStore(root=root, config=config)


__all__ = [
    "DirectorMemoryStore", "FACT_CATEGORIES", "LIFECYCLES", "ROLES",
    "SCOPES", "SHARING_POLICIES", "STYLE_CATEGORIES", "open_store",
]

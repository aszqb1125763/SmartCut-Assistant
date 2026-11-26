# -*- coding: utf-8 -*-
"""自动任务：一条命令跑完「分析 → 剧本 → 剪辑计划 → 转场 → 出片（字幕/BGM）」。

以前要分三步点：先分析索引，再点智能剪辑，再自己配 BGM。
现在：
    python autopilot.py "素材.mp4"                  # 出 15 分钟成片
    python autopilot.py "素材.mp4" --level heavy    # 重度
    python autopilot.py "素材目录" --no-bgm         # 不加 BGM

产物（都在素材旁边的「导出」目录）：
    <名字>_自动剪辑.mp4
    <名字>_字幕.srt / .ass
    <名字>_自动任务.md      ← 每个阶段的耗时、决策、警告
"""

from __future__ import annotations

try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass


import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import autocut                          # noqa: E402
import audio_evidence_cache             # noqa: E402
import bgm as bgm_mod                   # noqa: E402
import edit_plan                        # noqa: E402
import edit_modes                       # noqa: E402
import index as indexer                 # noqa: E402
import media_identity                   # noqa: E402
import workflow_engine                  # noqa: E402


class DirectorReviewFailed(RuntimeError):
    """The director retained unresolved evidence-backed review findings."""


class RenderedOutputReviewFailed(DirectorReviewFailed):
    """The actual rendered media failed the delivery review gate."""


def _out_dir(src: str) -> str:
    for base in (os.path.dirname(src), os.path.dirname(os.path.dirname(src))):
        if os.path.isdir(base):
            d = os.path.join(base, "导出")
            if not os.path.isdir(d) and os.path.isdir(os.path.join(base, "成片")):
                d = os.path.join(base, "成片")
            return d
    return os.path.join(os.path.dirname(src), "导出")


def _atomic_write_json(path, payload):
    """Durably replace a JSON report without exposing a partial file."""
    target = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(
        directory, ".%s.%s.tmp" % (os.path.basename(target), uuid.uuid4().hex))
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
    return target


class _RecordingMemory:
    """Record a new attempt without reusing prior completed stage states."""

    def __init__(self, backend):
        self.backend = backend

    def get_stage_state(self, _task_id, _stage):
        return None

    def record_stage_state(self, task_id, stage, status, **fields):
        return self.backend.record_stage_state(task_id, stage, status, **fields)


def _new_task_id(src):
    token = "%s|%s|%s" % (os.path.abspath(src), time.time_ns(), uuid.uuid4().hex)
    return "AUTO-%s" % hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]


def _run_signature(src, index_dir, mode, level, tracks, use_bgm,
                   burn_subs, loudness_norm, workflow, model_editor=False,
                   keep_ratio=None):
    """Fingerprint every user choice that can invalidate a resumed stage."""
    import cut_strength
    keep_ratio = cut_strength.normalize_ratio(
        keep_ratio, fallback=cut_strength.LEGACY_KEEP_RATIO.get(level))
    normalized_tracks = None
    if tracks is not None:
        values = tracks if isinstance(tracks, (list, tuple, set)) else [tracks]
        normalized_tracks = []
        for value in values:
            try:
                normalized_tracks.append(int(value))
            except (TypeError, ValueError):
                normalized_tracks.append(str(value))
    try:
        source_identity = media_identity.source_identity(src)
    except (OSError, TypeError, ValueError):
        # ``run_one`` will report a missing source in the normal stage path;
        # keep the signature helper deterministic for diagnostics/tests.
        source_identity = {
            "source_path": media_identity.normalized_path(src),
            "unavailable": True,
        }
    payload = {
        "source": os.path.abspath(src),
        "source_identity": source_identity,
        "index_dir": os.path.abspath(index_dir) if index_dir else None,
        "mode": str(mode), "level": str(level),
        "tracks": normalized_tracks, "use_bgm": bool(use_bgm),
        "burn_subs": bool(burn_subs),
        "loudness_norm": None if loudness_norm is None else bool(loudness_norm),
        "workflow": str(workflow), "artifact_versions": _WorkflowRun.ARTIFACTS,
        "model_editor": bool(model_editor),
        "keep_ratio": round(float(keep_ratio), 4),
    }
    from llm_gateway import backend_signature
    payload["ai_backend_signature"] = backend_signature()
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _WorkflowRun:
    ARTIFACTS = {
        "analyze": "INDEX-V2", "writer": "SCRIPT-V2", "editor": "CUT-V3",
        "boundary": "BOUNDARY-V2", "director": "REVIEW-V4",
        "transition": "TRANSITION-V3", "renderer": "RENDER-V3",
        "finalize": "FINAL-V2",
    }

    def __init__(self, src, level, use_bgm, index_dir, on_log,
                 force_reindex, tracks, output_dir, burn_subs,
                 loudness_norm, should_cancel, mode, task_id, memory=None,
                 on_progress=None, model_editor=False, keep_ratio=None):
        self.src = os.path.abspath(src)
        self.level = level
        self.keep_ratio = keep_ratio
        from llm_gateway import api_enabled
        self.model_editor = bool(model_editor) or api_enabled()
        # 导演审片没过时记在这里（默认降级出片，不再拦死整条流程）
        self._director_degraded = []
        # 成片复审没过时记在这里（默认"选最不极端的一版"出片并标记）
        self._render_degraded = []
        self.use_bgm = bool(use_bgm)
        self.force_reindex = bool(force_reindex)
        self.tracks = tracks
        self.burn_subs = bool(burn_subs)
        self.loudness_norm = loudness_norm
        self.should_cancel = should_cancel
        self.mode = edit_modes.normalize(mode)
        self.task_id = task_id
        self.memory = memory
        self.started = time.time()
        self.stages = []
        self.stem = os.path.splitext(os.path.basename(src))[0]
        self.out_dir = (os.path.abspath(output_dir) if output_dir
                        else (os.path.dirname(index_dir) if index_dir else _out_dir(src)))
        self.idx = index_dir or os.path.join(self.out_dir, self.stem + "_索引")
        os.makedirs(self.out_dir, exist_ok=True)
        self._sink = on_log
        self._progress_sink = on_progress
        self._audio_cache = None
        self._audio_cache_tracks = ()
        self._source_identity = None
        self._long_term_store = None
        self._long_term_disabled = False
        self._director_progress_state = None
        self._director_issue_ledger = None
        self._director_audio_review_cache = {}
        self._director_audio_lineage = []
        self._director_audio_anchor_serial = 0
        self._director_audio_attempts = {}
        self._director_audio_rounds = 0
        self._director_issue_orders = {}
        self._director_issue_saved_at = 0.0
        self._director_review_spans = []
        self._memory_context = []
        self._learning_guidance = {}
        self._applied_memory_ids = set()
        try:
            import memory_config
            self.director_conf = memory_config.load_config()
        except Exception:  # noqa: BLE001
            self.director_conf = {}

    def log(self, message):
        try:
            self._sink(str(message))
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            self._sink(str(message).encode(enc, errors="replace").decode(enc))

    def director_progress(self, total, resolved, round_no=0, message="",
                          reopened=0):
        total = max(0, int(total or 0))
        resolved = max(0, min(total, int(resolved or 0))) if total else 0
        remaining = max(0, total - resolved)
        fraction = (resolved / float(total)) if total else 0.0
        self._director_progress_state = {
            "total": total, "resolved": resolved, "fraction": fraction,
            "remaining": remaining, "reopened": int(reopened or 0),
        }
        text = "发现 %d 个问题，已解决 %d 个，剩余 %d 个" % (
            total, resolved, remaining)
        if round_no:
            text += " · 第 %d 次局部修复" % int(round_no)
        if reopened:
            text += " · 复发 %d 个" % int(reopened)
        if message:
            text += " · %s" % str(message)
        self.log("导演返工进度：" + text)
        if callable(self._progress_sink):
            self._progress_sink({
                "stage": "director", "total": total, "resolved": resolved,
                "remaining": remaining, "round": int(round_no or 0),
                "reopened": int(reopened or 0),
                "message": str(message or ""), "fraction": fraction,
                "task_id": self.task_id,
            })

    def issue_ledger(self):
        if getattr(self, "_director_issue_ledger", None) is None:
            from director_issue_ledger import DirectorIssueLedger
            snapshot = None
            path = os.path.join(self.idx, "director_issues", self.task_id + ".json")
            try:
                with open(audio_evidence_cache._cache_fs_path(path), encoding="utf-8") as handle:
                    candidate = json.load(handle)
                if candidate.get("task_id") == self.task_id:
                    snapshot = candidate
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                self.log("   导演问题台账不可恢复，重新复检：%s" % str(exc)[:100])
            self._director_issue_ledger = DirectorIssueLedger(snapshot)
            if snapshot:
                self._director_audio_lineage = snapshot.get("audio_lineage") or []
                self._director_audio_anchor_serial = int(snapshot.get("audio_anchor_serial") or 0)
                self._director_audio_attempts = dict(snapshot.get("audio_attempts") or {})
                self._director_audio_rounds = int(snapshot.get("repair_count") or 0)
                self._director_review_spans = self.spans(snapshot.get("spans"))
        return self._director_issue_ledger

    def observe_director_issues(self, scope, findings, spans, complete=True, coverage=None):
        """Reconcile one reviewed scope; another role's findings stay open."""
        self._director_review_spans = list(spans)
        return self.issue_ledger().observe(
            scope, findings, self.timeline_revision(spans), complete=complete, coverage=coverage,
            allow_revisit=True)

    def observe_text_issues(self, report, spans):
        rows = []
        for issue in (report or {}).get("issues") or []:
            row = dict(issue)
            row.setdefault("modality", "text")
            row.setdefault("suggested_interval", row.get("interval"))
            if row.get("type") in ("duplicate", "offtopic", "retake", "over_budget"):
                row.setdefault("issue_key", "text:group:" + str(row["type"]))
            rows.append(row)
        return self.observe_director_issues("text", rows, spans)

    def review_text_progress(self, report, spans):
        self.observe_text_issues(report, spans)
        self.report_director_issues(self._director_audio_rounds,
                                    "剧情复检；已通过位置不重复派工")

    def observe_audio_issues(self, reports, spans, complete=True):
        rows = []
        for report in reports or []:
            for item in report.get("findings") or []:
                row = dict(item)
                row.setdefault("modality", "audio")
                track = str(report.get("track_id") or row.get("track_id") or "")
                row["track_id"] = track
                anchor = report.get("anchor_key") or row.get("anchor_key")
                if not anchor:
                    # Legacy injected reports still need a stable local edge.
                    kind = report.get("review_kind") or row.get("action") or "audio"
                    index = report.get("span_index", row.get("span_index", 0))
                    anchor = "legacy:%s:%s" % (index, kind)
                row["anchor_key"] = str(anchor)
                row["issue_key"] = self.audio_issue_key(report, row)
                item.update({"anchor_key": str(anchor), "issue_key": row["issue_key"],
                             "track_id": track})
                rows.append(row)
        reviewed = {(str(row.get("review_window_id") or ""), str(row.get("track_id") or ""))
                    for row in reports or [] if row.get("actual_audio_inspected", True)}
        expected = getattr(self, "_director_audio_expected_pairs", set())
        coverage = None
        if expected:
            # Scope coverage includes optional content windows too. A sampled
            # window disappearing from the budget is not proof it was fixed.
            coverage = {"anchors": [], "issue_ids": []}
            for report in reports or []:
                if report.get("actual_audio_inspected", True) and report.get("anchor_key"):
                    coverage["anchors"].append({
                        "anchor_key": report["anchor_key"],
                        "track_id": str(report.get("track_id", "")),
                    })
            all_edges_reviewed = expected.issubset(reviewed)
            for pending in self.issue_ledger().pending("audio"):
                kind = str(pending.get("review_kind") or "")
                removed_edge = kind in ("cut_start", "cut_end") and all_edges_reviewed
                interval = pending.get("suggested_interval") or []
                removed_content = (kind not in ("cut_start", "cut_end") and len(interval) == 2
                                   and not any(min(float(interval[1]), b) >
                                               max(float(interval[0]), a) + 1e-6 for a, b in spans))
                if removed_edge or removed_content:
                    coverage["issue_ids"].append(pending["issue_id"])
        complete = bool(complete and reports)
        observed = self.observe_director_issues("audio", rows, spans, complete, coverage)
        ids = {row.get("issue_key"): row.get("issue_id") for row in observed}
        for report in reports or []:
            for row in report.get("findings") or []:
                row["issue_id"] = ids.get(row.get("issue_key"))
        return observed

    @staticmethod
    def audio_issue_key(report, row):
        if row.get("issue_key"):
            return row["issue_key"]
        anchor = report.get("anchor_key") or row.get("anchor_key")
        if not anchor:
            anchor = "legacy:%s:%s" % (
                report.get("span_index", row.get("span_index", 0)),
                report.get("review_kind") or row.get("action") or "audio")
        track = str(report.get("track_id") or row.get("track_id") or "")
        return "%s|%s|%s" % (anchor, track, row.get("issue_type") or row.get("type"))

    def report_director_issues(self, round_no=0, message="", force=False):
        ledger = self.issue_ledger()
        counts = ledger.counts()
        self.director_progress(counts["total"], counts["resolved"], round_no,
                               message, reopened=counts.get("reopened", 0))
        # A small atomic checkpoint once per second bounds IO and preserves
        # every completed repair pass. Final/failed/cancelled states force it.
        now = time.monotonic()
        if not force and now - getattr(self, "_director_issue_saved_at", 0.0) < 1.0:
            return counts
        self._director_issue_saved_at = now
        snapshot = ledger.snapshot()
        snapshot.update({
            "task_id": self.task_id, "repair_count": int(round_no),
            "spans": [[a, b] for a, b in self._director_review_spans],
            "message": str(message),
            "audio_lineage": self._director_audio_lineage,
            "audio_anchor_serial": self._director_audio_anchor_serial,
            "audio_attempts": self._director_audio_attempts,
        })
        try:
            path = os.path.join(self.idx, "director_issues", self.task_id + ".json")
            _atomic_write_json(audio_evidence_cache._cache_fs_path(path), snapshot)
            self._sync_director_issue_orders(snapshot)
        except Exception as exc:  # noqa: BLE001
            self.log("   导演问题台账保存失败：%s" % str(exc)[:100])
        return counts

    def _sync_director_issue_orders(self, snapshot):
        """Close each verified issue without waiting for the whole film."""
        creator = getattr(self.memory, "create_rework_order", None)
        resolver = getattr(self.memory, "resolve_rework_order", None)
        if not callable(creator) or not callable(resolver):
            return
        orders = self._director_issue_orders
        if not getattr(self, "_director_issue_orders_loaded", False):
            lister = getattr(self.memory, "list_rework_orders", None)
            if callable(lister):
                for order in lister(self.task_id, status="open"):
                    if order.get("operation") == "director_issue_rework":
                        orders[str(order["target_unit_id"])] = order["order_id"]
            self._director_issue_orders_loaded = True
        for entry in snapshot.get("entries") or []:
            issue_id = entry["issue_id"]
            pending = entry.get("status") != "resolved"
            existing = orders.get(issue_id)
            finding = entry.get("finding") or {}
            if pending and not existing:
                modality = finding.get("modality") or entry.get("scope")
                target = "boundary" if modality == "audio" else "editor"
                orders[issue_id] = creator(
                    self.task_id, "director", target,
                    operation="director_issue_rework",
                    reason=str(finding.get("message") or finding.get("type") or issue_id)[:800],
                    target_artifact_id=self.artifact("director"),
                    target_unit_id=issue_id)
            elif not pending and existing:
                resolver(existing, self.artifact("director"))
                del orders[issue_id]

    def audio_boundary_anchors(self, spans):
        """Carry cut identity through expansion, insertion, merges and reindexing."""
        old = getattr(self, "_director_audio_lineage", [])
        current = [{"span": (float(a), float(b)), "anchors": {}} for a, b in spans]
        for edge, kind in ((0, "cut_start"), (1, "cut_end")):
            candidates = []
            for index, row in enumerate(current):
                a, b = row["span"]
                for previous_index, previous in enumerate(old):
                    pa, pb = previous["span"]
                    if min(b, pb) > max(a, pa) + 1e-6:
                        candidates.append((abs(row["span"][edge] - previous["span"][edge]),
                                           index, previous_index))
            used_new, used_old = set(), set()
            for _distance, index, previous_index in sorted(candidates):
                if index in used_new or previous_index in used_old:
                    continue
                current[index]["anchors"][kind] = old[previous_index]["anchors"][kind]
                used_new.add(index)
                used_old.add(previous_index)
            for row in current:
                if kind not in row["anchors"]:
                    self._director_audio_anchor_serial += 1
                    row["anchors"][kind] = "edge:%d:%s" % (
                        self._director_audio_anchor_serial, kind)
        self._director_audio_lineage = current
        return [row["anchors"] for row in current]

    def done(self, name, started):
        seconds = round(time.time() - started, 1)
        self.stages.append((name, seconds))
        self.log("耗时 %s：%.1f 秒" % (name, seconds))

    def artifact(self, stage):
        return "%s.%s" % (self.task_id, self.ARTIFACTS[stage])

    def close(self):
        if getattr(self, "_director_issue_ledger", None) is not None:
            self.report_director_issues(
                getattr(self, "_director_audio_rounds", 0),
                "保存当前返工状态", force=True)
        cache, self._audio_cache = self._audio_cache, None
        if cache is not None:
            try:
                cache.close()
            except Exception:  # noqa: BLE001
                pass
        store, self._long_term_store = self._long_term_store, None
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    def long_term_store(self):
        if self._long_term_disabled or not self.director_conf.get("enabled", True):
            return None
        if self._long_term_store is None:
            try:
                import director_memory_store
                self._long_term_store = director_memory_store.open_store(
                    config=self.director_conf)
            except Exception as exc:  # noqa: BLE001
                self._long_term_disabled = True
                self.log("   导演长期记忆不可用，本次继续：%s" % str(exc)[:100])
                return None
        return self._long_term_store

    def memory_partition(self):
        content_type = "game_recording" if self.mode == "gameplay" else "general_video"
        parent = os.path.basename(os.path.dirname(os.path.dirname(self.src)))
        project_id = parent or os.path.splitext(os.path.basename(self.src))[0]
        return content_type, project_id

    def relevant_long_term_memory(self, limit=20):
        store = self.long_term_store()
        if store is None:
            return []
        content_type, project_id = self.memory_partition()
        try:
            return store.query(
                mode=self.mode, content_type=content_type,
                lifecycles=("active", "validated"), project_id=project_id,
                cross_mode_sharing=self.director_conf.get("cross_mode_sharing"),
                limit=limit)
        except Exception as exc:  # noqa: BLE001
            self.log("   导演长期记忆读取失败，本次不引用：%s" % str(exc)[:100])
            return []

    def compile_learning_guidance(self, memories):
        """Compile reusable memories into bounded, non-bypassable hints."""
        guidance = {"audio_guard_sec": 0.0, "roles": {}, "memory_ids": []}
        store = self.long_term_store()
        for row in memories or []:
            memory_id = str(row.get("memory_id") or "")
            role = str(row.get("role") or "")
            content = row.get("content") or {}
            if not memory_id or row.get("lifecycle") not in ("validated", "active"):
                continue
            guidance["roles"].setdefault(role, []).append(memory_id)
            policy = str(content.get("policy") or "")
            if role == "boundary" and policy == "expand_only_then_relisten":
                guards = []
                applications = getattr(store, "applications", None) if store else None
                if callable(applications):
                    try:
                        for application in applications(memory_id):
                            if application.get("outcome") != "success":
                                continue
                            value = (application.get("metadata") or {}).get(
                                "audio_guard_sec")
                            if value is not None:
                                guards.append(float(value))
                    except Exception:  # noqa: BLE001
                        guards = []
                # The guard only enlarges a boundary that current audio evidence
                # has already rejected.  It can never create a new edit or lower
                # a director veto threshold.
                learned = sorted(guards)[len(guards) // 2] if guards else 0.06
                guidance["audio_guard_sec"] = max(
                    guidance["audio_guard_sec"], max(0.03, min(0.18, learned)))
                guidance["memory_ids"].append(memory_id)
        guidance["memory_ids"] = sorted(set(guidance["memory_ids"]))
        return guidance

    def mark_memory_applied(self, role):
        ids = (self._learning_guidance.get("roles") or {}).get(role) or []
        self._applied_memory_ids.update(str(value) for value in ids if value)

    def guide_audio_finding(self, finding, duration):
        """Apply learned padding only to a currently rejected audio boundary."""
        row = dict(finding)
        guard = float(self._learning_guidance.get("audio_guard_sec") or 0.0)
        interval = row.get("suggested_interval") or []
        action = str(row.get("action") or "")
        if guard <= 0.0 or len(interval) != 2 or action not in (
                "expand_start", "expand_end"):
            return row
        try:
            start, end = map(float, interval)
        except (TypeError, ValueError):
            return row
        if action == "expand_start":
            start = max(0.0, start - guard)
        else:
            end = min(float(duration or end + guard), end + guard)
        row["suggested_interval"] = [start, end]
        row["learning_guard_sec"] = guard
        self.mark_memory_applied("boundary")
        return row

    def build_director_debrief(self, multimodal, modifications):
        """Summarize verified repair work before promoting any lesson."""
        findings = list(multimodal.get("findings") or [])
        serious = [row for row in findings
                   if row.get("severity") in ("critical", "major")
                   or row.get("veto")]
        audio_changes = [row for row in modifications
                         if row.get("source") == "audio_director"]
        text_changes = [row for row in modifications
                        if row.get("source") == "text_director"]
        transition_changes = [row for row in modifications
                              if row.get("source") == "multimodal_director"
                              and row.get("action") in (
                                  "apply_transition", "audio_crossfade",
                                  "transition_guard")]
        state = dict(self._director_progress_state or {})
        total = max(int(state.get("total") or 0), len(serious))
        resolved = max(0, min(total, int(state.get("resolved") or 0)))
        lessons = []
        if audio_changes and not any(
                row.get("modality") == "audio" and
                (row.get("veto") or row.get("severity") in ("critical", "major"))
                for row in findings):
            lessons.append({
                "role": "boundary", "result": "candidate",
                "rule": "只向外扩展问题切口，并在每轮后复听",
                "evidence": "最终听觉审核无否决",
                "actions": sorted(set(
                    change.get("action")
                    for row in audio_changes for change in row.get("changes") or []
                    if change.get("action"))),
            })
        if text_changes and not any(
                row.get("modality") == "text"
                and row.get("type") in ("abrupt_start", "abrupt_end")
                and row.get("severity") in ("critical", "major")
                for row in findings):
            lessons.append({
                "role": "editor", "result": "candidate",
                "rule": "同一接缝成组补回上下文，再重新审稿和复听",
                "evidence": "最终没有重大文字断句",
                "added": sum(len(row.get("added") or []) for row in text_changes),
                "dropped": sum(len(row.get("dropped") or []) for row in text_changes),
            })
        if transition_changes:
            lessons.append({
                "role": "renderer", "result": "task_only",
                "rule": "持续游戏声和普通画面跳变交由转场处理",
                "evidence": "仅保留为当前任务指令，不自动推广",
            })
        return {
            "schema": "director_debrief_v1",
            "outcome": "passed" if multimodal.get("passed") else "needs_rework",
            "score": (multimodal.get("scores") or {}).get("final"),
            "issues_discovered": total,
            "issues_resolved": resolved,
            "remaining_serious": len(serious),
            "repair_rounds": {
                "text": len(text_changes), "audio": len(audio_changes),
                "transition": len(transition_changes),
            },
            "lessons": lessons,
            "promotion_policy": (
                "导演通过后只形成候选；最终成片复审奖励或惩罚，"
                "至少两个独立任务成功才晋升可复用经验"),
        }

    def record_director_learning(self, multimodal, plan, modifications):
        """Persist only evidence-backed lessons from an approved review."""
        if (not self.director_conf.get("auto_learn", True)
                or multimodal.get("passed") is not True
                or multimodal.get("auto_learning_eligible") is not True):
            return []
        store = self.long_term_store()
        if store is None:
            return []
        content_type, project_id = self.memory_partition()
        scores = multimodal.get("scores") or {}
        confidence = max(0.0, min(1.0, min(
            float(scores.get("text") or 100.0),
            float(scores.get("audio") or 0.0)) / 100.0))
        refs = ["finding:%s" % row.get("id")
                for row in multimodal.get("findings") or [] if row.get("id")]
        refs.extend("review:%s" % row.get("id")
                    for row in multimodal.get("review_windows") or [] if row.get("id"))
        refs.extend(
            "repaired:%s" % change.get("finding_id")
            for row in modifications if row.get("source") == "audio_director"
            for change in row.get("changes") or [] if change.get("finding_id"))
        refs = sorted(set(refs))
        plan_sha = str(plan.get("plan_sha256") or "") or None
        ids = []
        try:
            transaction = getattr(store, "transaction", None)
            batch = transaction() if callable(transaction) else contextlib.nullcontext()
            with batch:
                ids = self._record_director_candidates(
                    store, multimodal, plan, modifications, content_type,
                    project_id, confidence, refs, plan_sha)
            store.flush()
        except Exception as exc:  # noqa: BLE001
            self.log("   导演长期记忆写入失败，本次成果不受影响：%s" % str(exc)[:120])
            return []
        return ids

    def _record_director_candidates(self, store, multimodal, plan, modifications,
                                    content_type, project_id, confidence, refs,
                                    plan_sha):
        """Write one atomic batch of task-derived candidate lessons."""
        ids = []
        try:
            director_id = None
            director_id = store.record_observation(
                mode=self.mode, content_type=content_type, role="director",
                category="fact", summary="多模态导演通过的证据组合",
                content={"policy": "text_audio_equal_visual_secondary"},
                confidence=confidence, evidence_refs=refs,
                source_task_id=self.task_id, source_plan_sha256=plan_sha,
                scope_type="project", scope_key=project_id,
                passed=None, auto_learning_eligible=False,
                promotion_policy="manual")
            ids.append(director_id)
            audio_rounds = [row for row in modifications
                            if row.get("source") == "audio_director"]
            unresolved_audio = any(
                row.get("modality") == "audio" and
                (row.get("veto") or row.get("severity") in ("critical", "major"))
                for row in multimodal.get("findings") or [])
            if audio_rounds and not unresolved_audio:
                boundary_id = store.record_observation(
                    mode=self.mode, content_type=content_type, role="boundary",
                    category="boundary_rule", summary="真实音频扩边后通过导演复审",
                    content={
                        "policy": "expand_only_then_relisten",
                    },
                    confidence=confidence, evidence_refs=refs,
                    source_task_id=self.task_id, source_plan_sha256=plan_sha,
                    scope_type="project", scope_key=project_id,
                    passed=None, auto_learning_eligible=False,
                    promotion_policy="manual")
                ids.append(boundary_id)
                if director_id and boundary_id != director_id:
                    store.add_relation(
                        director_id, boundary_id, "confirms", weight=confidence,
                        metadata={"task_id": self.task_id, "level": self.level})
            text_rounds = [row for row in modifications
                           if row.get("source") == "text_director"
                           and (row.get("added") or row.get("dropped"))]
            unresolved_text_boundary = any(
                row.get("modality") == "text"
                and row.get("type") in ("abrupt_start", "abrupt_end")
                and row.get("severity") in ("critical", "major")
                for row in multimodal.get("findings") or [])
            if text_rounds and not unresolved_text_boundary:
                editor_id = store.record_observation(
                    mode=self.mode, content_type=content_type, role="editor",
                    category="continuity_rule",
                    summary="成组修复文字接缝后通过视听复审",
                    content={
                        "policy": "group_same_seam_then_recheck_audio",
                    },
                    confidence=confidence, evidence_refs=refs,
                    source_task_id=self.task_id, source_plan_sha256=plan_sha,
                    scope_type="project", scope_key=project_id,
                    passed=None, auto_learning_eligible=False,
                    promotion_policy="manual")
                ids.append(editor_id)
                if director_id and editor_id != director_id:
                    store.add_relation(
                        director_id, editor_id, "confirms", weight=confidence,
                        metadata={"task_id": self.task_id, "level": self.level})
        except Exception:
            raise
        return ids

    @staticmethod
    def _learning_audio_guard(plan):
        deltas = []
        for row in (plan or {}).get("director_modifications") or []:
            if row.get("source") != "audio_director":
                continue
            for change in row.get("changes") or []:
                try:
                    deltas.append(abs(float(change["after"]) - float(change["before"])))
                except (KeyError, TypeError, ValueError):
                    continue
        if not deltas:
            return 0.06
        deltas.sort()
        return round(max(0.03, min(0.18, deltas[len(deltas) // 2] * 0.25)), 4)

    @staticmethod
    def _memory_matches_render_failure(memory, findings):
        role = str((memory or {}).get("role") or "")
        modalities = {str(row.get("modality") or "") for row in findings or []}
        types = {str(row.get("type") or "") for row in findings or []}
        if role == "director":
            return True
        if role == "boundary":
            return "audio" in modalities and any(
                "boundary" in kind or "word" in kind or "hot_audio" in kind
                for kind in types)
        if role == "editor":
            return "text" in modalities or any("timeline" in kind for kind in types)
        if role == "renderer":
            return bool(modalities.intersection({"visual", "container"})) or any(
                "transition" in kind or "level_jump" in kind for kind in types)
        return False

    def reinforce_director_learning(self, previous, rendered_qc, success):
        """Reward or penalize only lessons attributable to the final review."""
        audit = previous.get("director_multimodal_audit") or {}
        candidates = list(audit.get("learning_candidate_ids") or
                          audit.get("long_term_memory_ids") or [])
        applied = list(audit.get("applied_memory_ids") or [])
        memory_ids = list(dict.fromkeys(candidates + applied))
        store = self.long_term_store()
        reinforcer = getattr(store, "reinforce", None) if store else None
        if not memory_ids or not callable(reinforcer):
            return {"status": "skipped", "rewarded": 0, "penalized": 0,
                    "reason": "no_candidates_or_store"}
        findings = list((rendered_qc or {}).get("findings") or [])
        if not success:
            attributable = []
            for memory_id in memory_ids:
                try:
                    memory = store.get(memory_id)
                except Exception:  # noqa: BLE001
                    memory = None
                if self._memory_matches_render_failure(memory, findings):
                    attributable.append(memory_id)
            memory_ids = attributable
        if not memory_ids:
            return {"status": "skipped", "rewarded": 0, "penalized": 0,
                    "reason": "failure_not_attributable"}

        refs = ["render:%s" % row.get("id") for row in findings if row.get("id")]
        score = float((rendered_qc or {}).get("score") or 0.0)
        confidence = max(0.5, min(1.0, score / 100.0)) if success else 1.0
        rows = reinforcer(
            memory_ids, source_task_id=self.task_id, success=bool(success),
            source_plan_sha256=str(previous.get("plan_sha256") or "") or None,
            evidence_refs=refs,
            metadata={
                "rendered_score": score,
                "finding_types": sorted(set(
                    str(row.get("type") or "") for row in findings if row.get("type"))),
                "audio_guard_sec": self._learning_audio_guard(previous.get("plan") or {}),
            },
            confidence=confidence)
        store.flush()
        lifecycles = {}
        for row in rows or []:
            lifecycle = str((row or {}).get("lifecycle") or "unknown")
            lifecycles[lifecycle] = lifecycles.get(lifecycle, 0) + 1
        return {
            "status": "rewarded" if success else "penalized",
            "rewarded": len(memory_ids) if success else 0,
            "penalized": 0 if success else len(memory_ids),
            "memory_ids": memory_ids,
            "lifecycles": lifecycles,
        }

    def block_failed_rendered_review(self, previous, rendered_qc, report_path,
                                     message, phase="final_render"):
        """Persist failure evidence and negative learning before blocking.

        BGM is mixed into the delivery file in place.  Once its second review
        fails, swallowing an exception or writing the report afterwards would
        leave a modified file that looks deliverable but has no durable audit.
        This method is the single fail-closed exit: record attributable
        negative feedback, atomically replace the report, then raise the gate
        exception.  Report-write failure is itself converted to the same
        blocking exception and can never fall through to the generic
        "BGM skipped" path.
        """
        report = dict(rendered_qc or {})
        report["status"] = "needs_rework"
        report["delivery_blocked"] = True
        report["failure_phase"] = str(phase or "final_render")
        try:
            reinforcement = self.reinforce_director_learning(
                previous, report, False)
            if reinforcement.get("penalized"):
                self.log(
                    "   导演学习：最终成片复审失败，已对 %d 条相关经验记录负反馈"
                    % int(reinforcement["penalized"]))
        except Exception as exc:  # noqa: BLE001
            reinforcement = {
                "status": "error", "rewarded": 0, "penalized": 0,
                "reason": "%s: %s" % (type(exc).__name__, str(exc)[:160]),
            }
            self.log("   导演学习负反馈保存失败（仍阻断交付）：%s"
                     % str(exc)[:120])
        report["learning_reinforcement"] = reinforcement

        try:
            self.record_director_rework(
                {"findings": report.get("findings") or []},
                review_artifact_id=self.artifact("renderer"))
        except Exception as exc:  # noqa: BLE001
            report["rework_record_error"] = "%s: %s" % (
                type(exc).__name__, str(exc)[:160])
            self.log("   最终成片返工单保存失败（仍阻断交付）：%s"
                     % str(exc)[:120])

        try:
            _atomic_write_json(report_path, report)
        except Exception as exc:  # noqa: BLE001
            if self._strict_director():
                raise RenderedOutputReviewFailed(
                    "%s；失败报告原子写盘失败（%s），继续阻断交付"
                    % (message, str(exc)[:120])) from exc
            self.log("   失败报告写盘失败（%s），仍按降级出片" % str(exc)[:80])
        if self._strict_director():
            raise RenderedOutputReviewFailed(
                "%s，失败报告已原子保存：%s" % (message, report_path))
        # 默认：不完美时选"最不极端"的那一版照常交付，只把问题标出来
        # （子枫 2026-11-24：「不完美的地方选问题最不极端的进行出片，并标记问题」；
        #  规范 E04 原话：「全不完美时选最不极端，不是不处理」）
        report["delivery_blocked"] = False
        report["degraded_delivered"] = True
        # rendered_qc 是本方法的入参对象，调用方在后面还会用它覆盖写盘
        # （renderer 里 `_atomic_write_json(rendered_qc_path, rendered_qc)`），
        # 所以标记必须同时写进它，否则最终报告里看不到"降级交付"。
        if isinstance(rendered_qc, dict):
            rendered_qc.update(report)
        row = {"phase": str(phase or "final_render"), "message": str(message),
               "score": float(report.get("score") or 0.0),
               "veto": int(report.get("veto_count") or 0),
               "major": int(report.get("major_issues") or 0),
               "report": report_path}
        rows = list(getattr(self, "_render_degraded", None) or [])
        rows.append(row)
        self._render_degraded = rows
        try:
            _atomic_write_json(report_path, report)
        except Exception:  # noqa: BLE001
            pass
        self.log("   成片复审未通过（%s）：按降级照常出片，问题已标记在报告"
                 % message)
        return False

    def source_identity(self):
        if self._source_identity is None:
            self.log("   计划：计算素材 SHA-256（首次较慢，后续复用）…")
            self._source_identity = edit_plan.source_fingerprint(
                self.src, os.path.join(self.idx, "plans"))
        return dict(self._source_identity)

    def write_plan(self, stage, plan, artifact_type, source_stage=None):
        path = edit_plan.write_artifact(self.idx, stage, plan, self.task_id)
        register = getattr(self.memory, "register_artifact", None)
        getter = getattr(self.memory, "get_artifact", None)
        reviser = getattr(self.memory, "revise_artifact", None)
        if callable(register):
            artifact_id = self.artifact(stage)
            # TaskMemory 把转场和收尾归入渲染岗位；工作流节点名本身不是
            # 岗位名。这里保持与 task_memory.STAGE_ROLES 的公开契约一致。
            role = {"transition": "renderer", "finalize": "renderer"}.get(
                stage, workflow_engine.STAGES[stage].role)
            try:
                exists = getter(artifact_id) if callable(getter) else None
                if exists is None:
                    register(
                        self.task_id, role,
                        artifact_type, path, artifact_id=artifact_id,
                        source_artifact_id=(self.artifact(source_stage)
                                            if source_stage else None),
                        sha256=plan.get("plan_sha256"), activate=True)
                elif callable(reviser):
                    reviser(
                        artifact_id, path,
                        source_artifact_id=(self.artifact(source_stage)
                                            if source_stage else None),
                        sha256=plan.get("plan_sha256"), activate=True)
            except Exception as exc:  # noqa: BLE001
                self.log("   计划成果登记失败（文件已保留）：%s" % str(exc)[:100])
        return path

    def record_director_rework(self, multimodal, review_artifact_id=None):
        """Persist concise role handoff/rework records before blocking render."""
        findings = [dict(row) for row in (multimodal.get("findings") or [])
                    if row.get("severity") in ("critical", "major") or row.get("veto")]
        creator = getattr(self.memory, "create_rework_order", None)
        handoff = getattr(self.memory, "record_handoff", None)
        created = []
        if getattr(self, "_director_issue_ledger", None) is not None:
            findings = self.issue_ledger().pending()
            self._sync_director_issue_orders(self.issue_ledger().snapshot())
            created = list(self._director_issue_orders.values())
        elif callable(creator):
            grouped = {}
            for row in findings:
                modality = str(row.get("modality") or "text")
                grouped.setdefault(modality, []).append(row)
            for modality in sorted(grouped):
                rows = grouped[modality]
                target_role = "boundary" if modality == "audio" else "editor"
                interval = next((row.get("suggested_interval") for row in rows
                                 if row.get("suggested_interval")), None)
                reason = "；".join(str(row.get("message") or row.get("type") or "需返工")
                                  for row in rows[:4])
                try:
                    created.append(creator(
                        self.task_id, "director", target_role,
                        operation="director_%s_rework" % modality,
                        reason=reason[:800], target_artifact_id=review_artifact_id,
                        target_unit_id=("%.3f-%.3f" % tuple(interval)
                                        if interval and len(interval) == 2 else modality)))
                except Exception as exc:  # noqa: BLE001
                    self.log("   导演返工单保存失败：%s" % str(exc)[:100])
        if callable(handoff):
            try:
                handoff(
                    self.task_id, "director", "editor",
                    input_artifact_id=review_artifact_id,
                    summary="多模态导演未通过，禁止进入转场和渲染",
                    known_problems=[{
                        "id": row.get("issue_id") or row.get("id"), "modality": row.get("modality"),
                        "type": row.get("type"), "severity": row.get("severity"),
                        "interval": row.get("suggested_interval"),
                    } for row in findings[:24]],
                    required_actions=["只返工问题区间", "重新执行边界精修",
                                      "重新复听真实音频并检查关键画面"])
            except Exception as exc:  # noqa: BLE001
                self.log("   导演返工交接保存失败：%s" % str(exc)[:100])
        return created

    def _strict_director(self):
        """默认"审片不过也出片"；想严格（不过就不出）设 settings.director_strict=1。"""
        try:
            env = os.environ.get("SMARTCUT_DIRECTOR_STRICT")
            if env is not None:
                return str(env).strip().lower() in ("1", "true", "yes")
            import json as _json
            path = os.path.join(indexer.BASE_DIR, "conf", "settings.json")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    cfg = _json.load(handle) or {}
                return str(cfg.get("director_strict", "0")).strip().lower() \
                    in ("1", "true", "yes")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _mark_degraded(self, reason, detail):
        info = {"reason": reason, "detail": detail}
        rows = list(getattr(self, "_director_degraded", None) or [])
        rows.append(info)
        self._director_degraded = rows
        self.log("   导演未通过（%s）：按降级继续出片，问题留在返工台账" % detail)

    def require_director_approval(self, payload):
        """剧情模式：导演没过怎么办。

        默认 **降级出片**——软件的价值是出片，审片不过不该把整条流程拦死
        （子枫 2026-11-24：「我用软件就是为了出片的」）。问题照样记台账、
        写进成片报告；想回到"不过就不出"，设 conf\\settings.json 的
        director_strict=1（或环境变量 SMARTCUT_DIRECTOR_STRICT=1）。
        """
        if not bool(edit_modes.profile(self.mode).get("story_structure")):
            return True
        audit = payload.get("director_multimodal_audit") or {}
        strict = self._strict_director()
        if audit.get("status") == "skipped":
            if strict:
                raise DirectorReviewFailed("剧情模式缺少可执行的多模态导演审片")
            self._mark_degraded("audit_skipped", "导演审片没执行（skipped）")
            return False
        if audit.get("passed") is not True:
            scores = audit.get("scores") or {}
            detail = ("总分 %.1f，否决 %d，重大问题 %d"
                      % (float(scores.get("final") or 0.0),
                         int(audit.get("veto_count") or 0),
                         int(audit.get("major_issues") or 0)))
            if strict:
                raise DirectorReviewFailed(
                    "多模态导演未通过（%s），已停止出片并保留返工单" % detail)
            self._mark_degraded("not_passed", detail)
            return False
        reviewed_revision = audit.get("timeline_revision")
        finalized = bool((payload.get("cut_final") or {}).get("finalized"))
        if finalized and (not reviewed_revision or
                          audit.get("timeline_frozen") is not True):
            if strict:
                raise DirectorReviewFailed(
                    "导演审批缺少冻结时间轴 revision，必须重新执行三模态审片")
            self._mark_degraded("evidence_stale",
                                "导演审批缺少冻结时间轴 revision")
            return False
        if reviewed_revision:
            actual_revision = self.timeline_revision(
                self.spans(payload.get("spans") or []))
            claimed_revisions = [
                payload.get("timeline_revision"),
                (payload.get("cut_final") or {}).get("timeline_revision"),
            ]
            if (str(reviewed_revision) != actual_revision or
                    any(value and str(value) != str(reviewed_revision)
                        for value in claimed_revisions)):
                if strict:
                    raise DirectorReviewFailed(
                        "导演通过后时间轴又被修改；旧视听证据已失效，必须重新审片")
                self._mark_degraded(
                    "timeline_changed", "导演通过后时间轴又被修改（证据已失效）")
                return False

    @staticmethod
    def payload(ctx, stage):
        item = ctx.outputs.get(stage)
        return dict(item.payload) if item and isinstance(item.payload, dict) else {}

    @staticmethod
    def spans(rows):
        return [(float(a), float(b)) for a, b in (rows or [])]

    @staticmethod
    def timeline_revision(spans):
        """Stable identity for the exact cut timeline reviewed by the director."""
        rows = [[round(float(a), 6), round(float(b), 6)]
                for a, b in (spans or []) if float(b) > float(a)]
        encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()

    @staticmethod
    def words(data):
        words = []
        for segment in indexer.get_segment_evidence(data):
            segment_id = str(segment.get("segment_id") or "segment")
            tracks = segment.get("source_audio_tracks") or []
            for order, word in enumerate(segment.get("words") or [], 1):
                try:
                    start, end = float(word["start"]), float(word["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if end <= start:
                    continue
                row = dict(word)
                row.update({
                    "id": "%s.w%04d" % (segment_id, order),
                    "word": str(word.get("text", word.get("word", "")) or ""),
                    "start": start, "end": end, "required": True,
                    "source_audio_tracks": list(tracks),
                })
                if len(tracks) == 1:
                    row["source_track"] = tracks[0]
                words.append(row)
        return sorted(words, key=lambda row: (row["start"], row["end"], row["id"]))

    @staticmethod
    def confirmed_no_dialogue(data):
        """True only when ASR actually ran and found no transcribable speech."""
        return (data.get("asr_requested") is True
                and data.get("has_speech") is False
                and not (data.get("segments") or []))

    def selected_tracks(self, data):
        # 与最终渲染保持一致：用户明确选择时只审所选音轨；未选择时渲染会
        # 保留全部音轨，导演也必须听全部音轨，不能只听 ASR 使用的说话轨。
        raw = self.tracks
        if raw is None:
            raw = [stream.get("ordinal", index)
                   for index, stream in enumerate(data.get("audio_streams") or [])]
            if not raw:
                raw = data.get("audio_stream_used")
        if isinstance(raw, int):
            raw = [raw]
        if not raw:
            try:
                raw = autocut.auto_tracks(self.src)
            except Exception:  # noqa: BLE001
                raw = [0]
        count = len(data.get("audio_streams") or [])
        result = []
        for value in raw or []:
            try:
                track = int(value)
            except (TypeError, ValueError):
                continue
            if track < 0 or (count and track >= count) or track in result:
                continue
            result.append(track)
        return result or ([0] if data.get("audio_streams") else [])

    def speech_tracks(self, data, selected=None):
        selected = list(selected or self.selected_tracks(data))
        evidence = indexer.get_segment_evidence(data)
        found = []
        for segment in evidence:
            for track in segment.get("source_audio_tracks") or []:
                try:
                    track = int(track)
                except (TypeError, ValueError):
                    continue
                if track in selected and track not in found:
                    found.append(track)
        return found or selected[:1]

    def ensure_audio_cache(self, data):
        selected = tuple(self.selected_tracks(data))
        if not selected:
            return None
        if self._audio_cache is not None and selected == self._audio_cache_tracks:
            return self._audio_cache
        self.close()
        import audio_evidence_cache
        cache = audio_evidence_cache.AudioEvidenceCache(
            self.src, selected, ffmpeg=indexer.find_ffmpeg(),
            should_cancel=self.should_cancel)
        cache.prepare()
        self._audio_cache = cache
        self._audio_cache_tracks = selected
        return cache

    @staticmethod
    def visual_boundaries(data, spans):
        analysis = data.get("analysis") or {}
        times = analysis.get("times") or []
        diffs = analysis.get("diffs") or []
        pairs = []
        for time_value, diff in zip(times, diffs):
            try:
                pairs.append((float(time_value), max(0.0, float(diff))))
            except (TypeError, ValueError):
                continue
        max_diff = max((value for _time, value in pairs), default=1.0) or 1.0
        probes = []
        for start, end in spans:
            probes.extend((float(start), float(end)))
        rows = []
        for probe in probes:
            nearby = [(time_value, value) for time_value, value in pairs
                      if abs(time_value - probe) <= 0.65]
            if nearby:
                time_value, value = max(nearby, key=lambda item: (item[1], -abs(item[0] - probe)))
                rows.append({"time": time_value,
                             "confidence": min(1.0, 0.45 + 0.55 * value / max_diff),
                             "kind": "motion_change"})
        for run in analysis.get("runs") or []:
            if isinstance(run, (list, tuple)) and len(run) >= 2:
                rows.extend(({"time": float(run[0]), "confidence": 0.72,
                              "kind": "activity_run_start"},
                             {"time": float(run[1]), "confidence": 0.72,
                              "kind": "activity_run_end"}))
        unique = {}
        for row in rows:
            key = (round(float(row["time"]), 3), str(row["kind"]))
            if key not in unique or row["confidence"] > unique[key]["confidence"]:
                unique[key] = row
        return [unique[key] for key in sorted(unique)]

    def refine_boundaries(self, data, spans, task_seed):
        import boundary_refiner
        import audio_evidence_cache
        words = self.words(data)
        selected = self.selected_tracks(data)
        speech_tracks = set(self.speech_tracks(data, selected))
        primary, secondary = [], []
        cache = None
        audio_error = None
        try:
            cache = self.ensure_audio_cache(data)
            if cache is not None:
                for track in selected:
                    kind = "speech" if track in speech_tracks else "audio_event"
                    for start, end in spans:
                        rows = cache.activity_intervals(
                            track, max(0.0, start - 0.80), end + 0.95,
                            kind=kind)
                        (primary if track in speech_tracks else secondary).extend(rows)
        except audio_evidence_cache.AudioEvidenceCancelled as exc:
            raise workflow_engine.WorkflowCancelled(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            audio_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            self.log("   边界音频证据暂不可用，保留词级保护：%s" % audio_error)
        details = boundary_refiner.refine_spans(
            spans, words=words, primary_activity=primary,
            secondary_activity=secondary,
            visual_boundaries=self.visual_boundaries(data, spans),
            fps=float(data.get("fps") or 30.0),
            duration=float(data.get("duration_exact") or data.get("duration") or 0.0),
            task_seed=task_seed)
        refined = [tuple(row["final_interval"]) for row in details]
        return refined, details, {
            "words": len(words), "primary_activity": len(primary),
            "secondary_activity": len(secondary),
            "visual_boundaries": len(self.visual_boundaries(data, spans)),
            "audio_error": audio_error,
        }

    @staticmethod
    def apply_expansions(spans, intervals):
        rows = sorted([list(map(float, span)) for span in spans]
                      + [list(map(float, span)) for span in intervals],
                      key=lambda row: (row[0], row[1]))
        merged = []
        for start, end in rows:
            if end <= start:
                continue
            if merged and start <= merged[-1][1] + 0.03:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(row[0], row[1]) for row in merged]

    @staticmethod
    def frame_safe_spans(spans, fps, duration):
        """Align retained envelopes outward without moving either cut inward."""
        fps = max(1.0, float(fps or 30.0))
        duration = max(0.0, float(duration or 0.0))
        rows = []
        for start, end in spans:
            start = max(0.0, math.floor(float(start) * fps + 1e-9) / fps)
            end = math.ceil(float(end) * fps - 1e-9) / fps
            if duration:
                end = min(duration, end)
            if end > start:
                rows.append((round(start, 9), round(end, 9)))
        return _WorkflowRun.apply_expansions([], rows)

    @staticmethod
    def sentence_safe_spans(spans, segments, min_keep=0.3, duration=0.0,
                            tolerance=0.02):
        """把落在句子中间的刀口对齐到整句，避免「话说到一半就断」。

        为什么单独一道：词级收口只保证每个字的音完整，不保证这句话说完。
        刀口落在两个词之间（例如「完结|是做出真子」）时，字全是完整的，
        观众听到的却是「完结」然后跳走——实测一条素材有 11 处这种半句，
        成片复审一处都没报，只能靠人耳发现。

        按覆盖率判断导演本意（只动端点，不改「留哪句」的大方向）：
          · 这句话在 spans 里已经留下 ≥ min_keep → 认定想留，补全整句
          · 留下不足 min_keep → 认定想删，把残留的半句碎片挖掉

        返回 (新 spans, 统计)。扩出来的部分还要再过一遍词级收口。
        """
        rows = []
        for span in spans or []:
            try:
                start, end = float(span[0]), float(span[1])
            except (IndexError, TypeError, ValueError):
                continue
            if end > start:
                rows.append((start, end))
        sentences = []
        for segment in segments or []:
            try:
                start, end = float(segment[0]), float(segment[1])
            except (IndexError, TypeError, ValueError):
                continue
            if end - start > 0.05:
                sentences.append((start, end))
        stats = {"expanded": 0, "dropped": 0, "before": 0.0, "after": 0.0}
        if not rows or not sentences:
            return rows, stats

        def coverage(start, end):
            # 必须先把重叠区间合并再求和。
            # 切点重选会让两段出现重叠，直接 sum 会把重叠部分算两次，
            # 覆盖率虚高成 100% → 这里判“整句都在”跳过 → 成片复审报
            # “保留 72%”的半句（历史样本中出现过 4 条、0 分）。
            parts = []
            for left, right in rows:
                lo, hi = max(start, left), min(end, right)
                if hi > lo:
                    parts.append((lo, hi))
            parts.sort()
            merged = []
            for lo, hi in parts:
                if merged and lo <= merged[-1][1] + 0.001:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            return sum(hi - lo for lo, hi in merged)

        expansions = []
        drops = []
        for start, end in sentences:
            kept = coverage(start, end)
            if kept <= tolerance:
                continue
            if kept >= (end - start) - tolerance:
                continue                       # 整句都在，不用动
            if kept / (end - start) >= float(min_keep):
                expansions.append((start, end))
                stats["expanded"] += 1
            else:
                for left, right in rows:
                    overlap = min(end, right) - max(start, left)
                    if overlap > tolerance:
                        drops.append((max(start, left), min(end, right)))
                stats["dropped"] += 1

        stats["before"] = sum(right - left for left, right in rows)
        repaired = _WorkflowRun.apply_expansions([], expansions + list(rows))
        for drop_start, drop_end in sorted(drops):
            next_rows = []
            for left, right in repaired:
                if drop_end <= left + 1e-6 or drop_start >= right - 1e-6:
                    next_rows.append((left, right))
                    continue
                if drop_start > left + 0.05:
                    next_rows.append((left, min(right, drop_start)))
                if drop_end < right - 0.05:
                    next_rows.append((max(left, drop_end), right))
            repaired = next_rows
        limit = max(0.0, float(duration or 0.0))
        if limit:
            repaired = [(left, min(right, limit)) for left, right in repaired
                        if min(right, limit) > left]
        stats["after"] = sum(right - left for left, right in repaired)
        return repaired, stats

    @staticmethod
    def breath_pad_spans(spans, words, margin=0.28, gap_guard=0.06,
                         duration=0.0):
        """在刀口后面留一点气口，别切在字刚结束的那一瞬间。

        为什么需要：字级收口把刀口精确收到了词边界（误差 2 毫秒），字是完整了，
        但尾音（气息、混响、鼻腔收尾）没有时间衰减——听起来就是"咔"一下。
        子枫 404 处的示范片里，切点都落在词尾之后 0.25~0.37 秒的空隙里。

        规则（只往"空隙"里挪，绝不压到词上）：
          · 段尾：词后有空隙 → 往后延 min(margin, 空隙的一半)
          · 段首：词前有空隙 → 往前延 min(margin, 空隙的一半)
        两边都留一半空隙，是为了让相邻两个切点都能分到余量。
        """
        rows = []
        for span in spans or []:
            try:
                start, end = float(span[0]), float(span[1])
            except (IndexError, TypeError, ValueError):
                continue
            if end > start:
                rows.append([start, end])
        stamps = []
        for word in words or []:
            try:
                ws, we = float(word["start"]), float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if we > ws:
                stamps.append((ws, we))
        if not rows or not stamps:
            return [(a, b) for a, b in rows], []

        stamps.sort()
        starts = [row[0] for row in stamps]
        limit = max(0.0, float(duration or 0.0))
        changes = []
        import bisect

        def next_word_start(after):
            pos = bisect.bisect_right(starts, after - 1e-6)
            return stamps[pos][0] if pos < len(stamps) else float("inf")

        def prev_word_end(before):
            best = 0.0
            pos = bisect.bisect_left(starts, before + 1e-6) - 1
            while pos >= 0:
                if stamps[pos][1] <= before + 1e-6:
                    best = max(best, stamps[pos][1])
                    break
                pos -= 1
            return best

        for row in rows:
            old_start, old_end = row[0], row[1]

            # 段尾：最多往后挪 margin，且不压到下一个词。
            # 判断条件用"离最后一个词还差多少"，留够 margin 后重复跑不会再动。
            nxt = next_word_start(old_end)
            last_end = prev_word_end(old_end)
            if last_end > 0 and row[1] - last_end < float(margin) - 1e-6:
                target = last_end + float(margin)
                if nxt < float("inf"):
                    target = min(target, nxt - gap_guard)
                if limit:
                    target = min(target, limit)
                if target > row[1] + 1e-6:
                    row[1] = target

            # 段首：只在"紧接上一句"的短空隙里留气口（≤ margin+0.6 秒）。
            # 长空隙说明是跳到别的内容去了，那是天然分段，不必挪——
            # 否则会顺着空隙一路往前吞（实测吞过 60 秒）。
            before_end = prev_word_end(old_start)
            gap = old_start - before_end
            if 1e-6 < gap <= float(margin) + 0.6:
                target = before_end + float(margin)
                if old_start - target > 1e-6:
                    row[0] = max(0.0, target)

            if abs(row[0] - old_start) > 1e-6 or abs(row[1] - old_end) > 1e-6:
                changes.append((old_start, old_end, row[0], row[1]))
        return _WorkflowRun.apply_expansions([], rows), changes

    @staticmethod
    def word_safe_spans(spans, words, edge_margin=0.002, duration=0.0,
                        tolerance=0.025, max_rounds=4):
        """把落在词中间的刀口收到字边界上，避免成片把一句话的字切成半截。

        为什么还要单独一道：边界岗位只在 boundary 阶段护一次词
        （boundary_refiner），导演返工之后仍会增删句子
        （direct_text_rework / apply_text_findings），那条路径不经过护词，
        刀口就落在词里。实测一条 33 分钟素材，12 处切口把词切掉一半，
        最狠的一处只剩 0.013 秒，成片复审因此整片否决、片子交不出去。

        为什么收在字边界“内侧”而不是推到字外：连续说话的词挨着词，
        向外让 0.06 秒必然落进下一个词，等于把切字往后挪一个字。
        收到 ``词起点 + edge_margin`` / ``词终点 - edge_margin`` 既不会
        侵入相邻词，也落在复审容差（0.025 秒）之内，判定为完整保留。

        这里只微调端点，不改“留哪句、删哪句”的决定；重叠词会多迭代几轮，
        仍然互相打架时保留最后一次结果。
        """
        import bisect

        rows = []
        for span in spans or []:
            try:
                start, end = float(span[0]), float(span[1])
            except (IndexError, TypeError, ValueError):
                continue
            if end > start:
                rows.append([start, end])
        stamps = []
        for word in words or []:
            try:
                word_start, word_end = float(word["start"]), float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if word_end > word_start:
                stamps.append((word_start, word_end))
        if not rows or not stamps:
            return [(a, b) for a, b in rows], []

        stamps.sort()
        leading = [row[0] for row in stamps]
        limit = max(0.0, float(duration or 0.0))
        margin = max(0.0, float(edge_margin))
        tol = max(margin, float(tolerance))
        originals = [(row[0], row[1]) for row in rows]

        for _round in range(max(1, int(max_rounds))):
            moved = 0
            for index, row in enumerate(rows):
                # 起点落在词内部（且切掉的比容差多）→ 收到词起点内侧。
                # 已经贴在内侧 edge_margin 以内的不再动，避免反复来回。
                best = None
                for k in range(bisect.bisect_left(leading, row[0]) - 1, -1, -1):
                    word_start, word_end = stamps[k]
                    if row[0] - word_start > 5.0:
                        break
                    if row[0] - word_start > tol and word_end - row[0] > margin:
                        best = (word_start if best is None
                                else min(best, word_start))
                if best is not None:
                    row[0] = max(0.0, best + margin)
                    moved += 1

                # 终点落在词内部 → 收到词终点内侧；重叠词取最晚结束的那个。
                last = None
                for k in range(bisect.bisect_left(leading, row[1]) - 1, -1, -1):
                    word_start, word_end = stamps[k]
                    if row[1] - word_start > 5.0:
                        break
                    if row[1] - word_start > margin and word_end - row[1] > tol:
                        last = (word_end if last is None
                                else max(last, word_end))
                if last is not None:
                    candidate = last - margin
                    if limit:
                        candidate = min(limit, candidate)
                    if candidate > row[0]:
                        row[1] = candidate
                        moved += 1
                    else:
                        # 收口会把这一段收没（整段落在字里）：放弃这次调整。
                        row[0], row[1] = originals[index]
            if not moved:
                break

        merged = _WorkflowRun.apply_expansions([], rows)
        changes = []
        for (old_start, old_end), row in zip(originals, rows):
            if (abs(row[0] - old_start) > 1e-6 or
                    abs(row[1] - old_end) > 1e-6):
                changes.append((old_start, old_end, row[0], row[1]))
        return merged, changes

    def waveform_edge_spans(self, data, spans, words):
        """Use selected speech tracks to resolve final local waveform edges."""
        enabled = str(self.director_conf.get(
            "waveform_edge_snap_enabled", "1")).strip().lower()
        if enabled in ("0", "false", "no", "off"):
            return list(spans), [], []

        selected = self.selected_tracks(data)
        tracks = self.speech_tracks(data, selected)
        if not tracks:
            return list(spans), [], []
        cache = self.ensure_audio_cache(data)
        if cache is None:
            return list(spans), [], tracks

        import boundary_refiner
        search = max(0.20, min(1.50, float(self.director_conf.get(
            "waveform_edge_search_seconds", 0.80) or 0.80)))
        cfg = boundary_refiner.WaveformEdgeConfig(
            search_seconds=search,
            preroll_seconds=max(0.0, min(0.08, float(self.director_conf.get(
                "waveform_edge_preroll_seconds", 0.040) or 0.040))),
            postroll_seconds=max(0.0, min(0.10, float(self.director_conf.get(
                "waveform_edge_postroll_seconds", 0.070) or 0.070))),
        )
        limit = float(data.get("duration_exact") or
                      data.get("duration") or 0.0)
        activity = []
        windows = set()
        for start, end in spans or []:
            for boundary in (float(start), float(end)):
                lo = max(0.0, boundary - search)
                hi = min(limit, boundary + search) if limit else boundary + search
                if hi > lo:
                    windows.add((round(lo, 6), round(hi, 6)))
        for track in tracks:
            for lo, hi in sorted(windows):
                if self.should_cancel and self.should_cancel():
                    raise workflow_engine.WorkflowCancelled(
                        "task cancelled during waveform edge refinement")
                activity.extend(cache.activity_intervals(
                    track, lo, hi, kind="speech"))

        unique = {}
        for row in activity:
            key = (str(row.get("track") or ""),
                   round(float(row["start"]), 6),
                   round(float(row["end"]), 6))
            previous = unique.get(key)
            if previous is None or float(row.get("confidence") or 0.0) > float(
                    previous.get("confidence") or 0.0):
                unique[key] = row
        snapped, changes = boundary_refiner.snap_spans_to_activity_edges(
            spans, [unique[key] for key in sorted(unique)], words=words,
            duration=limit or None, config=cfg)
        return snapped, changes, tracks

    def close_timeline_boundaries(self, data, spans, words, logs,
                                  modifications):
        """Apply every deterministic boundary guard before director review.

        These operations can move cuts and therefore invalidate text, audio,
        and visual evidence.  Keeping them in one pre-review closure makes the
        reviewed revision identical to the revision handed to rendering.
        """
        limit = float(data.get("duration_exact") or
                      data.get("duration") or 0.0)
        before_revision = self.timeline_revision(spans)
        # 保留原来的“词 → 句”语义保护顺序：若刀口落在一个短词中间，先补全
        # 该词才能正确判断这句话是要保留还是丢弃。完成语义保护后，DSP 才在
        # 局部窗口内把刀口吸到真实波形起止，最后再做一次向外的词/帧保护。
        # 固定 0.28 秒气口不再覆盖真实音频边缘；防咔字职责由 offset 后的
        # 70ms postroll 接替，breath_pad_spans 只保留给旧调用方。
        spans, initial_word_snaps = self.word_safe_spans(
            spans, words, duration=limit)
        if initial_word_snaps:
            gained = sum((new_end - new_start) - (old_end - old_start)
                         for old_start, old_end, new_start, new_end
                         in initial_word_snaps)
            self.add_log(
                logs, "刀口收口：%d 处切口落在字中间，已收到字边界（成片 %+.1f 秒）"
                % (len(initial_word_snaps), gained))
            modifications.append({
                "source": "word_safe_boundary", "round": "final",
                "snapped": len(initial_word_snaps),
                "gained_seconds": round(gained, 3),
            })

        spans, sentence_stats = self.sentence_safe_spans(
            spans, data.get("segments") or [], duration=limit)
        if sentence_stats["expanded"] or sentence_stats["dropped"]:
            self.add_log(
                logs,
                "句级收口：补全 %d 句、去掉 %d 处半句碎片（成片 %+.1f 秒）"
                % (sentence_stats["expanded"], sentence_stats["dropped"],
                   sentence_stats["after"] - sentence_stats["before"]))
            modifications.append({
                "source": "sentence_safe_boundary", "round": "final",
                "expanded": sentence_stats["expanded"],
                "dropped": sentence_stats["dropped"],
                "gained_seconds": round(
                    sentence_stats["after"] - sentence_stats["before"], 3),
            })

        waveform_changes = []
        waveform_tracks = []
        waveform_error = None
        try:
            spans, waveform_changes, waveform_tracks = self.waveform_edge_spans(
                data, spans, words)
        except workflow_engine.WorkflowCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            waveform_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            self.add_log(logs, "波形边缘吸附跳过：%s" % waveform_error)
        if waveform_changes:
            def waveform_delta(row):
                # Current boundary_refiner rows are dictionaries.  Older
                # callers/tests used (old_start, old_end, new_start, new_end)
                # tuples; accepting both keeps the closure log compatible
                # without changing the delivered timeline.
                if isinstance(row, dict):
                    before = row.get("before") or ()
                    after = row.get("after") or ()
                elif isinstance(row, (list, tuple)) and len(row) >= 4:
                    before, after = row[:2], row[2:4]
                else:
                    return 0.0
                try:
                    return ((float(after[1]) - float(after[0])) -
                            (float(before[1]) - float(before[0])))
                except (IndexError, TypeError, ValueError):
                    return 0.0

            delta = sum(waveform_delta(row) for row in waveform_changes)
            self.add_log(
                logs, "波形边缘：%d 段已按%s的真实起音/尾音吸附（成片 %+.1f 秒）"
                % (len(waveform_changes),
                   "/".join("A%d" % (track + 1) for track in waveform_tracks),
                   delta))
            modifications.append({
                "source": "waveform_edge_boundary", "round": "final",
                "snapped": len(waveform_changes),
                "tracks": list(waveform_tracks),
                "gained_seconds": round(delta, 3),
            })

        spans, final_word_snaps = self.word_safe_spans(
            spans, words, duration=limit)
        if final_word_snaps:
            gained = sum((new_end - new_start) - (old_end - old_start)
                         for old_start, old_end, new_start, new_end
                         in final_word_snaps)
            self.add_log(
                logs, "刀口保护：%d 处波形边缘触到字内，已向外收到字边界（成片 %+.1f 秒）"
                % (len(final_word_snaps), gained))
            modifications.append({
                "source": "word_safe_boundary", "round": "final",
                "snapped": len(final_word_snaps),
                "gained_seconds": round(gained, 3),
            })

        frame_safe = self.frame_safe_spans(
            spans, float(data.get("fps") or 30.0), limit)
        frame_aligned = self.timeline_revision(frame_safe) != self.timeline_revision(spans)
        spans = frame_safe
        if frame_aligned:
            modifications.append({
                "source": "frame_safe_boundary", "round": "final",
                "aligned": len(spans),
            })

        revision = self.timeline_revision(spans)
        return spans, {
            "before_revision": before_revision,
            "timeline_revision": revision,
            "changed": revision != before_revision,
            "sentence": sentence_stats,
            "word_snaps": len(initial_word_snaps) + len(final_word_snaps),
            "waveform_snaps": len(waveform_changes),
            "waveform_tracks": list(waveform_tracks),
            "waveform_error": waveform_error,
            "frame_aligned": frame_aligned,
            "breath_pads": 0,
        }

    @staticmethod
    def apply_boundary_findings(spans, audio_reports, fps=30.0, duration=0.0):
        """Apply audio directions to the exact cut that produced each finding.

        Review windows carry a ``span_index``.  Keeping that identity prevents
        a small boundary suggestion from becoming a new, unrelated retained
        interval elsewhere on the timeline.
        """
        rows = [list(map(float, span)) for span in spans]
        applied = []
        for report in audio_reports or []:
            try:
                span_index = int(report.get("span_index"))
            except (TypeError, ValueError):
                span_index = -1
            if not 0 <= span_index < len(rows):
                kind = str(report.get("review_kind") or "")
                boundary_time = report.get("boundary_time")
                if boundary_time is None:
                    reviewed = report.get("span") or []
                    if len(reviewed) == 2:
                        boundary_time = reviewed[0] if kind == "cut_start" else reviewed[1]
                try:
                    boundary_time = float(boundary_time)
                except (TypeError, ValueError):
                    boundary_time = None
                if boundary_time is not None and rows:
                    edge = 0 if kind == "cut_start" else 1
                    span_index = min(
                        range(len(rows)),
                        key=lambda index: abs(rows[index][edge] - boundary_time))
                elif rows:
                    for candidate in report.get("findings") or []:
                        interval = candidate.get("suggested_interval") or []
                        action = candidate.get("action")
                        if len(interval) != 2 or action not in ("expand_start", "expand_end"):
                            continue
                        left, right = map(float, interval)
                        edge = 0 if action == "expand_start" else 1
                        touching = [
                            index for index, row in enumerate(rows)
                            if left - 0.06 <= row[edge] <= right + 0.06
                        ]
                        if touching:
                            span_index = min(
                                touching,
                                key=lambda index: min(
                                    abs(rows[index][edge] - left),
                                    abs(rows[index][edge] - right)))
                            break
            if not 0 <= span_index < len(rows):
                continue
            for finding in report.get("findings") or []:
                action = str(finding.get("action") or "")
                interval = finding.get("suggested_interval") or []
                if (action not in ("expand_start", "expand_end")
                        or len(interval) != 2
                        or finding.get("severity") not in ("critical", "major")):
                    continue
                try:
                    suggested_start, suggested_end = map(float, interval)
                except (TypeError, ValueError):
                    continue
                old_start, old_end = rows[span_index]
                if action == "expand_start":
                    # The evidence window must touch the current in-point.
                    if suggested_end < old_start - 0.06:
                        continue
                    new_start = min(old_start, suggested_start)
                    if new_start < old_start - 1e-4:
                        rows[span_index][0] = new_start
                        applied.append({
                            "span_index": span_index, "action": action,
                            "before": old_start, "after": new_start,
                            "finding_id": finding.get("id"),
                        })
                else:
                    # The evidence window must touch the current out-point.
                    if suggested_start > old_end + 0.06:
                        continue
                    new_end = max(old_end, suggested_end)
                    if new_end > old_end + 1e-4:
                        rows[span_index][1] = new_end
                        applied.append({
                            "span_index": span_index, "action": action,
                            "before": old_end, "after": new_end,
                            "finding_id": finding.get("id"),
                        })
        safe = _WorkflowRun.frame_safe_spans(rows, fps, duration)
        return safe, applied

    @staticmethod
    def settle_audio_reports(audio_reports, rework_rounds=0,
                             repair_attempts=None):
        """Turn persistent low-confidence ambience into transition guidance.

        A 0.72 waveform-only signal is useful enough to add a small guard once,
        but without a word reference it cannot permanently veto a gameplay
        edit.  Likewise, a non-speech tail that remains after local expansion
        is handed to the transition mixer instead of causing an infinite loop.
        """
        reports = copy.deepcopy(list(audio_reports or []))
        for report in reports:
            findings = []
            for finding in report.get("findings") or []:
                row = dict(finding)
                row.setdefault("issue_type", row.get("type"))
                refs = [str(value) for value in row.get("evidence_refs") or []]
                has_word_ref = (bool(row["boundary_word_refs"])
                                if "boundary_word_refs" in row else
                                bool(report.get("word_refs")) or any(
                                    value.startswith("s") and ".w" in value for value in refs))
                key = _WorkflowRun.audio_issue_key(report, row)
                repaired_count = (int(rework_rounds > 0) if repair_attempts is None else
                                  int(repair_attempts.get(key, 0)))
                repaired = repaired_count > 0
                low_confidence_speech = (
                    row.get("type") in ("speech_cut_start", "speech_cut_end")
                    and float(row.get("confidence") or 0.0) <= 0.72 + 1e-6
                    and not has_word_ref
                    and repaired
                )
                persistent_game_tail = (
                    row.get("type") == "important_audio_tail"
                    and (repaired if repair_attempts is None else repaired_count >= 2)
                )
                if low_confidence_speech:
                    row.update({
                        "type": "unconfirmed_boundary_activity",
                        "severity": "minor", "veto": False,
                        "action": "transition_guard", "learnable": False,
                        "message": "切口附近有持续声音但没有词级证据，已加保护并交由转场复核",
                    })
                elif persistent_game_tail:
                    row.update({
                        "severity": "minor", "veto": False,
                        "action": "audio_crossfade", "learnable": False,
                        "message": "游戏声音尾部已扩展保护，剩余连续声交由音频转场处理",
                    })
                findings.append(row)
            report["findings"] = findings
            report["score"] = round(max(0.0, 100.0 - sum(
                {"critical": 35.0, "major": 12.0, "minor": 4.0, "info": 0.0}
                .get(row.get("severity"), 12.0) for row in findings)), 3)
        return reports

    @staticmethod
    def prepare_visual_report(visual_report):
        """Convert ordinary hard jumps into explicit transition directions."""
        report = copy.deepcopy(visual_report) if visual_report else visual_report
        directives = []
        for node in (report or {}).get("nodes") or []:
            for issue in node.get("issues") or []:
                if (issue.get("type") == "hard_visual_jump"
                        and issue.get("action") == "inspect_transition"):
                    directives.append({
                        "node_id": node.get("id"),
                        "interval": list(node.get("sample_times") or []),
                        "operation": "choose_safe_transition",
                    })
                    issue.update({
                        "severity": "minor", "action": "apply_transition",
                        "message": "画面跳变已交由转场岗位选择硬切或短叠化",
                    })
        return report, directives

    @staticmethod
    def apply_text_findings(data, spans, issues, fps=30.0, duration=0.0,
                            level="medium", protected_indices=None):
        """Execute sentence-level director instructions on the local timeline."""
        import director_critic

        segments = data.get("segments") or []
        expansions = []
        drops = []
        changes = {"added": [], "dropped": [], "reasons": []}
        requested_additions = set()
        selected_before = director_critic.mask_from_spans(data, spans)
        labels = director_critic.classify_stages(data)
        scores = director_critic._scores_of(data)
        for issue in issues or []:
            if issue.get("severity") not in ("critical", "major"):
                continue
            indices = [int(value) for value in issue.get("indices") or []
                       if isinstance(value, int) and 0 <= value < len(segments)]
            action = str(issue.get("action") or "")
            if action in ("add_story_chain", "add_stage", "add_chapter") and indices:
                # These instructions identify sentences on the source timeline,
                # not a continuous interval between their first and last times.
                # A chapter may span thousands of seconds of omitted gameplay.
                if action == "add_story_chain":
                    additions = indices
                    reason = "补回章节目标/过程/结果/过渡"
                else:
                    weight = 0.16 if action == "add_stage" else 0.18
                    preferred = (("goal", "result", "closing")
                                 if action == "add_stage" else
                                 ("goal", "result", "explain"))
                    best = max(indices, key=lambda i: (
                        scores[i] + (weight if labels[i] in preferred else 0.0),
                        -i))
                    before = (1 if action == "add_chapter" or labels[best] in
                              ("goal", "explain", "result") else 0)
                    after = (1 if action == "add_chapter" or labels[best] in
                             ("goal", "result") else 0)
                    context = [False] * len(segments)
                    director_critic._add_context(
                        context, best, segments, before=before, after=after)
                    additions = [i for i, keep in enumerate(context) if keep]
                    reason = ("补回%s" % labels[best] if action == "add_stage"
                              else "补回章节关键节点")
                for index in additions:
                    row = segments[index]
                    if len(row) >= 2 and float(row[1]) > float(row[0]):
                        expansions.append((float(row[0]), float(row[1])))
                        requested_additions.add(index)
                        if not selected_before[index]:
                            changes["added"].append(index)
                changes["reasons"].append(reason)
            elif action in ("add_previous", "add_next") and indices:
                # A paired abrupt_start/abrupt_end often means both sentences
                # are already retained but the gameplay pause between them was
                # cut away.  Adding either sentence again is a no-op.  Retain
                # the complete local envelope so the two sides become one seam.
                first, last = indices[0], indices[-1]
                first_row, last_row = segments[first], segments[last]
                gap = float(last_row[0]) - float(first_row[1])
                if (first != last and gap <= float(
                        getattr(director_critic, "CONTEXT_MAX_GAP", 5.0))
                        and len(first_row) >= 2 and len(last_row) >= 2):
                    expansions.append((float(first_row[0]), float(last_row[1])))
                    changes["added"].extend(indices)
                    requested_additions.update(indices)
                    changes["reasons"].append("补回接缝上下文")
                else:
                    index = first if action == "add_previous" else last
                    row = segments[index]
                    if len(row) >= 2 and float(row[1]) > float(row[0]):
                        expansions.append((float(row[0]), float(row[1])))
                        changes["added"].append(index)
                        requested_additions.add(index)
                        changes["reasons"].append("修复文字承接")
            elif action == "drop" and issue.get("type") in (
                    "duplicate", "offtopic", "retake"):
                for index in indices:
                    row = segments[index]
                    if len(row) >= 2 and float(row[1]) > float(row[0]):
                        drops.append((index, float(row[0]), float(row[1])))
                if indices:
                    changes["reasons"].append("移除%s" % issue.get("type"))

        repaired = _WorkflowRun.apply_expansions(spans, expansions)
        story_indices = director_critic.genre.chapter_story_chain_indices(
            data, level=level)
        protected = director_critic._protected_indices(
            labels, director_critic.mask_from_spans(data, repaired), story_indices)
        protected.update(protected_indices or ())
        protected.update(requested_additions)
        for index, drop_start, drop_end in sorted(drops):
            if index in protected:
                continue
            changes["dropped"].append(index)
            next_rows = []
            for start, end in repaired:
                if drop_end <= start + 1e-6 or drop_start >= end - 1e-6:
                    next_rows.append((start, end))
                    continue
                if drop_start > start + 0.08:
                    next_rows.append((start, min(end, drop_start)))
                if drop_end < end - 0.08:
                    next_rows.append((max(start, drop_end), end))
            repaired = next_rows
        repaired = _WorkflowRun.frame_safe_spans(repaired, fps, duration)
        changes["added"] = sorted(set(changes["added"]))
        changes["dropped"] = sorted(set(changes["dropped"]))
        changes["reasons"] = sorted(set(changes["reasons"]))
        return repaired, changes

    @staticmethod
    def direct_text_rework(data, spans, report, level="medium",
                           target_seconds=None, fps=30.0, duration=0.0,
                           max_rounds=12, on_review=None):
        """Greedily execute one safe text instruction at a time."""
        import director_critic

        def serious(rows):
            return [row for row in rows or []
                    if row.get("severity") in ("critical", "major")
                    or row.get("veto")]

        structural_actions = {"add_story_chain", "add_chapter", "add_stage"}
        actionable = structural_actions | {"add_previous", "add_next", "drop"}

        def key(row):
            return (row.get("type"), row.get("action"),
                    tuple(row.get("indices") or []))

        current_spans = list(spans)
        current_report = dict(report or {})
        aggregate = {"added": [], "dropped": [], "reasons": []}
        history = []
        seen = {tuple((round(a, 6), round(b, 6)) for a, b in current_spans)}
        if callable(on_review):
            on_review(current_report, list(current_spans))
        for round_no in range(1, max(0, int(max_rounds)) + 1):
            current_serious = serious(current_report.get("issues"))
            current_issues = [row for row in current_serious
                              if row.get("action") in actionable]
            if not current_issues:
                break
            best = None
            for issue in current_issues:
                candidate_spans, changes = _WorkflowRun.apply_text_findings(
                    data, current_spans, [issue], fps, duration, level=level,
                    protected_indices=aggregate["added"])
                signature = tuple((round(a, 6), round(b, 6))
                                  for a, b in candidate_spans)
                if signature in seen or candidate_spans == current_spans:
                    continue
                candidate_report = director_critic.review(
                    data, candidate_spans, target_seconds=target_seconds,
                    level=level)
                candidate_issues = serious(candidate_report.get("issues"))
                candidate_keys = {key(row) for row in candidate_issues}
                removed_target = key(issue) not in candidate_keys
                issue_count_ok = (len(candidate_issues) < len(current_serious)
                                  or (removed_target and
                                      len(candidate_issues) <= len(current_serious)))
                critical_ok = (
                    sum(row.get("severity") == "critical" for row in candidate_issues)
                    <= sum(row.get("severity") == "critical" for row in current_serious))
                structure_ok = (
                    float(candidate_report.get("story_score") or 0.0)
                    >= float(current_report.get("story_score") or 0.0) - 1e-6
                    and float(candidate_report.get("chapter_score") or 0.0)
                    >= float(current_report.get("chapter_score") or 0.0) - 1e-6
                    and float(candidate_report.get("stage_score") or 0.0)
                    >= float(current_report.get("stage_score") or 0.0) - 1e-6
                    and float(candidate_report.get("continuity_score") or 0.0)
                    >= float(current_report.get("continuity_score") or 0.0) - 0.03
                    and float(candidate_report.get("relevance_score") or 0.0)
                    >= float(current_report.get("relevance_score") or 0.0) - 0.03
                    and float(candidate_report.get("score") or 0.0)
                    >= float(current_report.get("score") or 0.0) - 2.0
                )
                if not issue_count_ok or not critical_ok or not structure_ok:
                    continue
                rank = (0 if issue.get("action") in structural_actions else 1,
                        len(candidate_issues),
                        -float(candidate_report.get("score") or 0.0), signature)
                if best is None or rank < best[0]:
                    best = (rank, candidate_spans, candidate_report,
                            changes, issue, signature)
            if best is None:
                break
            _rank, current_spans, current_report, changes, issue, signature = best
            seen.add(signature)
            aggregate["added"].extend(changes.get("added") or [])
            aggregate["dropped"].extend(changes.get("dropped") or [])
            aggregate["reasons"].extend(changes.get("reasons") or [])
            history.append({
                "round": round_no, "accepted": True,
                "target": key(issue), "report": current_report,
                "changes": changes,
            })
            if callable(on_review):
                on_review(current_report, list(current_spans))
        for field in ("added", "dropped", "reasons"):
            aggregate[field] = sorted(set(aggregate[field]))
        return current_spans, current_report, aggregate, history

    def add_log(self, rows, message):
        message = str(message)
        rows.append(message)
        self.log("   " + message)

    def analyze(self, ctx):
        t0 = time.time()
        ctx.cancel_check()
        index_json = os.path.join(self.idx, "index.json")
        freshness = None
        rebuild = bool(self.force_reindex or not os.path.isfile(index_json))
        if not rebuild:
            freshness = indexer.index_freshness(
                index_json, self.src, index_path=index_json)
            rebuild = not bool(freshness.get("reusable"))
            if rebuild:
                self.log("① 素材分析员：旧索引已失效（%s），自动重建…"
                         % freshness.get("reason", "素材身份不匹配"))
        if rebuild:
            self.log("① 素材分析员：抽帧、语音识别、中立切片和标签…")
            try:
                import winproc
                proc = winproc.run_hidden(
                    winproc.script_cmd(os.path.join(BASE, "index.py"),
                                       [self.src, "--out", self.idx, "--step", "2"]),
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=BASE)
            except Exception:
                script = os.path.join(BASE, "index.py")
                command = ([sys.executable, "--run-script", script, self.src,
                            "--out", self.idx, "--step", "2"]
                           if getattr(sys, "frozen", False)
                           else [sys.executable, "-u", script, self.src,
                                 "--out", self.idx, "--step", "2"])
                proc = subprocess.run(command, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", cwd=BASE)
            ctx.cancel_check()
            for line in (proc.stdout or "").splitlines()[-6:]:
                self.log("   " + line)
            if proc.returncode != 0 or not os.path.isfile(index_json):
                raise RuntimeError("分析失败：%s" % (proc.stderr or "")[-200:])
        else:
            legacy = "（旧版身份规则）" if (freshness or {}).get("legacy") else ""
            self.log("① 素材分析员：已有中立索引且素材身份匹配%s，跳过分析：%s"
                     % (legacy, self.idx))
        data = autocut.load_index(self.idx)
        duration = float(data["duration"])
        self.log("   素材 %.1f 分钟 / %d 句台词" %
                 (duration / 60, len(data.get("segments") or [])))
        self.done("分析", t0)
        return workflow_engine.StageResult(
            self.artifact("analyze"),
            {"index_dir": self.idx, "duration": duration},
            {"artifact_type": "index", "neutral": True})

    def writer(self, ctx):
        t0 = time.time()
        previous = self.payload(ctx, "analyze")
        idx = previous.get("index_dir") or self.idx
        data = autocut.load_index(idx)
        story = bool(edit_modes.profile(self.mode).get("story_structure"))
        complete = bool(data.get("analysis") and data.get("segments"))
        built = False
        if story and complete:
            script = data.get("script") or {}
            import cut_strength
            import source_timeline
            import script_writer
            # Older resumed/test workflow objects may predate the explicit
            # keep_ratio field.  Preserve their script reuse semantics while
            # still giving rebuilt scripts a deterministic percentage.
            explicit_keep_ratio = getattr(self, "keep_ratio", None)
            writer_keep_ratio = cut_strength.normalize_ratio(
                explicit_keep_ratio,
                fallback=cut_strength.LEGACY_KEEP_RATIO.get(self.level, 0.75))
            source_events = script_writer._source_events(data)
            source_revision = source_timeline.revision(source_events)
            from llm_gateway import backend_signature
            mismatch = (str(script.get("cut_level") or "") != self.level
                        or (explicit_keep_ratio is not None
                            and script.get("keep_ratio") != writer_keep_ratio)
                        or str(script.get("mode") or "") != self.mode
                        or script.get("timeline_schema") != source_timeline.SCHEMA
                        or script.get("decision_space")
                        != "source_timeline_event_ids"
                        or script.get("backend_signature", "local") != backend_signature()
                        or script.get("timeline_revision") != source_revision)
            if not script.get("sections") or mismatch:
                self.log("② 编剧：按%s力度整理全片剧本…" % self.level)
                script_writer.build(
                    os.path.join(idx, "index.json"),
                    on_log=lambda m: self.log("   " + str(m)),
                    should_stop=self.should_cancel, level=self.level, mode=self.mode,
                    keep_ratio=writer_keep_ratio)
                built = True
            else:
                self.log("② 编剧：现有剧本已匹配%s力度" % self.level)
        elif story:
            self.log("② 编剧：索引不完整，后续使用兜底剧情策略")
        else:
            self.log("② 编剧：通用模式不强制生成剧情剧本")
        self.done("编剧", t0)
        return workflow_engine.StageResult(
            self.artifact("writer"), {"index_dir": idx},
            {"artifact_type": "script", "built": built,
             "mode": self.mode, "level": self.level})

    def editor(self, ctx):
        # 剪辑师换人：模型看图 + 听声 + 读台词，自己出剪辑单（model_editor.py）
        from llm_gateway import api_enabled
        if getattr(self, "model_editor", False) or api_enabled():
            self.model_editor = True
            import model_editor
            return model_editor.editor_by_model(self, ctx)
        t0 = time.time()
        idx = self.payload(ctx, "writer").get("index_dir") or self.idx
        data = autocut.load_index(idx)
        old_director = data.get("director_info") or {}
        from llm_gateway import backend_signature
        if (str(old_director.get("level") or "") != self.level
                or old_director.get("backend_signature", "local") != backend_signature()
                or str(old_director.get("mode") or "") != self.mode):
            data.pop("director", None)
        duration = float(data["duration"])
        conf = edit_modes.apply_to_conf(copy.deepcopy(autocut.load_conf()), self.mode)
        conf.setdefault("render", {})["scale"] = (
            conf.get("render", {}).get("scale") or "1920:-2")
        logs = []
        self.log("③ 剪辑师：%s / %s力度生成初剪" %
                 (edit_modes.label(self.mode), self.level))
        cuts = (autocut.detect_cuts_by_semantics(data, conf)
                or autocut.detect_cuts(data, conf)[0])
        candidates = autocut.keep_segments(cuts, duration)
        # 力度百分比（子枫 2026-11-23）：20~90%，数值越高成片越长。
        # 从 conf 里读（UI 滑块写进去的 keep_ratio），没设就走原来的三档。
        _kr = self.keep_ratio
        try:
            if _kr is None:
                _kr = (self.director_conf or {}).get("keep_ratio")
            # UI 的滑块写在 conf\settings.json（cards_ui.py save_settings），
            # 导演记忆那份配置里通常没有这个键——两处都要读，不能只读一边。
            if _kr is None:
                _set_path = os.path.join(indexer.BASE_DIR, "conf", "settings.json")
                if os.path.isfile(_set_path):
                    with open(_set_path, encoding="utf-8") as _set_f:
                        _kr = (json.load(_set_f) or {}).get("keep_ratio")
            # 命令行/API 调用方可以直接把 keep_ratio 放进 autocut.json
            if _kr is None:
                _kr = (conf or {}).get("keep_ratio")
            if _kr is not None:
                _kr = max(0.15, min(1.0, float(_kr)))
        except Exception:  # noqa: BLE001
            _kr = None
        if _kr is not None:
            self.log("   力度：保留 %.0f%%（目标约 %.0f 分钟）"
                     % (_kr * 100, (sum(float(b) - float(a) for a, b in candidates)
                                    * _kr) / 60))
        spans = autocut.apply_cut_level(
            dict(data), candidates, conf, self.level,
            on_log=lambda m: self.add_log(logs, m), keep_ratio=_kr)
        if not spans and self.confirmed_no_dialogue(data) and duration > 0:
            # 无对白素材没有可靠的文字语义可供删减。此时保留整条素材，后续
            # 由听觉和关键画面审稿；不能为了“有剪辑结果”而凭能量峰误删剧情。
            spans = [(0.0, duration)]
            self.add_log(logs, "确认无对白：保守保留全片，交由声音和关键画面导演审核")
        plan = edit_plan.build_plan(
            source=self.source_identity(), mode=self.mode, level=self.level,
            config=conf, semantic_spans=spans, versions=self.ARTIFACTS,
            evidence={"index_schema": data.get("schema") or {},
                      "editor": {"candidate_count": len(candidates)}})
        plan_path = self.write_plan("editor", plan, "cut_plan")
        self.done("初剪", t0)
        return workflow_engine.StageResult(
            self.artifact("editor"),
            {"index_dir": idx, "duration": duration, "conf": conf,
             "spans": [[a, b] for a, b in spans], "logs": logs,
             "plan": plan, "plan_path": plan_path,
             "plan_sha256": plan["plan_sha256"],
             "task_seed": plan["task_seed"]},
            {"artifact_type": "cut_plan", "segments": len(spans),
             "plan_sha256": plan["plan_sha256"]})

    def boundary(self, ctx):
        t0 = time.time()
        previous = self.payload(ctx, "editor")
        spans = self.spans(previous.get("spans"))
        idx = previous.get("index_dir") or self.idx
        data = autocut.load_index(idx)
        plan = dict(previous.get("plan") or {})
        self.log("④ 边界精修器：逐词时间 + 真实多音轨 + 画面变化保守定切口…")
        spans, details, summary = self.refine_boundaries(
            data, spans, previous.get("task_seed") or plan.get("task_seed"))
        plan = edit_plan.extend_plan(
            plan, refined_spans=spans,
            evidence={**dict(plan.get("evidence") or {}),
                      "boundary": summary})
        plan_path = self.write_plan(
            "boundary", plan, "boundary", source_stage="editor")
        previous.update({
            "spans": [[a, b] for a, b in spans],
            "boundary_details": details,
            "boundary_info": {"status": "refined", "version": 1, **summary},
            "plan": plan, "plan_path": plan_path,
            "plan_sha256": plan["plan_sha256"],
            "task_seed": plan["task_seed"],
        })
        self.log("   边界：%d 段，词证据 %d，主音轨活动 %d，其他音轨活动 %d"
                 % (len(spans), summary["words"], summary["primary_activity"],
                    summary["secondary_activity"]))
        self.done("边界精修", t0)
        return workflow_engine.StageResult(
            self.artifact("boundary"), previous,
            {"artifact_type": "boundary_plan", "passthrough": False,
             "segments": len(spans), "plan_sha256": plan["plan_sha256"]})

    def audio_review(self, data, spans, words):
        import director_multimodal
        import bisect
        cache = self.ensure_audio_cache(data)
        if cache is None:
            return [], []
        max_windows = int(self.director_conf.get("director_max_review_windows", 28) or 28)
        windows = director_multimodal.select_review_windows(
            data, spans, words=words, max_windows=max_windows)
        # Previously flagged optional content stays in the next review even
        # when a new low-confidence word takes its place in the sample budget.
        for pending in self.issue_ledger().pending("audio"):
            kind = str(pending.get("review_kind") or "")
            if kind in ("cut_start", "cut_end") or pending.get("boundary_time") is None:
                continue
            at = float(pending["boundary_time"])
            owner = next((i for i, (a, b) in enumerate(spans) if a <= at < b), None)
            if owner is None or any(row["kind"] == kind and
                                    abs(float(row["time"]) - at) < 1e-6 for row in windows):
                continue
            a, b = spans[owner]
            windows.append({
                "id": "review_pending_%d" % len(windows), "kind": kind,
                "time": at, "interval": [max(0.0, at - 0.8),
                                            min(float(data.get("duration") or b), at + 0.8)],
                "target_interval": [max(a, at - 0.3), min(b, at + 0.3)],
                "span_index": owner, "evidence_refs": pending.get("window_evidence_refs") or [],
                "anchor_key": pending.get("anchor_key"), "revisit_pending": True,
            })
        selected = self.selected_tracks(data)
        for window in windows:
            window["expected_audio_tracks"] = [str(track) for track in selected]
        self._director_audio_expected_pairs = {
            (str(window["id"]), str(track)) for window in windows
            if window.get("kind") in ("cut_start", "cut_end") for track in selected}
        speech_tracks = set(self.speech_tracks(data, selected))
        anchors = self.audio_boundary_anchors(spans)
        word_rows = director_multimodal.normalize_words(words)
        word_starts = [row["start"] for row in word_rows]
        max_word_length = max((row["end"] - row["start"] for row in word_rows), default=0.0)
        word_signature = hashlib.sha256(json.dumps(
            word_rows, sort_keys=True, ensure_ascii=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        review_cache = self._director_audio_review_cache
        reused, inspected = 0, 0
        reports = []
        for window in windows:
            if self.should_cancel and self.should_cancel():
                raise workflow_engine.WorkflowCancelled(
                    "task cancelled during director audio review")
            kind = window["kind"]
            time_value = float(window["time"])
            load_start, load_end = map(float, window["interval"])
            index = window.get("span_index")
            if kind in ("cut_start", "cut_end") and isinstance(index, int) and 0 <= index < len(anchors):
                anchor = anchors[index][kind]
            else:
                anchor = window.get("anchor_key") or "%s:%.6f:%s" % (
                    kind, time_value, ",".join(sorted(window.get("evidence_refs") or [])))
            window["anchor_key"] = anchor
            nearby_words = word_rows[
                bisect.bisect_left(word_starts, load_start - max_word_length):
                bisect.bisect_right(word_starts, load_end)]
            for track in selected:
                role = "speech" if track in speech_tracks else "game"
                if kind == "cut_start":
                    target = (time_value, min(load_end, time_value + 0.24))
                    flags = (True, False, False)
                elif kind == "cut_end":
                    target = (max(load_start, time_value - 0.24), time_value)
                    flags = (False, True, False)
                else:
                    target = tuple(window.get("target_interval") or (
                        max(load_start, time_value - 0.30),
                        min(load_end, time_value + 0.30)))
                    flags = (False, False, True)
                key = (id(cache), str(track), role, kind, target,
                       load_start, load_end, word_signature)
                if key in review_cache:
                    report = copy.deepcopy(review_cache[key])
                    reused += 1
                else:
                    samples = cache.review_samples(track, load_start, load_end)
                    if samples.size == 0:
                        continue
                    report = director_multimodal.analyze_audio_span(
                        samples, cache.sample_rate, target, words=nearby_words,
                        track_id=str(track), role=role, time_offset=load_start,
                        check_start=flags[0], check_end=flags[1],
                        check_content=flags[2])
                    review_cache[key] = copy.deepcopy(report)
                    inspected += 1
                report["review_window_id"] = window["id"]
                report["anchor_key"] = anchor
                report["review_kind"] = kind
                report["span_index"] = window.get("span_index")
                report["boundary_time"] = time_value
                for finding in report.get("findings") or []:
                    finding["span_index"] = window.get("span_index")
                    finding["review_kind"] = kind
                    finding["boundary_time"] = time_value
                    finding["anchor_key"] = anchor
                    finding["window_evidence_refs"] = list(window.get("evidence_refs") or [])
                reports.append(report)
        self._director_audio_review_stats = {
            "inspected": inspected, "reused": reused, "reports": len(reports),
        }
        self.log("   导演局部复听：新检查 %d 个音轨窗口，复用 %d 个未改动窗口" %
                 (inspected, reused))
        return windows, reports

    def direct_audio_rework(self, data, spans, words, progress_total=0,
                            round_offset=0):
        """Relisten and repair exact audio boundaries until they stabilize."""
        windows = []
        audio_reports = []
        audio_error = None
        modifications = []
        if not bool(self.director_conf.get("audio_review_enabled", True)):
            return (list(spans), windows, audio_reports, audio_error, 0,
                    modifications, progress_total)
        try:
            windows, audio_reports = self.audio_review(data, spans, words)
        except workflow_engine.WorkflowCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            audio_error = "%s: %s" % (type(exc).__name__, str(exc)[:180])
            self.log("   导演听觉证据失败：%s" % audio_error)
            return (list(spans), windows, audio_reports, audio_error, 0,
                    modifications, progress_total)

        max_audio_rounds = int(self.director_conf.get(
            "director_audio_rework_rounds", 12) or 12)
        max_audio_rounds = max(1, min(12, max_audio_rounds))
        audio_rounds_used = 0
        current_spans = list(spans)
        audio_reports = self.settle_audio_reports(
            audio_reports, repair_attempts=self._director_audio_attempts)
        self.observe_audio_issues(audio_reports, current_spans)
        self.report_director_issues(self._director_audio_rounds, "核对当前听觉问题")
        # Attempts follow a stable issue across all director phases. Calling
        # this helper again must not replenish a failed issue's repair budget.
        attempts = self._director_audio_attempts
        max_issue_attempts = 3
        seen_spans = {self.timeline_revision(current_spans)}
        for audio_round in range(1, max_audio_rounds + 1):
            actionable_reports = []
            actionable_count = 0
            for report in audio_reports:
                copied = dict(report)
                copied["findings"] = []
                for finding in report.get("findings") or []:
                    if not (finding.get("severity") in ("critical", "major")
                            and finding.get("action") in ("expand_start", "expand_end")
                            and finding.get("suggested_interval")):
                        continue
                    refs = [str(value) for value in finding.get("evidence_refs") or []]
                    has_word_ref = (bool(finding["boundary_word_refs"])
                                    if "boundary_word_refs" in finding else
                                    bool(report.get("word_refs")) or any(
                                        value.startswith("s") and ".w" in value for value in refs))
                    issue_key = finding.get("issue_key") or finding.get("id")
                    used = int(attempts.get(issue_key, 0))
                    low_unconfirmed = (
                        finding.get("type") in ("speech_cut_start", "speech_cut_end")
                        and float(finding.get("confidence") or 0.0) <= 0.72 + 1e-6
                        and not has_word_ref
                    )
                    if low_unconfirmed and used >= 1:
                        continue
                    if (finding.get("type") == "important_audio_tail"
                            and used >= 2):
                        continue
                    if used >= max_issue_attempts:
                        continue
                    copied["findings"].append(self.guide_audio_finding(
                        finding, float(data.get("duration_exact") or
                                       data.get("duration") or 0.0)))
                    actionable_count += 1
                if copied["findings"]:
                    actionable_reports.append(copied)
            if not actionable_reports:
                break
            before = list(current_spans)
            requested, applied = self.apply_boundary_findings(
                current_spans, actionable_reports,
                float(data.get("fps") or 30.0),
                float(data.get("duration_exact") or data.get("duration") or 0.0))
            signature = self.timeline_revision(requested)
            if not applied or signature in seen_spans or requested == before:
                break
            seen_spans.add(signature)
            current_spans = requested
            applied_keys = {change.get("finding_id") for change in applied}
            for report in actionable_reports:
                for finding in report["findings"]:
                    if finding.get("id") in applied_keys:
                        key = finding.get("issue_key") or finding.get("id")
                        attempts[key] = attempts.get(key, 0) + 1
            modifications.append({
                "source": "audio_director",
                "operation": "direct_boundary_repair",
                "round": self._director_audio_rounds + 1,
                "changes": applied,
            })
            audio_rounds_used = audio_round
            self._director_audio_rounds += 1
            try:
                windows, audio_reports = self.audio_review(
                    data, current_spans, words)
            except workflow_engine.WorkflowCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                audio_error = "%s: %s" % (type(exc).__name__, str(exc)[:180])
                # The old report belongs to the previous revision. Keep its
                # issues open, but never present it as review of the new cut.
                self._director_review_spans = list(current_spans)
                audio_reports = []
                break
            self.observe_audio_issues(audio_reports, current_spans)
            self.report_director_issues(
                self._director_audio_rounds,
                "定点修复后复检；只继续处理未解决位置")

        audio_reports = self.settle_audio_reports(
            audio_reports, rework_rounds=audio_rounds_used,
            repair_attempts=attempts)
        self.observe_audio_issues(audio_reports, current_spans, complete=not audio_error)
        counts = self.report_director_issues(
            self._director_audio_rounds,
            "本轮听觉复检完成" if not audio_error else "复听失败，保留未解决问题",
            force=True)
        progress_total = counts["total"]
        return (current_spans, windows, audio_reports, audio_error,
                audio_rounds_used, modifications, progress_total)

    def director(self, ctx):
        t0 = time.time()
        self.issue_ledger()
        previous = self.payload(ctx, "boundary")
        idx = previous.get("index_dir") or self.idx
        data = autocut.load_index(idx)
        spans = self.spans(previous.get("spans"))
        logs = list(previous.get("logs") or [])
        story = bool(edit_modes.profile(self.mode).get("story_structure"))
        text_applicable = not self.confirmed_no_dialogue(data)
        complete = bool(data.get("analysis") and
                        (data.get("segments") or not text_applicable))
        audit = {"status": "skipped", "reason": "mode_or_index_not_eligible"}
        multimodal = {"status": "skipped", "reason": "mode_or_index_not_eligible"}
        plan = dict(previous.get("plan") or {})
        modifications = []
        # P2b：定稿凭证。story 分支里会填；general 模式由转场阶段补做。
        cut_final = dict(previous.get("cut_final") or {})
        if story and complete and spans:
            import director_critic
            import director_multimodal
            self.log("⑤ 导演：按剧情审稿，并同步复听真实音频、检查关键画面…")
            memory_context = self.relevant_long_term_memory()
            self._memory_context = list(memory_context)
            self._learning_guidance = self.compile_learning_guidance(memory_context)
            if memory_context:
                self.log("   导演记忆：载入 %d 条同模式/可共享的已验证经验" %
                         len(memory_context))
            if self._learning_guidance.get("audio_guard_sec"):
                self.log("   导演教导：已启用 %.2f 秒听觉边界保护，只作用于当前已否决切口" %
                         float(self._learning_guidance["audio_guard_sec"]))
            if text_applicable:
                input_spans = list(spans)
                if self.model_editor:
                    # 模型初剪已经决定全片取舍。导演只能针对审片发现的
                    # 明确问题做局部补回/删除，不能再按另一套时长目标
                    # 从头重建时间轴，否则剪辑师的判断会被整体覆盖。
                    target_seconds = sum(end - start for start, end in spans)
                    initial_report = director_critic.review(
                        data, spans, target_seconds=target_seconds,
                        level=self.level)
                    audit = {
                        "status": ("passed" if initial_report.get("passed")
                                   else "needs_rework"),
                        "initial": initial_report,
                        "final": initial_report,
                        "history": [],
                        "target_seconds": target_seconds,
                        "model_timeline_preserved": True,
                    }
                    self.log("   文字审稿：保留模型初剪，只处理有证据的局部问题")
                else:
                    spans, audit = director_critic.review_and_rebuild(
                        data, spans, level=self.level,
                        max_rounds=int(self.director_conf.get(
                            "director_text_rework_rounds", 10) or 10),
                        on_log=lambda m: self.add_log(logs, m))
            else:
                no_text_report = {
                    "score": 100.0, "passed": True, "issues": [],
                    "status": "not_applicable", "reason": "confirmed_no_dialogue",
                }
                audit = {"status": "not_applicable", "initial": no_text_report,
                         "final": no_text_report, "history": []}
                self.log("   文字审稿：分析已确认无对白，本任务改由真实声音和画面主审")
            for item in audit.get("history") or []:
                if item.get("round") and item.get("accepted"):
                    modifications.append({"source": "text_director",
                                          "round": item.get("round"),
                                          **dict(item.get("changes") or {})})
            text_report = audit.get("final") or audit.get("initial") or {}
            self.observe_text_issues(audit.get("initial") or {},
                                     input_spans if text_applicable else spans)
            self.observe_text_issues(text_report, spans)
            progress_total = self.report_director_issues(
                self._director_audio_rounds, "检查剧情和断句")["total"]
            # 导演补回或删除剧情句后，新增切口必须重新交还边界岗位处理。
            spans, details, director_boundary = self.refine_boundaries(
                data, spans, plan.get("task_seed"))
            if text_applicable:
                # Boundary refinement may widen a semantic plan by many seconds
                # or move a cut across another sentence.  Local rework must use
                # the timeline that will actually be rendered, not the stale
                # report produced before boundary refinement.
                text_report = director_critic.review(
                    data, spans, target_seconds=audit.get("target_seconds"),
                    level=self.level)
                audit["final"] = text_report
            text_repaired, repaired_text_report, text_changes, text_history = (
                self.direct_text_rework(
                    data, spans, text_report, level=self.level,
                    target_seconds=audit.get("target_seconds"),
                    fps=float(data.get("fps") or 30.0),
                    duration=float(data.get("duration_exact") or
                                   data.get("duration") or 0.0),
                    max_rounds=int(self.director_conf.get(
                        "director_text_rework_rounds", 10) or 10),
                    on_review=self.review_text_progress))
            if text_repaired != spans:
                spans = text_repaired
                modifications.append({
                    "source": "text_director", "round": "local",
                    **text_changes,
                })
                text_report = repaired_text_report
                audit["final"] = text_report
                audit.setdefault("history", []).extend(text_history)
                self.add_log(
                    logs, "导演局部执导：补回 %d 句、删除 %d 句并重新检查边界" %
                    (len(text_changes["added"]), len(text_changes["dropped"])))
                spans, _details, repaired_boundary = self.refine_boundaries(
                    data, spans, plan.get("task_seed"))
                director_boundary = {
                    **dict(director_boundary or {}),
                    "after_text_rework": repaired_boundary,
                }
                text_report = director_critic.review(
                    data, spans, target_seconds=audit.get("target_seconds"),
                    level=self.level)
                audit["final"] = text_report
                self.review_text_progress(text_report, spans)
            words = self.words(data)
            (spans, windows, audio_reports, audio_error, audio_rounds_used,
             audio_modifications, progress_total) = self.direct_audio_rework(
                 data, spans, words, progress_total=progress_total)
            modifications.extend(audio_modifications)

            # Audio expansion can bring another sentence back into the kept
            # envelope.  The director therefore re-reads the actual timeline
            # and, when text changes it again, relistens without waiting for a
            # user to press “continue rework”.  A stable span signature ends the
            # loop; the safety cap prevents oscillation on contradictory cues.
            max_director_cycles = 3
            for cycle in range(2, max_director_cycles + 1):
                if not text_applicable:
                    break
                reviewed_after_audio = director_critic.review(
                    data, spans, target_seconds=audit.get("target_seconds"),
                    level=self.level)
                (next_spans, next_text_report, next_changes,
                 next_history) = self.direct_text_rework(
                    data, spans, reviewed_after_audio, level=self.level,
                    target_seconds=audit.get("target_seconds"),
                    fps=float(data.get("fps") or 30.0),
                    duration=float(data.get("duration_exact") or
                                   data.get("duration") or 0.0),
                    max_rounds=int(self.director_conf.get(
                        "director_text_rework_rounds", 10) or 10),
                    on_review=self.review_text_progress)
                text_report = next_text_report
                audit["final"] = text_report
                audit.setdefault("history", []).extend(next_history)
                if next_spans == spans:
                    break
                spans = next_spans
                modifications.append({
                    "source": "text_director", "round": "auto-%d" % cycle,
                    **next_changes,
                })
                self.review_text_progress(text_report, spans)
                self.add_log(
                    logs, "导演自动继续返工：补回 %d 句、删除 %d 句并重新复听" %
                    (len(next_changes["added"]), len(next_changes["dropped"])))
                spans, _details, cycle_boundary = self.refine_boundaries(
                    data, spans, plan.get("task_seed"))
                director_boundary = {
                    **dict(director_boundary or {}),
                    "after_auto_text_rework_%d" % cycle: cycle_boundary,
                }
                (spans, windows, audio_reports, audio_error, extra_rounds,
                 audio_modifications, progress_total) = self.direct_audio_rework(
                     data, spans, words, progress_total=progress_total,
                     round_offset=audio_rounds_used)
                modifications.extend(audio_modifications)
                audio_rounds_used += extra_rounds

            # P2b 切点定稿：三轴重选 / 删停顿必须在**审片之前**做完。
            # 2026-11-24 审计的教训：审的是 906.5 秒，交的是 780.8 秒——
            # 渲染阶段自己又删了 99 秒，而且没做语义复核。
            spans, cut_final = self.finalize_cut_points(
                data, spans, logs, data.get("source"))
            if cut_final.get("moved") or cut_final.get("trimmed_seconds"):
                self.add_log(
                    logs, "   切点定稿：重选 %d 处、删停顿 %.1f 秒（后面的审片基于定稿）"
                    % (cut_final.get("moved", 0),
                       cut_final.get("trimmed_seconds", 0.0)))

            # Premiere 式时间轴门禁：所有会移动刀口的确定性操作必须先完成，
            # 再让文字、听觉、视觉审同一个 revision。通过后该 revision 冻结。
            spans, boundary_closure = self.close_timeline_boundaries(
                data, spans, words, logs, modifications)
            timeline_revision = boundary_closure["timeline_revision"]
            seen_revisions = set()
            timeline_stable = False
            final_cycle_limit = 3
            for final_cycle in range(1, final_cycle_limit + 1):
                timeline_revision = self.timeline_revision(spans)
                if timeline_revision in seen_revisions:
                    audio_error = (audio_error or
                                   "最终视听返工出现重复时间轴 revision，停止振荡")
                    break
                seen_revisions.add(timeline_revision)

                if text_applicable:
                    current_text = director_critic.review(
                        data, spans,
                        target_seconds=audit.get("target_seconds"),
                        level=self.level)
                    (text_repaired, text_report, final_text_changes,
                     final_text_history) = self.direct_text_rework(
                        data, spans, current_text, level=self.level,
                        target_seconds=audit.get("target_seconds"),
                        fps=float(data.get("fps") or 30.0),
                        duration=float(data.get("duration_exact") or
                                       data.get("duration") or 0.0),
                        max_rounds=int(self.director_conf.get(
                            "director_text_rework_rounds", 10) or 10),
                        on_review=self.review_text_progress)
                    audit.setdefault("history", []).extend(final_text_history)
                    if text_repaired != spans:
                        spans = text_repaired
                        modifications.append({
                            "source": "text_director", "round":
                                "final-%d" % final_cycle,
                            **final_text_changes,
                        })
                        spans, _details, cycle_boundary = self.refine_boundaries(
                            data, spans, plan.get("task_seed"))
                        director_boundary = {
                            **dict(director_boundary or {}),
                            "after_final_text_rework_%d" % final_cycle:
                                cycle_boundary,
                        }
                        spans, cut_final = self.finalize_cut_points(
                            data, spans, logs, data.get("source"))
                        spans, boundary_closure = self.close_timeline_boundaries(
                            data, spans, words, logs, modifications)
                        continue
                else:
                    text_report = dict(no_text_report)

                text_report = dict(text_report or {})
                text_report["timeline_revision"] = timeline_revision
                audit["final"] = text_report

                (audio_spans, windows, audio_reports, final_audio_error,
                 final_audio_rounds, final_audio_modifications,
                 progress_total) = self.direct_audio_rework(
                    data, spans, words, progress_total=progress_total,
                    round_offset=audio_rounds_used)
                audio_rounds_used += final_audio_rounds
                modifications.extend(final_audio_modifications)
                if final_audio_error:
                    audio_error = final_audio_error
                if self.timeline_revision(audio_spans) != timeline_revision:
                    spans = audio_spans
                    spans, _details, cycle_boundary = self.refine_boundaries(
                        data, spans, plan.get("task_seed"))
                    director_boundary = {
                        **dict(director_boundary or {}),
                        "after_final_audio_rework_%d" % final_cycle:
                            cycle_boundary,
                    }
                    spans, cut_final = self.finalize_cut_points(
                        data, spans, logs, data.get("source"))
                    spans, boundary_closure = self.close_timeline_boundaries(
                        data, spans, words, logs, modifications)
                    continue

                audio_reports = self.settle_audio_reports(
                    audio_reports, rework_rounds=audio_rounds_used,
                    repair_attempts=self._director_audio_attempts)
                for report in audio_reports:
                    report["timeline_revision"] = timeline_revision
                timeline_stable = True
                break

            timeline_revision = self.timeline_revision(spans)
            if not timeline_stable:
                # Repair caps stop further mutations, not evidence gathering.
                # Review the actual final cut once; never stamp old text/audio
                # with a new revision just to satisfy the delivery gate.
                self.log("   导演：停止重复改切口，对当前定稿做同版复检并保留真实待办")
                if text_applicable:
                    text_report = director_critic.review(
                        data, spans, target_seconds=audit.get("target_seconds"),
                        level=self.level)
                else:
                    text_report = dict(no_text_report)
                text_report = dict(text_report)
                text_report["timeline_revision"] = timeline_revision
                audit["final"] = text_report
                try:
                    windows, audio_reports = self.audio_review(data, spans, words)
                    audio_reports = self.settle_audio_reports(
                        audio_reports, rework_rounds=audio_rounds_used,
                        repair_attempts=self._director_audio_attempts)
                    for report in audio_reports:
                        report["timeline_revision"] = timeline_revision
                    audio_error = None
                    timeline_stable = True
                except workflow_engine.WorkflowCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    audio_error = "%s: %s" % (type(exc).__name__, str(exc)[:180])
                    audio_reports = []
            self.observe_text_issues(text_report, spans)
            self.observe_audio_issues(audio_reports, spans, complete=not audio_error)
            cut_final.update({
                "finalized": True,
                "timeline_revision": timeline_revision,
                "boundary_closure": boundary_closure,
                "timeline_stable": bool(timeline_stable),
                "seen_revisions": sorted(seen_revisions),
                "segments": len(spans),
                "duration": round(sum(b - a for a, b in spans), 6),
            })

            visual_report = None
            visual_error = None
            visual_enabled = bool(self.director_conf.get("visual_review_enabled", True))
            if visual_enabled:
                try:
                    import visual_critic
                    visual_dir = os.path.join(
                        idx, "director_visual", str(plan.get("plan_sha256") or self.task_id)[:16])
                    visual_report = visual_critic.review_video(
                        self.src, data, spans=spans, output_dir=visual_dir,
                        max_nodes=int(self.director_conf.get("visual_max_nodes", 18) or 18),
                        use_vlm=bool(self.director_conf.get("visual_use_vlm", False)),
                        vlm_limit=int(self.director_conf.get("visual_vlm_limit", 2) or 2),
                        vlm_tier=str(self.director_conf.get("visual_vlm_tier", "fast") or "fast"),
                        on_log=lambda m: self.log("   " + str(m)),
                        should_cancel=self.should_cancel)
                except Exception as exc:  # noqa: BLE001
                    try:
                        import video_frame_cache
                        if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                            raise workflow_engine.WorkflowCancelled(str(exc)) from exc
                    except ImportError:
                        pass
                    visual_error = "%s: %s" % (type(exc).__name__, str(exc)[:180])
                    self.log("   导演视觉证据失败：%s" % visual_error)
            visual_report, transition_directives = self.prepare_visual_report(
                visual_report)
            if visual_report is not None:
                visual_report["timeline_revision"] = timeline_revision
            multimodal = director_multimodal.combine_review(
                text_report=text_report, audio_reports=audio_reports,
                visual_report=visual_report,
                require_audio=bool(self.director_conf.get("audio_review_enabled", True)),
                require_visual=visual_enabled, text_applicable=text_applicable,
                expected_audio_windows=windows,
                expected_timeline_revision=timeline_revision)
            multimodal.update({
                "status": "passed" if multimodal.get("passed") else "needs_rework",
                "review_windows": windows,
                "audio_error": audio_error,
                "visual_error": visual_error,
                "boundary_summary": director_boundary,
                "boundary_closure": boundary_closure,
                "timeline_revision": timeline_revision,
                "timeline_frozen": bool(timeline_stable),
                "long_term_context": [row.get("memory_id") for row in memory_context],
                "transition_directives": transition_directives,
            })
            # Text/audio are reconciled from their complete evidence above.
            # Container evidence and visual findings own separate scopes so
            # completing one modality cannot erase another one's open issues.
            self.observe_director_issues(
                "visual", director_multimodal._visual_findings(visual_report), spans,
                complete=not visual_error)
            gate_findings = [row for row in multimodal.get("findings") or []
                             if row.get("modality") == "container" or
                             row.get("type") in (
                                 "text_evidence_missing", "audio_evidence_missing",
                                 "audio_boundary_coverage_incomplete", "visual_evidence_missing")]
            self.observe_director_issues("review_gate", gate_findings, spans)
            self.report_director_issues(
                self._director_audio_rounds,
                "多模态复审%s" % ("通过" if multimodal.get("passed") else "未通过"),
                force=True)
            multimodal["issue_ledger"] = self.issue_ledger().snapshot()
            if multimodal.get("passed"):
                self.log("   多模态导演：通过 %.1f 分（文字 %.1f / 听觉 %.1f）"
                         % (multimodal["scores"]["final"],
                            multimodal["scores"]["text"],
                            multimodal["scores"]["audio"]))
            else:
                self.log("   多模态导演：仍有 %d 个否决、%d 个重大问题，已记录局部返工单"
                         % (multimodal.get("veto_count", 0),
                            multimodal.get("major_issues", 0)))
            modifications.extend({
                "source": "multimodal_director", "finding_id": row.get("id"),
                "type": row.get("type"), "action": row.get("action"),
                "interval": row.get("suggested_interval"),
            } for row in multimodal.get("findings") or [])
            if any(row.get("source") == "text_director" and
                   (row.get("added") or row.get("dropped"))
                   for row in modifications):
                self.mark_memory_applied("editor")
            multimodal["applied_memory_ids"] = sorted(self._applied_memory_ids)
            multimodal["learning_guidance"] = {
                "audio_guard_sec": self._learning_guidance.get("audio_guard_sec", 0.0),
                "applied_memory_ids": sorted(self._applied_memory_ids),
            }
            debrief = self.build_director_debrief(multimodal, modifications)
            multimodal["debrief"] = debrief
            if multimodal.get("passed"):
                self.log(
                    "   导演复盘：本次已解决 %d 个问题，形成 %d 条可验证经验" %
                    (debrief.get("issues_resolved", 0),
                     sum(row.get("result") == "candidate"
                         for row in debrief.get("lessons") or [])))
            plan = edit_plan.extend_plan(
                plan, refined_spans=spans,
                director_modifications=modifications,
                evidence={**dict(plan.get("evidence") or {}),
                          "director": {
                              "text_score": text_report.get("score"),
                              "multimodal_score": (multimodal.get("scores") or {}).get("final"),
                              "passed": multimodal.get("passed"),
                              "audio_reports": len(audio_reports),
                              "visual_nodes": (visual_report or {}).get("node_count", 0),
                              "auto_learning_eligible": multimodal.get("auto_learning_eligible"),
                              "timeline_revision": timeline_revision,
                              "timeline_frozen": bool(timeline_stable),
                              "debrief": debrief,
                          }})
            learned_ids = self.record_director_learning(multimodal, plan, modifications)
            multimodal["learning_candidate_ids"] = learned_ids
            multimodal["long_term_memory_ids"] = learned_ids
            if learned_ids:
                self.log("   导演学习：保存 %d 条候选经验，等待实际成片复审奖励" % len(learned_ids))
            self.report_director_issues(
                self._director_audio_rounds,
                "复盘学习：形成 %d 条候选经验；保留实际未解决项" % len(learned_ids),
                force=True)
            plan_path = self.write_plan(
                "director", plan, "review", source_stage="boundary")
            try:
                path = os.path.join(idx, "index.json")
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                saved["director_audit"] = audit
                saved["director_multimodal_audit"] = multimodal
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(saved, fh, ensure_ascii=False, indent=1)
            except Exception as exc:  # noqa: BLE001
                self.log("   导演审片报告未写回索引：%s" % str(exc)[:60])
            if not multimodal.get("passed"):
                self.record_director_rework(
                    multimodal, review_artifact_id=self.artifact("director"))
        else:
            self.log("⑤ 导演：当前模式或索引不满足审片条件，保持初剪")
        previous.update({"spans": [[a, b] for a, b in spans],
                         "logs": logs, "director_audit": audit,
                         "director_multimodal_audit": multimodal,
                         "timeline_revision": (multimodal or {}).get(
                             "timeline_revision"),
                         "cut_final": cut_final,
                         "plan": plan})
        if plan:
            previous.update({"plan_path": locals().get("plan_path", previous.get("plan_path")),
                             "plan_sha256": plan.get("plan_sha256"),
                             "task_seed": plan.get("task_seed")})
        self.done("导演审片", t0)
        self.require_director_approval(previous)
        return workflow_engine.StageResult(
            self.artifact("director"), previous,
            {"artifact_type": "review", "segments": len(spans),
             "plan_sha256": plan.get("plan_sha256") if plan else None,
             "timeline_revision": (multimodal or {}).get("timeline_revision"),
             "passed": multimodal.get("passed")})

    def transition(self, ctx):
        t0 = time.time()
        previous = self.payload(ctx, "director")
        self.require_director_approval(previous)
        idx = previous.get("index_dir") or self.idx
        data = autocut.load_index(idx)
        spans = self.spans(previous.get("spans"))
        # P2b：导演已经定稿（cut_final.finalized），转场阶段**不许再动切点**——
        # 动了就等于"审的是一份、交的是另一份"（2026-11-24 审计最贵的一条）。
        # 没有定稿（general 模式 / 自定义 stage_handlers / 旧索引）时在这里补做。
        cut_final = dict(previous.get("cut_final") or {})
        if not cut_final.get("finalized"):
            spans, cut_final = self.finalize_cut_points(
                data, spans, previous.get("logs") or [], data.get("source"))
            previous["cut_final"] = cut_final
            self.log("   切点定稿：转场前补做（此前没有导演定稿）")
        seams = autocut.judge_transitions(
            spans, data["source"], data, previous.get("conf") or {}, verbose=False)
        directives = list((previous.get("director_multimodal_audit") or {}).get(
            "transition_directives") or [])
        directed = 0
        for directive in directives:
            values = [float(value) for value in directive.get("interval") or []
                      if isinstance(value, (int, float))]
            if not values:
                continue
            left, right = min(values), max(values)
            for seam in seams:
                if float(seam.get("tb") or 0.0) < left - 0.5:
                    continue
                if float(seam.get("ta") or 0.0) > right + 0.5:
                    continue
                seam["director_directed"] = True
                seam["director_operation"] = directive.get("operation")
                seam["why"] = "导演标记画面跳变；" + str(seam.get("why") or "已选择安全转场")
                directed += 1
                break
        # 短叠化（vfade）在接缝处只给音频 40 毫秒交叉、其余补静音，等于把
        # 下一段开头 d 秒的人声吃掉。若那个位置正好落在字中间，就是一个字
        # 直接没了。这种接缝降级为硬切：宁可画面跳一下，也不让台词少字。
        words = self.words(data)
        transition_conf = (previous.get("conf") or {}).get("transition") or {}
        vfade_want = float(transition_conf.get("vfade", 0.22) or 0.22)
        degraded = 0
        for position, seam in enumerate(seams):
            if seam.get("kind") != "vfade" or position + 1 >= len(spans):
                continue
            next_start, next_end = spans[position + 1]
            prev_start, prev_end = spans[position]
            fade = min(vfade_want, (prev_end - prev_start) * 0.5,
                       (next_end - next_start) * 0.5)
            if fade <= 0.05:
                continue
            eaten_start, eaten_end = next_start, next_start + fade
            if any(min(eaten_end, word["end"]) - max(eaten_start, word["start"])
                   > 0.025 for word in words):
                seam["kind"] = "cut"
                seam["why"] = "字边界保护：短叠化会吃掉这个字，改硬切"
                degraded += 1
        if degraded:
            self.log("   字边界保护：%d 处短叠化会吃掉字，已改硬切" % degraded)
        kinds = {}
        for seam in seams:
            kinds[seam["kind"]] = kinds.get(seam["kind"], 0) + 1
        self.log("⑥ 转场：%s" % kinds)
        if directed:
            self.log("   导演执导：%d 个画面跳变点已交给转场岗位处理" % directed)
        plan = dict(previous.get("plan") or {})
        if plan:
            plan = edit_plan.extend_plan(
                plan, final_render_spans=spans,
                evidence={**dict(plan.get("evidence") or {}),
                          "transition": {"counts": kinds,
                                          "seam_count": len(seams),
                                          "director_directed": directed}})
            plan_path = self.write_plan(
                "transition", plan, "render_input", source_stage="director")
        else:
            plan_path = previous.get("plan_path")
        previous.update({"seams": seams, "kinds": kinds,
                         "plan": plan, "plan_path": plan_path,
                         "plan_sha256": plan.get("plan_sha256") if plan else None,
                         "task_seed": plan.get("task_seed") if plan else None})
        self.done("转场", t0)
        return workflow_engine.StageResult(
            self.artifact("transition"), previous,
            {"artifact_type": "transition_plan", "counts": kinds})

    def auto_fix_seams(self, spans, data):
        """复审能发现的切点问题，这里先自己换位置（发现→修→复检，别等出片后返工）。

        判据与交付复审**同一个 slack**（coords.CUT_WORD_SLACK_S）：
          · 接缝落在词内或离词边界太近 → 挪到词外（起点往左、终点往右，宁可多留）；
          · 仍落在句子中间 → 扩到句边界。
        修完合并重叠、丢掉碎段。返回 (spans, 修了几处)。
        """
        import coords as _coords
        slack = float(getattr(_coords, "CUT_WORD_SLACK_S", 0.10))
        duration = float(data.get("duration") or 0.0)

        words = []
        for evidence in data.get("segment_evidence") or []:
            for word in evidence.get("words") or []:
                try:
                    w0, w1 = float(word["start"]), float(word["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if w1 > w0:
                    words.append((w0, w1))
        units = []
        for segment in data.get("segments") or []:
            try:
                units.append((float(segment[0]), float(segment[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not words and not units:
            return list(spans), 0

        def clamp(value):
            value = max(0.0, value)
            return min(duration, value) if duration else value

        out = [[float(a), float(b)] for a, b in spans]
        fixed = 0
        for row in out:
            for edge in (0, 1):
                t = row[edge]
                moved = None
                for w0, w1 in words:
                    if w0 - slack < t < w1 + slack:
                        moved = clamp(w0 - slack) if edge == 0 else clamp(w1 + slack)
                        break
                if moved is None:
                    for u0, u1 in units:
                        if u0 - slack < t < u1 + slack:
                            moved = (clamp(u0 - slack) if edge == 0
                                     else clamp(u1 + slack))
                            break
                if moved is not None and abs(moved - t) > 0.01:
                    row[edge] = moved
                    fixed += 1

        out.sort()
        merged = []
        for a, b in out:
            if b - a < 0.3:
                continue
            if merged and a <= merged[-1][1] + 0.02:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        if fixed:
            self.log("   自检换位：%d 处接缝避开词/句中间（%d → %d 段）"
                     % (fixed, len(spans), len(merged)))
        return [(a, b) for a, b in merged], fixed

    def _legacy_finalize_cut_points(self, data, spans, logs, source_path=None):
        """切点定稿：三轴重选 → 删停顿 → 补重选 → 收口 → 三轴复核。

        P2a 只是把这段从 renderer 里抽出来，行为不变。
        P2b 会把它移到导演审片之前——**审的对象必须是交付的对象**（Q2 冻结）。

        返回 (spans, info)；info 里的 `finalized=True` 是给 Q2 闸门看的凭证。
        """
        source_path = source_path or data["source"]
        _reselect_spans = []
        _moved = 0
        _trimmed = 0.0
        _reverts = 0
        _fallback = 0
        # 出片前先重选切点：导演给的切点是按句子边界算的，但真正无缝的位置
        # 往往在“能量谷底”（ASR 的词尾比真实发音早约 0.1 秒）。这里对每处空隙
        # 在两端找一对“能量/音色/形状”三轴都匹配的切点，找不到就保留原样。
        if bool(self.director_conf.get("seam_pick_enabled", True)) and len(spans) > 1:
            try:
                import seam_picker
                import index as _index
                # Premiere 式媒体索引：切点、停顿、三轴复核共用同一条说话轨
                # 的 PCM，不允许三个岗位各自把整片重新解码一遍。
                _selected = self.selected_tracks(data)
                _speech = self.speech_tracks(data, _selected)
                _vtr = int((_speech or _selected or [0])[0])
                _source_cache = None
                try:
                    _source_cache = self.ensure_audio_cache(data)
                    if _source_cache is None:
                        raise RuntimeError("没有可用的共享音频索引")
                    _entry = _source_cache.entry(_vtr)
                    _audio = _source_cache.review_samples(
                        _vtr, 0.0, float(_entry.get("duration") or 0.0))
                    _audio_sr = int(_source_cache.sample_rate)
                except Exception as _cache_err:  # noqa: BLE001
                    if (_cache_err.__class__.__name__
                            == "AudioEvidenceCancelled"):
                        raise workflow_engine.WorkflowCancelled(
                            str(_cache_err)) from _cache_err
                    self.add_log(
                        logs, "   共享音频索引读取失败，兼容解码一次：%s"
                        % str(_cache_err)[:80])
                    _audio = seam_picker.load_audio(
                        data["source"], _index.find_ffmpeg(), track=_vtr)
                    _audio_sr = int(getattr(seam_picker, "SR", 16000))
                _gaps = [(spans[i][1], spans[i + 1][0])
                         for i in range(len(spans) - 1)]
                import longchain as _lc
                import llm_config as _llm
                import proofread as _pf
                with _pf.Server(port=8269,
                                model=_llm.pick("director"),
                                ctx=8192, api_role="director") as _srv:
                    _cuts = seam_picker.pick_cuts(
                        data.get("segments") or [], _audio, _gaps,
                        words=self.words(data), lc_server=_srv)
                for _k, _row in enumerate(_cuts):
                    _best = _row.get("best")
                    if not _best or _k >= len(spans) - 1:
                        continue
                    _old = (spans[_k][1], spans[_k + 1][0])
                    _new = (_best["left_t"], _best["right_t"])
                    if (abs(_new[0] - _old[0]) < 0.02
                            and abs(_new[1] - _old[1]) < 0.02):
                        continue
                    # 硬保护：重选后的两段都必须是正的。
                    # 否则会生成“负长度段”，渲染时 -t 变成负数，
                    # 那一段被整段吃掉——观众听到的就是“话说一半”
                    # （历史样本：30 处重选里出 1 个负段，总长少 107 秒，
                    #   复审 3 条半句、0 分）。
                    _left_end = float(_new[0])
                    _right_start = float(_new[1])
                    if _left_end - spans[_k][0] < 0.5:
                        continue
                    if spans[_k + 1][1] - _right_start < 0.5:
                        continue
                    if _right_start <= _left_end + 0.2:
                        continue
                    spans[_k] = (spans[_k][0], _left_end)
                    spans[_k + 1] = (_right_start, spans[_k + 1][1])
                    _moved += 1
                    self.add_log(logs,
                                 "   切点重选：%.2f→%.2f 改为 %.2f→%.2f"
                                 "（能量%.1fdB/音色%.0fHz/形状%.1fdB）"
                                 % (_old[0], _old[1], _new[0], _new[1],
                                    _best["energy_gap"], _best["tone_gap_hz"],
                                    _best["shape_rms"]))
                # Q1 记账：这批里有多少处是"没有合格候选、只能退回边缘区"的
                _fallback = sum(1 for _row in _cuts
                                if (_row.get("best") or {}).get("fallback"))
                if _fallback:
                    self.log("   Q1 兜底：%d 处没有合格切点，退回边缘区（已记账）"
                             % _fallback)
                # 删掉“没有说话的停顿空段”（子枫 2026-11-23）：
                # 三坐标系搞定之后，再往前延伸一步——说话轨上那些
                # 3~10 秒的空白（想、换气、看画面）留着就是节奏拖沓。
                # 判据两条同时成立：能量 < −45dB 且这段时间没有词。
                if bool(self.director_conf.get("silence_trim_enabled", True)):
                    try:
                        import silence_trim as _st
                        _before = sum(float(b) - float(a) for a, b in spans)
                        spans, _sil = _st.trim(
                            source_path, _index.find_ffmpeg(), spans, _vtr,
                            words=self.words(data),
                            log=lambda m: self.add_log(logs, m),
                            audio=_audio, sample_rate=_audio_sr)
                        _after = sum(float(b) - float(a) for a, b in spans)
                        _trimmed += max(0.0, _before - _after)
                        if _before - _after > 0.5:
                            self.log("   停顿空段：%d 段，成片缩短 %.1f 秒"
                                     % (len(_sil), _before - _after))
                            # 删停顿会**新产生接缝**（实测 32 段 → 43 段），
                            # 这些新接缝从没做过三轴优化——不补一次的话
                            # 整片三轴超标从 22/24 涨到 35/42。
                            if _moved and len(spans) > 1:
                                try:
                                    _g2 = [(spans[_q][1], spans[_q + 1][0])
                                           for _q in range(len(spans) - 1)]
                                    _c2 = seam_picker.pick_cuts(
                                        data.get("segments") or [], _audio, _g2,
                                        words=self.words(data))
                                    _m2 = 0
                                    for _q, _r2 in enumerate(_c2):
                                        _b2 = _r2.get("best")
                                        if not _b2 or _q >= len(spans) - 1:
                                            continue
                                        _le = float(_b2["left_t"])
                                        _rs = float(_b2["right_t"])
                                        if (_le - spans[_q][0] < 0.3
                                                or spans[_q + 1][1] - _rs < 0.3
                                                or _rs <= _le + 0.2):
                                            continue
                                        spans[_q] = (spans[_q][0], _le)
                                        spans[_q + 1] = (_rs, spans[_q + 1][1])
                                        _m2 += 1
                                    if _m2:
                                        self.log("   停顿后三轴重选：%d 处" % _m2)
                                except Exception as _p2_err:  # noqa: BLE001
                                    self.add_log(logs, "   停顿后三轴重选跳过：%s"
                                                 % str(_p2_err)[:60])
                    except Exception as _st_err:  # noqa: BLE001
                        self.add_log(logs, "   停顿空段跳过：%s" % str(_st_err)[:60])
                if _moved:
                    # 顺序必须是：切点重选 → 词收口 → 句子收口。
                    # 词收口（word_safe_spans）会把切点吸到“词边界”，
                    # 而词边界常常落在句子中间——它跑在句子收口之后就会
                    # 把句子重新切开（历史样本出现过 4 条半句、0 分）。
                    # 所以句子收口必须放在最后一步。
                    _limit = float(data.get("duration_exact")
                                   or data.get("duration") or 0.0)
                    # 记住“重选后的位置”：后面收口如果把三轴搞失配了，
                    # 要能回到这里（子枫 2026-11-23：三轴伪装好就是正常）。
                    _reselect_spans = [(float(a), float(b)) for a, b in spans]
                    spans, _snaps = self.word_safe_spans(
                        spans, self.words(data), duration=_limit)
                    if _snaps:
                        self.add_log(logs, "   切点收口：%d 处吸到词边界"
                                     % len(_snaps))
                    # 诊断：切点重选后，哪些句子被切成了 30%~95%
                    try:
                        _cov = []
                        for _si, _sg in enumerate(data.get("segments") or []):
                            _a, _b = float(_sg[0]), float(_sg[1])
                            if _b - _a < 0.15:
                                continue
                            _kp = 0.0
                            for _x, _y in spans:
                                if _y > _a and _x < _b:
                                    _kp += min(_b, _y) - max(_a, _x)
                            _r = _kp / (_b - _a)
                            if 0.30 < _r < 0.95:
                                _cov.append((_si + 1, round(_r * 100)))
                        if _cov:
                            self.log("   [诊断] 重选后被切开的句子 %d 句：%s"
                                     % (len(_cov), _cov[:8]))
                    except Exception:
                        pass
                    spans, _sstat = self.sentence_safe_spans(
                        spans, data.get("segments") or [], duration=_limit)
                    if _sstat.get("expanded") or _sstat.get("dropped"):
                        self.add_log(logs,
                                     "   切点后句子收口：补全 %d 句、去掉 %d 处碎片"
                                     % (_sstat["expanded"], _sstat["dropped"]))
                    # 三轴复核（子枫 2026-11-23 / 方案 A）：
                    # 收口会把切点从“能量谷底”拉回“句子/词边界”，
                    # 拉完三轴可能就失配了（实测 314 秒那处音色差 2592Hz）。
                    # 所以逐接缝再算一次，失配就退回“重选后的位置”——
                    # 你的标准是“三轴伪装好就是正常”，句子切一点不要紧。
                    try:
                        import seam_picker as _spc
                        import tolerance as _tol
                        _au = _audio
                        _reverted = 0
                        for _k2 in range(min(len(spans), len(_reselect_spans)) - 1):
                            _a = float(spans[_k2][1])
                            _b = float(spans[_k2 + 1][0])
                            _ra = float(_reselect_spans[_k2][1])
                            _rb = float(_reselect_spans[_k2 + 1][0])
                            if abs(_a - _ra) < 0.02 and abs(_b - _rb) < 0.02:
                                continue      # 收口没动过这处
                            _axes = _spc.seam_axes(_au, _a, _b)
                            _eg = _axes["energy_db"]
                            _tg = _axes["tone_hz"]
                            _sg = _axes["shape_db"]
                            if _tol.combine(_tol.energy_grade(_eg),
                                            _tol.tone_grade(_tg),
                                            _tol.shape_grade(_sg)) == "over":
                                spans[_k2] = (_reselect_spans[_k2][0], _ra)
                                spans[_k2 + 1] = (_rb, _reselect_spans[_k2 + 1][1])
                                _reverted += 1
                        _reverts = _reverted
                        if _reverted:
                            self.log("   三轴复核：%d 处收口后失配，已退回重选位置"
                                     % _reverted)
                    except Exception as _ax_err:  # noqa: BLE001
                        self.add_log(logs, "   三轴复核跳过：%s" % str(_ax_err)[:60])
                    self.log("   切点重选：%d 处换到了三轴更匹配的位置" % _moved)
            except Exception as _sp_err:  # noqa: BLE001
                from llm_gateway import propagate_api_error
                propagate_api_error(_sp_err)
                self.add_log(logs, "   切点重选跳过：%s" % str(_sp_err)[:80])

        # 复审能发现的切点问题，定稿前先自己换位置（发现→修）
        spans, _seam_fixed = self.auto_fix_seams(spans, data)

        # A21：成片开头必须是第一个字，不许留静音。
        # 边界精修会按音轨活动把段 1 起点往外扩（实测 4.75s → 2.0s，
        # 那 2.75 秒是 -70dB 静音，成片一开头就没声音）。这里最后收一道，
        # Q2 之后冻结的就是收口后的时间轴，审的和交的是同一版。
        try:
            _segs = [s for s in (data.get("segments") or []) if len(s) > 1]
            if spans and _segs:
                _first = min(float(s[0]) for s in _segs)
                import coords as _coords
                _pad = float(getattr(_coords, "CUT_PAD_MS", 150)) / 1000.0
                _limit = max(0.0, _first - _pad)
                _a0 = float(spans[0][0])
                if _a0 < _limit - 0.05:
                    spans[0] = (_limit, spans[0][1])
                    self.log("   开头收口：起点 %.2fs → %.2fs（首字 %.2fs 前 %.0fms）"
                             % (_a0, _limit, _first, _pad * 1000))
        except Exception as _hd_err:  # noqa: BLE001
            self.add_log(logs, "   开头收口跳过：%s" % str(_hd_err)[:80])

        return spans, {"finalized": True, "moved": _moved,
                       "trimmed_seconds": round(_trimmed, 1),
                       "reverts": _reverts, "fallback": int(_fallback),
                       "reselect_spans": [[round(a, 3), round(b, 3)]
                                          for a, b in _reselect_spans],
                       "segments": len(spans)}

    def finalize_cut_points(self, data, spans, logs, source_path=None):
        """Finalize one semantic-safe, waveform-aligned timeline revision.

        Seam scoring may propose better joins, but it is never allowed to
        overrule word/sentence protection. Silence removal and every other
        operation that creates a seam run before the unified boundary closure.
        """
        source_path = source_path or data["source"]
        spans = [(float(a), float(b)) for a, b in spans if float(b) > float(a)]
        words = self.words(data)
        selected = self.selected_tracks(data)
        speech = self.speech_tracks(data, selected)
        speech_track = int((speech or selected or [0])[0])
        moved = 0
        trimmed = 0.0
        fallback = 0
        semantic_rejected = 0
        protected_over = 0
        reselect_spans = []
        audio = None
        audio_sr = 16000
        seam_picker = None

        def load_audio_once():
            nonlocal audio, audio_sr, seam_picker
            if audio is not None:
                return audio
            import seam_picker as picker
            seam_picker = picker
            try:
                cache = self.ensure_audio_cache(data)
                if cache is None:
                    raise RuntimeError("没有可用的共享音频索引")
                entry = cache.entry(speech_track)
                audio = cache.review_samples(
                    speech_track, 0.0, float(entry.get("duration") or 0.0))
                audio_sr = int(cache.sample_rate)
            except Exception as exc:  # noqa: BLE001
                if exc.__class__.__name__ == "AudioEvidenceCancelled":
                    raise workflow_engine.WorkflowCancelled(str(exc)) from exc
                self.add_log(
                    logs, "共享音频索引读取失败，兼容解码一次：%s"
                    % str(exc)[:80])
                audio = picker.load_audio(
                    source_path, indexer.find_ffmpeg(), track=speech_track)
                audio_sr = int(getattr(picker, "SR", 16000))
            return audio

        def crosses_unselected_word(old, new):
            lo, hi = sorted((float(old), float(new)))
            if hi - lo <= 0.02:
                return False
            for word in words:
                try:
                    word_start = float(word["start"])
                    word_end = float(word["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if min(hi, word_end) - max(lo, word_start) <= 0.025:
                    continue
                if word_start - 0.025 <= float(old) <= word_end + 0.025:
                    continue
                return True
            return False

        def apply_candidates(cuts, minimum=0.5):
            nonlocal spans, moved, semantic_rejected
            changed = 0
            for index, row in enumerate(cuts or []):
                best = row.get("best") if isinstance(row, dict) else None
                if not best or index >= len(spans) - 1:
                    continue
                old_left = float(spans[index][1])
                old_right = float(spans[index + 1][0])
                left_end = float(best["left_t"])
                right_start = float(best["right_t"])
                if (abs(left_end - old_left) < 0.02 and
                        abs(right_start - old_right) < 0.02):
                    continue
                if left_end - spans[index][0] < minimum:
                    continue
                if spans[index + 1][1] - right_start < minimum:
                    continue
                if right_start <= left_end + 0.2:
                    continue
                if (crosses_unselected_word(old_left, left_end) or
                        crosses_unselected_word(old_right, right_start)):
                    semantic_rejected += 1
                    continue
                spans[index] = (spans[index][0], left_end)
                spans[index + 1] = (right_start, spans[index + 1][1])
                changed += 1
                self.add_log(
                    logs,
                    "切点重选：%.2f→%.2f 改为 %.2f→%.2f"
                    "（能量%.1fdB/音色%.0fHz/形状%.1fdB）"
                    % (old_left, old_right, left_end, right_start,
                       float(best.get("energy_gap") or 0.0),
                       float(best.get("tone_gap_hz") or 0.0),
                       float(best.get("shape_rms") or 0.0)))
            moved += changed
            return changed

        seam_enabled = bool(self.director_conf.get("seam_pick_enabled", True))
        if seam_enabled and len(spans) > 1:
            try:
                load_audio_once()
                gaps = [(spans[index][1], spans[index + 1][0])
                        for index in range(len(spans) - 1)]
                import llm_config
                import proofread
                with proofread.Server(
                        port=8269, model=llm_config.pick("director"),
                        ctx=8192, api_role="director") as server:
                    cuts = seam_picker.pick_cuts(
                        data.get("segments") or [], audio, gaps,
                        words=words, lc_server=server, sr=audio_sr)
                apply_candidates(cuts, minimum=0.5)
                fallback = sum(
                    1 for row in cuts
                    if ((row.get("best") or {}).get("fallback")
                        if isinstance(row, dict) else False))
            except workflow_engine.WorkflowCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                from llm_gateway import propagate_api_error
                propagate_api_error(exc)
                self.add_log(logs, "切点重选跳过：%s" % str(exc)[:100])

        if semantic_rejected:
            self.add_log(
                logs, "内容保护：%d 个波形候选跨过其他台词，保持模型/导演切点"
                % semantic_rejected)

        if bool(self.director_conf.get("silence_trim_enabled", True)) and spans:
            try:
                import silence_trim
                load_audio_once()
                before = sum(b - a for a, b in spans)
                spans, silences = silence_trim.trim(
                    source_path, indexer.find_ffmpeg(), spans, speech_track,
                    words=words, log=lambda message: self.add_log(logs, message),
                    audio=audio, sample_rate=audio_sr)
                spans = [(float(a), float(b)) for a, b in spans]
                after = sum(b - a for a, b in spans)
                trimmed = max(0.0, before - after)
                if trimmed > 0.0 and len(spans) > 1 and seam_enabled:
                    gaps = [(spans[index][1], spans[index + 1][0])
                            for index in range(len(spans) - 1)]
                    cuts = seam_picker.pick_cuts(
                        data.get("segments") or [], audio, gaps,
                        words=words, sr=audio_sr)
                    extra = apply_candidates(cuts, minimum=0.3)
                    if extra:
                        self.log("   停顿后三轴重选：%d 处" % extra)
                if silences and trimmed > 0.0:
                    self.log("   停顿空段：%d 段，成片缩短 %.1f 秒"
                             % (len(silences), trimmed))
            except workflow_engine.WorkflowCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                self.add_log(logs, "停顿空段跳过：%s" % str(exc)[:100])

        # Hard final constraint: long ranges without transcript evidence are
        # removed even when game audio/music is continuously loud.  Percentage
        # controls content selection; it must never be satisfied by padding the
        # result with minutes where nobody speaks.
        if spans and data.get("segments"):
            try:
                import cut_strength
                import silence_trim
                gap_limit = cut_strength.no_dialogue_gap_seconds(
                    self.keep_ratio)
                before = sum(b - a for a, b in spans)
                spans, no_dialogue = silence_trim.trim_no_dialogue(
                    spans, data.get("segments") or [], min_gap=gap_limit,
                    log=lambda message: self.add_log(logs, message))
                after = sum(b - a for a, b in spans)
                if no_dialogue and before > after:
                    trimmed += before - after
                    self.log("   无对白硬约束：最终时间轴缩短 %.1f 秒，不用空段凑比例"
                             % (before - after))
            except Exception as exc:  # noqa: BLE001
                self.add_log(logs, "无对白长段清理跳过：%s" % str(exc)[:100])

        reselect_spans = [(float(a), float(b)) for a, b in spans]
        had_override = hasattr(self, "_boundary_pcm_override")
        previous_override = getattr(self, "_boundary_pcm_override", None)
        if audio is not None:
            self._boundary_pcm_override = (speech_track, audio, audio_sr)
        try:
            spans, closure = self.close_timeline_boundaries(
                data, spans, words, logs, [])
        finally:
            if had_override:
                self._boundary_pcm_override = previous_override
            elif hasattr(self, "_boundary_pcm_override"):
                del self._boundary_pcm_override
        closed_spans_revision = self.timeline_revision(spans)
        closure_revision = str(
            closure.get("timeline_revision") or closed_spans_revision)
        # Boundary closure may move a seam for word/frame safety.  Reapply the
        # no-dialogue mask once, after every expansion, so no later stage can
        # bridge a deleted multi-second pause back into the PR timeline.
        if spans and data.get("segments"):
            try:
                import cut_strength
                import silence_trim
                before = sum(b - a for a, b in spans)
                spans, final_no_dialogue = silence_trim.trim_no_dialogue(
                    spans, data.get("segments") or [],
                    min_gap=cut_strength.no_dialogue_gap_seconds(
                        self.keep_ratio),
                    log=lambda message: self.add_log(logs, message))
                after = sum(b - a for a, b in spans)
                if final_no_dialogue and before > after:
                    trimmed += before - after
                    self.log("   最终无对白遮罩：再次移除 %.1f 秒，时间轴已冻结"
                             % (before - after))
            except Exception as exc:  # noqa: BLE001
                self.add_log(logs, "最终无对白遮罩跳过：%s" % str(exc)[:100])
        delivered_spans_revision = self.timeline_revision(spans)
        if delivered_spans_revision == closed_spans_revision:
            # close_timeline_boundaries owns the revision for the timeline it
            # returned.  Preserve that exact value (including test/adapter
            # implementations) when the final mask made no further change.
            revision = closure_revision
        else:
            revision = delivered_spans_revision
            closure["timeline_revision"] = revision

        # 三轴分数只决定转场策略，不能推翻词、句和波形边缘保护。
        if len(spans) > 1:
            try:
                load_audio_once()
                import tolerance
                for index in range(len(spans) - 1):
                    axes = seam_picker.seam_axes(
                        audio, float(spans[index][1]),
                        float(spans[index + 1][0]), sr=audio_sr)
                    grade = tolerance.combine(
                        tolerance.energy_grade(axes["energy_db"]),
                        tolerance.tone_grade(axes["tone_hz"]),
                        tolerance.shape_grade(axes["shape_db"]))
                    if grade == "over":
                        protected_over += 1
                if protected_over:
                    self.add_log(
                        logs,
                        "三轴复核：%d 处超标，保留语义安全切点并交给转场处理"
                        % protected_over)
            except workflow_engine.WorkflowCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                self.add_log(logs, "三轴复核跳过：%s" % str(exc)[:80])

        return spans, {
            "finalized": True,
            "moved": moved,
            "trimmed_seconds": round(trimmed, 3),
            "reverts": 0,
            "protected_over": protected_over,
            "fallback": int(fallback),
            "semantic_rejected": int(semantic_rejected),
            "speech_track": speech_track,
            "reselect_spans": [[round(a, 3), round(b, 3)]
                               for a, b in reselect_spans],
            "segments": len(spans),
            "duration": round(sum(b - a for a, b in spans), 6),
            "timeline_revision": revision,
            "boundary_closure": closure,
        }

    def renderer(self, ctx):
        t0 = time.time()
        previous = self.payload(ctx, "transition")
        self.require_director_approval(previous)
        idx = previous.get("index_dir") or self.idx
        data = autocut.load_index(idx)
        spans = self.spans(previous.get("spans"))
        seams = list(previous.get("seams") or [])
        conf = previous.get("conf") or {}
        logs = list(previous.get("logs") or [])
        out = os.path.join(self.out_dir, self.stem + "_自动剪辑.mp4")
        subtitles = [(float(s[0]), float(s[1]), str(s[2] or ""))
                     for s in data.get("segments") or []]
        bgm_conf = bgm_mod.load_conf()
        bgm_conf["sections"] = "1"
        # 用户未显式设置种子时，用最终不可变计划固定换曲位置与选择。
        if not bgm_conf.get("seed"):
            bgm_conf["seed"] = (previous.get("plan_sha256")
                                or previous.get("task_seed") or self.task_id)
        if self.loudness_norm is not None:
            enabled = (str(self.loudness_norm).strip().lower()
                       not in ("", "0", "false", "no", "off", "none"))
            bgm_conf["loudness_norm"] = "1" if enabled else "0"
        self.log("⑦ 渲染器：输出成片、字幕时间线和音频混合…")
        # 合成前逐轨审（子枫 2026-11-23）：
        # 「审核要能发现问题是什么音轨上的问题、什么问题，
        #   这样才能单独改一个音轨。」
        # 合成后声音混在一起就只能靠猜，所以这一步必须在 render 之前。
        track_findings = []
        if bool(self.director_conf.get("track_audit_enabled", True)):
            try:
                import track_audit
                _kinds = data.get("track_kinds") or {}
                _selected = set(self.selected_tracks(data))
                # 只审最终会进入成片的轨；共享缓存也严格按轨读取，不混音。
                _kinds = {
                    key: value for key, value in _kinds.items()
                    if str(key).lstrip("-").isdigit()
                    and int(key) in _selected
                }
                if _kinds:
                    self.log("   逐轨审核：%d 条轨各自检查（合成前）…"
                             % len(_kinds))
                    track_findings = track_audit.audit(
                        data["source"], __import__("index").find_ffmpeg(),
                        _kinds, words=self.words(data), spans=spans,
                        audio_cache=self.ensure_audio_cache(data))
                    _sum = track_audit.summarize(track_findings)
                    for _name, _n in sorted(_sum["by_track"].items()):
                        self.add_log(logs, "     %s：%d 个问题" % (_name, _n))
                    if _sum["critical"]:
                        self.log("   逐轨审核：%d 个严重问题（%s）"
                                 % (_sum["critical"],
                                    "、".join("%s×%d" % (k, v) for k, v
                                              in _sum["by_kind"].items())))
                    else:
                        self.log("   逐轨审核：%d 个问题，无阻断项"
                                 % _sum["count"])
            except Exception as _ta_err:  # noqa: BLE001
                if (_ta_err.__class__.__name__
                        == "AudioEvidenceCancelled"):
                    raise workflow_engine.WorkflowCancelled(
                        str(_ta_err)) from _ta_err
                self.add_log(logs, "   逐轨审核跳过：%s" % str(_ta_err)[:80])
        # 混音之前，先在“游戏轨”上把音效压掉。
        # 为什么不在成片上做：混音不可逆，合成后人声和音效已经搅在一起，
        # 只能靠频谱猜（实测误压 8.4% 的人声）。在原始游戏轨上做，
        # 空档 -96dB、音效 -29dB，差 67dB，误压降到 0.8% 且人声零损伤。
        source_path = data["source"]
        if bool(self.director_conf.get("game_audio_enabled", False)):
            try:
                import game_audio
                import index as _index
                import hashlib
                _cache = os.path.join(
                    idx, "game_audio",
                    hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:16]
                    + ".mp4")
                if not os.path.isfile(_cache) or os.path.getsize(_cache) <= 0:
                    os.makedirs(os.path.dirname(_cache), exist_ok=True)
                    self.log("   游戏轨处理：在原始音轨上压音效（混音前）…")
                    _spans = game_audio.suppress_track(
                        source_path, _cache, _index.find_ffmpeg(),
                        track=int(self.director_conf.get(
                            "game_audio_track", 1) or 1))
                    self.log("   游戏轨处理：%d 段音效已压，人声轨未动" % len(_spans))
                else:
                    self.log("   游戏轨处理：复用已处理源片")
                source_path = _cache
            except Exception as _ga_err:  # noqa: BLE001
                self.add_log(logs, "   游戏轨处理跳过：%s" % str(_ga_err)[:80])
                source_path = data["source"]
        # Q2 冻结闸：渲染阶段不许再改切点。审的对象必须就是交付的对象。
        plan_spans = [(float(a), float(b)) for a, b in
                      ((previous.get("plan") or {}).get("final_render_spans") or [])]
        if not plan_spans:
            raise RuntimeError("Q2 冻结：计划里没有 final_render_spans，拒绝渲染")
        if len(plan_spans) != len(spans):
            raise RuntimeError(
                "Q2 冻结：段数不一致（已审计划 %d 段 / 渲染 %d 段）"
                % (len(plan_spans), len(spans)))
        for _i, ((_pa, _pb), (_sa, _sb)) in enumerate(zip(plan_spans, spans), 1):
            if abs(_pa - _sa) > 0.002 or abs(_pb - _sb) > 0.002:
                raise RuntimeError(
                    "Q2 冻结：第 %d 段与已审计划不一致（%.3f→%.3f vs %.3f→%.3f）"
                    % (_i, _pa, _pb, _sa, _sb))
        self.log("   Q2 冻结：渲染时间线与已审计划逐段一致（%d 段）" % len(spans))
        self.log("   [诊断] 传入渲染：%d 段，总长 %.1f 秒"
                 % (len(spans), sum(float(b) - float(a) for a, b in spans)))
        try:
            import json as _json3
            with open(os.path.join(idx, "_spans_before_render.json"),
                      "w", encoding="utf-8") as _span_file:
                _json3.dump([list(map(float, s)) for s in spans],
                            _span_file, ensure_ascii=False)
        except Exception:
            pass
        info = autocut.render(
            source_path, spans, seams, out, conf,
            on_log=lambda m: self.add_log(logs, m), cancel=self.should_cancel,
            subs=subtitles, sub_burn=self.burn_subs,
            # BGM 不在这里混——按流程它要等审核通过之后才作为“新的一条轨”加进去
            # （子枫 11：【剪辑】新增新的一条音轨，轨道随意，这是 bgm 轨道）。
            bgm=False, bgm_conf=bgm_conf,
            tracks=self.tracks, data=data)
        if not os.path.isfile(out) or os.path.getsize(out) <= 0:
            raise RuntimeError("出片流程返回成功，但没有生成有效文件：%s" % out)
        rendered_qc = {"status": "disabled", "passed": None}
        rendered_qc["track_findings"] = track_findings
        rendered_qc_path = None
        learning_reinforcement = {
            "status": "pending_final_review", "rewarded": 0, "penalized": 0,
        }
        if bool(self.director_conf.get("rendered_review_enabled", True)):
            self.log("   交付复审：实际复听输出音轨并检查最终画面接缝…")
            try:
                import rendered_output_qc
                rendered_qc = rendered_output_qc.review_rendered_output(
                    out, timeline=(info or {}).get("timeline") or [],
                    sequence_timeline=(info or {}).get("timeline_v2"),
                    words=self.words(data),
                    segments=data.get("segments") or [],
                    skip_ranges=tuple((info or {}).get("chapter_ranges") or ()),
                    expected_duration=(
                        float(((info or {}).get("timeline_v2") or {}).get(
                            "duration") or 0.0)
                        or (sum(float(row[2]) for row in
                                ((info or {}).get("timeline") or [])
                                if len(row) >= 3)
                            + float((info or {}).get(
                                "chapter_seconds") or 0.0))),
                    selected_tracks=self.selected_tracks(data),
                    expect_audio=bool(data.get("audio_streams")),
                    output_dir=os.path.join(
                        idx, "rendered_output_qc",
                        str(previous.get("plan_sha256") or self.task_id)[:16]),
                    max_nodes=int(self.director_conf.get(
                        "rendered_review_max_nodes", 12) or 12),
                    should_cancel=self.should_cancel,
                    use_vlm=bool(self.director_conf.get("visual_use_vlm", False)),
                    vlm_tier=str(self.director_conf.get(
                        "visual_vlm_tier", "fast") or "fast"))
            except rendered_output_qc.RenderedOutputCancelled as exc:
                raise workflow_engine.WorkflowCancelled(str(exc)) from exc
            rendered_qc["status"] = "passed" if rendered_qc.get("passed") else "needs_rework"
            # 接缝体检：逐处比对声音与画面连续性。它回答的是"观众会不会觉得卡"，
            # 和上面的静态 QC 互补——静态 QC 查"字有没有被切"，这里查"接缝顺不顺"。
            # 只报告不阻断（换切点的自动修复还没做），先积累数据。
            if bool(self.director_conf.get("seam_review_enabled", True)):
                self.log("   接缝体检：逐处比对声音与画面连续性…")
                try:
                    import seam_continuity
                    seams = seam_continuity.score_seams(
                        out, (info or {}).get("timeline") or [],
                        smooth_at=(info or {}).get("smooth_seams") or (),
                        should_cancel=self.should_cancel)
                    # 出片前这一关用“宽松标准”：机器的精度远超人耳
                    # （机器看得见 0.5dB/50Hz，人耳只能听出约 3dB 和明显的音色变化）。
                    # 所以只要接缝跳变落在这个说话人自己的正常分布里就算过。
                    _gate = float(self.director_conf.get(
                        "seam_review_gate", 0.45) or 0.45)
                    summary = seam_continuity.summarize(seams, gate=_gate)
                    rendered_qc["seams"] = summary
                    # Q3 回执 + 交付闸（2026-11-24）：逐缝把三轴值、矩阵行号、
                    # 档位写进 coords_report.json。以前接缝体检只打一句日志，
                    # 判据不落盘、不阻断——"看了等于没看"。
                    try:
                        import coords_report as _cr
                        _points = [float(row.get("at") or 0.0)
                                   for row in seams or []]
                        _rep = _cr.build(out, _points,
                                         ffmpeg=indexer.find_ffmpeg(),
                                         should_cancel=self.should_cancel,
                                         extra={"source": data.get("source"),
                                                "spans": len(spans)})
                        _rep_path = _cr.write(
                            _rep, os.path.join(self.idx, "coords_report.json"))
                        _verdict = _cr.verdict(_rep)
                        rendered_qc["coords"] = _rep["summary"]
                        rendered_qc["coords_gate"] = _verdict
                        rendered_qc["coords_report"] = _rep_path
                        _sum = _rep["summary"]
                        _rel = _sum.get("relative") or {}
                        self.log(
                            "   三轴回执：%d 处接缝，绿 %d / 黄 %d / 红 %d"
                            "（严格超限 %.0f%%；相对超标 %.0f%%，P90 %.1fdB；P0 %d 处）"
                            % (_sum["total"], _sum["green"], _sum["yellow"],
                               _sum["red"], _sum["over_ratio"] * 100,
                               float(_rel.get("over_ratio") or 0.0) * 100,
                               float(_rel.get("p90_db") or 0.0), _sum["p0"]))
                        if not _verdict["passed"]:
                            self.log("   Q3 交付闸：不过（%s）"
                                     % (_verdict.get("reason") or "超标"))
                        else:
                            _lim = _verdict.get("limits") or {}
                            self.log(
                                "   Q3 交付闸：通过（相对超标上限 %.0f%%，P0 上限 %d）"
                                % (float(_lim.get("relative_over") or 0.0) * 100,
                                   int(_lim.get("p0") or 0)))
                    except Exception as _cr_err:  # noqa: BLE001
                        if isinstance(_cr_err, audio_evidence_cache.AudioEvidenceCancelled):
                            raise workflow_engine.WorkflowCancelled(str(_cr_err)) from _cr_err
                        # 回执出不来 = 判不了 = 不许默认通过（E08）。
                        rendered_qc["coords_gate"] = {
                            "passed": False,
                            "reason": "回执生成失败：%s" % str(_cr_err)[:80],
                        }
                        self.add_log(logs, "   三轴回执失败（按不通过处理）：%s"
                                     % str(_cr_err)[:100])
                    if summary["count"]:
                        worst = summary["worst"][0] if summary["worst"] else None
                        if summary["bad"]:
                            self.log(
                                "   接缝体检：%d 处偏卡（阈值 %.2f）%s"
                                % (summary["bad"], summary["gate"],
                                   ("，最低 %.2f 分 @ %.1f 秒"
                                    % (worst["total"], worst["at"]))
                                   if worst else ""))
                            for row in summary["worst"][:5]:
                                if row["total"] >= summary["gate"]:
                                    continue
                                detail = row.get("audio_detail") or {}
                                self.log(
                                    "     卡 %.1f 秒：声音 %.2f / 画面 %.2f（%s）"
                                    % (row["at"], row["audio"], row["visual"],
                                       "人声收尾但背景声断开"
                                       if not detail.get("natural_pause", True)
                                       else "自然停顿"))
                        else:
                            self.log("   接缝体检：%d 处全部顺畅"
                                     % summary["count"])
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, (audio_evidence_cache.AudioEvidenceCancelled,
                                        workflow_engine.WorkflowCancelled)):
                        raise workflow_engine.WorkflowCancelled(str(exc)) from exc
                    self.log("   接缝体检失败（不影响交付）：%s"
                             % str(exc)[:120])
            # 段落体检：句级管"一句话说完"，接缝管"这一刀顺不顺"，
            # 段落管"这一章还讲不讲得完、有没有跳段"。三者尺度不同。
            if bool(self.director_conf.get("paragraph_review_enabled", True)):
                try:
                    import paragraph_qc
                    para = paragraph_qc.summarize(
                        (data.get("script") or {}).get("sections") or [],
                        (info or {}).get("spans") or [])
                    rendered_qc["paragraphs"] = para
                    rows = para.get("chapters") or []
                    if rows:
                        self.log("   段落体检：%d 章，保留 %.0f%%~%.0f%%"
                                 % (len(rows),
                                    min(r["ratio"] for r in rows) * 100,
                                    max(r["ratio"] for r in rows) * 100))
                    for row in para.get("thin") or []:
                        self.log("     第%d章「%s」只留了 %.0f%%，可能讲不完"
                                 % (row["index"], row["title"],
                                    row["ratio"] * 100))
                    for row in para.get("vanished") or []:
                        self.log("     整章消失：第%d章「%s」"
                                 % (row["index"], row["title"]))
                    for row in para.get("jumps") or []:
                        self.log("     第%d章「%s」章内最大跳跃 %.0f 秒"
                                 % (row["index"], row["title"], row["biggest"]))
                except Exception as exc:  # noqa: BLE001
                    self.log("   段落体检失败（不影响交付）：%s"
                             % str(exc)[:120])
            rendered_qc_path = os.path.join(
                idx, "rendered_output_qc",
                str(previous.get("plan_sha256") or self.task_id)[:16],
                "rendered_output_qc.json")
            _atomic_write_json(rendered_qc_path, rendered_qc)
            _coords_gate = rendered_qc.get("coords_gate") or {}
            _coords_ok = bool(_coords_gate.get("passed", True))
            _review_ok = bool(rendered_qc.get("passed")) and _coords_ok
            if not _review_ok:
                _why = ("静态复审未通过（得分 %.1f，否决 %d，重大问题 %d）"
                        % (float(rendered_qc.get("score") or 0.0),
                           int(rendered_qc.get("veto_count") or 0),
                           int(rendered_qc.get("major_issues") or 0))
                        if not rendered_qc.get("passed") else
                        ("三轴交付闸未通过（%s）"
                         % (_coords_gate.get("reason") or "超限率高")))
                # 默认降级：函数只标记问题并返回，不再拦住出片
                self.block_failed_rendered_review(
                    previous, rendered_qc, rendered_qc_path,
                    "实际成片复审未通过：%s" % _why,
                    phase="pre_bgm")
            else:
                self.log("   交付复审：通过 %.1f 分（听觉、视觉和词级时间线证据齐全）"
                         % float(rendered_qc.get("score") or 0.0))
            # ---- 审核通过之后才加 BGM ----
            # 按子枫的流程（11）：审核没问题之后，才「新增一条音轨」放 BGM，
            # 加完再走一遍复审（12）。所以 BGM 在 render 时是关掉的。
            if self.use_bgm:
                try:
                    import bgm as _bgm
                    self.log("   加 BGM：新增一条音轨并混入成片…")
                    _b = _bgm.attach(out, conf=bgm_conf,
                                     log=lambda m: self.add_log(logs, "     " + str(m)))
                    if _b:
                        rendered_qc["bgm"] = _b
                        self.log("      BGM：%s（%.0f 秒起）"
                                 % (str(_b.get("track", ""))[-40:],
                                    float(_b.get("start") or 0)))
                        # attach() 会把原声音和 BGM 混成一条最终音轨并原地替换
                        # 文件，因此旧音频证据已失效。必须对最终文件重新建缓存、
                        # 复听并重写交付报告；不能把同一条混合轨误当作两条轨审。
                        try:
                            pre_bgm_qc = rendered_qc
                            post_bgm_qc = rendered_output_qc.review_rendered_output(
                                out, timeline=(info or {}).get("timeline") or [],
                                sequence_timeline=(info or {}).get("timeline_v2"),
                                words=self.words(data),
                                segments=data.get("segments") or [],
                                skip_ranges=tuple(
                                    (info or {}).get("chapter_ranges") or ()),
                                expected_duration=float(
                                    ((info or {}).get("timeline_v2") or {}).get(
                                        "duration") or 0.0) or None,
                                selected_tracks=[0], expect_audio=True,
                                output_dir=os.path.join(
                                    idx, "rendered_output_qc",
                                    str(previous.get("plan_sha256")
                                        or self.task_id)[:16], "post_bgm"),
                                max_nodes=int(self.director_conf.get(
                                    "rendered_review_max_nodes", 12) or 12),
                                should_cancel=self.should_cancel,
                                use_vlm=bool(self.director_conf.get(
                                    "visual_use_vlm", False)),
                                vlm_tier=str(self.director_conf.get(
                                    "visual_vlm_tier", "fast") or "fast"))
                            post_bgm_qc["status"] = (
                                "passed" if post_bgm_qc.get("passed")
                                else "needs_rework")
                            post_bgm_qc["bgm"] = _b
                            post_bgm_qc["pre_bgm"] = {
                                "passed": pre_bgm_qc.get("passed"),
                                "score": pre_bgm_qc.get("score"),
                                "sequence_revision": pre_bgm_qc.get(
                                    "sequence_revision"),
                            }
                            rendered_qc = post_bgm_qc
                            if not rendered_qc.get("passed"):
                                self.block_failed_rendered_review(
                                    previous, rendered_qc, rendered_qc_path,
                                    "BGM 混音后的最终成片复审未通过（得分 %.1f，否决 %d）"
                                    % (float(rendered_qc.get("score") or 0.0),
                                       int(rendered_qc.get("veto_count") or 0)),
                                    phase="post_bgm")
                            if rendered_qc.get("passed"):
                                self.log("   加 BGM 后复审：最终混合音轨通过 %.1f 分"
                                         % float(rendered_qc.get("score") or 0.0))
                        except rendered_output_qc.RenderedOutputCancelled as exc:
                            raise workflow_engine.WorkflowCancelled(str(exc)) from exc
                except (RenderedOutputReviewFailed,
                        workflow_engine.WorkflowCancelled):
                    raise
                except Exception as _bgm_err:  # noqa: BLE001
                    self.add_log(logs, "   加 BGM 跳过：%s" % str(_bgm_err)[:80])
            if rendered_qc_path:
                _atomic_write_json(rendered_qc_path, rendered_qc)
            if (rendered_qc.get("passed") and
                    not getattr(self, "_render_degraded", None)):
                try:
                    learning_reinforcement = self.reinforce_director_learning(
                        previous, rendered_qc, True)
                    if learning_reinforcement.get("rewarded"):
                        self.log("   导演学习：成片验证通过，奖励 %d 条经验；重复成功后才会晋升" %
                                 learning_reinforcement["rewarded"])
                except Exception as exc:  # noqa: BLE001
                    self.log("   导演学习奖励保存失败（不影响已通过成片）：%s" % str(exc)[:120])
            else:
                # Delivering a reviewable draft is not a successful review.
                # Keep the negative feedback already recorded at the gate;
                # never reward the same failed cut just because it was saved.
                learning_reinforcement = dict(rendered_qc.get(
                    "learning_reinforcement") or {
                        "status": "skipped", "rewarded": 0, "penalized": 0,
                        "reason": "review_not_passed"})
        previous.update({"out": out, "info": info, "logs": logs,
                         "rendered_output_qc": rendered_qc,
                         "rendered_output_qc_path": rendered_qc_path,
                         "learning_reinforcement": learning_reinforcement})
        self.done("出片", t0)
        return workflow_engine.StageResult(
            self.artifact("renderer"), previous,
            {"artifact_type": "render", "path": out})

    def finalize(self, ctx):
        t0 = time.time()
        previous = self.payload(ctx, "renderer")
        idx = previous.get("index_dir") or self.idx
        data = autocut.load_index(idx)
        duration = float(previous.get("duration") or data.get("duration") or 0)
        spans = self.spans(previous.get("spans"))
        conf = previous.get("conf") or {}
        kinds = dict(previous.get("kinds") or {})
        logs = list(previous.get("logs") or [])
        out = str(previous.get("out") or "")
        info = previous.get("info") or {}
        made = []
        try:
            timeline = ((info.get("timeline_v2") or info.get("timeline"))
                        if isinstance(info, dict) else None)
            if timeline:
                sub_info = autocut.export_subs(
                    data, out, timeline, res=autocut.out_resolution(conf, data))
                for key in ("srt", "ass"):
                    path = (sub_info or {}).get(key)
                    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                        made.append(path)
                if made:
                    self.log("字幕：已生成 %d 个旁挂文件" % len(made))
                elif data.get("segments"):
                    self.log("字幕：成片时间轴里没有可导出的台词")
            elif data.get("segments"):
                self.log("字幕没导出：出片结果里缺少 timeline")
        except Exception as exc:  # noqa: BLE001
            self.log("字幕没导出（不影响成片）：%s" % str(exc)[:120])
        minutes = float(info.get("minutes") or 0)
        report = os.path.join(self.out_dir, self.stem + "_自动任务.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("# 自动任务报告：%s\n\n" % self.stem)
            fh.write("- 任务：%s\n" % self.task_id)
            fh.write("- 原片：%.1f 分钟\n- 成片：%.1f 分钟（保留 %.0f%%）\n"
                     % (duration / 60, minutes,
                        minutes * 60 / max(1, duration) * 100))
            fh.write("- 剪辑模式：%s（%s）\n" %
                     (edit_modes.label(self.mode), self.mode))
            fh.write("- 段数：%d / 单段中位 %.1f 秒\n" %
                     (len(spans), _median(spans)))
            fh.write("- 转场：%s\n- 编码：%s\n- BGM：%s\n"
                     % (kinds, conf.get("render", {}).get("encoder"),
                        "开（分段换曲）" if self.use_bgm else "关"))
            fh.write("- 字幕：%d 个%s\n"
                     % (len(made), ("（%s）" % "、".join(
                         os.path.basename(p) for p in made)) if made else ""))
            rendered_qc = previous.get("rendered_output_qc") or {}
            fh.write("- 实际成片复审：%s%s\n" % (
                ("未启用" if rendered_qc.get("status") == "disabled" else
                 ("通过" if rendered_qc.get("passed") else "未通过")),
                ("（%.1f 分）" % float(rendered_qc.get("score")))
                if rendered_qc.get("score") is not None else ""))
            director_review = previous.get("director_multimodal_audit") or {}
            debrief = director_review.get("debrief") or {}
            reinforcement = previous.get("learning_reinforcement") or {}
            if debrief:
                fh.write("- 导演复盘：发现 %d / 已解决 %d / 剩余 %d\n" % (
                    int(debrief.get("issues_discovered") or 0),
                    int(debrief.get("issues_resolved") or 0),
                    int(debrief.get("remaining_serious") or 0)))
                fh.write("- 学习结果：候选 %d 条 / 奖励 %d 条 / 负反馈 %d 条\n" % (
                    len(director_review.get("learning_candidate_ids") or []),
                    int(reinforcement.get("rewarded") or 0),
                    int(reinforcement.get("penalized") or 0)))
            fh.write("- 总耗时：%.0f 秒\n\n## 阶段耗时\n" %
                     (time.time() - self.started))
            for name, seconds in self.stages:
                fh.write("- %s：%.1f 秒\n" % (name, seconds))
            if debrief:
                fh.write("\n## 导演教导与学习\n")
                for lesson in debrief.get("lessons") or []:
                    result_label = {
                        "candidate": "候选经验（等待成片/后续任务强化）",
                        "validated": "已验证经验",
                        "task_only": "仅当前任务",
                    }.get(str(lesson.get("result") or ""),
                          str(lesson.get("result") or "未知"))
                    fh.write("- %s：%s；%s；证据：%s\n" % (
                        str(lesson.get("role") or "导演"),
                        str(lesson.get("rule") or ""), result_label,
                        str(lesson.get("evidence") or "")))
                fh.write("\n%s\n" % str(debrief.get("promotion_policy") or ""))
            fh.write("\n## 决策日志\n```\n")
            fh.write("\n".join(logs[-60:]))
            fh.write("\n```\n")
        self.done("字幕与收尾", t0)
        self.log("⑧ 完成：%s（%.1f 分钟）" % (out, minutes))
        result = {
            "out": out, "report": report, "minutes": minutes,
            "segments": len(spans), "stages": list(self.stages),
            "total_sec": round(time.time() - self.started, 1),
            "index_dir": idx, "subtitles": made, "edit_mode": self.mode,
            "task_id": self.task_id, "workflow": workflow_engine.FULL_AUTO,
            "plan_path": previous.get("plan_path"),
            "plan_sha256": previous.get("plan_sha256"),
            "director_passed": bool((previous.get("director_multimodal_audit") or {}).get("passed")),
            "rendered_review_passed": (
                previous.get("rendered_output_qc") or {}).get("passed"),
            "rendered_review_report": previous.get("rendered_output_qc_path"),
            "director_debrief": (
                previous.get("director_multimodal_audit") or {}).get("debrief"),
            "learning_reinforcement": previous.get("learning_reinforcement"),
        }
        return workflow_engine.StageResult(
            self.artifact("finalize"), result,
            {"artifact_type": "final", "path": out})

    def handlers(self):
        return {name: getattr(self, name) for name in workflow_engine.STAGE_ORDER}


def _prepare_memory(memory, task_id, src, mode, level):
    if memory is None:
        return
    get_task = getattr(memory, "get_task", None)
    create_task = getattr(memory, "create_task", None)
    if callable(get_task) and callable(create_task) and get_task(task_id) is None:
        create_task({"sources": [os.path.abspath(src)]}, mode=mode, level=level,
                    task_id=task_id, status="pending")


def _set_task_status(memory, task_id, status, current_stage=None):
    setter = getattr(memory, "set_task_status", None) if memory is not None else None
    if callable(setter):
        setter(task_id, status, current_stage=current_stage)


def run_one(src: str, level="medium", use_bgm=True, index_dir=None,
            on_log=print, force_reindex=False, tracks=None, output_dir=None,
            burn_subs=False, loudness_norm=None, should_cancel=None,
            mode="gameplay", workflow=workflow_engine.FULL_AUTO,
            task_id=None, memory=None, resume=False, stage_handlers=None,
            on_progress=None, model_editor=False, keep_ratio=None):
    """Run neutral analysis or the complete role workflow.
    """
    import cut_strength
    from llm_gateway import api_enabled
    model_editor = bool(model_editor) or api_enabled()
    task_id = str(task_id or _new_task_id(src))
    mode = edit_modes.normalize(mode)
    if keep_ratio is None:
        keep_ratio = cut_strength.normalize_ratio(
            cut_strength.LEGACY_KEEP_RATIO.get(str(level).lower()))
    else:
        keep_ratio = cut_strength.normalize_ratio(keep_ratio)
    level = cut_strength.level_for_ratio(keep_ratio)
    runtime = _WorkflowRun(
        src, level, use_bgm, index_dir, on_log, force_reindex, tracks,
        output_dir, burn_subs, loudness_norm, should_cancel, mode, task_id,
        memory=memory, on_progress=on_progress, model_editor=model_editor,
        keep_ratio=keep_ratio)
    handlers = runtime.handlers()
    if stage_handlers:
        handlers.update(dict(stage_handlers))
    _prepare_memory(memory, task_id, src, mode, level)
    backend = memory if resume or memory is None else _RecordingMemory(memory)
    run_signature = _run_signature(
        src, runtime.idx, mode, level, tracks, use_bgm,
        burn_subs, loudness_norm, workflow, model_editor=model_editor,
        keep_ratio=keep_ratio)
    task = workflow_engine.TaskSpec(
        task_id, os.path.abspath(src), workflow=workflow,
        mode=mode, level=level,
        metadata={"index_dir": runtime.idx, "output_dir": runtime.out_dir,
                  "run_signature": run_signature,
                  "keep_ratio": keep_ratio})
    _set_task_status(memory, task_id, "running", "analyze")
    try:
        execution = workflow_engine.SerialExecutor(handlers, backend).run(
            task, should_cancel=should_cancel)
    finally:
        runtime.close()
    if execution.status == "cancelled":
        _set_task_status(memory, task_id, "cancelled", execution.current_stage)
        raise autocut.RenderCancelled("已取消自动任务（%s）" %
                                      (execution.current_stage or "等待阶段"))
    if execution.status != "completed":
        _set_task_status(memory, task_id, "failed", execution.current_stage)
        raise RuntimeError("自动任务在 %s 阶段失败：%s" %
                           (execution.current_stage or "未知", execution.error or "未知错误"))
    _set_task_status(memory, task_id, "completed", None)
    if workflow == workflow_engine.ANALYSIS_ONLY:
        payload = execution.outputs["analyze"].payload or {}
        return {"task_id": task_id, "workflow": workflow,
                "index_dir": payload.get("index_dir") or runtime.idx,
                "stages": list(runtime.stages), "edit_mode": mode}
    if workflow == workflow_engine.PREMIERE_EDIT:
        transition = execution.outputs.get("transition")
        if transition is None or not isinstance(transition.payload, dict):
            raise RuntimeError("PR 剪辑流程完成，但缺少导演定稿时间轴")
        previous = dict(transition.payload)
        spans = runtime.spans(previous.get("spans"))
        plan = dict(previous.get("plan") or {})
        if plan.get("final_render_spans"):
            spans = runtime.spans(plan.get("final_render_spans"))
        result = {
            "task_id": task_id,
            "workflow": workflow,
            "index_dir": previous.get("index_dir") or runtime.idx,
            "edit_mode": mode,
            "level": level,
            "keep_ratio": keep_ratio,
            "spans": [[a, b] for a, b in spans],
            "seams": list(previous.get("seams") or []),
            "plan": plan,
            "plan_path": previous.get("plan_path"),
            "plan_sha256": previous.get("plan_sha256"),
            "director_passed": bool(
                (previous.get("director_multimodal_audit") or {}).get("passed")),
            "director_debrief": (
                previous.get("director_multimodal_audit") or {}).get("debrief"),
            "stages": list(runtime.stages),
        }
        degraded = list(getattr(runtime, "_director_degraded", None) or [])
        if degraded:
            result["director_degraded"] = degraded
            result["director_passed"] = False
        return result
    final = execution.outputs.get("finalize")
    if final is None or not isinstance(final.payload, dict):
        raise RuntimeError("自动任务完成，但缺少最终成果")
    payload = dict(final.payload)
    degraded = list(getattr(runtime, "_director_degraded", None) or [])
    render_degraded = list(getattr(runtime, "_render_degraded", None) or [])
    if degraded:
        # 默认降级出片的记录：界面据此提示"已出片，N 处待修"
        payload["director_degraded"] = degraded
        payload["director_passed"] = False
    if render_degraded:
        payload["render_degraded"] = render_degraded
        payload["review_passed"] = False
    return payload


def _median(spans):
    import statistics
    lens = [b - a for a, b in spans]
    return statistics.median(lens) if lens else 0.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="自动任务：分析 → 剪辑 → 出片")
    ap.add_argument("target", help="视频文件，或素材文件夹（批量）")
    ap.add_argument("--level", default="medium", choices=["light", "medium", "heavy"])
    ap.add_argument("--keep-percent", type=float, default=None,
                    help="目标保留有效内容百分比（15~100）；优先于旧 --level")
    ap.add_argument("--mode", default=edit_modes.DEFAULT_MODE,
                    choices=[key for key, _label, _desc in edit_modes.choices()],
                    help="剪辑专项模式")
    ap.add_argument("--no-bgm", action="store_true")
    ap.add_argument("--reindex", action="store_true", help="强制重新分析")
    args = ap.parse_args(argv)

    tgt = args.target
    if os.path.isdir(tgt):
        vids = [os.path.join(tgt, f) for f in os.listdir(tgt)
                if f.lower().endswith((".mp4", ".mkv", ".mov"))]
    else:
        vids = [tgt]
    ok = 0
    for v in vids:
        print("=" * 70)
        print("自动任务：", os.path.basename(v))
        try:
            run_one(v, level=args.level, keep_ratio=args.keep_percent,
                    mode=args.mode, use_bgm=not args.no_bgm,
                    on_log=print, force_reindex=args.reindex)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("失败：", exc)
    print("完成 %d/%d" % (ok, len(vids)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

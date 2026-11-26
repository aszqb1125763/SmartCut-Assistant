# -*- coding: utf-8 -*-
"""Role-oriented workflow primitives for SmartCut.

This module deliberately has no Qt, media, model, or persistence dependency.
Production code injects stage handlers and an optional memory backend.  The
same graph can therefore run synchronously today and be scheduled by resource
aware workers later without changing the role contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple


ANALYSIS_ONLY = "analysis_only"
FULL_AUTO = "full_auto"
PREMIERE_EDIT = "premiere_edit"
WORKFLOW_TYPES = frozenset((ANALYSIS_ONLY, PREMIERE_EDIT, FULL_AUTO))

STAGE_ORDER = (
    "analyze",
    "writer",
    "editor",
    "boundary",
    "director",
    "transition",
    "renderer",
    "finalize",
)


@dataclass(frozen=True)
class StageSpec:
    """Static role-node definition retained in every queued job."""

    name: str
    role: str
    dependencies: Tuple[str, ...] = ()
    resources: Mapping[str, Any] = field(default_factory=dict)
    idempotent: bool = True

    def job_descriptor(self, task_id: str) -> Dict[str, Any]:
        return {
            "job_id": "%s:%s" % (task_id, self.name),
            "task_id": task_id,
            "stage": self.name,
            "role": self.role,
            "dependencies": list(self.dependencies),
            "resources": dict(self.resources),
            "idempotent": bool(self.idempotent),
        }


STAGES: Dict[str, StageSpec] = {
    "analyze": StageSpec(
        "analyze", "analyst", (),
        {"cpu": 1, "gpu": "optional", "disk": "high"}),
    "writer": StageSpec(
        "writer", "writer", ("analyze",),
        {"cpu": 1, "gpu": "optional", "vram_mb": "model-dependent"}),
    "editor": StageSpec(
        "editor", "editor", ("writer",),
        {"cpu": 1, "gpu": "optional"}),
    "boundary": StageSpec(
        "boundary", "boundary", ("editor",),
        {"cpu": 1, "gpu": "optional", "disk": "medium"}),
    "director": StageSpec(
        "director", "director", ("boundary",),
        {"cpu": 1, "gpu": "optional", "vram_mb": "model-dependent"}),
    "transition": StageSpec(
        "transition", "transition", ("director",),
        {"cpu": 1, "gpu": "optional"}),
    "renderer": StageSpec(
        "renderer", "renderer", ("transition",),
        {"cpu": "configurable", "gpu": "preferred", "disk": "high"}),
    "finalize": StageSpec(
        "finalize", "finalizer", ("renderer",),
        {"cpu": 1, "disk": "medium"}),
}


class WorkflowMemory(Protocol):
    """Minimal persistence contract; concrete storage is injected at runtime."""

    def get_stage_state(self, task_id: str, stage: str) -> Optional[Mapping[str, Any]]:
        ...

    def record_stage_state(
            self, task_id: str, stage: str, status: str, **fields: Any) -> None:
        ...


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    source: str
    workflow: str = FULL_AUTO
    mode: str = "gameplay"
    level: str = "medium"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.task_id).strip():
            raise ValueError("task_id cannot be empty")
        if not str(self.source).strip():
            raise ValueError("source cannot be empty")
        if self.workflow not in WORKFLOW_TYPES:
            raise ValueError("unsupported workflow: %s" % self.workflow)
        if self.level not in ("light", "medium", "heavy"):
            raise ValueError("unsupported edit level: %s" % self.level)

    @property
    def stages(self) -> Tuple[str, ...]:
        if self.workflow == ANALYSIS_ONLY:
            return ("analyze",)
        if self.workflow == PREMIERE_EDIT:
            # Premiere owns the editable timeline and final export.  Reuse the
            # complete editorial chain, but stop before media rendering.
            return STAGE_ORDER[:6]
        return STAGE_ORDER


@dataclass(frozen=True)
class StageResult:
    artifact_id: Optional[str] = None
    payload: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class WorkflowCancelled(RuntimeError):
    pass


class MissingStageHandler(RuntimeError):
    pass


@dataclass
class StageContext:
    task: TaskSpec
    stage: StageSpec
    input_artifact_ids: Tuple[str, ...]
    outputs: Mapping[str, StageResult]
    should_cancel: Callable[[], bool]

    def cancel_check(self) -> None:
        if self.should_cancel():
            raise WorkflowCancelled("task cancelled during %s" % self.stage.name)


@dataclass(frozen=True)
class ExecutionResult:
    task_id: str
    status: str
    completed_stages: Tuple[str, ...]
    outputs: Mapping[str, StageResult]
    current_stage: Optional[str] = None
    error: Optional[str] = None


StageHandler = Callable[[StageContext], Any]


def _coerce_result(value: Any) -> StageResult:
    if isinstance(value, StageResult):
        return value
    if isinstance(value, Mapping):
        known = {"artifact_id", "payload", "metadata"}
        if known.intersection(value):
            return StageResult(
                artifact_id=value.get("artifact_id"),
                payload=value.get("payload"),
                metadata=value.get("metadata") or {},
            )
        return StageResult(payload=dict(value))
    if isinstance(value, str):
        return StageResult(artifact_id=value)
    return StageResult(payload=value)


class InMemoryWorkflowMemory:
    """Small reference backend useful for tests and command-line prototypes."""

    def __init__(self) -> None:
        self.states: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.events = []

    def get_stage_state(self, task_id: str, stage: str) -> Optional[Mapping[str, Any]]:
        row = self.states.get((task_id, stage))
        return dict(row) if row else None

    def record_stage_state(
            self, task_id: str, stage: str, status: str, **fields: Any) -> None:
        row = {"task_id": task_id, "stage": stage, "status": status}
        row.update(fields)
        self.states[(task_id, stage)] = row
        self.events.append(dict(row))


class SerialExecutor:
    """Execute one task and one role node at a time in dependency order."""

    max_active_jobs = 1

    def __init__(
            self,
            handlers: Mapping[str, StageHandler],
            memory: Optional[WorkflowMemory] = None,
            stage_specs: Optional[Mapping[str, StageSpec]] = None) -> None:
        self.handlers = dict(handlers)
        self.memory = memory
        self.stage_specs = dict(stage_specs or STAGES)
        self._run_lock = threading.Lock()

    def _state(self, task_id: str, stage: str) -> Optional[Mapping[str, Any]]:
        if self.memory is None:
            return None
        getter = getattr(self.memory, "get_stage_state", None)
        return getter(task_id, stage) if callable(getter) else None

    def _record(self, task_id: str, stage: str, status: str, **fields: Any) -> None:
        if self.memory is None:
            return
        recorder = getattr(self.memory, "record_stage_state", None)
        if callable(recorder):
            recorder(task_id, stage, status, **fields)

    @staticmethod
    def _cancelled(should_cancel: Optional[Callable[[], bool]]) -> bool:
        return bool(should_cancel and should_cancel())

    def run(
            self,
            task: TaskSpec,
            should_cancel: Optional[Callable[[], bool]] = None) -> ExecutionResult:
        cancel = should_cancel or (lambda: False)
        completed = []
        outputs: Dict[str, StageResult] = {}

        with self._run_lock:
            for stage_name in task.stages:
                spec = self.stage_specs[stage_name]
                state = self._state(task.task_id, stage_name)
                expected_signature = task.metadata.get("run_signature")
                state_metadata = (state or {}).get("metadata") or {}
                signature_matches = (not expected_signature or
                                     state_metadata.get("run_signature") == expected_signature)
                if (spec.idempotent and state and state.get("status") == "completed"
                        and signature_matches):
                    result = StageResult(
                        artifact_id=state.get("output_artifact_id"),
                        payload=state.get("payload"),
                        metadata=state.get("metadata") or {},
                    )
                    outputs[stage_name] = result
                    completed.append(stage_name)
                    continue

                dependency_states = set(completed)
                missing = [name for name in spec.dependencies
                           if name not in dependency_states]
                if missing:
                    error = "stage %s blocked by %s" % (
                        stage_name, ", ".join(missing))
                    self._record(task.task_id, stage_name, "failed", error=error,
                                 job=spec.job_descriptor(task.task_id))
                    return ExecutionResult(
                        task.task_id, "failed", tuple(completed), dict(outputs),
                        stage_name, error)

                input_ids = tuple(
                    outputs[name].artifact_id for name in spec.dependencies
                    if outputs.get(name) and outputs[name].artifact_id)
                job = spec.job_descriptor(task.task_id)

                if self._cancelled(cancel):
                    self._record(task.task_id, stage_name, "cancelled",
                                 input_artifact_ids=list(input_ids), job=job)
                    return ExecutionResult(
                        task.task_id, "cancelled", tuple(completed), dict(outputs),
                        stage_name, "task cancelled")

                handler = self.handlers.get(stage_name)
                if handler is None:
                    error = "no handler registered for stage: %s" % stage_name
                    self._record(task.task_id, stage_name, "failed",
                                 input_artifact_ids=list(input_ids), error=error,
                                 job=job)
                    return ExecutionResult(
                        task.task_id, "failed", tuple(completed), dict(outputs),
                        stage_name, error)

                self._record(
                    task.task_id, stage_name, "started",
                    input_artifact_ids=list(input_ids),
                    started_at=time.time(), job=job)
                context = StageContext(task, spec, input_ids, dict(outputs), cancel)

                try:
                    context.cancel_check()
                    result = _coerce_result(handler(context))
                    context.cancel_check()
                except WorkflowCancelled as exc:
                    self._record(
                        task.task_id, stage_name, "cancelled",
                        input_artifact_ids=list(input_ids), error=str(exc), job=job)
                    return ExecutionResult(
                        task.task_id, "cancelled", tuple(completed), dict(outputs),
                        stage_name, str(exc))
                except Exception as exc:  # noqa: BLE001
                    error = "%s: %s" % (type(exc).__name__, exc)
                    self._record(
                        task.task_id, stage_name, "failed",
                        input_artifact_ids=list(input_ids), error=error, job=job)
                    return ExecutionResult(
                        task.task_id, "failed", tuple(completed), dict(outputs),
                        stage_name, error)

                outputs[stage_name] = result
                completed.append(stage_name)
                metadata = dict(result.metadata)
                if expected_signature:
                    metadata["run_signature"] = expected_signature
                self._record(
                    task.task_id, stage_name, "completed",
                    input_artifact_ids=list(input_ids),
                    output_artifact_id=result.artifact_id,
                    payload=result.payload,
                    metadata=metadata,
                    completed_at=time.time(), job=job)

        return ExecutionResult(
            task.task_id, "completed", tuple(completed), dict(outputs))


__all__ = [
    "ANALYSIS_ONLY", "FULL_AUTO", "STAGE_ORDER", "STAGES",
    "ExecutionResult", "InMemoryWorkflowMemory", "MissingStageHandler",
    "SerialExecutor", "StageContext", "StageResult", "StageSpec",
    "TaskSpec", "WorkflowCancelled", "WorkflowMemory",
]

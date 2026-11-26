# -*- coding: utf-8 -*-
"""Evidence-backed issue state for repeated, scoped director reviews.

Finding ids and suggested edit intervals describe one observation, not an
issue's identity.  Callers should give audio findings a stable ``anchor_key``
for their source cut lineage, plus ``track_id`` and the cut side.  An explicit
``issue_key`` overrides all inferred identity fields.
"""
from __future__ import annotations

import copy
import hashlib
import json


SCHEMA = "director_issue_ledger_v1"
VERSION = 1
_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3}
_OPEN = "open"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _values(value):
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        return [value]
    return list(value)


def _portable(value):
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_portable(item) for item in sorted(value, key=_json)]
    if isinstance(value, (tuple, list)):
        return [_portable(item) for item in value]
    return copy.deepcopy(value)


def _side(finding):
    side = str(finding.get("side") or finding.get("review_kind") or "")
    if side in ("start", "cut_start", "in", "head"):
        return "start"
    if side in ("end", "cut_end", "out", "tail"):
        return "end"
    kind = str(finding.get("type") or "")
    action = str(finding.get("action") or "")
    if kind == "speech_cut_start" or action == "expand_start":
        return "start"
    if kind in ("speech_cut_end", "important_audio_tail") or action == "expand_end":
        return "end"
    return side


def _track(finding):
    for field in ("track_id", "source_track", "track"):
        if finding.get(field) is not None:
            return str(finding[field])
    tracks = [str(ref)[6:] for ref in finding.get("evidence_refs") or []
              if str(ref).startswith("audio:")]
    return ",".join(sorted(set(tracks)))


def _kind(finding):
    kind = str(finding.get("issue_type") or finding.get("type") or "unknown")
    if kind in ("speech_cut_start", "speech_cut_end", "unconfirmed_boundary_activity"):
        return "speech_boundary"
    return kind


def _blocking(finding):
    return bool(finding.get("veto") or finding.get("severity") in ("major", "critical"))


def _identity(finding):
    explicit = finding.get("issue_key")
    if explicit is not None and explicit != "":
        return ["explicit", explicit]
    modality = str(finding.get("modality") or "")
    anchor = finding.get("anchor_key")
    if anchor is not None and anchor != "":
        return ["anchor", modality, anchor, _track(finding), _kind(finding), _side(finding)]
    indices = sorted(set(_values(finding.get("indices"))), key=str)
    events = sorted(set(str(value) for value in _values(finding.get("event_ids"))))
    refs = sorted(set(str(value) for value in finding.get("evidence_refs") or []
                      if not str(value).startswith(("text_issue:", "review:",
                                                    "source_timeline:", "timeline:"))))
    if modality == "text" or indices or events:
        return ["text", _kind(finding), str(finding.get("action") or ""),
                indices, events, [] if (indices or events) else refs]
    # A caller without an anchor can still use immutable source evidence.
    # Never use the model's per-observation id, wording, severity or revision.
    return ["evidence", modality, _kind(finding), _track(finding),
            _side(finding), refs, finding.get("source_interval")]


class DirectorIssueLedger:
    """Maintain unique issues across reviews; close only verified coverage.

    ``coverage=None, complete=True`` means the whole scope was reviewed.
    Otherwise coverage can contain issue ids, explicit keys or anchor keys,
    or a mapping with ``issue_ids``, ``issue_keys``, ``anchor_keys`` and/or
    ``anchors``.  ``anchors`` accepts dictionaries whose optional ``track_id``
    and ``side`` restrict a local review to one track/edge.  Missing findings
    are never closed by an incomplete observation.

    Different scopes keep separate review state.  An issue shared by scopes
    remains open until each scope that reported it has verified its closure.
    Replaying an already superseded revision does not overwrite newer state.
    """

    def __init__(self, snapshot=None):
        self._entries = {}
        self._scopes = {}
        self._sequence = 0
        self._revision = None
        if snapshot is not None:
            if snapshot.get("schema") != SCHEMA:
                raise ValueError("unsupported director issue ledger schema")
            self._entries = {str(row["issue_id"]): copy.deepcopy(row)
                             for row in snapshot.get("entries") or []}
            self._scopes = copy.deepcopy(snapshot.get("scopes") or {})
            self._sequence = int(snapshot.get("sequence") or 0)
            self._revision = snapshot.get("revision")

    @staticmethod
    def issue_id_for(finding):
        token = _json(_identity(finding))
        return "issue_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _covered(entry, coverage):
        if coverage is None or coverage is True or coverage == "all":
            return True
        if coverage is False:
            return False
        if isinstance(coverage, dict):
            if coverage.get("all") is True:
                return True
            if entry["issue_id"] in {str(x) for x in _values(coverage.get("issue_ids"))}:
                return True
            if _json(entry.get("explicit_issue_key")) in {
                    _json(x) for x in _values(coverage.get("issue_keys"))}:
                return True
            if _json(entry.get("anchor_key")) in {
                    _json(x) for x in _values(coverage.get("anchor_keys"))}:
                return True
            anchors = coverage.get("anchors") or []
            if isinstance(anchors, dict):
                anchors = [anchors]
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    if _json(anchor) == _json(entry.get("anchor_key")):
                        return True
                    continue
                if _json(anchor.get("anchor_key")) != _json(entry.get("anchor_key")):
                    continue
                if (anchor.get("track_id") is not None and
                        str(anchor["track_id"]) != entry.get("track_id")):
                    continue
                if anchor.get("side") is not None and _side(anchor) != entry.get("side"):
                    continue
                return True
            return False
        keys = {_json(value) for value in _values(coverage)}
        return any(_json(value) in keys for value in (
            entry["issue_id"], entry.get("explicit_issue_key"), entry.get("anchor_key"))
                   if value is not None)

    @staticmethod
    def _sync(entry):
        states = list(entry["scopes"].values())
        if any(state["status"] == _OPEN for state in states):
            entry["status"] = _OPEN
            entry["resolution"] = None
        else:
            # Public/global status stays binary for the rework-order bridge;
            # a handled downgrade keeps its distinction in the proof below.
            entry["status"] = "resolved"
            resolutions = [state["resolution"] for state in states if state.get("resolution")]
            entry["resolution"] = copy.deepcopy(max(
                resolutions, key=lambda row: row["sequence"])) if resolutions else None

    def _resolve(self, entry, scope, revision, outcome, finding=None):
        state = entry["scopes"][scope]
        if state["status"] != _OPEN:
            return
        resolution = {"scope": scope, "revision": revision,
                      "sequence": self._sequence, "basis": outcome}
        if finding is not None:
            resolution["evidence"] = copy.deepcopy(finding)
        state.update(status="handled" if outcome == "non_blocking_evidence" else "resolved",
                     resolution=resolution, last_review_revision=revision)
        entry["last_resolution"] = copy.deepcopy(resolution)
        entry["last_review_revision"] = revision
        self._sync(entry)

    def observe(self, scope, findings, revision, coverage=None, complete=True,
                allow_revisit=False):
        """Record a current scoped review and return deduplicated observations.

        Serious findings are recorded even in an incomplete review.  A new
        minor/info finding is returned but does not inflate the issue count.
        Existing issues close only after a complete review covering them.
        """
        scope, revision = str(scope), str(revision or "")
        if not scope or not revision:
            raise ValueError("scope and timeline revision are required")
        previous = self._scopes.get(scope) or {}
        seen_revisions = list(previous.get("seen_revisions") or [])
        if (not allow_revisit and revision in seen_revisions and
                previous.get("revision") != revision):
            return copy.deepcopy(previous.get("findings") or [])
        self._sequence += 1
        self._revision = revision
        if revision not in seen_revisions:
            seen_revisions.append(revision)
        selected = {}
        for item in findings or []:
            row = copy.deepcopy(item)
            issue_id = self.issue_id_for(row)
            row["issue_id"] = issue_id
            previous_row = selected.get(issue_id)
            rank = (bool(row.get("veto")), _RANK.get(row.get("severity"), 0),
                    float(row.get("confidence") or 0.0))
            old_rank = ((bool(previous_row.get("veto")),
                         _RANK.get(previous_row.get("severity"), 0),
                         float(previous_row.get("confidence") or 0.0))
                        if previous_row is not None else (-1, -1, -1))
            if rank > old_rank:
                selected[issue_id] = row
        rows = list(selected.values())
        present = set(selected)
        for row in rows:
            issue_id = row["issue_id"]
            entry = self._entries.get(issue_id)
            if entry is None and not _blocking(row):
                continue
            if entry is None:
                entry = {
                    "issue_id": issue_id, "identity": _identity(row),
                    "explicit_issue_key": copy.deepcopy(row.get("issue_key")),
                    "anchor_key": copy.deepcopy(row.get("anchor_key")),
                    "track_id": _track(row), "side": _side(row),
                    "modality": str(row.get("modality") or ""), "type": _kind(row),
                    "scope": scope, "finding": copy.deepcopy(row),
                    "status": _OPEN, "created_revision": revision,
                    "created_sequence": self._sequence, "seen_count": 0,
                    "reopened": 0, "scopes": {}, "resolution": None,
                    "history": [],
                }
                self._entries[issue_id] = entry
            was_closed = entry["status"] != _OPEN
            state = entry["scopes"].get(scope)
            if state is None:
                state = {"status": _OPEN if _blocking(row) else "handled",
                         "resolution": None, "finding": copy.deepcopy(row)}
                entry["scopes"][scope] = state
            entry["seen_count"] += 1
            entry["last_seen_revision"] = revision
            entry["last_review_revision"] = revision
            entry["last_finding"] = copy.deepcopy(row)
            entry["finding"] = copy.deepcopy(row)
            state["last_seen_revision"] = revision
            state["last_review_revision"] = revision
            if _blocking(row):
                state.update(status=_OPEN, resolution=None, finding=copy.deepcopy(row))
                if was_closed:
                    entry["reopened"] += 1
                self._sync(entry)
            elif complete and self._covered(entry, coverage):
                self._resolve(entry, scope, revision, "non_blocking_evidence", row)
            entry["history"].append({
                "scope": scope, "revision": revision, "sequence": self._sequence,
                "severity": row.get("severity"), "veto": bool(row.get("veto")),
                "finding_id": row.get("id"), "status": state["status"],
            })
            # Bound current-task memory while retaining the last complete proof.
            entry["history"] = entry["history"][-64:]
        if complete:
            for issue_id, entry in self._entries.items():
                if (issue_id not in present and scope in entry["scopes"] and
                        self._covered(entry, coverage)):
                    self._resolve(entry, scope, revision, "covered_review_no_longer_reports_issue")
        self._scopes[scope] = {
            "revision": revision, "seen_revisions": seen_revisions,
            "sequence": self._sequence, "complete": bool(complete),
            "coverage": _portable(coverage),
            "findings": copy.deepcopy(rows),
        }
        return rows

    def counts(self):
        entries = list(self._entries.values())
        remaining = sum(entry["status"] == _OPEN for entry in entries)
        return {"total": len(entries), "resolved": len(entries) - remaining,
                "remaining": remaining, "reopened": sum(
                    int(entry.get("reopened") or 0) for entry in entries)}

    def pending(self, scope=None):
        rows = []
        for entry in self._entries.values():
            states = [(name, state) for name, state in entry["scopes"].items()
                      if state["status"] == _OPEN and (scope is None or name == str(scope))]
            if not states:
                continue
            _, state = max(states, key=lambda item: (
                bool(item[1]["finding"].get("veto")),
                _RANK.get(item[1]["finding"].get("severity"), 0)))
            row = copy.deepcopy(state["finding"])
            row.update(issue_id=entry["issue_id"], status=_OPEN,
                       scopes=[name for name, _ in states],
                       reopened=entry["reopened"])
            rows.append(row)
        return rows

    def snapshot(self):
        return {"schema": SCHEMA, "version": VERSION,
                "revision": self._revision, "sequence": self._sequence,
                **self.counts(), "scopes": _portable(self._scopes),
                "entries": _portable(list(self._entries.values()))}

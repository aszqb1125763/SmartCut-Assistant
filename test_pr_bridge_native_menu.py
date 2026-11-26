# -*- coding: utf-8 -*-
"""Regression tests for the CEP native-menu Extract state machine.

Premiere Pro 26.5.1 does not expose ``app.executeCommand`` or
``app.findMenuCommandId`` to ExtendScript.  The panel therefore performs each
native Extract outside ExtendScript and asks ``host.jsx`` to prepare/verify
the step.  These tests simulate that external command between VM calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = os.path.dirname(os.path.abspath(__file__))
HOST_JSX = os.path.join(ROOT, "pr_bridge", "jsx", "host.jsx")


NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const hostPath = process.argv[2];
const spec = JSON.parse(process.argv[3]);
const TICKS = 254016000000;

function secondsOf(value) {
  if (value === null || value === undefined) return 0;
  if (typeof value === "number") return value > 1e9 ? value / TICKS : value;
  if (typeof value.seconds === "number") return value.seconds;
  if (value.ticks !== undefined) return Number(value.ticks) / TICKS;
  const n = Number(value);
  return n > 1e9 ? n / TICKS : n;
}

function timeValue(seconds) {
  return {
    seconds: seconds,
    ticks: String(Math.round(seconds * TICKS)),
    valueOf: function () { return Number(this.ticks); },
    toString: function () { return this.ticks; }
  };
}

function clip(start, end) {
  return { start: timeValue(start), end: timeValue(end) };
}

function track(name, targeted, locked, duration) {
  const values = [clip(0, duration)];
  values.numItems = values.length;
  return {
    name: name,
    targeted: !!targeted,
    locked: !!locked,
    clips: values,
    isTargeted: function () { return this.targeted; },
    setTargeted: function (value) { this.targeted = !!value; return true; },
    isLocked: function () { return this.locked; }
  };
}

function trackGroup(rows) {
  rows.numTracks = rows.length;
  return rows;
}

const initialDuration = Number(spec.duration || 100);
const sequence = {
  sequenceID: "native-menu-test-sequence",
  name: "native-menu-test",
  _duration: initialDuration,
  _in: Number(spec.initialIn === undefined ? 3 : spec.initialIn),
  _out: Number(spec.initialOut === undefined ? 7 : spec.initialOut),
  _cti: Number(spec.initialCti === undefined ? 12.5 : spec.initialCti),
  _history: [],
  extractCalls: 0,
  undoCalls: 0,
  videoTracks: trackGroup([
    track("V1", true, !!spec.lockVideo, initialDuration),
    track("V2", false, false, initialDuration)
  ]),
  audioTracks: trackGroup([
    track("A1", false, !!spec.lockAudio, initialDuration),
    track("A2", true, false, initialDuration)
  ]),
  getInPoint: function () { return timeValue(this._in); },
  getOutPoint: function () { return timeValue(this._out); },
  getInPointAsTime: function () { return timeValue(this._in); },
  getOutPointAsTime: function () { return timeValue(this._out); },
  setInPoint: function (value) { this._in = secondsOf(value); return true; },
  setOutPoint: function (value) { this._out = secondsOf(value); return true; },
  getPlayerPosition: function () { return timeValue(this._cti); },
  setPlayerPosition: function (value) { this._cti = secondsOf(value); return true; },
  extract: function () {
    this.extractCalls += 1;
    const width = this._out - this._in;
    if (!(width > 0)) return false;
    this._history.push(this._duration);
    this._duration -= width;
    return true;
  },
  _undo: function () {
    if (!this._history.length) return false;
    this._duration = this._history.pop();
    this.undoCalls += 1;
    return true;
  }
};

Object.defineProperty(sequence, "end", {
  get: function () { return timeValue(this._duration); }
});

// Deliberately no executeCommand and no findMenuCommandId: this is the real
// PR 26.5.1 capability shape that selects the native HMENU backend.
const app = {
  version: "26.5.1",
  project: { activeSequence: sequence }
};

const sandbox = {
  app: app,
  $: { sleep: function () {} },
  ProjectItemType: { BIN: 2 },
  decodeURIComponent: decodeURIComponent,
  encodeURIComponent: encodeURIComponent,
  isFinite: isFinite,
  Number: Number,
  Math: Math,
  Date: Date,
  String: String,
  JSON: JSON
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(hostPath, "utf8"), sandbox,
               { filename: hostPath });

const beforeTargets = [
  sequence.videoTracks[0].targeted,
  sequence.videoTracks[1].targeted,
  sequence.audioTracks[0].targeted,
  sequence.audioTracks[1].targeted
];

const payload = encodeURIComponent(spec.ranges.join("\n"));
const backend = sandbox.smartcutDeleteBackend();
let result = sandbox.smartcutBeginNativeExtract(payload);
let rollbackRequested = 0;

if (spec.scenario === "success") {
  while (String(result).indexOf("READY|") === 0) {
    const token = String(result).split("|")[1];
    sequence.extract(); // external native helper performs Premiere Extract
    result = sandbox.smartcutVerifyNativeExtract(token);
  }
} else if (spec.scenario === "second_noop") {
  // First range succeeds, second native command is a no-op.
  let token = String(result).split("|")[1];
  sequence.extract();
  result = sandbox.smartcutVerifyNativeExtract(token);
  token = String(result).split("|")[1];
  result = sandbox.smartcutVerifyNativeExtract(token);
  if (String(result).indexOf("ROLLBACK|") === 0) {
    const fields = String(result).split("|");
    rollbackRequested = Number(fields[2]);
    for (let i = 0; i < rollbackRequested; i++) sequence._undo();
    result = sandbox.smartcutFinishNativeRollback(fields[1]);
  }
}

const afterTargets = [
  sequence.videoTracks[0].targeted,
  sequence.videoTracks[1].targeted,
  sequence.audioTracks[0].targeted,
  sequence.audioTracks[1].targeted
];

process.stdout.write(JSON.stringify({
  backend: backend,
  result: result,
  duration: sequence._duration,
  inPoint: sequence._in,
  outPoint: sequence._out,
  cti: sequence._cti,
  extractCalls: sequence.extractCalls,
  undoCalls: sequence.undoCalls,
  rollbackRequested: rollbackRequested,
  beforeTargets: beforeTargets,
  afterTargets: afterTargets
}));
"""


class PremiereNativeMenuStateMachineTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required for CEP VM tests")
        if not os.path.isfile(HOST_JSX):
            raise unittest.SkipTest("CEP host.jsx is missing")

    def run_host(self, ranges, scenario, **options):
        spec = {"ranges": ranges, "scenario": scenario}
        spec.update(options)
        with tempfile.TemporaryDirectory(prefix="smartcut-native-menu-") as temp:
            runner = os.path.join(temp, "runner.js")
            with open(runner, "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent(NODE_HARNESS))
            completed = subprocess.run(
                [self.node, runner, HOST_JSX,
                 json.dumps(spec, ensure_ascii=False)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail("Node harness returned invalid JSON: %s\n%s" %
                      (exc, completed.stdout))

    def assert_user_state_restored(self, row):
        self.assertAlmostEqual(3.0, row["inPoint"], places=6)
        self.assertAlmostEqual(7.0, row["outPoint"], places=6)
        self.assertAlmostEqual(12.5, row["cti"], places=6)
        self.assertEqual(row["beforeTargets"], row["afterTargets"])

    def test_pr_26_uses_extendscript_rebuild_backend(self):
        row = self.run_host(["RANGE|20|30"], "success")
        self.assertEqual("extendscript", row["backend"])

    def test_two_ranges_complete_and_restore_user_state(self):
        row = self.run_host(
            ["RANGE|80|90", "RANGE|20|25"], "success")
        self.assertTrue(str(row["result"]).startswith("DONE|"), row)
        self.assertAlmostEqual(85.0, row["duration"], places=6)
        self.assertEqual(2, row["extractCalls"])
        self.assert_user_state_restored(row)

    def test_second_noop_requests_one_undo_and_restores_batch(self):
        row = self.run_host(
            ["RANGE|80|90", "RANGE|20|25"], "second_noop")
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertEqual(1, row["rollbackRequested"])
        self.assertEqual(1, row["undoCalls"])
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assert_user_state_restored(row)

    def test_locked_track_refuses_batch_without_mutation(self):
        row = self.run_host(
            ["RANGE|20|30"], "success", lockAudio=True)
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertIn("锁定轨", str(row["result"]))
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assertEqual(0, row["extractCalls"])
        self.assert_user_state_restored(row)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Regression tests for Premiere-native Extract range deletion.

The CEP host script is ExtendScript, so these tests execute it inside a Node
``vm`` context with a deliberately small Premiere sequence mock.  The mock
models the state that the range-delete implementation must preserve: sequence
In/Out, the CTI, track targeting, locking, duration changes, and Undo.
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
    isLocked: function () { return this.locked; },
    setLocked: function (value) { this.locked = !!value; return true; }
  };
}

function trackGroup(rows) {
  rows.numTracks = rows.length;
  return rows;
}

const initialDuration = Number(spec.duration || 100);
const sequence = {
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
  clearInPoint: function () { this._in = 0; return true; },
  clearOutPoint: function () { this._out = 0; return true; },
  getPlayerPosition: function () { return timeValue(this._cti); },
  setPlayerPosition: function (value) { this._cti = secondsOf(value); return true; },
  extract: function () {
    this.extractCalls += 1;
    if (Number(spec.failExtractCall || 0) === this.extractCalls) {
      throw new Error("mock Extract failure #" + this.extractCalls);
    }
    if (spec.extractNoop) return false;
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

const executedCommandIds = [];
const app = {
  version: String(spec.premiereVersion || "26.5.1"),
  project: {
    activeSequence: sequence,
    undo: function () { return sequence._undo(); }
  },
  executeCommand: function (id) {
    executedCommandIds.push(Number(id));
    if (Number(id) === 777 || Number(id) === 57643 ||
        String(id).toLowerCase() === "undo") {
      return sequence._undo();
    }
    if (Number(id) === 888 || Number(id) === 11612) {
      return sequence.extract();
    }
    return false;
  },
  undo: function () { return sequence._undo(); }
};

// PR 26.5.1 no longer exposes findMenuCommandId.  Keep it in the default
// mock for older builds, but let portability tests model the real 26.5.1 API.
// No keyboard/shortcut API is provided: production code must invoke Premiere's
// internal command IDs, so a user's custom Q/W bindings are irrelevant.
if (!spec.omitFindMenuCommandId) {
  app.findMenuCommandId = function (name) {
    if (/undo|撤销/i.test(String(name))) return 777;
    if (/extract|提取/i.test(String(name))) return 888;
    return 0;
  };
}

const sandbox = {
  app: app,
  $: { sleep: function () {} },
  ProjectItemType: { BIN: 2 },
  console: console,
  decodeURIComponent: decodeURIComponent,
  encodeURIComponent: encodeURIComponent,
  isFinite: isFinite,
  Number: Number,
  Math: Math,
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
let result;
try {
  result = sandbox.smartcutDeleteRanges(payload);
} catch (error) {
  result = "THREW:" + String(error);
}

const afterTargets = [
  sequence.videoTracks[0].targeted,
  sequence.videoTracks[1].targeted,
  sequence.audioTracks[0].targeted,
  sequence.audioTracks[1].targeted
];
process.stdout.write(JSON.stringify({
  result: result,
  duration: sequence._duration,
  inPoint: sequence._in,
  outPoint: sequence._out,
  cti: sequence._cti,
  extractCalls: sequence.extractCalls,
  undoCalls: sequence.undoCalls,
  executedCommandIds: executedCommandIds,
  beforeTargets: beforeTargets,
  afterTargets: afterTargets
}));
"""


@unittest.skip("旧 Extract 后端已停用；正式路径由可编辑工程重建测试覆盖")
class PremiereExtractRangeTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required for the CEP vm tests")
        if not os.path.isfile(HOST_JSX):
            raise unittest.SkipTest("CEP host.jsx is missing")

    def run_host(self, ranges, **options):
        spec = {"ranges": ranges}
        spec.update(options)
        with tempfile.TemporaryDirectory(prefix="smartcut-cep-test-") as temp:
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

    def assert_state_restored(self, row):
        self.assertAlmostEqual(3.0, row["inPoint"], places=6)
        self.assertAlmostEqual(7.0, row["outPoint"], places=6)
        self.assertAlmostEqual(12.5, row["cti"], places=6)
        self.assertEqual(row["beforeTargets"], row["afterTargets"])

    def test_two_descending_ranges_ripple_and_restore_user_state(self):
        row = self.run_host(["RANGE|80|90", "RANGE|20|25"])
        self.assertFalse(str(row["result"]).startswith("ERR:"), row)
        self.assertAlmostEqual(85.0, row["duration"], places=6)
        self.assertEqual(2, row["extractCalls"])
        self.assert_state_restored(row)

    def test_overlapping_ranges_are_merged_before_extract(self):
        row = self.run_host(["RANGE|25|35", "RANGE|20|30"])
        self.assertFalse(str(row["result"]).startswith("ERR:"), row)
        self.assertAlmostEqual(85.0, row["duration"], places=6)
        self.assertEqual(1, row["extractCalls"])
        self.assert_state_restored(row)

    def test_affected_locked_track_fails_preflight_without_mutation(self):
        row = self.run_host(["RANGE|20|30"], lockAudio=True)
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assertEqual(0, row["extractCalls"])
        self.assert_state_restored(row)

    def test_extract_noop_is_reported_as_failure(self):
        row = self.run_host(["RANGE|20|30"], extractNoop=True)
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assertEqual(1, row["extractCalls"])
        self.assert_state_restored(row)

    def test_mid_batch_failure_rolls_back_completed_extracts(self):
        row = self.run_host(
            ["RANGE|80|90", "RANGE|20|25"], failExtractCall=2)
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertEqual(2, row["extractCalls"])
        self.assertGreaterEqual(row["undoCalls"], 1, row)
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assert_state_restored(row)

    def test_pr_26_5_1_uses_internal_extract_id_without_command_lookup(self):
        row = self.run_host(
            ["RANGE|20|30"],
            omitFindMenuCommandId=True,
            premiereVersion="26.5.1",
            # Deliberately unrelated bindings: the bridge must never inspect
            # or synthesize these user-configurable shortcut keys.
            shortcutMap={"extract": "Alt+Shift+9", "q": "F13", "w": "F14"},
        )
        self.assertFalse(str(row["result"]).startswith("ERR:"), row)
        self.assertAlmostEqual(90.0, row["duration"], places=6)
        self.assertEqual([11612], row["executedCommandIds"])
        self.assert_state_restored(row)

    def test_pr_26_5_1_uses_internal_undo_id_for_failed_batch_rollback(self):
        row = self.run_host(
            ["RANGE|80|90", "RANGE|20|25"],
            omitFindMenuCommandId=True,
            premiereVersion="26.5.1",
            failExtractCall=2,
        )
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertEqual([11612, 11612, 57643], row["executedCommandIds"])
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assert_state_restored(row)

    def test_unknown_pr_version_refuses_unverified_internal_id(self):
        row = self.run_host(
            ["RANGE|20|30"],
            omitFindMenuCommandId=True,
            premiereVersion="26.6.0",
        )
        self.assertTrue(str(row["result"]).startswith("ERR:"), row)
        self.assertIn("无法安全解析", str(row["result"]))
        self.assertEqual([], row["executedCommandIds"])
        self.assertAlmostEqual(100.0, row["duration"], places=6)
        self.assert_state_restored(row)


if __name__ == "__main__":
    unittest.main()

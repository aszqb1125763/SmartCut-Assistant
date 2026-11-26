# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

import pr_workflow
import sync_server


ROOT = os.path.dirname(os.path.abspath(__file__))
HOST_JSX = os.path.join(ROOT, "pr_bridge", "jsx", "host.jsx")


NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const hostPath = process.argv[2];
const spec = JSON.parse(process.argv[3]);
const events = [];
let saveAsCalls = 0;
let insertCalls = 0;
const captionTracks = [];

function itemCollection(items) {
  Object.defineProperty(items, "numItems", { get: function () { return items.length; } });
  return items;
}

function trackCollection(items) {
  items.numTracks = items.length;
  return items;
}

function makeTrack(name, locked) {
  return {
    name: name,
    clips: itemCollection([]),
    isLocked: function () { return !!locked; }
  };
}

const sourceItem = {
  type: 1,
  name: "素材.mp4",
  _in: 0,
  _out: 0,
  getMediaPath: function () { return spec.source; },
  setInPoint: function (value) {
    this._in = Number(value);
    events.push("in:" + this._in.toFixed(3));
  },
  setOutPoint: function (value) {
    this._out = Number(value);
    events.push("out:" + this._out.toFixed(3));
  }
};
const rootItems = itemCollection([sourceItem]);

const videoTrack = makeTrack("V1", !!spec.lockVideo);
const audioTracks = [];
const audioTrackCount = Number(spec.audioTrackCount || 1);
for (let audioIndex = 0; audioIndex < audioTrackCount; audioIndex++) {
  audioTracks.push(makeTrack("A" + (audioIndex + 1),
                            !!spec.lockAudio && audioIndex === 0));
}

function addClip(track, start, end, item, label) {
  const clip = {
    start: start,
    end: end,
    inPoint: start,
    outPoint: end,
    projectItem: item,
    remove: function () {
      const index = track.clips.indexOf(this);
      if (index >= 0) track.clips.splice(index, 1);
      events.push("remove:" + label);
    }
  };
  track.clips.push(clip);
  return clip;
}

addClip(videoTrack, 0, 10, sourceItem, "V");
for (let oldAudio = 0; oldAudio < audioTracks.length; oldAudio++) {
  addClip(audioTracks[oldAudio], 0, 10, sourceItem,
          "A" + (oldAudio + 1));
}

videoTrack.insertClip = function (item, cursor) {
  insertCalls += 1;
  if (Number(spec.failInsert || 0) === insertCalls) {
    throw new Error("insert failure #" + insertCalls);
  }
  const start = Number(cursor);
  const requestedDuration = Number(item._out) - Number(item._in);
  const frameSeconds = Number(spec.frameSeconds || 0);
  const duration = frameSeconds > 0 ?
    Math.ceil((requestedDuration - 1e-9) / frameSeconds) * frameSeconds :
    requestedDuration;
  // Reproduce Premiere's insert-edit behaviour: inserting inside the real
  // frame-quantized tail splits that tail into a separate micro clip.
  if (frameSeconds > 0 && videoTrack.clips.length) {
    const previous = videoTrack.clips[videoTrack.clips.length - 1];
    if (start < Number(previous.end) - 1e-7) {
      const oldEnd = Number(previous.end);
      previous.end = start;
      addClip(videoTrack, start, oldEnd, item, "V-tail");
      events.push("split-tail:" + (oldEnd - start).toFixed(3));
    }
  }
  const end = start + duration;
  events.push("insert:" + start.toFixed(3) + ":" + item._in.toFixed(3) +
              ":" + item._out.toFixed(3));
  addClip(videoTrack, start, end, item, "V-new");
  for (let audioIndex = 0; audioIndex < audioTracks.length; audioIndex++) {
    if (Number(spec.skipInsertedAudioTrack || 0) === audioIndex + 1) continue;
    addClip(audioTracks[audioIndex], start, end, item,
            "A" + (audioIndex + 1) + "-new");
  }
};

const sequence = {
  name: "测试序列",
  videoTracks: trackCollection([videoTrack]),
  audioTracks: trackCollection(audioTracks)
};
if (Number(spec.frameSeconds || 0) > 0) {
  sequence.getSettings = function () {
    return { videoFrameRate: Number(spec.frameSeconds) };
  };
}
if (!spec.noCaptionApi) {
  sequence.createCaptionTrack = function (item, startAtTime) {
    const number = captionTracks.length + 1;
    if (Number(spec.failCaptionTrack || 0) === number) return false;
    captionTracks.push({name: item.name, path: item.getMediaPath(),
                        start: Number(startAtTime)});
    events.push("caption:" + item.name);
    return true;
  };
}
Object.defineProperty(sequence, "end", {
  get: function () {
    let top = 0;
    for (let i = 0; i < videoTrack.clips.length; i++) {
      top = Math.max(top, Number(videoTrack.clips[i].end));
    }
    return top;
  }
});

const project = {
  path: spec.currentPath || "C:\\原工程\\原项目.prproj",
  activeSequence: sequence,
  rootItem: { children: rootItems },
  importFiles: function (paths) {
    if (spec.failSubtitleImport) return false;
    for (let index = 0; index < paths.length; index++) {
      const mediaPath = String(paths[index]);
      const item = {
        type: 1,
        name: mediaPath.split(/[\\/]/).pop(),
        getMediaPath: function () { return mediaPath; }
      };
      rootItems.push(item);
      events.push("import:" + mediaPath);
    }
    return true;
  },
  saveAs: function (path) {
    saveAsCalls += 1;
    events.push("saveAs:" + path);
    if (Number(spec.throwSaveAs || 0) === saveAsCalls) {
      throw new Error("saveAs failure #" + saveAsCalls);
    }
    if (Number(spec.failSaveAs || 0) === saveAsCalls) return false;
    this.path = path;
    return 0;
  },
  save: function () {
    events.push("save");
    if (spec.failSave) return false;
    return 0;
  }
};

const sandbox = {
  app: { project: project, version: "26.5.1" },
  ProjectItemType: { BIN: 2 },
  decodeURIComponent: decodeURIComponent,
  encodeURIComponent: encodeURIComponent,
  isFinite: isFinite,
  Number: Number,
  Math: Math,
  String: String,
  JSON: JSON,
  console: console,
  File: function (path) {
    this.exists = (spec.subtitles || []).some(function (row) {
      return String(row.path) === String(path);
    });
  },
  $: { sleep: function () {} }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(hostPath, "utf8"), sandbox, { filename: hostPath });

const payload = [
  "BACKUP|" + spec.backup,
  "OUT|" + spec.output,
  "SRC|" + spec.source,
  "AUDIO_MODE|" + (spec.audioMode || "all"),
  "AUDIO_TRACKS|" + (spec.audioTracks || []).join(",")
].concat((spec.subtitles || []).map(function (row) {
  return "SUBTITLE|" + row.name + "|" + row.path;
})).concat(spec.keeps.map(function (row) {
  return "KEEP|" + row[0] + "|" + row[1];
})).join("\n");

let result;
try {
  result = sandbox.smartcutRebuildAndSave(encodeURIComponent(payload));
} catch (error) {
  result = "THREW:" + String(error);
}

process.stdout.write(JSON.stringify({
  result: result,
  events: events,
  projectPath: project.path,
  videoClips: videoTrack.clips.length,
  audioClips: audioTracks.reduce(function (total, track) {
    return total + track.clips.length;
  }, 0),
  audioClipCounts: audioTracks.map(function (track) {
    return track.clips.length;
  }),
  captionTracks: captionTracks,
  duration: sequence.end
}));
"""


NODE_LEGACY_GUARD_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const hostPath = process.argv[2];
const spec = JSON.parse(process.argv[3]);
const events = [];

function items(rows) {
  Object.defineProperty(rows, "numItems", { get: function () { return rows.length; } });
  return rows;
}
function tracks(rows) {
  rows.numTracks = rows.length;
  return rows;
}

const source = {
  type: 1,
  getMediaPath: function () { return spec.source; },
  setInPoint: function (value) { this._in = Number(value); events.push("set-in"); },
  setOutPoint: function (value) { this._out = Number(value); events.push("set-out"); }
};
const video = {
  clips: items([]),
  isLocked: function () { return !!spec.lockVideo; }
};
const audio = {
  clips: items([]),
  isLocked: function () { return false; }
};
if (spec.initialClip) {
  video.clips.push({
    start: 0,
    end: 10,
    remove: function () {
      events.push("remove");
      const index = video.clips.indexOf(this);
      if (index >= 0) video.clips.splice(index, 1);
    }
  });
}
video.insertClip = function (item, cursor) {
  events.push("insert");
  video.clips.push({start: Number(cursor), end: Number(cursor) + 1});
};

const sequence = {
  videoTracks: tracks([video]),
  audioTracks: tracks([audio]),
  getSettings: function () { return {videoFrameRate: 0.04}; }
};
const project = {
  activeSequence: sequence,
  rootItem: {children: items([source])},
  importFiles: function () { events.push("import"); return true; }
};
const sandbox = {
  app: {project: project, version: "26.5.1"},
  ProjectItemType: {BIN: 2},
  decodeURIComponent: decodeURIComponent,
  encodeURIComponent: encodeURIComponent,
  isFinite: isFinite,
  Number: Number,
  Math: Math,
  String: String,
  JSON: JSON,
  console: console,
  $: {sleep: function () {}}
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(hostPath, "utf8"), sandbox, {filename: hostPath});

let result;
if (spec.action === "insert") {
  result = sandbox.smartcutInsert(encodeURIComponent(JSON.stringify({
    source: spec.source,
    spans: [[0, 1]]
  })));
} else {
  result = sandbox.smartcutRebuild(encodeURIComponent([
    "SRC|" + spec.source,
    "KEEP|0|1"
  ].join("\n")));
}
process.stdout.write(JSON.stringify({
  result: result,
  events: events,
  videoClips: video.clips.length
}));
"""


class PremiereProjectPathTests(unittest.TestCase):
    def test_allocates_unique_editable_project_pair_beside_source(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "教程:第一集.mp4")
            with mock.patch.object(sync_server.time, "strftime", return_value="20261125_210000"):
                first = sync_server.allocate_project_paths(source)
                os.makedirs(os.path.dirname(first["project_path"]), exist_ok=True)
                open(first["project_path"], "wb").close()
                second = sync_server.allocate_project_paths(source)
        self.assertTrue(first["project_path"].endswith("_可编辑工程.prproj"))
        self.assertTrue(first["project_backup_path"].endswith("_剪辑前备份.prproj"))
        self.assertIn(os.path.join(root, "导出"), first["project_path"])
        self.assertNotIn(":第一集", os.path.basename(first["project_path"]))
        self.assertIn("_2_可编辑工程.prproj", second["project_path"])

    def test_sequence_fingerprint_includes_project_identity(self):
        base = {"name": "seq", "project_path": "C:/A.prproj", "tracks": [], "clips": []}
        changed = dict(base, project_path="C:/B.prproj")
        self.assertNotEqual(pr_workflow.sequence_fingerprint(base),
                            pr_workflow.sequence_fingerprint(changed))

    def test_load_sequence_reads_project_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sequence.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("SEQ|测试|1|1|10.0\n")
                handle.write("PROJECT|C:\\项目\\测试.prproj\n")
                handle.write("V|1|0|10|0|10|素材|C:\\素材.mp4\n")
            with mock.patch.object(sync_server, "SEQ_FILE", path):
                row = sync_server.load_sequence()
        self.assertEqual(r"C:\项目\测试.prproj", row["project_path"])


class PremiereAudioSettingsTests(unittest.TestCase):
    def test_audio_settings_are_normalised_and_preserve_other_values(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            path = os.path.join(root, "conf", "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"cut_level": "heavy", "edit_mode": "gameplay"}, handle)
            with mock.patch.object(sync_server, "BASE", root):
                saved = sync_server.save_pr_audio_settings({
                    "tracks": "3，2,3", "output_mode": "selected"})
                with open(path, encoding="utf-8") as handle:
                    raw = json.load(handle)
        self.assertEqual("2,3", saved["tracks"])
        self.assertEqual([2, 3], saved["track_numbers"])
        self.assertEqual("selected", saved["output_mode"])
        self.assertEqual("heavy", saved["cut_level"])
        self.assertAlmostEqual(0.45, saved["keep_ratio"])
        self.assertEqual("heavy", raw["cut_level"])
        self.assertAlmostEqual(0.45, raw["keep_ratio"])
        self.assertEqual("gameplay", raw["edit_mode"])

    def test_panel_saves_arbitrary_keep_percentage_and_derives_strategy(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            with mock.patch.object(sync_server, "BASE", root):
                saved = sync_server.save_pr_audio_settings({
                    "tracks": "", "output_mode": "all",
                    "keep_ratio": 0.62,
                })
        self.assertEqual("medium", saved["cut_level"])
        self.assertAlmostEqual(0.62, saved["keep_ratio"])
        self.assertEqual(62, saved["keep_percent"])

    def test_selected_output_requires_at_least_one_track(self):
        with self.assertRaisesRegex(ValueError, "至少填写一条音轨"):
            sync_server.save_pr_audio_settings({
                "tracks": "", "output_mode": "selected"})


class PremiereProjectExportHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required for CEP host tests")

    def run_host(self, **options):
        spec = {
            "source": r"F:\视频\素材.mp4",
            "backup": r"F:\视频\导出\素材_SmartCut_剪辑前备份.prproj",
            "output": r"F:\视频\导出\素材_SmartCut_可编辑工程.prproj",
            "keeps": [[0, 4], [7.5, 12.25]],
        }
        spec.update(options)
        with tempfile.TemporaryDirectory(prefix="smartcut-project-test-") as temp:
            runner = os.path.join(temp, "runner.js")
            with open(runner, "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent(NODE_HARNESS))
            done = subprocess.run(
                [self.node, runner, HOST_JSX,
                 json.dumps(spec, ensure_ascii=False)],
                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)

    def test_success_saves_backup_then_rebuilds_then_saves_final(self):
        row = self.run_host()
        self.assertTrue(row["result"].startswith("OK|"), row)
        events = row["events"]
        first_save = next(i for i, item in enumerate(events)
                          if item.startswith("saveAs:") and "剪辑前备份" in item)
        first_remove = next(i for i, item in enumerate(events)
                            if item.startswith("remove:"))
        first_insert = next(i for i, item in enumerate(events)
                            if item.startswith("insert:"))
        final_save = next(i for i, item in enumerate(events)
                          if item.startswith("saveAs:") and "可编辑工程" in item)
        self.assertLess(first_save, first_remove)
        self.assertLess(first_remove, first_insert)
        self.assertLess(first_insert, final_save)
        self.assertEqual("save", events[-1])
        self.assertEqual(2, row["videoClips"])
        self.assertAlmostEqual(8.75, row["duration"], places=3)
        self.assertTrue(row["projectPath"].endswith("_可编辑工程.prproj"))

    def test_frame_quantization_uses_real_tail_and_never_splits_one_frame(self):
        row = self.run_host(
            frameSeconds=0.04,
            keeps=[[0, 1.01], [3.0, 4.0]],
        )
        self.assertTrue(row["result"].startswith("OK|"), row)
        inserts = [event for event in row["events"] if event.startswith("insert:")]
        self.assertEqual(2, len(inserts), row)
        self.assertEqual("insert:1.040:3.000:4.000", inserts[1])
        self.assertFalse(any(event.startswith("split-tail:")
                             for event in row["events"]), row)
        self.assertEqual(2, row["videoClips"])
        self.assertAlmostEqual(2.04, row["duration"], places=3)

    def test_sub_two_frame_keep_is_rejected_before_backup_or_timeline_change(self):
        row = self.run_host(frameSeconds=0.04, keeps=[[0, 0.04], [1, 2]])
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertIn("无效的保留片段", row["result"])
        self.assertFalse(any(event.startswith("saveAs:") for event in row["events"]))
        self.assertEqual(1, row["videoClips"])

    def test_selected_audio_tracks_remove_every_other_source_track(self):
        row = self.run_host(
            audioTrackCount=4, audioMode="selected", audioTracks=[2, 3])
        self.assertTrue(row["result"].startswith("OK|"), row)
        self.assertEqual([0, 2, 2, 0], row["audioClipCounts"])

    def test_creates_simplified_and_traditional_native_caption_tracks(self):
        subtitles = [
            {"name": "简体中文", "path": r"F:\视频\导出\简体中文字幕.srt"},
            {"name": "繁体中文", "path": r"F:\视频\导出\繁体中文字幕.srt"},
        ]
        row = self.run_host(subtitles=subtitles)
        self.assertTrue(row["result"].startswith("OK|"), row)
        self.assertEqual(2, len(row["captionTracks"]))
        self.assertEqual(
            [r"F:\视频\导出\简体中文字幕.srt",
             r"F:\视频\导出\繁体中文字幕.srt"],
            [track["path"] for track in row["captionTracks"]])
        last_caption = max(i for i, event in enumerate(row["events"])
                           if event.startswith("caption:"))
        final_save = next(i for i, event in enumerate(row["events"])
                          if event.startswith("saveAs:") and "可编辑工程" in event)
        self.assertLess(last_caption, final_save)
        self.assertIn("|2|native|", row["result"])

    def test_missing_caption_api_imports_both_subtitles_and_saves_project(self):
        row = self.run_host(
            noCaptionApi=True,
            subtitles=[{"name": "简体中文", "path": r"F:\简体.srt"},
                       {"name": "繁体中文", "path": r"F:\繁体.srt"}])
        self.assertTrue(row["result"].startswith("OK|"), row)
        self.assertIn("|0|imported|", row["result"])
        self.assertEqual(2, len([event for event in row["events"]
                                if event.startswith("import:")]))
        self.assertEqual("save", row["events"][-1])
        self.assertEqual(2, len([event for event in row["events"]
                                if event.startswith("saveAs:")]))

    def test_caption_failure_keeps_both_imported_subtitles_and_saves(self):
        row = self.run_host(
            failCaptionTrack=2,
            subtitles=[
                {"name": "简体中文", "path": r"F:\简体.srt"},
                {"name": "繁体中文", "path": r"F:\繁体.srt"},
            ])
        self.assertTrue(row["result"].startswith("OK|"), row)
        self.assertIn("|1|imported|", row["result"])
        self.assertEqual(2, len([event for event in row["events"]
                                if event.startswith("import:")]))
        self.assertEqual(2, len([event for event in row["events"]
                                if event.startswith("saveAs:")]))
        self.assertEqual("save", row["events"][-1])

    def test_subtitle_import_failure_is_reported_instead_of_losing_subtitles(self):
        row = self.run_host(
            noCaptionApi=True, failSubtitleImport=True,
            subtitles=[{"name": "简体中文", "path": r"F:\简体.srt"}])
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertIn("字幕导入失败", row["result"])
        self.assertNotIn("save", row["events"])

    def test_missing_selected_audio_track_never_saves_final_project(self):
        row = self.run_host(
            audioTrackCount=4, audioMode="selected", audioTracks=[5])
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertIn("A5", row["result"])
        self.assertEqual(1, len([x for x in row["events"]
                                if x.startswith("saveAs:")]))
        self.assertNotIn("save", row["events"])

    def test_selected_track_must_receive_every_inserted_clip(self):
        row = self.run_host(
            audioTrackCount=3, audioMode="selected", audioTracks=[2],
            skipInsertedAudioTrack=2)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertIn("A2", row["result"])
        self.assertEqual(1, len([x for x in row["events"]
                                if x.startswith("saveAs:")]))

    def test_backup_save_failure_makes_no_timeline_changes(self):
        row = self.run_host(failSaveAs=1)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertEqual(1, len(row["events"]))
        self.assertTrue(row["events"][0].startswith("saveAs:"))
        self.assertEqual(1, row["videoClips"])

    def test_insert_failure_never_saves_final_project(self):
        row = self.run_host(failInsert=2)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertEqual(1, len([x for x in row["events"] if x.startswith("saveAs:")]))
        self.assertNotIn("save", row["events"])

    def test_locked_track_is_rejected_before_backup(self):
        row = self.run_host(lockVideo=True)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertFalse(any(x.startswith("saveAs:") for x in row["events"]))
        self.assertEqual(1, row["videoClips"])

    def test_final_save_failure_is_reported(self):
        row = self.run_host(failSave=True)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertEqual(2, len([x for x in row["events"] if x.startswith("saveAs:")]))
        self.assertEqual("save", row["events"][-1])


class PremiereLegacyHostGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required for CEP host tests")

    def run_legacy_host(self, action, **options):
        spec = {
            "action": action,
            "source": r"F:\视频\素材.mp4",
        }
        spec.update(options)
        with tempfile.TemporaryDirectory(prefix="smartcut-legacy-guard-") as temp:
            runner = os.path.join(temp, "runner.js")
            with open(runner, "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent(NODE_LEGACY_GUARD_HARNESS))
            done = subprocess.run(
                [self.node, runner, HOST_JSX,
                 json.dumps(spec, ensure_ascii=False)],
                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)

    def test_legacy_insert_rejects_nonempty_timeline_before_any_change(self):
        row = self.run_legacy_host("insert", initialClip=True)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertIn("不是空序列", row["result"])
        self.assertEqual([], row["events"])
        self.assertEqual(1, row["videoClips"])

    def test_legacy_rebuild_stops_when_clear_is_rejected(self):
        row = self.run_legacy_host(
            "rebuild", initialClip=True, lockVideo=True)
        self.assertTrue(row["result"].startswith("ERR:"), row)
        self.assertIn("轨道已锁定", row["result"])
        self.assertEqual([], row["events"])
        self.assertEqual(1, row["videoClips"])


if __name__ == "__main__":
    unittest.main()

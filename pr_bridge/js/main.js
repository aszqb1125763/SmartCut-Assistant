/* 智能剪辑桥 · 精简面板
   三件事：连接本机接口 → 读 PR 当前序列 → 让 AI 剪一步。
   执行层：定位（ExtendScript）+ 命令（服务端发 WM_COMMAND / 按键）。 */

window.onerror = function (msg, url, line) {
  try {
    var box = document.getElementById("log");
    if (box) {
      var div = document.createElement("div");
      div.className = "err";
      div.textContent = "[JS 错误] " + msg + "（行 " + line + "）";
      box.appendChild(div);
    }
  } catch (e) {}
  return false;
};

var cs = new CSInterface();
var BRIDGE = "http://127.0.0.1:17890";
var BRIDGE_TOKEN = "";
var lastDump = null;
var smartLogCursor = 0;
var audioTrackCount = 0;
var audioTrackMuted = {};
var audioTrackSelection = "";
var logCount = 0;

function $(id) { return document.getElementById(id); }

function log(text, kind) {
  var box = $("log");
  if (!box) { return; }
  var div = document.createElement("div");
  if (kind) { div.className = kind; }
  var now = new Date();
  var hh = now.getHours() < 10 ? "0" + now.getHours() : now.getHours();
  var mm = now.getMinutes() < 10 ? "0" + now.getMinutes() : now.getMinutes();
  var ss = now.getSeconds() < 10 ? "0" + now.getSeconds() : now.getSeconds();
  div.textContent = "[" + hh + ":" + mm + ":" + ss + "] " + text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  logCount++;
  if ($("logCount")) { $("logCount").textContent = logCount + " 条"; }
  if (kind === "err" && $("logPanel")) { $("logPanel").open = true; }
}

function setState(text) {
  var el = $("state");
  if (el) { el.textContent = text; }
}

function setSmartState(text, fraction) {
  var label = $("smartState");
  if (label) { label.textContent = text || ""; }
  var bar = $("smartProgress");
  if (bar && fraction !== undefined && fraction !== null) {
    var value = Math.max(0, Math.min(1, Number(fraction) || 0));
    bar.style.width = Math.round(value * 100) + "%";
  }
}

function parseJson(text) {
  try { return JSON.parse(text || "{}"); }
  catch (e) { return null; }
}

function refreshBridgeSession() {
  try {
    if (typeof require !== "function") { return false; }
    var fs = require("fs");
    var path = require("path");
    var os = require("os");
    var base = "";
    if (typeof process !== "undefined" && process.env) {
      base = process.env.LOCALAPPDATA || process.env.TEMP || process.env.TMP || "";
    }
    if (!base) { base = os.tmpdir(); }
    var sessionPath = path.join(base, "SmartCut", "bridge-session.json");
    var data = JSON.parse(fs.readFileSync(sessionPath, "utf8") || "{}");
    var token = String(data.token || "");
    var port = Number(data.port || 0);
    if (token.length < 32 || !isFinite(port) || port < 1 || port > 65535) {
      return false;
    }
    BRIDGE_TOKEN = token;
    BRIDGE = "http://127.0.0.1:" + Math.round(port);
    return true;
  } catch (e) {
    BRIDGE_TOKEN = "";
    return false;
  }
}

function xhrGet(path, cb) {
  if (!refreshBridgeSession()) {
    cb("找不到安全会话，请先启动或重启 SmartCut", null);
    return;
  }
  var req = new XMLHttpRequest();
  req.open("GET", BRIDGE + path, true);
  req.setRequestHeader("X-SmartCut-Token", BRIDGE_TOKEN);
  req.onreadystatechange = function () {
    if (req.readyState !== 4) { return; }
    if (req.status === 0) { cb("连不上接口", null); return; }
    try { cb(null, JSON.parse(req.responseText)); }
    catch (e) { cb("返回不是 JSON", null); }
  };
  req.onerror = function () { cb("请求失败", null); };
  req.send();
}

function xhrPost(path, body, cb) {
  if (!refreshBridgeSession()) {
    cb(0, JSON.stringify({ok: false, error: "找不到安全会话，请先启动或重启 SmartCut"}));
    return;
  }
  var req = new XMLHttpRequest();
  req.open("POST", BRIDGE + path, true);
  req.setRequestHeader("Content-Type", "text/plain; charset=utf-8");
  req.setRequestHeader("X-SmartCut-Token", BRIDGE_TOKEN);
  req.onreadystatechange = function () {
    if (req.readyState !== 4) { return; }
    cb(req.status, req.responseText || "");
  };
  req.onerror = function () { cb(0, ""); };
  req.send(body || "");
}

function normaliseAudioTracks(value) {
  var raw = String(value || "").replace(/，/g, ",").replace(/[；;]/g, ",");
  var parts = raw.split(",");
  var numbers = [];
  for (var i = 0; i < parts.length; i++) {
    var token = String(parts[i] || "").replace(/^\s+|\s+$/g, "");
    if (!token) { continue; }
    if (!/^\d+$/.test(token)) { return null; }
    var number = Number(token);
    if (!isFinite(number) || number < 1 || number > 64) { return null; }
    if (numbers.indexOf(number) < 0) { numbers.push(number); }
  }
  numbers.sort(function (a, b) { return a - b; });
  return numbers.join(",");
}

function audioTrackNumbers(value) {
  var normalised = normaliseAudioTracks(value);
  if (normalised === null || !normalised) { return []; }
  var parts = normalised.split(",");
  var rows = [];
  for (var i = 0; i < parts.length; i++) { rows.push(Number(parts[i])); }
  return rows;
}

function updateAudioTrackText() {
  var text = $("audioTrackText");
  var auto = $("audioTrackAuto");
  var tracks = audioTrackNumbers(audioTrackSelection);
  if (auto) { auto.checked = tracks.length === 0; }
  if (!text) { return; }
  if (!tracks.length) {
    text.textContent = "自动：当前未静音轨";
    return;
  }
  var labels = [];
  for (var i = 0; i < tracks.length; i++) { labels.push("A" + tracks[i]); }
  text.textContent = labels.join("、");
}

function readAudioTrackChecks() {
  var checks = document.getElementsByClassName("audio-track-check");
  var picked = [];
  for (var i = 0; i < checks.length; i++) {
    if (checks[i].checked && !checks[i].disabled) {
      picked.push(Number(checks[i].getAttribute("data-track")));
    }
  }
  audioTrackSelection = normaliseAudioTracks(picked.join(",")) || "";
  updateAudioTrackText();
}

function renderAudioTrackOptions() {
  var list = $("audioTrackList");
  if (!list) { return; }
  while (list.firstChild) { list.removeChild(list.firstChild); }
  var selected = audioTrackNumbers(audioTrackSelection);
  if (!audioTrackCount) {
    var empty = document.createElement("div");
    empty.className = "track-empty";
    empty.textContent = "请先读取当前序列";
    list.appendChild(empty);
    updateAudioTrackText();
    return;
  }
  for (var track = 1; track <= audioTrackCount; track++) {
    var label = document.createElement("label");
    label.className = "track-option";
    var check = document.createElement("input");
    check.type = "checkbox";
    check.className = "audio-track-check";
    check.setAttribute("data-track", String(track));
    check.checked = selected.indexOf(track) >= 0;
    check.addEventListener("change", readAudioTrackChecks);
    var name = document.createElement("span");
    name.textContent = "A" + track;
    var note = document.createElement("span");
    note.className = "track-note";
    note.textContent = audioTrackMuted[track] === "1" ? "当前静音" : "当前启用";
    label.appendChild(check);
    label.appendChild(name);
    label.appendChild(note);
    list.appendChild(label);
  }
  updateAudioTrackText();
}

function setAudioTrackSelection(value) {
  var normalised = normaliseAudioTracks(value);
  audioTrackSelection = normalised === null ? "" : normalised;
  renderAudioTrackOptions();
}

function updateAudioTracksFromSequence(head, lines) {
  audioTrackCount = Math.max(0, Number(head[3]) || 0);
  audioTrackMuted = {};
  for (var i = 1; i < lines.length; i++) {
    var parts = lines[i].split("|");
    if (parts[0] === "TRACK" && parts[1] === "A") {
      audioTrackMuted[Number(parts[2])] = parts[3] || "";
    }
  }
  renderAudioTrackOptions();
}

function toggleAudioTrackMenu(forceOpen) {
  var menu = $("audioTrackMenu");
  var button = $("audioTrackToggle");
  if (!menu || !button) { return; }
  var open = forceOpen === undefined ? menu.className.indexOf("hidden") >= 0 :
    !!forceOpen;
  menu.className = open ? "track-menu" : "track-menu hidden";
  button.setAttribute("aria-expanded", open ? "true" : "false");
}

function showAudioSettings(data) {
  if (!data) { return; }
  setAudioTrackSelection(data.tracks || "");
  if ($("keepRatioPercent")) {
    $("keepRatioPercent").value = Math.round(
      (Number(data.keep_ratio) || 0.75) * 100);
  }
  if ($("audioOutputMode")) {
    $("audioOutputMode").value = data.output_mode === "selected" ?
      "selected" : "all";
  }
  var ratio = Math.round((Number(data.keep_ratio) || 0) * 100);
  var tracks = data.tracks ?
    "A" + data.tracks.replace(/,/g, ", A") : "当前 PR 未静音轨";
  var output = data.output_mode === "selected" ?
    "最终工程只保留所选轨" : "最终工程保留全部原轨";
  if ($("audioState")) {
    $("audioState").textContent = "目标保留 " + ratio +
      "% 有效内容；AI：" + tracks + "；" + output;
  }
}

function loadAudioSettings(quiet) {
  xhrGet("/audio-settings", function (err, data) {
    if (err || !data || !data.ok) {
      if (!quiet) { log("读取音轨设置失败：" + (err || "未知"), "err"); }
      return;
    }
    showAudioSettings(data);
    if (!quiet) { log("已读取音轨设置", "dim"); }
  });
}

function saveAudioSettings(done) {
  var tracks = normaliseAudioTracks(audioTrackSelection);
  var outputMode = $("audioOutputMode") ?
    $("audioOutputMode").value : "all";
  var keepPercent = $("keepRatioPercent") ?
    Number($("keepRatioPercent").value) : 75;
  if (tracks === null) {
    log("音轨设置无效，请重新选择", "err");
    if (done) { done(false, null); }
    return;
  }
  var picked = audioTrackNumbers(tracks);
  if (audioTrackCount && picked.length &&
      picked[picked.length - 1] > audioTrackCount) {
    log("所选音轨不在当前序列内，请重新读取序列后选择", "err");
    if (done) { done(false, null); }
    return;
  }
  if (outputMode === "selected" && !tracks) {
    log("只保留所选轨时，至少填写一条音轨", "err");
    if (done) { done(false, null); }
    return;
  }
  if (!isFinite(keepPercent) || keepPercent < 15 || keepPercent > 100) {
    log("保留比例必须在 15% 到 100% 之间", "err");
    if (done) { done(false, null); }
    return;
  }
  var payload = JSON.stringify({
    tracks: tracks, output_mode: outputMode,
    keep_ratio: Math.round(keepPercent) / 100
  });
  xhrPost("/audio-settings", payload, function (status, text) {
    var data = parseJson(text);
    if (status !== 200 || !data || !data.ok) {
      log("保存音轨设置失败：" +
          ((data && data.error) || "HTTP " + status), "err");
      if (done) { done(false, data); }
      return;
    }
    showAudioSettings(data);
    log("剪辑设置已保存：保留 " +
        Math.round((Number(data.keep_ratio) || 0) * 100) + "%；" +
        (data.tracks ? "A" + data.tracks.replace(/,/g, ", A") : "自动选择") +
        "；" + (data.output_mode === "selected" ?
        "只输出所选轨" : "输出全部原轨"), "ok");
    if (done) { done(true, data); }
  });
}

function connect() {
  setState("连接中…");
  xhrGet("/health", function (err, data) {
    if (err || !data || !data.ok) {
      setState("未连接");
      log("连接失败：" + (err || "接口没回应"), "err");
      return;
    }
    setState("已连接");
    log("接口正常（" + BRIDGE + "）", "ok");
    loadAudioSettings(true);
    captureSequence(null, true);
  });
}

function captureSequence(done, quiet) {
  if (!quiet) { log("读取 PR 当前序列…"); }
  cs.evalScript("smartcutDumpSequence()", function (res) {
    if (!res || res.indexOf("ERR") === 0) {
      log("读序列失败：" + (res || "空"), "err");
      if (done) { done(false, null); }
      return;
    }
    var lines = res.split("\n");
    var head = (lines[0] || "").split("|");
    updateAudioTracksFromSequence(head, lines);
    var v = 0, a = 0;
    for (var i = 1; i < lines.length; i++) {
      var p = lines[i].split("|");
      if (p[0] === "V") { v++; }
      if (p[0] === "A") { a++; }
    }
    var total = head[4] ? Number(head[4]) : 0;
    if (!quiet) {
      log("序列「" + (head[1] || "?") + "」总长 " +
          (head[4] ? total.toFixed(1) : "?") + " 秒", "ok");
      log("视频片段 " + v + " 段，音频片段 " + a + " 段", "dim");
    }
    lastDump = res;
    xhrPost("/sequence", res, function (status, text) {
      if (status === 200) {
        if (!quiet) { log("已同步给 AI 服务", "ok"); }
        if (done) {
          done(true, {name: head[1] || "", total: total,
                      video: v, audio: a, raw: res});
        }
      } else {
        log("同步失败：HTTP " + status, "err");
        if (done) { done(false, null); }
      }
    });
  });
}

function dumpSequence() { captureSequence(null, false); }

function keyName(spec) {
  var key = String(spec || "").toUpperCase();
  var names = {
    "Q": "波纹修剪上一个编辑点到播放头",
    "W": "波纹修剪下一个编辑点到播放头",
    "CTRL+K": "添加编辑（切一刀）",
    "SHIFT+END": "波纹删除"
  };
  return names[key] || key;
}

/* 执行一步操作：seek 用 ExtendScript，命令交给服务端 */
function runOps(ops, done) {
  var i = 0;

  function next() {
    if (i >= ops.length) { done(); return; }
    var op = ops[i++];
    if (op.op === "seek") {
      var t = Number(op.t);
      cs.evalScript("smartcutSeek(" + t + ")", function (res) {
        log("定位到 " + t + " 秒：" + (res || "?"), "dim");
        setTimeout(next, 250);
      });
    } else if (op.op === "key") {
      xhrPost("/key", op.k, function (status) {
        log("执行：" + keyName(op.k), "dim");
        setTimeout(next, 500);
      });
    } else {
      next();
    }
  }
  next();
}

/* 完整流程专用：时间轴每删一段都会变短，所以每个 Q 后重新读取并验长度。 */
function runVerifiedOps(ops, initialTotal, done) {
  var i = 0;
  var currentTotal = Number(initialTotal) || 0;
  var verified = 0;

  function fail(message) {
    log("停止执行：" + message, "err");
    done(false, {verified: verified, total: currentTotal, error: message});
  }

  function next() {
    if (i >= ops.length) {
      done(true, {verified: verified, total: currentTotal});
      return;
    }
    var op = ops[i++];
    if (op.op === "seek") {
      var t = Number(op.t);
      cs.evalScript("smartcutSeek(" + t + ")", function (res) {
        if (!res || res.indexOf("ERR") === 0) {
          fail("定位 " + t + " 秒失败：" + (res || "空"));
          return;
        }
        log("定位到 " + t + " 秒", "dim");
        setTimeout(next, 220);
      });
      return;
    }
    if (op.op !== "key") { next(); return; }
    xhrPost("/key", op.k, function (status, text) {
      var reply = parseJson(text);
      if (status !== 200 || !reply || !reply.ok) {
        fail("命令 " + op.k + " 没有成功发送");
        return;
      }
      log("执行：" + keyName(op.k), "dim");
      if (String(op.k).toUpperCase() !== "Q" || !op.expected) {
        setTimeout(next, 420);
        return;
      }
      setTimeout(function () {
        captureSequence(function (ok, info) {
          if (!ok || !info) {
            fail("命令执行后无法重新读取时间轴");
            return;
          }
          var actual = currentTotal - Number(info.total || 0);
          var expected = Number(op.expected || 0);
          var tolerance = Math.max(0.12, Math.min(0.5, expected * 0.15));
          if (Math.abs(actual - expected) > tolerance) {
            fail("本段预计缩短 " + expected.toFixed(2) + " 秒，实际 " +
                 actual.toFixed(2) + " 秒；后续坐标已停止");
            return;
          }
          currentTotal = Number(info.total || 0);
          verified++;
          log("已验证第 " + verified + " 段：缩短 " + actual.toFixed(2) +
              " 秒，当前 " + currentTotal.toFixed(2) + " 秒", "ok");
          next();
        }, true);
      }, 650);
    });
  }
  next();
}

function finishSmartButton() {
  if ($("btnFull")) { $("btnFull").disabled = false; }
}

function runSmartProjectExport(result, done) {
  var keeps = result.keep_spans || [];
  var source = String(result.source || "");
  var backup = String(result.project_backup_path || "");
  var output = String(result.project_path || "");
  var audioMode = result.audio_output_mode === "selected" ? "selected" : "all";
  var audioTracks = result.audio_track_numbers || [];
  var subtitleTracks = result.subtitle_tracks || [];
  if (!audioTracks.length && result.tracks && result.tracks.length) {
    for (var t = 0; t < result.tracks.length; t++) {
      audioTracks.push(Number(result.tracks[t]) + 1);
    }
  }
  if (!source || !backup || !output || !keeps.length) {
    done(false, {error: "方案缺少素材、保留段或 PR 工程输出路径"});
    return;
  }
  if (audioMode === "selected" && !audioTracks.length) {
    done(false, {error: "已选择只保留所选音轨，但方案中没有音轨编号"});
    return;
  }
  var lines = ["BACKUP|" + backup, "OUT|" + output, "SRC|" + source,
               "AUDIO_MODE|" + audioMode,
               "AUDIO_TRACKS|" + audioTracks.join(",")];
  for (var subtitleIndex = 0; subtitleIndex < subtitleTracks.length;
       subtitleIndex++) {
    var subtitle = subtitleTracks[subtitleIndex] || {};
    var subtitleName = String(subtitle.name || "字幕").replace(/\|/g, " ");
    var subtitlePath = String(subtitle.path || "");
    if (!subtitlePath) {
      done(false, {error: subtitleName + "缺少字幕文件路径"});
      return;
    }
    lines.push("SUBTITLE|" + subtitleName + "|" + subtitlePath);
  }
  var expected = 0;
  for (var i = 0; i < keeps.length; i++) {
    var a = Number(keeps[i][0]);
    var b = Number(keeps[i][1]);
    if (!isFinite(a) || !isFinite(b) || b <= a) {
      done(false, {error: "第 " + (i + 1) + " 个保留片段无效"});
      return;
    }
    lines.push("KEEP|" + a.toFixed(6) + "|" + b.toFixed(6));
    expected += (b - a);
  }
  var encoded = encodeURIComponent(lines.join("\n")).replace(/'/g, "%27");
  cs.evalScript("smartcutRebuildAndSave('" + encoded + "')", function (res) {
    if (!res || res.indexOf("ERR:") === 0) {
      done(false, {error: res ? res.substring(4) : "Premiere 没有返回结果"});
      return;
    }
    var parts = String(res).split("|");
    if (parts[0] !== "OK") {
      done(false, {error: "无法识别 Premiere 返回结果：" + res});
      return;
    }
    done(true, {
      project: parts[1] || output,
      backup: parts[2] || backup,
      count: Number(parts[3] || keeps.length),
      duration: Number(parts[4] || expected),
      audioMode: audioMode,
      audioTracks: audioTracks,
      subtitleCount: Number(parts[5] || 0),
      subtitleTracks: subtitleTracks,
      captionMode: parts[6] || (Number(parts[5] || 0) ? "native" : "none"),
      captionWarning: parts[7] ? decodeURIComponent(parts[7]) : ""
    });
  });
}

function executeSmartResult() {
  // 规划可能耗时很久。执行前重新 dump，服务端会用 revision 拒绝过期方案。
  captureSequence(function (ok, info) {
    if (!ok || !info) { finishSmartButton(); return; }
    xhrPost("/smart-edit/result", "", function (status, text) {
      var data = parseJson(text);
      var err = status === 200 ? null : "HTTP " + status;
      if (err || !data || !data.ok || !data.result) {
        log("完整方案不能执行：" +
            (err || (data && data.error) || "未知错误"), "err");
        setSmartState("方案已失效，需要重新分析", 0);
        finishSmartButton();
        return;
      }
      var result = data.result;
      log("导演定稿：" + result.count + " 个片段，预计缩短 " +
          Number(result.expected_shorten || 0).toFixed(2) + " 秒", "ok");
      setSmartState("正在生成可继续编辑的 PR 工程…", 0.92);
      runSmartProjectExport(result, function (success, state) {
        if (success) {
          log("=== 可编辑 PR 工程已生成：" + state.count +
              " 段 / " + state.duration.toFixed(2) + " 秒 ===", "ok");
          log("工程文件：" + state.project, "ok");
          log("剪辑前备份：" + state.backup, "dim");
          log(state.audioMode === "selected" ?
              "最终音轨：仅保留 A" + state.audioTracks.join(", A") :
              "最终音轨：保留原素材全部音轨", "dim");
          if (state.captionMode === "imported") {
            log("工程字幕：简繁 SRT 已导入项目面板；未自动建轨的字幕可手动拖入序列。", "dim");
            if (state.captionWarning) {
              log("字幕兼容提示：" + state.captionWarning, "dim");
            }
          } else if (state.subtitleCount > 0) {
            log("工程字幕：简体中文、繁体中文（" + state.subtitleCount +
                " 条原生可编辑字幕轨）", "ok");
          } else {
            log("工程字幕：定稿时间轴没有可用台词，未创建字幕轨", "dim");
          }
          setSmartState("完成：可编辑 PR 工程已保存", 1);
          setTimeout(function () { captureSequence(null, true); }, 300);
        } else {
          log("PR 工程生成失败：" + (state.error || "未知错误"), "err");
          log("原工程没有被覆盖；如已生成剪辑前备份，可直接打开恢复。", "dim");
          setSmartState("失败：最终 PR 工程没有保存", 0);
        }
        finishSmartButton();
      });
    });
  }, true);
}

function pollSmartEdit() {
  xhrGet("/smart-edit/status", function (err, data) {
    if (err || !data || !data.ok) {
      log("读取完整剪辑进度失败：" + (err || "未知"), "err");
      finishSmartButton();
      return;
    }
    var rows = data.logs || [];
    for (var i = smartLogCursor; i < rows.length; i++) {
      log(rows[i], "dim");
    }
    smartLogCursor = rows.length;
    var progress = data.progress || {};
    var fraction = progress.fraction;
    if (fraction === undefined || fraction === null) {
      fraction = data.status === "ready" ? 0.9 : 0.15;
    }
    setSmartState(data.message || "完整智能剪辑运行中…", fraction);
    if (data.status === "ready") {
      executeSmartResult();
    } else if (data.status === "failed") {
      log("完整智能剪辑失败：" + (data.error || data.message || "未知"), "err");
      setSmartState("失败：没有修改 PR 时间轴", 0);
      finishSmartButton();
    } else {
      setTimeout(pollSmartEdit, 1200);
    }
  });
}

function startSmartEdit() {
  if ($("btnFull")) { $("btnFull").disabled = true; }
  smartLogCursor = 0;
  setSmartState("保存并冻结本次保留比例和音轨设置…", 0.02);
  saveAudioSettings(function (saved) {
    if (!saved) { setSmartState("音轨设置无效", 0); finishSmartButton(); return; }
    setSmartState("读取并冻结当前 PR 时间轴…", 0.03);
    captureSequence(function (ok) {
      if (!ok) { finishSmartButton(); return; }
      xhrPost("/smart-edit/start", "start", function (status, text) {
        var data = parseJson(text);
        if (status !== 200 || !data || !data.ok) {
          log("无法开始完整智能剪辑：" +
              ((data && data.error) || "接口失败"), "err");
          setSmartState("启动失败", 0);
          finishSmartButton();
          return;
        }
        log("已冻结时间轴和音轨设置，开始运行分析员、编剧、剪辑师和导演；完成后自动另存 PR 工程。", "ok");
        setTimeout(pollSmartEdit, 500);
      });
    }, true);
  });
}

function aiStep() {
  log("请 AI 看当前序列、决定下一步…");
  if ($("btnStep")) { $("btnStep").disabled = true; }
  xhrPost("/step", "", function (status, text) {
    var data = parseJson(text);
    if ($("btnStep")) { $("btnStep").disabled = false; }
    if (status !== 200 || !data || !data.ok) {
      log("AI 没给出方案：" + ((data && data.error) || "HTTP " + status), "err");
      return;
    }
    if (data.done) {
      log("AI 判断已剪完：" + (data.reason || ""), "ok");
      return;
    }
    if (!data.ops || !data.ops.length) {
      log("AI 没给操作（" + (data.reason || "") + "）", "err");
      return;
    }
    log("AI 这一步：" + (data.reason || ""), "ok");
    runOps(data.ops, function () {
      log("=== 完成：已剪辑，重新读取序列 ===", "ok");
      setTimeout(dumpSequence, 400);
    });
  });
}

/* 独立字幕转写：同步最新序列和音轨后生成简/繁 SRT；不运行剪辑角色。 */
function transcribeSequence() {
  log("独立字幕转写：先同步当前序列和所选音轨；不会修改时间轴。", "ok");
  if ($("btnAsr")) { $("btnAsr").disabled = true; }
  captureSequence(function (sequenceOk, startSnapshot) {
    if (!sequenceOk) {
      if ($("btnAsr")) { $("btnAsr").disabled = false; }
      return;
    }
      var subtitleTracks = normaliseAudioTracks(audioTrackSelection);
      if (subtitleTracks === null) {
        log("字幕音轨设置无效，请重新选择。", "err");
        if ($("btnAsr")) { $("btnAsr").disabled = false; }
        return;
      }
      var pickedTracks = audioTrackNumbers(subtitleTracks);
      if (audioTrackCount && pickedTracks.length &&
          pickedTracks[pickedTracks.length - 1] > audioTrackCount) {
        log("所选字幕音轨不在当前序列内，请重新选择。", "err");
        if ($("btnAsr")) { $("btnAsr").disabled = false; }
        return;
      }
      log("字幕音轨已锁定：" + (subtitleTracks ?
          "A" + subtitleTracks.replace(/,/g, "、A") : "当前未静音轨"), "dim");
      var transcribeStarted = new Date().getTime();
      var transcribeHeartbeat = setInterval(function () {
        var elapsed = Math.max(1, Math.round(
          (new Date().getTime() - transcribeStarted) / 60000));
        log("字幕仍在转写（已用时约 " + elapsed +
            " 分钟）；剪辑和出片功能不受影响。", "dim");
      }, 60000);
      xhrPost("/transcribe", JSON.stringify({tracks: subtitleTracks}),
              function (status, text) {
        clearInterval(transcribeHeartbeat);
        var data = parseJson(text);
        if (status !== 200 || !data || !data.ok) {
          if ($("btnAsr")) { $("btnAsr").disabled = false; }
          log("独立字幕转写失败（不影响剪辑/出片）：" +
              ((data && data.error) || "HTTP " + status), "err");
          return;
        }
        var trackLabel = data.track_numbers && data.track_numbers.length ?
          "A" + data.track_numbers.join("、A") : "自动音轨";
        log("转写完成：" + data.count + " 句，覆盖 " + data.duration +
            " 秒；" + trackLabel + "；" + (data.model || "当前 ASR 模型"), "ok");
        var tracks = data.tracks || [];
        if (!tracks.length && data.srt) {
          tracks = [{name: "简体中文", path: data.srt}];
        }
        for (var fileIndex = 0; fileIndex < tracks.length; fileIndex++) {
          log((tracks[fileIndex].name || "字幕") + "文件：" +
              tracks[fileIndex].path, "dim");
        }
        var rows = data.segments || [];
        for (var i = 0; i < Math.min(5, rows.length); i++) {
          log("  " + rows[i].a + "~" + rows[i].b + "  " + rows[i].text, "dim");
        }
        if (data.warning) { log("字幕质量提示：" + data.warning, "dim"); }

        // 两份 SRT 只导入项目面板，不创建/改动当前时间轴上的任何片段。
        function importTrack(index, imported, failed) {
          if (index >= tracks.length) {
            if (imported) {
              log("已导入 " + imported + " 份字幕到项目面板；需要时手动拖入序列。", "ok");
            }
            if (failed) {
              log(failed + " 份字幕未自动导入，SRT 文件仍已正常生成。", "err");
            }
            log("独立字幕任务结束；没有运行编剧、剪辑师或导演，也没有修改时间轴。", "ok");
            if ($("btnAsr")) { $("btnAsr").disabled = false; }
            return;
          }
          var path = String(tracks[index].path || "");
          if (!path) { importTrack(index + 1, imported, failed + 1); return; }
          var safePath = path.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
          cs.evalScript("smartcutImportSrt('" + safePath + "')", function (res) {
            if (res && res.indexOf("ok") === 0) {
              importTrack(index + 1, imported + 1, failed);
            } else {
              log((tracks[index].name || "字幕") + "导入失败：" +
                  (res || "空"), "err");
              importTrack(index + 1, imported, failed + 1);
            }
          });
        }
        // 长转写期间用户可能继续剪片。只在当前序列与开始快照一字不差时
        // 自动导入；变化后保留两份 SRT，但绝不把旧时间轴字幕混进新序列。
        captureSequence(function (latestOk, latestSnapshot) {
          if (!latestOk || !latestSnapshot || !startSnapshot ||
              latestSnapshot.raw !== startSnapshot.raw) {
            log("转写期间时间轴发生了变化：简繁 SRT 已生成，但为避免旧字幕污染新序列，已跳过自动导入。", "err");
            if ($("btnAsr")) { $("btnAsr").disabled = false; }
            return;
          }
          importTrack(0, 0, 0);
        }, true);
      });
  });
}

/* ---- 绑定 ---- */
if ($("btnConnect")) { $("btnConnect").addEventListener("click", connect); }
if ($("btnDump")) { $("btnDump").addEventListener("click", dumpSequence); }
if ($("btnAudioSave")) {
  $("btnAudioSave").addEventListener("click", function () {
    saveAudioSettings(null);
  });
}
if ($("audioTrackToggle")) {
  $("audioTrackToggle").addEventListener("click", function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    toggleAudioTrackMenu();
  });
}
if ($("audioTrackMenu")) {
  $("audioTrackMenu").addEventListener("click", function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
  });
}
if ($("audioTrackAuto")) {
  $("audioTrackAuto").addEventListener("change", function () {
    if ($("audioTrackAuto").checked) {
      setAudioTrackSelection("");
    } else if (!audioTrackSelection) {
      $("audioTrackAuto").checked = true;
    }
  });
}
document.addEventListener("click", function () { toggleAudioTrackMenu(false); });
document.addEventListener("keydown", function (event) {
  event = event || window.event;
  if (event && (event.key === "Escape" || event.keyCode === 27)) {
    toggleAudioTrackMenu(false);
  }
});
if ($("btnAsr")) { $("btnAsr").addEventListener("click", transcribeSequence); }
if ($("btnStep")) { $("btnStep").addEventListener("click", aiStep); }
if ($("btnFull")) { $("btnFull").addEventListener("click", startSmartEdit); }

log("面板已加载。先点「连接剪辑软件」。", "dim");

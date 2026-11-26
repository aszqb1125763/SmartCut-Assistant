// 智能剪辑 PR 桥 · ExtendScript 侧（在 Premiere Pro 内执行）
// 任务：把本机接口给的剪辑单，按 in/out 一段段插进当前序列。

function scFindItem(root, path) {
  if (!root || !root.children) { return null; }
  for (var i = 0; i < root.children.numItems; i++) {
    var child = root.children[i];
    try {
      if (child.type === ProjectItemType.BIN) {
        var found = scFindItem(child, path);
        if (found) { return found; }
      } else if (child.getMediaPath && child.getMediaPath() === path) {
        return child;
      }
    } catch (e) { /* 有些 item 没有 getMediaPath */ }
  }
  return null;
}

function smartcutStatus() {
  try {
    if (!app.project) { return "ERR:没有打开 PR 项目"; }
    var seq = app.project.activeSequence;
    if (!seq) { return "项目已打开，但没有活动序列"; }
    var v = seq.videoTracks[0];
    var a = seq.audioTracks[0];
    return "序列「" + seq.name + "」视频 " +
      (v ? v.clips.numItems : 0) + " 段 / 音频 " +
      (a ? a.clips.numItems : 0) + " 段";
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// PR 的时间字段可能是 number，也可能是 Time 对象（带 .seconds），统一转成秒。
// 注意：Time 对象被 Number() 转换时得到的是 **ticks**（每秒 254,016,000,000），
// 直接当秒用会读出 57585427200000.0 这种天文数字。
var SC_TICKS_PER_SECOND = 254016000000;

function scSeconds(value) {
  if (value === null || value === undefined) { return 0; }
  if (typeof value === "number") {
    return value > 1e9 ? value / SC_TICKS_PER_SECOND : value;
  }
  try {
    if (typeof value.seconds === "number") { return value.seconds; }
  } catch (e) {}
  var n = Number(value);
  if (!isFinite(n)) { return 0; }
  return n > 1e9 ? n / SC_TICKS_PER_SECOND : n;
}

// 读轨道的静音状态（不同 PR 版本 API 写法不同，逐个试）
function scMuted(track) {
  try { if (typeof track.isMuted === "function") { return track.isMuted() ? "1" : "0"; } }
  catch (e1) {}
  try { if (typeof track.isMuted === "boolean") { return track.isMuted ? "1" : "0"; } }
  catch (e2) {}
  try { if (typeof track.getMute === "function") { return track.getMute() ? "1" : "0"; } }
  catch (e3) {}
  return "";
}

// 读当前序列：返回行文本（ExtendScript 没有 JSON，用竖线分隔最稳）
// 格式：
//   SEQ|序列名|视频轨数|音频轨数|总时长秒
//   TRACK|V/A|轨号|静音(1/0)
//   V|轨号|起点|终点|inPoint|outPoint|素材名|素材路径
//   A|轨号|起点|终点|inPoint|outPoint|素材名|素材路径
function smartcutDumpSequence() {
  try {
    if (!app.project) { return "ERR:没有打开 PR 项目"; }
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }

    function clipLine(prefix, trackIndex, clip) {
      var item = null;
      try { item = clip.projectItem; } catch (e) { item = null; }
      var name = "";
      var path = "";
      try { name = item ? item.name : ""; } catch (e2) {}
      try { path = (item && item.getMediaPath) ? item.getMediaPath() : ""; }
      catch (e3) {}
      return prefix + "|" + (trackIndex + 1) + "|" +
        scSeconds(clip.start).toFixed(3) + "|" +
        scSeconds(clip.end).toFixed(3) + "|" +
        scSeconds(clip.inPoint).toFixed(3) + "|" +
        scSeconds(clip.outPoint).toFixed(3) + "|" +
        name + "|" + path;
    }

    function dumpTracks(group, prefix, lines) {
      if (!group) { return; }
      for (var t = 0; t < group.numTracks; t++) {
        var clips = group[t].clips;
        for (var c = 0; c < clips.numItems; c++) {
          try { lines.push(clipLine(prefix, t, clips[c])); }
          catch (e4) {}
        }
      }
    }

    var lines = [];
    var total = 0;
    try { total = scSeconds(seq.end); } catch (e5) { total = 0; }
    lines.push("SEQ|" + seq.name + "|" + seq.videoTracks.numTracks +
               "|" + seq.audioTracks.numTracks + "|" + total.toFixed(3));
    var projectPath = "";
    try { projectPath = String(app.project.path || ""); } catch (e6) {}
    lines.push("PROJECT|" + projectPath);
    // 轨道静音状态（AI 靠这个判断"用户只想用哪条轨"）
    for (var vt = 0; vt < seq.videoTracks.numTracks; vt++) {
      lines.push("TRACK|V|" + (vt + 1) + "|" + scMuted(seq.videoTracks[vt]));
    }
    for (var at = 0; at < seq.audioTracks.numTracks; at++) {
      lines.push("TRACK|A|" + (at + 1) + "|" + scMuted(seq.audioTracks[at]));
    }
    dumpTracks(seq.videoTracks, "V", lines);
    dumpTracks(seq.audioTracks, "A", lines);
    return lines.join("\n");
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

function smartcutInsert(payload) {
  try {
    var text = decodeURIComponent(payload);
    var data;
    if (typeof JSON !== "undefined" && JSON && JSON.parse) {
      data = JSON.parse(text);
    } else {
      // PR 的 ExtendScript 是 ES3，没有 JSON 对象；
      // 这段数据是本机接口自己生成的，用 eval 解析安全且最省事。
      data = eval("(" + text + ")");
    }
    if (!app.project) { return "ERR:没有打开 PR 项目"; }
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列，先在 PR 里新建一个序列"; }

    // 这是早期兼容入口，只能向空序列顺序追加。若时间轴已有内容，
    // insertClip 会先改动现有片段，再到插入后校验阶段才报错；必须在
    // 导入素材、设置入出点和插入之前拒绝，避免“先修改、后失败”。
    var vt = seq.videoTracks[0];
    if (!vt) { return "ERR:序列没有视频轨"; }
    if (scTimelineClipCount(seq) !== 0) {
      return "ERR:当前时间轴不是空序列，旧插入入口已停止，未修改时间轴";
    }

    var src = data.source;
    var item = scFindItem(app.project.rootItem, src);
    if (!item) {
      app.project.importFiles([src], true, app.project.rootItem, false);
      item = scFindItem(app.project.rootItem, src);
    }
    if (!item) { return "ERR:素材导入失败：" + src; }

    var cursor = 0.0;
    var frameSeconds = scSequenceFrameSeconds(seq);
    var inserted = 0;
    var failed = [];
    for (var i = 0; i < data.spans.length; i++) {
      var a = data.spans[i][0];
      var b = data.spans[i][1];
      if (b - a < frameSeconds * 1.5) {
        failed.push((i + 1) + "（不足两帧）");
        continue;
      }
      try {
        item.setInPoint(a, 4);
        item.setOutPoint(b, 4);
      } catch (e1) {
        try {
          item.setInPoint(a, 1);
          item.setOutPoint(b, 1);
        } catch (e2) {
          failed.push(i + 1);
          continue;
        }
      }
      var beforeCount = vt.clips.numItems;
      var insertAt = cursor;
      vt.insertClip(item, insertAt);
      // PR 会自动把素材音频放上音轨，这里不再单独插一次（否则 2 条变 3 条）
      var appended = scInsertedTail(vt, beforeCount, insertAt, frameSeconds);
      if (appended.error) { return "ERR:" + appended.error; }
      cursor = appended.end;
      inserted++;
    }
    var msg = inserted + " 段，共 " + cursor.toFixed(1) + " 秒";
    if (failed.length) { msg += "；失败段：" + failed.join(","); }
    return msg;
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// 执行 AI 下的操作。payload 是竖线分隔的文本，每行一个操作：
//   OP|delete|片段序号(1起)|0
//   OP|razor|片段序号(1起)|切点秒
// 片段序号按「视频轨内、按起点排序」编号（和面板读出来的一致）
function smartcutApply(payload) {
  try {
    var text = decodeURIComponent(payload);
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }

    var rows = [];
    for (var t = 0; t < seq.videoTracks.numTracks; t++) {
      var clips = seq.videoTracks[t].clips;
      for (var c = 0; c < clips.numItems; c++) {
        rows.push({ clip: clips[c], track: t });
      }
    }
    rows.sort(function (x, y) {
      return scSeconds(x.clip.start) - scSeconds(y.clip.start);
    });

    var lines = text.split("\n");
    var done = 0;
    var errors = [];
    for (var i = 0; i < lines.length; i++) {
      if (!lines[i] || lines[i].indexOf("OP|") !== 0) { continue; }
      var p = lines[i].split("|");
      var op = p[1];
      var index = parseInt(p[2], 10) - 1;
      if (!rows[index]) { errors.push("序号越界 " + p[2]); continue; }
      try {
        if (op === "delete") {
          rows[index].clip.remove(0, 0);
          done++;
        } else if (op === "razor") {
          seq.razor(parseFloat(p[3]));
          done++;
        } else {
          errors.push("未知操作 " + op);
        }
      } catch (e1) {
        errors.push(op + "#" + p[2] + " 失败:" + e1.toString());
      }
    }
    var msg = "执行 " + done + " 个操作";
    if (errors.length) { msg += "；" + errors.join("；"); }
    return msg;
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// PR 26.x 已经没有 Sequence.razor()；QE rippleDelete 在部分 26.x 版本也会 no-op。
// 任意 A→B 区间应交给 PR 原生「提取（Extract）」：设置序列入/出点后执行一次
// Extract，PR 会同时切开两端、删除目标轨内容并做一次跨轨波纹。原生命令也会写入
// PR 的撤销历史。下面的工具函数专门为这条路径服务。
// 这里存的是 Premiere 内部菜单命令 ID，不是 Q/W/Ctrl+Z 等用户可修改的快捷键。
// 内部 ID 只允许在实机验证过的精确版本中回退；未知版本宁可停止，也不能猜 ID。
var SC_COMMAND_IDS_BY_VERSION = {
  "26.5.1": {
    extract: 11612,
    undo: 57643,
    addEdit: 11610,
    addEditAll: 11613,
    rippleDelete: 11305
  }
};

function scPremiereVersionKey() {
  var raw = "";
  try { raw = String(app.version || ""); } catch (e) { raw = ""; }
  var match = raw.match(/^(\d+\.\d+\.\d+)/);
  return match ? match[1] : raw;
}

function scKnownCommandId(commandKey) {
  if (!commandKey) { return null; }
  var version = scPremiereVersionKey();
  var row = SC_COMMAND_IDS_BY_VERSION[version];
  if (!row || row[commandKey] === undefined) { return null; }
  var id = parseInt(row[commandKey], 10);
  return (isFinite(id) && id > 0) ? id : null;
}

function scFindCommand(names, commandKey) {
  // 旧版 PR 若仍提供名称查询，优先使用它；这样语言和版本变化可由 PR 自己解析。
  if (typeof app.findMenuCommandId === "function") {
    for (var i = 0; i < names.length; i++) {
      var found = 0;
      try { found = app.findMenuCommandId(names[i]); } catch (e1) { found = 0; }
      found = parseInt(found, 10);
      if (isFinite(found) && found > 0) {
        return { id: found, name: names[i], source: "名称查询" };
      }
    }
  }

  // PR 26.5.1 已移除 findMenuCommandId，但 executeCommand 仍可执行内部命令。
  var fallback = scKnownCommandId(commandKey);
  if (fallback !== null && typeof app.executeCommand === "function") {
    return {
      id: fallback,
      name: commandKey,
      source: "PR " + scPremiereVersionKey() + " 已验证内部ID"
    };
  }
  return null;
}

function scSequenceEnd(seq) {
  // Sequence.end 是 PR 自己维护的真实序列尾；Extract 后会立即更新。
  try {
    var sequenceEnd = scSeconds(seq.end);
    if (isFinite(sequenceEnd) && sequenceEnd > 0) { return sequenceEnd; }
  } catch (e0) {}
  // 极少数旧版本取不到 seq.end 时才退回扫描片段。
  var top = 0;
  var groups = [seq.videoTracks, seq.audioTracks];
  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    if (!group) { continue; }
    for (var t = 0; t < group.numTracks; t++) {
      var clips = group[t].clips;
      for (var c = 0; c < clips.numItems; c++) {
        var value = scSeconds(clips[c].end);
        if (value > top) { top = value; }
      }
    }
  }
  return top;
}

// Premiere 会把 ProjectItem 入/出点量化到实际帧边界。请求 1.010 秒的
// 片段在 25fps 序列里可能真正插入 1.040 秒；下一段若仍从 1.010 插入，
// insert edit 会切开上一段尾部，留下随机的 1 帧残片。
function scSequenceFrameSeconds(seq) {
  var fallback = 1 / 30;
  try {
    if (!seq || typeof seq.getSettings !== "function") { return fallback; }
    var settings = seq.getSettings();
    var value = settings ? settings.videoFrameRate : null;
    try {
      if (value && typeof value.seconds === "number" &&
          value.seconds > 0 && value.seconds < 1) {
        return value.seconds;
      }
    } catch (e1) {}
    var raw = Number(value && value.ticks !== undefined ? value.ticks : value);
    if (isFinite(raw) && raw > 1000000) {
      var seconds = raw / SC_TICKS_PER_SECOND;
      if (seconds > 0 && seconds < 1) { return seconds; }
    }
    if (isFinite(raw) && raw > 0 && raw < 1) { return raw; }
  } catch (e2) {}
  return fallback;
}

// 只允许“在真实尾点追加一段”。插入后必须恰好多一个视频片段，并从
// Premiere 实际写入的 TrackItem.end 读取下一 cursor，绝不累计浮点请求时长。
function scInsertedTail(track, beforeCount, insertAt, frameSeconds) {
  scPauseForTimeline();
  var clips = track ? track.clips : null;
  if (!clips || clips.numItems !== beforeCount + 1) {
    return { error: "插入没有恰好新增一个视频片段；已停止，避免产生单帧残片" };
  }
  var inserted = clips[clips.numItems - 1];
  var actualStart = scSeconds(inserted.start);
  var actualEnd = scSeconds(inserted.end);
  var tolerance = Math.max(0.001, frameSeconds * 0.55);
  if (!isFinite(actualStart) || !isFinite(actualEnd) ||
      Math.abs(actualStart - insertAt) > tolerance) {
    return { error: "新片段没有落在上一段真实尾点；已停止，避免产生单帧残片" };
  }
  if (actualEnd - actualStart < frameSeconds * 1.5) {
    return { error: "Premiere 实际只插入了不足两帧的片段；已停止" };
  }
  return { end: actualEnd };
}

function scTrackLocked(track) {
  try {
    if (typeof track.isLocked === "function") { return !!track.isLocked(); }
  } catch (e1) {}
  try {
    if (typeof track.isLocked === "boolean") { return track.isLocked; }
  } catch (e2) {}
  return false;
}

function scTrackHasContentAfter(track, seconds) {
  try {
    var clips = track.clips;
    for (var i = 0; i < clips.numItems; i++) {
      if (scSeconds(clips[i].end) > seconds + 0.001) { return true; }
    }
  } catch (e) {}
  return false;
}

// 临时把所有未锁轨设为目标轨，确保删除的是整个时间区间，而不是只动某一条轨。
// 操作结束无论成功失败都会恢复用户原来的目标轨状态。
function scPrepareExtractTracks(seq, earliestStart) {
  var states = [];
  var locked = [];
  var groups = [seq.videoTracks, seq.audioTracks];
  var labels = ["V", "A"];
  var supported = 0;
  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    if (!group) { continue; }
    for (var t = 0; t < group.numTracks; t++) {
      var track = group[t];
      var isLocked = scTrackLocked(track);
      if (isLocked && scTrackHasContentAfter(track, earliestStart)) {
        locked.push(labels[g] + (t + 1));
      }
      if (typeof track.isTargeted === "function" &&
          typeof track.setTargeted === "function") {
        var old = false;
        try { old = !!track.isTargeted(); } catch (e1) { old = false; }
        states.push({ track: track, targeted: old });
        supported++;
        if (!isLocked) {
          try { track.setTargeted(true, true); } catch (e2) {}
        }
      }
    }
  }
  return { states: states, locked: locked, supported: supported };
}

function scRestoreExtractTracks(prepared) {
  if (!prepared || !prepared.states) { return; }
  for (var i = 0; i < prepared.states.length; i++) {
    try {
      prepared.states[i].track.setTargeted(prepared.states[i].targeted, true);
    } catch (e) {}
  }
}

function scReadSequenceMarks(seq) {
  var result = { inPoint: "-400000", outPoint: "-400000", playerTicks: null };
  try {
    var rawIn = String(seq.getInPoint());
    if (rawIn === "-400000") {
      result.inPoint = rawIn;
    } else if (typeof seq.getInPointAsTime === "function") {
      result.inPoint = String(scSeconds(seq.getInPointAsTime()));
    } else {
      result.inPoint = String(scSeconds(seq.getInPoint()));
    }
  } catch (e1) {}
  try {
    var rawOut = String(seq.getOutPoint());
    if (rawOut === "-400000") {
      result.outPoint = rawOut;
    } else if (typeof seq.getOutPointAsTime === "function") {
      result.outPoint = String(scSeconds(seq.getOutPointAsTime()));
    } else {
      result.outPoint = String(scSeconds(seq.getOutPoint()));
    }
  } catch (e2) {}
  try {
    var player = seq.getPlayerPosition();
    if (player && player.ticks !== undefined) {
      result.playerTicks = String(player.ticks);
    } else {
      result.playerTicks = String(Math.round(scSeconds(player) * SC_TICKS_PER_SECOND));
    }
  } catch (e3) {}
  return result;
}

function scRestoreSequenceMarks(seq, marks) {
  if (!marks) { return; }
  // -400000 是 PR 对“未设置入/出点”的标准哨兵值。
  try { seq.setInPoint(parseFloat(marks.inPoint)); } catch (e1) {}
  try { seq.setOutPoint(parseFloat(marks.outPoint)); } catch (e2) {}
  if (marks.playerTicks !== null) {
    try { seq.setPlayerPosition(marks.playerTicks); } catch (e3) {}
  }
}

function scParseDeleteRanges(text, limit) {
  var lines = text.split("\n");
  var ranges = [];
  for (var i = 0; i < lines.length; i++) {
    if (lines[i].indexOf("RANGE|") !== 0) { continue; }
    var p = lines[i].split("|");
    var start = parseFloat(p[1]);
    var end = parseFloat(p[2]);
    if (!isFinite(start) || !isFinite(end)) { continue; }
    start = Math.max(0, start);
    end = Math.min(limit, end);
    if (end - start > 0.01) { ranges.push([start, end]); }
  }
  ranges.sort(function (a, b) { return a[0] - b[0]; });
  var merged = [];
  for (var r = 0; r < ranges.length; r++) {
    var last = merged.length ? merged[merged.length - 1] : null;
    if (last && ranges[r][0] <= last[1] + 0.001) {
      if (ranges[r][1] > last[1]) { last[1] = ranges[r][1]; }
    } else {
      merged.push([ranges[r][0], ranges[r][1]]);
    }
  }
  merged.sort(function (a, b) { return b[0] - a[0]; });
  return merged;
}

function scPauseForTimeline() {
  try {
    if (typeof $ !== "undefined" && typeof $.sleep === "function") { $.sleep(50); }
  } catch (e) {}
}

function scUndoExtracts(count) {
  if (count <= 0) { return true; }
  var undo = scFindCommand(["撤销", "Undo", "撤销提取", "Undo Extract"], "undo");
  if (!undo) { return false; }
  for (var i = 0; i < count; i++) {
    try { app.executeCommand(undo.id); } catch (e) { return false; }
    scPauseForTimeline();
  }
  return true;
}

// 按时间区间删除：序列入/出点 → PR 原生 Extract → 后面自动前移。
// payload 每行 "RANGE|起点|终点"。这里会自己合并重叠区间并按起点降序执行。
// PR 26.x 没有 razor / executeCommand：把「要删的区间」换算成「保留段」，走重建。
// 调用方仍然按 RANGE|a|b 传删除区间，内部改走 smartcutRebuild，接口不变。
function scDeleteViaRebuild(payload) {
  try {
    var text = decodeURIComponent(payload);
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }

    // 1) 解析要删的区间（PR 时间轴）
    var cuts = [];
    var lines = text.split("\n");
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("RANGE|") !== 0) { continue; }
      var p = lines[i].split("|");
      var a = parseFloat(p[1]);
      var b = parseFloat(p[2]);
      if (b > a) { cuts.push([a, b]); }
    }
    if (!cuts.length) { return "ERR:没有有效的删除区间"; }

    // 2) 取第一条视频片段（单素材场景）：PR 时间 → 素材时间
    var vt = seq.videoTracks[0];
    if (!vt || vt.clips.numItems < 1) { return "ERR:视频轨没有片段"; }
    var clip = vt.clips[0];
    var clipStart = scSeconds(clip.start);
    var clipIn = scSeconds(clip.inPoint);
    var clipOut = scSeconds(clip.outPoint);
    var src = "";
    try {
      var item = clip.projectItem;
      if (item && item.getMediaPath) { src = item.getMediaPath(); }
    } catch (e1) {}
    if (!src) { return "ERR:取不到素材路径（无法重建）"; }

    var matCuts = [];
    for (var k = 0; k < cuts.length; k++) {
      var ca = Math.max(clipIn, clipIn + (cuts[k][0] - clipStart));
      var cb = Math.min(clipOut, clipIn + (cuts[k][1] - clipStart));
      if (cb - ca > 0.05) { matCuts.push([ca, cb]); }
    }
    matCuts.sort(function (x, y) { return x[0] - y[0]; });

    // 3) 算保留段（素材时间）
    var keeps = [];
    var cursor = clipIn;
    for (var m = 0; m < matCuts.length; m++) {
      if (matCuts[m][0] - cursor > 0.05) { keeps.push([cursor, matCuts[m][0]]); }
      cursor = Math.max(cursor, matCuts[m][1]);
    }
    if (clipOut - cursor > 0.05) { keeps.push([cursor, clipOut]); }
    if (!keeps.length) { return "ERR:删除后没有保留内容"; }

    // 4) 交给重建（清空 + 按保留段插入）
    var out = ["SRC|" + src];
    for (var n = 0; n < keeps.length; n++) {
      out.push("KEEP|" + keeps[n][0] + "|" + keeps[n][1]);
    }
    return smartcutRebuild(encodeURIComponent(out.join("\n")));
  } catch (e2) {
    return "ERR:" + e2.toString();
  }
}

function smartcutDeleteRanges(payload) {
  // PR 26.x 已移除 razor 和 executeCommand，统一改成「算保留段 → 重建」。
  return scDeleteViaRebuild(payload);

  // ↓ 以下旧实现（原生提取）不再执行，保留以防将来 PR 恢复接口
  var seq = null;
  var prepared = null;
  var marks = null;
  try {
    var text = decodeURIComponent(payload);
    seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }
    var originalEnd = scSequenceEnd(seq);
    if (originalEnd <= 0.01) { return "ERR:当前序列没有可剪内容"; }
    var ranges = scParseDeleteRanges(text, originalEnd);
    if (!ranges.length) { return "ERR:没有有效的删除区间"; }
    if (typeof app.executeCommand !== "function") {
      return "ERR:当前 PR 没有提供 executeCommand；为避免误剪，未修改时间轴";
    }

    var extract = scFindCommand(["提取", "Extract"], "extract");
    if (!extract) {
      return "ERR:PR " + scPremiereVersionKey() +
             " 无法安全解析‘提取/Extract’内部命令；未修改时间轴。" +
             "这与用户设置的快捷键无关";
    }

    var earliest = ranges[ranges.length - 1][0];
    prepared = scPrepareExtractTracks(seq, earliest);
    if (prepared.locked.length) {
      scRestoreExtractTracks(prepared);
      return "ERR:这些锁定轨会阻止整段波纹：" + prepared.locked.join(",") +
             "。请先解锁；未修改时间轴";
    }
    if (prepared.supported <= 0) {
      scRestoreExtractTracks(prepared);
      return "ERR:当前 PR 没有提供轨道目标接口；为避免剪错轨，未修改时间轴";
    }

    marks = scReadSequenceMarks(seq);
    var done = 0;
    var details = [];
    for (var i = 0; i < ranges.length; i++) {
      var start = ranges[i][0];
      var end = ranges[i][1];
      var expected = end - start;
      var beforeEnd = scSequenceEnd(seq);
      try {
        seq.setInPoint(start);
        seq.setOutPoint(end);
        seq.setPlayerPosition(String(Math.round(start * SC_TICKS_PER_SECOND)));
        app.executeCommand(extract.id);
      } catch (e1) {
        var rolledException = scUndoExtracts(done);
        scRestoreSequenceMarks(seq, marks);
        scRestoreExtractTracks(prepared);
        return "ERR:原生提取执行异常：" + e1.toString() +
               (rolledException ? "；已回滚本批次" : "；自动回滚失败，请按 Ctrl+Z " + done + " 次");
      }
      scPauseForTimeline();
      var afterEnd = scSequenceEnd(seq);
      var actual = beforeEnd - afterEnd;
      var tolerance = Math.max(0.12, Math.min(0.5, expected * 0.15));
      if (Math.abs(actual - expected) > tolerance) {
        var changed = Math.abs(actual) > 0.01 ? 1 : 0;
        var rollbackCount = done + changed;
        var rolled = scUndoExtracts(rollbackCount);
        scRestoreSequenceMarks(seq, marks);
        scRestoreExtractTracks(prepared);
        return "ERR:PR 原生提取没有按预期生效（" + start.toFixed(2) + "~" +
               end.toFixed(2) + "，应缩短 " + expected.toFixed(2) + " 秒，实际 " +
               actual.toFixed(2) + " 秒）" +
               (rolled ? "；已回滚本批次" : "；请立即按 Ctrl+Z " + rollbackCount + " 次");
      }
      done++;
      details.push(start.toFixed(2) + "~" + end.toFixed(2) +
                   "（缩短 " + actual.toFixed(2) + " 秒）");
    }

    scRestoreSequenceMarks(seq, marks);
    scRestoreExtractTracks(prepared);
    var finalEnd = scSequenceEnd(seq);
    var msg = "原生提取完成 " + done + " 个区间，序列 " + originalEnd.toFixed(2) +
              "→" + finalEnd.toFixed(2) + " 秒；Ctrl+Z 可逐段撤销";
    if (details.length) { msg += "｜" + details.join("；"); }
    return msg;
  } catch (e) {
    try { if (seq && marks) { scRestoreSequenceMarks(seq, marks); } } catch (e1) {}
    try { if (prepared) { scRestoreExtractTracks(prepared); } } catch (e2) {}
    return "ERR:" + e.toString();
  }
}

// PR 26.5.1 在 ExtendScript 中同时移除了 executeCommand 和
// findMenuCommandId。CEP 面板会在这种环境下通过无窗口本机 helper 调用 PR 自己的
// Win32 菜单命令。因为 helper 是异步进程，区间删除拆成“准备 → 原生提取 → 验证”
// 三步；所有轨道状态、入出点和批次进度都保存在这个短生命周期会话中。
var SC_NATIVE_EXTRACT_SESSION = null;

function scSequenceIdentity(seq) {
  try {
    if (seq.sequenceID !== undefined && seq.sequenceID !== null) {
      return "sequenceID:" + String(seq.sequenceID);
    }
  } catch (e1) {}
  try {
    if (seq.id !== undefined && seq.id !== null) {
      return "id:" + String(seq.id);
    }
  } catch (e2) {}
  var name = "";
  try { name = String(seq.name || ""); } catch (e3) {}
  return "name:" + name;
}

function scNativeMessage(value) {
  return String(value || "").replace(/\|/g, "/").replace(/[\r\n]+/g, " ");
}

function scNativeSessionReady(session) {
  if (!session || session.index >= session.ranges.length) {
    return "ERR:原生提取会话没有待处理区间";
  }
  var range = session.ranges[session.index];
  var start = range[0];
  var end = range[1];
  var beforeEnd = scSequenceEnd(session.seq);
  session.step = {
    start: start,
    end: end,
    expected: end - start,
    beforeEnd: beforeEnd
  };
  try {
    session.seq.setInPoint(start);
    session.seq.setOutPoint(end);
    session.seq.setPlayerPosition(String(Math.round(start * SC_TICKS_PER_SECOND)));
  } catch (e) {
    return "ERR:设置原生提取入出点失败：" + scNativeMessage(e.toString());
  }
  return "READY|" + session.token + "|" + session.index + "|" +
         start.toFixed(6) + "|" + end.toFixed(6) + "|" +
         session.step.expected.toFixed(6) + "|" + beforeEnd.toFixed(6) + "|" +
         session.ranges.length;
}

function scNativeRestoreAndClear(session) {
  try { scRestoreSequenceMarks(session.seq, session.marks); } catch (e1) {}
  try { scRestoreExtractTracks(session.prepared); } catch (e2) {}
  SC_NATIVE_EXTRACT_SESSION = null;
}

function scNativeGetSession(token) {
  var session = SC_NATIVE_EXTRACT_SESSION;
  if (!session || session.token !== String(token || "")) { return null; }
  return session;
}

function scNativeSequenceStillActive(session) {
  try {
    var active = app.project.activeSequence;
    return active && scSequenceIdentity(active) === session.sequenceIdentity;
  } catch (e) {
    return false;
  }
}

function smartcutDeleteBackend() {
  // PR 26.5.1 实测没有 app.executeCommand，也没有 razor；
  // 唯一可用的删除方式是「算保留段 → 重建」（insertClip 已验证可用）。
  // 这里直接返回 extendscript，让面板走 smartcutDeleteRanges → scDeleteViaRebuild。
  return "extendscript";
}

function smartcutBeginNativeExtract(payload) {
  var seq = null;
  var prepared = null;
  try {
    if (SC_NATIVE_EXTRACT_SESSION) {
      return "ERR:已有原生提取批次正在执行，请等待或重新打开面板";
    }
    var text = decodeURIComponent(payload);
    seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }
    var originalEnd = scSequenceEnd(seq);
    if (originalEnd <= 0.01) { return "ERR:当前序列没有可剪内容"; }
    var ranges = scParseDeleteRanges(text, originalEnd);
    if (!ranges.length) { return "ERR:没有有效的删除区间"; }

    var earliest = ranges[ranges.length - 1][0];
    prepared = scPrepareExtractTracks(seq, earliest);
    if (prepared.locked.length) {
      scRestoreExtractTracks(prepared);
      return "ERR:这些锁定轨会阻止整段波纹：" + prepared.locked.join(",") +
             "。请先解锁；未修改时间轴";
    }
    if (prepared.supported <= 0) {
      scRestoreExtractTracks(prepared);
      return "ERR:当前 PR 没有提供轨道目标接口；为避免剪错轨，未修改时间轴";
    }

    var token = "native_" + (new Date().getTime()) + "_" +
                Math.floor(Math.random() * 1000000000);
    var session = {
      token: token,
      seq: seq,
      sequenceIdentity: scSequenceIdentity(seq),
      ranges: ranges,
      index: 0,
      done: 0,
      originalEnd: originalEnd,
      marks: scReadSequenceMarks(seq),
      prepared: prepared,
      details: [],
      step: null,
      failure: ""
    };
    SC_NATIVE_EXTRACT_SESSION = session;
    var ready = scNativeSessionReady(session);
    if (ready.indexOf("ERR:") === 0) {
      scNativeRestoreAndClear(session);
    }
    return ready;
  } catch (e) {
    try { if (prepared) { scRestoreExtractTracks(prepared); } } catch (e2) {}
    SC_NATIVE_EXTRACT_SESSION = null;
    return "ERR:" + e.toString();
  }
}

function smartcutVerifyNativeExtract(token) {
  var session = scNativeGetSession(token);
  if (!session) { return "ERR:原生提取会话已失效"; }
  if (!scNativeSequenceStillActive(session)) {
    session.failure = "执行期间活动序列发生变化";
    return "ROLLBACK|" + session.token + "|" + session.done + "|" +
           scNativeMessage(session.failure);
  }
  scPauseForTimeline();
  var afterEnd = scSequenceEnd(session.seq);
  var actual = session.step.beforeEnd - afterEnd;
  var expected = session.step.expected;
  var tolerance = Math.max(0.12, Math.min(0.5, expected * 0.15));
  if (Math.abs(actual - expected) > tolerance) {
    var changed = Math.abs(actual) > 0.01 ? 1 : 0;
    session.failure = "PR 原生提取没有按预期生效（" +
      session.step.start.toFixed(2) + "~" + session.step.end.toFixed(2) +
      "，应缩短 " + expected.toFixed(2) + " 秒，实际 " +
      actual.toFixed(2) + " 秒）";
    return "ROLLBACK|" + session.token + "|" + (session.done + changed) + "|" +
           scNativeMessage(session.failure);
  }

  session.done++;
  session.details.push(session.step.start.toFixed(2) + "~" +
                       session.step.end.toFixed(2) + "（缩短 " +
                       actual.toFixed(2) + " 秒）");
  session.index++;
  if (session.index < session.ranges.length) {
    return scNativeSessionReady(session);
  }

  var finalEnd = scSequenceEnd(session.seq);
  var message = "原生菜单提取完成 " + session.done + " 个区间，序列 " +
    session.originalEnd.toFixed(2) + "→" + finalEnd.toFixed(2) +
    " 秒；Ctrl+Z 可逐段撤销";
  if (session.details.length) { message += "｜" + session.details.join("；"); }
  scNativeRestoreAndClear(session);
  return "DONE|" + scNativeMessage(message);
}

// helper 报错或超时时，先根据序列是否已经缩短判断当前命令有没有部分生效，
// 再把应撤销的次数交给面板。这里不直接清会话，因为必须等原生 Undo 完成后再恢复状态。
function smartcutFailNativeExtract(token, encodedReason) {
  var session = scNativeGetSession(token);
  if (!session) { return "ERR:原生提取会话已失效"; }
  var reason = "本机执行桥失败";
  try { reason = decodeURIComponent(encodedReason); } catch (e) {}
  var afterEnd = scSequenceEnd(session.seq);
  var actual = session.step ? session.step.beforeEnd - afterEnd : 0;
  var changed = Math.abs(actual) > 0.01 ? 1 : 0;
  session.failure = scNativeMessage(reason);
  return "ROLLBACK|" + session.token + "|" + (session.done + changed) + "|" +
         session.failure;
}

function smartcutFinishNativeRollback(token) {
  var session = scNativeGetSession(token);
  if (!session) { return "ERR:原生提取会话已失效"; }
  scPauseForTimeline();
  var endNow = scSequenceEnd(session.seq);
  var restored = Math.abs(endNow - session.originalEnd) <= 0.5;
  var failure = session.failure || "原生提取批次失败";
  scNativeRestoreAndClear(session);
  if (restored) {
    return "ERR:" + failure + "；已自动回滚本批次";
  }
  return "ERR:" + failure + "；自动撤销后序列长度仍为 " +
         endNow.toFixed(2) + " 秒，请检查 PR 撤销历史";
}

function smartcutAbortNativeExtract(token) {
  var session = scNativeGetSession(token);
  if (!session) { return "ERR:原生提取会话已失效"; }
  scNativeRestoreAndClear(session);
  return "ERR:已终止原生提取会话；时间轴未继续修改";
}

function scProjectPathKey(value) {
  return String(value || "").replace(/\//g, "\\").toLowerCase();
}

function scProjectCallSucceeded(value) {
  // ExtendScript documents 0 as success; a few older builds return true/undefined.
  return value === 0 || value === true || value === undefined || value === null;
}

function scTimelineClipCount(seq) {
  var total = 0;
  var groups = [seq.videoTracks, seq.audioTracks];
  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    if (!group) { continue; }
    for (var t = 0; t < group.numTracks; t++) {
      try { total += group[t].clips.numItems; } catch (e) {}
    }
  }
  return total;
}

function scLockedContentTracks(seq) {
  var locked = [];
  var groups = [seq.videoTracks, seq.audioTracks];
  var labels = ["V", "A"];
  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    if (!group) { continue; }
    for (var t = 0; t < group.numTracks; t++) {
      var track = group[t];
      try {
        if (track.clips.numItems > 0 && scTrackLocked(track)) {
          locked.push(labels[g] + (t + 1));
        }
      } catch (e) {}
    }
  }
  return locked;
}

function scParseAudioTrackNumbers(value) {
  var parts = String(value || "").replace(/，/g, ",").split(",");
  var result = [];
  for (var i = 0; i < parts.length; i++) {
    var number = parseInt(parts[i], 10);
    if (!isFinite(number) || number < 1 || number > 64) { continue; }
    var exists = false;
    for (var j = 0; j < result.length; j++) {
      if (result[j] === number) { exists = true; break; }
    }
    if (!exists) { result.push(number); }
  }
  result.sort(function (a, b) { return a - b; });
  return result;
}

function scFindImportedMediaItem(path) {
  var found = scFindItem(app.project.rootItem, path);
  if (found) { return found; }
  var bins = [];
  try { bins.push(app.project.getInsertionBin()); } catch (e0) {}
  try { bins.push(app.project.rootItem); } catch (e1) {}
  for (var i = 0; i < bins.length; i++) {
    var bin = bins[i];
    if (!bin || typeof bin.findItemsMatchingMediaPath !== "function") {
      continue;
    }
    try {
      var matches = bin.findItemsMatchingMediaPath(path, 1);
      if (matches && matches.length) { return matches[0]; }
      if (matches && matches.numItems > 0) { return matches[0]; }
    } catch (e2) {}
  }
  return null;
}

function scImportSubtitle(path, label) {
  var file = new File(path);
  if (!file.exists) { return {error: "ERR:字幕文件不存在：" + path}; }
  var item = scFindImportedMediaItem(path);
  if (!item) {
    if (typeof app.project.importFiles !== "function") {
      return {error: "ERR:当前 Premiere 不支持导入字幕文件"};
    }
    var imported = app.project.importFiles(
      [path], true, app.project.rootItem, false);
    if (imported === false) { return {error: "ERR:" + label + "字幕导入失败"}; }
    item = scFindImportedMediaItem(path);
  }
  if (!item) { return {error: "ERR:找不到刚导入的" + label + "字幕项目项"}; }
  return {item: item};
}

function scCreateCaptionTrack(seq, item, label) {
  if (!seq || typeof seq.createCaptionTrack !== "function") {
    return "ERR:当前 Premiere 不支持脚本创建原生字幕轨";
  }
  try {
    // Adobe 官方 PProPanel 示例使用两参数形式；省略 captionFormat 时
    // Premiere 默认创建 Subtitle 类型的原生 Caption Track。
    var created = seq.createCaptionTrack(item, 0);
    if (created === false) { return "ERR:" + label + "字幕轨创建失败"; }
  } catch (e) {
    return "ERR:" + label + "字幕轨创建失败：" + e.toString();
  }
  return "OK";
}

function scKeepSelectedAudioTracks(seq, selected, expectedClips) {
  if (!seq.audioTracks) { return "ERR:序列没有音频轨"; }
  if (!selected || !selected.length) { return "ERR:没有指定要保留的音轨"; }
  var keep = {};
  var highest = 0;
  for (var i = 0; i < selected.length; i++) {
    keep[String(selected[i])] = true;
    if (selected[i] > highest) { highest = selected[i]; }
  }
  if (highest > seq.audioTracks.numTracks) {
    return "ERR:所选 A" + highest + " 不存在；当前重建后只有 " +
           seq.audioTracks.numTracks + " 条音轨";
  }

  for (var t = 0; t < seq.audioTracks.numTracks; t++) {
    var trackNumber = t + 1;
    var clips = seq.audioTracks[t].clips;
    if (keep[String(trackNumber)]) {
      if (clips.numItems !== expectedClips) {
        return "ERR:所选 A" + trackNumber + " 没有完整插入（应有 " +
               expectedClips + " 段，实际 " + clips.numItems + " 段）";
      }
      continue;
    }
    for (var pass = 0; pass < 1000 && clips.numItems > 0; pass++) {
      try { clips[0].remove(0, 0); } catch (e) { break; }
    }
    if (clips.numItems > 0) {
      return "ERR:无法移除未选择的 A" + trackNumber + " 音轨片段";
    }
  }
  return "OK:仅保留音轨 " + selected.join(",");
}

// 完整工程交付：先另存一份“剪辑前备份”，只在这个副本里重建时间轴；
// 校验全部片段和总时长后，再另存为最终可编辑工程。原工程永远不会被覆盖。
// payload: BACKUP|...prproj / OUT|...prproj / SRC|... / KEEP|起点|终点
function smartcutRebuildAndSave(payload) {
  try {
    if (!app.project) { return "ERR:没有打开 PR 项目"; }
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }
    if (typeof app.project.saveAs !== "function" ||
        typeof app.project.save !== "function") {
      return "ERR:当前 Premiere 没有提供工程保存接口";
    }

    var text = decodeURIComponent(payload || "");
    var lines = text.split("\n");
    var backup = "";
    var output = "";
    var src = "";
    var audioMode = "all";
    var audioTracks = [];
    var subtitleTracks = [];
    var keeps = [];
    var previousEnd = -1;
    var frameSeconds = scSequenceFrameSeconds(seq);
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("BACKUP|") === 0) {
        backup = lines[i].substring(7);
      } else if (lines[i].indexOf("OUT|") === 0) {
        output = lines[i].substring(4);
      } else if (lines[i].indexOf("SRC|") === 0) {
        src = lines[i].substring(4);
      } else if (lines[i].indexOf("AUDIO_MODE|") === 0) {
        audioMode = String(lines[i].substring(11) || "all").toLowerCase();
      } else if (lines[i].indexOf("AUDIO_TRACKS|") === 0) {
        audioTracks = scParseAudioTrackNumbers(lines[i].substring(13));
      } else if (lines[i].indexOf("SUBTITLE|") === 0) {
        var subtitleParts = lines[i].split("|");
        if (subtitleParts.length < 3) {
          return "ERR:字幕轨参数不完整";
        }
        subtitleTracks.push({
          name: subtitleParts[1] || "字幕",
          path: subtitleParts.slice(2).join("|")
        });
      } else if (lines[i].indexOf("KEEP|") === 0) {
        var p = lines[i].split("|");
        var a = parseFloat(p[1]);
        var b = parseFloat(p[2]);
        if (!isFinite(a) || !isFinite(b) ||
            b - a < frameSeconds * 1.5) {
          return "ERR:存在无效的保留片段";
        }
        if (previousEnd >= 0 && a < previousEnd - 0.001) {
          return "ERR:保留片段顺序重叠，未修改工程";
        }
        keeps.push([a, b]);
        previousEnd = b;
      }
    }
    if (!backup || !/\.prproj$/i.test(backup)) {
      return "ERR:剪辑前备份路径不是 .prproj";
    }
    if (!output || !/\.prproj$/i.test(output)) {
      return "ERR:最终工程路径不是 .prproj";
    }
    if (!src) { return "ERR:没有素材路径"; }
    if (!keeps.length) { return "ERR:没有可写入时间轴的保留片段"; }
    if (audioMode !== "all" && audioMode !== "selected") {
      return "ERR:最终工程音轨模式无效";
    }
    if (audioMode === "selected" && !audioTracks.length) {
      return "ERR:只保留所选轨时没有提供音轨编号";
    }
    for (var subtitleCheck = 0; subtitleCheck < subtitleTracks.length;
         subtitleCheck++) {
      if (!subtitleTracks[subtitleCheck].path ||
          !(new File(subtitleTracks[subtitleCheck].path)).exists) {
        return "ERR:" + subtitleTracks[subtitleCheck].name +
               "字幕文件不存在";
      }
    }
    if (scProjectPathKey(backup) === scProjectPathKey(output)) {
      return "ERR:备份工程和最终工程不能是同一个文件";
    }
    var currentPath = "";
    try { currentPath = String(app.project.path || ""); } catch (e0) {}
    if (currentPath &&
        (scProjectPathKey(currentPath) === scProjectPathKey(backup) ||
         scProjectPathKey(currentPath) === scProjectPathKey(output))) {
      return "ERR:输出路径会覆盖当前工程，已拒绝执行";
    }

    var item = scFindItem(app.project.rootItem, src);
    if (!item) { return "ERR:PR 项目里找不到原素材：" + src; }
    var locked = scLockedContentTracks(seq);
    if (locked.length) {
      return "ERR:这些轨道已锁定，无法生成工程：" + locked.join(",");
    }
    if (!seq.videoTracks || !seq.videoTracks[0]) {
      return "ERR:活动序列没有视频轨";
    }
    if (typeof seq.videoTracks[0].insertClip !== "function" ||
        typeof item.setInPoint !== "function" ||
        typeof item.setOutPoint !== "function") {
      return "ERR:当前 Premiere 缺少重建可编辑时间轴的脚本接口";
    }

    var firstSave = app.project.saveAs(backup);
    if (!scProjectCallSucceeded(firstSave)) {
      return "ERR:剪辑前工程副本保存失败";
    }
    scPauseForTimeline();
    try {
      if (app.project.path &&
          scProjectPathKey(app.project.path) !== scProjectPathKey(backup)) {
        return "ERR:Premiere 没有切换到剪辑前工程副本";
      }
    } catch (e1) {}

    var cleared = smartcutClear();
    if (!cleared || cleared.indexOf("ERR:") === 0) {
      return cleared || "ERR:清空旧时间轴失败";
    }
    if (scTimelineClipCount(seq) !== 0) {
      return "ERR:旧时间轴没有清空，最终工程未保存";
    }

    var vt = seq.videoTracks[0];
    var cursor = 0.0;
    var done = 0;
    for (var k = 0; k < keeps.length; k++) {
      var ka = keeps[k][0];
      var kb = keeps[k][1];
      try {
        item.setInPoint(ka, 4);
        item.setOutPoint(kb, 4);
      } catch (e2) {
        try {
          item.setInPoint(ka, 1);
          item.setOutPoint(kb, 1);
        } catch (e3) {
          return "ERR:第 " + (k + 1) + " 段设置素材入出点失败";
        }
      }
      try {
        var beforeCount = vt.clips.numItems;
        var insertAt = cursor;
        vt.insertClip(item, insertAt);
        var appended = scInsertedTail(
          vt, beforeCount, insertAt, frameSeconds);
        if (appended.error) {
          return "ERR:第 " + (k + 1) + " 段：" + appended.error;
        }
        cursor = appended.end;
      } catch (e4) {
        return "ERR:第 " + (k + 1) + " 段插入失败：" + e4.toString();
      }
      done++;
    }
    if (done !== keeps.length) {
      return "ERR:时间轴片段数不完整，最终工程未保存";
    }
    if (audioMode === "selected") {
      var audioFiltered = scKeepSelectedAudioTracks(seq, audioTracks, done);
      if (!audioFiltered || audioFiltered.indexOf("ERR:") === 0) {
        return audioFiltered || "ERR:筛选最终工程音轨失败";
      }
    }
    var actual = scSequenceEnd(seq);
    var tolerance = Math.max(0.12, Math.min(0.75, cursor * 0.002));
    if (Math.abs(actual - cursor) > tolerance) {
      return "ERR:重建后时长校验失败（应为 " + cursor.toFixed(2) +
             " 秒，实际 " + actual.toFixed(2) + " 秒）";
    }

    var captionTracksCreated = 0;
    var captionMode = subtitleTracks.length ? "native" : "none";
    var captionWarnings = [];
    var importedSubtitles = [];
    // 先确保两份 SRT 都进入项目面板。旧 PR 缺字幕轨 API，或部分轨创建
    // 失败时，仍能将字幕作为可编辑素材连同最终工程一起保存。
    for (var subtitleIndex = 0; subtitleIndex < subtitleTracks.length;
         subtitleIndex++) {
      var importedSubtitle = scImportSubtitle(
        subtitleTracks[subtitleIndex].path, subtitleTracks[subtitleIndex].name);
      if (!importedSubtitle || importedSubtitle.error) {
        return (importedSubtitle && importedSubtitle.error) || "ERR:字幕导入失败";
      }
      importedSubtitles.push(importedSubtitle.item);
    }
    for (var captionIndex = 0; captionIndex < subtitleTracks.length;
         captionIndex++) {
      var subtitleResult = scCreateCaptionTrack(
        seq, importedSubtitles[captionIndex], subtitleTracks[captionIndex].name);
      if (!subtitleResult || subtitleResult.indexOf("ERR:") === 0) {
        captionMode = "imported";
        captionWarnings.push(subtitleResult ? subtitleResult.substring(4) :
                             "字幕轨创建失败");
        continue;
      }
      captionTracksCreated++;
    }

    var finalSaveAs = app.project.saveAs(output);
    if (!scProjectCallSucceeded(finalSaveAs)) {
      return "ERR:时间轴已生成，但最终 .prproj 另存失败；剪辑前备份仍然安全";
    }
    scPauseForTimeline();
    try {
      if (app.project.path &&
          scProjectPathKey(app.project.path) !== scProjectPathKey(output)) {
        return "ERR:Premiere 没有切换到最终可编辑工程";
      }
    } catch (e5) {}
    var finalSave = app.project.save();
    if (!scProjectCallSucceeded(finalSave)) {
      return "ERR:最终工程写入失败，请在 Premiere 中立即按 Ctrl+S";
    }
    return "OK|" + output + "|" + backup + "|" + done + "|" +
           actual.toFixed(3) + "|" + captionTracksCreated + "|" + captionMode +
           "|" + encodeURIComponent(captionWarnings.join("；"));
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// 按「保留段」重建序列：清空当前序列，再按素材时间逐段插入。
// 不走 razor（实测 razor 在这台 PR 上不生效），走 insertClip —— 这条路已验证可用。
// payload 第一行 "SRC|素材路径"，后面每行 "KEEP|起点|终点"（素材时间）
function smartcutRebuild(payload) {
  try {
    var text = decodeURIComponent(payload);
    var lines = text.split("\n");
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列，先建一个空序列"; }
    if (!app.project) { return "ERR:没有打开 PR 项目"; }

    var src = "";
    var keeps = [];
    var frameSeconds = scSequenceFrameSeconds(seq);
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("SRC|") === 0) {
        src = lines[i].substring(4);
      } else if (lines[i].indexOf("KEEP|") === 0) {
        var p = lines[i].split("|");
        var a = parseFloat(p[1]);
        var b = parseFloat(p[2]);
        if (!isFinite(a) || !isFinite(b) ||
            b - a < frameSeconds * 1.5) {
          return "ERR:存在不足两帧或无效的保留段";
        }
        keeps.push([a, b]);
      }
    }
    if (!src) { return "ERR:没有素材路径"; }
    if (!keeps.length) { return "ERR:没有保留段"; }

    var item = scFindItem(app.project.rootItem, src);
    if (!item) {
      app.project.importFiles([src], true, app.project.rootItem, false);
      item = scFindItem(app.project.rootItem, src);
    }
    if (!item) { return "ERR:素材导入失败：" + src; }

    // 清空当前序列。旧代码忽略清空失败，随后仍继续 insertClip；锁轨或
    // 受保护片段存在时会把新内容插进未清空的时间轴，形成残片或错位。
    var clearResult = smartcutClear();
    if (String(clearResult).indexOf("OK:") !== 0) {
      if (String(clearResult).indexOf("ERR:") === 0) { return clearResult; }
      return "ERR:清空时间轴失败：" + String(clearResult);
    }
    if (scTimelineClipCount(seq) !== 0) {
      return "ERR:清空结果校验失败，未继续插入";
    }

    var vt = seq.videoTracks[0];
    var at = seq.audioTracks[0];
    if (!vt) { return "ERR:序列没有视频轨"; }
    var cursor = 0.0;
    var done = 0;
    for (var k = 0; k < keeps.length; k++) {
      var ka = keeps[k][0];
      var kb = keeps[k][1];
      try {
        item.setInPoint(ka, 4);
        item.setOutPoint(kb, 4);
      } catch (e1) {
        try { item.setInPoint(ka, 1); item.setOutPoint(kb, 1); }
        catch (e2) { continue; }
      }
      try {
        var beforeCount = vt.clips.numItems;
        var insertAt = cursor;
        vt.insertClip(item, insertAt);
        // PR 的 insertClip 已自动把素材音频放上音轨；再插一次会复制成 3 条
        // （实测：2 条变 3 条）。这里只插视频，音频交给 PR 自动分配。
        var appended = scInsertedTail(
          vt, beforeCount, insertAt, frameSeconds);
        if (appended.error) {
          return "ERR:第 " + (k + 1) + " 段：" + appended.error;
        }
        cursor = appended.end;
        done++;
      } catch (e3) {
        return "ERR:第 " + (k + 1) + " 段插入失败：" + e3.toString();
      }
    }
    return "重建完成：" + done + " 段，共 " + cursor.toFixed(1) + " 秒";
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// 枚举 PR 暴露的所有 API（只读，不改时间轴）。
// 用来搞清楚「哪些编辑功能在脚本层真的存在」，不再靠猜。
function smartcutProbeAPI() {
  var out = [];
  function collect(label, obj, limit) {
    if (!obj) { out.push(label + "|(空)"); return; }
    var names = [];
    try {
      for (var key in obj) {
        var value;
        try { value = obj[key]; } catch (e1) { continue; }
        var kind = typeof value;
        if (kind === "function") { names.push(key + "()"); }
        else if (kind !== "function") { names.push(key); }
        if (names.length >= (limit || 200)) { break; }
      }
    } catch (e2) {
      out.push(label + "|(枚举失败:" + e2.toString().slice(0, 40) + ")");
      return;
    }
    out.push(label + "|" + names.join(","));
  }

  try {
    collect("app", app, 300);
    collect("app.project", app.project, 300);
    var seq = app.project ? app.project.activeSequence : null;
    if (!seq) {
      out.push("sequence|(没有活动序列)");
    } else {
      collect("sequence", seq, 300);
      var vt = seq.videoTracks ? seq.videoTracks[0] : null;
      collect("videoTrack", vt, 200);
      if (vt && vt.clips && vt.clips.numItems > 0) {
        collect("trackItem", vt.clips[0], 200);
      }
      var at = seq.audioTracks ? seq.audioTracks[0] : null;
      collect("audioTrack", at, 200);
    }
    // 几个关键能力的直接判断
    var caps = [];
    caps.push("executeCommand=" + (typeof app.executeCommand));
    caps.push("findMenuCommandId=" + (typeof app.findMenuCommandId));
    caps.push("enableQE=" + (typeof app.enableQE));
    caps.push("razor=" + (seq ? typeof seq.razor : "无序列"));
    caps.push("trim=" + (seq ? typeof seq.trim : "无序列"));
    caps.push("saveAs=" + (app.project ? typeof app.project.saveAs : "无项目"));
    caps.push("createCaptionTrack=" +
              (seq ? typeof seq.createCaptionTrack : "无序列"));
    caps.push("caption_mode=" +
              (seq && typeof seq.createCaptionTrack === "function" ?
               "native" : "imported"));
    out.push("capabilities|" + caps.join(","));
  } catch (e3) {
    out.push("ERR|" + e3.toString());
  }
  return out.join("\n");
}

// 把播放头定位到指定秒数（ExtendScript 是唯一可靠的定位方式）
function smartcutSeek(seconds) {
  try {
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }
    var value = parseFloat(seconds);
    if (!isFinite(value) || value < 0) { return "ERR:秒数不合法"; }
    var ticks = String(Math.round(value * SC_TICKS_PER_SECOND));
    seq.setPlayerPosition(ticks);
    return "ok:" + value.toFixed(2);
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// 把 SRT 字幕导入当前项目（导入后项目面板会出现字幕项，可拖到序列）
function smartcutImportSrt(path) {
  try {
    if (!path) { return "ERR:没有给路径"; }
    var file = new File(path);
    if (!file.exists) { return "ERR:文件不存在 " + path; }
    var before = 0;
    try { before = app.project.rootItem.children.numItems; } catch (e0) {}
    app.project.importFiles([path], false, app.project.rootItem, false);
    var after = 0;
    try { after = app.project.rootItem.children.numItems; } catch (e1) {}
    return "ok:" + before + "→" + after;
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

// 只读探测菜单命令 ID；不会修改时间轴。
function smartcutFindCommands() {
  var names = [
    "提取",
    "Extract",
    "波纹修剪上一个编辑点到播放指示器",
    "Ripple Trim Previous Edit To Playhead",
    "波纹修剪下一个编辑点到播放指示器",
    "Ripple Trim Next Edit To Playhead",
    "添加编辑",
    "Add Edit",
    "撤销",
    "Undo",
    "Ripple Delete",
    "Razor",
    "波纹删除",
    "剃刀"
  ];
  var out = [];
  out.push("PR " + scPremiereVersionKey());
  out.push("executeCommand=" +
           (typeof app.executeCommand === "function" ? "可用" : "不可用"));
  out.push("findMenuCommandId=" +
           (typeof app.findMenuCommandId === "function" ? "可用" : "不可用"));
  for (var i = 0; i < names.length; i++) {
    var id = null;
    if (typeof app.findMenuCommandId === "function") {
      try { id = app.findMenuCommandId(names[i]); } catch (e) { id = "异常"; }
    } else {
      id = "名称查询不可用";
    }
    out.push(names[i] + " → " + id);
  }
  var fallbackExtract = scKnownCommandId("extract");
  var fallbackUndo = scKnownCommandId("undo");
  out.push("版本回退提取=" + (fallbackExtract === null ? "无" : fallbackExtract));
  out.push("版本回退撤销=" + (fallbackUndo === null ? "无" : fallbackUndo));
  out.push("不读取、不发送用户快捷键");
  return out.join("｜");
}

function smartcutProbe() {
  try {
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }
    var out = [];
    var extract = scFindCommand(["提取", "Extract"], "extract");
    var undo = scFindCommand(["撤销", "Undo"], "undo");
    out.push("PR " + app.version);
    out.push("executeCommand=" +
             (typeof app.executeCommand === "function" ? "可用" : "不可用"));
    out.push("提取=" + (extract ? extract.id + "(" + extract.source + ")" : "未找到"));
    out.push("撤销=" + (undo ? undo.id + "(" + undo.source + ")" : "未找到"));
    out.push("快捷键=不依赖");
    out.push("序列=" + seq.name + " / " + scSequenceEnd(seq).toFixed(2) + "秒");
    var prepared = scPrepareExtractTracks(seq, scSequenceEnd(seq));
    scRestoreExtractTracks(prepared);
    out.push("可控目标轨=" + prepared.supported);
    out.push("受影响锁轨=" + (prepared.locked.length ? prepared.locked.join(",") : "无"));
    return out.join("｜");
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

function smartcutClear() {
  try {
    var seq = app.project.activeSequence;
    if (!seq) { return "ERR:没有活动序列"; }
    var locked = scLockedContentTracks(seq);
    if (locked.length) {
      return "ERR:这些轨道已锁定：" + locked.join(",");
    }
    var removed = 0;
    for (var pass = 0; pass < 500; pass++) {
      var did = false;
      for (var ti = 0; ti < 2; ti++) {
        var group = (ti === 0) ? seq.videoTracks : seq.audioTracks;
        for (var i = 0; i < group.numTracks; i++) {
          var clips = group[i].clips;
          if (clips.numItems > 0) {
            try { clips[0].remove(0, 0); removed++; did = true; } catch (e) {}
          }
        }
      }
      if (!did) { break; }
    }
    if (scTimelineClipCount(seq) !== 0) {
      return "ERR:仍有片段无法移除，可能存在锁定或受保护内容";
    }
    return "OK:清掉 " + removed + " 段";
  } catch (e) {
    return "ERR:" + e.toString();
  }
}

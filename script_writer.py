# -*- coding: utf-8 -*-
"""先读片、再出剧本，最后才剪。

为什么分两步：
  · 直接剪 = 模型只看得到眼前几十句，不知道整期在讲什么，
    容易把铺垫剪掉、把重复内容留下
  · 先出剧本 = 模型通读一遍，理清"这期分几段、每段在干什么"，
    剪辑时按剧本走，结构才连贯

产物存进 index.json 的 script 字段：
  sections: [{title, start, end, summary, importance}]
"""
import json
import os
import re
import sys

import source_timeline
from memory_config import app_home

BASE = app_home()
sys.path.insert(0, BASE)

# The segment model currently runs with a 4096-token context.  Leave room for
# the system prompt, task instructions and reply instead of batching only by
# transcript sentence count.
READ_BODY_MAX_CHARS = 2600
READ_BODY_MAX_TOKENS = 2400
READ_SPLIT_DEPTH = 8
ERROR_DETAIL_MAX_CHARS = 1000

SYS_READ = ("你是资深游戏实况编导。输入是一条按绝对源时间展开的事件流，不是按文字"
            "数组排列的文章。必须先按 t 起止、停顿、重叠和声音状态理解先后关系，再把"
            "文本当作该时间区间的一层证据。要判断这一段在讲什么、有多重要。只按格式输出。")

PROMPT_READ = """下面是实况源片的一段时间轴事件。U 是带台词的区间，G 是没有检测到台词的
真实区间（不代表静音，可能仍有游戏声/音乐/音效）。t 是绝对源时间，d 是持续时长，
gap/ov 是与前后事件的空隙或重叠。事件在文字意义上相邻，不代表播放时间上连续；必须以 t 为准。

画面值越大表示画面变化越剧烈（造机器、战斗、爆炸），音量越大表示说得越激动。

请概括这一段：
1. 这一段主要在干什么（不超过 25 字）
2. 重要度 1-5（5=关键进展或高光，1=找东西/废话）
3. 这一段里最关键的 U 事件 ID（最多 3 个，不要填写秒数）

输出格式固定三行：
摘要：xxx
重要度：4
关键：U0012,U0013,U0040

时间轴事件：
%s"""

SYS_PLAN = ("你是资深游戏实况编导。根据分段摘要，写这期视频的剧本大纲。"
            "只按格式输出。")

LEVEL_SCRIPT_RULES = {
    "light": ("轻度：剧本要保留完整上下文和各章过程，只把明显重复、失败重录、"
              "纯等待标成可删；不要为了缩时长合并掉剧情步骤。"),
    "medium": ("中度：每章必须写清目标、主要过程、结果和到下一章的过渡；"
               "可以删重复铺垫和弱支线，但主线过程要完整。"),
    "heavy": ("重度：目标是最短完整故事链，不是单纯最短时长。每个主体章节都必须"
              "包含目标、关键过程、结果/评价和过渡；优先删重复、等待和支线，"
              "不得为了压时长删掉因果链中间步骤。"),
}


class ScriptCancelled(RuntimeError):
    pass


def _check_cancel(should_stop):
    if should_stop and should_stop():
        raise ScriptCancelled("已取消剧本分析")

PROMPT_PLAN = """下面是同一期实况各时间轴段的摘要。B 是稳定段落 ID，t 是绝对源时间。
段落出现于文字中的先后不能覆盖 t 的真实先后；章节只能引用已有 B 边界。

当前剪辑力度要求：
{level_rule}

请把它整理成这期视频的剧本：合并成 4-8 个章节，每章给一个标题，
并说明这一章保留多少内容（全部保留 / 只留关键 / 可以大幅删减）。

输出格式，一章一行：
章节编号|开始段Bxxx|结束段Bxxx|标题|取舍建议

例：1|B001|B003|找到资源并完成第一台机器|全部保留

最后再加一行总结：整期主线是什么（不超过 40 字），以「主线：」开头。

时间轴段摘要：
{body}

只输出上述格式。"""


def _ts(sec):
    """秒 → MM:SS，剧本文件里好读。"""
    try:
        t = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    return "%02d:%02d" % (t // 60, t % 60)


def _source_events(data):
    """Build the one source-coordinate chronology shown to the writer."""
    events = source_timeline.build_events(
        data.get("segments") or [],
        duration=data.get("duration_exact", data.get("duration")),
        min_gap=0.35)
    # Bind the cached script to the same light-weight sound/visual evidence
    # that the writer sees, not only to timestamped text.
    for event in events:
        if event.get("kind") == "speech":
            motion, loudness = _signal_values(
                data, event.get("source_index", event.get("source_pos")))
            event["audio"] = "motion=%.4f;loudness_db=%.3f" % (
                motion, loudness)
        else:
            peak, mean, visual = _gap_signal_values(
                data, event.get("start", 0.0), event.get("end", 0.0))
            event["audio"] = (
                "gap_peak_db=%.3f;gap_mean_db=%.3f;visual_peak=%.4f"
                % (peak, mean, visual))
    return events


def _signal_values(data, source_index):
    feat = data.get("seg_feat") or []
    if not isinstance(source_index, int) or source_index >= len(feat):
        return 0.0, -120.0
    try:
        return float(feat[source_index][0]), float(feat[source_index][1])
    except (TypeError, ValueError, IndexError):
        return 0.0, -120.0


def _gap_signal_values(data, start, end):
    """Summarise indexed sound/visual evidence inside a no-transcript event."""
    analysis = data.get("analysis") or {}
    audio = analysis.get("audio_1s") or []
    left = max(0, int(float(start)))
    right = min(len(audio), max(left + 1, int(float(end) + 0.999)))
    audio_values = []
    for value in audio[left:right]:
        try:
            audio_values.append(float(value))
        except (TypeError, ValueError):
            continue
    audio_peak = max(audio_values) if audio_values else -120.0
    audio_mean = (sum(audio_values) / len(audio_values)
                  if audio_values else -120.0)

    times = analysis.get("times") or []
    diffs = analysis.get("diffs") or []
    visual_values = []
    for seconds, value in zip(times, diffs):
        try:
            seconds, value = float(seconds), float(value)
        except (TypeError, ValueError):
            continue
        if float(start) <= seconds <= float(end):
            visual_values.append(value)
    visual_peak = max(visual_values) if visual_values else 0.0
    return audio_peak, audio_mean, visual_peak


def _fmt_event(data, event):
    """Compact event record: chronology first, transcript last."""
    event_id = str(event.get("event_id") or "?")
    start = float(event.get("start", event.get("a", 0.0)))
    end = float(event.get("end", event.get("b", start)))
    duration = max(0.0, end - start)
    if event.get("kind") == "gap":
        peak, mean, visual = _gap_signal_values(data, start, end)
        return ("%s | G | t=%.2f-%.2f | d=%.2f | 声音峰=%.1fdB | "
                "声音均值=%.1fdB | 画面变化峰=%.2f | 无台词区（不等于静音）"
                % (event_id, start, end, duration, peak, mean, visual))
    source_index = event.get("source_index", event.get("source_pos"))
    motion, loudness = _signal_values(data, source_index)
    if motion >= 6:
        visual = "剧烈"
    elif motion >= 2:
        visual = "变化"
    else:
        visual = "平缓"
    voice = "响" if loudness >= -18 else "普通/弱"
    text = " ".join(str(event.get("text") or "").split()).strip()
    return (
        "%s | U | t=%.2f-%.2f | d=%.2f | gap-=%.2f | ov-=%.2f | "
        "gap+=%.2f | ov+=%.2f | 画面=%s(%.2f) | 声音=%s(%.1fdB) | 文本=%s"
        % (event_id, start, end, duration,
           float(event.get("prev_gap") or 0.0),
           float(event.get("prev_overlap") or 0.0),
           float(event.get("next_gap") or 0.0),
           float(event.get("next_overlap") or 0.0),
           visual, motion, voice, loudness, text or "（无可用转写）"))


def _fmt_signals(data):
    """Compatibility helper returning the canonical time-ordered records."""
    return [_fmt_event(data, event) for event in _source_events(data)]


def _estimate_tokens(text):
    """Conservative token estimate for mixed Chinese/ASCII timeline text."""
    ascii_chars = 0
    non_ascii_chars = 0
    for char in str(text or ""):
        if ord(char) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return non_ascii_chars + (ascii_chars + 3) // 4


def _event_batches(events, max_speech=50, data=None,
                   max_chars=READ_BODY_MAX_CHARS,
                   max_tokens=READ_BODY_MAX_TOKENS):
    """Split complete events using sentence, character and token budgets."""
    max_speech = max(1, int(max_speech))
    max_chars = max(1, int(max_chars)) if max_chars else None
    max_tokens = max(1, int(max_tokens)) if max_tokens else None
    batches = []
    current = []
    current_lines = []
    speech_count = 0
    for event in events:
        is_speech = event.get("kind") == "speech"
        line = _fmt_event(data, event) if data is not None else ""
        next_chars = (sum(len(value) for value in current_lines)
                      + max(0, len(current_lines)) + len(line))
        next_tokens = (_estimate_tokens("\n".join(current_lines))
                       + (1 if current_lines else 0)
                       + _estimate_tokens(line))
        exceeds_size = data is not None and (
            (max_chars is not None and next_chars > max_chars)
            or (max_tokens is not None and next_tokens > max_tokens))
        # Keep a leading G event with the following speech event.  A single
        # oversized event is never cut in half; the runtime retry path can
        # still report the model's exact response if it cannot be reduced.
        if current and speech_count > 0 and (
                (is_speech and speech_count >= max_speech) or exceeds_size):
            batches.append(current)
            current = []
            current_lines = []
            speech_count = 0
        current.append(event)
        current_lines.append(line)
        if is_speech:
            speech_count += 1
    if current:
        batches.append(current)
    return batches


def _split_event_batch(events):
    """Bisect a batch at a speech-event boundary without cutting an event."""
    speech_positions = [index for index, event in enumerate(events)
                        if event.get("kind") == "speech"]
    if len(speech_positions) < 2:
        return None
    split_at = speech_positions[len(speech_positions) // 2]
    left, right = events[:split_at], events[split_at:]
    if not left or not right:
        return None
    return left, right


def _error_detail(exc):
    """Return a bounded HTTP response body when urllib exposes one."""
    reader = getattr(exc, "read", None)
    if not callable(reader):
        return ""
    try:
        detail = reader()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        closer = getattr(exc, "close", None)
        if callable(closer):
            try:
                closer()
            except OSError:
                pass
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", errors="replace")
    detail = " ".join(str(detail or "").split())
    if len(detail) > ERROR_DETAIL_MAX_CHARS:
        detail = detail[:ERROR_DETAIL_MAX_CHARS] + "..."
    return detail


def _is_request_too_large(exc, detail=""):
    """Recognise context-window failures from status or response text."""
    from llm_gateway import APIRequestError
    if isinstance(exc, APIRequestError):
        # An invalid API credential is often described as an invalid "token";
        # splitting source batches cannot fix authentication or model errors.
        message = ("%s %s" % (exc, detail)).lower()
        return any(marker in message for marker in (
            "context window", "context_length", "maximum context", "context limit",
            "prompt exceeds", "prompt too long", "too many tokens", "request too large",
            "上下文过长", "超出上下文", "上下文长度超", "请求过大", "http 413"))
    if getattr(exc, "code", None) == 400:
        return True
    message = "%s %s" % (exc, detail)
    message = message.lower()
    return any(marker in message for marker in (
        "context", "token", "prompt", "too long", "too large", "exceed"))


def _read_batch_with_retry(srv, data, events, batch_index, on_log,
                           should_stop=None, depth=0):
    """Read one batch, recursively reducing it after context-size errors."""
    _check_cancel(should_stop)
    body = "\n".join(_fmt_event(data, event) for event in events)
    try:
        raw = srv.chat(SYS_READ, PROMPT_READ % body, max_tokens=300)
        return [(events, raw)]
    except Exception as exc:  # noqa: BLE001
        detail = _error_detail(exc)
        description = str(exc)
        if detail:
            description += "；服务返回：%s" % detail
        split = _split_event_batch(events)
        if (depth < READ_SPLIT_DEPTH and split
                and _is_request_too_large(exc, detail)):
            left, right = split
            left_count = sum(event.get("kind") == "speech" for event in left)
            right_count = sum(event.get("kind") == "speech" for event in right)
            on_log("  第 %d 段请求过大，自动拆为 %d + %d 句重试；%s"
                   % (batch_index, left_count, right_count, description))
            return (_read_batch_with_retry(
                        srv, data, left, batch_index, on_log,
                        should_stop=should_stop, depth=depth + 1)
                    + _read_batch_with_retry(
                        srv, data, right, batch_index, on_log,
                        should_stop=should_stop, depth=depth + 1))
        on_log("  第 %d 段失败：%s" % (batch_index, description))
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        return []


def _key_ids(raw):
    """Parse stable U ids; old numeric replies remain a safe fallback."""
    return [value.upper() for value in re.findall(r"\bU\d{1,8}\b", raw or "",
                                                  flags=re.I)]


def fallback_sections(chunks, want=None, level="medium"):
    """模型没把分段合并成章节时的兜底：按时间顺序合并成 want 章。

    为什么会走到这：实测 99 分钟的素材有 41 个分段，模型偶尔会
    把每个分段原样当一章吐回来（38 章），主线也空着。
    这时按重要度自行归并，至少保证结构可用。
    """
    if not chunks:
        return [], ""
    level = level if level in LEVEL_SCRIPT_RULES else "medium"
    if want is None:
        want = {"light": 8, "medium": 6, "heavy": 5}[level]
    n = max(1, min(want, len(chunks)))
    per = (len(chunks) + n - 1) // n
    out = []
    for k in range(0, len(chunks), per):
        group = chunks[k:k + per]
        best = max(group, key=lambda c: c.get("importance", 0))
        avg = sum(c.get("importance", 0) for c in group) / len(group)
        if level == "light":
            adv = "全部保留" if avg >= 2.5 else "只留关键"
        elif level == "heavy" and avg < 2.6:
            adv = "可以大幅删减"
        elif avg >= 4.5:
            adv = "全部保留"
        elif avg >= 3.5:
            adv = "只留关键"
        else:
            adv = "可以大幅删减"
        out.append({"title": best.get("summary") or "（未命名章节）",
                    "start": group[0]["start"], "end": group[-1]["end"],
                    "advice": adv,
                    "source_chunks": [
                        str(c.get("chunk_id") or "B%03d" % int(c["idx"]))
                        for c in group]})
    top = max(chunks, key=lambda c: c.get("importance", 0))
    return out, (top.get("summary") or "")


def read_pass(data, batch=50, on_log=print, should_stop=None):
    """第一遍：按源时间轴读片，产出摘要、重要度和关键事件。"""
    import proofread as pf
    segs = data.get("segments") or []
    events = _source_events(data)
    event_lookup = source_timeline.event_map(events)
    batches = _event_batches(
        events, max_speech=batch, data=data,
        max_chars=READ_BODY_MAX_CHARS,
        max_tokens=READ_BODY_MAX_TOKENS)
    chunks = []
    _check_cancel(should_stop)
    on_log("  编剧分批：%d 段（正文最多 %d 字 / 约 %d tokens）"
           % (len(batches), READ_BODY_MAX_CHARS, READ_BODY_MAX_TOKENS))
    with pf.Server(task="segment", port=8132, api_role="writer") as srv:
        speech_done = 0
        for batch_index, part in enumerate(batches, 1):
            _check_cancel(should_stop)
            if not any(event.get("kind") == "speech" for event in part):
                continue
            results = _read_batch_with_retry(
                srv, data, part, batch_index, on_log,
                should_stop=should_stop)
            for resolved_part, raw in results:
                _check_cancel(should_stop)
                speech_part = [event for event in resolved_part
                               if event.get("kind") == "speech"]
                summ = imp = 0
                keys = []
                key_event_ids = []
                for line in (raw or "").splitlines():
                    line = line.strip()
                    if line.startswith("摘要"):
                        summ = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                    elif line.startswith("重要度"):
                        m = re.search(r"(\d)", line)
                        imp = int(m.group(1)) if m else 0
                    elif line.startswith("关键"):
                        body2 = line.split("：", 1)[-1].split(":", 1)[-1]
                        key_event_ids = _key_ids(body2)[:3]
                        for event_id in key_event_ids:
                            event = event_lookup.get(event_id)
                            source_index = (event or {}).get("source_index")
                            if (event in resolved_part
                                    and isinstance(source_index, int)
                                    and source_index not in keys):
                                keys.append(source_index)
                        # Old local-number output is accepted only inside this
                        # batch. It can never address a sentence not shown.
                        if not key_event_ids:
                            local = [int(value) for value in
                                     re.findall(r"\d+", body2)][:3]
                            for number in local:
                                if 1 <= number <= len(speech_part):
                                    source_index = speech_part[
                                        number - 1].get("source_index")
                                    if isinstance(source_index, int):
                                        keys.append(source_index)
                a = min(float(event["start"]) for event in resolved_part)
                b = max(float(event["end"]) for event in resolved_part)
                chunk_id = "B%03d" % (len(chunks) + 1)
                chunks.append({
                    "idx": len(chunks) + 1, "start": a, "end": b,
                    "chunk_id": chunk_id,
                    "summary": summ or "（没读出来）", "importance": imp,
                    "keys": keys,
                    "key_event_ids": key_event_ids,
                    "event_ids": [event["event_id"]
                                  for event in resolved_part]})
                speech_done += len(speech_part)
                on_log("  读片 %d/%d：%s（重要度 %d）"
                       % (min(speech_done, len(segs)), len(segs),
                          summ[:24], imp))
    return chunks


def plan_pass(chunks, on_log=print, should_stop=None, level="medium",
              keep_ratio=None):
    """第二遍：把分段摘要整理成剧本。"""
    import proofread as pf
    lines = []
    for c in chunks:
        chunk_id = str(c.get("chunk_id") or "B%03d" % int(c["idx"]))
        lines.append("%s / t=%.2f~%.2f秒 / d=%.2f秒 / %s / 重要度 %d"
                     % (chunk_id, float(c["start"]), float(c["end"]),
                        max(0.0, float(c["end"]) - float(c["start"])),
                        c["summary"], c["importance"]))
    body = "\n".join(lines)
    _check_cancel(should_stop)
    level = level if level in LEVEL_SCRIPT_RULES else "medium"
    level_rule = LEVEL_SCRIPT_RULES[level]
    if keep_ratio is not None:
        level_rule += ("\n目标保留约 %.0f%% 的有效内容；长时间无人说话的区间不计入"
                       "保留比例，也不能为了凑时长保留。" %
                       (float(keep_ratio) * 100.0))
    with pf.Server(task="paragraph", port=8133, ctx=8192,
                   api_role="writer") as srv:
        raw = srv.chat(
            SYS_PLAN,
            PROMPT_PLAN.format(level_rule=level_rule, body=body),
            max_tokens=900)
    _check_cancel(should_stop)
    sections, mainline = [], ""
    chunk_lookup = {
        str(c.get("chunk_id") or "B%03d" % int(c["idx"])).upper(): c
        for c in chunks}
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("主线"):
            mainline = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            continue
        parts = [x.strip() for x in re.split(r"[|｜]", line)]
        if len(parts) >= 5 and parts[0].isdigit():
            start_chunk = chunk_lookup.get(parts[1].upper())
            end_chunk = chunk_lookup.get(parts[2].upper())
            if start_chunk and end_chunk:
                if int(start_chunk["idx"]) > int(end_chunk["idx"]):
                    start_chunk, end_chunk = end_chunk, start_chunk
                a, b = float(start_chunk["start"]), float(end_chunk["end"])
                source_chunks = [
                    str(c.get("chunk_id") or "B%03d" % int(c["idx"]))
                    for c in chunks
                    if int(start_chunk["idx"]) <= int(c["idx"])
                    <= int(end_chunk["idx"])]
            else:
                # Backward compatibility for an older local model prompt.
                # Seconds are accepted only when they stay inside the shown
                # source timeline; new prompts require B ids.
                try:
                    a, b = float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                lower = min(float(c["start"]) for c in chunks)
                upper = max(float(c["end"]) for c in chunks)
                a, b = max(lower, min(a, upper)), max(lower, min(b, upper))
                if b < a:
                    a, b = b, a
                source_chunks = []
            sections.append({"title": parts[3], "start": a, "end": b,
                             "advice": parts[4],
                             "source_chunks": source_chunks})
    on_log("  剧本：%d 个章节，主线：%s" % (len(sections), mainline[:30]))
    return sections, mainline


def build(index_path, on_log=print, should_stop=None, level="medium",
          mode="gameplay", reuse_chunks=True, keep_ratio=None):
    import cut_strength
    from llm_gateway import APIRequestError, backend_signature
    backend = backend_signature()
    keep_ratio = cut_strength.normalize_ratio(
        keep_ratio, fallback=cut_strength.LEGACY_KEEP_RATIO.get(level))
    level = cut_strength.level_for_ratio(keep_ratio)
    level = level if level in LEVEL_SCRIPT_RULES else "medium"
    mode = str(mode or "gameplay")
    with open(index_path, encoding="utf-8") as handle:
        data = json.load(handle)
    segs = data.get("segments") or []
    if not segs:
        on_log("没有台词数据，先重新分析素材")
        return 0
    variant_key = "%s:%s:%d" % (
        mode, level, int(round(keep_ratio * 100.0)))
    variants = data.get("script_variants") or {}
    cached = variants.get(variant_key)
    current_events = _source_events(data)
    current_timeline_revision = source_timeline.revision(current_events)
    if (cached and len(cached.get("sections") or []) > 0
            and int(cached.get("sentences") or 0) == len(segs)
            and cached.get("timeline_schema") == source_timeline.SCHEMA
            and cached.get("backend_signature", "local") == backend
            and cached.get("timeline_revision") == current_timeline_revision):
        data["script"] = cached
        data["script_variants"] = variants
        with open(index_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        on_log("剧本：复用%s力度缓存（%d 章）" % (level, len(cached["sections"])))
        return len(cached["sections"])

    old_script = data.get("script") or {}
    chunks = (old_script.get("chunks") if (
        reuse_chunks
        and old_script.get("backend_signature", "local") == backend
        and old_script.get("timeline_schema") == source_timeline.SCHEMA
        and old_script.get("timeline_revision") == current_timeline_revision)
        else None)
    if chunks:
        on_log("第一遍：复用已有通读摘要（%d 段），按%s力度重排剧本…"
               % (len(chunks), level))
    else:
        on_log("第一遍：分段读片（%d 句）…" % len(segs))
        chunks = read_pass(data, on_log=on_log, should_stop=should_stop)
    if not chunks:
        if backend.startswith("api:"):
            raise APIRequestError("AI API 编剧未完成通读，已停止剪辑")
        on_log("读片失败")
        return 0
    on_log("第二遍：按%s力度整理成剧本…" % level)
    sections, mainline = plan_pass(chunks, on_log=on_log,
                                   should_stop=should_stop, level=level,
                                   keep_ratio=keep_ratio)
    _check_cancel(should_stop)
    # 兜底：章数太多或主线缺失，说明模型没做归并，按重要度自己合
    if not sections or len(sections) > 10 or not mainline:
        if backend.startswith("api:"):
            raise APIRequestError("AI API 编剧未返回有效章节和主线，已停止剪辑")
        on_log("  模型没归并（%d 章），改用重要度合并" % len(sections))
        sections, mainline = fallback_sections(chunks, level=level)
        on_log("  合并后：%d 章，主线：%s" % (len(sections), mainline[:28]))
    events = current_events
    script = {"mainline": mainline, "sections": sections,
              "chunks": chunks, "sentences": len(segs),
              "cut_level": level, "keep_ratio": keep_ratio, "mode": mode,
              "strategy": LEVEL_SCRIPT_RULES[level],
              "backend_signature": backend,
              "timeline_schema": source_timeline.SCHEMA,
              "timeline_revision": current_timeline_revision,
              "timeline_event_count": len(events),
              "decision_space": "source_timeline_event_ids"}
    variants[variant_key] = script
    data["script"] = script
    data["script_variants"] = variants
    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)

    # 同时写一份人能读的 Markdown，方便直接打开核对（以后目录可自定义）
    try:
        md = os.path.join(os.path.dirname(index_path), "剧本.md")
        lines = ["# 剧本", "", "**主线**：%s" % (mainline or "（没写出来）"), ""]
        lines.append("共 %d 章，%d 句台词" % (len(sections), len(segs)))
        lines.append("")
        for k, sec in enumerate(sections, 1):
            lines.append("## 第 %d 章  %s" % (k, sec["title"]))
            lines.append("")
            lines.append("- 时间：%s ~ %s" % (_ts(sec["start"]), _ts(sec["end"])))
            lines.append("- 取舍：%s" % sec["advice"])
            lines.append("")
            # 这一章里说了什么，取几句代表性台词
            inside = [sg for sg in segs
                      if sec["start"] <= float(sg[0]) <= sec["end"]][:5]
            for sg in inside:
                lines.append("  - %s" % str(sg[2])[:60])
            lines.append("")
        with open(md, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        on_log("剧本文件：%s" % md)
    except Exception as exc:  # noqa: BLE001
        on_log("剧本文件没写成：%s" % str(exc)[:60])

    on_log("剧本已写入：%d 章" % len(sections))
    for s in sections[:8]:
        on_log("   %6.0f~%6.0f  %s  （%s）"
               % (s["start"], s["end"], s["title"], s["advice"]))
    return len(sections)


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else ""
    if os.path.isdir(p):
        p = os.path.join(p, "index.json")
    if not p or not os.path.isfile(p):
        print("用法：python script_writer.py <索引目录或 index.json>")
        sys.exit(1)
    build(p)

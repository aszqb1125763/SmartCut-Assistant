# -*- coding: utf-8 -*-
"""自主剪辑入口：统一进入正式岗位工作流。

子枫 2026-11-24：
    「把所有流程打乱重新安排，让他自己拥有相关功能，让他自己去剪辑一个试试看。」

默认流程：中立分析 → 编剧 → 时间轴模型剪辑师 → 边界精修 →
三模态导演/自动返工 → 转场 → 渲染 → 成片门禁 → 记忆。

旧的“模型自由输出秒数、没有导演层”流程只保留给开发回退，必须显式传
``--legacy-experimental``，正式界面不会再走它。

用法：
    python app/autoedit.py "视频.mp4" --track 0 [--target 60]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import numpy as np

APP = os.path.dirname(os.path.abspath(__file__))
BASE = APP
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "core"))

SR = 16000


# ---------------------------------------------------------------- 基础工具
def _safe_stdout():
    """打包版（--windowed）里 sys.stdout / sys.stderr 是 None，print 会直接抛错。

    开发环境有控制台，这里什么都不做。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _redirect_log(path):
    """把日志写进文件：界面（客户端窗口）按行读它显示进度。

    用 buffering=1（行缓冲）保证界面能及时看到，不会等进程退出才刷出来。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    handle = open(path, "w", encoding="utf-8", buffering=1)
    sys.stdout = handle
    sys.stderr = handle
    return handle


def ffmpeg():
    return os.path.join(BASE, "bin", "ffmpeg.exe")


def ffprobe():
    return os.path.join(BASE, "bin", "ffprobe.exe")


def duration_of(path):
    done = subprocess.run([ffprobe(), "-v", "error", "-show_entries",
                           "format=duration", "-of", "default=nw=1:nk=1",
                           path], capture_output=True, text=True)
    try:
        return float((done.stdout or "0").strip())
    except ValueError:
        return 0.0


def decode(path, track=0):
    done = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error",
                           "-i", path, "-map", "0:a:%d" % track,
                           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                          capture_output=True)
    return np.frombuffer(done.stdout, dtype="<i2").astype(np.float32) / 32768.0


def silence_points(audio, min_gap=1.2, floor_db=-45.0):
    """长静音中点 = 天然的话题断点。"""
    frame = int(0.02 * SR)
    count = audio.size // frame
    if count < 2:
        return []
    frames = audio[:count * frame].reshape(count, frame)
    env = 20 * np.log10(np.maximum(
        np.sqrt((frames ** 2).mean(axis=1)), 1e-9))
    quiet = env <= floor_db
    points = []
    start = None
    for index, flag in enumerate(quiet):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            length = (index - start) * 0.02
            if length >= min_gap:
                points.append((start + index) / 2 * 0.02)
            start = None
    return points


def scene_points(path, step=1.0, threshold=0.45):
    """画面直方图突变 = 场景切换。"""
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return []
    tmp = os.path.join(BASE, "data", "_scene")
    os.makedirs(tmp, exist_ok=True)
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", path, "-vf", "fps=1,scale=320:-2",
                    "-q:v", "4", os.path.join(tmp, "f_%05d.jpg")],
                   capture_output=True)
    names = sorted(os.listdir(tmp))
    points = []
    prev = None
    for index, name in enumerate(names):
        img = cv2.imdecode(np.fromfile(os.path.join(tmp, name),
                                       dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        hist = cv2.calcHist([img], [0, 1, 2], None, [8, 8, 8],
                            [0, 256, 0, 256, 0, 256])
        hist = cv2.normalize(hist, hist).flatten()
        if prev is not None:
            corr = float(cv2.compareHist(prev, hist, cv2.HISTCMP_CORREL))
            if corr < threshold:
                points.append(index * step)
        prev = hist
    for name in names:
        try:
            os.remove(os.path.join(tmp, name))
        except OSError:
            pass
    return points


# ---------------------------------------------------------------- 章节
def chapters_from(points, total, min_len=8.0):
    """把候选点整理成章节边界（相邻太近的合并）。"""
    edges = [0.0]
    for point in sorted(points):
        if point - edges[-1] >= min_len and total - point >= min_len:
            edges.append(round(point, 2))
    edges.append(round(total, 2))
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


# ---------------------------------------------------------------- 模型
SYS_CHAPTER = """你是剪辑助手。给你一段口播的转写（可能有不认识的字，用候选表示），
写一句话摘要：这段在讲什么。不超过 25 字，不要评价，不要加"这段"。"""

SYS_TIME = """你在剪视频。

下面是一条**时间轴**：每格 0.5 秒，数字 0~9 表示这一格有多响
（0 = 安静，9 = 很响）。

你看不到内容——不知道说了什么、讲的什么事。
你只知道哪里安静、哪里响、哪里断断续续。

任务：把这条视频精简到目标时长。
- 长时间安静的地方，多半是废话之间的空场，删掉；
- 响得密集的地方是正片，留着；
- 忽响忽停、断断续续的地方，往往是在犹豫或者重录，可以删。

输出 JSON：
{"delete": [[起,止],...], "reason": "一句话说清你删了什么、为什么"}"""

SYS_PLAN = """你在剪视频。

你的工作是：找出视频中能够精简和完善的故事线，
把重复的、影响故事线的删除，
删除位置的声音要顺耳。

**三条要一起看：**
【一·重复就是重录】行尾标【建议删】的是没说完的半句 → 删它，留后一句完整版。
【二·删话不删节奏】删的是多余的话，不是停顿；连续讲同一件事的整段保留。
【三·切口要顺】切口落在有停顿的地方；标"会突"的别切，宁可多留半句。

下面是一份逐句转写（带时间和每句后面的停顿）。
只告诉我你要删哪些，保留的我自己算。

输出 JSON：
{"delete": [[起,止],...], "reason": "一句话说清你删了什么、为什么"}"""


def draw_timeline(items, total, width=64):
    """把时间轴画出来：横轴是秒，每段的长度按比例。

    items: [(起, 止, 编号)]；返回可读的字符画。
    """
    if not total or not items:
        return ""
    scale = width / float(total)
    ticks = [" "] * width
    for second in range(0, int(total) + 1, 10):
        pos = int(second * scale)
        label = str(second)
        for offset, char in enumerate(label):
            if pos + offset < width:
                ticks[pos + offset] = char
    bar = [" "] * width
    for a, b, index in items:
        start = max(0, int(a * scale))
        end = min(width, max(start + 1, int(b * scale)))
        for pos in range(start, end):
            bar[pos] = str(index % 10)
    rule = ["-"] * width
    return "%s\n%s\n%s" % ("".join(ticks), "".join(rule), "".join(bar))


def ask(llm, system, user, max_tokens=300):
    return llm.chat(system, user, max_tokens=max_tokens) or ""


def parse_json(text):
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    body = match.group(0)
    # 模型爱在 JSON 里写 // 注释、末尾多余逗号，先清掉
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r",\s*([}\]])", r"\1", body)
    try:
        return json.loads(body)
    except ValueError:
        return None


# ---------------------------------------------------------------- 主流程
def run_time_only(path, track, target, total):
    """纯时间感知剪辑：只给能量轴时间轴，不给任何文字。"""
    import time_sense
    import brain
    from ui.llm_bridge import LLM

    print("\n[1/4] 读能量轴…")
    calc = brain.Calculator(path, track=track)
    try:
        audio = calc.audio
    finally:
        calc.close()
    ts = time_sense.analyse(audio)
    print("    %s" % ts["note"])

    print("\n[2/4] 把时间轴交给模型（不含任何文字）…")
    body = ("时间轴（每格 %.1f 秒，数字 0~9 = 这一格有多响，0=安静）：\n%s\n%s\n\n%s"
            % (1 / ts["fps"], ts["ruler"], ts["art"], ts["note"]))
    aim = ("目标 %.0f 秒。" % target) if target else "尽量紧凑。"
    print(body)
    llm = LLM(base_dir=BASE)
    llm.start()
    try:
        plan_text = ask(llm, SYS_TIME,
                        "%s\n\n总长 %.1f 秒，%s" % (body, total, aim),
                        max_tokens=800)
        print("\n   模型原始输出：%s" % plan_text.strip()[:300])
        plan = parse_json(plan_text)
        if not plan or not plan.get("delete"):
            print("   ❌ 没给出删除区间")
            return 1

        print("\n[3/4] 执行…")
        drops = sorted(tuple(map(float, p)) for p in plan["delete"])
        keep, cursor = [], 0.0
        for a, b in drops:
            if a > cursor + 0.05:
                keep.append((round(cursor, 2), round(a, 2)))
            cursor = max(cursor, b)
        if cursor < total - 0.05:
            keep.append((round(cursor, 2), round(total, 2)))
        if not keep:
            print("   ❌ 删光了")
            return 1

        # 无损失精简：相邻保留段连起来，停顿保留
        merged = []
        for a, b in keep:
            if merged and a - merged[-1][1] <= 1.5:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        keep = merged
        kept = sum(b - a for a, b in keep)
        print("   保留 %d 段，%.1f 秒（原 %.1f 秒，压掉 %.0f%%）"
              % (len(keep), kept, total, (1 - kept / total) * 100))

        out = os.path.join(BASE, "data", "timeonly_%s.mp4"
                           % time.strftime("%H%M%S"))
        cmd = [ffmpeg(), "-hide_banner", "-y", "-i", path,
               "-filter_complex", build_filter(keep),
               "-map", "[outv]", "-map", "[outa]",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out]
        done = subprocess.run(cmd, capture_output=True)
        if done.returncode != 0:
            print("   ❌ ffmpeg 失败：%s" % done.stderr[-300:])
            return 1
        print("   ✅ 出片：%s（%.1f 秒）"
              % (os.path.basename(out), duration_of(out)))

        print("\n[4/4] 回执…")
        report = ("原片 %.1f 秒，剪成 %.1f 秒。\n你的理由：%s\n"
                  % (total, kept, plan.get("reason") or ""))
        print("   " + ask(llm, "一句话评价这次剪辑。", report,
                          max_tokens=120).replace("\n", " "))
        return 0
    finally:
        llm.stop()


def _sentences(words, gap=0.45, max_chars=32):
    """词级转写 → 句级字幕段（和文字小脑同一套停顿口径）。"""
    groups, current = [], []
    for word in words:
        if current:
            pause = float(word["start"]) - float(current[-1]["end"])
            length = sum(len(str(w.get("word") or "")) for w in current)
            if pause >= gap or length >= max_chars:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    rows = []
    for group in groups:
        text = "".join(str(w.get("word") or "") for w in group).strip()
        if not text:
            continue
        rows.append((float(group[0]["start"]), float(group[-1]["end"]), text))
    return rows


def _polish_subs(llm, segs, batch=40):
    """让模型逐句修正转写错字，再拿它的文字做字幕。

    子枫 2026-11-24：
        「新模型的文字输出能力更强了，用这个文字输出做字幕。」

    转写负责时间轴和原始文字；模型只做保守纠错，不再自由重写：
    给它「编号|转写文本」，要求保持行数、只回「编号|修正文本」。
    对不上号、疑似改写或低置信的结果一律退回原文，时间戳始终不变。
    """
    if not segs:
        return segs
    fixed = {}
    changed = 0
    # 复用正式字幕校对的防改写规则：邻句只供理解，真正写回仍只接受
    # 已知术语误写或单字级修正。时间戳从始至终不交给模型修改。
    try:
        import proofread as _proof
        _table, _wrong_set, _kb_all = _proof.load_resources()
        known_pairs = _proof.known_pairs_from_table(_table)
    except Exception:  # noqa: BLE001
        _proof, known_pairs = None, []
    for start in range(0, len(segs), batch):
        part = segs[start:start + batch]
        lines = ["%d|%s" % (start + i + 1, text)
                 for i, (_a, _b, text) in enumerate(part)]
        before = [text for _a, _b, text in segs[max(0, start - 2):start]]
        after_at = start + len(part)
        after = [text for _a, _b, text in segs[after_at:after_at + 2]]
        context = ""
        if before:
            context += "只读上文（不要输出）：\n" + "\n".join(before) + "\n"
        if after:
            context += "只读下文（不要输出）：\n" + "\n".join(after) + "\n"
        prompt = (
            "下面是从视频里转写的台词，可能有错别字（尤其是游戏术语、人名、"
            "物品名）。请逐行修正错别字，保持意思不变。\n"
            "要求：\n"
            "1. 行数、顺序都不变，不合并、不拆句、不删减；\n"
            "2. 只输出「编号|修正后的文字」，一行一条；\n"
            "3. 不要任何解释、不要 Markdown。\n\n"
            + context + "待校对（只输出这些编号）：\n" + "\n".join(lines))
        answer = ask(llm, "你是字幕校对员，只按格式输出。", prompt,
                     max_tokens=min(2000, 80 + len(part) * 40))
        for line in (answer or "").splitlines():
            match = re.match(r"^\s*(\d+)\s*[|｜]\s*(.+?)\s*$", line)
            if not match:
                continue
            index = int(match.group(1))
            if 1 <= index <= len(segs):
                fixed[index] = match.group(2)
    out = []
    for index, (a, b, text) in enumerate(segs, 1):
        new = fixed.get(index, "").strip()
        if new and new != text and _proof is not None:
            if _proof.accept_correction(text, new, known_pairs):
                changed += 1
                text = new
        out.append((a, b, text))
    print("    字幕文字：模型校对 %d 句，改动 %d 句" % (len(segs), changed))
    return out


def _split_for_style(segs, limit):
    """按字幕样式的一行字数把长句拆成多条，时间按字数均分，绝不丢字。"""
    if limit <= 0:
        return segs
    out = []
    for a, b, text in segs:
        text = text.strip()
        if len(text) <= limit:
            out.append((a, b, text))
            continue
        parts = [text[i:i + limit] for i in range(0, len(text), limit)]
        span = max(1e-3, float(b) - float(a))
        step = span / len(parts)
        for index, part in enumerate(parts):
            out.append((float(a) + index * step,
                        float(a) + (index + 1) * step, part))
    return out


def _burn_subtitles(video, keep, segs, stamp):
    """把字幕烧进成片：原片时间轴先按 keep 换成成片时间轴。"""
    import subtitles
    style = subtitles.load_style()
    limit = max(4, int(style.get("max_chars", 18))
                * max(1, int(style.get("max_lines", 2))))
    segs = _split_for_style(segs, limit)
    timeline, offset = [], 0.0
    for a, b in keep:
        duration = float(b) - float(a)
        timeline.append((offset, float(a), duration))
        offset += duration
    mapped = subtitles.remap(segs, timeline)
    if not mapped:
        print("    字幕：没有可烧的句子，跳过")
        return video
    ass = os.path.join(BASE, "data", "subs_%s.ass" % stamp)
    subtitles.write_ass({}, ass, segments=mapped)
    tmp = os.path.join(BASE, "data", "_sub_%s.mp4" % stamp)
    subtitles.burn(video, ass, tmp)
    os.replace(tmp, video)
    print("    字幕：烧了 %d 句（样式 %d 字/行）" % (len(mapped), limit))
    return video


def _add_bgm(video, loud):
    """配 BGM（曲库没配就跳过，不报错）。"""
    import bgm
    conf = bgm.load_conf()
    conf["enabled"] = "1"
    conf["loudness_norm"] = "1" if loud else "0"
    info = bgm.attach(video, video, conf, log=lambda m: print("    " + str(m)))
    return bool(info)


def build_filter(keep):
    filters, parts = [], []
    for index, (a, b) in enumerate(keep):
        filters.append("[0:v]trim=start=%.2f:end=%.2f,setpts=PTS-STARTPTS[v%d]"
                       % (a, b, index))
        filters.append("[0:a]atrim=start=%.2f:end=%.2f,asetpts=PTS-STARTPTS[a%d]"
                       % (a, b, index))
        parts.append("[v%d][a%d]" % (index, index))
    filters.append("%sconcat=n=%d:v=1:a=1[outv][outa]"
                   % ("".join(parts), len(keep)))
    return ";".join(filters)


def run_role_workflow(path, track=0, target=None, burn_subs=False,
                      use_bgm=False, loud=True, out_dir=None):
    """Compatibility adapter from the old dialog to the safe full workflow."""
    import autopilot

    total = duration_of(path)
    if target and total > 0:
        import cut_strength
        ratio = cut_strength.normalize_ratio(float(target) / total)
        level = cut_strength.level_for_ratio(ratio)
        print("目标 %.0f 秒 → 保留 %.0f%% 有效内容；剧情完整性和听觉边界优先"
              % (float(target), ratio * 100.0))
    else:
        ratio = None
        level = "medium"
    stage_map = {"①": 1, "②": 2, "③": 3, "④": 4,
                 "⑤": 5, "⑥": 5, "⑦": 5, "⑧": 6}
    shown = set()

    def log(message):
        text = str(message)
        marker = text.lstrip()[:1]
        stage = stage_map.get(marker)
        if stage and stage not in shown:
            shown.add(stage)
            print("[%d/6] %s" % (stage, text))
        else:
            print(text)

    print("[1/6] 进入正式时间轴工作流…")
    try:
        result = autopilot.run_one(
            path, level=level, mode="gameplay", use_bgm=bool(use_bgm),
            tracks=[int(track)], output_dir=out_dir,
            burn_subs=bool(burn_subs), loudness_norm=bool(loud),
            on_log=log, model_editor=True, keep_ratio=ratio)
    except Exception as exc:  # noqa: BLE001
        print("正式工作流失败：%s" % exc)
        return 1
    record = {
        "source": os.path.abspath(path), "out": result.get("out"),
        "total": total, "kept": round(float(result.get("minutes") or 0.0)
                                         * 60.0, 3),
        "burn_subs": bool(burn_subs), "bgm": bool(use_bgm),
        "loud": bool(loud), "workflow": "full_auto_roles",
        "model_editor": True, "director_passed": result.get("director_passed"),
        "rendered_review_passed": result.get("rendered_review_passed"),
        "task_id": result.get("task_id"), "result": result,
    }
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(os.path.join(BASE, "data", "autoedit_last.json"),
              "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
    print("[6/6] 完成：%s" % (record.get("out") or ""))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _safe_stdout()
    if "--log" in argv:
        _redirect_log(argv[argv.index("--log") + 1])
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    track = 0
    target = None
    if "--track" in argv:
        track = int(argv[argv.index("--track") + 1])
    if "--target" in argv:
        target = float(argv[argv.index("--target") + 1])
    burn_subs = "--burn-subs" in argv
    use_bgm = "--bgm" in argv
    loud = "--loud" in argv
    out_dir = os.path.join(BASE, "data")
    if "--outdir" in argv:
        out_dir = argv[argv.index("--outdir") + 1]
    if not os.path.isfile(path):
        print("找不到：%s" % path)
        return 2
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        print("输出目录建不了（%s），改回 data\\" % exc)
        out_dir = os.path.join(BASE, "data")

    if "--legacy-experimental" not in argv:
        return run_role_workflow(
            path, track=track, target=target, burn_subs=burn_subs,
            use_bgm=use_bgm, loud=loud, out_dir=out_dir)

    import asr
    import neutral_text
    from ui.llm_bridge import LLM

    total = duration_of(path)
    if "--time-only" in argv:
        return run_time_only(path, track, target, total)
    print("视频：%s（%.1f 秒）" % (os.path.basename(path), total))

    # ① 听
    print("\n[1/6] 转写中…")
    start = time.time()
    rows = asr.transcribe(path, name="large-v3-turbo", beam_size=5,
                          with_probability=True)
    words = []
    for row in rows:
        words.append({"start": float(row["start"]), "end": float(row["end"]),
                      "word": str(row["word"]), "prob": float(row["prob"])})
    text, concerns = neutral_text.render(words)
    print("    %d 个词，%d 处没把握，%.1f 秒"
          % (len(words), len(concerns), time.time() - start))

    # ② 看
    print("\n[2/6] 找章节边界…")
    audio = decode(path, track)
    silences = silence_points(audio)
    scenes = scene_points(path)
    points = sorted(silences + scenes)
    chapters = chapters_from(points, total, min_len=4.0)
    chapters = [c for c in chapters if c[1] - c[0] >= 3.0]
    print("    静音断点 %d 个，画面切换 %d 个 → %d 章"
          % (len(silences), len(scenes), len(chapters)))

    # ③ 剧本
    print("\n[3/6] 校对文字 + 写剧本（每章一句）…")
    llm = LLM(base_dir=BASE)
    llm.start()
    script = []
    subs_segs = []
    try:
        # 转写只负责"什么时候说了话"，文字交给模型重写（模型输出更准）。
        # 这一步同时供后面的剪辑单和字幕使用，不会再校对第二遍。
        polished = _polish_subs(llm, _sentences(words))
        if polished:
            subs_segs = polished
            words = [{"start": a, "end": b, "word": text, "prob": 1.0}
                     for a, b, text in polished]
            print("    %d 句（时间轴不变，文字按模型输出）" % len(words))
        for start_t, end_t in chapters:
            chunk = "".join(str(w["word"]) for w in words
                            if start_t <= w["start"] < end_t)
            if len(chunk) > 400:
                chunk = chunk[:400]
            if len(chunk) < 8:
                summary = "（无词，应删）"
            else:
                summary = ask(llm, SYS_CHAPTER,
                              "转写：\n%s" % chunk, max_tokens=80)
            script.append({"t0": round(start_t, 1), "t1": round(end_t, 1),
                           "chars": len(chunk),
                           "summary": summary.strip().replace("\n", " ")})
            print("    %.1f~%.1fs（%d 字）：%s"
                  % (start_t, end_t, len(chunk), script[-1]["summary"]))

        # ④ 剪辑单
        print("\n[4/6] 出剪辑单…")
        plain = "".join(str(w["word"]) for w in words)
        if len(plain) <= 900:
            # 短素材：直接把带时间戳的全文给它，让它自己找废段
            lines = []
            gaps = []
            cursor = ""
            begin = None
            for word in words:
                if begin is None:
                    begin = word["start"]
                cursor += str(word["word"])
                stripped = cursor.rstrip()
                if stripped.endswith(("。", "?", "？", "!", "！")) \
                        or len(cursor) >= 24:
                    lines.append("%.1f~%.1fs %s"
                                 % (begin, word["end"], cursor))
                    gaps.append(round(float(word["end"]), 2))
                    cursor, begin = "", None
            if cursor:
                lines.append("%.1fs~ %s" % (begin or 0, cursor))
                gaps.append(round(total, 2))
            # 句后停顿（气口）：切点落在这里才顺
            starts = []
            for line in lines:
                try:
                    starts.append(float(line.split("~", 1)[0]))
                except ValueError:
                    starts.append(0.0)
            # 停顿写在句尾（不占独立行，否则行数翻倍，8B 会乱）
            for index in range(len(lines)):
                if index + 1 < len(starts):
                    pause = starts[index + 1] - gaps[index]
                else:
                    pause = total - gaps[index]
                if pause >= 0.30:
                    lines[index] += "  ← 句后 %.2fs 停顿（保留，不是要删的）" % pause
                elif pause >= 0.12:
                    lines[index] += "  ← 句后 %.2fs 停顿（偏短）" % pause
                else:
                    lines[index] += "  ← 句后无停顿（切这里会突）"

            # 相邻句相似度：代码定位"疑似重录对"，并直接指出建议删哪句
            import difflib
            marked = []
            for index, line in enumerate(lines):
                text = line.split(" ", 1)[-1]
                flag = ""
                if index:
                    prev = lines[index - 1].split(" ", 1)[-1]
                    ratio = difflib.SequenceMatcher(None, prev, text).ratio()
                    if ratio >= 0.45:
                        # 上一行是没说完的重录前半 → 直接标"建议删"
                        marked[-1] = (marked[-1]
                                      + "  ← 【建议删：这句没说完，下一句才是完整的】")
                        flag = ("  ← 与上句相似 %.0f%%（这句是重录的完整版，保留）"
                                % (ratio * 100))
                marked.append(line + flag)
            lines = marked

            # 句内重复：给出重复那一段的精确时间，让它删词不删句
            fine = []
            for line in lines:
                fine.append(line)
                try:
                    span = line.split("s ", 1)[0]
                    a_txt, b_txt = span.split("~")
                    a_edge, b_edge = float(a_txt), float(b_txt)
                except ValueError:
                    continue
                inside = [w for w in words if a_edge <= w["start"] < b_edge]
                if len(inside) < 4:
                    continue
                found = None
                for i in range(len(inside)):
                    for j in range(i + 2, min(i + 6, len(inside)) + 1):
                        seg = "".join(str(w["word"]) for w in inside[i:j])
                        head = "".join(str(w["word"]) for w in inside[:i])
                        if len(seg) >= 2 and seg in head:
                            found = (inside[i]["start"], inside[j - 1]["end"], seg)
                            break
                    if found:
                        break
                if found:
                    fine.append(
                        "      ↳ 句内重复：%.1f~%.1fs「%s」← 只删这一小段，"
                        "整句其余部分保留" % found)
            lines = fine
            # 无词区间：列表里只会有"有词的句子"，20 秒空场它根本看不到 → 补上
            holes = []
            head = words[0]["start"] if words else 0.0
            if head >= 1.0:
                holes.append((0.0, head))
            for prev, nxt in zip(words, words[1:]):
                gap = nxt["start"] - prev["end"]
                if gap >= 1.0:
                    holes.append((prev["end"], nxt["start"]))
            tail = total - (words[-1]["end"] if words else total)
            if tail >= 1.0:
                holes.append((words[-1]["end"] if words else 0.0, total))
            for a, b in holes:
                lines.insert(0, "%.1f~%.1fs（无词空场 %.1f 秒 ← 这里没人说话，"
                                "留着等于放空气）" % (a, b, b - a))
            if holes:
                print("    补上 %d 段无词空场" % len(holes))
            script = [{"t0": 0, "t1": total, "chars": len(plain),
                       "summary": "（全文直给）"}]
            print("    短文模式：全文 %d 字" % len(plain))
        if len(plain) <= 900:
            body = "逐句转写（带时间和停顿）：\n%s" % "\n".join(lines)
        else:
            rows_txt = ["%.1f~%.1fs ｜ %d 字 ｜ %s"
                        % (c["t0"], c["t1"], c["chars"], c["summary"])
                        for c in script]
            body = "分章剧本：\n%s" % "\n".join(rows_txt)
        print("    逐句剧本：%d 行" % len(lines))
        goal = ("目标时长：约 %.0f 秒（要删掉约 %.1f 秒）"
                % (target, max(0.0, total - target))) if target \
            else "目标：尽量紧凑"
        # 先让它说出"这片在讲什么"——没有故事线概念，它就会把内容当废话删
        theme = ask(llm, "一句话说清这段视频在讲什么，不超过 25 字。",
                    body, max_tokens=60).strip().replace("\n", " ")
        print("    主题：%s" % theme)

        # 小脑帮它算：每段多长、要删几段、删完剩多少，全由代码算好贴上去
        numbered, clock = [], 0.0
        seg_lines = []
        for line in body.split("\n"):
            stripped = line.lstrip()
            if stripped[:1].isdigit() and "~" in stripped and "s" in stripped:
                try:
                    span = stripped.split("s ", 1)[0]
                    a_txt, b_txt = span.split("~")
                    a_edge, b_edge = float(a_txt), float(b_txt)
                    seg_lines.append((a_edge, b_edge, line))
                except ValueError:
                    numbered.append(line)
            else:
                numbered.append(line)
        # 段落计时
        timed = {}
        for a_edge, b_edge, line in seg_lines:
            timed[line] = b_edge - a_edge
        total_move = sum(timed.values())
        avg = total_move / len(timed) if timed else 0.0
        if target:
            need = max(0.0, total - target)
            rough = int(round(need / avg)) if avg else 0
            ledger = (
                "测量数据（小脑算的，你直接用，不用自己算）：\n"
                "· 全片 %.1f 秒；有台词部分 %.1f 秒，分 %d 段，平均每段 %.1f 秒。\n"
                "· 目标 %.0f 秒；与当前总长相差 %.1f 秒。\n"
                "· 每行前面的（X.Xs）是这一段的时长。\n"
                % (total, total_move, len(timed), avg, target, need))
        else:
            ledger = ("小脑已经替你算好：全片 %.1f 秒，有台词部分 %.1f 秒，"
                      "分 %d 段，平均每段 %.1f 秒。目标是尽量紧凑。\n"
                      % (total, total_move, len(timed), avg))
        # 每行标上时长
        for a_edge, b_edge, line in seg_lines:
            label = "（%.1fs）" % (b_edge - a_edge)
            numbered.append("  " + line.replace("s ", "s " + label, 1))
        # 画一条时间轴：横轴是秒，每段的长度按比例，数字是段号
        art = draw_timeline(
            [(a_edge, b_edge, index + 1)
             for index, (a_edge, b_edge, _line) in enumerate(seg_lines)],
            total)
        if art:
            art = ("这是这条视频的**时间轴**（横轴是秒，从 0 到 %.1f；"
                   "格子里是段号，一格大约 %.1f 秒）：\n%s\n"
                   % (total, total / 64.0, art))
        plan_text = ask(llm, SYS_PLAN,
                        "这支视频在讲：%s\n\n%s\n\n%s\n%s"
                        % (theme, body, ledger, goal),
                        max_tokens=1500)
        print("    模型原始输出：%s" % plan_text.strip()[:300])
        plan = parse_json(plan_text)
        if not plan:
            print("    ❌ 不是合法 JSON，放弃")
            return 1

        # 对账只做"记录"，不触发重试——实测它越改越错（第一次往往最准）
        if target:
            drops = sorted(tuple(map(float, p))
                           for p in plan.get("delete") or [])
            kept_now, cursor_x = 0.0, 0.0
            for a, b in drops:
                if a > cursor_x:
                    kept_now += a - cursor_x
                cursor_x = max(cursor_x, b)
            kept_now += max(0.0, total - cursor_x)
            print("    对账：删完剩 %.1f 秒，目标 %.1f 秒（差 %+.1f）"
                  % (kept_now, target, kept_now - target))

        # ⑤ 执行
        print("\n[5/6] 校验并执行…")
        # 只信 delete：保留 = 全集 - 删除（它会同时填 keep/delete，容易自相矛盾）
        if plan.get("delete") and not plan.get("keep"):
            drops = sorted(tuple(map(float, p)) for p in plan["delete"])
            keep, cursor2 = [], 0.0
            for a, b in drops:
                if a > cursor2 + 0.05:
                    keep.append((round(cursor2, 2), round(a, 2)))
                cursor2 = max(cursor2, b)
            if cursor2 < total - 0.05:
                keep.append((round(cursor2, 2), round(total, 2)))
        else:
            keep = sorted(tuple(map(float, p))
                          for p in plan.get("keep") or [])
        drop = sorted(tuple(map(float, p)) for p in plan.get("delete") or [])
        if not keep:
            print("    ❌ keep 为空")
            return 1
        # 同一段既说保留又说删除 = 它没想清楚
        clash = [k for k in keep
                 for d in drop
                 if abs(k[0] - d[0]) < 0.2 and abs(k[1] - d[1]) < 0.2]
        if clash:
            print("    ⚠️ keep/delete 冲突 %d 段，按 keep 处理" % len(clash))
            drop = [d for d in drop if d not in clash]
        kept = sum(b - a for a, b in keep)
        print("    保留 %d 段，共 %.1f 秒（原 %.1f 秒，压掉 %.0f%%）"
              % (len(keep), kept, total, (1 - kept / total) * 100))

        # 无损失精简：保留的句子之间如果有停顿，连起来（停顿是节奏，不是废话）
        merged = []
        for a, b in keep:
            if merged and a - merged[-1][1] <= 1.5:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        if len(merged) < len(keep):
            print("    合并相邻段：%d → %d 段（句间停顿保留）"
                  % (len(keep), len(merged)))
        keep = merged

        # 三轴补丁：切点吸附到最近的静音中点（气口），避免切在半句话上
        silences = silence_points(audio, min_gap=0.25, floor_db=-50.0)
        snapped = []
        for a, b in keep:
            for edge, is_start in ((a, True), (b, False)):
                near = [s for s in silences if abs(s - edge) <= 0.6]
                value = (min(near, key=lambda s: abs(s - edge))
                         if near else edge)
                if is_start:
                    a = max(0.0, value - 0.05)
                else:
                    b = min(total, value + 0.05)
            if b - a >= 0.4:
                snapped.append((round(a, 2), round(b, 2)))
        if snapped:
            moved = sum(1 for old, new in zip(keep, snapped)
                        if old != new)
            print("    切点吸附：%d/%d 段边界对齐到气口" % (moved, len(snapped)))
            keep = snapped
            kept = sum(b - a for a, b in keep)

        stamp = time.strftime("%H%M%S")
        out = os.path.join(out_dir, "autoedit_%s.mp4" % stamp)
        filters, parts = [], []
        for index, (a, b) in enumerate(keep):
            filters.append("[0:v]trim=start=%.2f:end=%.2f,setpts=PTS-STARTPTS[v%d]"
                           % (a, b, index))
            filters.append("[0:a]atrim=start=%.2f:end=%.2f,asetpts=PTS-STARTPTS[a%d]"
                           % (a, b, index))
            parts.append("[v%d][a%d]" % (index, index))
        filters.append("%sconcat=n=%d:v=1:a=1[outv][outa]"
                       % ("".join(parts), len(keep)))
        cmd = [ffmpeg(), "-hide_banner", "-y", "-i", path,
               "-filter_complex", ";".join(filters),
               "-map", "[outv]", "-map", "[outa]", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", out]
        start = time.time()
        done = subprocess.run(cmd, capture_output=True)
        if done.returncode != 0:
            print("    ❌ ffmpeg 失败：%s" % done.stderr[-300:])
            return 1
        print("    ✅ 出片：%s（%.1f 秒，用时 %.1f 秒）"
              % (os.path.basename(out), duration_of(out), time.time() - start))

        # ⑤.5 成片后处理：配 BGM / 烧字幕
        # 这两项是旧界面里有用、又跟导演剪辑无关的功能，接到新流程上继续用。
        if use_bgm:
            print("\n[5.5/6] 配 BGM…")
            _add_bgm(out, loud)
        if burn_subs:
            print("\n[5.6/6] 烧字幕（用模型校对过的文字）…")
            _burn_subtitles(out, keep,
                            subs_segs or _sentences(words), stamp)
        if use_bgm or burn_subs:
            print("    成片：%s（%.1f 秒）"
                  % (os.path.basename(out), duration_of(out)))

        # ⑥ 回执
        print("\n[6/6] 让模型看结果…")
        # 告诉它删的是什么，不然它会本能地说"删太多"
        removed = []
        for chapter in script:
            for a, b in keep:
                if abs(chapter["t0"] - a) < 0.6:
                    break
            else:
                removed.append("%.0f~%.0fs（%s）"
                               % (chapter["t0"], chapter["t1"],
                                  "无词" if chapter["chars"] < 8
                                  else "%d 字" % chapter["chars"]))
        report = ("原片 %.1f 秒，剪成 %.1f 秒。\n"
                  "删掉的段：%s\n保留的段：%d 个\n你的理由：%s\n"
                  % (total, kept, "、".join(removed) or "无",
                     len(keep), plan.get("reason") or ""))
        print("    " + ask(llm, "一句话评价这次剪辑是否合理。",
                           report, max_tokens=120).replace("\n", " "))

        result = {"source": path, "out": out, "total": total,
                  "kept": round(kept, 2), "keep": keep,
                  "burn_subs": bool(burn_subs), "bgm": bool(use_bgm),
                  "loud": bool(loud),
                  "chapters": script, "plan": plan}
        with open(os.path.join(BASE, "data", "autoedit_last.json"),
                  "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1)
        print("\n记录：data\\autoedit_last.json")
        return 0
    finally:
        llm.stop()


if __name__ == "__main__":
    sys.exit(main())

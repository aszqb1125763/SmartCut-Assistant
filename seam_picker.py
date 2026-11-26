# -*- coding: utf-8 -*-
"""切口挑选：由近到远，找“三轴对得上”的一对切点。

为什么这么挑（子枫 2026-11-23 定的）：
  · 时间轴：删掉后两端贴在一起，天然连续，**没有约束**
  · 能量轴：两端能量要对得上（实测 5dB 以内，人耳听着就很连贯）
  · 音色轴：两端频谱形状接近（他会挑“同一个字”，同一个字频率天生相似）
  · 文字轴：两端都得落在词边界上，而且**长链语义要完善**（删完前后 500 字还连贯）

搜索策略：
  从最近的距离开始（0.1 秒），一档一档往外扩；
  每一档里只留“三轴都对得上、长链也过”的候选；有就停，没有才扩。

判据里的“正常分布”：
  接缝处的跳变要落在这个说话人**自己说话时的跳变分布**里。
  实测示范片：全片跳变平均 6.4dB、最大 64.8dB，接缝是 +1.3 / +17.6 / +15.5，
  全在正常范围里，所以听不出来。而且方向是“升”或“小幅降”，不是一路往下掉。
"""
from __future__ import annotations

import json
import os
import subprocess

import numpy as np

import coords

try:
    import tolerance
except Exception:  # noqa: BLE001
    tolerance = None

try:
    import longchain
except Exception:  # noqa: BLE001
    longchain = None

try:
    import decision
except Exception:  # noqa: BLE001
    decision = None

SR = 16000
# ---- 阈值一律来自 coords.py（2026-11-24 收口）-------------------------------
# 这里的名字只是兼容旧调用点；**改阈值请改 coords.py，别在这里改**。
# 为什么：原来这里和 tolerance.py 各写一份 5.0/500.0/5.0，改一处漏一处，
# 于是选择器按 2 倍收、审计按 1 倍算，同一处接缝两个结论。
#
# 音色用"绝对差值"而不是"倍率"：重心 1000 的 1.5 倍只差 500Hz，
# 重心 4000 的 1.5 倍差 2000Hz——同一个倍率在跨量级时完全不是一回事。
# 子枫那处示范：两端重心 3442 vs 3478，只差 36Hz。
#
# 形状距离：两端 ±200ms 能量曲线的均方根差（dB）。
# 实测（2026-11-23）：示范、他听着无缝的两处 3.70dB；旧的刺耳那处 9.77dB。
ENERGY_GAP_DB = coords.ENERGY_OK_DB
TONE_GAP_HZ = coords.TONE_OK_HZ
SHAPE_RMS_DB = coords.SHAPE_OK_DB
# 每个空隙最多问几次长链（三轴排前面几个）。问太多会很慢。
LONGCHAIN_TRIES = coords.LONGCHAIN_TRIES
# 接缝跳变要落在这个说话人正常跳变的哪个分位以内
JUMP_PERCENTILE = coords.JUMP_PERCENTILE
# 由近到远的距离档（秒）。
# 0.1 秒那种不是“删内容”，只是词间微调——真正的剪辑要删掉至少半句话。
# 所以第一档从 0.5 秒起（半句话的最小长度）。
DISTANCES = coords.DISTANCES

# 虚词：切在这些字旁边，句子会断在短语中间（"最下面 / 这一层"）。
# 实测 58.43 那处：能量差 1.9dB、音色 1.02，看上去完美，
# 但它把"最下面这一层"切成了"最下面 / 这一层"，语义断了。
FUNCTION_WORDS = set("的 了 是 在 和 跟 与 就 都 也 还 又 把 被 给 对 从 到 "
                     "这 那 一 个 之 而 或 但 因 所 以".split())
SEARCH_SPAN = coords.SEARCH_SPAN          # 空隙两端各搜多少（会被空隙长度自适应放大）
SEARCH_RATIO = coords.SEARCH_RATIO        # 自适应：搜索范围 = 空隙长度 × 这个比例
SEARCH_SPAN_MAX = coords.SEARCH_SPAN_MAX  # 上限：别无限往外扩（否则相似点太多）


def load_audio(path, ffmpeg, ss=None, dur=None, sr=SR, track=0):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-i", path, "-map", "0:a:%d" % int(track),
            "-ac", "1", "-ar", str(sr),
            "-f", "s16le", "-"]
    done = subprocess.run(cmd, capture_output=True)
    return (np.frombuffer(done.stdout, dtype="<i2").astype(np.float32)
            / 32768.0)


def frame_db(audio, sr=SR, win=0.02):
    """20ms 窗的 dB 包络。"""
    step = max(1, int(sr * win))
    n = audio.size // step
    if not n:
        return np.zeros(0)
    frames = audio[:n * step].reshape(n, step)
    return 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-9))


def db_at(audio, second, sr=SR, win=0.10):
    i0 = max(0, int((second - win / 2) * sr))
    i1 = min(audio.size, int((second + win / 2) * sr))
    seg = audio[i0:i1]
    if seg.size < 64:
        return -120.0
    return 20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9))


def tone_at(audio, second, sr=SR, win=0.10):
    """频谱重心（Hz）——代表音色亮度。"""
    i0 = max(0, int((second - win / 2) * sr))
    i1 = min(audio.size, int((second + win / 2) * sr))
    seg = audio[i0:i1]
    if seg.size < 128:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    freq = np.fft.rfftfreq(seg.size, 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    return float((spec * freq).sum() / total)


def side_audio(audio, second, side, sr=SR, win=0.10):
    """接缝的**单侧**窗口：[t-win, t] 或 [t, t+win]。

    为什么必须单侧（2026-11-24 找到的目标函数 bug）：
        成片里观众听到的是"左段最后 100ms"接"右段最前 100ms"，
        而被删掉的那段（a 之后、b 之前）根本不会播。
        以前用"以切点为中心 ±50ms"的窗口去量，把**将被删除的音频**
        也算进了接缝值——于是选择器说"三轴匹配"，交付出来却是 30dB 的跳变。
        35/42 超标、0/41 达标，根源就在这里。
    """
    half = max(1, int(win * sr))
    centre = int(round(second * sr))
    if side == "before":
        i0, i1 = max(0, centre - half), min(audio.size, centre)
    else:
        i0, i1 = max(0, centre), min(audio.size, centre + half)
    seg = audio[i0:i1]
    if seg.size < 64:
        return np.zeros(0)
    return seg


def side_db(audio, second, side, sr=SR, win=0.10):
    seg = side_audio(audio, second, side, sr, win)
    if seg.size < 64:
        return -120.0
    return 20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9))


def side_tone(audio, second, side, sr=SR, win=0.10):
    seg = side_audio(audio, second, side, sr, win)
    if seg.size < 128:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    freq = np.fft.rfftfreq(seg.size, 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    return float((spec * freq).sum() / total)


def side_shape(audio, second, side, sr=SR, span=0.20, step=0.02):
    """单侧能量曲线（去均值）：左切点看它**前面**、右切点看它**后面**。"""
    n = max(1, int(round(span / step)))
    vals = []
    for k in range(n):
        offset = (k - n + 1) * step if side == "before" else (k + 1) * step
        vals.append(db_at(audio, second + offset, sr, win=step))
    arr = np.array(vals, dtype=np.float32)
    return arr - arr.mean()


def seam_axes(audio, left_t, right_t, sr=SR):
    """一处接缝的三轴真值（与听众听到的一致）。

    left_t 是左段的出点，right_t 是右段的入点；两者之间的音频会被删掉，
    所以只取"出点之前"和"入点之后"。
    """
    e = abs(side_db(audio, left_t, "before", sr)
            - side_db(audio, right_t, "after", sr))
    t = abs(side_tone(audio, left_t, "before", sr)
            - side_tone(audio, right_t, "after", sr))
    s = shape_distance(side_shape(audio, left_t, "before", sr),
                       side_shape(audio, right_t, "after", sr))
    return {"energy_db": round(float(e), 1),
            "tone_hz": round(float(t), 0),
            "shape_db": round(float(s), 1),
            "grade": grade_of(e, t, s)}


def grade_of(energy_db, tone_hz, shape_db):
    """三轴合成档（同一份 tolerance，不许各写各的）。"""
    if tolerance is None:
        from coords import combine, energy_grade, shape_grade, tone_grade
        return combine(energy_grade(energy_db), tone_grade(tone_hz),
                       shape_grade(shape_db))
    return tolerance.combine(tolerance.energy_grade(energy_db),
                             tolerance.tone_grade(tone_hz),
                             tolerance.shape_grade(shape_db))


def normal_jump_stats(audio, sr=SR):
    """这个说话人自己说话时，相邻 50ms 的跳变分布。

    用来判断“接缝的跳变算不算正常”——不是看绝对值，是看它落不落在
    他自己说话的起伏范围里（子枫的示范片就是这么成立的）。
    """
    env = frame_db(audio, sr, win=0.05)
    if env.size < 2:
        return {"mean": 0.0, "p90": 0.0, "max": 0.0}
    d = np.abs(np.diff(env))
    return {"mean": round(float(d.mean()), 1),
            "p90": round(float(np.percentile(d, JUMP_PERCENTILE)), 1),
            "max": round(float(d.max()), 1)}


def sentence_edges(segments):
    """句子边界——只有这里才是“允许下刀”的候选。

    用词边界当候选会找出这种事：能量完全匹配，但把“最下面这一层”
    切成“最下面 / 这一层”，语义断了（实测 58.43 就是这种）。
    文字轴的意义就在这里：它先把“哪里能断”圈出来，能量轴再在里面挑。
    """
    edges = []
    for seg in segments or []:
        try:
            start, end = float(seg[0]), float(seg[1])
            text = str(seg[2] or "")
        except (IndexError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        # 句子边界也给一个“允许挪动”的窄区间：句子结束时前后 120ms 内
        # 都是安全的（那是气口），但不能挪得更远——挪远了就进到别的句子里。
        edges.append({"t": round(end, 3), "kind": "end", "text": text,
                      "level": "sentence",
                      "gap_lo": round(end, 3),
                      "gap_hi": round(end + 0.12, 3)})
        edges.append({"t": round(start, 3), "kind": "start", "text": text,
                      "level": "sentence",
                      "gap_lo": round(start - 0.12, 3),
                      "gap_hi": round(start, 3)})
    edges.sort(key=lambda x: x["t"])
    return edges


def word_edges(words):
    """所有词边界（前一个词结束 / 后一个词开始）。

    每个边界带上 `gap_lo`/`gap_hi`——也就是"这个边界所在的词间隙范围"。
    谷底细化只能在这个范围内找，越过去就是切进字里面了。
    """
    edges = []
    for k, w in enumerate(words):
        try:
            start, end = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        # “词尾”这个边界：允许从本词结尾找到下一个词开始
        nxt_start = None
        if k + 1 < len(words):
            try:
                nxt_start = float(words[k + 1]["start"])
            except (KeyError, TypeError, ValueError):
                nxt_start = None
        edges.append({"t": round(end, 3), "kind": "end",
                      "word": w.get("word", ""),
                      "next": (words[k + 1].get("word", "")
                               if k + 1 < len(words) else ""),
                      "gap_lo": round(end, 3),
                      "gap_hi": round(nxt_start if nxt_start else end + 0.12, 3)})
        # “词头”这个边界：允许从前一个词结尾找到本词开头
        prev_end = None
        if k > 0:
            try:
                prev_end = float(words[k - 1]["end"])
            except (KeyError, TypeError, ValueError):
                prev_end = None
        edges.append({"t": round(start, 3), "kind": "start",
                      "word": w.get("word", ""),
                      "prev": (words[k - 1].get("word", "")
                               if k > 0 else ""),
                      "gap_lo": round(prev_end if prev_end else start - 0.12, 3),
                      "gap_hi": round(start, 3)})
    edges.sort(key=lambda x: x["t"])
    return edges


def _inside_word(t, words, margin=0.0):
    """这个时刻是不是落在某个词的内部（离两端都超过 margin）。"""
    for w in words or []:
        try:
            st, en = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if st + margin < t < en - margin:
            return True
    return False


def _is_safe_edge(edge):
    """这个边界能不能下刀：旁边不能是虚词（否则断在短语中间）。

    注意：这条只对“词级边界”生效。整句的边界永远是安全的——一句话已经
    说完了，不存在“断在短语中间”的问题。之前对所有边界都过滤，结果
    小空隙里滤完一个候选都不剩（实测 6 处空隙因此找不到切点）。
    """
    if edge.get("level") == "sentence":
        return True
    text = str(edge.get("text") or "")
    if not text:
        return True
    head = text[:1]
    tail = text[-1:]
    if edge.get("kind") == "end" and tail in FUNCTION_WORDS:
        return False
    if edge.get("kind") == "start" and head in FUNCTION_WORDS:
        return False
    return True


def shape_at(audio, second, sr=SR, span=0.20, step=0.02):
    """接缝处 ±span 的能量形状（一维数组，均值归零）。

    单点测不出“送气被切断”——那是 100ms 尺度的事件。子枫的判断是
    “两端形状像才叫无缝”，所以要拿两段曲线去比，而不是拿两个数去比。
    """
    n = int(span / step)
    vals = []
    for k in range(-n, n + 1):
        vals.append(db_at(audio, second + k * step, sr, win=step))
    arr = np.array(vals, dtype=np.float32)
    return arr - arr.mean()


def shape_distance(s1, s2):
    """两段形状的距离：均方根差（dB）。越小越像。"""
    if s1 is None or s2 is None:
        return 999.0
    if s1.size != s2.size or s1.size == 0:
        return 999.0
    return float(np.sqrt(((s1 - s2) ** 2).mean()))


def refine_to_valley(audio, second, sr=SR, span=0.18,
                     lo=None, hi=None):
    """把候选边界收到附近的能量谷底，**但不能越进字里面**。

    子枫那两刀（60.24 / 69.28）都在字的交界处、正好是能量最低那一帧：
    ASR 的词尾比真实发音早 0.1 秒左右，所以“按词边界切”会切在韵母还在响
    的地方，按谷底切才对。

    但谷底不能乱跑：lo/hi 是这个候选允许的区间（通常是“前一个词结束”到
    “后一个词开始”这段空隙）。越过它就会切进字里，成片复审直接否决
    （实测：不限制时 6 处“切字”否决、复审 0 分）。
    """
    step = 0.01
    n = int(span / step)
    low = second - span if lo is None else max(second - span, lo)
    high = second + span if hi is None else min(second + span, hi)
    if high - low < 0.02:
        # 区间太窄（两个字紧贴着），就别挪了——挪一步就进到字里面，
        # 成片复审会判“切字”。保持原边界，让三轴去决定要不要用这个位置。
        return round(second, 3), round(db_at(audio, second, sr, win=0.02), 1)
    best_t, best_db = second, db_at(audio, second, sr, win=0.02)
    t = low
    while t <= high + 1e-9:
        v = db_at(audio, t, sr, win=0.02)
        if v < best_db:
            best_t, best_db = t, v
        t += step
    return round(best_t, 3), round(best_db, 1)


def pick_cuts(segments, audio, gaps, words=None, lc_server=None, sr=SR):
    """对每一处“要删掉的空隙”，在它两端找一对三轴匹配的切点。

    注意：不是在“全片所有词边界”里两两配对——那样会得到一堆毫无意义的
    “把相邻两个词接起来”的方案。正确的输入是 gaps：已经决定要删的区间
    [(左边段的结束, 右边段的开始), ...]（也就是保留段之间的空隙）。

    对每个 gap，在两端各 SEARCH_SPAN 秒内的句子边界里各挑一个，
    要求：能量差 ≤5dB、音色重心倍率 ≤1.5、且不切在虚词旁边。
    如果句子边界不够近，就退到词边界（同样要过虚词检查）。
    """
    # 候选**只能是句子边界**。
    # 用词边界会切在句子内部——哪怕那个位置恰好在两个字之间、三轴都匹配，
    # 观众听到的是“这句话说了一半”（实测成片复审 15 条否决：
    # “接缝落在句子中间…保留 39%”）。子枫自己那两刀（60.24/69.28）
    # 也都是句子边界，不是词边界。
    edge_texts = [(float(x[0]), float(x[1]), str(x[2] or ""))
                  for x in (segments or []) if len(x) >= 3]
    # 文字先过文字小脑（子枫 2026-11-24）：模型只吃小脑输出，不吃裸台词。
    _text_brain = None
    if words:
        try:
            from brains.base import WordSource
            from brains.text_brain import TextBrain
            _ws = WordSource("")
            _ws.set_words(words)
            _text_brain = TextBrain(words=_ws)
        except Exception:  # noqa: BLE001
            _text_brain = None
    edges = sentence_edges(segments)
    edges.sort(key=lambda x: x["t"])
    if len(edges) < 4:
        return []
    for e in edges:
        # 边界收到谷底再算特征——子枫的刀口都在谷底，不在词典的词边界上
        v_t, v_db = refine_to_valley(
            audio, e["t"], sr,
            lo=e.get("gap_lo"), hi=e.get("gap_hi"))
        e["valley_t"] = v_t
        e["db"] = v_db
        e["tone"] = round(tone_at(audio, v_t, sr), 0)
        e["shape"] = shape_at(audio, v_t, sr)
        # 单侧特征（2026-11-24）：和听众听到的一致——左段只看"出点之前"，
        # 右段只看"入点之后"。中心窗会把即将被删掉的音频也算进接缝值。
        e["db_before"] = round(side_db(audio, v_t, "before", sr), 1)
        e["db_after"] = round(side_db(audio, v_t, "after", sr), 1)
        e["tone_before"] = round(side_tone(audio, v_t, "before", sr), 0)
        e["tone_after"] = round(side_tone(audio, v_t, "after", sr), 0)
        e["shape_before"] = side_shape(audio, v_t, "before", sr)
        e["shape_after"] = side_shape(audio, v_t, "after", sr)
    out = []
    for left_end, right_start in gaps:
        # 搜索范围跟着空隙长度走：小空隙小范围，大空隙大范围。
        # 子枫那处示范：空隙 19.5 秒，他找的切口离右端 4.0 秒——
        # 固定 1.5 秒根本够不着。
        span = min(SEARCH_SPAN_MAX,
                   max(SEARCH_SPAN, (right_start - left_end) * SEARCH_RATIO))
        near_left = [e for e in edges
                     if abs(e["t"] - left_end) <= span
                     and _is_safe_edge(e)]
        near_right = [e for e in edges
                      if abs(e["t"] - right_start) <= span
                      and _is_safe_edge(e)]
        cands = []
        for a in near_left:
            for b in near_right:
                if b["t"] - a["t"] < 0.30:
                    continue
                e_gap = abs(a["db_before"] - b["db_after"])
                tone_gap = abs(a["tone_before"] - b["tone_after"])
                # 形状也要像：左段"出点之前"的 200ms 曲线 vs 右段"入点之后"的
                s_gap = shape_distance(a.get("shape_before"), b.get("shape_after"))
                # 三段式：合格区和边缘区都收，只有超限才淘汰。
                # 硬阈值会把 5.1dB 判成不合格，可人耳分不清 4.9 和 5.1
                # （子枫：「所有审核都要允许范围值，不能绝对值」）。
                if tolerance is not None:
                    _tg = tolerance.combine(
                        tolerance.energy_grade(e_gap),
                        tolerance.tone_grade(tone_gap),
                        tolerance.shape_grade(s_gap))
                    if _tg == "over":
                        continue
                else:
                    if (e_gap > ENERGY_GAP_DB or tone_gap > TONE_GAP_HZ
                            or s_gap > SHAPE_RMS_DB):
                        continue
                    _tg = "ok"
                # 谷底细化可能把切点挪进字里面（ASR 的词尾比真实发音早
                # 0.1 秒，按它切反而截掉尾音）。这里直接排除切进词内部的候选，
                # 不靠事后收口——实测事后收口只修掉 5 处，还剩 3 处。
                if words and (_inside_word(a["valley_t"], words)
                              or _inside_word(b["valley_t"], words)):
                    continue
                cands.append({
                    "left_t": a["valley_t"], "right_t": b["valley_t"],
                    "left_text": a.get("text", "")[:18],
                    "right_text": b.get("text", "")[:18],
                    "left_db": a["db_before"], "right_db": b["db_after"],
                    "energy_gap": round(e_gap, 1),
                    "tone_gap_hz": round(tone_gap, 0),
                    "shape_rms": round(s_gap, 1),
                    "score": round(e_gap + tone_gap / 100.0 + s_gap / 2.0, 2),
                    "removed": round(b["t"] - a["t"], 2),
                    "tolerance": _tg,
                })
        # 合格区排前面，边缘区排后面（都可用，只是优先级不同）
        cands.sort(key=lambda x: (0 if x.get("tolerance") == "ok" else 1,
                                  x["score"]))
        # Q1 选点闸（2026-11-24）：优先只在**合格区**里挑；
        # 一个合格候选都没有时才退到边缘区，并且必须标 fallback 记账。
        # 以前 edge 和 ok 一起排，语义好一点就能压过三轴——于是"选了它"
        # 但交付端判它超标。现在把"合格"作为入场券，语义只在合格区里排序。
        _ok_pool = [c for c in cands if c.get("tolerance") == "ok"]
        _pool = _ok_pool or cands
        _edge_only = not _ok_pool
        # **一次多问几个，最后用最好的**（子枫：「可以让模型一个切点多选几个，
        # 范围可以稍微大一点，选出来后用最好的，不能一次一个」）。
        # 只问最优那一个的毛病：三轴最优的位置语义常常是断的
        # （实测 57.47→70.14 能量 0.7dB/形状 2.6dB，但前文讲氢、后文讲泥土），
        # 而排第二第三的候选可能语义通、三轴也不差多少。
        best = None
        checked = 0
        best_rank = -1.0
        for cand in _pool[:LONGCHAIN_TRIES]:
            if longchain is None or lc_server is None:
                cand["longchain"] = "?"
                _rank = 0.55
            else:
                try:
                    _before, _after = _texts_around(edge_texts, cand["left_t"],
                                                    cand["right_t"])
                    _before = _annotate(_text_brain, _before)
                    _after = _annotate(_text_brain, _after)
                    _lv = longchain.level_for_removed(cand["removed"])
                    _lc = longchain.judge(_before, "（中间省略）", _after,
                                          level=_lv, server=lc_server)
                    cand["longchain"] = _lc.get("result")
                    cand["longchain_why"] = _lc.get("why")
                    cand["longchain_level"] = _lv
                    checked += 1
                except Exception as _lc_err:  # noqa: BLE001
                    from llm_gateway import propagate_api_error
                    propagate_api_error(_lc_err)
                    cand["longchain_error"] = str(_lc_err)[:60]
                    cand["longchain"] = "?"
                _rank = {"通": 1.0, "勉强": 0.6, "断": 0.0}.get(
                    cand.get("longchain"), 0.5)
            # 权重属于本地模型（子枫 2026-11-24）：这里只读参数，不自己定比例。
            if decision is not None:
                _w_sem, _w_ax = decision.candidate_rank()
            else:
                _w_sem, _w_ax = 0.7, 0.3
            _axis = 1.0 / (1.0 + (cand["score"] / 5.0))
            _tol = 1.0 if cand.get("tolerance") == "ok" else 0.75
            cand["rank"] = round(_w_sem * _rank + _w_ax * _axis * _tol, 3)
            if cand["rank"] > best_rank:
                best_rank = cand["rank"]
                best = cand
        if best is None and _pool:
            # 兜底标准（子枫 2026-11-23 定）：
            # 所有候选都不完美时，**选最不极端的那个，而不是不处理**。
            # 因为原有切点是按“句子边界”硬切的，通常比这里任何一个都差；
            # 不选 = 放弃改善。这就是“宽松”的最后一层：不追求完美，只求不最坏。
            best = _pool[0]
            best["fallback"] = "最不极端"
        if best is not None:
            best["longchain_checked"] = checked
            best["candidates"] = len(cands)
            best["ok_candidates"] = len(_ok_pool)
            if best.get("tolerance") != "ok":
                best.setdefault("fallback", "edge")
        out.append({"gap": (round(left_end, 2), round(right_start, 2)),
                    "best": best})
    return out


def _annotate(text_brain, text):
    """把一句话过一遍文字小脑，附上标记再交给模型（模型不碰裸台词）。"""
    if not text_brain or not text:
        return text
    try:
        mark = text_brain.mark(text)
    except Exception:  # noqa: BLE001
        return text
    tags = []
    if mark.get("ends_with_function"):
        tags.append("句末虚词")
    if mark.get("sticky"):
        tags.append("黏着:" + "".join(mark["sticky"]))
    if mark.get("logic_hits"):
        tags.append("逻辑:" + ",".join(mark["logic_hits"]))
    if mark.get("pronouns"):
        tags.append("指代:" + ",".join(mark["pronouns"]))
    return text + ("[" + ";".join(tags) + "]" if tags else "")


def _texts_around(sentences, left_t, right_t):
    """取左切点之前最近的一句、右切点之后最近的一句。"""
    before = ""
    after = ""
    for start, end, text in sentences:
        if end <= left_t + 0.10:
            before = text
    for start, end, text in sentences:
        if start >= right_t - 0.10:
            after = text
            break
    return before, after


def report(index_dir, ffmpeg, limit=20):
    """读索引 → 用现有剪辑计划里的空隙当“要删的区间” → 逐处找切口。"""
    base = index_dir
    data = json.load(open(os.path.join(base, "index.json"), encoding="utf-8"))
    src = data.get("source") or data.get("path") or ""
    words = []
    for seg in data.get("segment_evidence") or []:
        for w in seg.get("words") or []:
            words.append(w)
    if not words:
        return {"error": "索引里没有词级时间戳"}
    # 最新剪辑计划里的保留段 → 相邻两段之间的空隙就是要删的
    import glob
    plans = sorted(glob.glob(os.path.join(base, "plans", "transition-*.json")),
                   key=os.path.getmtime, reverse=True)
    if not plans:
        return {"error": "没有剪辑计划（plans/transition-*.json）"}
    plan = json.load(open(plans[0], encoding="utf-8"))["plan"]
    spans = [(float(a), float(b)) for a, b in plan.get("final_render_spans") or []]
    gaps = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)]
    audio = load_audio(src, ffmpeg)
    stats = normal_jump_stats(audio)
    cuts = pick_cuts(data.get("segments") or [], audio, gaps)
    return {
        "source": src,
        "plan": os.path.basename(plans[0]),
        "words": len(words),
        "gaps": len(gaps),
        "normal_jump": stats,
        "cuts": cuts[:limit],
        "total": len(cuts),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("index_dir")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    import index as _index
    out = report(args.index_dir, _index.find_ffmpeg())
    if out.get("error"):
        print(out["error"])
        return 1
    print("说话人正常跳变：平均 %.1f dB / P90 %.1f dB / 最大 %.1f dB"
          % (out["normal_jump"]["mean"], out["normal_jump"]["p90"],
             out["normal_jump"]["max"]))
    print("剪辑计划 %s：%d 处空隙" % (out["plan"], out["gaps"]))
    print()
    print("空隙（源片秒）        最佳切口 A→B        能量差  音色差  形状差  删掉")
    ok = 0
    for row in out["cuts"]:
        g = row["gap"]
        b = row["best"]
        if not b:
            print("%7.2f → %7.2f    （两端 %.1f 秒内找不到匹配的切点）"
                  % (g[0], g[1], SEARCH_SPAN))
            continue
        ok += 1
        print("%7.2f → %7.2f   %7.2f → %7.2f   %5.1fdB  %6.0fHz  %5.1fdB  %5.2fs"
              % (g[0], g[1], b["left_t"], b["right_t"], b["energy_gap"],
                 b["tone_gap_hz"], b["shape_rms"], b["removed"]))
    print()
    print("有匹配切点的空隙：%d / %d" % (ok, len(out["cuts"])))


if __name__ == "__main__":
    main()

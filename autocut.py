# -*- coding: utf-8 -*-
"""自动剪辑：空片段剔除 + 转场判定 + 拼接出片。

设计来源：《素材卡片工具：自动剪辑与智能拼接（需求规格）》与《语义判断方案》

两条原则写在代码里：
  1. 默认硬切，叠化只是画面接不上时的兜底——叠化用多了整片会发虚
  2. 静默 ≠ 空。玩家被震惊后的留白是高光，只按"信号低就剪"会把好东西剪掉

用法：
    python autocut.py 索引目录/index.json --dry-run      # 只打印剪辑计划，不出片
    python autocut.py 索引目录/index.json --out 成片.mp4  # 真的出片
    python autocut.py 索引目录/index.json --no-cut       # 不剪空段，只拼接
    python autocut.py --conf                            # 生成默认配置到 conf/autocut.json
"""


try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
# 打不开黑窗：抽帧、试编、渲染都是子进程，不加这个标志每起一个都闪一个窗口
_NO_WIN = 0x08000000 if os.name == "nt" else 0
sys.path.insert(0, BASE)

import numpy as np  # noqa: E402

import index as indexer  # noqa: E402
import completeness  # noqa: E402
import audio_evidence_cache  # noqa: E402
import llm_gateway  # noqa: E402

CONF_DIR = os.path.join(BASE, "conf")
CONF_PATH = os.path.join(CONF_DIR, "autocut.json")


def _require_local_content_selection():
    if llm_gateway.api_enabled():
        raise llm_gateway.APIRequestError(
            "API 模式请使用「智能剪辑」完成内容取舍；传统按分数剪辑入口已停用。"
            "命令行请使用 autopilot.py <素材路径>。")


DEFAULT_CONF = {
    "blank": {
        "max_keep_blank": 6.0,     # 留白 ≤ 这个长度就整段留着（观众需要停顿）
        "keep_head": 3.0,          # 长留白保留开头几秒（反应）
        "keep_tail": 1.0,          # 长留白保留结尾几秒
        "min_cut": 1.5,            # 要剪就至少剪这么长，否则不值得动刀
        "min_blank": 1.5,          # 短于这个的空白根本不参与判定
        "gap_merge": 0.8,          # 两段空白的间隔小于此值就当成一段
        "motion_below": 0.055,     # 画面变化低于此值算"画面低"
        "level_below_db": -55.0,   # 人声轨电平低于此值算"人声低"
        "protect_window": 8.0,     # 往前看多少秒找保护信号
        "peak_percentile": 90.0,   # P3：前 window 秒内音量峰值超过全片这个分位 → 保护
        "motion_percentile": 90.0  # P4：前 window 秒内画面变化超过全片这个分位 → 保护
    },
    "protect": {
        "shock_words": [
            "卧槽", "我靠", "啊？", "这也行", "不是吧", "这么猛", "离谱", "真的假的",
            "牛", "牛逼", "完了", "没了", "这么大", "这么高", "这也太", "怎么可",
            "不可能", "见鬼", "什么情况", "哎哟", "哇", "我去", "天哪", "好家伙"
        ],
        "unfinished_endings": [
            "然后", "就是", "那个", "这个", "所以", "因为", "但是", "而且",
            "的话", "的时候", "对了", "还有"
        ]
    },
    "select": {
        # 按目标时长挑选：把素材压到 target_ratio 左右。
        # 实测这份示范成片留了 27%，所以默认 0.30。
        "enabled": False,
        "target_ratio": 0,        # 0 = 由模型判断留多少
        # 打分只用语速（字/秒）。实测加关键词、音量、画面变化都更差。
        "weight_cps": 1.0,
        "weight_prog": 0.0,
        "min_card": 4.0          # 短于这个的卡片不单独保留
    },
    "transition": {
        "crossfade": 0.5,          # 叠化时长（秒）
        "corr_threshold": 0.55,    # (1-相关系数) 小于此值算"画面连贯"；实测接缝中位 0.30
        "voice_margin": 1.0,       # 接缝前后这段时间内有人说话 → 强制硬切
        "audio_gap_max": 2.0,      # 删掉的部分小于此值 → 声音还算是连着的
        "visual_only": "cut",      # 画面连贯但声音不连贯时：cut / crossfade
        # 画面差异超过这个值、且接缝有人声时，做短视频叠化（人声不叠）
        "vfade_min_hist": 0.55,
        "vfade": 0.22,
        # 刀口对齐原片镜头切换点（画面识别）；0 = 关闭
        "shot_snap": "1",
        "shot_snap_tol": 0.6,
        # 微过渡：每一刀都做一个极短交叉（视频+音频）。0 = 关
        "micro_bridge": 0.0,
    },
    "render": {
        "crf": 20,
        "x264_preset": "veryfast",
        "preset": "p5",
        "audio_bitrate": "192k",
        "use_nvenc": True,
        # 编码器：auto=预览走显卡、1080p 走 x264；也可以强制 nvenc / x264
        "encoder": "auto",
        "nvenc_preset": "p1",
        "nvenc_cq": 30,
        # 选中多条音轨时混成一条（播放器只播第一条音频流，分轨会"没声音"）
        "mix_tracks": True,
    "export_stems": False,
    }
}


def load_conf(path=None):
    path = path or CONF_PATH
    conf = json.loads(json.dumps(DEFAULT_CONF))       # 深拷贝
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                user = json.load(handle)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(conf.get(k), dict):
                    conf[k].update(v)
                else:
                    conf[k] = v
        except Exception as exc:  # noqa: BLE001
            print("配置文件读不了，用默认值：%s" % exc)
    return conf


def write_default_conf():
    os.makedirs(CONF_DIR, exist_ok=True)
    if os.path.isfile(CONF_PATH):
        print("配置已存在，保持不动：%s" % CONF_PATH)
        return CONF_PATH
    json.dump(DEFAULT_CONF, open(CONF_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("已写出默认配置：%s" % CONF_PATH)
    return CONF_PATH


# --------------------------------------------------------------------------
# 一、空片段检测（三低 + 语义保护）
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 句子/段落/类型分析
# --------------------------------------------------------------------------
NOISE_WORDS = ("咳嗽", "清嗓", "喷嚏", "吸鼻", "叹气", "咳嗽声", "哈欠")
FILLER_SET = {"嗯", "啊", "哦", "呃", "唉", "嘿", "额", "唔", "呐"}


def classify_segment(text, dur):
    """给一句话定性：speech / laugh / filler / noise。

    目的是把"不是内容"的东西识别出来——咳嗽、笑声、拖音，
    这些在剪的时候不必保护，可以随手剪掉。
    """
    import re as _re
    t = (text or "").strip()
    if not t:
        return "noise"
    if any(w in t for w in NOISE_WORDS):
        return "noise"
    if len(t) <= 3 and t.strip("。，、！？…~") in FILLER_SET:
        return "filler"
    if _re.fullmatch(r"[哈呵嘿嘻hH]{2,10}", t):
        return "laugh"
    cps = len(t) / max(0.3, dur)
    if dur < 0.45 and len(t) <= 2:
        return "noise"
    if cps < 0.45:
        return "slow"                 # 拖长音/含糊，仍然按"有说话"保护
    return "speech"


def load_units(data):
    """把逐句时间轴整理成"单元"，并定出类型和段落。

    优先用本地大模型给的语义标注（data["semantic"]）：
      · 类型：speech / laugh / filler / noise —— 决定要不要保护
      · 段落：模型判断的话题边界 —— 决定在哪切听感最自然
    没有语义数据时退回规则判断（只看字数和语速）。
    """
    segs = data.get("segments") or []
    sem = data.get("semantic") or []
    scores = ([0.5] * len(segs) if llm_gateway.api_enabled()
              else data.get("scores") or [])
    units = []
    for i, it in enumerate(segs):
        try:
            a, b, t = float(it[0]), float(it[1]), str(it[2])
        except (TypeError, ValueError, IndexError):
            continue
        if b <= a:
            continue
        if i < len(sem) and isinstance(sem[i], dict):
            kind = sem[i].get("kind") or classify_segment(t, b - a)
            pb = bool(sem[i].get("para_break"))
        else:
            kind = classify_segment(t, b - a)
            pb = False
        sc = float(scores[i]) if i < len(scores) else None
        units.append({"a": a, "b": b, "text": t, "dur": b - a,
                      "kind": kind, "pb": pb, "score": sc})
    units.sort(key=lambda u: u["a"])
    para = 0
    for i, u in enumerate(units):
        if i == 0:
            para = 0
        elif u.get("pb"):
            para += 1
        elif u["a"] - units[i - 1]["b"] > 1.2:
            para += 1          # 长停顿也当段落断开
        u["para"] = para
    return units


def _merge(iv, gap=0.05):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if b - a <= 0:
            continue
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def snap_cuts_to_sentences(cuts, units, min_piece=0.35):
    """把剪除区间裁到"不碰任何一句话"。

    做法：沿着剪除区间走，遇到需要保护的句子就把剪点推到句子边界，
    把原来一个区间拆成句子之间的几段空白。
    """
    protect = [u for u in units if u["kind"] != "noise"]
    out = []
    for a, b in cuts:
        cur = a
        for u in protect:
            if u["b"] <= cur or u["a"] >= b:
                continue
            if u["a"] > cur:
                out.append((cur, u["a"]))
            cur = max(cur, u["b"])
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    out = [(a, b) for a, b in out if b - a >= min_piece]
    return _merge(out)


def expand_to_sentences(segments, units, max_extend=2.5, min_piece=0.3):
    """保留片段的两端如果切在句子内部，往外扩到句子边界。

    这一步和上面那个是一对：剪的地方避开句子，留的地方也要补全句子，
    两边合起来才能保证"一句话不会只剩半句"。
    """
    prot = [u for u in units if u["kind"] != "noise"]
    out = []
    for a, b in segments:
        na, nb = a, b
        for u in prot:
            if u["a"] < na < u["b"] and na - u["a"] <= max_extend:
                na = u["a"]
                break
        for u in prot:
            if u["a"] < nb < u["b"] and u["b"] - nb <= max_extend:
                nb = u["b"]
                break
        if nb - na >= min_piece:
            out.append((max(0.0, na), nb))
    return _merge(out)


def drop_non_content(segments, units, min_piece=0.3):
    """把咳嗽、杂音这类"不是话"的片段从成片里剔掉。

    训练出来的习惯是"留说话的内容"，咳嗽不属于内容，
    留在片子里只会显得粗糙。
    """
    bad = [(u["a"], u["b"]) for u in units
           if u["kind"] == "noise" and (u["b"] - u["a"]) < 4.0]
    if not bad:
        return segments
    out = []
    for a, b in segments:
        cur = a
        for x, y in bad:
            if y <= cur or x >= b:
                continue
            if x > cur:
                out.append((cur, x))
            cur = max(cur, y)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return _merge([(a, b) for a, b in out if b - a >= min_piece])


def align_to_speech(segments, units, max_shift=3.0, head=1.5):
    """每段开头往前顶到最近的一句说话。

    3 秒定律：观众最先看到的 3 秒决定要不要继续看。
    如果一段开头是静音或者"嗯…"，观感很差，所以把开头推到真正开始说话的位置。
    """
    speech = [u for u in units if u["kind"] in ("speech", "laugh")]
    out = []
    for a, b in segments:
        na = a
        for u in speech:
            if u["b"] <= a:
                continue
            if u["a"] > a + max_shift:
                break
            if u["a"] > a:
                na = u["a"]
            break
        if b - na >= 0.4:
            out.append((na, b))
    return _merge(out)


def finalize_segments(segments, units, verbose=None):
    """成片前的最后一道整理，界面和命令行共用。

    顺序有讲究：
      1) 先剔掉咳嗽/杂音——它们不算内容
      2) 开头顶到最近的一句说话——3 秒定律，开头别是静音或"嗯…"
      3) 两端补全到句子边界——不留半句话
      4) 段落残尾丢掉——半段话听感最差
    """
    if not units:
        return segments
    before = sum(b - a for a, b in segments)
    out = drop_non_content(segments, units)
    out = align_to_speech(out, units)
    out = expand_to_sentences(out, units)
    out = drop_orphan_paragraphs(out, units)
    after = sum(b - a for a, b in out)
    if verbose:
        verbose("整理成片：剔除噪声/补全句子，%.1f → %.1f 分钟"
                % (before / 60, after / 60))
    return out


def drop_orphan_paragraphs(segments, units, min_keep=1.2):
    """段落被切得只剩个开头/结尾（不足 min_keep 秒）时，整段让给剪除。

    听感上"半句话"往往就是这么来的：一段话留下 0.8 秒的尾巴。
    """
    if not segments or not units:
        return segments
    para_span = {}
    for u in units:
        if u["kind"] == "noise":
            continue
        p = u["para"]
        lo, hi = para_span.get(p, (u["a"], u["b"]))
        para_span[p] = (min(lo, u["a"]), max(hi, u["b"]))

    def covered(a, b, span):
        return max(0.0, min(b, span[1]) - max(a, span[0]))

    out = []
    for a, b in segments:
        keep = True
        for p, span in para_span.items():
            ov = covered(a, b, span)
            if 0 < ov < min_keep and (span[1] - span[0]) > min_keep:
                keep = False
                break
        if keep:
            out.append((a, b))
    return out or segments


def _voice_mask(duration, runs, grid=1.0):
    """每秒一个 bool：这一秒里有没有人说话。"""
    n = max(1, int(round(duration / grid)))
    mask = np.zeros(n, dtype=bool)
    for a, b in runs or []:
        i0 = max(0, int(a / grid))
        i1 = min(n, int(np.ceil(b / grid)))
        if i1 > i0:
            mask[i0:i1] = True
    return mask


def _motion_per_second(times, diffs, duration, grid=1.0):
    """把抽帧的画面变化量摊到每秒，取该秒内最大值。"""
    n = max(1, int(round(duration / grid)))
    out = np.zeros(n, dtype=np.float32)
    for t, d in zip(times or [], diffs or []):
        i = int(t / grid)
        if 0 <= i < n and d > out[i]:
            out[i] = d
    return out


def _blank_candidates(data, conf, grid=1.0):
    """返回 [(start, end)]：三低、但还没做语义保护判断的候选空段。"""
    c = conf["blank"]
    duration = float(data["duration"])
    an = data.get("analysis") or {}
    voice = _voice_mask(duration, an.get("runs"), grid)
    motion = _motion_per_second(an.get("times"), an.get("diffs"), duration, grid)
    level = np.array(an.get("audio_1s") or [], dtype=np.float32)

    n = len(voice)
    low = np.zeros(n, dtype=bool)
    for i in range(n):
        if voice[i]:
            continue
        if motion[i] > c["motion_below"]:
            continue
        if i < len(level) and level[i] > c["level_below_db"]:
            continue
        low[i] = True

    runs = []
    i = 0
    gap = int(round(c["gap_merge"] / grid))
    while i < n:
        if not low[i]:
            i += 1
            continue
        j = i
        last = i
        while j < n:
            if low[j]:
                last = j
                j += 1
            elif j - last <= gap:
                j += 1          # 中间夹了一两秒的动静，仍然算同一段
            else:
                break
        runs.append((i * grid, min(duration, (last + 1) * grid)))
        i = last + 1
    return runs


def _protect_reason(data, start, conf):
    """看一下这段空白前面有没有"不能剪"的信号；命中就返回原因。"""
    b = conf["blank"]
    p = conf["protect"]
    win = b["protect_window"]
    lo = max(0.0, start - win)
    cards = data.get("cards") or []

    # P1 感叹词 / P2 话没说完：看窗口内最后一张有台词的卡片。
    # 原文和参考文本（text_fixed）都参与匹配——识别错字最容易把感叹词打歪，
    # 两份都看能少漏判。
    tail = [c for c in cards
            if ((c.get("text") or "").strip() or (c.get("text_fixed") or "").strip())
            and float(c["end"]) <= start + 0.5 and float(c["end"]) >= lo]
    if tail:
        last = max(tail, key=lambda c: float(c["end"]))
        t = " ".join(x for x in ((last.get("text") or "").strip(),
                                 (last.get("text_fixed") or "").strip()) if x)
        for w in p["shock_words"]:
            if w in t:
                return "P1 感叹词「%s」" % w
        stripped = t.rstrip("。！？!?…")
        if t and t[-1] in "，、,；;":
            return "P2 上一句没说完（逗号结尾）"
        for w in p["unfinished_endings"]:
            if stripped.endswith(w):
                return "P2 上一句停在「%s」" % w

    an = data.get("analysis") or {}
    # P3 刚炸过：窗口内音量峰值进入全片前 10%
    level = np.array(an.get("audio_1s") or [], dtype=np.float32)
    if level.size:
        thr = float(np.percentile(level, b["peak_percentile"]))
        i0, i1 = int(lo), int(min(len(level), start + 1))
        if i1 > i0 and float(level[i0:i1].max()) >= thr:
            return "P3 前 %.0f 秒有音量峰值" % win

    # P4 刚有大动作：窗口内画面变化进入全片前 10%
    times = an.get("times") or []
    diffs = an.get("diffs") or []
    if diffs:
        thr = float(np.percentile(diffs, b["motion_percentile"]))
        win_d = [d for t, d in zip(times, diffs) if lo <= t <= start]
        if win_d and max(win_d) >= thr:
            return "P4 前 %.0f 秒画面剧烈变化" % win
    return ""


# ---------------------------------------------------------------------------
# 语义空段检测
# ---------------------------------------------------------------------------
def detect_cuts_by_semantics(data, conf=None, min_gap=2.0, head=0.0, tail=0.0):
    """按台词判断哪里没人说话：返回剪除区间 [(start, end)]。

    只在有台词数据时可用；没有台词就返回 None，让调用方退回电平判断。
    """
    segs = data.get("segments") or []
    if len(segs) < 5:
        return None
    dur = float(data.get("duration") or 0) or float(segs[-1][1]) + 1
    sem = data.get("semantic") or []

    cuts = []
    # 开头：第一句之前的空场
    first = float(segs[0][0])
    if first >= max(min_gap, 0.8):
        cuts.append((0.0, first + head))
    # 句子之间的大空档
    for i in range(len(segs) - 1):
        a = float(segs[i][1])
        b = float(segs[i + 1][0])
        gap = b - a
        if gap >= min_gap:
            cuts.append((a + head, b - tail))
    # 结尾
    last = float(segs[-1][1])
    if dur - last >= max(min_gap, 0.8):
        cuts.append((last - head, dur))
    # 被判定为噪声/杂音的句子，整句剪掉
    for i, sg in enumerate(segs):
        if i < len(sem) and sem[i].get("kind") == "noise":
            a, b = float(sg[0]), float(sg[1])
            if b - a >= 0.3:
                cuts.append((a, b))

    cuts = [(max(0.0, a), min(dur, b)) for a, b in cuts if b - a > 0.15]
    cuts.sort()
    merged = []
    for a, b in cuts:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def detect_cuts(data, conf):
    """算出要剪掉的区间，以及每个空段的判定理由（方便人工复核）。"""
    b = conf["blank"]
    duration = float(data["duration"])
    report = []
    cuts = []
    for a, e in _blank_candidates(data, conf):
        length = e - a
        if length < b.get("min_blank", 1.5):
            continue
        reason = _protect_reason(data, a, conf)
        if reason:
            if length <= b["max_keep_blank"]:
                report.append({"start": a, "end": e, "length": length,
                               "action": "keep", "why": reason + "（短留白整段保留）"})
                continue
            # 长留白：保留反应头 + 结尾，剪中间
            ca = a + b["keep_head"]
            cb = e - b["keep_tail"]
            if cb - ca >= b["min_cut"]:
                cuts.append((ca, cb))
                report.append({"start": a, "end": e, "length": length,
                               "action": "trim", "cut_from": round(ca, 2),
                               "cut_to": round(cb, 2),
                               "why": "%s；长留白，只留头尾" % reason})
            else:
                report.append({"start": a, "end": e, "length": length,
                               "action": "keep", "why": reason})
            continue
        if length >= b["min_cut"]:
            cuts.append((a, e))
            report.append({"start": a, "end": e, "length": length,
                           "action": "cut", "why": "三低且无保护信号"})
        else:
            report.append({"start": a, "end": e, "length": length,
                           "action": "keep", "why": "太短，不值得剪"})
    cuts.sort()
    # 关键：把剪点吸到句子边界，绝不从句子中间切
    units = load_units(data)
    if units:
        before = len(cuts)
        cuts = snap_cuts_to_sentences(cuts, units)
        noise = sum(1 for u in units if u["kind"] == "noise")
        if report is not None:
            report.append({"start": -1, "end": -1, "length": 0, "action": "meta",
                           "why": "句子保护：%d 句（其中噪声/咳嗽 %d 句），"
                                  "剪除区间 %d → %d 段"
                                  % (len(units), noise, before, len(cuts))})
    return cuts, report


def save_plan(path, data, report, cuts, duration, extra=None):
    """把剪辑计划存成可读可改的 JSON。

    每个空白段都记着：时间、判定动作、理由、以及实际要剪掉的区间。
    想反悔就把 action 从 cut 改成 keep（或反过来），再跑 --plan 重出，
    不用重新分析一遍。
    """
    plan = {
        "source": data.get("source"),
        "name": data.get("name"),
        "duration": round(float(duration), 2),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blanks": sorted(report, key=lambda x: x["start"]),
        "cuts": [[round(a, 2), round(b, 2)] for a, b in sorted(cuts)],
    }
    if extra:
        plan.update(extra)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    return plan


def cuts_from_plan(plan):
    """按计划里的 action 重新算出剪除区间（支持手改）。"""
    cuts = []
    for b in plan.get("blanks") or []:
        act = b.get("action")
        if act == "cut":
            cuts.append((float(b["start"]), float(b["end"])))
        elif act == "trim":
            if "cut_from" in b and "cut_to" in b:
                cuts.append((float(b["cut_from"]), float(b["cut_to"])))
    return sorted(cuts)


FILL_WORDS = ["就是", "然后", "那个", "这个", "是吧", "对吧", "嗯", "啊", "额",
              "我看看", "怎么", "为什么", "稍微", "一点", "可能", "应该", "等一下", "不知道"]

PROGRESS_WORDS = ["完成", "做好", "做成", "搭好", "搞定", "成功", "合成", "获得",
                  "挖到", "放上", "下一步", "接下来", "开始", "启动", "接上", "连上",
                  "装满", "够了", "出来", "可以了", "直接", "然后我们", "放进去", "拿出来"]


MODEL_PATH = os.path.join(BASE, "conf", "editor_model.json")
_MODEL_CACHE = {}


def load_model():
    """读训练出来的剪辑模型（没有就返回 None）。"""
    # API decisions must not inherit a local model already cached in memory.
    if llm_gateway.api_enabled():
        return None
    if "m" in _MODEL_CACHE:
        return _MODEL_CACHE["m"]
    m = None
    try:
        if os.path.isfile(MODEL_PATH):
            with open(MODEL_PATH, encoding="utf-8") as handle:
                m = json.load(handle)
            if not m.get("weights") or not m.get("mean"):
                m = None
    except Exception:  # noqa: BLE001
        m = None
    _MODEL_CACHE["m"] = m
    return m


def _seg_feature_row(segs, i):
    """和 learn.py 里训练时用的特征保持一致（顺序不能变）。"""
    s0, e0, t = segs[i][0], segs[i][1], segs[i][2]
    L = max(0.2, e0 - s0)
    prev_gap = max(0.0, s0 - segs[i - 1][1]) if i else 0.0
    nxt_gap = max(0.0, segs[i + 1][0] - e0) if i + 1 < len(segs) else 0.0
    lo, hi = max(0, i - 3), min(len(segs), i + 4)
    wl = sum(segs[k][1] - segs[k][0] for k in range(lo, hi))
    wc = sum(len(segs[k][2]) for k in range(lo, hi))
    total = segs[-1][1] if segs else 1.0
    prog = sum(t.count(w) for w in PROGRESS_WORDS)
    fill = sum(t.count(w) for w in FILL_WORDS)
    return [len(t) / L, wc / max(0.2, wl), min(L, 15.0) / 15.0,
            min(prev_gap, 5.0) / 5.0, min(nxt_gap, 5.0) / 5.0,
            s0 / max(1.0, total), 1.0 if s0 < 60 else 0.0,
            prog / max(1.0, L) * 10, fill / max(1.0, L) * 10, len(t) / 10.0]


FEATURE_NAMES_V2 = ["本句语速", "局部语速", "句长", "前置停顿", "后置停顿",
                    "相对位置", "是否开头", "进度词", "废话词", "字数",
                    "画面变化", "音量", "是否高光",
                    "画面峰值", "镜头切换", "音量波动"]

# 推理和训练都必须用这个函数，否则模型学到的东西对不上
def seg_feature_matrix(data, names=None):
    """按 index 数据算出每句的特征矩阵。

    data 需要：segments、analysis.diffs、analysis.audio_1s
    特征 = 这句话说了什么（语速/停顿/长度）+ 当时画面怎么样（变化/切换/音量）
    """
    segs = data.get("segments") or []
    if not segs:
        return None
    md = seg_motion_db(data)
    if md is None:
        motion = [0.0] * len(segs)
        db = [-60.0] * len(segs)
        cuts = [0.0] * len(segs)
        dbstd = [0.0] * len(segs)
    else:
        motion, db, cuts, dbstd = md
    hl_th = (sum(motion) / len(motion) * 2.0) if motion else 999.0
    rows = []
    for i in range(len(segs)):
        rows.append(_seg_feature_row_v2(segs, i, motion, db, hl_th,
                                        cuts, dbstd))
    return np.array(rows, dtype=np.float64)


def seg_motion_db(data):
    """从索引里取每句对应的画面变化和音量（没有就返回 None）。

    索引里本来就有 diffs（每 0.5 秒的画面变化）和 audio_1s（每秒音量），
    以前模型压根没用它们——所以"画面重要但話少"的段落会被误剪。
    """
    segs = data.get("segments") or []
    an = data.get("analysis") or {}
    diffs = an.get("diffs") or []
    audio = an.get("audio_1s") or []
    if not segs or (not len(diffs or []) and not len(audio or [])):
        return None
    dur = float(data.get("duration") or 1.0) or 1.0
    hop_d = dur / max(1, len(diffs)) if diffs is not None and len(diffs) > 0 else 0.0
    hop_a = dur / max(1, len(audio)) if audio is not None and len(audio) > 0 else 0.0
    motion, db, cutcnt, dbstd = [], [], [], []
    for a, b, _t in segs:
        m = 0.0
        if diffs is not None and len(diffs) > 0 and hop_d > 0:
            i0 = max(0, int(a / hop_d))
            i1 = min(len(diffs), max(i0 + 1, int(b / hop_d) + 1))
            vals = [float(x) for x in diffs[i0:i1] if isinstance(x, (int, float))]
            m = (sum(vals) / len(vals)) if vals else 0.0
        level = -60.0
        if audio is not None and len(audio) > 0 and hop_a > 0:
            j0 = max(0, int(a / hop_a))
            j1 = min(len(audio), max(j0 + 1, int(b / hop_a) + 1))
            vals = [float(x) for x in audio[j0:j1] if isinstance(x, (int, float))]
            level = max(vals) if vals else -60.0
        # 镜头切换：相邻采样点变化超过阈值就算一次
        nc = 0
        if diffs and hop_d > 0:
            i0 = max(0, int(a / hop_d))
            i1 = min(len(diffs), max(i0 + 1, int(b / hop_d) + 1))
            seg_d = [float(x) for x in diffs[i0:i1] if isinstance(x, (int, float))]
            nc = sum(1 for k in range(1, len(seg_d))
                     if abs(seg_d[k] - seg_d[k - 1]) > 8.0)
        sd = 0.0
        if audio and hop_a > 0:
            j0 = max(0, int(a / hop_a))
            j1 = min(len(audio), max(j0 + 1, int(b / hop_a) + 1))
            seg_a = [float(x) for x in audio[j0:j1] if isinstance(x, (int, float))]
            if len(seg_a) > 1:
                mu2 = sum(seg_a) / len(seg_a)
                sd = (sum((x - mu2) ** 2 for x in seg_a) / len(seg_a)) ** 0.5
        motion.append(m)
        db.append(level)
        cutcnt.append(float(nc))
        dbstd.append(sd)
    return motion, db, cutcnt, dbstd


def _seg_feature_row_v2(segs, i, motion, db, hl_th, cuts=None, dbstd=None):
    """10 个文本/时间特征 + 6 个画面/声音特征。"""
    base = _seg_feature_row(segs, i)
    m = float(motion[i]) if i < len(motion) else 0.0
    d = float(db[i]) if i < len(db) else -60.0
    c = float(cuts[i]) if cuts is not None and i < len(cuts) else 0.0
    v = float(dbstd[i]) if dbstd is not None and i < len(dbstd) else 0.0
    peak = float(motion[i]) if i < len(motion) else 0.0
    return base + [min(m, 60.0) / 30.0, (max(-60.0, d) + 60.0) / 60.0,
                   1.0 if m >= hl_th else 0.0,
                   min(peak, 90.0) / 45.0, min(c, 10.0) / 10.0,
                   min(v, 20.0) / 20.0]


def model_card_scores(data, model):
    """用模型给每张卡片打分：取卡片覆盖范围内句子的最高分。

    卡片是几十秒一块，模型是按句训练的，所以取"这段时间里最有价值的那句"，
    而不是平均——一段里只要有一句干货，这段就值得留。
    """
    segs = data.get("segments") or []
    cards = data.get("cards") or []
    if llm_gateway.api_enabled():
        return [0.5] * len(cards)
    if not segs or not cards:
        return None
    stored = data.get("scores") or []
    if stored and len(stored) == len(segs):
        # 分析阶段已经算好，直接用，省得每次剪辑重算
        score = stored
        out = []
        for c in cards:
            a, b2 = float(c["start"]), float(c["end"])
            best = 0.0
            for k in range(len(segs)):
                if segs[k][1] <= a or segs[k][0] >= b2:
                    continue
                if score[k] > best:
                    best = float(score[k])
            out.append(best)
        return out
    if not model:
        return None
    mu = np.array(model["mean"], dtype=np.float64)
    sd = np.array(model["std"], dtype=np.float64) + 1e-9
    w = np.array(model["weights"], dtype=np.float64)
    b = float(model["bias"])
    dim = len(mu)
    if dim > 10:
        # 和训练完全同一套特征（画面/音量/切换都在里面）
        X = seg_feature_matrix(data)
        if X is None:
            return None
        if X.shape[1] != dim:      # 模型维度对不上就退回语速，别乱算
            return None
    else:
        X = np.array([_seg_feature_row(segs, i) for i in range(len(segs))],
                     dtype=np.float64)
    z = ((X - mu) / sd) @ w + b
    score = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
    out = []
    j = 0
    for c in cards:
        a, b2 = float(c["start"]), float(c["end"])
        best = 0.0
        for k in range(len(segs)):
            if segs[k][1] <= a or segs[k][0] >= b2:
                continue
            if score[k] > best:
                best = float(score[k])
        out.append(best)
    return out


def score_cards(cards, conf):
    """给每张卡片打一个"值不值得留"的分。

    实测（用一份人工剪辑成片当答案）：语速（字/秒）是唯一有效的特征，
    准确率、召回都在 0.5 左右；再加关键词/音量/画面变化都不升反降。
    所以默认只用语速，其它的留着当可调项。
    """
    if llm_gateway.api_enabled():
        return [0.5] * len(cards)
    sel = conf.get("select") or {}
    w_cps = float(sel.get("weight_cps", 1.0))
    w_prog = float(sel.get("weight_prog", 0.0))
    out = []
    for c in cards:
        L = max(0.1, float(c["end"]) - float(c["start"]))
        t = (c.get("text_fixed") or c.get("text") or "").strip()
        cps = len(t) / L
        prog = sum(t.count(w) for w in PROGRESS_WORDS) / (L / 60.0)
        out.append(w_cps * cps + w_prog * prog)
    return out


def select_cards(data, conf, target_seconds):
    """按分数从高到低挑卡片，直到凑够目标时长；返回保留的卡片列表。"""
    _require_local_content_selection()
    cards = data.get("cards") or []
    if not cards:
        return []
    sel = conf.get("select") or {}
    min_card = float(sel.get("min_card", 4.0))
    cand = [c for c in cards
            if float(c["end"]) - float(c["start"]) >= min_card]
    if not cand:
        cand = list(cards)
    model = load_model()
    ms = model_card_scores(data, model) if model else None
    if ms:
        # cards 和 cand 顺序一致（cand 是 cards 的子集）。这里重新按 cand 取分。
        by_id = {id(c): v for c, v in zip(cards, ms)}
        scores = [by_id.get(id(c), 0.0) for c in cand]
    else:
        scores = score_cards(cand, conf)
    order = sorted(range(len(cand)), key=lambda i: -scores[i])
    kept = []
    acc = 0.0
    limit = float(target_seconds) if target_seconds else float("inf")
    for i in order:
        if acc >= limit:
            break
        kept.append(cand[i])
        acc += float(cand[i]["end"]) - float(cand[i]["start"])
    kept.sort(key=lambda c: float(c["start"]))
    return kept


def subtract(segments, spans):
    """从保留片段里减掉若干区间（重复内容用）。

    segments = [(a,b), ...] 按时间排好；spans = [(x,y), ...] 要扔掉的部分。
    """
    if not spans:
        return list(segments)
    out = []
    for a, b in segments:
        cur = [(float(a), float(b))]
        for x, y in spans:
            nxt = []
            for c, d in cur:
                if y <= c or x >= d:
                    nxt.append((c, d))
                    continue
                if c < x:
                    nxt.append((c, x))
                if y < d:
                    nxt.append((y, d))
            cur = nxt
        out.extend((round(c, 2), round(d, 2)) for c, d in cur if d - c >= 0.3)
    return out


def intersect(segments, keep_iv):
    """把保留片段和卡片区间求交集。"""
    out = []
    for a, b in segments:
        for ka, kb in keep_iv:
            lo, hi = max(a, ka), min(b, kb)
            if hi - lo >= 0.3:
                out.append((lo, hi))
    out.sort()
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] <= 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def keep_segments(cuts, duration):
    """把"要剪的区间"取反，得到保留片段。"""
    segs = []
    cur = 0.0
    for a, b in sorted(cuts):
        a, b = max(0.0, a), min(duration, b)
        if a - cur >= 0.05:
            segs.append((cur, a))
        cur = max(cur, b)
    if duration - cur >= 0.05:
        segs.append((cur, duration))
    return segs


# --------------------------------------------------------------------------
# 二、转场判定（硬切优先）
# --------------------------------------------------------------------------
def _hw():
    """硬件解码参数（GPU 解码）。失败就退回软解，绝不让它把出片弄挂。"""
    try:
        import index as _ix
        return _ix.hwaccel_args()
    except Exception:  # noqa: BLE001
        return []


def _grab_frames(src, times, width=160, workers=8):
    """并行抽若干帧，返回 {时间: 图片数组}。"""
    import cv2
    from concurrent.futures import ThreadPoolExecutor
    tmpdir = tempfile.mkdtemp(prefix="seam_", dir=indexer.temp_root())
    frame_cache = None
    frame_evidence = {}
    try:
        import video_frame_cache
        frame_cache = video_frame_cache.VideoFrameCache(src)
    except Exception:  # noqa: BLE001
        pass

    def one(t):
        if frame_cache is not None:
            try:
                img, evidence = frame_cache.exact_frame_at(t, width=width,
                                                           quality=5)
                if img is not None:
                    return t, img, evidence
            except Exception:  # noqa: BLE001
                pass
        out = os.path.join(tmpdir, "f_%08.2f.jpg" % t)
        _base = [indexer.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
        _tail = ["-ss", "%.3f" % t, "-i", src, "-frames:v", "1",
                 "-vf", "scale=%d:-2" % width, "-q:v", "5", out]
        r = subprocess.run(_base + _hw() + _tail,
                           capture_output=True, creationflags=_NO_WIN)
        if r.returncode != 0 or not os.path.isfile(out):
            # 有些素材/显卡组合不支持硬件解码（比如 10bit 4:2:2），退回软解再来一次
            r = subprocess.run(_base + _tail,
                               capture_output=True, creationflags=_NO_WIN)
        if r.returncode != 0 or not os.path.isfile(out):
            return t, None, {"precision": "unavailable", "reused": False}
        # 中文路径下 cv2.imread 读不出来（返回 None），用 imdecode
        try:
            import numpy as _np
            buf = _np.fromfile(out, dtype=_np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
        except OSError:
            img = None
        try:
            os.remove(out)
        except OSError:
            pass
        return t, img, {"precision": "legacy_source_seek", "reused": False}

    res = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for t, img, evidence in ex.map(one, sorted(set(times))):
                res[t] = img
                frame_evidence[t] = evidence
    finally:
        # 以前这里没清理，每判一次转场就在临时目录留一个空目录，
        # 实测攒了 65 个。顺手删掉。
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
    _grab_frames.last_evidence = frame_evidence
    return res


_grab_frames.last_evidence = {}


def _hist_distance(img_a, img_b):
    """两张图的 HSV 直方图卡方距离，0=一样，越大越不像。"""
    import cv2
    if img_a is None or img_b is None:
        return 1.0
    ha = cv2.calcHist([cv2.cvtColor(img_a, cv2.COLOR_BGR2HSV)], [0, 1], None,
                      [30, 32], [0, 180, 0, 256])
    hb = cv2.calcHist([cv2.cvtColor(img_b, cv2.COLOR_BGR2HSV)], [0, 1], None,
                      [30, 32], [0, 180, 0, 256])
    cv2.normalize(ha, ha, 0, 1, cv2.NORM_MINMAX)
    cv2.normalize(hb, hb, 0, 1, cv2.NORM_MINMAX)
    # 实测三种距离：
    #   卡方    —— 几乎全部饱和到 1.0，没法用
    #   巴氏    —— 相邻卡中位 0.19 / 远隔 0.60，可用但分离度一般
    #   相关系数 —— 相邻卡中位 0.05 / 远隔 0.73，分离度最好，选它
    r = cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL)
    return float(max(0.0, 1.0 - r))


def _voice_near(data, t, margin=1.0):
    """t 附近 margin 秒内有没有人在说话。"""
    an = data.get("analysis") or {}
    for a, b in an.get("runs") or []:
        if b >= t - margin and a <= t + margin:
            return True
    return False


_SHOT_CACHE = {}


def shot_cuts_of(data, conf=None, should_cancel=None):
    """取这份素材的镜头切换点（缓存；按 路径+大小+修改时间 键）。"""
    src = (data or {}).get("source") or ""
    if not src or not os.path.isfile(src):
        return []
    try:
        key = (src, os.path.getsize(src), int(os.path.getmtime(src)))
    except OSError:
        return []
    if key in _SHOT_CACHE:
        return _SHOT_CACHE[key]
    # 落盘缓存：同一素材只抽一次帧（抽 4018 帧要 80 秒，出片每次都算太浪费）
    cache_path = ""
    idx_dir = (data or {}).get("index_dir") or ""
    if idx_dir and os.path.isdir(idx_dir):
        cache_path = os.path.join(idx_dir, "shot_cuts.json")
        try:
            if os.path.isfile(cache_path):
                import json as _json
                with open(cache_path, encoding="utf-8") as fh:
                    blob = _json.load(fh)
                if (blob.get("size") == key[1] and blob.get("mtime") == key[2]
                        and blob.get("source") == src):
                    cuts = [float(x) for x in blob.get("cuts") or []]
                    _SHOT_CACHE[key] = cuts
                    return cuts
        except Exception:  # noqa: BLE001
            pass
    try:
        import frames
        res = frames.analyze(src, fps=2.0, width=192, thr=0.45,
                             should_cancel=should_cancel)
        # cuts 是 numpy 数组：不能写 `or []`，会抛 "truth value ambiguous"
        # 然后被 except 吞掉，表现为吸附永远 0 刀（踩过）
        _arr = res.get("cuts")
        cuts = [float(x) for x in (_arr if _arr is not None else [])]
    except Exception:  # noqa: BLE001
        cuts = []
    _SHOT_CACHE[key] = cuts
    if cache_path:
        try:
            import json as _json
            with open(cache_path, "w", encoding="utf-8") as fh:
                _json.dump({"source": src, "size": key[1], "mtime": key[2],
                            "cuts": cuts[:4000]}, fh)
        except Exception:  # noqa: BLE001
            pass
    return cuts


def seam_shot_gap(cuts, t):
    """该接缝离最近的镜头切换点有多远（秒）；没有镜头点返回很大值。"""
    if not cuts:
        return 9.9
    import bisect
    i = bisect.bisect_left(cuts, t)
    cand = [c for c in (cuts[i - 1] if i > 0 else None,
                        cuts[i] if i < len(cuts) else None) if c is not None]
    return min(abs(c - t) for c in cand) if cand else 9.9


def snap_seams_to_shots(data, spans, tol=0.6, min_len=0.8):
    """把每个接缝挪到最近的镜头切换点；挪不动的保持原样。

    返回 (新片段, 对齐率)。只做"接缝"层面的微调：每段至少保留 min_len 秒。
    """
    cuts = shot_cuts_of(data)
    spans = [[float(a), float(b)] for a, b in spans]
    if not cuts or len(spans) < 2:
        return [(a, b) for a, b in spans], (0, max(1, len(spans) - 1))
    aligned = 0
    for k in range(len(spans) - 1):
        a_end, b_start = spans[k][1], spans[k + 1][0]
        # 只在"原刀口"附近找：分别贴 A 的结尾和 B 的开头，
        # 不能贴两段间隔的中点（间隔大时会把删掉的内容捡回来）
        # 必须两个边界都贴近同一个镜头点才动手：
        # 否则只贴一头，另一头会被拉长（间隔 44 秒时曾把删掉的内容全捡回来）
        c = min(cuts, key=lambda x: abs(x - a_end))
        if abs(c - a_end) > tol or abs(c - b_start) > tol + 0.25:
            continue
        target = c
        if target - spans[k][0] < min_len or spans[k + 1][1] - target < min_len:
            continue
        # A 收在镜头点上，B 从镜头点 +80ms 开始：
        # 如果两段首尾正好相接，后面合并逻辑会把它们并成一段，这一刀就没了。
        spans[k][1] = target
        spans[k + 1][0] = target + 0.08
        aligned += 1
    # 并掉可能重叠/相邻的段
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    total = max(1, len(spans) - 1)
    return [(a, b) for a, b in merged], (aligned, total)

def parse_time(txt):
    """解析 "40" / "00:40" / "1:02:03" → 秒。"""
    parts = [p for p in str(txt).strip().split(":") if p != ""]
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    if len(vals) == 1:
        return vals[0]
    if len(vals) == 2:
        return vals[0] * 60 + vals[1]
    if len(vals) == 3:
        return vals[0] * 3600 + vals[1] * 60 + vals[2]
    return None


def manual_drops_of(data, path=None):
    """读"手动丢弃.txt"（一行一个区间），返回 [(start, end, 原文)]。"""
    p = path or (data or {}).get("drop_file") or ""
    if not p:
        d = (data or {}).get("index_dir") or ""
        if d:
            p = os.path.join(d, "手动丢弃.txt")
    if not p or not os.path.isfile(p):
        return []
    out = []
    for line in open(p, encoding="utf-8", errors="replace"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s2 = s.replace("，", ",").replace("~", "-").replace("—", "-")
        parts = [x for x in s2.replace(",", "-").split("-") if x.strip()]
        if len(parts) != 2:
            continue
        a, b = parse_time(parts[0]), parse_time(parts[1])
        if a is None or b is None or b <= a:
            continue
        out.append((float(a), float(b), line.strip()))
    return out


def write_drop_suggestions(data, drops, path=None):
    """把自动找到的"疑似重录"写成建议清单（不自动生效）。"""
    d = (data or {}).get("index_dir") or ""
    p = path or (os.path.join(d, "建议丢弃.txt") if d else "")
    if not p:
        return None
    lines = ["# 疑似重录 / 建议丢弃（拿去人工确认）",
             "# 确认要丢的，把这一行复制到 手动丢弃.txt（同目录），只保留 a-b 部分",
             ""]
    for x in drops or []:
        if "a_start" in x:
            lines.append("%.1f-%.1f   或   %.1f-%.1f   # %s"
                         % (x["a_start"], x["a_end"], x["b_start"], x["b_end"],
                            x.get("why", "")))
        else:
            lines.append("%.1f-%.1f   # %s" % (x.get("start", 0), x.get("end", 0),
                                               x.get("why", "")))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return p

def seam_out_times(pieces):
    """算出每个接缝在成片时间轴上的位置（叠化片取中点，硬切取片间边界）。"""
    out, acc = [], 0.0
    prev_bridge = False
    for pc in pieces:
        if pc[0] == "bridge":
            dur = min(pc[1][1] - pc[1][0], pc[2][1] - pc[2][0])
            out.append(acc + dur / 2.0)
            acc += dur
            prev_bridge = True
        else:
            dur = pc[2] - pc[1]
            if out and not prev_bridge:
                out.append(acc)          # 硬切：正好在片尾
            acc += dur
            if not out or len(out) < 1:
                pass
            prev_bridge = False
    return out


def build_sequence_timeline(pieces, tracks=None, fixed_tracks=None,
                            free_tracks=None, piece_offsets=None, source=None):
    """Build a Premiere-style lane map for the media actually rendered.

    The legacy ``timeline`` can represent only one source interval per output
    interval.  Crossfades and audio bridges have two simultaneous sources, so
    subtitles and delivery QC need a lane-aware mapping instead.
    """
    tracks = [int(value) for value in (tracks or [0])]
    fixed_tracks = [int(value) for value in (
        fixed_tracks if fixed_tracks is not None else tracks)]
    free_tracks = [int(value) for value in (free_tracks or [])]
    offsets = list(piece_offsets or [])
    rows = []
    cursor = 0.0

    def mapping(lane, source_start, source_end, output_start, output_end,
                role="primary", track=None):
        item = {
            "lane": lane, "role": str(role),
            "output_start": round(float(output_start), 6),
            "output_end": round(float(output_end), 6),
            "source_start": round(float(source_start), 6),
            "source_end": round(float(source_end), 6),
            "rate": 1.0,
        }
        if track is not None:
            item["source_track"] = int(track)
        return item

    for index, piece in enumerate(pieces or []):
        if not piece:
            continue
        kind = str(piece[0])
        start = cursor
        video = []
        audio = []
        transition = None
        if kind == "chapter":
            duration = max(0.0, float(piece[1]))
            end = start + duration
            transition = {"kind": "chapter", "synthetic": True}
        elif kind == "trim":
            source_start, source_end = float(piece[1]), float(piece[2])
            duration = max(0.0, source_end - source_start)
            end = start + duration
            video.append(mapping("V1", source_start, source_end,
                                 start, end, "primary"))
            for track in fixed_tracks:
                audio.append(mapping("A%d" % (track + 1), source_start,
                                     source_end, start, end, "primary", track))
            free_start = (float(offsets[index]) if index < len(offsets)
                          else source_start)
            for track in free_tracks:
                audio.append(mapping("A%d" % (track + 1), free_start,
                                     free_start + duration, start, end,
                                     "continuous", track))
        elif kind in ("bridge", "audio_bridge"):
            left = (float(piece[1][0]), float(piece[1][1]))
            right = (float(piece[2][0]), float(piece[2][1]))
            left_duration = max(0.0, left[1] - left[0])
            right_duration = max(0.0, right[1] - right[0])
            duration = (min(left_duration, right_duration)
                        if kind == "audio_bridge"
                        else max(left_duration, right_duration))
            end = start + duration
            if kind == "bridge":
                video.extend([
                    mapping("V1", left[0], left[1], start, end, "outgoing"),
                    mapping("V1", right[0], right[1], start, end, "incoming"),
                ])
            else:
                video.append(mapping("V1", right[0], right[1],
                                     start, end, "primary"))
            for track in fixed_tracks:
                lane = "A%d" % (track + 1)
                audio.extend([
                    mapping(lane, left[0], left[1], start, end,
                            "outgoing", track),
                    mapping(lane, right[0], right[1], start, end,
                            "incoming", track),
                ])
            free_start = (float(offsets[index]) if index < len(offsets)
                          else right[0])
            for track in free_tracks:
                audio.append(mapping("A%d" % (track + 1), free_start,
                                     free_start + duration, start, end,
                                     "continuous", track))
            transition = {
                "kind": kind,
                "video": "crossfade" if kind == "bridge" else "hard_cut",
                "audio": "crossfade",
                "duration": round(duration, 6),
            }
        else:
            raise ValueError("unknown render piece: %r" % (piece,))
        rows.append({
            "id": "piece_%05d" % (index + 1), "kind": kind,
            "output_start": round(start, 6),
            "output_end": round(end, 6),
            "duration": round(duration, 6),
            "video": video, "audio": audio,
            "transition": transition,
        })
        cursor = end

    source_info = None
    if source:
        source_path = os.path.abspath(os.fspath(source))
        try:
            stat = os.stat(source_path)
            source_info = {"path": source_path, "size": int(stat.st_size),
                           "mtime_ns": int(stat.st_mtime_ns)}
        except OSError:
            source_info = {"path": source_path}
    payload = {
        "schema": "smartcut.sequence-timeline", "schema_version": 2,
        "source": source_info, "duration": round(cursor, 6),
        "selected_audio_tracks": tracks,
        "pieces": rows,
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    payload["sequence_revision"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _subtitle_piece_rows(pieces, timeline):
    """Pair each legacy source mapping with its actual render-piece index.

    Synthetic chapter cards occupy a ``pieces`` slot but intentionally have
    no legacy timeline row.  Enumerating ``timeline`` and indexing ``pieces``
    with the same number therefore shifts every subtitle after the first
    chapter card onto the wrong render segment.  Keep the two coordinate
    spaces explicit and fail closed if their cardinalities ever diverge.
    """
    piece_indices = [index for index, piece in enumerate(pieces or [])
                     if piece and str(piece[0]) != "chapter"]
    rows = list(timeline or [])
    if len(piece_indices) != len(rows):
        raise ValueError(
            "subtitle timeline/piece mismatch: %d source rows for %d media pieces"
            % (len(rows), len(piece_indices)))
    result = []
    for piece_index, row in zip(piece_indices, rows):
        try:
            output_start, source_start, duration = (
                float(row[0]), float(row[1]), float(row[2]))
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError("invalid subtitle timeline row: %r" % (row,)) from exc
        result.append((piece_index, output_start, source_start, duration))
    return result

def judge_transitions(segments, src, data, conf, verbose=True):
    """给每个接缝判定 硬切 还是 叠化。"""
    t = conf["transition"]
    seams = []
    for i in range(len(segments) - 1):
        a_end = segments[i][1]
        b_start = segments[i + 1][0]
        seams.append({"i": i, "ta": max(0.0, a_end - 0.10), "tb": b_start + 0.10,
                      "gap": max(0.0, b_start - a_end)})
    if not seams:
        return []

    frames = _grab_frames(src, [s["ta"] for s in seams] + [s["tb"] for s in seams])
    shot_cuts = shot_cuts_of(data)
    out = []
    for s in seams:
        d = _hist_distance(frames.get(s["ta"]), frames.get(s["tb"]))
        audio_ok = s["gap"] <= t["audio_gap_max"]
        # 几乎没剪掉东西时，两边本来就是连着的，直接硬切
        visual_ok = (s["gap"] < 0.6) or (d < t.get("corr_threshold", 0.55))
        # 接缝附近有没有人在说话
        near_voice = _voice_near(data, s["ta"], t.get("voice_margin", 1.0)) or \
                     _voice_near(data, s["tb"], t.get("voice_margin", 1.0))
        shot_gap = seam_shot_gap(shot_cuts, (s["ta"] + s["tb"]) / 2.0)
        if shot_gap <= 0.30:
            # 这一刀正好落在原片的镜头切换点上：本来就该切，硬切最自然
            kind, why = "cut", "落在原片镜头切换点（±%.2fs），自然硬切" % shot_gap
        elif near_voice and d >= float(t.get("vfade_min_hist", 0.55)):
            # 画面跳得厉害但中间有台词：视频叠 0.22 秒盖住跳变，
            # 音频只做 40ms 交叉（听不出叠），人声不会被糊掉。
            kind, why = "vfade", "画面跳变（差异 %.2f）+ 有台词 → 短叠化，人声不叠" % d
        elif near_voice:
            # 叠化会把两句话叠在一起 0.5 秒，听不清；声音清楚比画面顺滑重要
            kind, why = "cut", "接缝附近有台词且画面接近，硬切"
        elif visual_ok and audio_ok:
            kind, why = "cut", "画面连贯 + 声音连贯"
        elif visual_ok and not audio_ok:
            if t.get("visual_only", "cut") == "cut":
                kind, why = "cut", "画面连贯（声音断开 %.1fs，叠化救不了，仍硬切）" % s["gap"]
            else:
                kind, why = "crossfade", "画面连贯但声音断开"
        else:
            kind, why = "crossfade", "画面跳变（差异 %.2f）+ 无人声" % d
        s.update({"kind": kind, "why": why, "hist": round(d, 3),
                  "shot_gap": round(shot_gap, 2),
                  "visual_ok": visual_ok, "audio_ok": audio_ok,
                  "frame_cache_evidence": [
                      (_grab_frames.last_evidence or {}).get(s["ta"]),
                      (_grab_frames.last_evidence or {}).get(s["tb"]),
                  ]})
        out.append(s)
        if verbose:
            print("  接缝 %2d  %8.2fs → %8.2fs  %s  (%s)"
                  % (s["i"] + 1, s["ta"], s["tb"], kind, why))
    return out


# --------------------------------------------------------------------------
# 三、渲染
# --------------------------------------------------------------------------
_NVENC_CACHE = {}


def _has_nvenc(ffmpeg):
    """真的试编一小段。只看 -encoders 列表是不够的：
    本机 ffmpeg 9.1 的 nvenc 要求驱动 610.00+，实际装的是 591.86，
    列表里有、但一编就报错。所以必须实跑一次。"""
    if ffmpeg in _NVENC_CACHE:
        return _NVENC_CACHE[ffmpeg]
    ok = False
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=black:s=256x144:d=0.2:r=30",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WIN)
        ok = r.returncode == 0
        if not ok:
            msg = (r.stderr or "").strip().splitlines()
            print("  GPU 编码不可用（%s），改用 libx264" % (msg[-1][:80] if msg else "未知原因"))
    except Exception:  # noqa: BLE001
        ok = False
    _NVENC_CACHE[ffmpeg] = ok
    return ok


def probe_track_levels(src, seconds=12.0):
    """量每条音轨的电平，用来判断哪条是空的、哪条有用。

    返回 [{"ordinal":0,"label":"aac 立体声","db":-22.3,"silent":False}, ...]
    """
    import shutil as _sh
    import tempfile as _tf
    info = indexer.probe_streams(src)
    streams = info.get("audio_streams") or []
    dur = float(info.get("duration") or 0)
    # 只量开头不准：开场常常很安静，会把有用的轨误判成空轨。
    # 取三个位置各量一段，用最大值代表这条轨"最响的时候有多响"。
    spots = [max(0.0, dur * f) for f in (0.15, 0.45, 0.75)] if dur > 60 else [0.0]
    step = min(float(seconds), 8.0)
    out = []
    tmpdir = _tf.mkdtemp(prefix="trk_")
    try:
        for st in streams:
            db = -180.0
            for si, off in enumerate(spots):
                wav = os.path.join(tmpdir, "t%d_%d.wav" % (st["ordinal"], si))
                cmd = [indexer.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
                if off > 0:
                    cmd += ["-ss", "%.2f" % off]
                cmd += ["-t", str(step), "-i", src,
                        "-map", "0:a:%d" % st["ordinal"],
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav]
                subprocess.run(cmd, capture_output=True, creationflags=_NO_WIN)
                if os.path.isfile(wav):
                    a = indexer.read_wav_mono(wav)
                    if a.size:
                        rms = float(np.sqrt((a ** 2).mean()))
                        db = max(db, 20 * float(np.log10(max(rms, 1e-9))))
            out.append({"ordinal": st["ordinal"],
                        "label": st.get("label", "音轨 %d" % (st["ordinal"] + 1)),
                        "db": round(db, 1),
                        "silent": db < -55.0})
    finally:
        _sh.rmtree(tmpdir, ignore_errors=True)
    return out


def auto_tracks(src):
    """没指定音轨时的兜底：全部保留（混音时静音轨没有贡献，不会出错）。

    以前按电平自动判断"静音"并踢掉，实测会误判（游戏声 -62 dB 被当静音），
    结果玩家选了轨却没声音。现在统一交给玩家勾选。
    """
    lv = probe_track_levels(src)
    return [x["ordinal"] for x in lv] or [0]


_ENC_CACHE = {}
_ENC_WHY = {}


def _nvenc_ok(ff):
    """显卡编码能不能用（按 ffmpeg 路径缓存，探测一次）。

    以前缓存只写死一个 "ok" 键：谁第一个来探测就决定了全局结果。
    自检那边传的是裸名字 "ffmpeg"，PATH 上没有这东西 → FileNotFoundError
    → 判成"显卡编码不可用"，而且这个 False 会被后面真正出片时复用，
    整场都把能用的 NVENC 当成坏的一直用 libx264（11-22 踩过）。
    现在：裸名字先按 find_ffmpeg() 解析成真实路径，再按路径分开缓存。
    """
    try:
        if not ff or not os.path.isfile(str(ff)):
            import index as _ix
            ff = _ix.find_ffmpeg()
    except Exception:  # noqa: BLE001
        pass
    key = str(ff)
    if key in _ENC_CACHE:
        return _ENC_CACHE[key]
    ok, why = False, ""
    try:
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=black:s=256x256:d=1", "-c:v", "h264_nvenc",
                            "-f", "null", "-"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=_NO_WIN)
        ok = (r.returncode == 0)
        if not ok:
            lines = (r.stderr or "").strip().splitlines()
            why = lines[-1][:120] if lines else "ffmpeg 返回 %d" % r.returncode
    except FileNotFoundError:
        why = "找不到 ffmpeg（%s）" % ff
    except Exception as exc:  # noqa: BLE001
        why = str(exc)[:120]
    _ENC_CACHE[key] = ok
    _ENC_WHY[key] = why
    return ok


def nvenc_reason(ff=None) -> str:
    """上次探测失败的原因，给人看的。"""
    if ff is not None:
        return _ENC_WHY.get(str(ff), "")
    return next((v for v in _ENC_WHY.values() if v), "")


def _video_enc_args(ff, r, crf):
    """视频编码参数：优先显卡（NVENC），不可用则 libx264。"""
    want = str(r.get("encoder", "auto")).lower()
    if want == "auto":
        # 预览/低分辨率走显卡（快、不占 CPU）；1080p 成片仍用 x264（体积省一半）
        try:
            h = int(str(r.get("scale", "") or "0:-2").split(":")[0])
        except ValueError:
            h = 0
        want = "nvenc" if 0 < h <= 1280 else "x264"
    if want == "nvenc" and _nvenc_ok(ff):
        # NVENC 用恒定质量：cq 越小越好；x264 的 crf 与 cq 大致差 4~6
        # NVENC：p1 最快；cq 比 x264 的 crf 高 4~6 才接近同等画质
        cq = int(r.get("nvenc_cq", max(10, min(35, int(crf) + 6))))
        return ["-c:v", "h264_nvenc", "-preset", str(r.get("nvenc_preset", "p1")),
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", str(r.get("x264_preset", "veryfast")),
            "-pix_fmt", "yuv420p"]


def _piece_cmd(ff, src, piece, out, r, fps, scale, tracks=None, ass_name=None,
               free_tracks=None, free_offset=0.0, track_kinds=None):
    """生成单个片段的 ffmpeg 命令。tracks 要保留的音轨序号列表。

    ass_name：本片段要烧的字幕文件（相对路径，配合 cwd 用，避开 Windows 盘符转义）。

    free_tracks / free_offset（子枫 2026-11-23 「时间线是给软件看的」）：
      分轨时间线的核心——**说话轨跟着剪辑走，其他轨不跟着走**。
        · tracks 里的轨：用本片段的源区间（-ss a -t d），会跟着切点跳
        · free_tracks 里的轨：用成片时间（free_offset 起），从源片连续取，
          所以人声切掉 20 秒，游戏声还是连续的——这就是天然的“贯穿底噪”
      不传 free_tracks 时行为和以前完全一样（向后兼容）。
    """
    preset = str(r.get("x264_preset", "veryfast"))
    vf_tail = ",fps=%d,settb=AVTB,format=yuv420p,setsar=1" % fps
    if scale:
        vf_tail += ",scale=%s" % scale
    if ass_name:
        # 放在 scale 之后：字号按成片分辨率算，预览和成品看起来一样大
        vf_tail += ",ass=%s" % ass_name
    af_tail = (",asetpts=PTS-STARTPTS,"
               "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")
    enc = _video_enc_args(ff, r, r["crf"]) + [
        "-c:a", "aac", "-b:a", r["audio_bitrate"], "-ar", "48000", "-ac", "2"]
    tracks = tracks if tracks else [0]
    mix = bool(r.get("mix_tracks", True)) and len(tracks) > 1
    # 章节卡：纯黑画面 + 静音，长度由 piece[1] 给。用 lavfi 直接生成，不读源片。
    if piece[0] == "chapter":
        dur = max(0.1, float(piece[1]))
        width, height = 1920, 1080
        if scale and ":" in str(scale):
            parts = str(scale).split(":")[:2]
            try:
                cand = [int(x) for x in parts]
                if all(16 <= v <= 4096 for v in cand):
                    width, height = cand
            except (TypeError, ValueError):
                pass
        command = ([ff, "-hide_banner", "-loglevel", "error", "-y"] + _hw() +
                   ["-f", "lavfi", "-i",
                    "color=c=black:s=%dx%d:r=%d:d=%.3f" % (width, height, fps, dur),
                    "-f", "lavfi", "-t", "%.3f" % dur, "-i",
                    "anullsrc=r=48000:cl=stereo"])
        command += ["-map", "0:v:0"]
        if mix:
            command += ["-map", "1:a:0"]
        else:
            # Concat-demuxed pieces must have matching stream layouts.  When
            # tracks are kept separate, emit one silent stream per selected
            # track so the chapter card cannot collapse the later tracks.
            if len(tracks) == 1:
                graph = "[1:a:0]anull[silent0]"
            else:
                graph = "[1:a:0]asplit=%d%s" % (
                    len(tracks), "".join("[silent%d]" % index
                                          for index in range(len(tracks))))
            command += ["-filter_complex", graph]
            for index in range(len(tracks)):
                command += ["-map", "[silent%d]" % index]
        return command + enc + [out], dur
    kind = piece[0]
    if kind == "trim":
        a, b = piece[1], piece[2]
        dur = max(0.05, b - a)
        # 防爆音微淡化。剪映规范是 15ms，但实测 30ms 还不够——"层"这种字的
        # 韵母会延续到切点之后，硬切听起来就是"卡了个字"。段尾给 50ms 让韵母
        # 平滑消失，段首仍 15ms（起音短一点不容易吞字）。
        _in_d = min(0.015, dur * 0.25)
        _out_d = min(0.050, dur * 0.25)
        fade = (",afade=t=in:st=0:d=%.3f,"
                "afade=t=out:st=%.3f:d=%.3f"
                % (_in_d, max(0.0, dur - _out_d), _out_d))
        # 章节卡前后要"渐暗→黑→渐亮"，不能直接黑屏（那是硬切，比原来更刺眼）。
        # 画面淡 0.30 秒、声音淡 0.16 秒——声音淡得短一些，免得把最后一个字的
        # 尾音也吃掉（这是 v139 学到的：字完整不等于尾音完整）。
        _tag = str(piece[3]) if len(piece) > 3 else ""
        _vf = float(piece[4]) if len(piece) > 4 else 0.30
        _af = float(piece[5]) if len(piece) > 5 else 0.16
        # 接缝音色对齐：piece[6]=段首 dB，piece[7]=段尾 dB（按接缝另一端算出来的）
        _eq_head = float(piece[6]) if len(piece) > 6 else None
        _eq_tail = float(piece[7]) if len(piece) > 7 else None
        # 段尾"高频收敛"：像"层"这种字带 c 声母的送气（4~5kHz 能量很足），
        # 硬切时送气会留在接缝上，听起来像"咔"的小尖峰。默认压 10dB，
        # 但如果有按接缝算出来的值（_eq_tail），就用那个值。
        _eq_hz = float(r.get("seam_tone_hz", 4000.0) or 4000.0)
        _eq_tail_db = (_eq_tail if _eq_tail not in (None, 0.0)
                       else float(r.get("seam_tone_tail_db", -10.0) or -10.0))
        _eq_head_db = _eq_head if _eq_head is not None else 0.0
        if dur > 0.40 and abs(_eq_tail_db) > 0.1:
            # 用 highshelf（搁架）而不是 equalizer：送气是 2~8kHz 的宽带噪声，
            # 窄带 Q=1.2 只压 4kHz 附近，实测压了 15dB 高频占比只降 0.05。
            # 搁架一次压掉 3kHz 以上全部，才压得住。
            fade += (",highshelf=f=3000:g=%.1f:"
                     "enable='between(t,%.3f,%.3f)'"
                     % (_eq_tail_db, max(0.0, dur - 0.25), dur))
        # 段首"高频对齐"：接缝另一端比这边更亮的话，这边开头也抬一点，
        # 让两端往同一个值靠——只压一边的话，另一端会留下一个"相对尖峰"。
        if dur > 0.40 and abs(_eq_head_db) > 0.1:
            fade += (",highshelf=f=3000:g=%.1f:"
                     "enable='between(t,0,%.3f)'"
                     % (_eq_head_db, min(0.25, dur * 0.5)))
        vf_extra, af_extra = "", ""
        if "out" in _tag and dur > _vf + 0.05:
            vf_extra += ",fade=t=out:st=%.3f:d=%.3f" % (dur - _vf, _vf)
            af_extra += ",afade=t=out:st=%.3f:d=%.3f" % (dur - _af, _af)
        if "in" in _tag and dur > _vf + 0.05:
            vf_extra += ",fade=t=in:st=0:d=%.3f" % _vf
            af_extra += ",afade=t=in:st=0:d=%.3f" % _af
        graph = "[0:v]setpts=PTS-STARTPTS%s%s[v]" % (vf_tail, vf_extra)
        maps = ["-map", "[v]"]
        # 有章节淡出时不再叠 80ms 的接缝淡出（两个 afade 同区间会衰减过快）
        _audio_fade = af_extra if af_extra else fade
        if mix:
            # 多条轨混成一条：播放器只播第一条音频流，
            # 分轨输出会让人以为"没人声"，其实是没播第二条。
            # 每条轨独立加效果（分割同步、效果独立）：
            # 游戏/音乐轨压高频（音效刺耳），说话轨只做事位对齐。
            _kinds_map = track_kinds or {}
            for k, t in enumerate(tracks):
                _k = _kinds_map.get(str(t)) or _kinds_map.get(int(t)) or {}
                _extra = ""
                if str(_k.get("kind")) in ("other", "dense"):
                    _g = float(r.get("game_track_gain_db", -3.0) or 0.0)
                    if abs(_g) > 0.1:
                        _extra = ",highshelf=f=3000:g=%.1f" % _g
                graph += ";[0:a:%d]asetpts=PTS-STARTPTS%s%s[m%d]" % (
                    t, af_tail, _extra, k)
            _n_in = len(tracks)
            # free_tracks：这些轨**不跟着剪辑走**，按成片时间从源片连续取。
            # 效果等价于“贯穿底噪”，而且是天然的（不需要另外铺一条）。
            for k, t in enumerate(free_tracks or []):
                graph += ";[1:a:%d]asetpts=PTS-STARTPTS[mf%d]" % (t, k)
                _n_in += 1
            ins = "".join("[m%d]" % k for k in range(len(tracks)))
            ins += "".join("[mf%d]" % k for k in range(len(free_tracks or [])))
            # normalize=1：按输入数平均。normalize=0 时两条相干音轨直接相加，
            # 峰值翻倍 +6dB——实测 8.7 秒因此出现 -4dB 尖峰（原片只有 -10.9dB），
            # 成片复审一直判"突兀尖峰"。
            graph += (";%samix=inputs=%d:duration=longest:normalize=1,"
                      "alimiter=limit=0.9:level=false%s[a]"
                      % (ins, _n_in, _audio_fade))
            maps += ["-map", "[a]"]
        else:
            for k, t in enumerate(tracks):
                graph += (";[0:a:%d]asetpts=PTS-STARTPTS%s%s[a%d]"
                          % (t, af_tail, _audio_fade, k))
            for k in range(len(tracks)):
                maps += ["-map", "[a%d]" % k]
            # 分轨输出时 free_tracks 也要出：这些轨留在原地，
            # 单独作为一条输出流（比如游戏轨），不跟着剪辑跳。
            for k, t in enumerate(free_tracks or []):
                graph += "; [1:a:%d]asetpts=PTS-STARTPTS[af%d]" % (t, k)
                maps += ["-map", "[af%d]" % k]
        _inputs = ["-ss", "%.3f" % a, "-t", "%.3f" % dur, "-i", src]
        # free_tracks 要按成片时间另取一份源片（第二路输入 index=1）。
        if free_tracks:
            _fo = max(0.0, float(free_offset))
            _inputs += ["-ss", "%.3f" % _fo, "-t", "%.3f" % dur,
                        "-i", src]
        return ([ff, "-hide_banner", "-loglevel", "error", "-y"] + _hw() +
                _inputs + ["-filter_complex", graph] + maps + enc + [out]), (b - a)

    # bridge：把前段尾巴和后段开头叠化成一个过渡小片
    (a1, b1), (a2, b2) = piece[1], piece[2]
    audio_d = float(piece[3]) if len(piece) > 3 and piece[3] else None
    d1, d2 = b1 - a1, b2 - a2
    d = min(d1, d2)
    # audio_bridge：画面直接切到后段（硬切），声音在接缝处交叉混合。
    # 这是专业剪辑里的 J-cut/L-cut：观众看到的是新画面，听到的是两句话
    # 叠在一起的尾音——像人说话时自然的重叠，而不是"咔"一声断开。
    if piece[0] == "audio_bridge":
        ad = min(float(audio_d or d), d)
        graph = "[1:v]setpts=PTS-STARTPTS[v]"
        maps = ["-map", "[v]"]
        if mix:
            for k, t in enumerate(tracks):
                graph += (";[0:a:%d]asetpts=PTS-STARTPTS[x%d];"
                          "[1:a:%d]asetpts=PTS-STARTPTS[y%d];"
                          "[x%d][y%d]acrossfade=d=%.3f:c1=qsin:c2=qsin,"
                          "apad,atrim=0:%.3f,aformat=sample_fmts=fltp:"
                          "sample_rates=48000:channel_layouts=stereo[m%d]"
                          % (t, k, t, k, k, k, ad, d, k))
            ins = "".join("[m%d]" % k for k in range(len(tracks)))
            graph += (";%samix=inputs=%d:duration=longest:normalize=1,"
                      "alimiter=limit=0.9[a]" % (ins, len(tracks)))
            maps += ["-map", "[a]"]
        else:
            for k, t in enumerate(tracks):
                graph += (";[0:a:%d]asetpts=PTS-STARTPTS[x%d];"
                          "[1:a:%d]asetpts=PTS-STARTPTS[y%d];"
                          "[x%d][y%d]acrossfade=d=%.3f:c1=qsin:c2=qsin,"
                          "apad,atrim=0:%.3f,aformat=sample_fmts=fltp:"
                          "sample_rates=48000:channel_layouts=stereo[a%d]"
                          % (t, k, t, k, k, k, ad, d, k))
            for k in range(len(tracks)):
                maps += ["-map", "[a%d]" % k]
        return ([ff, "-hide_banner", "-loglevel", "error", "-y"] + _hw() +
                ["-ss", "%.3f" % a1, "-t", "%.3f" % d1, "-i", src,
                 "-ss", "%.3f" % a2, "-t", "%.3f" % d2, "-i", src,
                 "-filter_complex", graph] + maps + enc + [out]), d

    # 音频交叉长度可以和视频叠化分开：vfade 时视频 0.22s、音频只有 40ms
    ad = d if audio_d is None else min(float(audio_d), d)
    graph = (
        "[0:v]setpts=PTS-STARTPTS%s[v0];"
        "[1:v]setpts=PTS-STARTPTS%s[v1];"
        "[v0][v1]xfade=transition=fade:duration=%.3f:offset=0[vo]"
        % (vf_tail, vf_tail, d))
    maps = ["-map", "[vo]"]
    if mix:
        for k, t in enumerate(tracks):
            graph += (";[0:a:%d]asetpts=PTS-STARTPTS%s[x%d];"
                      "[1:a:%d]asetpts=PTS-STARTPTS%s[y%d];"
                      "[x%d][y%d]acrossfade=d=%.3f:c1=qsin:c2=qsin,apad,atrim=0:%.3f[m%d]"
                      % (t, af_tail, k, t, af_tail, k, k, k, ad, d, k))
        ins = "".join("[m%d]" % k for k in range(len(tracks)))
        graph += (";%samix=inputs=%d:duration=longest:normalize=1,alimiter=limit=0.9[a]"
                  % (ins, len(tracks)))
        maps += ["-map", "[a]"]
    else:
        for k, t in enumerate(tracks):
            graph += (";[0:a:%d]asetpts=PTS-STARTPTS%s[x%d];"
                      "[1:a:%d]asetpts=PTS-STARTPTS%s[y%d];"
                      "[x%d][y%d]acrossfade=d=%.3f:c1=qsin:c2=qsin,apad,atrim=0:%.3f[a%d]"
                      % (t, af_tail, k, t, af_tail, k, k, k, ad, d, k))
        for k in range(len(tracks)):
            maps += ["-map", "[a%d]" % k]
    return ([ff, "-hide_banner", "-loglevel", "error", "-y"] + _hw() +
            ["-ss", "%.3f" % a1, "-t", "%.3f" % d1, "-i", src,
             "-ss", "%.3f" % a2, "-t", "%.3f" % d2, "-i", src,
             "-filter_complex", graph] + maps + enc + [out]), (d1 + d2 - d)


def _single_decode_eligibility(pieces, free_tracks=None):
    """Return whether one sequential source decode can reproduce ``pieces``.

    Hard-cut trims and synthetic chapter cards can be expressed as one
    ``select/aselect`` or branch timeline.  Bridge transitions are rejected
    here because FFmpeg's single-input graph has not passed repeatable audible
    sequence validation on the bundled build; render() therefore routes those
    jobs through the known compatibility renderer.  A free audio lane also
    follows a different clock.  This production gate is intentionally fail
    closed: re-ordered, overlapping, or unverified ranges never silently change
    meaning.
    """
    if free_tracks:
        return False, "自由音轨使用独立时间钟"
    source_ranges = []
    for piece in pieces or []:
        kind = str(piece[0]) if piece else ""
        if kind == "chapter":
            continue
        try:
            if kind == "trim" and len(piece) >= 3:
                clean = [(float(piece[1]), float(piece[2]))]
            elif kind in ("bridge", "audio_bridge") and len(piece) >= 3:
                return False, "叠化过渡的单输入音频时间轴未通过可听性验证"
            else:
                return False, "%s 不是受支持的时间线片段" % (kind or "未知")
        except (IndexError, TypeError, ValueError):
            return False, "片段时间无效"
        if any(end <= start for start, end in clean):
            return False, "存在空片段"
        source_ranges.extend(clean)
    if not source_ranges:
        return False, "没有源画面片段"
    return True, ""


def _single_decode_rows(pieces):
    """Map source trim pieces onto their absolute output intervals."""
    rows = []
    output_time = 0.0
    for piece_index, piece in enumerate(pieces or []):
        kind = str(piece[0]) if piece else ""
        if kind == "chapter":
            output_time += max(0.0, float(piece[1]))
            continue
        if kind != "trim":
            raise ValueError("single-decode timeline only accepts trim/chapter")
        start, end = float(piece[1]), float(piece[2])
        duration = max(0.0, end - start)
        rows.append({
            "piece_index": piece_index,
            "piece": piece,
            "source_start": start,
            "source_end": end,
            "output_start": output_time,
            "output_end": output_time + duration,
            "duration": duration,
        })
        output_time += duration
    return rows, output_time


def _filter_number(value):
    """Stable compact decimal for FFmpeg filter expressions."""
    text = ("%.6f" % float(value)).rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _select_expr(rows):
    # Commas belong to the expression function, not the filter chain.
    return "+".join(
        "gte(t\\,%s)*lt(t\\,%s)" % (
            _filter_number(row["source_start"]),
            _filter_number(row["source_end"]))
        for row in rows)


def _remap_pts_expr(rows):
    """Piecewise source PTS -> output PTS, preserving chapter-card gaps."""
    tail = None
    for row in reversed(rows):
        mapped = "PTS*TB-%s+%s" % (
            _filter_number(row["source_start"]),
            _filter_number(row["output_start"]))
        if tail is None:
            tail = mapped
        else:
            tail = "if(lt(PTS*TB\\,%s)\\,%s\\,%s)" % (
                _filter_number(row["source_end"]), mapped, tail)
    return "(%s)/TB" % (tail or "0")


def _single_piece_effects(rows, r, *, audio=False):
    """Recreate per-piece fades/EQ on the remapped continuous timeline."""
    filters = []
    gain_windows = []
    for row in rows:
        piece = row["piece"]
        duration = float(row["duration"])
        output_start = float(row["output_start"])
        output_end = float(row["output_end"])
        tag = str(piece[3]) if len(piece) > 3 else ""
        video_fade = float(piece[4]) if len(piece) > 4 else 0.30
        audio_fade = float(piece[5]) if len(piece) > 5 else 0.16
        eq_head = float(piece[6]) if len(piece) > 6 else 0.0
        eq_tail = float(piece[7]) if len(piece) > 7 else 0.0
        if not audio:
            if "out" in tag and duration > video_fade + 0.05:
                gain_windows.append((output_end - video_fade, output_end,
                                     "out"))
            if "in" in tag and duration > video_fade + 0.05:
                gain_windows.append((output_start,
                                     output_start + video_fade, "in"))
            continue

        # The legacy renderer substitutes the chapter fade for the ordinary
        # 15/50 ms anti-click pair on tagged pieces; preserve that exactly.
        if "out" in tag and duration > audio_fade + 0.05:
            gain_windows.append((output_end - audio_fade, output_end, "out"))
        elif "in" in tag and duration > audio_fade + 0.05:
            gain_windows.append((output_start,
                                 output_start + audio_fade, "in"))
        else:
            fade_in = min(0.015, duration * 0.25)
            fade_out = min(0.050, duration * 0.25)
            gain_windows.extend([
                (output_start, output_start + fade_in, "in"),
                (max(output_start, output_end - fade_out), output_end, "out"),
            ])
        if duration > 0.40:
            tail_db = (eq_tail if abs(eq_tail) > 0.1
                       else float(r.get("seam_tone_tail_db", -10.0) or -10.0))
            if abs(tail_db) > 0.1:
                filters.append(
                    "highshelf=f=3000:g=%s:enable='between(t,%s,%s)'" % (
                        _filter_number(tail_db),
                        _filter_number(max(output_start, output_end - 0.25)),
                        _filter_number(output_end)))
            if abs(eq_head) > 0.1:
                filters.append(
                    "highshelf=f=3000:g=%s:enable='between(t,%s,%s)'" % (
                        _filter_number(eq_head), _filter_number(output_start),
                        _filter_number(min(output_end, output_start + 0.25))))
    if gain_windows:
        variable = "t" if audio else "T"
        gain = "1"
        for start, end, kind in reversed(gain_windows):
            duration = max(1e-6, end - start)
            if kind == "in":
                local = "clip((%s-%s)/%s,0,1)" % (
                    variable, _filter_number(start), _filter_number(duration))
            else:
                local = "clip((%s-%s)/%s,0,1)" % (
                    _filter_number(end), variable, _filter_number(duration))
            gain = "if(between(%s,%s,%s),%s,%s)" % (
                variable, _filter_number(start), _filter_number(end),
                local, gain)
        if audio:
            # Unlike afade, a conditional volume returns to gain=1 outside its
            # own window, so one piece's fade cannot mute later pieces.
            filters.insert(0, "volume='%s':eval=frame" % gain)
        else:
            # geq exposes frame time as T and lets us apply the same local gain
            # without fade's permanent before/after black state.
            filters[:0] = [
                "format=gbrp",
                "geq=r='r(X,Y)*%s':g='g(X,Y)*%s':b='b(X,Y)*%s'" %
                (gain, gain, gain),
                "format=yuv420p",
            ]
    return filters


def _single_select_timeline_cmd(ff, src, pieces, out, r, fps, scale,
                                tracks=None, ass_name=None, track_kinds=None,
                                output_size=None, stem_outputs=None,
                                progress_path=None):
    """Build one FFmpeg process with exactly one source-media input.

    The source is decoded sequentially once.  ``select``/``aselect`` discard
    removed intervals, a piecewise PTS expression closes cuts and leaves gaps
    for chapter cards, and synthetic black/silent lanes fill those gaps.
    """
    if any(str(piece[0]) not in ("trim", "chapter") for piece in pieces or []):
        raise ValueError("select timeline only accepts trim/chapter")
    rows, total_duration = _single_decode_rows(pieces)
    tracks = [int(value) for value in (tracks or [0])]
    mix = bool(r.get("mix_tracks", True)) and len(tracks) > 1
    width, height = output_size or (0, 0)
    if not width or not height:
        try:
            media = indexer.probe_streams(src)
            width, height = out_resolution({"render": r}, media)
        except Exception:  # noqa: BLE001
            width, height = 1920, 1080
    width, height = max(16, int(width)), max(16, int(height))
    # Encoders require even dimensions.
    width -= width % 2
    height -= height % 2
    select_expr = _select_expr(rows)
    pts_expr = _remap_pts_expr(rows)
    max_source_end = max(row["source_end"] for row in rows)
    chapter_intervals = []
    chapter_cursor = 0.0
    for piece in pieces or []:
        if str(piece[0]) == "chapter":
            chapter_duration = max(0.0, float(piece[1]))
            chapter_intervals.append(
                (chapter_cursor, chapter_cursor + chapter_duration))
            chapter_cursor += chapter_duration
        else:
            chapter_cursor += max(0.0, float(piece[2]) - float(piece[1]))

    video_chain = (
        "[0:v:0]fps=%d,select='%s',settb=AVTB,setpts='%s'" %
        (int(fps), select_expr, pts_expr))
    if scale:
        video_chain += ",scale=%s" % scale
    video_chain += ",format=yuv420p,setsar=1"
    video_effects = _single_piece_effects(rows, r, audio=False)
    if video_effects:
        video_chain += "," + ",".join(video_effects)
    video_chain += "[selected_v]"
    graph = [video_chain]
    graph.append(
        "[1:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[base_v]")
    composed = (
        "[base_v][selected_v]overlay=shortest=0:eof_action=pass:repeatlast=0")
    if chapter_intervals:
        disabled = "+".join(
            "between(t,%s,%s)" % (
                _filter_number(start), _filter_number(end))
            for start, end in chapter_intervals)
        # framesync otherwise keeps the last selected frame alive across a PTS
        # hole.  Explicitly disabling the overlay makes the synthetic black
        # base visible for the exact chapter-card interval.
        composed += ":enable='not(%s)'" % disabled
    if ass_name:
        composed += ",ass=%s" % ass_name
    composed += "[video_out]"
    graph.append(composed)

    audio_effects = _single_piece_effects(rows, r, audio=True)
    kinds = track_kinds or {}
    stem_outputs = list(stem_outputs or [])
    if stem_outputs and len(stem_outputs) != len(tracks):
        raise ValueError("stem_outputs must align with selected tracks")
    audio_labels = []
    stem_labels = []
    for position, track in enumerate(tracks):
        chain = (
            "[0:a:%d]aresample=48000,aselect='%s',asetpts='%s',"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
            % (track, select_expr, pts_expr))
        kind = kinds.get(str(track)) or kinds.get(track) or {}
        if str(kind.get("kind")) in ("other", "dense"):
            gain = float(r.get("game_track_gain_db", -3.0) or 0.0)
            if abs(gain) > 0.1:
                chain += ",highshelf=f=3000:g=%s" % _filter_number(gain)
        if audio_effects:
            chain += "," + ",".join(audio_effects)
        label = "selected_a%d" % position
        chain += "[%s]" % label
        graph.append(chain)
        audio_labels.append(label)

    if stem_outputs:
        for position, label in enumerate(list(audio_labels)):
            main_label = "main_a%d" % position
            stem_label = "stem_a%d" % position
            graph.append("[%s]asplit=2[%s][%s]" % (
                label, main_label, stem_label))
            audio_labels[position] = main_label
            stem_labels.append(stem_label)

    main_silence_count = (1 if mix or len(audio_labels) == 1
                          else len(audio_labels))
    silence_count = main_silence_count + len(stem_labels)
    if silence_count == 1:
        silence_labels = ["2:a"]
    else:
        silence_labels = ["timeline_silence%d" % position
                          for position in range(silence_count)]
        graph.append("[2:a]asplit=%d%s" % (
            silence_count,
            "".join("[%s]" % label for label in silence_labels)))
    silence_position = 0

    maps = ["-map", "[video_out]"]
    if mix:
        joined = "".join("[%s]" % label for label in audio_labels)
        # normalize=1：按输入数平均。
        # normalize=0 时两条相干音轨会直接相加，峰值翻倍 +6dB——
        # 实测这条六轨素材混出来 8.7 秒有个 -4dB 的尖峰（原片只有 -10.9dB），
        # 成片复审一直拿它当"突兀尖峰"扣分。
        graph.append(
            "%samix=inputs=%d:duration=longest:normalize=1,"
            "alimiter=limit=0.9:level=false[selected_mix]" %
            (joined, len(audio_labels)))
        graph.append(
            "[%s][selected_mix]amix=inputs=2:duration=first:normalize=0,"
            "atrim=0:%s[audio_out]" % (
                silence_labels[silence_position],
                _filter_number(total_duration)))
        silence_position += 1
        maps += ["-map", "[audio_out]"]
    elif len(audio_labels) == 1:
        graph.append(
            "[%s][%s]amix=inputs=2:duration=first:normalize=0,"
            "atrim=0:%s[audio_out]" %
            (silence_labels[silence_position], audio_labels[0],
             _filter_number(total_duration)))
        silence_position += 1
        maps += ["-map", "[audio_out]"]
    else:
        for position, label in enumerate(audio_labels):
            output_label = "audio_out%d" % position
            graph.append(
                "[%s][%s]amix=inputs=2:duration=first:normalize=0,"
                "atrim=0:%s[%s]" % (
                    silence_labels[silence_position], label,
                    _filter_number(total_duration), output_label))
            silence_position += 1
            maps += ["-map", "[%s]" % output_label]

    for position, stem_label in enumerate(stem_labels):
        output_label = "stem_out%d" % position
        graph.append(
            "[%s][%s]amix=inputs=2:duration=first:normalize=0,"
            "atrim=0:%s[%s]" % (
                silence_labels[silence_position], stem_label,
                _filter_number(total_duration), output_label))
        silence_position += 1

    enc = _video_enc_args(ff, r, r["crf"]) + [
        "-c:a", "aac", "-b:a", r["audio_bitrate"], "-ar", "48000",
        "-ac", "2"]
    command = [
        ff, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
        "-t", _filter_number(max_source_end), "-i", src,
        "-f", "lavfi", "-i",
        "color=c=black:s=%dx%d:r=%d:d=%s" % (
            width, height, int(fps), _filter_number(total_duration)),
        "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=stereo:d=%s" % _filter_number(total_duration),
        "-filter_complex", ";".join(graph),
    ]
    if progress_path:
        command += ["-progress", progress_path, "-nostats"]
    command += maps + enc + [
        "-t", _filter_number(total_duration), "-movflags", "+faststart", out]
    for position, stem_path in enumerate(stem_outputs):
        command += [
            "-map", "[stem_out%d]" % position, "-vn", "-c:a", "pcm_s16le",
            "-ar", "48000", "-ac", "2", "-t",
            _filter_number(total_duration), os.fspath(stem_path)]
    return command, total_duration


def _local_video_chain(source_label, start, end, output_label, fps, scale,
                       piece=None):
    duration = max(0.001, float(end) - float(start))
    chain = (
        "[%s]trim=start=%s:end=%s,setpts=PTS-STARTPTS,fps=%d,"
        "settb=AVTB,format=yuv420p,setsar=1" % (
            source_label, _filter_number(start), _filter_number(end), int(fps)))
    if scale:
        chain += ",scale=%s" % scale
    if piece is not None:
        tag = str(piece[3]) if len(piece) > 3 else ""
        video_fade = float(piece[4]) if len(piece) > 4 else 0.30
        if "out" in tag and duration > video_fade + 0.05:
            chain += ",fade=t=out:st=%s:d=%s" % (
                _filter_number(duration - video_fade),
                _filter_number(video_fade))
        if "in" in tag and duration > video_fade + 0.05:
            chain += ",fade=t=in:st=0:d=%s" % _filter_number(video_fade)
    return chain + "[%s]" % output_label


def _local_audio_chain(source_label, start, end, output_label, r,
                       piece=None, track_kind=None):
    duration = max(0.001, float(end) - float(start))
    chain = (
        "[%s]atrim=start=%s:end=%s,asetpts=PTS-STARTPTS,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo" % (
            source_label, _filter_number(start), _filter_number(end)))
    if str((track_kind or {}).get("kind")) in ("other", "dense"):
        gain = float(r.get("game_track_gain_db", -3.0) or 0.0)
        if abs(gain) > 0.1:
            chain += ",highshelf=f=3000:g=%s" % _filter_number(gain)
    if piece is not None:
        tag = str(piece[3]) if len(piece) > 3 else ""
        audio_fade = float(piece[5]) if len(piece) > 5 else 0.16
        eq_head = float(piece[6]) if len(piece) > 6 else 0.0
        eq_tail = float(piece[7]) if len(piece) > 7 else 0.0
        if "out" in tag and duration > audio_fade + 0.05:
            chain += ",afade=t=out:st=%s:d=%s" % (
                _filter_number(duration - audio_fade),
                _filter_number(audio_fade))
        elif "in" in tag and duration > audio_fade + 0.05:
            chain += ",afade=t=in:st=0:d=%s" % _filter_number(audio_fade)
        else:
            fade_in = min(0.015, duration * 0.25)
            fade_out = min(0.050, duration * 0.25)
            chain += ",afade=t=in:st=0:d=%s,afade=t=out:st=%s:d=%s" % (
                _filter_number(fade_in),
                _filter_number(max(0.0, duration - fade_out)),
                _filter_number(fade_out))
        if duration > 0.40:
            tail_db = (eq_tail if abs(eq_tail) > 0.1
                       else float(r.get("seam_tone_tail_db", -10.0) or -10.0))
            if abs(tail_db) > 0.1:
                chain += (",highshelf=f=3000:g=%s:"
                          "enable='between(t,%s,%s)'" % (
                              _filter_number(tail_db),
                              _filter_number(max(0.0, duration - 0.25)),
                              _filter_number(duration)))
            if abs(eq_head) > 0.1:
                chain += (",highshelf=f=3000:g=%s:"
                          "enable='between(t,0,%s)'" % (
                              _filter_number(eq_head),
                              _filter_number(min(duration, 0.25))))
    return chain + "[%s]" % output_label


def _single_branch_timeline_cmd(ff, src, pieces, out, r, fps, scale,
                                tracks=None, ass_name=None, track_kinds=None,
                                output_size=None, stem_outputs=None,
                                progress_path=None, filter_script_path=None):
    """One-source chapter timeline graph; unverified transitions fail closed."""
    eligible, reason = _single_decode_eligibility(pieces)
    if not eligible:
        raise ValueError(reason)
    tracks = [int(value) for value in (tracks or [0])]
    stem_outputs = list(stem_outputs or [])
    if stem_outputs and len(stem_outputs) != len(tracks):
        raise ValueError("stem_outputs must align with selected tracks")
    mix = bool(r.get("mix_tracks", True)) and len(tracks) > 1
    width, height = output_size or (0, 0)
    if not width or not height:
        try:
            media = indexer.probe_streams(src)
            width, height = out_resolution({"render": r}, media)
        except Exception:  # noqa: BLE001
            width, height = 1920, 1080
    width, height = max(16, int(width)), max(16, int(height))
    width -= width % 2
    height -= height % 2
    graph = []
    piece_video = []
    track_piece_audio = [[] for _track in tracks]
    piece_offsets = []
    max_source_end = 0.0
    total_duration = 0.0
    kinds = track_kinds or {}

    def track_kind(track):
        return kinds.get(str(track)) or kinds.get(track) or {}

    video_uses = sum(
        0 if str(piece[0]) == "chapter"
        else (2 if str(piece[0]) == "bridge" else 1)
        for piece in pieces or [])
    video_sources = ["source_v%d" % index for index in range(video_uses)]
    if video_uses == 1:
        graph.append("[0:v:0]null[%s]" % video_sources[0])
    elif video_uses > 1:
        graph.append("[0:v:0]split=%d%s" % (
            video_uses, "".join("[%s]" % label for label in video_sources)))
    video_source_index = 0
    audio_sources = {}
    audio_source_indices = {track: 0 for track in tracks}
    for track in tracks:
        uses = sum(
            0 if str(piece[0]) == "chapter"
            else (1 if str(piece[0]) == "trim" else 2)
            for piece in pieces or [])
        labels = ["source_a%d_%d" % (track, index)
                  for index in range(uses)]
        audio_sources[track] = labels
        if uses == 1:
            graph.append("[0:a:%d]anull[%s]" % (track, labels[0]))
        elif uses > 1:
            graph.append("[0:a:%d]asplit=%d%s" % (
                track, uses, "".join("[%s]" % label for label in labels)))

    def take_video_source():
        nonlocal video_source_index
        label = video_sources[video_source_index]
        video_source_index += 1
        return label

    def take_audio_source(track):
        position = audio_source_indices[track]
        audio_source_indices[track] += 1
        return audio_sources[track][position]

    for piece_index, piece in enumerate(pieces or []):
        kind = str(piece[0])
        video_label = "piece_v%d" % piece_index
        track_labels = []
        if kind == "chapter":
            duration = max(0.1, float(piece[1]))
            graph.append(
                "color=c=black:s=%dx%d:r=%d:d=%s,format=yuv420p[%s]" % (
                    width, height, int(fps), _filter_number(duration), video_label))
            for track_position, _track in enumerate(tracks):
                raw = "p%d_t%d_raw" % (piece_index, track_position)
                graph.append(
                    "anullsrc=r=48000:cl=stereo,atrim=0:%s,"
                    "asetpts=PTS-STARTPTS,"
                    "aformat=sample_fmts=fltp:sample_rates=48000:"
                    "channel_layouts=stereo[%s]" %
                    (_filter_number(duration), raw))
                track_labels.append(raw)
        elif kind == "trim":
            start, end = float(piece[1]), float(piece[2])
            duration = max(0.001, end - start)
            max_source_end = max(max_source_end, end)
            graph.append(_local_video_chain(
                take_video_source(), start, end, video_label, fps, scale,
                piece=piece))
            for track_position, track in enumerate(tracks):
                raw = "p%d_t%d_raw" % (piece_index, track_position)
                graph.append(_local_audio_chain(
                    take_audio_source(track), start, end, raw, r, piece=piece,
                    track_kind=track_kind(track)))
                track_labels.append(raw)
        else:
            (start1, end1), (start2, end2) = piece[1], piece[2]
            start1, end1 = float(start1), float(end1)
            start2, end2 = float(start2), float(end2)
            duration = min(end1 - start1, end2 - start2)
            audio_duration = min(
                float(piece[3]) if len(piece) > 3 and piece[3] else duration,
                duration)
            max_source_end = max(max_source_end, end1, end2)
            if kind == "audio_bridge":
                graph.append(_local_video_chain(
                    take_video_source(), start2, start2 + duration, video_label,
                    fps, scale))
            else:
                left_video = "p%d_v_left" % piece_index
                right_video = "p%d_v_right" % piece_index
                graph.append(_local_video_chain(
                    take_video_source(), start1, start1 + duration, left_video,
                    fps, scale))
                graph.append(_local_video_chain(
                    take_video_source(), start2, start2 + duration, right_video,
                    fps, scale))
                graph.append(
                    "[%s][%s]xfade=transition=fade:duration=%s:offset=0[%s]" % (
                        left_video, right_video, _filter_number(duration),
                        video_label))
            for track_position, track in enumerate(tracks):
                left_audio = "p%d_t%d_left" % (piece_index, track_position)
                right_audio = "p%d_t%d_right" % (piece_index, track_position)
                raw = "p%d_t%d_raw" % (piece_index, track_position)
                graph.append(_local_audio_chain(
                    take_audio_source(track), start1, start1 + duration,
                    left_audio, r))
                graph.append(_local_audio_chain(
                    take_audio_source(track), start2, start2 + duration,
                    right_audio, r))
                graph.append(
                    "[%s][%s]acrossfade=d=%s:c1=qsin:c2=qsin,"
                    "apad,atrim=0:%s,asetpts=PTS-STARTPTS,"
                    "aformat=sample_fmts=fltp:sample_rates=48000:"
                    "channel_layouts=stereo[%s]" % (
                        left_audio, right_audio,
                        _filter_number(audio_duration),
                        _filter_number(duration), raw))
                track_labels.append(raw)

        for track_position, label in enumerate(track_labels):
            track_piece_audio[track_position].append(label)
        piece_video.append(video_label)
        piece_offsets.append(total_duration)
        total_duration += duration

    graph.append(
        "%sconcat=n=%d:v=1:a=0[timeline_v_raw]" % (
            "".join("[%s]" % label for label in piece_video),
            len(piece_video)))
    graph.append("[timeline_v_raw]setpts=PTS-STARTPTS[timeline_v]")

    track_timeline_labels = []
    for track_position, labels in enumerate(track_piece_audio):
        output_label = "track_timeline%d" % track_position
        delayed_labels = []
        for piece_position, label in enumerate(labels):
            delay_samples = int(round(piece_offsets[piece_position] * 48000))
            delayed_label = "track%d_piece%d_delayed" % (
                track_position, piece_position)
            graph.append(
                "[%s]asettb=1/48000,asetpts=PTS-STARTPTS,"
                "adelay=delays=%dS|%dS:all=1[%s]" % (
                    label, delay_samples, delay_samples, delayed_label))
            delayed_labels.append(delayed_label)
        # Build the track timeline by delaying each isolated piece into its
        # sample-accurate position and summing the non-overlapping windows.
        # This avoids concat's audio-frame scheduling/DTS failure for single-
        # frame acrossfades while preserving one decode of the source input.
        graph.append(
            "%samix=inputs=%d:duration=longest:normalize=0,"
            "asettb=1/48000,asetpts=NB_CONSUMED_SAMPLES/SR/TB,"
            "aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[%s]" % (
                "".join("[%s]" % label for label in delayed_labels),
                len(delayed_labels), output_label))
        if not mix:
            # On this FFmpeg build an explicit pass-through boundary prevents
            # the audio-only timeline output from ending early after a
            # one-frame acrossfade.  Keep it before optional stem fan-out.
            ready_label = "track_timeline_ready%d" % track_position
            graph.append("[%s]anull[%s]" % (output_label, ready_label))
            output_label = ready_label
        track_timeline_labels.append(output_label)

    timeline_audio_labels = []
    stem_labels = []
    if mix:
        mix_track_labels = list(track_timeline_labels)
        if stem_outputs:
            mix_track_labels = []
            for track_position, label in enumerate(track_timeline_labels):
                mix_label = "mix_track%d" % track_position
                stem_label = "stem_out%d" % track_position
                graph.append("[%s]asplit=2[%s][%s]" % (
                    label, mix_label, stem_label))
                mix_track_labels.append(mix_label)
                stem_labels.append(stem_label)
        main_label = "timeline_a0"
        # 多轨主混音：normalize=1 按输入数平均，避免两条相干人声相加后
        # 峰值 +6dB（实测 8.7 秒尖峰就是这么来的）。
        graph.append(
            "%samix=inputs=%d:duration=longest:normalize=1,"
            "alimiter=limit=0.9:level=false,"
            "asettb=1/48000,asetpts=NB_CONSUMED_SAMPLES/SR/TB,"
            "aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[%s]" % (
                "".join("[%s]" % label for label in mix_track_labels),
                len(mix_track_labels), main_label))
        timeline_audio_labels = [main_label]
    else:
        timeline_audio_labels = list(track_timeline_labels)
        if stem_outputs:
            timeline_audio_labels = []
            for track_position, label in enumerate(track_timeline_labels):
                main_label = "timeline_a%d" % track_position
                stem_label = "stem_out%d" % track_position
                graph.append("[%s]asplit=2[%s][%s]" % (
                    label, main_label, stem_label))
                timeline_audio_labels.append(main_label)
                stem_labels.append(stem_label)
    video_out = "video_out"
    if ass_name:
        graph.append("[timeline_v]ass=%s[%s]" % (ass_name, video_out))
    else:
        graph.append("[timeline_v]null[%s]" % video_out)

    filter_graph = ";".join(graph)
    command = [
        ff, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
        "-t", _filter_number(max_source_end), "-i", src,
    ]
    if filter_script_path:
        with open(filter_script_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(filter_graph)
            handle.write("\n")
        command += ["-filter_complex_script", filter_script_path]
    else:
        command += ["-filter_complex", filter_graph]
    if progress_path:
        command += ["-progress", progress_path, "-nostats"]
    command += ["-map", "[%s]" % video_out]
    for label in timeline_audio_labels:
        command += ["-map", "[%s]" % label]
    command += _video_enc_args(ff, r, r["crf"]) + [
        "-c:a", "aac", "-b:a", r["audio_bitrate"], "-ar", "48000",
        "-ac", "2", "-t", _filter_number(total_duration),
        "-movflags", "+faststart", out]
    for position, stem_path in enumerate(stem_outputs):
        command += [
            "-map", "[%s]" % stem_labels[position], "-vn",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            "-t", _filter_number(total_duration), os.fspath(stem_path)]
    return command, total_duration


def _single_timeline_cmd(ff, src, pieces, out, r, fps, scale, tracks=None,
                         ass_name=None, track_kinds=None, output_size=None,
                         stem_outputs=None, progress_path=None,
                         filter_script_path=None):
    """Dispatch to the single-input hard-cut/chapter graph when verified."""
    eligible, reason = _single_decode_eligibility(pieces)
    if not eligible:
        raise ValueError(reason)
    # Aselect collapses audio timestamp holes on some FFmpeg builds; synthetic
    # chapter silence must therefore use the explicit branch+concat graph.
    if all(str(piece[0]) == "trim" for piece in pieces or []):
        command, duration = _single_select_timeline_cmd(
            ff, src, pieces, out, r, fps, scale, tracks=tracks,
            ass_name=ass_name, track_kinds=track_kinds,
            output_size=output_size, stem_outputs=stem_outputs,
            progress_path=progress_path)
        if filter_script_path and "-filter_complex" in command:
            position = command.index("-filter_complex")
            graph = command[position + 1]
            with open(filter_script_path, "w", encoding="utf-8",
                      newline="\n") as handle:
                handle.write(graph)
                handle.write("\n")
            command[position:position + 2] = [
                "-filter_complex_script", filter_script_path]
        return command, duration
    return _single_branch_timeline_cmd(
        ff, src, pieces, out, r, fps, scale, tracks=tracks,
        ass_name=ass_name, track_kinds=track_kinds,
        output_size=output_size, stem_outputs=stem_outputs,
        progress_path=progress_path, filter_script_path=filter_script_path)


def _concat_piece_cmd(ff, listfile, out):
    """Stream-copy rendered pieces while preserving all selected audio lanes."""
    return [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", listfile,
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-movflags", "+faststart", out,
    ]


class RenderCancelled(Exception):
    """用户中途取消出片。

    以前关窗口是对 QThread 直接 terminate()，线程池里还有几路 ffmpeg 在读盘，
    Python 最后化阶段会撞成 0xc0000005（logs/fatal.log 里那次就是）。
    改成协作取消：每段开工前看一眼标志，正在跑的几段跑完就收手。
    """


class _SeamAudioWindows:
    """Share one selected-track PCM index across all render seam checks."""

    def __init__(self, source, track, ffmpeg, cancel=None, on_log=print):
        self.source = source
        self.track = int(track)
        self.ffmpeg = ffmpeg
        self.cancel = cancel
        self.on_log = on_log
        self.cache = None
        self._fallback_logged = False

    def _cancel_check(self):
        if self.cancel is not None and self.cancel():
            raise RenderCancelled("已取消接缝音频检查")

    def __enter__(self):
        self._cancel_check()
        try:
            self.cache = audio_evidence_cache.AudioEvidenceCache(
                self.source, [self.track], sample_rate=16000,
                ffmpeg=self.ffmpeg, should_cancel=self.cancel)
            entry = self.cache.prepare_track(self.track)
            self._cancel_check()
            self.on_log("接缝音频：第 %d 条%s共享 PCM，后续边界只读缓存窗口"
                        % (self.track + 1,
                           "复用" if entry.get("reused") else "已建立"))
        except (audio_evidence_cache.AudioEvidenceCancelled,
                RenderCancelled) as exc:
            self.close()
            raise RenderCancelled("已取消接缝音频检查") from exc
        except Exception as exc:  # noqa: BLE001
            self._fallback(exc)
        return self

    def _fallback(self, error):
        # A failed index is disabled for this entire precheck.  In particular,
        # later windows must not repeatedly retry a failed full-track decode.
        self.close()
        if not self._fallback_logged:
            self._fallback_logged = True
            self.on_log("接缝共享 PCM 不可用，改用音频窗口：%s"
                        % str(error)[:100])

    def read(self, start, duration):
        self._cancel_check()
        start = max(0.0, float(start))
        duration = max(0.0, float(duration))
        if duration <= 0.0:
            return np.zeros(0, dtype=np.float32)
        if self.cache is not None:
            try:
                samples = self.cache.review_samples(
                    self.track, start, start + duration)
                self._cancel_check()
                return samples
            except (audio_evidence_cache.AudioEvidenceCancelled,
                    RenderCancelled) as exc:
                raise RenderCancelled("已取消接缝音频检查") from exc
            except Exception as exc:  # noqa: BLE001
                self._fallback(exc)
        self._cancel_check()
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", "%.3f" % start, "-t", "%.3f" % duration,
            "-i", self.source, "-map", "0:a:%d" % self.track,
            "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=_NO_WIN)
        try:
            while True:
                self._cancel_check()
                try:
                    output, error = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            self._cancel_check()
            if process.returncode != 0:
                raise RuntimeError((error or b"").decode(
                    "utf-8", "replace")[-400:] or "接缝音频窗口读取失败")
            return np.frombuffer(output, dtype="<i2").astype(np.float32) / 32768.0
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            raise

    def close(self):
        cache, self.cache = self.cache, None
        if cache is not None:
            cache.close()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def _render_seam_prechecks(source, work, seam_map, conf, tracks, ffmpeg,
                          data=None, cancel=None, on_log=print):
    """Read background/tone evidence from the same selected-track timeline."""
    transition = conf.get("transition") or {}
    render_conf = conf["render"]
    glue_seconds = float(transition.get("audio_glue", 0.15) or 0)
    card_seconds = float(transition.get("chapter_card", 0.0) or 0)
    card_gap = float(transition.get("chapter_card_gap", 20.0))
    check_band = glue_seconds > 0.02 or card_seconds > 0.05
    check_tone = bool(render_conf.get("seam_tone_align", True))
    glue_at, card_at, tone_eq = {}, {}, {}
    if len(work) <= 1 or not (check_band or check_tone):
        return glue_at, card_at, tone_eq
    track = int((tracks or [0])[0])
    with _SeamAudioWindows(source, track, ffmpeg, cancel, on_log) as audio:
        if check_band:
            try:
                words = []
                for segment in (data or {}).get("segment_evidence") or []:
                    for word in segment.get("words") or []:
                        try:
                            words.append((float(word["start"]),
                                          float(word["end"])))
                        except (KeyError, TypeError, ValueError):
                            continue
                words.sort()

                def tail_gap(end):
                    last = 0.0
                    for _start, stop in words:
                        if stop <= end + 1e-6 and stop > last:
                            last = stop
                    return max(0.0, end - last) if last > 0 else end

                def band(samples):
                    if samples.size < 256:
                        return -120.0
                    spec = np.abs(np.fft.rfft(
                        samples * np.hanning(len(samples))))
                    freq = np.fft.rfftfreq(len(samples), 1.0 / 16000)
                    mask = (freq >= 80.0) & (freq <= 800.0)
                    if not mask.any():
                        return -120.0
                    return 20 * np.log10(max(float(spec[mask].mean()), 1e-9))

                for position in range(len(work) - 1):
                    end, next_start = work[position][1], work[position + 1][0]
                    gap = abs(band(audio.read(end - 0.30, 0.30))
                              - band(audio.read(next_start, 0.30)))
                    if card_seconds > 0.05 and gap >= card_gap:
                        card_at[position] = card_seconds
                    elif glue_seconds > 0.02 and gap > 6.0:
                        seam = seam_map.get(position)
                        if seam and seam.get("kind") == "cut":
                            duration = min(glue_seconds, tail_gap(end))
                            if duration >= 0.06:
                                glue_at[position] = duration
                if card_at:
                    on_log("章节卡：%d 处接缝背景声差 ≥%.0f dB，改成分段处理（%.1f 秒黑场）"
                           % (len(card_at), card_gap, card_seconds))
                if glue_at:
                    on_log("声音胶合：%d 处接缝背景声差异大，交叉混合 150 毫秒"
                           % len(glue_at))
            except RenderCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                on_log("声音胶合/章节卡预判跳过：%s" % str(exc)[:80])
        if check_tone:
            try:
                window = float(render_conf.get("seam_tone_window", 0.25) or 0.25)

                def high_frequency(start, end):
                    samples = audio.read(start, max(0.05, end - start))
                    if samples.size < 512:
                        return 0.0
                    step = max(1, int(0.05 * 16000))
                    peak = 0.0
                    for first in range(0, max(1, samples.size - step), step):
                        piece = samples[first:first + step]
                        if piece.size < 128:
                            break
                        spec = np.abs(np.fft.rfft(piece * np.hanning(piece.size)))
                        freq = np.fft.rfftfreq(piece.size, 1.0 / 16000)
                        total = float(spec.sum()) + 1e-12
                        peak = max(peak, float(spec[freq > 1000.0].sum() / total))
                    return peak

                pairs = []
                for position in range(len(work) - 1):
                    first_start, first_end = work[position]
                    next_start, next_end = work[position + 1]
                    if first_end - first_start < 0.5 or next_end - next_start < 0.5:
                        continue
                    first_hi = high_frequency(first_end - window, first_end)
                    next_hi = high_frequency(next_start, next_start + window)
                    if first_hi < 0.06 and next_hi < 0.06:
                        continue
                    middle = (first_hi + next_hi) / 2.0
                    if abs(first_hi - next_hi) < 0.20:
                        continue
                    first_db = max(-18.0, min(6.0, (middle - first_hi) * 60.0))
                    next_db = max(-18.0, min(6.0, (middle - next_hi) * 60.0))
                    slot = tone_eq.setdefault(position, [0.0, 0.0])
                    slot[1] = round(min(slot[1], first_db) if slot[1] else first_db, 1)
                    next_slot = tone_eq.setdefault(position + 1, [0.0, 0.0])
                    next_slot[0] = round(
                        min(next_slot[0], next_db) if next_slot[0] else next_db, 1)
                    pairs.append((position + 1, first_hi, next_hi, first_db, next_db))
                if pairs:
                    on_log("接缝音色对齐：%d 处硬切两端差别大，已按中间值调整 EQ"
                           % len(pairs))
                    for position, first_hi, next_hi, first_db, next_db in pairs[:4]:
                        on_log("   接缝%d：高频 %.2f → %.2f，两端调 %+.1f / %+.1f dB"
                               % (position, first_hi, next_hi, first_db, next_db))
            except RenderCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                on_log("接缝音色对齐跳过：%s" % str(exc)[:80])
    return glue_at, card_at, tone_eq


def out_resolution(conf, data):
    """成片分辨率：有 scale 就按它算，否则跟素材一致。字幕排版要按这个来。"""
    w = int(data.get("width") or 1920)
    h = int(data.get("height") or 1080)
    scale = str((conf.get("render") or {}).get("scale") or "")
    head = scale.split(":")[0].strip()
    if head.isdigit() and int(head) > 0:
        nw = int(head)
        nh = max(2, int(round(nw * h / max(1, w) / 2.0) * 2))
        return nw, nh
    return w, h


def export_subs(data, out_path, timeline, style=None, res=None):
    """按成片时间轴把字幕导出到成片旁边（SRT 给平台 / ASS 给后期）。

    这是字幕能用的关键：成片是重排过的，索引里那份是原片时间，
    直接拿去烧会整片错位。timeline 由 render() 返回。
    """
    try:
        import subtitles as _subs
    except Exception:  # noqa: BLE001
        return {}
    segs = _subs.get_segments(data)
    mapped = _subs.remap(segs, timeline)
    if not mapped:
        return {}
    stem = os.path.splitext(out_path)[0]
    w, h = res or (1920, 1080)
    srt = stem + "_字幕.srt"
    ass = stem + "_字幕.ass"
    _subs.write_srt(data, srt, style=style, segments=mapped)
    _subs.write_ass(data, ass, style=style, width=w, height=h, segments=mapped)
    return {"srt": srt, "ass": ass, "n": len(mapped)}


def render(src, segments, seams, out_path, conf, on_log=print, on_progress=None,
           tracks=None, cancel=None, subs=None, sub_burn=False,
           sub_style=None, sub_res=None, bgm=False, bgm_conf=None, data=None):
    """按剪辑计划出片：分段并行编码 → concat 无损拼接。

    subs：原片时间轴的逐句字幕 [(start, end, text)]。
          给了就能做两件事——回一份「成片时间轴」给调用方（写 SRT/ASS），
          以及 sub_burn=True 时把字幕烧进画面（每段各自烧，不额外多一遍编码）。
    sub_res：成片分辨率 (w, h)，字幕排版按它算。
    """
    import time as _tm
    _t_start = _tm.time()
    _timing = {}

    def _mark(tag, extra=""):
        _timing[tag] = round(_tm.time() - _t_start, 1)
        on_log("⏱ %s：累计 %.1f 秒%s"
               % (tag, _timing[tag], ("（%s）" % extra) if extra else ""))

    if not segments:
        raise ValueError("没有可输出的片段")
    import shutil as _sh
    import tempfile as _tf
    from concurrent.futures import ThreadPoolExecutor

    ff = indexer.find_ffmpeg()
    r = conf["render"]
    # 帧率跟着素材走：以前写死 60，30 帧的素材会被补成 60 帧，
    # 体积翻倍还多一遍插帧，看着反而更糊。
    fps = 60
    try:
        _f = float((indexer.probe_streams(src).get("fps") or 0))
        if 20 <= _f <= 70:
            fps = 60 if _f >= 50 else (30 if _f >= 25 else int(round(_f)))
    except Exception:  # noqa: BLE001
        pass
    scale = r.get("scale") or ""
    if tracks is None:
        tracks = r.get("tracks")
    if not tracks:
        tracks = auto_tracks(src)
    # 保险：音轨序号必须真实存在（旧配置/手填值可能越界，越界会让滤镜整段失败）
    try:
        _n_audio = len((indexer.probe_streams(src).get("audio_streams") or []))
        if _n_audio:
            _valid = [int(t) for t in tracks if 0 <= int(t) < _n_audio]
            if _valid != list(tracks):
                on_log("音轨序号越界，已修正：%s → %s" % (list(tracks), _valid or [0]))
                tracks = _valid or [0]
    except Exception:
        pass
    if len(tracks) > 1 and r.get("mix_tracks", True):
        on_log("音轨：%s → 混音成一条"
               % ("、".join("第 %d 条" % (t + 1) for t in tracks)))
    else:
        on_log("音轨：%s"
               % ("、".join("第 %d 条" % (t + 1) for t in tracks)))

    # ---- 0.5 分轨时间线：哪些轨跟着剪辑走、哪些留在原地 ----
    # 子枫 2026-11-23：「时间线不是给我看的，是给软件看的，软件剪辑需要。」
    # 说话轨跟着切点走（话被删了它也要删），其他轨（游戏/音乐）留在原地：
    # 人声切掉 20 秒，游戏声照样连续——这是天然的「贯穿底噪」，
    # 不需要另外铺一条背景音。
    _track_kinds_map = (data or {}).get("track_kinds") or {}
    _free_tracks = []
    _fixed_tracks = list(tracks)
    on_log("  [调试] per_track=%r kinds=%d tracks=%r"
           % (r.get("per_track_timeline"),
              len((data or {}).get("track_kinds") or {}), list(tracks)))
    # 注意：分割必须**同步**（子枫 2026-11-23：「轨道虽然平行、更改也是独立的，
    # 但是分割要同步」）。所以这里默认**不用** free_tracks——所有轨一起切。
    # 独立的是“效果”，不是“分割点”。
    if bool(r.get("per_track_timeline", False)):
        try:
            _kinds = (data or {}).get("track_kinds") or {}
            if _kinds:
                _free = []
                for _t in tracks:
                    _k = _kinds.get(str(_t)) or _kinds.get(int(_t)) or {}
                    if str(_k.get("kind")) in ("other", "dense"):
                        _free.append(int(_t))
                _fixed = [int(_t) for _t in tracks if int(_t) not in _free]
                # 只有「确实有轨要跟着切」时才分行，否则行为和以前完全一样
                if _free and _fixed:
                    _free_tracks = _free
                    _fixed_tracks = _fixed
                    on_log("  分轨时间线：跟着剪的是 %s；留在原地的是 %s"
                           % ("、".join("第%d条" % (t + 1) for t in _fixed),
                              "、".join("第%d条" % (t + 1) for t in _free)))
        except Exception as _tl_err:  # noqa: BLE001
            on_log("  分轨时间线跳过：%s" % str(_tl_err)[:60])

    # ---- 1. 把片段拆成「普通段」和「叠化过渡片」 ----
    seam_map = {x["i"]: x for x in seams}
    work = [list(x) for x in segments]
    pieces = []
    i = 0
    n_cross = 0
    _fade_in_at = set()   # 章节卡后面那些段要渐亮（集合，多个章节卡互不覆盖）
    # 章节卡前后渐变的长度：画面 0.30 秒、声音 0.16 秒（声音短一些，别吞尾音）
    _vfade = float(conf.get("transition", {}).get(
        "chapter_fade_video", 0.30) or 0.30)
    _afade = float(conf.get("transition", {}).get(
        "chapter_fade_audio", 0.16) or 0.16)
    # 背景频段差和音色对齐共用选中首轨的 PCM 索引；不再按刀口反复解码原片。
    glue_at, card_at, tone_eq = _render_seam_prechecks(
        src, work, seam_map, conf, tracks, ff,
        data=data, cancel=cancel, on_log=on_log)
    while i < len(work):
        a, b = work[i]
        s = seam_map.get(i)
        if s and s.get("kind") in ("crossfade", "vfade") and i + 1 < len(work):
            na, nb = work[i + 1]
            _want = (conf["transition"]["crossfade"] if s.get("kind") == "crossfade"
                     else float(conf["transition"].get("vfade", 0.22)))
            d = min(_want, (b - a) * 0.5, (nb - na) * 0.5)
            if d > 0.05:
                pieces.append(("trim", a, b - d))
                if s.get("kind") == "vfade":
                    # 音频交叉长度：原来固定 40ms（等于不叠），结果"画面叠化了、
                    # 声音还是硬切"，接缝听着就卡。改成跟叠化等长，让两边尾音
                    # 真的混在一起——像人说话时前一句尾音和后一句自然重叠。
                    _audio_d = float(conf["transition"].get("vfade_audio",
                                                            d) or d)
                    pieces.append(("bridge", (b - d, b), (na, na + d),
                                   min(_audio_d, d)))
                else:
                    pieces.append(("bridge", (b - d, b), (na, na + d)))
                work[i + 1] = [na + d, nb]
                n_cross += 1
                i += 1
                continue
        _mb = float(conf.get("transition", {}).get("micro_bridge", 0) or 0)
        if s and s.get("kind") == "cut" and _mb > 0.02 and i + 1 < len(work):
            na, nb = work[i + 1]
            _d = min(_mb, (b - a) * 0.5, (nb - na) * 0.5)
            if _d > 0.02:
                pieces.append(("trim", a, b - _d))
                pieces.append(("bridge", (b - _d, b), (na, na + _d), _d))
                work[i + 1] = [na + _d, nb]
                n_cross += 1
                i += 1
                continue
        if i in card_at and i + 1 < len(work):
            # 章节卡：前段渐暗 → 黑场 → 后段渐亮。直接切黑太刺眼，必须渐变。
            pieces.append(("trim", a, b, "out", _vfade, _afade))
            pieces.append(("chapter", card_at[i]))
            _fade_in_at.add(i + 1)
            i += 1
            continue
        if s and s.get("kind") == "cut" and i in glue_at and i + 1 < len(work):
            na, nb = work[i + 1]
            _ad = min(glue_at[i], (b - a) * 0.5, (nb - na) * 0.5)
            if _ad > 0.05:
                pieces.append(("trim", a, b - _ad))
                pieces.append(("audio_bridge", (b - _ad, b),
                               (na, na + _ad), _ad))
                work[i + 1] = [na + _ad, nb]
                n_cross += 1
                i += 1
                continue
        _teq = tone_eq.get(i) or (0.0, 0.0)
        if i in _fade_in_at:
            pieces.append(("trim", a, b, "in", _vfade, _afade, _teq[0], _teq[1]))
        else:
            pieces.append(("trim", a, b, "", 0.30, 0.16, _teq[0], _teq[1]))
        i += 1

    _single_requested = bool(r.get("single_decode", True))
    _single_mode, _single_reason = _single_decode_eligibility(
        pieces, _free_tracks)
    if not _single_requested:
        _single_mode, _single_reason = False, "配置显式使用兼容渲染器"
    on_log("开始渲染：%d 个片段（其中 %d 处叠化）"
           % (len(pieces), n_cross))
    if _single_mode:
        on_log("渲染引擎：单输入时间轴（原素材只打开、顺序解码一次）")
    else:
        on_log("渲染引擎：兼容分段（%s；会重复读取原素材）"
               % (_single_reason or "特殊时间线"))

    # ---- 2. 成片时间轴：成片第几秒 ↔ 原片第几秒 ----
    # 字幕、后期精修都要靠这张表，不然重排之后就不知道哪句话跑到哪去了。
    timeline = []
    _cur = 0.0
    _chapter_ranges = []
    _seam_kinds = []   # [(成片时刻, "smooth"/"hard")]，供接缝体检精确判断
    for _k, _p in enumerate(pieces):
        if _p[0] == "chapter":
            # 章节卡没有源区间，只占成片时长，不进时间轴（否则字幕 remap 会错位）。
            # 但要把它的成片区间记下来，供 QC 跳过黑屏检查。
            _chapter_ranges.append(
                (round(_cur, 3), round(_cur + float(_p[1]), 3)))
            _cur += float(_p[1])
            continue
        if _p[0] == "trim":
            _d = max(0.0, _p[2] - _p[1])
            _s0 = float(_p[1])
        elif _p[0] == "audio_bridge":
            # 画面取后段（硬切），所以时间轴的源起点是后段开头。
            _d = min(_p[1][1] - _p[1][0], _p[2][1] - _p[2][0])
            _s0 = float(_p[2][0])
        else:
            _d = max(_p[1][1] - _p[1][0], _p[2][1] - _p[2][0])
            _s0 = float(_p[1][0])
        timeline.append((round(_cur, 3), round(_s0, 3), round(_d, 3)))
        # 这个接缝是"叠化"还是"硬切"：本段是 bridge，或者下一段是 bridge，
        # 说明这里做了叠化；audio_bridge 的画面是硬切，仍算 hard。
        _nxt = pieces[_k + 1][0] if _k + 1 < len(pieces) else ""
        _smooth = (_p[0] == "bridge" or (_nxt == "bridge" and _p[0] != "audio_bridge"))
        _seam_kinds.append((round(_cur + _d, 3), "smooth" if _smooth else "hard"))
        _cur += _d

    tmpdir = _tf.mkdtemp(prefix="autocut_seg_", dir=indexer.temp_root())
    listfile = os.path.join(tmpdir, "concat.txt")
    done = [0]
    _single_stem_temp = []
    _single_stem_targets = []
    _single_stems = {}
    if _single_mode and bool(r.get("export_stems", False)):
        _stem_dir = os.path.join(
            os.path.dirname(os.path.abspath(out_path)),
            os.path.splitext(os.path.basename(out_path))[0] + "_分轨")
        os.makedirs(_stem_dir, exist_ok=True)
        for _track in (tracks or []):
            _single_stem_temp.append(
                os.path.join(tmpdir, "stem_track_%d.wav" % int(_track)))
            _single_stem_targets.append((
                int(_track), os.path.join(
                    _stem_dir, "第%d条.wav" % (int(_track) + 1))))

    # ---- 3. 要烧字幕就先按段生成小 ASS（每段时间从 0 开始） ----
    ass_files = {}
    _single_ass = None
    if sub_burn and subs and timeline:
        try:
            import subtitles as _subs
            _w, _h = sub_res or (1920, 1080)
            _style = dict(sub_style or _subs.load_style())
            # 样式是按 1080p 调的。预览出片画布更小（比如 1280x720），
            # 不按比例缩的话字会比成品里大一截，预览就没参考价值了。
            _ratio = float(_h) / 1080.0
            if abs(_ratio - 1.0) > 0.01:
                for _key in ("size", "margin_v", "outline", "shadow"):
                    _v = _style.get(_key)
                    if isinstance(_v, (int, float)):
                        _style[_key] = max(1, int(round(float(_v) * _ratio)))
            # 样式里写的字体这台机器没装的话，libass 会退回一个没有中文字形的
            # 字体，字幕直接变成一排方块。所以先在本机挑一个能显示中文的。
            try:
                if not _subs.font_exists(_style.get("font") or ""):
                    _style["font"] = _subs.default_font() or _style.get("font")
                    on_log("字幕字体本机没有，改用 %s" % _style.get("font"))
            except Exception:  # noqa: BLE001
                pass
            for _piece_idx, _o0, _s0, _d in _subtitle_piece_rows(
                    pieces, timeline):
                if pieces[_piece_idx][0] != "trim":
                    continue          # 叠化过渡片不烧，半秒钟不值得
                _local = _subs.remap(subs, [(0.0, _s0, _d)])
                if not _local:
                    continue
                _name = "s%05d.ass" % _piece_idx
                _subs.write_ass({}, os.path.join(tmpdir, _name), style=_style,
                                width=_w, height=_h, segments=_local)
                # Key by render-piece index because ``one(idx)`` consumes the
                # full pieces list, including synthetic chapter cards.
                ass_files[_piece_idx] = _name
            on_log("字幕：%d 段里有台词，按成片时间轴烧进画面" % len(ass_files))
        except Exception as exc:  # noqa: BLE001
            on_log("字幕没烧进去（不影响出片）：%s" % str(exc)[:100])
            ass_files = {}

    # 单输入引擎在完整成片时间轴上只烧一份字幕。章节卡造成的输出时间
    # 偏移已经包含在 timeline 中，不需要给每个源片段另起一次编码。
    if _single_mode and sub_burn and subs and timeline:
        try:
            import subtitles as _subs
            _w, _h = sub_res or (1920, 1080)
            _style = dict(sub_style or _subs.load_style())
            _ratio = float(_h) / 1080.0
            if abs(_ratio - 1.0) > 0.01:
                for _key in ("size", "margin_v", "outline", "shadow"):
                    _value = _style.get(_key)
                    if isinstance(_value, (int, float)):
                        _style[_key] = max(1, int(round(float(_value) * _ratio)))
            try:
                if not _subs.font_exists(_style.get("font") or ""):
                    _style["font"] = _subs.default_font() or _style.get("font")
            except Exception:  # noqa: BLE001
                pass
            _mapped = _subs.remap(subs, timeline)
            if _mapped:
                _single_ass = "timeline.ass"
                _subs.write_ass({}, os.path.join(tmpdir, _single_ass),
                                style=_style, width=_w, height=_h,
                                segments=_mapped)
                on_log("字幕：%d 条按完整成片时间轴一次烧入" % len(_mapped))
        except Exception as exc:  # noqa: BLE001
            on_log("单输入字幕没烧进去（不影响出片）：%s" % str(exc)[:100])
            _single_ass = None

    # 逐段日志：卡住时能看出是哪个片段、什么时候开始、有没有结束。
    # pythonw 没有控制台，光靠界面状态看不出停在哪。
    _logpath = os.path.join(BASE, "logs", "render_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        os.makedirs(os.path.dirname(_logpath), exist_ok=True)
    except OSError:
        _logpath = None
    _lk = __import__("threading").Lock()

    def _rlog(msg):
        if not _logpath:
            return
        try:
            with _lk:
                with open(_logpath, "a", encoding="utf-8") as fh:
                    fh.write("%s [%s] %s\n" % (time.strftime("%H:%M:%S"),
                                              __import__("threading").current_thread().name,
                                              msg))
        except OSError:
            pass

    # 每段的“源片起点”。只给分轨时间线（free_tracks）用，
    # 默认关着（分割同步，所有轨一起切），所以这里必须容错——
    # 一旦某个 piece 的形状不认识就跳过，绝不能让它把整个渲染搞挂
    # （实测：chapter 段没有 _p[2]，直接取会报 tuple - tuple）。
    _piece_offsets = []
    try:
        _src_cursor = None
        for _p in pieces:
            _kind = str(_p[0]) if _p else ""
            if _src_cursor is None:
                _start = (float(_p[1]) if _kind == "trim" and len(_p) > 1
                          else 0.0)
            else:
                _start = _src_cursor
            _piece_offsets.append(round(float(_start), 3))
            if _kind == "trim" and len(_p) > 2:
                _src_cursor = float(_p[2])
            elif _kind in ("bridge", "audio_bridge") and len(_p) > 2:
                _seg = _p[2]
                if isinstance(_seg, (list, tuple)) and len(_seg) > 1:
                    _src_cursor = float(_seg[1])
            # chapter：没有源片区间，cursor 不动
    except Exception as _po_err:  # noqa: BLE001
        _piece_offsets = [0.0] * len(pieces)
        on_log("  分段起点计算跳过：%s" % str(_po_err)[:60])

    timeline_v2 = build_sequence_timeline(
        pieces, tracks=tracks,
        fixed_tracks=(_fixed_tracks if _free_tracks else tracks),
        free_tracks=_free_tracks, piece_offsets=_piece_offsets,
        source=src)

    _rlog("开始 段数=%d 音轨=%s engine=%s workers=%s" % (
        len(pieces), tracks, "single" if _single_mode else "legacy",
        r.get("workers")))

    retries = int(r.get("retries") or 2)

    def _run_single_command(command, cwd, duration, progress_path):
        """Run on the caller/QThread so progress and cancellation stay safe."""
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_NO_WIN)
        last_fraction = -1.0
        try:
            while process.poll() is None:
                if cancel is not None and cancel():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise RenderCancelled("已取消剪辑")
                try:
                    with open(progress_path, encoding="utf-8",
                              errors="replace") as _progress_file:
                        values = {}
                        for line in _progress_file:
                            if "=" in line:
                                key, value = line.rstrip().split("=", 1)
                                values[key] = value
                    micros = float(values.get("out_time_us") or
                                   values.get("out_time_ms") or 0.0)
                    fraction = min(0.98, max(0.0, micros / 1000000.0 /
                                              max(0.001, float(duration))))
                    if on_progress and fraction >= last_fraction + 0.01:
                        on_progress(fraction)
                        last_fraction = fraction
                except (OSError, TypeError, ValueError):
                    pass
                time.sleep(0.15)
            _stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(
                command, process.returncode, "",
                (stderr or b"").decode("utf-8", "replace"))
        finally:
            try:
                os.remove(progress_path)
            except OSError:
                pass

    def one(idx, single_direct=False):
        # 机械硬盘上几路 ffmpeg 同时读同一个 10 GB 文件，偶发读失败很正常
        # （实测单独编这一段 4 秒就好，多路一起跑就挂）。
        # 所以失败先重试，别直接判死刑。
        if cancel is not None and cancel():
            raise RenderCancelled("已取消")
        piece = pieces[idx]
        out = os.path.join(tmpdir, "p%05d.mp4" % idx)
        last_err = ""
        _ass = _single_ass if _single_mode else ass_files.get(idx)
        _rlog("%s %d 开始 %s" % (
            "时间轴" if _single_mode else "段", idx + 1,
            "single-decode" if _single_mode else piece[0]))
        attempt_count = 1 if _single_mode else retries + 1
        for attempt in range(attempt_count):
            if _single_mode:
                _progress_path = os.path.join(tmpdir, "single_progress.txt")
                _filter_script = os.path.join(tmpdir, "timeline_filter.txt")
                cmd, _dur = _single_timeline_cmd(
                    ff, src, pieces, out, r, fps, scale,
                    _fixed_tracks if _free_tracks else tracks,
                    ass_name=_ass, track_kinds=_track_kinds_map,
                    output_size=sub_res,
                    stem_outputs=_single_stem_temp,
                    progress_path=(_progress_path if single_direct else None),
                    filter_script_path=_filter_script)
            else:
                cmd, _dur = _piece_cmd(
                    ff, src, piece, out, r, fps, scale,
                    _fixed_tracks if _free_tracks else tracks,
                    ass_name=_ass, free_tracks=_free_tracks,
                    free_offset=_piece_offsets[idx],
                    track_kinds=_track_kinds_map)
            if _single_mode and single_direct:
                p = _run_single_command(
                    cmd, tmpdir if _ass else None, _dur, _progress_path)
            else:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   cwd=(tmpdir if _ass else None),
                                   creationflags=_NO_WIN)
            if not _single_mode and p.returncode != 0 and "-hwaccel" in cmd:
                # 硬件解码不支持这种素材：去掉 -hwaccel 立刻重试一次（软解）
                _soft = []
                skip = False
                for _x in cmd:
                    if skip:
                        skip = False
                        continue
                    if _x == "-hwaccel":
                        skip = True
                        continue
                    _soft.append(_x)
                p = subprocess.run(_soft, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   cwd=(tmpdir if _ass else None),
                                   creationflags=_NO_WIN)
            if p.returncode == 0:
                _rlog("%s %d 完成" % (
                    "时间轴" if _single_mode else "段", idx + 1))
                done[0] += 1
                # 这里不发进度：本函数跑在线程池里，从工作线程发 Qt 信号会崩。
                # 进度统一由调用方（QThread）在 as_completed 里发。
                return idx
            last_err = (p.stderr or "").strip()
            if not _single_mode and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        if _single_mode:
            raise RuntimeError("单输入时间轴编码失败（未退回重复解码）：\n%s"
                               % (last_err[-800:] or "（没有错误输出）"))
        where = ("%.1f–%.1fs" % (piece[1], piece[2]) if piece[0] == "trim"
                 else "叠化过渡 %.1f–%.1fs" % (piece[1][0], piece[1][1]))
        raise RuntimeError("第 %d/%d 段编码失败（%s，重试 %d 次仍不行）：\n%s"
                           % (idx + 1, len(pieces), where, retries,
                              last_err[-500:] or "（没有错误输出）"))

    workers = int(r.get("workers") or 3)
    try:
        # 注意：进度只能在调用方线程里发（Qt 里就是那个 QThread）。
        # 之前是在线程池的工作线程里直接 on_progress → 发 Qt 信号，
        # PySide6 下会偶发 0xc0000005 访问违规，界面直接崩。
        from concurrent.futures import as_completed as _ac
        total_pieces = 1 if _single_mode else max(1, len(pieces))
        finished = 0
        aborted = 0
        cancel_after_start = False
        if _single_mode:
            # Direct call is deliberate: this function already runs in the
            # render QThread, so progress signals must originate here instead
            # of a ThreadPool worker (PySide otherwise occasionally crashes).
            try:
                one(0, single_direct=True)
                finished = 1
                if on_progress:
                    on_progress(0.98)
            except RenderCancelled:
                aborted = 1
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, total_pieces))) as ex:
                futs = [ex.submit(one, k) for k in range(total_pieces)]
                for f in _ac(futs):
                    try:
                        f.result()
                    except RenderCancelled:
                        aborted += 1
                        continue
                    finished += 1
                    _rlog("已收 %d/%d" % (finished, total_pieces))
                    if on_progress:
                        on_progress(min(0.98, finished / total_pieces))
                    # 分段任务只在开工前检查一次。若用户是在所有 worker 都启动后
                    # 才点取消，必须在收结果时再看一次，否则仍会继续拼接并报成功。
                    if cancel is not None and cancel():
                        cancel_after_start = True
                        for pending in futs:
                            pending.cancel()
                        break
        if aborted or cancel_after_start:
            on_log("已取消：%d 段没做，临时文件已清掉" % aborted)
            raise RenderCancelled("已取消剪辑")

        # 最后一处竞态：最后一段收完到 concat 启动之间也可能点取消。
        if cancel is not None and cancel():
            on_log("已取消：尚未拼接最终成片")
            raise RenderCancelled("已取消剪辑")

        if _single_mode:
            os.replace(os.path.join(tmpdir, "p00000.mp4"), out_path)
            for (_track, _target), _temporary in zip(
                    _single_stem_targets, _single_stem_temp):
                if os.path.isfile(_temporary) and os.path.getsize(_temporary) > 44:
                    os.replace(_temporary, _target)
                    _single_stems["第%d条" % (_track + 1)] = _target
            if _single_stems:
                on_log("  分轨导出：%d 条已在同一次源解码中完成 → %s"
                       % (len(_single_stems), os.path.dirname(
                           next(iter(_single_stems.values())))))
            _mark("时间轴编码完成", "单输入 / 单次源解码")
        else:
            with open(listfile, "w", encoding="utf-8") as fh:
                for k in range(len(pieces)):
                    p = os.path.join(tmpdir, "p%05d.mp4" % k).replace("'", "'\\''")
                    fh.write("file '%s'\n" % p)

            try:
                _mark("片段编码完成", "%d 段 / %d 并发" % (len(pieces), workers))
            except Exception:  # noqa: BLE001
                pass
            on_log("片段编码完成，正在拼接 …")
            cat = subprocess.run(
                _concat_piece_cmd(ff, listfile, out_path),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=_NO_WIN)
            if cat.returncode != 0:
                raise RuntimeError("拼接失败：%s" % (cat.stderr or "")[-800:])
    finally:
        _sh.rmtree(tmpdir, ignore_errors=True)

    _rlog("%s完成：%s" % ("时间轴编码" if _single_mode else "拼接", out_path))
    if on_progress:
        on_progress(1.0)

    _mark("渲染主片完成")

    # ---- 3.5 贯穿底噪：从源片取一条连续背景音铺满全片 ----
    # 《剪辑基础技巧》第 163 行：「环境音、背景音不能随镜头切换忽断忽续，
    # 通常使用一条贯穿的环境音底噪，保证声音空间的统一。」
    # 这才是"背景声差 20dB"的根治办法——黑场只是遮住症状。
    _bed_track = r.get("background_track")
    try:
        _bed_track = int(_bed_track) if _bed_track is not None else -1
    except (TypeError, ValueError):
        _bed_track = -1
    # 底噪只能用“用户勾选过的轨”（子枫 2026-11-23：他勾了 2、3，
    # 结果底噪把第 4 条音乐混进出片了——那是他没选的轨）。
    if _bed_track >= 0 and _bed_track not in list(tracks or []):
        on_log("  贯穿底噪：第 %d 条不在勾选范围内，跳过" % (_bed_track + 1))
        _bed_track = -1
    _bed_gain = float(r.get("background_gain_db", -10.0) or -10.0)
    _bed_total = (sum(_d for _o, _s, _d in timeline)
                  + sum(_b - _a for _a, _b in _chapter_ranges))
    if _bed_track >= 0 and _bed_total > 1.0:
        try:
            on_log("  贯穿底噪：从源片第 %d 条音轨铺 %.0f dB 背景，覆盖全片 %.0f 秒 …"
                   % (_bed_track + 1, _bed_gain, _bed_total))
            _bed_out = out_path + ".bed.mp4"
            # 多音轨时不能只取 [0:a]——那会把其余轨丢掉（实测：分轨时间线
            # 出来的 2 条轨，被底噪这一步压回了 1 条）。
            # 所以：第一条轨和底噪混，其余轨原样带过去。
            try:
                _n_a = len((indexer.probe_streams(out_path) or {})
                           .get("audio_streams") or [])
            except Exception:
                _n_a = 1
            if _n_a > 1:
                _extra = "".join(
                    ";[0:a:%d]anull[keep%d]" % (k, k) for k in range(1, _n_a))
                _bed_graph = (
                    "[1:a:%d]volume=%.1fdB,atrim=0:%.3f,asetpts=PTS-STARTPTS[bed];"
                    "[0:a:0][bed]amix=inputs=2:duration=first:normalize=0,"
                    "alimiter=limit=0.95[a0]%s"
                    % (_bed_track, _bed_gain, _bed_total, _extra))
                _bed_maps = ["-map", "0:v", "-map", "[a0]"]
                for _k in range(1, _n_a):
                    _bed_maps += ["-map", "[keep%d]" % _k]
            else:
                _bed_graph = (
                    "[1:a:%d]volume=%.1fdB,atrim=0:%.3f,asetpts=PTS-STARTPTS[bed];"
                    "[0:a][bed]amix=inputs=2:duration=first:normalize=0,"
                    "alimiter=limit=0.95[a]"
                    % (_bed_track, _bed_gain, _bed_total))
                _bed_maps = ["-map", "0:v", "-map", "[a]"]
            _bed_cmd = [
                ff, "-hide_banner", "-loglevel", "error", "-y",
                "-i", out_path,
                "-stream_loop", "-1", "-t", "%.3f" % _bed_total, "-i", src,
                "-filter_complex", _bed_graph] + _bed_maps + [
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", r["audio_bitrate"],
                "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", _bed_out]
            _bed_run = subprocess.run(
                _bed_cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", creationflags=_NO_WIN)
            if _bed_run.returncode == 0 and os.path.isfile(_bed_out):
                os.replace(_bed_out, out_path)
                on_log("  贯穿底噪：已混入（背景不会再随剪切忽断忽续）")
            else:
                if os.path.isfile(_bed_out):
                    os.remove(_bed_out)
                on_log("  贯穿底噪混入失败：%s"
                       % (_bed_run.stderr or "")[-200:])
        except Exception as _bed_err:  # noqa: BLE001
            on_log("  贯穿底噪跳过：%s" % str(_bed_err)[:100])

    # ---- 4. 自动配 BGM（视频流复制，只重编码音频，几秒钟） ----
    if bgm:
        import bgm as _bgm
        on_log("BGM：按波形挑一段铺底…")
        _dips = seam_out_times(pieces)
        _cfg = dict(bgm_conf or _bgm.load_conf())
        # 先把"无声成片"挪开：BGM 要写到 out_path 上，
        # 直接覆盖刚被写过的文件在 Windows 上偶尔会失败（文件被占用）。
        # 这个备份也是事务回滚点：曲库为空或混音失败时必须恢复，不能让
        # render() 返回成功、实际成片却只剩一个 .pre.mp4 文件。
        _pre = out_path + ".pre.mp4"
        _bgm_src = out_path
        _pre_moved = False
        _info = None
        _bgm_error = None
        try:
            if os.path.isfile(_pre):
                os.remove(_pre)
            os.replace(out_path, _pre)
            _bgm_src = _pre
            _pre_moved = True
        except OSError as _exc:  # noqa: BLE001
            on_log("BGM：原片挪不开（%s），就地混音" % str(_exc)[:40])
        try:
            if str(_cfg.get("sections", "0")) in ("1", "true", "True"):
                try:
                    import bgm_sections as _bs
                    import index as _ix
                    _dur = float(_ix.probe_streams(_bgm_src)["duration"])
                    try:
                        _min_sec = float(_cfg.get("section_min_min", 1.0)) * 60.0
                    except (TypeError, ValueError):
                        _min_sec = 60.0
                    try:
                        _max_sec = float(_cfg.get("section_max_min", 3.0)) * 60.0
                    except (TypeError, ValueError):
                        _max_sec = 180.0
                    # 旧配置里可能仍写着 4 分钟；运行时强制收敛到 1~3 分钟。
                    _min_sec = max(60.0, min(180.0, _min_sec))
                    _max_sec = max(_min_sec, min(180.0, _max_sec))
                    _secs = _bs.make_sections(
                        data or {}, _dur, pieces=pieces,
                        min_len=_min_sec, max_len=_max_sec,
                        seed=_cfg.get("seed") or None)
                    _info = _bs.attach_sections(
                        _bgm_src, _secs, seam_times=_dips, out_path=out_path,
                        conf=_cfg, log=on_log, data=data or {}, pieces=pieces)
                except Exception as _exc:  # noqa: BLE001
                    on_log("BGM 分段跳过（%s），改单曲" % str(_exc)[:50])
                    _info = _bgm.attach(_bgm_src, out_path=out_path,
                                        conf=bgm_conf, log=on_log, dip_times=_dips)
            else:
                _info = _bgm.attach(_bgm_src, out_path=out_path,
                                    conf=bgm_conf, log=on_log, dip_times=_dips)
        except Exception as exc:  # noqa: BLE001
            _bgm_error = exc
        finally:
            _made = bool(_info) and os.path.isfile(out_path) \
                and os.path.getsize(out_path) > 0
            if _pre_moved:
                if _made:
                    try:
                        os.remove(_pre)
                    except OSError:
                        pass
                else:
                    # BGM 是可选增强，但成片不是。任何失败都回滚到配乐前成片。
                    try:
                        os.replace(_pre, out_path)
                    except OSError:
                        try:
                            _sh.copy2(_pre, out_path)
                            os.remove(_pre)
                        except OSError as _restore_exc:
                            raise RuntimeError(
                                "BGM 失败且原成片恢复失败：%s" % _restore_exc
                            ) from _restore_exc
            if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
                raise RuntimeError("BGM 处理后找不到有效成片：%s" % out_path)
        if _bgm_error is not None:
            on_log("BGM 没配上（已保留无 BGM 成片）：%s"
                   % str(_bgm_error)[:120])
        elif not _info:
            on_log("BGM：这次没配上，已保留无 BGM 成片（可以到 conf/bgm.json 调）")

    # 这里只代表编码/混音完成；完整自动流程还要经过实际成片导演复审。
    _mark("渲染完成")
    on_log("⏱ 分段：%s"
           % "，".join("%s %.1fs" % (k, v) for k, v in _timing.items()))
    # ---- 4.5 分轨导出（可选）----
    # 子枫 2026-11-23：「混音要改到最后，所有问题解决要在混音之前解决，
    # 因为要考虑我可能需要的多音轨输出。」
    # 所以成片仍然是混好的（播放器只播第一条，多轨成片会被误以为没人声），
    # 但**额外**把每条处理过的音轨单独导出一份 wav，供后期使用。
    _stems = dict(_single_stems)
    if (not _single_mode and bool(r.get("export_stems", False)) and timeline):
        try:
            _stem_dir = os.path.join(
                os.path.dirname(os.path.abspath(out_path)),
                os.path.splitext(os.path.basename(out_path))[0] + "_分轨")
            os.makedirs(_stem_dir, exist_ok=True)
            # 从“无声成片 + 源片”重新按时间线取每条轨，拼成整条。
            # 这样每条轨都是“切过、拼过”的干净版本，可以直接进 PR。
            for _tr in (tracks or []):
                _pieces_dir = []
                _out_wav = os.path.join(_stem_dir, "第%d条.wav" % (_tr + 1))
                _parts = []
                for _k, (_ot, _ss, _dd) in enumerate(timeline):
                    _part = os.path.join(_stem_dir, "_p%03d_%d.wav" % (_k, _tr))
                    _cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", "%.3f" % _ss, "-t", "%.3f" % _dd,
                            "-i", src, "-map", "0:a:%d" % _tr,
                            "-vn", "-ac", "2", "-ar", "48000",
                            "-c:a", "pcm_s16le", _part]
                    _done = subprocess.run(_cmd, capture_output=True,
                                           text=True, encoding="utf-8",
                                           errors="replace",
                                           creationflags=_NO_WIN)
                    if _done.returncode == 0:
                        _parts.append(_part)
                if _parts:
                    _lst = os.path.join(_stem_dir, "_list_%d.txt" % _tr)
                    with open(_lst, "w", encoding="utf-8") as _fh:
                        for _p in _parts:
                            _fh.write("file '%s'\n" % _p.replace("'", "'\\''"))
                    _cat = subprocess.run(
                        [ff, "-hide_banner", "-loglevel", "error", "-y",
                         "-f", "concat", "-safe", "0", "-i", _lst,
                         "-c", "copy", _out_wav],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", creationflags=_NO_WIN)
                    if _cat.returncode == 0:
                        _stems["第%d条" % (_tr + 1)] = _out_wav
                    for _p in _parts:
                        try:
                            os.remove(_p)
                        except OSError:
                            pass
                    try:
                        os.remove(_lst)
                    except OSError:
                        pass
            if _stems:
                on_log("  分轨导出：%d 条 → %s" % (len(_stems), _stem_dir))
        except Exception as _stem_err:  # noqa: BLE001
            on_log("  分轨导出跳过：%s" % str(_stem_err)[:80])

    if _single_mode:
        _source_decode_passes = 1
        if _bed_track >= 0 and _bed_total > 1.0:
            _source_decode_passes += 1
    else:
        _source_decode_passes = sum(
            0 if str(piece[0]) == "chapter"
            else (2 if str(piece[0]) in ("bridge", "audio_bridge") else 1)
            + (1 if _free_tracks and str(piece[0]) == "trim" else 0)
            for piece in pieces)
        if _bed_track >= 0 and _bed_total > 1.0:
            _source_decode_passes += 1
        if bool(r.get("export_stems", False)):
            _source_decode_passes += len(tracks or []) * len(timeline)

    return {"out": out_path, "timeline": timeline,
            "timeline_v2": timeline_v2,
            "sequence_revision": timeline_v2["sequence_revision"],
            "pieces": len(pieces),
            "render_decode_mode": (
                "single_source_timeline" if _single_mode
                else "legacy_segmented_compatibility"),
            "render_fallback_reason": None if _single_mode else _single_reason,
            "source_decode_passes": int(_source_decode_passes),
            "stems": _stems,
            "timing": _timing,
            "spans": [(round(float(a), 3), round(float(b), 3))
                      for a, b in (segments or [])],
            "chapter_ranges": _chapter_ranges,
            "seam_kinds": _seam_kinds,
            "smooth_seams": [at for at, kind in _seam_kinds if kind == "smooth"],
            "chapter_seconds": round(
                sum(_b - _a for _a, _b in _chapter_ranges), 3),
            "sub_burned": len(ass_files),
            "minutes": round(
                (sum(d for _o, _s, d in timeline)
                 + sum(_b - _a for _a, _b in _chapter_ranges)) / 60, 2)}


# --------------------------------------------------------------------------
# 四、命令行
# --------------------------------------------------------------------------
def load_index(path):
    if os.path.isdir(path):
        path = os.path.join(path, "index.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if "analysis" not in data:
        print("提示：这份索引是老格式（没有 analysis 数据），只能拼接、不能判空段。")
        print("      重新建一次索引就会带上。")
    return data


INTRO_HINTS = (
    "大家好", "各位好", "我是子枫", "欢迎来到", "欢迎收看", "这期", "这一期",
    "本期", "今天做", "今天来", "我们今天", "好久不见", "又见面了",
)


def find_intro(data, within=300.0, tail_sentences=2):
    """找片头自我介绍。

    实况录像里开头一两分钟通常有一句"大家好我是XX，今天我们来做……"，
    观众就是靠这句判断这是谁的视频、这期讲什么。
    它不该由模型分数决定去留——分数低也得留，而且要排在最前面。

    返回 [(start, end)]，没有就返回 []。
    """
    segs = data.get("segments") or []
    if not segs:
        return []
    # 只在前 5 分钟里找，避免把中途的"欢迎来到我的基地"误判成片头
    hits = []
    for i, sg in enumerate(segs):
        a = float(sg[0])
        if a > within:
            break
        txt = str(sg[2])
        if any(w in txt for w in INTRO_HINTS):
            hits.append(i)
    if not hits:
        return []
    # 命中句往后顺延 1~2 句（"今天我们做……"往往接在自我介绍后面）
    spans = []
    for i in hits[:3]:
        j = min(len(segs) - 1, i + max(0, tail_sentences))
        spans.append((float(segs[i][0]), float(segs[j][1])))
    # 合并重叠
    spans.sort()
    out = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= out[-1][1] + 1.0:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def select_by_script(data, conf=None, level=None):
    """按剧本章节剪：每一章用它的取舍建议决定删多少。

    为什么要有目标：模型逐句判断时只看得到眼前几十句，
    容易把铺垫剪掉、把重复内容留下。剧本给了整期结构，
    剪辑就有"这一章要讲完这件事"的目标，不会剪得七零八落。

    取舍建议 → 保留阈值：
        全部保留   → 0（整章都留）
        只留关键   → 0.55
        可以大幅删减 → 0.68
    返回 [(start, end)]；没有剧本返回 None。
    """
    # Neutral compatibility scores are not values to compare with a cut threshold.
    if llm_gateway.api_enabled():
        return None
    script = data.get("script") or {}
    sections = script.get("sections") or []
    segs = data.get("segments") or []
    scores = data.get("scores") or []
    if not sections or not segs or len(scores) != len(segs):
        return None

    # 剪辑力度：调用方可以显式传 level（推荐），否则读设置
    _level = level or "light"
    if level is None:
        try:
            import json as _j
            _p = os.path.join(BASE, "conf", "settings.json")
            if os.path.isfile(_p):
                _level = str(_j.load(open(_p, encoding="utf-8"))
                             .get("cut_level", "light"))
        except Exception:  # noqa: BLE001
            pass
    base_thr = {"light": 0.30, "medium": 0.45, "heavy": 0.60}.get(_level, 0.30)

    def _thr(advice):
        a = advice or ""
        if "全部" in a:
            return 0.0
        if "大幅" in a or "删减" in a:
            return min(0.95, base_thr + 0.15)
        if "关键" in a:
            return base_thr
        return max(0.0, base_thr - 0.05)

    keep = [False] * len(segs)
    for sec in sections:
        a, b = float(sec.get("start", 0)), float(sec.get("end", 0))
        th = _thr(sec.get("advice"))
        for i, sg in enumerate(segs):
            mid = (float(sg[0]) + float(sg[1])) / 2
            if a <= mid <= b and float(scores[i]) >= th:
                keep[i] = True
    # 片头无条件保留
    try:
        for ia, ib in find_intro(data):
            for i, sg in enumerate(segs):
                mid = (float(sg[0]) + float(sg[1])) / 2
                if ia <= mid <= ib:
                    keep[i] = True
    except Exception:  # noqa: BLE001
        pass
    if not any(keep):
        return None
    # 合并成区间。中/重度已经淘汰的低分内容不能因为间隔较短又被
    # 整段带回来；只连接自然气口。轻度本来就偏保守，可以宽松连接。
    bridge_gap = {"light": 8.0, "medium": 2.5, "heavy": 1.2}.get(
        _level, 2.5)
    out, cur = [], None
    for i, k in enumerate(keep):
        if not k:
            continue
        a, b = float(segs[i][0]), float(segs[i][1])
        if cur and a - cur[1] <= bridge_gap:
            cur[1] = b
        else:
            if cur:
                out.append(tuple(cur))
            cur = [a, b]
    if cur:
        out.append(tuple(cur))
    out = [(a, b) for a, b in out if b - a >= 2.5]
    return out or None


def span_value(data, a: float, b: float):
    """一段内容的"值不值得留"分数（0~1）：段内句子价值按时长加权。

    优先用 cutvalue 学出来的模型；没有权重就退回索引里的模型分。
    """
    if llm_gateway.api_enabled():
        return 0.5
    try:
        import cutvalue
        segs = data.get("segments") or []
        idx = [i for i, s in enumerate(segs)
               if float(s[0]) >= a - 0.05 and float(s[1]) <= b + 0.05]
        if idx:
            rows = cutvalue.features(data)
            vals = cutvalue.predict(rows)
            num = den = 0.0
            for i in idx:
                w = max(0.2, float(segs[i][1]) - float(segs[i][0]))
                num += vals[i] * w
                den += w
            return num / den if den else 0.0
    except Exception:  # noqa: BLE001
        pass
    sc = data.get("scores") or []
    segs = data.get("segments") or []
    vals = [float(sc[i]) for i, s in enumerate(segs)
            if i < len(sc) and float(s[0]) >= a - 0.05 and float(s[1]) <= b + 0.05]
    return sum(vals) / len(vals) if vals else 0.5


def bounds_of(data):
    """每句的"真起止"：有能量谷修正就用它，没有就用文字时间戳。"""
    segs = data.get("segments") or []
    ref = data.get("segments_refined") or []
    if ref and len(ref) == len(segs):
        return [(float(a), float(b)) for a, b in ref]
    return [(float(s[0]), float(s[1])) for s in segs]


def refine_span_edges(data, spans, tol=0.6, min_gap=0.04):
    """把每段的起止对齐到最近的"真开口/真收口"。

    为什么：文字时间戳是按字猜的，实测 24% 的句子偏差超过 0.3 秒，
    37% 的句子真正开口比它早——按文字切就会切掉半个字。
    这里用能量谷定出来的边界（segments_refined）把每一刀挪到真正的声音起止。
    """
    bounds = bounds_of(data)
    if not bounds or not spans:
        return spans
    starts = sorted(a for a, _b in bounds)
    ends = sorted(b for _a, b in bounds)
    import bisect

    intro = protected_intro(data)
    intro_start = float(intro[0][0]) if intro else None

    def near(vals, t):
        i = bisect.bisect_left(vals, t)
        cand = [v for v in (vals[i - 1] if i > 0 else None,
                            vals[i] if i < len(vals) else None) if v is not None]
        if not cand:
            return t
        best = min(cand, key=lambda x: abs(x - t))
        return best if abs(best - t) <= tol else t

    out = []
    for a, b in spans:
        na, nb = near(starts, float(a)), near(ends, float(b))
        if intro_start is not None and abs(a - intro_start) < 0.5:
            na = float(a)
        if nb <= na:
            na, nb = float(a), float(b)
        out.append([na, nb])
    # 保证不重叠
    for i in range(len(out) - 1):
        if out[i][1] > out[i + 1][0] - min_gap:
            mid = (out[i][1] + out[i + 1][0]) / 2.0
            out[i][1] = mid - min_gap / 2.0
            out[i + 1][0] = mid + min_gap / 2.0
    return [(round(a, 3), round(b, 3)) for a, b in out if b > a]

def snap_cuts_to_pauses(data, spans, min_gap=None, search=None, protect_intro=True):
    """把每个切点挪到最近的"气口"（≥min_gap 秒的停顿）中心。

    为什么：人工剪辑的刀基本都落在气口上（停顿中位 1.2~2.6 秒），
    而我们的刀有一半落在 0.2 秒以内的小缝里，听着像突然断气。
    挪动范围限制在 ±search 秒，且只在"确实有小缝"时挪；
    片头的起点不许动（观众要听到"哈喽"）。

    标准统一（2026-11-24）：min_gap / search 默认取
    `coords.CUT_PAUSE_MIN_S` / `CUT_PAUSE_SEARCH_S`；吸附后还要过一道
    「离词边界 ≥ CUT_WORD_SLACK_S」的硬校验——交付复审
    `find_word_boundary_cuts` 判的就是同一个数，两边不再各写各的。
    """
    import coords as _coords
    if min_gap is None:
        min_gap = float(getattr(_coords, "CUT_PAUSE_MIN_S", 0.22))
    if search is None:
        search = float(getattr(_coords, "CUT_PAUSE_SEARCH_S", 3.0))
    slack = float(getattr(_coords, "CUT_WORD_SLACK_S", 0.10))
    duration = float(data.get("duration") or 0.0)
    segs = data.get("segments") or []
    if not segs or not spans:
        return spans
    spans = [[float(a), float(b)] for a, b in spans]
    # 候选气口：中心点 + 长度
    cands = []
    for i in range(len(segs) - 1):
        g0, g1 = float(segs[i][1]), float(segs[i + 1][0])
        gl = g1 - g0
        if gl >= min_gap:
            cands.append(((g0 + g1) / 2.0, gl))
    if not cands:
        return [(a, b) for a, b in spans]
    intro = protected_intro(data) if protect_intro else []
    intro_start = float(intro[0][0]) if intro else None

    def snap(t):
        best, bestd = None, search + 1e-6
        for c, gl in cands:
            d = abs(c - t)
            if d < bestd:
                best, bestd = c, d
        return best if best is not None else t

    # 词级边界（统一标准的另一半）：切点离词边界太近就往词外退，
    # 宁可多留一点，也不切在半句话上。和复审读同一个 slack。
    word_bounds = []
    for evidence in data.get("segment_evidence") or []:
        for word in evidence.get("words") or []:
            try:
                w0, w1 = float(word["start"]), float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if w1 > w0:
                word_bounds.append((w0, w1))
    if not word_bounds:
        word_bounds = [(float(s[0]), float(s[1])) for s in segs if len(s) > 1]

    def safe_start(t):
        for w0, w1 in word_bounds:
            if w0 - slack < t < w1 + slack:
                return max(0.0, w0 - slack)
        return t

    def safe_end(t):
        for w0, w1 in word_bounds:
            if w0 - slack < t < w1 + slack:
                value = w1 + slack
                return min(duration, value) if duration else value
        return t

    # 段 1 起点不许早于「首字 − CUT_PAD_MS」（A21）。
    # 边界精修会按音轨活动把切口往外扩，实测把 4.75s 扩到了 2.0s，
    # 那 2.75 秒是 -70dB 的静音——成片一开头就是 3 秒没声音。
    if spans and segs:
        try:
            first_word = min(float(s[0]) for s in segs if len(s) > 1)
        except (TypeError, ValueError):
            first_word = None
        if first_word is not None:
            head_limit = max(0.0,
                             first_word - float(getattr(_coords, "CUT_PAD_MS", 150)) / 1000.0)
            if float(spans[0][0]) < head_limit - 0.05:
                spans[0] = [head_limit, spans[0][1]]

    for k in range(len(spans)):
        a, b = spans[k]
        na = safe_start(snap(a))
        nb = safe_end(snap(b))
        if intro_start is not None and abs(a - intro_start) < 0.5:
            na = a                       # 片头起点不动
        if na < b - 0.3:
            spans[k][0] = na
        if nb > na + 0.3:
            spans[k][1] = nb
    # 挪完可能交叉，合并
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]

def ensure_chapter_openers(data, spans, max_add=2, on_log=None):
    """保证"留下来的每一章"都有开头（交代这一章要做什么）。

    为什么：按分数挑句子会从章节中间开始留，观众看到"突然在讲某个配方"，
    剧情就断了。这里给每章补回开头 1~2 句（前提是这章还有内容被留下）。
    """
    segs = data.get("segments") or []
    sections = (data.get("script") or {}).get("sections") or []
    if not segs or not sections or not spans:
        return spans
    log = on_log or (lambda _m: None)
    spans = [[float(a), float(b)] for a, b in spans]
    added = 0
    for sec in sections:
        sa, sb = float(sec.get("start", 0)), float(sec.get("end", 0))
        kept = [sp for sp in spans if not (sb <= sp[0] or sa >= sp[1])]
        if not kept:
            continue
        op = [x for x in segs if sa - 0.01 <= float(x[0]) <= sa + 25.0][:max_add]
        if not op:
            continue
        first, end = float(op[0][0]), float(op[-1][1])
        if any(sp[0] <= first + 0.5 for sp in kept):
            continue                      # 已经留了开头
        spans.append([first, end])
        added += 1
    if not added:
        return [(a, b) for a, b in spans]
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    log("章节开头补齐：%d 章 → %.1f 分钟"
        % (added, sum(b - a for a, b in merged) / 60))
    return [(a, b) for a, b in merged]

def split_long_spans(data, spans, max_span=55.0, min_gap=0.8, max_cuts=80):
    """把过长的连续段在自然停顿处断开（不删内容，只加切点）。

    为什么：人剪的时候一段 10~15 秒，最长也就 50 秒左右；
    我们有时候连着留 90 多秒不切，看着就"闷"。
    断开点只挑句子之间的真实停顿（≥min_gap），绝不切在句子中间。
    """
    segs = [(float(a), float(b)) for a, b, _t in (data.get("segments") or [])]
    if not segs or not spans:
        return spans
    out, cuts = [], 0
    mids = [(segs[i][1] + segs[i + 1][0]) / 2.0 for i in range(len(segs) - 1)]
    gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
    for a, b in spans:
        cur = (a, b)
        while (cur[1] - cur[0]) > max_span and cuts < max_cuts:
            mid = (cur[0] + cur[1]) / 2.0
            best = None
            for k, m in enumerate(mids):
                if m <= cur[0] + 3.0 or m >= cur[1] - 3.0:
                    continue
                if gaps[k] < min_gap:
                    continue
                key = (abs(m - mid), -gaps[k])
                if best is None or key < best[0]:
                    best = (key, m)
            if best is None:
                break
            out.append((cur[0], best[1]))
            cur = (best[1], cur[1])
            cuts += 1
        out.append(cur)
    return out


def enforce_budget(data, spans, target_sec: float, keep_intro=True):
    """把总时长压到目标以内：按"段价值"从低往高丢，直到不超预算。

    为什么必须单独做这一步：挑句子时是按目标选的，但后面的
    「完整度修补 / 情绪延长」会把时间加回来（实测要 14.7 分钟、出来 20 分钟）。
    所以要在最后再收一次口，而不是指望上游。
    """
    # API editor/director must resolve an over-budget plan; local training
    # weights (or equal neutral placeholders) must never silently delete it.
    if llm_gateway.api_enabled():
        return spans
    if not spans or not target_sec:
        return spans
    spans = [(float(a), float(b)) for a, b in spans]
    total = sum(b - a for a, b in spans)
    if total <= target_sec * 1.02:
        return spans
    intro = protected_intro(data) if keep_intro else []
    def protected(a, b):
        return any(not (b <= x or a >= y) for x, y in intro)
    scored = []
    for i, (a, b) in enumerate(spans):
        v = span_value(data, a, b)
        if protected(a, b):
            v += 10.0                    # 片头不动
        scored.append((v, i, a, b))
    scored.sort(key=lambda x: x[0])      # 价值低的先丢
    drop = set()
    cur = total
    for v, i, a, b in scored:
        if cur <= target_sec:
            break
        if v >= 10.0:
            break                        # 剩下的都是受保护的
        drop.add(i)
        cur -= (b - a)
    out = [(a, b) for i, (a, b) in enumerate(spans) if i not in drop]
    return out or spans


def default_target(dur: float) -> float:
    """默认目标时长：按频道规则「系列连载 13~25 分钟」。

    短视频平台超 18 分钟要另剪，那是另一条规则；这里管成片主线。
    素材本身不到 13 分钟就按 45% 走，不要硬凑。
    """
    dur = float(dur or 0)
    if dur <= 0:
        return 0.0
    return max(min(13 * 60.0, dur * 0.75), min(25 * 60.0, dur * 0.45))


# --------------------------------------------------------------------------
# 剪辑力度：三档各自的行为（用户 2026-11-21 定义）
# --------------------------------------------------------------------------
LEVEL_LABEL = {
    "light": "轻度 · 只删空屏",
    "medium": "中度 · 再删语义不连贯",
    "heavy": "重度 · 严格按剧情流程",
}

LEVEL_HINT = {
    "light": "只删空屏和明确噪声，说话内容全留",
    "medium": "删掉语义不连贯、重复啰嗦、跑题的段落，保留主线",
    "heavy": "严格按剧情流程：片头介绍 → 每章只留关键节点 → 收尾，其余全删",
}


FILLER_WORDS = ("嗯", "啊", "呃", "那个", "这个", "就是", "然后", "对吧", "你知道",
                "怎么说", "就是说", "反正")


def fallback_scores(data):
    """没有模型分时的兜底打分（0~1）：够用就行，目的是让三档仍能区分。

    依据：句子长度、语速偏离、口头禅密度、音量、画面变化。
    这些不需要大模型，纯文本+已有数字就能算。
    """
    segs = data.get("segments") or []
    if llm_gateway.api_enabled():
        return [0.5] * len(segs)
    import re
    feat = data.get("seg_feat") or []
    if not segs:
        return []
    lens = []
    for i, s in enumerate(segs):
        d = max(0.3, float(s[1]) - float(s[0]))
        txt = str(s[2] or "")
        chars = len(re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", txt))
        cps = chars / d
        filler = sum(txt.count(w) for w in FILLER_WORDS)
        lvl = -26.0
        mot = 0.05
        if i < len(feat) and isinstance(feat[i], (list, tuple)) and len(feat[i]) >= 2:
            try:
                mot = float(feat[i][0])
                lvl = float(feat[i][1])
            except (TypeError, ValueError):
                pass
        v = 0.0
        v += 0.30 * (1.0 if 2.0 <= d <= 9.0 else 0.35)      # 句长合适
        v += 0.25 * max(0.0, 1.0 - abs(cps - 4.2) / 3.5)     # 语速接近正常
        v += 0.20 * max(0.0, 1.0 - filler / 2.0)             # 口头禅少
        v += 0.15 * max(0.0, min(1.0, (lvl + 40.0) / 22.0))  # 有声音
        v += 0.10 * max(0.0, min(1.0, mot * 2.0))            # 画面有变化
        if chars < 3:                                        # 太短的语气句
            v *= 0.5
        lens.append(max(0.0, min(1.0, v)))
    # 兜底分要能当阈值用：原始分常常挤在一个窄band里（实测 0.66~0.80），
    # 阈值 0.45/0.60 全落在下面 → 三档全留、看起来一模一样。
    # 所以掺一半名次分（0~1 均匀分布），让阈值等价于留前百分之多少。
    n = len(lens)
    if n > 1:
        order = sorted(range(n), key=lambda i: lens[i])
        rank = [0.0] * n
        for pos, i in enumerate(order):
            rank[i] = pos / float(n - 1)
        lens = [0.5 * lens[i] + 0.5 * rank[i] for i in range(n)]
    return [round(x, 4) for x in lens]


def scores_of(data):
    """取打分：优先模型分，缺了就用兜底分（并保证长度对齐）。"""
    segs = data.get("segments") or []
    if llm_gateway.api_enabled():
        return [0.5] * len(segs), "api_neutral"
    sc = data.get("scores") or []
    if len(sc) == len(segs) and any(float(x or 0) > 0 for x in sc):
        return [float(x) for x in sc], "model"
    fb = fallback_scores(data)
    return fb, "fallback"

def protected_intro(data):
    """受保护的片头区间：从第一句说话开始，到自我介绍结束。

    为什么要往前兜：模型识别"自我介绍"常常只圈到主体那几句，
    把前面的"哈喽各位兄弟们好"漏在外面；重度剪辑时它就会被当成普通句子剪掉，
    结果成片一开头就是"我们今天新整包…"，开场白没了。
    """
    intro = list(find_intro(data) or [])
    sp = data.get("segments") or []
    if not sp:
        return intro
    first = float(sp[0][0])
    if intro:
        end = max(float(b) for _a, b in intro)
        if float(intro[0][0]) - first <= 25.0:
            return [(first, end)]
        return intro
    return [(first, first + 40.0)]


def select_whole_chapters(data, conf=None, level="medium", trim_ratio=0.15):
    """按章整块保留：章是留/丢的最小单位，章内几乎不抽句。

    为什么：逐句抽取会让"这一句留、下一句丢"，观众看到的是
    "画面 A → 画面 C"，剧情自然断。整章保留时间上有连续性，
    就算长了点，剧情是连着的（子枫要的就是这个）。
    """
    _require_local_content_selection()
    segs = data.get("segments") or []
    sections = (data.get("script") or {}).get("sections") or []
    if not segs or not sections:
        return None
    scores, _src = scores_of(data)
    # 每章算分
    info = []
    for sec in sections:
        sa, sb = float(sec.get("start", 0)), float(sec.get("end", 0))
        idx = [i for i, s in enumerate(segs)
               if sa - 0.01 <= float(s[0]) <= sb + 0.01]
        if not idx:
            continue
        vals = [scores[i] for i in idx if i < len(scores)]
        advice = str(sec.get("advice") or "")
        base = float(np.mean(vals)) if vals else 0.5
        if "全部" in advice:
            base += 0.12
        if "大幅" in advice:
            base -= 0.10
        info.append({"sec": sec, "idx": idx, "score": base,
                     "dur": float(segs[idx[-1]][1]) - float(segs[idx[0]][0])})
    if not info:
        return None
    # 重度：只留前半（按分数排序，留到总量 55%）；中/轻：全留
    keep = [True] * len(info)
    if level == "heavy":
        order = sorted(range(len(info)), key=lambda k: -info[k]["score"])
        total = sum(x["dur"] for x in info)
        acc = 0.0
        for k in order:
            if acc + info[k]["dur"] > total * 0.55:
                keep[k] = False
            else:
                acc += info[k]["dur"]
    # 章内修剪：只删"明显口水"（分数最低那部分），上限 trim_ratio
    out = []
    for k, item in enumerate(info):
        if not keep[k]:
            continue
        idx = item["idx"]
        sec_dur = item["dur"]
        vals = np.array([scores[i] if i < len(scores) else 0.5 for i in idx])
        order = np.argsort(vals)
        removed = 0.0
        budget = sec_dur * trim_ratio
        drop = set()
        for pos in order:
            i = idx[pos]
            d = float(segs[i][1]) - float(segs[i][0])
            if removed + d > budget:
                continue
            if vals[pos] > 0.35:          # 分数不算差就别删
                continue
            drop.add(i)
            removed += d
        cur = None
        for i in idx:
            if i in drop:
                if cur:
                    out.append(tuple(cur))
                    cur = None
                continue
            a, b = float(segs[i][0]), float(segs[i][1])
            if cur and a - cur[1] <= 2.5:
                cur[1] = b
            else:
                if cur:
                    out.append(tuple(cur))
                cur = [a, b]
        if cur:
            out.append(tuple(cur))
    return out if out else None
def _target_minutes():
    """设置里的目标时长（分钟）；0 或没设 = 自动。"""
    try:
        import json as _j
        p = os.path.join(BASE, "conf", "settings.json")
        if os.path.isfile(p):
            v = _j.load(open(p, encoding="utf-8")).get("target_min", "0")
            return max(0.0, float(v or 0))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def apply_cut_level(data, segs, conf, level="light", on_log=None, keep_ratio=None):
    """按剪辑力度返回最终保留片段（已经过完整度修补与长度控制）。

    segs：已经删过空屏的片段（detect_cuts_by_semantics 的结果）
    为什么要有这个函数：以前力度只在两处生效（剧本阈值、导演提示词），
    但最后的"长度预算"会无条件把总时长压到同一区间，
    结果三档出片几乎一样长——用户实测"力度完全没用"。
    现在三档的**长度目标**和**取舍依据**都不一样：
        light  ：不挑内容、不设目标 → 时长≈全部说话
        medium ：按剧本/分数挑，目标 13~25 分钟（频道安全区）
        heavy  ：只留剧本里"关键节点"，目标 ≤30% 且不低于 8 分钟
    """

    _require_local_content_selection()
    if keep_ratio is not None:
        import cut_strength
        keep_ratio = cut_strength.normalize_ratio(keep_ratio)
        level = cut_strength.level_for_ratio(keep_ratio)
    level = level if level in LEVEL_LABEL else "light"
    dur = float(data.get("duration") or 0)
    log = on_log or (lambda _m: None)
    # 手动丢弃清单（重录等）：先减掉，再走后面的挑选
    try:
        _drops = manual_drops_of(data)
        if _drops:
            _sp = subtract([(float(a), float(b)) for a, b in segs],
                           [(a, b) for a, b, _src in _drops])
            log("手动丢弃 %d 段（%s）：%.1f → %.1f 分钟"
                % (len(_drops),
                   "、".join("%.0f-%.0f 秒" % (a, b) for a, b, _s in _drops[:6]),
                   sum(b - a for a, b in segs) / 60,
                   sum(b - a for a, b in _sp) / 60))
            segs = _sp
    except Exception as exc:  # noqa: BLE001
        log("手动丢弃清单读取失败：%s" % str(exc)[:60])

    # 语义重复剔除（子枫 2026-11-23 需求 2）：
    # 「把上下两句、或者上下两段文字语义相近的内容剔除」。
    # 之前 dedupe 只在命令行分支里跑，软件走 autopilot 时根本不执行。
    _sel0 = conf.get("select", {}) or {}
    if str(_sel0.get("auto_dedupe", "1")) in ("1", "true", "True"):
        try:
            import dedupe as _dd0
            _dups0 = _dd0.find_duplicates(data, verbose=log)
            if _dups0:
                _b0 = sum(float(b) - float(a) for a, b in segs)
                segs = subtract([(float(a), float(b)) for a, b in segs],
                                _dd0.drop_spans(_dups0))
                _a0 = sum(float(b) - float(a) for a, b in segs)
                log("语义重复：%d 组，去掉 %.1f 秒（只留更完整的一版）"
                    % (len(_dups0), _b0 - _a0))
            # 短时间重录：同一段内容几十秒内说两次（第一次没说好立刻重录）。
            # 子枫的例子：280 秒「我们把最后一组金块给他全部处理完」
            #            287 秒「这里我们把最后一组金块给他处理完」
            _rt0 = _dd0.find_short_retakes(data, verbose=log)
            if _rt0:
                _b1 = sum(float(b) - float(a) for a, b in segs)
                segs = subtract([(float(a), float(b)) for a, b in segs],
                                [(float(r["drop"][0]), float(r["drop"][1]))
                                 for r in _rt0])
                _a1 = sum(float(b) - float(a) for a, b in segs)
                log("短时间重录：%d 组，去掉 %.1f 秒（保留后说的那次）"
                    % (len(_rt0), _b1 - _a1))
        except Exception as _ded_err:  # noqa: BLE001
            log("语义重复检查跳过：%s" % str(_ded_err)[:60])

    # 自动白录检测：规律（报菜名→跑偏→重报）+ 本地模型裁决
    try:
        _sel = conf.get("select", {})
        if str(_sel.get("auto_retake", "1")) in ("1", "true", "True"):
            import retake_pattern as _rp
            _ab = _rp.find_aborted_attempts(data)
            for _x in _ab:
                _x["keep_start"] = _x["end"]
            if _ab and str(_sel.get("retake_llm", "1")) in ("1", "true", "True"):
                try:
                    import retakes_v2 as _r2
                    log("白录候选 %d 段，交本地模型裁决…" % len(_ab))
                    _ab = _r2.adjudicate(data, _ab, on_log=log)
                except Exception as _exc:  # noqa: BLE001
                    from llm_gateway import propagate_api_error
                    propagate_api_error(_exc)
                    log("重录裁决跳过（%s），只用规律结果" % str(_exc)[:40])
            if str(_sel.get("retake_sentence", "0")) in ("1", "true", "True"):
                try:
                    import retakes_v2 as _r2
                    _ab += _r2.drop_sentence_repeats(data)
                except Exception:  # noqa: BLE001
                    pass
            if _ab:
                segs = subtract([(float(a), float(b)) for a, b in segs],
                                [(x["start"], x["end"]) for x in _ab])
                for _x in _ab[:5]:
                    log("剪掉白录：%.1f~%.1f 秒（%.0f 秒）%s"
                        % (_x["start"], _x["end"], _x["dur"], str(_x.get("why", ""))[:46]))
            else:
                log("白录检测：没发现（规律+模型都认为没有重复录制）")
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        log("白录检测跳过：%s" % str(exc)[:60])

    # 索引质量检查：缺语义分/剧本时大声说出来（以前是静默降级，
    # 结果中度和重度都拿不到内容，出片一模一样，用户以为是力度没生效）
    try:
        _sc0, _src0 = scores_of(data)
        _has_script = bool((data.get("script") or {}).get("sections"))
        if _src0 == "fallback" or not _has_script:
            log("⚠ 这份索引缺语义分/剧本（分析时本地模型没跑成）→ 用兜底打分，"
                "效果会差一些；重新分析一次可修复")
    except Exception:  # noqa: BLE001
        pass
    intro = protected_intro(data)
    if intro:
        log("片头保护区：%.1f~%.1f 秒（开头必须是介绍）" % (intro[0][0], intro[-1][1]))
    if not segs:
        return []

    if level == "light" and (keep_ratio is None or keep_ratio >= 0.999):
        # 只删空屏：不挑内容，也不压长度（切点仍然吸附到气口 + 精确边界）
        out = snap_cuts_to_pauses(data, [(float(a), float(b)) for a, b in segs])
        log("力度=%s：只删空屏，说话内容全留（%.1f 分钟）"
            % (LEVEL_LABEL[level], sum(b - a for a, b in out) / 60))
        return out

    # ---- 中/重：先按内容挑 ----
    units = None
    # 体裁模式（游戏实况 14.5 分钟、每章保底）：优先级高于逐章整块/逐句挑选
    _genre = str(conf.get("select", {}).get("genre", "gameplay") or "").strip()
    if level in ("medium", "heavy") and _genre and _genre != "off":
        try:
            import genre as _genre_mod
            _g = _genre_mod.allocate(data, conf, level=level, name=_genre)
            if _g:
                units = _g
                for _ln in _genre_mod.describe(
                        data, _g, _genre, level=level).splitlines():
                    log(_ln)
        except Exception as _exc:  # noqa: BLE001
            log("体裁模式跳过（%s）" % str(_exc)[:60])

    # 整章保留（剧情连贯优先）：只在配置开启且是 medium/heavy 时用
    if (not units) and level in ("medium", "heavy") and str(conf.get("select", {}).get("chapter_mode", "1")) in ("1", "true", "True"):
        try:
            units = select_whole_chapters(data, conf, level=level)
            if units:
                log("按章整块保留：%d 段连续内容（章内只修剪口水）" % len(units))
        except Exception as exc:  # noqa: BLE001
            log("整章模式跳过：%s" % str(exc)[:60])
    try:
        if not units:
            units = select_by_script(data, conf, level=level)
    except TypeError:
        units = select_by_script(data, conf)
    except Exception as exc:  # noqa: BLE001
        log("剧本筛选没跑成（%s），退回逐句挑选" % str(exc)[:50])
    if units:
        log("力度=%s：按剧本挑 %d 段（%s）"
            % (LEVEL_LABEL[level], len(units), LEVEL_HINT[level]))
    else:
        # 没有剧本：导演结果优先，其次按句子分数（阈值随力度变）
        dir_spans = data.get("director") or []
        if dir_spans:
            units = [(float(a), float(b)) for a, b in dir_spans]
            log("力度=%s：没有剧本，用大模型导演结果 %d 段"
                % (LEVEL_LABEL[level], len(units)))
        else:
            thr = 0.45 if level == "medium" else 0.60
            units = select_by_sentence(data, conf, None, score_threshold=thr)
            if units:
                log("力度=%s：按句子分数（阈值 %.2f）挑 %d 段"
                    % (LEVEL_LABEL[level], thr, len(units)))

    if not units:
        # 没有剧本/导演/模型分：用兜底打分再挑一次，否则中度和重度会一模一样
        _fb, _src = scores_of(data)
        if _fb and _src == "fallback":
            _thr = 0.45 if level == "medium" else 0.60
            units = select_by_sentence(data, conf, None, score_threshold=_thr)
            if units:
                log("索引缺少语义/评分数据，改用兜底打分（阈值 %.2f）挑 %d 段"
                    % (_thr, len(units)))
    if not units:
        log("力度=%s：挑不出内容，保留全部说话（退化为轻度）" % LEVEL_LABEL[level])
        return [(float(a), float(b)) for a, b in segs]

    # ---- 完整度修补（切点不落在半句上）----
    try:
        units = completeness.extend_spans(data, units)
        units, _sf = completeness.repair_by_semantics(data, units)
        units, _emo = extend_by_emotion(data, units)
    except Exception:  # noqa: BLE001
        pass

    # 先和"空段"求交：预算要作用在真正会输出的片段上，
    # 否则先预算再求交，时长会缩水（实测要 12 分钟只出 9.3）
    units = intersect(segs, units)
    if not units:
        return [(float(a), float(b)) for a, b in segs]

    # ---- 片头无条件保留（开头必须是介绍）----
    if intro:
        units = [(float(a), float(b)) for a, b in units]
        # 用并集把片头并进去。以前是"和任何一段重叠就跳过"，
        # 结果片头 16.7~29.1 和第一段 21.9~58.8 一重叠，整个片头就被丢了，
        # 成片开头变成"我们今天新整包…"，问候没了。
        for ia, ib in intro:
            ia, ib = float(ia), float(ib)
            keep, ma, mb = [], ia, ib
            for a, b in units:
                if ib <= a or ia >= b:
                    keep.append((a, b))
                else:
                    ma, mb = min(ma, a), max(mb, b)
            keep.append((ma, mb))
            units = sorted(keep)
        log("片头已并入最前（%.1f~%.1f 秒）" % (intro[0][0], intro[-1][1]))

    # ---- 内容优先：默认不设目标时长，只在超过"上限"时裁 ----
    # （子枫 2026-11-21：不要强求时长，尽量少损失内容、剧情完整）
    _user_cap_min = _target_minutes()             # 0 = 没有手动上限
    units = ensure_chapter_openers(data, units, on_log=log)
    before = sum(b - a for a, b in units)
    if _user_cap_min > 0 and before > _user_cap_min * 60.0:
        units = enforce_budget(data, units, _user_cap_min * 60.0)
        log("超过上限 %.0f 分钟 → 按价值裁到 %.1f 分钟"
            % (_user_cap_min, sum(b - a for a, b in units) / 60))
    else:
        log("未设置手动时长上限：当前保留 %.1f 分钟（内容/剧情优先）"
            % (before / 60))
    # 切点吸附到气口：数据上人的刀都落在停顿里，我们原来一半落在小缝里
    _n0 = len(units)
    units = snap_cuts_to_pauses(data, units)
    # 注：refine_span_edges（按能量谷精确定边）实测让边界落在有声帧从 40% 升到 46%，
    # 反而更差，所以剪辑不用它；它留给字幕（时间准不准观众一眼看得出）。
    units = split_long_spans(data, units, max_span=55.0)
    if len(units) != _n0:
        log("气口吸附：%d → %d 段" % (_n0, len(units)))
    # 刀口对齐原片镜头切换点（只在开启时做；画面识别结果有缓存）
    try:
        if str(conf.get("transition", {}).get("shot_snap", "1")) in ("1", "true", "True"):
            units, (_al, _tot) = snap_seams_to_shots(
                data, units, tol=float(conf.get("transition", {}).get("shot_snap_tol", 0.6)))
            if _al:
                log("刀口对齐镜头切换点：%d/%d 刀落在原片本来就切镜头的点上" % (_al, _tot))
    except Exception as exc:  # noqa: BLE001
        log("镜头切换点对齐跳过：%s" % str(exc)[:60])

    # 收尾：按档位上限压一次 + 补骨架（中间步骤总会把内容拉长，这里兜底）
    try:
        _spoken = sum(float(b) - float(a) for a, b in segs)
        # 力度改成百分比（子枫 2026-11-23）：20~90%，数值越高成片越长。
        # 传了 keep_ratio 就用它（0.2~0.9），否则退回原来的三档。
        if keep_ratio is not None:
            try:
                _level_cap_ratio = max(0.15, min(1.0, float(keep_ratio)))
            except (TypeError, ValueError):
                _level_cap_ratio = {"medium": 0.80, "heavy": 0.55}.get(level, 1.0)
        else:
            _level_cap_ratio = {"medium": 0.80, "heavy": 0.55}.get(level, 1.0)
        # 用百分比时目标就是 keep_ratio × 有效说话时长，不再套 15.5/11 分钟硬上限
        # （那是三档时代的频道安全区）。子枫的规则：50% = 剧本压到一半。
        if keep_ratio is not None:
            _target2 = _level_cap_ratio * _spoken
        else:
            _target2 = min(_level_cap_ratio * _spoken,
                           15.5 * 60 if level == "medium" else 11.0 * 60)
        import genre as _g2
        _before = sum(b - a for a, b in units)
        units = _g2.ensure_spine(data, units, _target2, level=level)
        _after = sum(b - a for a, b in units)
        if abs(_before - _after) > 1.0:
            log("档位收尾：%.1f → %.1f 分钟（上限 %.1f 分钟）"
                % (_before / 60, _after / 60, _target2 / 60))
    except Exception as _exc:  # noqa: BLE001
        log("档位收尾跳过：%s" % str(_exc)[:50])

    # 章内节奏：最后一步按 ~11 秒切碎（切在停顿上）。
    # 必须放在气口吸附/镜头点对齐之后，否则会被合并回大块（实测中位 11.7 → 20.2 秒）。
    try:
        _gname = str(conf.get("select", {}).get("genre", "gameplay") or "gameplay")
        if _gname not in ("", "off") and level in ("medium", "heavy") and units:
            import rhythm as _rhythm
            _n0 = len(units)
            units = _rhythm.split_blocks(data, [(float(a), float(b)) for a, b in units],
                                        target=11.0, max_len=36.0)
            _st = _rhythm.stats(units)
            log("章内节奏：%d → %d 段，中位 %.1f 秒（子枫 11.6 秒）"
                % (_n0, _st["n"], _st["median"]))
            # 切碎会让总长涨回去（切口算额外帧），这里再收一次预算
            import genre as _genre
            # 先算 _cur 再当兜底：原来写成 `sum(...) or _cur`，而 `_cur` 在下面
            # 才赋值——segs 为空时这里会 UnboundLocalError（or 短路平时掩盖了它，
            # 2026-11-24 闸门 F821 揪出来的）。
            _cur = sum(float(b) - float(a) for a, b in units)
            _spoken = sum(float(b) - float(a) for a, b in segs) or _cur
            _target = (_level_cap_ratio * _spoken
                       if keep_ratio is not None
                       else _genre.suggest_budget(data, _gname, level=level))
            if keep_ratio is None and dur and dur < 12 * 60:
                if level == "heavy":
                    _target = min(_target, dur * 0.55, _spoken * 0.55)
                else:
                    # 中度短素材尽量保留完整，只清掉明显无效内容。
                    _target = max(dur * 0.85, min(_target, dur * 0.95))
            if _cur > _target * 1.08:
                units = _genre.ensure_spine(data, units, _target, level=level)
                log("预算复核：%d 段 / %.1f 分钟（目标 %.1f 分钟）"
                    % (len(units), sum(b - a for a, b in units) / 60, _target / 60))
    except Exception as _exc:  # noqa: BLE001
        log("章内切碎跳过：%s" % str(_exc)[:50])

    after = sum(b - a for a, b in units)
    if _user_cap_min > 0 and before > _user_cap_min * 60.0:
        log("力度=%s：上限 %.0f 分钟，实得 %.1f 分钟（裁掉 %.1f）"
            % (LEVEL_LABEL[level], _user_cap_min, after / 60,
               (before - after) / 60))
    else:
        if keep_ratio is not None:
            log("剪辑保留比例=%.0f%%：实得 %.1f 分钟（有效内容预算）"
                % (keep_ratio * 100.0, after / 60))
        else:
            log("力度=%s：实得 %.1f 分钟（档位预算）"
                % (LEVEL_LABEL[level], after / 60))
    return units


def select_by_sentence(data, conf, target_seconds=None, min_piece=1.2,
                       bridge_gap=2.5, score_threshold=None):
    """按句子挑内容，而不是整张卡片一起留/一起扔。

    为什么必须按句子：卡片现在有几十秒甚至上百秒（段落），
    整张留会把无关内容带进来，整张扔又会丢掉卡里的好句子。
    索引里每句都有模型分，直接按句挑最准。

    返回 [(start, end)]，两端都是句子边界，不会切半句。
    """
    _require_local_content_selection()
    segs = data.get("segments") or []
    scores, _src = scores_of(data)
    if not segs:
        return None
    # 优先用 cutvalue 学出来的句子价值（它比索引里的模型分能区分
    # "人工会留"和"人工会丢"——实测模型分两者几乎一样，0.49 vs 0.48）
    # 注：试过用学出来的句子价值替换模型分，三期留一验证下来 F1 打平
    # （0.59 vs 0.59），没有可靠增益，所以默认不用它——留着 cutvalue.py
    # 和 conf/cut_value.json 供以后数据变多再试。这里只做安全检查。
    try:
        import cutvalue  # noqa: F401  保留模块引用，便于以后切换
    except Exception:  # noqa: BLE001
        pass
    if len(scores) != len(segs):
        return None
    items = []
    for i, sg in enumerate(segs):
        a, b = float(sg[0]), float(sg[1])
        if b - a < 0.3:
            continue
        items.append((float(scores[i]), a, b, i))
    if not items:
        return None
    if score_threshold is None:
        score_threshold = 0.30        # 默认只删明确废话，不做精选
    items.sort(key=lambda x: -x[0])
    picked = []
    acc = 0.0
    # 不设比例时：模型分 ≥ 阈值就留（由模型自己决定留多少）
    limit = float(target_seconds) if target_seconds else float("inf")
    for sc, a, b, idx in items:
        if acc >= limit:
            break
        if not target_seconds and sc < score_threshold:
            continue
        picked.append((a, b))
        acc += (b - a)
    if not picked:
        return None
    picked.sort()
    # 片头（自我介绍）无条件保留，并排在最前面
    intro = find_intro(data)
    if intro:
        picked = sorted(set([(round(a, 3), round(b, 3)) for a, b in intro]
                            + [(round(a, 3), round(b, 3)) for a, b in picked]))
    out = [list(picked[0])]
    for a, b in picked[1:]:
        if a - out[-1][1] <= bridge_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    out = [(a, b) for a, b in out if b - a >= min_piece]
    return out or None


def extend_by_emotion(data, spans, table=None):
    """按情绪给片尾加时长。

    这是之前定过的规则，但只在批量导出里实现了，剪辑链路漏了：
      惊讶 / 激动  → 片尾 +1.2 秒（表情、反应别剪断）
      笑声        → 片尾 +0.8 秒
      其他        → 不加
    返回 (新spans, 加长的段数)。
    """
    table = table or {"惊讶": 1.2, "笑声": 0.9}   # 激动太常见，不单独加长
    cards = data.get("cards") or []
    if not spans or not cards:
        return spans, 0
    dur = float(data.get("duration") or 0) or 1e9
    # 卡片和句子是一对一的，直接用时间找情绪
    marks = []
    for c in cards:
        emo = c.get("emotion") or ""
        if emo in table:
            marks.append((float(c["start"]), float(c["end"]), table[emo]))
    if not marks:
        return spans, 0
    fixed = 0
    out = []
    for a, b in spans:
        extra = 0.0
        for s0, s1, pad in marks:
            # 结尾前 3 秒内出现过情绪句，也算（一句片段常常含好几句）
            if s1 <= b + 0.35 and s1 >= b - 3.0:
                extra = max(extra, pad)
                break
        if extra:
            fixed += 1
        out.append((a, min(dur, b + extra)))
    return out, fixed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("index", nargs="?", help="index.json 或索引目录")
    ap.add_argument("--out", default=None, help="输出文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不出片")
    ap.add_argument("--no-cut", action="store_true", help="不剪空段，只拼接")
    ap.add_argument("--force-crossfade", action="store_true", help="所有接缝都用叠化")
    ap.add_argument("--conf", default=None)
    ap.add_argument("--make-conf", action="store_true", help="写出默认配置后退出")
    ap.add_argument("--director", action="store_true",
                    help="先用本地大模型当导演判断留哪些句子，再剪辑")
    ap.add_argument("--tracks", default=None,
                    help="保留哪几条音轨，从 1 开始，逗号分隔。例：--tracks 2,3")
    ap.add_argument("--preview", action="store_true",
                    help="快速预览：降分辨率+ultrafast，编码快几倍，用来确认剪得对不对")
    ap.add_argument("--plan", default=None,
                    help="按已有的剪辑计划重出（可手改里面的 action 再跑）")
    ap.add_argument("--no-plan", action="store_true", help="不写剪辑计划文件")
    ap.add_argument("--burn-subs", action="store_true",
                    help="出片时把字幕烧进画面（另外总会生成成片字幕 SRT/ASS）")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="不查重复录制（默认会查，重复的只留更完整的一版）")
    ap.add_argument("--cut-level", default=None,
                    choices=["light", "medium", "heavy"],
                    help=argparse.SUPPRESS)
    ap.add_argument("--keep-percent", type=float, default=None,
                    help="目标内容保留百分比，15~100；例如 62 表示保留约 62%%")
    ap.add_argument("--no-bgm", action="store_true",
                    help="不自动配 BGM（默认按 conf/settings.json 的 bgm 开关）")
    args = ap.parse_args(argv)

    if args.make_conf:
        write_default_conf()
        return 0
    if not args.index:
        ap.print_help()
        return 1

    try:
        _require_local_content_selection()
    except llm_gateway.APIRequestError as exc:
        ap.error(str(exc))
    conf = load_conf(args.conf)
    data = load_index(args.index)
    src = data["source"]
    duration = float(data["duration"])
    print("=" * 66)
    print("  自动剪辑 · %s" % data.get("name", ""))
    print("  时长 %.1f 分钟" % (duration / 60))
    print("=" * 66)

    if args.plan:
        plan = json.load(open(args.plan, encoding="utf-8"))
        cuts = cuts_from_plan(plan)
        report = plan.get("blanks") or []
        print("按计划重出：%s（%d 条空白判定，剪 %d 段）"
              % (args.plan, len(report), len(cuts)))
    elif args.no_cut:
        cuts, report = [], []
        print("按 --no-cut：跳过空段检测")
    else:
        cuts, report = detect_cuts(data, conf)
    _sem = detect_cuts_by_semantics(data, conf)
    if _sem is not None:
        print("【空段】按语义判断：%d 段，剪掉 %.1f 分钟（不导电平）"
              % (len(_sem), sum(b - a for a, b in _sem) / 60))
        cuts = _sem
        print("\n【空段判定】共 %d 处候选" % len(report))
        for r0 in sorted(report, key=lambda x: x["start"]):
            print("  %8.1f ~ %8.1f  (%5.1fs)  %-5s  %s"
                  % (r0["start"], r0["end"], r0["length"], r0["action"], r0["why"]))
        total_cut = sum(b - a for a, b in cuts)
        print("  → 共剪掉 %.1f 秒（%.1f%%）" % (total_cut, total_cut / duration * 100))

    segments = keep_segments(cuts, duration)
    segments = finalize_segments(segments, load_units(data),
                                verbose=lambda m: print("\n【清理】" + m))
    if not args.no_dedupe:
        try:
            import dedupe as _dd
            _dups = _dd.find_duplicates(data, verbose=lambda m: print(m))
            if _dups:
                _b = sum(b - a for a, b in segments)
                segments = subtract(segments, _dd.drop_spans(_dups))
                _a = sum(b - a for a, b in segments)
                print("【重复】%d 组，去掉 %.1f 分钟（只留更完整的一版）"
                      % (len(_dups), (_b - _a) / 60))
            else:
                print("【重复】没发现重复内容")
        except Exception as exc:  # noqa: BLE001
            print("【重复】检测跳过：%s" % str(exc)[:80])
    print("\n【保留片段】%d 段，合计 %.1f 分钟"
          % (len(segments), sum(b - a for a, b in segments) / 60))

    # 粗剪模式：不挑内容，全保留说话部分，只去掉空段和噪声
    _rough = False
    try:
        import json as _json
        _sp = os.path.join(BASE, "conf", "settings.json")
        if os.path.isfile(_sp):
            _rough = str(_json.load(open(_sp, encoding="utf-8"))
                         .get("rough", "0")) in ("1", "true", "True")
    except Exception:  # noqa: BLE001
        pass
    if _rough:
        print("【粗剪模式】全保留说话内容，只删空段与噪声")

    # ---- 用户设置百分比；旧档位只作为兼容策略 ----
    _lvl = getattr(args, "cut_level", None)
    _keep_ratio = None
    if args.keep_percent is not None:
        import cut_strength as _cut_strength
        _keep_ratio = _cut_strength.normalize_ratio(args.keep_percent)
        _lvl = _cut_strength.level_for_ratio(_keep_ratio)
    elif not _lvl:
        try:
            import json as _jl
            import cut_strength as _cut_strength
            _sp = os.path.join(BASE, "conf", "settings.json")
            if os.path.isfile(_sp):
                _settings = _jl.load(open(_sp, encoding="utf-8")) or {}
                if _settings.get("keep_ratio") is not None:
                    _keep_ratio = _cut_strength.ratio_from_settings(_settings)
                    _lvl = _cut_strength.level_for_ratio(_keep_ratio)
                else:
                    _lvl = str(_settings.get("cut_level", "light"))
        except Exception:  # noqa: BLE001
            _lvl = "light"
    _lvl = _lvl if _lvl in LEVEL_LABEL else "light"
    if _keep_ratio is not None:
        print("\n【目标保留】%.0f%%" % (_keep_ratio * 100.0))
    else:
        print("\n【兼容力度】%s" % LEVEL_LABEL[_lvl])
    _seg_before = sum(b - a for a, b in segments)
    segments = apply_cut_level(data, segments, conf, _lvl, on_log=print,
                               keep_ratio=_keep_ratio)
    print("【保留片段】%d 段，合计 %.1f 分钟（力度前 %.1f 分钟）"
          % (len(segments), sum(b - a for a, b in segments) / 60,
             _seg_before / 60))
    kept_cards = segments

    print("\n【转场判定】")
    if args.force_crossfade:
        seams = [{"i": i, "kind": "crossfade", "why": "强制叠化"}
                 for i in range(len(segments) - 1)]
        print("  按 --force-crossfade：全部叠化")
    else:
        seams = judge_transitions(segments, src, data, conf)
    n_cut = sum(1 for s in seams if s["kind"] == "cut")
    print("  → 硬切 %d 处，叠化 %d 处" % (n_cut, len(seams) - n_cut))

    if args.dry_run:
        print("\n（--dry-run，不出片）")
        return 0

    if args.preview:
        conf["render"]["scale"] = conf["render"].get("preview_scale", "1280:-2")
        print("预览模式：输出 %s，用 ultrafast 编码" % conf["render"]["scale"])

    # 传进来可能是索引目录，也可能是 index.json；剪辑计划要落在同一个目录
    idx_dir = os.path.abspath(args.index)
    if os.path.isfile(idx_dir):
        idx_dir = os.path.dirname(idx_dir)
    if not args.no_plan and not args.plan:
        plan_path = os.path.join(idx_dir, "剪辑计划.json")
        save_plan(plan_path, data, report, cuts, duration,
                  {"seams": [{"i": x["i"], "kind": x["kind"], "why": x.get("why", "")}
                             for x in seams]})
        print("剪辑计划：%s（想反悔就改里面的 action，再跑 --plan 重出）" % plan_path)

    suffix = "_预览.mp4" if args.preview else "_剪辑.mp4"
    if args.director and not data.get("director"):
        import director
        print("【导演】让本地大模型通读台词、决定留哪些…")
        director.apply(os.path.join(idx_dir, "index.json"))
        data = load_index(idx_dir)

    out = args.out or os.path.join(idx_dir, (data.get("name") or "output") + suffix)
    _tracks = None
    if args.tracks:
        _tracks = [int(x) - 1 for x in args.tracks.replace("，", ",").split(",")
                   if x.strip().isdigit()]
    _last = [-1]

    def _pg(f):
        pct = int(f * 100)
        if pct - _last[0] >= 5 or pct >= 100:
            _last[0] = pct
            print("  渲染进度 %d%%" % pct, flush=True)

    _subs = None
    try:
        import subtitles as _submod
        _subs = _submod.get_segments(data)
    except Exception:  # noqa: BLE001
        _subs = None
    _bgm_on = not args.no_bgm
    if _bgm_on:
        try:
            import json as _json
            _sp = os.path.join(BASE, "conf", "settings.json")
            if os.path.isfile(_sp):
                _bgm_on = str(_json.load(open(_sp, encoding="utf-8"))
                              .get("bgm", "1")) in ("1", "true", "True")
        except Exception:  # noqa: BLE001
            pass
    _info = render(src, segments, seams, out, conf,
                   on_log=lambda m: print("  " + m), on_progress=_pg,
                   tracks=_tracks, subs=_subs, sub_burn=bool(args.burn_subs),
                   sub_res=out_resolution(conf, data), bgm=_bgm_on)
    if _subs and isinstance(_info, dict) and (_info.get("timeline_v2")
                                               or _info.get("timeline")):
        _r = export_subs(data, out,
                         _info.get("timeline_v2") or _info["timeline"],
                         res=out_resolution(conf, data))
        if _r.get("srt"):
            print("成片字幕：%s（%d 条）" % (_r["srt"], _r["n"]))
    print("\n完成：%s（%.1f MB）" % (out, os.path.getsize(out) / 1048576))
    return 0

if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# 片头识别（语义）
# ---------------------------------------------------------------------------
INTRO_SYS = ("你是视频剪辑助手。判断哪些句子是视频开头的自我介绍/开场白。"
             "只输出编号。")

INTRO_PROMPT = """下面是这期实况最前面的几句台词（编号从 1 开始）。

找出属于「开头自我介绍 / 开场白」的句子，特征是：
  · 在跟观众打招呼（不管用词是"大家好"还是"哈喽各位小伙伴们好"）
  · 报自己的名字/身份
  · 说明这期要做什么、接着上期做什么

注意：只是恰好出现在开头的普通游戏解说，不算开场白。

台词：
%s

只输出编号，一行一个。如果没有开场白，输出 0。"""


def find_intro_ai(data, within=420.0, on_log=None, port=8137):
    """用大模型找片头，失败时返回 None（让调用方退回关键词）。"""
    segs = data.get("segments") or []
    if len(segs) < 3:
        return None
    head = [(i, sg) for i, sg in enumerate(segs) if float(sg[0]) <= within][:60]
    if not head:
        return None
    lines = "\n".join("%d. %s" % (k + 1, sg[2]) for k, (_i, sg) in enumerate(head))
    try:
        import proofread as pf
        with pf.Server(task="segment", port=port, api_role="director") as srv:
            raw = srv.chat(INTRO_SYS, INTRO_PROMPT % lines, max_tokens=200)
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        if on_log:
            on_log("  片头识别（模型）没跑成：%s" % str(exc)[:60])
        return None
    import re as _re
    nums = [int(x) for x in _re.findall(r"^\s*(\d{1,3})\s*$", raw or "", _re.M)]
    idxs = [k for k in nums if 1 <= k <= len(head)]
    if not idxs:
        if on_log:
            on_log("  片头识别（模型）：模型说没有开场白")
        return []
    # 命中后连带后面 1~2 句（"今天做 XX"常另起一句）
    picked = set()
    for k in idxs:
        for d in (0, 1, 2):
            if k + d <= len(head):
                picked.add(k + d)
    spans = []
    for k in sorted(picked):
        _i, sg = head[k - 1]
        spans.append((float(sg[0]), float(sg[1])))
    spans.sort()
    out = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= out[-1][1] + 2.0:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    if on_log:
        on_log("  片头识别（模型）：第 %s 句 → %.0f~%.0f 秒"
               % (",".join(map(str, sorted(idxs))), out[0][0], out[0][1]))
    return [(a, b) for a, b in out]

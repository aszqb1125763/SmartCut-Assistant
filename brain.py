# -*- coding: utf-8 -*-
"""计算器门面（编排层）：**不处理数据，只把请求分给对应的小脑。**

子枫 2026-11-24：
    1. 处理原始数据的能力分类打包给小脑，允许加插件，**小脑之间不交流**；
    2. 小脑只接受原始数据、输出结果，**不受本地模型干扰**；
    3. 本地模型只接受小脑输出的数据，它不处理数据，只做决策；
    4. **数据直接分类打包给对应的小脑**——多一类不给、错一类报错。

所以这里没有一行信号处理，也没有"万能数据源"：
    AudioSource → audio_brain（E03/A04/A07/A13/A20）
    WordSource + AudioSource → speech_brain（B04/D03）
    FrameSource → picture_brain（C01–C04）

两个口（给模型的 vs 给审计的）：
    look(at)      → 颜色：绿/黄/红/灰
    probe_*(...)  → 波长：数值 + 口径
"""
from __future__ import annotations

import json
import os

import coords
import decision
import seam_picker as spc

import brains
from brains.base import AudioSource, FrameSource, WordSource

SR = 16000
SCHEMA_VERSION = brains.SCHEMA_VERSION

COLORS = {
    "ok": ("green", "顺"),
    "edge": ("yellow", "边缘"),
    "over": ("red", "卡"),
    None: ("gray", "看不清"),
}


def color_of(grade):
    return COLORS.get(grade, COLORS[None])


def load_words(index_dir):
    return brains.base.load_words(index_dir)


class Calculator:
    """一个素材一个门面：三类原始数据各装一包，分别递给对应的小脑。"""

    def __init__(self, source, ffmpeg=None, track=0, sr=SR, sources=None):
        self._sources = {
            "audio": AudioSource(source, track=track, ffmpeg=ffmpeg, sr=sr),
            "words": WordSource(""),
            "frames": FrameSource(source, ffmpeg=ffmpeg),
        }
        if sources:
            self._sources.update(sources)
        self._brains = {
            "audio": brains.load("audio", audio=self._sources["audio"]),
            "speech": brains.load("speech", words=self._sources["words"],
                                  audio=self._sources["audio"]),
            "picture": brains.load("picture", frames=self._sources["frames"]),
            "text": brains.load("text", words=self._sources["words"]),
        }
        self.index_dir = ""
        self.edges = []

    # -- 原始数据 / 生命周期 ------------------------------------------------
    @property
    def raw_sr(self):
        return self._sources["audio"].sr

    @property
    def audio(self):
        return self._sources["audio"].audio

    @property
    def words(self):
        return self._sources["words"].words

    @property
    def track(self):
        return self._sources["audio"].track

    @property
    def duration(self):
        return self._sources["audio"].duration()

    @property
    def probes(self):
        return sum(brain.probes for brain in self._brains.values())

    def bind_index(self, index_dir):
        self.index_dir = index_dir or ""
        self._sources["words"].set_index(self.index_dir)
        self.edges = []
        path = os.path.join(self.index_dir, "index.json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.edges = spc.sentence_edges(data.get("segments") or [])
            except (OSError, ValueError):
                self.edges = []
        return self

    def close(self):
        for source in self._sources.values():
            source.close()

    # -- 波长层（转给小脑） -------------------------------------------------
    def probe_pair(self, left_t, right_t, with_tail=False):
        return self._brains["audio"].probe_pair(left_t, right_t,
                                                with_tail=with_tail)

    def probe_seam(self, at):
        return self._brains["audio"].probe_seam(at)

    def probe_tail(self, at, span=0.60):
        return self._brains["audio"].probe_tail(at, span=span)

    def probe_region(self, start, end):
        return self._brains["audio"].probe_region(start, end)

    def probe_band(self, start, end):
        return self._brains["audio"].probe_band(start, end)

    def probe_breath(self, start, end):
        return self._brains["audio"].probe_breath(start, end)

    def check_cut_safety(self, at):
        return self._brains["speech"].check_cut_safety(at)

    def probe_voiced(self, start, end):
        return self._brains["speech"].probe_voiced(start, end)

    def energy_shape(self, start, end):
        return self._brains["audio"].energy_shape(start, end)

    def text_span(self, start, end, limit=12):
        return self._brains["text"].span(start, end, limit=limit)

    def axis_snapshot(self, start, end):
        """三轴快照：时间轴统一 → 文字(是什么+多久) + 能量(形态)。"""
        return {
            "t0": round(float(start), 3), "t1": round(float(end), 3),
            "timeline": "source",
            "text": self.text_span(start, end),
            "energy": self.energy_shape(start, end),
            "note": "时间轴全程同步：能量轴自带时间信息，模型只看形态即可",
            "schema_version": SCHEMA_VERSION,
        }

    def brief(self, start, end):
        """给模型的三个数据（定性）：说了什么 / 多久 / 能量形态。"""
        return decision.brief(self.axis_snapshot(start, end))

    def probe_picture_jump(self, at, window=0.30):
        return self._brains["picture"].probe_jump(at, window=window)

    def probe_picture_span(self, start, end, fps=2.0):
        return self._brains["picture"].probe_span(start, end, fps=fps)

    def probe_text(self, start, end):
        return self._brains["text"].probe(start, end)

    def text_context(self, start, end, limit=12):
        """给模型的文字包（文字小脑的输出，不是裸台词）。"""
        return self._brains["text"].context(start, end, limit=limit)

    def safe_edges(self, start, end, wanted=("S", "A"), limit=40):
        return self._brains["text"].safe_edges(start, end, wanted=wanted,
                                               limit=limit)

    def edge_grade(self, at):
        return self._brains["text"].edge_grade(at)

    def concept_counts(self, limit=80):
        return self._brains["text"].concept_counts(limit=limit)

    # -- 颜色层（编排：颜色来自小脑档位，这里不重算） ------------------------
    def look(self, at):
        probe = self.probe_seam(at)
        verdict = decision.judge(probe)      # 判断属于决策层，不属于小脑
        color, label = color_of(verdict.get("grade"))
        if verdict.get("row") is None and verdict.get("grade") == "ok":
            why = verdict.get("note") or "三轴连续"
        else:
            why = verdict.get("row") or verdict.get("note") or ""
        out = {"at": round(float(at), 3), "color": color, "label": label,
               "why": why, "clause": probe.get("clause"),
               "row": verdict.get("row"), "severity": verdict.get("severity"),
               "pause_edge": verdict.get("pause_edge", False),
               "tail_chopped": bool(probe.get("tail_chopped"))}
        out.update({"brain": "look", "schema_version": SCHEMA_VERSION,
                    "win_ms": 20, "track": self.track})
        return out

    # -- 搜索层（编排：分别问两个小脑，它俩之间不说话） ----------------------
    def candidates(self, left_end, right_end, top=10, span=None, max_scan=6):
        """在一处空隙两端找可切的位置（局部暴力，不做全片配对）。"""
        left_end = float(left_end)
        right_end = float(right_end)
        gap = max(0.0, right_end - left_end)
        span = float(span or max(coords.SEARCH_SPAN,
                                 min(coords.SEARCH_SPAN_MAX,
                                     gap * coords.SEARCH_RATIO)))
        lefts = [e for e in self.edges
                 if e.get("kind") == "end"
                 and abs(e["t"] - left_end) <= span][:max_scan]
        rights = [e for e in self.edges
                  if e.get("kind") == "start"
                  and abs(e["t"] - right_end) <= span][:max_scan]
        rows = []
        for left in lefts:
            left_t, _ = spc.refine_to_valley(
                self.audio, left["t"], self.raw_sr,
                lo=left.get("gap_lo"), hi=left.get("gap_hi"))
            for right in rights:
                right_t, _ = spc.refine_to_valley(
                    self.audio, right["t"], self.raw_sr,
                    lo=right.get("gap_lo"), hi=right.get("gap_hi"))
                if right_t - left_t < 0.30:
                    continue
                pair = self.probe_pair(left_t, right_t)
                verdict = decision.judge(pair)
                if self.check_cut_safety(left_t)["inside_word"]:
                    continue
                if self.check_cut_safety(right_t)["inside_word"]:
                    continue
                color, label = color_of(verdict["grade"])
                values = pair["values"]
                rows.append({
                    "left": pair["left"], "right": pair["right"],
                    "removed_s": pair["removed_s"],
                    "d_energy": values["energy_gap_db"],
                    "d_centroid": values["tone_gap_hz"],
                    "d_shape": values["shape_rms_db"],
                    "grade": verdict["grade"], "color": color,
                    "label": label, "row": verdict.get("row"),
                    "pause_edge": verdict["pause_edge"],
                    # 中立：不给自己发明权重。排序只看"档位 → 最大轴差"，
                    # 最大轴差 = 各轴超线倍数里的最大值，可解释、无偏好。
                    "worst_axis": round(max(
                        abs(values["energy_gap_db"]) / coords.ENERGY_OK_DB,
                        abs(values["tone_gap_hz"]) / coords.TONE_OK_HZ,
                        abs(values["shape_rms_db"]) / coords.SHAPE_OK_DB), 3),
                    "win_ms": 20, "track": self.track,
                    "schema_version": SCHEMA_VERSION,
                })
        order = {"ok": 0, "edge": 1, "over": 2}
        rows.sort(key=lambda row: (order.get(row["grade"], 3),
                                   row["worst_axis"], row["left"]))
        return rows[:max(1, int(top))]

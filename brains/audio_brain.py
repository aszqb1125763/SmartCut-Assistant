# -*- coding: utf-8 -*-
"""音频小脑：只吃原始音频，只出数值与档位。

覆盖条款：E03（三轴）、A04（三频段）、A07（尾音衰减/A07）、A13（换气点）、
A20/M03（区间静音）、A03（20ms 包络）。

**不许** import 其它小脑，**不许** import 本地模型。
"""
from __future__ import annotations

import numpy as np

import coords

from .base import BrainPlugin
from . import dsp


class AudioBrain(BrainPlugin):
    name = "audio"
    accepts = ("audio",)
    emits = ("energy_gap_db", "tone_gap_hz", "shape_rms_db", "tail_ms",
             "low_db", "voice_db", "high_db", "longest_silence_ms")

    # -- 接缝 ---------------------------------------------------------------
    def probe_pair(self, left_t, right_t, with_tail=False):
        """一对切点：能量/音色/形状 + 档位（E03 / M01 / M07）。"""
        self.probes += 1
        audio = self.src["audio"].audio
        sr = self.src["audio"].sr
        left_t = float(left_t)
        right_t = float(right_t)
        axes = dsp.seam_axes(audio, left_t, right_t, sr)
        left_db = dsp.side_db(audio, left_t, "before", sr)
        right_db = dsp.side_db(audio, right_t, "after", sr)
        left_silent = bool(left_db <= coords.SILENCE_DB)
        right_silent = bool(right_db <= coords.SILENCE_DB)
        # 小脑不下结论：只报左右两个通道各是什么样 + 差值。
        state = {"energy_gap_db": axes["energy_db"],
                 "tone_gap_hz": axes["tone_hz"],
                 "shape_rms_db": axes["shape_db"]}
        tail = None
        tail_chopped = False
        if with_tail:
            tail = self.probe_tail(left_t)
            # 只有"声音真的断了"才算被削（A07）：右边掉到静音，或掉幅 ≥20dB。
            tail_chopped = bool(
                tail.get("shape") == "hard"
                and (right_db <= coords.SILENCE_DB
                     or right_db <= left_db - coords.TAIL_CHOP_DB))
            if tail_chopped and tail.get("decay_ms") is not None:
                state["tail_decay_ms"] = float(tail["decay_ms"])
        out = {
            "probe": "pair", "brain": self.name, "kind": "seam_channels",
            "left": round(left_t, 3), "right": round(right_t, 3),
            "removed_s": round(right_t - left_t, 2),
            "values": state,
            # 左右双通道：模型看这个自己决定（小脑不下结论）
            "channels": {
                "left": {"db": round(left_db, 1),
                         "tone_hz": round(dsp.side_tone(
                             self.src["audio"].audio, left_t, "before",
                             self.src["audio"].sr), 0),
                         "silent": bool(left_silent)},
                "right": {"db": round(right_db, 1),
                          "tone_hz": round(dsp.side_tone(
                              self.src["audio"].audio, right_t, "after",
                              self.src["audio"].sr), 0),
                          "silent": bool(right_silent)},
            },
            "clause": ["E03"],
            "tail_ms": (tail or {}).get("decay_ms"),
            "tail_shape": (tail or {}).get("shape"),
            "tail_grade": (tail or {}).get("grade"),
            "tail_chopped": tail_chopped,
        }
        out.update(self.meta())
        return out

    def probe_seam(self, at):
        """成片上一刀（E03 + A07）。"""
        return self.probe_pair(at, at, with_tail=True)

    # -- 尾音 ---------------------------------------------------------------
    def probe_tail(self, at, span=0.60):
        """尾音衰减时长（A07 / M04）。"""
        self.probes += 1
        env_full = dsp.envelope(self.src["audio"].audio, self.src["audio"].sr)
        frame_ms = dsp.FRAME_MS
        end_frame = int(round(float(at) * 1000.0 / frame_ms))
        start_frame = max(0, end_frame - int(span * 1000.0 / frame_ms))
        env = env_full[start_frame:end_frame]
        base = {"probe": "tail", "brain": self.name,
                "at": round(float(at), 3), "clause": ["A07"]}
        if env.size < 4:
            base.update({"decay_ms": None, "grade": None, "reason": "near_edge"})
            base.update(self.meta())
            return base
        peak_index = int(np.argmax(env))
        peak_db = float(env[peak_index])
        end_db = float(env[max(0, env.size - 2):].mean())
        drop = peak_db - end_db
        if drop < 6.0:
            if peak_db > coords.SILENCE_DB:
                verdict = coords.judge({"tail_decay_ms": 0.0})
                base.update({"decay_ms": 0, "drop_db": round(drop, 1),
                             "grade": "over", "shape": "hard",
                             "row": verdict.get("row"),
                             "severity": verdict.get("severity")})
            else:
                base.update({"decay_ms": None, "drop_db": round(drop, 1),
                             "grade": "ok", "shape": "silent"})
            base.update(self.meta())
            return base
        target = peak_db - coords.TAIL_DROP_RATIO * drop
        decay_ms = None
        for index in range(peak_index, env.size):
            if env[index] <= target:
                decay_ms = (index - peak_index) * frame_ms
                break
        if decay_ms is None:
            decay_ms = (env.size - 1 - peak_index) * frame_ms
        shape = ("hard" if decay_ms < coords.TAIL_HARD_MS
                 else ("natural" if decay_ms >= coords.TAIL_NATURAL_MS
                       else "tight"))
        verdict = coords.judge({"tail_decay_ms": float(decay_ms)})
        base.update({"decay_ms": int(decay_ms), "drop_db": round(drop, 1),
                     "grade": "over" if verdict.get("severity") == "P0" else "ok",
                     "shape": shape, "row": verdict.get("row"),
                     "severity": verdict.get("severity")})
        base.update(self.meta())
        return base

    # -- 区间 ---------------------------------------------------------------
    def probe_region(self, start, end):
        """区间统计：最长连续静音、有没有人在说（A20 / M03）。"""
        self.probes += 1
        audio = self.src["audio"].audio
        i0 = max(0, int(float(start) * self.src["audio"].sr))
        i1 = min(audio.size, int(float(end) * self.src["audio"].sr))
        seg = audio[i0:i1]
        base = {"probe": "region", "brain": self.name,
                "start": round(float(start), 3), "end": round(float(end), 3),
                "clause": ["A20", "M03"]}
        if seg.size < 512:
            base.update({"grade": None, "reason": "too_short"})
            base.update(self.meta())
            return base
        env = dsp.envelope(seg, self.src["audio"].sr)
        quiet = env <= coords.SILENCE_DB
        longest = 0
        run = 0
        for flag in quiet:
            run = run + 1 if flag else 0
            longest = max(longest, run)
        longest_ms = longest * dsp.FRAME_MS
        silence_ratio = float(quiet.mean())
        hollow = longest_ms > coords.HOLLOW_GAP_MS
        base.update({
            "gap_ms": int(round((float(end) - float(start)) * 1000.0)),
            "longest_silence_ms": int(longest_ms),
            "valley_db": round(float(env.min()) if env.size else -120.0, 1),
            "silence_ratio": round(silence_ratio, 3),
            "voice_ratio": round(1.0 - silence_ratio, 3),
            "grade": "over" if hollow else "ok",
            "row": "M03" if hollow else None})
        base.update(self.meta())
        return base

    # -- 频段 / 换气 --------------------------------------------------------
    def probe_band(self, start, end):
        """三频段分层（A04）。"""
        self.probes += 1
        audio = self.src["audio"].audio
        sr = self.src["audio"].sr
        seg = audio[max(0, int(float(start) * sr)):min(audio.size,
                                                       int(float(end) * sr))]
        low = dsp.band_rms(seg, sr, *coords.BAND_LOW_HZ)
        voice = dsp.band_rms(seg, sr, *coords.BAND_VOICE_HZ)
        high = dsp.band_rms(seg, sr, coords.BAND_HIGH_HZ, sr / 2.0 - 100)
        total = dsp.band_rms(seg, sr, 60.0, sr / 2.0 - 100)
        high_ratio = (high - total) if total > -119 else 0.0
        out = {"probe": "band", "brain": self.name,
               "start": round(float(start), 3), "end": round(float(end), 3),
               "low_db": round(low, 1), "voice_db": round(voice, 1),
               "high_db": round(high, 1), "total_db": round(total, 1),
               "high_ratio": round(float(high_ratio), 3),
               "clause": ["A04"], "grade": "ok"}
        out.update(self.meta())
        return out

    def probe_breath(self, start, end):
        """换气点（A13）：100–300ms 静音 + 高频短暂抬升。"""
        self.probes += 1
        audio = self.src["audio"].audio
        sr = self.src["audio"].sr
        seg = audio[max(0, int(float(start) * sr)):min(audio.size,
                                                       int(float(end) * sr))]
        base = {"probe": "breath", "brain": self.name,
                "start": round(float(start), 3), "end": round(float(end), 3),
                "clause": ["A13"]}
        if seg.size < 256:
            base.update({"is_breath": False, "grade": None,
                         "reason": "too_short"})
            base.update(self.meta())
            return base
        gap_ms = (float(end) - float(start)) * 1000.0
        env = dsp.envelope(seg, sr)
        silent_ratio = float((env <= coords.SILENCE_DB).mean())
        band = self.probe_band(start, end)
        in_range = coords.BREATH_MIN_MS <= gap_ms <= coords.BREATH_MAX_MS
        is_breath = bool(in_range and silent_ratio > 0.5
                         and band["high_ratio"] > 0.15)
        base.update({"gap_ms": int(round(gap_ms)),
                     "silent_ratio": round(silent_ratio, 3),
                     "high_ratio": band["high_ratio"],
                     "is_breath": is_breath, "grade": "ok"})
        base.update(self.meta())
        return base

    @staticmethod
    def _shape_label(segments, peak_db):
        """形态标签：全静音却峰值很高时，要说"静音夹响声"，不能只报 silence。"""
        kinds = [row["kind"] for row in segments]
        if not kinds:
            return "未知"
        quiet = sum(1 for kind in kinds if kind == "silence")
        # 大半是静音、峰值却不低 → 这是"短促响声夹在静音里"（笑声/喊叫/敲击）
        if quiet * 2 >= len(kinds) and peak_db > coords.SILENCE_DB:
            return "静音夹响声(%d段，%d段静音)" % (len(kinds), quiet)
        if len(kinds) > 4:
            return "→".join(kinds[:3]) + "…(%d段)" % len(kinds)
        return "→".join(kinds)

    # -- 能量形态（给模型的压缩表示）--------------------------------------
    @staticmethod
    def level_rank(label):
        order = ["极强", "强", "中上", "中", "弱", "静音"]
        return order.index(label) if label in order else len(order)

    @staticmethod
    def level_of(peak_db):
        """六档标签（与 decision.brief 同一套阈值，避免两处口径）。"""
        ladder = (
            (coords.ENERGY_VERY_STRONG_DB, "极强"),
            (coords.ENERGY_STRONG_DB, "强"),
            (coords.ENERGY_UPPER_MID_DB, "中上"),
            (coords.ENERGY_MID_DB, "中"),
            (coords.ENERGY_WEAK_DB, "弱"),
        )
        if peak_db is None:
            return "未知"
        for edge, label in ladder:
            if peak_db >= edge:
                return label
        return "静音"

    def energy_shape(self, start, end, min_run=3, max_segments=12,
                     fps=None):
        """一段声音的能量形态：能量轴自带时间轴，所以每段都带绝对时间。"""
        self.probes += 1
        audio = self.src["audio"].audio
        sr = self.src["audio"].sr
        start, end = float(start), float(end)
        i0 = max(0, int(start * sr))
        i1 = min(audio.size, int(end * sr))
        seg = audio[i0:i1]
        base = {"probe": "energy_shape", "brain": self.name,
                "start": round(start, 3), "end": round(end, 3),
                "duration_s": round(max(0.0, end - start), 3),
                "clause": ["A03", "A04", "A05", "A06"]}
        if seg.size < 512:
            base.update({"grade": None, "reason": "too_short"})
            base.update(self.meta())
            return base
        env = dsp.envelope(seg, sr)
        n = env.size
        frame_s = dsp.FRAME_MS / 1000.0
        # 一帧一个数据：按 fps 采样（点太多就降分辨率），每点一个六档标签
        span_s = max(0.001, end - start)
        rate = float(fps or coords.ENERGY_FPS_DEFAULT)
        if rate * span_s > coords.ENERGY_POINTS_MAX:
            rate = coords.ENERGY_POINTS_MAX / span_s
        rate = max(0.2, min(rate, 60.0))
        step = max(1, int(round(sr / rate / (sr * frame_s))))
        levels = []
        for k in range(0, n, step):
            t = start + k * frame_s
            # 一个采样点代表它那一段（1/fps 秒）的平均电平，
            # 不是取某一帧的瞬时值——否则 5fps 下档位会一直来回跳。
            chunk = env[k:min(n, k + step)]
            value = float(chunk.mean()) if chunk.size else float(env[k])
            levels.append({"t": round(t, 3),
                           "level": self.level_of(value)})
        level_runs = []
        for row in levels:
            if level_runs and level_runs[-1]["level"] == row["level"]:
                level_runs[-1]["t1"] = round(row["t"] + 1.0 / rate, 3)
                level_runs[-1]["points"] += 1
            else:
                level_runs.append({"level": row["level"], "t0": row["t"],
                                   "t1": round(row["t"] + 1.0 / rate, 3),
                                   "points": 1})
        steps = []
        for k in range(1, n):
            delta = float(env[k] - env[k - 1])
            if delta > 1.0:
                steps.append(1)
            elif delta < -1.0:
                steps.append(-1)
            else:
                steps.append(0)
        runs = []
        for k, step in enumerate(steps):
            if runs and runs[-1]["sign"] == step:
                runs[-1]["end"] = k + 1
            else:
                runs.append({"sign": step, "start": k, "end": k + 1})
        merged = []
        for run in runs:
            if run["end"] - run["start"] < min_run and merged:
                merged[-1]["end"] = run["end"]
            else:
                merged.append(dict(run))
        if len(merged) > max_segments:
            stride = len(merged) / float(max_segments)
            keep = [merged[int(i * stride)] for i in range(max_segments)]
            keep[-1]["end"] = merged[-1]["end"]
            merged = keep
        segments = []
        for run in merged:
            a, b = run["start"], min(run["end"], n - 1)
            db0 = float(env[a])
            db1 = float(env[b])
            quiet = db1 <= coords.SILENCE_DB and db0 <= coords.SILENCE_DB
            if quiet:
                kind = "silence"
            elif run["sign"] > 0:
                kind = "rise"
            elif run["sign"] < 0:
                kind = "fall"
            else:
                kind = "flat"
            segments.append({
                "kind": kind,
                "t0": round(start + a * frame_s, 3),
                "t1": round(start + b * frame_s, 3),
                "dur_s": round(max(frame_s, (b - a) * frame_s), 3),
                "db0": round(db0, 1), "db1": round(db1, 1),
            })
        peak_i = int(env.argmax())
        valley_i = int(env.argmin())
        base.update({
            "peak_db": round(float(env[peak_i]), 1),
            "peak_at": round(start + peak_i * frame_s, 3),
            "valley_db": round(float(env[valley_i]), 1),
            "valley_at": round(start + valley_i * frame_s, 3),
            "start_db": round(float(env[0]), 1),
            "end_db": round(float(env[-1]), 1),
            "mean_db": round(float(env.mean()), 1),
            "label": self._shape_label(segments,
                                       float(env[peak_i])),
            "segments": segments,
            # 一帧一个数据的结果：档位游程 + 采样率（口径）
            "energy_fps": round(rate, 2),
            "energy_points": len(levels),
            "levels": level_runs,
            "level_path": ("→".join(row["level"] for row in level_runs[:5])
                           + ("…(共%d段)" % len(level_runs)
                              if len(level_runs) > 5 else "")),
            "level_swing": ("%s~%s" % (max(level_runs, key=lambda r: -self.level_rank(r["level"]))["level"],
                                          min(level_runs, key=lambda r: -self.level_rank(r["level"]))["level"])
                             if level_runs else "未知"),
            "level_runs": len(level_runs),
            # 稳定度：主档位占了多少点、平均每段多长——用来区分"情绪起伏"和"平稳陈述"
            "level_dominant_s": (level_runs[0]["t1"] - level_runs[0]["t0"]
                                  if len(level_runs) == 1 else
                                  max((row["t1"] - row["t0"] for row in level_runs),
                                      default=0.0)),
            "level_dominant": (max(level_runs, key=lambda r: r["points"])["level"]
                                if level_runs else "未知"),
            "level_flat_ratio": (max((r["points"] for r in level_runs), default=0)
                                  / float(len(levels)) if levels else 0.0),
            "level_mean_run_s": round(
                (sum(r["t1"] - r["t0"] for r in level_runs)
                 / float(len(level_runs))) if level_runs else 0.0, 2),
            "grade": "ok",
        })
        base.update(self.meta())
        return base

    def probe(self, start, end):
        """默认探针 = 区间统计（插件统一入口）。"""
        return self.probe_region(start, end)

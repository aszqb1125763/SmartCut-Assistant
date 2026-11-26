# -*- coding: utf-8 -*-
"""语音/文字小脑：只吃原始词级时间戳（+ 原始音频算"有没有人声"），出结果。

覆盖条款：B04（切字红线）、D03（有人声无词 = ASR 漏识别）。

**不许** import 其它小脑，**不许** import 本地模型。
"""
from __future__ import annotations

import coords

from .base import BrainPlugin
from . import dsp


class SpeechBrain(BrainPlugin):
    name = "speech"
    accepts = ("words", "audio")
    emits = ("inside_word", "voiced_no_word", "words")

    def probe(self, at):
        return self.check_cut_safety(at)

    def check_cut_safety(self, at):
        """这一刀切没切在字中间（B04）。"""
        self.probes += 1
        at = float(at)
        for word in self.src["words"].words:
            try:
                start = float(word["start"])
                end = float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start + 1e-6 < at < end - 1e-6:
                out = {"probe": "cut_safety", "brain": self.name,
                       "at": round(at, 3), "inside_word": True,
                       "word": str(word.get("word")
                                   or word.get("text") or "")[:12],
                       "grade": "over", "clause": ["B04"]}
                out.update(self.meta())
                return out
        out = {"probe": "cut_safety", "brain": self.name, "at": round(at, 3),
               "inside_word": False, "grade": "ok", "clause": ["B04"]}
        out.update(self.meta())
        return out

    def probe_voiced(self, start, end):
        """有人声能量但没有词 = ASR 漏识别，不能当静音删（D03）。"""
        self.probes += 1
        audio = self.src["audio"].audio
        sr = self.src["audio"].sr
        seg = audio[max(0, int(float(start) * sr)):min(audio.size,
                                                       int(float(end) * sr))]
        if seg.size < 512:
            voice_ratio = 0.0
        else:
            env = dsp.envelope(seg, sr)
            voice_ratio = float((env > coords.SILENCE_DB).mean())
        overlap = 0
        for word in self.src["words"].words:
            try:
                ws, we = float(word["start"]), float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if we > float(start) and ws < float(end):
                overlap += 1
        hit = bool(voice_ratio >= coords.VOICE_GATE and overlap == 0)
        out = {"probe": "voiced", "brain": self.name,
               "start": round(float(start), 3), "end": round(float(end), 3),
               "voice_ratio": round(voice_ratio, 3), "words": overlap,
               "voiced_no_word": hit,
               "grade": "over" if hit else "ok",
               "clause": ["D03"], "row": "M15" if hit else None}
        out.update(self.meta())
        return out

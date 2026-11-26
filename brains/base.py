# -*- coding: utf-8 -*-
"""小脑契约：**原始数据按类别打包**，一个脑只拿自己声明的那几类。

子枫 2026-11-24：
    「数据直接分类打包给对应的小脑就行。」
    「小脑之间不能互相交流；只接受原始数据；本地模型只读小脑输出。」

所以这里没有"万能数据源"：
    AudioSource  → 只有音频
    WordSource   → 只有词级时间戳
    FrameSource  → 只有帧
    BrainPlugin(accepts=("audio",)) 只会被塞 AudioSource，
    多塞一类、塞错类型，构造时直接 TypeError。
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

RAW_KINDS = ("audio", "words", "frames")
SCHEMA_VERSION = 1
SR = 16000


def _ffmpeg():
    local = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "ffmpeg.exe")
    return local if os.path.isfile(local) else "ffmpeg"


class AudioSource:
    """原始音频（一根轨，16k 单声道）。"""

    kind = "audio"

    def __init__(self, path, track=0, ffmpeg=None, sr=SR, should_cancel=None):
        self.path = path
        self.track = int(track)
        self.sr = int(sr)
        if not ffmpeg:
            # The packaged application keeps its executable outside the
            # frozen module directory; use the same resolver as ingest/QC.
            import index as indexer
            ffmpeg = indexer.find_ffmpeg()
        self.ffmpeg = ffmpeg
        self.should_cancel = should_cancel
        self._audio = None
        self._audio_cache = None
        self._cache_entry = None
        self.last_cache_audit = None

    def _check_cancel(self):
        if self.should_cancel is not None and self.should_cancel():
            from audio_evidence_cache import AudioEvidenceCancelled
            raise AudioEvidenceCancelled("brain audio loading cancelled")

    def _entry(self):
        self._check_cancel()
        if self._cache_entry is None:
            try:
                from audio_evidence_cache import AudioEvidenceCache
                self._audio_cache = AudioEvidenceCache(
                    self.path, [self.track], sample_rate=self.sr,
                    ffmpeg=self.ffmpeg, should_cancel=self.should_cancel)
                self._cache_entry = self._audio_cache.prepare_track(self.track)
                self._check_cancel()
                self.last_cache_audit = dict(self._cache_entry)
                self.last_cache_audit.update({
                    "cache_key": self._cache_entry.get("key"),
                    "precision": "source_pcm",
                })
            except Exception as exc:
                self.close()
                self.last_cache_audit = {
                    "track": self.track, "sample_rate": self.sr,
                    "reused": False, "precision": "source_pcm",
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:160]),
                }
                raise
        return self._cache_entry

    @property
    def audio(self):
        self._check_cancel()
        if self._audio is None:
            try:
                entry = self._entry()
                samples = self._audio_cache.review_samples(
                    self.track, 0.0, entry["duration"])
                self._check_cancel()
                if samples.size != int(entry["sample_count"]):
                    raise RuntimeError("brain audio PCM index is incomplete")
                self._audio = samples
            except Exception as exc:
                self.close()
                self.last_cache_audit = dict(self.last_cache_audit or {})
                self.last_cache_audit["error"] = "%s: %s" % (
                    type(exc).__name__, str(exc)[:160])
                raise
        return self._audio

    def duration(self):
        self._check_cancel()
        if self._audio is not None:
            return self._audio.size / float(self.sr)
        return float(self._entry()["duration"])

    def close(self):
        self._audio = None
        self._cache_entry = None
        cache, self._audio_cache = self._audio_cache, None
        if cache is not None:
            cache.close()


class WordSource:
    """原始词级时间戳（ASR 输出，不是结论）。"""

    kind = "words"

    def __init__(self, index_dir):
        self.index_dir = index_dir or ""
        self._words = None

    @property
    def words(self):
        if self._words is None:
            self._words = load_words(self.index_dir)
        return self._words

    def set_index(self, index_dir):
        self.index_dir = index_dir or ""
        self._words = None
        return self

    def set_words(self, words):
        self._words = list(words or [])
        return self

    def close(self):
        self._words = None


class FrameSource:
    """原始帧（360p 代理，按需抽）。"""

    kind = "frames"

    def __init__(self, path, ffmpeg=None):
        self.path = path
        self.ffmpeg = ffmpeg or _ffmpeg()
        self._cache = {}
        self._frame_cache = None
        self.last_cache_audit = None

    def frames(self, start=0.0, end=None, fps=2.0, width=360, limit=240):
        """返回 (times, frames)；frames 是 BGR uint8。同一区间只抽一次。"""
        import shutil
        import tempfile
        start = float(start)
        end = float(end) if end is not None else start + 20.0
        key = (round(start, 2), round(end, 2), float(fps), int(width))
        if key in self._cache:
            return self._cache[key]
        # All picture brains share the same persistent ingest proxy.  A fresh
        # FrameSource object in another role/run therefore does not decode the
        # original recording again.
        try:
            if self._frame_cache is None:
                import video_frame_cache
                self._frame_cache = video_frame_cache.VideoFrameCache(
                    self.path, ffmpeg=self.ffmpeg)
            times, frames = self._frame_cache.frames(
                start, end, fps=fps, width=width, limit=limit)
            self.last_cache_audit = dict(self._frame_cache.audit)
            self.last_cache_audit.update({
                "precision": "review_proxy_nearest_0.5s",
                "requested_interval": [round(start, 6), round(end, 6)],
            })
            self._cache[key] = (times, frames)
            return times, frames
        except Exception as exc:  # noqa: BLE001
            self.last_cache_audit = {
                "precision": "legacy_interval_fallback", "reused": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:160]),
            }
        try:
            import cv2
        except Exception:  # noqa: BLE001
            return [], []
        span = max(0.2, end - start)
        rate = min(float(fps), max(0.5, limit / span))
        tmp = tempfile.mkdtemp(prefix="frames_")
        try:
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", "%.3f" % max(0.0, start), "-t", "%.3f" % span,
                   "-i", self.path, "-vf",
                   "fps=%g,scale=%d:-2" % (rate, int(width)),
                   "-q:v", "3", os.path.join(tmp, "f_%05d.jpg")]
            subprocess.run(cmd, capture_output=True)
            names = sorted(n for n in os.listdir(tmp) if n.startswith("f_"))
            frames = []
            for name in names:
                buf = np.fromfile(os.path.join(tmp, name), dtype=np.uint8)
                img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if img is not None:
                    frames.append(img)
            times = [start + index / rate for index in range(len(frames))]
            self._cache[key] = (times, frames)
            return times, frames
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def close(self):
        self._cache = {}
        self._frame_cache = None


SOURCE_TYPES = {"audio": AudioSource, "words": WordSource,
                "frames": FrameSource}


class BrainPlugin:
    """所有小脑的基类：只拿自己声明的那几类原始数据。"""

    name = "base"
    accepts = ()          # 只能填 RAW_KINDS 里的项
    emits = ()

    def __init__(self, **sources):
        unknown = set(sources) - set(self.accepts)
        if unknown:
            raise TypeError("%s 不接受这些数据：%s（它只要 %s）"
                            % (self.name, sorted(unknown), list(self.accepts)))
        for kind in self.accepts:
            if kind not in RAW_KINDS:
                raise ValueError("小脑只允许接受原始数据：%s" % (kind,))
            source = sources.get(kind)
            if source is None:
                raise TypeError("%s 缺原始数据：%s" % (self.name, kind))
            expect = SOURCE_TYPES[kind]
            if not isinstance(source, expect):
                raise TypeError("%s 的 %s 必须是 %s，收到 %s"
                                % (self.name, kind, expect.__name__,
                                   type(source).__name__))
        # 只挂自己声明的那几类，别的拿不到
        self.src = {kind: sources[kind] for kind in self.accepts}
        self.probes = 0

    def probe(self, *args, **kwargs):
        raise NotImplementedError

    def meta(self):
        audio = self.src.get("audio")
        return {"brain": self.name, "schema_version": SCHEMA_VERSION,
                "win_ms": 20,
                "track": getattr(audio, "track", None)}


def load_words(index_dir):
    """读 ASR 的词级时间戳（原始数据）。"""
    import json
    path = os.path.join(index_dir or "", "index.json")
    if not index_dir or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    words = []
    seen = set()
    for segment in data.get("segment_evidence") or []:
        for word in segment.get("words") or []:
            try:
                start = float(word["start"])
                end = float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(word.get("text") or word.get("word") or "")
            # 同一句话可能因为多轨 ASR 被写进来两遍（实测 2402 条里 774 条重复），
            # 交给小脑之前先在**原始数据层**去重，否则文字和时长都会翻倍。
            key = (round(start, 3), round(end, 3), text)
            if key in seen:
                continue
            seen.add(key)
            words.append({"start": start, "end": end, "word": text})
    words.sort(key=lambda row: (row["start"], row["end"]))
    # 两遍 ASR 的时间戳会差几十毫秒：同一个词、区间重叠过半，只留一条。
    kept = []
    for word in words:
        duplicated = False
        for prev in kept[-4:]:
            if prev["word"] != word["word"]:
                continue
            overlap = min(prev["end"], word["end"]) - max(prev["start"],
                                                         word["start"])
            shorter = min(prev["end"] - prev["start"], word["end"] - word["start"])
            if shorter > 0 and overlap / shorter > 0.5:
                duplicated = True
                break
        if not duplicated:
            kept.append(word)
    return kept

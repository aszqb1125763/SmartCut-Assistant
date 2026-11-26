# -*- coding: utf-8 -*-
"""内置 ASR：把录音转成文字（faster-whisper，本地跑，不联网）。

子枫 2026-11-24：
    「内置 ASR 也要加上。」→ 之后又说「声音转译速度太慢了能加快嘛」

实测（5.2 秒音频、CPU int8）：

| 模型 | beam | 转写耗时 |
|---|---|---|
| large-v3-turbo | 1 | 2.8 s |
| **small** | **1** | **0.9 s** |

独立实验版曾默认用 small 追求速度；正式剪辑软件现在默认使用
**large-v3 + beam 5 确定性解码**，并沿用软件保存的识别档。领域说明只放在
``initial_prompt``，专有词走 ``hotwords``，避免词表被误当成上一句续写。
仍可通过 `ASR_MODEL` / `ASR_BEAM` 调整速度与精度。

分工没变：ASR 只负责"说了什么"，能量仍由小脑测；模型只拿文字 + 测量。
"""
from __future__ import annotations

import os
import subprocess
import threading

import numpy as np

import asr_hints
from memory_config import app_home

BASE = app_home()
ASR_DIR = os.path.join(BASE, "models")
#: 术语词表放在 models\asr\term.txt（和独立版一致）；也兼容旧的 models\term.txt
TERM_PATH = os.path.join(ASR_DIR, "asr", "term.txt")

#: 与素材分析共用已保存识别档；ASR_MODEL/ASR_BEAM 保留显式速度选择。
DEFAULT_MODEL = asr_hints.configured_model(BASE)
BEAM = asr_hints.beam_size()

_lock = threading.Lock()
_models = {}
_terms_cache = None


def terms():
    """术语词表（models/asr/term.txt，一行一个）。

    small 模型不认游戏术语：实测把「四元素精华」听成「四元兽精华」。
    词形走 hotwords，不把长词表塞进 initial_prompt。
    """
    global _terms_cache
    if _terms_cache is not None:
        return _terms_cache
    path = TERM_PATH
    if not os.path.isfile(path):
        path = os.path.join(ASR_DIR, "term.txt")
    words = []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                word = line.strip()
                if word and not word.startswith("#"):
                    words.append(word)
    except OSError:
        words = []
    _terms_cache = words
    return words


def _prompt():
    # initial_prompt 是“上一段文字”，塞词表会被当成口播并诱发续写；
    # 专有名词应走 faster-whisper 的 hotwords 通道。
    return asr_hints.INITIAL_CONTEXT


def _hotwords(extra_terms=()):
    """短、完整、稳定的术语热词；不做任何文字替换。"""
    _context, value, _selected = asr_hints.build(
        BASE, extra_terms=list(extra_terms or ()) + terms())
    return value


def model_dir(name=None):
    """主项目的模型目录带 -full 后缀（large-v3-turbo-full 这种）。"""
    name = name or asr_hints.configured_model(BASE)
    for candidate in (name, name + "-full"):
        folder = os.path.join(ASR_DIR, candidate)
        if os.path.isdir(folder):
            return folder
    return os.path.join(ASR_DIR, name)


def available(name=None):
    folder = model_dir(name)
    return os.path.isdir(folder) and os.path.isfile(
        os.path.join(folder, "model.bin"))


def _load(name=None):
    """按需加载并缓存（同一模型只加载一次）。"""
    name = name or asr_hints.configured_model(BASE)
    if name in _models:
        return _models[name]
    with _lock:
        if name in _models:
            return _models[name]
        from faster_whisper import WhisperModel
        folder = model_dir(name)
        if not os.path.isfile(os.path.join(folder, "model.bin")):
            raise RuntimeError("找不到 ASR 模型：%s" % folder)
        # CPU int8：不占显存，留给 8B；本机 CUDA 是 13，ctranslate2 要 12
        _models[name] = WhisperModel(folder, device="cpu", compute_type="int8")
        return _models[name]


def warmup(name=None):
    """后台预热：第一次说语音不用等加载。"""
    def run():
        try:
            _load(name)
        except Exception:  # noqa: BLE001
            pass
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _decode(path, sr=16000):
    """ffmpeg 解码成 16k 单声道 float32（绕开 PyAV 版本不兼容）。"""
    ffmpeg = os.path.join(BASE, "bin", "ffmpeg.exe")
    if not os.path.isfile(ffmpeg):
        ffmpeg = "ffmpeg"
    done = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path,
         "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True)
    return np.frombuffer(done.stdout, dtype="<i2").astype(np.float32) / 32768.0


def transcribe(path, language="zh", beam_size=None, name=None,
               with_probability=False, initial_prompt=None, hotwords=None):
    """返回 [{start, end, text}]。

    with_probability=True 时返回**词级**结果（带 prob），
    供同音候选使用——ASR 不替人做选择（子枫 2026-11-24）。
    """
    model = _load(name)
    audio = _decode(path)
    if audio.size == 0:
        raise RuntimeError("解码出来是空的：%s" % os.path.basename(path))
    if initial_prompt is None:
        initial_prompt = _prompt()
    if hotwords is None:
        hotwords = _hotwords()
    elif hotwords is False:
        hotwords = None
    segments, _info = model.transcribe(
        audio, language=language, task="transcribe",
        beam_size=asr_hints.beam_size(BEAM if beam_size is None else beam_size),
        vad_filter=True,
        vad_parameters=dict(asr_hints.VAD_PARAMETERS),
        word_timestamps=bool(with_probability),
        condition_on_previous_text=False,
        # 固定 0 温度：同一素材重复分析不再因采样回退得到不同错字。
        temperature=0.0,
        compression_ratio_threshold=asr_hints.COMPRESSION_RATIO_THRESHOLD,
        no_speech_threshold=asr_hints.NO_SPEECH_THRESHOLD,
        initial_prompt=initial_prompt,
        hotwords=hotwords)
    rows = []
    for segment in segments:
        if with_probability:
            for word in (segment.words or []):
                rows.append({"start": round(float(word.start), 2),
                             "end": round(float(word.end), 2),
                             "word": str(word.word),
                             "prob": float(word.probability)})
            continue
        text = (segment.text or "").strip()
        if text:
            rows.append({"start": round(float(segment.start), 2),
                         "end": round(float(segment.end), 2),
                         "text": text})
    return rows


def to_sentences(rows):
    """兼容两种结果：带 text 的段、带 word 的词。"""
    return [{"start": row["start"], "end": row["end"],
             "word": str(row.get("text") or row.get("word") or "")}
            for row in rows]


def full_text(rows):
    return "".join(str(row.get("text") or row.get("word") or "")
                   for row in rows)

# -*- coding: utf-8 -*-
"""
素材索引器 —— 把一段长素材拆成"可挑选的卡片"

为什么按"说话段落"切，而不是按镜头切：
    我的世界直播画面几乎不变，没有硬切镜头，按镜头切只会切出一堆碎片。
    真正天然的边界是主播说话时的停顿——一个停顿基本就是一件事讲完。

每个卡片记录：起止时间、缩略图、这段在说什么、音量峰值（高光）、画面变化量。

用法：
    python index.py 素材.mp4
    python index.py D:\\录像 --out D:\\索引
"""


from __future__ import annotations

try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass


import argparse
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np

import audio_evidence_cache
import media_identity
import llm_gateway
from memory_config import app_home

# 国内直连 huggingface.co 基本是「连上但不动」，而且不报错也不超时，
# 表现就是界面永远停在"正在加载模型"。所以一开始就走镜像，别等失败再切。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

try:
    import _av_compat  # noqa: F401  修 faster-whisper 与新版 PyAV 的参数不兼容
except Exception:  # noqa: BLE001
    pass

try:
    import tagger
    import asr_hints
except ImportError:  # 作为模块被别处导入时补一下路径
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tagger
    import asr_hints

# Source imports may live in PyInstaller's _internal; mutable configuration,
# model downloads and work products belong to the installed application.
BASE_DIR = app_home()
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# HuggingFace 的下载缓存也钉在程序目录里。
# 不设的话它默认写 C:\Users\<你>\.cache\huggingface —— 模型几个 GB，
# 就和"所有文件都在安装目录"冲突了。
os.environ.setdefault("HF_HOME", os.path.join(BASE_DIR, "models", ".hf"))
os.environ.setdefault("HF_HUB_CACHE",
                      os.path.join(BASE_DIR, "models", ".hf", "hub"))

# 喂给语音识别的术语提示。
# 注意：必须是**短词表**，不要写成句子——以前写成一整句话，
# Whisper 会把这句提示当成"上一句"，偶尔直接把它吐进结果里，看起来很乱。
JARGON_PROMPT = (
    "我的世界 科技包 整合包 机械动力 通用机械 应用能源 格雷科技 沉浸工程 "
    "粉碎轮 传动轴 齿轮 研磨机 压板机 熔炉 矿石 残骸 背包 物品栏 工作台 "
    "下界 末地 区块 红石 刷怪笼 附魔 转速 合成 搭建"
)

# initial_prompt 的长度上限。Whisper 的 prompt 只有 224 token，中文基本一字
# 一 token；实测 731 字会让开头 45 秒整段被吃掉（模型接着提示词的尾巴往下编）。
_HINT_MAX = 140

# 索引仍保留旧的 ``segments=[[start, end, text], ...]``，避免旧版界面、
# 训练脚本和用户已经生成的索引失效；精确的词级时间另存为版本化证据。
INDEX_SCHEMA_NAME = "smartcut.material-index"
INDEX_SCHEMA_VERSION = 2
SEGMENT_EVIDENCE_VERSION = 1
# v7 adds source/track-bound local waveform refinement for caption cues.  Old
# indexes cannot prove which audio stream produced ``segments_refined`` and
# must not reuse those timestamps after a track or source change.
ASR_PROFILE_VERSION = 9


def asr_profile(model_size=None):
    """Snapshot the actual ASR inputs so old transcription cannot masquerade as new."""
    context, hotwords, _selected = asr_hints.build(
        BASE_DIR, extra_terms=JARGON_PROMPT.split(), max_hotword_chars=_HINT_MAX)
    profile = {
        "version": ASR_PROFILE_VERSION,
        "model": str(model_size or asr_hints.configured_model(BASE_DIR)),
        "beam_size": asr_hints.beam_size(),
        "batch_size": asr_hints.batch_size(BASE_DIR),
        "language": "zh", "task": "transcribe",
        "temperature": 0.0, "condition_on_previous_text": False,
        "word_timestamps": True,
        "vad_parameters": dict(asr_hints.VAD_PARAMETERS),
        "compression_ratio_threshold": asr_hints.COMPRESSION_RATIO_THRESHOLD,
        "no_speech_threshold": asr_hints.NO_SPEECH_THRESHOLD,
        "initial_prompt": context, "hotwords": hotwords,
    }
    profile["signature"] = hashlib.sha256(json.dumps(
        profile, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return profile


def _asr_profile_freshness(index_data):
    if index_data.get("asr_requested") is not True and not index_data.get("segments"):
        return None
    saved = index_data.get("asr_profile")
    if not isinstance(saved, dict) or not saved.get("signature"):
        return "asr_profile_missing"
    if saved.get("version") != ASR_PROFILE_VERSION:
        return "asr_profile_version_changed"
    if saved.get("signature") != asr_profile().get("signature"):
        return "asr_profile_changed"
    return None


def index_freshness(index_data, source: str, *, index_path: str | None = None) -> dict:
    """Explain whether an existing neutral index still belongs to ``source``.

    New indexes carry a sampled source identity.  Legacy indexes can only be
    reused when their stored absolute path matches and the source is not newer
    than the index file itself.  That compatibility rule is intentionally
    conservative; it never treats a missing/unknown timestamp as fresh.
    """
    if isinstance(index_data, (str, os.PathLike)):
        index_path = os.path.abspath(os.fspath(index_data))
        try:
            with open(index_path, encoding="utf-8") as handle:
                index_data = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            return {"reusable": False, "legacy": False,
                    "reason": "index_unreadable", "error": str(exc)[:200]}
    if not isinstance(index_data, dict):
        return {"reusable": False, "legacy": False,
                "reason": "index_payload_invalid"}
    if index_data.get("ai_backend_signature", "local") != llm_gateway.backend_signature():
        return {"reusable": False, "legacy": False,
                "reason": "ai_backend_changed"}
    try:
        source_path = media_identity.normalized_path(source)
    except (OSError, TypeError, ValueError) as exc:
        return {"reusable": False, "legacy": False,
                "reason": "source_invalid", "error": str(exc)[:200]}
    if not os.path.isfile(source_path):
        return {"reusable": False, "legacy": False,
                "reason": "source_missing"}
    stored_path = str(index_data.get("source") or "")
    if not stored_path or media_identity.normalized_path(stored_path) != source_path:
        return {"reusable": False, "legacy": False,
                "reason": "source_path_mismatch"}
    asr_stale = _asr_profile_freshness(index_data)
    if asr_stale:
        return {"reusable": False, "legacy": False, "reason": asr_stale}

    saved_identity = index_data.get("source_identity")
    if isinstance(saved_identity, dict):
        try:
            current = media_identity.source_identity(source_path)
        except (OSError, TypeError, ValueError) as exc:
            return {"reusable": False, "legacy": False,
                    "reason": "identity_check_failed", "error": str(exc)[:200]}
        ok = media_identity.matches(saved_identity, current)
        return {
            "reusable": bool(ok), "legacy": False,
            "reason": "identity_match" if ok else "source_identity_mismatch",
            "identity_key": current.get("identity_key"),
        }

    # Backward compatibility for indexes created before source identities.
    if not index_path:
        candidate = os.path.join(str(index_data.get("index_dir") or ""),
                                 "index.json")
        if os.path.isfile(candidate):
            index_path = candidate
    try:
        source_mtime_ns = int(os.stat(source_path).st_mtime_ns)
        index_mtime_ns = int(os.stat(os.path.abspath(index_path)).st_mtime_ns)
    except (OSError, TypeError, ValueError):
        return {"reusable": False, "legacy": True,
                "reason": "legacy_timestamp_unknown"}
    ok = source_mtime_ns <= index_mtime_ns
    return {"reusable": bool(ok), "legacy": True,
            "reason": "legacy_path_and_timestamp_match" if ok
                      else "legacy_source_newer_than_index"}


def index_matches_source(index_data, source: str, *,
                         index_path: str | None = None) -> bool:
    """Boolean adapter for callers that do not need freshness diagnostics."""
    return bool(index_freshness(index_data, source,
                                index_path=index_path).get("reusable"))


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
_HW_CACHE = {}


def hwaccel_args(prefer="cuda"):
    """返回硬件解码参数（ffmpeg 解码走显卡）。不可用时返回空列表，自动退回软解。"""
    key = "hw"
    if key in _HW_CACHE:
        return _HW_CACHE[key]
    args = []
    try:
        import subprocess as _sp
        ff = find_ffmpeg()
        out = _sp.run([ff, "-hide_banner", "-hwaccels"], capture_output=True,
                      text=True, creationflags=CREATE_NO_WINDOW).stdout
        have = [x.strip() for x in out.splitlines()]
        for cand in (prefer, "cuda", "d3d11va"):
            if cand and cand in have:
                args = ["-hwaccel", cand]
                break
    except Exception:  # noqa: BLE001
        args = []
    _HW_CACHE[key] = args
    return args


def find_ffmpeg() -> str:
    # PyInstaller 模块位于 ``_internal``，但 ffmpeg 按产品设计放在 EXE
    # 根目录的 ``bin``。源码运行时 BASE_DIR 就是安装根；打包运行时则必须
    # 优先尊重 app_main 传入的 SMARTCUT_HOME，并兼容 EXE 所在目录。
    roots = []
    for value in (
            os.environ.get("SMARTCUT_HOME"),
            os.path.dirname(os.path.abspath(sys.executable or "")),
            BASE_DIR):
        value = os.path.abspath(value) if value else ""
        if value and value not in roots:
            roots.append(value)
    candidates = [os.path.join(root, "bin", "ffmpeg.exe")
                  for root in roots]
    candidates.append(os.path.join(
        BASE_DIR, "..", "video-roughcut", "bin", "ffmpeg.exe"))
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.isfile(c):
            return c
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError("找不到 ffmpeg：请把 bin\\ffmpeg.exe 放进程序目录。")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, **kw)


def _text(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SIZE_RE = re.compile(r"Video:.*?,\s*(\d+)x(\d+)")
_FPS_RE = re.compile(r"([\d.]+)\s+fps")
_STREAM_RE = re.compile(r"Stream #\d+:(\d+)(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*(Video|Audio|Subtitle|Data):\s*([^,\s]+)")
_CH_RE = re.compile(r"(\d+) channels|,\s*(mono|stereo|quad|5\.1|7\.1)")
_SR_RE = re.compile(r"(\d+)\s*Hz")


def probe_streams(path: str) -> dict:
    """
    解析 ffmpeg -i 的输出，列出**每一条**音轨和视频轨。

    OBS 多轨录像里，音轨 1 常常是游戏声、音轨 2 才是麦克风。
    ffmpeg 默认只取第一条，所以必须自己列出来让用户选。
    """
    p = _run([find_ffmpeg(), "-hide_banner", "-i", path])
    text = _text(p.stderr)

    info = {
        "duration": 0.0, "has_video": False, "has_audio": False,
        "width": 0, "height": 0, "fps": 0.0, "video_codec": "",
        "audio_streams": [], "video_streams": [],
    }
    m = _DUR_RE.search(text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    for line in text.splitlines():
        s = line.strip()
        m = _STREAM_RE.search(s)
        if not m:
            continue
        idx, kind, codec = int(m.group(1)), m.group(2), m.group(3).strip()
        if kind == "Audio":
            ch = _CH_RE.search(s)
            sr = _SR_RE.search(s)
            channels = 0
            if ch:
                channels = int(ch.group(1)) if ch.group(1) else {
                    "mono": 1, "stereo": 2, "quad": 4, "5.1": 6, "7.1": 8,
                }.get(ch.group(2) or "", 0)
            ordinal = len(info["audio_streams"])      # 第几条音轨（给 -map 0:a:N 用）
            info["audio_streams"].append({
                "index": idx, "ordinal": ordinal, "codec": codec, "channels": channels,
                "sample_rate": int(sr.group(1)) if sr else 0,
                "label": _track_label(ordinal, codec, channels, int(sr.group(1)) if sr else 0),
            })
        elif kind == "Video":
            if not info["video_codec"]:
                info["video_codec"] = codec
            info["video_streams"].append({"index": idx, "codec": codec})

    m = _SIZE_RE.search(text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = _FPS_RE.search(text)
    if m:
        try:
            info["fps"] = float(m.group(1))
        except ValueError:
            pass
    info["has_video"] = bool(info["video_streams"])
    info["has_audio"] = bool(info["audio_streams"])
    return info


def _track_label(ordinal: int, codec: str, channels: int, sr: int) -> str:
    ch = {1: "单声道", 2: "立体声", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}声道" if channels else "未知声道")
    sr_txt = f"{sr // 1000}kHz" if sr else ""
    return f"音轨 {ordinal + 1} · {codec} {ch} {sr_txt}".strip()


def probe(path: str) -> dict:
    return probe_streams(path)


def work_root() -> str:
    """
    索引和导出的默认存放位置 —— 一律放在程序目录下的「工作目录」。

    为什么要这样：要求「装到哪，所有文件就都在那」，不再往 C 盘 AppData 里写。
    好处是整个程序目录复制走就能换机器、卸载就是删目录，不留垃圾。

    例外：程序被装到 Program Files 之类只读的地方时写不进去，
    这时才退回 AppData（否则程序直接不能用）。环境变量 SC_WORK 可以强指。
    """
    forced = (os.environ.get("SC_WORK") or "").strip()
    cands = [os.path.join(BASE_DIR, "工作目录")]
    if forced:
        cands.insert(0, forced)
    cands.append(os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "素材卡片"))
    for cand in cands:
        try:
            os.makedirs(cand, exist_ok=True)
            probe_file = os.path.join(cand, ".writable")
            with open(probe_file, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe_file)
            return cand
        except OSError:
            continue
    return BASE_DIR


def temp_root() -> str:
    """中间文件（抽出来的 wav、抽帧、分段编码）也放在程序目录下。

    以前都在 %TEMP%（C 盘），一个 33 分钟素材抽音轨就 60 MB，
    分段编码还要几个 GB —— 装到别的盘、C 盘又小的时候会直接爆。
    """
    d = os.path.join(work_root(), "_temp")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".writable")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return d
    except OSError:
        return tempfile.gettempdir()


def migrate_legacy_workdir(on_log=None):
    """把老版本留在 C 盘 AppData 的工作目录搬进程序目录（只搬一次）。

    老版本把索引/导出放在 %LOCALAPPDATA%\\素材卡片。
    现在要求"所有文件都在安装目录"，所以第一次运行时把它搬过来。
    目标已存在就跳过（不覆盖用户的现有数据）。
    """
    def _log(m):
        if on_log:
            on_log(m)

    legacy = os.path.join(os.environ.get("LOCALAPPDATA") or "", "素材卡片")
    target = os.path.join(BASE_DIR, "工作目录")
    if not os.path.isdir(legacy):
        return False
    if os.path.normcase(os.path.abspath(legacy)) == os.path.normcase(
            os.path.abspath(target)):
        return False
    if os.path.isdir(target) and os.listdir(target):
        return False            # 已经有自己的数据，不碰
    marker = os.path.join(BASE_DIR, ".ported")
    if os.path.isfile(marker):
        return False
    try:
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(legacy, target)
    except OSError as exc:
        _log("旧工作目录搬不过来（%s），继续用新的空目录" % str(exc)[:60])
        return False
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(legacy + "\n")
    except OSError:
        pass
    _log("已把旧工作目录从 %s 搬进程序目录" % legacy)
    return True


def tc(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


# --------------------------------------------------------------------------
# 1. 音频能量：找"在说话"的段落 + 找高光
# --------------------------------------------------------------------------
def extract_audio_wav(path: str, out_wav: str, stream: int | None = None,
                      max_seconds: float | None = None, sr: int = 16000,
                      normalize: bool = False) -> bool:
    """
    把指定音轨抽成 16kHz 单声道 wav。stream 是"第几条音轨"（从 0 开始）。

    normalize=True 时做一次响度归一化：OBS 里麦克风经常录得偏小，
    音量太低 Whisper 会大量吞字、出乱码。注意只用于识别，
    不要用于切点分析（那会把静音段的电平也抬起来，静音就检测不出来了）。
    """
    # stream 可以是单条，也可以是列表：多条时先混音再识别。
    # 为什么要能混：玩家勾了「游戏声 + 人声」两条，只拿第一条分析的话，
    # 万一说话在另一条，转写就会一片空白。
    tracks = ([stream] if isinstance(stream, int) else
              [t for t in (stream or []) if isinstance(t, int)])
    if not tracks:
        tracks = [0]
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", path]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:g}"]
    if len(tracks) == 1:
        cmd += ["-map", f"0:a:{tracks[0]}"]
        if normalize:
            cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    else:
        # 多条轨：必须走 filter_complex（-af 只能作用于单条流）
        chain = "".join("[0:a:%d]" % t for t in tracks)
        chain += "amix=inputs=%d:duration=longest:normalize=0" % len(tracks)
        if normalize:
            chain += ",loudnorm=I=-16:TP=-1.5:LRA=11"
        cmd += ["-filter_complex", chain + "[a]", "-map", "[a]"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", out_wav]
    p = _run(cmd, timeout=1800)
    return p.returncode == 0 and os.path.isfile(out_wav) and os.path.getsize(out_wav) > 44


def read_wav_mono(path: str) -> np.ndarray:
    import wave
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def load_audio_mono16k(path: str, stream: int | None = None) -> np.ndarray:
    fd, tmp = tempfile.mkstemp(suffix=".wav", dir=temp_root())
    os.close(fd)
    try:
        if not extract_audio_wav(path, tmp, stream):
            return np.zeros(0, dtype=np.float32)
        return read_wav_mono(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_audio_window_mono16k(path: str, stream, start: float,
                              end: float) -> np.ndarray:
    """Decode one bounded source-time window without reopening the full ASR job.

    ``stream`` uses the same zero-based audio ordinal convention as
    :func:`extract_audio_wav` and may contain multiple tracks.  The returned
    samples start at ``start``; callers that repair absolute timestamps must
    pass that offset to :func:`repair_suspected_alignment`.
    """
    start = max(0.0, float(start))
    end = max(start, float(end))
    if end - start < 0.05:
        return np.zeros(0, dtype=np.float32)
    tracks = ([stream] if isinstance(stream, int) else
              [int(value) for value in (stream or [])
               if isinstance(value, (int, np.integer))])
    if not tracks:
        tracks = [0]
    command = [find_ffmpeg(), "-hide_banner", "-loglevel", "error",
               "-nostdin", "-ss", "%.6f" % start, "-t", "%.6f" % (end - start),
               "-i", path]
    if len(tracks) == 1:
        command += ["-map", "0:a:%d" % tracks[0]]
    else:
        chain = "".join("[0:a:%d]" % track for track in tracks)
        chain += "amix=inputs=%d:duration=longest:normalize=0[a]" % len(tracks)
        command += ["-filter_complex", chain, "-map", "[a]"]
    command += ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, timeout=180,
        creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0 or not result.stdout:
        message = (result.stderr or b"").decode("utf-8", "replace")[-800:]
        raise RuntimeError("局部音轨读取失败：%s" % message)
    return (np.frombuffer(result.stdout, dtype="<i2").astype(np.float32)
            / 32768.0)


def pick_audio_stream(path: str, streams: list[dict], sample_seconds: float = 180,
                      on_status=None, audio_cache=None) -> int:
    """
    多音轨时自动挑"最像麦克风"的那条。

    两条判据并用：
      1) 绝对电平门槛 —— 一条几乎没声的轨（比如 OBS 里没接上的辅助轨）
         也能在自己那条轨内部算出"静音 60%、几十段说话"，光看相对值会误选。
         所以先按整体电平把比最响那条低 15 dB 以上的直接排除。
      2) 停顿结构 —— 人声有停顿、能量起伏大；游戏声/背景音乐是连续的。
    """
    if len(streams) <= 1:
        return streams[0]["ordinal"] if streams else 0

    cands = []
    for s in streams:
        tmp = None
        try:
            if audio_cache is not None:
                a = audio_cache.review_samples(
                    s["ordinal"], 0.0, float(sample_seconds))
            else:
                fd, tmp = tempfile.mkstemp(suffix=".wav", dir=temp_root())
                os.close(fd)
                if not extract_audio_wav(
                        path, tmp, s["ordinal"], max_seconds=sample_seconds):
                    continue
                a = read_wav_mono(tmp)
            if a.size < 16000:
                continue
            rms, hop = audio_energy(a)
            db = 20 * np.log10(np.maximum(rms, 1e-6))
            # 用 90 分位代表"这条轨平时多大声"，避开偶发爆音
            level = float(np.percentile(db, 90))
            lo, hi = float(np.percentile(db, 10)), level
            thr = lo + 0.3 * (hi - lo)
            silence = float((db <= thr).mean())
            runs = speech_runs(rms, hop)
            per_min = len(runs) / max(0.1, a.size / 16000 / 60)
            score = 0.0
            if 0.03 < silence < 0.92 and len(runs) >= 2:
                # 越接近"三成时间在停顿"越像人声
                score = per_min * (1.0 - abs(silence - 0.35))
            cands.append(dict(ordinal=s["ordinal"], label=s["label"], level=level,
                              silence=silence, runs=len(runs), score=score))
        except audio_evidence_cache.AudioEvidenceCancelled:
            raise
        except Exception:  # noqa: BLE001
            continue
        finally:
            try:
                if tmp:
                    os.remove(tmp)
            except (OSError, TypeError):
                pass

    if not cands:
        return streams[0]["ordinal"]

    loudest = max(c["level"] for c in cands)
    keep = [c for c in cands if c["level"] >= loudest - 15.0]

    for c in cands:
        try:
            muted = c not in keep
        except ValueError:
            muted = False
        if on_status:
            on_status(f"{c['label']}：电平 {c['level']:.0f} dB，"
                      f"静音 {c['silence'] * 100:.0f}%，{c['runs']} 段说话，"
                      f"匹配度 {c['score']:.1f}"
                      + ("（太轻，排除）" if muted else ""))

    best = max(keep, key=lambda c: (c["score"], c["level"]))
    return best["ordinal"]


def audio_energy(audio: np.ndarray, sr: int = 16000, hop: float = 0.05):
    """每 hop 秒一个 RMS 值。"""
    n = max(1, int(sr * hop))
    usable = len(audio) // n * n
    if usable == 0:
        return np.zeros(0, dtype=np.float32), hop
    frames = audio[:usable].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    return rms.astype(np.float32), hop


def _otsu_threshold(values: np.ndarray, bins: int = 64) -> float:
    """双峰分布自动找分界点（Otsu 法）。"""
    hist, edges = np.histogram(values, bins=bins)
    total = hist.sum()
    if total == 0:
        return float(values.min()) if values.size else 0.0
    centers = (edges[:-1] + edges[1:]) / 2.0
    w = np.cumsum(hist).astype(np.float64)
    wb = w
    wf = total - w
    valid = (wb > 0) & (wf > 0)
    if not valid.any():
        return float(np.median(values))
    sum_all = float((hist * centers).sum())
    cum = np.cumsum(hist * centers)
    mb = cum / np.maximum(wb, 1e-9)
    mf = (sum_all - cum) / np.maximum(wf, 1e-9)
    between = wb * wf * (mb - mf) ** 2
    between[~valid] = -1
    i = int(np.argmax(between))
    return float(centers[i])


def speech_runs(rms: np.ndarray, hop: float, min_gap: float = 0.65,
                min_len: float = 0.35, floor_db: float = -60.0):
    """
    返回"在说话"的时间段 [(start, end)]。

    阈值用 Otsu 在 dB 分布上自动找双峰分界：底噪一峰、人声一峰。
    如果素材几乎一直在说话（分不开两峰），就当作整段都在说话，不硬切。
    """
    if rms.size == 0:
        return []
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    db = np.maximum(db, -100.0)

    thr = _otsu_threshold(db)
    voiced_ratio = float((db > thr).mean())
    if voiced_ratio > 0.92 or voiced_ratio < 0.02:
        # 分不开：要么几乎全在说，要么几乎全静音
        thr = max(floor_db, float(np.percentile(db, 99)) - 12.0)
        if voiced_ratio > 0.92:
            thr = float(np.percentile(db, 10))

    voiced = db > thr

    runs = []
    start = None
    gap = 0
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap * hop >= min_gap:
                    end = i - gap + 1
                    if (end - start) * hop >= min_len:
                        runs.append((start * hop, end * hop))
                    start = None
                    gap = 0
    if start is not None:
        end = len(voiced) - gap
        if (end - start) * hop >= min_len:
            runs.append((start * hop, end * hop))
    return runs


def peak_score(db: np.ndarray, a: float, b: float, hop: float) -> float:
    """
    这一段的响度在全片里排第几（0~1）。
    用排名而不是 z 值：直播素材里安静段和说话段比例悬殊，z 值会被压扁。
    """
    if db.size == 0:
        return 0.0
    i0, i1 = int(a / hop), int(b / hop)
    seg = db[max(0, i0):max(1, i1)]
    if seg.size == 0:
        return 0.0
    level = float(np.percentile(seg, 95))
    return float((db <= level).mean())


# --------------------------------------------------------------------------
# 2. 画面：抽样帧、算变化、生成缩略图
# --------------------------------------------------------------------------
def _sample_motion_seek(path: str, step: float, small_w: int,
                        max_points: int = 1200):
    """按时间点 seek 抽帧（大文件用）：只读需要的那几帧，不整条解码。

    为什么：原做法用 -skip_frame nokey 把整条视频过一遍，27 GB 的录播
    光读盘就要 8 分钟（机械盘），比语音识别还慢。但画面变化只是粗特征，
    每句取平均，不需要 1 秒 1 帧的密度。改成均匀取 N 个时间点各抽 1 帧，
    读盘量从几十 GB 降到几百 MB。
    """
    import shutil as _shutil
    ff = find_ffmpeg()
    try:
        dur = float(probe_streams(path).get("duration") or 0)
    except Exception:  # noqa: BLE001
        dur = 0.0
    if dur <= 0:
        return [], []
    n = int(min(max_points, max(8, dur / max(0.5, step) + 1)))
    step2 = dur / max(1, n - 1)
    tmpdir = tempfile.mkdtemp(prefix="mseek_", dir=temp_root())
    import numpy as _np

    def one(i):
        t = round(i * step2, 3)
        out = os.path.join(tmpdir, "%05d.jpg" % i)
        r = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                  "-ss", "%.3f" % t, "-i", path, "-frames:v", "1",
                  "-vf", "scale=%d:-2" % small_w, "-q:v", "5", out])
        if r.returncode != 0 or not os.path.isfile(out):
            return t, None
        img = None
        try:
            buf = _np.fromfile(out, dtype=_np.uint8)
            import cv2
            img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE) if buf.size else None
        except Exception:  # noqa: BLE001
            img = None
        try:
            os.remove(out)
        except OSError:
            pass
        return t, img

    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            got = list(ex.map(one, range(n)))
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)
    times, grays = [], []
    for t, img in got:
        if img is not None:
            times.append(t)
            grays.append(img)
    return times, _diff_sequence(grays)


def sample_motion(path: str, step: float = 1.0, small_w: int = 96,
                  frame_cache=None, return_cache_audit: bool = False):
    """
    按固定间隔抽帧，只算相邻帧的画面差异（0~1），**不把画面留在内存里**。

    短素材走"整条解码关键帧"（准）；长素材（≥20 分钟）自动改走 seek 抽帧，
    否则 27 GB 的录播光抽帧就要 8 分钟。两条路结果都是 (时间点, 差异) 序列。
    """
    # Premiere-style ingest: the neutral analysis owns the one sequential
    # source-picture decode. Motion, card thumbnails, editor context and later
    # visual review all read this persistent low-resolution proxy afterwards.
    cache_audit = None
    try:
        if frame_cache is None:
            import video_frame_cache
            frame_cache = video_frame_cache.VideoFrameCache(path)
        times, diffs = frame_cache.analyze_motion(step=step,
                                                  small_width=small_w)
        cache_audit = dict(frame_cache.audit)
        cache_audit["consumer"] = "neutral_motion_ingest"
        if times:
            return ((times, diffs, cache_audit) if return_cache_audit
                    else (times, diffs))
    except Exception as exc:  # noqa: BLE001
        try:
            import video_frame_cache
            if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                raise
        except ImportError:
            pass
        cache_audit = {"reused": False, "precision": "legacy_motion_fallback",
                       "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}

    dur = 0.0
    try:
        dur = float(probe_streams(path).get("duration") or 0)
    except Exception:  # noqa: BLE001
        dur = 0.0
    # 试过"按时间点 seek 抽帧"来省读盘：实测更慢（1200 次进程启动 137 秒），
    # 而且帧间隔变大后 diff 的含义都变了（和整条解码的曲线相关只有 0.04）。
    # 放弃。真正的提速在 index_video：抽帧和语音识别并行跑。
    times, diffs = _sample_motion_ffmpeg(path, step, small_w)
    if not times:
        times, diffs = _sample_motion_cv2(path, step, small_w)
    return ((times, diffs, cache_audit) if return_cache_audit
            else (times, diffs))


def _diff_sequence(grays: list) -> list[float]:
    out, prev = [], None
    for g in grays:
        if prev is not None and getattr(prev, "shape", None) == g.shape:
            out.append(float(np.abs(g.astype(np.int16) - prev.astype(np.int16)).mean()) / 64.0)
        else:
            out.append(0.0)
        prev = g
    return out


def _sample_motion_cv2(path: str, step: float, small_w: int):
    try:
        import cv2
    except ImportError:
        return [], []

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return [], []
    grays, times = [], []
    prev_gray = None
    diffs = []
    t = 0.0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total / fps if fps > 0 and total > 0 else 0.0
    while t < duration:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(cv2.resize(frame, (small_w, max(2, int(h * small_w / w)))),
                            cv2.COLOR_BGR2GRAY)
        if prev_gray is not None and prev_gray.shape == gray.shape:
            diffs.append(float(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)).mean()) / 64.0)
        else:
            diffs.append(0.0)
        times.append(t)
        prev_gray = gray
        t += step
    cap.release()
    return times, diffs


def _sample_motion_ffmpeg(path: str, step: float, small_w: int):
    """用一次 ffmpeg 导出低分辨率帧序列来算变化，绕开 OpenCV 解码。"""
    import shutil as _shutil
    tmpdir = tempfile.mkdtemp(prefix="motion_", dir=temp_root())
    try:
        cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               # 只解关键帧：实测 180 秒素材 6.3s -> 1.6s，抽到的帧数基本不变
               "-skip_frame", "nokey",
               "-i", path, "-vf", f"fps=1/{max(0.05, step):g},scale={small_w}:-2",
               "-q:v", "5", os.path.join(tmpdir, "%06d.jpg")]
        _run(cmd, timeout=3600)
        files = sorted(f for f in os.listdir(tmpdir) if f.endswith(".jpg"))
        if not files:
            return [], []
        try:
            import cv2
            grays = []
            for f in files:
                # 注意：不能用 cv2.imread —— Windows 上它读不了中文路径，
                # 而临时目录在安装目录里（安装目录名通常是中文），
                # 会静默返回 None，画面分析直接退化成全 0。用 imdecode 绕开。
                fp = os.path.join(tmpdir, f)
                try:
                    buf = np.fromfile(fp, dtype=np.uint8)
                    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE) if buf.size else None
                except OSError:
                    img = None
                if img is None:
                    break
                grays.append(img)
        except ImportError:
            return [], []
        times = [i * step for i in range(len(grays))]
        return times, _diff_sequence(grays)
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)


def grab_thumbnail(path: str, t: float, out_jpg: str, width: int = 480,
                   frame_cache=None) -> bool:
    """
    用 ffmpeg 抽一帧写成 jpg。

    以前用 OpenCV 抽，在 OBS 录的大文件上经常直接打不开，结果就是一张缩略图都没有，
    而且没有任何提示。ffmpeg 对本机录的容器支持最好，所以这里改用它。
    """
    # Card pictures are browsing aids, not cut evidence.  Reuse the ingest
    # proxy's nearest frame instead of reopening the multi-GB source per card.
    try:
        if frame_cache is None:
            import video_frame_cache
            frame_cache = video_frame_cache.VideoFrameCache(path)
        cached_path, _evidence = frame_cache.frame_path_at(t)
        if cached_path:
            import cv2
            buf = np.fromfile(cached_path, dtype=np.uint8)
            image = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
            if image is not None:
                target_width = max(32, int(width))
                if image.shape[1] != target_width:
                    target_height = max(2, int(image.shape[0] * target_width /
                                               image.shape[1]))
                    image = cv2.resize(image, (target_width, target_height),
                                       interpolation=cv2.INTER_AREA)
                ok, encoded = cv2.imencode(
                    ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if ok:
                    os.makedirs(os.path.dirname(os.path.abspath(out_jpg)),
                                exist_ok=True)
                    temporary = out_jpg + ".%d.part.jpg" % os.getpid()
                    encoded.tofile(temporary)
                    os.replace(temporary, out_jpg)
                    return True
    except Exception:  # noqa: BLE001
        pass
    if os.path.exists(out_jpg):
        try:
            os.remove(out_jpg)
        except OSError:
            pass
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={width}:-2", "-q:v", "3", out_jpg]
    p = _run(cmd, timeout=180)
    ok = (p.returncode == 0 and os.path.isfile(out_jpg)
          and os.path.getsize(out_jpg) > 0)
    if not ok and os.path.isfile(out_jpg):
        try:
            os.remove(out_jpg)
        except OSError:
            pass
    return ok


# --------------------------------------------------------------------------
# 3. 语音转写
# --------------------------------------------------------------------------
_MODEL_CACHE = {}

# 界面上的"识别精度"用这两档。turbo 快一倍，但中文明显更容易出乱字。
MODEL_FAST = "large-v3-turbo"
MODEL_ACCURATE = "large-v3"


def _resolve_model_path(model_size: str) -> str:
    """如果本地已经放好整套模型文件，就直接用目录，不联网。"""
    local = os.path.join(BASE_DIR, "models", f"{model_size}-full")
    if os.path.isfile(os.path.join(local, "model.bin")):
        return local
    return model_size


_CUDA_DLL_HANDLES: list = []
_DEVICE_CACHE: dict = {}


def _cuda_device_count() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001
        return 0


def _cuda_roots() -> list:
    """所有可能放着 pip 版 CUDA 运行库（nvidia\\*\\bin）的目录。

    开发环境是 BASE_DIR\\.venv；打包后 BASE_DIR 变成 _internal（bundle 里
    并没有 .venv），所以还得往安装根目录、exe 所在目录再找一遍。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [os.path.join(BASE_DIR, ".venv", "Lib", "site-packages", "nvidia"),
             os.path.join(here, ".venv", "Lib", "site-packages", "nvidia")]
    bases = [os.environ.get("SMARTCUT_HOME") or "",
             os.path.dirname(os.path.abspath(sys.executable)),
             BASE_DIR]
    for base in bases:
        if not base:
            continue
        roots.append(os.path.join(base, ".venv", "Lib", "site-packages", "nvidia"))
        roots.append(os.path.join(base, "runtime", "nvidia"))
        roots.append(os.path.join(base, "nvidia"))
    try:
        import site as _site
        for p in _site.getsitepackages():
            roots.append(os.path.join(p, "nvidia"))
    except Exception:  # noqa: BLE001
        pass
    out, seen = [], set()
    for r in roots:
        k = os.path.normcase(os.path.abspath(r))
        if k in seen:
            continue
        seen.add(k)
        if os.path.isdir(r):
            out.append(r)
    return out


def _enable_cuda_dlls() -> None:
    """把 pip 装的 nvidia\\*\\bin 注册进 DLL 搜索路径。

    Windows 上 pip 的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 只是把 dll 丢进
    site-packages，不在 PATH 里，ctranslate2 找不到就会报
    "Library cublas64_12.dll is not found or cannot be loaded"。
    """
    if os.name != "nt" or _CUDA_DLL_HANDLES:
        return
    for root in _cuda_roots():
        for sub in sorted(os.listdir(root)):
            b = os.path.join(root, sub, "bin")
            if not os.path.isdir(b):
                continue
            try:
                _CUDA_DLL_HANDLES.append(os.add_dll_directory(b))
            except Exception:  # noqa: BLE001
                pass
            os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def _cuda_runtime_ok() -> bool:
    """CUDA 运行库是真的能加载吗？

    ctranslate2 只在**真正开始算**的时候才去 LoadLibrary cublas/cudnn，
    所以模型能加载、一到识别就抛 "cublas64_12.dll is not found"。
    打包版里 .venv 不在 bundle 内，必须先自己注册目录，再试探性加载一次。
    """
    if os.name != "nt":
        return True
    _enable_cuda_dlls()
    import ctypes
    for name in ("cublas64_12.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.WinDLL(name)
        except Exception:  # noqa: BLE001
            return False
    return True


def _device_plan() -> str:
    """返回 cuda / cpu。环境变量 SC_DEVICE=cpu 可以强制走 CPU。"""
    if _DEVICE_CACHE.get("plan"):
        return _DEVICE_CACHE["plan"]
    want = (os.environ.get("SC_DEVICE") or "auto").strip().lower()
    plan = "cpu"
    if want in ("auto", "cuda", "gpu") and _cuda_device_count() > 0:
        # 有显卡但运行库装不全时直接走 CPU，别等识别到一半才炸
        plan = "cuda" if _cuda_runtime_ok() else "cpu"
    _DEVICE_CACHE["plan"] = plan
    return plan


_MODEL_REPO_FALLBACK = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
    "base": "Systran/faster-whisper-base",
    "tiny": "Systran/faster-whisper-tiny",
}
_MODEL_APPROX_MB = {"large-v3": 3090, "large-v3-turbo": 1550, "medium": 1530,
                    "small": 484, "base": 145, "tiny": 75}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _repo_for(model_size: str) -> str | None:
    try:
        from faster_whisper.utils import _MODELS  # type: ignore
        r = _MODELS.get(model_size)
        if r:
            return r
    except Exception:  # noqa: BLE001
        pass
    return _MODEL_REPO_FALLBACK.get(model_size)


def _download_model(model_size: str, models_dir: str, on_status=None) -> bool:
    """本地没有这个模型时，显式下载并持续上报进度。

    以前是直接把模型名丢给 WhisperModel，它会自己联网下——但没有任何进度，
    一旦网络卡住，界面就永远停在"正在加载模型"。
    """
    dest = os.path.join(models_dir, f"{model_size}-full")
    if os.path.isfile(os.path.join(dest, "model.bin")):
        return True
    repo = _repo_for(model_size)
    if not repo:
        return False
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return False

    os.makedirs(dest, exist_ok=True)
    approx = _MODEL_APPROX_MB.get(model_size)
    if on_status:
        on_status("本地没有 %s 模型，开始下载%s（走镜像，可断点续传）…"
                  % (model_size, ("，约 %.1f GB" % (approx / 1024)) if approx else ""))

    # 首选自带下载器：huggingface_hub 的 snapshot_download 在本机下 3 GB 的
    # large-v3 会传几十秒就停住（turbo 1.5 GB 却正常），所以先用自己写的。
    fetched = False
    script = os.path.join(BASE_DIR, "fetch_model.py")
    if os.path.isfile(script):
        import threading as _th
        stop = _th.Event()
        part = os.path.join(dest, "model.bin.part")

        def _watch():
            last = -1.0
            while not stop.wait(2.0):
                try:
                    mb = os.path.getsize(part) / 1048576.0
                except OSError:
                    mb = 0.0
                if mb - last >= 20 and on_status:
                    last = mb
                    pct = ("（%.1f%%）" % (mb / approx * 100)) if approx else ""
                    on_status("正在下载 %s 模型：已下 %.0f MB%s" % (model_size, mb, pct))

        wt = _th.Thread(target=_watch, daemon=True)
        wt.start()
        try:
            try:
                import winproc as _wp
                _cmd = _wp.script_cmd(script, [model_size])
            except Exception:
                _cmd = ([sys.executable, "--run-script", script, model_size]
                        if getattr(sys, "frozen", False)
                        else [sys.executable, script, model_size])
            rc = subprocess.run(
                _cmd,
                cwd=BASE_DIR, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=3600,
                creationflags=CREATE_NO_WINDOW,
            ).returncode
            fetched = rc == 0 and os.path.isfile(os.path.join(dest, "model.bin"))
        except Exception:  # noqa: BLE001
            fetched = False
        finally:
            stop.set()
            wt.join(timeout=1.0)
        if fetched and on_status:
            on_status("模型 %s 下载完成。" % model_size)
        elif on_status:
            on_status("自带下载器没成功，换 huggingface_hub 再试一次…")

    if fetched:
        return True

    import threading
    stop = threading.Event()
    state = {"last": -1.0, "stall": 0}

    def watch():
        while not stop.wait(2.0):
            mb = _dir_size(dest) / 1048576.0
            if mb - state["last"] >= 15 or (state["last"] < 0 and mb > 0):
                state["last"] = mb
                pct = ("（%.0f%%）" % (mb / approx * 100)) if approx else ""
                if on_status:
                    on_status("正在下载 %s 模型：已下 %.0f MB%s"
                              % (model_size, mb, pct))

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    try:
        snapshot_download(
            repo_id=repo, local_dir=dest,
            allow_patterns=["config.json", "tokenizer.json", "vocabulary.json",
                            "preprocessor_config.json", "model.bin"])
    except Exception as exc:  # noqa: BLE001
        if on_status:
            on_status("下载 %s 失败：%s: %s"
                      % (model_size, type(exc).__name__, str(exc)[:120]))
        return False
    finally:
        stop.set()
        th.join(timeout=1.0)

    ok = os.path.isfile(os.path.join(dest, "model.bin"))
    if on_status:
        on_status("模型 %s 下载完成。" % model_size if ok
                  else "模型 %s 下载不完整。" % model_size)
    return ok


def _load_model(model_size: str, models_dir: str | None, on_status=None):
    from faster_whisper import WhisperModel
    models_dir = models_dir or os.path.join(BASE_DIR, "models")
    os.makedirs(models_dir, exist_ok=True)

    # 关键：本地没有就先自己下，能报进度；不要丢给 WhisperModel 闷头联网
    if not os.path.isdir(_resolve_model_path(model_size)):
        _download_model(model_size, models_dir, on_status=on_status)

    source = _resolve_model_path(model_size)
    threads = max(4, (os.cpu_count() or 8) - 2)
    plan = _device_plan()
    key = (source, models_dir, plan)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]

    if on_status:
        on_status(f"正在加载语音模型 {model_size}"
                  + ("（GPU 加速）…" if plan == "cuda" else "…"))

    def build(device: str, compute_type: str):
        return WhisperModel(
            source, device=device, compute_type=compute_type,
            download_root=models_dir, cpu_threads=threads,
        )

    def build_default():
        if plan == "cuda":
            _enable_cuda_dlls()
            return build("cuda", "float16")
        return build("cpu", "int8")

    try:
        _MODEL_CACHE[key] = build_default()
    except Exception as first:  # noqa: BLE001
        if plan == "cuda":
            # 显卡这条路不通（缺运行库 / 驱动太旧 / 架构不支持），退回 CPU
            if on_status:
                on_status(f"GPU 不可用，改用 CPU：{type(first).__name__} "
                          f"{str(first)[:80]}")
            _DEVICE_CACHE["plan"] = "cpu"
            key = (source, models_dir, "cpu")
            try:
                _MODEL_CACHE[key] = build("cpu", "int8")
            except Exception:  # noqa: BLE001
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                _MODEL_CACHE[key] = build("cpu", "int8")
            return _MODEL_CACHE[key]
        # 国内直连 HuggingFace 经常超时，换镜像再试一次
        if on_status:
            on_status(f"直连下载失败，换镜像重试：{type(first).__name__}")
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        _MODEL_CACHE[key] = build("cpu", "int8")
    return _MODEL_CACHE[key]


def normalize_for_asr(audio: np.ndarray, target: float = 0.12,
                      max_gain: float = 12.0) -> np.ndarray:
    """用静态增益把音量拉齐，替代 loudnorm。

    为什么不用 loudnorm：它要把整条音轨完整过一遍滤镜，64 分钟的素材
    光这一步就要 54 秒；而且它是"逐段自适应"的，对 Whisper 并不比
    一次性增益更好。这里只取一个幅度分位数算系数，O(n) 扫一遍，
    几乎不花时间。
    """
    if audio is None or audio.size == 0:
        return audio

    # 按 20 秒一个窗口分别算增益：这样"开头很轻、后面很响"的素材
    # 不会被一个全局系数埋掉。窗口之间的增益做线性过渡，避免爆音。
    sr = 16000
    win = sr * 20
    n = len(audio)
    if n <= win:
        ref = float(np.percentile(np.abs(audio), 90.0))
        if ref < 1e-5:
            return audio
        g = float(min(max_gain, max(0.5, target / ref)))
        return np.clip(audio * g, -1.0, 1.0).astype(np.float32)

    gains = []
    for i in range(0, n, win):
        seg = audio[i:i + win]
        ref = float(np.percentile(np.abs(seg), 90.0)) if seg.size else 0.0
        if ref < 1e-5:
            gains.append(1.0)             # 真静音就别放大
        else:
            gains.append(float(min(max_gain, max(0.5, target / ref))))
    # 不做任何跨窗口平均：每个窗口按自己的音量处理，
    # 否则轻的段落会被邻近响的窗口“平均掉”，又回到丢内容的老问题。
    out = np.empty_like(audio)
    for i, g in enumerate(gains):
        a = i * win
        b = min(n, a + win)
        if b <= a:
            break
        out[a:b] = audio[a:b] * g
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def _obj_value(obj, name, default=None):
    """同时读取 faster-whisper 对象和测试/兼容用字典。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _float_or_none(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _audio_track_ids(source_audio_track):
    if isinstance(source_audio_track, (list, tuple)):
        return [int(x) for x in source_audio_track if isinstance(x, (int, np.integer))]
    if isinstance(source_audio_track, (int, np.integer)):
        return [int(source_audio_track)]
    return []


def _visible_text_length(text):
    """Count spoken characters while ignoring punctuation and whitespace."""
    return len(re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE))


def _timing_alignment_flags(seg, text, start, end, word_rows):
    """Return deterministic warnings for impossible Whisper word alignment.

    A high word probability only says that Whisper likes the token.  It does
    not prove that the token was placed at the right time.  In particular,
    batched decoding can occasionally stretch one Chinese character over many
    seconds or put two neighbouring words hundreds of seconds apart.  Those
    rows must be re-heard before they are allowed to become subtitles.
    """
    details = []
    duration = max(0.0, float(end) - float(start))
    visible = _visible_text_length(text)
    if visible and duration > max(6.0, visible * 2.5):
        details.append("short_text_long_duration")

    ordered = sorted(
        (row for row in (word_rows or [])
         if _float_or_none(row.get("start")) is not None
         and _float_or_none(row.get("end")) is not None),
        key=lambda row: float(row["start"]))
    # Whisper occasionally compresses a stock phrase into a few hundred
    # milliseconds: the first token has near-zero probability and the
    # remaining tokens collapse to identical timestamps.  Average confidence
    # looks deceptively high in that case, so detect the physical impossibility
    # from speech rate and word geometry instead of trusting the mean score.
    zero_words = [row for row in ordered
                  if float(row["end"]) - float(row["start"]) < 0.015]
    probabilities = [_float_or_none(row.get("probability")) for row in ordered]
    probabilities = [value for value in probabilities if value is not None]
    compact_text = re.sub(r"\s+", "", str(text or "")).lower()
    template_prefixes = (
        "请不吝", "感谢观看", "字幕由", "中文字幕", "订阅转发",
        "支持明镜", "语言为普通话", "amara.org",
    )
    if (probabilities and min(probabilities) < 0.01
            and any(token in compact_text for token in template_prefixes)):
        details.append("template_hallucination_suspected")
    collapsed_short = (
        visible >= 4 and 0.0 < duration < 0.40
        and visible / max(duration, 1e-6) > 14.0
        and len(zero_words) >= max(2, len(ordered) // 2)
        and probabilities and min(probabilities) < 0.05)
    if collapsed_short:
        details.extend(("impossible_speech_rate", "collapsed_word_timestamps"))
    for row in ordered:
        word_duration = float(row["end"]) - float(row["start"])
        word_visible = _visible_text_length(row.get("text"))
        if (word_visible and
                word_duration > max(2.5, word_visible * 1.5)):
            details.append("word_duration_outlier")
            break
    if any(float(right["start"]) - float(left["end"]) > 8.0
           for left, right in zip(ordered, ordered[1:])):
        details.append("word_gap_outlier")

    details = list(dict.fromkeys(details))
    return (["timestamp_alignment_suspected"] + details) if details else []


def _parent_timing_diagnostics(seg):
    """Describe sparse restored Whisper parents without blocking child rows.

    With VAD enabled, faster-whisper may restore one decoder segment across
    several distant speech islands.  The child word timestamps can still be
    valid, so parent sparsity is diagnostic only.  Missing human speech is
    checked independently by the full-track Silero coverage gate.
    """
    parent_words = []
    for word in _obj_value(seg, "words", None) or []:
        row = _word_evidence(word)
        if row is not None:
            parent_words.append(row)
    parent_words.sort(key=lambda row: float(row["start"]))
    parent_start = _float_or_none(_obj_value(seg, "start"))
    parent_end = _float_or_none(_obj_value(seg, "end"))
    parent_duration = ((parent_end - parent_start)
                       if parent_start is not None and parent_end is not None
                       else 0.0)
    details = []
    if any(float(right["start"]) - float(left["end"]) > 8.0
           for left, right in zip(parent_words, parent_words[1:])):
        details.append("parent_word_gap_outlier")
    if parent_duration > 20.0:
        if not parent_words:
            details.append("parent_words_missing")
        else:
            envelope = max(0.0, float(parent_words[-1]["end"])
                           - float(parent_words[0]["start"]))
            if envelope / parent_duration < 0.35:
                details.append("parent_word_coverage_outlier")
    return details


def _word_evidence(word):
    start = _float_or_none(_obj_value(word, "start"))
    end = _float_or_none(_obj_value(word, "end"))
    if start is None or end is None or end < start:
        return None
    probability = _float_or_none(_obj_value(word, "probability"))
    if probability is None:
        probability = _float_or_none(_obj_value(word, "confidence"))
    return {
        "text": str(_obj_value(word, "word", "") or ""),
        # 不 round：边界精修必须拿到 Whisper 返回的原始浮点时间。
        "start": start,
        "end": end,
        "probability": probability,
    }


def _append_segment_evidence(evidence_out, seg, text, start, end, words,
                             source_audio_track=None, split_reason="unsplit"):
    if evidence_out is None:
        return
    word_rows = []
    for word in words or []:
        row = _word_evidence(word)
        if row is not None:
            word_rows.append(row)
    probabilities = [w["probability"] for w in word_rows
                     if w.get("probability") is not None]
    confidence = (sum(probabilities) / len(probabilities)) if probabilities else None
    tracks = _audio_track_ids(source_audio_track)
    original_start = _float_or_none(_obj_value(seg, "start"))
    original_end = _float_or_none(_obj_value(seg, "end"))
    timing_flags = _timing_alignment_flags(
        seg, text, start, end, word_rows)
    evidence_out.append({
        "start": float(start),
        "end": float(end),
        "text": str(text or ""),
        "text_original": str(text or ""),
        "words": word_rows,
        "confidence": confidence,
        "source_audio_track": tracks[0] if len(tracks) == 1 else None,
        "source_audio_tracks": tracks,
        "original_asr_segment_start": original_start,
        "original_asr_segment_end": original_end,
        "avg_logprob": _float_or_none(_obj_value(seg, "avg_logprob")),
        "no_speech_prob": _float_or_none(_obj_value(seg, "no_speech_prob")),
        "split_reason": split_reason,
        "timing_source": "faster-whisper-word-timestamps" if word_rows
                         else "faster-whisper-segment",
        # A repeated phrase may be real speech. Keep it and expose the concern
        # to downstream review instead of erasing the whole subtitle sentence.
        "asr_flags": list(dict.fromkeys(
            (["repetition_suspected"] if looks_like_loop(text) else [])
            + timing_flags)),
        "asr_diagnostics": _parent_timing_diagnostics(seg),
    })


def _split_long_segment(seg, text, max_len=12.0, prefer_gap=0.45,
                        evidence_out=None, source_audio_track=None):
    """把过长的识别片段按“词边界”拆成几句。

    Whisper 有时会把一句短话和一个很长的静音并成一条 100 秒的片段，
    结果就是：字幕出现一个两分钟的字幕块、卡片切出一张两分钟的巨卡。
    有了词级时间戳就能在词与词之间断开，绝不切在字中间。
    """
    a, b = float(seg.start), float(seg.end)
    words = getattr(seg, "words", None) or []
    has_split_gap = any(
        float(right.start) - float(left.end) >= float(prefer_gap)
        for left, right in zip(words, words[1:]))
    if b - a <= max_len and not has_split_gap:
        _append_segment_evidence(evidence_out, seg, text, a, b, words,
                                 source_audio_track, "unsplit")
        return [(a, b, text)]
    if len(words) < 2:
        _append_segment_evidence(evidence_out, seg, text, a, b, words,
                                 source_audio_track, "no-word-boundary")
        return [(a, b, text)]
    aligned_text = "".join(str(word.word) for word in words)
    if re.sub(r"\s+", "", aligned_text) != re.sub(r"\s+", "", text):
        # Word alignment can omit a token even when segment.text contains it.
        # The original sentence is authoritative; splitting must never drop it.
        _append_segment_evidence(evidence_out, seg, text, a, b, words,
                                 source_audio_track, "unaligned-text-preserved")
        return [(a, b, text)]
    out = []
    cur_start = a
    cur_words = []
    for i, w in enumerate(words):
        cur_words.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        span = float(w.end) - cur_start
        gap = (float(nxt.start) - float(w.end)) if nxt else 999.0
        if nxt is None or span >= max_len or gap >= prefer_gap:
            piece = "".join(str(x.word) for x in cur_words).strip()
            if piece:
                piece_end = float(w.end)
                out.append((cur_start, piece_end, piece))
                reason = ("segment-end" if nxt is None else
                          "duration" if span >= max_len else "word-gap")
                _append_segment_evidence(
                    evidence_out, seg, piece, cur_start, piece_end, cur_words,
                    source_audio_track, reason)
            cur_words = []
            cur_start = float(nxt.start) if nxt else float(w.end)
    if out:
        return out
    _append_segment_evidence(evidence_out, seg, text, a, b, words,
                             source_audio_track, "split-fallback")
    return [(a, b, text)]


def _fallback_segment_evidence(start, end, text, source_audio_track=None):
    tracks = _audio_track_ids(source_audio_track)
    return {
        "start": float(start),
        "end": float(end),
        "text": str(text or ""),
        "text_original": str(text or ""),
        "words": [],
        "confidence": None,
        "source_audio_track": tracks[0] if len(tracks) == 1 else None,
        "source_audio_tracks": tracks,
        "original_asr_segment_start": float(start),
        "original_asr_segment_end": float(end),
        "avg_logprob": None,
        "no_speech_prob": None,
        "split_reason": "compatibility-fallback",
        "timing_source": "legacy-segment",
    }


def align_segment_evidence(text_segs, evidence, source_audio_track=None):
    """把纠错后的句子与原始 ASR 证据重新对齐，并分配稳定引用 ID。

    术语纠错只应改文字，不应破坏 Whisper 的原始词边界。正常情况下两边
    一一对应；兼容第三方纠错器改变列表长度的情况时，再按时间重叠匹配。
    """
    available = [dict(row) for row in (evidence or []) if isinstance(row, dict)]
    unused = set(range(len(available)))
    result = []
    for order, (start, end, text) in enumerate(text_segs or [], 1):
        start, end = float(start), float(end)
        best = None
        best_key = None
        # Normal generation keeps the two lists paired. Prefer that exact row
        # before falling back to overlap matching, especially when two speech
        # tracks contain utterances at the same time.
        positional = order - 1
        if positional in unused:
            row = available[positional]
            ra = _float_or_none(row.get("start"))
            rb = _float_or_none(row.get("end"))
            if (ra is not None and rb is not None
                    and abs(start - ra) <= 0.02 and abs(end - rb) <= 0.02):
                best = positional
        for idx in sorted(unused):
            if best is not None:
                break
            row = available[idx]
            ra = _float_or_none(row.get("start"))
            rb = _float_or_none(row.get("end"))
            if ra is None or rb is None:
                continue
            overlap = max(0.0, min(end, rb) - max(start, ra))
            distance = abs(start - ra) + abs(end - rb)
            key = (overlap > 0.0, overlap, -distance)
            if best_key is None or key > best_key:
                best, best_key = idx, key
        if best is None:
            row = _fallback_segment_evidence(start, end, text, source_audio_track)
        else:
            unused.remove(best)
            row = available[best]
        row["segment_id"] = "s%06d" % order
        row["start"] = start
        row["end"] = end
        row["text"] = str(text or "")
        row.setdefault("text_original", row["text"])
        row.setdefault("words", [])
        tracks = row.get("source_audio_tracks") or _audio_track_ids(source_audio_track)
        row["source_audio_tracks"] = _audio_track_ids(tracks)
        row["source_audio_track"] = (row["source_audio_tracks"][0]
                                     if len(row["source_audio_tracks"]) == 1 else None)
        result.append(row)
    return result


def sort_segment_evidence_pairs(text_segs, evidence):
    """Sort already-paired ASR rows without separating simultaneous tracks."""
    segments = list(text_segs or [])
    rows = list(evidence or [])
    if len(segments) != len(rows):
        return segments, rows, [{
            "reason": "segment_evidence_count_mismatch",
            "segment_count": len(segments), "evidence_count": len(rows),
        }]
    pairs = []
    for order, (segment, row) in enumerate(zip(segments, rows)):
        try:
            start, end, text = float(segment[0]), float(segment[1]), str(segment[2])
        except (IndexError, TypeError, ValueError):
            return segments, rows, [{
                "reason": "invalid_segment", "index": order,
            }]
        item = dict(row) if isinstance(row, dict) else {}
        tracks = _audio_track_ids(
            item.get("source_audio_tracks") or item.get("source_audio_track"))
        track_key = tuple(tracks) if tracks else (10 ** 9,)
        pairs.append(((start, end, text), item, track_key, order))
    pairs.sort(key=lambda pair: (
        pair[0][0], pair[0][1], pair[2], pair[3]))
    return ([pair[0] for pair in pairs], [pair[1] for pair in pairs], [])


def get_segment_evidence(index_data):
    """读取新索引证据；旧索引自动从 ``segments`` 构造保守兼容证据。"""
    if not isinstance(index_data, dict):
        return []
    raw = index_data.get("segment_evidence")
    segments = index_data.get("segments") or []
    source = index_data.get("audio_stream_used")
    if isinstance(raw, list) and raw:
        tuples = []
        for order, row in enumerate(raw):
            if not isinstance(row, dict):
                continue
            try:
                tuples.append((float(row["start"]), float(row["end"]),
                               str(row.get("text") or "")))
            except (KeyError, TypeError, ValueError):
                if order < len(segments) and len(segments[order]) >= 3:
                    tuples.append(tuple(segments[order][:3]))
        return align_segment_evidence(tuples, raw, source)
    tuples = []
    for row in segments:
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            try:
                tuples.append((float(row[0]), float(row[1]), str(row[2] or "")))
            except (TypeError, ValueError):
                continue
    return align_segment_evidence(tuples, [], source)


_VAD_READY = None


def _vad_ready() -> bool:
    """确认 faster_whisper 的静音检测模型（silero_vad_v6.onnx）真的能加载。

    打包后 faster_whisper 的代码在 exe 里，它按
    dirname(faster_whisper.utils.__file__)+"assets" 找模型 —— 打包后就是
    <安装目录>\\_internal\\faster_whisper\\assets\\silero_vad_v6.onnx。
    打包时漏带这个文件，就会在"正在识别说话内容"这一步抛 NoSuchFile，
    整条队列全部失败；而开发环境跑得好好的，极难联想到是打包漏文件（11-22 踩过）。

    兜底：别处能找到就复制到那个位置；实在找不到就关掉 VAD 降级，
    宁可句子黏一点，也不要整个片子分析不出来。
    """
    global _VAD_READY
    if _VAD_READY is not None:
        return bool(_VAD_READY)
    _VAD_READY = False
    try:
        import faster_whisper.utils as _fw_utils
    except Exception:  # noqa: BLE001
        return False
    name = "silero_vad_v6.onnx"
    try:
        target = os.path.join(_fw_utils.get_assets_path(), name)
    except Exception:  # noqa: BLE001
        return False
    if os.path.isfile(target):
        _VAD_READY = True
        return True

    here = os.path.dirname(os.path.abspath(__file__))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    meipass = getattr(sys, "_MEIPASS", "") or ""
    cands = [
        os.environ.get("SMARTCUT_VAD") or "",
        os.path.join(BASE_DIR, "assets", name),
        os.path.join(here, "assets", name),
        os.path.join(meipass, "assets", name) if meipass else "",
        os.path.join(exe_dir, "_internal", "faster_whisper", "assets", name),
        # 依赖装在别处时（虚拟环境 / 另一个安装目录）最后再试原包位置
        os.path.join(os.path.dirname(os.path.abspath(
            getattr(_fw_utils, "__file__", "") or "")), "assets", name),
    ]
    for src in cands:
        if not src or not os.path.isfile(src):
            continue
        try:
            if os.path.normcase(os.path.abspath(src)) != os.path.normcase(
                    os.path.abspath(target)):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(src, target)
            _VAD_READY = os.path.isfile(target)
        except Exception:  # noqa: BLE001
            _VAD_READY = False
        if _VAD_READY:
            return True
    return False


def silero_speech_windows(audio, sr=16000):
    """Return confirmed human-speech windows, or ``(None, reason)`` on failure.

    RMS activity is not human-speech evidence: game effects, music and ambient
    noise can all be loud. Only faster-whisper's Silero VAD output may drive a
    missing-speech re-listen or a hard subtitle export blocker.
    """
    if audio is None or getattr(audio, "size", 0) == 0:
        return [], ""
    if not _vad_ready():
        return None, "Silero VAD 模型不可用"
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        # Coverage validation is deliberately stricter than ASR chunking:
        # clicks and game effects must not become subtitle export blockers.
        options = {
            "threshold": 0.55,
            "min_speech_duration_ms": 300,
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 150,
        }
        vad_options = VadOptions(**options)
        timestamps = get_speech_timestamps(
            np.asarray(audio, dtype=np.float32), vad_options=vad_options,
            sampling_rate=int(sr))
        windows = []
        for row in timestamps or []:
            try:
                start = float(row["start"]) / float(sr)
                end = float(row["end"]) / float(sr)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            if end - start >= 0.12:
                windows.append((start, end))
        return _merge_alignment_windows(windows, gap=0.10), ""
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def probe_tracks(path, tracks, sr=16000, max_seconds=None,
                 voice_ratio_min=0.55, on_status=None, audio_cache=None):
    """对每条轨判“是说话轨还是其他轨”——只跑 VAD，不转写。

    为什么需要（子枫 2026-11-23）：
      转写要发生在“用户勾选音轨之后”，而且要分清“这条轨在说话”还是
      “这条轨是游戏/音乐”。逐条转写太慢（large-v3 每条 2 分钟），
      而 VAD 十几秒就能给答案。

    判据（实测）：
      人声占比高 → 说话轨
      人声占比低 → 其他轨（游戏/音乐/空）

    返回 {track_index: {"voice_ratio": 0.62, "kind": "speech"}}
    """
    out = {}
    tracks = [int(t) for t in (tracks or []) if isinstance(t, int)]
    if not tracks:
        return out
    try:
        import numpy as np
    except ImportError:
        return out
    for tr in tracks:
        try:
            if audio_cache is not None:
                entry = audio_cache.entry(tr)
                review_end = (float(max_seconds) if max_seconds
                              else float(entry["duration"]))
                audio = audio_cache.review_samples(tr, 0.0, review_end)
            else:
                wav = os.path.join(tempfile.gettempdir(),
                                   "probe_track_%d.wav" % tr)
                ok = extract_audio_wav(path, wav, stream=tr, sr=sr,
                                       max_seconds=max_seconds, normalize=False)
                if not ok:
                    out[tr] = {"voice_ratio": 0.0, "kind": "unknown"}
                    continue
                audio = read_wav_mono(wav)
                try:
                    os.remove(wav)
                except OSError:
                    pass
            if audio.size < sr:
                out[tr] = {"voice_ratio": 0.0, "kind": "empty"}
                continue
            # 用能量包络近似“有没有人声”：说话是“有起伏的能量”，
            # 游戏音效是“突发的尖峰”，音乐是“一直在”。这里只用
            # 一个粗判据（有声占比），够用来分开“说话”和“其他”。
            win = max(1, int(sr * 0.05))
            n = audio.size // win
            frames = audio[:n * win].reshape(n, win)
            db = 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)),
                                          1e-9))
            voiced = float((db > -45).mean())
            silent = float((db < -55).mean())
            # 说话的真正特征不是“有声”，是“**有停顿**”：
            # 人说话必然一句一顿；音乐是一直在响；游戏音效是零星尖峰。
            # 实测：人声轨 有声 57.6% / 静音 39.9%；音乐轨 有声 69.1% / 静音 11.6%。
            if voiced >= 0.35 and silent >= 0.30:
                kind = "speech"        # 有说话的停顿节奏
            elif voiced >= 0.55:
                kind = "dense"         # 一直在响 → 音乐 / 持续音效
            else:
                kind = "other"         # 大部分是静音 → 空轨 / 零星音效
            out[tr] = {"voice_ratio": round(voiced, 3),
                        "silent_ratio": round(silent, 3), "kind": kind}
        except audio_evidence_cache.AudioEvidenceCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            out[tr] = {"voice_ratio": 0.0, "kind": "error",
                        "why": str(exc)[:60]}
    return out


def transcribe(path, model_size: str = MODEL_ACCURATE,
               models_dir: str | None = None, on_status=None,
               initial_prompt: str | None = None, beam_size: int | None = None,
               on_progress=None, batch_size: int = 0, should_stop=None,
               evidence_out=None, source_audio_track=None,
               hotwords: str | None = None):
    """返回 [(start, end, text)]；没装 faster-whisper 就返回空列表。

    path 可以是文件路径，也可以是 16kHz 单声道 float32 的 numpy 数组
    （faster-whisper 原生支持数组，省掉一次落盘和二次解码）。
    """
    global _VAD_READY

    def _stop_check():
        if should_stop and should_stop():
            raise IndexCancelled("用户停止")

    _stop_check()
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        if on_status:
            on_status("未安装 faster-whisper，跳过转写（只做画面和音量分析）")
        return []

    model = _load_model(model_size, models_dir, on_status=on_status)
    beam_size = asr_hints.beam_size(beam_size)
    default_context, default_hotwords, _selected = asr_hints.build(
        os.environ.get("SMARTCUT_HOME") or BASE_DIR,
        extra_terms=JARGON_PROMPT.split())
    prompt = default_context if initial_prompt is None else initial_prompt
    hints = default_hotwords if hotwords is None else hotwords

    vad = _vad_ready()
    if on_status:
        on_status("正在识别说话内容…")
        if not vad:
            on_status("静音检测模型缺失，先关掉 VAD 继续（能出片，句子可能偏长）")
    # 批处理：faster-whisper 1.2 的 BatchedInferencePipeline，实测 3.5 倍速，
    # 代价是它会把话并成长段（靠后面的按词拆句还原）。失败自动退回逐条。
    if batch_size and batch_size > 1:
        try:
            from faster_whisper import BatchedInferencePipeline
            _pipe = BatchedInferencePipeline(model=model)
            if on_status:
                on_status("批处理识别（快约 3 倍）…")
            _gs, _gi = _pipe.transcribe(
                path, language="zh", vad_filter=vad, beam_size=beam_size,
                vad_parameters=dict(asr_hints.VAD_PARAMETERS),
                initial_prompt=prompt, hotwords=hints or None,
                condition_on_previous_text=False, temperature=0.0,
                compression_ratio_threshold=asr_hints.COMPRESSION_RATIO_THRESHOLD,
                no_speech_threshold=asr_hints.NO_SPEECH_THRESHOLD,
                word_timestamps=True, batch_size=int(batch_size))
            total0 = float(getattr(_gi, "duration", 0.0) or 0.0)
            out0, dropped0 = [], 0
            batch_evidence = []
            for seg in _gs:
                _stop_check()
                text = (seg.text or "").strip()
                if text and looks_like_loop(text):
                    dropped0 += 1
                if text:
                    out0.extend(_split_long_segment(seg, text, max_len=8.0,
                                                    prefer_gap=0.35,
                                                    evidence_out=batch_evidence,
                                                    source_audio_track=source_audio_track))
                if on_progress and total0 > 0:
                    on_progress(min(1.0, float(seg.end) / total0))
            if on_progress:
                on_progress(1.0)
            if out0:
                if evidence_out is not None:
                    evidence_out.extend(batch_evidence)
                if dropped0 and on_status:
                    on_status("保留 %d 句重复疑点，供后续复核" % dropped0)
                return out0
        except IndexCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if on_status:
                on_status("批处理没跑成，退回逐条：%s" % str(exc)[:60])
    _kw = dict(
        language="zh", beam_size=beam_size,
        vad_parameters=dict(asr_hints.VAD_PARAMETERS),
        initial_prompt=prompt, hotwords=hints or None,
        condition_on_previous_text=False,
        temperature=0.0,
        compression_ratio_threshold=asr_hints.COMPRESSION_RATIO_THRESHOLD,
        no_speech_threshold=asr_hints.NO_SPEECH_THRESHOLD,
        word_timestamps=True,     # 长句要按词边界拆，不然会出现 100 多秒的“句子”
    )
    try:
        segments, _info = model.transcribe(path, vad_filter=vad, **_kw)
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        if ("cublas" in low or "cudnn" in low or "cannot be loaded" in low) \
                and _DEVICE_CACHE.get("plan") == "cuda":
            # 显卡这条路算不了（CUDA 运行库没找到）：换 CPU 重来一次，
            # 慢一点但能出片，比整条队列失败强。
            if on_status:
                on_status("CUDA 运行库用不了，改用 CPU 重跑：%s" % str(exc)[:50])
            _DEVICE_CACHE["plan"] = "cpu"
            model = _load_model(model_size, models_dir, on_status=on_status)
            segments, _info = model.transcribe(path, vad_filter=vad, **_kw)
        elif vad and "silero_vad" in low:
            # VAD 模型真的加载不出来（打包漏文件 / onnxruntime 缺 dll）时，
            # 关掉 VAD 重跑一遍，别让整条队列全挂在这一个可选功能上。
            if on_status:
                on_status("静音检测模型加载失败，关掉 VAD 重跑（%s）" % str(exc)[:40])
            _VAD_READY = False
            segments, _info = model.transcribe(path, vad_filter=False, **_kw)
        else:
            raise
    total = float(getattr(_info, "duration", 0.0) or 0.0)
    out = []
    dropped = 0
    for seg in segments:
        _stop_check()
        text = (seg.text or "").strip()
        if text and looks_like_loop(text):
            dropped += 1
        if text:
            out.extend(_split_long_segment(
                seg, text, evidence_out=evidence_out,
                source_audio_track=source_audio_track))
        if on_progress and total > 0:
            on_progress(min(1.0, float(seg.end) / total))
    if on_progress:
        on_progress(1.0)
    if dropped and on_status:
        on_status(f"保留 {dropped} 句重复疑点和原词置信度，供后续复核")
    return out


def fill_missing_head(text_segs, audio, model_size: str = MODEL_ACCURATE,
                      models_dir: str | None = None, on_status=None,
                      min_gap: float = 12.0, window: float = 120.0,
                      should_stop=None, evidence_out=None,
                      source_audio_track=None, beam_size=None):
    """开头复核：第一句来得太晚就单独把开头重转一遍补进去。

    为什么要这一步：Whisper 对开头的处理最不稳定——片头往往是「轻声的问候 +
    游戏还在加载」，VAD 容易整段跳过，或者被提示词带跑成复读。
    一旦开头丢了，成片就没有开场白，而 B 站前 3 秒跳出率全靠这段。

    做法：只补「第一句之前」的那段，不动已经识别好的内容。
    重转时不带任何提示词（实测空提示词的开头最准），窗口默认 120 秒。
    """
    if not text_segs or audio is None or getattr(audio, "size", 0) == 0:
        return text_segs
    try:
        first = min(float(s[0]) for s in text_segs)
    except (TypeError, IndexError):
        return text_segs
    if first <= min_gap:
        return text_segs

    sr = 16000
    dur = float(audio.size) / sr
    # Only inspect the uncovered prefix, with a fixed upper bound. A long
    # silent intro must not silently turn a 120-second check into a full scan.
    head_end = min(dur, first, max(0.0, float(window)))
    head_audio = audio[:int(head_end * sr)]
    if not head_audio.size or not np.any(np.abs(head_audio) > 1e-5):
        if on_status:
            on_status("开头复核跳过：未识别区域为静音")
        return text_segs
    if on_status:
        on_status("开头 %.0f 秒没识别到台词，单独复核一次…" % head_end)
    head_evidence = []
    try:
        segs = transcribe(head_audio, model_size=model_size,
                          models_dir=models_dir, initial_prompt="",
                          hotwords="",
                          beam_size=beam_size,
                          should_stop=should_stop,
                          evidence_out=head_evidence,
                          source_audio_track=source_audio_track)
    except IndexCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        if on_status:
            on_status("开头复核没跑成（其余部分不受影响）：%s" % str(exc)[:80])
        return text_segs

    add = []
    add_evidence = []
    for pos, (a, b, t) in enumerate(segs):
        a, b = float(a), float(b)
        if a >= first - 0.5:
            continue                      # 已经和旧内容重叠，不要
        row = (dict(head_evidence[pos]) if pos < len(head_evidence)
               else _fallback_segment_evidence(a, b, t, source_audio_track))
        if b > first:
            # Cropping only the time would leave words from the existing next
            # sentence in this subtitle. Only crop text with real word evidence.
            kept_words = [w for w in row.get("words") or []
                          if _float_or_none(w.get("start")) is not None
                          and _float_or_none(w.get("end")) is not None
                          and float(w["start"]) >= a and float(w["end"]) <= first]
            if not kept_words:
                continue
            t = "".join(str(w.get("text") or "") for w in kept_words).strip()
            a, b = max(a, float(kept_words[0]["start"])), float(kept_words[-1]["end"])
            row["words"] = kept_words
        if b - a >= 0.2 and (t or "").strip():
            add.append((a, b, t))
            row["start"], row["end"], row["text"] = a, b, str(t or "")
            # 跨到原有第一句的词不能冒充为“已保留”，只保留完整落在补回区间内的词。
            row["words"] = [w for w in (row.get("words") or [])
                            if _float_or_none(w.get("start")) is not None
                            and _float_or_none(w.get("end")) is not None
                            and float(w["start"]) >= a and float(w["end"]) <= b]
            add_evidence.append(row)
    if not add:
        if on_status:
            on_status("开头复核：未发现可可靠补回的台词")
        return text_segs

    paired = sorted(zip(add, add_evidence), key=lambda pair: pair[0][0])
    add = [pair[0] for pair in paired]
    add_evidence = [pair[1] for pair in paired]
    merged = add + [(float(a), float(b), t) for a, b, t in text_segs]
    if evidence_out is not None:
        evidence_out[:] = add_evidence + list(evidence_out)
    if on_status:
        on_status("开头补回 %d 句：%s" % (len(add), add[0][2][:24]))
    return merged


def _has_timing_alignment_warning(row):
    if not isinstance(row, dict):
        return False
    flags = {str(value) for value in (row.get("asr_flags") or [])}
    if "timestamp_alignment_suspected" not in flags:
        return False
    # A previous bounded re-listen that explicitly failed is itself a local
    # delivery blocker and must remain eligible for the later finite retry.
    if "alignment_repair_failed" in flags:
        return True
    # v4 and earlier copied parent sparsity onto every child row.  Parent gaps
    # are normal after faster-whisper restores VAD-compressed timestamps; only
    # a defect in this emitted child is eligible for destructive repair.
    parent_only = {
        "timestamp_alignment_suspected", "parent_word_gap_outlier",
        "parent_word_coverage_outlier", "parent_words_missing",
        "alignment_repair_failed",
    }
    return bool(flags - parent_only) or flags == {"timestamp_alignment_suspected"}


_LOCAL_ASR_HALLUCINATIONS = (
    "amara.org", "字幕由", "字幕组", "感谢观看", "请不吝点赞",
    "支持明镜", "语言为普通话", "订阅转发打赏",
)

_DEFINITE_ASR_HALLUCINATIONS = (
    "支持明镜与点点栏目", "amara.org", "中文字幕由", "字幕组提供",
    "语言为普通话",
)


def _definite_asr_hallucination(text):
    value = re.sub(r"\s+", "", str(text or "")).lower()
    return bool(value) and any(
        token in value for token in _DEFINITE_ASR_HALLUCINATIONS)


def _definite_timing_hallucination(row, text):
    """Return True for an acoustically impossible collapsed decoder phrase."""
    if not isinstance(row, dict):
        return False
    flags = {str(value) for value in (row.get("asr_flags") or [])}
    if "delivery_stretched_overlap_hallucination" in flags:
        return True
    start = _float_or_none(row.get("start"))
    end = _float_or_none(row.get("end"))
    if start is None or end is None or end <= start:
        return False
    flags = set(_timing_alignment_flags(
        row, text, start, end, list(row.get("words") or [])))
    return {"impossible_speech_rate", "collapsed_word_timestamps"}.issubset(flags)


def _low_confidence_template_hallucination(row, text):
    """Treat a stock decoder phrase as definite only with acoustic evidence."""
    if not isinstance(row, dict):
        return False
    flags = set(_timing_alignment_flags(
        row, text, row.get("start", 0.0), row.get("end", 0.0),
        list(row.get("words") or [])))
    return "template_hallucination_suspected" in flags


def _local_asr_is_reliable(row, text):
    """Reject short-window prompt/noise hallucinations before they reach captions."""
    value = re.sub(r"\s+", "", str(text or "")).lower()
    if not value or any(token in value for token in _LOCAL_ASR_HALLUCINATIONS):
        return False
    no_speech = _float_or_none((row or {}).get("no_speech_prob"))
    if no_speech is not None and no_speech >= 0.60:
        return False
    avg_logprob = _float_or_none((row or {}).get("avg_logprob"))
    if avg_logprob is not None and avg_logprob < -1.0:
        return False
    return not _has_timing_alignment_warning(row)


def _loop_suspected_text(text):
    """Detect Whisper loops even when spaces hide the repeated pattern."""
    value = str(text or "").strip()
    compact = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    tokens = re.findall(r"[\w\u3400-\u9fff]+", value.lower(), flags=re.UNICODE)
    repeated_tokens = any(
        tokens[index] == tokens[index + 1] == tokens[index + 2]
        for index in range(max(0, len(tokens) - 2)))
    return repeated_tokens or looks_like_loop(value) or looks_like_loop(compact)


def _repetition_units(text):
    """Extract the small token/pattern that a Whisper loop is repeating."""
    value = str(text or "").strip().lower()
    compact = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    units = set()
    tokens = re.findall(r"[\w\u3400-\u9fff]+", value, flags=re.UNICODE)
    if len(tokens) >= 3 and len(set(tokens)) == 1:
        units.add(re.sub(r"[\s\W_]+", "", tokens[0], flags=re.UNICODE))
    for match in re.finditer(r"(.{1,8}?)(?:\1){2,}", compact):
        unit = str(match.group(1) or "")
        if unit:
            units.add(unit)
    return {unit for unit in units if unit}


def _is_repetition_shard(text, units):
    """Return True for split-off pieces of an already confirmed loop unit."""
    compact = re.sub(r"[\s\W_]+", "", str(text or "").lower(), flags=re.UNICODE)
    if not compact:
        return False
    for unit in units or ():
        if compact in unit:
            return True
        if len(compact) % len(unit) == 0 and compact == unit * (len(compact) // len(unit)):
            return True
    return False


def _repetition_has_credible_word_timing(row):
    """Distinguish a spoken stutter from zero-duration Whisper loop tokens."""
    words = list((row or {}).get("words") or [])
    if len(words) < 4:
        return False
    previous_end = None
    for word in words:
        start = _float_or_none((word or {}).get("start"))
        end = _float_or_none((word or {}).get("end"))
        if start is None or end is None or end - start < 0.09:
            return False
        if previous_end is not None and start < previous_end - 0.02:
            return False
        previous_end = end
    return True


def _same_audio_track(left, right):
    left_tracks = set(_audio_track_ids(
        (left or {}).get("source_audio_tracks") or
        (left or {}).get("source_audio_track")))
    right_tracks = set(_audio_track_ids(
        (right or {}).get("source_audio_tracks") or
        (right or {}).get("source_audio_track")))
    # Unknown is not evidence that two rows came from the same track.  Treating
    # it as a match can erase a valid caption from another Premiere audio lane.
    return bool(left_tracks and right_tracks and (left_tracks & right_tracks))


def _span_overlap(left, right):
    return max(0.0, min(float(left[1]), float(right[1]))
               - max(float(left[0]), float(right[0])))


def _prune_overlapping_asr_fragments(pairs, anchors=None):
    """Drop stale fragments only beside a newly accepted same-track decode.

    Repetition can be real speech.  Arbitrary pre-existing rows therefore must
    never act as deletion evidence; only prompt-free rows accepted by the
    current repair pass may be supplied as anchors.
    """
    anchors = list(anchors or [])
    if not anchors:
        return list(pairs)
    remove = set()
    for index, (segment, row) in enumerate(pairs):
        if any(row is anchor_row for _anchor, anchor_row in anchors):
            continue
        duration = max(0.0, float(segment[1]) - float(segment[0]))
        visible = _visible_text_length(segment[2])
        loop_fragment = _loop_suspected_text(segment[2])
        single_fragment = visible == 1
        if not loop_fragment and not single_fragment:
            continue
        for other, other_row in anchors:
            if not _same_audio_track(row, other_row):
                continue
            if _loop_suspected_text(other[2]) or _visible_text_length(other[2]) < 2:
                continue
            overlap = _span_overlap(segment, other)
            if loop_fragment and overlap >= max(0.08, duration * 0.25):
                remove.add(index)
                break
            if (single_fragment and float(segment[0]) >= float(other[0]) - 0.02
                    and float(segment[1]) <= float(other[1]) + 0.02):
                remove.add(index)
                break
    return [pair for index, pair in enumerate(pairs) if index not in remove]


def _segment_coverage_ratio(target, candidates):
    """Return the union coverage of target by candidate time spans."""
    start, end = float(target[0]), float(target[1])
    duration = max(0.0, end - start)
    if duration <= 1e-6:
        return 0.0
    spans = sorted(
        (max(start, float(segment[0])), min(end, float(segment[1])))
        for segment, _row in candidates
        if _span_overlap(target, segment) > 0.0)
    merged = []
    for left, right in spans:
        if right <= left:
            continue
        if merged and left <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return min(1.0, sum(right - left for left, right in merged) / duration)


def _offset_segment_evidence(row, offset):
    """Move chunk-local ASR evidence back to source timeline coordinates."""
    shifted = dict(row or {})
    for key in ("start", "end", "original_asr_segment_start",
                "original_asr_segment_end"):
        value = _float_or_none(shifted.get(key))
        if value is not None:
            shifted[key] = value + float(offset)
    shifted_words = []
    for word in shifted.get("words") or []:
        if not isinstance(word, dict):
            continue
        item = dict(word)
        for key in ("start", "end"):
            value = _float_or_none(item.get(key))
            if value is not None:
                item[key] = value + float(offset)
        shifted_words.append(item)
    shifted["words"] = shifted_words
    return shifted


def _suspected_parent_ranges(evidence, source_audio_track=None):
    """Return emitted child spans that need a local non-batched re-listen."""
    wanted_tracks = set(_audio_track_ids(source_audio_track))
    grouped = {}
    for index, row in enumerate(evidence or []):
        if not _has_timing_alignment_warning(row):
            continue
        row_tracks = set(_audio_track_ids(
            row.get("source_audio_tracks") or row.get("source_audio_track")))
        if wanted_tracks and row_tracks and not (wanted_tracks & row_tracks):
            continue
        start = _float_or_none(row.get("start"))
        end = _float_or_none(row.get("end"))
        if start is None or end is None or end <= start:
            continue
        key = (round(start, 6), round(end, 6))
        grouped.setdefault(key, []).append(index)
    return [(start, end, tuple(indexes))
            for (start, end), indexes in sorted(grouped.items())]


def _merge_alignment_windows(windows, gap=0.20):
    """Merge nearby acoustic/evidence windows without hiding long gaps."""
    rows = sorted((float(a), float(b)) for a, b in windows if b > a)
    merged = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + float(gap):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _alignment_repair_expected_windows(audio, parent_start, parent_end,
                                       parent_rows, sr=16000,
                                       max_active_window=12.0,
                                       audio_offset=0.0):
    """Return source-time windows that a replacement decode must cover.

    Reliable old word timestamps are evidence that speech existed at a precise
    location.  Energy runs additionally protect speech that the corrupt parent
    alignment failed to represent at word level.  Long continuously-active
    spans are tiled so one decoded token cannot make an otherwise missed
    40-second chunk look complete.  True silence produces no energy run and is
    therefore never treated as a missing subtitle window.
    """
    parent_start, parent_end = float(parent_start), float(parent_end)
    audio_offset = float(audio_offset or 0.0)
    samples = audio[
        max(0, int((parent_start - audio_offset) * sr)):
        max(0, int((parent_end - audio_offset) * sr))]
    expected = []
    vad_windows, _vad_warning = silero_speech_windows(samples, sr=sr)
    # If Silero is unavailable, reliable old word anchors below remain usable,
    # but RMS activity must never become a hard export requirement.
    for local_start, local_end in (vad_windows or []):
        start = parent_start + float(local_start)
        end = min(parent_end, parent_start + float(local_end))
        cursor = start
        while cursor < end - 1e-6:
            tile_end = min(end, cursor + max(2.0, float(max_active_window)))
            expected.append((cursor, tile_end))
            cursor = tile_end

    # Word timestamps lasting several seconds are the defect being repaired;
    # only bounded word spans are precise enough to act as mandatory anchors.
    word_windows = []
    for row in parent_rows or []:
        for word in row.get("words") or []:
            if not isinstance(word, dict) or not _visible_text_length(
                    word.get("text", word.get("word", ""))):
                continue
            start = _float_or_none(word.get("start"))
            end = _float_or_none(word.get("end"))
            if (start is None or end is None or end < start
                    or end - start > 2.5):
                continue
            word_windows.append((max(parent_start, start - 0.60),
                                 min(parent_end, end + 0.60)))
    expected.extend(_merge_alignment_windows(word_windows, gap=0.40))
    # Keep adjacent acoustic tiles separate.  Merging them here would let one
    # short decoded token satisfy an entire continuously-active parent span.
    return sorted(expected)


def _alignment_repair_coverage_issue(repaired_pairs, expected_windows,
                                     parent_rows):
    """Return a reason when a sane-looking replacement still loses content."""
    repaired_spans = [(float(segment[0]), float(segment[1]))
                      for segment, _row in repaired_pairs
                      if float(segment[1]) > float(segment[0])]
    for start, end in expected_windows or []:
        if not _alignment_window_covered((start, end), repaired_spans):
            return "有声/原语音窗口 %.2f~%.2f 秒没有识别结果" % (start, end)

    old_text = "".join(str(row.get("text_original") or row.get("text") or "")
                       for row in parent_rows or [])
    new_text = "".join(str(segment[2] or "") for segment, _row in repaired_pairs)
    old_visible = _visible_text_length(old_text)
    new_visible = _visible_text_length(new_text)
    if old_visible >= 4 and new_visible < max(1, int(np.ceil(old_visible * 0.50))):
        return "重听文字覆盖不足（原证据 %d 字，新识别 %d 字）" % (
            old_visible, new_visible)
    return ""


def _alignment_overlap_required(start, end):
    """Meaningful overlap needed to count one acoustic window as covered."""
    duration = max(0.0, float(end) - float(start))
    return min(0.50, max(0.12, duration * 0.25))


def _alignment_window_covered(window, spans):
    start, end = float(window[0]), float(window[1])
    pieces = []
    for left, right in spans or []:
        left, right = max(start, float(left)), min(end, float(right))
        if right > left:
            pieces.append((left, right))
    covered = sum(right - left for left, right in
                  _merge_alignment_windows(pieces, gap=0.0))
    return covered >= _alignment_overlap_required(start, end)


def _tile_alignment_windows(windows, max_window=12.0):
    tiled = []
    for start, end in _merge_alignment_windows(windows, gap=0.20):
        cursor = float(start)
        while cursor < float(end) - 1e-6:
            tile_end = min(float(end), cursor + max(2.0, float(max_window)))
            if tile_end - cursor >= 0.30:
                tiled.append((cursor, tile_end))
            cursor = tile_end
    return tiled


def _reliable_alignment_spans(text_segs, evidence):
    spans = []
    for segment, row in zip(text_segs or [], evidence or []):
        try:
            start, end, text = float(segment[0]), float(segment[1]), str(segment[2])
        except (IndexError, TypeError, ValueError):
            continue
        good_words = []
        for word in row.get("words") or [] if isinstance(row, dict) else []:
            if not isinstance(word, dict):
                continue
            a = _float_or_none(word.get("start"))
            b = _float_or_none(word.get("end"))
            chars = _visible_text_length(word.get("text", word.get("word", "")))
            if (a is None or b is None or b <= a or not chars
                    or b - a > max(2.5, chars * 1.5)):
                continue
            good_words.append((a, b))
        if good_words:
            spans.extend(good_words)
            for coverage in row.get("coverage_spans") or []:
                try:
                    a, b = float(coverage[0]), float(coverage[1])
                except (IndexError, TypeError, ValueError):
                    continue
                if b > a:
                    spans.append((a, b))
            continue
        chars = _visible_text_length(text)
        duration = end - start
        if (chars and duration > 0
                and duration <= max(6.0, chars * 2.5)
                and not _has_timing_alignment_warning(row)):
            spans.append((start, end))
    return _merge_alignment_windows(spans, gap=0.15)


def _group_uncovered_windows(windows, max_group=40.0, gap=0.80):
    groups = []
    for window in sorted(windows or []):
        if (groups and window[0] <= groups[-1][-1][1] + float(gap)
                and window[1] - groups[-1][0][0] <= float(max_group)):
            groups[-1].append(window)
        else:
            groups.append([window])
    return groups


def _subtitle_text_key(value):
    return re.sub(r"[\s\W_]+", "", str(value or ""), flags=re.UNICODE).lower()


def _segment_distance(left, right):
    a, b = float(left[0]), float(left[1])
    c, d = float(right[0]), float(right[1])
    if b < c:
        return c - b
    if d < a:
        return a - d
    return 0.0


def _merge_local_boundary(existing_pairs, segment, row, max_distance=1.50):
    """Merge a locally re-heard prefix/suffix into its existing full sentence."""
    new_key = _subtitle_text_key(segment[2])
    if len(new_key) < 2:
        return False
    new_tracks = set(_audio_track_ids(
        row.get("source_audio_tracks") or row.get("source_audio_track")))
    best = None
    for index, (old_segment, old_row) in enumerate(existing_pairs):
        old_key = _subtitle_text_key(old_segment[2])
        if len(old_key) < 2 or not (new_key in old_key or old_key in new_key):
            continue
        old_tracks = set(_audio_track_ids(
            old_row.get("source_audio_tracks") or old_row.get("source_audio_track")))
        if new_tracks and old_tracks and not (new_tracks & old_tracks):
            continue
        distance = _segment_distance(segment, old_segment)
        if distance > float(max_distance):
            continue
        key = (distance, abs(len(old_key) - len(new_key)))
        if best is None or key < best[0]:
            best = (key, index)
    if best is None:
        return False
    index = best[1]
    old_segment, old_row = existing_pairs[index]
    merged_start = min(float(old_segment[0]), float(segment[0]))
    merged_end = max(float(old_segment[1]), float(segment[1]))
    merged_row = dict(old_row)
    merged_row["start"], merged_row["end"] = merged_start, merged_end
    coverage = list(merged_row.get("coverage_spans") or [])
    coverage.append([float(segment[0]), float(segment[1])])
    merged_row["coverage_spans"] = coverage
    merged_row["timing_source"] = "local-overlap-merge"
    existing_pairs[index] = (
        (merged_start, merged_end, str(old_segment[2])), merged_row)
    return True


def repair_uncovered_speech(text_segs, audio, speech_windows,
                            model_size: str = MODEL_ACCURATE,
                            models_dir: str | None = None, on_status=None,
                            should_stop=None, evidence_out=None,
                            source_audio_track=None, beam_size=None,
                            initial_prompt=None, hotwords=None,
                            blockers_out=None, diagnostics_out=None):
    """Re-listen voiced windows that initial ASR covered with no real text.

    Unlike parent-timestamp repair, this also works when ASR emitted no segment
    at all. Unrecovered voiced windows are persisted as global export blockers.
    """
    blockers_out = blockers_out if blockers_out is not None else []
    diagnostics_out = diagnostics_out if diagnostics_out is not None else []
    if audio is None or getattr(audio, "size", 0) == 0:
        return text_segs, blockers_out
    segments, rows, structural = sort_segment_evidence_pairs(
        text_segs, evidence_out or [])
    if structural:
        blockers_out.extend(structural)
        return text_segs, blockers_out

    sr = 16000
    duration = float(audio.size) / sr
    expected = _tile_alignment_windows([
        (max(0.0, min(duration, float(a))),
         max(0.0, min(duration, float(b))))
        for a, b in (speech_windows or []) if float(b) > float(a)
    ])
    uncovered = [window for window in expected if not _alignment_window_covered(
        window, _reliable_alignment_spans(segments, rows))]
    if not uncovered:
        return text_segs, blockers_out
    if on_status:
        on_status("字幕覆盖复核：%d 个有声窗口没有可靠台词，局部重听…"
                  % len(uncovered))

    existing_pairs = list(zip(segments, rows))
    new_pairs = []
    for group in _group_uncovered_windows(uncovered):
        if should_stop and should_stop():
            raise IndexCancelled("用户停止")
        group_duration = max(0.0, float(group[-1][1]) - float(group[0][0]))
        # Sub-second VAD windows often contain only the first syllable of a
        # longer sentence.  A 0.6 s pad clipped the following word and made the
        # local decoder return nothing, leaving a real caption almost one
        # second late.  Give only those very short windows enough sentence
        # context; longer uncovered regions keep the tighter repair scope.
        context_pad = 1.50 if group_duration < 0.75 else 0.60
        decode_start = max(0.0, group[0][0] - context_pad)
        decode_end = min(duration, group[-1][1] + context_pad)
        samples = audio[int(decode_start * sr):int(decode_end * sr)]
        local_evidence = []
        try:
            local_segments = transcribe(
                samples, model_size=model_size, models_dir=models_dir,
                on_status=None, initial_prompt="", hotwords="",
                beam_size=beam_size, batch_size=0,
                should_stop=should_stop, evidence_out=local_evidence,
                source_audio_track=source_audio_track)
        except IndexCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if on_status:
                on_status("字幕覆盖复核失败 %.1f~%.1f 秒：%s" % (
                    decode_start, decode_end, str(exc)[:80]))
            continue
        if len(local_segments) != len(local_evidence):
            continue
        for segment, row in zip(local_segments, local_evidence):
            try:
                local_start, local_end, text = segment
                source_start = float(local_start) + decode_start
                source_end = float(local_end) + decode_start
            except (TypeError, ValueError):
                continue
            midpoint = (source_start + source_end) / 2.0
            overlaps = [max(0.0, min(b, source_end) - max(a, source_start))
                        for a, b in group]
            strict_match = any(a <= midpoint <= b for a, b in group) or any(
                value >= 0.10 for value in overlaps)
            near_match = min(
                min(abs(source_end - a), abs(source_start - b))
                for a, b in group) <= 0.75
            if not strict_match and not near_match:
                continue
            shifted = _offset_segment_evidence(row, decode_start)
            shifted["start"], shifted["end"] = source_start, source_end
            shifted["text"] = str(text or "")
            if not _local_asr_is_reliable(shifted, text):
                continue
            if _merge_local_boundary(
                    existing_pairs,
                    (source_start, source_end, str(text or "")), shifted):
                continue
            if not strict_match:
                continue
            new_pairs.append(
                ((source_start, source_end, str(text or "")), shifted))

    seen = set()
    combined = []
    for segment, row in existing_pairs + new_pairs:
        key = (round(float(segment[0]), 3), round(float(segment[1]), 3),
               str(segment[2]), tuple(_audio_track_ids(
                   row.get("source_audio_tracks") or row.get("source_audio_track"))))
        if key in seen:
            continue
        seen.add(key)
        combined.append((segment, row))
    combined.sort(key=lambda pair: (
        float(pair[0][0]), float(pair[0][1]),
        tuple(_audio_track_ids(pair[1].get("source_audio_tracks") or
                               pair[1].get("source_audio_track")))))
    segments = [pair[0] for pair in combined]
    rows = [pair[1] for pair in combined]
    reliable = _reliable_alignment_spans(segments, rows)
    remaining = [window for window in expected
                 if not _alignment_window_covered(window, reliable)]
    tracks = _audio_track_ids(source_audio_track)
    blocked_count = 0
    warning_count = 0
    for group in _group_uncovered_windows(remaining):
        start, end = group[0][0], group[-1][1]
        if end - start >= 1.50:
            blockers_out.append({
                "reason": "uncovered_speech_window",
                "start": round(start, 6), "end": round(end, 6),
                "source_audio_track": tracks[0] if len(tracks) == 1 else None,
                "source_audio_tracks": tracks,
            })
            blocked_count += 1
        else:
            diagnostics_out.append({
                "reason": "unresolved_short_speech_window",
                "start": round(start, 6), "end": round(end, 6),
                "source_audio_track": tracks[0] if len(tracks) == 1 else None,
                "source_audio_tracks": tracks,
            })
            warning_count += 1
    text_segs[:] = segments
    if evidence_out is not None:
        evidence_out[:] = rows
    if on_status:
        if blocked_count:
            on_status("字幕覆盖复核未通过：仍有 %d 组连续人声无台词，已阻止字幕导出"
                      % blocked_count)
        elif warning_count:
            on_status("字幕覆盖复核完成：%d 个不足 1.5 秒的未决短窗仅记录诊断"
                      % warning_count)
        elif new_pairs:
            on_status("字幕覆盖复核完成：补回 %d 句" % len(new_pairs))
    return text_segs, blockers_out


def repair_uncovered_human_speech(text_segs, audio, rms_windows=(),
                                  model_size: str = MODEL_ACCURATE,
                                  models_dir: str | None = None,
                                  on_status=None, should_stop=None,
                                  evidence_out=None, source_audio_track=None,
                                  beam_size=None, initial_prompt=None,
                                  hotwords=None, blockers_out=None,
                                  diagnostics_out=None):
    """Gate missing-speech repair on Silero VAD, never on RMS alone."""
    blockers_out = blockers_out if blockers_out is not None else []
    diagnostics_out = diagnostics_out if diagnostics_out is not None else []
    vad_windows, warning = silero_speech_windows(audio, sr=16000)
    tracks = _audio_track_ids(source_audio_track)
    diagnostic = {
        "source_audio_track": tracks[0] if len(tracks) == 1 else None,
        "source_audio_tracks": tracks,
        "rms_active_window_count": len(rms_windows or []),
        "vad_available": vad_windows is not None,
        "human_speech_window_count": len(vad_windows or []),
        "warning": warning,
    }
    diagnostics_out.append(diagnostic)
    if vad_windows is None:
        if on_status:
            on_status("Silero 人声 VAD 不可用，RMS 有声段仅记录诊断，"
                      "不会触发字幕重听或导出阻断：%s" % warning)
        return text_segs, blockers_out
    if on_status:
        on_status("人声 VAD 确认 %d 段（RMS 活跃段 %d，仅前者参与字幕覆盖检查）"
                  % (len(vad_windows), len(rms_windows or [])))
    return repair_uncovered_speech(
        text_segs, audio, vad_windows, model_size=model_size,
        models_dir=models_dir, on_status=on_status,
        should_stop=should_stop, evidence_out=evidence_out,
        source_audio_track=source_audio_track, beam_size=beam_size,
        initial_prompt=initial_prompt, hotwords=hotwords,
        blockers_out=blockers_out, diagnostics_out=diagnostics_out)


def repair_suspected_alignment(text_segs, audio,
                               model_size: str = MODEL_ACCURATE,
                               models_dir: str | None = None, on_status=None,
                               should_stop=None, evidence_out=None,
                               source_audio_track=None, beam_size=None,
                               initial_prompt=None, hotwords=None,
                               chunk_seconds: float = 40.0,
                               overlap: float = 0.8, blockers_out=None,
                               audio_offset: float = 0.0,
                               target_indexes=None,
                               use_parent_context: bool = False):
    """Re-transcribe only emitted child rows with impossible local timing.

    A restored faster-whisper parent may legitimately span distant VAD speech
    islands.  Repairing that whole parent used to replace or flag every valid
    sibling when one child was bad.  This pass therefore decodes a small window
    around each locally impossible row and changes only that row.
    """
    if (evidence_out is None or audio is None
            or getattr(audio, "size", 0) == 0):
        return text_segs
    if len(text_segs) != len(evidence_out):
        if blockers_out is not None:
            blockers_out.append({
                "reason": "segment_evidence_count_mismatch",
                "segment_count": len(text_segs),
                "evidence_count": len(evidence_out),
            })
        if on_status:
            on_status("字幕时间复核跳过：台词与原始时间证据数量不一致")
        return text_segs
    if not text_segs:
        return text_segs

    sr = 16000
    audio_offset = max(0.0, float(audio_offset or 0.0))
    duration = audio_offset + float(audio.size) / sr
    targets = _suspected_parent_ranges(evidence_out, source_audio_track)
    if target_indexes is not None:
        wanted_indexes = {int(value) for value in target_indexes}
        targets = [target for target in targets
                   if wanted_indexes.intersection(target[2])]
    if not targets:
        return text_segs

    pairs = [((float(a), float(b), str(t or "")), dict(row))
             for (a, b, t), row in zip(text_segs, evidence_out)]
    chunk_seconds = max(8.0, float(chunk_seconds))
    overlap = max(0.0, min(float(overlap), chunk_seconds / 4.0))
    step = max(4.0, chunk_seconds - overlap)

    for target_start, target_end, _original_indexes in targets:
        if should_stop and should_stop():
            raise IndexCancelled("用户停止")
        target_start = max(0.0, min(duration, float(target_start)))
        target_end = max(target_start, min(duration, float(target_end)))
        if target_end - target_start < 0.2:
            continue
        target_positions = []
        for position, (segment, row) in enumerate(pairs):
            row_start = _float_or_none(row.get("start"))
            row_end = _float_or_none(row.get("end"))
            if (row_start is not None and row_end is not None
                    and abs(row_start - target_start) < 0.01
                    and abs(row_end - target_end) < 0.01
                    and _has_timing_alignment_warning(row)):
                target_positions.append(position)
        if not target_positions:
            continue
        target_rows = [pairs[position][1] for position in target_positions]
        target_is_definite_hallucination = bool(target_rows) and all(
            _definite_asr_hallucination(
                row.get("text_original") or row.get("text") or "")
            or _definite_timing_hallucination(
                row, row.get("text_original") or row.get("text") or "")
            or _low_confidence_template_hallucination(
                row, row.get("text_original") or row.get("text") or "")
            for row in target_rows)
        decode_start = max(audio_offset, target_start - 1.20)
        decode_end = min(duration, target_end + 1.20)
        if use_parent_context:
            parent_starts = [_float_or_none(
                row.get("original_asr_segment_start")) for row in target_rows]
            parent_ends = [_float_or_none(
                row.get("original_asr_segment_end")) for row in target_rows]
            parent_starts = [value for value in parent_starts if value is not None]
            parent_ends = [value for value in parent_ends if value is not None]
            context_start = min(parent_starts) if parent_starts else target_start
            context_end = max(parent_ends) if parent_ends else target_end
            # The original Whisper parent gives useful language context, but a
            # corrupt historic parent must never turn a local repair into
            # another full-recording decode.
            if context_end - context_start > chunk_seconds:
                half = chunk_seconds / 2.0
                context_start = max(context_start, target_start - half)
                context_end = min(context_end, target_end + half)
            decode_start = max(audio_offset, min(decode_start,
                                                 context_start - 0.20))
            decode_end = min(duration, max(decode_end, context_end + 0.20))
        if on_status:
            on_status("字幕时间异常 %.1f~%.1f 秒，分块复听…"
                      % (target_start, target_end))

        # A known Whisper template hallucination is not evidence that any of
        # its words existed.  Requiring the replacement to cover the old text
        # or its word anchors can preserve the exact hallucination we are
        # trying to remove when a nearby real utterance is decoded.  For this
        # special case only, the prompt-free local decode is the authority.
        expected_windows = ([] if target_is_definite_hallucination else
                            _alignment_repair_expected_windows(
                                audio, target_start, target_end, target_rows,
                                sr=sr, audio_offset=audio_offset))
        repaired_pairs = []
        cursor = decode_start
        failed = False
        decode_error = False
        failure_reason = ""
        while cursor < decode_end - 1e-6:
            chunk_end = min(decode_end, cursor + chunk_seconds)
            samples = audio[
                max(0, int((cursor - audio_offset) * sr)):
                max(0, int((chunk_end - audio_offset) * sr))]
            local_evidence = []
            try:
                local_segments = transcribe(
                    samples, model_size=model_size, models_dir=models_dir,
                    on_status=None, initial_prompt="", hotwords="",
                    beam_size=beam_size, batch_size=0,
                    should_stop=should_stop, evidence_out=local_evidence,
                    source_audio_track=source_audio_track)
            except IndexCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                failed = True
                decode_error = True
                if on_status:
                    on_status("字幕时间复核失败，保留并标记原字幕：%s"
                              % str(exc)[:80])
                break
            if len(local_segments) != len(local_evidence):
                failed = True
                decode_error = True
                failure_reason = "台词与时间证据数量不一致"
                break

            # Overlapping chunks are accepted by midpoint ownership.  This
            # keeps a boundary utterance exactly once without text matching.
            left_guard = cursor + (overlap / 2.0 if cursor > decode_start else 0.0)
            right_guard = chunk_end - (
                overlap / 2.0 if chunk_end < decode_end else 0.0)
            for segment, row in zip(local_segments, local_evidence):
                local_start, local_end, text = segment
                source_start = float(local_start) + cursor
                source_end = float(local_end) + cursor
                midpoint = (source_start + source_end) / 2.0
                if midpoint < left_guard or midpoint >= right_guard:
                    continue
                if (midpoint < target_start or midpoint > target_end) and max(
                        0.0, min(target_end, source_end)
                        - max(target_start, source_start)) < 0.10:
                    continue
                shifted = _offset_segment_evidence(row, cursor)
                shifted["start"], shifted["end"] = source_start, source_end
                shifted["text"] = str(text or "")
                if not _local_asr_is_reliable(shifted, text):
                    continue
                repaired_pairs.append(
                    ((source_start, source_end, str(text or "")), shifted))
            if chunk_end >= decode_end:
                break
            cursor += step

        bad_new_timing = any(_has_timing_alignment_warning(row)
                             for _segment, row in repaired_pairs)
        coverage_issue = ("" if target_is_definite_hallucination else
                          _alignment_repair_coverage_issue(
                              repaired_pairs, expected_windows, target_rows))
        if coverage_issue:
            failed = True
            failure_reason = coverage_issue
        if failed or not repaired_pairs or bad_new_timing:
            if target_is_definite_hallucination and not decode_error:
                target_set = set(target_positions)
                pairs = [pair for position, pair in enumerate(pairs)
                         if position not in target_set]
                if on_status:
                    on_status("字幕时间复核：删除局部复听无法确认的 Whisper 模板幻觉")
                continue
            for position in target_positions:
                row = pairs[position][1]
                flags = list(row.get("asr_flags") or [])
                if "alignment_repair_failed" not in flags:
                    flags.append("alignment_repair_failed")
                row["asr_flags"] = flags
            if on_status:
                detail = failure_reason or (
                    "新时间仍异常" if bad_new_timing else "没有得到可靠台词")
                on_status("字幕时间复核未通过：%s；已保留并标记原字幕" % detail)
            continue

        target_set = set(target_positions)
        kept = [pair for position, pair in enumerate(pairs)
                if position not in target_set]
        pairs = sorted(kept + repaired_pairs,
                       key=lambda pair: (pair[0][0], pair[0][1]))
        if on_status:
            on_status("字幕时间复核完成：异常父段替换为 %d 句" % len(repaired_pairs))

    text_segs[:] = [segment for segment, _row in pairs]
    evidence_out[:] = [row for _segment, row in pairs]
    return text_segs


def _atomic_write_bytes(path, payload):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".index-repair-", suffix=".tmp",
                                     dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _atomic_write_index_json(path, data):
    body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    _atomic_write_bytes(path, body)


def _delivery_repair_targets(data):
    """Return locally repairable evidence rows, latest source time first."""
    targets = []
    for index, row in enumerate(data.get("segment_evidence") or []):
        if not isinstance(row, dict) or not _has_timing_alignment_warning(row):
            continue
        start = _float_or_none(row.get("start"))
        end = _float_or_none(row.get("end"))
        if start is None or end is None or end <= start:
            continue
        parent_start = _float_or_none(row.get("original_asr_segment_start"))
        parent_end = _float_or_none(row.get("original_asr_segment_end"))
        if (parent_start is None or parent_end is None
                or parent_end <= parent_start):
            parent_start, parent_end = start, end
        # Corrupt legacy parents can span minutes.  Preserve ordinary Whisper
        # parent context but never let delivery repair become a full-file pass.
        if parent_end - parent_start > 44.0:
            parent_start = max(parent_start, start - 20.0)
            parent_end = min(parent_end, end + 20.0)
        tracks = _audio_track_ids(
            row.get("source_audio_tracks") or row.get("source_audio_track"))
        if not tracks:
            tracks = _audio_track_ids(data.get("audio_stream_used"))
        flags = {str(value) for value in (row.get("asr_flags") or [])}
        stretched_single = (
            _visible_text_length(row.get("text_original") or row.get("text")) <= 1
            and end - start >= 2.5
            and "word_duration_outlier" in flags)
        overlaps_reliable_neighbor = False
        if stretched_single:
            evidence = data.get("segment_evidence") or []
            for neighbor_index in (index - 1, index + 1):
                if not (0 <= neighbor_index < len(evidence)):
                    continue
                neighbor = evidence[neighbor_index]
                if (not isinstance(neighbor, dict)
                        or _has_timing_alignment_warning(neighbor)
                        or not _same_audio_track(row, neighbor)):
                    continue
                neighbor_start = _float_or_none(neighbor.get("start"))
                neighbor_end = _float_or_none(neighbor.get("end"))
                if (neighbor_start is not None and neighbor_end is not None
                        and min(end, neighbor_end) - max(start, neighbor_start) > 0.08):
                    overlaps_reliable_neighbor = True
                    break
        targets.append({
            "index": index,
            "segment_id": str(row.get("segment_id") or ""),
            "start": start, "end": end,
            "window_start": max(0.0, parent_start - 1.40),
            "window_end": parent_end + 1.40,
            "tracks": tracks,
            "allow_drop_stretched_single": overlaps_reliable_neighbor,
        })
    return sorted(targets, key=lambda item: (item["start"], item["index"]),
                  reverse=True)


def _find_delivery_target(evidence, target):
    segment_id = target.get("segment_id")
    if segment_id:
        for index, row in enumerate(evidence or []):
            if (isinstance(row, dict)
                    and str(row.get("segment_id") or "") == segment_id
                    and _has_timing_alignment_warning(row)):
                return index
    for index, row in enumerate(evidence or []):
        if not isinstance(row, dict) or not _has_timing_alignment_warning(row):
            continue
        start = _float_or_none(row.get("start"))
        end = _float_or_none(row.get("end"))
        if (start is not None and end is not None
                and abs(start - target["start"]) <= 0.02
                and abs(end - target["end"]) <= 0.02):
            return index
    return None


def _dedupe_delivery_relisten_pairs(text_segs, evidence):
    """Remove only near-identical same-track rows created by local re-listen.

    A repair window can decode the reliable sentence immediately after the bad
    child and return it as the replacement.  Keep the pre-existing row (it has
    a stable ``segment_id``) when both rows carry the same text and cover at
    least 75% of the shorter time span.  Different text and different tracks
    are never merged here.
    """
    pairs = [((float(segment[0]), float(segment[1]), str(segment[2] or "")),
              dict(row)) for segment, row in zip(text_segs or [], evidence or [])]
    pairs.sort(key=lambda item: (item[0][0], item[0][1]))
    kept = []
    for pair in pairs:
        segment, row = pair
        text_key = _subtitle_text_key(segment[2])
        duplicate_index = None
        for index, (other, other_row) in enumerate(kept):
            if not _same_audio_track(row, other_row):
                continue
            shorter = min(float(segment[1]) - float(segment[0]),
                          float(other[1]) - float(other[0]))
            if shorter <= 0.0:
                continue
            overlap_ratio = _span_overlap(segment, other) / shorter
            current_stable = bool(str(row.get("segment_id") or ""))
            other_stable = bool(str(other_row.get("segment_id") or ""))
            same_text = bool(text_key) and text_key == _subtitle_text_key(other[2])
            # A no-ID row was produced by this prompt-free repair call.  When
            # it covers the same acoustic event as one stable old row, it is a
            # corrected re-decode (real case: 有星辉 vs old 有心灰), not a
            # second simultaneous utterance.  Otherwise require exact text.
            authoritative_relisten = (current_stable != other_stable
                                      and overlap_ratio >= 0.85)
            if not authoritative_relisten and not (same_text and overlap_ratio >= 0.75):
                continue
            duplicate_index = index
            break
        if duplicate_index is None:
            kept.append(pair)
            continue
        other, other_row = kept[duplicate_index]
        current_stable = bool(str(row.get("segment_id") or ""))
        other_stable = bool(str(other_row.get("segment_id") or ""))
        # Prefer the fresh prompt-free row when it conflicts with one stable
        # old row; for exact duplicates with equal provenance, keep first.
        if not current_stable and other_stable:
            kept[duplicate_index] = pair
    return ([segment for segment, _row in kept],
            [row for _segment, row in kept])


def _refresh_delivery_repair_metadata(data, old_segment_count):
    """Invalidate or rebuild fields whose row identity followed old ASR rows."""
    segments = data.get("segments") or []
    evidence = data.get("segment_evidence") or []
    data["has_speech"] = bool(segments)
    data["segments_refined"] = []
    data["segments_refined_version"] = None
    data["segments_refined_metadata"] = {}

    if len(segments) != int(old_segment_count):
        # These fields are positional.  Reusing them after a row was inserted
        # or removed silently assigns another sentence's meaning/score.
        data["semantic"] = []
        data["scores"] = []
        data["seg_feat"] = []
        data["complete"] = {}
        data.pop("neutral", None)

    for card in data.get("cards") or []:
        if not isinstance(card, dict):
            continue
        start = _float_or_none(card.get("start"))
        end = _float_or_none(card.get("end"))
        if start is None or end is None or end <= start:
            continue
        positions = [index for index, segment in enumerate(segments)
                     if float(segment[1]) > start and float(segment[0]) < end]
        card["text"] = " ".join(str(segments[index][2] or "")
                                for index in positions).strip()
        rows = [evidence[index] for index in positions
                if index < len(evidence) and isinstance(evidence[index], dict)]
        words = [word for row in rows for word in (row.get("words") or [])
                 if isinstance(word, dict)]
        word_starts = [_float_or_none(word.get("start")) for word in words]
        word_ends = [_float_or_none(word.get("end")) for word in words]
        word_starts = [value for value in word_starts if value is not None]
        word_ends = [value for value in word_ends if value is not None]
        tracks = sorted({track for row in rows for track in _audio_track_ids(
            row.get("source_audio_tracks") or row.get("source_audio_track"))})
        card["speech_evidence"] = {
            "segment_ids": [str(row.get("segment_id") or "") for row in rows],
            "word_start": min(word_starts) if word_starts else None,
            "word_end": max(word_ends) if word_ends else None,
            "source_audio_tracks": tracks,
        }

    if evidence:
        try:
            import neutral_index
            neutral = neutral_index.build(data)
            if neutral:
                data["neutral"] = neutral
            else:
                data.pop("neutral", None)
        except Exception:  # noqa: BLE001 - neutral text is non-blocking metadata
            data.pop("neutral", None)


_LOW_CONFIDENCE_RELISTEN_VERSION = 1
_LOW_CONFIDENCE_WORD_HARD = 0.08
_LOW_CONFIDENCE_WORD_SINGLE = 0.18
_LOW_CONFIDENCE_RELISTEN_LIMIT = 12


def _low_confidence_review_signature(data, model_size, beam_size):
    """Fingerprint only acoustic rows relevant to the bounded re-listen pass."""
    evidence = []
    for row in data.get("segment_evidence") or []:
        if not isinstance(row, dict):
            evidence.append(None)
            continue
        evidence.append({
            "start": row.get("start"), "end": row.get("end"),
            "text": row.get("text_original") or row.get("text"),
            "tracks": row.get("source_audio_tracks") or
                      row.get("source_audio_track"),
            "words": [{
                "text": word.get("text", word.get("word", "")),
                "start": word.get("start"), "end": word.get("end"),
                "probability": word.get("probability"),
            } for word in (row.get("words") or []) if isinstance(word, dict)],
        })
    body = {
        "version": _LOW_CONFIDENCE_RELISTEN_VERSION,
        "model": str(model_size or ""), "beam_size": int(beam_size or 0),
        "segments": data.get("segments") or [], "evidence": evidence,
    }
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _low_confidence_relisten_targets(data, max_targets=None):
    """Return a small set of high-risk word windows, never every uncertain word.

    Whisper emits moderately low probabilities for ordinary conversational
    particles, so using a broad threshold would make delivery re-run most of a
    long recording.  This pass targets only extremely unlikely words, or an
    unlikely single-character token at a sentence boundary.  A neighboring
    same-track row is included only when the suspect token touches that edge;
    this lets a split such as ``我`` / ``们这...`` be re-heard as one phrase.
    """
    rows = data.get("segment_evidence") or []
    raw = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or _has_timing_alignment_warning(row):
            continue
        words = [word for word in (row.get("words") or [])
                 if isinstance(word, dict)]
        risky = []
        for word_index, word in enumerate(words):
            probability = _float_or_none(word.get("probability"))
            visible = _visible_text_length(
                word.get("text", word.get("word", "")))
            if probability is None or not visible:
                continue
            if (probability < _LOW_CONFIDENCE_WORD_HARD or
                    (visible == 1 and
                     probability < _LOW_CONFIDENCE_WORD_SINGLE)):
                risky.append((word_index, probability, visible))
        if not risky:
            continue
        start = _float_or_none(row.get("start"))
        end = _float_or_none(row.get("end"))
        if start is None or end is None or end <= start:
            continue
        indexes = {index}
        low_at_start = any(position == 0 for position, _p, _v in risky)
        low_at_end = any(position == len(words) - 1
                         for position, _p, _v in risky)
        if low_at_start and index > 0:
            previous = rows[index - 1]
            previous_end = (_float_or_none(previous.get("end"))
                            if isinstance(previous, dict) else None)
            if (isinstance(previous, dict) and
                    _same_audio_track(row, previous) and
                    previous_end is not None and
                    0.0 <= start - previous_end <= 2.0):
                indexes.add(index - 1)
        if low_at_end and index + 1 < len(rows):
            following = rows[index + 1]
            following_start = (_float_or_none(following.get("start"))
                               if isinstance(following, dict) else None)
            if (isinstance(following, dict) and
                    _same_audio_track(row, following) and
                    following_start is not None and
                    0.0 <= following_start - end <= 2.0):
                indexes.add(index + 1)
        indexes = sorted(indexes)
        selected = [rows[position] for position in indexes]
        focus_start = min(float(item["start"]) for item in selected)
        focus_end = max(float(item["end"]) for item in selected)
        tracks = _audio_track_ids(
            row.get("source_audio_tracks") or row.get("source_audio_track"))
        if not tracks:
            tracks = _audio_track_ids(data.get("audio_stream_used"))
        raw.append({
            "indexes": indexes,
            "segment_ids": [str(rows[position].get("segment_id") or "")
                            for position in indexes],
            "focus_start": focus_start, "focus_end": focus_end,
            "window_start": max(0.0, focus_start - 1.0),
            "window_end": focus_end + 1.0,
            "tracks": tracks,
            "min_probability": min(item[1] for item in risky),
            "reason": "low_confidence_boundary_word" if len(indexes) > 1
                      else "low_confidence_word",
        })

    # A boundary token can nominate the same pair from both sides.  Merge such
    # targets before applying the strict per-delivery budget.
    merged = []
    for target in sorted(raw, key=lambda item: (
            item["focus_start"], item["focus_end"], item["min_probability"])):
        duplicate = next((item for item in merged
                          if set(item["indexes"]) & set(target["indexes"])), None)
        if duplicate is None:
            merged.append(dict(target))
            continue
        combined = sorted(set(duplicate["indexes"]) | set(target["indexes"]))
        duplicate["indexes"] = combined
        duplicate["segment_ids"] = [
            str(rows[position].get("segment_id") or "") for position in combined]
        duplicate["focus_start"] = min(
            duplicate["focus_start"], target["focus_start"])
        duplicate["focus_end"] = max(
            duplicate["focus_end"], target["focus_end"])
        duplicate["window_start"] = max(0.0, duplicate["focus_start"] - 1.0)
        duplicate["window_end"] = duplicate["focus_end"] + 1.0
        duplicate["min_probability"] = min(
            duplicate["min_probability"], target["min_probability"])
        duplicate["reason"] = "low_confidence_boundary_word"
    limit = (_LOW_CONFIDENCE_RELISTEN_LIMIT if max_targets is None
             else max(0, int(max_targets)))
    return sorted(merged, key=lambda item: (
        item["min_probability"], item["focus_start"]))[:limit]


def _low_confidence_target_key(target):
    return "%0.3f:%0.3f:%s" % (
        float(target.get("focus_start") or 0.0),
        float(target.get("focus_end") or 0.0),
        ",".join(str(value) for value in (target.get("tracks") or [])))


def _decode_low_confidence_focus(samples, decode_start, focus_start,
                                 focus_end, *, model_size, models_dir,
                                 beam_size, initial_prompt, hotwords,
                                 source_audio_track):
    """Decode one bounded window and retain only words centered in the focus."""
    local_evidence = []
    local_segments = transcribe(
        samples, model_size=model_size, models_dir=models_dir,
        on_status=None, initial_prompt=initial_prompt, hotwords=hotwords,
        beam_size=beam_size, batch_size=0, evidence_out=local_evidence,
        source_audio_track=source_audio_track)
    if len(local_segments) != len(local_evidence):
        return None

    selected_words = []
    contributing_rows = []
    fallback_segments = []
    for segment, local_row in zip(local_segments, local_evidence):
        try:
            local_start, local_end, text = segment
            absolute = (float(local_start) + decode_start,
                        float(local_end) + decode_start, str(text or ""))
        except (TypeError, ValueError):
            return None
        shifted = _offset_segment_evidence(local_row, decode_start)
        shifted["start"], shifted["end"] = absolute[0], absolute[1]
        shifted["text"] = absolute[2]
        overlap = max(0.0, min(focus_end, absolute[1]) -
                      max(focus_start, absolute[0]))
        if overlap <= 0.0:
            continue
        if not _local_asr_is_reliable(shifted, absolute[2]):
            return None
        contributing_rows.append(shifted)
        midpoint = (absolute[0] + absolute[1]) / 2.0
        if focus_start <= midpoint <= focus_end:
            fallback_segments.append(absolute)
        for word in shifted.get("words") or []:
            if not isinstance(word, dict):
                continue
            word_start = _float_or_none(word.get("start"))
            word_end = _float_or_none(word.get("end"))
            value = str(word.get("text", word.get("word", "")) or "")
            if (word_start is None or word_end is None or word_end < word_start
                    or not value.strip()):
                continue
            word_midpoint = (word_start + word_end) / 2.0
            if focus_start <= word_midpoint <= focus_end:
                item = dict(word)
                item["text"] = value
                item.pop("word", None)
                item["start"] = max(focus_start, word_start)
                item["end"] = min(focus_end, word_end)
                selected_words.append(item)

    if selected_words:
        selected_words.sort(key=lambda item: (
            float(item["start"]), float(item["end"])))
        text = "".join(str(item.get("text") or "")
                       for item in selected_words).strip()
        start = min(float(item["start"]) for item in selected_words)
        end = max(float(item["end"]) for item in selected_words)
    elif fallback_segments:
        fallback_segments.sort(key=lambda item: (item[0], item[1]))
        text = " ".join(item[2].strip() for item in fallback_segments
                        if item[2].strip()).strip()
        start = max(focus_start, min(item[0] for item in fallback_segments))
        end = min(focus_end, max(item[1] for item in fallback_segments))
    else:
        return None
    if not _subtitle_text_key(text) or end - start < 0.04:
        return None

    probabilities = [_float_or_none(item.get("probability"))
                     for item in selected_words]
    probabilities = [value for value in probabilities if value is not None]
    logprobs = [_float_or_none(item.get("avg_logprob"))
                for item in contributing_rows]
    logprobs = [value for value in logprobs if value is not None]
    no_speech = [_float_or_none(item.get("no_speech_prob"))
                 for item in contributing_rows]
    no_speech = [value for value in no_speech if value is not None]
    tracks = _audio_track_ids(source_audio_track)
    row = {
        "start": start, "end": end, "text": text, "text_original": text,
        "words": selected_words,
        "confidence": (sum(probabilities) / len(probabilities)
                       if probabilities else None),
        "avg_logprob": (sum(logprobs) / len(logprobs) if logprobs else None),
        "no_speech_prob": (max(no_speech) if no_speech else None),
        "source_audio_track": tracks[0] if len(tracks) == 1 else None,
        "source_audio_tracks": tracks,
        "original_asr_segment_start": decode_start,
        "original_asr_segment_end": decode_start + float(samples.size) / 16000.0,
        "split_reason": "low-confidence-consensus-relisten",
        "timing_source": "bounded-consensus-relisten",
        "asr_flags": [],
    }
    score = ((sum(probabilities) / len(probabilities)) if probabilities else 0.0,
             (sum(logprobs) / len(logprobs)) if logprobs else -99.0)
    return (start, end, text), row, score


def repair_low_confidence_before_delivery(index_dir, log=print, model_size=None,
                                          models_dir=None, max_targets=None):
    """Re-hear only high-risk word windows with two conservative decodes.

    Both decodes use the model saved in the index (or the user's current model
    for a legacy index).  One is prompt-free and one uses the saved glossary
    context.  Text is replaced only when both normalized results agree; all
    other outcomes remain diagnostics and never block Premiere delivery.
    """
    import subtitle_quality
    import time as _time

    index_path = (os.path.join(os.fspath(index_dir), "index.json")
                  if os.path.isdir(index_dir) else os.path.abspath(os.fspath(index_dir)))
    index_folder = os.path.dirname(index_path)
    with open(index_path, "rb") as handle:
        original_bytes = handle.read()
    data = json.loads(original_bytes.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError("字幕索引结构无效，不能执行低置信词局部重听")
    if len(data.get("segments") or []) != len(data.get("segment_evidence") or []):
        raise RuntimeError("低置信词局部重听跳过：台词与时间证据数量不一致")

    profile = data.get("asr_profile") or {}
    selected_model = str(model_size or profile.get("model") or
                         asr_hints.configured_model(BASE_DIR))
    try:
        selected_beam = int(profile.get("beam_size") or asr_hints.beam_size())
    except (TypeError, ValueError):
        selected_beam = asr_hints.beam_size()
    current_signature = _low_confidence_review_signature(
        data, selected_model, selected_beam)
    previous = data.get("subtitle_low_confidence_review") or {}
    previous_matches = (isinstance(previous, dict) and
                        previous.get("signature") == current_signature)
    if previous_matches and not int(previous.get("remaining_count") or 0):
        return {
            "changed": False, "cache_hit": True,
            "target_count": int(previous.get("target_count") or 0),
            "corrected_count": int(previous.get("corrected_count") or 0),
            "unchanged_count": int(previous.get("unchanged_count") or 0),
            "unresolved_count": int(previous.get("unresolved_count") or 0),
            "model": selected_model,
        }

    reviewed_keys = set(previous.get("reviewed_target_keys") or []) \
        if previous_matches else set()
    all_targets = _low_confidence_relisten_targets(data, max_targets=100000)
    pending_targets = [target for target in all_targets
                       if _low_confidence_target_key(target) not in reviewed_keys]
    budget = (_LOW_CONFIDENCE_RELISTEN_LIMIT if max_targets is None
              else max(0, int(max_targets)))
    targets = pending_targets[:budget]
    if not targets:
        return {
            "changed": False, "cache_hit": False, "target_count": 0,
            "corrected_count": 0, "unchanged_count": 0,
            "unresolved_count": 0, "model": selected_model,
        }
    source = str(data.get("source") or "")
    if not source or not os.path.isfile(source):
        raise RuntimeError("低置信词局部重听找不到原始素材：%s" %
                           (source or "未记录路径"))

    prompt = str(profile.get("initial_prompt") or "")
    hotwords = str(profile.get("hotwords") or "")
    pairs = [((float(segment[0]), float(segment[1]), str(segment[2] or "")),
              dict(row)) for segment, row in zip(
                  data.get("segments") or [], data.get("segment_evidence") or [])]
    full_audio_cache = {}
    details = []
    reviewed_this_run = set()
    corrected = unchanged = unresolved = 0
    log("字幕低置信词复核：使用 %s，仅局部重听 %d 个高风险窗口…" %
        (selected_model, len(targets)))
    for target in sorted(targets, key=lambda item: item["focus_start"],
                         reverse=True):
        reviewed_this_run.add(_low_confidence_target_key(target))
        segment_ids = {value for value in target.get("segment_ids") or [] if value}
        positions = [position for position, (_segment, row) in enumerate(pairs)
                     if (str(row.get("segment_id") or "") in segment_ids
                         if segment_ids else position in target["indexes"])]
        if not positions:
            unresolved += 1
            details.append(dict(target, outcome="target_not_found"))
            continue
        focus_start = min(float(pairs[position][0][0]) for position in positions)
        focus_end = max(float(pairs[position][0][1]) for position in positions)
        decode_start = max(0.0, min(target["window_start"], focus_start))
        decode_end = max(focus_end, target["window_end"])
        tracks = list(target.get("tracks") or [])
        stream = tracks[0] if len(tracks) == 1 else tracks
        try:
            samples = load_audio_window_mono16k(
                source, stream, decode_start, decode_end)
            if samples.size < 800:
                raise RuntimeError("局部音频为空")
        except Exception as exc:  # noqa: BLE001
            key = tuple(tracks or [0])
            try:
                if key not in full_audio_cache:
                    full_audio_cache[key] = load_audio_mono16k(source, stream)
                full_audio = full_audio_cache[key]
                samples = full_audio[
                    int(decode_start * 16000):int(decode_end * 16000)]
                if samples.size < 800:
                    raise RuntimeError("兼容模式局部音频为空")
            except Exception as fallback_exc:  # noqa: BLE001
                unresolved += 1
                details.append(dict(
                    target, outcome="audio_read_failed",
                    warning=("%s；%s" % (exc, fallback_exc))[:180]))
                continue
        samples = normalize_for_asr(samples)
        attempts = []
        attempt_error = ""
        for attempt_prompt, attempt_hotwords in (("", ""), (prompt, hotwords)):
            try:
                attempts.append(_decode_low_confidence_focus(
                    samples, decode_start, focus_start, focus_end,
                    model_size=selected_model, models_dir=models_dir,
                    beam_size=selected_beam, initial_prompt=attempt_prompt,
                    hotwords=attempt_hotwords, source_audio_track=stream))
            except Exception as exc:  # noqa: BLE001
                attempts.append(None)
                attempt_error = str(exc)[:160]
        if (len(attempts) != 2 or not attempts[0] or not attempts[1] or
                _subtitle_text_key(attempts[0][0][2]) !=
                _subtitle_text_key(attempts[1][0][2])):
            unresolved += 1
            details.append(dict(
                target, outcome="decode_disagreement",
                first=(attempts[0][0][2] if attempts and attempts[0] else ""),
                second=(attempts[1][0][2]
                        if len(attempts) > 1 and attempts[1] else ""),
                warning=attempt_error))
            continue

        chosen = max(attempts, key=lambda item: item[2])
        replacement_segment, replacement_row, _score = chosen
        original_text = " ".join(pairs[position][0][2]
                                 for position in positions).strip()
        original_length = _visible_text_length(original_text)
        replacement_length = _visible_text_length(replacement_segment[2])
        if (replacement_length < max(1, int(np.floor(original_length * 0.55)))
                or replacement_length > max(original_length + 8,
                                              int(np.ceil(original_length * 1.8)))):
            unresolved += 1
            details.append(dict(
                target, outcome="coverage_rejected", original=original_text,
                candidate=replacement_segment[2]))
            continue
        if _subtitle_text_key(original_text) == _subtitle_text_key(
                replacement_segment[2]):
            unchanged += 1
            details.append(dict(target, outcome="confirmed", text=original_text))
            continue

        position_set = set(positions)
        pairs = [pair for position, pair in enumerate(pairs)
                 if position not in position_set]
        replacement_row = dict(replacement_row)
        replacement_row.pop("segment_id", None)
        replacement_row["low_confidence_relisten_from"] = original_text
        pairs.append((replacement_segment, replacement_row))
        pairs.sort(key=lambda item: (item[0][0], item[0][1]))
        corrected += 1
        details.append(dict(
            target, outcome="corrected", original=original_text,
            replacement=replacement_segment[2]))

    candidate = copy.deepcopy(data)
    segments = [segment for segment, _row in pairs]
    rows = [row for _segment, row in pairs]
    segments, rows, structural = sort_segment_evidence_pairs(segments, rows)
    if structural:
        raise RuntimeError("低置信词局部重听产生结构问题，已保留原索引")
    rows = align_segment_evidence(segments, rows, data.get("audio_stream_used"))
    candidate["segments"] = [[round(float(start), 6), round(float(end), 6), text]
                             for start, end, text in segments]
    candidate["segment_evidence"] = rows
    introduced_issues = subtitle_quality.alignment_issues(candidate)
    if introduced_issues:
        raise RuntimeError("低置信词局部重听未通过时间轴安全检查，已保留原索引")
    _refresh_delivery_repair_metadata(candidate, len(data.get("segments") or []))
    reviewed_keys.update(reviewed_this_run)
    remaining_targets = _low_confidence_relisten_targets(
        candidate, max_targets=100000)
    remaining_count = sum(
        1 for target in remaining_targets
        if _low_confidence_target_key(target) not in reviewed_keys)
    review = {
        "version": _LOW_CONFIDENCE_RELISTEN_VERSION,
        "model": selected_model, "beam_size": selected_beam,
        "target_count": len(targets), "corrected_count": corrected,
        "unchanged_count": unchanged, "unresolved_count": unresolved,
        "remaining_count": remaining_count,
        "reviewed_target_keys": sorted(reviewed_keys),
        "details": details,
        "reviewed_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    review["signature"] = _low_confidence_review_signature(
        candidate, selected_model, selected_beam)
    candidate["subtitle_low_confidence_review"] = review

    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_path = index_path + ".before_low_confidence_relisten_%s.bak" % stamp
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = (index_path +
                       ".before_low_confidence_relisten_%s_%d.bak" %
                       (stamp, suffix))
        suffix += 1
    with open(backup_path, "xb") as handle:
        handle.write(original_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        _atomic_write_index_json(index_path, candidate)
    except Exception:
        _atomic_write_bytes(index_path, original_bytes)
        raise
    # Review metadata does not change the subtitle text or timing.  Keep the
    # expensive full-film semantic proofreading cache when both local decodes
    # merely confirmed/disagreed with the original.  Only a committed ASR text
    # replacement invalidates that cache.
    if corrected:
        for name in (subtitle_quality.CACHE_NAME, subtitle_quality.REPORT_NAME):
            try:
                os.remove(os.path.join(index_folder, name))
            except FileNotFoundError:
                pass
            except OSError:
                pass
    log("字幕低置信词复核完成：修正 %d 处，确认 %d 处，保留疑点 %d 处。" %
        (corrected, unchanged, unresolved))
    return {
        "changed": bool(corrected), "cache_hit": False,
        "target_count": len(targets), "corrected_count": corrected,
        "unchanged_count": unchanged, "unresolved_count": unresolved,
        "remaining_count": remaining_count,
        "model": selected_model, "backup_path": backup_path,
    }


def repair_index_before_delivery(index_dir, log=print, max_attempts=2,
                                 model_size=None, models_dir=None):
    """Locally heal ASR timing defects before writing editable PR captions.

    The original index is not touched until a fully repaired in-memory copy
    passes the same delivery gate.  Each flagged row is re-heard from its own
    source audio track using only the original Whisper parent plus short
    context.  No call to :func:`index_video` or full-recording ASR is made.
    """
    import subtitle_quality
    import time as _time

    index_path = (os.path.join(os.fspath(index_dir), "index.json")
                  if os.path.isdir(index_dir) else os.path.abspath(os.fspath(index_dir)))
    index_folder = os.path.dirname(index_path)
    try:
        with open(index_path, "rb") as handle:
            original_bytes = handle.read()
        original = json.loads(original_bytes.decode("utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("字幕索引无法读取，不能执行交付前自动修复：%s" % exc) from exc
    if not isinstance(original, dict):
        raise RuntimeError("字幕索引结构无效，不能执行交付前自动修复")

    initial_issues = subtitle_quality.alignment_issues(original)
    if not initial_issues:
        return {
            "changed": False, "attempts": 0, "repaired_issue_count": 0,
            "remaining_issues": [], "backup_path": "",
        }

    segments = original.get("segments") or []
    evidence = original.get("segment_evidence") or []
    if len(segments) != len(evidence):
        raise RuntimeError(
            "字幕时间轴质量检查未通过：索引结构损坏，segments 与 "
            "segment_evidence 数量不一致，"
            "局部自动修复不会猜测或删除台词。")
    if any(not isinstance(row, dict) for row in evidence):
        raise RuntimeError(
            "字幕时间轴质量检查未通过：索引结构损坏，"
            "segment_evidence 含无效记录")
    source = str(original.get("source") or "")
    if not source or not os.path.isfile(source):
        raise RuntimeError(
            "字幕时间轴质量检查未通过：局部自动修复找不到原始素材：%s。"
            "请重新分析素材生成带来源路径的新索引。" % (source or "未记录路径"))

    profile = original.get("asr_profile") or {}
    selected_model = str(model_size or profile.get("model") or
                         asr_hints.configured_model(BASE_DIR))
    try:
        selected_beam = int(profile.get("beam_size") or asr_hints.beam_size())
    except (TypeError, ValueError):
        selected_beam = asr_hints.beam_size()
    selected_prompt = str(profile.get("initial_prompt") or "")
    selected_hotwords = str(profile.get("hotwords") or "")
    max_attempts = max(1, min(4, int(max_attempts or 1)))
    candidate = copy.deepcopy(original)
    full_audio_cache = {}
    remaining = initial_issues

    for attempt in range(1, max_attempts + 1):
        targets = _delivery_repair_targets(candidate)
        if not targets:
            reasons = sorted({reason for issue in remaining
                              for reason in (issue.get("reasons") or [])})
            raise RuntimeError(
                "字幕时间轴仍有无法局部复听的结构/重叠问题：%s。"
                "原索引未修改。" % ("、".join(reasons) or "未知原因"))
        log("字幕交付前自愈：第 %d/%d 次，局部复听 %d 处…" %
            (attempt, max_attempts, len(targets)))
        text_segs = [(float(row[0]), float(row[1]), str(row[2] or ""))
                     for row in candidate.get("segments") or []]
        rows = [dict(row) for row in candidate.get("segment_evidence") or []]
        blockers = list(candidate.get("subtitle_alignment_blockers") or [])

        for target in targets:
            target_index = _find_delivery_target(rows, target)
            if target_index is None:
                continue
            if target.get("allow_drop_stretched_single"):
                target_flags = list(rows[target_index].get("asr_flags") or [])
                if "delivery_stretched_overlap_hallucination" not in target_flags:
                    target_flags.append("delivery_stretched_overlap_hallucination")
                rows[target_index]["asr_flags"] = target_flags
            tracks = list(target.get("tracks") or [])
            stream = (tracks[0] if len(tracks) == 1 else tracks)
            window_start = float(target["window_start"])
            window_end = float(target["window_end"])
            try:
                repair_audio = load_audio_window_mono16k(
                    source, stream, window_start, window_end)
                repair_offset = window_start
                if repair_audio.size < 800:
                    raise RuntimeError("局部音频为空")
            except Exception as exc:  # noqa: BLE001
                # Compatibility fallback for media containers that cannot seek
                # accurately.  Decode at most once per selected track set and
                # still run only the bounded ASR repair, never index_video.
                key = tuple(tracks or [0])
                if key not in full_audio_cache:
                    log("  局部读取失败，复用完整 PCM 兼容模式：%s" % str(exc)[:80])
                    full_audio_cache[key] = load_audio_mono16k(source, stream)
                repair_audio = full_audio_cache[key]
                repair_offset = 0.0
            if repair_audio is None or getattr(repair_audio, "size", 0) == 0:
                raise RuntimeError("字幕局部自动修复无法读取音轨 %s" % (tracks or [0]))
            repair_audio = normalize_for_asr(repair_audio)
            repair_suspected_alignment(
                text_segs, repair_audio, model_size=selected_model,
                models_dir=models_dir, on_status=lambda message: log("  " + message),
                evidence_out=rows, source_audio_track=stream,
                beam_size=selected_beam, initial_prompt=selected_prompt,
                hotwords=selected_hotwords, blockers_out=blockers,
                audio_offset=repair_offset, target_indexes=[target_index],
                use_parent_context=True)

        text_segs, rows = _dedupe_delivery_relisten_pairs(text_segs, rows)
        text_segs, rows, pair_issues = sort_segment_evidence_pairs(text_segs, rows)
        if pair_issues:
            remaining = pair_issues
            continue
        rows = align_segment_evidence(
            text_segs, rows, candidate.get("audio_stream_used"))
        candidate["segments"] = [[round(float(start), 6), round(float(end), 6),
                                  str(text or "")]
                                 for start, end, text in text_segs]
        candidate["segment_evidence"] = rows
        candidate["subtitle_alignment_blockers"] = blockers
        remaining = subtitle_quality.alignment_issues(candidate)
        if remaining:
            log("  第 %d 次复检仍有 %d 处时间问题，准备有限重试。" %
                (attempt, len(remaining)))
            continue

        candidate["subtitle_alignment_blockers"] = []
        _refresh_delivery_repair_metadata(candidate, len(segments))
        candidate["delivery_alignment_repair"] = {
            "version": 1,
            "attempts": attempt,
            "initial_issue_count": len(initial_issues),
            "remaining_issue_count": 0,
            "model": selected_model,
            "beam_size": selected_beam,
            "repaired_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        backup_path = index_path + ".before_delivery_repair_%s.bak" % stamp
        suffix = 1
        while os.path.exists(backup_path):
            backup_path = (index_path +
                           ".before_delivery_repair_%s_%d.bak" % (stamp, suffix))
            suffix += 1
        with open(backup_path, "xb") as handle:
            handle.write(original_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _atomic_write_index_json(index_path, candidate)
            with open(index_path, encoding="utf-8") as handle:
                verified = json.load(handle)
            readback_issues = subtitle_quality.alignment_issues(verified)
            if readback_issues:
                raise RuntimeError("写回后字幕时间轴复检失败")
        except Exception:
            _atomic_write_bytes(index_path, original_bytes)
            raise
        for name in (subtitle_quality.CACHE_NAME, subtitle_quality.REPORT_NAME):
            try:
                os.remove(os.path.join(index_folder, name))
            except FileNotFoundError:
                pass
            except OSError as exc:
                log("  旧字幕缓存未能删除，将由签名自动失效：%s" % str(exc)[:80])
        log("字幕交付前自愈完成：%d 处时间问题已修复。" % len(initial_issues))
        return {
            "changed": True, "attempts": attempt,
            "repaired_issue_count": len(initial_issues),
            "remaining_issues": [], "backup_path": backup_path,
        }

    raise RuntimeError(
        "字幕时间轴局部自动修复在 %d 次尝试后仍未通过（剩余 %d 处）。"
        "原索引未修改，请检查原素材音轨或 ASR 模型。" %
        (max_attempts, len(remaining)))


def repair_suspected_repetitions(text_segs, audio,
                                 model_size: str = MODEL_ACCURATE,
                                 models_dir: str | None = None,
                                 on_status=None, should_stop=None,
                                 evidence_out=None, source_audio_track=None,
                                 beam_size=None, chunk_seconds: float = 40.0,
                                 overlap: float = 0.8):
    """Re-hear Whisper loop parents without prompt pollution.

    A repeated phrase may be real speech, so a loop flag alone never deletes
    text.  We replace a parent only when a prompt-free local decode produces
    reliable non-loop speech covering every flagged loop row.  The entire
    original parent is then replaced, removing small loop fragments that the
    first word-gap split emitted as separate, apparently normal rows.
    """
    if (evidence_out is None or audio is None
            or getattr(audio, "size", 0) == 0
            or len(text_segs) != len(evidence_out)):
        return text_segs
    pairs = [((float(a), float(b), str(t or "")), dict(row))
             for (a, b, t), row in zip(text_segs, evidence_out)]
    parents = []
    seen = set()
    wanted_tracks = set(_audio_track_ids(source_audio_track))
    for _segment, row in pairs:
        if "repetition_suspected" not in set(row.get("asr_flags") or []):
            continue
        if _repetition_has_credible_word_timing(row):
            continue
        row_tracks = set(_audio_track_ids(
            row.get("source_audio_tracks") or row.get("source_audio_track")))
        if wanted_tracks and row_tracks and not (wanted_tracks & row_tracks):
            continue
        start = _float_or_none(row.get("original_asr_segment_start"))
        end = _float_or_none(row.get("original_asr_segment_end"))
        if start is None or end is None or end <= start:
            start = _float_or_none(row.get("start"))
            end = _float_or_none(row.get("end"))
        if start is None or end is None or end <= start:
            continue
        key = (round(start, 6), round(end, 6))
        if key not in seen:
            seen.add(key)
            parents.append(key)
    if not parents:
        pairs = _prune_overlapping_asr_fragments(pairs)
        text_segs[:] = [segment for segment, _row in pairs]
        evidence_out[:] = [row for _segment, row in pairs]
        return text_segs

    sr = 16000
    duration = float(audio.size) / sr
    chunk_seconds = max(8.0, float(chunk_seconds))
    overlap = max(0.0, min(float(overlap), chunk_seconds / 4.0))
    step = max(4.0, chunk_seconds - overlap)
    repaired_parent_count = 0
    repair_anchors = []

    for parent_start, parent_end in parents:
        if should_stop and should_stop():
            raise IndexCancelled("用户停止")
        parent_start = max(0.0, min(duration, float(parent_start)))
        parent_end = max(parent_start, min(duration, float(parent_end)))
        parent_positions = []
        loop_targets = []
        for position, (segment, row) in enumerate(pairs):
            old_start = _float_or_none(row.get("original_asr_segment_start"))
            old_end = _float_or_none(row.get("original_asr_segment_end"))
            same_parent = (old_start is not None and old_end is not None
                           and abs(old_start - parent_start) < 0.01
                           and abs(old_end - parent_end) < 0.01)
            if not same_parent:
                continue
            parent_positions.append(position)
            if "repetition_suspected" in set(row.get("asr_flags") or []):
                loop_targets.append(segment)
        if not loop_targets:
            continue
        decode_start = max(0.0, parent_start - 0.60)
        decode_end = min(duration, parent_end + 0.60)
        local_pairs = []
        local_loop_seen = False
        cursor = decode_start
        while cursor < decode_end - 1e-6:
            chunk_end = min(decode_end, cursor + chunk_seconds)
            local_evidence = []
            try:
                local_segments = transcribe(
                    audio[int(cursor * sr):int(chunk_end * sr)],
                    model_size=model_size, models_dir=models_dir,
                    on_status=None, initial_prompt="", hotwords="",
                    beam_size=beam_size, batch_size=0,
                    should_stop=should_stop, evidence_out=local_evidence,
                    source_audio_track=source_audio_track)
            except IndexCancelled:
                raise
            except Exception:  # noqa: BLE001
                local_segments, local_evidence = [], []
            if len(local_segments) != len(local_evidence):
                local_segments, local_evidence = [], []
            left_guard = cursor + (overlap / 2.0 if cursor > decode_start else 0.0)
            right_guard = chunk_end - (
                overlap / 2.0 if chunk_end < decode_end else 0.0)
            for segment, row in zip(local_segments, local_evidence):
                local_start, local_end, text = segment
                source_start = float(local_start) + cursor
                source_end = float(local_end) + cursor
                midpoint = (source_start + source_end) / 2.0
                if midpoint < left_guard or midpoint >= right_guard:
                    continue
                if (midpoint < parent_start or midpoint > parent_end) and max(
                        0.0, min(parent_end, source_end)
                        - max(parent_start, source_start)) < 0.10:
                    continue
                shifted = _offset_segment_evidence(row, cursor)
                shifted["start"], shifted["end"] = source_start, source_end
                shifted["text"] = str(text or "")
                if _loop_suspected_text(text):
                    if max(0.0, min(parent_end, source_end)
                           - max(parent_start, source_start)) >= 0.10:
                        local_loop_seen = True
                    continue
                if not _local_asr_is_reliable(shifted, text):
                    continue
                local_pairs.append(
                    ((source_start, source_end, str(text or "")), shifted))
            if chunk_end >= decode_end:
                break
            cursor += step

        # A real stutter remains repetitive when re-heard without prompts.
        # Only replace when every loop row has reliable non-loop local speech.
        loop_coverage = [
            _segment_coverage_ratio(target, local_pairs)
            for target in loop_targets]
        if (not local_pairs or any(value < 0.20 for value in loop_coverage)
                or (local_loop_seen
                    and any(value < 0.75 for value in loop_coverage))):
            continue

        # Replacing a Whisper parent deletes every child emitted from it, not
        # only the repeated child.  Preserve the parent unless each ordinary
        # child is substantially covered by the prompt-free decode.
        loop_units = set().union(*(
            _repetition_units(segment[2]) for segment in loop_targets))
        ordinary_children = [
            segment for position in parent_positions
            for segment, row in [pairs[position]]
            if "repetition_suspected" not in set(row.get("asr_flags") or [])
            and not _loop_suspected_text(segment[2])
            and not _is_repetition_shard(segment[2], loop_units)]
        if any(_segment_coverage_ratio(segment, local_pairs) < 0.45
               for segment in ordinary_children):
            continue

        target_set = set(parent_positions)
        kept = [pair for position, pair in enumerate(pairs)
                if position not in target_set]
        accepted = []
        for segment, row in local_pairs:
            # Alignment repairs already accepted elsewhere are authoritative;
            # do not duplicate them when the whole repetition parent is heard.
            if any(_same_audio_track(row, old_row) and (
                    _span_overlap(segment, old_segment)
                    >= max(0.08, (float(segment[1]) - float(segment[0])) * 0.50)
                    or float(old_segment[0]) <=
                    (float(segment[0]) + float(segment[1])) / 2.0
                    <= float(old_segment[1]))
                   for old_segment, old_row in kept):
                continue
            accepted.append((segment, row))
        if not accepted:
            continue
        pairs = sorted(kept + accepted,
                       key=lambda pair: (float(pair[0][0]), float(pair[0][1])))
        repair_anchors.extend(accepted)
        repaired_parent_count += 1

    pairs = _prune_overlapping_asr_fragments(pairs, anchors=repair_anchors)
    text_segs[:] = [segment for segment, _row in pairs]
    evidence_out[:] = [row for _segment, row in pairs]
    if repaired_parent_count and on_status:
        on_status("重复疑点复听：修复 %d 个 Whisper 循环父段" % repaired_parent_count)
    return text_segs


# --------------------------------------------------------------------------
# 4. 组装卡片
# --------------------------------------------------------------------------
def semantic_pass(text_segs, on_status=None, should_stop=None):
    """用本地大模型给逐句台词标类型和段落。失败就返回 None（退回规则）。"""
    try:
        import semantic as sem
    except Exception as exc:  # noqa: BLE001
        if on_status:
            on_status("语义分析模块不可用：%s" % exc)
        return None
    try:
        res = sem.analyze(text_segs, on_log=(on_status or (lambda m: None)),
                          should_stop=should_stop)
        return res
    except getattr(sem, "SemanticCancelled", RuntimeError) as exc:
        if should_stop and should_stop():
            raise IndexCancelled(str(exc))
        raise
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        if on_status:
            on_status("语义分析没跑成（不影响索引）：%s" % str(exc)[:120])
        return None


def seg_feature_rows(text_segs, diffs, audio, duration):
    """给每句算 [画面变化, 音量]，存进索引，训练和推理共用同一份。"""
    n = len(text_segs)
    if not n:
        return []
    dur = float(duration or 1.0) or 1.0
    hop_d = dur / max(1, len(diffs)) if diffs is not None and len(diffs) > 0 else 0.0
    hop_a = dur / max(1, len(audio)) if audio is not None and len(audio) > 0 else 0.0
    rows = []
    for a, b, _t in text_segs:
        m = 0.0
        if diffs is not None and len(diffs) > 0 and hop_d > 0:
            i0 = max(0, int(a / hop_d))
            i1 = min(len(diffs), max(i0 + 1, int(b / hop_d) + 1))
            vals = [float(x) for x in diffs[i0:i1] if isinstance(x, (int, float))]
            m = (sum(vals) / len(vals)) if vals else 0.0
        lv = -60.0
        if audio is not None and len(audio) > 0 and hop_a > 0:
            j0 = max(0, int(a / hop_a))
            j1 = min(len(audio), max(j0 + 1, int(b / hop_a) + 1))
            vals = [float(x) for x in audio[j0:j1] if isinstance(x, (int, float))]
            lv = max(vals) if vals else -60.0
        rows.append([round(m, 3), round(lv, 1)])
    return rows


def completeness_pair(text_segs):
    """算每句完整度，返回 {score:[], flag:[]}。"""
    try:
        import completeness as cmp
        fake = {"segments": [[a, b, t] for a, b, t in text_segs]}
        scores, flags = cmp.compute(fake)
        return {"score": scores, "flag": flags}
    except Exception:  # noqa: BLE001
        return {}


def score_sentences(text_segs, on_status=None, feature_data=None):
    """用训练出来的剪辑模型给每句打分（0~1，越高越该留）。

    放在分析阶段做，而不是等到剪辑时才算：
      · 建索引时算一次，之后反复剪不用重复计算
      · 卡片能直接带上分数，界面上也能量化"这张卡值不值得留"
    """
    if llm_gateway.api_enabled():
        if on_status:
            on_status("API 模式：跳过本地训练评分，0.5 仅为中立占位；内容取舍交给 API")
        return [0.5] * len(text_segs)
    try:
        import autocut
        model = autocut.load_model()
        if not model:
            # 没有训练模型：用规则兜底打分（句长/语速/口头禅/音量/画面），
            # 否则索引里 scores 是空的，剪辑时三档力度会拿不到内容而撞车。
            fb = autocut.fallback_scores({"segments": [[a, b, t] for a, b, t in text_segs]})
            if fb:
                if on_status:
                    on_status("没有训练模型，用规则打分（%d 句）" % len(fb))
                return [round(float(v), 3) for v in fb]
            return None
        segs = [[a, b, t] for a, b, t in text_segs]
        n = len(segs)
        if not n:
            return None
        import numpy as _np
        mu = _np.array(model["mean"], dtype=_np.float64)
        sd = _np.array(model["std"], dtype=_np.float64) + 1e-9
        w = _np.array(model["weights"], dtype=_np.float64)
        dim = int(len(mu))
        if len(sd) != dim or len(w) != dim:
            raise ValueError("剪辑模型参数维度不一致")
        if dim == 10:
            X = _np.array([autocut._seg_feature_row(segs, i) for i in range(n)],
                          dtype=_np.float64)
            if X.shape[1] != dim:
                raise ValueError("剪辑模型需要 %d 维，文本特征只有 %d 维"
                                 % (dim, X.shape[1]))
        elif dim == 16:
            # 画面/音量分析和 ASR 并行。第一次走到这里时画面结果通常还没取回，
            # 不能拿 10 维文本特征硬套 16 维模型；等 motion future 完成后重算。
            if not feature_data:
                if on_status:
                    on_status("剪辑模型需要画面/音量特征，等待画面分析完成后打分")
                return None
            X = autocut.seg_feature_matrix(feature_data)
            if X is None or X.shape != (n, dim):
                raise ValueError("画面/音量特征不完整，期望 (%d,%d)" % (n, dim))
        else:
            raise ValueError("不支持的剪辑模型维度：%d（仅支持 10 或 16）" % dim)
        z = ((X - mu) / sd) @ w + float(model["bias"])
        return [round(float(v), 3) for v in 1.0 / (1.0 + _np.exp(-_np.clip(z, -30, 30)))]
    except Exception as exc:  # noqa: BLE001
        if on_status:
            on_status("模型打分失败，改用规则分：%s" % str(exc)[:100])
        try:
            fb = autocut.fallback_scores(
                {"segments": [[a, b, t] for a, b, t in text_segs]})
            return [round(float(v), 3) for v in fb] if fb else None
        except Exception:  # noqa: BLE001
            return None


def paragraph_runs(text_segs, sem):
    """把句子按模型给的段落边界合并成「段落区间」，替代音量能量切分。

    这一步是"分析输出合不合理"的关键：按能量切会把一段话从中间劈开，
    按语义段落切，每张卡片天然是一个完整话题。
    """
    runs = []
    cur_a = cur_b = None
    for i, (a, b, _t) in enumerate(text_segs):
        pb = bool(sem[i][1]) if i < len(sem) else False
        if cur_a is None:
            cur_a, cur_b = a, b
        elif pb:
            runs.append((cur_a, cur_b))
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_a is not None:
        runs.append((cur_a, cur_b))
    return runs


def analysis_settings():
    """读取分析参数（和界面共用 conf/settings.json）。

    卡片长度上限是这轮新增的：段落切得太长，卡片里会混进不相关的内容，
    挑的时候很难判断，所以允许玩家自己定一个上限（带 ±偏移）。
    """
    # 一句一个片段：不再按秒数合并，卡片=句子
    cfg = {"card_max": 1e9, "card_tol": 0.0, "card_min": 2.0,
           "card_mode": "sentence"}
    try:
        p = os.path.join(BASE_DIR, "conf", "settings.json")
        if os.path.isfile(p):
            user = json.load(open(p, encoding="utf-8"))
            # 模式固定为 sentence，旧配置里的值忽略
            for k in ("card_max", "card_tol", "card_min"):
                if k in user:
                    try:
                        v = float(user[k])
                    except (TypeError, ValueError):
                        continue
                    # 下限保护：5 秒切一张卡毫无意义，多半是设置被写坏了
                    if k == "card_max":
                        v = max(15.0, min(v, 300.0))
                    elif k == "card_tol":
                        v = max(0.0, min(v, 60.0))
                    else:
                        v = max(2.0, min(v, 60.0))
                    cfg[k] = v
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _split_gap(a, b, piece=30.0):
    """把一段没有声音的时间切成不超过 piece 秒的卡片。"""
    if b <= a:
        return []
    n = max(1, int((b - a) // piece) + 1)
    step = (b - a) / n
    return [(a + i * step, min(b, a + (i + 1) * step)) for i in range(n)]


def fix_names(text_segs):
    """按 conf/asr_terms.txt 把听错的人名/系列名改回来。

    返回 (新的 text_segs, 纠正次数)。只替换"错写法"，不猜测、不模糊匹配。
    """
    try:
        import re as _re
        p = os.path.join(BASE_DIR, "conf", "asr_terms.txt")
        if not os.path.isfile(p):
            return text_segs, 0
        rules = []
        for line in open(p, encoding="utf-8").read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            good, bads = line.split("|", 1)
            good = good.strip()
            for b in _re.split(r"[,，\s]+", bads):
                b = b.strip()
                if good and b and b != good:
                    rules.append((b, good))
        if not rules:
            return text_segs, 0
        rules.sort(key=lambda x: -len(x[0]))     # 长的先换，避免切碎
        out, n = [], 0
        for a, b, t in text_segs:
            nt = t
            for bad, good in rules:
                if bad in nt:
                    n += nt.count(bad)
                    nt = nt.replace(bad, good)
            out.append((a, b, nt))
        return out, n
    except Exception:  # noqa: BLE001
        return text_segs, 0


def build_cards(duration: float, runs, text_segs, times, diffs,
                min_card: float = 3.0, max_card: float = 75.0,
                mode: str = "paragraph"):
    """切卡片：以「句子」为单位，不按时长硬切。

    以前按时长切，会把一句话从中间劈开——观众听到半句话，很难受。
    现在的规则：
      1) 一句话绝不拆开，卡片的两端一定是句子边界
      2) 段落（runs）是天然的话题边界，跨段落当然要断开
      3) 段落太长时，在句子边界处断开，让每张卡尽量接近 max_card
      4) 没有说话的空档不单独成卡（那不是内容）
    """
    segs = [(float(a), float(b), str(t)) for a, b, t in (text_segs or [])
            if float(b) > float(a)]
    if not segs:
        return _cards_from_runs(duration, runs, max_card)   # 没台词时退回时长切

    segs.sort(key=lambda x: x[0])

    def _in_span(span):
        a, b = span
        out = [x for x in segs if x[0] >= a - 0.05 and x[1] <= b + 0.05]
        return out

    # 段落区间；没给 runs 就把整片当一个段落
    spans = [(max(0.0, float(a)), min(duration, float(b))) for a, b in (runs or [])]
    if not spans:
        spans = [(0.0, duration)]

    cards = []
    if mode == "sentence":
        # 一句一张卡；没有声音的时间段也要留卡（单独成卡，不并进句子）。
        # 分析阶段不删除任何内容 —— 空场也是素材的一部分，只是没台词。
        cards = [(x[0], x[1]) for x in segs if (x[1] - x[0]) >= 0.4]
        out = []
        prev_end = 0.0
        for a, b in cards:
            if a - prev_end >= 0.8:
                out.extend(_split_gap(prev_end, a))
            out.append((a, b))
            prev_end = max(prev_end, b)
        if duration - prev_end >= 0.8:
            out.extend(_split_gap(prev_end, duration))
        return out or cards

    for span in spans:
        inside = _in_span(span)
        if not inside:
            continue
        cur = []
        for sg in inside:
            if cur and (sg[1] - cur[0][0]) > max_card:
                cards.append((cur[0][0], cur[-1][1]))
                cur = []
            cur.append(sg)
        if cur:
            cards.append((cur[0][0], cur[-1][1]))

    # 合并过短卡片和重叠（都是句子边界，不会切句）
    # 只合并“太短”的卡片，绝不能因为首尾相接就合并：
    # Whisper 的句子是首尾相连的（上句结束时间 = 下句开始时间），
    # 一旦按“接触就合并”，所有卡片会连成一张巨卡。
    merged = []
    for a, b in cards:
        if (merged and (b - a) < min_card and (a - merged[-1][1]) < 3.0
                and (b - merged[-1][0]) <= max_card):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if not merged:
        return [(0.0, min(duration, max_card))]

    return _tile(duration, merged, max_card)


def _tile(duration, merged, max_card):
    """把内容卡片铺满全片：空白并入前一张，太长的空白单独成卡。"""
    if not merged:
        return [(0.0, min(duration, max_card))]
    # 做法：把小空档并给前一张卡（边界仍然落在下一句的开始处，不会切句）；
    # 空档太长（超过 gap_split）就单独成一张“无台词”卡，免得出现巨卡。
    gap_split = max(20.0, max_card * 0.8)
    filled = [list(merged[0])]
    filled[0][0] = 0.0                     # 开头也算进第一张卡
    for a, b in merged[1:]:
        prev = filled[-1]
        gap = a - prev[1]
        if gap <= gap_split:
            prev[1] = a                    # 空白并入前一张
        else:
            filled.append([prev[1], a])    # 太长的空白单独成卡
        filled.append([a, b])
    if duration - filled[-1][1] > 0.05:
        last = filled[-1]
        if duration - last[1] <= gap_split:
            last[1] = duration
        else:
            filled.append([last[1], duration])
    return [(float(a), float(b)) for a, b in filled if b - a > 0.05]


def _cards_from_runs(duration: float, runs, max_card: float):
    """没有台词时的兜底：按说话区间切，超长的按 max_card 硬切。"""
    if not runs:
        return [(0.0, min(duration, max_card))]
    out = []
    for a, b in runs:
        a, b = max(0.0, float(a)), min(duration, float(b))
        if b - a <= 0:
            continue
        if b - a <= max_card:
            out.append((a, b))
            continue
        n = int((b - a) // max_card) + 1
        step = (b - a) / n
        for m in range(n):
            out.append((a + m * step, min(b, a + (m + 1) * step)))
    return out


def looks_like_loop(text: str) -> bool:
    """判断这句是不是 Whisper 的重复幻觉。

    长时间静音或纯游戏音上，Whisper 会吐「硅硅硅硅硅硅」这种重复串。
    实测 64 分钟素材里有一段连续 5 分钟全是这种，直接污染了 8 张卡片的标题。
    """
    t = (text or "").strip()
    if len(t) < 6:
        return False
    uniq = len(set(t))
    if uniq / len(t) < 0.30:
        return True
    # 短模式重复 4 次以上
    for n in (1, 2, 3, 4):
        if n * 5 > len(t):
            continue
        for i in range(0, min(len(t) - n * 4, 12) + 1):
            pat = t[i:i + n]
            if pat.strip() and pat * 4 in t:
                return True
    return False


def _fold_db(db, hop: float, grid: float = 1.0) -> list:
    """把逐帧 RMS(dB) 压成每秒一格，给自动剪辑判断静音用。"""
    if db is None or len(db) == 0:
        return []
    per = max(1, int(round(grid / max(hop, 1e-6))))
    out = []
    for i in range(0, len(db), per):
        chunk = db[i:i + per]
        out.append(round(float(np.percentile(chunk, 90)), 1))
    return out


def tag_cards(cards):
    """按分位数打标签，不依赖模型。"""
    if not cards:
        return cards
    motions = np.array([c["motion"] for c in cards])
    m_hi = float(np.percentile(motions, 75)) if motions.size > 1 else 1.0
    for c in cards:
        tags = []
        dur = c["end"] - c["start"]
        if c["motion"] >= m_hi and c["motion"] > 0.05:
            tags.append("画面变化大")
        if dur >= 40:
            tags.append("长段落")
        if c.get("highlight"):
            tags.append("音量高光")
        c["tags"] = tags
    return cards


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _cleanup_stale_temp(max_age: float = 7200) -> None:
    """
    清掉上次崩溃或被强杀留下的临时音频。

    抽出来的 wav 是 16kHz 单声道，一小时素材约 115 MB，跑完会自己删；
    但程序被强行关掉时会残留，所以在每次建索引前顺手扫一遍。
    """
    import glob
    import time as _time
    now = _time.time()
    # 新位置 + 老位置都扫一遍（老版本把 wav 丢在系统临时目录）
    for _root in (temp_root(), tempfile.gettempdir()):
        for p in glob.glob(os.path.join(_root, "tmp*.wav")):
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.remove(p)
            except OSError:
                continue


# 各阶段占总进度的权重，按 64 分钟 1080p 实测耗时估的
_W_START, _W_AUDIO, _W_VOL, _W_MOTION, _W_ASR, _W_THUMB = 0.02, 0.04, 0.06, 0.30, 0.49, 0.09


def _open_index_log(name: str):
    """每次分析写一份独立日志（logs/index_时间.log）。

    为什么要这个：64 分钟素材分析要 8 分钟，界面只有一个状态文字，
    中间"卡住"了根本没法判断卡在哪一步。日志带耗时，事后一眼看出。
    """
    try:
        import time as _time
        d = os.path.join(BASE_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "index_%s.log" % _time.strftime("%Y%m%d_%H%M%S"))
        fh = open(p, "w", encoding="utf-8")
        fh.write("# %s  %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), name))
        return fh
    except OSError:
        return None


class IndexCancelled(Exception):
    """用户中途停止分析。"""


def index_video(path: str, out_dir: str, model_size: str | None = "",
                models_dir: str | None = None, frame_step: float = 1.0,
                audio_stream: int | None = None, on_status=None,
                on_progress=None, should_stop=None) -> dict:
    import time as _time
    if model_size == "":
        model_size = asr_hints.configured_model(BASE_DIR)
    selected_asr_profile = asr_profile(model_size) if model_size else None
    _t0 = _time.time()
    _log = _open_index_log(os.path.basename(path))

    def st(msg):
        el = _time.time() - _t0
        stamp = "[%d:%02d]" % (int(el // 60), int(el % 60))
        if _log:
            try:
                _log.write("%s %s %s\n" % (_time.strftime("%H:%M:%S"), stamp, msg))
                _log.flush()
            except OSError:
                pass
        if on_status:
            on_status("%s %s" % (stamp, msg))

    _acc = [0.0]

    def pg(frac, base=None):
        """上报 0~1 的总体进度。frac=None 表示按 base 直接设定。"""
        if not on_progress:
            return
        if frac is None:
            _acc[0] = float(base or 0.0)
        else:
            _acc[0] = min(1.0, _acc[0] + float(frac))
        on_progress(_acc[0])

    def _stop_check(where=""):
        if should_stop and should_stop():
            st("已按要求停止（%s）" % where if where else "已按要求停止")
            raise IndexCancelled("用户停止")

    _cleanup_stale_temp()
    pg(None, 0.0)
    st(f"读取素材信息：{os.path.basename(path)}")
    _stop_check("开始")
    info = probe_streams(path)
    if info["duration"] <= 0:
        raise ValueError("读不出时长，可能不是有效的视频文件。")
    if not info["has_audio"]:
        raise ValueError("这个文件里没有任何音轨。")
    source_identity_at_start = media_identity.source_identity(path)

    streams = info["audio_streams"]
    # Premiere-style media indexing: decode each relevant source stream once,
    # then let VAD, ASR, the editor and the director read aligned PCM windows
    # from the same index directory.  If the cache directory itself is not
    # usable, keep the old WAV path as a compatibility fallback.
    if isinstance(audio_stream, int):
        _cache_tracks = [audio_stream]
    elif isinstance(audio_stream, (list, tuple)):
        _cache_tracks = [int(track) for track in audio_stream
                         if isinstance(track, int)]
    else:
        _cache_tracks = [int(stream["ordinal"]) for stream in streams]
    _audio_cache = None
    try:
        _audio_cache = audio_evidence_cache.AudioEvidenceCache(
            path, _cache_tracks, sample_rate=16000,
            ffmpeg=find_ffmpeg(), should_cancel=should_stop)
    except Exception as _cache_exc:  # noqa: BLE001
        st("共享音频索引不可用，回退临时 WAV：%s" % str(_cache_exc)[:80])
    # 逐轨探针：判出“哪条在说话”。这是后面所有处理的依据——
    # 说话相关的只用 speech 轨，音效相关的只用 other/dense 轨，
    # 互不干扰（子枫 2026-11-23：单轨改，效果只作用于它自己的轨）。
    _track_kinds = {}
    if len(streams) > 1 and audio_stream is None:
        try:
            st(f"检测到 {len(streams)} 条音轨，正在分辨哪条在说话…")
            _track_kinds = probe_tracks(
                path, [s["ordinal"] for s in streams], max_seconds=180,
                audio_cache=_audio_cache)
            # 可能有**多条**在说话（子枫的流程：3 和 4 都可能有人声）。
            # 所以这里只确定“主轨”（第一条说话轨），
            # 其余说话轨在下面转写阶段逐条处理，各自标自己的 track。
            _speech = sorted(t for t, v in _track_kinds.items()
                             if v.get("kind") == "speech")
            if _speech:
                audio_stream = _speech[0]
                st("说话在第 %d 条（其余轨：%s）"
                   % (audio_stream + 1,
                      "、".join("第%d条 %s" % (t + 1, v.get("kind"))
                               for t, v in sorted(_track_kinds.items())
                               if v.get("kind") != "speech") or "无"))
        except audio_evidence_cache.AudioEvidenceCancelled as _pk_err:
            if _audio_cache is not None:
                _audio_cache.close()
            raise IndexCancelled(str(_pk_err)) from _pk_err
        except Exception as _pk_err:  # noqa: BLE001
            st("逐轨探针跳过：%s" % str(_pk_err)[:60])
    if audio_stream is None:
        if len(streams) > 1:
            st(f"检测到 {len(streams)} 条音轨，正在自动挑说话那条…")
            try:
                audio_stream = pick_audio_stream(
                    path, streams, on_status=st, audio_cache=_audio_cache)
            except audio_evidence_cache.AudioEvidenceCancelled as _pick_exc:
                if _audio_cache is not None:
                    _audio_cache.close()
                raise IndexCancelled(str(_pick_exc)) from _pick_exc
        else:
            audio_stream = streams[0]["ordinal"]
    # audio_stream 可能是单条（int），也可能是勾了几条要混音（list）。
    # 以前这里直接拿 int 比，传 list 时永远匹配不上，状态栏会谎报「用音轨 1」，
    # 排错时被这条假信息带偏过。
    if isinstance(audio_stream, (list, tuple)):
        _ords = [t for t in audio_stream if isinstance(t, int)]
        used = next((s for s in streams if s["ordinal"] == (_ords[0] if _ords else 0)),
                    streams[0])
        st("使用 %s（混音：%s）"
           % (used["label"], "、".join("第%d条" % (t + 1) for t in _ords)))
    else:
        used = next((s for s in streams if s["ordinal"] == audio_stream), streams[0])
        st(f"使用 {used['label']}")

    os.makedirs(out_dir, exist_ok=True)
    thumbs_dir = os.path.join(out_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    out_dir = os.path.abspath(out_dir)

    # 只抽选中的那条音轨，后面音量分析和语音识别都用它，
    # 避免"分析的是麦克风、识别的是游戏声"这种错位
    pg(None, _W_START)
    st("抽取音轨…")
    wav = None
    _pool = None
    _motion_fut = None
    _visual_cache = None
    try:
        import video_frame_cache
        _visual_cache = video_frame_cache.VideoFrameCache(
            path, ffmpeg=find_ffmpeg(), should_cancel=should_stop)
    except Exception as _visual_cache_exc:  # noqa: BLE001
        st("共享画面代理不可用，将使用兼容抽帧：%s" %
           str(_visual_cache_exc)[:80])
    _analysis_stage_ok = False
    try:
        audio = None
        if _audio_cache is not None:
            try:
                if isinstance(audio_stream, (list, tuple)):
                    audio = _audio_cache.review_mix(
                        audio_stream, 0.0, float(info["duration"]))
                else:
                    audio = _audio_cache.review_samples(
                        int(audio_stream), 0.0, float(info["duration"]))
                st("共享音频索引已就绪（后续岗位直接复用，不再整轨解码）")
            except audio_evidence_cache.AudioEvidenceCancelled as exc:
                raise IndexCancelled(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                st("共享音频索引读取失败，回退临时 WAV：%s" % str(exc)[:80])
                _audio_cache.close()
                _audio_cache = None
        if audio is None:
            fd, wav = tempfile.mkstemp(suffix=".wav", dir=temp_root())
            os.close(fd)
            if not extract_audio_wav(path, wav, audio_stream):
                raise ValueError(f"抽不出音轨：{used['label']}")
            audio = read_wav_mono(wav)

        pg(None, _W_START + _W_AUDIO)
        st("分析音量，找说话段落和高光…")
        rms, hop = audio_energy(audio)
        db = (20 * np.log10(np.maximum(rms, 1e-6)) if rms.size
              else np.zeros(0, dtype=np.float32))
        runs = speech_runs(rms, hop)
        st(f"检测到 {len(runs)} 段说话")

        _stop_check("抽音轨后")
        pg(None, _W_START + _W_AUDIO + _W_VOL)
        st("抽样画面，算画面变化…")
        # 和语音识别并行：抽帧是读盘（机械盘上 27 GB 要几分钟），
        # 识别是显卡。串行做就是"先等盘、再等卡"，时间白搭一半。
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _pool = _TPE(max_workers=1)
        _motion_fut = _pool.submit(sample_motion, path, frame_step, 96,
                                   _visual_cache, True)

        text_segs = []
        segment_evidence = []
        _subtitle_alignment_blockers = []
        _subtitle_alignment_diagnostics = []
        sem = None
        scores = None
        if model_size:
            _asr_base = _W_START + _W_AUDIO + _W_VOL + _W_MOTION

            def _asr_pg(frac, _b=_asr_base):
                pg(None, _b + _W_ASR * max(0.0, min(1.0, frac)))

            # 音量在内存里用静态增益拉齐，不再让 ffmpeg 把整条音轨过一遍 loudnorm。
            # audio 就是前面做音量分析时已经读进来的那条 16k 单声道，直接复用。
            st(f"用 {model_size} 识别（音量已在内存里拉齐）…")
            audio_asr = normalize_for_asr(audio)
            # 热词只放人名/系列名与一小组通用游戏术语。领域说明单独走
            # initial_prompt，词形走 hotwords，避免 Whisper 把词表当上一句话续写。
            #
            # 为什么不再拼 700 字模组词库（v071 实测，33 分钟、large-v3、同一段混音）：
            #     空提示词             → 第一句 16.7 秒 ✓
            #     JARGON_PROMPT 104 字 → 第一句 16.7 秒 ✓
            #     731 字（原「asr_terms + 词库 700」）
            #                          → 开头被整段吃掉，模型把被截断后剩下的尾部
            #                            「沙块2x，金块2x…」当成上一句继续往下写，
            #                            0~45 秒变成复读，真话拖到 45.9 秒才出现
            # Whisper 的 prompt 只有 224 token，中文基本一字一 token，
            # 超了就从前面截断——留下的尾巴正好是词库里最像口头禅的那些。
            # 模组名的准确率改由「转写后词库纠错」（termbase.apply_to_segments）保证，
            # 那条路是确定性替换，比指望模型听对更稳。
            _asr_context = selected_asr_profile["initial_prompt"]
            _hotwords = selected_asr_profile["hotwords"]
            _selected_hotwords = _hotwords.split()
            st("转写领域说明 %d 字 + 热词 %d 个/%d 字（完整词截断）"
               % (len(_asr_context), len(_selected_hotwords), len(_hotwords)))
            _bsz = selected_asr_profile["batch_size"]
            _beam = selected_asr_profile["beam_size"]
            st("识别参数：%s / beam %d / %s" %
               (model_size, _beam, "批处理 %d" % _bsz if _bsz > 1 else "逐段"))
            text_segs = transcribe(audio_asr, model_size=model_size,
                                   models_dir=models_dir, on_status=st,
                                   initial_prompt=_asr_context,
                                   hotwords=_hotwords,
                                   beam_size=_beam,
                                   on_progress=_asr_pg, batch_size=_bsz,
                                   should_stop=should_stop,
                                   evidence_out=segment_evidence,
                                   source_audio_track=audio_stream)
            # 高概率不等于时间正确。遇到单字被拉长、词间数十秒空洞等
            # 不可能的对齐时，只重听对应 Whisper 父段，并强制关闭批处理。
            text_segs = repair_suspected_alignment(
                text_segs, audio_asr, model_size=model_size,
                models_dir=models_dir, on_status=st,
                should_stop=should_stop, evidence_out=segment_evidence,
                source_audio_track=audio_stream, beam_size=_beam,
                initial_prompt=_asr_context, hotwords=_hotwords,
                blockers_out=_subtitle_alignment_blockers)
            text_segs = repair_suspected_repetitions(
                text_segs, audio_asr, model_size=model_size,
                models_dir=models_dir, on_status=st,
                should_stop=should_stop, evidence_out=segment_evidence,
                source_audio_track=audio_stream, beam_size=_beam)
            text_segs, _subtitle_alignment_blockers = repair_uncovered_human_speech(
                text_segs, audio_asr, rms_windows=runs, model_size=model_size,
                models_dir=models_dir, on_status=st,
                should_stop=should_stop, evidence_out=segment_evidence,
                source_audio_track=audio_stream, beam_size=_beam,
                initial_prompt=_asr_context, hotwords=_hotwords,
                blockers_out=_subtitle_alignment_blockers,
                diagnostics_out=_subtitle_alignment_diagnostics)
            # Head recovery must run while the primary track is still paired
            # with its own evidence; doing it after interleaving other tracks
            # can attach the wrong track to simultaneous utterances.
            if text_segs:
                text_segs = fill_missing_head(
                    text_segs, audio_asr, model_size=model_size,
                    models_dir=models_dir, on_status=st,
                    should_stop=should_stop, evidence_out=segment_evidence,
                    source_audio_track=audio_stream, beam_size=_beam)
            # 逐轨转写：除了主轨，其余说话轨也要各转一遍，各自标自己的 track。
            # 子枫的流程里 3 和 4 都可能有人声，两个都要出文字。
            _selected_tracks = set(_audio_track_ids(audio_stream))
            _other_speech = [t for t in (_track_kinds or {})
                             if (_track_kinds.get(t) or {}).get("kind") == "speech"
                              and t not in _selected_tracks]
            for _t2 in _other_speech:
                _wav2 = None
                try:
                    st("其余说话轨：转第 %d 条…" % (_t2 + 1))
                    if _audio_cache is not None:
                        _a2 = _audio_cache.review_samples(
                            _t2, 0.0, float(info["duration"]))
                    else:
                        _wav2 = os.path.join(tempfile.gettempdir(),
                                             "asr_track_%d.wav" % _t2)
                        if not extract_audio_wav(path, _wav2, stream=_t2):
                            continue
                        _a2 = read_wav_mono(_wav2)
                    if _a2.size < 16000:
                        continue
                    _ev2 = []
                    _a2_asr = normalize_for_asr(_a2)
                    _segs2 = transcribe(
                        _a2_asr, model_size=model_size,
                        models_dir=models_dir, on_status=st,
                        initial_prompt=_asr_context, hotwords=_hotwords,
                        beam_size=_beam,
                        batch_size=_bsz, should_stop=should_stop,
                        evidence_out=_ev2, source_audio_track=_t2)
                    _segs2 = repair_suspected_alignment(
                        _segs2, _a2_asr,
                        model_size=model_size, models_dir=models_dir,
                        on_status=st, should_stop=should_stop,
                        evidence_out=_ev2, source_audio_track=_t2,
                        beam_size=_beam, initial_prompt=_asr_context,
                        hotwords=_hotwords,
                        blockers_out=_subtitle_alignment_blockers)
                    _segs2 = repair_suspected_repetitions(
                        _segs2, _a2_asr,
                        model_size=model_size, models_dir=models_dir,
                        on_status=st, should_stop=should_stop,
                        evidence_out=_ev2, source_audio_track=_t2,
                        beam_size=_beam)
                    _rms2, _hop2 = audio_energy(_a2)
                    _runs2 = speech_runs(_rms2, _hop2)
                    _segs2, _subtitle_alignment_blockers = repair_uncovered_human_speech(
                        _segs2, _a2_asr, rms_windows=_runs2,
                        model_size=model_size, models_dir=models_dir,
                        on_status=st, should_stop=should_stop,
                        evidence_out=_ev2, source_audio_track=_t2,
                        beam_size=_beam, initial_prompt=_asr_context,
                        hotwords=_hotwords,
                        blockers_out=_subtitle_alignment_blockers,
                        diagnostics_out=_subtitle_alignment_diagnostics)
                    if _segs2:
                        _segs2 = fill_missing_head(
                            _segs2, _a2_asr, model_size=model_size,
                            models_dir=models_dir, on_status=st,
                            should_stop=should_stop, evidence_out=_ev2,
                            source_audio_track=_t2, beam_size=_beam)
                    if _segs2:
                        # Merge pairs as pairs. Sorting segments separately from
                        # evidence used to cross-wire simultaneous audio tracks.
                        _merged_segments = list(text_segs or []) + [
                            (float(a), float(b), str(t)) for a, b, t in _segs2]
                        _merged_evidence = (list(segment_evidence or [])
                                            + list(_ev2))
                        text_segs, segment_evidence, _pair_issues = \
                            sort_segment_evidence_pairs(
                                _merged_segments, _merged_evidence)
                        _subtitle_alignment_blockers.extend(_pair_issues)
                        st("第 %d 条转出 %d 句，合并后共 %d 句"
                           % (_t2 + 1, len(_segs2), len(text_segs)))
                except audio_evidence_cache.AudioEvidenceCancelled as _t2_err:
                    raise IndexCancelled(str(_t2_err)) from _t2_err
                except Exception as _t2_err:  # noqa: BLE001
                    st("第 %d 条转写跳过：%s" % (_t2 + 1, str(_t2_err)[:60]))
                finally:
                    if _wav2:
                        try:
                            os.remove(_wav2)
                        except OSError:
                            pass
            if text_segs:
                st(f"识别出 {len(text_segs)} 句台词")
                _fixed, _nfix = fix_names(text_segs)
                if _nfix:
                    text_segs = _fixed
                    st("人名/系列名纠正 %d 处" % _nfix)
                try:
                    import termbase
                    _fixed, _n = termbase.apply_to_segments(
                        [list(x) for x in text_segs], on_log=st)
                    if _n:
                        text_segs = [(a, b, t) for a, b, t in _fixed]
                except Exception as exc:  # noqa: BLE001
                    st("词库纠错跳过：%s" % str(exc)[:60])
                # 拼音纠错：词库是"整包全量物品名"时，同音错字（过冲/过充）改成正确写法
                try:
                    import terms as _terms
                    _st = _terms.load()
                    if _st.get("terms"):
                        text_segs, _n, _ex = _terms.correct_segments(
                            text_segs, on_log=st)
                        if _n:
                            st("拼音纠错：改了 %d 处（词典 %d 词）"
                               % (_n, len(_st["terms"])))
                            for _a, _b in _ex[:5]:
                                st("    %s → %s" % (_a, _b))
                    else:
                        st("拼音纠错：没有全量词典（放 kb\\术语_全量.txt 后生效）")
                except Exception as exc:  # noqa: BLE001
                    st("拼音纠错跳过：%s" % str(exc)[:60])
                # 索引阶段只应用用户明确维护的术语规则。旧版把全片每一句
                # 都先交给模型，批量输出一旦解析失败还会逐句重试；一部长片
                # 会产生数百次请求，而安全门最终又不会接受普通同音猜改。
                # 真正需要上下文的字幕在交付前由 subtitle_quality 按声学
                # 置信度筛选，只把疑点句交给用户当前选择的模型复核。
                try:
                    import proofread as _pf
                    _items = [(str(_k), str(_s[2]).strip())
                              for _k, _s in enumerate(text_segs)
                              if len(_s) > 2 and str(_s[2]).strip()]
                    if _items:
                        _pairs, _table = _pf.load_terms()
                        _known = _pf.known_pairs_from_table(_table)
                        _changes = {
                            _cid: _fixed for _cid, _text in _items
                            for _fixed in [_pf._canonical_terms(_text, _known)]
                            if _fixed != _text
                        }
                        if _changes:
                            text_segs = [
                                (s[0], s[1], _changes.get(str(k), s[2]))
                                if len(s) > 2 else s
                                for k, s in enumerate(text_segs)]
                            st("术语规则校对：采用 %d 处明确修正"
                               % len(_changes))
                            for _k, _v in list(_changes.items())[:6]:
                                st("    [%s] %s" % (_k, str(_v)[:46]))
                        else:
                            st("术语规则校对：没有发现明确错写")
                        st("字幕上下文校对：交付前仅复核低置信疑点句")
                except Exception as exc:  # noqa: BLE001
                    st("术语规则校对跳过：%s" % str(exc)[:80])
                try:
                    import proofread as _pf
                    _pf.kill_stale_servers(on_log=st)
                except Exception:  # noqa: BLE001
                    pass
                st("语义分析：判断段落与类型（本地模型，约十几秒）…")
                sem = semantic_pass(text_segs, st, should_stop=should_stop)
                if sem:
                    from collections import Counter as _C
                    cnt = _C(k for k, _ in sem)
                    runs = paragraph_runs(text_segs, sem)
                    st("语义分析完成：%s，划分为 %d 个段落"
                       % ("、".join("%s %d" % (k, v) for k, v in cnt.items()),
                          len(runs)))
                else:
                    st("语义分析没跑成，退回按音量切分")
            if text_segs:
                scores = score_sentences(text_segs, st)
                if scores and not llm_gateway.api_enabled():
                    st("剪辑模型打分完成（%d 句），卡片会带上分数" % len(scores))
        _stop_check("识别后")
        _analysis_stage_ok = True
    finally:
        try:
            if wav:
                os.remove(wav)
        except (OSError, TypeError):
            pass
        if _audio_cache is not None:
            _audio_cache.close()
        if not _analysis_stage_ok and _pool is not None:
            try:
                if _motion_fut is not None:
                    _motion_fut.cancel()
                _pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass

    # 取画面分析结果（如果识别比抽帧快，这里会等一下）
    try:
        times, diffs, _visual_cache_audit = _motion_fut.result()
    except Exception as exc:  # noqa: BLE001
        try:
            import video_frame_cache
            if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                raise IndexCancelled(str(exc)) from exc
        except ImportError:
            pass
        if should_stop and should_stop():
            raise IndexCancelled("画面代理缓存期间用户停止") from exc
        st("画面分析失败（不影响其余）：%s" % str(exc)[:60])
        times, diffs, _visual_cache_audit = [], [], {
            "reused": False, "error": "%s: %s" %
            (type(exc).__name__, str(exc)[:160])}
    finally:
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
    pg(None, _W_START + _W_AUDIO + _W_VOL + _W_MOTION)
    if times:
        st("画面分析完成：%d 个采样点" % len(times))
    # 16 维剪辑模型需要画面变化和音量特征。ASR 阶段只能先延后，
    # 现在 motion future 已完成，用将要写入 index.json 的同一份数据正式打分。
    if text_segs and not scores:
        _score_data = {
            "segments": [[a, b, t] for a, b, t in text_segs],
            "duration": float(info.get("duration") or 0),
            "analysis": {
                "diffs": [float(x) for x in diffs],
                "audio_1s": _fold_db(db, hop),
            },
        }
        scores = score_sentences(text_segs, st, feature_data=_score_data)
        if scores and not llm_gateway.api_enabled():
            st("剪辑模型打分完成（%d 句，含画面/音量特征）" % len(scores))

    # 纠错只修改最终显示文字；原始词时间和原始 ASR 文本仍保存在证据里。
    # 这里重新按时间对齐，保证后续边界精修器能稳定引用 s000001 这类 ID。
    text_segs, segment_evidence, _pair_issues = sort_segment_evidence_pairs(
        text_segs, segment_evidence)
    _subtitle_alignment_blockers.extend(_pair_issues)
    segment_evidence = align_segment_evidence(
        text_segs, segment_evidence, audio_stream)

    # Subtitle cues use a separate, conservative waveform pass.  Work only on
    # the already-selected PCM held in memory, keep every cue and word anchor,
    # and persist provenance so stale/wrong-track refinements are ignored.
    _segments_refined = []
    _segments_refined_metadata = {}
    _segments_refined_version = None
    if text_segs and segment_evidence and audio is not None:
        try:
            import audiofeat_bounds as _caption_bounds
            _segments_refined = _caption_bounds.refine_caption_segments(
                audio, text_segs, segment_evidence, audio_stream, sr=16000)
            if len(_segments_refined) != len(text_segs):
                raise RuntimeError("精修没有保持逐句字幕条数")
            _segments_refined_metadata = _caption_bounds.build_refinement_metadata(
                source_identity_at_start, audio_stream, selected_asr_profile,
                segment_evidence)
            _segments_refined_version = _caption_bounds.CAPTION_REFINEMENT_VERSION
            _moved_caption_edges = sum(
                1 for refined, segment in zip(_segments_refined, text_segs)
                if (abs(float(refined[0]) - float(segment[0])) > 0.01
                    or abs(float(refined[1]) - float(segment[1])) > 0.01))
            st("字幕波形边缘微调：%d/%d 句有安全微调" % (
                _moved_caption_edges, len(text_segs)))
        except Exception as _caption_exc:  # noqa: BLE001
            _segments_refined = []
            _segments_refined_metadata = {}
            st("字幕波形边缘微调跳过：%s" % str(_caption_exc)[:100])

    _an = analysis_settings()
    _spans = build_cards(info["duration"], runs, text_segs, times, diffs,
                         min_card=max(2.0, _an["card_min"] * 0.4),
                         max_card=max(12.0, _an["card_max"] + _an["card_tol"]),
                         mode=_an.get("card_mode", "paragraph"))
    # build_cards 返回的是 (start, end) 区间，这里转成卡片字典
    cards = [{"start": round(a, 2), "end": round(b, 2)} for a, b in _spans]

    # 卡片文案 + 画面/音量指标（打标签和排序要用）
    def _motion_of(a, b):
        if len(diffs) == 0 or len(times) == 0:
            return 0.0
        vals = [d for t, d in zip(times, diffs) if a <= t <= b]
        return (sum(vals) / len(vals)) if vals else 0.0

    _sem = sem or []
    for c in cards:
        _idx = [i for i, (x, y, t) in enumerate(text_segs)
                if x >= c["start"] - 0.05 and y <= c["end"] + 0.05]
        c["text"] = " ".join(text_segs[i][2] for i in _idx)
        kind = "speech"
        if _idx and _idx[0] < len(_sem):
            kind = _sem[_idx[0]][0]
        txt = c.get("text") or ""
        cps = len(txt) / max(0.5, c["end"] - c["start"])
        emo = "普通"
        if kind == "laugh":
            emo = "笑声"
        elif kind in ("noise", "filler"):
            emo = "杂音"
        elif any(w in txt for w in ("卧槽", "我去", "竟然", "居然", "天啊",
                                    "不可能", "什么鬼")):
            emo = "惊讶"
        elif cps >= 5.0:
            emo = "激动"
        c["emotion"] = emo
        c["motion"] = round(_motion_of(c["start"], c["end"]), 3)
        _ev = [row for row in segment_evidence
               if float(row.get("end", 0.0)) > c["start"]
               and float(row.get("start", 0.0)) < c["end"]]
        _words = [word for row in _ev for word in (row.get("words") or [])]
        _word_starts = [float(w["start"]) for w in _words
                        if _float_or_none(w.get("start")) is not None]
        _word_ends = [float(w["end"]) for w in _words
                      if _float_or_none(w.get("end")) is not None]
        _tracks = sorted({int(track) for row in _ev
                          for track in (row.get("source_audio_tracks") or [])})
        c["speech_evidence"] = {
            "segment_ids": [row["segment_id"] for row in _ev],
            "word_start": min(_word_starts) if _word_starts else None,
            "word_end": max(_word_ends) if _word_ends else None,
            "source_audio_tracks": _tracks,
        }

    # 缩略图可选：一句一张卡时有两千多张，逐张抓帧在机械盘上要 9 分钟。
    # 默认不生成——挑片主要看台词，需要看图双击就能预览。
    _want_thumbs = True
    try:
        import json as _json
        _sp = os.path.join(BASE_DIR, "conf", "settings.json")
        if os.path.isfile(_sp):
            # 默认关闭：没有配置项时按“不生成”处理
            _want_thumbs = str(_json.load(open(_sp, encoding="utf-8"))
                               .get("thumbs", "0")) not in ("0", "false", "False", "")
    except Exception:  # noqa: BLE001
        pass

    st("生成缩略图…" if _want_thumbs else "跳过缩略图（设置里可以打开）")
    for i, c in enumerate(cards, 1):
        c["id"] = f"c{i:04d}"
        c["duration"] = round(c["end"] - c["start"], 2)
        # 音量高光：这一段最响的时刻在全片排进前 10%
        c["peak"] = round(peak_score(db, c["start"], c["end"], hop), 3)
        c["highlight"] = c["peak"] >= 0.90
        c["thumb"] = ""

    # 缩略图改成多路并行：每张图都要单独起一次 ffmpeg（打开 10 GB 容器 + seek），
    # 串行 129 次要 32 秒；8 路并行基本能压到 5 秒以内。
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(card):
        if not _want_thumbs:
            return card, False
        target = card["start"] + min(1.5, (card["end"] - card["start"]) / 2)
        jpg = os.path.join(thumbs_dir, f"{card['id']}.jpg")
        ok = grab_thumbnail(path, target, jpg, frame_cache=_visual_cache)
        if not ok and target > 1.0:
            ok = grab_thumbnail(path, max(0.0, target - 1.0), jpg,
                                frame_cache=_visual_cache)
        return card, ok

    failed = 0
    total = len(cards)
    if _want_thumbs:
        workers = min(8, max(2, (os.cpu_count() or 4) // 2))
        finished = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_one, c) for c in cards]
            for fut in as_completed(futures):
                card, ok = fut.result()
                if ok:
                    card["thumb"] = f"thumbs/{card['id']}.jpg"
                else:
                    failed += 1
                finished += 1
                if finished % 10 == 0 or finished == total:
                    st(f"缩略图 {finished}/{total}")
                    pg(None, _W_START + _W_AUDIO + _W_VOL + _W_MOTION + _W_ASR
                       + _W_THUMB * (finished / max(1, total)))
    if failed:
        st(f"有 {failed} 张缩略图没取到（不影响挑选，卡片可以点开预览）")

    cards = tag_cards(cards)
    try:
        tagger.apply_to_cards(cards)     # 起标题 + 分类
    except Exception:  # noqa: BLE001
        for c in cards:
            c.setdefault("title", (c.get("text") or "").strip()[:16])
            c.setdefault("cats", [])
    result = {
        "schema": {
            "name": INDEX_SCHEMA_NAME,
            "version": INDEX_SCHEMA_VERSION,
            "segment_evidence_version": SEGMENT_EVIDENCE_VERSION,
            "legacy_segments_preserved": True,
        },
        "source": os.path.abspath(path),
        "ai_backend_signature": llm_gateway.backend_signature(),
        "source_identity": source_identity_at_start,
        "name": os.path.splitext(os.path.basename(path))[0],
        "duration": round(info["duration"], 2),
        "duration_exact": float(info["duration"]),
        "width": info["width"], "height": info["height"], "fps": info["fps"],
        "video_codec": info["video_codec"],
        "audio_streams": streams,
        "audio_stream_used": audio_stream,
        "track_kinds": _track_kinds,
        "speech_tracks": [t for t, v in (_track_kinds or {}).items()
                          if v.get("kind") == "speech"],
        "asr_requested": bool(model_size),
        "asr_profile": selected_asr_profile,
        "has_speech": bool(text_segs),
        "thumb_failures": failed,
        "index_dir": out_dir,
        # 分析数据留给自动剪辑用：不存的话，autocut 还得把整部片子再读一遍
        # 逐句时间轴：字幕功能要用，卡片粒度太粗（几十秒一句）
        "segments": [[round(a, 2), round(b, 2), t] for a, b, t in text_segs],
        "segments_refined": [[float(a), float(b)]
                             for a, b in _segments_refined],
        "segments_refined_version": (
            _segments_refined_version if _segments_refined else None),
        "segments_refined_metadata": _segments_refined_metadata,
        # 边界精修/导演听觉审核使用：未舍入时间、逐词置信度、原始 ASR 范围、
        # 来源音轨。它与旧 segments 平行，segment_id 可由卡片稳定引用。
        "segment_evidence": segment_evidence,
        # Global defects include voiced windows for which ASR returned no
        # segment at all; they cannot be represented by a per-segment flag.
        "subtitle_alignment_blockers": list({
            json.dumps(row, ensure_ascii=False, sort_keys=True): row
            for row in _subtitle_alignment_blockers
            if isinstance(row, dict)
        }.values()),
        "subtitle_alignment_diagnostics": _subtitle_alignment_diagnostics,
        # 语义标注：类型（说话/笑声/杂音）与段落边界，剪辑和字幕都用它
        "semantic": ([{"kind": k, "para_break": bool(pb)} for k, pb in sem]
                     if text_segs and sem else []),
        # 每句对应的画面变化与音量：训练和打分都要用
        "seg_feat": seg_feature_rows(text_segs, diffs, _fold_db(db, hop),
                                      info["duration"]),
        # 每句「说完了没有」的完整度：剪辑点不能落在没说完的地方
        "complete": (completeness_pair(text_segs) if text_segs else {}),
        # 本地模型分；API 模式仅保留兼容旧字段的中立占位，不是 API 评价。
        "scores": (scores if text_segs and 'scores' in dir() and scores else []),
        "scores_source": ("api_neutral" if llm_gateway.api_enabled() else "local"),
        "analysis": {
            "step": frame_step,
            "times": [round(t, 2) for t in times],
            "diffs": [round(d, 3) for d in diffs],
            "runs": [[round(a, 2), round(b, 2)] for a, b in runs],
            "audio_1s": _fold_db(db, hop),
            "visual_cache": _visual_cache_audit,
        },
        "cards": cards,
    }
    # 中立转写：逐词概率 → 正文 + 不确定清单（E:\本地大模型 那套接进来）。
    # 只在有逐词证据时生成；老索引/没跑 ASR 时不写这个键。
    if segment_evidence:
        try:
            import neutral_index
            neutral = neutral_index.build(result)
            if neutral:
                result["neutral"] = neutral
                if _log:
                    _log.write("中立转写：%d 字，%d 处没把握（候选 %d 个）\n"
                               % (len(neutral.get("text") or ""),
                                  len(neutral.get("concerns") or []),
                                  sum(len(c.get("candidates") or [])
                                      for c in neutral.get("concerns") or [])))
        except Exception as _exc:  # noqa: BLE001
            if _log:
                _log.write("中立转写生成失败：%s\n" % _exc)
    # A recording modified during a long analysis must never publish a mixed
    # index (early frames/audio from one version, later samples from another).
    current_source_identity = media_identity.source_identity(path)
    if not media_identity.matches(source_identity_at_start,
                                  current_source_identity):
        raise RuntimeError("分析期间素材发生变化；已拒绝保存过期索引，请重新分析。")
    _index_path = os.path.join(out_dir, "index.json")
    with open(_index_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # 分析阶段保持中立：只解析、切片、打标签和提取特征。
    # 剧本、力度取舍、导演初剪与审片全部由后续“智能剪辑”阶段执行。
    st(f"完成：{len(cards)} 张卡片，索引在 {out_dir}")
    if _log:
        try:
            _log.write("\n合计 %.1f 分钟\n" % ((_time.time() - _t0) / 60))
            _log.close()
        except OSError:
            pass
    return result


def collect_videos(target: str) -> list[str]:
    exts = {".mp4", ".mov", ".mkv", ".flv", ".ts", ".avi", ".m4v", ".webm"}
    if os.path.isfile(target):
        return [target]
    out = []
    for root, _dirs, files in os.walk(target):
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                out.append(os.path.join(root, f))
    return sorted(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="把长素材拆成可挑选的卡片")
    ap.add_argument("target", help="视频文件或素材文件夹")
    ap.add_argument("--out", default=None, help="索引输出目录")
    ap.add_argument("--model", default=None,
                    help="语音模型；默认沿用软件识别档，未设置时为 large-v3")
    ap.add_argument("--models-dir", default=os.path.join(BASE_DIR, "models"), help="模型缓存目录")
    ap.add_argument("--step", type=float, default=1.0, help="抽帧间隔（秒）")
    ap.add_argument("--no-asr", action="store_true", help="不转写说话内容（快，只做画面和音量）")
    ap.add_argument("--track", type=int, default=None, help="用第几条音轨（从 1 开始），默认自动挑")
    args = ap.parse_args(argv)

    videos = collect_videos(args.target)
    if not videos:
        print("没找到视频文件。")
        return 2

    for v in videos:
        out = args.out or os.path.join(os.path.dirname(os.path.abspath(v)), "_索引",
                                       os.path.splitext(os.path.basename(v))[0])
        print(f"\n=== {os.path.basename(v)} ===")
        res = index_video(
            v, out, model_size=None if args.no_asr else
                              (args.model or asr_hints.configured_model(BASE_DIR)),
            models_dir=args.models_dir,
            frame_step=args.step,
            audio_stream=(args.track - 1) if args.track else None,
            on_status=lambda m: print("[索引]", m),
        )
        n_hi = sum(1 for c in res["cards"] if c["highlight"])
        print(f"用了 {res.get('audio_stream_used', 0) + 1} 号音轨，"
              f"卡片 {len(res['cards'])} 张，其中音量高光 {n_hi} 张，"
              f"总时长 {tc(res['duration'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

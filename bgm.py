# -*- coding: utf-8 -*-
"""自动配 BGM：从曲库里挑一首、按波形截一段合适的、循环铺满成片，再压到人声下面。

为什么这么做：
  · 直接整首循环最容易踩的坑是「前奏是淡入的、尾巴是渐弱的」，
    循环几次后成片里会出现周期性的一静一响。所以先按波形找「平稳段」
  · 循环接缝必须交叉淡化，否则每 2 分钟「咔」一下
  · BGM 音量不能用固定值——人说话时要让路，所以用人声做侧链压缩（ducking）

流程：
  1. 扫描用户在“背景音乐”设置中选择的曲库
  2. 解码成 16k 单声道算每秒 RMS，挑「最平稳、不空、不太靠头尾」的一段
  3. 按选定段解码成 48k 立体声，用 numpy 做交叉淡化循环，铺满成片时长
  4. 归一化到目标电平，写临时 wav
  5. 一次 ffmpeg：视频流复制（不重编码），音频走 sidechaincompress 混音

用法：
    python bgm.py --list                      # 看曲库
    python bgm.py --pick 成片.mp4              # 只报告会选哪首、哪一段
    python bgm.py 成片.mp4 --out 带bgm.mp4     # 真混
"""

from __future__ import annotations

try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass


import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import wave

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import index as indexer  # noqa: E402
from memory_config import app_home  # noqa: E402

APP_HOME = app_home()

CONF_PATH = os.path.join(APP_HOME, "conf", "bgm.json")
EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma")

DEFAULT_CONF = {
    # 空 = 还没设：安装器里那个「BGM 曲库」选一次就行（设置窗口能改）
    "folder": "",
    "enabled": "1",
    # BGM 在「没人说话」时的目标电平（dBFS RMS）。人声一般在 -20~-16，
    # 这里低 10 dB 左右，属于"垫着"的程度。
    "level_db": "-22",
    # 有人说话时再压多少（侧链压缩的力度）。0 = 不做闪避，全程固定电平。
    "duck_db": "0",
    "dip_db": "4.0",
    "sections": "1",
    "section_min_min": "1.0",
    "section_max_min": "3.0",
    "dip_sec": "1.2",
    # 1 = 音量拉齐：不同曲子按响度(LUFS)对齐 + 曲内起伏拉平
    "loudness_norm": "1",
    "attack_ms": "30",
    "release_ms": "600",
    # 单段最长多少秒（太长循环次数少、但可能包含曲子本身的结构变化）
    "seg_len": "150",
    "xfade": "2.0",
    "fade_in": "1.5",
    "fade_out": "4.0",
    "seed": "",
}


def load_conf(path=None):
    cfg = dict(DEFAULT_CONF)
    p = path or CONF_PATH
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                cfg.update({k: str(v) for k, v in user.items()})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_default_conf(path=None):
    p = path or CONF_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONF, fh, ensure_ascii=False, indent=2)
    return p


def save_conf(patch, path=None):
    """只改传进来的那几项，其余保持原样（界面上换曲库目录用）。"""
    p = path or CONF_PATH
    cfg = load_conf(p)
    for k, v in (patch or {}).items():
        cfg[k] = str(v)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg


def list_tracks(folder=None):
    cfg = load_conf()
    folder = folder or cfg["folder"]
    if not os.path.isdir(folder):
        return []
    out = []
    for root, _d, files in os.walk(folder):
        for f in sorted(files):
            if f.lower().endswith(EXTS):
                out.append(os.path.join(root, f))
    return out


def _decode(path, seconds=0.0, start=0.0, sr=16000, channels=1):
    """解码成 numpy float32。seconds=0 表示整首。"""
    ff = indexer.find_ffmpeg()
    fd, wav = tempfile.mkstemp(suffix=".wav", dir=indexer.temp_root())
    os.close(fd)
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", "%.3f" % start]
    if seconds:
        cmd += ["-t", "%.3f" % seconds]
    cmd += ["-i", path, "-vn", "-ac", str(channels), "-ar", str(sr),
            "-c:a", "pcm_s16le", wav]
    try:
        r = subprocess.run(cmd, capture_output=True,
                           creationflags=indexer.CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        with wave.open(wav, "rb") as w:
            raw = w.readframes(w.getnframes())
            ch = w.getnchannels()
        if not raw:
            return None
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        return a
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def analyze(path):
    """整首解码 → 每秒 RMS(dB) 曲线 + 时长。"""
    a = _decode(path, sr=16000)
    if a is None or a.size < 16000:
        return None
    per = 16000
    n = len(a) // per
    if n < 3:
        return None
    rms = np.array([float(np.sqrt(np.mean(a[i * per:(i + 1) * per] ** 2)))
                    for i in range(n)], dtype=np.float32)
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    # 曲线平滑一下，别被单个爆点带跑
    if n >= 3:
        db = np.convolve(db, np.ones(3) / 3.0, mode="same")
    return {"path": path, "duration": len(a) / 16000.0, "db": db}


def best_window(info, seg_len=150.0):
    """在 dB 曲线上找最「平稳、不空、不靠头尾」的一段。"""
    db = info["db"]
    n = len(db)
    L = int(min(seg_len, max(20, n * 0.9)))
    if n <= L + 2:
        return 0.0, float(n), 0.0
    # 头尾各留 6%，避开淡入淡出
    margin = max(2, int(n * 0.06))
    best = None
    step = max(1, (n - L) // 60 or 1)
    for s in range(margin, max(margin + 1, n - L - margin + 1), step):
        w = db[s:s + L]
        steady = float(np.std(w))
        silent = float((w < -55).mean())
        loud = -float(np.percentile(w, 50))       # 越小越响
        score = -(1.6 * steady + 6.0 * silent) - 0.15 * loud
        if best is None or score > best[0]:
            best = (score, s, L, steady, silent)
    if best is None:
        return 0.0, float(min(seg_len, n)), 0.0
    _sc, s, L, steady, silent = best
    return float(s), float(L), round(steady, 2)


def pick_track(tracks, seed=""):
    if not tracks:
        return None
    rnd = random.Random(seed or "bgm")
    return rnd.choice(tracks)


LUFS_RMS_OFFSET = 2.6      # 标定值：让 -22 档的听感与原来 RMS 档一致（实测抬升 +3.85dB）


def _write_wav(path: str, seg, sr: int) -> None:
    x = np.asarray(seg, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def measure_lufs(wav_path: str):
    """用 ffmpeg loudnorm 量响度（EBU R128 积分响度，LUFS）。

    量不出来返回 None，调用方退回 RMS 归一。
    """
    import re as _re
    ff = indexer.find_ffmpeg()
    try:
        r = subprocess.run(
            [ff, "-hide_banner", "-nostats", "-i", wav_path,
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=indexer.CREATE_NO_WINDOW)
        m = _re.search('"input_i"\\s*:\\s*"([-\\d.]+)"', r.stderr or "")
        return float(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def even_out(seg, sr, level_db, window=2.0, strength=0.7, max_db=12.0):
    """音量均衡：把曲子自身的起伏拉平（慢速 AGC），再整体归到目标电平。

    为什么不只做一个静态增益：一首曲子本身有前奏、副歌、间奏，
    电平能差 6~10 dB。只乘一个系数，副歌就盖住人声、间奏又听不见了。
    这里按 2 秒窗口算 RMS，把每个窗口往目标推（strength 控制力度），
    再把增益曲线平滑一遍避免"抽气"，最后整体归到 level_db。
    """
    x = np.asarray(seg, dtype=np.float32)
    n = len(x)
    if n == 0:
        return x
    mono = x.mean(axis=1) if x.ndim == 2 else x
    hop = max(1, int(0.25 * sr))
    frames = max(1, n // hop)
    rms = np.empty(frames, dtype=np.float64)
    for i in range(frames):
        part = mono[i * hop:(i + 1) * hop]
        rms[i] = float(np.sqrt(np.mean(part ** 2))) if part.size else 0.0
    rms = np.maximum(rms, 1e-6)
    k = max(1, int(window / 0.25))
    kernel = np.ones(k, dtype=np.float64) / k
    smooth = np.convolve(rms, kernel, mode="same")
    smooth = np.maximum(smooth, 1e-6)
    target = 10 ** (float(level_db) / 20.0)
    gain = np.clip((target / smooth) ** float(strength),
                   10 ** (-max_db / 20.0), 10 ** (max_db / 20.0))
    gs = np.convolve(gain, kernel, mode="same")
    pos = np.arange(n, dtype=np.float64) / hop
    g = np.interp(pos, np.arange(len(gs), dtype=np.float64), gs)
    y = x * (g[:, None] if x.ndim == 2 else g)
    # 只做相对拉平：整体电平不变，绝对电平交给响度归一
    base = float(np.sqrt(np.mean(x ** 2))) or 1e-6
    cur = float(np.sqrt(np.mean(y ** 2))) or 1e-6
    y = y * (base / cur)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def _frame_db(x, sr, hop_ms=20, win_ms=60):
    """按帧算能量（dB），返回 (时间数组, dB 数组)。"""
    hop = max(1, int(sr * hop_ms / 1000))
    win = max(hop + 1, int(sr * win_ms / 1000))
    n = max(0, 1 + (len(x) - win) // hop)
    if n <= 0:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = x[idx]
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    return np.arange(n) * hop / float(sr), 20 * np.log10(np.maximum(rms, 1e-6))


def snap_segment_to_valleys(seg, sr, head_frac=0.15, tail_frac=0.25, min_keep=0.5):
    """把一段音频的"进场点"和"收尾点"挪到附近最安静的位置。

    返回 (新段, 进了多少秒, 收了多少秒)。
    head：只看开头 head_frac 那一段，往后挪到谷底（宁可晚进，不从重音上切进来）
    tail：只看结尾 tail_frac 那一段，往前挪到谷底（宁可早收，不在重音上切走）
    """
    x = np.asarray(seg)
    n = len(x)
    if n < sr:                          # 不到 1 秒就别折腾
        return x, 0.0, 0.0
    tt, db = _frame_db(x.mean(axis=1) if x.ndim == 2 else x, sr)
    if db.size < 8:
        return x, 0.0, 0.0
    # 平滑一下，避免抓到单个毛刺
    k = 3
    kern = np.ones(k) / k
    sm = np.convolve(db, kern, mode="same")

    head_end = max(2, int(len(sm) * head_frac))
    tail_start = min(len(sm) - 2, int(len(sm) * (1.0 - tail_frac)))
    cut_in = int(np.argmin(sm[:head_end]))
    cut_out = tail_start + int(np.argmin(sm[tail_start:]))
    # 限制改动幅度，别把整段削没了
    t_in = tt[cut_in] if cut_in > 0 else 0.0
    t_out = tt[cut_out] if cut_out < len(tt) else n / float(sr)
    if t_out - t_in < min_keep * n / float(sr):
        return x, 0.0, 0.0
    a = int(t_in * sr)
    b = int(t_out * sr)
    return x[a:b], t_in, (n - b) / float(sr)

def apply_dip(seg, sr, times, dip_db=4.0, dip_sec=1.2):
    """在指定时间点给音频加"轻压一下"（升余弦凹陷）。"""
    x = np.asarray(seg, dtype=np.float32)
    if not times or dip_db <= 0:
        return x
    n = len(x)
    env = np.ones(n, dtype=np.float32)
    half = max(1, int(dip_sec * sr / 2.0))
    depth = float(10 ** (-abs(dip_db) / 20.0))
    for t in times:
        c = int(float(t) * sr)
        if c < 0 or c >= n:
            continue
        lo, hi = max(0, c - half), min(n, c + half)
        if hi <= lo:
            continue
        w = np.linspace(0.0, 2.0 * np.pi, hi - lo, dtype=np.float32)
        dip = 1.0 - (1.0 - depth) * (0.5 - 0.5 * np.cos(w))   # 中间最深、两端回到 1
        env[lo:hi] = np.minimum(env[lo:hi], dip)
    return (x * (env[:, None] if x.ndim == 2 else env)).astype(np.float32)

def _calibrate_wav_level(path, sr, want_db, tolerance=0.3, max_gain_db=12.0):
    """把整段 wav 的 RMS 校到 want_db（含淡入淡出后的真实电平）。

    为什么必须在写完之后做：淡入 1.5s、淡出 4s 会把整段 RMS 拉低 1~3.5dB，
    写盘之前算出来的都偏。用户要的是"设 -40 就是 -40"。
    """
    try:
        with wave.open(path, "rb") as handle:
            frames = handle.readframes(handle.getnframes())
            channels = handle.getnchannels()
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if data.size == 0:
            return None
        rms = float(np.sqrt(np.mean(data ** 2))) or 1e-9
        current = 20.0 * np.log10(rms)
        if abs(current - want_db) <= tolerance:
            return current
        gain_db = max(-max_gain_db, min(max_gain_db, float(want_db) - current))
        data = np.clip(data * (10 ** (gain_db / 20.0)), -1.0, 1.0)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(sr)
            handle.writeframes((data * 32767).astype("<i2").tobytes())
        return 20.0 * np.log10(
            float(np.sqrt(np.mean(data ** 2))) or 1e-9)
    except Exception:  # noqa: BLE001
        return None


def build_loop(track, start, seg_len, total_sec, out_wav, level_db=-22.0,
               xfade=2.0, fade_in=1.5, fade_out=4.0, sr=48000,
               loudnorm=True, seam_snap=False, dip_times=None,
               dip_db=4.0, dip_sec=1.2):
    """把选定段交叉淡化循环成恰好 total_sec 的 wav，并归一到 level_db。

    even=True 时先做音量均衡（把曲子自身起伏拉平），否则只做静态增益。
    """
    seg = _decode(track, seconds=seg_len, start=start, sr=sr, channels=2)
    if seg is None or seg.size == 0:
        return None
    seg = np.asarray(seg, dtype=np.float32)
    # 单声道时复制成双声道
    if seg.ndim == 1:
        seg = np.stack([seg, seg], axis=1)

    # 循环接缝吸附到能量谷：进/出都落在最安静的位置，
    # 和语音边界那套是同一个思路（找局部最低点，不硬切在重音上）
    if seam_snap:
        seg, _ti, _to = snap_segment_to_valleys(seg, sr)

    target = 10 ** (float(level_db) / 20.0)
    if loudnorm:
        # 1) 曲内起伏拉平（相对增益）
        seg = even_out(seg, sr, float(level_db))
        # 2) 按响度整体对齐（不同曲子之间"听起来一样响"）
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=indexer.temp_root())
        os.close(fd)
        try:
            _write_wav(tmp, seg, sr)
            cur_lufs = measure_lufs(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if cur_lufs is not None:
            want = float(level_db) + LUFS_RMS_OFFSET
            seg = np.clip(seg * (10 ** ((want - cur_lufs) / 20.0)), -1.0, 1.0)
        else:
            rms = float(np.sqrt(np.mean(seg ** 2))) or 1e-6
            seg = np.clip(seg * (target / rms), -1.0, 1.0)
    else:
        rms = float(np.sqrt(np.mean(seg ** 2))) or 1e-6
        seg = np.clip(seg * (target / rms), -1.0, 1.0)

    # 转场处让 BGM 让一下（在电平归一再压，压完不会再被归一拉回去）
    if dip_times:
        seg = apply_dip(seg, sr, dip_times, dip_db=dip_db, dip_sec=dip_sec)

    # 闭环校准（子枫 2026-11-25：「设置了 -40dB，BGM 就直接 -40dB」）：
    # 上面的 LUFS 对齐带着 2.6dB 标定偏移（注释记的实测抬升 +3.85dB），
    # 设 -45 出来并不是 -45。用户要的是"设多少就是多少"，这里按 RMS 拉回去，
    # 偏差超过 0.5dB 才动，避免来回抖动。
    _rms = float(np.sqrt(np.mean(seg ** 2))) or 1e-6
    _cur_db = 20.0 * np.log10(_rms)
    _want_db = float(level_db)
    if abs(_cur_db - _want_db) > 0.5:
        seg = np.clip(seg * (10 ** ((_want_db - _cur_db) / 20.0)), -1.0, 1.0)

    xf = max(1, int(xfade * sr))
    if xf * 2 > len(seg):
        xf = max(1, len(seg) // 4)
    need = int(total_sec * sr)
    ramp = np.linspace(0.0, 1.0, xf, dtype=np.float32)[:, None]

    n_fade_in = max(1, int(float(fade_in) * sr))
    n_fade_out = max(1, int(float(fade_out) * sr))

    written = 0
    pending = None
    first = True
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        while written < need:
            chunk = seg.copy()
            if pending is not None:
                n = min(len(pending), len(chunk))
                chunk[:n] = pending[:n] * (1.0 - ramp[:n]) + chunk[:n] * ramp[:n]
            body = chunk[:max(1, len(chunk) - xf)]
            if written + len(body) > need:
                body = body[:need - written]
            # 首段淡入
            if first and len(body) > n_fade_in:
                body = body.copy()
                body[:n_fade_in] *= np.linspace(0, 1, n_fade_in,
                                                dtype=np.float32)[:, None]
                first = False
            # 尾段淡出
            if written + len(body) >= need and len(body) > n_fade_out:
                body = body.copy()
                body[-n_fade_out:] *= np.linspace(1, 0, n_fade_out,
                                                  dtype=np.float32)[:, None]
            w.writeframes((np.clip(body, -1, 1) * 32767).astype("<i2").tobytes())
            written += len(body)
            pending = chunk[len(chunk) - xf:]
            if len(chunk) <= xf:
                pending = None
    # 写完之后按"整段真实 RMS"（含淡入淡出）再校一次：设 -40 就应该是 -40
    _calibrate_wav_level(out_wav, sr, float(level_db))
    return out_wav


def mix_into(video_path, out_path, bgm_wav, duck_db=0.0, attack_ms=30,
             release_ms=600, log=print):
    """把 BGM 混到成片下面：视频流直接复制，只重编码音频。

    duck_db = 0（默认）：固定电平混音，BGM 全程一样响，不跟着人声起伏。
    duck_db > 0：开侧链闪避，人一说话 BGM 自动压下去（会有点"呼吸感"）。
    """
    ff = indexer.find_ffmpeg()
    if float(duck_db) > 0.01:
        # 把「要让路多少 dB」换成侧链压缩的压缩比：让得多就压得狠
        ratio = max(2.0, min(20.0, float(duck_db)))
        graph = (
            "[0:a]asplit=2[voice][key];"
            "[1:a]anull[bg];"
            "[bg][key]sidechaincompress=threshold=0.02:ratio=%.1f:"
            "attack=%.0f:release=%.0f:makeup=1[bgd];"
            "[voice][bgd]amix=inputs=2:duration=first:normalize=0,"
            "alimiter=limit=0.95:level=false[aout]"
            % (ratio, float(attack_ms), float(release_ms))
        )
        log("BGM：混音（人声闪避 %.0f dB）…" % float(duck_db))
    else:
        graph = ("[0:a][1:a]amix=inputs=2:duration=first:normalize=0,"
                 "alimiter=limit=0.95:level=false[aout]")
        log("BGM：混音（固定电平，不做闪避）…")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-i", video_path, "-i", bgm_wav,
           "-filter_complex", graph,
           "-map", "0:v:0", "-map", "[aout]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", out_path]
    log("BGM：混音（视频流复制，只重编码音频）…")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=indexer.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("BGM 混音失败：%s" % (r.stderr or "")[-600:])
    return out_path


def attach(video_path, out_path=None, conf=None, log=print, pick=None,
           dip_times=None):
    """给成片配 BGM（就地替换）。返回 {"track":..,"start":..,"len":..}。"""
    cfg = conf or load_conf()
    if str(cfg.get("enabled", "1")) not in ("1", "true", "True"):
        log("BGM：已关闭")
        return None
    tracks = list_tracks(cfg.get("folder"))
    if not tracks:
        if not str(cfg.get("folder") or "").strip():
            log("BGM：还没设曲库目录（设置… → BGM 曲库），这次先不配")
        else:
            log("BGM：曲库是空的（%s）" % cfg.get("folder"))
        return None
    out_path = out_path or video_path
    dur = _duration(video_path)
    if dur <= 1:
        log("BGM：读不出成片时长")
        return None
    track = pick or pick_track(tracks, cfg.get("seed", ""))
    info = analyze(track)
    if not info:
        log("BGM：这首读不出来，跳过：%s" % os.path.basename(track))
        return None
    start, seg_len, steady = best_window(info, float(cfg.get("seg_len", 150)))
    log("BGM：%s（%.1f 分钟）→ 取 %.0f~%.0f 秒这段，波动 %.2f dB"
        % (os.path.basename(track), info["duration"] / 60,
           start, start + seg_len, steady))
    # 临时目录必须放在成片同一个盘：跨盘 os.replace 会报
    # WinError 17（系统无法将文件移到不同的磁盘驱动器）
    tmp = tempfile.mkdtemp(prefix="bgm_",
                           dir=os.path.dirname(os.path.abspath(out_path)) or None)
    loop_wav = os.path.join(tmp, "loop.wav")
    try:
        _ln = str(cfg.get("loudness_norm", "1")) in ("1", "true", "True")
        log("BGM：音量拉齐 %s，目标 %.0f dB"
            % ("开（按响度，不同曲子一样响）" if _ln else "关（只按 RMS）",
               float(cfg.get("level_db", -22))))
        build_loop(track, start, seg_len, dur, loop_wav,
                   level_db=float(cfg.get("level_db", -22)),
                   xfade=float(cfg.get("xfade", 2.0)),
                   fade_in=float(cfg.get("fade_in", 1.5)),
                   fade_out=float(cfg.get("fade_out", 4.0)),
                   loudnorm=_ln,
                   # 接缝吸附实测没收益（跳变均值不变），默认关；想试填 seam_snap=1
                   seam_snap=str(cfg.get("seam_snap", "0")) in ("1", "true", "True"),
                   dip_times=dip_times,
                   dip_db=float(cfg.get("dip_db", 4.0)),
                   dip_sec=float(cfg.get("dip_sec", 1.2)))
        mixed = os.path.join(tmp, "mixed.mp4")
        _duck = float(cfg.get("duck_db", 0))
        mix_into(video_path, mixed, loop_wav,
                 duck_db=_duck,
                 attack_ms=float(cfg.get("attack_ms", 30)),
                 release_ms=float(cfg.get("release_ms", 600)), log=log)
        os.replace(mixed, out_path)
        log("BGM：完成（%s）" % os.path.basename(out_path))
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass
    return {"track": track, "start": start, "len": seg_len,
            "level_db": float(cfg.get("level_db", -22))}


def _duration(path):
    try:
        ff = indexer.find_ffmpeg()
        exe = os.path.join(os.path.dirname(ff), "ffprobe.exe")
        if not os.path.isfile(exe):
            exe = "ffprobe"
        r = subprocess.run([exe, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, errors="replace",
                           creationflags=indexer.CREATE_NO_WINDOW)
        return float((r.stdout or "0").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?")
    ap.add_argument("--out", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pick", action="store_true", help="只报告选曲结果，不混音")
    ap.add_argument("--seed", default=None)
    ap.add_argument("--level", type=float, default=None)
    args = ap.parse_args(argv)

    if args.list:
        cfg = load_conf()
        ts = list_tracks(cfg["folder"])
        print("曲库：%s（%d 首）" % (cfg["folder"], len(ts)))
        for t in ts:
            info = analyze(t)
            if info:
                print("  %-44s %5.1f 分钟  %s"
                      % (os.path.basename(t), info["duration"] / 60,
                         "平" if np.std(info["db"]) < 3 else "起伏大"))
        return 0
    if not args.video:
        ap.print_help()
        return 1
    cfg = load_conf()
    if args.seed:
        cfg["seed"] = args.seed
    if args.level is not None:
        cfg["level_db"] = str(args.level)
    if args.pick:
        track = pick_track(list_tracks(cfg["folder"]), cfg.get("seed", ""))
        info = analyze(track)
        s, L, sd = best_window(info, float(cfg.get("seg_len", 150)))
        print("会选：%s（%.1f 分钟）第 %.0f~%.0f 秒，波动 %.2f dB"
              % (os.path.basename(track), info["duration"] / 60, s, s + L, sd))
        return 0
    r = attach(args.video, args.out, conf=cfg)
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main())

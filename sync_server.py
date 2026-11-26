# -*- coding: utf-8 -*-
"""PR 桥：本机 HTTP 接口，把「最新剪辑单」交给 Premiere 插件。

子枫 2026-11-25：
    「做一个可以兼容 PR 的插件，两端链接，接入后直接用 PR 的时间轴剪辑，
      最后导出视频用 PR 自己导。」

职责边界：
  · 本脚本**只读**剪辑结果（剪好的 keep 段 + 素材路径 + 音轨），不剪不渲；
  · PR 插件（CEP 面板）拉这个接口，把片段按 in/out 插进当前序列；
  · 之后所有编辑、转场、调色、导出都在 PR 里完成。

启动：
    .venv\\Scripts\\python.exe sync_server.py            # 前台
    .venv\\Scripts\\pythonw.exe sync_server.py           # 无窗口

接口（全部要求 ``X-SmartCut-Token`` 会话头）：
    GET  /health     → {"ok": true, "app": "smartcut"}
    GET  /timeline   → 最新一条剪辑单（见 build_timeline）
    POST /transcribe /step /plan /smart-edit/result → 会改变状态或运行任务
"""
from __future__ import annotations

import hmac
import json
import os
import glob
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.abspath(os.environ.get("SMARTCUT_HOME") or
                       os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("SMARTCUT_BRIDGE_PORT", "17890"))
BRIDGE_HEADER = "X-SmartCut-Token"
BRIDGE_TOKEN = ""
SESSION_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "SmartCut")
SESSION_FILE = os.path.join(SESSION_DIR, "bridge-session.json")
WORK = os.path.join(BASE, "工作目录", "索引")
SEQ_FILE = os.path.join(BASE, "工作目录", "pr_sequence.txt")
PROBE_FILE = os.path.join(BASE, "工作目录", "pr_api_probe.txt")
LOG_FILE = os.path.join(BASE, "工作目录", "sync_server.log")
_LAST = {"key": None, "value": None}
_SMART_EDIT_LOCK = threading.Lock()
_SETTINGS_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()
_SMART_EDIT_JOB = {
    "status": "idle", "message": "", "logs": [], "progress": {},
    "result": None, "error": "", "fingerprint": "", "started_at": 0.0,
}


def _allowed_origin(value):
    """Return the CORS origin accepted for the installed CEP panel.

    CEP loads ``index.html`` from disk, which Chromium normally serialises as
    the opaque ``null`` origin. Real web origins are deliberately rejected so
    a browser page cannot drive Premiere through localhost. Requests without
    an Origin header are local native/Python clients and still require a token.
    """
    origin = str(value or "").strip()
    if not origin:
        return ""
    lowered = origin.lower()
    if (lowered == "null" or lowered.startswith("file://") or
            lowered.startswith("cep://")):
        return origin
    return None


def load_bridge_session(path=None):
    """Read and validate the current per-user bridge session descriptor."""
    path = path or SESSION_FILE
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle) or {}
        token = str(data.get("token") or "")
        port = int(data.get("port") or 0)
        if len(token) < 32 or not 1 <= port <= 65535:
            return None
        return {
            "schema": int(data.get("schema") or 1),
            "token": token,
            "port": port,
            "pid": int(data.get("pid") or 0),
            "created_at": int(data.get("created_at") or 0),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_bridge_session(token=None, port=None, path=None):
    """Atomically publish a fresh, unlogged token for the current user only."""
    token = str(token or secrets.token_urlsafe(32))
    if len(token) < 32:
        raise ValueError("bridge session token is too short")
    port = int(port or PORT)
    path = path or SESSION_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "schema": 1,
        "token": token,
        "port": port,
        "pid": os.getpid(),
        "created_at": int(time.time()),
    }
    temp = path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass
    return token


def clear_bridge_session(token, path=None):
    """Remove only the session descriptor that belongs to this server."""
    path = path or SESSION_FILE
    current = load_bridge_session(path)
    if not current or not hmac.compare_digest(current["token"], str(token or "")):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False

def _normalise_audio_tracks(value):
    """Return a stable comma-separated list of one-based source tracks."""
    if isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        text = str(value or "").replace("，", ",").replace("；", ",")
        parts = text.replace(";", ",").split(",")
    tracks = []
    for part in parts:
        token = str(part).strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError("音轨只能填写正整数，例如 2,3")
        number = int(token)
        if number < 1 or number > 64:
            raise ValueError("音轨编号必须在 1 到 64 之间")
        if number not in tracks:
            tracks.append(number)
    tracks.sort()
    return ",".join(str(number) for number in tracks)


def load_pr_audio_settings():
    """Load the audio choice shared by SmartCut and the Premiere panel."""
    path = os.path.join(BASE, "conf", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError, TypeError):
        data = {}
    try:
        tracks = _normalise_audio_tracks(data.get("autocut_tracks") or "")
    except ValueError:
        tracks = ""
    output_mode = str(data.get("pr_output_audio_mode") or "all").lower()
    if output_mode not in ("all", "selected"):
        output_mode = "all"
    import cut_strength
    keep_ratio = cut_strength.ratio_from_settings(data)
    cut_level = cut_strength.level_for_ratio(keep_ratio)
    return {
        "ok": True,
        "tracks": tracks,
        "track_numbers": [int(value) for value in tracks.split(",") if value],
        "output_mode": output_mode,
        "cut_level": cut_level,
        "keep_ratio": keep_ratio,
        "keep_percent": cut_strength.percent(keep_ratio),
    }


def save_pr_audio_settings(payload):
    """Persist panel audio settings without disturbing other app settings."""
    if not isinstance(payload, dict):
        raise ValueError("音轨设置格式无效")
    tracks = _normalise_audio_tracks(payload.get("tracks") or "")
    output_mode = str(payload.get("output_mode") or "all").lower()
    if output_mode not in ("all", "selected"):
        raise ValueError("最终工程音轨模式无效")
    if output_mode == "selected" and not tracks:
        raise ValueError("只保留所选轨时，至少填写一条音轨")

    path = os.path.join(BASE, "conf", "settings.json")
    with _SETTINGS_LOCK:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError, TypeError):
            data = {}
        import cut_strength
        if payload.get("keep_ratio") is not None:
            try:
                requested = float(payload.get("keep_ratio"))
            except (TypeError, ValueError):
                raise ValueError("剪辑保留比例必须是 15% 到 100%")
            if requested > 1.0:
                requested /= 100.0
            if not 0.15 <= requested <= 1.0:
                raise ValueError("剪辑保留比例必须是 15% 到 100%")
            keep_ratio = cut_strength.normalize_ratio(requested)
        else:
            legacy_level = str(payload.get("cut_level") or
                               data.get("cut_level") or "medium").lower()
            keep_ratio = cut_strength.normalize_ratio(
                cut_strength.LEGACY_KEEP_RATIO.get(legacy_level),
                fallback=cut_strength.ratio_from_settings(data))
        cut_level = cut_strength.level_for_ratio(keep_ratio)
        data["autocut_tracks"] = tracks
        data["pr_output_audio_mode"] = output_mode
        data["cut_level"] = cut_level
        data["keep_ratio"] = keep_ratio
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp, path)
        finally:
            try:
                os.remove(temp)
            except FileNotFoundError:
                pass
    return load_pr_audio_settings()


def _safe_project_stem(value):
    """Return a Windows-safe filename stem for generated Premiere projects."""
    text = str(value or "SmartCut").strip()
    for char in '<>:"/\\|?*':
        text = text.replace(char, "_")
    text = text.rstrip(" .")
    return text[:96] or "SmartCut"


def _unique_project_pair(directory, stem, stamp=None):
    """Allocate non-overwriting backup/final .prproj paths as one pair."""
    os.makedirs(directory, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    base = "%s_SmartCut_%s" % (_safe_project_stem(stem), stamp)
    suffix = ""
    number = 1
    while True:
        backup = os.path.join(
            directory, base + suffix + "_剪辑前备份.prproj")
        output = os.path.join(
            directory, base + suffix + "_可编辑工程.prproj")
        if not os.path.exists(backup) and not os.path.exists(output):
            return backup, output
        number += 1
        suffix = "_%d" % number


def allocate_project_paths(source):
    """Choose an editable-project output folder beside the source when possible."""
    source = os.path.abspath(str(source or ""))
    stem = os.path.splitext(os.path.basename(source))[0] or "SmartCut"
    preferred = os.path.join(os.path.dirname(source), "导出")
    fallback = os.path.join(BASE, "工作目录", "PR工程")
    try:
        backup, output = _unique_project_pair(preferred, stem)
    except OSError:
        backup, output = _unique_project_pair(fallback, stem)
    return {"project_backup_path": backup, "project_path": output}


def _log(line):
    """请求日志：/plan 卡住时能看出卡在哪一步。"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))
    except OSError:
        pass


# ------------------------------------------------------------------ 发按键
# PR 26.x 没有 razor/executeCommand，但实测「向主窗口发 WM_KEYDOWN」有效：
#   Q = 波纹修剪上一个编辑点到播放头
#   W = 波纹修剪下一个编辑点到播放头
#   Ctrl+K = 添加编辑（在播放头切一刀）
# 这些键位来自 PR 自带的 .kys 方案文件，不是我们写死的。
VK_CODES = {
    "Q": 0x51, "W": 0x57, "K": 0x4B, "E": 0x45, "R": 0x52,
    "HOME": 0x24, "END": 0x23, "DELETE": 0x2E, "BACKSPACE": 0x08,
    "LEFT": 0x25, "RIGHT": 0x27, "UP": 0x26, "DOWN": 0x28,
    "I": 0x49, "O": 0x4F, "SPACE": 0x20,
}
VK_CONTROL = 0x11
VK_SHIFT = 0x10
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


def _pr_hwnd():
    """找 PR 主窗口句柄（标题含 Adobe Premiere）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if "Adobe Premiere" in buffer.value and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def send_keys(spec, activate_timeline=True):
    """spec 形如 "Q" / "Ctrl+K" / "Shift+End"；返回 (ok, 消息)。

    先发 Shift+3 激活时间轴面板：用户点 CEP 面板按钮后焦点在面板里，
    直接发编辑命令会被面板吃掉（表现就是"日志显示执行了，但 PR 没剪"）。
    """
    import ctypes

    hwnd = _pr_hwnd()
    if not hwnd:
        return False, "找不到 PR 主窗口（PR 没开？）"
    parts = [p.strip().upper() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        return False, "空的按键"
    keys = []
    for part in parts:
        if part in ("CTRL", "CONTROL"):
            keys.append(VK_CONTROL)
        elif part == "SHIFT":
            keys.append(VK_SHIFT)
        elif part in VK_CODES:
            keys.append(VK_CODES[part])
        else:
            return False, "不认识的键：%s" % part

    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # 关键：64 位下必须声明参数类型，否则 HWND 会被截断成 32 位
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    handle = wintypes.HWND(hwnd)
    user32.SetForegroundWindow(handle)
    time.sleep(0.2)

    def tap(vk, mods=()):
        for mod in mods:
            user32.SendMessageW(handle, WM_KEYDOWN, mod, 0)
        user32.SendMessageW(handle, WM_KEYDOWN, vk, 0)
        time.sleep(0.04)
        user32.SendMessageW(handle, WM_KEYUP, vk, 0)
        for mod in reversed(mods):
            user32.SendMessageW(handle, WM_KEYUP, mod, 0)

    # 第一步：激活时间轴（Shift+3），让编辑命令有接收方
    if activate_timeline:
        tap(0x33, (VK_SHIFT,))
        time.sleep(0.15)

    mods = [k for k in keys if k in (VK_CONTROL, VK_SHIFT)]
    mains = [k for k in keys if k not in (VK_CONTROL, VK_SHIFT)]
    for key in mains:
        tap(key, mods)
        time.sleep(0.05)
    return True, "已发送 %s（窗口 %s）" % ("+".join(parts), hwnd)


def _read_index(index_dir):
    path = os.path.join(index_dir, "index.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _latest_plan(index_dir):
    """取这份索引里最新的一份「最终渲染时间轴」计划。"""
    plans = os.path.join(index_dir, "plans")
    if not os.path.isdir(plans):
        return None
    files = sorted(glob.glob(os.path.join(plans, "*.json")),
                   key=os.path.getmtime, reverse=True)
    for path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        body = doc.get("plan") or doc
        spans = (body.get("final_render_spans")
                 or body.get("refined_spans")
                 or body.get("semantic_spans"))
        if spans and len(spans) >= 1:
            return spans, path, os.path.getmtime(path)
    return None


def find_latest():
    """全盘找「最近被剪过」的那份索引。"""
    if not os.path.isdir(WORK):
        return None
    best = None
    for name in os.listdir(WORK):
        index_dir = os.path.join(WORK, name)
        if not os.path.isdir(index_dir):
            continue
        found = _latest_plan(index_dir)
        if not found:
            continue
        spans, path, mtime = found
        if best is None or mtime > best[2]:
            best = (index_dir, spans, mtime, path)
    if best is None:
        return None
    index_dir, spans, mtime, path = best
    data = _read_index(index_dir) or {}
    source = str(data.get("source") or "")
    if not source or not os.path.isfile(source):
        return None
    return {
        "source": source,
        "name": os.path.basename(source),
        "duration": round(float(data.get("duration") or 0.0), 3),
        "track": int(data.get("audio_stream_used") or 0),
        "spans": [[round(float(a), 3), round(float(b), 3)] for a, b in spans],
        "count": len(spans),
        "plan": os.path.basename(path),
        "plan_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(mtime)),
        "index_dir": index_dir,
    }


def build_timeline():
    """带缓存：索引没变动就直接复用，插件轮询也不费事。"""
    key = None
    try:
        key = os.path.getmtime(WORK) if os.path.isdir(WORK) else None
    except OSError:
        key = None
    if _LAST["key"] == key and _LAST["value"] is not None:
        return _LAST["value"]
    value = find_latest()
    _LAST["key"] = key
    _LAST["value"] = value
    return value


# ---------------------------------------------------------------- PR 序列
def load_sequence():
    """解析 PR 面板发来的竖线文本（同步接口落盘的那份）。"""
    if not os.path.isfile(SEQ_FILE):
        return None
    try:
        with open(SEQ_FILE, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    head = lines[0].split("|")
    clips = []
    tracks = []
    project_path = ""
    for line in lines[1:]:
        parts = line.split("|")
        if parts[0] == "PROJECT" and len(parts) >= 2:
            project_path = parts[1]
            continue
        if parts[0] == "TRACK" and len(parts) >= 3:
            try:
                tracks.append({
                    "kind": parts[1],
                    "track": int(parts[2]),
                    "muted": ((parts[3] == "1")
                              if len(parts) > 3 and parts[3] else None),
                })
            except (TypeError, ValueError):
                continue
            continue
        if len(parts) < 8 or parts[0] not in ("V", "A"):
            continue
        try:
            clips.append({
                "kind": parts[0],
                "track": int(parts[1]),
                "start": float(parts[2]),
                "end": float(parts[3]),
                "in": float(parts[4]),
                "out": float(parts[5]),
                "name": parts[6],
                "path": parts[7],
            })
        except (TypeError, ValueError):
            continue
    return {
        "head": head,
        "name": head[1] if len(head) > 1 else "",
        "project_path": project_path,
        "clips": clips,
        "tracks": tracks,
        "video": [c for c in clips if c["kind"] == "V"],
        "audio": [c for c in clips if c["kind"] == "A"],
    }


def find_index(source):
    """按素材路径找对应索引（A：转写/音轨/OCR）。"""
    if not source or not os.path.isdir(WORK):
        return None
    target = os.path.normcase(os.path.abspath(source))
    for name in os.listdir(WORK):
        index_dir = os.path.join(WORK, name)
        path = os.path.join(index_dir, "index.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        found = os.path.normcase(os.path.abspath(str(data.get("source") or "")))
        if found and found == target:
            return data, index_dir
    return None


def _segment_energy(data, a, b):
    """这句台词期间的平均音量（索引里现成的每秒电平，不重新解码）。"""
    levels = (data.get("analysis") or {}).get("audio_1s") or []
    if not levels:
        return None
    lo = max(0, int(a))
    hi = min(len(levels), max(lo + 1, int(b) + 1))
    values = []
    for index in range(lo, hi):
        try:
            values.append(float(levels[index]))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def _energy_word(db):
    """把 dB 折成定性档（沿用 coords 的三档：响/中/轻）。"""
    if db is None:
        return ""
    if db >= -20.0:
        return "响"
    if db >= -35.0:
        return "中"
    return "轻"


SUB_DIR = os.path.join(BASE, "工作目录", "序列字幕")


def _asr_wav(src, out_wav, start, end, track=0):
    """从素材里截一段音频出来（时间轴片段 = 素材 in~out）。"""
    ffmpeg = os.path.join(BASE, "bin", "ffmpeg.exe")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", "%.3f" % start]
    if end > start:
        cmd += ["-t", "%.3f" % (end - start)]
    cmd += ["-i", src, "-map", "0:a:%d" % track, "-vn", "-ac", "1",
            "-ar", "16000", "-c:a", "pcm_s16le", out_wav]
    import subprocess
    subprocess.run(cmd, check=True)


def _sequence_duration(sequence):
    """Return the real PR timeline length, including silent gaps."""
    values = []
    head = (sequence or {}).get("head") or []
    if len(head) > 4:
        try:
            values.append(float(head[4]))
        except (TypeError, ValueError):
            pass
    for clip in (sequence or {}).get("clips") or []:
        try:
            values.append(float(clip.get("end") or 0.0))
        except (TypeError, ValueError):
            continue
    duration = max(values or [0.0])
    if not 0.0 < duration <= 4 * 3600:
        raise ValueError("独立字幕单次支持 4 小时以内的序列；请分段转写，避免内存不足")
    return duration


def _selected_transcription_tracks(sequence, settings=None):
    """Resolve the panel's one-based A-track selection for standalone ASR."""
    audio = list((sequence or {}).get("audio") or [])
    available = sorted({int(row.get("track") or 0) for row in audio
                        if int(row.get("track") or 0) > 0})
    if not available:
        raise ValueError("当前序列没有可转写的音频片段")
    settings = settings or load_pr_audio_settings()
    configured = [int(value) for value in settings.get("track_numbers") or []]
    if configured:
        missing = [track for track in configured if track not in available]
        if missing:
            raise ValueError("所选音轨不在当前序列：%s" %
                             ", ".join("A%d" % track for track in missing))
        return sorted(set(configured))

    muted = {
        int(row.get("track") or 0): row.get("muted")
        for row in (sequence or {}).get("tracks") or []
        if str(row.get("kind") or "").upper() == "A"
    }
    active = [track for track in available if muted.get(track) is not True]
    if not active:
        raise ValueError("当前序列的音频轨都已静音，请手动选择需要转写的音轨")
    return active


def _assemble_sequence_audio(sequence, track_numbers, log=print, sr=16000,
                             warnings_out=None):
    """Decode selected PR audio clips into their absolute timeline positions.

    ``TRACK``/``A`` rows exported by the CEP bridge are one based.  ffmpeg and
    :mod:`index` use zero-based source-stream ordinals, hence ``track - 1``.
    Silent timeline holes remain zeroes instead of being concatenated away.
    """
    import numpy as np
    import index

    warnings_out = warnings_out if warnings_out is not None else []
    selected = {int(value) for value in track_numbers or []}
    clips = [dict(row) for row in (sequence or {}).get("audio") or []
             if int(row.get("track") or 0) in selected]
    if not clips:
        raise ValueError("所选音轨在当前序列中没有音频片段")
    duration = _sequence_duration(sequence)
    timeline = np.zeros(int(np.ceil(duration * sr)), dtype=np.float32)
    decoded = 0
    stream_overrides = {}
    for clip in sorted(clips, key=lambda row: (
            float(row.get("start") or 0.0), int(row.get("track") or 0))):
        path = os.path.abspath(str(clip.get("path") or ""))
        if not os.path.isfile(path):
            raise FileNotFoundError("找不到序列音频素材：%s" % path)
        track = int(clip.get("track") or 0)
        start, end = float(clip.get("start") or 0.0), float(clip.get("end") or 0.0)
        source_in = float(clip.get("in") or 0.0)
        source_out = float(clip.get("out") or 0.0)
        if end <= start or source_out <= source_in:
            continue
        log("  取 A%d：时间轴 %.2f~%.2f 秒（素材 %.2f~%.2f）" %
            (track, start, end, source_in, source_out))
        stream_key = (os.path.normcase(path), track)
        source_stream = stream_overrides.get(stream_key, track - 1)
        try:
            samples = index.load_audio_window_mono16k(
                path, source_stream, source_in, source_out)
        except Exception as first:  # noqa: BLE001
            # A Premiere destination track is not a guaranteed source-stream
            # ordinal: users can freely move a mono clip from A1 to A6.  Only
            # fall back when the requested non-zero source stream cannot be
            # decoded, and make the assumption visible in the report/log.
            if source_stream <= 0:
                raise
            try:
                samples = index.load_audio_window_mono16k(
                    path, 0, source_in, source_out)
            except Exception:
                raise first
            stream_overrides[stream_key] = 0
            warning = ("A%d 位于 PR 目标轨，但素材没有可读的第 %d 条源音轨；"
                       "本片段已回退素材源音轨 1" % (track, track))
            if warning not in warnings_out:
                warnings_out.append(warning)
                log("  音轨映射提示：" + warning)
        if ((samples is None or not getattr(samples, "size", 0))
                and source_stream > 0):
            samples = index.load_audio_window_mono16k(
                path, 0, source_in, source_out)
            stream_overrides[stream_key] = 0
            warning = ("A%d 位于 PR 目标轨，但素材第 %d 条源音轨为空；"
                       "本片段已回退素材源音轨 1" % (track, track))
            if warning not in warnings_out:
                warnings_out.append(warning)
                log("  音轨映射提示：" + warning)
        if samples is None or not getattr(samples, "size", 0):
            raise RuntimeError("A%d 音频读取为空：%s" % (track, path))
        wanted = max(1, int(round((end - start) * sr)))
        if len(samples) != wanted:
            # Premiere 变速/帧量化后，素材时长可能不等于时间轴时长。
            # 在识别副本内重采样到实际时间轴长度，不修改原音频或 PR 序列。
            positions = np.linspace(0, max(0, len(samples) - 1), wanted)
            samples = np.interp(positions, np.arange(len(samples)), samples).astype(
                np.float32)
        begin = max(0, int(round(start * sr)))
        finish = min(len(timeline), begin + len(samples))
        if finish <= begin:
            continue
        timeline[begin:finish] += samples[:finish - begin]
        decoded += 1
    if not decoded or not np.any(np.abs(timeline) > 1e-7):
        raise ValueError("所选音轨没有可识别的声音")
    return timeline, duration, decoded


def _standalone_subtitle_directory(sequence):
    """Prefer a visible folder beside the current .prproj, with safe fallback."""
    raw_project = str((sequence or {}).get("project_path") or "").strip()
    candidates = []
    if raw_project:
        project = os.path.abspath(raw_project)
        candidates.append(os.path.join(os.path.dirname(project), "SmartCut字幕"))
    candidates.append(SUB_DIR)
    last_error = None
    for folder in candidates:
        try:
            os.makedirs(folder, exist_ok=True)
            return folder
        except OSError as exc:
            last_error = exc
    raise OSError("无法创建字幕输出目录：%s" % last_error)


def _atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def transcribe_sequence(log=print, requested_tracks=None):
    """Independently transcribe current PR audio without editing the timeline."""
    import numpy as np
    import audiofeat_bounds
    import index
    import pr_subtitles
    import pr_workflow
    import subtitles

    if not _TRANSCRIBE_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有一项字幕转写正在运行，请等待完成"}
    try:
        sequence = load_sequence()
        if not sequence:
            return {"ok": False, "error": "还没有 PR 序列数据，请先读取当前序列"}
        if requested_tracks is None:
            settings = load_pr_audio_settings()
        else:
            normalised_tracks = _normalise_audio_tracks(requested_tracks)
            settings = {
                "track_numbers": [int(value) for value in
                                  normalised_tracks.split(",") if value]
            }
        track_numbers = _selected_transcription_tracks(sequence, settings)
        log("独立字幕转写：只读取 %s，不运行剪辑角色、不修改 PR 时间轴" %
            "、".join("A%d" % value for value in track_numbers))
        warnings = []
        audio, total, decoded = _assemble_sequence_audio(
            sequence, track_numbers, log=log, warnings_out=warnings)
        log("  时间轴音频：%.1f 秒，%d 个片段（空隙保持静音）" %
            (total, decoded))

        profile = index.asr_profile()
        model_size = str(profile.get("model") or "large-v3")
        source_tracks = [value - 1 for value in track_numbers]
        models_dir = os.path.join(BASE, "models")
        evidence = []
        blockers, diagnostics = [], []
        normalised = index.normalize_for_asr(audio)
        segments = index.transcribe(
            normalised, model_size=model_size, models_dir=models_dir,
            on_status=log, initial_prompt=profile.get("initial_prompt"),
            hotwords=profile.get("hotwords"),
            beam_size=profile.get("beam_size"),
            batch_size=int(profile.get("batch_size") or 0),
            evidence_out=evidence, source_audio_track=source_tracks)
        segments = [(float(a), float(b), str(text or "").strip())
                    for a, b, text in segments if str(text or "").strip()]
        if not segments:
            return {"ok": False, "error": "所选音轨没有转写出可靠文字"}

        # Keep the same repair order as index_video so segment/evidence pairs
        # cannot be cross-wired by a later local re-listen.
        try:
            segments = index.repair_suspected_alignment(
                segments, normalised, model_size=model_size,
                models_dir=models_dir, on_status=log, evidence_out=evidence,
                source_audio_track=source_tracks,
                beam_size=profile.get("beam_size"),
                initial_prompt=profile.get("initial_prompt"),
                hotwords=profile.get("hotwords"), blockers_out=blockers)
        except Exception as exc:  # noqa: BLE001
            warnings.append("异常时间窗复听未完成：%s" % str(exc)[:100])
        try:
            segments = index.repair_suspected_repetitions(
                segments, normalised, model_size=model_size,
                models_dir=models_dir, on_status=log, evidence_out=evidence,
                source_audio_track=source_tracks,
                beam_size=profile.get("beam_size"))
        except Exception as exc:  # noqa: BLE001
            warnings.append("循环复读复听未完成：%s" % str(exc)[:100])
        try:
            segments, blockers = index.repair_uncovered_human_speech(
                segments, normalised, rms_windows=_voice_segments(audio),
                model_size=model_size, models_dir=models_dir, on_status=log,
                evidence_out=evidence, source_audio_track=source_tracks,
                beam_size=profile.get("beam_size"),
                initial_prompt=profile.get("initial_prompt"),
                hotwords=profile.get("hotwords"), blockers_out=blockers,
                diagnostics_out=diagnostics)
        except Exception as exc:  # noqa: BLE001
            warnings.append("漏字窗口复听未完成：%s" % str(exc)[:100])
        try:
            segments = index.fill_missing_head(
                segments, normalised, model_size=model_size,
                models_dir=models_dir, on_status=log, evidence_out=evidence,
                source_audio_track=source_tracks,
                beam_size=profile.get("beam_size"))
        except Exception as exc:  # noqa: BLE001 - non-blocking subtitle delivery
            warnings.append("开头复听未完成：%s" % str(exc)[:100])
        if blockers:
            warnings.append("仍有 %d 个连续人声窗口无法可靠识别，已保留诊断；不影响其他出片" %
                            len(blockers))

        rows = [{"a": float(a), "b": float(b), "text": str(text or "")}
                for a, b, text in segments]
        original_rows = [dict(row) for row in rows]
        try:
            rows = calibrate_subtitles(rows, BASE, log=log)
        except Exception as exc:  # noqa: BLE001
            rows = original_rows
            warnings.append("上下文校对未完成，已保留原始 ASR：%s" % str(exc)[:100])
        segments = [(row["a"], row["b"], row["text"]) for row in rows]
        evidence = index.align_segment_evidence(
            segments, evidence, source_audio_track=source_tracks)

        simplified_converter = pr_subtitles._converter("t2s")
        traditional_converter = pr_subtitles._converter("s2t")
        if simplified_converter is None or traditional_converter is None:
            warnings.append("OpenCC 字典不可用，简繁转换已使用内置基础字表")
        simplified = [(a, b, pr_subtitles.to_simplified(
            text, converter=simplified_converter)) for a, b, text in segments]
        try:
            refined_bounds = audiofeat_bounds.refine_caption_segments(
                audio, simplified, evidence, source_tracks, sr=16000)
            if len(refined_bounds) != len(simplified):
                raise ValueError("波形边界数量与字幕数量不一致")
        except Exception as exc:  # noqa: BLE001
            refined_bounds = [(row[0], row[1]) for row in simplified]
            warnings.append("波形边界细化未完成，已保留词级时间：%s" % str(exc)[:100])
        simplified = [(bounds[0], bounds[1], row[2])
                      for row, bounds in zip(simplified, refined_bounds)]

        style = dict(subtitles.load_style())
        style.update({"semantic_target_chars": 15, "semantic_max_chars": 15,
                      "max_chars": 15, "max_lines": 1})
        simplified, semantic_evidence = subtitles.semantic_segment_cues(
            simplified, word_segments=evidence, target_chars=15,
            max_chars=15, return_evidence=True)
        simplified = subtitles.paginate_segments(
            simplified, style=style, max_chars=15,
            word_segments=semantic_evidence)
        simplified, overlap_repairs = subtitles.normalize_caption_overlaps(
            simplified, return_repairs=True)
        traditional = [(a, b, pr_subtitles.to_traditional(
            text, converter=traditional_converter)) for a, b, text in simplified]
        if not simplified:
            return {"ok": False, "error": "语义断句后没有可安全写入的字幕"}

        folder = _standalone_subtitle_directory(sequence)
        stem = _safe_project_stem(sequence.get("name") or "当前序列")
        simplified_path = os.path.join(folder, stem + "_简体中文字幕.srt")
        traditional_path = os.path.join(folder, stem + "_繁体中文字幕.srt")
        token = ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
        simplified_temp = simplified_path + token
        traditional_temp = traditional_path + token
        try:
            _, simplified_count = subtitles.write_srt(
                {}, simplified_temp, segments=simplified, paginated=True)
            _, traditional_count = subtitles.write_srt(
                {}, traditional_temp, segments=traditional, paginated=True)
            if simplified_count != traditional_count:
                raise RuntimeError("简体和繁体字幕条数不一致")
            os.replace(simplified_temp, simplified_path)
            os.replace(traditional_temp, traditional_path)
        finally:
            for temp_path in (simplified_temp, traditional_temp):
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass
        cache_rows = []
        for position, (start, end, text) in enumerate(simplified):
            i0 = max(0, int(start * 16000))
            i1 = min(len(audio), int(end * 16000))
            piece = audio[i0:i1]
            rms = float(np.sqrt(np.mean(piece ** 2))) if len(piece) else 0.0
            next_start = (simplified[position + 1][0]
                          if position + 1 < len(simplified) else total)
            cache_rows.append({
                "a": round(float(start), 3), "b": round(float(end), 3),
                "text": text,
                "db": round(float(20.0 * np.log10(max(rms, 1e-6))), 1),
                "gap": round(max(0.0, float(next_start) - float(end)), 3),
            })
        os.makedirs(SUB_DIR, exist_ok=True)
        sequence_fingerprint = pr_workflow.sequence_fingerprint(sequence)
        cache_payload = {
            "schema": "smartcut.sequence-subtitle-cache.v1",
            "sequence_fingerprint": sequence_fingerprint,
            "track_numbers": list(track_numbers),
            "asr_profile_signature": str(profile.get("signature") or ""),
            "segments": cache_rows,
        }
        _atomic_json(os.path.join(SUB_DIR, "序列字幕.json"), cache_payload)
        report = {
            "schema": "smartcut.standalone-subtitles.v1",
            "sequence": sequence.get("name") or "", "duration": total,
            "sequence_fingerprint": sequence_fingerprint,
            "model": model_size, "track_numbers": track_numbers,
            "asr_profile_signature": str(profile.get("signature") or ""),
            "audio_mapping": "PR destination A# -> source stream ordinal, stream-1 fallback",
            "segments": cache_rows, "warnings": warnings,
            "alignment_blockers": blockers,
            "alignment_diagnostics": diagnostics,
            "caption_overlap_repairs": overlap_repairs,
        }
        _atomic_json(os.path.join(folder, stem + "_字幕转写报告.json"), report)
        tracks = [
            {"name": "简体中文", "language": "zh-Hans",
             "path": simplified_path, "count": simplified_count},
            {"name": "繁体中文", "language": "zh-Hant",
             "path": traditional_path, "count": traditional_count},
        ]
        return {
            "ok": True, "count": simplified_count,
            "duration": round(total, 2), "model": model_size,
            "sequence_fingerprint": sequence_fingerprint,
            "track_numbers": track_numbers, "tracks": tracks,
            "srt": simplified_path, "simplified_srt": simplified_path,
            "traditional_srt": traditional_path, "segments": cache_rows,
            "warning": "；".join(dict.fromkeys(warnings)),
        }
    finally:
        _TRANSCRIBE_LOCK.release()


POLISH_SYS = """你在整理字幕分段。下面是一段视频的语音转写，每行格式：
  序号: 起始秒~结束秒 文本

这些句子是按停顿切的，切得很碎（半句、几个字就成一条）。请把它们合并成
读起来通顺的完整句子，但必须遵守字幕的硬性限制。

硬性规则（必须遵守）：
· 每条字幕不超过 20 个字（含标点）——这是字幕，长一句话必须继续拆开；
· 一组最多合并 3 条；
· 只合并，不改写文字（可以删掉"呃/那个/就是"这类口头禅重复）；
· 合并后的时间由程序取"组首起点 ~ 组尾终点"，不用你算；
· 不要跨话题硬合。

输出 JSON（不要多余文字）：
{"groups": [[1,2,3], [4], [5,6], ...], "reason": "一句话说明"}
每组是一个数组，数组里是上面那些句子的序号（从 1 开始）。
每条句子必须且只能出现在一个组里，按顺序不遗漏。"""


def _split_long_text(text, limit=20):
    """把超过 limit 字的字幕切开（时间按字数比例分）。

    转写文本常常没有标点，所以除了按标点切，超过 limit+8 字也硬切一刀。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text]
    pieces = []
    buffer = ""
    for ch in text:
        buffer += ch
        if len(buffer) >= limit and ch in "，。！？、；：,.!?;:":
            pieces.append(buffer)
            buffer = ""
        elif len(buffer) >= limit + 8:
            pieces.append(buffer)
            buffer = ""
    if buffer:
        pieces.append(buffer)
    return [p for p in pieces if p]


def polish_subtitles(rows, base_dir, log=print):
    """让模型把碎句合并成通顺句子；失败就返回原样。"""
    if len(rows) < 2:
        return rows
    lines = []
    for i, row in enumerate(rows, 1):
        lines.append("%d: %.2f~%.2f %s" % (i, row["a"], row["b"], row["text"]))
    try:
        from ui.llm_bridge import LLM
        llm = LLM(base_dir=base_dir, task="analyst")
        llm.start()
        log("  让模型整理 %d 条碎句…" % len(rows))
        answer = llm.chat(POLISH_SYS, "\n".join(lines), max_tokens=1500)
        llm.stop()
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        log("  模型整理失败（保留原样）：%s" % exc)
        return rows

    plan = None
    try:
        start = answer.find("{")
        end = answer.rfind("}")
        if start >= 0 and end > start:
            plan = json.loads(answer[start:end + 1])
    except (ValueError, TypeError):
        plan = None
    if not plan or not plan.get("groups"):
        return rows

    merged = []
    used = set()
    for group in plan["groups"]:
        if not isinstance(group, list):
            continue
        idxs = []
        for item in group:
            try:
                k = int(item) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= k < len(rows) and k not in used:
                idxs.append(k)
        if not idxs:
            continue
        idxs.sort()
        for k in idxs:
            used.add(k)
        start = rows[idxs[0]]["a"]
        end = rows[idxs[-1]]["b"]
        text = "".join(rows[k]["text"] for k in idxs)
        # 程序兜底：模型给出超长句时按标点再切，时间按字数比例分
        pieces = _split_long_text(text, 20)
        if len(pieces) <= 1:
            merged.append({"a": start, "b": end, "text": text})
            continue
        total_chars = sum(len(p) for p in pieces) or 1
        cursor = start
        span = max(end - start, 0.5)
        for piece in pieces:
            piece_end = cursor + span * (len(piece) / total_chars)
            merged.append({"a": round(cursor, 2), "b": round(piece_end, 2),
                           "text": piece})
            cursor = piece_end
    if not merged:
        return rows
    log("  整理后 %d 条（原 %d 条）" % (len(merged), len(rows)))
    return merged


def _align_to_voice(rows, audio, sr=16000, thresh_db=-45.0, pad=0.4,
                    log=print):
    """把每条字幕的边界对齐到音频里真正有声的位置。

    转写的时间常和实际波形差一点（开头早、结尾拖）。这里用 10ms 能量包络：
      · 起点 = 该句附近第一个超过阈值的帧；
      · 终点 = 该句附近最后一个超过阈值的帧。
    这样在 PR 里看，字幕两头正好贴着波形，不盖静音。
    """
    import numpy as np

    if not rows or len(audio) < sr:
        return rows
    hop = sr // 100  # 10ms
    frames = len(audio) // hop
    if frames <= 0:
        return rows
    block = audio[:frames * hop].reshape(frames, hop)
    rms = np.sqrt(np.mean(block ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    voiced = db > thresh_db

    changed = 0
    for row in rows:
        # 原边界附近 ±pad 秒内找有声帧
        lo = max(0, int((row["a"] - pad) * 100))
        hi = min(frames, int((row["b"] + pad) * 100))
        if hi <= lo:
            continue
        # 往前找第一个有声
        start = None
        for k in range(lo, min(hi, int(row["a"] * 100) + 20)):
            if voiced[k]:
                start = k
                break
        if start is None:
            for k in range(int(row["a"] * 100), hi):
                if voiced[k]:
                    start = k
                    break
        # 往后找最后一个有声
        end = None
        for k in range(hi - 1, max(lo, int(row["b"] * 100) - 20), -1):
            if voiced[k]:
                end = k
                break
        if end is None:
            for k in range(min(hi, int(row["b"] * 100)), lo - 1, -1):
                if voiced[k]:
                    end = k
                    break
        if start is not None and end is not None and end >= start:
            new_a = round(start / 100.0, 2)
            new_b = round((end + 1) / 100.0, 2)
            if abs(new_a - row["a"]) > 0.01 or abs(new_b - row["b"]) > 0.01:
                changed += 1
            row["a"] = new_a
            row["b"] = new_b
    if changed:
        log("  按波形校准了 %d 条字幕边界" % changed)
    # 防重叠：校准会往两边扩，导致相邻字幕叠在一起
    for i in range(len(rows) - 1):
        if rows[i]["b"] > rows[i + 1]["a"]:
            rows[i]["b"] = rows[i + 1]["a"]
    for row in rows:
        if row["b"] <= row["a"]:
            row["b"] = round(row["a"] + 0.3, 2)
    return rows


CALIBRATE_SYS = """你在校对字幕文字。下面每行格式：
  序号 | 起~止 | 文字

这些文字是语音转写出来的（有错别字、重复、断句问题），请逐行校对：
· 修正同音错别字、专有名词（比如游戏里的方块名、机器名）；
· 去掉明显的口头禅重复（"呃/那个/就是/然后"连续出现时留一个）；
· 如果一行里明显是两句话被连在一起，用逗号断开（不要拆成两行）；
· 不要改行数、不要改起止时间、不要合并或拆分行。

输出 JSON（不要多余文字）：
{"fixed": ["第1行修正后", "第2行修正后", ...]}
数量必须和输入完全一致（就是每行一条）。"""


def calibrate_subtitles(rows, base_dir, log=print):
    """Use the shared context-aware gate; never accept deletion or rewriting."""
    import subtitle_quality
    return subtitle_quality.correct_rows(rows, log=log, use_model=True)


def _voice_segments(audio, sr=16000, thresh_db=-42.0, min_gap=0.35,
                    min_speech=0.25):
    """按能量切出「说话区间」：返回 [(起, 止), ...] 秒。

    这是「说话→字幕出、说话停→字幕没」的核心：
      · 能量 > thresh_db 的连续区 = 说话；
      · 短于 min_gap 的静音（换气）并进说话区，不切断；
      · 短于 min_speech 的说话（气声/杂音）丢掉。
    """
    import numpy as np

    hop = sr // 100
    n = len(audio) // hop
    if n <= 0:
        return []
    rms = np.sqrt(np.mean(audio[:n * hop].reshape(n, hop) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    voiced = db > thresh_db

    # 短静音并进说话区（换气不算停）
    gap_frames = int(min_gap * 100)
    i = 0
    while i < n:
        if not voiced[i]:
            j = i
            while j < n and not voiced[j]:
                j += 1
            if j < n and (j - i) < gap_frames:
                voiced[i:j] = True
            i = j
        else:
            i += 1

    # 切出连续说话段
    segs = []
    i = 0
    while i < n:
        if voiced[i]:
            j = i
            while j < n and voiced[j]:
                j += 1
            if (j - i) >= int(min_speech * 100):
                segs.append((round(i / 100.0, 2), round(j / 100.0, 2)))
            i = j
        else:
            i += 1
    return segs


def _dedup_loops(text):
    """去掉 whisper 的循环重复（同一段连着吐两次）。

    whisper 偶尔会在长段里循环输出，比如"注入器的话就123456789…"连着来两遍。
    这里检测「整段对半重复」和「连续 12 字重复」，删掉后半。
    """
    text = str(text or "")
    n = len(text)
    if n < 16:
        return text
    half = n // 2
    if text[:half] == text[half:half * 2]:
        return text[:half]
    for size in range(n // 2, 7, -1):
        for start in range(0, n - 2 * size + 1):
            chunk = text[start:start + size]
            if text[start + size:start + 2 * size] == chunk:
                return text[:start + size] + text[start + 2 * size:]
    return text


def _split_long_rows(rows, max_chars=24, max_sec=6.0):
    """把过长的字幕切开（一字幕一行，超过 max_chars 字或 max_sec 秒就拆）。

    时间是按字数比例分配的（不改变整段起止）。
    """
    out = []
    for row in rows:
        text = str(row.get("text") or "")
        span = row["b"] - row["a"]
        if len(text) <= max_chars and span <= max_sec:
            out.append(row)
            continue
        # 优先按标点切（不切断词），没标点才按字数硬切
        pieces = []
        buf = ""
        for ch in text:
            buf += ch
            if len(buf) >= max_chars and ch in "，。！？、；：,.!?;: ":
                pieces.append(buf)
                buf = ""
            elif len(buf) >= max_chars * 2:
                pieces.append(buf)
                buf = ""
        if buf:
            pieces.append(buf)
        # 再按时间上限切（字数太少的孤片不要）
        total_chars = sum(len(p) for p in pieces) or 1
        cursor = row["a"]
        for piece in pieces:
            piece_span = span * (len(piece) / total_chars)
            out.append({"a": round(cursor, 2),
                        "b": round(cursor + piece_span, 2),
                        "text": piece})
            cursor += piece_span
    return out


def _rows_from_voice(voice_segs, asr_rows):
    """把说话区间和转写文字对上：每段说话 = 一条字幕。

    每个转写句只分配给「重叠最多」的说话段，避免同一句被两段各拼一遍。
    """
    buckets = [[] for _ in voice_segs]
    for asr in asr_rows:
        best = -1
        best_overlap = 0.0
        for idx, (start, end) in enumerate(voice_segs):
            overlap = min(end, asr["b"]) - max(start, asr["a"])
            if overlap > best_overlap:
                best_overlap = overlap
                best = idx
        if best >= 0 and best_overlap > 0.05:
            buckets[best].append(asr["text"])
    rows = []
    for idx, (start, end) in enumerate(voice_segs):
        if buckets[idx]:
            rows.append({"a": start, "b": end,
                         "text": "".join(buckets[idx])})
    return rows


def _merge_subtitles(rows, gap=0.3, min_chars=0.12, total=0.0):
    """合并「同一句话里的小停顿」，但保留说话停的空隙。

    whisper 的句子首尾相接（间隔≈0），但两句之间可能有几句的换气停顿：
      · 间隔 ≤ gap（0.3 秒）= 同一句话里的停顿 → 合并成一条；
      · 间隔 > gap = 说话停了 → 保留空隙（字幕该消失）。

    另外补显示时长：每条至少 max(1.2, 字数×min_chars) 秒，
    但不能越过下一条起点（否则说话停的地方又会被字幕盖住）。
    """
    if not rows:
        return rows

    merged = []
    i = 0
    while i < len(rows):
        row = dict(rows[i])
        while (i + 1 < len(rows)
               and (rows[i + 1]["a"] - row["b"]) <= gap):
            i += 1
            row["b"] = rows[i]["b"]
            row["text"] = row["text"] + rows[i]["text"]
        merged.append(row)
        i += 1

    limit = total if total > 0 else (merged[-1]["b"] if merged else 0)
    for idx, row in enumerate(merged):
        want = max(1.2, len(row["text"]) * min_chars)
        want = min(want, 8.0)
        ceiling = merged[idx + 1]["a"] if idx + 1 < len(merged) else limit
        if row["b"] - row["a"] < want:
            target = row["a"] + want
            if ceiling > row["a"]:
                target = min(target, ceiling)
            row["b"] = target
        if row["b"] <= row["a"]:
            row["b"] = round(row["a"] + 0.3, 2)
    return merged


def _srt_time(seconds):
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        ms = 999
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _write_srt(rows, path):
    import subtitles

    lines = []
    count = 0
    for row in rows:
        text = subtitles.clean_caption_text(row.get("text", ""))
        if not text:
            continue
        count += 1
        lines.append(str(count))
        lines.append("%s --> %s" % (_srt_time(row["a"]), _srt_time(row["b"])))
        lines.append(text)
        lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def build_view():
    """模型视角：PR 里每条片段 + 它覆盖到的台词。

    台词来源优先级：
      1. 工作目录\\序列字幕\\序列字幕.json（自己听序列转的，时间轴对齐）
      2. 素材索引（素材时间，按片段映射换算）
    """
    seq = load_sequence()
    if not seq:
        return {"ok": False, "error": "还没有 PR 序列数据（先在面板点「读取当前序列」）"}
    import pr_workflow
    current_fingerprint = pr_workflow.sequence_fingerprint(seq)
    try:
        import index
        current_asr_signature = str(index.asr_profile().get("signature") or "")
    except Exception:  # noqa: BLE001 - stale cache is safer than a failed view
        current_asr_signature = ""
    available_audio_tracks = {
        int(row.get("track") or 0) for row in seq.get("audio") or []
        if int(row.get("track") or 0) > 0
    }
    try:
        current_subtitle_tracks = set(_selected_transcription_tracks(
            seq, load_pr_audio_settings()))
    except (TypeError, ValueError):
        current_subtitle_tracks = set()
    # 如果做过「转写当前序列」，优先用序列字幕（时间轴时间，最准）
    sub_rows = None
    sub_json = os.path.join(SUB_DIR, "序列字幕.json")
    if os.path.isfile(sub_json):
        try:
            with open(sub_json, encoding="utf-8") as handle:
                cached = json.load(handle)
            cached_tracks = ({int(value) for value in cached.get("track_numbers") or []}
                             if isinstance(cached, dict) else set())
            if (isinstance(cached, dict)
                    and cached.get("schema") == "smartcut.sequence-subtitle-cache.v1"
                    and cached.get("sequence_fingerprint") == current_fingerprint
                    and current_asr_signature
                    and cached.get("asr_profile_signature") == current_asr_signature
                    and cached_tracks
                    and cached_tracks == current_subtitle_tracks
                    and cached_tracks.issubset(available_audio_tracks)
                    and isinstance(cached.get("segments"), list)):
                sub_rows = cached["segments"]
        except (OSError, ValueError):
            sub_rows = None
    cache = {}
    clips = []
    for number, clip in enumerate(seq["video"], 1):
        key = os.path.normcase(os.path.abspath(clip["path"]))
        if key not in cache:
            cache[key] = find_index(clip["path"])
        found = cache[key]
        segments = []
        total = 0
        if sub_rows is not None:
            # 用「序列字幕」：a/b 已经是时间轴时间，直接按片段范围裁剪
            for row in sub_rows:
                try:
                    a = float(row["a"])
                    b = float(row["b"])
                except (TypeError, ValueError, KeyError):
                    continue
                if b <= clip["start"] or a >= clip["end"]:
                    continue
                ta = max(clip["start"], a)
                tb = min(clip["end"], b)
                if tb <= ta:
                    continue
                total += 1
                segments.append({
                    "a": round(ta, 2),
                    "b": round(tb, 2),
                    "text": " ".join(str(row.get("text") or "").split())[:60],
                    "db": row.get("db"),
                    "gap": row.get("gap"),
                    "level": None,
                })
        elif found:
            data = found[0]
            for segment in data.get("segments") or []:
                try:
                    a, b = float(segment[0]), float(segment[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if b <= clip["in"] or a >= clip["out"]:
                    continue
                total += 1
                text = str(segment[2]) if len(segment) > 2 else ""
                energy = _segment_energy(data, a, b)
                # 关键：索引记的是「素材时间」，PR 时间轴是剪辑后的时间。
                # 波纹删除会让后面片段整体前移，所以必须按片段的映射换算：
                #   时间轴时间 = 片段起点 + (素材时间 - 素材入点)
                ta = clip["start"] + (a - clip["in"])
                tb = clip["start"] + (b - clip["in"])
                # 裁剪到片段范围内（跨片段边界的台词只保留落在本段的部分）
                ta = max(clip["start"], min(ta, clip["end"]))
                tb = max(clip["start"], min(tb, clip["end"]))
                if tb <= ta:
                    continue
                segments.append({
                    "a": round(ta, 2),           # 时间轴时间（给模型用）
                    "b": round(tb, 2),
                    "src_a": round(a, 2),        # 素材时间（调试用）
                    "src_b": round(b, 2),
                    "text": " ".join(text.split())[:60],
                    "db": None if energy is None else round(energy, 1),
                    "level": _energy_word(energy),
                })
        clips.append({
            "index": number,
            "track": clip["track"],
            "start": clip["start"],
            "end": clip["end"],
            "in": clip["in"],
            "out": clip["out"],
            "name": clip["name"],
            "path": clip["path"],
            "has_index": bool(found),
            "segments_in_clip": total,
            "segments": segments,
        })
    return {"ok": True, "sequence": seq["name"], "clips": clips,
            "tracks": seq.get("tracks") or [],
            "video_count": len(seq["video"]), "audio_count": len(seq["audio"])}


def _smart_edit_snapshot(include_result=False):
    with _SMART_EDIT_LOCK:
        payload = {
            "ok": True,
            "status": _SMART_EDIT_JOB.get("status") or "idle",
            "message": _SMART_EDIT_JOB.get("message") or "",
            "logs": list(_SMART_EDIT_JOB.get("logs") or []),
            "log_count": len(_SMART_EDIT_JOB.get("logs") or []),
            "progress": dict(_SMART_EDIT_JOB.get("progress") or {}),
            "error": _SMART_EDIT_JOB.get("error") or "",
            "started_at": _SMART_EDIT_JOB.get("started_at") or 0.0,
        }
        if include_result:
            payload["result"] = _SMART_EDIT_JOB.get("result")
        elif isinstance(_SMART_EDIT_JOB.get("result"), dict):
            result = _SMART_EDIT_JOB["result"]
            payload["summary"] = {
                "count": result.get("count", 0),
                "expected_shorten": result.get("expected_shorten", 0.0),
                "director_passed": result.get("director_passed"),
                "task_id": result.get("task_id"),
            }
        return payload


def start_smart_edit():
    """Start the normal SmartCut roles without rendering a video file."""
    sequence = load_sequence()
    if not sequence:
        return {"ok": False, "error": "还没有 PR 序列数据，先读取当前序列"}
    try:
        import pr_workflow
        fingerprint = pr_workflow.sequence_fingerprint(sequence)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "无法冻结 PR 时间轴：%s" % exc}

    with _SMART_EDIT_LOCK:
        if _SMART_EDIT_JOB.get("status") == "running":
            return {"ok": False, "error": "完整智能剪辑正在运行，请等待"}
        _SMART_EDIT_JOB.update({
            "status": "running", "message": "冻结 PR 时间轴，准备素材分析",
            "logs": [], "progress": {}, "result": None, "error": "",
            "fingerprint": fingerprint, "started_at": time.time(),
        })

    def job_log(message):
        text = str(message)
        _log("SMART_EDIT " + text)
        with _SMART_EDIT_LOCK:
            rows = _SMART_EDIT_JOB.setdefault("logs", [])
            rows.append(text)
            if len(rows) > 240:
                del rows[:-240]
            _SMART_EDIT_JOB["message"] = text

    def job_progress(payload):
        with _SMART_EDIT_LOCK:
            _SMART_EDIT_JOB["progress"] = dict(payload or {})

    def worker():
        try:
            video = sequence.get("video") or []
            source = str(video[0].get("path") or "") if video else ""
            found = find_index(source) if source else None
            index_dir = found[1] if found else None
            result = pr_workflow.run(
                sequence, BASE, index_dir=index_dir,
                log=job_log, on_progress=job_progress)
            result.update(allocate_project_paths(source))
            subtitle_result = pr_workflow.write_editable_subtitle_tracks(
                result.get("index_dir") or index_dir,
                result.get("keep_spans") or [], result["project_path"],
                log=job_log)
            result["subtitle_tracks"] = list(
                subtitle_result.get("tracks") or [])
            result["subtitle_count"] = int(
                subtitle_result.get("count") or 0)
            result["subtitle_converter"] = str(
                subtitle_result.get("converter") or "")
            if result["subtitle_tracks"]:
                job_log("PR 可编辑字幕：简体中文、繁体中文各 %d 条" %
                        result["subtitle_count"])
            if subtitle_result.get("warning"):
                job_log("PR 可编辑字幕：%s" % subtitle_result["warning"])
            with _SMART_EDIT_LOCK:
                _SMART_EDIT_JOB.update({
                    "status": "ready", "message": (
                        "导演定稿完成：%d 个删除区间，预计缩短 %.2f 秒" %
                        (int(result.get("count") or 0),
                         float(result.get("expected_shorten") or 0.0))),
                    "result": result,
                })
        except Exception as exc:  # noqa: BLE001
            job_log("完整智能剪辑失败：%s" % exc)
            with _SMART_EDIT_LOCK:
                _SMART_EDIT_JOB.update({
                    "status": "failed", "error": str(exc),
                    "message": "完整智能剪辑失败：%s" % exc,
                })

    threading.Thread(target=worker, name="pr-smart-edit", daemon=True).start()
    return _smart_edit_snapshot()


def claim_smart_edit_result():
    """Return a ready plan only if Premiere still matches the frozen snapshot."""
    snapshot = _smart_edit_snapshot(include_result=True)
    if snapshot.get("status") != "ready":
        return {"ok": False, "error": "完整智能剪辑尚未生成可执行方案",
                "status": snapshot.get("status")}
    sequence = load_sequence()
    if not sequence:
        return {"ok": False, "error": "无法重新读取当前 PR 序列"}
    import pr_workflow
    current = pr_workflow.sequence_fingerprint(sequence)
    with _SMART_EDIT_LOCK:
        expected = _SMART_EDIT_JOB.get("fingerprint") or ""
    if current != expected:
        return {"ok": False,
                "error": "规划期间 PR 时间轴已经变化；旧方案已拒绝执行，请重新智能剪辑",
                "stale": True}
    return {"ok": True, "result": snapshot.get("result")}


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartCutBridge/0.1"
    max_request_bytes = 16 * 1024 * 1024

    def _cors_origin(self):
        return _allowed_origin(self.headers.get("Origin"))

    def _send(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        if self._cors_origin() is None:
            self._send({"ok": False, "error": "不允许的请求来源"}, 403)
            return False
        expected = str(getattr(self.server, "bridge_token", BRIDGE_TOKEN) or "")
        provided = str(self.headers.get(BRIDGE_HEADER) or "")
        if not expected or not hmac.compare_digest(expected, provided):
            self._send({"ok": False,
                        "error": "Premiere 桥会话无效，请重启 SmartCut 和插件"}, 401)
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._authorised():
            return
        try:
            self._route_get()
        except Exception as exc:  # noqa: BLE001
            try:
                self._send({"ok": False, "error": "服务异常：%s" % exc})
            except Exception:  # noqa: BLE001
                pass

    def _route_get(self):
        path = (self.path or "/").split("?")[0]
        _log("GET " + path)
        if path == "/health":
            return self._send({"ok": True, "app": "smartcut", "port": PORT})
        if path == "/audio-settings":
            return self._send(load_pr_audio_settings())
        if path == "/timeline":
            data = build_timeline()
            if not data:
                return self._send({"ok": False,
                                   "error": "还没有剪好的时间轴（先在软件里剪一条）"})
            return self._send({"ok": True, "timeline": data})
        if path == "/view":
            return self._send(build_view())
        if path == "/smart-edit/status":
            return self._send(_smart_edit_snapshot())
        if path in ("/pending", "/transcribe", "/step", "/plan",
                    "/smart-edit/result"):
            return self._send({"ok": False, "error": "此操作只接受 POST"}, 405)
        return self._send({"ok": False, "error": "not found"}, 404)

    def do_OPTIONS(self):  # noqa: N802
        origin = self._cors_origin()
        if origin is None:
            return self._send({"ok": False, "error": "不允许的请求来源"}, 403)
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, " + BRIDGE_HEADER)
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        """PR 面板发来的数据：/sequence=当前序列，/probe=API 枚举，/key=发按键。"""
        if not self._authorised():
            return
        path = (self.path or "/").split("?")[0]
        if path not in ("/sequence", "/probe", "/key", "/focus",
                        "/smart-edit/start", "/audio-settings", "/pending",
                        "/transcribe", "/step", "/plan", "/smart-edit/result"):
            return self._send({"ok": False, "error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length < 0 or length > self.max_request_bytes:
            return self._send({"ok": False, "error": "请求数据过大"}, 413)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        empty_allowed = ("/focus", "/smart-edit/start", "/pending",
                         "/transcribe", "/step", "/plan", "/smart-edit/result")
        if not raw.strip() and path not in empty_allowed:
            return self._send({"ok": False, "error": "空数据"}, 400)
        if path == "/pending":
            action = _PENDING.get("action")
            _PENDING["action"] = None
            return self._send({"ok": True, "action": action})
        if path == "/transcribe":
            try:
                requested_tracks = None
                if raw.strip():
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("字幕音轨参数格式无效")
                    requested_tracks = payload.get("tracks", "")
                result = transcribe_sequence(
                    log=print, requested_tracks=requested_tracks)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._send({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                return self._send({"ok": False, "error": "转写失败：%s" % exc})
            return self._send(result)
        if path == "/step":
            view = build_view()
            if not view.get("ok"):
                return self._send(view)
            try:
                import pr_engine
                result = pr_engine.step(view, BASE, log=print)
            except Exception as exc:  # noqa: BLE001
                return self._send({"ok": False, "error": "AI 出方案失败：%s" % exc})
            return self._send(result)
        if path == "/plan":
            view = build_view()
            if not view.get("ok"):
                return self._send(view)
            try:
                import pr_engine
                result = pr_engine.plan(view, BASE, log=print)
            except Exception as exc:  # noqa: BLE001
                return self._send({"ok": False, "error": "AI 剪辑失败：%s" % exc})
            try:
                clips = view.get("clips") or []
                cache = dict(result)
                cache["source"] = (clips[0].get("path") if clips else "")
                cache["media_duration"] = max(
                    (float(c["out"]) for c in clips), default=0.0)
                with open(os.path.join(BASE, "工作目录", "last_plan.json"),
                          "w", encoding="utf-8") as handle:
                    json.dump(cache, handle, ensure_ascii=False)
            except (OSError, ValueError, TypeError):
                pass
            return self._send(result)
        if path == "/smart-edit/result":
            return self._send(claim_smart_edit_result())
        if path == "/audio-settings":
            try:
                payload = json.loads(raw)
                saved = save_pr_audio_settings(payload)
            except (ValueError, TypeError, OSError) as exc:
                return self._send({"ok": False, "error": str(exc)}, 400)
            _log("EDIT_SETTINGS keep=%.0f%% strategy=%s tracks=%s output=%s" %
                 (float(saved.get("keep_ratio") or 0.0) * 100.0,
                  saved.get("cut_level"),
                  saved.get("tracks") or "自动", saved.get("output_mode")))
            return self._send(saved)
        if path == "/smart-edit/start":
            return self._send(start_smart_edit())
        # 发按键：对 PR 主窗口发 WM_KEYDOWN（实测有效，不依赖插件接口）
        if path == "/key":
            ok, message = send_keys(raw.strip())
            _log("KEY %s → %s" % (raw.strip(), message))
            return self._send({"ok": ok, "message": message})
        # 抢焦点：点一下时间轴（调用方随后要重新 seek）
        if path == "/focus":
            ok, message = click_timeline()
            _log("FOCUS → %s" % message)
            return self._send({"ok": ok, "message": message})
        target = SEQ_FILE if path == "/sequence" else PROBE_FILE
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(raw)
        except OSError as exc:
            return self._send({"ok": False, "error": str(exc)}, 500)
        if path == "/probe":
            _log("PROBE 已收到 %d 字节" % len(raw))
            return self._send({"ok": True, "saved": target, "bytes": len(raw)})
        video = sum(1 for line in raw.splitlines() if line.startswith("V|"))
        audio = sum(1 for line in raw.splitlines() if line.startswith("A|"))
        head = raw.splitlines()[0] if raw.splitlines() else ""
        return self._send({"ok": True, "video_clips": video,
                           "audio_clips": audio, "seq_head": head,
                           "saved": SEQ_FILE})

    def log_message(self, fmt, *args):
        print("[bridge] " + (fmt % args))


def click_timeline():
    """点一下 PR 窗口下部（时间轴区域），把键盘焦点从 CEP 面板抢回来。

    点面板按钮后焦点在面板里，编辑命令会被面板吃掉；先点时间轴就能解决。
    （点击会移动播放头，所以调用方要在点击后重新 seek。）
    """
    import ctypes
    from ctypes import wintypes

    hwnd = _pr_hwnd()
    if not hwnd:
        return False, "找不到 PR 窗口"
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return False, "拿不到窗口矩形"
    x = (rect.left + rect.right) // 2
    # 点在「时间轴顶部的空白带」（标尺下方、轨道上方）：那里没有文字和片段，
    # 不会误触改名/选中，又能把焦点交给时间轴面板。
    # 55% 会落到节目监视器（实测偏上），所以用 60%。
    y = rect.top + int((rect.bottom - rect.top) * 0.60)
    user32.SetCursorPos(x, y)
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # 左键按下
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # 左键抬起
    time.sleep(0.15)
    return True, "已点击时间轴 (%d,%d)" % (x, y)


# 热键 → 面板的指令队列（面板轮询 /pending 取走再执行）
_PENDING = {"action": None}


def start_hotkeys(log=print):
    """注册全局热键：F9 = 让面板执行「AI 剪一步」。

    为什么用「热键 + 队列」而不是「按钮 + 点坐标」：
    点坐标依赖窗口布局，换人换机器就失效；而热键是系统级的，
    你按 F9 时焦点本来就在时间轴，随后发的编辑命令一定生效。
    """
    import threading

    def loop():
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                          wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        MOD_NOREPEAT = 0x4000
        VK_F9 = 0x78
        ok = user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F9)
        if not ok:
            log("[热键] F9 注册失败（可能被别的程序占用）")
            return
        log("[热键] F9 = AI 剪一步（已就绪）")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                _PENDING["action"] = "ai_step"
                _log("HOTKEY F9 → 已排入 AI 剪一步")

    threading.Thread(target=loop, daemon=True).start()


def _setup_cuda_dlls():
    """把 venv 里 nvidia 的 DLL 目录加进 PATH。

    ctranslate2（faster-whisper 的推理后端）需要 cublas64_12.dll / cudnn，
    这些 DLL 装在 site-packages\\nvidia\\*\\bin 里，不在 PATH 里就会报
    "Library cublas64_12.dll is not found"。
    """
    import glob

    base = os.path.join(BASE, ".venv", "Lib", "site-packages", "nvidia")
    if not os.path.isdir(base):
        return
    for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
        for folder in glob.glob(os.path.join(base, sub, "bin")):
            os.environ["PATH"] = folder + os.pathsep + os.environ["PATH"]
            try:
                os.add_dll_directory(folder)
            except (AttributeError, OSError):
                pass


def main():
    global BRIDGE_TOKEN
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    token = write_bridge_session(port=PORT)
    BRIDGE_TOKEN = token
    server.bridge_token = token
    print("PR 桥已启动：http://127.0.0.1:%d/health" % PORT)
    print("剪辑单接口：http://127.0.0.1:%d/timeline" % PORT)
    _setup_cuda_dlls()
    start_hotkeys()
    sample = build_timeline()
    if sample:
        print("当前可交：%s（%d 段，计划 %s）"
              % (sample["name"], sample["count"], sample["plan"]))
    else:
        print("当前还没有剪好的时间轴。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        clear_bridge_session(token)


if __name__ == "__main__":
    main()

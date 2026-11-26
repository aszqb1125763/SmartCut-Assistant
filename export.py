# -*- coding: utf-8 -*-
"""
导出：把选中的卡片变成成片

两种模式：
    单段导出 —— 每张卡片一个文件（做 B-roll、素材库）
    合并一条 —— 按卡片顺序拼成一条成片（直接进剪映/PR）
"""


from __future__ import annotations

try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass


import os
import re
import shutil
import subprocess
import tempfile

import index as indexer

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def find_ffmpeg() -> str:
    from index import find_ffmpeg as _f
    return _f()


def _text(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run_progress(cmd, total: float, on_progress=None, should_cancel=None) -> None:
    """跑 ffmpeg，按 out_time 上报进度。"""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        for raw in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                raise RuntimeError("已取消")
            line = _text(raw).strip()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if on_progress and total > 0:
                    on_progress(min(0.999, max(0.0, us / 1_000_000.0 / total)))
        _out, err = proc.communicate()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        tail = "\n".join(_text(err).strip().splitlines()[-6:])
        raise RuntimeError(f"ffmpeg 导出失败（{proc.returncode}）：\n{tail}")


def _video_opts(crf: int = 20, preset: str = "veryfast") -> list[str]:
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart"]


def safe_name(text: str, fallback: str) -> str:
    """拿台词当前缀文件名，去掉 Windows 不允许的字符。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "", text).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return fallback
    return text[:24].rstrip(" .")


def _cut_copy(src: str, start: float, dur: float, out: str,
              audio_stream: int = 0) -> None:
    """不重编码直接切一段（秒出，但起点会退到最近的关键帧）。"""
    ff = find_ffmpeg()
    cmd = [ff, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-map", f"0:a:{audio_stream}",
           "-c", "copy", "-avoid_negative_ts", "make_zero", out]
    proc = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        tail = "\n".join(_text(proc.stderr).strip().splitlines()[-6:])
        raise RuntimeError(f"快速切分失败（{proc.returncode}）：\n{tail}")


def _preview_audio_ordinals(src: str, audio_streams=None,
                            fallback_audio_stream: int | None = 0) -> list[int]:
    """把索引里的音轨信息归一化为 ``0:a:N`` 使用的序号。"""
    actual_rows = None
    try:
        actual_rows = indexer.probe_streams(src).get("audio_streams") or []
    except Exception:  # noqa: BLE001 - 探测失败时仍按调用方提供的序号尝试
        pass
    rows = audio_streams
    if rows is None:
        rows = (actual_rows if actual_rows is not None else
                ([fallback_audio_stream] if isinstance(fallback_audio_stream, int) else []))

    ordinals: list[int] = []
    for row in rows or []:
        value = row.get("ordinal") if isinstance(row, dict) else row
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value >= 0 and value not in ordinals:
            ordinals.append(value)
    if actual_rows is not None:
        available = {int(row.get("ordinal", -1)) for row in actual_rows
                     if isinstance(row, dict)}
        requested = list(ordinals)
        ordinals = [value for value in ordinals if value in available]
        # 同名源文件被替换后，旧索引中的非空音轨列表可能全部越界。
        if requested and not ordinals and available:
            ordinals = sorted(available)
    return ordinals


def _cut_preview(src: str, start: float, dur: float, out: str,
                 audio_streams=None,
                 fallback_audio_stream: int | None = 0) -> list[int]:
    """精确生成卡片预览；有多条音轨时全部混成播放器可听的一条。

    正式快速导出仍使用 :func:`_cut_copy`。预览必须重编码视频才能从卡片的
    精确起点播放；同时这也给多轨 ``amix`` 提供了统一的音频输出格式。
    返回实际使用的音轨序号，便于界面提示和自动测试。
    """
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    dur = max(0.05, float(dur))
    start = max(0.0, float(start))
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)

    tracks = _preview_audio_ordinals(
        src, audio_streams=audio_streams,
        fallback_audio_stream=fallback_audio_stream,
    )
    ff = find_ffmpeg()
    cmd = [ff, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]

    maps = ["-map", "0:v:0"]
    if len(tracks) > 1:
        graph_parts = []
        for i, track in enumerate(tracks):
            graph_parts.append(
                "[0:a:%d]asetpts=PTS-STARTPTS,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
                "[m%d]" % (track, i)
            )
        inputs = "".join("[m%d]" % i for i in range(len(tracks)))
        graph_parts.append(
            "%samix=inputs=%d:duration=longest:normalize=0,"
            "alimiter=limit=0.9:level=false[aout]" % (inputs, len(tracks))
        )
        cmd += ["-filter_complex", ";".join(graph_parts)]
        maps += ["-map", "[aout]"]
    elif tracks:
        maps += ["-map", "0:a:%d" % tracks[0]]
    else:
        maps += ["-an"]

    cmd += maps + [
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p",
    ]
    if tracks:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += ["-movflags", "+faststart", out]

    proc = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        try:
            os.remove(out)
        except OSError:
            pass
        tail = "\n".join(_text(proc.stderr).strip().splitlines()[-8:])
        raise RuntimeError(f"预览片段生成失败（{proc.returncode}）：\n{tail}")
    if not os.path.isfile(out) or os.path.getsize(out) <= 0:
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError("预览片段没有生成有效文件")
    return tracks


def export_single(src: str, cards: list[dict], out_dir: str,
                  on_progress=None, on_log=None, should_cancel=None,
                  crf: int = 20, fast: bool = False,
                  audio_stream: int = 0) -> list[str]:
    """每张卡片单独导出成一个 mp4。"""
    os.makedirs(out_dir, exist_ok=True)
    ff = find_ffmpeg()
    total = sum(max(0.1, c["end"] - c["start"]) for c in cards)
    done_time = 0.0
    files: list[str] = []

    for i, c in enumerate(cards, 1):
        if should_cancel and should_cancel():
            raise RuntimeError("已取消")
        start, end = float(c["start"]), float(c["end"])
        # 惊讶/激动/笑声这类反应，多留一点尾巴，别把表情/反应剪断
        emo = c.get("emotion") or "普通"
        tail = 1.2 if emo in ("惊讶", "激动") else (0.8 if emo == "笑声" else 0.0)
        end = end + tail
        dur = max(0.1, end - start)
        base = safe_name(c.get("text", ""), "")
        stamp = "%02d%02d" % (int(start // 60), int(start % 60))
        name = f"{i:03d}_{stamp}_{emo}_{base or '片段'}.mp4"
        out = os.path.join(out_dir, name)
        n = 1
        while os.path.exists(out):
            out = os.path.join(out_dir, f"{i:02d}_{c['id']}_{base or '片段'}_{n}.mp4")
            n += 1

        if on_log:
            on_log(f"导出第 {i}/{len(cards)} 段：{c['id']} {start:.1f}s → {end:.1f}s")

        if fast:
            _cut_copy(src, start, dur, out, audio_stream)
            done_time += dur
            if on_progress and total > 0:
                on_progress(min(0.999, done_time / total))
            files.append(out)
            continue

        cmd = [ff, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}",
               "-map", "0:v:0", "-map", f"0:a:{audio_stream}",
               *_video_opts(crf), "-progress", "pipe:1", out]

        def local(p, _base=done_time):
            if on_progress and total > 0:
                on_progress(min(0.999, (_base + p * dur) / total))

        _run_progress(cmd, dur, on_progress=local, should_cancel=should_cancel)
        done_time += dur
        files.append(out)

    if on_progress:
        on_progress(1.0)
    return files


def export_merged(src: str, cards: list[dict], out_path: str,
                  on_progress=None, on_log=None, should_cancel=None,
                  crf: int = 20, fast: bool = False,
                  audio_stream: int = 0) -> str:
    """
    按卡片顺序拼成一条成片。

    注意：这里不做快速模式。直接复制会因为"只能在关键帧下刀"，每段开头多出
    几秒（实测三段 66 秒被拉成 77.7 秒），拼出来的成片是错位的。
    所以合并一律重新编码，保证切点准确。
    """
    if not cards:
        raise ValueError("没有选中任何片段。")
    if fast and on_log:
        on_log("合并需要重新编码才能切准，快速模式对合并不生效（单段导出才用快速模式）")
    ff = find_ffmpeg()
    total = sum(max(0.05, c["end"] - c["start"]) for c in cards)
    a_idx = f"0:a:{audio_stream}"

    parts: list[str] = []
    labels: list[str] = []
    single = len(cards) == 1
    for i, c in enumerate(cards):
        a, b = float(c["start"]), float(c["end"])
        vl = "vout" if single else f"v{i}"
        al = "aout" if single else f"a{i}"
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[{vl}]")
        parts.append(f"[{a_idx}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[{al}]")
        labels.append(f"[v{i}][a{i}]")
    if not single:
        parts.append("".join(labels) + f"concat=n={len(cards)}:v=1:a=1[vout][aout]")

    fd, script = tempfile.mkstemp(suffix=".txt", prefix="cards_",
                              dir=indexer.temp_root())
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(";\n".join(parts))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    cmd = [ff, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-i", src, "-filter_complex_script", script,
           "-map", "[vout]", "-map", "[aout]", *_video_opts(crf),
           "-progress", "pipe:1", out_path]
    if on_log:
        on_log(f"合并 {len(cards)} 段，总长 {total:.1f} 秒")
    try:
        _run_progress(cmd, total, on_progress=on_progress, should_cancel=should_cancel)
    finally:
        try:
            os.remove(script)
        except OSError:
            pass
    if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("合并导出没有生成有效文件：%s" % out_path)
    return out_path

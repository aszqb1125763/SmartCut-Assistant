# -*- coding: utf-8 -*-
"""按「你原序列的结构」导出 XML：几条轨、每轨几个片段、各自的 in/out，全部照搬；
AI 判定要删的区间扣掉，其余原样保留（切点可拖）。

和 export_xml.py 的区别：
  · export_xml.py   = 单素材 + 保留段 → 一条视频轨 + N 条音频轨（硬编码结构）
  · export_seq_xml  = 读 pr_sequence.txt（你 PR 里那条序列的真实结构）+ 删除区间
                      → 原样复刻轨道数/片段/in-out，只是把删掉的部分去掉

用法：
    .venv\\Scripts\\python.exe export_seq_xml.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from xml.sax.saxutils import escape

BASE = os.path.dirname(os.path.abspath(__file__))
SEQ_FILE = os.path.join(BASE, "工作目录", "pr_sequence.txt")
PLAN_FILE = os.path.join(BASE, "工作目录", "last_plan.json")
OUT_DIR = os.path.join(BASE, "工作目录", "导出XML")

FPS = 60
WIDTH = 1920
HEIGHT = 1080
SAMPLE_RATE = 48000


def _frames(seconds: float) -> int:
    return int(round(float(seconds) * FPS))


def _path_url(path: str) -> str:
    posix = path.replace("\\", "/")
    return "file://localhost/" + urllib.parse.quote(posix, safe="/:")


def load_sequence():
    """解析 pr_sequence.txt：返回 (head, tracks)。tracks = {"V": {轨号: [片段]}, "A": {...}}"""
    if not os.path.isfile(SEQ_FILE):
        return None
    with open(SEQ_FILE, encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if line.strip()]
    if not lines:
        return None
    head = lines[0].split("|")
    tracks = {"V": {}, "A": {}}
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < 8 or parts[0] not in ("V", "A"):
            continue
        try:
            track = int(parts[1])
            clip = {
                "kind": parts[0], "track": track,
                "start": float(parts[2]), "end": float(parts[3]),
                "in": float(parts[4]), "out": float(parts[5]),
                "name": parts[6], "path": parts[7],
            }
        except (TypeError, ValueError):
            continue
        tracks[clip["kind"]].setdefault(track, []).append(clip)
    for kind in tracks:
        for track in tracks[kind]:
            tracks[kind][track].sort(key=lambda c: c["start"])
    return head, tracks


def load_cuts():
    """AI 判定的删除区间（PR 时间轴）。"""
    if not os.path.isfile(PLAN_FILE):
        return []
    try:
        with open(PLAN_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return [[float(a), float(b)] for a, b in (data.get("ranges") or [])]
    except (OSError, ValueError, TypeError):
        return []


def _overlaps(start, end, cuts):
    for a, b in cuts:
        if min(end, b) - max(start, a) > 0.05:
            return True
    return False


def split_clip(clip, cuts):
    """把一个片段按删除区间切成若干保留段（素材时间）。

    删除区间是 PR 时间轴，先换算到素材时间：
        素材时间 = clip.in + (PR时间 - clip.start)
    这样片段内部的删除才精确，而不是"碰到就整段丢"。
    """
    clip_in = float(clip["in"])
    clip_out = float(clip["out"])
    clip_start = float(clip["start"])
    cuts_in_media = []
    for ca, cb in sorted(cuts):
        ma = clip_in + (ca - clip_start)
        mb = clip_in + (cb - clip_start)
        lo, hi = max(clip_in, ma), min(clip_out, mb)
        if hi - lo > 0.05:
            cuts_in_media.append((lo, hi))
    segments = []
    cursor = clip_in
    for lo, hi in cuts_in_media:
        if lo - cursor > 0.05:
            segments.append((cursor, lo))
        cursor = max(cursor, hi)
    if clip_out - cursor > 0.05:
        segments.append((cursor, clip_out))
    return segments


def _extract_audio_stream(src, stream_index, out_dir):
    """把素材的第 N 条音频流单独提取成 m4a（缓存，已存在就复用）。

    为什么要拆：PR 导入 XML 时会把「同一素材的多个音频 clipitem」合并成
    Merged Clip（就是那个「混合」）。每条音轨引用各自独立的文件，它就没有
    可合并的对象了，多音轨才能保持独立。
    """
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "stream_%d.m4a" % (stream_index + 1))
    if os.path.isfile(out) and os.path.getsize(out) > 1024:
        return out
    ffmpeg = os.path.join(BASE, "bin", "ffmpeg.exe")
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", src, "-map", "0:a:%d" % stream_index, "-vn",
             "-c:a", "copy", out],
            capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if os.path.isfile(out) and os.path.getsize(out) > 1024 else None


def _clip_block(clip, timeline_start, timeline_end, file_id, audio_index=None,
                media_path=None, media_duration=None):
    begin = _frames(clip["in"])
    finish = _frames(clip["out"])
    # 音频用「单独提取出来的流文件」，视频用原素材——这样 PR 不会把多条
    # 音频合并成 Merged Clip（不同文件 = 不同素材，没有可合并的对象）。
    use_path = media_path or clip["path"]
    name = escape(os.path.basename(use_path) or clip["name"])
    url = escape(_path_url(use_path))
    media_frames = media_duration or max(finish, begin + 1)

    # 关键：视频 file 只声明 video、音频 file 只声明 audio。
    # 两类都写 video+audio 时，PR 会把它们当成"同一素材的多流"合并成「混合」，
    # 多音轨就变成了一个 Merged Clip。
    if audio_index is None:
        source_track = ""
        media_block = f"""          <media>
            <video>
              <samplecharacteristics>
                <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
                <width>{WIDTH}</width>
                <height>{HEIGHT}</height>
                <anamorphic>FALSE</anamorphic>
                <pixelaspectratio>square</pixelaspectratio>
              </samplecharacteristics>
            </video>
          </media>"""
    else:
        source_track = f"""        <sourcetrack>
          <mediatype>audio</mediatype>
          <trackindex>{audio_index}</trackindex>
        </sourcetrack>
"""
        media_block = f"""          <media>
            <audio>
              <samplecharacteristics>
                <depth>16</depth>
                <samplerate>{SAMPLE_RATE}</samplerate>
              </samplecharacteristics>
              <channelcount>2</channelcount>
            </audio>
          </media>"""

    return f"""      <clipitem id="{file_id}">
        <name>{name}</name>
        <duration>{media_frames}</duration>
        <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
        <start>{timeline_start}</start>
        <end>{timeline_end}</end>
        <in>{begin}</in>
        <out>{finish}</out>
{source_track}        <file id="{file_id}-file">
          <name>{name}</name>
          <pathurl>{url}</pathurl>
          <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
          <duration>{media_frames}</duration>
{media_block}
        </file>
      </clipitem>"""


def build_xml(head, tracks, cuts, out_path):
    video_tracks = []
    audio_tracks = []
    for kind, bucket in (("V", video_tracks), ("A", audio_tracks)):
        for track_no in sorted(tracks.get(kind) or {}):
            clips = tracks[kind][track_no]
            items = []
            cursor = 0
            for index, clip in enumerate(clips, 1):
                # 片段按删除区间切成保留段；每段一个 clipitem，时间轴顺次累加
                for seg_no, (seg_in, seg_out) in enumerate(
                        split_clip(clip, cuts), 1):
                    length = _frames(seg_out) - _frames(seg_in)
                    if length <= 0:
                        continue
                    piece = dict(clip)
                    piece["in"] = seg_in
                    piece["out"] = seg_out
                    audio_index = track_no if kind == "A" else None
                    # 默认引用**原始素材**（同一个 mp4）：序列只是编辑记录，
                    # 素材链接必须指向原文件，用户还要继续编辑/换素材。
                    media_path = None
                    if "--split-audio" in sys.argv:
                        # 备用：把每条音频流拆成独立文件（音轨绝对独立，但素材变新文件）
                        media_path = _extract_audio_stream(
                            clip["path"], track_no - 1,
                            os.path.join(OUT_DIR, "_audio"))
                    items.append(_clip_block(
                        piece, cursor, cursor + length,
                        "c-%s%d-%d-%d" % (kind, track_no, index, seg_no),
                        audio_index=audio_index, media_path=media_path))
                    cursor += length
            if items:
                bucket.append("\n".join(items))

    video_xml = ""
    for items in video_tracks:
        video_xml += f"""
        <track>
{items}
        </track>"""
    audio_xml = ""
    for items in audio_tracks:
        audio_xml += f"""
        <track>
{items}
        </track>"""
    if not video_tracks:
        return None

    # 总时长按最长的那条轨算（切段之后的有效长度）
    total = 0
    for kind in ("V", "A"):
        for track_no, clips in (tracks.get(kind) or {}).items():
            length = 0
            for c in clips:
                for seg_in, seg_out in split_clip(c, cuts):
                    length += max(0, _frames(seg_out) - _frames(seg_in))
            total = max(total, length)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <sequence id="sequence-1">
    <name>SmartCut 原序列复刻</name>
    <duration>{total}</duration>
    <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
    <timecode>
      <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
      <string>00:00:00:00</string>
      <frame>0</frame>
      <displayformat>NDF</displayformat>
    </timecode>
    <media>
      <video>
        <format>
          <samplecharacteristics>
            <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
            <width>{WIDTH}</width>
            <height>{HEIGHT}</height>
            <anamorphic>FALSE</anamorphic>
            <pixelaspectratio>square</pixelaspectratio>
          </samplecharacteristics>
        </format>{video_xml}
      </video>
      <audio>
        <format>
          <samplecharacteristics>
            <depth>16</depth>
            <samplerate>{SAMPLE_RATE}</samplerate>
          </samplecharacteristics>
        </format>{audio_xml}
      </audio>
    </media>
  </sequence>
</xmeml>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(xml)
    return out_path


def main():
    loaded = load_sequence()
    if not loaded:
        print("没有序列数据：先在面板点「读取当前序列」")
        return 1
    head, tracks = loaded
    cuts = load_cuts()
    name = head[1] if len(head) > 1 else "seq"
    out = os.path.join(OUT_DIR, "原序列复刻.xml")
    path = build_xml(head, tracks, cuts, out)
    if not path:
        print("序列里没有可用片段")
        return 1
    v = len(tracks.get("V") or {})
    a = len(tracks.get("A") or {})
    vc = sum(len(x) for x in (tracks.get("V") or {}).values())
    print(path)
    print("原序列「%s」：视频轨 %d 条 / 音频轨 %d 条 / 视频片段 %d 个"
          % (name, v, a, vc))
    print("扣掉的删除区间：%d 个" % len(cuts))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""把「保留段」导出成 Premiere 能直接导入的 FCP7 XML 时间轴。

子枫要的效果（2026-11-25）：
    「我能在 PR 里看时间轴、后期能微调、切点的接缝」
PR 导不进脚本剪辑，但原生支持「文件 → 导入 → Final Cut Pro XML」，
导入后就是一条正常时间轴：每个切点都是可拖的编辑点。

用法（小样）：
    .venv\\Scripts\\python.exe export_xml.py --demo
用法（按最近一次 AI 保留段，整条）：
    .venv\\Scripts\\python.exe export_xml.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from xml.sax.saxutils import escape

BASE = os.path.dirname(os.path.abspath(__file__))
SEQ_FILE = os.path.join(BASE, "工作目录", "pr_sequence.txt")
OUT_DIR = os.path.join(BASE, "工作目录", "导出XML")

# 素材信息（导出前用 ffprobe 确认过：1920x1080 / 60fps）
FPS = 60
WIDTH = 1920
HEIGHT = 1080
SAMPLE_RATE = 48000


def _audio_stream_count(src: str) -> int:
    """素材有几条音频流——多音轨要原样带进 XML。"""
    ffprobe = os.path.join(BASE, "bin", "ffprobe.exe")
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", src],
            capture_output=True, text=True, timeout=60)
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        return max(1, len(lines))
    except (OSError, subprocess.SubprocessError):
        return 1


def _frames(seconds: float) -> int:
    return int(round(float(seconds) * FPS))


def _path_url(path: str) -> str:
    """FCP7 的 pathurl：file://localhost/绝对路径（URL 编码）。"""
    posix = path.replace("\\", "/")
    return "file://localhost/" + urllib.parse.quote(posix, safe="/:")


def _file_block(file_id: str, src: str, duration_frames: int) -> str:
    name = escape(os.path.basename(src))
    url = escape(_path_url(src))
    return f"""      <file id="{file_id}">
        <name>{name}</name>
        <pathurl>{url}</pathurl>
        <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
        <duration>{duration_frames}</duration>
        <media>
          <video>
            <samplecharacteristics>
              <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
              <width>{WIDTH}</width>
              <height>{HEIGHT}</height>
              <anamorphic>FALSE</anamorphic>
              <pixelaspectratio>square</pixelaspectratio>
              <fielddominance>none</fielddominance>
            </samplecharacteristics>
          </video>
          <audio>
            <samplecharacteristics>
              <depth>16</depth>
              <samplerate>{SAMPLE_RATE}</samplerate>
            </samplecharacteristics>
            <channelcount>2</channelcount>
          </audio>
        </media>
      </file>"""


def build_xml(src: str, keeps, out_path: str, media_duration: float,
              name: str = "SmartCut 剪辑") -> str:
    """keeps: [(素材起点秒, 素材终点秒), ...]；音频轨按素材的音频流数原样生成。"""
    media_frames = _frames(media_duration)
    audio_count = _audio_stream_count(src)
    cursor = 0  # 时间轴游标（帧）
    video_items = []
    audio_tracks = [[] for _ in range(audio_count)]
    for index, (start, end) in enumerate(keeps, 1):
        begin = _frames(start)
        finish = _frames(end)
        length = max(0, finish - begin)
        if length <= 0:
            continue
        timeline_start = cursor
        timeline_end = cursor + length
        cursor = timeline_end
        file_id = "file-1"
        common = f"""      <duration>{media_frames}</duration>
      <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
      <start>{timeline_start}</start>
      <end>{timeline_end}</end>
      <in>{begin}</in>
      <out>{finish}</out>
      <alphatype>none</alphatype>
"""
        video_items.append(
            f"""    <clipitem id="clipitem-v{index}">
      <name>{escape(os.path.basename(src))}</name>
{common}{_file_block(file_id, src, media_frames)}
    </clipitem>""")
        # 每条音频流一轨，轨内每个保留段一个 clipitem
        for track_index in range(1, audio_count + 1):
            audio_tracks[track_index - 1].append(
                f"""    <clipitem id="clipitem-a{track_index}-{index}">
      <name>{escape(os.path.basename(src))}</name>
{common}      <sourcetrack>
        <mediatype>audio</mediatype>
        <trackindex>{track_index}</trackindex>
      </sourcetrack>
{_file_block(file_id, src, media_frames)}
    </clipitem>""")

    audio_xml = ""
    for track_index, items in enumerate(audio_tracks, 1):
        audio_xml += f"""
        <track>
{chr(10).join(items)}
        </track>"""

    sequence_duration = cursor
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <sequence id="sequence-1">
    <name>{escape(name)}</name>
    <duration>{sequence_duration}</duration>
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
            <fielddominance>none</fielddominance>
          </samplecharacteristics>
        </format>
        <track>
{chr(10).join(video_items)}
        </track>
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


def _latest_plan():
    """从最近一次 /plan 的输出里取素材和保留段（读 pr_sequence + 再算一次不可行，改读缓存）。"""
    cache = os.path.join(BASE, "工作目录", "last_plan.json")
    if not os.path.isfile(cache):
        return None
    try:
        with open(cache, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def main():
    demo = "--demo" in sys.argv
    if demo:
        # 小样：从当前素材里取 3 小段，每段 8 秒，方便快速验证导入
        src = r"D:\Media\demo_input.mp4"
        if not os.path.isfile(src):
            print("小样素材不存在：", src)
            return 1
        keeps = [(0.0, 8.0), (250.69, 258.69), (505.5, 513.5)]
        out = os.path.join(OUT_DIR, "小样_3段24秒.xml")
        build_xml(src, keeps, out, 561.87, name="SmartCut 小样")
        print(out)
        print("预期：3 段、每段 8 秒，共 24 秒")
        return 0

    data = _latest_plan()
    if not data:
        print("没有缓存的最新计划（先点一次一键，会写 工作目录/last_plan.json）")
        return 1
    src = data.get("source") or ""
    keeps = data.get("keep_segments") or []
    if not src or not keeps:
        print("计划里缺素材或保留段")
        return 1
    out = os.path.join(OUT_DIR, "整条_%d段.xml" % len(keeps))
    build_xml(src, keeps, out, float(data.get("media_duration") or 0) or
              max(b for _a, b in keeps), name="SmartCut 剪辑")
    print(out)
    print("预期：%d 段，共 %.1f 秒" % (len(keeps), sum(b - a for a, b in keeps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

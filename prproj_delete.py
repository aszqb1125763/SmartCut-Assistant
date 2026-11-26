# -*- coding: utf-8 -*-
"""直接修改 .prproj：删除一个时间区间（视频轨 + 音频轨同步），后面自动前移。

依据 2026-11-25 逆向结论：
  · 轨道 = <VideoClipTrack>/<AudioClipTrack>（ObjectUID 标识）
  · 轨道上的片段 = <ClipItems><TrackItems><TrackItem Index="N" ObjectRef="ID"/>
  · TrackItem 的时间轴位置 = <Start>/<End>（ticks）
  · 素材入出点 = 其 SubClip → Clip 的 <InPoint>/<OutPoint>（ticks）
  · 每秒 254,016,000,000 ticks

删除区间 = 把原片段切成若干保留段，段与段在时间轴上首尾相接（前面删掉后，
后面的 Start 自然前移，等价于 PR 的波纹删除）。

用法：
    python prproj_delete.py <源.prproj> <输出.prproj> <起秒> <止秒>
"""
from __future__ import annotations

import gzip
import os
import re
import sys
import uuid

TICKS = 254016000000

# 轨道 GUID（来自这台机器工程文件的实测）
TRACKS = [
    ("VideoClipTrack", "899ac339-a283-4e80-9ebc-3f2cb7bd87ae"),
    ("AudioClipTrack", "2c248935-99d8-40cf-8283-bc416a68a022"),
    ("AudioClipTrack", "0aa4b222-edbe-418c-9486-91e095399dd9"),
]


def load(path):
    with gzip.open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def save(path, text):
    with gzip.open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


def find_block(text, tag_name, attr, value):
    pattern = r'<%s[^>]*\b%s="%s"[^>]*>' % (re.escape(tag_name),
                                            re.escape(attr), re.escape(value))
    match = re.search(pattern, text)
    if not match:
        return None
    start = match.start()
    end = text.find("</%s>" % tag_name, start)
    if end < 0:
        return None
    end += len(tag_name) + 3
    return start, end, text[start:end]


def find_object(text, oid):
    pattern = r'<(\w+)[^>]*\bObjectID="%s"[^>]*?(/>|>)' % re.escape(str(oid))
    match = re.search(pattern, text)
    if not match:
        return None
    tag_name = match.group(1)
    start = match.start()
    if match.group(2) == "/>":
        return start, match.end(), match.group(0), tag_name
    end = text.find("</%s>" % tag_name, start)
    if end < 0:
        return None
    end += len(tag_name) + 3
    return start, end, text[start:end], tag_name


def set_tag(block, tag_name, value):
    pattern = r"<%s>[^<]*</%s>" % (re.escape(tag_name), re.escape(tag_name))
    return re.sub(pattern, "<%s>%s</%s>" % (tag_name, value, tag_name), block,
                  count=1)


def retime_track_item(block, start, end, sub_ref):
    """把 TrackItem 的时间轴位置改成 [start, end]，并指向指定 SubClip。"""
    block = re.sub(r"<Start>\d+</Start>", "", block)
    block = re.sub(r"<End>\d+</End>", "", block)
    block = re.sub(r'<SubClip ObjectRef="\d+"/>',
                   '<SubClip ObjectRef="%d"/>' % sub_ref, block, count=1)
    # 插到 <TrackItem> 标签里
    block = re.sub(r"(<TrackItem Version=\"\d+\">)",
                   r'\1\n\t\t\t\t<Start>%d</Start>\n\t\t\t\t<End>%d</End>'
                   % (start, end), block, count=1)
    return block


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 1
    src, out = sys.argv[1], sys.argv[2]
    cut_from, cut_to = float(sys.argv[3]), float(sys.argv[4])
    if cut_to <= cut_from:
        print("删除区间不合法")
        return 1

    text = load(src)
    next_id_match = re.search(r"<NextID>(\d+)</NextID>", text)
    next_id = int(next_id_match.group(1)) if next_id_match else 1000000

    new_objects = []
    report = []

    for tag_name, guid in TRACKS:
        found = find_block(text, tag_name, "ObjectUID", guid)
        if not found:
            report.append("轨道 %s 找不到，跳过" % guid[:8])
            continue
        tstart, tend, tblock = found
        items = re.findall(r'<TrackItem Index="(\d+)" ObjectRef="(\d+)"/>', tblock)
        if not items:
            report.append("轨道 %s 上没有片段，跳过" % guid[:8])
            continue
        first_index, first_ref = items[0]
        ti = find_object(text, first_ref)
        if not ti:
            report.append("轨道 %s 的 TrackItem #%s 找不到" % (guid[:8], first_ref))
            continue
        ti_start, ti_end, ti_block, ti_tag = ti
        sub_ref = re.search(r'<SubClip ObjectRef="(\d+)"/>', ti_block)
        sub = find_object(text, sub_ref.group(1)) if sub_ref else None
        if not sub:
            report.append("轨道 %s 的 SubClip 找不到" % guid[:8])
            continue
        sub_start, sub_end, sub_block, sub_tag = sub
        clip_ref = re.search(r'<Clip ObjectRef="(\d+)"/>', sub_block)
        clip = find_object(text, clip_ref.group(1)) if clip_ref else None
        if not clip:
            report.append("轨道 %s 的 Clip 找不到" % guid[:8])
            continue
        clip_start, clip_end, clip_block, clip_tag = clip
        in_point = int(re.search(r"<InPoint>(\d+)</InPoint>", clip_block).group(1))
        out_point = int(re.search(r"<OutPoint>(\d+)</OutPoint>", clip_block).group(1))

        # 算保留段（素材时间，ticks）
        cut_lo = max(in_point, int(round(cut_from * TICKS)))
        cut_hi = min(out_point, int(round(cut_to * TICKS)))
        segments = []
        if cut_lo > in_point:
            segments.append((in_point, cut_lo))
        if cut_hi < out_point:
            segments.append((cut_hi, out_point))
        if not segments:
            report.append("轨道 %s：删除区间覆盖整段，跳过" % guid[:8])
            continue

        # 生成 N 段：第一段复用原 TrackItem，其余新增
        cursor = 0  # 时间轴位置从 0 开始
        new_refs = []
        for seg_no, (seg_in, seg_out) in enumerate(segments):
            length = seg_out - seg_in
            seg_start = cursor
            seg_end = cursor + length
            cursor = seg_end

            if seg_no == 0:
                # 复用原 TrackItem / SubClip / Clip，只改时间
                patched_ti = retime_track_item(ti_block, seg_start, seg_end,
                                               int(sub_ref.group(1)))
                text = text[:ti_start] + patched_ti + text[ti_end:]
                # 原 Clip 改 in/out
                patched_clip = set_tag(clip_block, "InPoint", str(seg_in))
                patched_clip = set_tag(patched_clip, "OutPoint", str(seg_out))
                text = text[:clip_start] + patched_clip + text[clip_end:]
                new_refs.append(int(first_ref))
            else:
                new_ti = next_id
                new_sub = next_id + 1
                new_clip = next_id + 2
                next_id += 3
                # 新 Clip
                fresh_clip = re.sub(r'ObjectID="\d+"',
                                    'ObjectID="%d"' % new_clip, clip_block, count=1)
                fresh_clip = set_tag(fresh_clip, "InPoint", str(seg_in))
                fresh_clip = set_tag(fresh_clip, "OutPoint", str(seg_out))
                fresh_clip = re.sub(r"<ClipID>[^<]*</ClipID>",
                                    "<ClipID>%s</ClipID>" % uuid.uuid4(),
                                    fresh_clip, count=1)
                new_objects.append(fresh_clip)
                # 新 SubClip
                fresh_sub = re.sub(r'ObjectID="\d+"',
                                   'ObjectID="%d"' % new_sub, sub_block, count=1)
                fresh_sub = re.sub(r'<Clip ObjectRef="\d+"/>',
                                   '<Clip ObjectRef="%d"/>' % new_clip,
                                   fresh_sub, count=1)
                new_objects.append(fresh_sub)
                # 新 TrackItem
                fresh_ti = re.sub(r'ObjectID="\d+"',
                                  'ObjectID="%d"' % new_ti, ti_block, count=1)
                fresh_ti = retime_track_item(fresh_ti, seg_start, seg_end, new_sub)
                # 音频 TrackItem 带唯一 <ID> GUID，复制时必须换新的，
                # 否则两个对象同 ID，PR 打开会校验失败（弹空白错误框）。
                fresh_ti = re.sub(r"<ID>[0-9a-fA-F\-]{36}</ID>",
                                  "<ID>%s</ID>" % uuid.uuid4(), fresh_ti, count=1)
                new_objects.append(fresh_ti)
                new_refs.append(new_ti)

        # 替换该轨道的 TrackItems 列表
        found2 = find_block(text, tag_name, "ObjectUID", guid)
        if not found2:
            report.append("轨道 %s 第二次定位失败" % guid[:8])
            continue
        v2start, v2end, v2block = found2
        item_xml = "\n".join(
            '\t\t\t\t\t<TrackItem Index="%d" ObjectRef="%d"/>' % (i, ref)
            for i, ref in enumerate(new_refs))
        patched_track, count = re.subn(
            r'(<TrackItems Version="1">)([\s\S]*?)(</TrackItems>)',
            r"\1\n%s\n\t\t\t\t\3" % item_xml, v2block, count=1)
        if count == 0:
            report.append("轨道 %s 的 TrackItems 替换失败" % guid[:8])
            continue
        text = text[:v2start] + patched_track + text[v2end:]
        report.append("轨道 %s（%s）：%d 段保留" % (
            guid[:8], tag_name, len(segments)))

    # 新对象插到末尾
    marker = "</PremiereData>"
    pos = text.rfind(marker)
    if pos < 0:
        print("找不到 </PremiereData>")
        return 1
    if new_objects:
        text = text[:pos] + "\n".join(new_objects) + "\n" + text[pos:]
    text = re.sub(r"<NextID>\d+</NextID>", "<NextID>%d</NextID>" % next_id,
                  text, count=1)

    save(out, text)
    print("\n".join(report))
    print("已写出：%s" % out)
    print("新增对象 %d 个，NextID → %d" % (len(new_objects), next_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())

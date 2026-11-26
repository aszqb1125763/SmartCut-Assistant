# -*- coding: utf-8 -*-
"""直接修改 .prproj：在指定时间加一个切点（视频轨 + 音频轨）。

依据 2026-11-25 的逆向（对比切前/切后工程文件）：
  切一刀 = 原 Clip.OutPoint 缩短 + 原 TrackItem.End 缩短
         + 新增 Clip / TrackItem / SubClip（从切点开始）
         + 轨道 TrackItems 列表加一项
  时间单位：ticks，每秒 254,016,000,000

用法：
    python prproj_cut.py <源.prproj> <输出.prproj> <切点秒> [更多切点秒...]
"""
from __future__ import annotations

import gzip
import os
import re
import sys
import uuid

TICKS = 254016000000
SRC_VIDEO_GUID = "899ac339-a283-4e80-9ebc-3f2cb7bd87ae"   # 视频轨 0
SRC_AUDIO_GUIDS = [
    "2c248935-99d8-40cf-8283-bc416a68a022",                # 音频轨 0
    "0aa4b222-edbe-418c-9486-91e095399dd9",                # 音频轨 1
]


def load(path):
    with gzip.open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def save(path, text):
    with gzip.open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


def find_block(text, tag_name, attr, value):
    """按属性找到标签块，返回 (start, end, block)。"""
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
    """把块内某个标签的值改掉（没有就插在 <TrackItem> 里）。"""
    pattern = r"<%s>[^<]*</%s>" % (re.escape(tag_name), re.escape(tag_name))
    if re.search(pattern, block):
        return re.sub(pattern, "<%s>%s</%s>" % (tag_name, value, tag_name), block,
                      count=1)
    return block


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    src, out = sys.argv[1], sys.argv[2]
    cuts = sorted(float(x) for x in sys.argv[3:])
    text = load(src)

    # NextID（新对象从这里往后分配）
    next_id_match = re.search(r"<NextID>(\d+)</NextID>", text)
    next_id = int(next_id_match.group(1)) if next_id_match else 1000000
    print("起始 NextID = %d" % next_id)

    # 找视频轨的 TrackItems
    found = find_block(text, "VideoClipTrack", "ObjectUID", SRC_VIDEO_GUID)
    if not found:
        print("找不到视频轨 %s" % SRC_VIDEO_GUID)
        return 1
    vstart, vend, vblock = found
    track_items = re.findall(r'<TrackItem Index="(\d+)" ObjectRef="(\d+)"/>', vblock)
    if not track_items:
        print("视频轨上没有 TrackItem")
        return 1
    first_index, first_ref = track_items[0]
    print("视频轨第一个 TrackItem：Index=%s ObjectRef=%s" % (first_index, first_ref))

    # 原 TrackItem → SubClip → Clip
    ti = find_object(text, first_ref)
    if not ti:
        print("找不到 TrackItem #%s" % first_ref)
        return 1
    ti_start, ti_end, ti_block, ti_tag = ti
    sub_ref = re.search(r"<SubClip ObjectRef=\"(\d+)\"/>", ti_block)
    if not sub_ref:
        print("TrackItem 里没有 SubClip")
        return 1
    sub = find_object(text, sub_ref.group(1))
    if not sub:
        print("找不到 SubClip #%s" % sub_ref.group(1))
        return 1
    sub_start, sub_end, sub_block, sub_tag = sub
    clip_ref = re.search(r"<Clip ObjectRef=\"(\d+)\"/>", sub_block)
    if not clip_ref:
        print("SubClip 里没有 Clip")
        return 1
    clip = find_object(text, clip_ref.group(1))
    if not clip:
        print("找不到 Clip #%s" % clip_ref.group(1))
        return 1
    clip_start, clip_end, clip_block, clip_tag = clip
    in_point = int(re.search(r"<InPoint>(\d+)</InPoint>", clip_block).group(1))
    out_point = int(re.search(r"<OutPoint>(\d+)</OutPoint>", clip_block).group(1))
    print("原 Clip #%s InPoint=%d OutPoint=%d" % (clip_ref.group(1), in_point, out_point))

    # 只做「在原始片段内部切一刀」：切点必须落在 (in, out) 之间
    cut_ticks = int(round(cuts[0] * TICKS))
    if not (in_point < cut_ticks < out_point):
        print("切点 %s 秒（%d ticks）不在素材范围 (%d, %d) 内"
              % (cuts[0], cut_ticks, in_point, out_point))
        return 1
    print("切点 = %d ticks（%.3f 秒）" % (cut_ticks, cuts[0]))

    new_ti_id = next_id
    new_sub_id = next_id + 1
    new_clip_id = next_id + 2
    next_id += 3

    # 1) 原 TrackItem：End 改切点
    new_ti_block = set_tag(ti_block, "End", str(cut_ticks))
    text = text[:ti_start] + new_ti_block + text[ti_end:]

    # 2) 原 Clip：OutPoint 改切点
    new_clip_block = set_tag(clip_block, "OutPoint", str(cut_ticks))
    text = text[:clip_start] + new_clip_block + text[clip_end:]

    # 3) 新 Clip（复制原 Clip，改 InPoint/OutPoint/ClipID）
    fresh_clip = clip_block
    fresh_clip = re.sub(r'ObjectID="\d+"', 'ObjectID="%d"' % new_clip_id,
                        fresh_clip, count=1)
    fresh_clip = set_tag(fresh_clip, "InPoint", str(cut_ticks))
    fresh_clip = set_tag(fresh_clip, "OutPoint", str(out_point))
    fresh_clip = re.sub(r"<ClipID>[^<]*</ClipID>",
                        "<ClipID>%s</ClipID>" % uuid.uuid4(), fresh_clip, count=1)

    # 4) 新 SubClip（复制原 SubClip，指向新 Clip）
    fresh_sub = sub_block
    fresh_sub = re.sub(r'ObjectID="\d+"', 'ObjectID="%d"' % new_sub_id,
                       fresh_sub, count=1)
    fresh_sub = re.sub(r'<Clip ObjectRef="\d+"/>',
                       '<Clip ObjectRef="%d"/>' % new_clip_id, fresh_sub, count=1)

    # 5) 新 TrackItem（复制原 TrackItem，Start=切点、End=原结尾）
    fresh_ti = ti_block
    fresh_ti = re.sub(r'ObjectID="\d+"', 'ObjectID="%d"' % new_ti_id,
                      fresh_ti, count=1)
    fresh_ti = re.sub(r"<End>\d+</End>",
                      "<Start>%d</Start><End>%d</End>" % (cut_ticks, out_point),
                      fresh_ti, count=1)
    fresh_ti = re.sub(r'<SubClip ObjectRef="\d+"/>',
                      '<SubClip ObjectRef="%d"/>' % new_sub_id, fresh_ti, count=1)

    # 6) 轨道 TrackItems 加一项（改在原文里，注意此时 vblock 位置已变，重新定位）
    found2 = find_block(text, "VideoClipTrack", "ObjectUID", SRC_VIDEO_GUID)
    if not found2:
        print("第二次找不到视频轨")
        return 1
    v2start, v2end, v2block = found2
    patched = v2block.replace(
        '<TrackItem Index="%s" ObjectRef="%s"/>' % (first_index, first_ref),
        '<TrackItem Index="%s" ObjectRef="%s"/>\n\t\t\t\t\t<TrackItem Index="%d" ObjectRef="%d"/>'
        % (first_index, first_ref, int(first_index) + 1, new_ti_id), 1)
    if patched == v2block:
        print("没能在 TrackItems 里插入新项")
        return 1
    text = text[:v2start] + patched + text[v2end:]

    # 7) 新对象插到 </PremiereData> 前面
    inserted = "\n".join([fresh_ti, fresh_sub, fresh_clip])
    marker = "</PremiereData>"
    pos = text.rfind(marker)
    if pos < 0:
        print("找不到 </PremiereData>")
        return 1
    text = text[:pos] + inserted + "\n" + text[pos:]

    # 8) NextID 更新
    text = re.sub(r"<NextID>\d+</NextID>", "<NextID>%d</NextID>" % next_id, text, count=1)

    save(out, text)
    print("已写出：%s" % out)
    print("新增对象：#%d TrackItem / #%d SubClip / #%d Clip" % (new_ti_id, new_sub_id, new_clip_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())

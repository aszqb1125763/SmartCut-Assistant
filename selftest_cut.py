# -*- coding: utf-8 -*-
"""功能自测：一条命令跑完自动剪辑的关键功能点（不需要人工、不碰你的索引）。

用法：
    .venv\\Scripts\\python.exe selftest_cut.py            # 全跑
    .venv\\Scripts\\python.exe selftest_cut.py dedupe     # 只跑名字含 dedupe 的组

退出码：0 = 全过；非 0 = 失败条数。

覆盖：
    区间运算       subtract（去重复段用）
    字幕映射       remap / _wrap（成片时间轴、超长句截断）
    分辨率         out_resolution（字幕排版尺寸）
    重复检测       造"重录"数据：差版该丢、好版该留、干净素材不该报
    BGM 选段       波形曲线造出"淡入+平稳+静音"，应挑在平稳段
    BGM 循环       输出长度精确、电平落在目标 ±1.5 dB
    BGM 混音       固定电平 vs 开闪避：抬升量必须不同（验证开关真的生效）
    出片取消       cancel 立刻抛 RenderCancelled 且不留输出文件
    界面           主窗口/设置窗口能建；保存不丢配置项；曲库目录能改
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

import autocut          # noqa: E402
import index as ix      # noqa: E402
import subtitles        # noqa: E402

TMP = os.path.join(BASE, "logs", "selftest")
RESULT = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "%s: %s" % (type(exc).__name__, str(exc)[:120])
    RESULT.append((name, ok, detail))
    print(("  [通过] " if ok else "  [失败] ") + name
          + ("  —— " + str(detail) if detail else ""))
    return ok


# --------------------------------------------------------------------------
# 测试素材：24 秒小视频（纯色 + 低电平正弦），只用于出片/混音测试
# --------------------------------------------------------------------------
_MEDIA = [None]
_MULTI_MEDIA = [None]
_SILENT_MEDIA = [None]


def media():
    if _MEDIA[0]:
        return _MEDIA[0]
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "selftest2.mp4")
    if not os.path.isfile(p):
        ff = ix.find_ffmpeg()
        # 音量调到跟真实人声差不多（约 -20dB RMS）：BGM 测试要靠这个电平差
        # 才能看出"闪避"和"固定电平"的区别
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=0x203040:s=320x180:d=24:r=30",
                        "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=24:sample_rate=48000",
                        "-af", "volume=1.1", "-c:v", "libx264", "-preset",
                        "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-b:a", "128k", "-shortest", p],
                       capture_output=True, creationflags=ix.CREATE_NO_WINDOW)
    _MEDIA[0] = p
    return p


def multi_audio_media():
    """4 秒六音轨素材：每条轨用不同频率，便于验证确实全部混入。"""
    if _MULTI_MEDIA[0]:
        return _MULTI_MEDIA[0]
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "selftest_multitrack6.mp4")
    if not os.path.isfile(p):
        ff = ix.find_ffmpeg()
        freqs = (233, 347, 461, 593, 719, 887)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=0x203040:s=320x180:d=4:r=30"]
        for freq in freqs:
            cmd += ["-f", "lavfi", "-i",
                    "sine=frequency=%d:duration=4:sample_rate=48000" % freq]
        cmd += ["-map", "0:v:0"]
        for i in range(len(freqs)):
            cmd += ["-map", "%d:a:0" % (i + 1)]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-shortest", p]
        subprocess.run(cmd, check=True, capture_output=True,
                       creationflags=ix.CREATE_NO_WINDOW)
    _MULTI_MEDIA[0] = p
    return p


def silent_media():
    """4 秒无音轨视频，用于预览的无声回退测试。"""
    if _SILENT_MEDIA[0]:
        return _SILENT_MEDIA[0]
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "selftest_silent.mp4")
    if not os.path.isfile(p):
        ff = ix.find_ffmpeg()
        subprocess.run([
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x203040:s=320x180:d=4:r=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-an", p,
        ], check=True, capture_output=True, creationflags=ix.CREATE_NO_WINDOW)
    _SILENT_MEDIA[0] = p
    return p


def decode(path, sr=16000):
    ff = ix.find_ffmpeg()
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                    "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", wav],
                   creationflags=ix.CREATE_NO_WINDOW)
    a = ix.read_wav_mono(wav)
    os.remove(wav)
    return a


def rms_db(a):
    if a is None or a.size == 0:
        return -120.0
    return 20 * np.log10(max(float(np.sqrt(np.mean(a.astype(np.float64) ** 2))), 1e-9))


def t_export_merged():
    """卡片合并必须生成可探测成片，并清理滤镜脚本。"""
    import export as exporter

    root = os.path.join(TMP, "export_merge")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    out = os.path.join(root, "merged.mp4")
    old_temp_root = exporter.indexer.temp_root
    exporter.indexer.temp_root = lambda: root
    try:
        cards = [
            {"id": "a", "start": 0.0, "end": 2.0, "text": "第一段"},
            {"id": "b", "start": 4.0, "end": 6.0, "text": "第二段"},
        ]
        got = exporter.export_merged(media(), cards, out)
    finally:
        exporter.indexer.temp_root = old_temp_root
    if got != out or not os.path.isfile(out) or os.path.getsize(out) <= 10000:
        return False, "合并没有生成有效文件：%r" % got
    info = ix.probe_streams(out)
    dur = float(info.get("duration") or 0)
    if not (3.6 <= dur <= 4.4):
        return False, "合并时长应约 4 秒，实际 %.2f" % dur
    if not info.get("audio_streams"):
        return False, "合并成片缺少音频流"
    leftovers = [n for n in os.listdir(root)
                 if n.startswith("cards_") and n.endswith(".txt")]
    if leftovers:
        return False, "滤镜临时脚本未清理：%s" % leftovers
    return True, "两段合并 %.2f 秒，音视频正常，临时脚本已清理" % dur


def t_card_preview_audio_modes():
    """卡片预览必须覆盖 0/1/N 音轨，多轨输出要真的含每一条轨。"""
    import export as exporter

    root = os.path.join(TMP, "card_preview")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    multi_out = os.path.join(root, "multi.mp4")
    used = exporter._cut_preview(
        multi_audio_media(), 0.4, 2.0, multi_out,
        audio_streams=[{"ordinal": i} for i in range(6)],
    )
    info = ix.probe_streams(multi_out)
    if used != list(range(6)):
        return False, "多轨序号没有全部使用：%r" % used
    if len(info.get("audio_streams") or []) != 1:
        return False, "多轨预览应混成播放器可听的一条，实际 %r" % info.get("audio_streams")
    samples = decode(multi_out, sr=16000)
    if samples.size < 8000:
        return False, "多轨预览音频解码为空"
    samples = samples[2000:-2000]
    t = np.arange(samples.size, dtype=np.float64) / 16000.0
    levels = []
    for freq in (233, 347, 461, 593, 719, 887):
        wave_ref = np.exp(-2j * np.pi * freq * t)
        levels.append(float(abs(np.dot(samples, wave_ref)) / samples.size))
    if any(level < 0.006 for level in levels):
        return False, "并非所有六条音轨都进入混音，频率电平=%r" % levels

    subset_out = os.path.join(root, "subset.mp4")
    subset_used = exporter._cut_preview(
        multi_audio_media(), 0.4, 2.0, subset_out, audio_streams=[1, 4])
    if subset_used != [1, 4]:
        return False, "所选音轨序号没有被严格使用：%r" % subset_used
    subset_samples = decode(subset_out, sr=16000)[2000:-2000]
    subset_t = np.arange(subset_samples.size, dtype=np.float64) / 16000.0
    subset_levels = []
    for freq in (233, 347, 461, 593, 719, 887):
        wave_ref = np.exp(-2j * np.pi * freq * subset_t)
        subset_levels.append(float(abs(np.dot(subset_samples, wave_ref)) /
                                   subset_samples.size))
    selected_levels = [subset_levels[1], subset_levels[4]]
    rejected_levels = [subset_levels[i] for i in (0, 2, 3, 5)]
    if min(selected_levels) < 0.006 or max(rejected_levels) >= min(selected_levels) * 0.2:
        return False, "预览混入了未选择音轨，频率电平=%r" % subset_levels

    single_out = os.path.join(root, "single.mp4")
    single_used = exporter._cut_preview(
        media(), 0.5, 1.5, single_out, audio_streams=[0])
    single_info = ix.probe_streams(single_out)
    if single_used != [0] or len(single_info.get("audio_streams") or []) != 1:
        return False, "单音轨预览回退异常：used=%r info=%r" % (
            single_used, single_info.get("audio_streams"))

    silent_out = os.path.join(root, "silent.mp4")
    silent_used = exporter._cut_preview(
        silent_media(), 0.5, 1.5, silent_out, audio_streams=[])
    silent_info = ix.probe_streams(silent_out)
    if silent_used or silent_info.get("audio_streams"):
        return False, "无音轨素材没有保持无声：used=%r info=%r" % (
            silent_used, silent_info.get("audio_streams"))
    if not silent_info.get("has_video"):
        return False, "无音轨预览丢失视频"
    return True, "0/1/6 音轨均可预览；部分选择只混入指定音轨"


def write_loop_wav(path, seconds=12.0, level_db=-28.0, sr=48000):
    """造一段平稳的正弦当 BGM（不依赖曲库，保证测试可离线跑）。"""
    n = int(seconds * sr)
    t = np.arange(n) / sr
    x = 0.3 * np.sin(2 * np.pi * 220.0 * t)
    gain = 10 ** (level_db / 20.0) / (float(np.sqrt(np.mean(x ** 2))) or 1e-6)
    x = np.clip(x * gain, -1, 1)
    stereo = np.stack([x, x], axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((stereo * 32767).astype("<i2").tobytes())
    return path


# --------------------------------------------------------------------------
# 1. 区间运算
# --------------------------------------------------------------------------
def t_subtract():
    cases = [
        ([(10.0, 20.0)], [(12.0, 15.0)], [(10.0, 12.0), (15.0, 20.0)]),
        ([(0.0, 10.0)], [(0.0, 4.0)], [(4.0, 10.0)]),
        ([(0.0, 10.0), (20.0, 30.0)], [(8.0, 22.0)],
         [(0.0, 8.0), (22.0, 30.0)]),
        ([(5.0, 6.0)], [(0.0, 100.0)], []),
        # 只削掉末尾 0.1 秒的边角 → 保留 9.9 秒，不该整段丢
        ([(0.0, 10.0)], [(9.9, 20.0)], [(0.0, 9.9)]),
    ]
    for segs, spans, want in cases:
        got = autocut.subtract(segs, spans)
        if got != want:
            return False, "subtract(%s,%s)=%s 期望 %s" % (segs, spans, got, want)
    return True, "%d 组用例" % len(cases)


# --------------------------------------------------------------------------
# 2. 字幕时间轴映射
# --------------------------------------------------------------------------
def t_remap():
    tl = [(0.0, 100.0, 10.0)]          # 成片 0~10 秒 = 原片 100~110
    segs = [(105.0, 108.0, "到了")]
    got = subtitles.remap(segs, tl)
    if len(got) != 1 or abs(got[0][0] - 5.0) > 0.01 or abs(got[0][1] - 8.0) > 0.01:
        return False, "单段映射错误：%s" % (got,)
    # 一句话跨两个片段 → 拆成两条，各自落在对应片段里
    tl2 = [(0.0, 100.0, 5.0), (5.0, 200.0, 5.0)]
    segs2 = [(103.0, 204.0, "跨段")]
    got2 = subtitles.remap(segs2, tl2)
    if len(got2) != 2:
        return False, "跨段应拆成 2 条，实际 %d" % len(got2)
    # 极短台词也要撑到 0.4 秒，否则一闪而过
    got3 = subtitles.remap([(101.0, 101.05, "嗯")], [(0.0, 100.0, 10.0)])
    if not got3 or (got3[0][1] - got3[0][0]) < 0.39:
        return False, "最短展示时长没生效：%s" % (got3,)
    return True, "单段/跨段/最小时长 3 项"


def t_wrap():
    from subtitles import _wrap
    lines = _wrap("一" * 60, 16, 2)
    if "".join(lines) != "一" * 60 or any(len(line) > 16 for line in lines):
        return False, "字幕折行丢字或超过单行上限：%r" % lines
    pages = subtitles.paginate_segments([(0, 8, "一" * 60)],
                                        {"max_chars": 16, "max_lines": 2})
    if len(pages) != 2 or any(len(row[2].splitlines()) > 2 for row in pages):
        return False, "无损分页失败：%r" % pages
    return True, "长句无损折行与连续分页"


def t_resolution():
    w, h = autocut.out_resolution({}, {"width": 1920, "height": 1080})
    if (w, h) != (1920, 1080):
        return False, "默认应跟素材一致，实际 %s" % ((w, h),)
    w, h = autocut.out_resolution({"render": {"scale": "1280:-2"}},
                                  {"width": 1920, "height": 1080})
    if (w, h) != (1280, 720):
        return False, "1280 缩放应得 (1280,720)，实际 %s" % ((w, h),)
    if h % 2:
        return False, "高度必须是偶数，实际 %d" % h
    return True, "原尺寸/缩放/偶数"


# --------------------------------------------------------------------------
# 3. 重复检测
# --------------------------------------------------------------------------
def _fake_index(dup=True):
    """造一份小索引：80 句各不相同的台词，可选在结尾再"重录"一遍 40~60 秒那段。

    注意：每句内容必须真的不一样，否则整份索引互相都像（自测第一版就踩了：
    句子只差一个编号，结果全片互配，检测直接指到 23 秒去了）。
    """
    import random
    words = ["机械", "动力", "压板机", "离心机", "电解", "储罐", "火箭", "月球",
             "氧气", "燃料", "硅岩", "钢板", "传送带", "漏斗", "转速", "配方",
             "任务", "章节", "材料", "工序", "接口", "终端", "电缆", "水泵"]
    segs, t = [], 10.0
    for i in range(80):
        rnd = random.Random(i * 7919)
        txt = "第%d段%s" % (i, "，".join(rnd.choice(words) for _ in range(7)))
        segs.append([round(t, 2), round(t + 2.4, 2), txt])
        t += 2.6
    data = {"duration": t + 5, "segments": segs, "semantic": [], "cards": []}
    info = {"orig": (40.0, 60.0), "retake": None}
    if dup:
        base = [s for s in segs if 40.0 <= s[0] < 60.0]
        shift = t + 10.0 - base[0][0]
        retake = []
        for i, s in enumerate(base):
            tail = "然后" if i % 3 == 0 else ""     # 重录版更差：1/3 说半截
            retake.append([round(s[0] + shift, 2), round(s[1] + shift, 2),
                           s[2] + tail])
        data["segments"] = sorted(segs + retake, key=lambda x: x[0])
        data["duration"] = t + 10.0 + (base[-1][1] - base[0][0]) + 10.0
        info["retake"] = (base[0][0] + shift, base[-1][1] + shift)
    return data, info


def t_dedupe_find():
    import dedupe
    data, info = _fake_index(True)
    d = dedupe.find_duplicates(data)
    if len(d) != 1:
        return False, "应命中 1 组，实际 %d 组" % len(d)
    drop0, keep0 = d[0]["drop"][0], d[0]["keep"][0]
    want_drop, want_keep = info["retake"][0], info["orig"][0]
    if abs(drop0 - want_drop) > 20:
        return False, "丢的那段该是重录（%.0f 附近），实际 %.1f" % (want_drop, drop0)
    if abs(keep0 - want_keep) > 20:
        return False, "留的那段该是原版（%.0f 附近），实际 %.1f" % (want_keep, keep0)
    span = d[0]["drop"][1] - d[0]["drop"][0]
    if span < 12:
        return False, "只认出 %.1f 秒，重复段有 20 秒" % span
    return True, "命中 1 组：丢 %.1f 秒起的重录版（%.1f 秒长）" % (drop0, span)


def t_dedupe_clean():
    import dedupe
    data, _info = _fake_index(False)
    d = dedupe.find_duplicates(data)
    if d:
        return False, "干净素材误报 %d 组：%s" % (len(d), d[:1])
    return True, "0 组"


# --------------------------------------------------------------------------
# 4. BGM 选段 / 循环 / 混音
# --------------------------------------------------------------------------
def t_bgm_window():
    import bgm
    # 0~40 秒淡入，40~160 秒平稳，160~200 秒静音
    db = np.concatenate([
        np.linspace(-60, -26, 40),
        np.full(120, -26.0) + np.random.RandomState(0).normal(0, 0.3, 120),
        np.full(40, -95.0)])
    info = {"path": "fake", "duration": 200.0, "db": db}
    start, length, steady = bgm.best_window(info, 100.0)
    mid = start + length / 2
    if not (60 <= mid <= 160):
        return False, "挑到了 %.0f~%.0f（中点 %.0f），应落在平稳段 60~160" % (
            start, start + length, mid)
    return True, "挑 %.0f~%.0f 秒（中点 %.0f）" % (start, start + length, mid)


def t_bgm_loop():
    import bgm
    os.makedirs(TMP, exist_ok=True)
    out = os.path.join(TMP, "loop.wav")
    src = os.path.join(TMP, "tone.mp3")
    ff = ix.find_ffmpeg()
    if not os.path.isfile(src):
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=20:"
                        "sample_rate=48000", "-c:a", "libmp3lame", src],
                       creationflags=ix.CREATE_NO_WINDOW)
    got = bgm.build_loop(src, 2.0, 12.0, 30.0, out, level_db=-28.0,
                         xfade=1.0, fade_in=0.5, fade_out=1.0, loudnorm=False)
    if not got or not os.path.isfile(out):
        return False, "没生成循环文件"
    with wave.open(out, "rb") as w:
        secs = w.getnframes() / float(w.getframerate())
    if abs(secs - 30.0) > 0.05:
        return False, "长度应 30 秒，实际 %.2f" % secs
    db = rms_db(decode(out))
    if abs(db - (-28.0)) > 1.5:
        return False, "电应 -28±1.5，实际 %.1f dB" % db
    # 响度拉齐：两首天生音量不同的曲子，拉齐后 LUFS 应该靠得很近
    if os.path.isdir(str(bgm.load_conf().get("folder") or "")):
        tracks = bgm.list_tracks()
        if len(tracks) >= 2:
            vals = []
            for t in tracks[:2]:
                bgm.build_loop(t, 5.0, 30.0, 12.0, out, level_db=-22.0,
                               loudnorm=True)
                vals.append(bgm.measure_lufs(out))
            if all(v is not None for v in vals) and abs(vals[0] - vals[1]) > 2.0:
                return False, ("两首曲子拉齐后响度差 %.1f dB（应 <2）"
                               % abs(vals[0] - vals[1]))
    return True, "30.0 秒、%.1f dB（RMS 档目标 -28）；响度档对齐正常" % db


def t_bgm_mix_duck():
    import bgm
    src = media()
    os.makedirs(TMP, exist_ok=True)
    loop = os.path.join(TMP, "loop2.wav")
    write_loop_wav(loop, 24.0, -28.0)
    base_db = rms_db(decode(src))
    outs = {}
    for tag, duck in (("无闪避", 0.0), ("闪避6", 6.0)):
        p = os.path.join(TMP, "mix_%s.mp4" % tag)
        bgm.mix_into(src, p, loop, duck_db=duck, log=lambda *_a: None)
        outs[tag] = rms_db(decode(p))
    add_plain = outs["无闪避"] - base_db
    add_duck = outs["闪避6"] - base_db
    if not (add_plain > add_duck + 0.15):
        return False, ("固定电平应比闪避多抬一些：无闪避 +%.2f dB，闪避 +%.2f dB"
                       % (add_plain, add_duck))
    return True, "无闪避 +%.2f dB vs 闪避 +%.2f dB" % (add_plain, add_duck)


def t_bgm_failure_keeps_output():
    """曲库为空或混音报错时，必须保留可播放的无 BGM 成片。"""
    import bgm
    import bgm_sections

    conf = autocut.load_conf()
    conf["render"]["scale"] = "320:-2"
    conf["render"]["workers"] = 1
    conf["render"]["encoder"] = "x264"

    def verify(path):
        if not os.path.isfile(path) or os.path.getsize(path) < 10000:
            return False, "成片不存在或太小：%s" % path
        info = ix.probe_streams(path)
        if not info.get("has_video") or float(info.get("duration") or 0) < 1.5:
            return False, "恢复后的成片不可探测：%s" % info
        if os.path.isfile(path + ".pre.mp4"):
            return False, "遗留了 .pre.mp4 备份"
        return True, ""

    # 1) 正常但曲库为空：attach 返回 None。
    empty_out = os.path.join(TMP, "bgm空曲库回滚.mp4")
    for p in (empty_out, empty_out + ".pre.mp4"):
        if os.path.isfile(p):
            os.remove(p)
    old_make_sections = bgm_sections.make_sections
    section_call = {}

    def capture_sections(data, duration, **kwargs):
        section_call.update(kwargs)
        return [(0.0, float(duration))]

    bgm_sections.make_sections = capture_sections
    try:
        autocut.render(
            media(), [(0.0, 2.0)], [], empty_out, conf,
            on_log=lambda *_a: None, bgm=True,
            bgm_conf={"enabled": "1", "folder": r"Z:\不存在的BGM目录",
                      "sections": "1", "section_max_min": "4.0"},
            data={},
        )
    finally:
        bgm_sections.make_sections = old_make_sections
    if not section_call.get("pieces"):
        return False, "BGM 分段没有收到最终成片 pieces，无法吸附合理边界"
    if abs(float(section_call.get("min_len", 0)) - 60.0) > 0.01:
        return False, "BGM 最短段不是 60 秒：%r" % section_call.get("min_len")
    if abs(float(section_call.get("max_len", 0)) - 180.0) > 0.01:
        return False, "旧 4 分钟配置没有被限制到 180 秒：%r" % section_call.get("max_len")
    ok, detail = verify(empty_out)
    if not ok:
        return ok, "空曲库：" + detail

    # 2) 分段和单曲混音都抛错：仍要从 .pre.mp4 回滚。
    fail_out = os.path.join(TMP, "bgm异常回滚.mp4")
    for p in (fail_out, fail_out + ".pre.mp4"):
        if os.path.isfile(p):
            os.remove(p)
    old_sections = bgm_sections.attach_sections
    old_attach = bgm.attach

    def fail(*_a, **_kw):
        raise RuntimeError("测试强制混音失败")

    bgm_sections.attach_sections = fail
    bgm.attach = fail
    try:
        autocut.render(
            media(), [(0.0, 2.0)], [], fail_out, conf,
            on_log=lambda *_a: None, bgm=True,
            bgm_conf={"enabled": "1", "folder": "任意", "sections": "1"},
            data={},
        )
    finally:
        bgm_sections.attach_sections = old_sections
        bgm.attach = old_attach
    ok, detail = verify(fail_out)
    if not ok:
        return ok, "混音异常：" + detail
    return True, "空曲库/混音异常均保留可播放成片"


# --------------------------------------------------------------------------
# 5. 出片取消
# --------------------------------------------------------------------------
def t_render_cancel():
    out = os.path.join(TMP, "取消测试.mp4")
    if os.path.isfile(out):
        os.remove(out)
    conf = autocut.load_conf()
    conf["render"]["scale"] = "320:-2"
    conf["render"]["workers"] = 1
    try:
        autocut.render(media(), [(0.0, 3.0), (5.0, 8.0)], [], out, conf,
                       on_log=lambda *_a: None, cancel=lambda: True)
    except autocut.RenderCancelled:
        if os.path.isfile(out):
            return False, "取消了却留下了输出文件"
        return True, "抛 RenderCancelled，无输出"
    return False, "取消没有生效（没有抛异常）"


def t_render_cancel_after_start():
    """worker 已经开始编码后再取消，也不能继续拼接最终成片。"""
    out = os.path.join(TMP, "启动后取消测试.mp4")
    if os.path.isfile(out):
        os.remove(out)
    conf = autocut.load_conf()
    conf["render"]["scale"] = "320:-2"
    conf["render"]["workers"] = 1
    checks = [0]

    def cancel():
        checks[0] += 1
        return checks[0] > 1

    try:
        autocut.render(media(), [(0.0, 3.0)], [], out, conf,
                       on_log=lambda *_a: None, cancel=cancel)
    except autocut.RenderCancelled:
        if checks[0] < 2:
            return False, "没有在任务启动后再次检查取消标志"
        if os.path.isfile(out):
            return False, "启动后取消仍留下了最终输出"
        return True, "分段收尾后停止，未拼接输出"
    return False, "启动后的取消没有生效"


def t_render_ok():
    out = os.path.join(TMP, "正常测试.mp4")
    conf = autocut.load_conf()
    conf["render"]["scale"] = "320:-2"
    conf["render"]["workers"] = 1
    info = autocut.render(media(), [(0.0, 3.0), (5.0, 8.0)], [], out, conf,
                          on_log=lambda *_a: None)
    if not isinstance(info, dict) or "timeline" not in info:
        return False, "返回值里没有 timeline：%s" % type(info)
    tl = info["timeline"]
    if len(tl) != 2 or abs(sum(x[2] for x in tl) - 6.0) > 0.1:
        return False, "时间轴不对：%s" % (tl,)
    if not os.path.isfile(out) or os.path.getsize(out) < 10000:
        return False, "输出文件不对"
    return True, "2 段 / 6.0 秒 / %.0f KB" % (os.path.getsize(out) / 1024)


def _fake_long_index():
    """造一份 30 分钟、带分数和剧本的索引（给力度测试和界面链路测试用）。"""
    import random
    words = ["机械", "动力", "压板机", "离心机", "电解", "储罐", "火箭", "月球",
             "氧气", "燃料", "硅岩", "钢板", "传送带", "漏斗", "转速", "配方"]
    segs, t = [], 20.0
    for i in range(600):
        rnd = random.Random(i * 7919)
        txt = "第%d句%s" % (i, "，".join(rnd.choice(words) for _ in range(6)))
        segs.append([round(t, 2), round(t + 2.6, 2), txt])
        t += 3.0
    dur = t
    data = {"duration": dur, "segments": segs, "semantic": [], "cards": [],
            "seg_feat": [[0.4, -28.0]] * len(segs), "complete": {},
            "scores": [round(0.30 + 0.45 * ((i * 37) % 100) / 100.0, 3)
                       for i in range(len(segs))],
            "script": {"mainline": "测试主线", "sections": [
                {"start": 20.0 + k * (dur / 6.0), "end": 20.0 + (k + 1) * (dur / 6.0),
                 "title": "第%d章" % (k + 1),
                 "advice": ["全部保留", "只留关键", "只留关键", "可以大幅删减",
                            "只留关键", "全部保留"][k]}
                for k in range(6)]}}
    # 界面剪辑链路会检查有没有画面/音量分析数据
    data["analysis"] = {"times": [float(x) for x in range(int(dur))],
                        "diffs": [0.05 for _ in range(int(dur))],
                        "runs": [[20.0, dur - 10.0]]}
    return data


def t_cut_levels():
    """三档力度：轻度只删空屏、中度删不连贯、重度严格按流程，时长必须递减。"""
    import autocut
    import random
    data = _fake_long_index()
    dur = float(data["duration"])
    # 设置里的"目标时长"会改变结果，测试期间锁成 0（自动），跑完还原
    _sp = os.path.join(BASE, "conf", "settings.json")
    _bak = None
    _cfg = {}
    try:
        if os.path.isfile(_sp):
            _bak = open(_sp, "rb").read()
            _cfg = json.loads(_bak.decode("utf-8"))
        _cfg["target_min"] = "0"
        with open(_sp, "w", encoding="utf-8") as fh:
            json.dump(_cfg, fh, ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        _bak = None
    conf = autocut.load_conf()
    segs_in = [(0.0, dur - 5.0)]
    got = {}
    for lv in ("light", "medium", "heavy"):
        out = autocut.apply_cut_level(data, segs_in, conf, lv, on_log=None)
        got[lv] = sum(b - a for a, b in out)
        first = out[0]
        if not (first[0] <= data["segments"][0][0] + 0.5):
            return False, "%s 档没从第一句开始（首段起 %.1f）" % (lv, first[0])
    if not (got["light"] > got["medium"] + 60):
        return False, ("轻度没比重度长：light %.0fs vs medium %.0fs"
                       % (got["light"], got["medium"]))
    if not (got["medium"] > got["heavy"] + 30):
        return False, ("中度没比重度长：medium %.0fs vs heavy %.0fs"
                       % (got["medium"], got["heavy"]))
    if _bak is not None:
        try:
            with open(_sp, "wb") as fh:
                fh.write(_bak)
        except OSError:
            pass
    return True, ("light %.1f / medium %.1f / heavy %.1f 分钟，均从片头开始"
                  % (got["light"] / 60, got["medium"] / 60, got["heavy"] / 60))



def t_levels_degraded_index():
    """索引没有分数/剧本（本地模型没跑成）时，三档力度不能撞车。"""
    import autocut
    data = _fake_long_index()
    data["scores"] = []          # 模拟"分析时打分失败"
    data["script"] = {}          # 模拟"剧本没生成"
    data["semantic"] = []
    dur = float(data["duration"])
    conf = autocut.load_conf()
    segs = [(20.0, dur - 10.0)]
    out = {}
    for lv in ("medium", "heavy"):
        sp = autocut.apply_cut_level(data, segs, conf, lv, on_log=None)
        out[lv] = sum(b - a for a, b in sp)
    if not out["medium"] or not out["heavy"]:
        return False, "退化索引下挑不出内容（会表现为三档一模一样）"
    if abs(out["medium"] - out["heavy"]) < 60:
        return False, ("中度 %.0f 秒 / 重度 %.0f 秒，差距太小（应明显不同）"
                       % (out["medium"], out["heavy"]))
    if out["heavy"] >= out["medium"]:
        return False, ("重度应比中度短：medium %.0f 秒 vs heavy %.0f 秒"
                       % (out["medium"], out["heavy"]))
    return True, ("退化索引下仍可分档：中度 %.1f / 重度 %.1f 分钟"
                  % (out["medium"] / 60, out["heavy"] / 60))


def t_script_selection_does_not_restore_rejected_gap():
    """中/重度剧本筛选不能把已淘汰的数秒废话重新并回成片。"""
    data = {
        "segments": [
            [0.0, 3.0, "第一段有效内容"],
            [3.2, 9.5, "低分离题废话"],
            [10.0, 13.0, "第二段有效内容"],
        ],
        "scores": [0.9, 0.1, 0.9],
        "script": {"sections": [
            {"start": 0.0, "end": 13.0, "title": "测试", "advice": "只留关键"},
        ]},
    }
    medium = autocut.select_by_script(data, level="medium")
    heavy = autocut.select_by_script(data, level="heavy")
    for name, spans in (("中度", medium), ("重度", heavy)):
        if not spans or len(spans) != 2:
            return False, "%s把低分间隙重新并回去了：%r" % (name, spans)
        if any(a < 3.2 and b > 9.5 for a, b in spans):
            return False, "%s输出仍跨过整段低分内容：%r" % (name, spans)
    return True, "中度 2.5 秒、重度 1.2 秒连接上限均未吞回废话"


def t_gui_cut_chain():
    """界面剪辑链路（把 render/judge 换成桩，不真出片）：必须不崩、段不为空。"""
    import json
    import shutil
    import autocut
    idx_dir = os.path.join(TMP, "idx_levels")
    if os.path.isdir(idx_dir):
        shutil.rmtree(idx_dir, ignore_errors=True)
    os.makedirs(idx_dir, exist_ok=True)
    data = _fake_long_index()
    data["source"] = media()
    data["name"] = "自测素材"
    with open(os.path.join(idx_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import cards_ui
    _app = QApplication.instance() or QApplication([])

    calls = {}
    real_judge = autocut.judge_transitions
    real_render = autocut.render

    def fake_judge(segments, src, d, conf, verbose=True):
        calls["judged"] = len(segments)
        return []

    def fake_render(src, segments, seams, out, conf, **kw):
        calls["rendered"] = len(segments)
        calls["total"] = sum(b - a for a, b in segments)
        with open(out, "wb") as fh:
            fh.write(b"x" * 2048)
        return {"out": out, "timeline": [], "pieces": len(segments),
                "sub_burned": 0, "minutes": calls["total"] / 60}

    autocut.judge_transitions = fake_judge
    autocut.render = fake_render
    errs, msgs = [], []
    # 力度由 conf\settings.json 决定，而这份设置是用户自己的（可能选了中/重）。
    # 测试要的是"链路不崩"，所以临时把力度锁成轻度，跑完原样还回去。
    _sp = os.path.join(BASE, "conf", "settings.json")
    _bak = None
    try:
        if os.path.isfile(_sp):
            _bak = open(_sp, "rb").read()
        _cfg = {}
        if _bak:
            try:
                _cfg = json.loads(_bak.decode("utf-8"))
            except ValueError:
                _cfg = {}
        _cfg["cut_level"] = "light"
        os.makedirs(os.path.dirname(_sp), exist_ok=True)
        with open(_sp, "w", encoding="utf-8") as fh:
            json.dump(_cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        _bak = None
    w = None
    status_cb = lambda m: msgs.append(str(m))
    error_cb = lambda m: errs.append(str(m))
    try:
        w = cards_ui.AutoCutWorker(idx_dir, os.path.join(TMP, "gui_cut.mp4"), 0)
        w.director_first = False
        w.mix_tracks = True
        w.status.connect(status_cb)
        w.err.connect(error_cb)
        w.run()
    finally:
        if w is not None:
            try:
                w.status.disconnect(status_cb)
                w.err.disconnect(error_cb)
            except (RuntimeError, TypeError):
                pass
            try:
                from PySide6.QtCore import QCoreApplication, QEvent
                w.deleteLater()
                QCoreApplication.sendPostedEvents(w, QEvent.DeferredDelete)
                _app.processEvents()
            except Exception:  # noqa: BLE001
                pass
        autocut.judge_transitions = real_judge
        autocut.render = real_render
        if _bak is not None:
            try:
                with open(_sp, "wb") as fh:
                    fh.write(_bak)
            except OSError:
                pass
        # 这条链路会按生产路径拉起共享本地模型。若留到后续 Qt 界面测试
        # 才关闭，Windows 在解释器收尾阶段会触发原生 0xc0000409。
        try:
            import proofread
            proofread._shutdown_shared()
        except Exception:  # noqa: BLE001
            pass
        try:
            import gc
            gc.collect()
        except Exception:  # noqa: BLE001
            pass

    if errs:
        return False, "界面链路报错：%s" % errs[0][:120]
    if not calls.get("rendered"):
        return False, "没有走到出片（可能空段/挑选环节就断了）"
    total_min = calls["total"] / 60.0
    if not (20.0 <= total_min <= 32.0):
        return False, ("轻度应保留约 30 分钟说话，实际 %.1f 分钟" % total_min)
    if not any("空段" in m for m in msgs):
        return False, "状态里没有空段阶段（说明空段检测被跳过）"
    return True, ("链路正常：空段→去重→力度→转场→出片，保留 %.1f 分钟"
                  % total_min)

def t_term_correct():
    """术语纠错：只改用户明确别名，全量词典不能擅自改正文。"""
    import terms
    cases = [("子缝开始介绍", "子枫开始介绍"),
             ("这里需要六氟化油", "这里需要六氟化铀")]
    ok = 0
    for src, want in cases:
        new, _reps = terms.correct(src)
        if new == want:
            ok += 1
    plain = "硅岩锭做好以后继续处理硅岩燃"
    new_plain, reps_plain = terms.correct(plain)
    if reps_plain:
        return False, "未授权正文被改坏：%s（%s）" % (new_plain, reps_plain[:2])
    if ok != len(cases):
        return False, "明确别名只纠对 %d/%d 例" % (ok, len(cases))
    return True, "明确别名纠对 %d/%d 例，全量词典未改正文" % (ok, len(cases))


def t_retake_pattern():
    """白录规律：报菜名→跑偏→重报同一批物件，要能认出来；不同话题不许误报。"""
    import retake_pattern as rp

    def seg(a, b, t):
        return [a, b, t]

    # 造一段：先报菜名，中间跑偏，停 2 秒后又重报同一批物件
    hit = {"segments": [
        seg(0.0, 2.0, "好我们先看堕落智能和过充水晶"),
        seg(2.0, 4.0, "这两个东西都要做"),
        seg(4.0, 6.0, "但是第一件事"),
        seg(6.0, 8.0, "先去把这个收藏关了"),
        seg(8.0, 9.0, "太多了"),
        seg(11.0, 13.0, "我们先看堕落智能和过充水晶"),
        seg(13.0, 15.0, "这两个东西都要做"),
        seg(15.0, 17.0, "先做两个东西"),
    ]}
    drops = rp.find_aborted_attempts(hit)
    if not drops:
        return False, "该认出来的白录没认出来"
    span = drops[0]
    if not (0.0 <= span["start"] <= 8.0 and 8.0 <= span["end"] <= 11.5):
        return False, "剪掉的区间不对：%.1f~%.1f" % (span["start"], span["end"])

    # 反差：两段讲不同东西，不许报
    clean = {"segments": [
        seg(0.0, 2.0, "好我们先看堕落智能和过充水晶"),
        seg(2.0, 4.0, "这两个东西都要做"),
        seg(6.0, 8.0, "然后去挖铁矿石"),
        seg(8.0, 10.0, "铁矿石在下面"),
        seg(10.0, 12.0, "挖完回来熔炼"),
    ]}
    if rp.find_aborted_attempts(clean):
        return False, "不同话题被误判成白录"
    return True, "认出 %.1f~%.1f（%.1f 秒）白录；不同话题不误报" % (
        span["start"], span["end"], span["dur"])


def t_bgm_sections():
    """BGM 分段：1~3 分钟随机，并优先落在合理段落边界。"""
    import numpy as np
    import bgm_sections as bs
    import bgm

    secs = bs.make_sections({}, 900.0, min_len=60.0, max_len=180.0, seed="A")
    same = bs.make_sections({}, 900.0, min_len=60.0, max_len=180.0, seed="A")
    other = bs.make_sections({}, 900.0, min_len=60.0, max_len=180.0, seed="B")
    if secs != same or secs == other:
        return False, "随机种子不可复现或不同种子没有变化：%r / %r" % (secs, other)
    if len(secs) < 5:
        return False, "900 秒只切了 %d 段" % len(secs)
    total = sum(b - a for a, b in secs)
    if abs(total - 900.0) > 0.01:
        return False, "段落总长 %.1f ≠ 900" % total
    bad = [round(b - a, 2) for a, b in secs if not (60.0 <= b - a <= 180.0)]
    if bad:
        return False, "出现不在 1~3 分钟内的 BGM 段：%s" % bad
    for duration in (181.0, 200.0):
        short = bs.make_sections({}, duration, min_len=60.0, max_len=180.0,
                                 seed="tail")
        if any(b - a < 60.0 or b - a > 180.0 for a, b in short):
            return False, "%.0f 秒素材产生短尾段：%r" % (duration, short)
    for duration in (45.0, 100.0):
        short = bs.make_sections({}, duration, min_len=60.0, max_len=180.0,
                                 seed="short")
        if short != [(0.0, duration)]:
            return False, "%.0f 秒短素材不应换曲：%r" % (duration, short)

    pieces = [("trim", 0.0, 90.0), ("trim", 100.0, 170.0),
              ("trim", 200.0, 300.0), ("trim", 300.0, 400.0)]
    data = {"script": {"sections": [
        {"start": 0.0, "end": 210.0, "title": "准备"},
        {"start": 210.0, "end": 400.0, "title": "推进"},
    ]}}
    natural = bs.make_sections(data, 360.0, pieces=pieces,
                               min_len=60.0, max_len=180.0, seed="chapter")
    first_cut = natural[0][1]
    if abs(first_cut - 170.0) > 0.01:
        return False, "没有优先吸附映射后的章节边界 170 秒：%r" % natural

    home = os.path.join(TMP, "bgm_home")
    os.makedirs(os.path.join(home, "conf"), exist_ok=True)
    env = dict(os.environ)
    env["SMARTCUT_HOME"] = home
    probe = subprocess.run(
        [sys.executable, "-c", "import bgm; print(bgm.CONF_PATH)"],
        cwd=BASE, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=ix.CREATE_NO_WINDOW,
    )
    shutil.rmtree(home, ignore_errors=True)
    expected_conf = os.path.normcase(os.path.join(home, "conf", "bgm.json"))
    if probe.returncode != 0 or os.path.normcase(probe.stdout.strip()) != expected_conf:
        return False, "打包版 BGM 配置没有指向安装根目录：%r / %r" % (
            probe.stdout.strip(), probe.stderr[-200:])
    sr = 48000
    x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)[:, None].repeat(2, 1)
    y = bgm.apply_dip(x, sr, [0.5], dip_db=4.0, dip_sec=1.2)
    db = lambda a: 20 * np.log10(max(float(np.sqrt(np.mean(a ** 2))), 1e-9))
    c0, c1 = int(0.48 * sr), int(0.52 * sr)
    depth = db(y[c0:c1]) - db(x[c0:c1])
    if not (-4.6 <= depth <= -3.4):
        return False, "下压深度 %.1f dB（应≈-4）" % depth
    return True, "900 秒随机切 %d 段；章节边界优先；接缝下压 %.1f dB" % (
        len(secs), depth)


def t_genre_gameplay():
    """体裁模式：时长使用软预算，每章都留，叙事骨架基本保全。"""
    import genre
    import stage

    data = __import__("json").load(
        open(r"D:\Media\cache\index.json", encoding="utf-8"))
    spans = genre.allocate(data, None, "medium", "gameplay")
    if not spans:
        return False, "没分配出片段"
    dur = float(data.get("duration") or 0)
    kept = sum(b - a for a, b in spans) / 60.0
    reference = genre.suggest_budget(data, "gameplay", "medium") / 60.0
    if not (reference * 0.78 <= kept <= reference * 1.20):
        return False, "成片 %.1f 分钟，偏离动态软预算 %.1f 分钟" % (kept, reference)
    secs = (data.get("script") or {}).get("sections") or []
    worst = 100.0
    for sec in secs:
        a, b = float(sec.get("start", 0)), float(sec.get("end", 0))
        if b - a < 5:
            continue
        got = sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)
        worst = min(worst, got / (b - a) * 100)
    if worst < 12.0:
        return False, "有章节只保留了 %.0f%%" % worst
    labels = stage.label_sentences(data)
    need = total = 0
    for i, lab in enumerate(labels):
        if lab in ("hook", "goal", "result", "tease"):
            total += 1
            a = float(data["segments"][i][0])
            if any(x <= a <= y for x, y in spans):
                need += 1
    cov = need / max(1, total) * 100
    if cov < 85:
        return False, "叙事骨架只覆盖 %.0f%%" % cov
    # 节奏：单段中位要贴近子枫（11.6 秒），段数 45~75
    import rhythm
    st = rhythm.stats(spans)
    if not (8.0 <= st["median"] <= 14.0):
        return False, "单段中位 %.1f 秒，脱离 8~14 秒区间" % st["median"]
    if not (45 <= st["n"] <= 80):
        return False, "段数 %d，不在 45~80" % st["n"]
    return True, "%.1f 分钟 / %d 段（中位 %.1f 秒）/ 最差章节 %.0f%% / 骨架覆盖 %.0f%%" % (
        kept, len(spans), st["median"], worst, cov)


def t_heavy_preserves_chapter_story_chain():
    """重度可以更短，但每章必须保留目标、低分关键过程、结果和过渡。"""
    import genre
    import autocut

    segments, scores, sections = [], [], []
    for chapter in range(3):
        base = chapter * 300.0
        sections.append({"start": base, "end": base + 300.0,
                         "title": "第%d章" % (chapter + 1)})
        for step in range(20):
            if step == 0:
                text = "这一章我们要找到出口"
            elif step == 1:
                text = "先确认当前任务背景"
            elif step == 10:
                text = "然后执行关键操作打开机关"
            elif step == 16:
                text = "机关已经成功启动完成"
            elif step == 18:
                text = "接下来进入下一个区域"
            elif step in (3, 5, 13):
                text = "重复闲聊与当前任务无关"
            else:
                text = "第%d步继续推进主要流程" % step
            start = base + step * 15.0
            segments.append([start, start + 12.0,
                             "第%d章%s" % (chapter + 1, text)])
            # 关键操作故意给低分：它仍属于因果链，不能被高分闲聊挤掉。
            scores.append(0.08 if step in (3, 5, 10, 13)
                          else 0.58 + (step % 4) * 0.05)
    data = {
        "duration": 900.0,
        "segments": segments,
        "scores": scores,
        "script": {"sections": sections},
    }
    medium = genre.allocate(data, None, "medium", "gameplay")
    heavy = genre.allocate(data, None, "heavy", "gameplay")
    medium_dur = sum(b - a for a, b in medium)
    heavy_dur = sum(b - a for a, b in heavy)
    if not (medium_dur > heavy_dur):
        return False, "重度没有短于中度：中 %.1f / 重 %.1f 秒" % (
            medium_dur, heavy_dur)
    ratio = heavy_dur / max(1.0, medium_dur)
    if not (0.55 <= ratio <= 0.85):
        return False, "重度与中度比例 %.0f%%，应在 55%%~85%% 的软区间" % (ratio * 100)

    def covered(spans, index):
        t = float(segments[index][0])
        return any(a <= t < b for a, b in spans)

    anchors = (0, 10, 16, 18)  # 目标、低分关键过程、结果、过渡
    for chapter in range(3):
        base = chapter * 20
        missing = [step for step in anchors if not covered(heavy, base + step)]
        if missing:
            return False, "重度第%d章缺故事阶段：%s" % (chapter + 1, missing)
    removed_side = sum(not covered(heavy, chapter * 20 + step)
                       for chapter in range(3) for step in (3, 5, 13))
    if removed_side < 6:
        return False, "重度没有优先删除重复/旁枝：只删 %d/9 句" % removed_side

    conf = {"select": {"genre": "gameplay", "auto_retake": "0",
                       "chapter_mode": "0"},
            "transition": {"shot_snap": "0"}}
    original_target = autocut._target_minutes
    try:
        autocut._target_minutes = lambda: 0.0
        final = autocut.apply_cut_level(
            data, [(float(s[0]), float(s[1])) for s in segments], conf,
            "heavy", on_log=lambda _m: None)
    finally:
        autocut._target_minutes = original_target
    for chapter in range(3):
        base = chapter * 20
        missing = [step for step in anchors if not covered(final, base + step)]
        if missing:
            return False, "最终预算复核又删掉第%d章故事阶段：%s" % (
                chapter + 1, missing)
    return True, "重度为中度 %.0f%%；三章目标/过程/结果/过渡均保留，旁枝删 %d/9" % (
        ratio * 100, removed_side)


def t_director_critic_repairs_story_chain():
    """导演审片必须补回低分因果步骤，并优先删非故事内容而非拒绝返工。"""
    import director_critic
    import genre

    segments, scores, sections = [], [], []
    texts = [
        "这一章我们要修好机器", "先确认任务背景", "然后安装关键核心",
        "高分但离题的闲聊", "继续无关支线展示", "机器已经成功启动完成",
        "评价效果很稳定", "接下来进入下一个任务",
    ]
    for chapter in range(2):
        base = chapter * 100.0
        sections.append({"start": base, "end": base + 100.0,
                         "title": "第%d章" % (chapter + 1), "advice": "全部保留"})
        for step, text in enumerate(texts):
            segments.append([base + step * 10.0, base + step * 10.0 + 6.0, text])
            scores.append(0.95 if step in (3, 4) else (0.10 if step == 2 else 0.65))
    data = {"duration": 200.0, "segments": segments, "scores": scores,
            "script": {"sections": sections}}
    initial = [(30.0, 46.0), (130.0, 146.0)]  # 只有高分支线，缺主线
    rebuilt, result = director_critic.review_and_rebuild(
        data, initial, level="heavy", target_seconds=80.0, max_rounds=3)
    if result["final"]["story_score"] < 0.999:
        return False, "返工后故事链只覆盖 %.0f%%" % (
            result["final"]["story_score"] * 100)
    required = genre.chapter_story_chain_indices(data, level="heavy")
    missing = [i for i in required
               if not any(a <= float(segments[i][0]) < b for a, b in rebuilt)]
    if missing:
        return False, "导演没有补回故事链句：%s" % missing
    for chapter in range(2):
        key_step = chapter * len(texts) + 2
        if not any(a <= float(segments[key_step][0]) < b for a, b in rebuilt):
            return False, "第%d章低分关键操作仍被高分支线挤掉" % (chapter + 1)
    removed_side = sum(
        not any(a <= float(segments[chapter * len(texts) + step][0]) < b
                for a, b in rebuilt)
        for chapter in range(2) for step in (3, 4))
    if removed_side < 2:
        return False, "导演没有优先删除非故事支线：%d/4" % removed_side
    if sum(b - a for a, b in rebuilt) > 80.0 * 1.20 + 0.5:
        return False, "返工超过重度软时长容差：%.1f 秒" % sum(b - a for a, b in rebuilt)
    return True, "故事链 %.0f%%，低分关键操作补回，支线删 %d/4，返工 %.1f 秒" % (
        result["final"]["story_score"] * 100, removed_side,
        sum(b - a for a, b in rebuilt))


def t_genre_short_heavy_entrypoint():
    """短素材重度剪辑必须真正进入体裁分配，不能静默降级。"""
    import autocut

    segments = []
    for i in range(12):
        a = float(i * 10)
        segments.append([a, a + 8.0, "第%d段有效讲解内容" % (i + 1)])
    data = {
        "duration": 120.0,
        "segments": segments,
        "scores": [0.55 + (i % 4) * 0.08 for i in range(len(segments))],
        "script": {"sections": [
            {"start": 0.0, "end": 60.0, "title": "前半"},
            {"start": 60.0, "end": 120.0, "title": "后半"},
        ]},
    }
    conf = {
        "select": {"genre": "gameplay", "auto_retake": "0",
                   "chapter_mode": "0"},
        "transition": {"shot_snap": "0"},
    }
    logs = []
    raw = [(float(s[0]), float(s[1])) for s in segments]
    original_target = autocut._target_minutes
    try:
        autocut._target_minutes = lambda: 0.0
        medium = autocut.apply_cut_level(
            data, raw, conf, "medium", on_log=lambda _m: None)
        spans = autocut.apply_cut_level(
            data, raw, conf, "heavy", on_log=logs.append)
    finally:
        autocut._target_minutes = original_target
    if not spans:
        return False, "短素材重度剪辑没有保留任何内容"
    skipped = [line for line in logs if "体裁模式跳过" in line]
    if skipped:
        return False, skipped[0]
    if not any("体裁=游戏实况" in line for line in logs):
        return False, "没有从 apply_cut_level 进入体裁分配"
    kept = sum(b - a for a, b in spans)
    spoken = sum(float(s[1]) - float(s[0]) for s in segments)
    medium_kept = sum(b - a for a, b in medium)
    if not (kept < medium_kept and kept <= medium_kept * 0.80):
        return False, "重度短素材没有明显短于中度：重 %.1f / 中 %.1f 秒" % (
            kept, medium_kept)
    if kept > spoken * 0.75:
        return False, "重度短素材保留 %.1f/%.1f 秒，压缩没有生效" % (kept, spoken)
    return True, "体裁入口正常，重度 %.1f 秒 / 中度 %.1f 秒" % (
        kept, medium_kept)


def t_gameplay_budget_is_dynamic():
    """游戏模式时长应随原片与章节复杂度变化，不固定卡在 15 分钟。"""
    import genre

    short = {"duration": 1200.0, "script": {"sections": [
        {"start": i * 300.0, "end": (i + 1) * 300.0} for i in range(4)]}}
    long = {"duration": 3600.0, "script": {"sections": [
        {"start": i * 450.0, "end": (i + 1) * 450.0} for i in range(8)]}}
    short_min = genre.suggest_budget(short, "gameplay", "medium") / 60.0
    long_min = genre.suggest_budget(long, "gameplay", "medium") / 60.0
    if not (8.0 <= short_min < long_min <= 28.0):
        return False, "动态预算异常：短片 %.1f / 长片 %.1f 分钟" % (short_min, long_min)
    if abs(short_min - 15.0) < 0.05 and abs(long_min - 15.0) < 0.05:
        return False, "仍然死守 15 分钟"
    return True, "20 分钟原片参考 %.1f 分，60 分钟原片参考 %.1f 分" % (
        short_min, long_min)


def t_cut_level_budget_logs():
    """预算日志必须使用实际档位，不能把比例误报成分钟上限。"""
    import re
    import autocut
    import genre

    data = {"duration": 951.75, "script": {"sections": [
        {"start": i * 105.75, "end": (i + 1) * 105.75,
         "title": "第%d章" % (i + 1)} for i in range(9)]}}
    medium_desc = genre.describe(data, [(0.0, 300.0)], "gameplay", level="medium")
    heavy_desc = genre.describe(data, [(0.0, 300.0)], "gameplay", level="heavy")
    pattern = r"软预算约\s*([0-9.]+)\s*分钟"
    mm = re.search(pattern, medium_desc)
    hm = re.search(pattern, heavy_desc)
    if not mm or not hm:
        return False, "体裁日志里缺少软预算：%s / %s" % (medium_desc, heavy_desc)
    medium_min, heavy_min = float(mm.group(1)), float(hm.group(1))
    if not heavy_min < medium_min:
        return False, "重度仍显示中度预算：中 %.1f / 重 %.1f" % (
            medium_min, heavy_min)

    segments = [[float(i * 10), float(i * 10 + 8),
                 "第%d段推进目标并展示结果" % (i + 1)] for i in range(12)]
    work = {
        "duration": 120.0,
        "segments": segments,
        "scores": [0.35 + (i % 6) * 0.1 for i in range(len(segments))],
        "script": {"sections": [
            {"start": 0.0, "end": 60.0, "title": "前半"},
            {"start": 60.0, "end": 120.0, "title": "后半"},
        ]},
    }
    conf = {"select": {"genre": "gameplay", "auto_retake": "0",
                       "chapter_mode": "0"},
            "transition": {"shot_snap": "0"}}
    original_target = autocut._target_minutes
    try:
        autocut._target_minutes = lambda: 0.0
        for level in ("medium", "heavy"):
            logs = []
            autocut.apply_cut_level(
                work, [(float(s[0]), float(s[1])) for s in segments],
                conf, level, on_log=logs.append)
            if any("上限 1 分钟" in line or "上限 0 分钟" in line for line in logs):
                return False, "%s 仍把档位比例当分钟：%s" % (level, logs[-4:])
            if not any("档位预算" in line for line in logs):
                return False, "%s 最终日志没有标明档位预算：%s" % (level, logs[-4:])
    finally:
        autocut._target_minutes = original_target
    return True, "预算显示中 %.1f / 重 %.1f 分，比例不再误报成分钟" % (
        medium_min, heavy_min)


def t_levels_distinct():
    """三档必须真的不同：轻度 > 中度 > 重度，且中度不超过轻度的 85%。"""
    import autocut
    import rhythm
    try:
        data = autocut.load_index(r"D:\Media\cache")
        dur = float(data["duration"])
        conf = autocut.load_conf()
        conf.setdefault("select", {}).update({"genre": "gameplay", "auto_retake": "1",
                                              "retake_llm": "1"})
        segs = autocut.keep_segments(
            autocut.detect_cuts_by_semantics(data, conf)
            or autocut.detect_cuts(data, conf)[0], dur)
        got = {}
        for lvl in ("light", "medium", "heavy"):
            spans = autocut.apply_cut_level(dict(data), segs, conf, lvl,
                                            on_log=lambda _m: None)
            st = rhythm.stats(spans)
            got[lvl] = (sum(b - a for a, b in spans) / 60.0,
                        st["n"], st["median"])
        L, M, H = got["light"][0], got["medium"][0], got["heavy"][0]
        if not (L > M > H):
            return False, "不单调：轻 %.1f / 中 %.1f / 重 %.1f" % (L, M, H)
        if M > L * 0.85:
            return False, "中度压得不够：轻 %.1f / 中 %.1f" % (L, M)
        if H > M * 0.90:
            return False, "重度压得不够：中 %.1f / 重 %.1f" % (M, H)
        if H < 3.0:
            return False, "重度只剩 %.1f 分钟（少于 3 分钟）" % H
        return True, "轻 %.1f / 中 %.1f / 重 %.1f 分钟（段数 %d/%d/%d）" % (
            L, M, H, got["light"][1], got["medium"][1], got["heavy"][1])
    finally:
        try:
            import proofread
            proofread._shutdown_shared()
        except Exception:  # noqa: BLE001
            pass
        try:
            import gc
            gc.collect()
        except Exception:  # noqa: BLE001
            pass


def t_index_pipeline():
    """索引主流程（不跑语音识别）：抽音轨 → 音量 → 抽帧 → 卡片 → index.json。"""
    out_dir = os.path.join(TMP, "idx")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    res = ix.index_video(media(), out_dir, model_size=None,
                         models_dir=os.path.join(BASE, "models"),
                         on_status=lambda *_a: None)
    if not os.path.isfile(os.path.join(out_dir, "index.json")):
        return False, "没写出 index.json"
    dur = float(res.get("duration") or 0)
    if abs(dur - 24.0) > 2.0:
        return False, "时长应约 24 秒，实际 %.1f" % dur
    cards = res.get("cards") or []
    if not cards:
        return False, "没切出卡片"
    for c in cards:
        if not (0 <= c["start"] < c["end"] <= dur + 0.1):
            return False, "卡片时间越界：%s" % (c,)
    if "analysis" not in res or not res["analysis"].get("times"):
        return False, "画面分析数据缺失"
    return True, "%d 张卡片 / %.1f 秒" % (len(cards), dur)


def t_transcribe_cancel():
    """普通/批处理转写都要把停止信号传播成 IndexCancelled。"""
    import types
    import faster_whisper

    segs = [types.SimpleNamespace(start=0.0, end=1.0, text="测试", words=[])]
    info = types.SimpleNamespace(duration=1.0)

    class FakeModel:
        def transcribe(self, *_a, **_kw):
            return iter(segs), info

    class FakeBatch:
        def __init__(self, model=None):
            self.model = model

        def transcribe(self, *_a, **_kw):
            return iter(segs), info

    old_load = ix._load_model
    old_vad = ix._vad_ready
    old_batch = faster_whisper.BatchedInferencePipeline
    try:
        ix._load_model = lambda *_a, **_kw: FakeModel()
        ix._vad_ready = lambda: False
        faster_whisper.BatchedInferencePipeline = FakeBatch
        for batch in (0, 2):
            checks = [0]

            def stop():
                checks[0] += 1
                return checks[0] > 1

            try:
                ix.transcribe(np.zeros(16000, dtype=np.float32), model_size="fake",
                              batch_size=batch, should_stop=stop)
            except ix.IndexCancelled:
                continue
            return False, "%s 转写没有传播取消" % ("批处理" if batch else "普通")
    finally:
        ix._load_model = old_load
        ix._vad_ready = old_vad
        faster_whisper.BatchedInferencePipeline = old_batch
    return True, "普通/批处理均抛 IndexCancelled"


def t_score_model_dimensions():
    """16D 模型等待画面特征；未知维度安全退回规则分。"""
    segs = [(0.0, 2.0, "开场说明"), (3.0, 6.0, "完成目标")]
    feature_data = {
        "segments": [list(x) for x in segs],
        "duration": 8.0,
        "analysis": {"diffs": [0.1, 0.2, 0.8, 0.3],
                     "audio_1s": [-30.0, -24.0, -20.0, -28.0]},
    }
    old = autocut.load_model
    logs = []
    try:
        autocut.load_model = lambda: {
            "mean": [0.0] * 16, "std": [1.0] * 16,
            "weights": [0.0] * 16, "bias": 0.0,
        }
        early = ix.score_sentences(segs, logs.append)
        if early is not None or not any("等待画面" in x for x in logs):
            return False, "16D 模型没有延后：%r / %s" % (early, logs)
        got = ix.score_sentences(segs, logs.append, feature_data=feature_data)
        if got != [0.5, 0.5]:
            return False, "16D 模型打分错误：%r" % got

        autocut.load_model = lambda: {
            "mean": [0.0] * 12, "std": [1.0] * 12,
            "weights": [0.0] * 12, "bias": 0.0,
        }
        fallback = ix.score_sentences(segs, logs.append, feature_data=feature_data)
        if not fallback or len(fallback) != len(segs):
            return False, "未知维度没有降级到规则分：%r" % fallback
    finally:
        autocut.load_model = old
    return True, "16D 延后打分成功，未知维度安全降级"


def t_llm_stage_cancel():
    """语义/剧本阶段在启动模型前必须响应取消。"""
    import script_writer

    segs = [(0.0, 1.0, "测试")]
    try:
        ix.semantic_pass(segs, should_stop=lambda: True)
    except ix.IndexCancelled:
        pass
    else:
        return False, "语义阶段没有传播 IndexCancelled"

    root = os.path.join(TMP, "cancel_script")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "index.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"segments": [list(x) for x in segs]}, fh,
                  ensure_ascii=False)
    try:
        script_writer.build(path, on_log=lambda _m: None,
                            should_stop=lambda: True)
    except script_writer.ScriptCancelled:
        pass
    else:
        return False, "剧本阶段没有响应取消"
    return True, "语义/剧本均在模型调用前取消"


def t_autopilot_output_dir():
    """输出目录独立，渲染参数透传，并拒绝假成功。"""
    import autopilot
    import rendered_output_qc

    idx = os.path.join(TMP, "autopilot_idx")
    out_dir = os.path.join(TMP, "autopilot_out")
    bad_dir = os.path.join(TMP, "autopilot_bad")
    os.makedirs(idx, exist_ok=True)
    for d in (out_dir, bad_dir):
        shutil.rmtree(d, ignore_errors=True)
    with open(os.path.join(idx, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({}, fh)

    data = {"source": media(), "duration": 24.0,
            "segments": [[0.0, 2.0, "测试"]], "script": {"sections": []}}
    conf = {"select": {}, "render": {"scale": "", "encoder": "x264"},
            "transition": {}}
    names = ("load_index", "load_conf", "detect_cuts_by_semantics", "detect_cuts",
             "keep_segments", "apply_cut_level", "judge_transitions", "render")
    old = {name: getattr(autocut, name) for name in names}
    old_review = rendered_output_qc.review_rendered_output
    try:
        autocut.load_index = lambda *_a, **_kw: data
        autocut.load_conf = lambda: conf
        autocut.detect_cuts_by_semantics = lambda *_a, **_kw: [(0.0, 2.0)]
        autocut.detect_cuts = lambda *_a, **_kw: ([(0.0, 2.0)], {})
        autocut.keep_segments = lambda spans, *_a, **_kw: spans
        autocut.apply_cut_level = lambda *_a, **_kw: [(0.0, 2.0)]
        autocut.judge_transitions = lambda *_a, **_kw: []
        rendered_output_qc.review_rendered_output = lambda *_a, **_kw: {
            "passed": True, "score": 100.0, "veto_count": 0,
            "major_issues": 0, "findings": [],
        }

        seen = {}

        def good_render(_src, _spans, _seams, path, *_a, **_kw):
            seen.update(_kw)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(media(), path)
            return {"minutes": 0.03, "timeline": [(0.0, 0.0, 2.0)]}

        cancel_cb = lambda: False
        autocut.render = good_render
        res = autopilot.run_one(media(), index_dir=idx, output_dir=out_dir,
                                mode="general", use_bgm=False, burn_subs=True,
                                loudness_norm=False, should_cancel=cancel_cb,
                                on_log=lambda *_a: None)
        if os.path.dirname(os.path.abspath(res["out"])) != os.path.abspath(out_dir):
            return False, "output_dir 未生效：%s" % res["out"]
        if seen.get("cancel") is not cancel_cb:
            return False, "should_cancel 没传给 render"
        if seen.get("sub_burn") is not True:
            return False, "burn_subs 没传给 render"
        if str((seen.get("bgm_conf") or {}).get("loudness_norm")) != "0":
            return False, "loudness_norm 没覆盖 BGM 配置"
        made = res.get("subtitles") or []
        if len(made) != 2 or any(not os.path.isfile(p) or os.path.getsize(p) <= 0
                                 for p in made):
            return False, "没有生成有效 SRT/ASS：%s" % made
        report_text = open(res["report"], encoding="utf-8").read()
        if "字幕：2 个" not in report_text:
            return False, "报告没有记录字幕文件"

        autocut.render = lambda *_a, **_kw: {"minutes": 0.03}
        try:
            autopilot.run_one(media(), index_dir=idx, output_dir=bad_dir,
                              mode="general", use_bgm=False,
                              on_log=lambda *_a: None)
        except RuntimeError as exc:
            if "没有生成有效文件" not in str(exc):
                return False, "失败提示不明确：%s" % exc
        else:
            return False, "render 未写文件却被当成成功"
    finally:
        rendered_output_qc.review_rendered_output = old_review
        for name, value in old.items():
            setattr(autocut, name, value)
    return True, "输出目录/取消/烧录/BGM 参数生效，SRT/ASS 已生成，缺失成片会报错"


def t_edit_mode_pipeline():
    """模式必须集中配置，并从前台工作线程传进自动任务。"""
    import autopilot
    import cards_ui
    import edit_modes
    import inspect
    import ui_main_v2

    gameplay = edit_modes.apply_to_conf({"select": {}}, "gameplay", clone=True)
    general = edit_modes.apply_to_conf({"select": {}}, "general", clone=True)
    if gameplay["select"].get("genre") != "gameplay":
        return False, "游戏模式没有启用 gameplay 体裁"
    if general["select"].get("genre") != "off":
        return False, "通用模式没有关闭游戏专用分配"

    seen = {}
    old = autopilot.run_one
    try:
        def fake_run(*_args, **kwargs):
            seen.update(kwargs)
            return {"out": "ok.mp4"}

        autopilot.run_one = fake_run
        worker = ui_main_v2.CutWorker("fake.mp4", mode="general")
        worker.run()
    finally:
        autopilot.run_one = old
    if seen.get("mode") != "general":
        return False, "前台工作线程没有透传模式：%r" % seen
    legacy = inspect.getsource(cards_ui.MainWindow._run_autopilot)
    if "mode=mode" not in legacy:
        return False, "旧界面自动任务没有透传模式"
    return True, "游戏/通用配置不同，直剪工作线程透传 general"


# --------------------------------------------------------------------------
# 6. 界面
# --------------------------------------------------------------------------
def t_ui():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import cards_ui
    import bgm
    app = QApplication.instance() or QApplication([])
    win = cards_ui.MainWindow()
    dlg = cards_ui.SettingsDialog()

    sp = cards_ui.SETTINGS_PATH
    bp = os.path.join(BASE, "conf", "bgm.json")
    sb = open(sp, "rb").read() if os.path.isfile(sp) else None
    bb = open(bp, "rb").read() if os.path.isfile(bp) else None
    try:
        # 1) 真曲库：配置里的目录存在时，计数必须算出来
        fol = str(bgm.load_conf().get("folder", "") or "")
        n_real = dlg.bgmn_lb.text()
        if os.path.isdir(fol) and bgm.list_tracks(fol):
            if "首" not in n_real:
                return False, "曲库计数没算出来：%r" % n_real

        # 2) 换一个目录 → 保存 → conf/bgm.json 里真的改掉
        newdir = os.path.join(BASE, "conf")
        dlg.bgmdir_edit.setText(newdir)
        before = json.loads(sb.decode("utf-8")) if sb else {}
        dlg._save()
        after = json.load(open(sp, encoding="utf-8"))
        lost = [k for k in before if k not in after]
        if lost:
            return False, "保存丢了配置项：%s" % lost
        if "bgm" not in after:
            return False, "没写入 bgm 开关"
        got = json.load(open(bp, encoding="utf-8"))
        if os.path.normcase(got.get("folder", "")) != os.path.normcase(newdir):
            return False, "曲库目录没写进 bgm.json：%r" % got.get("folder")
        # 音量控件要能写进 bgm.json（默认 -22，不闪避）
        dlg.bgmvol.setValue(-25.0)
        dlg._save()
        got = json.load(open(bp, encoding="utf-8"))
        if str(got.get("level_db")) != "-25":
            return False, "BGM 音量没写进 bgm.json：%r" % got.get("level_db")
        if str(got.get("duck_db")) != "0":
            return False, "闪避应该保持 0，实际 %r" % got.get("duck_db")
        # 音量均衡开关也要能存
        dlg.bgmeven_chk.setChecked(False)
        dlg._save()
        got = json.load(open(bp, encoding="utf-8"))
        if str(got.get("loudness_norm")) != "0":
            return False, "音量拉齐开关没写进 bgm.json：%r" % got.get("loudness_norm")
        n_all = dlg.bgmn_lb.text()
    finally:
        if sb:
            open(sp, "wb").write(sb)
        if bb:
            open(bp, "wb").write(bb)
        win.close()
        dlg.close()
    return True, ("主窗口/设置窗口正常；真曲库计数=%s；换目录能存、保存不丢配置"
                  % (n_real or n_all))


def t_single_instance_guard():
    """同一 GUI 互斥量只能被首个实例取得。"""
    import cards_ui

    name = "Local\\SmartCutGUIKitSelftest_%d_%d" % (os.getpid(), id(object()))
    if not cards_ui._spawn_guard(name, activate=False):
        return False, "首个 GUI 实例没能取得互斥量"
    if cards_ui._spawn_guard(name, activate=False):
        return False, "第二个 GUI 实例没有被互斥量拦截"
    return True, "首实例通过，第二实例被拦截"


def t_child_processes_are_fully_detached():
    """分析链路的控制台程序必须脱离 conhost，不能只做隐藏。"""
    import no_console
    import winproc

    needed = no_console.CREATE_NO_WINDOW | no_console.DETACHED_PROCESS
    if no_console.PROCESS_FLAGS & needed != needed:
        return False, "全局子进程标志缺少 CREATE_NO_WINDOW/DETACHED_PROCESS"
    flags = int(winproc._hidden_kw({}).get("creationflags") or 0)
    if flags & needed != needed:
        return False, "winproc 默认没有彻底脱离控制台：0x%X" % flags
    return True, "FFmpeg/ffprobe/llama 默认无窗口且脱离 conhost"


def t_popup_window_guard_rules():
    """异常标题栏必须拦截，正常主窗口和对话框不能误伤。"""
    import window_guard

    hide = window_guard._should_hide
    if not hide("智能剪辑助手", "Qt6120QWindowIcon", 220, 42, 0, True):
        return False, "没有识别截图中的无主标题栏窗口"
    if hide("智能剪辑助手", "Qt6120QWindowIcon", 220, 42, 123, True):
        return False, "误判了有主窗口 owner 的小型对话框"
    if not hide("", "ConsoleWindowClass", 800, 300, 0, True):
        return False, "没有拦截本程序子进程的控制台窗口"
    if hide("智能剪辑助手", "Qt6120QWindowIcon", 220, 42, 0, False):
        return False, "误判了不属于本程序链路的同名窗口"
    if hide("智能剪辑助手", "Qt6120QWindowIcon", 1456, 940, 0, True):
        return False, "误判了主窗口"
    if hide("设置", "Qt6120QWindowIcon", 720, 620, 123, True):
        return False, "误判了设置窗口"
    if hide("关于 智能剪辑助手", "Qt6120QWindowIcon", 440, 150, 0, True):
        return False, "误判了关于窗口"
    if hide("智能剪辑助手", "Qt6120QWindowIcon", 440, 150, 123, True):
        return False, "误判了正常消息框"
    return True, "截图小窗/子控制台会隐藏，主窗口与正常对话框保留"


def t_queue_rows_are_not_top_level_windows():
    """队列刷新创建的行控件从出生起就必须属于列表 viewport。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QApplication, QProgressBar
    import ui_queue
    import ui_queuepanel

    app = QApplication.instance() or QApplication([])
    top_level_shows = []

    class _ProgressShowWatch(QObject):
        def eventFilter(self, obj, event):
            if (event.type() == QEvent.Show and
                    isinstance(obj, QProgressBar) and obj.isWindow()):
                top_level_shows.append((type(obj).__name__, obj.width(),
                                        obj.height()))
            return False

    watch = _ProgressShowWatch()
    app.installEventFilter(watch)
    item = {
        "path": os.path.join(TMP, "queue-row.mp4"),
        "status": "run", "pct": 0.25, "msg": "语义分析中",
        "out": "", "dur": 60.0, "level": "medium",
        "out_dir": "", "tracks": None,
    }
    panel = ui_queuepanel.QueuePanel()
    dialog = ui_queue.QueueDialog()
    fallback = None
    try:
        panel.items = [dict(item)]
        panel._refresh()
        app.processEvents()
        embedded = panel.list.itemWidget(panel.list.item(0))
        if embedded is None or embedded.isWindow():
            return False, "内嵌队列行仍被创建成顶级窗口"
        if embedded.parentWidget() is not panel.list.viewport():
            return False, "内嵌队列行没有直接绑定列表 viewport"

        dialog.items = [dict(item)]
        dialog._refresh_list()
        app.processEvents()
        expanded = dialog.list.itemWidget(dialog.list.item(0))
        if expanded is None or expanded.isWindow():
            return False, "展开队列行仍被创建成顶级窗口"
        if expanded.parentWidget() is not dialog.list.viewport():
            return False, "展开队列行没有直接绑定列表 viewport"

        fallback = ui_queue.QueueRow(dict(item), 0,
                                     parent=dialog.list.viewport())
        if fallback.isWindow():
            return False, "展开队列回退行仍会成为顶级窗口"
        app.processEvents()
        if top_level_shows:
            return False, "队列刷新期间显示了顶级进度条：%r" % top_level_shows
    finally:
        app.removeEventFilter(watch)
        if fallback is not None:
            fallback.deleteLater()
        dialog.close()
        panel.close()
        app.processEvents()
    return True, "内嵌/展开/回退队列行均直接绑定 viewport，不再闪顶级小窗"


def t_ui_v2_settings_bgm():
    """新版设置必须能编辑并保存独立的 BGM 配置。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import cards_ui
    import ui_settings

    app = QApplication.instance() or QApplication([])
    root = os.path.join(TMP, "ui_settings_v2")
    folder = os.path.join(root, "bgm")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "conf"), exist_ok=True)
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, "one.mp3"), "wb").write(b"test")
    nested = os.path.join(folder, "nested")
    os.makedirs(nested, exist_ok=True)
    open(os.path.join(nested, "two.ogg"), "wb").write(b"test")
    open(os.path.join(nested, "ignore.txt"), "wb").write(b"test")
    with open(os.path.join(root, "conf", "bgm.json"), "w", encoding="utf-8") as fh:
        json.dump({"folder": folder, "level_db": -17, "dip_db": "7.5",
                   "loudness_norm": "0", "custom_key": "keep-me"},
                  fh, ensure_ascii=False)

    old_home = ui_settings._home
    old_load = cards_ui.load_settings
    old_save = cards_ui.save_settings
    dlg = None
    try:
        ui_settings._home = lambda: root
        cards_ui.load_settings = lambda: {"model": "large-v3-turbo"}
        cards_ui.save_settings = lambda *_a, **_kw: None
        dlg = ui_settings.SettingsDialogV2(initial_page=3)
        if dlg.nav.currentRow() != 3 or dlg.stack.currentIndex() != 3:
            return False, "快捷入口没有定位到背景音乐页"
        if dlg.bgm_folder_edit.text() != folder:
            return False, "曲库路径未加载：%r" % dlg.bgm_folder_edit.text()
        if dlg.bgm_count_label.text() != "2 首":
            return False, "曲目数量未递归统计：%r" % dlg.bgm_count_label.text()
        if abs(dlg.bgm_level.value() - (-17.0)) > 0.01:
            return False, "音量控件未加载 level_db：%r" % dlg.bgm_level.value()

        new_folder = os.path.join(root, "new-bgm")
        os.makedirs(new_folder, exist_ok=True)
        open(os.path.join(new_folder, "new.wav"), "wb").write(b"test")
        dlg.bgm_folder_edit.setText(new_folder)
        dlg.bgm_level.setValue(-25.0)
        dlg.accept()
        got = json.load(open(os.path.join(root, "conf", "bgm.json"), encoding="utf-8"))
        if os.path.normcase(got.get("folder", "")) != os.path.normcase(new_folder):
            return False, "曲库目录没有保存：%r" % got.get("folder")
        if str(got.get("level_db")) != "-25":
            return False, "音量没有保存：%r" % got.get("level_db")
        for key, want in (("dip_db", "7.5"), ("loudness_norm", "0"),
                          ("custom_key", "keep-me")):
            if str(got.get(key)) != want:
                return False, "保存时丢失 BGM 参数 %s=%r" % (key, got.get(key))
    finally:
        if dlg is not None:
            dlg.close()
        ui_settings._home = old_home
        cards_ui.load_settings = old_load
        cards_ui.save_settings = old_save
        shutil.rmtree(root, ignore_errors=True)
    return True, "曲库路径/递归计数/音量可编辑保存，其他 BGM 参数保留"


def t_queue_model_wiring():
    """队列必须把识别模型和剪辑模式传到各自后台。"""
    import autopilot
    import ui_queue

    root = os.path.join(TMP, "queue_model")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    src = os.path.join(root, "fake.mp4")
    open(src, "wb").write(b"0" * 20000)
    seen = {}
    cut_seen = {}
    old_index = ix.index_video
    old_run = autopilot.run_one
    try:
        def fake_index(path, out_dir, **kwargs):
            seen.update(kwargs)
            os.makedirs(out_dir, exist_ok=True)
            return {"source": path, "duration": 1.0}

        ix.index_video = fake_index
        autopilot.run_one = lambda *_a, **kw: cut_seen.update(kw) or {"out": ""}
        worker = ui_queue.QueueWorker(
            [{"path": src, "status": "wait", "level": "medium"}],
            auto_cut=True, model_dir=os.path.join(BASE, "models"),
            model_size="large-v3-turbo", edit_mode="general",
        )
        worker.run()
        if seen.get("model_size") != "large-v3-turbo":
            return False, "实际传入 %r" % seen.get("model_size")
        if cut_seen.get("mode") != "general":
            return False, "队列未透传剪辑模式：%r" % cut_seen
        if not cut_seen.get("task_id") or not cut_seen.get("resume"):
            return False, "队列没有沿用分析任务继续智能剪辑：%r" % cut_seen
        if cut_seen.get("memory") is None:
            return False, "队列没有把任务记忆传给统一工作流"
    finally:
        ix.index_video = old_index
        autopilot.run_one = old_run
        shutil.rmtree(root, ignore_errors=True)
    return True, "识别模型、general 模式和同一任务记忆均传到队列后台"


def t_queue_start_defaults_to_analysis_and_switches_to_auto():
    """左上角开始默认只分析；显式开关后才允许进入自动出片。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import autopilot
    import cards_ui
    import ui_main_v2
    import ui_queue
    import ui_queuepanel

    app = QApplication.instance() or QApplication([])
    root = os.path.join(TMP, "queue_level_rerun")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    normal = os.path.join(root, "normal.mp4")
    custom = os.path.join(root, "custom.mp4")
    open(normal, "wb").write(b"0" * 20000)
    open(custom, "wb").write(b"1" * 20000)
    indexed = []
    cut_seen = []
    old_index = ix.index_video
    old_run = autopilot.run_one
    old_runner = ui_queuepanel._PlainRunner
    old_load = cards_ui.load_settings
    old_save = cards_ui.save_settings
    panel = None
    dialog = None
    try:
        def fake_index(path, out_dir, **_kwargs):
            indexed.append(os.path.basename(path))
            os.makedirs(out_dir, exist_ok=True)
            return {"source": path, "duration": 1.0}

        def fake_run(path, **kwargs):
            cut_seen.append((os.path.basename(path), kwargs.get("level")))
            return {"out": ""}

        class _SyncRunner:
            def __init__(self, worker):
                self.w = worker

            def start(self):
                self.w.run()

            def isRunning(self):
                return False

            def stop(self):
                self.w.stop()

            def skip_current(self):
                self.w.skip_current()

        ix.index_video = fake_index
        autopilot.run_one = fake_run
        ui_queuepanel._PlainRunner = _SyncRunner
        panel = ui_queuepanel.QueuePanel()
        panel.items = [
            {"path": normal, "status": "done", "pct": 1.0, "msg": "",
             "out": "", "dur": 1.0, "level": "light",
             "level_custom": False, "out_dir": "", "tracks": None},
            {"path": custom, "status": "done", "pct": 1.0, "msg": "",
             "out": "", "dur": 1.0, "level": "light",
             "level_custom": True, "out_dir": "", "tracks": None},
        ]
        saved = {}
        cards_ui.load_settings = lambda: {"cut_level": "light", "custom_key": "keep"}
        cards_ui.save_settings = lambda cfg: saved.update(cfg)
        fake_main = type("_FakeMain", (), {"queue_panel": panel})()
        ui_main_v2.MainWindowV2._sync_main_level(fake_main, "heavy")
        if saved.get("cut_level") != "heavy" or saved.get("custom_key") != "keep":
            return False, "主界面切档没有持久化或丢了其他设置：%r" % saved
        if panel.cb_auto_workflow.isChecked():
            return False, "全自动出片没有默认关闭"
        panel.start_all()
        app.processEvents()
        if sorted(indexed) != ["custom.mp4", "normal.mp4"]:
            return False, "默认开始没有完整分析队列：%r" % indexed
        if cut_seen:
            return False, "默认分析错误进入自动出片：%r" % cut_seen
        if panel.worker.w.auto_cut or panel.worker.w.merge_after:
            return False, "默认 worker 仍开启剪辑/合并"
        if any(it.get("out") for it in panel.items):
            return False, "纯分析不应生成视频路径：%r" % panel.items

        panel.cb_auto_workflow.setChecked(True)
        panel.start_all()
        app.processEvents()
        got = dict(cut_seen)
        if got.get("normal.mp4") != "heavy":
            return False, "开启自动出片后未使用新档：%r" % cut_seen
        if got.get("custom.mp4") != "light":
            return False, "单项力度覆盖被全局档位抹掉：%r" % cut_seen
        if not panel.worker.w.auto_cut:
            return False, "打开开关后没有进入自动出片"
        if panel.worker.w.merge_after:
            return False, "全自动出片不应默认合并不同素材"

        panel.cb_auto_workflow.setChecked(False)
        dialog = ui_queue.QueueDialog(panel=panel)
        if dialog.cb_autocut.isChecked() or dialog.cb_merge.isChecked():
            return False, "展开队列没有继承默认关闭状态"
        dialog.cb_autocut.setChecked(True)
        dialog.cb_merge.setChecked(True)
        if not panel.cb_auto_workflow.isChecked() or not panel.merge_after:
            return False, "展开队列开关没有同步到左侧队列"
        panel.cb_auto_workflow.setChecked(False)
        dialog._mirror_panel()
        if dialog.cb_autocut.isChecked() or dialog.cb_merge.isChecked():
            return False, "左侧关闭后展开队列仍残留自动/合并状态"
    finally:
        ix.index_video = old_index
        autopilot.run_one = old_run
        ui_queuepanel._PlainRunner = old_runner
        cards_ui.load_settings = old_load
        cards_ui.save_settings = old_save
        if dialog is not None:
            dialog.close()
        if panel is not None:
            panel.close()
        app.processEvents()
        shutil.rmtree(root, ignore_errors=True)
    return True, "默认只分析；打开开关后才自动出片，且不会默认合并"


def t_ui_v2():
    """默认新版界面：加载、分类、预览和切换素材必须可用。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import ui_main_v2
    import ui_settings

    app = QApplication.instance() or QApplication([])
    root = os.path.join(TMP, "ui_v2")
    one = os.path.join(root, "one")
    two = os.path.join(root, "two")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(one, exist_ok=True)
    os.makedirs(two, exist_ok=True)
    os.makedirs(os.path.join(root, "conf"), exist_ok=True)
    with open(os.path.join(root, "conf", "bgm.json"), "w", encoding="utf-8") as fh:
        json.dump({"folder": one, "level_db": "-19", "dip_db": "3.5"},
                  fh, ensure_ascii=False)
    fake_src = os.path.join(root, "source.mp4")
    open(fake_src, "wb").write(b"preview-test")
    data1 = {
        "source": fake_src, "duration": 8.0, "width": 640, "height": 360,
        "audio_streams": [{"ordinal": i} for i in range(6)],
        "cards": [
            {"id": "a", "start": 0.0, "end": 2.5, "text": "开场说明",
             "cats": ["开场", "高光"]},
            {"id": "b", "start": 2.5, "end": 7.0, "text": "知识讲解",
             "cats": ["知识"]},
        ],
        "segments": [[0.0, 2.5, "开场说明"], [2.5, 7.0, "知识讲解"]],
        "analysis": {"times": [0.0, 2.5, 7.0]},
    }
    data2 = {
        "source": fake_src, "duration": 4.0, "width": 640, "height": 360,
        "audio_streams": [{"ordinal": 0}],
        "cards": [{"id": "c", "start": 0.5, "end": 3.5,
                   "text": "第二份索引", "cats": ["结果"]}],
        "segments": [[0.5, 3.5, "第二份索引"]],
        "analysis": {"times": [0.5, 3.5]},
    }
    for folder, data in ((one, data1), (two, data2)):
        with open(os.path.join(folder, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    import cards_ui
    import ui_dialogs

    opened = []
    render_tracks = {"value": None}

    class FakePreviewDialog:
        def __init__(self, *args, **kwargs):
            opened.append({"args": args, "kwargs": kwargs, "exec": False})

        def exec(self):
            opened[-1]["exec"] = True
            return 0

    old_preview_dialog = cards_ui.PreviewDialog
    old_load_render_conf = ui_dialogs.load_render_conf
    old_home = ui_main_v2._home
    old_settings_dialog = ui_settings.SettingsDialogV2
    opened_settings = []

    class FakeSettingsDialog:
        def __init__(self, _parent=None, initial_page=None):
            opened_settings.append(initial_page)

        def exec(self):
            with open(os.path.join(root, "conf", "bgm.json"), "w", encoding="utf-8") as fh:
                json.dump({"folder": two, "level_db": "-27", "dip_db": "5"},
                          fh, ensure_ascii=False)
            return 1

    cards_ui.PreviewDialog = FakePreviewDialog
    ui_dialogs.load_render_conf = lambda: {
        "render": {"tracks": render_tracks["value"]}
    }
    ui_main_v2._home = lambda: root
    ui_settings.SettingsDialogV2 = FakeSettingsDialog
    win = ui_main_v2.MainWindowV2()
    try:
        if not hasattr(win, "btn_bgm_settings"):
            return False, "主界面 BGM 面板没有快速设置按钮"
        if "−19 dB" not in win.lbl_bgm2.text() or "3.5 dB" not in win.lbl_bgm2.text():
            return False, "主界面没有读取真实 BGM 音量：%r" % win.lbl_bgm2.text()
        win.btn_bgm_settings.click()
        app.processEvents()
        if opened_settings != [3]:
            return False, "快速设置没有直达背景音乐页：%r" % opened_settings
        if two not in win.lbl_bgm.text() or "−27 dB" not in win.lbl_bgm2.text():
            return False, "关闭快速设置后主界面没有刷新：%r / %r" % (
                win.lbl_bgm.text(), win.lbl_bgm2.text())
        if win.status.toast_holder.isWindow():
            return False, "状态栏浮层被错误创建成顶级窗口（会在分析时弹小窗）"
        if win.status.toast_holder.parentWidget() is not win.status:
            return False, "状态栏浮层没有绑定到主状态栏"
        win.load_index_dir(one)
        win.show()
        app.processEvents()
        if len(win.cards_all) != 2:
            return False, "第一份索引应有 2 张卡，实际 %d" % len(win.cards_all)
        cats = {str(win.cb_cat.itemData(i)) for i in range(win.cb_cat.count())
                if win.cb_cat.itemData(i)}
        if not {"开场", "高光", "知识"}.issubset(cats):
            return False, "分类没填完整：%s" % sorted(cats)

        QTest.mouseDClick(win.tiles["a"], Qt.LeftButton)
        app.processEvents()
        if "开场说明" not in win.lbl_sel_text.text():
            return False, "预览文字没更新：%r" % win.lbl_sel_text.text()
        if not opened or not opened[-1]["exec"]:
            return False, "双击卡片没有打开播放器"
        if opened[-1]["kwargs"].get("audio_streams") != data1["audio_streams"]:
            return False, "新版界面没有把全部音轨交给播放器：%r" % opened[-1]
        if opened[-1]["kwargs"].get("audio_selection_explicit"):
            return False, "未选择音轨时被错误标记为显式选择"

        render_tracks["value"] = [1, 4]
        QTest.mouseDClick(win.tiles["b"], Qt.LeftButton)
        app.processEvents()
        if opened[-1]["kwargs"].get("audio_streams") != [1, 4]:
            return False, "全局音轨选择没有作用于预览：%r" % opened[-1]
        if not opened[-1]["kwargs"].get("audio_selection_explicit"):
            return False, "全局音轨选择没有标记为显式选择"

        win.queue_panel.items = [{"path": fake_src, "tracks": [2, 5]}]
        QTest.mouseDClick(win.tiles["a"], Qt.LeftButton)
        app.processEvents()
        if opened[-1]["kwargs"].get("audio_streams") != [2, 5]:
            return False, "单素材音轨选择没有优先于全局设置：%r" % opened[-1]
        win.queue_panel.items = []
        render_tracks["value"] = None
        win._clear_sel()
        win.toggle_card("a")
        if len(win.selected) != 1:
            return False, "选中卡片失败：%d" % len(win.selected)

        before_switch = len(opened)
        win.load_index_dir(two)
        if win.selected:
            return False, "切换索引后仍残留 %d 张选择" % len(win.selected)
        if len(win.cards_all) != 1 or str(win.cards_all[0]["id"]) != "c":
            return False, "切换后卡片没有替换：%s" % win.cards_all
        QTest.mouseDClick(win.tiles["c"], Qt.LeftButton)
        app.processEvents()
        if "第二份索引" not in win.lbl_sel_text.text():
            return False, "第二份索引预览失败"
        if len(opened) != before_switch + 1 or not opened[-1]["exec"]:
            return False, "切换索引后双击没有再次打开播放器：%r" % opened
        old_cfg = win._cfg
        win._cfg = lambda: {"model": "large-v3-turbo"}
        mode_i = win.mode_combo.findData("general")
        win.mode_combo.blockSignals(True)
        win.mode_combo.setCurrentIndex(mode_i)
        win.mode_combo.blockSignals(False)
        try:
            opts = win._queue_options()
        finally:
            win._cfg = old_cfg
        if opts.get("model_size") != "large-v3-turbo":
            return False, "主界面队列选项没有读取识别模型：%s" % opts
        if opts.get("edit_mode") != "general":
            return False, "主界面队列选项没有透传剪辑模式：%s" % opts
    finally:
        win.close()
        cards_ui.PreviewDialog = old_preview_dialog
        ui_dialogs.load_render_conf = old_load_render_conf
        ui_main_v2._home = old_home
        ui_settings.SettingsDialogV2 = old_settings_dialog
        shutil.rmtree(root, ignore_errors=True)
    return True, "BGM 快速设置可直达并刷新；双击预览遵循音轨选择"


def t_installer_check_exit():
    """安装体检必须用退出码向自动化准确报告成败。"""
    import installer

    old_argv = list(sys.argv)
    old_health = installer.health_check
    old_setup = installer._setup_log
    try:
        installer._setup_log = lambda: None
        for health, want in ((False, 1), (True, 0)):
            installer.health_check = lambda value=health: value
            sys.argv = ["installer.py", "--check"]
            got = installer.main()
            if got != want:
                return False, "health_check=%s 时返回 %r，期望 %d" % (
                    health, got, want)
    finally:
        sys.argv = old_argv
        installer.health_check = old_health
        installer._setup_log = old_setup
    return True, "失败返回 1，成功返回 0"


def t_learn_loo_fixed_threshold():
    """留一验证不得在测试标签上搜索最佳阈值。"""
    import learn

    samples = [
        {"name": "A", "X": np.array([[0.0], [0.2], [0.8], [1.0]]),
         "y": np.array([0.0, 0.0, 1.0, 1.0])},
        {"name": "B", "X": np.array([[0.1], [0.3], [0.7], [0.9]]),
         "y": np.array([0.0, 0.0, 1.0, 1.0])},
        {"name": "C", "X": np.array([[0.0], [0.4], [0.6], [1.0]]),
         "y": np.array([0.0, 0.0, 1.0, 1.0])},
    ]
    old = learn.f1_curve
    seen = []
    try:
        def spy(y, scores, threshold):
            seen.append(float(threshold))
            return old(y, scores, threshold)

        learn.f1_curve = spy
        rows = learn.leave_one_out(samples, threshold=0.5)
    finally:
        learn.f1_curve = old
    if len(rows) != 3:
        return False, "应评估 3 个留出样本，实际 %d" % len(rows)
    if not seen or any(abs(x - 0.5) > 1e-9 for x in seen):
        return False, "留一评估使用了测试集调参阈值：%r" % seen
    return True, "3 个留出样本均固定阈值 0.50"


def t_director_does_not_restore_rejected_gap():
    """分段导演不能用长桥接把明确拒绝的内容重新带回成片。"""
    import director

    segs = [
        [0.0, 2.0, "保留开头"],
        [2.6, 4.0, "短停顿后的连续说明"],
        [4.0, 8.5, "明确丢弃的低价值内容"],
        [9.0, 11.0, "后续保留内容"],
    ]
    spans = director._spans_from_keeps(segs, [True, True, False, True])
    if spans != [(0.0, 4.0), (9.0, 11.0)]:
        return False, "长间隙被错误桥接：%r" % (spans,)
    return True, "短停顿合并，5 秒拒绝区间保持删除"


def t_director_line_mapping_and_review_protocol():
    """导演展示编号必须从 1 开始，复查同时支持补回和删除。"""
    import director

    segs = [[0, 1, "甲"], [1, 2, "乙"], [2, 3, "丙"]]
    body = director._numbered_lines(segs, 0, 3, 0, 3)
    if body.splitlines() != ["1. >甲", "2. >乙", "3. >丙"]:
        return False, "行号仍然错位：%r" % body.splitlines()
    adds, drops = director._parse_review("NOT OK\nADD 2\nDROP 3", 3)
    if adds != {1} or drops != {2}:
        return False, "复查协议解析错误：adds=%r drops=%r" % (adds, drops)
    return True, "编号 1~3 正确，NOT OK 的 ADD/DROP 均生效"


def t_director_builds_script_before_segment_plan():
    """岗位必须分离：分析中立；智能剪辑按 编剧→初剪→导演审片 执行。"""
    import inspect
    import index as indexer
    import autopilot
    import cards_ui
    import director
    import script_writer

    analysis_source = inspect.getsource(indexer.index_video)
    if "script_writer" in analysis_source or "director_critic" in analysis_source:
        return False, "分析阶段仍在生成剧情或审片"

    for name, source in (
            ("旧界面", inspect.getsource(cards_ui.AutoCutWorker.run)),):
        script_at = source.find("script_writer.build")
        cut_at = source.find("apply_cut_level")
        audit_at = source.find("director_critic.review_and_rebuild")
        transition_at = source.find("judge_transitions")
        if min(script_at, cut_at, audit_at, transition_at) < 0:
            return False, "%s缺少完整岗位：%s" % (
                name, (script_at, cut_at, audit_at, transition_at))
        if not (script_at < cut_at < audit_at < transition_at):
            return False, "%s顺序错误：编剧%d 初剪%d 审片%d 转场%d" % (
                name, script_at, cut_at, audit_at, transition_at)

    # 新生产链已经拆成独立岗位处理器，不能再靠 run_one 源码里的文字顺序判断。
    # 直接检查阶段图和每个处理器真正调用的业务函数，防止用注释“骗过”自测。
    import workflow_engine
    if workflow_engine.STAGE_ORDER != (
            "analyze", "writer", "editor", "boundary", "director",
            "transition", "renderer", "finalize"):
        return False, "自动任务岗位图顺序错误：%r" % (workflow_engine.STAGE_ORDER,)
    actual_calls = {
        "writer": (autopilot._WorkflowRun.writer, "script_writer.build"),
        "editor": (autopilot._WorkflowRun.editor, "apply_cut_level"),
        "director": (autopilot._WorkflowRun.director,
                     "director_critic.review_and_rebuild"),
        "transition": (autopilot._WorkflowRun.transition, "judge_transitions"),
    }
    for stage_name, (handler, marker) in actual_calls.items():
        if marker not in inspect.getsource(handler):
            return False, "自动任务%s岗位没有调用 %s" % (stage_name, marker)
    run_source = inspect.getsource(autopilot.run_one)
    if "workflow_engine.SerialExecutor" not in run_source:
        return False, "自动任务没有由统一串行工作流执行器驱动"

    if "level" not in inspect.signature(script_writer.build).parameters:
        return False, "编剧接口没有显式力度"
    if "level" not in inspect.signature(director.plan_by_segment).parameters:
        return False, "导演接口没有显式力度"
    if len(set(script_writer.LEVEL_SCRIPT_RULES.values())) != 3:
        return False, "三档剧本策略没有真正分开"
    chunks = [{"idx": 1, "start": 0.0, "end": 60.0,
               "summary": "普通铺垫", "importance": 2}]
    light, _ = script_writer.fallback_sections(chunks, level="light")
    heavy, _ = script_writer.fallback_sections(chunks, level="heavy")
    if light[0]["advice"] == heavy[0]["advice"]:
        return False, "轻度/重度兜底剧本取舍仍相同"
    return True, "分析中立；编剧→初剪→导演审片→转场顺序正确，三档剧本策略分离"


# --------------------------------------------------------------------------
GROUPS = [
    ("区间运算", "interval", [("subtract 去重复段", t_subtract)]),
    ("卡片导出", "export", [("合并成片与临时清理", t_export_merged)]),
    ("卡片预览", "preview", [("双击播放的 0/1/N 音轨", t_card_preview_audio_modes)]),
    ("字幕映射", "subs", [("remap 成片时间轴", t_remap), ("_wrap 超长句", t_wrap),
                       ("out_resolution 分辨率", t_resolution)]),
    ("重复检测", "dedupe", [("造重录：丢差的留好的", t_dedupe_find),
                         ("干净素材不误报", t_dedupe_clean)]),
    ("BGM", "bgm", [("按波形选段", t_bgm_window), ("循环长度与电平", t_bgm_loop),
                    ("固定电平 vs 闪避", t_bgm_mix_duck),
                    ("配乐失败保留原成片", t_bgm_failure_keeps_output)]),
    ("索引", "index", [("索引主流程（不跑 ASR）", t_index_pipeline),
                       ("转写取消传播", t_transcribe_cancel),
                       ("剪辑模型维度一致性", t_score_model_dimensions),
                       ("语义与剧本取消传播", t_llm_stage_cancel)]),
    ("自动任务", "autopilot", [("输出目录与成片校验", t_autopilot_output_dir),
                                ("剪辑模式调用链", t_edit_mode_pipeline)]),
    ("出片", "render", [("取消立刻生效", t_render_cancel),
                      ("任务启动后取消仍生效", t_render_cancel_after_start),
                      ("正常出片+时间轴", t_render_ok),
                      ("剪辑力度三档真的不同", t_cut_levels),
                      ("界面剪辑链路不崩", t_gui_cut_chain),
                      ("退化索引下三档仍不同", t_levels_degraded_index),
                      ("低分间隙不会被并回", t_script_selection_does_not_restore_rejected_gap),
                      ("术语纠错（整合包词典）", t_term_correct),
                      ("白录规律检测", t_retake_pattern),
                      ("BGM 分段与下压", t_bgm_sections),
                      ("游戏实况体裁模式", t_genre_gameplay),
                      ("重度保留章节故事链", t_heavy_preserves_chapter_story_chain),
                       ("导演返工补齐故事链", t_director_critic_repairs_story_chain),
                      ("游戏模式动态软预算", t_gameplay_budget_is_dynamic),
                      ("剪辑力度预算日志", t_cut_level_budget_logs),
                      ("短素材重度体裁入口", t_genre_short_heavy_entrypoint),
                      ("三档必须不同", t_levels_distinct)]),
    ("界面", "ui", [("GUI 单实例守卫", t_single_instance_guard),
                    ("后台进程彻底无窗口", t_child_processes_are_fully_detached),
                    ("异常小窗运行期拦截", t_popup_window_guard_rules),
                     ("队列行不创建顶级小窗", t_queue_rows_are_not_top_level_windows),
                    ("旧版主窗口/设置/保存", t_ui),
                    ("新版设置读取 BGM 键", t_ui_v2_settings_bgm),
                     ("队列识别模型透传", t_queue_model_wiring),
                     ("队列分析/自动出片开关", t_queue_start_defaults_to_analysis_and_switches_to_auto),
                    ("默认新版界面回归", t_ui_v2)]),
    ("训练", "learn", [("留一验证固定阈值", t_learn_loo_fixed_threshold),
                         ("分段导演不复活拒绝区间", t_director_does_not_restore_rejected_gap),
                         ("导演行号与复查协议", t_director_line_mapping_and_review_protocol),
                         ("先建剧本再分段导演", t_director_builds_script_before_segment_plan)]),
    ("安装", "installer", [("体检退出码", t_installer_check_exit)]),
]


def _cleanup_test_process():
    """显式收掉本地模型子进程和 Qt 对象，保证自动化退出码稳定。"""
    try:
        import proofread
        proofread._shutdown_shared()
    except Exception:  # noqa: BLE001
        pass
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for widget in list(app.topLevelWidgets()):
                try:
                    widget.close()
                    widget.deleteLater()
                except Exception:  # noqa: BLE001
                    pass
            app.processEvents()
            app.quit()
            app.processEvents()
    except Exception:  # noqa: BLE001
        pass
    try:
        import gc
        gc.collect()
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    only = (argv[0] if argv else "").lower()
    print("=" * 66)
    print("  自动剪辑功能自测")
    print("=" * 66)
    for gname, alias, tests in GROUPS:
        if only and only not in gname.lower() and only not in alias \
                and not any(only in n.lower() for n, _ in tests):
            continue
        print("\n【%s】" % gname)
        for name, fn in tests:
            check(name, fn)
    bad = [r for r in RESULT if not r[1]]
    print("\n" + "=" * 66)
    print("  共 %d 项，通过 %d，失败 %d"
          % (len(RESULT), len(RESULT) - len(bad), len(bad)))
    if bad:
        print("  失败项：")
        for name, _ok, detail in bad:
            print("   - %s：%s" % (name, detail))
    print("=" * 66)
    _cleanup_test_process()
    return len(bad)


if __name__ == "__main__":
    sys.exit(main())

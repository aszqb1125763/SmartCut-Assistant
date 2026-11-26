# -*- coding: utf-8 -*-
"""只做转写 → 输出 SRT 字幕（不跑完整索引，省掉画面采样）。

用途：PR 里那条 46 分钟素材没有索引，模型看不到台词；
先用这个脚本把音频转成字幕，导入 PR 或者喂给模型判断内容。

用法：
    python make_srt.py <视频文件> [输出.srt]
"""
from __future__ import annotations

import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def _setup_cuda_dlls():
    """把 venv 里 nvidia 的 DLL 加进 PATH（ctranslate2 需要 cublas）。"""
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


def _srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        millis = 999
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def extract_audio(src: str, out_wav: str, track: int = 0,
                  start: float = 0.0, end: float = 0.0) -> str:
    """提取音频；给了 start/end 就只取那一段（对应序列引用的素材区间）。"""
    ffmpeg = os.path.join(BASE, "bin", "ffmpeg.exe")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", "%.3f" % start]
    if end and end > start:
        cmd += ["-t", "%.3f" % (end - start)]
    cmd += ["-i", src, "-map", "0:a:%d" % track, "-vn", "-ac", "1",
            "-ar", "16000", "-c:a", "pcm_s16le", out_wav]
    subprocess.run(cmd, check=True)
    return out_wav



def _align_to_voice(rows, audio, sr=16000, thresh_db=-45.0, pad=0.4):
    """把每条字幕的边界对齐到真正有声的位置（10ms 能量包络）。"""
    import numpy as np
    if not rows or len(audio) < sr:
        return rows
    hop = sr // 100
    frames = len(audio) // hop
    if frames <= 0:
        return rows
    rms = np.sqrt(np.mean(audio[:frames * hop].reshape(frames, hop) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    voiced = db > thresh_db
    for row in rows:
        lo = max(0, int((row["a"] - pad) * 100))
        hi = min(frames, int((row["b"] + pad) * 100))
        if hi <= lo:
            continue
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
            row["a"] = round(start / 100.0, 2)
            row["b"] = round((end + 1) / 100.0, 2)
    for i in range(len(rows) - 1):
        if rows[i]["b"] > rows[i + 1]["a"]:
            rows[i]["b"] = rows[i + 1]["a"]
    for row in rows:
        if row["b"] <= row["a"]:
            row["b"] = round(row["a"] + 0.3, 2)
    return rows
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    out_srt = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + ".srt"
    start = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    end = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    if not os.path.isfile(src):
        print("素材不存在：", src)
        return 1

    wav = os.path.join(BASE, "工作目录", "_asr_tmp.wav")
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    print("抽取音频…%s" % ("（只取 %.2f~%.2f 秒这段）" % (start, end)
                         if end > start else ""))
    extract_audio(src, wav, 0, start, end)
    print("音频：%.1f MB" % (os.path.getsize(wav) / 1048576))

    from faster_whisper import WhisperModel
    _setup_cuda_dlls()
    # 自己读 WAV 成 numpy：faster_whisper 自带的解码走 PyAV，
    # 而这台机器的 av 版本不支持 metadata_errors 参数，会直接报 TypeError。
    import wave
    import numpy as np
    with wave.open(wav, "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        audio = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    print("音频样本：%.1f 秒" % (len(audio) / 16000.0))

    models_dir = os.path.join(BASE, "models")
    model_path = os.path.join(models_dir, "large-v3-full")
    if not os.path.isdir(model_path):
        model_path = "large-v3"
    print("加载模型：%s（首次约 40 秒）…" % model_path)
    model = WhisperModel(model_path, device="cuda", compute_type="float16")

    print("开始转写…")
    segments, info = model.transcribe(
        audio, language="zh", vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        beam_size=5)
    print("语言：%s  概率：%.2f" % (info.language, info.language_probability))

    rows = []
    from subtitles import clean_caption_text
    for seg in segments:
        text = clean_caption_text(seg.text)
        if not text:
            continue
        rows.append({"a": round(float(seg.start), 2), "b": round(float(seg.end), 2),
                     "text": text})
    rows = _align_to_voice(rows, audio)
    lines = []
    count = 0
    for row in rows:
        count += 1
        lines.append("%d" % count)
        lines.append("%s --> %s" % (_srt_time(row["a"]), _srt_time(row["b"])))
        lines.append(row["text"])
        lines.append("")

    with open(out_srt, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print("\n完成：%d 句 → %s" % (count, out_srt))
    print("文件大小：%.1f KB" % (os.path.getsize(out_srt) / 1024))
    try:
        os.remove(wav)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

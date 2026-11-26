# -*- coding: utf-8 -*-
"""卡片缩略图：按需抓帧 + 磁盘缓存。

为什么按需：5000 张卡片全抓要几十分钟；用户只看当前一屏（120 张），
所以先抓可见页，滚动到哪抓到哪，抓过的进缓存（工作目录\\缩略图\\<索引名>）。

实现：ffmpeg -ss <t> -frames:v 1，160×90，硬件解码（失败自动回退软解）。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QThread, Signal

CACHE_NAME = "缩略图"


def cache_dir(index_dir: str) -> str:
    stem = os.path.basename(os.path.normpath(index_dir or "默认"))
    try:
        import index as ix
        root = os.path.join(ix.work_root(), CACHE_NAME)
    except Exception:
        root = os.path.join(os.path.dirname(index_dir or "."), CACHE_NAME)
    d = os.path.join(root, stem)
    os.makedirs(d, exist_ok=True)
    return d


def thumb_path(index_dir: str, cid) -> str:
    return os.path.join(cache_dir(index_dir), "%s.jpg" % cid)


def _ffmpeg() -> str:
    try:
        import export as ex
        return ex.find_ffmpeg()
    except Exception:
        return "ffmpeg"


def extract(src: str, t: float, out: str, hw: bool = False,
            frame_cache=None) -> bool:
    """抓一帧存 jpg。成功返回 True。"""
    if os.path.isfile(out) and os.path.getsize(out) > 500:
        return True
    try:
        if frame_cache is None:
            import video_frame_cache
            frame_cache = video_frame_cache.VideoFrameCache(src)
        cached, _evidence = frame_cache.frame_path_at(t)
        if cached and os.path.getsize(cached) > 128:
            # 384-wide ingest proxy is already small; downscale to the card's
            # 160px cache without reopening/decoding the original recording.
            import cv2
            import numpy as np
            buf = np.fromfile(cached, dtype=np.uint8)
            image = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
            if image is not None:
                height = max(2, int(image.shape[0] * 160 / image.shape[1]))
                image = cv2.resize(image, (160, height),
                                   interpolation=cv2.INTER_AREA)
                ok, encoded = cv2.imencode(
                    ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if ok:
                    os.makedirs(os.path.dirname(os.path.abspath(out)),
                                exist_ok=True)
                    temporary = out + ".%d.part.jpg" % os.getpid()
                    encoded.tofile(temporary)
                    os.replace(temporary, out)
                    return True
    except Exception as exc:  # noqa: BLE001
        try:
            import video_frame_cache
            if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                raise
        except ImportError:
            pass
    ff = _ffmpeg()
    base = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    cmds = []
    if hw:
        cmds.append(base + ["-hwaccel", "cuda", "-ss", "%.3f" % max(0.0, t),
                            "-i", src, "-frames:v", "1",
                            "-vf", "scale=160:-2", "-q:v", "4", out])
    cmds.append(base + ["-ss", "%.3f" % max(0.0, t), "-i", src, "-frames:v", "1",
                        "-vf", "scale=160:-2", "-q:v", "4", out])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=25)
            if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 500:
                return True
        except Exception:
            continue
    return False


class ThumbWorker(QThread):
    """后台抓帧：一次处理一批卡片，抓到一张发一张。"""

    got = Signal(str, str)      # cid, 图片路径

    def __init__(self, src: str, index_dir: str, tasks, parent=None):
        super().__init__(parent)
        self.src = src
        self.index_dir = index_dir
        self.tasks = tasks           # 共享列表：[(cid, t)]，跑的过程中可以继续加
        self._stop = False
        self._idle_rounds = 0
        self._idle = 0
        self._frame_cache = None
        try:
            import video_frame_cache
            self._frame_cache = video_frame_cache.VideoFrameCache(
                src, should_cancel=lambda: self._stop)
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        self._stop = True

    def run(self):
        """串行抓帧：源文件多在机械盘，多线程随机读会更慢（寻道），所以单线程顺序抓。"""
        while not self._stop:
            if not self.tasks:
                self.msleep(150)
                self._idle += 1
                if self._idle > 20:      # 约 3 秒没有新任务就退出
                    return
                continue
            self._idle = 0
            try:
                cid, tv = self.tasks.pop(0)
            except IndexError:
                continue
            self._one(cid, tv)

    def _one(self, cid, tv):
        if self._stop:
            return
        out = thumb_path(self.index_dir, cid)
        try:
            if extract(self.src, float(tv), out,
                       frame_cache=self._frame_cache):
                self.got.emit(str(cid), out)
        except Exception:
            pass

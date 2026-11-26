# -*- coding: utf-8 -*-
"""PyAV 兼容补丁。

背景：faster-whisper 1.2.1 里写的是
    av.open(path, mode="r", metadata_errors="ignore")
但 PyAV 从 15.0 起把这个参数删掉了，于是在不带这个参数的版本上会直接抛
    TypeError: open() got an unexpected keyword argument 'metadata_errors'

这里在导入时把 av.open 包一层：只有当前 PyAV 不支持这个参数时才静默丢掉它。
不修改 site-packages，删掉本文件即可完全还原。
"""
import av

_INSTALLED = False


def _patch():
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        major = int(str(av.__version__).split(".")[0])
    except (ValueError, AttributeError):
        major = 0

    # 15 之前都还认 metadata_errors，不必动
    if major and major < 15:
        _INSTALLED = True
        return

    _orig_open = av.open

    def open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_open(*args, **kwargs)

    open_compat.__name__ = getattr(_orig_open, "__name__", "open")
    open_compat.__doc__ = getattr(_orig_open, "__doc__", None)
    av.open = open_compat
    _INSTALLED = True


_patch()

# -*- coding: utf-8 -*-
"""本地大模型档位：软件里直接下载、切换，用户只需选择一个。

为什么要分档：4B 快但理解一般，8B 明显更准但要 4.7GB 显存和硬盘。
不同机器、不同耐心的人需求不一样，所以让用户自己挑；选定后所有本地
字幕、分析、编剧、剪辑和导演任务都统一使用这个模型。
"""
import json
import hashlib
import os
import time
import urllib.request
from memory_config import app_home


class Cancelled(Exception):
    """用户主动取消：要立刻停下，不能当成“这个源坏了”去换源。"""
    pass

BASE = app_home()
LLM_DIR = os.path.join(BASE, "models", "llm")
# 下载源，按实测速度排序：魔搭最快（约 11MB/s），HF 官方次之（5MB/s），
# hf-mirror 时好时坏（实测出现过 0 字节），放最后兜底。
SOURCES = [
    ("modelscope", "https://modelscope.cn/models/%s/resolve/master/%s"),
    ("huggingface", "https://huggingface.co/%s/resolve/main/%s"),
    ("hf-mirror", "https://hf-mirror.com/%s/resolve/main/%s"),
]

# 档位 → 说明、文件名、仓库路径、大致体积
CATALOG = [
    {
        "tier": "fast",
        "name": "快速档 · Qwen3-4B",
        "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "repo": "lmstudio-community/Qwen3-4B-Instruct-2507-GGUF",
        # ModelScope 没有这个精确文件；空值会跳过该源，
        # 避免将不同权重的字节续到同一个 .part 中。
        "ms": "",
        "size": 2.5,
        "bytes": 2497280448,
        "sha256": "8cdb57cbb880d313736a9bc4e3d3d2485f145b5e19cf33783746e753e82641fc",
        "vram": 2.6,
        "speed": "254 句约 13 秒",
        "measured": True,
        "note": "全流程可用、速度最快；理解力一般，复杂话题切换可能漏判。",
    },
    {
        "tier": "large",
        "name": "大理解档 · Qwen3-14B",
        "file": "Qwen3-14B-Q4_K_M.gguf",
        "repo": "Qwen/Qwen3-14B-GGUF",
        "ms": "Qwen/Qwen3-14B-GGUF",
        "size": 9.3,
        "bytes": 9001752960,
        "sha256": "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
        "vram": 10.2,
        "speed": "254 句约 60~90 秒",
        "measured": False,
        "note": "理解力最强档，12G 显存刚好够；追字幕/完整度判定的用这个。",
    },
    {
        "tier": "smart",
        "name": "均衡档 · Qwen3-8B",
        "file": "Qwen3-8B-Q4_K_M.gguf",
        "repo": "Qwen/Qwen3-8B-GGUF",
        "ms": "Qwen/Qwen3-8B-GGUF",
        "size": 4.9,
        "bytes": 5027783488,
        "sha256": "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
        "vram": 5.9,
        "speed": "254 句约 22 秒",
        "measured": True,
        "note": "推荐档。全流程理解力明显更好，比快档慢约 1.7 倍。",
    },
]


def info(tier):
    for c in CATALOG:
        if c["tier"] == tier:
            return c
    return None


def path_of(tier):
    c = info(tier)
    return os.path.join(LLM_DIR, c["file"]) if c else ""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def downloaded(tier, verify_hash=False):
    """判断是否真的下完整了。

    以前只看“大于 10MB”，结果下到 60MB 就断了也被当成完成——
    界面上两个档位都显示“已下载”，玩家以为都好了，实际 8B 根本跑不起来。
    现在按目录里的期望体积判断（留 10% 余量容忍不同量化版本）。
    """
    c = info(tier)
    p = path_of(tier)
    if not (c and p) or not os.path.isfile(p):
        return False
    expect = c.get("bytes") or 0
    size = os.path.getsize(p)
    expected_hash = str(c.get("sha256") or "").lower()
    if expected_hash:
        if size != int(expect):
            return False
        if not verify_hash:
            return True
        stamp = p + ".sha256"
        try:
            if open(stamp, encoding="ascii").read().strip().lower() == expected_hash:
                return True
        except OSError:
            pass
        actual = _sha256(p).lower()
        if actual != expected_hash:
            return False
        try:
            with open(stamp, "w", encoding="ascii", newline="\n") as handle:
                handle.write(expected_hash + "\n")
        except OSError:
            pass
        return True
    if expect:
        return size >= expect * 0.90
    return size > 100 * 1024 * 1024


def size_gb(tier):
    p = path_of(tier)
    try:
        return os.path.getsize(p) / (1024 ** 3)
    except OSError:
        return 0.0


def download(tier, on_progress=None, should_stop=None):
    """下载模型，支持断点续传（网断了再点一次能接着下）。

    on_progress(已下 MB, 总 MB) 用来画进度条。
    """
    c = info(tier)
    if not c:
        raise ValueError("未知档位：%s" % tier)
    os.makedirs(LLM_DIR, exist_ok=True)
    dst = path_of(tier)
    part = dst + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    last_err = None
    srcfile = part + ".src"
    for _name, _tpl in SOURCES:
        repo = c.get("ms") if _name == "modelscope" else c["repo"]
        if not repo:
            continue
        url = _tpl % (repo, c["file"])
        # 续传必须同一个源：不同源的字节拼在一起会得到坏文件
        # （实测这么干过一次，8B 模型加载后只会输出 GGGG）
        try:
            _prev = open(srcfile, encoding="utf-8").read().strip() if os.path.isfile(srcfile) else ""
        except OSError:
            _prev = ""
        if have and _prev and _prev != _name:
            try:
                os.remove(part)
            except OSError:
                pass
            have = 0
        try:
            with open(srcfile, "w", encoding="utf-8") as fh:
                fh.write(_name)
        except OSError:
            pass
        try:
            out = _fetch(url, part, have, c, on_progress, should_stop, dst)
            try:
                os.remove(srcfile)
            except OSError:
                pass
            return out
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = "%s: %s" % (_name, str(exc)[:90])
            if on_progress:
                on_progress(0, 0)          # 重置进度显示
            continue
    raise RuntimeError("所有下载源都失败了：%s" % last_err)


def _fetch(url, part, have, c, on_progress, should_stop, dst):
    """从一个源下载到 .part，成功后改名。"""
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    with urllib.request.urlopen(req, timeout=60) as r:
        content_length = int(r.headers.get("Content-Length") or 0)
        mode = "ab" if have and r.status == 206 else "wb"
        if mode == "wb":
            have = 0
        total = content_length + have
        done = have
        with open(part, mode) as fh:
            last = 0.0
            while True:
                if should_stop and should_stop():
                    raise Cancelled("已取消")
                buf = r.read(1024 * 512)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                if on_progress and time.time() - last > 0.4:
                    last = time.time()
                    on_progress(done / 1048576.0, total / 1048576.0)
    got = os.path.getsize(part)
    expect = c.get("bytes") or 0
    # 服务器给了总长就以它为准；没给就按目录里的期望值核对
    if total and got < total * 0.995:
        raise RuntimeError("下载中断（%.2f/%.2f GB），再点一次会接着下"
                           % (got / 1024 ** 3, total / 1024 ** 3))
    expected_hash = str(c.get("sha256") or "").lower()
    if expected_hash and got != int(expect):
        raise RuntimeError("模型大小不一致（收到 %d，固定版本应为 %d）" %
                           (got, int(expect)))
    if expect and not expected_hash and got < expect * 0.90:
        raise RuntimeError("文件不完整（%.2f GB），再点一次会接着下"
                           % (got / 1024 ** 3))
    if expected_hash:
        actual_hash = _sha256(part).lower()
        if actual_hash != expected_hash:
            raise RuntimeError("模型 SHA256 不匹配；拒绝使用可能已变更或损坏的文件")
    os.replace(part, dst)
    if expected_hash:
        try:
            with open(dst + ".sha256", "w", encoding="ascii", newline="\n") as handle:
                handle.write(expected_hash + "\n")
        except OSError:
            pass
    if on_progress:
        on_progress(os.path.getsize(dst) / 1048576.0,
                    os.path.getsize(dst) / 1048576.0)
    return dst


def repair(tier):
    """把“名字是正式文件、其实没下完”的残留降级成 .part，好继续续传。"""
    c = info(tier)
    p = path_of(tier)
    if not (c and p and os.path.isfile(p)):
        return False
    expect = c.get("bytes") or 0
    if expect and os.path.getsize(p) < expect * 0.90:
        part = p + ".part"
        try:
            if os.path.isfile(part):
                os.remove(p)
            else:
                os.replace(p, part)
            return True
        except OSError:
            return False
    return False


def delete(tier):
    p = path_of(tier)
    for f in (p, p + ".part", p + ".sha256"):
        if f and os.path.isfile(f):
            os.remove(f)

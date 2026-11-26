# -*- coding: utf-8 -*-
"""准备本地模型环境：llama.cpp 运行库 + 用户选择的一套 GGUF。

下载到工具目录下，不写系统目录：
    bin/llama/      llama-cli.exe / llama-server.exe 等
    models/llm/     当前选择的 Qwen3 4B / 8B / 14B（只需一套）
"""
import os
import hashlib
import posixpath
import sys
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
LLAMA_DIR = os.path.join(BASE, "bin", "llama")
LLM_DIR = os.path.join(BASE, "models", "llm")
TAG = "b12559"
GH = "https://github.com/ggml-org/llama.cpp/releases/download/%s/" % TAG

ZIPS = [
    (
        "llama-b12559-bin-win-cuda-13.5-x64.zip", "CUDA 13.5 主程序",
        225467224, "b870d0e53f43513af440ac25dc395c2fe5be466f3ebbafc60038d42c812f7335",
    ),
    (
        "cudart-llama-bin-win-cuda-13.5-x64.zip", "CUDA 13.5 运行库",
        426314199, "c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba",
    ),
    (
        "llama-b12559-bin-win-cpu-x64.zip", "CPU 兜底版",
        24876565, "2f3dcb3aea4f52bf734f52040d2a688bdd76c2f762ae200a159646b37d84e393",
    ),
]


def human(n):
    return "%.0f MB" % (n / 1048576) if n < 1073741824 else "%.2f GB" % (n / 1073741824)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(path, expected_size, expected_sha256):
    if not os.path.isfile(path):
        return False
    if os.path.getsize(path) != int(expected_size):
        return False
    return _sha256(path).lower() == str(expected_sha256).lower()


class IntegrityError(RuntimeError):
    pass


def fetch(url, dest, expected_size, expected_sha256, retries=6):
    part = dest + ".part"
    retried_after_integrity_failure = False
    for attempt in range(1, retries + 1):
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Range": "bytes=%d-" % have})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content_length = int(resp.headers.get("Content-Length") or 0)
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                total = have + content_length
                got = have
                last = 0.0
                t0 = time.time()
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last >= 2.0:
                            last = now
                            sys.stdout.write("\r      %5.1f%%  %s / %s  %.1f MB/s   "
                                             % (got / total * 100 if total else 0,
                                                human(got), human(total),
                                                (got - have) / max(0.1, now - t0) / 1048576))
                            sys.stdout.flush()
                sys.stdout.write("\n")
            if total and got < total:
                raise IOError("只下到 %s / %s" % (human(got), human(total)))
            if got != int(expected_size):
                raise IntegrityError("下载大小不匹配")
            actual = _sha256(part).lower()
            if actual != str(expected_sha256).lower():
                raise IntegrityError("下载 SHA256 不匹配")
            os.replace(part, dest)
            return True
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write("\n      第 %d 次失败：%s\n" % (attempt, str(exc)[:100]))
            if isinstance(exc, IntegrityError):
                try:
                    os.remove(part)
                except OSError:
                    pass
                # 旧版本留下的 .part 可能与当前固定资产不同。
                # 清空后只允许一次从零重试，避免对不可信内容循环下载。
                if not retried_after_integrity_failure and attempt < retries:
                    retried_after_integrity_failure = True
                    continue
                return False
            if attempt == retries:
                return False
            time.sleep(min(10, 2 * attempt))
    return False


def safe_extract(zf, destination):
    """解压已验签 ZIP，同时拒绝 Zip Slip 与符号链接成员。"""
    root = os.path.abspath(destination)
    for info in zf.infolist():
        raw = info.filename.replace("\\", "/")
        normalized = posixpath.normpath(raw)
        first = normalized.split("/", 1)[0]
        if (normalized in ("", ".") or normalized.startswith("/")
                or normalized == ".." or normalized.startswith("../")
                or ":" in first):
            raise RuntimeError("ZIP 含不安全路径：%s" % info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise RuntimeError("ZIP 含不允许的符号链接：%s" % info.filename)
        target = os.path.abspath(os.path.join(root, *normalized.split("/")))
        if os.path.commonpath((root, target)) != root:
            raise RuntimeError("ZIP 成员越界：%s" % info.filename)
    zf.extractall(root)


def main(tier=None):
    os.makedirs(LLAMA_DIR, exist_ok=True)
    os.makedirs(LLM_DIR, exist_ok=True)

    print("=== 1/2 llama.cpp 运行库 → %s" % LLAMA_DIR)
    for name, label, expected_size, expected_sha256 in ZIPS:
        dest = os.path.join(LLAMA_DIR, name)
        if verified(dest, expected_size, expected_sha256):
            print("  已有 %s（%s）" % (name, label))
        else:
            if os.path.isfile(dest):
                print("  !! 已有文件校验失败，将重新下载：%s" % name)
                try:
                    os.remove(dest)
                except OSError as exc:
                    print("  !! 无法移除损坏文件：%s" % exc)
                    continue
            print("  下载 %s（%s）" % (name, label))
            if not fetch(GH + name, dest, expected_size, expected_sha256):
                print("  !! %s 下载失败，跳过" % name)
                continue
        if not verified(dest, expected_size, expected_sha256):
            print("  !! %s 完整性校验失败，拒绝解压" % name)
            continue
        print("  已通过固定 SHA256 校验。")
        print("  解压 …")
        try:
            with zipfile.ZipFile(dest, "r") as zf:
                safe_extract(zf, LLAMA_DIR)
        except Exception as exc:  # noqa: BLE001
            print("  !! 解压失败：%s" % exc)

    found = []
    for root, _d, files in os.walk(LLAMA_DIR):
        for f in files:
            if f.lower().startswith("llama-") and f.lower().endswith(".exe"):
                found.append(os.path.relpath(os.path.join(root, f), LLAMA_DIR))
    print("  可用程序：%s" % ", ".join(sorted(set(found))[:8]))

    import llm_config
    import llm_models
    tier = str(tier or llm_config.active_tier()).strip().lower()
    item = llm_models.info(tier)
    if not item:
        print("  !! 未知模型档位：%s（可选 fast / smart / large）" % tier)
        return 1
    print("\n=== 2/2 语言模型 → %s" % LLM_DIR)
    target = llm_models.path_of(tier)
    if llm_models.downloaded(tier, verify_hash=True):
        print("  已有 %s（%s）" % (item["file"], human(os.path.getsize(target))))
    else:
        print("  下载 %s" % item["name"])
        try:
            llm_models.download(
                tier,
                on_progress=lambda done, total: sys.stdout.write(
                    "\r      %5.1f%%  %.0f MB / %.0f MB   "
                    % ((done / total * 100) if total else 0, done, total)))
            sys.stdout.write("\n")
        except Exception as exc:  # noqa: BLE001
            print("  !! 模型下载失败：%s" % str(exc)[:120])
            return 1
        if not llm_models.downloaded(tier, verify_hash=True):
            print("  !! 模型完整性校验失败，未启用")
            return 1
        print("  完成并通过固定版本校验：%s" % human(os.path.getsize(target)))
    llm_config.select_model(tier, item["file"])
    print("  已设为全流程唯一模型：%s" % item["name"])

    print("\n全部就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))

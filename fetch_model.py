# -*- coding: utf-8 -*-
"""稳健的模型下载器：直连镜像 + Range 断点续传 + 进度上报。

huggingface_hub 的 snapshot_download 在本机上下 1.5 GB 的 turbo 正常，
但下 3 GB 的 large-v3 时会卡在 0%（传了几十秒就不动了）。
而直接对同一个 URL 发 Range 请求能跑到 3.5 MB/s，说明是客户端问题，
所以这里自己实现一遍。
"""
import os
import sys
import time
import hashlib
import urllib.request

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

BASE = os.path.dirname(os.path.abspath(__file__))
MIRROR = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
FILES = ["config.json", "preprocessor_config.json", "tokenizer.json",
         "vocabulary.json", "model.bin"]
MODEL_MANIFEST = {
    "large-v3": {
        "config.json": (2394, "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9"),
        "model.bin": (3087284237, "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
        "preprocessor_config.json": (340, "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
        "tokenizer.json": (2480617, "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca"),
        "vocabulary.json": (1068114, "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
    },
    "large-v3-turbo": {
        "config.json": (2263, "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e"),
        "model.bin": (1617884929, "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
        "preprocessor_config.json": (340, "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
        "tokenizer.json": (2710337, "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd"),
        "vocabulary.json": (1068114, "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
    },
}
REPOS = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def human(n):
    return "%.1f MB" % (n / 1048576.0) if n < 1073741824 else "%.2f GB" % (n / 1073741824.0)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model_dir(model_size, base=None, verify_hash=True):
    """Validate the exact ASR payload tested for this SmartCut release."""
    manifest = MODEL_MANIFEST.get(model_size) or {}
    folder = os.path.join(base or BASE, "models", "%s-full" % model_size)
    if not manifest:
        return os.path.isfile(os.path.join(folder, "model.bin"))
    for name, (expected_size, expected_hash) in manifest.items():
        path = os.path.join(folder, name)
        try:
            if os.path.getsize(path) != int(expected_size):
                return False
        except OSError:
            return False
        if verify_hash and _sha256(path).lower() != expected_hash:
            return False
    return True


def fetch(url, dest, retries=8, timeout=60):
    """断点续传下载单个文件，失败自动重试。"""
    part = dest + ".part"
    for attempt in range(1, retries + 1):
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Range": "bytes=%d-" % have,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = have + int(resp.headers.get("Content-Length") or 0)
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
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
                        if now - last >= 1.5:
                            last = now
                            speed = (got - have) / max(0.1, now - t0) / 1048576
                            pct = (got / total * 100) if total else 0
                            sys.stdout.write("\r    %s  %5.1f%%  %s / %s   %.1f MB/s   "
                                             % (os.path.basename(dest), pct,
                                                human(got), human(total), speed))
                            sys.stdout.flush()
                sys.stdout.write("\n")
                sys.stdout.flush()
            if total and got < total:
                raise IOError("只下到 %s / %s" % (human(got), human(total)))
            os.replace(part, dest)
            return True
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write("\n    第 %d 次失败：%s\n" % (attempt, str(exc)[:120]))
            sys.stdout.flush()
            if attempt == retries:
                return False
            time.sleep(min(10, 2 * attempt))
    return False


def main(model_size, base):
    repo = REPOS.get(model_size)
    if not repo:
        print("不认识的模型：", model_size)
        return 2
    dest_dir = os.path.join(base, "models", "%s-full" % model_size)
    os.makedirs(dest_dir, exist_ok=True)
    print("模型 %s  <-  %s" % (model_size, repo))
    print("目标目录：%s\n" % dest_dir)

    ok = True
    for name in FILES:
        dest = os.path.join(dest_dir, name)
        expected = (MODEL_MANIFEST.get(model_size) or {}).get(name)
        valid_existing = False
        if expected and os.path.isfile(dest) and os.path.getsize(dest) == expected[0]:
            valid_existing = _sha256(dest).lower() == expected[1]
        if valid_existing:
            print("  [跳过] %s（固定版本校验通过）" % name)
            continue
        url = "%s/%s/resolve/main/%s" % (MIRROR, repo, name)
        print("  [下载] %s" % name)
        if not fetch(url, dest):
            ok = False
            break

    size = sum(os.path.getsize(os.path.join(dest_dir, f))
               for f in os.listdir(dest_dir) if os.path.isfile(os.path.join(dest_dir, f)))
    print("\n完成，目录共 %s" % human(size))
    if not verify_model_dir(model_size, base=base, verify_hash=True):
        print("固定版本完整性校验失败：文件缺失、损坏或上游内容已经变化")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    ms = sys.argv[1] if len(sys.argv) > 1 else "large-v3"
    bs = os.path.dirname(os.path.abspath(__file__))
    sys.exit(main(ms, bs))

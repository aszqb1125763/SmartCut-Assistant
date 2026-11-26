# -*- coding: utf-8 -*-
r"""预下载 faster-whisper 模型到 models\<size>-full\。

和 index.py 的 _resolve_model_path 认的目录一致，所以下完之后程序不会再联网。
走 hf-mirror 镜像，支持断点续传。
"""
import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

BASE = os.path.dirname(os.path.abspath(__file__))
PATTERNS = [
    "config.json",
    "tokenizer.json",
    "vocabulary.json",
    "preprocessor_config.json",
    "model.bin",
]
FALLBACK_REPO = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def repo_for(size):
    """优先问 faster-whisper 自己认哪个仓库，免得手写映射过时。"""
    try:
        from faster_whisper.utils import _MODELS  # type: ignore
        repo = _MODELS.get(size)
        if repo:
            return repo
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_REPO.get(size)


def main(sizes):
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("缺少 huggingface_hub，先跑一次安装器。", flush=True)
        return 1

    failed = 0
    for size in sizes:
        dest = os.path.join(BASE, "models", "%s-full" % size)
        import fetch_model
        if fetch_model.verify_model_dir(size, base=BASE, verify_hash=True):
            print("[跳过] %s 已存在：%s" % (size, dest), flush=True)
            continue
        repo = repo_for(size)
        if not repo:
            print("[失败] 不认识这个模型名：%s" % size, flush=True)
            failed += 1
            continue
        print("[下载] %s  <-  %s" % (size, repo), flush=True)
        try:
            snapshot_download(repo_id=repo, local_dir=dest, allow_patterns=PATTERNS)
        except Exception as exc:  # noqa: BLE001
            print("[失败] %s：%s" % (size, exc), flush=True)
            failed += 1
            continue
        if fetch_model.verify_model_dir(size, base=BASE, verify_hash=True):
            print("[完成] %s -> %s" % (size, dest), flush=True)
        else:
            print("[失败] %s 下载后未通过固定版本完整性校验" % size, flush=True)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["large-v3-turbo"]))

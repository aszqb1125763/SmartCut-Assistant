# -*- coding: utf-8 -*-
"""SmartCut 智能剪辑助手 · 一站式安装器

一次运行完成：准备运行环境 -> 下 ffmpeg -> 下语音模型和用户选中的唯一
本地语言模型 -> 自动安装 Premiere 插件 -> 建桌面快捷方式 -> 启动程序。

设计原则：**每一步都先看产物在不在，在就跳过**。所以中途断了、装失败了，
再双击一次会从没做完的那步继续，不会重头再来。

用法（一般通过 setup_windows.ps1 运行）：
    python installer.py            完整安装并启动
    python installer.py --no-launch  装完不启动（排查用）
    python installer.py --no-model   不下载语音模型（模型已有或想跳过）
    python installer.py --accurate   强制准备 large-v3 精确模型（约 3 GB）
    python installer.py --no-llm     暂不下载当前选择的本地语言模型
    python installer.py --no-premiere-plugin  暂不安装 Premiere 插件
    python installer.py --check      只体检，不做任何改动
"""
import os
import sys
import time
import glob
import json
import shutil
import zipfile
import hashlib
import subprocess
import urllib.error
import urllib.request

from product_info import PRODUCT_NAME, VERSION, credits_plain_text

BASE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(BASE, ".venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe")
VENV_PYW = os.path.join(VENV, "Scripts", "pythonw.exe")
BIN_DIR = os.path.join(BASE, "bin")
MODEL_DIR = os.path.join(BASE, "models")
LOG_DIR = os.path.join(BASE, "logs")
REQ_FILE = os.path.join(BASE, "requirements.txt")
CONSTRAINT_FILE = os.path.join(BASE, "constraints-runtime.txt")
STAMP = os.path.join(BASE, ".installed")
DEPS_STAMP = os.path.join(VENV, ".smartcut-requirements.sha256")
STARTER = os.path.join(BASE, "启动SmartCut.bat")
FROZEN_APP = bool(getattr(sys, "frozen", False) and os.path.isfile(sys.executable))

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
MIRROR_BACKUP = "https://mirrors.aliyun.com/pypi/simple"

# 固定到已核验的 BtbN 自动构建。不使用 latest/gyan 滚动地址：
# 那些地址的内容会原地变化，无法保证新电脑安装到同一份已验证程序。
FFMPEG_SOURCES = [
    (
        "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/"
        "autobuild-2026-11-25-12-41/"
        "ffmpeg-N-129288-gc81e35cf2b-win64-gpl.zip",
        "GitHub BtbN 固定构建",
        205524259,
        "ee17bc29170b6f1d6685985260900c57c92967aaaabaf6f2bc8c65b67d76b8c5",
    ),
]

# 3.10 以上都能跑；优先 3.12，实在装不上依赖才回退
GOOD_PY = (3, 10)
PREFER_PY = "3.12"

_log_file = None
NO_GPU = False          # --no-gpu：不装 CUDA 运行库
WITH_LLM = True         # 默认只装当前设置选中的一套本地语言模型
NO_SHORTCUT = False     # --no-shortcut：不建桌面快捷方式（测试用）
INSTALL_PREMIERE = True # 默认扫描 Premiere 并安装当前用户 CEP 插件


def _init_console():
    """把控制台切成 UTF-8，否则中文 Windows 默认 GBK 会打印报错。"""
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _safe_input(prompt=""):
    try:
        input(prompt)
    except (EOFError, OSError):
        pass


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def _setup_log():
    global _log_file
    os.makedirs(LOG_DIR, exist_ok=True)
    name = time.strftime("install_%Y%m%d_%H%M%S.log")
    _log_file = open(os.path.join(LOG_DIR, name), "w", encoding="utf-8")


def log(msg=""):
    print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


def rule(title=""):
    log()
    log("=" * 62)
    if title:
        log("  " + title)
        log("=" * 62)


def progress(label, done, total):
    if total > 0:
        pct = done * 100.0 / total
        bar = "#" * int(pct / 4) + "-" * (25 - int(pct / 4))
        sys.stdout.write("\r  %s [%s] %5.1f%%  %.0f/%.0f MB   "
                         % (label, bar, pct, done / 1048576, total / 1048576))
    else:
        sys.stdout.write("\r  %s  %.0f MB   " % (label, done / 1048576))
    sys.stdout.flush()


def progress_done():
    sys.stdout.write("\n")
    sys.stdout.flush()


def ask(prompt, default=True):
    """没有交互终端时直接返回默认值（比如被计划任务唤起）。"""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    tail = " [Y/n] " if default else " [y/N] "
    try:
        ans = input("  " + prompt + tail).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes", "是", "1")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def run(cmd, cwd=None, check=False, quiet=True):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, cwd=cwd or BASE, capture_output=quiet,
                           text=True, encoding="utf-8", errors="replace", **kw)
    except OSError as exc:
        r = subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))
    if check and r.returncode != 0:
        raise RuntimeError("命令失败：%s\n%s" % (" ".join(map(str, cmd)),
                                               (r.stderr or r.stdout or "")[-1500:]))
    return r


def script_command(script, *args):
    """Run a project script from either the packaged EXE or source venv."""
    if FROZEN_APP:
        return [sys.executable, "--run-script", script, *map(str, args)]
    return [VENV_PY, script, *map(str, args)]


def py_version(exe):
    r = run([exe, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"])
    if r.returncode != 0:
        return None
    try:
        return tuple(int(x) for x in r.stdout.strip().split("."))
    except ValueError:
        return None


def python_is_usable(exe):
    v = py_version(exe)
    if not v:
        return None
    return v if v[:2] >= GOOD_PY else None


def find_base_python():
    """按优先级找一个能用来建虚拟环境的 Python 解释器（3.10 以上即可）。"""
    # 1) py 启动器，优先 3.12
    for ver in (PREFER_PY, "3.11", "3.10", "3.13", "3.14"):
        r = run(["py", "-%s" % ver, "-c", "import sys;print(sys.executable)"])
        if r.returncode == 0 and r.stdout.strip():
            exe = r.stdout.strip()
            if python_is_usable(exe):
                return exe, "py -%s" % ver

    # 2) 当前可用解释器。没有 py 启动器时仍可用 python installer.py。
    if python_is_usable(sys.executable):
        return sys.executable, "当前解释器"

    # 3) 常见安装位置，版本号高的在前
    patterns = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
        os.path.expandvars(r"%ProgramFiles%\Python3*\python.exe"),
        r"C:\Python3*\python.exe",
    ]
    cands = []
    for p in patterns:
        cands += glob.glob(p)

    def ver_key(path):
        digits = "".join(ch for ch in os.path.basename(os.path.dirname(path)) if ch.isdigit())
        return int(digits) if digits else 0

    for exe in sorted(cands, key=ver_key, reverse=True):
        if python_is_usable(exe):
            return exe, "已安装"

    # 4) PATH 上的 python
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and python_is_usable(found):
            return found, "PATH"

    return None, "未找到"


def ensure_base_python():
    exe, how = find_base_python()
    if exe:
        v = py_version(exe)
        log("  找到 Python %d.%d.%d（%s）" % (v[0], v[1], v[2], how))
        log("  %s" % exe)
        return exe

    log("  没找到可用的 Python（需要 3.10 以上）。")
    return install_python_312()


def install_python_312():
    """用 winget 装一个 Python 3.12，装完再找一次。"""
    log("  正在尝试用 winget 自动安装 Python 3.12 …")
    r = run(["winget", "install", "-e", "--id", "Python.Python.3.12",
             "--scope", "user", "--accept-package-agreements",
             "--accept-source-agreements", "--disable-interactivity"], quiet=False)
    if r.returncode != 0:
        log()
        log("  [X] 自动安装失败。请手动安装 Python 3.12：")
        log("    https://www.python.org/downloads/")
        log("    安装时务必勾选 “Add python.exe to PATH”，装完重新双击本脚本。")
        return None

    log("  安装完成，正在重新查找 …")
    exe, how = find_base_python()
    if exe:
        v = py_version(exe)
        log("  找到 Python %d.%d.%d（%s）" % (v[0], v[1], v[2], how))
        return exe
    log("  [X] 装完了但还是找不到，请重启电脑后再试一次。")
    return None


# --------------------------------------------------------------------------
# 步骤 1：虚拟环境
# --------------------------------------------------------------------------
def venv_ok():
    if not os.path.isfile(VENV_PY):
        return False
    v = py_version(VENV_PY)
    return bool(v and v[:2] >= GOOD_PY)


def use_bundled_runtime():
    """包自带 Python 运行时（runtime\\python + .venv）时，直接修 pyvenv.cfg 用起来。

    新电脑可能连 Python 都没装，所以 1.0.1 的包带运行时；这里优先用它，
    不建环境、不联网、不 pip install。
    """
    base = os.path.join(BASE, "runtime", "python", "python.exe")
    if not (os.path.isfile(base) and os.path.isfile(VENV_PY)):
        return False
    cfg = os.path.join(VENV, "pyvenv.cfg")
    txt = ("home = %s\ninclude-system-site-packages = false\n"
           "version = 3.14.4\nexecutable = %s\ncommand = portable\n"
           % (os.path.join(BASE, "runtime", "python"), base))
    try:
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(txt)
    except OSError:
        return False
    r = run([VENV_PY, "-c", "import faster_whisper, PySide6, cv2, numpy;print('ok')"])
    if r.returncode == 0 and "ok" in (r.stdout or ""):
        log("  使用包自带的 Python 运行时（本机无需安装 Python）")
        return True
    log("  自带运行时不可用，改为常规安装：%s" % (r.stderr or r.stdout or "")[-200:])
    return False


def ensure_venv(base_py):
    rule("第 1 步 / 共 5 步 · 建运行环境")
    if venv_ok():
        v = py_version(VENV_PY)
        log("  已存在（Python %d.%d.%d），跳过。" % v)
        return True

    if os.path.isdir(VENV):
        log("  发现一个不完整的旧环境，先清理 …")
        shutil.rmtree(VENV, ignore_errors=True)

    log("  正在创建 .venv（不联网，几秒钟）…")
    r = run([base_py, "-m", "venv", VENV])
    if r.returncode != 0:
        log("  [X] 创建失败：")
        log((r.stderr or r.stdout or "")[-1200:])
        return False
    log("  完成。")
    return venv_ok()


# --------------------------------------------------------------------------
# 步骤 2：依赖
# --------------------------------------------------------------------------
def deps_ok():
    if FROZEN_APP:
        try:
            import av  # noqa: F401
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401
            import PySide6  # noqa: F401
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False
    if not os.path.isfile(VENV_PY):
        return False
    r = run([VENV_PY, "-c", "import faster_whisper, PySide6, cv2, numpy, av, "
                            "pypinyin, opencc, rapidocr_onnxruntime, "
                            "onnxruntime, PIL, pyclipper, shapely;print('ok')"])
    return r.returncode == 0 and "ok" in (r.stdout or "")


def requirements_digest():
    if not os.path.isfile(REQ_FILE) or not os.path.isfile(CONSTRAINT_FILE):
        return ""
    digest = hashlib.sha256()
    for path in (REQ_FILE, CONSTRAINT_FILE):
        with open(path, "rb") as handle:
            digest.update(os.path.basename(path).encode("utf-8") + b"\0")
            digest.update(handle.read())
    return digest.hexdigest()


def dependencies_current():
    """A changed requirements file must be installed even if old imports work."""
    digest = requirements_digest()
    try:
        with open(DEPS_STAMP, encoding="ascii") as handle:
            return bool(digest) and handle.read().strip() == digest and deps_ok()
    except OSError:
        return False


CUDA_PKGS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def has_nvidia() -> bool:
    """有没有 NVIDIA 显卡。没有就完全不碰 CUDA，省掉 800 MB 下载。"""
    exe = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                       "System32", "nvidia-smi.exe")
    return os.path.isfile(exe) or bool(shutil.which("nvidia-smi"))


def cuda_ok() -> bool:
    """CUDA 运行库装没装。"""
    if not os.path.isfile(VENV_PY):
        return False
    r = run([VENV_PY, "-c", "import nvidia.cublas, nvidia.cudnn; print(1)"])
    return r.returncode == 0 and "1" in (r.stdout or "")


def ensure_cuda_libs() -> None:
    """检测到 N 卡就补装 CUDA 运行库，让语音识别走显卡（快约 3.8 倍）。

    这两个包不在 requirements.txt 里：合计约 800 MB，只有 N 卡机器才用得上，
    没显卡的电脑不该为它付下载成本。
    """
    if NO_GPU:
        log("  按 --no-gpu 跳过 GPU 运行库。")
        return
    if not has_nvidia():
        log("  没有检测到 NVIDIA 显卡，识别走 CPU（不影响使用）。")
        return
    if cuda_ok():
        log("  GPU 运行库已就绪。")
        return

    log("  检测到 NVIDIA 显卡，补装 CUDA 运行库（约 800 MB，识别可快约 3.8 倍）…")
    for mirror, label in ((MIRROR, "清华镜像"), (MIRROR_BACKUP, "阿里云镜像"), (None, "官方源")):
        cmd = [VENV_PY, "-m", "pip", "install", "--disable-pip-version-check",
               "--timeout", "120"] + CUDA_PKGS
        if mirror:
            cmd += ["-i", mirror]
        r = run(cmd)
        if r.returncode == 0 and cuda_ok():
            log("  GPU 运行库装好了。")
            return
        log("  %s没成功，换下一个源。" % label)
    log("  GPU 运行库没装上。不影响使用，识别会走 CPU。")


def llm_ready(verify_hash=False) -> bool:
    """当前用户选择的唯一模型是否就绪；其他档位不能冒充。"""
    exe = os.path.join(BASE, "bin", "llama", "llama-server.exe")
    if not os.path.isfile(exe):
        return False
    try:
        import llm_config
        import llm_models
        tier = llm_config.active_tier()
        model = llm_config.model_path()
    except Exception:  # noqa: BLE001
        return False
    return bool(model and os.path.isfile(model)
                and llm_models.downloaded(tier, verify_hash=verify_hash))


def ensure_llm():
    """安装配置中选中的唯一语言模型；不会顺带安装其他档位。"""
    if llm_ready(verify_hash=True):
        log("  本地语言模型已就绪，跳过。")
        return True
    log("")
    try:
        import llm_config
        selected = llm_config.selected_model_name()
    except Exception:  # noqa: BLE001
        selected = "当前设置的模型"
    log("  安装全流程唯一语言模型：%s …" % selected)
    script = os.path.join(BASE, "setup_llm.py")
    if not os.path.isfile(script):
        log("  找不到 setup_llm.py，跳过。")
        return False
    r = run(script_command(script))
    if llm_ready(verify_hash=True):
        log("  语言模型装好了。")
        return True
    log("  语言模型没装完。软件仍会启动，可稍后在“本地模型”中续装。")
    return False


def ensure_deps():
    rule("第 2 步 / 共 5 步 · 安装依赖（约 500 MB，需联网）")
    if FROZEN_APP:
        if deps_ok():
            log("  成品程序已内置运行依赖，跳过 Python/venv/pip。")
            return True
        log("  [X] 成品程序运行库不完整，请重新下载完整 ZIP。")
        return False
    if not os.path.isfile(REQ_FILE):
        log("  [X] 找不到 requirements.txt")
        return False
    if not os.path.isfile(CONSTRAINT_FILE):
        log("  [X] 找不到 constraints-runtime.txt；无法保证换机依赖版本一致。")
        return False
    if dependencies_current():
        log("  依赖齐全且清单未变，跳过。")
        return True

    for mirror, label in ((MIRROR, "清华镜像"), (MIRROR_BACKUP, "阿里云镜像"), (None, "官方源")):
        log("  用%s安装 …" % label)
        cmd = [VENV_PY, "-m", "pip", "install", "--upgrade", "pip", "-q"]
        run(cmd)
        cmd = [VENV_PY, "-m", "pip", "install", "-r", REQ_FILE,
               "-c", CONSTRAINT_FILE, "--disable-pip-version-check", "--timeout", "60"]
        if mirror:
            cmd += ["-i", mirror]
        r = run(cmd)
        if r.returncode == 0 and deps_ok():
            with open(DEPS_STAMP, "w", encoding="ascii") as handle:
                handle.write(requirements_digest())
            log("  完成，依赖已就绪。")
            ensure_cuda_libs()
            return True
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
        log("  这个源没成功：")
        for line in tail:
            log("    " + line)
    log("  [X] 三个源都失败了。请把上面几行报错发给做工具的人。")
    return False


# --------------------------------------------------------------------------
# 步骤 3：ffmpeg
# --------------------------------------------------------------------------
def ffmpeg_ok():
    exe = os.path.join(BIN_DIR, "ffmpeg.exe")
    probe = os.path.join(BIN_DIR, "ffprobe.exe")
    if not (os.path.isfile(exe) and os.path.isfile(probe)):
        return False
    r = run([exe, "-version"])
    return r.returncode == 0


def _download(url, dest, label):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                progress(label, done, total)
    progress_done()
    return os.path.getsize(dest)


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_download(path, expected_size, expected_sha256):
    """下载件必须同时匹配固定字节数和 SHA256。"""
    if not os.path.isfile(path):
        return False
    if os.path.getsize(path) != int(expected_size):
        return False
    return _file_sha256(path).lower() == str(expected_sha256).lower()


def ensure_ffmpeg():
    rule("第 3 步 / 共 5 步 · 准备 ffmpeg")
    if ffmpeg_ok():
        log("  已存在，跳过。")
        return True

    os.makedirs(BIN_DIR, exist_ok=True)
    tmp_zip = os.path.join(BIN_DIR, "_ffmpeg_download.zip")

    for url, label, expected_size, expected_sha256 in FFMPEG_SOURCES:
        try:
            log("  从 %s 下载（约 196 MB）…" % label)
            _download(url, tmp_zip, "ffmpeg")
            if not _verified_download(tmp_zip, expected_size, expected_sha256):
                raise RuntimeError("FFmpeg 大小或 SHA256 不匹配，拒绝解压")
            log("  已通过固定 SHA256 校验。")
        except Exception as exc:  # noqa: BLE001
            log("  下载失败：%s" % exc)
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            continue

        try:
            log("  正在解压 …")
            with zipfile.ZipFile(tmp_zip) as zf:
                wanted = {}
                for name in zf.namelist():
                    base = os.path.basename(name).lower()
                    if base in ("ffmpeg.exe", "ffprobe.exe"):
                        wanted[base] = name
                if "ffmpeg.exe" not in wanted:
                    raise RuntimeError("压缩包里没有 ffmpeg.exe")
                for base, name in wanted.items():
                    with zf.open(name) as src, open(os.path.join(BIN_DIR, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except Exception as exc:  # noqa: BLE001
            log("  解压失败：%s" % exc)
            continue
        finally:
            if os.path.exists(tmp_zip):
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass

        if ffmpeg_ok():
            log("  ffmpeg 就绪。")
            return True
        log("  文件放下了但跑不起来，换下一个源。")

    log("  [X] 所有源都失败。请手动下载后放到 bin\\ 目录：")
    log("    请重试，或从项目文档中记录的固定 BtbN 版本安装。")
    return False


# --------------------------------------------------------------------------
# 步骤 4：语音模型
# --------------------------------------------------------------------------
def model_ok(size):
    try:
        import fetch_model
        return fetch_model.verify_model_dir(size, base=BASE, verify_hash=False)
    except Exception:  # noqa: BLE001
        d = os.path.join(MODEL_DIR, "%s-full" % size)
        return os.path.isfile(os.path.join(d, "model.bin"))


def selected_asr_model(base_dir=None):
    """Return the ASR model selected by the software, with its UI default."""
    base_dir = os.path.abspath(base_dir or BASE)
    path = os.path.join(base_dir, "conf", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            configured = str((json.load(handle) or {}).get("model") or "").strip()
    except (OSError, ValueError, TypeError):
        configured = ""
    return configured if configured in ("large-v3", "large-v3-turbo") else "large-v3"


def ensure_models(want_accurate=False):
    rule("第 4 步 / 共 5 步 · 下载语音模型")
    selected = "large-v3" if want_accurate else selected_asr_model()
    log("  跟随软件识别设置，只准备当前模型：%s" % selected)
    todo = [] if model_ok(selected) else [selected]

    if not todo:
        log("  当前语音模型已存在，跳过。")
        return True

    log("  需要下载：%s" % "、".join(todo))
    log("  走镜像 + 断点续传；中途断了重新双击会接着下。")
    env = dict(os.environ)
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

    # 优先用自带的 fetch_model.py：huggingface_hub 的 snapshot_download
    # 在本机下 3 GB 的 large-v3 会传几十秒就停住，自写的 Range 下载器没问题。
    fetch = os.path.join(BASE, "fetch_model.py")
    r = None
    if os.path.isfile(fetch):
        for size in todo:
            log("  · %s" % size)
            r = subprocess.run(script_command(fetch, size), cwd=BASE, env=env)
    if r is None or any(not model_ok(s) for s in todo):
        log("  换成 huggingface_hub 再试一次 …")
        r = subprocess.run(script_command(
            os.path.join(BASE, "predownload_models.py"), *todo),
                           cwd=BASE, env=env)

    # 以「文件到底在不在」为准，不完全信退出码：
    # 下载器有时会因为进度条写 stderr 被上层判成非零，其实文件已经好了。
    missing = [s for s in todo if not model_ok(s)]
    if not missing:
        log("  模型就绪。")
        return True

    log("  [X] 还缺：%s" % "、".join(missing))
    if r.returncode != 0:
        log("    下载器退出码 %d。可以重新运行本脚本接着下，" % r.returncode)
        log("    或者先跳过、等第一次识别时程序自己下（会慢一点）。")
    return False


# --------------------------------------------------------------------------
# Premiere 插件（best effort，不阻断主程序）
# --------------------------------------------------------------------------
def install_premiere_plugin_best_effort():
    rule("Premiere 插件 · 自动扫描与安装")
    if not INSTALL_PREMIERE:
        log("  按 --no-premiere-plugin 跳过。")
        return True
    try:
        import pr_bridge_installer as bridge
        source = bridge.extension_source(BASE)
        if not source.is_dir():
            log("  未找到随包插件目录，跳过；可稍后在软件内手动安装。")
            return False
        premieres = bridge.discover_premiere()
        if not premieres:
            log("  未检测到 Premiere；仍预装到当前用户 CEP，之后安装 PR 可直接使用。")
        target = next((row for row in bridge.discover_targets() if not row.system), None)
        if target is None:
            log("  未找到当前用户 CEP 目录候选，跳过自动安装。")
            return False
        current = bridge.inspect_target(target.root, source)
        if current.get("matches"):
            log("  已是当前版：%s" % current["path"])
            return True
        if premieres:
            log("  检测到：%s" % "、".join(row.name for row in premieres))
        result = bridge.install_extension(source, target.root)
        log("  已安装 v%s（%d 个文件）：%s" %
            (result.version, result.file_count, result.target))
        for warning in result.warnings:
            log("  提示：%s" % warning)
        log("  已打开的 Premiere 需要重启后才能看到插件。")
        return True
    except Exception as exc:  # noqa: BLE001
        log("  自动安装未完成：%s" % exc)
        log("  主程序继续安装；稍后可双击“安装PR插件.bat”选择目录重试。")
        return False


# --------------------------------------------------------------------------
# 步骤 5：快捷方式
# --------------------------------------------------------------------------
def ensure_shortcut():
    rule("第 5 步 / 共 5 步 · 桌面快捷方式")
    if NO_SHORTCUT:
        log("  按 --no-shortcut 跳过。")
        return True
    try:
        desktop = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"], text=True).strip()
    except Exception:  # noqa: BLE001
        log("  拿不到桌面路径，跳过（可以直接双击 启动SmartCut.bat）。")
        return True

    lnk = os.path.join(desktop, PRODUCT_NAME + ".lnk")
    packaged = sys.executable if FROZEN_APP else ""
    target = packaged or (STARTER if os.path.isfile(STARTER) else VENV_PYW)
    arguments = "" if target in (packaged, STARTER) else os.path.join(BASE, "app_main.py")
    icon_file = os.path.join(BASE, "assets", "icon.ico")
    icon = icon_file if os.path.isfile(icon_file) else (packaged or VENV_PYW)

    def q(p):
        """PowerShell 单引号字符串；反斜杠必须保持原样，不能用 repr。"""
        return "'" + str(p).replace("'", "''") + "'"

    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        "$s = $ws.CreateShortcut(%s);"
        "$s.TargetPath = %s;"
        "$s.Arguments = %s;"
        "$s.WorkingDirectory = %s;"
        "$s.IconLocation = %s;"
        "$s.Description = %s;"
        "$s.Save()"
    ) % (q(lnk), q(target), q(arguments), q(BASE), q(icon), q(PRODUCT_NAME))
    r = run(["powershell", "-NoProfile", "-Command", ps])
    if r.returncode == 0 and os.path.isfile(lnk):
        log("  已创建：%s" % lnk)
    else:
        log("  创建失败，以后直接双击 启动SmartCut.bat 也行。")
    return True


# --------------------------------------------------------------------------
# 启动 / 体检
# --------------------------------------------------------------------------
def launch():
    rule("启动")
    if FROZEN_APP:
        subprocess.Popen([sys.executable], cwd=BASE)
        log("  已启动成品程序，窗口应该马上出来。")
        return True
    if not os.path.isfile(VENV_PYW):
        log("  [X] 找不到 pythonw.exe，环境不完整。")
        return False
    subprocess.Popen([VENV_PYW, os.path.join(BASE, "app_main.py")], cwd=BASE)
    log("  已启动，窗口应该马上出来。")
    return True


def health_check():
    rule("体检（不做任何改动）")
    items = []

    if FROZEN_APP:
        items.append(("成品运行环境", "OK（无需本机 Python）", True))
    else:
        v = py_version(VENV_PY) if os.path.isfile(VENV_PY) else None
        items.append(("虚拟环境", "OK  Python %d.%d.%d" % v if v else "缺失", bool(v)))
    items.append(("依赖包", "OK" if deps_ok() else "缺失", deps_ok()))
    items.append(("ffmpeg", "OK  %s" % os.path.join(BIN_DIR, "ffmpeg.exe")
                  if ffmpeg_ok() else "缺失", ffmpeg_ok()))
    if has_nvidia():
        items.append(("GPU 运行库", "OK（识别走显卡）" if cuda_ok()
                      else "未装（识别走 CPU）", True))
    else:
        items.append(("GPU 运行库", "无 NVIDIA 显卡，走 CPU", True))
    selected_asr = selected_asr_model()
    items.append(("语音模型·%s" % selected_asr,
                  "OK" if model_ok(selected_asr) else "缺失",
                  model_ok(selected_asr)))
    items.append(("本地语言模型", "OK（字幕纠错可用）" if llm_ready()
                  else "未装（可选，字幕纠错用不了）", True))
    for name, state, ok in items:
        log("  %s %-18s %s" % ("[OK]" if ok else "[X]", name, state))
    return all(ok for _, _, ok in items)


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        _init_console()
        print(__doc__)
        print("额外选项：--no-gpu  --no-shortcut  --no-llm  --no-premiere-plugin")
        return 0
    if "--check" in args:
        _init_console()
        return 0 if health_check() else 1

    _setup_log()
    global NO_GPU
    global NO_SHORTCUT, WITH_LLM, INSTALL_PREMIERE
    NO_GPU = "--no-gpu" in args
    WITH_LLM = "--no-llm" not in args
    INSTALL_PREMIERE = "--no-premiere-plugin" not in args
    NO_SHORTCUT = "--no-shortcut" in args
    no_launch = "--no-launch" in args
    no_model = "--no-model" in args
    accurate = "--accurate" in args

    rule(PRODUCT_NAME + " " + VERSION + " · 一站式安装")
    log(credits_plain_text())
    log("  装好之后，双击桌面上的「%s」即可。" % PRODUCT_NAME)
    log("  中途失败不要紧：再双击一次会从没做完的那步继续。")

    # 成品 EXE 自己运行 installer.py 时，依赖已经在 _internal 中，不查找或
    # 安装 Python，也不创建第二套 .venv。
    if FROZEN_APP:
        base_py = sys.executable
        deps_ready = ensure_deps()
    # 包自带运行时：直接用，不找本机 Python、不建环境、不联网
    elif use_bundled_runtime():
        base_py = os.path.join(BASE, "runtime", "python", "python.exe")
        deps_ready = ensure_deps()
    else:
        base_py = ensure_base_python()
        if not base_py:
            return 1
        if not ensure_venv(base_py):
            return 1
        deps_ready = ensure_deps()

    if (not deps_ready and not FROZEN_APP and py_version(base_py)
            and py_version(base_py)[:2] >= (3, 13)):
        # 新版本 Python 偶尔有轮子还没跟上，退回 3.12 再试一次
        log()
        log("  当前 Python %d.%d 装依赖失败，改用 3.12 再试一次 …"
            % (py_version(base_py)[0], py_version(base_py)[1]))
        py312 = install_python_312()
        if py312:
            base_py = py312
            shutil.rmtree(VENV, ignore_errors=True)   # 旧环境是别的版本建的，必须重建
            if ensure_venv(base_py):
                deps_ready = ensure_deps()
    if not deps_ready:
        return 1
    if not ensure_ffmpeg():
        return 1

    model_ok_all = True
    if no_model:
        rule("第 4 步 / 共 5 步 · 下载语音模型")
        log("  按参数要求跳过。第一次识别时程序会自己下。")
    else:
        model_ok_all = ensure_models(accurate)

    llm_ok = True
    if WITH_LLM:
        llm_ok = ensure_llm()
    else:
        log("  按 --no-llm 跳过本地语言模型；可稍后在软件内下载。")

    plugin_ok = install_premiere_plugin_best_effort()
    ensure_shortcut()

    with open(STAMP, "w", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))

    rule("全部就绪")
    if not model_ok_all:
        log("  注意：语音模型还没下完，第一次识别会等得久一些。")
    if not llm_ok:
        log("  注意：本地语言模型未完成，可进软件后继续下载当前选择的模型。")
    if not plugin_ok:
        log("  注意：PR 插件未自动装好，不影响主程序；可在工具菜单中重试。")
    log("  以后两种打开方式：")
    log("    · 双击桌面「%s」" % PRODUCT_NAME)
    log("    · 双击本文件夹里的 启动SmartCut.bat")
    log()
    if not no_launch:
        launch()
    else:
        log("  （按要求不自动启动）")
    log()
    log("  日志：%s" % (os.path.basename(_log_file.name) if _log_file else "-"))
    if not no_launch:
        time.sleep(1.5)
    return 0


if __name__ == "__main__":
    _init_console()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。再运行一次会接着装。")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print("\n[意外错误] %s: %s" % (type(exc).__name__, exc))
        import traceback
        traceback.print_exc()
        _safe_input("\n按回车关闭 …")
        sys.exit(1)

# -*- coding: utf-8 -*-
"""把「一站式版」打成可以发给别人的干净压缩包。

默认只挑程序源码、安装入口、教程、PR 插件和许可证材料，排除：
    .venv / models / bin / logs / __pycache__ / .installed / .cache
运行组件由目标机器上的安装器自动准备。只有显式传入
``--with-app-bundle`` 且二进制许可证门禁完整时才附带 EXE/_internal。

用法：
    python make_release.py
产物：
    dist/SmartCutAssistant/          （可直接双击试）
    dist/SmartCutAssistant.zip        （发给别人）
"""
import hashlib
import json
import os
import re
import shutil
import time
import zipfile

import product_info

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
NAME = product_info.PRODUCT_ID
RELEASE_VERSION = product_info.VERSION

# 只给自己用的脚本，不发给别人（其余 .py 全部自动收进来）
DEV_ONLY = {"dev_finish.py", "selftest_track.py"}
DEV_ONLY_PREFIXES = ("test_", "evaluate_")
# 只发清单里的非 .py 文件（bat / vbs / 说明）
PLAIN = [
    "安装并启动.bat", "安装到指定目录.bat", "启动SmartCut.bat", "启动（排错用）.bat",
    "安装PR插件.bat", "install_pr_bridge.ps1",
    "使用说明.txt", "使用说明-简易版.txt",
    "README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md",
    "RELEASE_CHECKLIST.md", "RELEASE_NOTES.md", "CHANGELOG.md", "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md", "SECURITY.md", "OPEN_SOURCE_DECISIONS.md",
    "requirements.txt", "constraints-runtime.txt", "requirements.lock", "requirements-dev.txt",
    "setup_windows.ps1", "LICENSE", "SBOM.cdx.json",
]

REQUIRED_FILES = (
    "README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md",
    "RELEASE_CHECKLIST.md", "RELEASE_NOTES.md", "CHANGELOG.md", "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md", "SECURITY.md", "OPEN_SOURCE_DECISIONS.md",
    "requirements.txt", "constraints-runtime.txt", "requirements.lock", "setup_windows.ps1", "启动SmartCut.bat",
    "SBOM.cdx.json", "LICENSES/manifest.json",
    "install_pr_bridge.ps1", "pr_bridge/CSXS/manifest.xml",
    "pr_bridge/js/main.js", "pr_bridge/jsx/host.jsx",
    "docs/安装指南.md", "docs/Premiere插件.md",
    "build_tools/generate_compliance.py", "brains/__init__.py", "brains/audio_brain.py",
)

PUBLIC_KB_FILES = tuple("%02d_%s.txt" % item for item in (
    (1, "模组名"), (2, "机器设备"), (3, "材料化学"),
    (4, "游戏机制"), (5, "口头语"), (6, "化学元素"),
    (7, "单位与符号"), (8, "物理工程"),
    (9, "电学与电子"), (10, "计算机与信息"),
))


def _ship_list():
    """自动扫描：所有运行期模块都进包。

    以前是手写清单，加了 director/script_writer/semantic 这些新模块后
    只补了 version.py 的兜底清单，打包清单没跟着改 —— 打出来的包缺 11 个
    模块，第三方装完一启动就 ModuleNotFoundError。改成扫目录，不再漏。
    """
    pys = sorted(f for f in os.listdir(BASE)
                 if f.endswith(".py") and f not in DEV_ONLY
                 and not f.startswith(DEV_ONLY_PREFIXES)
                 and os.path.isfile(os.path.join(BASE, f)))
    return pys + [f for f in PLAIN if os.path.isfile(os.path.join(BASE, f))]


INCLUDE = _ship_list()


def normalize_windows_batch(path):
    """Write a CMD launcher as UTF-8 without BOM and with CRLF lines.

    Older Windows command processors can treat a UTF-8 BOM as part of the
    first command, and LF-only batch files can then be tokenized incorrectly.
    Release staging normalizes every launcher instead of depending on the
    developer's Git checkout settings.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    with open(path, "wb") as handle:
        handle.write(text.replace("\n", "\r\n").encode("utf-8"))

# 打包时写一份「干净的默认配置」，不要把本机的导出路径带给别人
CLEAN_CONF = {
    "conf/settings.json": json.dumps({"model": "large-v3"}, ensure_ascii=False, indent=2) + "\n",
    "conf/llm.json": json.dumps({
        "models": {
            "fast": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            "smart": "Qwen3-8B-Q4_K_M.gguf",
            "large": "Qwen3-14B-Q4_K_M.gguf",
        },
        "active_tier": "smart",
        "tasks": {name: "smart" for name in (
            "proofread", "subtitle", "segment", "paragraph", "complete",
            "analyst", "writer", "editor", "director",
        )},
        "escalate": False,
        "disable_thinking": True,
    }, ensure_ascii=False, indent=2) + "\n",
    "conf/bgm.json": '{\n  "folder": "",\n  "enabled": "1",\n'
                     '  "level_db": "-22",\n  "duck_db": "0",\n  "loudness_norm": "1",\n'
                     '  "attack_ms": "30",\n  "release_ms": "600",\n'
                     '  "seg_len": "150",\n  "xfade": "2.0",\n'
                     '  "fade_in": "1.5",\n  "fade_out": "4.0",\n'
                     '  "seed": ""\n}\n',
}
CONF_COPY = [
    "conf/subtitle_style.json", "conf/subtitle_styles.json",
    "conf/autocut.json", "conf/terms.txt", "conf/asr_terms.txt",
    "conf/cut_value.json", "conf/decision.json", "conf/spec_manifest.json",
    "conf/coords_matrix.json", "conf/coords.json", "conf/editor_model.json",
]
# settings/llm/bgm 会在上方生成无私人路径的干净默认值；导演记忆目录留空时
# 由 memory_config 的同版本默认值生成。editor_model.json 是项目的剪辑权重，
# 不是用户的模型选择，漏掉会让换机后的剪辑结果改变，因此必须随版本发布。
EXCLUDE_DIRS = {".venv", "models", "bin", "logs", "__pycache__", "dist",
                ".cache", ".git"}
APP_BUNDLE = os.path.join(BASE, "发布版", "智能剪辑助手")


def _json_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return None


def publication_metadata_issues(base=BASE, with_models=False, with_bin=False,
                                with_app_bundle=False, with_runtime=False):
    """Return release-blocking legal/provenance metadata problems."""
    root = os.path.abspath(base)
    issues = []
    licence = os.path.join(root, "LICENSE")
    try:
        with open(licence, encoding="utf-8") as handle:
            licence_text = handle.read().strip()
    except OSError:
        licence_text = ""
    if len(licence_text) < 40:
        issues.append("LICENSE 缺失或尚未填写正式开源许可证")

    lock = os.path.join(root, "requirements.lock")
    try:
        with open(lock, encoding="utf-8") as handle:
            lock_text = handle.read()
    except OSError:
        lock_text = ""
    if "==" not in lock_text:
        issues.append("requirements.lock 缺失或没有锁定依赖版本")

    sbom = _json_file(os.path.join(root, "SBOM.cdx.json"))
    if not (isinstance(sbom, dict)
            and sbom.get("bomFormat") == "CycloneDX"
            and isinstance(sbom.get("components"), list)
            and sbom["components"]):
        issues.append("SBOM.cdx.json 缺失或不是有效的 CycloneDX 清单")

    manifest = _json_file(os.path.join(root, "LICENSES", "manifest.json"))
    ships_third_party_binaries = bool(
        with_app_bundle or with_runtime or with_models or with_bin)
    if not (isinstance(manifest, dict)
            and isinstance(manifest.get("components"), list)
            and manifest["components"]):
        issues.append("LICENSES/manifest.json 缺失或没有组件清单")
    elif ships_third_party_binaries and manifest.get("complete") is not True:
        issues.append("二进制发行要求 LICENSES/manifest.json complete=true")
    elif ships_third_party_binaries:
        for component in manifest["components"]:
            files = component.get("license_files") if isinstance(component, dict) else None
            if not files:
                issues.append("LICENSES/manifest.json 存在没有许可证正文的组件")
                break
            if any(not os.path.isfile(os.path.join(root, str(relative)))
                   for relative in files):
                issues.append("LICENSES/manifest.json 引用了不存在的许可证文件")
                break

    optional = []
    if with_models:
        optional.append("LICENSES/models/MANIFEST.json")
    if with_bin:
        optional.append("LICENSES/bin/MANIFEST.json")
    for relative in optional:
        value = _json_file(os.path.join(root, *relative.split("/")))
        if not (isinstance(value, dict) and value.get("complete") is True):
            issues.append(relative + " 缺失或尚未完成授权核对")
    return issues


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)
_WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]:\\+[^\s\"'；，。]+)")
_POSIX_PRIVATE = re.compile(r"(?<![A-Za-z0-9])(/(?:home|Users)/[^\s\"'；，。]+)")


def _public_example_path(value):
    """Recognize conventional placeholders without hiding real user paths."""
    normalized = str(value or "").replace("/", "\\").casefold()
    if any(mark in normalized for mark in ("<", ">", "{", "}", "*", "%")):
        return True
    examples = (
        r"c:\windows", r"c:\program", r"d:\media", r"d:\apps\smartcut",
        r"d:\adobecep",
        r"d:\录像", r"d:\索引", r"d:\片库", r"e:\本地大模型",
        r"z:\不存在",
    )
    return "..." in normalized or "…" in normalized or normalized.startswith(examples)


def audit_text_for_private_data(text, relative_path=""):
    """Find credentials and machine-specific absolute paths in public text."""
    findings = []
    for number, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.replace("\\\\", "\\")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append("%s:%d 检测到疑似真实密钥" %
                                (relative_path or "<text>", number))
                break
        is_example = bool(re.search(r"示例|example|placeholder", line, re.I))
        for match in _WINDOWS_ABSOLUTE.finditer(line):
            value = match.group(1)
            if is_example or _public_example_path(value):
                continue
            findings.append("%s:%d 检测到本机绝对路径：%s" %
                            (relative_path or "<text>", number, value))
        for match in _POSIX_PRIVATE.finditer(line):
            if not is_example:
                findings.append("%s:%d 检测到本机绝对路径：%s" %
                                (relative_path or "<text>", number, match.group(1)))
    return findings


def write_sha256(path):
    """Write the conventional ``<archive>.sha256`` sidecar."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    output = str(path) + ".sha256"
    with open(output, "w", encoding="ascii", newline="\n") as handle:
        handle.write("%s  %s\n" % (digest.hexdigest(), os.path.basename(path)))
    return output


def quarantine_release_artifacts(artifacts, reason="failed", dist=DIST):
    """Move partial/failed release outputs into one recoverable directory."""
    existing = [os.path.abspath(path) for path in (artifacts or {}).values()
                if path and os.path.exists(path)]
    if not existing:
        return None
    safe_reason = re.sub(r"[^0-9A-Za-z_-]+", "-", str(reason or "failed")).strip("-")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    blocked = os.path.join(os.path.abspath(dist),
                           "_发布隔离_%s_%s" % (stamp, safe_reason or "failed"))
    suffix = 1
    base_blocked = blocked
    while os.path.exists(blocked):
        suffix += 1
        blocked = base_blocked + "_%d" % suffix
    os.makedirs(blocked, exist_ok=False)
    for path in existing:
        shutil.move(path, os.path.join(blocked, os.path.basename(path)))
    with open(os.path.join(blocked, "隔离原因.txt"), "w", encoding="utf-8") as handle:
        handle.write(str(reason or "failed") + "\n")
    return blocked



def main():
    ap_opts = None
    import sys as _sys
    with_bin = "--with-bin" in _sys.argv[1:]
    with_runtime = "--with-runtime" in _sys.argv[1:]
    with_models = "--with-models" in _sys.argv[1:]
    with_app_bundle = "--with-app-bundle" in _sys.argv[1:]

    metadata_issues = publication_metadata_issues(
        with_models=with_models, with_bin=with_bin,
        with_app_bundle=with_app_bundle, with_runtime=with_runtime)
    if metadata_issues:
        print("!! 公开发布门禁未通过：")
        for issue in metadata_issues:
            print("   -", issue)
        return 4

    missing = [f for f in REQUIRED_FILES
               if not os.path.isfile(os.path.join(BASE, f))]
    missing.extend(f for f in INCLUDE
                   if not os.path.isfile(os.path.join(BASE, f)))
    if with_app_bundle:
        for rel in ("智能剪辑助手.exe", "_internal"):
            if not os.path.exists(os.path.join(APP_BUNDLE, rel)):
                missing.append(os.path.join("发布版", "智能剪辑助手", rel))
    bundle_exe = os.path.join(APP_BUNDLE, "智能剪辑助手.exe")
    if with_app_bundle and os.path.isfile(bundle_exe):
        critical_sources = [
            os.path.join(BASE, name) for name in
            ("app_main.py", "window_guard.py", "winproc.py", "no_console.py",
             "cards_ui.py", "index.py", "autopilot.py", "model_editor.py",
             "autoedit.py", "asr.py", "asr_hints.py",
             "proofread.py", "ui_autoedit.py", "autoedit_qt.py",
             "subtitles.py", "silence_trim.py", "boundary_refiner.py",
             "seam_picker.py", "director_multimodal.py", "llm_config.py",
             "ui_main_v2.py", os.path.join("ui", "llm_bridge.py"))
        ]
        newer = [os.path.basename(path) for path in critical_sources
                 if os.path.isfile(path)
                 and os.path.getmtime(path) > os.path.getmtime(bundle_exe) + 1]
        if newer:
            missing.append("发布版 EXE 已过期（源码更新：%s）" % "、".join(newer))
    if missing:
        print("!! 缺少这些文件，先别打包：")
        for m in missing:
            print("   -", m)
        return 1

    stage = os.path.join(DIST, NAME)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage, exist_ok=True)

    for f in INCLUDE:
        shutil.copy2(os.path.join(BASE, f), os.path.join(stage, f))


    def _copytree(rel, label, skip_pyc=True, excluded_dirs=()):
        """把 BASE/rel 整个拷进包（大目录用，报进度）。"""
        src = os.path.join(BASE, rel)
        if not os.path.isdir(src):
            print("  [跳过] 没有 %s" % rel)
            return 0
        dst = os.path.join(stage, rel)
        total = 0
        n = 0
        for root, dirs, files in os.walk(src):
            excluded = set(excluded_dirs or ())
            if skip_pyc:
                dirs[:] = [d for d in dirs if d != "__pycache__" and d not in excluded]
            else:
                dirs[:] = [d for d in dirs if d not in excluded]
            r = os.path.relpath(root, src)
            out = os.path.join(dst, r) if r != "." else dst
            os.makedirs(out, exist_ok=True)
            for f in files:
                if skip_pyc and f.endswith(".pyc"):
                    continue
                sp = os.path.join(root, f)
                try:
                    shutil.copy2(sp, os.path.join(out, f))
                    total += os.path.getsize(sp)
                    n += 1
                except OSError:
                    continue
        print("  附带 %s：%d 个文件，%.0f MB" % (label, n, total / 1048576))
        return total

    # 公开包只发布人工维护的十份通用词表。整合包、统计缓存和本机
    # 模组索引可能包含第三方或用户数据，不能通过目录复制混进发行版。
    kb_dst = os.path.join(stage, "kb")
    os.makedirs(kb_dst, exist_ok=True)
    for filename in PUBLIC_KB_FILES:
        shutil.copy2(os.path.join(BASE, "kb", filename),
                     os.path.join(kb_dst, filename))
    print("  附带公开术语词典：%d 个文件" % len(PUBLIC_KB_FILES))
    _copytree("assets", "界面资源")
    # ui\ 是本地模型桥（llm_bridge）：自主剪辑要用它启动 llama-server，
    # 只扫根目录 .py 会漏掉，exe 里点「自主剪辑」就会 ModuleNotFoundError。
    _copytree("ui", "模型桥")
    _copytree("brains", "音频/画面/文字小脑")
    # 一键安装、软件内教程和开源发布都依赖这些目录。过去发布脚本只复制
    # 根目录文件，导致 ZIP 里没有 CEP 插件载荷，也没有完整教程和实际被
    # manifest 引用的许可证正文；安装器即使成功启动也无从安装 PR 插件。
    # bin/ 是已经停用的原生菜单实验，不参与当前 CEP 链路，也没有进入
    # 默认二进制许可证门禁；公开源码包只保留 native/*.cs 源码。
    _copytree("pr_bridge", "Premiere 插件", excluded_dirs={"bin"})
    _copytree("docs", "教程与开发文档")
    _copytree("LICENSES", "第三方许可证正文")
    build_dst = os.path.join(stage, "build_tools")
    os.makedirs(build_dst, exist_ok=True)
    for filename in ("智能剪辑助手.spec", "build_app.py", "build_exe.py",
                     "generate_compliance.py", "packaged_subtitle_smoke.py",
                     "version_info.txt"):
        source = os.path.join(BASE, "build_tools", filename)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(build_dst, filename))

    # 默认 GitHub ZIP 是联网自举包：用户双击后自动准备 Python/PyPI 依赖、
    # FFmpeg、模型和 PR 插件，不重新分发复杂的第三方 DLL。只有明确使用
    # --with-app-bundle 且二进制许可证门禁完整时，才附带成套 EXE/_internal。
    if with_app_bundle:
        shutil.copy2(os.path.join(APP_BUNDLE, "智能剪辑助手.exe"),
                     os.path.join(stage, "智能剪辑助手.exe"))
        shutil.copytree(os.path.join(APP_BUNDLE, "_internal"),
                        os.path.join(stage, "_internal"), dirs_exist_ok=True)
        print("  附带正式程序：EXE + _internal（同一次构建）")
    else:
        print("  生成联网一键安装包：第三方运行组件由安装器从官方源获取")

    if with_runtime:
        _copytree(os.path.join("runtime", "python"), "Python 运行时")
        _copytree(".venv", "依赖环境(.venv)")
    if with_bin:
        _copytree("bin", "FFmpeg / llama")
    if with_models:
        _copytree("models", "模型（语音+大模型）")

    # 默认配置：写干净的一份，别把本机的导出目录带给别人
    os.makedirs(os.path.join(stage, "conf"), exist_ok=True)
    for rel, text in CLEAN_CONF.items():
        with open(os.path.join(stage, rel), "w", encoding="utf-8") as fh:
            fh.write(text)
    for rel in CONF_COPY:
        src = os.path.join(BASE, rel)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(stage, rel))

    # 版本说明
    import time as _t
    with open(os.path.join(stage, "版本.txt"), "w", encoding="utf-8") as fh:
        fh.write("SmartCut 智能剪辑助手\n版本：%s\n打包时间：%s\n\n"
                 "第一次使用：解压后双击「安装到指定目录.bat」，选一个安装目录；\n"
                 "或者直接双击「安装并启动.bat」就地安装。\n"
                 "装好后：设置… → BGM 曲库，选你放 BGM 的文件夹。\n"
                 "自检：.venv\\Scripts\\python.exe selftest_cut.py\n"
                 % (RELEASE_VERSION, _t.strftime("%Y-%m-%d %H:%M")))

    # CMD launchers must work even when this build was made from an LF-only
    # checkout.  Keep the public ZIP independent of Git autocrlf settings.
    for root, _dirs, files in os.walk(stage):
        for filename in files:
            if filename.lower().endswith((".bat", ".cmd")):
                normalize_windows_batch(os.path.join(root, filename))

    # 顺手检查有没有漏收的 .py（防止以后加了新模块忘了加进清单）
    src_py = {f for f in os.listdir(BASE)
              if f.endswith(".py") and os.path.isfile(os.path.join(BASE, f))}
    left = {f for f in src_py
            if f not in DEV_ONLY and not f.startswith(DEV_ONLY_PREFIXES)} - set(INCLUDE)
    if left:
        print("提示：这些 .py 没进打包清单，确认是否需要：", sorted(left))

    total = 0
    for root, _dirs, files in os.walk(stage):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))

    privacy_findings = []
    text_extensions = {".py", ".md", ".txt", ".json", ".toml", ".ps1",
                       ".bat", ".vbs", ".yml", ".yaml", ".ini", ".cfg"}
    for root, _dirs, files in os.walk(stage):
        for filename in files:
            if os.path.splitext(filename)[1].lower() not in text_extensions:
                continue
            full = os.path.join(root, filename)
            relative = os.path.relpath(full, stage)
            try:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    privacy_findings.extend(
                        audit_text_for_private_data(handle.read(), relative))
            except OSError as exc:
                privacy_findings.append("%s 无法审计：%s" % (relative, exc))
    if privacy_findings:
        print("!! 隐私扫描未通过：")
        for finding in privacy_findings[:30]:
            print("   -", finding)
        blocked = quarantine_release_artifacts(
            {"stage": stage}, reason="privacy-scan", dist=DIST)
        if blocked:
            print("不完整产物已隔离：", blocked)
        return 5

    # 可选：把 ffmpeg/ffprobe 一起打进去（离线包，约 85 MB），第三方装的时候不用再下
    if with_bin:
        bdst = os.path.join(stage, "bin")
        os.makedirs(bdst, exist_ok=True)
        for f in ("ffmpeg.exe", "ffprobe.exe"):
            s_ = os.path.join(BASE, "bin", f)
            if os.path.isfile(s_):
                shutil.copy2(s_, os.path.join(bdst, f))
        print("  附带 ffmpeg：%d 个文件"
              % len([f for f in os.listdir(bdst)]))

    _tag = ""
    if with_models:
        _tag = "_完全离线版"
    elif with_app_bundle:
        _tag = "_成品版"
    elif with_runtime:
        _tag = "_免安装Python"
    elif with_bin:
        _tag = "_含ffmpeg"
    zip_path = os.path.join(DIST, NAME + "_v%s" % RELEASE_VERSION + _tag + ".zip")
    if os.path.isfile(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(stage):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, DIST)
                zf.write(full, rel)

    required = {
        os.path.join(NAME, "安装并启动.bat").replace("\\", "/"),
        os.path.join(NAME, "安装到指定目录.bat").replace("\\", "/"),
        os.path.join(NAME, "启动SmartCut.bat").replace("\\", "/"),
        os.path.join(NAME, "installer.py").replace("\\", "/"),
        os.path.join(NAME, "setup_windows.ps1").replace("\\", "/"),
        os.path.join(NAME, "window_guard.py").replace("\\", "/"),
        os.path.join(NAME, "assets", "icon.ico").replace("\\", "/"),
        os.path.join(NAME, "brains", "__init__.py").replace("\\", "/"),
        os.path.join(NAME, "brains", "audio_brain.py").replace("\\", "/"),
        os.path.join(NAME, "conf", "decision.json").replace("\\", "/"),
        os.path.join(NAME, "conf", "coords_matrix.json").replace("\\", "/"),
        os.path.join(NAME, "conf", "spec_manifest.json").replace("\\", "/"),
        os.path.join(NAME, "conf", "editor_model.json").replace("\\", "/"),
        os.path.join(NAME, "install_pr_bridge.ps1").replace("\\", "/"),
        os.path.join(NAME, "pr_bridge", "CSXS", "manifest.xml").replace("\\", "/"),
        os.path.join(NAME, "pr_bridge", "jsx", "host.jsx").replace("\\", "/"),
        os.path.join(NAME, "docs", "安装指南.md").replace("\\", "/"),
        os.path.join(NAME, "docs", "Premiere插件.md").replace("\\", "/"),
        os.path.join(NAME, "LICENSES", "manifest.json").replace("\\", "/"),
    }
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
    if with_app_bundle:
        required.update({
            os.path.join(NAME, "智能剪辑助手.exe").replace("\\", "/"),
            os.path.join(NAME, "_internal", "python314.dll").replace("\\", "/"),
            os.path.join(NAME, "_internal", "PySide6", "QtCore.pyd").replace("\\", "/"),
        })
    elif not (with_runtime or with_bin or with_models):
        unexpected_binary = sorted(
            name for name in names
            if os.path.splitext(name)[1].lower() in (".exe", ".dll", ".pyd", ".so", ".dylib")
        )
        if unexpected_binary:
            print("!! 默认源码包意外包含二进制：", unexpected_binary)
            quarantine_release_artifacts(
                {"stage": stage, "zip": zip_path}, reason="unexpected-binary", dist=DIST)
            return 6
    absent = sorted(required - names)
    if absent:
        print("!! 发布包缺少关键文件：", absent)
        quarantine_release_artifacts(
            {"stage": stage, "zip": zip_path}, reason="missing-files", dist=DIST)
        return 2
    checksum_path = write_sha256(zip_path)

    print("打包完成")
    print("  目录：%s" % stage)
    print("  压缩包：%s（%.2f MB）" % (zip_path, os.path.getsize(zip_path) / 1048576))
    print("  校验和：%s" % checksum_path)
    if with_app_bundle:
        print("  这个包已带成品程序；安装器仍会自动准备 FFmpeg、模型和 PR 插件。")
    elif with_runtime:
        print("  这台包自带 Python，接收方不需要装 Python/依赖。")
    else:
        print("  这是联网一键安装包；接收方不需要预先安装 Python。")
    print("  文件数：%d，未压缩 %.2f MB" % (len(INCLUDE), total / 1048576))
    print()
    print("收件人拿到后：")
    print("  · 想装到自选目录：双击「安装到指定目录.bat」→ 选目录 → 开始安装")
    print("  · 就在解压目录用： 双击「安装并启动.bat」")
    if with_bin:
        print("这个包已带 ffmpeg，装的时候只需下依赖（约 500 MB）和语音模型。")
    else:
        print("安装器会自动准备运行依赖、FFmpeg、所选语音模型和 PR 插件。")
    print("安装器会跟随软件设置下载一套语音模型，并只下载当前选择的一套本地语言模型。")
    print("新安装默认是高准确率 large-v3 语音模型与 8B 本地模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

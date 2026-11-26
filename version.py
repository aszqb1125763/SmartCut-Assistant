# -*- coding: utf-8 -*-
"""SmartCut 智能剪辑助手 · 版本备份与恢复

用法（在工具目录里执行）：

    python version.py backup "改了进度条和 GPU"     # 存一个新版本快照
    python version.py list                          # 看有哪些版本
    python version.py restore v003                  # 恢复到某个版本
    python version.py diff v002                     # 看当前文件和某版本差在哪

设计要点：
  · 只备份源码和脚本，不含 .venv / models / bin（那三样由安装器重建）
  · 只增不删，版本号自动递增，永不复用
  · 内容与上一版完全相同则跳过，不产生噪音快照
  · restore 之前会先把"当前状态"自动存一份，误操作也能回去
  · 快照目录结构 = 直接可以当程序目录用（restore 就是复制文件）

备份位置：应用目录旁的 _版本备份 文件夹（可由配置覆盖）
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time

from product_info import PRODUCT_NAME, VERSION

BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(os.path.dirname(BASE), "_版本备份")
MANIFEST = os.path.join(STORE, "版本清单.md")

# 备份哪些文件。make_release.py 里有一份同样的清单，优先复用它，
# 避免以后加了新模块两边不同步。
try:
    from make_release import INCLUDE as FILES
except Exception:  # noqa: BLE001
    FILES = [
        "cards_ui.py", "index.py", "export.py", "tagger.py",
        "_av_compat.py", "fetch_model.py", "predownload_models.py",
        "installer.py", "make_release.py", "version.py", "requirements.txt",
        "启动SmartCut.bat", "启动（排错用）.bat", "setup_windows.ps1",
        "product_info.py", "README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md",
        "RELEASE_CHECKLIST.md", "requirements-dev.txt",
        "autocut.py", "dedupe.py", "bgm.py", "build_kb.py",
        "completeness.py", "director.py", "learn.py", "llm_config.py",
        "models.py", "proofread.py", "script_writer.py",
        "selftest_track.py", "semantic.py", "setup_llm.py",
        "subtitles.py", "termbase.py",
    ]

SKIP_IF_MISSING = {"make_release.py", "version.py"}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# 开发用脚本：要进版本快照，但不该进发给别人的压缩包
DEV_EXTRA = ["make_release.py", "dev_finish.py", "selftest_track.py",
             "selftest_cut.py"]
# 子目录里的文件（配置），备份时保留目录结构
NESTED = ["conf/autocut.json", "conf/terms.txt", "conf/settings.json",
         "conf/subtitle_style.json", "conf/editor_model.json",
         "conf/bgm.json", "conf/cut_value.json"] + \
         ["kb/" + f for f in sorted(__import__("os").listdir(os.path.join(BASE, "kb")))
          if f.endswith(".txt")] if __import__("os").path.isdir(os.path.join(BASE, "kb")) else ["conf/autocut.json", "conf/terms.txt"]
for _folder in ("docs", "ui", "brains", "pr_bridge", "build_tools"):
    for _root, _dirs, _files in os.walk(os.path.join(BASE, _folder)):
        _dirs[:] = [d for d in _dirs if d not in ("__pycache__", "build")]
        NESTED.extend(os.path.relpath(os.path.join(_root, f), BASE)
                      for f in _files if not f.endswith((".pyc", ".pyo", ".spec")))


def present_files():
    allf = list(dict.fromkeys(list(FILES) + DEV_EXTRA + NESTED))
    return [f for f in allf if os.path.isfile(os.path.join(BASE, f))]


def fingerprint(files):
    """整个版本的指纹：文件内容 hash 的排序拼接。"""
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.encode("utf-8"))
        h.update(sha(os.path.join(BASE, f)).encode("ascii"))
    return h.hexdigest()


def snapshots():
    if not os.path.isdir(STORE):
        return []
    out = []
    for name in sorted(os.listdir(STORE)):
        p = os.path.join(STORE, name)
        if os.path.isdir(p) and re.match(r"^v\d{3}_", name):
            meta = os.path.join(p, "_VERSION.json")
            info = {}
            if os.path.isfile(meta):
                try:
                    info = json.load(open(meta, encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    pass
            out.append((name, p, info))
    return out


def next_id():
    ids = [int(n[1:4]) for n, _, _ in snapshots()]
    return "v%03d" % (max(ids) + 1 if ids else 1)


def slug(text, limit=24):
    s = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", text.strip())
    return (s[:limit] or "未命名").strip("_")


def append_manifest(line):
    new = not os.path.isfile(MANIFEST)
    with open(MANIFEST, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# SmartCut 智能剪辑助手 · 版本清单\n\n"
                     "> 只增不删；内容相同的版本会被跳过。\n\n"
                     "| 版本 | 时间 | 说明 | 文件数 |\n"
                     "|------|------|------|--------|\n")
        fh.write(line + "\n")


def do_backup(label, quiet=False):
    os.makedirs(STORE, exist_ok=True)
    files = present_files()
    if not files:
        print("错误：当前目录里没找到可备份的文件。")
        return None

    fp = fingerprint(files)
    snaps = snapshots()
    if snaps:
        last_name, last_path, last_info = snaps[-1]
        if last_info.get("fingerprint") == fp:
            if not quiet:
                print("内容与 %s 完全相同，跳过（不产生重复快照）。" % last_name)
            return last_name

    vid = next_id()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    folder = "%s_%s_%s" % (vid, time.strftime("%Y-%m-%d"), slug(label))
    dest = os.path.join(STORE, folder)
    os.makedirs(dest, exist_ok=True)

    hashes = {}
    for f in files:
        src = os.path.join(BASE, f)
        d = os.path.join(dest, f)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(src, d)
        hashes[f] = sha(src)

    meta = {
        "version": vid,
        "created": stamp,
        "label": label,
        "fingerprint": fp,
        "files": hashes,
        "source": BASE,
    }
    json.dump(meta, open(os.path.join(dest, "_VERSION.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    append_manifest("| %s | %s | %s | %d |" % (vid, stamp, label.replace("|", "／"), len(files)))
    if not quiet:
        print("已备份 %s：%s" % (vid, label))
        print("  目录：%s" % dest)
        print("  文件：%d 个（不含 .venv / models / bin）" % len(files))
        print("  恢复：python version.py restore %s" % vid)
    return vid


def do_list():
    snaps = snapshots()
    if not snaps:
        print("还没有任何备份。先跑：python version.py backup \"说明\"")
        return
    print("%-6s %-20s %-28s %s" % ("版本", "时间", "说明", "文件数"))
    print("-" * 78)
    for name, path, info in snaps:
        print("%-6s %-20s %-28s %s"
              % (info.get("version", name[:4]),
                 info.get("created", "?")[:19],
                 (info.get("label", "") or "")[:26],
                 len(info.get("files", {}))))
    print()
    print("备份目录：%s" % STORE)


def do_diff(target):
    snaps = {info.get("version") or name[:4]: (name, p, info)
             for name, p, info in snapshots()}
    if target not in snaps:
        print("没有这个版本：%s" % target)
        return 1
    name, path, info = snaps[target]
    old = info.get("files", {})
    cur = present_files()
    cur_set = set(cur)
    changed, added, removed = [], [], []
    for f in sorted(old):
        if f not in cur_set:
            removed.append(f)
        elif sha(os.path.join(BASE, f)) != old[f]:
            changed.append(f)
    for f in sorted(cur_set - set(old)):
        added.append(f)
    print("与 %s（%s）相比：" % (target, info.get("label", "")))
    print("  改动：%s" % (", ".join(changed) or "无"))
    print("  新增：%s" % (", ".join(added) or "无"))
    print("  缺失：%s" % (", ".join(removed) or "无"))
    if not (changed or added or removed):
        print("  —— 当前内容与该版本完全一致")
    return 0


def do_restore(target, dest=None, yes=False):
    snaps = {info.get("version") or name[:4]: (name, p, info)
             for name, p, info in snapshots()}
    if target not in snaps:
        print("没有这个版本：%s" % target)
        print("用 python version.py list 看可用的版本")
        return 1
    name, path, info = snaps[target]
    dest = dest or BASE

    print("准备把 %s（%s）恢复到：" % (target, info.get("label", "")))
    print("  %s" % dest)
    print("  文件 %d 个" % len(info.get("files", {})))

    if dest == BASE and not yes:
        ans = input("当前内容会被覆盖（.venv / models / bin 不受影响）。继续？[y/N] ").strip().lower()
        if ans not in ("y", "yes", "是"):
            print("已取消。")
            return 1

    # 恢复前先把当前状态存一份，误操作也能回去
    if dest == BASE:
        safe = do_backup("restore %s 之前的自动快照" % target, quiet=True)
        if safe:
            print("  已先把当前状态存为 %s" % safe)

    missing = []
    for f in info.get("files", {}):
        src = os.path.join(path, f)
        if not os.path.isfile(src):
            missing.append(f)
            continue
        d = os.path.join(dest, f)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(src, d)
    if missing:
        print("  注意：这些文件在快照里没有，跳过：%s" % ", ".join(missing))

    extra = sorted(set(present_files()) - set(info.get("files", {})))
    if extra:
        print("  提示：当前目录还有快照里没有的文件，未删除：%s" % ", ".join(extra))
    print("恢复完成。请重启%s使改动生效。" % PRODUCT_NAME)
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == "backup":
        label = " ".join(argv[2:]).strip() or "未命名改动"
        do_backup(label)
        return 0
    if cmd == "list":
        do_list()
        return 0
    if cmd == "diff":
        return do_diff(argv[2]) if len(argv) > 2 else 1
    if cmd == "restore":
        if len(argv) < 3:
            print("用法：python version.py restore v003 [--to 目标目录] [--yes]")
            return 1
        dest = None
        yes = "--yes" in argv
        if "--to" in argv:
            dest = argv[argv.index("--to") + 1]
        return do_restore(argv[2], dest=dest, yes=yes)
    print("不认识的命令：%s" % cmd)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

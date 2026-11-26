# -*- coding: utf-8 -*-
"""打包入口：给 PyInstaller 用。

只做三件事：设应用标识、装"子进程静默"补丁、调 cards_ui.main()。
不在这里写业务逻辑，避免和源码版本分叉。
"""
import os
import sys


def _start_pr_bridge_service() -> None:
    """Start the local Premiere bridge in-process when it is not already alive."""
    try:
        import urllib.request
        import sync_server
        session = sync_server.load_bridge_session()
        if not session:
            raise RuntimeError("no active bridge session")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/health" % session["port"],
            headers={sync_server.BRIDGE_HEADER: session["token"]})
        with urllib.request.urlopen(request, timeout=0.25) as response:
            if getattr(response, "status", 200) == 200:
                return
    except Exception:
        pass

    def serve():
        try:
            import sync_server
            sync_server.main()
        except OSError:
            # Another SmartCut instance may have won the port race.
            pass
        except Exception:
            pass

    import threading
    threading.Thread(target=serve, name="premiere-bridge", daemon=True).start()


def _root() -> str:
    # 打包后是 exe 所在目录；开发时是源码目录
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _script_arg_index(argv):
    """返回脚本参数的位置，兼容新旧两种后台调用格式。

    正式格式是 ``--run-script script.py``。旧代码曾把打包后的 GUI exe
    当 Python 用，生成 ``exe -u script.py`` / ``exe -X utf8 script.py``；
    如果不在入口处识别，那个子进程就会再次创建完整的 Qt 主窗口。
    """
    if "--run-script" in argv:
        i = argv.index("--run-script")
        return i + 1

    i = 0
    no_value = {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO"}
    with_value = {"-X", "-W"}
    while i < len(argv):
        arg = str(argv[i])
        if arg in no_value:
            i += 1
            continue
        if arg in with_value:
            i += 2
            continue
        if arg.startswith("-X") or arg.startswith("-W"):
            i += 1
            continue
        break
    if i < len(argv) and str(argv[i]).lower().endswith(".py"):
        return i
    return None


def run_script(argv):
    """支持 `--run-script`，并兜底旧式 `-u 脚本.py` 调用。

    打包版没有 python.exe，但 index.py / fetch_model.py / proofread.py 这些
    后台脚本还得跑。以前拿 exe 当 python 用，等于每跑一次就再开一个程序
    窗口（一闪一闪），还会卡住等那个窗口退出。现在同一个 exe 里直接跑脚本，
    不建界面、不开窗口，跑完就退。
    """
    i = _script_arg_index(argv)
    if i is None:
        return None
    if i >= len(argv):
        print("用法：智能剪辑助手.exe --run-script 脚本.py [参数…]")
        return 2
    script = os.path.abspath(argv[i])
    if not os.path.isfile(script):
        print("找不到脚本：%s" % script)
        return 2
    root = _root()
    os.chdir(os.path.dirname(script) or root)
    os.environ.setdefault("SMARTCUT_HOME", root)
    for p in (os.path.dirname(script), root):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    sys.argv = [script] + argv[i + 1:]
    import runpy
    runpy.run_path(script, run_name="__main__")
    return 0


def _safe_stdio():
    """打包成 --windowed 后 sys.stdout / sys.stderr 是 None。

    任何 `print(...)` 或 `sys.stdout.write(...)` 都会抛
    `'NoneType' object has no attribute 'write'`。实测 index.py 的语义校对
    因此整段被跳过（logs\\index_20261124_205849.log）。入口处统一兜底，
    各模块不用逐个判空。
    """
    handle = None
    if sys.stdout is None:
        handle = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = handle
    if sys.stderr is None:
        if handle is None:
            handle = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = handle
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace",
                        write_through=True)
        except (OSError, ValueError):
            # 测试替身、已关闭流或不支持重配置的宿主流保持原样。
            pass


def main() -> int:
    _safe_stdio()
    # 后台脚本也会继续调用 ffmpeg / ffprobe / llama-server；必须在分流前
    # 安装，否则 GUI 不弹了，黑色控制台窗口仍会随着每个子进程反复闪现。
    try:
        import no_console
        no_console.install()
    except Exception:
        pass

    rc = run_script(sys.argv[1:])
    if rc is not None:
        return rc
    root = _root()
    os.chdir(root)
    os.environ.setdefault("SMARTCUT_HOME", root)
    if root not in sys.path:
        sys.path.insert(0, root)

    _start_pr_bridge_service()
    import cards_ui
    return cards_ui.main() or 0


if __name__ == "__main__":
    sys.exit(main())

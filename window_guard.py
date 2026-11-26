# -*- coding: utf-8 -*-
"""Contain and diagnose stray top-level windows during background work.

The primary fixes are correct widget parenting and windowless subprocess flags.
This module is a narrow last line of defence for the Windows build: while an
analysis/render worker is active, it hides only small ownerless windows that
belong to this process tree and exactly use the application title.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time

from product_info import PRODUCT_NAME

APP_TITLE = PRODUCT_NAME
TH32CS_SNAPPROCESS = 0x00000002
GW_OWNER = 4
GWL_STYLE = -16
GWL_EXSTYLE = -20
SW_HIDE = 0
MIN_SMALL_WIDTH = 120
MAX_SMALL_WIDTH = 600
MIN_SMALL_HEIGHT = 20
MAX_SMALL_HEIGHT = 160

_seen = set()


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _winapi():
    """Return Win32 libraries with 64-bit-safe signatures."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                      ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                     ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND,
                                     ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                  wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return user32, kernel32, get_long


def _parent_map(kernel32):
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid:
        return {}
    out = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def _belongs_to_app(pid, app_pid, parents):
    current = int(pid or 0)
    visited = set()
    while current and current not in visited:
        if current == int(app_pid):
            return True
        visited.add(current)
        current = int(parents.get(current, 0) or 0)
    return False


def _should_hide(title, class_name, width, height, owner, belongs):
    """Return a diagnostic reason, or an empty string for a valid window."""
    title = str(title or "").strip()
    class_name = str(class_name or "")
    width, height = int(width), int(height)
    if not belongs:
        return ""
    if class_name == "ConsoleWindowClass":
        return "descendant_console"
    small = (MIN_SMALL_WIDTH <= width <= MAX_SMALL_WIDTH and
             MIN_SMALL_HEIGHT <= height <= MAX_SMALL_HEIGHT)
    if not small or title != APP_TITLE or int(owner or 0) != 0:
        return ""
    if not (class_name.startswith("Qt") or class_name == "Ghost"):
        return ""
    return "stray_app_titlebar"


def _log(message):
    try:
        home = os.environ.get("SMARTCUT_HOME") or os.path.dirname(
            os.path.abspath(__file__))
        folder = os.path.join(home, "logs")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "window_guard.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def scan_once(main_hwnd=0):
    """Hide matching visible windows once and return the hidden count."""
    if os.name != "nt":
        return 0
    user32, kernel32, get_long = _winapi()
    app_pid = os.getpid()
    rows = []
    enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                   wintypes.LPARAM)

    @enum_type
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or int(hwnd) == int(main_hwnd or 0):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(max(2, length + 1))
        user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, len(class_buf))
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        rows.append({
            "hwnd": int(hwnd), "pid": int(pid.value),
            "title": title_buf.value, "class": class_buf.value,
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
            "left": int(rect.left), "top": int(rect.top),
            "owner": int(user32.GetWindow(hwnd, GW_OWNER) or 0),
        })
        return True

    user32.EnumWindows(visit, 0)
    external = any(row["pid"] != app_pid and
                   (row["class"] == "ConsoleWindowClass" or
                    row["title"].strip() == APP_TITLE) for row in rows)
    parents = _parent_map(kernel32) if external else {}
    hidden = []
    for row in rows:
        belongs = (row["pid"] == app_pid or
                   _belongs_to_app(row["pid"], app_pid, parents))
        reason = _should_hide(row["title"], row["class"], row["width"],
                              row["height"], row["owner"], belongs)
        if not reason:
            continue
        hwnd = row["hwnd"]
        style = int(get_long(hwnd, GWL_STYLE))
        exstyle = int(get_long(hwnd, GWL_EXSTYLE))
        user32.ShowWindow(hwnd, SW_HIDE)
        key = (hwnd, row["pid"], row["title"], row["class"], reason)
        if key not in _seen:
            _seen.add(key)
            hidden.append(
                "%s hwnd=0x%X pid=%d class=%r title=%r rect=(%d,%d,%dx%d) "
                "owner=0x%X style=0x%X exstyle=0x%X" %
                (reason, hwnd, row["pid"], row["class"], row["title"],
                 row["left"], row["top"], row["width"], row["height"],
                 row["owner"], style, exstyle))
    for line in hidden:
        _log("隐藏异常窗口：" + line)
    return len(hidden)


def install_qt_timer(owner, is_busy, interval_ms=40):
    """Install a lightweight scan timer owned by the main Qt window."""
    from PySide6.QtCore import QTimer

    timer = QTimer(owner)
    timer.setInterval(max(20, int(interval_ms)))

    def tick():
        try:
            if is_busy():
                scan_once(main_hwnd=int(owner.winId()))
        except RuntimeError:
            timer.stop()
        except Exception as exc:  # noqa: BLE001
            _log("守卫扫描失败：%s: %s" % (type(exc).__name__, exc))

    timer.timeout.connect(tick)
    timer.start()
    _log("窗口守卫已安装 pid=%d interval=%dms（仅后台任务运行时扫描）" %
         (os.getpid(), timer.interval()))
    return timer

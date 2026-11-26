# -*- coding: utf-8 -*-
"""向 Premiere Pro 发送一次真实按键（模拟人操作，绕过脚本接口限制）。

验证用：先在 PR 里手动设好入点(I)、出点(O)，再跑本脚本，
它会把「提取」快捷键（默认 '）发给 PR，成功的话入点~出点之间的内容
会被删掉、后面自动前移。

用法：
    .venv\\Scripts\\python.exe press_key.py              # 发送「提取」(')
    .venv\\Scripts\\python.exe press_key.py AddEdit     # 发送 Ctrl+K（添加编辑/切开）
"""
import subprocess
import sys
import time

# PR 默认快捷键（可在 PR 的「编辑 → 键盘快捷键」里核对）
ACTIONS = {
    "extract": "'",             # 提取（Extract）：删入点~出点，后面补上
    "addedit": "^k",            # 添加编辑（Add Edit）：在播放头切一刀
    "rippledelete": "+{DELETE}",  # 波纹删除（Shift+Delete）
    "delete": "{DELETE}",
}


def activate(title_keyword="Adobe Premiere"):
    """把 PR 主窗口调到前台。"""
    ps = (
        "$wshell = New-Object -ComObject wscript.shell; "
        "$null = $wshell.AppActivate('%s'); "
        "Start-Sleep -Milliseconds 250" % title_keyword
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True)


def send(keys):
    """用 SendKeys 发送按键序列。"""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.SendKeys]::SendWait('%s')" % keys.replace("'", "''")
    )
    done = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True, text=True)
    return done.returncode == 0, (done.stderr or "").strip()[:200]


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "extract").lower()
    keys = ACTIONS.get(action)
    if not keys:
        print("不认识的动作：%s（可选：%s）" % (action, ", ".join(ACTIONS)))
        return 1
    print("激活 PR 窗口…")
    activate()
    time.sleep(0.3)
    print("发送按键：%s（%r）" % (action, keys))
    ok, err = send(keys)
    if ok:
        print("已发送。回 PR 看时间轴有没有变化。")
        return 0
    print("发送失败：%s" % err)
    return 1


if __name__ == "__main__":
    sys.exit(main())

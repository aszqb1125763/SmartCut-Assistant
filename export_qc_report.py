# -*- coding: utf-8 -*-
"""把最新审片数据导成人能看的报告（Markdown）。

子枫 2026-11-25：
    「导演不是会审片吗，把最后审片数据打印一下，方便我后期自己更改。」
所以这份报告按「成片时间点 → 是什么问题 → 原片哪里 → 怎么改」组织，
不按程序内部字段罗列。

用法：
    .venv\\Scripts\\python.exe export_qc_report.py            # 最新一份
    .venv\\Scripts\\python.exe export_qc_report.py --all      # 全部历史
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "工作目录", "索引")

TYPE_NAMES = {
    "render_word_boundary_cut": "切在词中间（半句/半字）",
    "render_half_sentence": "接缝落在句子中间",
    "render_tail_cut": "尾音被切断",
    "render_sudden_jump": "声音突兀跳变",
    "render_spike": "突兀尖峰",
    "render_gap": "声音断崖/空白",
    "hard_visual_jump": "画面硬跳",
    "static_ui_like": "画面静止像 UI",
    "render_energy_cliff": "能量断崖",
    "render_loudness": "响度异常",
}

ACTION_NAMES = {
    "rebuild_source_boundary": "把切口挪到词边界外",
    "rebuild_sentence_boundary": "把接缝扩到完整句",
    "inspect_transition": "检查这段转场是否合理",
    "inspect": "人工看一眼",
    "restore_tail": "补回尾音",
}


def _latest_report():
    pattern = os.path.join(WORK, "**", "rendered_output_qc.json")
    files = sorted(glob.glob(pattern, recursive=True),
                   key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def _source_from_index(report_path):
    """从报告所在索引目录反查素材名。

    报告路径形如：索引\\<素材名>\\rendered_output_qc\\<hash>\\rendered_output_qc.json
    所以往上数三层才是索引目录（之前少算一层，素材名显示"未知"）。
    """
    index_dir = os.path.dirname(os.path.dirname(os.path.dirname(report_path)))
    path = os.path.join(index_dir, "index.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return os.path.basename(str(data.get("source") or "")), index_dir
    except (OSError, ValueError):
        return "", index_dir


def _fmt_interval(item):
    iv = item.get("suggested_interval")
    if not iv or len(iv) < 2:
        return "—"
    try:
        a, b = float(iv[0]), float(iv[1])
    except (TypeError, ValueError):
        return "—"
    return "%.2f 秒（%.2f~%.2f）" % ((a + b) / 2, a, b)


def _evidence(item):
    refs = item.get("evidence_refs") or []
    out = []
    for ref in refs:
        text = str(ref)
        if text.startswith("source:"):
            out.append("原片 " + text[7:])
        elif text.startswith("segment:"):
            out.append("句子 " + text[8:])
        elif text.startswith("visual:"):
            out.append("画面节点 " + text[7:])
        else:
            out.append(text)
    return "；".join(out) or "—"


def build_report(report_path, out_path):
    with open(report_path, encoding="utf-8") as handle:
        data = json.load(handle)
    source_name, index_dir = _source_from_index(report_path)
    findings = data.get("findings") or []

    order = {"critical": 0, "major": 1, "minor": 2}
    findings = sorted(findings, key=lambda f: (
        order.get(str(f.get("severity")), 3),
        (f.get("suggested_interval") or [0])[0]))

    lines = []
    lines.append("# 审片报告")
    lines.append("")
    lines.append("- 素材：%s" % (source_name or "未知"))
    lines.append("- 报告时间：%s" % time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(report_path))))
    lines.append("- 复审得分：**%s**" % data.get("score"))
    lines.append("- 否决（critical）：%s ｜ 重大问题（major）：%s"
                 % (data.get("veto_count"), data.get("major_issues")))
    passed = data.get("passed")
    lines.append("- 结论：%s" % ("通过" if passed else
                                 ("按降级交付（成片照出，问题已标记）"
                                  if data.get("degraded_delivered") else "未通过")))
    lines.append("")

    if not findings:
        lines.append("没有发现问题。")
    else:
        lines.append("## 问题清单（按严重程度排序）")
        lines.append("")
        lines.append("| # | 级别 | 成片位置 | 问题 | 原片来源 | 怎么改 |")
        lines.append("|---|---|---|---|---|---|")
        for number, item in enumerate(findings, 1):
            lines.append("| %d | %s | %s | %s | %s | %s |" % (
                number,
                str(item.get("severity") or ""),
                _fmt_interval(item),
                TYPE_NAMES.get(str(item.get("type")), str(item.get("type"))),
                _evidence(item),
                ACTION_NAMES.get(str(item.get("action")),
                                 str(item.get("action") or "—")),
            ))
        lines.append("")
        lines.append("## 逐条说明")
        lines.append("")
        for number, item in enumerate(findings, 1):
            lines.append("### %d. [%s] %s" % (
                number, str(item.get("severity") or ""),
                TYPE_NAMES.get(str(item.get("type")), str(item.get("type")))))
            lines.append("")
            lines.append("- 成片位置：%s" % _fmt_interval(item))
            lines.append("- 原片来源：%s" % _evidence(item))
            lines.append("- 系统说明：%s" % (item.get("message") or ""))
            lines.append("- 建议动作：%s" % ACTION_NAMES.get(
                str(item.get("action")), str(item.get("action") or "—")))
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("改法提示：")
    lines.append("- 半句/半字类：在 PR 里把接缝往两边挪 0.1~0.3 秒，避开词和句子中间；")
    lines.append("- 画面硬跳：确认是不是合理跳切，不合理就给这段加转场；")
    lines.append("- 静止像 UI：这类不影响出片，只是观感，可自行忽略。")
    lines.append("")

    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return out_path


def main():
    if "--all" in sys.argv:
        pattern = os.path.join(WORK, "**", "rendered_output_qc.json")
        files = sorted(glob.glob(pattern, recursive=True),
                       key=os.path.getmtime, reverse=True)
        if not files:
            print("还没有审片数据。")
            return 1
        for path in files:
            name = os.path.basename(os.path.dirname(path))
            out = os.path.join(BASE, "工作目录",
                               "审片报告_%s.md" % name[:8])
            print(build_report(path, out))
        return 0

    latest = _latest_report()
    if not latest:
        print("还没有审片数据。")
        return 1
    stamp = time.strftime("%Y%m%d_%H%M")
    out = os.path.join(BASE, "工作目录", "审片报告_%s.md" % stamp)
    build_report(latest, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

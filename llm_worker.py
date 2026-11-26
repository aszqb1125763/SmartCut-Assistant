# -*- coding: utf-8 -*-
"""本地模型工作机：把"读台词/拆结构/打标签/写总结"这类活交给本地模型跑。

为什么不自己干：这些活是大量重复的文本推理，本地模型不要钱、跑得也不慢
（4B 约 0.06 秒/次），我只需要看它交回来的结果。

用法：
    python llm_worker.py flow   <索引目录>   # 拆这期的实况结构
    python llm_worker.py stage  <索引目录>   # 给每句打阶段标签
    python llm_worker.py test                # 自检（不碰索引）

产物：
    <索引目录>\\结构拆解.json     （flow）
    <索引目录>\\阶段标签.json     （stage）
"""
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

STAGES = ("hook", "goal", "progress", "result", "tease", "other")
STAGE_CN = {"hook": "开场招呼", "goal": "本集目标", "progress": "过程推进",
            "result": "结果/成果", "tease": "下期预告", "other": "其他"}


def load_segs(index_dir):
    with open(os.path.join(index_dir, "index.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    return data, [(float(s[0]), float(s[1]), str(s[2] or ""))
                  for s in data.get("segments") or []]


def fmt_lines(segs, start, end, width=1):
    out = []
    for a, b, t in segs:
        if start <= a < end:
            out.append("%.1f  %s" % (a, t))
    return "\n".join(out)


def job_flow(index_dir, on_log=print):
    """把整期拆成"开场 → 目标 → 阶段 → 结果 → 结尾"，带时间码。"""
    import proofread as pf
    data, segs = load_segs(index_dir)
    dur = float(data.get("duration") or 0)
    sys_p = "你是资深游戏实况剪辑指导，负责把一整期录播拆成成片结构。"
    prompt = """下面是主播一整期录播的台词（带秒级时间码，共 %.0f 分钟）。

请把它拆成 5~8 个"成片结构段"，每段给：起止秒数、类型、一句话概括（不超过 20 字）。
类型只能用这五个：hook(开场招呼)、goal(本集目标)、progress(过程推进)、result(结果成果)、tease(下期预告)。

要求：
· 必须覆盖全片，段与段首尾相接
· progress 段按"做了什么"分，不要按时间硬切
· 概括要具体（做什么机器/打什么怪/拿到什么），不要写"继续推进"这种空话

只输出 JSON 数组：
[{"start": 秒, "end": 秒, "type": "hook", "summary": "..."}]

台词：
%s""" % (dur / 60, fmt_lines(segs, 0, dur + 1))
    out = []
    t0 = time.time()
    with pf.Server(task="segment", port=8213, ctx=16384) as srv:
        # 台词太长就分段喂（8 分钟一段），再拼接
        step = 480.0
        for start in range(0, int(dur) + 1, int(step)):
            part = "%s" % prompt.replace(fmt_lines(segs, 0, dur + 1),
                                         fmt_lines(segs, start, start + step))
            raw = srv.chat(sys_p, part, max_tokens=900)
            m = re.search(r"\[.*\]", raw or "", re.S)
            if not m:
                continue
            try:
                arr = json.loads(m.group(0))
            except ValueError:
                continue
            for it in arr:
                try:
                    out.append({"start": float(it["start"]), "end": float(it["end"]),
                                "type": str(it.get("type", "other")),
                                "summary": str(it.get("summary", ""))[:30]})
                except (KeyError, TypeError, ValueError):
                    continue
    out.sort(key=lambda x: x["start"])
    path = os.path.join(index_dir, "结构拆解.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    on_log("结构拆解：%d 段，用时 %.1f 秒 → %s" % (len(out), time.time() - t0, path))
    for it in out:
        on_log("  %6.1f~%6.1f  %-8s %s" % (it["start"], it["end"],
                                           STAGE_CN.get(it["type"], it["type"]),
                                           it["summary"]))
    return out


def job_stage(index_dir, batch=40, on_log=print):
    """给每句打阶段标签，用于"开头/结尾必须保留"这类保护。"""
    import proofread as pf
    data, segs = load_segs(index_dir)
    sys_p = "你是剪辑助理，给游戏实况的每句台词打阶段标签。"
    prompt = """给下面每句台词打一个阶段标签：
hook 开场招呼 / goal 本集目标 / progress 过程推进 / result 结果成果 / tease 下期预告 / other 其他

只输出 JSON 数组，每项 {"i": 序号, "s": "标签"}，一句都不能漏。

台词：
%s"""
    labels = [""] * len(segs)
    t0 = time.time()
    with pf.Server(task="segment", port=8215, ctx=8192) as srv:
        for start in range(0, len(segs), batch):
            chunk = segs[start:start + batch]
            body = "\n".join("%d. %s" % (k, t) for k, (_a, _b, t) in enumerate(chunk))
            raw = srv.chat(sys_p, prompt % body, max_tokens=1200)
            m = re.search(r"\[.*\]", raw or "", re.S)
            if not m:
                continue
            try:
                arr = json.loads(m.group(0))
            except ValueError:
                continue
            for it in arr:
                try:
                    k = int(it["i"])
                    s = str(it["s"]).strip().lower()
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= k < len(chunk) and s in STAGES:
                    labels[start + k] = s
    hit = sum(1 for x in labels if x)
    path = os.path.join(index_dir, "阶段标签.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(labels, fh, ensure_ascii=False)
    cnt = {}
    for x in labels:
        cnt[x or "?"] = cnt.get(x or "?", 0) + 1
    on_log("阶段标签：%d/%d 句，用时 %.1f 秒，分布 %s"
           % (hit, len(labels), time.time() - t0,
              {STAGE_CN.get(k, k): v for k, v in sorted(cnt.items())}))
    return labels


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    job = argv[1]
    if job == "flow":
        job_flow(argv[2] if len(argv) > 2 else os.path.join(BASE, "..", "索引"))
    elif job == "stage":
        job_stage(argv[2] if len(argv) > 2 else os.path.join(BASE, "..", "索引"))
    else:
        print("未知任务：", job)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

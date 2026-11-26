# -*- coding: utf-8 -*-
"""重复片段：规律检测 + 本地模型裁决（模型不要钱，就让它干活）。

两级：
  ① 规律（retake_pattern）：报菜名 → 跑偏 → 重报同一批物件 ⇒ 中间那段是白录
  ② 裁决（本模块）：把"疑似白录段"和"后面那遍"一起交给本地模型，
     问它"这两段是不是同一件事讲了两遍、哪遍更完整"，只保留判定为真的，
     避免规则误伤正常内容。

另外顺手处理"一句话说了两遍"（如 44.66"我们只要按任务" + 45.56"按照任务"），
这种句子级重复直接删短的。
"""
from __future__ import annotations

import json
import re
import sys

SYS = "你是剪辑助理，只判断两段台词是不是同一件事被讲了两遍。"

PROMPT = """主播连续录制时经常"说一半、跑偏、再重来一遍"。下面给你两段：

A（%.1f~%.1f 秒，共 %.0f 秒）：
%s

B（%.1f~%.1f 秒，开头部分）：
%s

问题：
1. A 和 B 是不是在讲同一件事（同一批物品/同一个操作/同一个开场）？
2. 如果是，A 是不是没讲完/跑偏了，B 更完整？

只输出 JSON：{"same": true/false, "a_abandoned": true/false, "why": "不超过12字"}
不要输出其他内容。"""


def _txt(data, a, b, limit=260):
    out = []
    for s in data.get("segments") or []:
        t = float(s[0])
        if a <= t <= b:
            out.append(str(s[2] or ""))
    s = "".join(out)
    return s[:limit]


def adjudicate(data, drops, srv=None, on_log=None):
    """对候选白录段做模型裁决，返回确定要删的段。"""
    kept = []
    close = False
    if srv is None:
        try:
            import proofread as pf
            srv = pf.Server(task="segment", port=8193, ctx=6144, api_role="director")
            srv.__enter__()
            close = True
        except Exception as exc:  # noqa: BLE001
            from llm_gateway import propagate_api_error
            propagate_api_error(exc)
            if on_log:
                on_log("重录裁决：模型不可用（%s），只用规律结果" % str(exc)[:40])
            return list(drops)
    try:
        for d in drops:
            a0, a1 = float(d["start"]), float(d["end"])
            b0 = float(d.get("keep_start", a1))
            body_a = _txt(data, a0, a1)
            body_b = _txt(data, b0, b0 + 45)
            if len(body_a) < 8 or len(body_b) < 8:
                kept.append(d)
                continue
            prompt = PROMPT % (a0, a1, a1 - a0, body_a, b0, b0 + 45, body_b)
            raw = ""
            try:
                raw = srv.chat(SYS, prompt, max_tokens=160)
            except Exception as exc:  # noqa: BLE001
                from llm_gateway import propagate_api_error
                propagate_api_error(exc)
                kept.append(d)
                continue
            m = re.search(r"\{.*\}", raw or "", re.S)
            ok = False
            why = ""
            if m:
                try:
                    obj = json.loads(m.group(0))
                    ok = bool(obj.get("same")) and bool(obj.get("a_abandoned", True))
                    why = str(obj.get("why", ""))[:20]
                except ValueError:
                    ok = False
            if on_log:
                on_log("  裁决 %.1f~%.1f（%.0f 秒）：%s %s"
                       % (a0, a1, a1 - a0, "确认白录" if ok else "不算重复，保留",
                          why))
            if ok:
                d = dict(d)
                d["why"] = (d.get("why", "") + "｜模型：" + (why or "同一件事讲了两遍"))[:80]
                kept.append(d)
    finally:
        if close:
            try:
                srv.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
    return kept


def drop_sentence_repeats(data, min_overlap=0.8, max_len=8):
    """删除"同一句立刻重复"的短句（只删后一句，且都很短）。"""
    segs = data.get("segments") or []
    out = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        ta = str(a[2] or "").strip()
        tb = str(b[2] or "").strip()
        if not ta or not tb or len(ta) > max_len or len(tb) > max_len:
            continue
        if float(b[0]) - float(a[1]) > 1.0:
            continue
        # 只认短句是长句的开头（口误重说），不做泛化的字符重合
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if len(short) < 2 or not long_.startswith(short):
            continue
        ov = 1.0
        if ov >= min_overlap:
            out.append({"start": float(b[0]), "end": float(b[1]),
                        "dur": float(b[1]) - float(b[0]),
                        "why": "口误重复：「%s」紧接着又说了「%s」" % (ta, tb)})
    return out

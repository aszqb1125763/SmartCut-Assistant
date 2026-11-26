# -*- coding: utf-8 -*-
"""语义分析：用本地大模型给逐句台词做「类型 + 段落」标注。

分工：
    Whisper 出时间轴（模型给不出毫秒边界，这是硬事实）
    本模块出语义（这句话是不是人话、到哪算一段）
    剪辑器按语义边界切

用法：
    python semantic.py <索引目录>          # 分析并写回 index.json
    python semantic.py <索引目录> --dry    # 只看结果不写
"""
import argparse
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import proofread as pf  # noqa: E402


class SemanticCancelled(RuntimeError):
    pass

SYSTEM = ("你是视频剪辑助手。给你一串编号台词，你要判断每条是什么，"
          "以及段落从哪里断开。只按格式输出，不要解释。")

PROMPT = """下面是 Minecraft 实况的逐句台词（自动识别的，可能有错字）。

给每一条判断两件事，输出格式固定为「编号|类型|段落」，一行一条：

类型只能选：
  speech = 正常说话。【重要】物品名、机器名、方块名、数字、专业词
           （例如"铁针""脉冲""七十七""硅岩""压缩机"）全都算 speech，
           哪怕只有一个词、听起来没头没尾，也算 speech。
  laugh  = 笑声（哈哈哈、嘿嘿、呵 这种连续拟声）
  noise  = 只有这些才算：咳嗽、清嗓、打喷嚏、叹气、纯杂音、
           完全不成词的乱码字（例如"啊哈嗯吧"这种堆在一起的语气字）
  filler = 单个语气词（嗯、啊、哦、呃、唉）—— 只限一个字

段落：如果这一条开始了一个新话题/新动作，第三列写 P；接着上文的写 -。
判断依据是内容转没转场（比如从"做机器"转到"去挖矿"就是 P）。
宁可少标 P，也不要乱标——段落切太碎，剪辑就会把话题截断。

示例输出：
1|speech|-
2|speech|-
3|noise|-
4|speech|P
5|filler|-

台词：
%s

只输出上面那种格式的行，共 %d 行，不要任何其它文字。"""


def parse(raw, n):
    """解析模型输出，返回 {序号: (类型, 是否段首)}。"""
    out = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\s*[|｜,，]\s*([a-zA-Z]+)\s*[|｜,，]\s*([Pp\-—]?)", line)
        if not m:
            continue
        idx = int(m.group(1))
        kind = m.group(2).lower()
        if kind not in ("speech", "laugh", "noise", "filler"):
            kind = "speech"
        if 1 <= idx <= n and idx not in out:
            out[idx] = (kind, m.group(3).upper() == "P")
    return out


def analyze(segs, batch=30, on_log=print, should_stop=None):
    """返回和 segs 等长的 [(kind, para_break)]。"""
    n = len(segs)
    result = [("speech", False)] * n
    if not n:
        return result
    t0 = time.time()
    ok = 0
    if should_stop and should_stop():
        raise SemanticCancelled("已取消语义分析")
    with pf.Server() as srv:
        for start in range(0, n, batch):
            if should_stop and should_stop():
                raise SemanticCancelled("已取消语义分析")
            chunk = list(range(start, min(start + batch, n)))
            lines = "\n".join("%d. %s" % (i - start + 1, segs[i][2]) for i in chunk)
            try:
                raw = srv.chat(SYSTEM, PROMPT % (lines, len(chunk)), max_tokens=600)
            except Exception as exc:  # noqa: BLE001
                from llm_gateway import propagate_api_error
                propagate_api_error(exc)
                on_log("  第 %d 批失败：%s" % (start // batch + 1, exc))
                continue
            if should_stop and should_stop():
                raise SemanticCancelled("已取消语义分析")
            got = parse(raw, len(chunk))
            for k, (kind, para) in got.items():
                result[chunk[k - 1]] = (kind, para)
            ok += len(got)
            on_log("  进度 %d/%d（解析到 %d 条）" % (min(start + batch, n), n, ok))
    on_log("  语义分析完成：%d/%d 条，用时 %.0f 秒" % (ok, n, time.time() - t0))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("index", nargs="?")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--batch", type=int, default=30)
    args = ap.parse_args(argv)
    if not args.index:
        ap.print_help()
        return 1

    p = args.index
    if os.path.isdir(p):
        p = os.path.join(p, "index.json")
    data = json.load(open(p, encoding="utf-8"))
    segs = data.get("segments") or []
    print("逐句台词：%d 条" % len(segs))
    if not segs:
        print("这份索引没有逐句数据，先重新索引。")
        return 1

    res = analyze(segs, batch=args.batch)

    from collections import Counter
    cnt = Counter(k for k, _ in res)
    paras = sum(1 for _, pb in res if pb)
    print("类型分布：%s" % dict(cnt))
    print("段落数：%d" % (paras + 1))

    print("\n被判为非说话的：")
    shown = 0
    for i, (k, _) in enumerate(res):
        if k in ("noise", "filler", "laugh") and shown < 8:
            print("   %7.1f~%7.1f  [%s] %s" % (segs[i][0], segs[i][1], k, segs[i][2][:24]))
            shown += 1

    if args.dry:
        print("\n（--dry，没写回）")
        return 0
    data["semantic"] = [{"kind": k, "para_break": pb} for k, pb in res]
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n已写回：%s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

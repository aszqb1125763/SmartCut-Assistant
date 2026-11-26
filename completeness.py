# -*- coding: utf-8 -*-
"""判断"这句话说完了没有"，让剪辑点别切在半句话上。

为什么需要：Whisper 的句子边界经常落在句中（遇到换气、停顿就断），
我们直接拿它当切点，就会出现"一句话没说完就被切断"。

四层判据（不需要下载新模型）：
  1. 标点：句号/问号/感叹号结尾 → 说完了；逗号/顿号 → 大概没说完
  2. 尾字：了/吧/呢/啊/嘛 这类收尾语气词 → 更像说完
  3. 尾巴词：然后/就是/因为/所以/但是 → 明显还要接下去
  4. 下一句开头：以"然后/就是/而且/所以"起头 → 这句多半没说完
"""
import re

END_PUNCT = "。！？…!?"
MID_PUNCT = "，、；：,;:"
TAIL_OK = "了吧呢啊嘛呀哦哈啦的"
TAIL_BAD = ("然后", "就是", "那个", "这个", "因为", "所以", "但是", "而且",
            "的话", "如果", "虽然", "或者说", "比如说", "等于", "也就是")
HEAD_BAD = ("然后", "就是", "而且", "所以", "但是", "因为", "接着", "之后",
            "还有", "另外", "对", "嗯", "那")


def score_sentence(text, nxt_text=""):
    """给一句打「完整度」0~1，越高越像说完了。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    s = 0.35
    core = t.rstrip(END_PUNCT + MID_PUNCT + "」』”’")
    if t and t[-1] in END_PUNCT:
        s += 0.40
    elif t and t[-1] in MID_PUNCT:
        s += 0.05
    else:
        s += 0.15
    if core and core[-1] in TAIL_OK:
        s += 0.12
    for w in TAIL_BAD:
        if core.endswith(w):
            s -= 0.30
            break
    n2 = (nxt_text or "").strip()
    for w in HEAD_BAD:
        if n2.startswith(w):
            s -= 0.18
            break
    # 太短的句子多半是半句话
    if len(core) <= 4:
        s -= 0.10
    return max(0.0, min(1.0, s))


def compute(data, threshold=0.45):
    """给整片每句算完整度，返回 (scores, incomplete_flags)。"""
    segs = data.get("segments") or []
    texts = [str(s[2]) for s in segs]
    scores = []
    for i, t in enumerate(texts):
        nxt = texts[i + 1] if i + 1 < len(texts) else ""
        scores.append(round(score_sentence(t, nxt), 2))
    flags = [x < threshold for x in scores]
    return scores, flags


def extend_spans(data, spans, max_extra=2):
    """把落在"没说完"的切点往后推，最多多带 max_extra 句。

    返回修正后的 [(start, end)]。切点仍然落在句子边界上。
    """
    segs = data.get("segments") or []
    if not segs or not spans:
        return spans
    flags = data.get("complete_flag")
    if not flags or len(flags) != len(segs):
        _, flags = compute(data)
    out = []
    for a, b in spans:
        # 找到当前结束句的下标
        idx = None
        for i, sg in enumerate(segs):
            if float(sg[1]) >= b - 0.05:
                idx = i
                break
        if idx is None:
            out.append((a, b))
            continue
        extra = 0
        nb = b
        while (idx < len(flags) and flags[idx] and extra < max_extra
               and idx + 1 < len(segs)):
            idx += 1
            extra += 1
            nb = float(segs[idx][1])
        out.append((a, max(b, nb)))
    # 扩展后可能重叠，合并
    out.sort()
    merged = [list(out[0])]
    for a, b in out[1:]:
        if a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


if __name__ == "__main__":
    import json
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else ""
    if not p:
        print("用法：python completeness.py <index.json>")
        sys.exit(1)
    d = json.load(open(p, encoding="utf-8"))
    sc, fl = compute(d)
    print("句子 %d 句，判定没说完 %d 句（%.0f%%）"
          % (len(sc), sum(fl), 100 * sum(fl) / max(1, len(sc))))
    segs = d.get("segments") or []
    for i, f in enumerate(fl):
        if f and i < 12:
            print("   %.1f~%.1f  %s" % (segs[i][0], segs[i][1], segs[i][2][:34]))

# ---------------------------------------------------------------------------
# 音频边缘：切进说话就往外补
# ---------------------------------------------------------------------------
def repair_by_semantics(data, spans, max_back=2):
    """语义修正切点：端点落在"没说完"的句子上，就往那一侧补。

    为什么不用音量：音量只能说明"这里响"，说明不了"这句话说完没有"。
    语义才是判据——
      · 片段开头：紧挨着的上一句如果没说完（complete_flag），说明话没讲完
        就跳进来了，往前补上那一句
      · 片段结尾：最后一句没说完，往后补到句子说完为止
    返回 (新spans, 修补处数)。
    """
    segs = data.get("segments") or []
    if not spans or not segs:
        return spans, 0
    comp = data.get("complete") or {}
    flags = comp.get("flag")
    if not flags or len(flags) != len(segs):
        _, flags = compute(data)
    starts = [float(sg[0]) for sg in segs]
    ends = [float(sg[1]) for sg in segs]

    def idx_of_end(t):
        best = None
        for k, e in enumerate(ends):
            if abs(e - t) <= 0.12:
                best = k
        return best

    def idx_of_start(t):
        best = None
        for k, st in enumerate(starts):
            if abs(st - t) <= 0.12:
                best = k
        return best

    fixed = 0
    out = []
    for a, b in spans:
        na, nb = a, b
        # 开头：前一句没说完 → 把那句并进来
        prev = idx_of_start(a)
        back = 0
        while prev is not None and prev - 1 >= 0 and back < max_back:
            p = prev - 1
            if not flags[p]:
                break
            na = starts[p]
            prev = p
            back += 1
        # 结尾：最后一句没说完 → 往后补
        cur = idx_of_end(b)
        fwd = 0
        while cur is not None and cur + 1 < len(segs) and fwd < max_back:
            if not flags[cur]:
                break
            cur += 1
            nb = ends[cur]
            fwd += 1
        if (na, nb) != (a, b):
            fixed += 1
        out.append((na, nb))

    out.sort()
    merged = [list(out[0])]
    for a, b in out[1:]:
        if a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged], fixed


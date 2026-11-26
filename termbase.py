# -*- coding: utf-8 -*-
"""模组词库：从整合包的 mods/*.jar 里扒官方中文名。

为什么这么做：
  · MC Wiki 只有原版词条，模组（机械动力、Mekanism、硅岩）它基本没有
  · 但每个模组 jar 里都带 assets/<modid>/lang/zh_cn.json，
    那就是汉化组/authored 官方中文名，最准、最全、还不用联网
  · 扒出来存成词表，转写时当提示词、纠错时当对照表

用法：
    python termbase.py --mods "E:\\...\\mods"          # 扫描并保存
    python termbase.py --mods ... --out kb/mods_x.txt
    python termbase.py --check 硅岩                     # 查一个词在不在库里
"""
import argparse
import json
import os
import re
import sys
import zipfile

from memory_config import app_home

BASE = app_home()
KB_DIR = os.path.join(BASE, "kb")
TERMS_FILE = os.path.join(BASE, "conf", "terms.txt")
MODS_KB = os.path.join(KB_DIR, "mods_terms.json")

# 太通用的词不要，否则纠错会乱改
BAD_WORDS = {"物品", "方块", "工具", "材料", "容器", "能量", "升级", "模块",
             "机器", "组件", "碎片", "粉尘", "锭", "板", "粉", "块"}


CJK = lambda ch: "\u4e00" <= ch <= "\u9fff"


def _good_term(t):
    """判断一个字符串能不能当“术语”：要像名字，不能像句子。"""
    if not (2 <= len(t) <= 10):
        return False
    if any(ch in t for ch in '\u0025{}\u3002\uff01\uff1f\uff0c\uff1b\uff1a\u3001\\n<>!"'):
        return False                     # 整句提示文本 / wiki 残留 / 引号
    cn = sum(1 for ch in t if CJK(ch))
    if cn < 2:
        return False                     # 纯英文/纯符号，用不上
    if t[0] in "将把可无有只某每该其此" and len(t) > 6:
        return False                     # “将鼠标所指的物品…”这类
    if "的" in t and len(t) > 8:
        return False
    # 说明性短语（“需要附近存在村庄袭击怪物”“开关量子装甲自动补货功能”）
    for w in ("需要", "可以", "自动", "开关", "增加", "减少", "提高",
              "降低", "显示", "使用", "获得", "只能", "无法", "速度",
              "功能", "效果", "数量", "上限", "概率", "距离", "范围",
              "打开", "按住", "查看", "点击", "选择", "置于", "放入"):
        if w in t:
            return False
    return True


def scan_jar(path, max_terms=4000):
    """从一个模组 jar 里读 zh_cn.json，返回中文名列表。"""
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                low = name.lower()
                if not low.endswith(".json"):
                    continue
                if "/lang/" not in low:
                    continue
                if not ("zh_cn" in low or "zh-cn" in low):
                    continue
                try:
                    data = json.loads(z.read(name).decode("utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(data, dict):
                    continue
                for k, v in data.items():
                    if not isinstance(v, str):
                        continue
                    # 只要"名字类"键，跳过提示文本、工具说明
                    lk = k.lower()
                    if any(x in lk for x in ("tooltip", "desc", "message",
                                             "gui.", "button", "info")):
                        continue
                    t = v.strip()
                    if _good_term(t):
                        out.append(t)
                    if len(out) >= max_terms:
                        return out
    except (zipfile.BadZipFile, OSError):
        return []
    return out


def scan_mods(mods_dir, on_progress=None):
    """扫描整个 mods 目录，返回 {词: 模组文件名}。"""
    terms = {}
    jars = [f for f in os.listdir(mods_dir) if f.lower().endswith(".jar")]
    for i, jar in enumerate(sorted(jars), 1):
        for t in scan_jar(os.path.join(mods_dir, jar)):
            if t in BAD_WORDS:
                continue
            terms.setdefault(t, jar)
        if on_progress and i % 25 == 0:
            on_progress(i, len(jars), len(terms))
    return terms


def save(terms, pack_name, mods_dir=""):
    """存成 JSON（带来源），并同步一份纯词表给转写当提示词。"""
    os.makedirs(KB_DIR, exist_ok=True)
    all_terms = load_all()
    all_terms.update({k: v for k, v in terms.items() if k not in all_terms})
    with open(MODS_KB, "w", encoding="utf-8") as fh:
        json.dump({"updated": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                   "packs": {pack_name: {"dir": mods_dir, "count": len(terms)}},
                   "terms": all_terms}, fh, ensure_ascii=False)
    write_hint_list(all_terms)
    return len(all_terms)


def load_all():
    try:
        with open(MODS_KB, encoding="utf-8") as fh:
            return json.load(fh).get("terms") or {}
    except (OSError, ValueError):
        return {}


def write_hint_list(terms, limit=3000):
    """写给 Whisper 的提示词：优先常用的、名字短的，长句放后面。

    之前按长度降序，结果一上来全是“128³空间存储元件”这种，把预算吃光了，
    真正高频的词反而挤不进去。
    """
    # 优先带拉丁字母的（ME、AE2、GT 这类模组专用）和 3-6 字的中文名，
    # 纯两字通用词（上方/下方/世界）价值低，放最后
    def _rank(w):
        has_latin = any(('a' <= c.lower() <= 'z') for c in w)
        return (0 if has_latin else 1, 0 if 3 <= len(w) <= 6 else 1, len(w), w)
    words = sorted(terms, key=_rank)[:limit]
    p = os.path.join(KB_DIR, "mods_hint.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("，".join(words))
    return p


def hint_prompt(max_chars=800):
    """取一段词表当转写的 initial_prompt（太长会拖慢识别）。"""
    p = os.path.join(KB_DIR, "mods_hint.txt")
    if not os.path.isfile(p):
        return ""
    try:
        s = open(p, encoding="utf-8").read()
    except OSError:
        return ""
    return s[:max_chars]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mods", help="整合包的 mods 目录")
    ap.add_argument("--pack", default="", help="整合包名字（存库用）")
    ap.add_argument("--check", help="查一个词在不在库里")
    ap.add_argument("--hint", action="store_true", help="输出提示词片段")
    args = ap.parse_args(argv)

    if args.check:
        terms = load_all()
        hit = terms.get(args.check)
        print("命中：%s ← %s" % (args.check, hit) if hit else "库里没有：%s" % args.check)
        return 0

    if args.hint:
        print(hint_prompt())
        return 0

    if not args.mods:
        ap.print_help()
        return 1
    if not os.path.isdir(args.mods):
        print("目录不存在：%s" % args.mods)
        return 1

    print("扫描：%s" % args.mods)
    terms = scan_mods(args.mods,
                      on_progress=lambda i, n, c: print("  %d/%d 个 jar，已收 %d 词"
                                                        % (i, n, c), flush=True))
    name = args.pack or os.path.basename(os.path.dirname(args.mods.rstrip("\\/")))
    total = save(terms, name, args.mods)
    print("本次识别 %d 个词，词库合计 %d 个" % (len(terms), total))
    print("词表：%s" % MODS_KB)
    print("\n样例（长词优先）：")
    for w in sorted(terms, key=lambda x: -len(x))[:15]:
        print("   %s   ← %s" % (w, terms[w][:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# 纠错：只在“本片里确实出现过正确写法”的时候才改
# ---------------------------------------------------------------------------
def known_terms_in(texts, terms=None):
    """本片台词里原样出现过的词库词（高置信度）。

    关键设计：纠错只朝这些词改。因为“把某个词改成词库里的另一个词”
    风险很大（可能改反），但如果同一个片子里别处已经正确说过一次，
    那这一处八成是听错了——这样的替换既有效又安全。
    """
    terms = terms if terms is not None else load_all()
    joined = "\n".join(texts)
    return {t for t in terms if t in joined}


def load_explicit_aliases(path=None):
    """Load only user-confirmed ``canonical|alias`` substitutions.

    The generated mod/KB dictionaries are useful ASR hints, but they are not
    authoritative enough to rewrite transcript text.  Exact aliases in
    ``conf/terms.txt`` are the only deterministic write rules.
    """
    source = path or TERMS_FILE
    targets = {}
    try:
        with open(source, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                canonical, aliases = line.split("|", 1)
                canonical = canonical.strip()
                if not canonical:
                    continue
                for alias in aliases.replace("，", ",").split(","):
                    alias = alias.strip()
                    if alias and alias != canonical:
                        targets.setdefault(alias, set()).add(canonical)
    except OSError:
        return {}
    return {alias: next(iter(values)) for alias, values in targets.items()
            if len(values) == 1}


def apply_explicit_aliases(text, aliases=None):
    mapping = load_explicit_aliases() if aliases is None else dict(aliases)
    out = str(text or "")
    fixes = []
    for wrong in sorted(mapping, key=len, reverse=True):
        right = mapping[wrong]
        if wrong in out:
            count = out.count(wrong)
            out = out.replace(wrong, right)
            fixes.extend([(wrong, right)] * count)
    return out, fixes


def _bucket(known):
    b = {}
    for t in known:
        b.setdefault((len(t), t[0]), []).append(t)
    return b


def correct_text(text, buckets, targets=None):
    """把句中与已知词仅差一个字的片段改成正确写法。

    精度优先：宁可漏改，也不能把“开始合成”改成“开始了成”。
      · 目标词必须在同一片里出现过至少两次（只出现一次可能是巧合）
      · 长词优先（先认“二氧化硫”，再考虑“合成”）
      · 片段本身已包含正确的目标词就跳过
    """
    if not text or not buckets:
        return text, []
    targets = targets or set()
    fixes = []
    out = text
    i = 0
    while i < len(out):
        hit = None
        for L in range(8, 2, -1):
            w = out[i:i + L]
            if len(w) < L:
                continue
            # 已经含有正确词，不折腾
            if any(t in w for t in targets if t and t in w) and w not in targets:
                continue
            cands = buckets.get((L, w[0]))
            if not cands:
                continue
            # 句尾语气/助词不要动：“合成了”不该被改成“合成卡”
            if w[-1] in "了的着过吗吧呢啊呀嘛哦" :
                continue
            for c in cands:
                if c == w:
                    hit = (w, c, True)
                    break
                if sum(1 for a, b in zip(w, c) if a != b) == 1:
                    hit = (w, c, False)
                    break
            if hit:
                break
        if hit:
            w, c, same = hit
            if not same:
                out = out[:i] + c + out[i + len(w):]
                fixes.append((w, c))
            i += len(w)
        else:
            i += 1
    return out, fixes


def apply_to_segments(segs, on_log=None, allow_kb=False):
    """对整片台词做一遍词库纠错，返回 (新segs, 纠错条数)。

    默认只应用 ``conf/terms.txt`` 的明确别名，只改 text、不动时间。
    全量 KB 仍可用于 ASR 提示和候选；``allow_kb=True`` 仅保留给离线
    诊断，不应在正式字幕链路中启用。
    """
    if not segs:
        return segs, 0
    if not allow_kb:
        aliases = load_explicit_aliases()
        total, samples, out = 0, [], []
        for a, b, text in segs:
            fixed, fixes = apply_explicit_aliases(text, aliases)
            total += len(fixes)
            if fixes and len(samples) < 5:
                samples.append(fixes[0])
            out.append([a, b, fixed])
        if on_log:
            on_log("  术语纠错：仅应用 conf/terms.txt 明确别名，纠正 %d 处"
                   % total)
            for wrong, right in samples:
                on_log("    %s → %s" % (wrong, right))
        return out, total

    terms = load_all()
    if not terms:
        return segs, 0
    texts = [x[2] for x in segs]
    known = known_terms_in(texts, terms)
    if not known:
        if on_log:
            on_log("  词库纠错：本片没有可对照的已知词，跳过（%d 词库）" % len(terms))
        return segs, 0
    # 目标词 = 本片里出现过两次以上的词（一次可能只是巧合）
    counts = {}
    joined = "\n".join(texts)
    for t in known:
        counts[t] = joined.count(t)
    strong = {t for t, n in counts.items() if n >= 2 and len(t) >= 3}
    buckets = _bucket(strong)
    total = 0
    samples = []
    out = []
    for a, b, t in segs:
        nt, fixes = correct_text(t, buckets, targets=strong)
        if fixes:
            total += len(fixes)
            if len(samples) < 5:
                samples.append(fixes[0])
        out.append([a, b, nt])
    if on_log:
        on_log("  词库纠错：本片已知词 %d 个（可作目标 %d 个），纠正 %d 处"
               % (len(known), len(strong), total))
        for wrong, right in samples:
            on_log("    %s → %s" % (wrong, right))
    return out, total

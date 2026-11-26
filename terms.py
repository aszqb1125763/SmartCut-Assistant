# -*- coding: utf-8 -*-
"""术语纠错：整合包词典 + 拼音 + 模糊匹配，把 ASR 写错的物品名改回来。

三级纠错（按可靠性从高到低）：
  ① 拼音完全一致 → 直接换（同音错字，实测 100%）
  ② 模糊匹配（字符二元组 + 前缀，阈值 0.55）→ 换（听错音节的错，实测约 46%，
     300 个正确词 0 误改）
  ③ 都不命中 → 原样保留（宁可不动，不乱改）

词典来源：从整合包的 mod jar 语言文件中提取，
共 13 万条物品/方块/实体名，装进 kb\\术语_全量.txt。

用法：
    import terms
    terms.load()
    new, reps = terms.correct("我们先把过冲水晶做下")
    # → ("我们先把过充水晶做下", [("过冲水晶", "过充水晶")])
"""
from __future__ import annotations

import os
import sys

_SOURCE_BASE = os.path.dirname(os.path.abspath(__file__))
if _SOURCE_BASE not in sys.path:
    sys.path.insert(0, _SOURCE_BASE)

from memory_config import app_home  # noqa: E402

BASE = app_home()

DICT_CANDIDATES = [
    os.path.join(BASE, "kb", "术语_全量.txt"),
    os.path.join(BASE, "conf", "asr_terms.txt"),
]
USER_TERMS_FILE = os.path.join(BASE, "conf", "terms.txt")
CACHE = os.path.join(BASE, "conf", "terms_dict_cache.json")
DEFAULT_MIN_LEN = 4          # 只改 ≥4 字的专有名词（3 字误报多）
FUZZY_MIN_SCORE = 0.55       # 模糊匹配阈值（实测：46% 修复率、0 误改）

_state = {"terms": set(), "by_py": {}, "by_len": {}, "loaded": False,
          "char_py": {}, "min_len": 2, "max_len": 8, "source": ""}
_fuzzy = {"ready": False, "mod": None}


def _pinyin_of(text, py):
    """把一段中文转成拼音元组（逐字缓存；多音字取第一个读音）。"""
    out = []
    for ch in text:
        if ch not in py:
            try:
                from pypinyin import lazy_pinyin
                py[ch] = (lazy_pinyin(ch) or [""])[0]
            except Exception:  # noqa: BLE001
                py[ch] = ch
        out.append(py[ch])
    return tuple(out)


def dict_path():
    for p in DICT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def load_explicit_aliases(path=None):
    """Return unambiguous aliases explicitly maintained by the user."""
    targets = {}
    try:
        with open(path or USER_TERMS_FILE, encoding="utf-8") as handle:
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
    replacements = []
    for wrong in sorted(mapping, key=len, reverse=True):
        right = mapping[wrong]
        if wrong in out:
            count = out.count(wrong)
            out = out.replace(wrong, right)
            replacements.extend([(wrong, right)] * count)
    return out, replacements


def _fuzzy_enabled():
    """模糊纠错默认关闭（见 correct_segments 的说明）。conf 里可临时打开排查。"""
    try:
        import json
        path = os.path.join(BASE, "conf", "settings.json")
        with open(path, encoding="utf-8") as fh:
            value = str(json.load(fh).get("terms_fuzzy", "0")).strip().lower()
        return value in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def load(path=None, force=False):
    """读词典，建"拼音 → 词"索引。"""
    if _state["loaded"] and not force:
        return _state
    src = path or dict_path()
    terms = set()
    if src:
        try:
            with open(src, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    t = line.strip()
                    if not t or t.startswith("#"):
                        continue
                    if "=" in t:                      # 兼容 asr_terms.txt
                        t = t.split("=", 1)[-1].strip()
                    if 2 <= len(t) <= 10:
                        terms.add(t)
        except OSError:
            pass
    by_py, by_len, char_py = {}, {}, {}
    for t in terms:
        key = _pinyin_of(t, char_py)
        by_py.setdefault(key, []).append(t)
        by_len.setdefault(len(t), set()).add(t)
    _state.update({"terms": terms, "by_py": by_py, "by_len": by_len,
                   "char_py": char_py, "loaded": True,
                   "min_len": min(by_len) if by_len else 2,
                   "max_len": max(by_len) if by_len else 8,
                   "source": src})
    return _state


def _fuzzy_mod():
    if not _fuzzy["ready"]:
        try:
            import terms_fuzzy as _tf
            _tf.train()
            _fuzzy["mod"] = _tf
        except Exception:  # noqa: BLE001
            _fuzzy["mod"] = None
        _fuzzy["ready"] = True
    return _fuzzy["mod"]


def fuzzy_fix(text, min_score=FUZZY_MIN_SCORE):
    """对"拼音对不上"的错词做模糊匹配。返回 (新文本, [(错, 对)])。"""
    tf = _fuzzy_mod()
    st = load()
    if tf is None or not text:
        return text, []
    reps, out, i, n = [], text, 0, len(text)
    while i < n - 2:
        hit = False
        for L in (6, 5, 4, 3):
            if i + L > n:
                continue
            cand = out[i:i + L]
            if cand in st["terms"]:
                i += L
                hit = True
                break
            best = None
            for score, _cov, t in tf.candidates(cand, topk=5):
                if t != cand and score >= min_score:
                    best = t
                    break
            if best:
                out = out[:i] + best + out[i + L:]
                reps.append((cand, best))
                i += len(best)
                hit = True
                break
        if not hit:
            i += 1
    return out, reps


# 功能词：拼音纠错不许把它们吞掉。
# 实测反例："说是门阁海绵"被整体同音替换成"说湿门格海绵"——"是"被吃掉，
# 句子从"说是门格海绵"变成"说湿门格海绵"，读起来就是错的。
FUNCTION_CHARS = set("是的了在这那就都也还和跟把被给对从到说有个上下里呢吧啊")


def correct(text, min_len=DEFAULT_MIN_LEN, max_len=None, max_reps=20,
            fuzzy=True, allow_dictionary=False):
    """Apply explicit aliases; optionally run the legacy KB matcher.

    Full dictionaries are ASR hints/candidates, not write authority.  The
    production default therefore stops after exact ``conf/terms.txt`` aliases.
    ``allow_dictionary=True`` is retained only for offline diagnostics.
    """
    out, explicit = apply_explicit_aliases(text)
    if not allow_dictionary:
        return out, explicit
    st = load()
    if not st["terms"] or not out:
        return out, explicit
    py = st["char_py"]
    lo = max(int(min_len or 0), DEFAULT_MIN_LEN)
    hi = int(max_len or st["max_len"])
    reps, i, n = list(explicit), 0, len(out)
    while i < n:
        hit = False
        for L in range(min(hi, n - i), lo - 1, -1):
            cand = out[i:i + L]
            if cand in st["terms"]:
                i += L
                hit = True
                break
            options = st["by_py"].get(_pinyin_of(cand, py))
            if not options:
                continue
            best = None
            for t in options:
                if t == cand:
                    best = None
                    break
                if best is None or len(t) > len(best):
                    best = t
            if best and best != cand:
                # 功能词保护：等长替换时不许换掉开头的功能词
                if (len(cand) == len(best) and cand[0] in FUNCTION_CHARS
                        and best[0] != cand[0]):
                    continue
                out = out[:i] + best + out[i + L:]
                reps.append((cand, best))
                i += len(best)
                hit = True
                if len(reps) >= max_reps:
                    return out, reps
                break
        if not hit:
            i += 1
    if fuzzy and len(reps) < max_reps:
        out2, reps2 = fuzzy_fix(out)
        if reps2:
            out, reps = out2, reps + reps2
    return out, reps


def unify_terms(segments, known_terms, on_log=None):
    """（暂不启用）按"多余单字"归并术语写法。

    实测会误删，不能直接用于成片：
      · `这个门格海绵真贵` → `这个门格海绵`（"真贵"被吃）
      · `原本皇家钢剑单`   → `皇家钢剑`（"原本"被吃）
    规则分不清"前缀修饰"（湿+门格海绵）和"后续词"（门格海绵+真贵）。
    `皇家钢剑单` 这类交给语义校对（8B）处理，比硬规则可靠。
    """
    import re

    st = load()
    terms = st["terms"]
    targets = [t for t in dict.fromkeys(known_terms or []) if len(t) >= 3]
    if not targets or not terms:
        return segments, 0, []
    total, examples = 0, []
    out = []
    for s in segments:
        txt = str(s[2] or "") if len(s) > 2 else ""
        new = txt
        for term in targets:
            if term not in new:
                continue
            # 前多一字：X + term
            pat_pre = re.compile(r"." + re.escape(term))
            for _ in range(6):
                m = pat_pre.search(new)
                if not m:
                    break
                ext = m.group(0)
                if ext in terms or ext[0] in FUNCTION_CHARS:
                    break
                new = new.replace(ext, term, 1)
                total += 1
                examples.append((ext, term))
            # 后多一字：term + X
            pat_post = re.compile(re.escape(term) + r".")
            for _ in range(6):
                m = pat_post.search(new)
                if not m:
                    break
                ext = m.group(0)
                if ext in terms or ext[-1] in FUNCTION_CHARS:
                    break
                new = new.replace(ext, term, 1)
                total += 1
                examples.append((ext, term))
        out.append((s[0], s[1], new) if len(s) > 2 else s)
    if on_log and total:
        on_log("术语统一：%d 处（多余单字归并到词典写法）" % total)
        for a, b in examples[:5]:
            on_log("    %s → %s" % (a, b))
    return out, total, examples


def correct_segments(segments, on_log=None, limit=None, fuzzy=None,
                     allow_dictionary=False):
    """纠错台词，返回 (新台词列表, 改动数, 例子)。

    fuzzy 默认读 conf 开关（默认关）：模糊纠错在真实素材上净收益是负的——
    实测 406 句里改错 13 处（"录完之后"→"录完恶魂之后"、"这里是"→"停在这里是"），
    而逐字拼音匹配本身已经能改对 8 处真错字（皇家纲简→皇家钢剑、墨影接口→末影接口）。
    """
    if fuzzy is None:
        fuzzy = _fuzzy_enabled()
    st = load() if allow_dictionary else {"terms": set()}
    total, examples = 0, []
    out = []
    for k, s in enumerate(segments):
        seg = list(s)
        txt = seg[2] if len(seg) > 2 else ""
        if (not limit or k < limit) and txt:
            new, reps = correct(
                txt, fuzzy=fuzzy, allow_dictionary=allow_dictionary)
            if reps:
                total += len(reps)
                seg[2] = new
                examples.extend(reps[:2])
        out.append(tuple(seg))
    if on_log and total:
        mode = ("全量词典诊断" if allow_dictionary
                else "conf/terms.txt 明确别名")
        on_log("术语纠错：改了 %d 处（%s，模糊匹配%s）"
               % (total, mode, "开" if fuzzy and allow_dictionary else "关"))
        for a, b in examples[:5]:
            on_log("    %s → %s" % (a, b))
    return out, total, examples


if __name__ == "__main__":
    st = load(force=True)
    print("词典：%s（%d 词）" % (st.get("source"), len(st["terms"])))
    for t in ("我们先把过冲水晶做下", "堕落智能和乌妖电容", "在冲浓站做的",
              "我们先把过的一水晶做下", "缺这些电容先去挖矿"):
        print("  %-22s → %s" % (t, correct(t)))

# -*- coding: utf-8 -*-
"""
给卡片起标题、打分类。

这一版是**规则引擎**，不是大模型：
    - 稳定（同样的台词永远得到同样的结果，不会随机）
    - 零成本、零依赖（不用下几个 G 的模型，不用显卡）
    - 可解释：每个标签都能说清是哪几个词命中的

为什么先用规则：科技包的台词里术语密度很高（"粉碎轮""传动轴""通用机械"），
关键词命中率天然就高。等这一版跑出来的标题明显不够用时，再换本地小模型改写，
接口已经留好（见 tagger_llm.py 的说明）。
"""

from __future__ import annotations

import re

# 分类词表：命中越多分越高。词要尽量具体，避免"打""做"这种到处都有的动词单独成判据。
CATEGORY_WORDS = {
    "挖矿": ["挖矿", "挖煤", "挖铁", "矿石", "矿洞", "矿道", "残骸", "远古",
             "隧道", "钻头", "镐子", "矿车", "铁矿", "煤矿", "金矿", "铜矿",
             "锡矿", "铀矿", "钻石", "红石矿", "青金石", "绿宝石", "挖"],
    "合成": ["合成", "配方", "工作台", "合成表", "烧制", "熔炉", "冶炼", "熔炼",
             "做出来", "合一个", "造一个", "打粉", "粉碎", "研磨"],
    "机器": ["机器", "机械", "产线", "生产线", "管道", "传送带", "动力", "齿轮",
             "传动轴", "电动机", "发电机", "发电", "线缆", "能量", "储罐",
             "自动化", "接口", "控制器", "粉碎轮", "熔炉组", "反应堆"],
    "建造": ["建造", "搭建", "搭", "盖", "地基", "墙面", "屋顶", "房子", "基地",
             "装饰", "铺设", "装修", "整地", "围墙"],
    "战斗": ["打怪", "僵尸", "骷髅", "苦力怕", "末影人", "蜘蛛", "boss", "龙",
             "血量", "被打", "死了", "阵亡", "复活", "刷怪", "战斗", "武器"],
    "探索": ["下界", "末地", "传送门", "探索", "找遗迹", "村庄", "地图",
             "洞穴", "远行", "迷路", "坐标"],
    "整理": ["整理", "仓库", "箱子", "背包", "分类存放", "清背包", "收纳", "存储"],
    "闲聊": ["哈哈", "笑死", "观众", "弹幕", "聊天", "你们", "大家", "问一下"],
}

# 语气词 / 口水话，标题里要清掉
FILLER = re.compile(
    r"^(嗯+|啊+|哦+|哎+|诶+|那个|这个|然后|就是|好的|好|OK|ok|对|其实|差不多|"
    r"我们|我|你|现在|那么|所以|但是|不过|接着|继续)+[，,、\s]*"
)

# 动作词，用来抓"动宾"短语当标题
ACTION = re.compile(
    r"(做|建|搭|放|摆|挖|打|合|烧|熔|造|去|找|拿|装|接|修|拆|调|换|清|收|放好|"
    r"升级|测试|检查|连接|铺设|启动|关闭|打开|合成|修复|修复好)"
)
# "去/找/拿"这类只是位移，含在句子里时不该抢标题，让给后面的实词动作
WEAK_ACTION = {"去", "找", "拿"}


def _pick_action(text: str):
    """优先取第一个"实词动作"，没有才退回位移类动作。"""
    weak = None
    for m in ACTION.finditer(text):
        if m.group(1) in WEAK_ACTION:
            if weak is None:
                weak = m
            continue
        return m
    return weak

PUNCT = re.compile(r"[，。！？、,.!?~～\s]+")
KEEP = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]")


def _clean(text: str) -> str:
    text = PUNCT.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def classify(text: str) -> list[str]:
    """返回命中的分类（最多两个，按命中词数排序）。"""
    t = text or ""
    scores = {}
    for cat, words in CATEGORY_WORDS.items():
        hit = sum(1 for w in words if w in t)
        if hit:
            scores[cat] = hit
    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [c for c, _ in ranked[:2]]


def make_title(text: str, limit: int = 16) -> str:
    """
    从台词里抓一个能当标题的短语。

    优先抓"动作+宾语"（做机械动力的粉碎轮），抓不到就砍掉语气词取前半句。
    实在没内容就返回空字符串，交给调用方用标签或时间兜底。
    """
    t = _clean(text)
    if not t:
        return ""
    # 去掉句首的口水话
    for _ in range(3):
        new = FILLER.sub("", t).strip()
        if new == t:
            break
        t = new
    if not t:
        return ""

    m = _pick_action(t)
    if m:
        tail = t[m.start():]
        # 在第一个词组边界（空格）就收住，别把后半句也塞进标题
        phrase = tail.split(" ")[0][:limit].strip(" 的")
        if len(phrase) >= 4:
            return phrase
    first = t.split(" ")[0]
    return (first if len(first) >= 6 else t[:limit]).strip()


def describe(text: str, start: float, end: float, tags: list[str],
             highlight: bool) -> dict:
    """给一张卡片算出 title / cats。"""
    cats = classify(text)
    title = make_title(text)
    if not title:
        # 没台词：用标签 + 时长兜底
        if highlight:
            title = "高光片段"
        elif tags:
            title = tags[0]
        else:
            title = f"{end - start:.0f} 秒片段"
    return {"title": title, "cats": cats}


def apply_to_cards(cards: list[dict]) -> int:
    """就地补上 title / cats，返回改了几张。"""
    changed = 0
    for c in cards:
        info = describe(c.get("text", ""), c.get("start", 0), c.get("end", 0),
                        c.get("tags") or [], bool(c.get("highlight")))
        if c.get("title") != info["title"] or c.get("cats") != info["cats"]:
            c["title"] = info["title"]
            c["cats"] = info["cats"]
            changed += 1
    return changed

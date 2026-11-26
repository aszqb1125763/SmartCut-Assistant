# -*- coding: utf-8 -*-
"""手动配对训练：用户指定「原片 + 成品」，这里把它们变成剪辑判据模型。

流程：
    1. 两边都转写（缓存，第二次秒开）
    2. 文字对齐 → 反推作者真正保留的区间
    3. 逐句抽特征、打标签
    4. 训练逻辑回归，留一交叉验证给出真实准确率
    5. 导出 conf/editor_model.json

用法：
    python learn.py --pair "原片.mp4" "成品.mp4" [--pair ...] [--out 模型.json]
    python learn.py --list                     # 看已有的模型和样本
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

_SOURCE_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SOURCE_BASE)
import numpy as np  # noqa: E402
import index as indexer  # noqa: E402
from memory_config import app_home  # noqa: E402

BASE = app_home()
CONF_DIR = os.path.join(BASE, "conf")
# 训练产物目录：可以在设置里改，默认放在工具目录下的 cache_learn
TRAIN_DIR = os.environ.get("SC_TRAIN_DIR") or os.path.join(BASE, "cache_learn")
CACHE = os.path.join(TRAIN_DIR, "cache_learn")
os.makedirs(CACHE, exist_ok=True)
MODEL_PATH = os.path.join(CONF_DIR, "editor_model.json")

PROGRESS = ["完成", "做好", "做成", "搭好", "搞定", "成功", "合成", "获得", "挖到", "放上",
            "下一步", "接下来", "开始", "启动", "接上", "连上", "装满", "够了", "出来",
            "可以了", "直接", "然后我们", "放进去", "拿出来", "第一个", "第二个"]
FILLER = ["就是", "然后", "那个", "这个", "是吧", "对吧", "嗯", "啊", "额", "我看看",
          "怎么", "为什么", "稍微", "一点", "可能", "应该", "等一下", "不知道"]

FEAT_NAMES = ["本句语速", "局部语速", "句长", "前置停顿", "后置停顿",
              "相对位置", "是否开头", "进度词", "废话词", "字数"]


def log(m=""):
    print(m, flush=True)


def cache_path(path):
    h = hashlib.md5(path.lower().encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(os.path.basename(path))[0])[:24]
    return os.path.join(CACHE, "%s_%s.json" % (stem, h))


def transcribe_cached(path, tag=""):
    cp = cache_path(path)
    if os.path.isfile(cp):
        try:
            d = json.load(open(cp, encoding="utf-8"))
            if d.get("segments"):
                log("  %s 用缓存（%d 句）" % (tag, len(d["segments"])))
                return d
        except Exception:  # noqa: BLE001
            pass
    t0 = time.time()
    info = indexer.probe_streams(path)
    streams = info.get("audio_streams") or []
    track = 0
    if len(streams) > 1:
        track = indexer.pick_audio_stream(path, streams, on_status=lambda m: None)
    wav = os.path.join(CACHE, "tmp_%d.wav" % (abs(hash(path)) % 100000))
    indexer.extract_audio_wav(path, wav, track)
    audio = indexer.normalize_for_asr(indexer.read_wav_mono(wav))
    segs = indexer.transcribe(audio, model_size="large-v3-turbo",
                              on_status=lambda m: None, beam_size=1)
    try:
        os.remove(wav)
    except OSError:
        pass
    d = {"path": path, "duration": info.get("duration", 0),
         "segments": [[round(s, 2), round(e, 2), t] for s, e, t in segs]}
    json.dump(d, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    log("  %s 转写完成：%d 句 / %.0f 分钟 / 耗时 %.0f 秒"
        % (tag, len(d["segments"]), d["duration"] / 60, time.time() - t0))
    return d


def bigrams(t):
    t = "".join(ch for ch in t if "\u4e00" <= ch <= "\u9fff")
    return frozenset(t[i:i + 2] for i in range(len(t) - 1))


def dice(A, B):
    if not A or not B:
        return 0.0
    return 2.0 * len(A & B) / (len(A) + len(B))


def align(fin_segs, raw_segs, th=0.35):
    n, m = len(fin_segs), len(raw_segs)
    if not n or not m:
        return []
    FB = [bigrams(s[2]) for s in fin_segs]
    RB = [bigrams(s[2]) for s in raw_segs]
    prev = np.zeros(m + 1, dtype=np.float32)
    back = np.zeros((n + 1, m + 1), dtype=np.int8)
    for i in range(1, n + 1):
        cur = np.zeros(m + 1, dtype=np.float32)
        Bi = FB[i - 1]
        for j in range(1, m + 1):
            best, arg = prev[j], 2
            if cur[j - 1] > best:
                best, arg = cur[j - 1], 3
            s = dice(Bi, RB[j - 1])
            if s >= th:
                d = prev[j - 1] + s
                if d > best:
                    best, arg = d, 1
            cur[j] = best
            back[i][j] = arg
        prev = cur
    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        d = back[i][j]
        if d == 1:
            if dice(FB[i - 1], RB[j - 1]) >= th:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif d == 2:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def seg_features(segs, i):
    s, e, t = segs[i]
    L = max(0.2, e - s)
    prev_gap = max(0.0, s - segs[i - 1][1]) if i else 0.0
    nxt_gap = max(0.0, segs[i + 1][0] - e) if i + 1 < len(segs) else 0.0
    lo, hi = max(0, i - 3), min(len(segs), i + 4)
    wl = sum(segs[k][1] - segs[k][0] for k in range(lo, hi))
    wc = sum(len(segs[k][2]) for k in range(lo, hi))
    total = segs[-1][1] if segs else 1.0
    return [len(t) / L, wc / max(0.2, wl), min(L, 15.0) / 15.0,
            min(prev_gap, 5.0) / 5.0, min(nxt_gap, 5.0) / 5.0,
            s / max(1.0, total), 1.0 if s < 60 else 0.0,
            sum(t.count(w) for w in PROGRESS) / max(1.0, L) * 10,
            sum(t.count(w) for w in FILLER) / max(1.0, L) * 10,
            len(t) / 10.0]


def build_pair(raw_path, fin_path):
    """一组配对 → 特征矩阵 + 标签 + 保留区间。"""
    raw = transcribe_cached(raw_path, "原片")
    fin = transcribe_cached(fin_path, "成品")
    if not raw["segments"] or not fin["segments"]:
        return None
    pairs = align(fin["segments"], raw["segments"])
    if len(pairs) < 10:
        log("  对齐太少（%d 对），跳过" % len(pairs))
        return None
    # 配对质量检查：成品里的句子有多少能在原片里找到。
    # 太低说明这两份素材根本不是同一期（或者左右顺序放反了），
    # 拿它训练等于教模型“这段该剪”，实际那是另一期的内容。
    cover = len(pairs) / max(1.0, float(len(fin["segments"])))
    if cover < 0.45:
        log("  台词重合只有 %.0f%%（%d/%d），这两份多半不是同一期，跳过"
            % (100 * cover, len(pairs), len(fin["segments"])))
        return None
    keeps = []
    for fi, ri in pairs:
        a, b = raw["segments"][ri][0], raw["segments"][ri][1]
        if keeps and a - keeps[-1][1] < 4.0 and a >= keeps[-1][0] - 0.5:
            keeps[-1][1] = max(keeps[-1][1], b)
        else:
            keeps.append([a, b])

    def ov(a, b):
        return sum(max(0.0, min(b, e) - max(a, s)) for s, e in keeps)

    X, y = [], []
    for i in range(len(raw["segments"])):
        a, b = raw["segments"][i][0], raw["segments"][i][1]
        L = max(0.2, b - a)
        X.append(seg_features(raw["segments"], i))
        y.append(1.0 if ov(a, b) / L >= 0.5 else 0.0)
    kept = sum(b - a for a, b in keeps)
    ratio = kept / max(1e-6, raw["duration"])
    log("  台词重合 %.0f%% / 匹配 %d 句 / 原片 %.0f 分 → 保留 %.0f 分（%.0f%%）"
        % (100 * cover, len(pairs), raw["duration"] / 60,
           kept / 60, 100 * ratio))
    return {"X": np.array(X), "y": np.array(y), "ratio": ratio,
            "name": os.path.basename(fin_path), "keeps": keeps,
            "raw_duration": raw["duration"]}


def train_logreg(X, y, epochs=700, lr=0.25, l2=0.01):
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w + b, -30, 30)))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w)
        b -= lr * g.mean()
    return w, b


def f1_curve(y, sc, thr):
    pred = sc >= thr
    tp = int(((pred) & (y == 1)).sum())
    fp = int(((pred) & (y == 0)).sum())
    fn = int(((~pred) & (y == 1)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return (2 * p * r / (p + r) if p + r else 0.0), p, r


def leave_one_out(samples, threshold=0.5):
    """逐视频留一评估，阈值固定为生产实际使用的 0.5。

    不能在留出视频的标签上再搜索最佳阈值，否则等于测试时偷看答案，
    会显著高估模型泛化能力。
    """
    rows = []
    for k, test in enumerate(samples):
        train_set = [sample for j, sample in enumerate(samples) if j != k]
        if not train_set:
            continue
        Xtr = np.vstack([sample["X"] for sample in train_set])
        ytr = np.concatenate([sample["y"] for sample in train_set])
        mu, sd = Xtr.mean(axis=0), Xtr.std(axis=0) + 1e-9
        w, b = train_logreg((Xtr - mu) / sd, ytr)
        z = ((test["X"] - mu) / sd) @ w + b
        scores = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        f1, prec, rec = f1_curve(test["y"], scores, float(threshold))
        rows.append({"name": test["name"], "f1": float(f1),
                     "prec": float(prec), "rec": float(rec)})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, action="append", default=[], metavar=("原片", "成品"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dir", default=None, help="训练产物目录（缓存和中间文件）")
    args = ap.parse_args(argv)

    # 注意：CACHE 在模块导入时就定了值，光设环境变量没用，
    # 必须在这里重新算一遍，否则训练产物还是写回工具目录。
    global CACHE, TRAIN_DIR
    if args.dir:
        TRAIN_DIR = os.path.abspath(args.dir)
        CACHE = os.path.join(TRAIN_DIR, "cache_learn")
        os.makedirs(CACHE, exist_ok=True)
        os.environ["SC_TRAIN_DIR"] = TRAIN_DIR
        log("训练产物目录：%s" % TRAIN_DIR)
    if args.list:
        log("模型：%s（%s）" % (MODEL_PATH, "存在" if os.path.isfile(MODEL_PATH) else "还没有"))
        if os.path.isfile(MODEL_PATH):
            m = json.load(open(MODEL_PATH, encoding="utf-8"))
            log("  样本 %d 组 / 留一验证 F1 %.2f" % (m.get("samples", 0), m.get("f1_loo", 0)))
        n = len([f for f in os.listdir(CACHE) if f.endswith(".json")])
        log("  转写缓存 %d 份" % n)
        return 0

    if not args.pair:
        ap.print_help()
        return 1

    log("=" * 60)
    log("  开始训练：%d 组配对" % len(args.pair))
    log("=" * 60)
    samples = []
    for k, (raw, fin) in enumerate(args.pair, 1):
        log("\n[%d/%d] %s" % (k, len(args.pair), os.path.basename(raw)))
        if not os.path.isfile(raw) or not os.path.isfile(fin):
            log("  文件不存在，跳过")
            continue
        s = build_pair(raw, fin)
        if s:
            samples.append(s)
    if not samples:
        log("\n没有可用样本。")
        return 1

    log("\n=== 留一验证（每部片子单独当测试集）===")
    loo = leave_one_out(samples, threshold=0.5)
    for row in loo:
        log("  %-32s P %.2f / R %.2f / F1 %.2f"
            % (row["name"][:30], row["prec"], row["rec"], row["f1"]))
    if loo:
        f1_loo = float(np.mean([row["f1"] for row in loo]))
        prec_loo = float(np.mean([row["prec"] for row in loo]))
        rec_loo = float(np.mean([row["rec"] for row in loo]))
        log("  宏平均 P %.2f / R %.2f / F1 %.2f（固定阈值 0.50）"
            % (prec_loo, rec_loo, f1_loo))
    else:
        f1_loo = prec_loo = rec_loo = 0.0
        log("  样本太少，无法留一验证")

    X = np.vstack([s["X"] for s in samples])
    y = np.concatenate([s["y"] for s in samples])
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    w, b = train_logreg((X - mu) / sd, y)

    log("\n=== 特征权重（正=更该留）===")
    for n, v in sorted(zip(FEAT_NAMES, w), key=lambda x: -abs(x[1])):
        log("  %-8s %+0.3f" % (n, v))

    model = {"feature_names": FEAT_NAMES, "mean": mu.tolist(), "std": sd.tolist(),
             "weights": w.tolist(), "bias": float(b),
             "samples": len(samples), "f1_loo": f1_loo,
             "prec_loo": prec_loo, "rec_loo": rec_loo,
             "eval_threshold": 0.5, "loo": loo,
             "trained": time.strftime("%Y-%m-%d %H:%M:%S")}
    out = args.out or MODEL_PATH
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(model, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("\n模型已保存：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""判据单一真源（Single Source of Truth）。

为什么要这个文件（2026-11-24 定）：
    阈值原来散在 5 个文件里（seam_picker / tolerance / seam_continuity /
    track_audit / rendered_output_qc），同一个概念两套值——
    选择器按 2 倍容差收，审计按 1 倍算，于是"内部说找到了匹配切点、
    实测 35/42 超标"这种争吵永远吵不出结果。

规则（违反测试会红）：
    1. 阈值常量**只许在这里定义**。其它文件一律 `from coords import ...`。
    2. 分级只用这里的三段式 `grade()`，不许各写各的 if。
    3. 条款清单从 `conf/spec_manifest.json` 读（由 `_规范/_tools/gen_manifest.py`
       从规范档案生成），这里不手写条款。

对应规范档案：
    `_规范/01_视频坐标系.md`     条款（A/B/C/D/E）
    `_规范/02_判据表.md`         判据矩阵与分级（M01–M15）
    `_规范/04_阈值与量纲.md`     每个键的出处、单位、量纲
"""
from __future__ import annotations

import json
import os

from memory_config import app_home

BASE = app_home()

#: 规范档案版本。改这里之前先改 `_规范/07_变更日志.md`。
SPEC_VERSION = "2026-11-24.1"

# ---------------------------------------------------------------------------
# 三轴判据（E03 混音前严格 / E05 量纲 / E04 三段式）
# 出处：`_规范/04_阈值与量纲.md` 第二节
# ---------------------------------------------------------------------------
ENERGY_OK_DB = 5.0      # 两端能量差 ≤5dB（子枫："5dB 我听着非常连贯"）
TONE_OK_HZ = 500.0      # 频谱重心**绝对差** ≤500Hz（禁止倍率，E05）
SHAPE_OK_DB = 5.0       # 两端 ±200ms 能量曲线 RMS 差（示范 3.70dB / 旧 9.77dB）
EDGE_FACTOR = 2.0       # 边缘区 = 中心的 1~2 倍；只在没有 ok 候选时兜底用
JUMP_P90_DB = 10.7      # 出片前宽松线：该片自己正常跳变的 P90（E03）
JUMP_PERCENTILE = 90.0  # 算上面那个 P90 用的分位

# ---------------------------------------------------------------------------
# 选点搜索（§15 / E04 兜底）
# ---------------------------------------------------------------------------
LONGCHAIN_TRIES = 6     # 每处空隙最多问几次语义（问太多很慢）
SEARCH_SPAN = 3.5       # 空隙两端各搜多少秒
SEARCH_RATIO = 0.35     # 自适应：搜索范围 = 空隙长度 × 这个比例
SEARCH_SPAN_MAX = 10.0  # 上限
DISTANCES = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 40.0)  # 由近到远的距离档

# ---------------------------------------------------------------------------
# 时间与能量（A03 / A20 / A21 / M02 / M03 / M07）
# ---------------------------------------------------------------------------
ENV_WINDOW_MS = 20      # 能量包络窗必须 ≤20ms（30ms 会把 0.08s 断崖平滑掉）
SILENCE_DB = -45.0      # 低于它算"几乎没声"
VALLEY_DB = -42.0       # 自然停顿的谷
HOLLOW_GAP_MS = 500     # 词间空隙 >500ms 判空洞（A20）
GAP_TARGET_MS = 250     # 空洞压缩目标（A20）
BREATH_MIN_MS = 100     # 换气点下限（A13）
BREATH_MAX_MS = 300     # 换气点上限（A13）
TAIL_HARD_MS = 20       # 尾音衰减 <20ms = 硬断（A07 / M04）
TAIL_CHOP_DB = 20.0     # 被削的另一种判法：右边比左边低 ≥20dB（声音断了）
TAIL_DROP_RATIO = 0.8   # A07：20ms 内跌掉"总落差的 80%"即算硬断

# ---------------------------------------------------------------------------
# 给模型的定性分档（不给精确 dB，只给"强/中/弱/静音"）
# ---------------------------------------------------------------------------
ENERGY_VERY_STRONG_DB = -12.0  # ≥ −12dB = 极强
ENERGY_STRONG_DB = -20.0       # −20 ~ −12 = 强
ENERGY_UPPER_MID_DB = -28.0    # −28 ~ −20 = 中上
ENERGY_MID_DB = -35.0          # −35 ~ −28 = 中
ENERGY_WEAK_DB = -45.0         # −45 ~ −35 = 弱；低于 −45 = 静音
SUDDEN_SEGMENT_S = 0.08    # 单段 ≤80ms 的涨落算"骤变"，否则"渐变"
ENERGY_FPS_DEFAULT = 5.0    # 能量采样默认 5fps（30fps 视频的 1/6，一帧一个数据）
ENERGY_POINTS_MAX = 600     # 单次最多给模型这么多点；超了自动降分辨率
TAIL_NATURAL_MS = 80    # 尾音衰减 ≥80ms = 自然收句/拖音（A07）
CLIFF_DB_20MS = 15.0    # 人声硬断：20ms 内跌 ≥15dB（A04 / M02）
HEDGE_PAD_MS = 280      # 刀口后留气口（**旧值，已被 CUT_PAD_MS 取代**：A21 定 150ms）

# ---------------------------------------------------------------------------
# 切点标准（2026-11-24 统一：文档 A14/A21、剪辑代码、交付复审共用这一组数）
# 统一前四处各写各的，互相打架：
#   · 气口最小长度 0.45s（autocut.snap_cuts_to_pauses）
#   · 词边界容差 0.025s（rendered_output_qc.find_word_boundary_cuts）
#   · 复审提示里却写「ASR ±0.1 秒误差」
#   · 刀口留气口 280ms（HEDGE_PAD_MS）vs 文档 A21 的 150ms
# 统一口径：切点离词边界 ≥ CUT_WORD_SLACK_S，复审按同一个数判。
# ---------------------------------------------------------------------------
CUT_PAD_MS = 150           # 刀口留气口（A21 定稿：入点收到首字前 ~150ms）
CUT_WORD_SLACK_S = 0.10    # 切点离词边界至少留这么多 = ASR 词边界误差上限
CUT_PAUSE_MIN_S = 0.22     # 可用气口的最小长度（A 级分句口径，取代 0.45）
CUT_PAUSE_SEARCH_S = 3.0   # 切点吸附搜索范围（秒）

# 剪辑力度 = 台词保留比例的**范围**（不是精确值：差不多就行，E04 允许范围值）。
# 软件侧 model_editor 与 PR 侧 pr_engine 共用这一份，避免又出现"两处各写各的"。
LEVEL_KEEP_RANGE = {
    "light": (0.85, 1.00),   # 只删空场/完全重复
    "medium": (0.70, 0.85),  # 接近人工成片
    "heavy": (0.35, 0.55),   # 只留关键节点
}

# ---------------------------------------------------------------------------
# 频段（A04 三频段分层）
# 现在代码只有 80–800Hz 一段，P4 才拆三段。
# ---------------------------------------------------------------------------
BAND_VOICE_HZ = (300.0, 3000.0)   # 人声主频带
BAND_LOW_HZ = (80.0, 300.0)       # 低频背景带
BAND_HIGH_HZ = 3000.0             # 高频细节带下限

VOICE_GATE = 0.6        # 语音门：voice ≥0.6 才进判据（A17）

# ---------------------------------------------------------------------------
# 文字轴（B11 切口等级 / B08 概念分级）——没有标点，只能靠停顿与长度
# ---------------------------------------------------------------------------
TEXT_SENTENCE_GAP_S = 0.45   # 停顿 ≥0.45s 视为句末（S 级）
TEXT_CLAUSE_GAP_S = 0.22     # 停顿 ≥0.22s 视为分句（A 级）
TEXT_MAX_CHARS = 160         # 单句最长字数（超过强制切）
TEXT_CONCEPT_MIN = 2         # 概念至少出现几次才进候选（B08）
TEXT_CONCEPT_CORE = 5        # ≥5 次 = 核心概念

# ---------------------------------------------------------------------------
# 画面（C01–C04）—— 阈值都在这里定，脑里不许出现裸数字
# 说明：规范原文没给画面具体数值，这四条是本轮标定的第一版，
# 后续按实测在 06 里补依据（棘轮：只许收紧）。
# ---------------------------------------------------------------------------
PICTURE_BRIGHT_GAP = 60.0   # 平均亮度差（0–255）
PICTURE_HIST_CORR = 0.55    # HSV 直方图相关系数下限，低于它算换了画面
PICTURE_EDGE_GAP = 0.060    # 边缘密度差（0–1）
PICTURE_MOTION = 0.180      # 相邻帧灰度差均值（0–1），低于算静止

# ---------------------------------------------------------------------------
# 三段式分级（E04）——全项目唯一实现
# ---------------------------------------------------------------------------
def grade(value, center, edge_factor=None):
    """返回 'ok' / 'edge' / 'over'。

    · ok   ：在中心容差内 → 直接用
    · edge ：1~2 倍 → 可用，但必须记账（报告里标 fallback）
    · over ：2 倍以上 → 弃用

    规范原话（子枫）：「所有审核都要允许范围值，不能绝对值，人分不清楚绝对。」
    """
    try:
        v = abs(float(value))
    except (TypeError, ValueError):
        return "over"
    factor = EDGE_FACTOR if edge_factor is None else float(edge_factor)
    if v <= float(center):
        return "ok"
    if v <= float(center) * factor:
        return "edge"
    return "over"


def energy_grade(gap_db):
    return grade(gap_db, ENERGY_OK_DB)


def tone_grade(gap_hz):
    return grade(gap_hz, TONE_OK_HZ)


def shape_grade(rms_db):
    return grade(rms_db, SHAPE_OK_DB)


def jump_grade(jump_db, p90=None):
    return grade(jump_db, float(p90 or JUMP_P90_DB))


def combine(*grades):
    """多轴合成：有一个 over 就是 over，有 edge 就是 edge。"""
    present = [g for g in grades if g]
    if not present:
        return "ok"
    if "over" in present:
        return "over"
    if "edge" in present:
        return "edge"
    return "ok"


def grade_seam(energy_db, tone_hz, shape_db,
               left_silent=False, right_silent=False):
    """一处接缝的最终档位（全项目唯一实现）。

    额外一条（规范 M07 / §13.4）：**只要有一侧落在静音里，就不算技术错误**。
    从话音切进气口、或从气口切进话音，能量差可以很大，但观众听到的是
    "话说完了/话开始了"，不是"声音断了"。以前不分这一档，
    一半的接缝会被误判成"超限"——这也是 0/41 那种数字的一部分来源。

    返回 (grade, note)。
    """
    if left_silent and right_silent:
        return "ok", "pause_to_pause"
    if left_silent or right_silent:
        return "ok", "pause_edge"
    return combine(energy_grade(energy_db), tone_grade(tone_hz),
                   shape_grade(shape_db)), "切在语音里"


def label(g):
    return {"ok": "合格", "edge": "边缘", "over": "超限"}.get(g, "未知")


def score_of(g):
    """把档位折算成 0~1，供排序用。"""
    return {"ok": 1.0, "edge": 0.7, "over": 0.0}.get(g, 0.5)


# ---------------------------------------------------------------------------
# 规范条款清单（从档案生成，不手写）
# ---------------------------------------------------------------------------
MANIFEST_NAMES = ("spec_manifest.json",)


def manifest_path():
    """优先运行副本 `conf/spec_manifest.json`，其次工作区档案。"""
    cands = [os.path.join(BASE, "conf", name) for name in MANIFEST_NAMES]
    cands.append(os.path.join(os.path.dirname(BASE), "_规范",
                              "spec_manifest.json"))
    for path in cands:
        if os.path.isfile(path):
            return path
    return ""


def manifest():
    """读条款清单。读不到返回空 dict（调用方自己决定要不要报错）。"""
    path = manifest_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def clauses():
    """{条款号: {title, status, group}}。"""
    out = {}
    for row in (manifest().get("clauses") or []):
        ident = str(row.get("id") or "").strip()
        if ident:
            out[ident] = row
    return out


def thresholds():
    """所有判据键值，供报告与"文档/代码一致"测试使用。"""
    keys = [name for name in globals()
            if name.isupper() and isinstance(globals()[name], (int, float))]
    return {name: globals()[name] for name in sorted(keys)}


# ---------------------------------------------------------------------------
# 判据矩阵（数据驱动，不是三个 if 拼字符串）
# 规范：`_规范/02_判据表.md` M01–M15；数据：`conf/coords_matrix.json`
# ---------------------------------------------------------------------------
SEVERITY_RANK = {"P0": 3, "P1": 2, "P2": 1, "none": 0}
MATRIX_PATH = os.path.join(BASE, "conf", "coords_matrix.json")

_OP = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def matrix(path=None):
    """读判据矩阵。读不到返回空表（调用方自己决定要不要报错）。"""
    target = path or MATRIX_PATH
    try:
        with open(target, encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def _row_matches(row, state):
    for cond in row.get("when") or []:
        axis = str(cond.get("axis") or "")
        if axis not in state:
            return False
        op = _OP.get(str(cond.get("op") or ""))
        if op is None:
            return False
        try:
            if not op(state[axis], cond.get("value")):
                return False
        except TypeError:
            return False
    return True


#: 报告里用的"人话键名"（给 compares 用，模型看也不吓人）
_COMPARE_NAMES = {
    "energy_gap_db": "d_energy",
    "tone_gap_hz": "d_centroid",
    "shape_rms_db": "d_shape",
    "tail_decay_ms": "tail_decay_ms",
    "gap_ms": "gap_ms",
    "silence_db": "silence_db",
    "high_ratio_gap": "hf_gap",
    "low_band_gap_db": "low_band_gap",
    "cliff_db_20ms": "cliff_db",
    "pitch_jump_semitones": "pitch_jump",
}


def _jsonable(value):
    """numpy 的 bool/浮点不能直接进 JSON（实测把回执写崩过一次）。"""
    if isinstance(value, bool):
        return value
    try:
        import numpy as _np
        if isinstance(value, _np.bool_):
            return bool(value)
        if isinstance(value, _np.integer):
            return int(value)
        if isinstance(value, _np.floating):
            return float(value)
    except Exception:  # noqa: BLE001
        pass
    return value


def _compares(row, state):
    """把命中的条件翻译成可审计的比较式：[名字, 实际值, 比较符, 阈值, 是否通过]。"""
    out = []
    for cond in row.get("when") or []:
        axis = str(cond.get("axis") or "")
        actual = _jsonable(state.get(axis))
        ok = False
        try:
            op = _OP.get(str(cond.get("op") or ""))
            ok = bool(op(actual, cond.get("value"))) if op else False
        except TypeError:
            ok = False
        out.append([_COMPARE_NAMES.get(axis, axis), actual,
                    cond.get("op"), _jsonable(cond.get("value")), ok])
    # 回退行（M18/M19）额外把三轴明细带上，否则报告里只知道"超限"不知道为什么
    limits = {"energy_gap_db": ENERGY_OK_DB, "tone_gap_hz": TONE_OK_HZ,
              "shape_rms_db": SHAPE_OK_DB}
    for axis in row.get("explain") or []:
        if axis not in state:
            continue
        value = _jsonable(state[axis])
        limit = limits.get(axis)
        ok = bool(limit is not None and abs(float(value)) <= float(limit))
        out.append([_COMPARE_NAMES.get(axis, axis), value, "<=", limit, ok])
    return out


def judge(state, rows=None):
    """三轴交叉定案：返回最严重的那一条。

    state 是"缝合状态"字典（键见 coords_matrix.json 的 requires）。
    缺键的行直接跳过——**宁可判不出来，也不许猜**。

    返回 {"row","verdict","severity","action","spec","passed","matched","compares"}；
    什么都没命中时 passed=None（"判不了"要显式区别于"通过"）。
    """
    candidates = rows if rows is not None else (matrix().get("rows") or [])
    best = None
    matched = []
    for row in candidates:
        if not row.get("requires"):
            continue
        if any(key not in state for key in row["requires"]):
            continue
        if not _row_matches(row, state):
            continue
        matched.append(row.get("id"))
        rank = SEVERITY_RANK.get(str(row.get("severity") or "none"), 0)
        if best is None or rank > best[0]:
            best = (rank, row)
    if best is None:
        return {"row": None, "verdict": None, "severity": None,
                "action": None, "spec": None, "passed": None,
                "matched": matched, "compares": []}
    row = dict(best[1])
    severity = str(row.get("severity") or "none")
    return {"row": row.get("id"), "verdict": row.get("verdict"),
            "severity": severity,
            "action": row.get("action"), "spec": row.get("spec"),
            "passed": severity in ("none", "P2"),
            "matched": matched,
            "compares": _compares(row, state)}

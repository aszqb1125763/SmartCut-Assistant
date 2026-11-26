# -*- coding: utf-8 -*-
"""Remote-only output contracts, independent of local-model instructions."""
from __future__ import annotations

import json
import math
import re

SCHEMA = "smartcut-api-contracts-v4"

POLICY = ("先按绝对时间、逐轨声音和画面证据理解素材，再判断文字。"
          "保留比例表示有效内容的保留量，百分比越低剪得越多；长时间无台词的等待"
          "不计入有效内容，禁止为凑时长保留空场。删除与主题无关、重复、跑题、"
          "无信息的试错和纯等待；保留目标、关键过程、结果及必要过渡，不截断因果链。"
          "无台词不等于静音，只有明确的关键画面或声音证据才可保留无台词事件。")

PROMPTS = {
    "writer_read": ("概括当前批素材。严格输出三行：\n摘要：不超过25字的概括\n"
                    "重要度：1到5的整数\n关键：最多3个当前批已有的U事件ID，以逗号分隔；"
                    "没有关键事件写无。禁止用秒数或本批之外的ID。"),
    "writer_plan": ("把输入的B段落组织为4到8个连贯章节（输入很少时可更少，最多10章）。"
                    "一章一行：章节编号|开始B段ID|结束B段ID|标题|取舍建议。"
                    "建议只能是全部保留、只留关键、可以大幅删减。B边界必须存在于输入，"
                    "章节按输入时间顺序且不能重叠。最后一行写主线：不超过40字的总结。"
                    "禁止用秒数代替B段ID，不输出Markdown或解释。"),
    "editor_events": ("仅输出JSON对象：{\"delete_events\":[],\"delete_ranges\":[],"
                      "\"reason\":\"一句话理由\"}。delete_events是U/G事件ID数组，"
                      "delete_ranges是[起始事件ID,终止事件ID]的数组。只允许当前批可写ID，"
                      "前后只读ID不可删除。禁止填写秒数；无删除决定时两个数组都为空。"
                      "safe只是候选余量，不是已经认证的声音边界；不切断词句或重叠发音。"),
    "editor_ranges": ("输出单个JSON对象：{\"delete_ranges\":[[起点秒,终点秒]],"
                      "\"reason\":\"一句话理由\"}。只能使用输入所示的时间坐标，"
                      "起点终点必须为数值且终点大于起点。不要删第一句。无删除时数组为空。"),
    "editor_step": ("判断当前PR序列下一步该删的区间，使用输入的当前时间轴秒数。"
                    "输出JSON：{\"delete_ranges\":[[起点秒,终点秒]],\"reason\":\"理由\"}；"
                    "确已完成时输出{\"done\":true,\"reason\":\"完成理由\"}。"
                    "禁止越出当前序列范围，不删开头第一句，不输出键盘操作或Markdown。"),
    "semantic": ("逐条标注，输入每个编号必须且只能输出一次，格式：编号|类型|段落。"
                 "类型仅speech/laugh/noise/filler，段落仅P或-。物品名、机器名、数字、"
                 "专业词及短句均为speech；filler只限单个语气字；不能凭缺少上下文判噪声。"),
    "proofread": ("只校正错字和同音词，参考输入术语表；不补写、不改语序、不删句、"
                  "不增标点、不删语气词/重复字/句尾字，不新增省略号，不概括。"
                  "人称、说话人、数字和单位保持原样。每行输出编号. 改正后的句子，编号与待校对输入一一对应，"
                  "只读上下文不得输出。无错原样返回，不输出解释。"),
    "proofread_pipe": ("只校正转写错字，保留原意、行数、顺序和编号，不补写、删减或改写。"
                       "不删语气词、重复字或句尾字，不新增省略号或总结；人称、说话人、数字不变。"
                       "逐行输出编号|修正后的文字，编号必须与待校对输入一一对应，"
                       "可能不是从1开始。只读上下文不能输出，不加解释或Markdown。"),
    "subtitle_groups": ("仅输出JSON：{\"groups\":[[1,2],[3]],\"reason\":\"理由\"}。"
                        "每组最多3个连续输入编号，按原顺序分组，每条输入恰好出现一次；"
                        "只合并完整话题，不改写文字，合并字幕不超过20字；时间由程序计算。"),
    "subtitle_fixed": ("仅输出JSON：{\"fixed\":[\"第1行修正后\",\"第2行修正后\"]}。"
                       "数组长度和输入行数完全一致，顺序不变。只校正同音错字、专有名词、"
                       "禁止删除口头禅/重复字/句尾字，禁止新增省略号、概括、扩写，"
                       "禁止改变人称、说话人、数字、标点，禁止合并、拆行或改动时间。"),
    "director_keep": ("只输出要保留的已有行号，一行一个数字，不带标点或说明。"
                      "必须维护目标、过程、结果的因果和首段自我介绍。"
                      "当前批完全无有效内容时只输出NONE，不得编造行号。"),
    "director_review": ("复查已选句是否连贯。无需调整时只输出OK；否则一行一个"
                        "ADD 编号或DROP 编号。编号必须来自输入，禁止解释。"),
    "continuity": ("判断删除中间内容后前后文是否能听懂。通=话题指代接得上；"
                   "勉强=观众可自行补上少量信息；断=话题突跳或结论缺依据。"
                   "不要把口语省略一律判断。严格输出三行：\n结果：通或勉强或断\n"
                   "理由：20字以内\n缺口：缺失的信息；没有写无。"),
    "intro": ("识别开场问候、自我介绍及本集目标，只输出对应输入编号，一行一个。"
              "恰好位于开头的普通解说不算开场；没有开场只输出0。"),
    "boundary": ("检查剪去中间内容后的语义接缝。能独立听懂只输出OK；"
                 "需向右补留句子输出EXTEND 1到4；被删部分为必要前提输出BACK 1到4。"
                 "仅输出一行，不解释。"),
    "retake": ("比较A、B是否同一批物品/操作/开场重录，且A未讲完或跑偏而B更完整。"
               "仅输出JSON：{\"same\":true或false,\"a_abandoned\":true或false,\"why\":\"理由\"}。"),
    "sentence_judge": ("依据完整性、上下文依赖、节奏、主线相关、口水五条逐句判定。"
                       "只输出JSON数组，每项{\"i\":输入编号,\"keep\":true或false,"
                       "\"rule\":\"1/2/3/4/5/none之一\",\"why\":\"理由\"}。"
                       "每个输入编号按顺序恰好输出一次；保留关键前提，删除重复跑题。"),
    "stage_labels": ("逐句标记阶段，只输出JSON数组，每项{\"i\":输入编号,\"s\":\"标签\"}。"
                     "标签仅hook/goal/progress/result/tease/other。保持输入编号，可能从0开始，"
                     "每句恰好一次，不得遗漏或增加。"),
    "story_flow": ("按时间拆成连贯的开场、目标、过程、结果、预告段，只输出JSON数组。"
                   "每项{\"start\":起点秒,\"end\":终点秒,\"type\":\"阶段标签\",\"summary\":\"具体概括\"}。"
                   "标签仅hook/goal/progress/result/tease；按输入时间顺序且首尾衔接，不虚构时间。"),
    "legacy_delete": ("根据提供的时间轴或转写证据决定删除，不能编造声音、文字或画面。"
                      "仅输出JSON：{\"delete\":[[起点秒,终点秒]],\"reason\":\"一句话理由\"}。"
                      "起止必须是输入坐标内的数值，终点大于起点，没有删除时delete为空。"),
    "chapter_summary": "只给出这段内容的具体一句话摘要，不超过25字，不加评价、前后缀或解释。",
}

CONTRACT_ROLES = {
    **{key: "writer" for key in ("writer_read", "writer_plan", "story_flow")},
    **{key: "editor" for key in ("editor_events", "editor_ranges", "editor_step", "legacy_delete")},
    **{key: "analyst" for key in ("semantic", "proofread", "proofread_pipe", "subtitle_groups",
                                  "subtitle_fixed", "stage_labels", "chapter_summary")},
    **{key: "director" for key in ("director_keep", "director_review", "continuity", "intro",
                                   "boundary", "retake", "sentence_judge")},
}


def detect(system, user):
    """Identify a known caller, without forwarding its local system prompt."""
    system, user = str(system or ""), str(user or "")
    if "delete_events" in system:
        return "editor_events"
    if "delete_ranges" in system:
        return "editor_step" if '"done"' in system else "editor_ranges"
    if '"groups"' in system:
        return "subtitle_groups"
    if '"fixed"' in system:
        return "subtitle_fixed"
    if '"keep"' in system and "JSON 数组" in system:
        return "sentence_judge"
    if '"a_abandoned"' in user:
        return "retake"
    if '"s": "标签"' in user:
        return "stage_labels"
    if '"summary"' in user and "成片结构段" in user:
        return "story_flow"
    if '"delete"' in system and "JSON" in system:
        return "legacy_delete"
    if "一句话摘要" in system and "25 字" in system:
        return "chapter_summary"
    if "摘要：" in user and "重要度：" in user and "关键：" in user:
        return "writer_read"
    if "时间轴段摘要：" in user and "开始段B" in user:
        return "writer_plan"
    if "编号|类型|段落" in user:
        return "semantic"
    if "改正后的句子" in user and "【输入】" in user:
        return "proofread"
    if "编号|修正后的文字" in user and "待校对（只输出这些编号）" in user:
        return "proofread_pipe"
    if "缺口：" in system and "结果：" in system:
        return "continuity"
    if "EXTEND" in user and "BACK" in user:
        return "boundary"
    if "自我介绍" in system and "只输出编号" in system:
        return "intro"
    if "ADD" in user and "DROP" in user:
        return "director_review"
    if "只输出编号" in user or "只输出行号" in user:
        return "director_keep"
    return ""


def _numbers(user):
    return [int(value) for value in re.findall(r"^\s*(\d+)\s*[.:：|｜]", user, re.M)]


def _input(user, marker):
    return user.rsplit(marker, 1)[-1]


def _json(raw):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I | re.S)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("必须输出单个合法JSON对象")
    if not isinstance(value, dict):
        raise ValueError("JSON顶层必须是对象")
    return value


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _proofread_pairs(user):
    """Extract only explicit glossary arrows; numbered input is never a term."""
    import proofread
    rows = [line for line in str(user or "").splitlines() if "←" in line]
    return proofread.known_pairs_from_table("\n".join(rows))


def _preserve_proofread_rows(old_rows, new_rows, user):
    import proofread
    _require(len(old_rows) == len(new_rows), "校对输出数量必须与输入一致")
    pairs = _proofread_pairs(user)
    for old, new in zip(old_rows, new_rows):
        problem = proofread.correction_violation(old, new, pairs)
        _require(not problem, problem)


def validate(key, raw, user):
    """Return normalized output or a credential-free contract diagnostic."""
    raw, user = str(raw or "").strip(), str(user or "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if key in ("sentence_judge", "stage_labels", "story_flow"):
        try:
            data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                                     flags=re.I | re.S))
        except (TypeError, ValueError):
            raise ValueError("必须输出合法JSON数组")
        _require(isinstance(data, list) and all(isinstance(item, dict) for item in data),
                 "JSON必须是对象数组")
        if key == "story_flow":
            previous = None
            for item in data:
                a, b = item.get("start"), item.get("end")
                _require(type(a) in (int, float) and type(b) in (int, float)
                         and math.isfinite(a) and math.isfinite(b) and 0 <= a < b
                         and item.get("type") in ("hook", "goal", "progress", "result", "tease")
                         and isinstance(item.get("summary"), str), "结构段字段或时间无效")
                _require(previous is None or abs(a - previous) < 0.05, "结构段必须按顺序首尾相接")
                previous = b
            _require(bool(data), "结构段不能为空")
        else:
            _require([item.get("i") for item in data] == _numbers(_input(user, "台词：")),
                     "每个输入编号必须按顺序输出且只能输出一次")
            if key == "sentence_judge":
                _require(all(type(item.get("keep")) is bool
                             and item.get("rule") in ("1", "2", "3", "4", "5", "none")
                             and isinstance(item.get("why"), str) for item in data), "逐句审片字段无效")
            else:
                _require(all(item.get("s") in ("hook", "goal", "progress", "result", "tease", "other")
                             for item in data), "阶段标签无效")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if key.startswith("editor_") or key.startswith("subtitle_") or key == "json":
        data = _json(raw)
        if key.startswith("editor_"):
            _require(isinstance(data.get("reason"), str), "缺少字符串reason")
            if key == "editor_step" and data.get("done") is True:
                _require(not data.get("delete_ranges"), "done与删除区间不能同时生效")
            elif key == "editor_events":
                _require(isinstance(data.get("delete_events"), list)
                         and isinstance(data.get("delete_ranges"), list),
                         "缺少事件删除数组delete_events/delete_ranges")
                ids = list(data["delete_events"])
                for item in data["delete_ranges"]:
                    _require(isinstance(item, list) and len(item) == 2,
                             "delete_ranges必须是两个事件ID组成的数组")
                    ids.extend(item)
                auth = re.search(r"当前批可写事件[^\n]*只允许这些 ID[：:]([^】\n]*)", user)
                source = auth.group(1) if auth else user
                allowed = {value.upper() for value in re.findall(r"\b[UG]\d+\b", source, re.I)}
                _require(all(isinstance(value, str) and re.fullmatch(r"[UG]\d+", value)
                             and value in allowed for value in ids),
                         "删除结果包含未授权事件ID或自由秒数")
            else:
                _require(isinstance(data.get("delete_ranges"), list), "缺少delete_ranges数组")
                bounds = None
                if key == "editor_step":
                    total = re.search(r"序列总长\s*(\d+(?:\.\d+)?)\s*秒", user)
                    if total:
                        bounds = (0.0, float(total.group(1)))
                elif key == "editor_ranges":
                    rows = re.findall(r"^\s*\d+\s+(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)", user, re.M)
                    if rows:
                        bounds = (min(float(a) for a, _b in rows),
                                  max(float(b) for _a, b in rows))
                for item in data["delete_ranges"]:
                    _require(isinstance(item, list) and len(item) == 2
                             and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                     and math.isfinite(value) for value in item)
                             and 0 <= item[0] < item[1], "删除区间必须是有效的数值起止秒数")
                    _require(bounds is None or bounds[0] - 0.01 <= item[0] < item[1] <= bounds[1] + 0.01,
                             "删除区间超出当前批或序列的时间范围")
        elif key == "subtitle_groups":
            groups = data.get("groups")
            _require(isinstance(groups, list)
                     and all(isinstance(group, list) and 1 <= len(group) <= 3
                             and all(type(value) is int for value in group) for group in groups),
                     "groups必须由最多3个编号组成")
            _require([value for group in groups for value in group] == _numbers(user),
                     "字幕分组必须按顺序完整覆盖输入且不能重复")
        elif key == "subtitle_fixed":
            fixed = data.get("fixed")
            _require(isinstance(fixed, list) and all(isinstance(value, str) for value in fixed)
                     and len(fixed) == len(_numbers(user)), "fixed数量必须和输入字幕一致")
            numbered = {}
            for line in user.splitlines():
                match = re.fullmatch(r"\s*(\d+)\s*[.、:：|｜]\s*(.+?)\s*", line)
                if match:
                    body = match.group(2)
                    if re.search(r"[|｜]", body):
                        body = re.split(r"[|｜]", body)[-1].strip()
                    else:
                        body = re.sub(r"^\s*\d+(?:\.\d+)?\s*[~～-]\s*\d+(?:\.\d+)?\s*", "", body)
                    numbered[int(match.group(1))] = body
            original = [numbered.get(number) for number in _numbers(user)]
            _require(all(value is not None for value in original), "无法读取待校对的原字幕")
            _preserve_proofread_rows(original, fixed, user)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if key == "legacy_delete":
        data = _json(raw)
        # Keep the legacy delete field because its parser does not read delete_ranges.
        canonical = {"delete_ranges": data.get("delete"), "reason": data.get("reason")}
        validate("editor_ranges", json.dumps(canonical, ensure_ascii=False), user)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if key == "retake":
        data = _json(raw)
        _require(type(data.get("same")) is bool and type(data.get("a_abandoned")) is bool
                 and isinstance(data.get("why"), str), "重录裁决必须包含same/a_abandoned/why")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if key == "writer_read":
        _require(len(lines) == 3 and re.fullmatch(r"摘要[：:].+", lines[0])
                 and re.fullmatch(r"重要度[：:]\s*[1-5]", lines[1])
                 and re.fullmatch(r"关键[：:].*", lines[2]), "通读结果必须为摘要/重要度/关键三行")
        body = re.split(r"[：:]", lines[2], maxsplit=1)[-1].strip()
        ids = re.findall(r"\bU\d+\b", body)
        allowed = set(re.findall(r"\bU\d+\b", _input(user, "时间轴事件：")))
        _require((body in ("无", "") or bool(re.fullmatch(r"U\d+(?:\s*[,，]\s*U\d+){0,2}", body)))
                 and len(ids) <= 3 and len(ids) == len(set(ids)) and set(ids) <= allowed,
                 "关键必须是当前批最多3个U事件ID")
    elif key == "writer_plan":
        _require(bool(lines) and re.fullmatch(r"主线[：:].+", lines[-1]), "剧本缺少主线")
        chunks = re.findall(r"^\s*(B\d+)\s*/", _input(user, "时间轴段摘要："), re.M)
        positions = {value: index for index, value in enumerate(chunks)}
        _require(1 <= len(lines) - 1 <= 10, "章节数必须为1到10")
        last = -1
        for index, line in enumerate(lines[:-1], 1):
            parts = [value.strip() for value in re.split(r"[|｜]", line)]
            _require(len(parts) == 5 and parts[0] == str(index) and bool(parts[3])
                     and parts[1] in positions and parts[2] in positions
                     and parts[4] in ("全部保留", "只留关键", "可以大幅删减"),
                     "章节必须使用已有B段ID和规定的五列格式")
            _require(last < positions[parts[1]] <= positions[parts[2]], "章节边界必须按顺序且不重叠")
            last = positions[parts[2]]
    elif key in ("semantic", "proofread", "proofread_pipe"):
        marker = ("台词：" if key == "semantic" else
                  "待校对（只输出这些编号）：" if key == "proofread_pipe" else
                  "【待校对（只输出这些编号）】")
        source = _input(user, marker)
        expected = _numbers(source)
        pattern = (r"(\d+)\s*[|｜]\s*(speech|laugh|noise|filler)\s*[|｜]\s*([P-])"
                   if key == "semantic" else r"(\d+)\s*[|｜]\s*(.+)"
                   if key == "proofread_pipe" else r"(\d+)\s*[.、:：]\s*(.+)")
        matches = [re.fullmatch(pattern, line) for line in lines]
        _require(all(matches) and [int(match.group(1)) for match in matches] == expected
                 and bool(expected), "每个输入编号必须按顺序输出且只能输出一次")
        if key in ("proofread", "proofread_pipe"):
            source_pattern = (r"\s*(\d+)\s*[|｜]\s*(.+?)\s*" if key == "proofread_pipe"
                              else r"\s*(\d+)\s*[.、:：]\s*(.+?)\s*")
            originals = {}
            for line in source.splitlines():
                source_match = re.fullmatch(source_pattern, line)
                if source_match:
                    originals[int(source_match.group(1))] = source_match.group(2)
            _require(all(number in originals for number in expected), "无法读取待校对的原字幕")
            _preserve_proofread_rows(
                [originals[number] for number in expected],
                [match.group(2) for match in matches], user)
    elif key in ("director_keep", "director_review"):
        allowed = set(_numbers(user))
        if key == "director_keep" and raw == "NONE":
            return raw
        if key == "director_review" and raw == "OK":
            return raw
        pattern = r"(\d+)" if key == "director_keep" else r"(?:ADD|DROP)\s+(\d+)"
        matches = [re.fullmatch(pattern, line) for line in lines]
        _require(bool(lines) and all(matches)
                 and all(int(match.group(1)) in allowed for match in matches), "导演只能返回已有编号或规定的OK/NONE")
    elif key == "continuity":
        _require(len(lines) == 3 and re.fullmatch(r"结果[：:]\s*(通|勉强|断)", lines[0])
                 and re.fullmatch(r"理由[：:].+", lines[1]) and re.fullmatch(r"缺口[：:].+", lines[2]),
                 "连贯审查必须为结果/理由/缺口三行")
    elif key == "intro":
        _require(raw == "0" or bool(lines) and all(line.isdigit()
                 and int(line) in set(_numbers(user)) for line in lines), "片头识别只能输出已有编号或0")
    elif key == "boundary":
        _require(bool(re.fullmatch(r"OK|(?:EXTEND|BACK)\s+[1-4]", raw)), "接缝审片只能输出OK/EXTEND/BACK")
    elif key == "chapter_summary":
        _require(len(lines) == 1 and len(raw) <= 25, "章节摘要必须是25字内的一句话")
    return raw

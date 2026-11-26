# -*- coding: utf-8 -*-
"""字幕纠错：用本地小模型（Qwen3-4B）修正语音识别的同音字和专有名词。

为什么需要它：实测识别结果里
    水超乌战士 → 水槽无战事（系列名）
    精灰魔法   → 星辉魔法
    辅照器     → 辐照器
这些错字会直接毁掉后续的语义判断（P1 感叹词命中）和标题生成。

设计要点：
  · 模型不知道你的术语，必须喂词表（实测：不给词表它就原样返回）
  · 只改错字，不改语序、不加标点、不补全 —— 提示词里写死，输出后再做长度校验
  · 批量送（一次 8 句），用常驻 llama-server，避免每句都重新加载模型
  · 每处改动都记进「订正记录.md」，可以逐条复核，不满意就还原

用法：
    python proofread.py <索引目录或 index.json>          # 纠错并写回
    python proofread.py <索引目录> --dry-run             # 只看会改成什么，不写
    python proofread.py <索引目录> --batch 12            # 调整批量
    python proofread.py --make-terms                     # 生成术语表模板
"""


try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from memory_config import app_home

BASE = app_home()
CONF_DIR = os.path.join(BASE, "conf")
TERMS_PATH = os.path.join(CONF_DIR, "terms.txt")

LLAMA_DIR = os.path.join(BASE, "bin", "llama")
LLM_DIR = os.path.join(BASE, "models", "llm")
SERVER_EXE = os.path.join(LLAMA_DIR, "llama-server.exe")

DEFAULT_MODEL = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"

DEFAULT_TERMS = """# 字幕术语表
# 每行：正确写法|错误写法1,错误写法2,...
# 只写正确写法也行（模型会结合上下文判断），但把常见错误列出来效果最好。
# 改完保存即可，不用重启程序。

水槽无战事|水超乌战士,水槽乌战事,水草无战事,水槽无战士,水槽 吴战士,水槽 战士
子枫|子凤
六氟化铀|六伏化油,六幅化油
富硅岩燃料|韩服硅岩燃料,含氟硅岩燃料,含氟硅岩油燃料,含福归盐油燃料,含福归盐油
液态富硅岩燃料|液态领兵燃料,液态含浮燃料
六氟化硅岩|六伏化硅岩,六伏化硅盐
氟化氢|六伏化氢,六幅化氢
氢氟酸|氢伏酸,氢浮酸
硅岩锭|硅岩定,硅盐定
氧化铀|氧化油
超限算枢|超线算书,超线算数,超线算术,超限算术
量子灌注液|量子关注页,量子关注液
星辉魔法|精灰魔法,星灰魔法,星辉魔发
辐照器|辅照器,浮照器,付照器
下界合金|轻金石,下届合金
氟酸|浮酸,伏酸
硅岩|归岩,龟岩,硅盐
戴斯|带丝,待死
机械动力|机械东西,机械动力学
通用机械|通用机械学,通机械
应用能源|应用能源2,AE2,应用能原
沉浸工程|沉浸攻城,沉浸工程学
格雷科技|格蕾科技,格雷科记
硫酸|流酸,硫算
氧化|养化
冷凝|冷盈
已达研究上限|以达研究上线
闪电装备室|闪电装备是
盈能福鲁伊克斯水晶|银能 弗里克斯水晶,银能弗里克斯水晶
福鲁伊克斯水晶|弗里克斯水晶
高纯福鲁伊克斯水晶|高层弗里克斯水晶
量子锭|量子定
就差青金石了|下清津时了
青金石|氢金石
星辉锭|星辉钉,星辉定
无限量子计算机多线程处理器|限量子计算机多线程处理器
石英|时音
猛猛涨|蒙蒙涨
液态反物质|液态反复制
"""


def log(msg=""):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 术语表
# --------------------------------------------------------------------------
def ensure_terms():
    os.makedirs(CONF_DIR, exist_ok=True)
    if not os.path.isfile(TERMS_PATH):
        with open(TERMS_PATH, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_TERMS)
        log("已生成术语表模板：%s" % TERMS_PATH)
    return TERMS_PATH


KB_DIR = os.path.join(BASE, "kb")


def load_kb():
    """读 kb/*.txt，返回 [(正确写法, [错误写法], 分类名)]。

    每个分类一个文件，方便你自己往里加。格式和 conf/terms.txt 一样：
        正确写法|错误写法1,错误写法2
    只写正确写法也可以（比如口头语那份）。
    """
    entries = []
    if not os.path.isdir(KB_DIR):
        return entries
    for name in sorted(os.listdir(KB_DIR)):
        if not name.endswith(".txt"):
            continue
        cat = os.path.splitext(name)[0]
        with open(os.path.join(KB_DIR, name), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    right, wrongs = line.split("|", 1)
                    ws = [w.strip() for w in wrongs.replace("，", ",").split(",")
                          if w.strip()]
                else:
                    right, ws = line, []
                right = right.strip()
                if right:
                    entries.append((right, ws, cat))
    return entries


def apply_known_terms(text, entries):
    """规则层：把知识库里已知的错误写法全部替换掉（所有出现位置）。

    实测模型做机械替换会漏：一句里三处「孵」它只改一处。
    这类替换本该由规则做——不漏、不花时间、不看上下文也不会错。
    长词优先，避免「氢浮酸」被「浮酸」先切开。
    """
    pairs = []
    for right, wrongs, _cat in entries:
        for w in wrongs:
            if len(w) >= 2:
                pairs.append((w, right))
    pairs.sort(key=lambda x: -len(x[0]))
    out = text
    hits = 0
    for w, r in pairs:
        if w in out:
            hits += out.count(w)
            out = out.replace(w, r)
    return out, hits


def select_entries(texts, entries, max_chars=600):
    """只挑出「这批句子里可能用得上」的条目。

    为什么要挑：一次纠错 8 句，如果把 250 条术语全塞进去，
    提示词会涨到几千字，模型反而更容易看漏、变慢。
    做法：句子里出现了某条的错误写法或正确写法 → 直接命中；
    否则看它的错误写法和句子的字面重合度，够高也算弱命中。
    """
    blob = "".join(texts)
    strong, weak = [], []
    for right, wrongs, cat in entries:
        hit = False
        for w in wrongs:
            # 单字的错误写法不能做子串匹配：「油」会命中「氧化油」，
            # 于是把「铀←油」塞进提示词，模型可能真把「油」改成「铀」。
            if w and len(w) >= 2 and w in blob:
                hit = True
                break
        if not hit and right in blob:
            hit = True
        if hit:
            strong.append((right, wrongs, cat))
            continue
        # 弱命中：错误写法里有两个字出现在句中（同音字错位时常见）
        for w in wrongs:
            if len(w) >= 2 and sum(1 for ch in w if ch in blob) >= 2:
                weak.append((right, wrongs, cat))
                break

    # A large generated mod glossary usually contains only the canonical
    # spelling.  Literal matching therefore cannot surface terms such as
    # ``量子锭`` when ASR emitted the homophone ``量子定``.  Build a small
    # in-memory pinyin index once per loaded glossary and retrieve only
    # same-length, mostly-same-character candidates from the current batch.
    # These rows are prompt context only: the existing preservation gate still
    # requires the model's replacement to be a one-for-one homophone change.
    phonetic = _phonetic_candidates(texts, entries)

    chosen = []
    used = 0
    seen = set()
    for item in strong + phonetic + weak:
        key = (item[0], tuple(item[1]), item[2])
        if key in seen:
            continue
        seen.add(key)
        line = ("%s ← %s" % (item[0], "、".join(item[1]))) if item[1] else item[0]
        if used + len(line) > max_chars:
            break
        chosen.append(item)
        used += len(line) + 1
    return chosen, used


_PHONETIC_ENTRY_CACHE = {}


def _phonetic_candidates(texts, entries, max_candidates=32):
    """Return glossary terms pronounced like suspicious transcript n-grams.

    The full mod dictionary can exceed 100k rows, so it is indexed once by
    ``(character count, pinyin sequence)``.  Candidate retrieval is deliberately
    conservative: at least half of the written characters must already match,
    and only pure CJK terms of two to eight characters participate.
    """
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return []

    cache_key = id(entries)
    cached = _PHONETIC_ENTRY_CACHE.get(cache_key)
    if cached is None or cached[0] is not entries:
        index = {}
        for item in entries or ():
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            right = str(item[0] or "").strip()
            if not (2 <= len(right) <= 8) or not re.fullmatch(
                    r"[\u3400-\u9fff\uf900-\ufaff]+", right):
                continue
            key = (len(right), tuple(lazy_pinyin(
                right, style=Style.NORMAL, errors="ignore")))
            if len(key[1]) != len(right):
                continue
            index.setdefault(key, []).append(item)
        cached = (entries, index)
        _PHONETIC_ENTRY_CACHE.clear()
        _PHONETIC_ENTRY_CACHE[cache_key] = cached
    index = cached[1]

    matches = {}
    for text in texts or ():
        for run in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]{2,}", str(text or "")):
            for length in range(2, min(8, len(run)) + 1):
                for offset in range(0, len(run) - length + 1):
                    fragment = run[offset:offset + length]
                    key = (length, tuple(lazy_pinyin(
                        fragment, style=Style.NORMAL, errors="ignore")))
                    for item in index.get(key, ()):
                        right = str(item[0])
                        if right == fragment:
                            continue
                        same = sum(a == b for a, b in zip(fragment, right))
                        if same < max(1, (length + 1) // 2):
                            continue
                        score = (same / float(length), same, length)
                        old = matches.get(right)
                        if old is None or score > old[0]:
                            matches[right] = (score, item)
    ranked = sorted(matches.values(), key=lambda row: row[0], reverse=True)
    return [item for _score, item in ranked[:max(1, int(max_candidates))]]


def load_terms():
    """返回 [(正确写法, [错误写法...])]，以及给模型看的术语表文本。"""
    ensure_terms()
    pairs = []
    with open(TERMS_PATH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                right, wrongs = line.split("|", 1)
                right = right.strip()
                ws = [w.strip() for w in wrongs.replace("，", ",").split(",")
                      if w.strip()]
            else:
                right, ws = line, []
            if right:
                pairs.append((right, ws))
    table = "\n".join(
        ("%s ← %s" % (r, "、".join(ws))) if ws else r for r, ws in pairs)
    return pairs, table


def load_resources():
    """Load glossary/KB once: return (table, wrong_set, merged_kb_entries)."""
    pairs, table = load_terms()
    kb_all = list(load_kb()) + [(right, wrongs, "自定义") for right, wrongs in pairs]
    wrong_set = {wrong for _right, wrongs, _category in kb_all for wrong in wrongs}
    return table, wrong_set, kb_all


# --------------------------------------------------------------------------
# 模型服务
# --------------------------------------------------------------------------
def _port_busy(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def _pid_on_port(port: int):
    """谁占着这个端口（拿不到就返回 None）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, errors="replace", timeout=8,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":%d" % int(port)) \
                and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _ps_json(cmd: str, timeout=12):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                              "-Command", cmd],
                             capture_output=True, text=True, errors="replace",
                             timeout=timeout,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
        s = (out or "").strip()
        return json.loads(s) if s else None
    except Exception:  # noqa: BLE001
        return None


def _image_of(pid: int) -> str:
    if not pid:
        return ""
    d = _ps_json("(Get-Process -Id %d -ErrorAction SilentlyContinue).Path" % pid)
    if isinstance(d, str):
        return d
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-Process -Id %d -ErrorAction SilentlyContinue).Path"
                              % pid], capture_output=True, text=True,
                             errors="replace", timeout=10,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
        return (out or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _cuda_ok():
    """本机有没有能用的 CUDA 设备（没有就把 llama-server 放 CPU 跑）。"""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True,
                           errors="replace", timeout=8,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode == 0 and "GPU 0" in (r.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def kill_stale_servers(on_log=None, max_age_s=600):
    """收掉"上一条命"留下的 llama-server。

    为什么会留：程序被强关/崩溃时，子进程不会跟着死。
    它继续占着端口和显存，下一次分析就会去连这个僵尸服务——
    请求发出去没人应答，界面就"卡住"了（实测 8134 被占了一整晚）。

    判定"僵尸"：进程活了很久（> max_age_s）且当前没有任何已建立的连接。
    正在被使用的服务一定有连接，或者进程很新，不会被误杀。

    注意：Windows 11 已经把 wmic 删掉了，所以走 PowerShell 的 CIM，
    不能再用 wmic（用 wmic 会静默失败，一个都杀不掉——踩过）。
    """
    raw = _ps_json(
        "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        "Select-Object ProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress")
    if not raw:
        return 0
    entries = raw if isinstance(raw, list) else [raw]
    killed = 0
    import datetime as _dt
    now = _dt.datetime.now()
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            pid = int(e.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        age = 0.0
        cd = e.get("CreationDate")
        if isinstance(cd, dict):                     # {"value": "2026-11-21T00:31:39"}
            cd = cd.get("value") or cd.get("DateTime")
        if isinstance(cd, str):
            t = None
            if cd.startswith("/Date(") and ")" in cd:
                try:
                    t = _dt.datetime.fromtimestamp(
                        int(cd[6:cd.index(")")]) / 1000.0)
                except (ValueError, OSError):
                    t = None
            else:
                try:
                    t = _dt.datetime.fromisoformat(cd.replace("Z", "")[:19])
                except ValueError:
                    t = None
            if t:
                age = (now - t).total_seconds()
        if age and age < max_age_s:
            continue
        busy = False
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, errors="replace",
                                 timeout=12,
                                 creationflags=0x08000000 if os.name == "nt" else 0).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[4] == str(pid) and \
                        parts[3].upper() == "ESTABLISHED":
                    busy = True
                    break
        except Exception:  # noqa: BLE001
            busy = True                    # 查不清楚就别动
        if busy:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8,
                           creationflags=0x08000000 if os.name == "nt" else 0)
            killed += 1
        except Exception:  # noqa: BLE001
            pass
    if killed and on_log:
        on_log("  清掉了 %d 个上次没退干净的本地模型服务" % killed)
    return killed


_SHARED_SERVER = None      # 复用的服务（同一次运行只起一次）


def _shutdown_shared():
    global _SHARED_SERVER
    srv = _SHARED_SERVER
    _SHARED_SERVER = None
    if srv is not None and srv.proc is not None and srv.proc.poll() is None:
        try:
            srv.proc.terminate()
            srv.proc.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                srv.proc.kill()
            except Exception:
                pass


try:
    import atexit as _atexit
    _atexit.register(_shutdown_shared)
except Exception:  # noqa: BLE001
    pass


class Server:
    def __init__(self, port=8123, model=None, gpu_layers=None, task=None, ctx=4096,
                 api_role=None):
        self.port = port
        from llm_gateway import OpenAICompatibleClient
        role = api_role or ({"paragraph": "writer"}.get(str(task), "analyst"))
        self.api = OpenAICompatibleClient.from_config(BASE, role=role)
        if not self.api.enabled and model is None:
            try:
                import llm_config
                model = llm_config.pick(task or "complete")
            except Exception:  # noqa: BLE001
                model = None
        self.model = model or os.path.join(LLM_DIR, DEFAULT_MODEL)
        # 新电脑可能没有 N 卡：有 CUDA 才把层放显卡（-ngl 99），
        # 否则必须 -ngl 0 走 CPU，不然 llama-server 直接起不来。
        if gpu_layers is None:
            gpu_layers = 0 if self.api.enabled else (99 if _cuda_ok() else 0)
        self.gpu_layers = gpu_layers
        self.ctx = ctx
        self.proc = None
        self._shared = False

    def _healthy(self) -> bool:
        try:
            if self.proc is None or self.proc.poll() is not None:
                return False
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % self.port, timeout=2) as r:
                return json.load(r).get("status") == "ok"
        except Exception:  # noqa: BLE001
            return False

    def __enter__(self):
        global _SHARED_SERVER
        # 用户选择 API 后完全不启动本地 llama-server；失败由远端异常直接
        # 返回并记录到 logs/ai-api.log，避免暗中换模型导致结果不可追踪。
        if self.api.enabled:
            if _SHARED_SERVER is not None:
                _shutdown_shared()
            log("  使用 AI API：%s / %s / %s"
                % (self.api.provider, self.api.role, self.api.model))
            return self
        # 同模型且上下文够用就复用。调用方使用不同端口只是为了避免历史冲突，
        # 并不需要同时驻留多份相同模型；复用能避免分析过程中反复起控制台宿主，
        # 也避免旧服务被新引用覆盖后继续占显存。
        sh = _SHARED_SERVER
        if (sh is not None and sh is not self and sh.model == self.model
                and sh.gpu_layers == self.gpu_layers and sh.ctx >= self.ctx
                and sh._healthy()):
            self.proc = sh.proc
            self.port = sh.port
            self._shared = True
            return self
        # 不兼容的共享服务必须先收掉；旧实现直接覆盖全局引用，导致每个阶段
        # 都残留一份 llama-server，进而重复弹窗并持续占用显存。
        if sh is not None and sh is not self:
            _shutdown_shared()
        if not os.path.isfile(SERVER_EXE):
            raise RuntimeError(
                "找不到 llama-server。先跑一次：python setup_llm.py")
        if not os.path.isfile(self.model):
            raise RuntimeError("找不到模型文件：%s\n先跑：python setup_llm.py" % self.model)

        # 端口被别人占着：先分清是谁。
        # 是个没退干净的 llama-server → 收掉它；收不掉就换端口。
        # 绝不能"起个新进程然后去连旧服务的健康检查"——那样新进程因为端口冲突
        # 已经死了，代码却以为就绪，后面每个请求都发进黑洞，界面就"卡住"。
        if _port_busy(self.port):
            pid = _pid_on_port(self.port)
            exe = _image_of(pid) if pid else ""
            same = (exe and os.path.isfile(SERVER_EXE)
                    and os.path.normcase(os.path.abspath(exe))
                    == os.path.normcase(os.path.abspath(SERVER_EXE)))
            if same and pid:
                log("  端口 %d 被上次没退的服务占着（PID %d），先收掉…"
                    % (self.port, pid))
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=8,
                               creationflags=0x08000000 if os.name == "nt" else 0)
                for _ in range(20):
                    if not _port_busy(self.port):
                        break
                    time.sleep(0.25)
            if _port_busy(self.port):
                base = int(self.port)
                for cand in range(base + 10, base + 60, 10):
                    if not _port_busy(cand):
                        log("  端口 %d 被别的程序占着，改用 %d" % (self.port, cand))
                        self.port = cand
                        break
                else:
                    raise RuntimeError("端口 %d 被占用，且找不到可用端口" % self.port)

        log("  启动本地模型服务（首次加载约 2 秒）…")
        command = [
            SERVER_EXE, "-m", self.model, "-ngl", str(self.gpu_layers),
            "-c", str(self.ctx), "--port", str(self.port), "--host", "127.0.0.1",
            "--no-warmup", "-t", str(max(4, (os.cpu_count() or 8) // 2)),
        ]
        try:
            import winproc
            self.proc = winproc.popen_hidden(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                detached=True)
        except Exception:  # noqa: BLE001
            creation = ((0x08000000 | 0x00000008) if os.name == "nt" else 0)
            self.proc = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation)
        for _ in range(240):
            time.sleep(0.5)
            # 先看自己起的进程：它死了就别再等（端口冲突/显存不足都会这样）
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "模型服务启动失败（进程退出码 %s，端口 %d）"
                    % (self.proc.returncode, self.port))
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port, timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        log("  服务就绪")
                        _SHARED_SERVER = self
                        return self
            except Exception:  # noqa: BLE001
                continue
        # 超时：把自己起的进程收掉，避免又留一个僵尸
        if self.proc.poll() is None:
            self.proc.kill()
        raise RuntimeError("模型服务 120 秒内没就绪（端口 %d）" % self.port)

    def __exit__(self, *exc):
        # 复用别人的服务、或自己就是共享服务 → 不在这里杀，退出程序时统一收
        if getattr(self, "_shared", False):
            return
        if _SHARED_SERVER is self:
            return
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                self.proc.kill()

    def chat(self, system, user, max_tokens=600, timeout=300):
        """走对话接口。指令类任务必须用它——原始补全容易把编号列表续写下去。"""
        if self.api.enabled:
            return self.api.chat(system, user, max_tokens=max_tokens,
                                 temperature=0.0)
        payload = {"messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0, "max_tokens": max_tokens,
                   "stream": False}
        try:
            import llm_config
            if llm_config.load().get("disable_thinking", True):
                # Qwen3 是思考型：不关掉会把推理过程也吐出来
                payload["chat_template_kwargs"] = {"enable_thinking": False}
        except Exception:  # noqa: BLE001
            pass
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self.port, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        return d["choices"][0]["message"]["content"]

    def complete(self, prompt, n_predict=512, timeout=180):
        if self.api.enabled:
            return self.api.chat(
                "按输入合同直接续写结果，不要解释或添加前后缀。",
                prompt, max_tokens=n_predict, temperature=0.0)
        body = json.dumps({
            "prompt": prompt, "n_predict": n_predict, "temperature": 0.0,
            "top_k": 1, "repeat_penalty": 1.0, "stop": ["\n\n\n"],
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/completion" % self.port, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("content", "")


# --------------------------------------------------------------------------
# 纠错
# --------------------------------------------------------------------------
PROMPT_HEAD = """你是 Minecraft 科技整合包视频的字幕校对员。

【术语表】左边是正确写法，右边是语音识别可能出现的错误写法：
%s

【规则】
1. 把句子里命中术语表的错误写法替换成正确写法。
2. 结合只读上下文，只改有依据的同音错字；拿不准时保留原文。
3. 除了改错字，一个字都不要动：不加标点、不改语序、不补全、不删减。
   不删语气词、不删重复字和句尾字，不扩写，不概括，不新增省略号。
   人称、说话人、数字和单位保持原样；专名只能按明确术语表修正。
   普通错字只能一字换一字，而且替换前后读音必须相同；不要改近义词。
4. 如果某句没错，就原样输出。
5. 输出格式：每行「编号. 改正后的句子」，编号与输入一一对应，不要任何解释。

【输入】
%s

【输出】
"""

BAD_MARK = ("抱歉", "无法", "我不能", "注意：", "说明：", "以下是")


def known_pairs_from_entries(entries):
    """Convert existing (right, wrongs[, category]) terms to (wrong, right)."""
    pairs = []
    for entry in entries or ():
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        right, wrongs = str(entry[0] or "").strip(), entry[1]
        if not right:
            continue
        pairs.append((right, right))  # Protect pronoun characters inside a name.
        if isinstance(wrongs, str):
            wrongs = re.split(r"[,，、]", wrongs)
        for wrong in wrongs or ():
            wrong = str(wrong or "").strip()
            if wrong:
                pairs.append((wrong, right))
    return list(dict.fromkeys(pairs))


def known_pairs_from_table(table):
    """Read only explicit glossary rows, never infer corrections from prose."""
    entries = []
    for line in str(table or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "【", "（")):
            continue
        parts = re.split(r"\s*←\s*|\s*\|\s*", line, maxsplit=1)
        if len(parts) == 2:
            entries.append((parts[0], re.split(r"[,，、]", parts[1])))
        elif re.fullmatch(r"[\w\u4e00-\u9fff -]{2,40}", line):
            entries.append((line, []))
    return known_pairs_from_entries(entries)


def relevant_known_pairs(texts, entries, table="", max_chars=12000):
    """Bound a large KB to explicit terms relevant to the supplied transcript."""
    selected, _used = select_entries(list(texts or ()), entries or (), max_chars=max_chars)
    return list(dict.fromkeys(known_pairs_from_table(table)
                              + known_pairs_from_entries(selected)))


def trusted_known_pairs(texts, entries=(), table="", min_kb_alias_chars=4):
    """Return only user-confirmed substitutions safe for automatic writes.

    Bundled KB rows remain model prompt context and ASR candidates.  They are
    not authoritative enough to rewrite transcript text: real material showed
    valid phrases such as ``硅岩锭``/``硅岩燃`` being collapsed to another KB
    entry.  Exact aliases in ``conf/terms.txt`` are the sole deterministic
    write rules; contextual same-length homophone fixes still pass the existing
    conservative model gate separately.
    """
    del texts, entries, min_kb_alias_chars
    return known_pairs_from_table(table)


def _canonical_terms(text, pairs):
    choices = {}
    for wrong, right in pairs:
        if wrong and right:
            choices.setdefault(str(wrong), set()).add(str(right))
    mapping = {wrong: next(iter(rights)) for wrong, rights in choices.items()
               if len(rights) == 1}
    if not mapping:
        return text
    pattern = "|".join(re.escape(word) for word in sorted(mapping, key=len, reverse=True))
    return re.sub(pattern, lambda match: mapping[match.group(0)], text)


def _pronouns(text, pairs):
    # A known proper name such as 我的世界/吉他 must not be treated as a speaker.
    terms = {str(right) for _wrong, right in pairs if len(str(right)) >= 3}
    terms.update(("其他", "其它", "吉他", "自我", "忘我", "无我"))
    if terms:
        pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
        text = re.sub(pattern, "", text)
    found = []
    multi = r"我们|你们|他们|她们|它们|咱们|俺们|您们|我俩|你俩|他俩|她俩|它俩"
    masked = list(text)
    for match in re.finditer(multi, text):
        found.append((match.start(), match.group(0)))
        for index in range(match.start(), match.end()):
            masked[index] = "_"
    grammar_after = ("是", "要", "想", "会", "能", "的", "来", "去", "有", "没", "把",
                     "被", "在", "就", "也", "都", "现在", "刚才", "这", "那", "不", "已经", "正在")
    grammar_before = set("给叫让问找帮跟和对是看等向陪替")
    for index, char in enumerate(masked):
        if char not in "我你他她它咱俺您":
            continue
        before = text[index - 1] if index else ""
        after = text[index + 1:]
        standalone = (index == 0 or before.isspace()
                      or bool(re.match(r"[，。！？、；：,.!?;:]", before))
                      or before in grammar_before
                      or any(after.startswith(word) for word in grammar_after))
        if standalone:
            found.append((index, char))
    return [token for _position, token in sorted(found)]


def correction_violation(old, new, known_pairs=()):
    """Return an objective preservation error, or '' for a valid candidate.

    known_pairs contains explicit (wrong, right) glossary substitutions. Only
    these may change length or an otherwise protected name/number spelling.
    This validates preservation, not whether a same-length correction is true.
    """
    import difflib
    import unicodedata

    old, new = str(old or ""), str(new or "")
    if old == new:
        return ""
    if not new.strip():
        return "校对不得清空台词"
    if any(marker in new and marker not in old for marker in BAD_MARK):
        return "校对不得添加解释或总结"
    summary = r"^\s*(?:总结|概括|摘要|修正后|校对后|改写后|本句意思)[：:]"
    if re.match(summary, new) and not re.match(summary, old):
        return "校对不得添加解释或总结"
    ellipsis = r"…+|⋯+|\.{3,}|。{3,}"
    if re.findall(ellipsis, new) != re.findall(ellipsis, old):
        return "校对不得新增、删除或改造省略号"
    pairs = list(known_pairs or ())
    left, right = _canonical_terms(old, pairs), _canonical_terms(new, pairs)
    if left == right:
        return ""
    if _pronouns(left, pairs) != _pronouns(right, pairs):
        return "校对不得更换人称或说话人"
    numbers = r"\d+(?:[.,]\d+)*(?:[%％])?|[零〇一二三四五六七八九十百千万亿两]+"
    if re.findall(numbers, left) != re.findall(numbers, right):
        return "校对不得改动数字"
    speaker = r"^\s*([^\n：:]{1,20}[：:])"
    a, b = re.match(speaker, left), re.match(speaker, right)
    if (a.group(1) if a else None) != (b.group(1) if b else None):
        return "校对不得更换说话人标签"
    punctuation = lambda text: [ch for ch in text if unicodedata.category(ch)[0] in "PS"]
    if punctuation(left) != punctuation(right):
        return "校对不得增删或替换标点"
    touched = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, left, right, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or i2 - i1 != j2 - j1:
            return "校对不得增删原句内容，变长术语必须有明确词表"
        touched += i2 - i1
    if touched > max(1, min(4, int(len(left) * 0.3))):
        return "校对改动过多，不能改写或概括原句"
    return ""


def accept_correction(old, new, known_pairs=(), allow_homophone=False):
    """Accept trusted glossary changes; bare homophones are opt-in only.

    Phonetic similarity proves a change is possible, not that it is correct.
    Keeping it opt-in prevents common words such as ``东西`` from being
    automatically changed to a same-sounding item name such as ``东锡``.
    """
    import difflib

    old, new = str(old or ""), str(new or "")
    pairs = list(known_pairs or ())
    if old == new or correction_violation(old, new, pairs):
        return False
    left, right = _canonical_terms(old, pairs), _canonical_terms(new, pairs)
    if left == right:
        return True
    if not allow_homophone:
        return False
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        return False
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, left, right, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for before, after in zip(left[i1:i2], right[j1:j2]):
            if before == after:
                continue
            if not re.fullmatch(r"[\u4e00-\u9fff]", before) or not re.fullmatch(
                    r"[\u4e00-\u9fff]", after):
                return False
            a = set(pinyin(before, style=Style.NORMAL, heteronym=True)[0])
            b = set(pinyin(after, style=Style.NORMAL, heteronym=True)[0])
            if not a.intersection(b):
                return False
    return True


def _contextual_input(items, before=(), after=()):
    """邻句只给模型理解同音词，模型只允许输出待校对编号。"""
    chunks = []
    if before:
        chunks.append("【只读上文（不要输出）】\n" + "\n".join(
            str(text) for _cid, text in before))
    lines = "\n".join("%d. %s" % (i + 1, text)
                      for i, (_cid, text) in enumerate(items))
    chunks.append("【待校对（只输出这些编号）】\n" + lines)
    if after:
        chunks.append("【只读下文（不要输出）】\n" + "\n".join(
            str(text) for _cid, text in after))
    return "\n\n".join(chunks)


def build_batch(items, table, before=(), after=()):
    return PROMPT_HEAD % (table, _contextual_input(items, before, after))


def build_batch_kb(items, kb_entries, kb_all, before=(), after=()):
    """按批动态检索：只注入这批句子命中到的术语。"""
    chosen, used = select_entries(
        [t for _c, t in list(before) + list(items) + list(after)], kb_all)
    if not chosen:
        table = "（本批没有命中术语表，只按上下文改明显错字）"
    else:
        by_cat = {}
        for right, wrongs, cat in chosen:
            by_cat.setdefault(cat, []).append(
                ("%s ← %s" % (right, "、".join(wrongs))) if wrongs else right)
        chunks = []
        for cat, lines in by_cat.items():
            chunks.append("【%s】\n%s" % (cat, "\n".join(lines)))
        table = "\n".join(chunks)
    body = _contextual_input(items, before, after)
    return PROMPT_HEAD % (table, body), len(chosen), used


def parse_batch(raw, n):
    """从模型输出里抠出 n 条。返回 list[str] 或 None（数量对不上）。"""
    out = {}
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*(\d+)\s*[.、,，:：]\s*(.+?)\s*", line)
        if not m:
            return None
        idx = int(m.group(1))
        if idx != len(out) + 1 or not 1 <= idx <= n:
            return None
        out[idx] = m.group(2)
    if len(out) != n:
        return None
    return [out[i + 1] for i in range(n)]


def plausible(old, new, known_pairs=()):
    """Compatibility wrapper for conservative preservation checks."""
    return bool(new and new != old and not correction_violation(old, new, known_pairs))


def classify(old, new, wrong_set=None, known_pairs=()):
    """Legacy confidence API; a wrong spelling alone cannot authorise a rewrite."""
    return "high" if accept_correction(old, new, known_pairs) else "low"


def correct_texts(items, table, server, batch=8, wrong_set=None, kb_all=None,
                  context=4, allow_context_homophones=True, progress=None):
    """items: [(card_id, text)] → {card_id: corrected}，只保留真的改了的。"""
    changes = {}
    lowconf = {}
    rule_changes = {}
    failed = 0
    stats = {"terms": 0, "chars": 0, "rule_hits": 0}
    context = max(0, min(8, int(context)))
    # Full KB rows are prompt-only context.  Only the user-maintained glossary
    # may authorise deterministic replacements before/after the model call.
    known_pairs = trusted_known_pairs(
        [text for _cid, text in items], kb_all or (), table)

    # 规则层先跑：已知错写法全量替换。
    # 注意要单独记一份——即使模型没再改，这些改动也必须写回。
    base_items = []
    for cid, text in items:
        fixed = _canonical_terms(str(text), known_pairs)
        hits = int(fixed != text)
        stats["rule_hits"] += hits
        if fixed != text:
            rule_changes[cid] = fixed
        base_items.append((cid, fixed))
    if stats["rule_hits"]:
        log("  规则层先替换了 %d 处已知错写法（%d 条句子）"
            % (stats["rule_hits"], len(rule_changes)))
    items = base_items
    total = len(items)
    for start in range(0, total, batch):
        chunk = items[start:start + batch]
        before = items[max(0, start - context):start]
        after = items[start + len(chunk):start + len(chunk) + context]
        if kb_all:
            prompt, n_hit, n_chars = build_batch_kb(
                chunk, None, kb_all, before=before, after=after)
            stats["terms"] += n_hit
            stats["chars"] = max(stats["chars"], n_chars)
        else:
            prompt = build_batch(chunk, table, before=before, after=after)
        raw = server.complete(prompt,
                              n_predict=min(1024, 120 * len(chunk) + 120))
        got = parse_batch(raw, len(chunk))
        if got is None:
            # 8B 本地模型偶尔会漏一个编号。只拆成两个半批重试；仍不合
            # 同就整批保留原文，不能退化成几百次逐句调用。
            retried = []
            retry_ok = len(chunk) > 1
            middle = max(1, len(chunk) // 2)
            for offset in range(0, len(chunk), middle):
                part = chunk[offset:offset + middle]
                pos = start + offset
                part_before = items[max(0, pos - context):pos]
                part_after = items[pos + len(part):pos + len(part) + context]
                if kb_all:
                    part_prompt, _n, _chars = build_batch_kb(
                        part, None, kb_all, before=part_before, after=part_after)
                else:
                    part_prompt = build_batch(
                        part, table, before=part_before, after=part_after)
                part_raw = server.complete(
                    part_prompt, n_predict=min(768, 120 * len(part) + 120))
                part_got = parse_batch(part_raw, len(part))
                if part_got is None:
                    retry_ok = False
                    break
                retried.extend(part_got)
            got = retried if retry_ok and len(retried) == len(chunk) else [
                text for _cid, text in chunk]
            failed += 1
        for (cid, old), new in zip(chunk, got):
            new = (new or "").strip()
            # 模型有时会把规则层改好的地方又写回去，这里再兜一次
            new = _canonical_terms(new, known_pairs)
            if new == old:
                continue
            if accept_correction(
                    old, new, known_pairs,
                    allow_homophone=bool(allow_context_homophones)):
                changes[cid] = new
            else:
                lowconf[cid] = new
        done = min(start + batch, total)
        # 打包成 --windowed 后 sys.stdout 是 None，直接 write 会
        # 'NoneType' object has no attribute 'write'，整段语义校对被跳过
        # （logs\index_20261124_205849.log 实锤）。
        if progress is not None:
            # Windowed builds have no stdout.  Report a bounded number of
            # milestones through the caller's UI logger so a long 8B pass does
            # not look frozen for tens of minutes.
            milestone = max(batch, batch * max(1, (total + batch - 1) // batch // 10))
            if done == total or done == len(chunk) or done % milestone < batch:
                progress("  字幕校对进度：%d/%d 句" % (done, total))
        elif sys.stdout is not None:
            sys.stdout.write("\r  纠错进度 %d/%d" % (done, total))
            sys.stdout.flush()
    if progress is None and sys.stdout is not None:
        sys.stdout.write("\n")
    if failed:
        log("  （有 %d 批格式异常，已半批重试或保留原文）" % failed)
    if stats["terms"]:
        log("  （每批平均注入 %d 条术语，最多占 %d 字——不是把整本塞进去）"
            % (stats["terms"] // max(1, (total + batch - 1) // batch), stats["chars"]))
    # 规则层的改动 + 模型的改动，合并成最终结果
    merged = dict(rule_changes)
    merged.update(changes)
    return merged, lowconf


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("index", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default=None)
    ap.add_argument("--make-terms", action="store_true")
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args(argv)

    if args.make_terms:
        ensure_terms()
        log("术语表：%s" % TERMS_PATH)
        return 0
    if not args.index:
        ap.print_help()
        return 1

    path = args.index
    if os.path.isdir(path):
        path = os.path.join(path, "index.json")
    if not os.path.isfile(path):
        log("找不到：%s" % path)
        return 1

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    cards = data.get("cards") or []
    segments = data.get("segments") or []
    segment_rows = [{"id": "s%06d" % (i + 1), "start": row[0], "end": row[1],
                     "text": str(row[2]), "segment_index": i}
                    for i, row in enumerate(segments)
                    if isinstance(row, (list, tuple)) and len(row) >= 3]
    segment_mode = bool(segment_rows)
    source_rows = segment_rows if segment_mode else cards
    by_id = {row["id"]: row for row in source_rows}
    items = [(row["id"], (row.get("text") or "").strip()) for row in source_rows
             if (row.get("text") or "").strip()]
    log("=" * 66)
    log("  字幕纠错 · %s" % data.get("name", ""))
    log("  待校句子 %d 条" % len(items))
    log("=" * 66)
    if not items:
        log("没有可纠错的台词。")
        return 0

    table, wrong_set, kb_all = load_resources()
    log("  术语表与知识库合计 %d 条（%s）" % (len(kb_all), KB_DIR))

    t0 = time.time()
    with Server(port=args.port, model=args.model) as srv:
        changes, lowconf = correct_texts(items, table, srv, batch=args.batch,
                                         wrong_set=wrong_set, kb_all=kb_all)
    cost = time.time() - t0

    log("\n  高置信改动 %d 条，低置信（未应用）%d 条，用时 %.1f 秒"
        % (len(changes), len(lowconf), cost))
    if lowconf:
        log("  低置信的这些需要你人工看（模型想改但拿不准）：")
        for _cid, _new in list(lowconf.items())[:6]:
            log("    [%s] %s" % (_cid, (by_id[_cid].get("text") or "").strip()[:40]))
            log("         模型建议 → %s" % _new[:40])
    if not changes and not lowconf:
        log("  没有发现需要修正的地方。")
        return 0

    log("\n  前 12 处改动：")
    shown = 0
    for cid, new in sorted(changes.items(),
                           key=lambda kv: by_id[kv[0]]["start"]):
        old = (by_id[cid].get("text") or "").strip()
        log("   [%s] %s" % (cid, old[:56]))
        log("        → %s" % new[:56])
        shown += 1
        if shown >= 12:
            log("   …")
            break

    if args.dry_run:
        log("\n（--dry-run，没有写回）")
        return 0

    # 备份 + 写回 + 订正记录
    idx_dir = os.path.dirname(os.path.abspath(path))
    bak = os.path.join(idx_dir, "index.before_proof.json")
    if not os.path.isfile(bak):
        with open(bak, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)

    # 逐句校对独立保存，不能用几十秒的卡片摘要覆盖原 ASR 时间轴。
    old_texts = {cid: (by_id[cid].get("text") or "").strip() for cid in changes}
    if segment_mode:
        fixed_rows = [list(row[:3]) if isinstance(row, (list, tuple)) and len(row) >= 3
                      else row for row in segments]
        for cid, new in changes.items():
            fixed_rows[by_id[cid]["segment_index"]][2] = new
        data["segments_proofread"] = fixed_rows
        data["segments_proofread_suggestions"] = dict(lowconf)
    else:
        for cid, new in changes.items():
            c = by_id[cid]
            c["text_fixed"] = new
            old_title = (c.get("title") or "").strip()
            if old_title and len(old_title) <= 16 and old_title in new:
                c["title"] = new[:16]
        for cid, sug in lowconf.items():
            by_id[cid]["text_suggest"] = sug

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)

    report = os.path.join(idx_dir, "订正记录.md")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("# 字幕纠错记录\n\n")
        fh.write("共修正 %d 条，用时 %.1f 秒\n\n" % (len(changes), cost))
        fh.write("原索引备份：`index.before_proof.json`\n\n")
        target_field = "segments_proofread" if segment_mode else "text_fixed"
        fh.write("> 原始 ASR 与词级证据未被修改；校对稿保存于 `%s`。\n\n" % target_field)
        fh.write("| 时间点 | 原文本 | 参考文本 |\n|---|---|---|\n")
        for cid, new in sorted(changes.items(), key=lambda kv: by_id[kv[0]]["start"]):
            c = by_id[cid]
            fh.write("| %s | %s | %s |\n"
                     % (time.strftime("%H:%M:%S", time.gmtime(c["start"])),
                        old_texts[cid].replace("|", "／")[:80],
                        new.replace("|", "／")[:80]))
    log("\n已写入独立校对稿 %s（原始文字与时间戳保留）：%s"
        % ("segments_proofread" if segment_mode else "text_fixed", path))
    log("订正记录：%s" % report)
    log("想完全还原：把 index.before_proof.json 改名成 index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

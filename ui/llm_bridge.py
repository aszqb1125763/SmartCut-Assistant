# -*- coding: utf-8 -*-
"""本地模型桥：启动 llama-server、发对话请求。

该模块已并入主项目，专门负责本地对话模型进程管理。
为什么独立实现：原有 proofread.Server 绑定了较多 ASR 依赖（ctranslate2 等），
自主剪辑只需要"一个小模型能对话"，所以独立成这一个文件。

外部依赖只有标准库（urllib），不装 requests。
日志写到主项目的 `logs/llama-server.log`（和窗口守护日志同一处）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request


class LLM:
    """一个 llama-server 子进程 + 一个对话方法。"""

    def __init__(self, base_dir=None, model=None, port=None, ctx=None,
                 gpu_layers=None, log_path=None, task="editor"):
        from memory_config import app_home
        self.base = app_home(base_dir)
        from llm_gateway import OpenAICompatibleClient
        self.api = OpenAICompatibleClient.from_config(
            self.base, role=str(task or "editor"))
        self.exe = os.path.join(self.base, "bin", "llama", "llama-server.exe")
        # 模型可切换：环境变量 LLM_MODEL=14B / 8B，或直接传文件名
        name = os.environ.get("LLM_MODEL", "").strip()
        if name and not name.lower().endswith(".gguf"):
            preset = {"8b": "Qwen3-8B-Q4_K_M.gguf",
                      "14b": "Qwen3-14B-Q4_K_M.gguf"}
            name = preset.get(name.lower(), name + ".gguf")
        self.model = model or os.path.join(
            self.base, "models", "llm",
            name or "Qwen3-8B-Q4_K_M.gguf")
        self.port = int(port or (0 if self.api.enabled else self._free_port()))
        self.ctx = int(ctx or int(os.environ.get("LLM_CTX", "8192")))
        self.gpu_layers = 99 if gpu_layers is None else int(gpu_layers)
        self.log_path = log_path or os.path.join(self.base, "logs",
                                                 "llama-server.log")
        self.pid_path = os.path.join(os.path.dirname(self.log_path),
                                     "llama.pid")
        self.proc = None

    # -- 进程 ---------------------------------------------------------------
    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _cuda_ok():
        try:
            out = subprocess.run(["nvidia-smi"], capture_output=True,
                                 timeout=6)
            return out.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _kill_stale(self):
        """上一条命留下的 llama-server 会白占显存，启动前收掉。"""
        try:
            with open(self.pid_path, encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except Exception:  # noqa: BLE001
            return
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                                 capture_output=True, text=True)
            if "llama-server" in (out.stdout or ""):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True)
        except Exception:  # noqa: BLE001
            pass

    def start(self, timeout=90):
        # API 模式与本地模型严格互斥；远端失败也不得暗中启动 llama-server。
        if self.api.enabled:
            return self
        return self._start_local(timeout)

    def _start_local(self, timeout=90):
        if self.proc and self.proc.poll() is None:
            return self
        self._kill_stale()
        if not os.path.isfile(self.exe):
            raise RuntimeError("找不到 llama-server：%s" % self.exe)
        if not os.path.isfile(self.model):
            raise RuntimeError("找不到模型：%s" % self.model)
        if self.gpu_layers and not self._cuda_ok():
            self.gpu_layers = 0
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        log = open(self.log_path, "ab")
        cmd = [self.exe, "-m", self.model, "-c", str(self.ctx),
               "-ngl", str(self.gpu_layers), "--host", "127.0.0.1",
               "--port", str(self.port), "--jinja"]
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=log,
                                     creationflags=getattr(
                                         subprocess, "CREATE_NO_WINDOW", 0))
        try:
            os.makedirs(os.path.dirname(self.pid_path), exist_ok=True)
            with open(self.pid_path, "w", encoding="utf-8") as handle:
                handle.write(str(self.proc.pid))
        except Exception:  # noqa: BLE001
            pass
        started = time.time()
        while time.time() - started < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server 起不来，看日志：%s"
                                   % self.log_path)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port,
                        timeout=2) as answer:
                    if answer.status == 200:
                        return self
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        raise RuntimeError("llama-server 启动超时（%ds）" % timeout)

    def stop(self):
        if self.api.enabled:
            self.proc = None
            return
        if self.proc and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid),
                                "/T", "/F"], capture_output=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        try:
            os.remove(self.pid_path)
        except Exception:  # noqa: BLE001
            pass

    # -- 对话 ---------------------------------------------------------------
    def chat(self, system, user, max_tokens=400, temperature=0.3,
             timeout=180):
        if self.api.enabled:
            return self.api.chat(system, user, max_tokens=max_tokens,
                                 temperature=temperature)
        payload = {
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
            # Qwen3 默认会先"思考"，把 token 花在 reasoning_content 上、content 永远是空。
            # 这里关掉，让它直接回答。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self.port,
            data=data, headers={"Content-Type": "application/json"})
        body = None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as answer:
                    body = json.loads(answer.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 503) and attempt == 1:
                    time.sleep(0.8)      # 模型还在忙/刚起来，等一下再来
                    continue
                raise
            except urllib.error.URLError:
                if attempt == 1:
                    time.sleep(0.8)
                    continue
                raise
        if body is None:
            return ""
        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            return ""

    def alive(self):
        """进程活着 **且** HTTP 能答 200 才算可用（503 时进程也活着）。"""
        if self.api.enabled:
            return True
        if not (self.proc and self.proc.poll() is None):
            return False
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % self.port, timeout=1) as answer:
                return answer.status == 200
        except Exception:  # noqa: BLE001
            return False

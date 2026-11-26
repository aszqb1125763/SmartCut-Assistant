# -*- coding: utf-8 -*-
"""Remote AI gateway for OpenAI-compatible, Anthropic and Gemini APIs."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import ai_api_contracts as contracts


class APIRequestError(RuntimeError):
    """A user-safe remote model error that never contains credentials."""


ROLE_PROMPTS = {
    "analyst": ("你是视频素材分析员。只依据提供的台词、音频和画面证据做中立标注；"
                "不得编造未出现的内容，不负责决定最终删留。按用户消息指定的格式输出，"
                "不得添加解释性前后缀。"),
    "writer": ("你是视频编剧。把完整素材整理成目标、过程、结果和过渡清晰的故事结构；"
               "不得直接执行时间轴修改。章节只能引用输入里已有的事件或段落 ID。"
               "按用户消息指定的格式输出，不得添加解释性前后缀。"),
    "editor": ("你是执行剪辑师。只能基于给定事件和证据决定保留或删除，禁止虚构时间点，"
               "禁止切断词句。严格使用本次输出合同，不得使用Markdown。"),
    "director": ("你是总导演兼质量审片员。检查故事完整性、声音边界、画面连续性和误删风险；"
                 "只报告有证据的问题并给出可执行修复。按用户消息指定的结构输出，"
                 "不得添加解释性前后缀。"),
    "generic": "你是结构化视频处理助手。只使用提供的证据，按用户消息指定格式输出。",
}


def _role_system(role, contract=""):
    # API roles/contracts are independent. The local prompt is only inspected
    # to identify its caller; none of its instruction text is sent remotely.
    role = contracts.CONTRACT_ROLES.get(contract, str(role or "").lower())
    prompt = ROLE_PROMPTS.get(role, ROLE_PROMPTS["generic"])
    if role in ("writer", "editor", "director"):
        prompt += "\n" + contracts.POLICY
    if contract in contracts.PROMPTS:
        prompt += "\n本次API输出合同：\n" + contracts.PROMPTS[contract]
    elif contract == "json":
        prompt += "\n仅输出满足用户消息字段要求的单个合法JSON对象，不加Markdown或说明。"
    return prompt


def propagate_api_error(exc):
    """Keep legacy local fallbacks from swallowing a remote API failure."""
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        if isinstance(current, APIRequestError):
            raise current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


def api_enabled():
    """Use the current backend policy without loading a local content model."""
    import llm_config
    return bool(llm_config.api_settings().get("enabled"))


def backend_signature(settings=None):
    """A cache identity; credentials and routing headers never appear in it."""
    if settings is None:
        import llm_config
        settings = llm_config.api_settings()
    if not settings.get("enabled"):
        return "local"
    identity = {key: settings.get(key) for key in (
        "provider", "protocol", "base_url", "model", "api_key", "headers")}
    identity["contract_schema"] = contracts.SCHEMA
    body = json.dumps(identity, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return "api:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _expects_json(system, user):
    text = (str(system or "") + "\n" + str(user or "")).lower()
    return ("json" in text and any(token in text for token in
            ("只输出", "输出格式", "delete", "keep", "ranges", "剪辑单")))


def _normalized_json_text(text):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(),
                 flags=re.I | re.S).strip()
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _parse_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        # Accessing port also validates a malformed configured port number.
        valid = bool(parsed.hostname) and (parsed.port is None or 0 < parsed.port <= 65535)
    except ValueError:
        raise APIRequestError("AI API 地址格式无效") from None
    if parsed.scheme not in ("http", "https") or not valid:
        raise APIRequestError("AI API 地址必须是 http:// 或 https://")
    return parsed


def _openai_endpoint(base_url):
    parsed = _parse_url(base_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions" if path else "/v1/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path,
                                    parsed.query, ""))


def _anthropic_endpoint(base_url):
    parsed = _parse_url(base_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/messages"):
        path += "/messages" if path.endswith("/v1") else "/v1/messages"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path,
                                    parsed.query, ""))


def _gemini_endpoint(base_url, model, api_key):
    parsed = _parse_url(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/openai"):
        path = path[:-len("/openai")]
    suffix = "/models/%s:generateContent" % urllib.parse.quote(
        str(model), safe="-._/")
    if not path.endswith(suffix):
        path += suffix
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if api_key and not any(key == "key" for key, _value in query):
        query.append(("key", api_key))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path,
                                    urllib.parse.urlencode(query), ""))


def _public_endpoint(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return "(invalid-url)"
    # Userinfo and query strings may both contain credentials.
    authority = parsed.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path,
                                    "", ""))


class OpenAICompatibleClient:
    """Compatibility name retained; this client supports three protocols."""

    def __init__(self, settings, log_path=None, role="generic"):
        settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(settings.get("enabled"))
        self.provider = str(settings.get("provider") or "openai_compatible")
        self.protocol = str(settings.get("protocol") or "openai").lower()
        self.base_url = str(settings.get("base_url") or "").strip()
        self.api_key = str(settings.get("api_key") or "").strip()
        self.model = str(settings.get("model") or "").strip()
        try:
            self.timeout = max(5.0, min(600.0,
                                       float(settings.get("timeout") or 120)))
        except (TypeError, ValueError):
            self.timeout = 120.0
        raw_headers = settings.get("headers") if isinstance(
            settings.get("headers"), dict) else {}
        self.headers = {str(key).strip(): str(value)
                        for key, value in raw_headers.items()
                        if str(key).strip() and value is not None}
        self.fallback_local = False
        self.log_path = log_path
        self.role = str(role or "generic").lower()

    @classmethod
    def from_config(cls, base_dir=None, role="generic"):
        import llm_config
        from memory_config import app_home
        root = app_home(base_dir)
        return cls(llm_config.api_settings(base_dir=base_dir),
                   log_path=os.path.join(root, "logs", "ai-api.log"), role=role)

    def _redact(self, value):
        text = str(value or "")
        secrets = [self.api_key] + [value for value in self.headers.values()
                                    if str(value)]
        try:
            parsed = urllib.parse.urlsplit(self.base_url)
            secrets += [value for _key, value in urllib.parse.parse_qsl(parsed.query)
                        if value]
            secrets += [value for value in (parsed.username, parsed.password) if value]
        except ValueError:
            pass
        for secret in secrets:
            if secret:
                for encoded in (str(secret), urllib.parse.quote(str(secret), safe=""),
                                urllib.parse.quote_plus(str(secret))):
                    text = text.replace(encoded, "***")
        text = re.sub(r"https?://[^\s<>\"']+", lambda match: _public_endpoint(
            match.group(0)), text)
        text = re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1***", text)
        text = re.sub(r"(?i)((?:api[_-]?key|authorization|x-api-key|access[_-]?token|\bkey)"
                      r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+", r"\1***", text)
        return text

    def _log_failure(self, endpoint, message):
        if not self.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = ("[%s] provider=%s role=%s model=%s endpoint=%s error=%s\n" %
                    (stamp, self.provider, self.role, self.model or "(empty)",
                     _public_endpoint(endpoint),
                     self._redact(message).replace("\r", " ").replace("\n", " ")[:1200]))
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(self._redact(line))
        except OSError:
            pass

    def _endpoint(self):
        if self.protocol == "anthropic":
            return _anthropic_endpoint(self.base_url)
        if self.protocol == "gemini":
            return _gemini_endpoint(self.base_url, self.model, self.api_key)
        return _openai_endpoint(self.base_url)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)
        names = {name.lower() for name in headers}
        if self.protocol == "anthropic":
            if self.api_key and "x-api-key" not in names:
                headers["x-api-key"] = self.api_key
            if "anthropic-version" not in names:
                headers["anthropic-version"] = "2023-06-01"
        elif self.protocol != "gemini" and self.api_key and "authorization" not in names:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _payload(self, system, messages, max_tokens, temperature):
        if self.protocol == "anthropic":
            return {"model": self.model, "system": system,
                    "messages": messages, "max_tokens": int(max_tokens),
                    "temperature": float(temperature)}
        if self.protocol == "gemini":
            payload = {
                "contents": [{"role": "user", "parts": [{"text": "\n\n".join(
                    str(item.get("content") or "") for item in messages)}]}],
                "generationConfig": {"maxOutputTokens": int(max_tokens),
                                     "temperature": float(temperature)},
            }
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            return payload
        return {"model": self.model,
                "messages": ([{"role": "system", "content": system}]
                             if system else []) + messages,
                "max_tokens": int(max_tokens),
                "temperature": float(temperature), "stream": False}

    def _content(self, payload):
        try:
            if self.protocol == "anthropic":
                text = "".join(str(item.get("text") or "") for item in
                               payload["content"] if isinstance(item, dict))
            elif self.protocol == "gemini":
                text = "".join(str(item.get("text") or "") for item in
                               payload["candidates"][0]["content"]["parts"]
                               if isinstance(item, dict))
            else:
                text = payload["choices"][0]["message"]["content"]
                if isinstance(text, list):
                    text = "".join(str(item.get("text") or "") for item in text
                                   if isinstance(item, dict))
        except (KeyError, IndexError, TypeError):
            raise APIRequestError("AI API 返回格式无效：缺少文本内容")
        if not isinstance(text, str) or not text.strip():
            raise APIRequestError("AI API 返回了空内容")
        return text.strip()

    def _send(self, endpoint, system, messages, max_tokens, temperature, timeout):
        body = json.dumps(self._payload(system, messages, max_tokens, temperature),
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body,
                                         headers=self._headers(), method="POST")
        last_error = None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as answer:
                    raw = answer.read().decode("utf-8", "replace")
                return self._content(json.loads(raw))
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:2000]
                except Exception:  # noqa: BLE001
                    detail = ""
                finally:
                    exc.close()
                last_error = "HTTP %d%s" % (exc.code,
                                             ("：" + detail) if detail else "")
                if exc.code in (429, 500, 502, 503, 504) and attempt == 1:
                    time.sleep(0.8)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                    json.JSONDecodeError, APIRequestError) as exc:
                last_error = str(exc) or type(exc).__name__
                if attempt == 1 and not isinstance(exc, APIRequestError):
                    time.sleep(0.8)
                    continue
                break
        safe = self._redact(last_error or "未知错误")
        self._log_failure(endpoint, safe)
        raise APIRequestError("AI API 请求失败：%s" % safe[:500])

    def chat(self, system, user, max_tokens=400, temperature=0.3, timeout=None):
        if not self.enabled:
            raise APIRequestError("AI API 未启用")
        if not self.model:
            raise APIRequestError("AI API 模型名称为空")
        endpoint = self._endpoint()
        contract = contracts.detect(system, user)
        if not contract and _expects_json(system, user):
            contract = "json"
        role_system = _role_system(self.role, contract)
        messages = [{"role": "user", "content": str(user or "")}]
        request_timeout = self.timeout if timeout is None else max(
            5.0, min(600.0, float(timeout)))
        for attempt in (1, 2):
            text = self._send(endpoint, role_system, messages, max_tokens,
                              temperature if attempt == 1 else 0.0, request_timeout)
            try:
                return contracts.validate(contract, text, str(user or "")) if contract else text
            except ValueError as exc:
                detail = str(exc)
                if attempt == 2:
                    self._log_failure(endpoint, "输出合同重试失败：" + detail)
                    raise APIRequestError("AI API 两次未满足输出合同：" + detail) from None
                repair = ("上一次输出不满足本次合同：%s。请重新输出完整结果，"
                          "严格保持原定字段、格式和输入编号，不要解释。" % detail)
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": repair}]

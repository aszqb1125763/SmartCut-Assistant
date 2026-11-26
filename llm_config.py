# -*- coding: utf-8 -*-
"""本地模型配置：用户选择一个模型，所有本地 AI 任务统一跟随。

旧版会把字幕校对和逐句分段固定到 4B、把段落判断交给 8B，导致
用户明明选择了 8B，运行时仍会暗中寻找并启动 4B。现在配置只有一个
``active_tier`` 真源；任务映射字段仅为兼容旧配置和便于诊断。
"""
import json
import os
from memory_config import app_home

BASE = app_home()
LLM_DIR = os.path.join(BASE, "models", "llm")
CONF = os.path.join(BASE, "conf", "llm.json")
APP_SETTINGS = os.path.join(BASE, "conf", "settings.json")
MODEL_TIERS = ("fast", "smart", "large")
LOCAL_TASKS = (
    "proofread", "subtitle", "segment", "paragraph", "complete",
    "analyst", "writer", "editor", "director",
)

DEFAULTS = {
    # API 的用户可见字段统一保存在 conf/settings.json；这里仅保留高级
    # 自定义请求头，供手工配置不常见的兼容服务。
    "api": {
        "headers": {},
    },
    # 档位：文件名（放在 models/llm 下）
    "models": {
        "fast": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "smart": "Qwen3-8B-Q4_K_M.gguf",
        "large": "Qwen3-14B-Q4_K_M.gguf",
    },
    "active_tier": "smart",
    # 兼容旧版配置与诊断输出；保存时全部同步为 active_tier。
    "tasks": {task: "smart" for task in LOCAL_TASKS},
    # 单模型模式不跨档位升级；解析失败应保留原结果或明确报错。
    "escalate": False,
    # Qwen3 是思考型模型，跑结构化标注要关掉思考，否则会输出推理过程
    "disable_thinking": True,
}


def _valid_tier(value, fallback="fast"):
    tier = str(value or "").strip().lower()
    return tier if tier in MODEL_TIERS else fallback


def _normalize(cfg, explicit_active=None):
    """Return one-model configuration while preserving future unknown keys."""
    cfg = json.loads(json.dumps(cfg if isinstance(cfg, dict) else DEFAULTS))
    tasks = cfg.setdefault("tasks", {})
    # 旧版没有 active_tier；paragraph 是旧界面真正记录用户选择的位置。
    active = explicit_active
    if active is None:
        active = cfg.get("active_tier")
    if active is None:
        active = tasks.get("paragraph") or tasks.get("complete") or "fast"
    active = _valid_tier(active)
    cfg["active_tier"] = active
    for task in LOCAL_TASKS:
        tasks[task] = active
    cfg["escalate"] = False
    return cfg


def load(base_dir=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    conf_path = (CONF if base_dir is None else
                 os.path.join(app_home(base_dir), "conf", "llm.json"))
    explicit_active = None
    try:
        if os.path.isfile(conf_path):
            with open(conf_path, encoding="utf-8") as handle:
                user = json.load(handle)
            if isinstance(user, dict):
                explicit_active = user.get("active_tier")
                if explicit_active is None:
                    old_tasks = user.get("tasks") or {}
                    explicit_active = (old_tasks.get("paragraph") or
                                       old_tasks.get("complete"))
            for k, v in user.items():
                if isinstance(v, dict):
                    cfg.setdefault(k, {}).update(v)
                else:
                    cfg[k] = v
    except Exception:  # noqa: BLE001
        pass
    return _normalize(cfg, explicit_active=explicit_active)


def save(cfg):
    cfg = _normalize(cfg, explicit_active=(cfg or {}).get("active_tier"))
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    return True


def active_tier(cfg=None):
    """Return the only model tier used by every local AI task."""
    data = _normalize(cfg) if isinstance(cfg, dict) else load()
    return _valid_tier(data.get("active_tier"))


def _configured_model_path(cfg, tier):
    name = str((cfg.get("models") or {}).get(tier) or "").strip()
    if not name:
        try:
            import llm_models as _m
            item = _m.info(tier)
            name = str((item or {}).get("file") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
    return os.path.join(LLM_DIR, name) if name else ""


def model_path(_tier=None):
    """Return the selected model path; the legacy tier argument is ignored.

    Returning the expected path even when the file is absent is deliberate:
    callers then report that the selected model is missing instead of silently
    falling back to a different model that the user did not choose.
    """
    cfg = load()
    return _configured_model_path(cfg, active_tier(cfg))


def pick(task):
    """Return the single user-selected model for any local task."""
    return model_path()


def select_model(tier, model_file=None):
    """Persist one model selection for the complete local workflow."""
    tier = _valid_tier(tier, fallback="")
    if not tier:
        raise ValueError("未知本地模型档位")
    cfg = load()
    cfg["active_tier"] = tier
    if model_file:
        cfg.setdefault("models", {})[tier] = os.path.basename(str(model_file))
    for task in LOCAL_TASKS:
        cfg.setdefault("tasks", {})[task] = tier
    save(cfg)
    return load()


def selected_model_name(cfg=None):
    """Short user-facing name for the active local model."""
    tier = active_tier(cfg)
    try:
        import llm_models as _m
        item = _m.info(tier) or {}
        return str(item.get("name") or tier)
    except Exception:  # noqa: BLE001
        return tier


def list_models():
    try:
        return sorted(f for f in os.listdir(LLM_DIR) if f.lower().endswith(".gguf"))
    except OSError:
        return []


def api_settings(cfg=None, base_dir=None):
    """Return normalized user API settings shared with the settings UI.

    Environment variables intentionally override the JSON file so a key does
    not have to be persisted on disk.  This function never logs or formats the
    key; callers must treat the returned dictionary as secret-bearing data.
    """
    cfg = cfg if isinstance(cfg, dict) else load(base_dir=base_dir)
    raw = cfg.get("api") if isinstance(cfg.get("api"), dict) else {}
    settings_path = (APP_SETTINGS if base_dir is None else
                     os.path.join(app_home(base_dir), "conf", "settings.json"))
    try:
        with open(settings_path, encoding="utf-8") as handle:
            app = json.load(handle) or {}
    except (OSError, ValueError, TypeError):
        app = {}
    import ai_api_settings
    provider = ai_api_settings.normalize_provider(
        os.environ.get("SMARTCUT_AI_PROVIDER") or app.get("llm_provider"))
    base_url = str(os.environ.get("SMARTCUT_AI_BASE_URL") or
                   app.get("llm_api_base_url") or
                   ai_api_settings.default_base_url(provider) or "").strip()
    api_key = str(os.environ.get("SMARTCUT_AI_API_KEY") or
                  app.get("llm_api_key") or "").strip()
    model = str(os.environ.get("SMARTCUT_AI_MODEL") or
                app.get("llm_api_model") or "").strip()
    timeout_raw = (os.environ.get("SMARTCUT_AI_TIMEOUT") or
                   app.get("llm_api_timeout"))
    timeout = ai_api_settings.normalize_timeout(timeout_raw)
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    app_headers = app.get("llm_api_headers")
    if isinstance(app_headers, str) and app_headers.strip():
        try:
            app_headers = json.loads(app_headers)
        except ValueError:
            app_headers = {}
    if isinstance(app_headers, dict):
        headers = dict(headers)
        headers.update(app_headers)
    env_headers = os.environ.get("SMARTCUT_AI_HEADERS")
    if env_headers:
        try:
            parsed_headers = json.loads(env_headers)
            if isinstance(parsed_headers, dict):
                headers = dict(headers)
                headers.update(parsed_headers)
        except ValueError:
            pass
    headers = {str(key).strip(): str(value)
               for key, value in headers.items()
               if str(key).strip() and value is not None}
    protocol = ("anthropic" if provider == "anthropic" else
                "gemini" if provider == "gemini" else "openai")
    return {
        "enabled": ai_api_settings.is_remote(provider),
        "provider": provider,
        "protocol": protocol,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout": timeout,
        "headers": headers,
        # API 模式和本地模式严格互斥；远端失败不得暗中启动 llama-server。
        "fallback_local": False,
    }


def provider_presets():
    """Public, secret-free provider metadata from the UI source of truth."""
    import ai_api_settings
    return [{"id": item[0], "label": item[1], "base_url": item[2]}
            for item in ai_api_settings.PROVIDERS]

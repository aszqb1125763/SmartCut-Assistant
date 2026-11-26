# -*- coding: utf-8 -*-
"""启动自检：程序自己先检查关键依赖，发现问题直接弹通知条，而不是等用户点到卡住。

检查项：
  · ffmpeg 在不在、能不能跑
  · 语音模型目录（models/large-v3-full）在不在
  · llama 服务与模型（可选，缺了只是不能用本地大模型）
  · conf/*.json 能不能读
  · 工作目录能不能写
  · 显卡（没 N 卡也能跑，只是慢）

返回 (errors, warnings) 两个字符串列表，都已经是人话。
"""
from __future__ import annotations

import json
import os


def _home() -> str:
    from memory_config import app_home
    return app_home()


def run() -> tuple[list[str], list[str]]:
    base = _home()
    errors: list[str] = []
    warns: list[str] = []

    # ffmpeg
    try:
        import export as ex
        ff = ex.find_ffmpeg()
        if not ff or not os.path.isfile(ff):
            errors.append("找不到 ffmpeg（bin\\ffmpeg.exe），无法分析/出片")
    except Exception as exc:  # noqa: BLE001
        errors.append("ffmpeg 检查失败：%s" % exc)

    # 语音模型
    mdir = os.path.join(base, "models", "large-v3-full")
    if not os.path.isdir(mdir):
        other = [d for d in (os.listdir(os.path.join(base, "models"))
                             if os.path.isdir(os.path.join(base, "models")) else [])
                 if d.startswith("large-v3")]
        if other:
            warns.append("没找到 large-v3-full，但有 %s，会用现有模型" % "、".join(other))
        else:
            errors.append("没有语音识别模型（models\\large-v3-full），分析会卡在下载")
    else:
        # 目录在，但里面要有文件（有人只建了空目录）
        try:
            n = sum(len(f) for _, _, f in os.walk(mdir))
            if n == 0:
                errors.append("语音模型目录是空的（models\\large-v3-full）")
        except Exception:
            pass

    # 配置
    for name in ("settings.json", "autocut.json", "bgm.json"):
        p = os.path.join(base, "conf", name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    json.load(f)
            except Exception:
                warns.append("conf\\%s 读不了，会用默认值" % name)

    # API 模式仍需要本地转写/媒体工具，但不需要本地语言模型文件。
    # 这里只验证必要配置，不发网络请求，也不在提示中包含密钥。
    try:
        import llm_config
        api = llm_config.api_settings()
        if api.get("enabled"):
            if not api.get("model"):
                errors.append("AI API 尚未填写模型名称，请到设置 → AI 模型中补全")
            from urllib.parse import urlsplit
            parsed = urlsplit(api.get("base_url") or "")
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append("AI API 地址无效，请到设置 → AI 模型中填写完整接口地址")
    except (ValueError, TypeError):
        errors.append("AI API 配置无效，请在设置 → AI 模型中重新保存")

    # 工作目录可写
    try:
        import index as ix
        wd = ix.work_root()
        os.makedirs(wd, exist_ok=True)
        probe = os.path.join(wd, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as exc:  # noqa: BLE001
        errors.append("工作目录不可写：%s" % exc)

    # 显卡编码（只是提示）——必须拿真实 ffmpeg 路径去试，
    # 传裸名字 "ffmpeg" 在没配 PATH 的机器上一定失败，会误报"显卡编码不可用"。
    try:
        import autocut as ac
        import index as ix
        ff = ix.find_ffmpeg()
        if not ac._nvenc_ok(ff):
            why = ac.nvenc_reason(ff)
            warns.append("显卡编码不可用，会用 CPU 编码（更慢，但能出片）"
                         + ("：%s" % why if why else ""))
    except FileNotFoundError:
        pass  # ffmpeg 都没有，上面已经报过错了，别重复刷
    except Exception:  # noqa: BLE001
        pass

    return errors, warns


if __name__ == "__main__":
    e, w = run()
    print("错误：", e or "无")
    print("警告：", w or "无")

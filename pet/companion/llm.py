# -*- coding: utf-8 -*-
"""复用宿主「AI 设置」里配好的模型，做一次性问答（不流式、不走联网工具）。

为什么复用而不是另配一套：宿主已经解决了 Base URL / 模型名 / Key（keyring）/
SSL 校验 / 代理这些事，陪伴扩展只需要「问一句、拿一段文本」。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


class CompanionLLMError(RuntimeError):
    """模型调用失败（配置缺失、网络、HTTP 错误、返回格式不对）。"""


def _provider(cfg):
    try:
        settings = cfg.chat_settings()
        conf = settings.active_config
    except Exception as exc:  # noqa: BLE001
        raise CompanionLLMError(f"读不到 AI 设置：{exc}") from exc
    if not getattr(conf, "base_url", "") or not getattr(conf, "model", ""):
        raise CompanionLLMError("还没配置模型：右键 → 桌宠设置 → AI 对话，先填一家服务和 Key")
    return conf


def is_ready(cfg) -> bool:
    try:
        conf = _provider(cfg)
    except CompanionLLMError:
        return False
    return bool(conf.base_url and conf.model)


def ask(
    cfg,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.8,
    timeout: float | None = None,
    json_mode: bool = False,
) -> str:
    """问一次模型，返回纯文本。失败抛 :class:`CompanionLLMError`。"""
    from ..chat.providers import (
        ProviderError,
        _make_ssl_context,
        build_browser_headers,
        normalize_chat_endpoint,
    )

    conf = _provider(cfg)
    key = ""
    try:
        key = cfg.resolve_api_key(conf) or getattr(conf, "api_key", "") or ""
    except Exception:
        key = getattr(conf, "api_key", "") or ""

    endpoint = normalize_chat_endpoint(conf.base_url, getattr(conf, "chat_path", "/v1/chat/completions"))
    payload: dict[str, Any] = {
        "model": conf.model,
        "messages": messages,
        "stream": False,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = build_browser_headers({"Content-Type": "application/json", "Accept": "application/json"})
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req,
            timeout=float(timeout or getattr(conf, "timeout", 60.0) or 60.0),
            context=_make_ssl_context(getattr(conf, "verify_ssl", True)),
        ) as resp:
            raw = resp.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            pass
        friendly = {
            401: "API Key 无效或已过期",
            402: "余额不足",
            403: "没有权限（模型或分组不对）",
            404: "接口不存在（检查 Base URL）",
            429: "请求太频繁，被限流了",
        }.get(exc.code, "")
        raise CompanionLLMError(f"{friendly or '模型返回错误'}（HTTP {exc.code}）：{detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise CompanionLLMError(f"连不上模型接口：{exc.reason}") from exc
    except ProviderError as exc:
        raise CompanionLLMError(str(exc)) from exc

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise CompanionLLMError("模型返回的不是 JSON") from exc
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise CompanionLLMError(str(err.get("message") if isinstance(err, dict) else err))
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CompanionLLMError("模型返回格式不符合 Chat Completions") from exc
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def extract_json(text: str) -> Any:
    """从回复里抠出第一个 JSON 对象/数组。"""
    s = str(text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise CompanionLLMError(f"模型没有返回 JSON：{s[:120]}")
    end = max(s.rfind("}"), s.rfind("]"))
    if end <= start:
        raise CompanionLLMError(f"模型没有返回 JSON：{s[:120]}")
    try:
        return json.loads(s[start:end + 1])
    except ValueError as exc:
        raise CompanionLLMError(f"JSON 解析失败：{exc}") from exc

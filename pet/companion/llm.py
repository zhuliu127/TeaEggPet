# -*- coding: utf-8 -*-
"""复用宿主「AI 设置」里配好的模型，做一次性问答（不流式、不走联网工具）。

为什么复用而不是另配一套：宿主已经解决了 Base URL / 模型名 / Key（keyring）/
SSL 校验 / 代理这些事，陪伴扩展只需要「问一句、拿一段文本」。
"""

from __future__ import annotations

import json
import re
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
    return ask_with_info(
        cfg, messages, max_tokens=max_tokens, temperature=temperature,
        timeout=timeout, json_mode=json_mode,
    )[0]


def ask_with_info(
    cfg,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.8,
    timeout: float | None = None,
    json_mode: bool = False,
) -> tuple[str, dict[str, Any]]:
    """问一次模型，返回 ``(文本, 元信息)``。

    元信息里带 ``finish_reason`` / ``truncated`` / ``usage``：
    推理模型（deepseek-reasoner 一类）会把预算烧在思维链上，正文可能只有半句话
    甚至空串——调用方据此加预算重试，比直接报「模型没有返回 JSON」有用得多。
    """
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
    choice = (data.get("choices") or [{}])[0] or {}
    info = {
        "finish_reason": str(choice.get("finish_reason") or ""),
        "usage": data.get("usage") or {},
        "reasoning_only": not str(content or "").strip()
        and bool(str(message.get("reasoning_content") or "").strip()),
    }
    info["truncated"] = info["finish_reason"] == "length" or info["reasoning_only"]
    return str(content or ""), info


def _strip_fence(text: str) -> str:
    s = str(text or "").strip()
    if "```" not in s:
        return s
    parts = s.split("```")
    # ```json\n{...}\n``` → 取代码块里最长的一段
    chunks = [c for c in parts if c.strip()]
    if not chunks:
        return s
    best = max(chunks, key=len)
    return best.split("\n", 1)[-1].strip() if best.lstrip().lower().startswith(("json", "javascript")) else best


def _drop_trailing_commas(s: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", s)


def _repair_truncated(s: str) -> str | None:
    """把被 max_tokens 截断的 JSON 尽量补齐：只保留到最后一个完整的对象/数组元素。"""
    closers = []
    for cut in range(len(s) - 1, -1, -1):
        if s[cut] not in "}]":
            continue
        body = s[: cut + 1].rstrip().rstrip(",")
        # 统计未闭合的括号，按相反顺序补
        stack: list[str] = []
        in_str = False
        esc = False
        for ch in body:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        if in_str:
            continue
        closers = [("}" if c == "{" else "]") for c in reversed(stack)]
        if not closers:
            continue
        candidate = body + "".join(closers)
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def extract_json(text: str) -> Any:
    """从回复里抠出第一个 JSON 对象/数组（尽量容错）。

    真实模型常见的三种坏输出：包了 ```json 代码块、多了尾逗号、被 max_tokens
    截断成半截。以前一律报「模型没有返回 JSON / JSON 解析失败」，用户只能重试；
    现在能救就救，救不了才抛。
    """
    s = _strip_fence(text)
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise CompanionLLMError(f"模型没有返回 JSON：{s[:120]}")
    end = max(s.rfind("}"), s.rfind("]"))
    if end <= start:
        raise CompanionLLMError(f"模型没有返回 JSON：{s[:120]}")
    chunk = s[start:end + 1]
    for candidate in (chunk, _drop_trailing_commas(chunk)):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    repaired = _repair_truncated(s[start:])
    if repaired is not None:
        log.warning("模型输出被截断，已从完整段落里救回（%s 字）", len(s))
        return repaired
    try:
        return json.loads(_drop_trailing_commas(chunk))
    except ValueError as exc:
        raise CompanionLLMError(f"JSON 解析失败：{exc}") from exc

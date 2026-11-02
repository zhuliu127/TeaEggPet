# -*- coding: utf-8 -*-
"""生图接口适配（只用标准库）：

- ``65535``：``POST /v1/tasks`` 提交，轮询 ``GET /v1/tasks/{id}``；参考图以 data URI 放进
  ``input.image_urls``，只有成功才扣费。
- ``openai``：无参考图走 ``/v1/images/generations``；有参考图走 ``/v1/images/edits``（multipart）。

两个通道都遵守调用方给的超时；失败一律抛 :class:`ImageGenError`（消息可直接给用户看）。
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from ..chat.providers import _make_ssl_context, build_browser_headers

log = logging.getLogger(__name__)

FRIENDLY = {
    400: "参数有误",
    401: "API Key 无效或已过期",
    402: "余额不足或 Key 配额用尽",
    403: "没有权限（模型 / 分组不对，或被内容审核拦下）",
    404: "接口或任务不存在（检查 Base URL）",
    409: "同一请求还在处理中",
    413: "请求太大（参考图太多或太大）",
    429: "请求太频繁，被限流了",
    502: "上游暂时不可用",
    503: "队列繁忙或暂无可用渠道",
    524: "网关超时",
}


class ImageGenError(RuntimeError):
    """生图失败（消息可直接给用户看）。"""

    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class _Cfg:
    def __init__(self, store):
        self.provider = str(store.get("companion_image_provider", "65535") or "65535")
        self.base_url = str(store.get("companion_image_base_url", "") or "").strip()
        self.api_key = store.image_api_key()
        self.model = str(store.get("companion_image_model", "") or "").strip()
        self.aspect = str(store.get("companion_image_aspect", "3:4") or "3:4")
        try:
            self.timeout = max(30, int(store.get("companion_image_timeout", 240) or 240))
        except (TypeError, ValueError):
            self.timeout = 240
        self.ssl_ctx = _make_ssl_context(True)

    def v1(self) -> str:
        base = self.base_url.rstrip("/")
        if not base:
            raise ImageGenError("还没填生图接口地址（右键 → 陪伴扩展设置 → 生图）")
        return base if base.endswith("/v1") else f"{base}/v1"

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if not self.api_key:
            raise ImageGenError("还没填生图 API Key（右键 → 陪伴扩展设置 → 生图）")
        headers = build_browser_headers({"Authorization": f"Bearer {self.api_key}"})
        if extra:
            headers.update(extra)
        return headers


def _read_error(exc: urllib.error.HTTPError) -> ImageGenError:
    detail = ""
    try:
        detail = exc.read(2048).decode("utf-8", "replace")
    except Exception:
        pass
    try:
        parsed = json.loads(detail)
        message = parsed.get("error", {}).get("message") if isinstance(parsed.get("error"), dict) else parsed.get("error")
        message = message or parsed.get("message") or ""
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
    except Exception:
        message = " ".join(detail.split())[:300]
    head = FRIENDLY.get(exc.code, f"HTTP {exc.code}")
    if exc.code == 403 and "not served by this gateway" in detail:
        head = "这个 Key 的分组不在这台网关上"
        message = "把「接口类型」换成 OpenAI 兼容，或换成 Key 所属分组的 Base URL"
    err = ImageGenError(f"{head}（HTTP {exc.code}）" + (f"：{message}" if message else ""))
    err.status = exc.code
    err.detail = detail[:600]
    return err


def _data_uri(path: Path) -> str:
    raw = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _download(url: str, ctx, timeout: float = 120.0) -> bytes:
    if url.startswith("data:"):
        head, _, payload = url.partition(",")
        if "base64" in head:
            return base64.b64decode(payload)
        raise ImageGenError("结果是无法解析的 data URI")
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=build_browser_headers())
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise ImageGenError(f"图片已生成，但下载失败：{last}")


def pixel_size(aspect: str) -> str | None:
    if "x" in aspect.lower():
        return aspect.lower()
    if ":" not in aspect:
        return None
    try:
        left, right = (float(x) for x in aspect.split(":", 1))
    except ValueError:
        return None
    ratio = left / right if right else 1.0
    if ratio > 1.15:
        return "1536x1024"
    if ratio < 0.87:
        return "1024x1536"
    return "1024x1024"


def _multipart(fields: dict[str, str], files: list[Path]) -> tuple[bytes, str]:
    boundary = f"----petdesk{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    field_name = "image[]" if len(files) > 1 else "image"
    for path in files:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _post_json(url: str, payload: dict[str, Any], cfg: _Cfg, timeout: float, extra: dict[str, str] | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = cfg.headers({"Content-Type": "application/json", "Accept": "application/json", **(extra or {})})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=cfg.ssl_ctx) as resp:
            return json.loads(resp.read(8 * 1024 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise _read_error(exc) from exc
    except urllib.error.URLError as exc:
        raise ImageGenError(f"连不上生图接口：{exc.reason}") from exc
    except OSError as exc:
        raise ImageGenError(f"网络请求失败：{exc}") from exc


def _get_json(url: str, cfg: _Cfg, timeout: float):
    req = urllib.request.Request(url, headers=cfg.headers({"Accept": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=cfg.ssl_ctx) as resp:
            return json.loads(resp.read(8 * 1024 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise _read_error(exc) from exc
    except urllib.error.URLError as exc:
        raise ImageGenError(f"连不上生图接口：{exc.reason}") from exc


# ---------------------------------------------------------------- 65535 异步任务
def _run_tasks(cfg: _Cfg, prompt: str, refs: list[Path], aspect: str,
               on_status: Callable[[str], None] | None) -> tuple[bytes, dict[str, Any]]:
    base = cfg.v1()
    payload_input: dict[str, Any] = {"prompt": prompt, "n": 1}
    if aspect:
        payload_input["size"] = aspect
    if refs:
        payload_input["image_urls"] = [_data_uri(p) for p in refs]
    payload = {"kind": "image", "model": cfg.model, "input": payload_input}

    created = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            created = _post_json(f"{base}/tasks", payload, cfg, min(120, cfg.timeout),
                                 {"Idempotency-Key": f"petdesk-{uuid.uuid4()}"})
            break
        except ImageGenError as exc:
            last_error = exc
            message = str(exc)
            if "429" in message or "503" in message or "409" in message:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    if created is None:
        raise last_error or ImageGenError("提交生图任务失败")
    task_id = str(created.get("id") or "")
    if not task_id:
        raise ImageGenError("接口没有返回任务 ID")
    if on_status:
        on_status(f"已提交（{task_id[:8]}）")

    deadline = time.time() + cfg.timeout
    interval = 2.0
    last_state = ""
    while time.time() < deadline:
        time.sleep(interval)
        try:
            task = _get_json(f"{base}/tasks/{task_id}", cfg, 30)
        except ImageGenError:
            interval = min(interval * 1.5, 8)
            continue
        state = str(task.get("status") or "")
        if state != last_state:
            last_state = state
            label = {"queued": "排队中", "running": "生成中", "pending": "等待中"}.get(state, state)
            if on_status:
                on_status(label or "生成中")
        if state == "done":
            urls = [u for u in (task.get("result_urls") or []) if isinstance(u, str) and u]
            if urls:
                return _download(urls[0], cfg.ssl_ctx), {"task_id": task_id, "model": task.get("model") or cfg.model}
            data = ((task.get("result") or {}).get("data") or [])
            for item in data:
                if isinstance(item, dict) and item.get("b64_json"):
                    return base64.b64decode(item["b64_json"]), {"task_id": task_id, "model": cfg.model}
            raise ImageGenError("任务完成了，但没有返回图片")
        if state == "failed":
            code = f"{task.get('error_code')}：" if task.get("error_code") else ""
            raise ImageGenError(f"生成失败（不扣费）：{code}{task.get('error_message') or '未知原因'}")
        interval = min(interval * 1.25, 5)
    raise ImageGenError(f"等了 {cfg.timeout} 秒还没出图（任务 {task_id} 可能还在排队）")


# ---------------------------------------------------------------- OpenAI 兼容
def _run_openai(cfg: _Cfg, prompt: str, refs: list[Path], aspect: str,
                on_status: Callable[[str], None] | None) -> tuple[bytes, dict[str, Any]]:
    base = cfg.v1()
    size = pixel_size(aspect)
    if on_status:
        on_status("生成中")
    if not refs:
        payload: dict[str, Any] = {"model": cfg.model, "prompt": prompt, "n": 1}
        if size:
            payload["size"] = size
        try:
            data = _post_json(f"{base}/images/generations", payload, cfg, cfg.timeout)
        except ImageGenError as exc:
            # 有些网关只认 1024x1024：尺寸被拒就去掉 size 再试一次
            if getattr(exc, "status", None) == 400 and "size" in str(exc).lower():
                if on_status:
                    on_status("网关不认这个尺寸，改成默认尺寸")
                payload.pop("size", None)
                data = _post_json(f"{base}/images/generations", payload, cfg, cfg.timeout)
            else:
                raise
    else:
        fields = {"model": cfg.model, "prompt": prompt, "n": "1"}
        if size:
            fields["size"] = size
        body, content_type = _multipart(fields, refs)
        headers = cfg.headers({"Content-Type": content_type, "Accept": "application/json"})
        req = urllib.request.Request(f"{base}/images/edits", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout, context=cfg.ssl_ctx) as resp:
                data = json.loads(resp.read(8 * 1024 * 1024).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise _read_error(exc) from exc
        except urllib.error.URLError as exc:
            raise ImageGenError(f"连不上生图接口：{exc.reason}") from exc

    items = data.get("data") if isinstance(data, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), {"model": cfg.model}
        if isinstance(item.get("url"), str):
            return _download(item["url"], cfg.ssl_ctx), {"model": cfg.model}
    raise ImageGenError("接口返回里没有图片")


def _should_switch_channel(err: ImageGenError) -> bool:
    """这个错误是不是「这台网关没有这条通道」，值得换一种通道再试。"""
    if err.status not in (403, 404, 405):
        return False
    text = f"{err} {getattr(err, 'detail', '')}"
    return any(mark in text for mark in (
        "not served by this gateway", "404", "405", "not found", "Not Found",
    ))


def generate(store, prompt: str, refs: list[Path] | None = None, aspect: str | None = None,
             on_status: Callable[[str], None] | None = None) -> tuple[bytes, str, dict[str, Any]]:
    """生一张图，返回 (图片字节, 扩展名, 元信息)。

    网关没有配置的那条通道会自动换一条试（65535 异步任务 ⇄ OpenAI 兼容），
    换成功之后把配置也改过去，下次就不用再试错。
    """
    cfg = _Cfg(store)
    refs = [Path(p) for p in (refs or []) if Path(p).is_file()]
    aspect = aspect or cfg.aspect
    if cfg.provider == "comfyui":
        # 本地模型这条路不走网关、不需要 Key：直接给 ComfyUI 提交工作流
        from . import comfy

        data, meta = comfy.generate(store, prompt, refs, aspect, on_status)
        ext = ".png"
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        meta["provider"] = "comfyui"
        meta["refs"] = len(refs)
        return data, ext, meta
    order = ["openai", "65535"] if cfg.provider == "openai" else ["65535", "openai"]
    errors: list[str] = []
    for provider in order:
        try:
            if provider == "openai":
                data, meta = _run_openai(cfg, prompt, refs, aspect, on_status)
            else:
                data, meta = _run_tasks(cfg, prompt, refs, aspect, on_status)
        except ImageGenError as exc:
            errors.append(f"{'OpenAI 兼容' if provider == 'openai' else '65535 异步任务'}：{exc}")
            if provider == order[0] and _should_switch_channel(exc):
                if on_status:
                    on_status("换一种接口通道再试…")
                continue
            raise ImageGenError("；".join(errors), status=exc.status, detail=getattr(exc, "detail", "")) from exc
        if provider != cfg.provider:
            try:
                store.set("companion_image_provider", provider)
                store.save()
            except Exception:
                pass
            if on_status:
                on_status("已自动换成可用的接口通道")
        meta["channel"] = provider
        break
    if not data:
        raise ImageGenError("接口没有返回图片内容")
    ext = ".png"
    if data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    meta["provider"] = cfg.provider
    meta["refs"] = len(refs)
    return data, ext, meta

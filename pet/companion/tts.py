# -*- coding: utf-8 -*-
"""本地 GPT-SoVITS 语音（api_v2 的 /tts）。

设计取舍：
- **合成在后台线程**、**播放回主线程**：合成是 HTTP + 模型推理，几秒到十几秒；
  直接在主线程做会把桌宠动画卡住。
- 模型 / 参考音频 / 语速全部走配置，用户可以自己换（设置页里能选文件、能试听）。
- 服务没起来、模型没配、端口不通……一律只记日志并**静默放弃**——语音是锦上添花，
  不能因为它把主动消息本身也搞丢。

踩过的坑（2026-11）：整合包会同时开三个 Gradio 网页（webui.py 9874、
inference_webui.py 9872、subfix 9871），它们**不是** API。把地址填成网页端口时
``POST /tts`` 会回 ``404 {"detail":"Not Found"}``，看起来「在线」其实根本调不通。
所以这里把「网页 / 别的服务 / 代理占用 / 真接口 / 没开」分开判断，
并且能自己挑一个空闲端口把 ``api_v2.py`` 拉起来。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal

log = logging.getLogger(__name__)

DEFAULT_DIR = r"D:\AI\AI Voice"
DEFAULT_API = "http://127.0.0.1:9880"
# api_v2 在 9880；其余是「万一被占就往后挪」的候选
API_HINT_PORTS = (9880, 9881, 9882, 9886, 9888, 9890, 9899, 9780)


# ------------------------------------------------------------------ 合成缓存
#: 合成结果缓存目录名（``<配置目录>/companion/tts_cache``）。
CACHE_DIR_NAME = "tts_cache"
#: 缓存文件上限：固定台词只占几十个，其余是对话里的句子，攒够就按最久没用淘汰。
CACHE_MAX_FILES = 600


def cache_dir_of(store) -> str:
    """缓存目录；拿不到 store 的 root 时返回空串（等于不缓存）。"""
    root = getattr(store, "root", None)
    if not root:
        return ""
    return str(Path(root) / CACHE_DIR_NAME)


def cache_key(text: str, cfg: dict[str, Any]) -> str:
    """缓存键 = 文本 + 所有影响音色的参数。

    服务地址（含端口）**不进键**：换个端口拉起来的还是同一套权重，把它算进去
    会让「换端口就全部失效」。真正改音色的是权重 / 参考音频 / 语速 / 语言。
    """
    parts = [
        str(text or "").strip(),
        str(cfg.get("gpt") or ""),
        str(cfg.get("sovits") or ""),
        str(cfg.get("ref_audio") or ""),
        str(cfg.get("ref_text") or ""),
        str(cfg.get("speed") or 1.0),
        str(cfg.get("text_lang") or "zh"),
        str(cfg.get("prompt_lang") or "zh"),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _cache_path(cfg: dict[str, Any], text: str) -> Path | None:
    folder = str(cfg.get("cache_dir") or "")
    if not folder:
        return None
    return Path(folder) / f"{cache_key(text, cfg)}.wav"


def cache_get(cfg: dict[str, Any], text: str) -> bytes | None:
    """取已合成过的音频（命中顺手更新 mtime，避免这句被当成「最久没用」淘汰）。"""
    path = _cache_path(cfg, text)
    if path is None:
        return None
    try:
        if not path.is_file() or path.stat().st_size <= 44:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    try:
        os.utime(path, None)
    except OSError:
        pass
    return data


def cache_put(cfg: dict[str, Any], text: str, data: bytes) -> Path | None:
    """把合成结果写进缓存（先写 .part 再替换，读到的永远是整文件）。"""
    path = _cache_path(cfg, text)
    if path is None or not data:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_bytes(data)
        part.replace(path)
    except OSError:
        log.debug("写语音缓存失败：%s", path, exc_info=True)
        return None
    prune_cache(path.parent)
    return path


def prune_cache(folder: Path, *, max_files: int = CACHE_MAX_FILES) -> int:
    """缓存超过上限时按「最久没用」删旧的，返回删掉的个数。"""
    try:
        files = list(Path(folder).glob("*.wav"))
    except OSError:
        return 0
    extra = len(files) - int(max_files)
    if extra <= 0:
        return 0
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    removed = 0
    for path in files[:extra]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.debug("旧语音缓存没删掉：%s", path)
    return removed


def cache_stat(cfg: dict[str, Any]) -> tuple[int, int]:
    """(文件数, 总字节)，给设置页/日志用。"""
    folder = str(cfg.get("cache_dir") or "")
    if not folder:
        return 0, 0
    count = size = 0
    try:
        for path in Path(folder).glob("*.wav"):
            try:
                size += path.stat().st_size
                count += 1
            except OSError:
                continue
    except OSError:
        return 0, 0
    return count, size


# ------------------------------------------------------------------ 配置
def tts_config(store) -> dict[str, Any]:
    def _get(key, default):
        try:
            value = store.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value in (None, "") else value

    try:
        speed = float(_get("companion_tts_speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    return {
        "enabled": bool(_get("companion_tts_enabled", False)),
        "read_proactive": bool(_get("companion_tts_read_proactive", True)),
        # 「所有对话」= AI 对话（完整聊天窗 / 快速对话 / 灵动岛）+ 识屏问答 + 主动开口
        "read_chat": bool(_get("companion_tts_read_chat", True)),
        "read_screen": bool(_get("companion_tts_read_screen", True)),
        # 点击桌宠跳出来的台词（「好女孩……」「欧鲸鲸……」这些）
        "read_self_talk": bool(_get("companion_tts_read_self_talk", True)),
        # 出去溜达时报的那几句（出门、途中「风挺大」、回家）
        "read_walk": bool(_get("companion_tts_read_walk", True)),
        "api": str(_get("companion_tts_api", DEFAULT_API)).rstrip("/"),
        "dir": str(_get("companion_tts_dir", DEFAULT_DIR)),
        "gpt": str(_get("companion_tts_gpt", "") or ""),
        "sovits": str(_get("companion_tts_sovits", "") or ""),
        "ref_audio": str(_get("companion_tts_ref_audio", "") or ""),
        "ref_text": str(_get("companion_tts_ref_text", "") or ""),
        "text_lang": str(_get("companion_tts_text_lang", "zh") or "zh"),
        "prompt_lang": str(_get("companion_tts_prompt_lang", "zh") or "zh"),
        "speed": max(0.5, min(2.0, speed)),
        # 合成结果复用目录：固定台词（「好女孩……」这些）点第一次之后就直读缓存，
        # 不再每次调模型——一句要 2~10 秒，点击变「没反应」多半就是它。
        "cache_dir": cache_dir_of(store),
    }


def cfg_from_form(values: dict[str, Any]) -> dict[str, Any]:
    """设置页表单（``companion_tts_*``）→ :func:`tts_config` 的短键。

    两边键名不一样（表单是配置键、这里用短键），直接 ``cfg.update(表单)``
    等于什么都没改——之前「改了地址/模型/参考音频却不生效」就是这个原因。
    """
    return {str(k)[len("companion_tts_"):]: v for k, v in (values or {}).items()
            if str(k).startswith("companion_tts_")}


def scan_models(install_dir: str) -> tuple[list[str], list[str]]:
    """扫出 GPT(.ckpt) / SoVITS(.pth) 权重，返回相对安装目录的路径。"""
    root = Path(install_dir)
    gpt: list[str] = []
    sovits: list[str] = []
    if not root.is_dir():
        return gpt, sovits
    for folder in sorted(root.glob("GPT_weights*")):
        if folder.is_dir():
            gpt += [f"{folder.name}/{p.name}" for p in sorted(folder.glob("*.ckpt"))]
    for folder in sorted(root.glob("SoVITS_weights*")):
        if folder.is_dir():
            sovits += [f"{folder.name}/{p.name}" for p in sorted(folder.glob("*.pth"))]
    return gpt, sovits


# ------------------------------------------------------------------ 探测
# 本机请求一律不走代理：开着 Clash/系统代理时，urllib 会把 http://127.0.0.1:9880
# 也丢给代理，于是拿到 502 upstream connect failed 或干脆超时，看着像「服务没开」。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout: float):
    return _NO_PROXY.open(req, timeout=timeout)


def _detail(exc: urllib.error.HTTPError, limit: int = 300) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _content_type(url: str, timeout: float) -> str:
    """只看响应头，用来区分「网页」和「接口」。"""
    try:
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        with _open(req, timeout) as resp:
            return str(resp.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        return str((exc.headers or {}).get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return ""


def probe(cfg: dict[str, Any], *, timeout: float = 4.0) -> dict[str, Any]:
    """这个地址到底是什么。

    ``state`` 取值：
    - ``api``     : 真 GPT-SoVITS 接口（/tts 路由在，参数不全时回 400/422）
    - ``webui``   : Gradio 网页（webui.py / inference_webui.py），不是接口
    - ``proxy``   : 502/503/504，端口被代理或别的上游占着
    - ``foreign`` : 有响应，但不是 GPT-SoVITS
    - ``down``    : 连不上（服务没开 / 端口不通）
    - ``empty``   : 没填地址
    """
    api = str(cfg.get("api") or "").rstrip("/")
    if not api:
        return {"state": "empty", "api": api, "status": 0, "detail": "还没填服务地址"}
    try:
        req = urllib.request.Request(f"{api}/tts", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with _open(req, timeout) as resp:
            resp.read()
            return {"state": "api", "api": api, "status": int(resp.status), "detail": ""}
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        body = _detail(exc)
        if code in (400, 405, 415, 422):
            # 路由在，只是没给全参数 —— 这就是 api_v2
            return {"state": "api", "api": api, "status": code, "detail": body}
        if code in (502, 503, 504):
            return {"state": "proxy", "api": api, "status": code, "detail": body}
        if code == 404:
            if "html" in _content_type(f"{api}/", timeout).lower():
                return {"state": "webui", "api": api, "status": code, "detail": body}
            return {"state": "foreign", "api": api, "status": code, "detail": body}
        return {"state": "foreign", "api": api, "status": code, "detail": body}
    except Exception as exc:  # noqa: BLE001
        return {"state": "down", "api": api, "status": 0, "detail": str(exc)}


def scan_api_ports(cfg: dict[str, Any], *, timeout: float = 0.4) -> str:
    """在候选端口里找已经在跑的 api_v2（整合包常常是 pid 里带端口启动的）。"""
    host = _host_port(cfg.get("api") or DEFAULT_API)[0]
    for port in API_HINT_PORTS:
        api = f"http://{host}:{port}"
        if probe({"api": api}, timeout=timeout)["state"] == "api":
            return api
    return ""


def ping(cfg: dict[str, Any], *, timeout: float = 4.0) -> bool:
    """服务在不在（要求是真接口，不是网页也不是代理）。"""
    return probe(cfg, timeout=timeout)["state"] == "api"


def describe(cfg: dict[str, Any], *, timeout: float = 4.0) -> str:
    """给设置页用的一句话诊断。"""
    info = probe(cfg, timeout=timeout)
    state, api = info["state"], info["api"]
    if state == "empty":
        return "还没填服务地址（一般是 http://127.0.0.1:9880）"
    if state == "api":
        extra = ""
        if info["status"] == 400:
            extra = "（接口在，试听时缺的参数会由桌宠补上）"
        return f"{api} 在线，是 GPT-SoVITS 接口（HTTP {info['status']}）{extra}"
    if state == "webui":
        found = scan_api_ports(cfg)
        if found:
            return (f"{api} 是 GPT-SoVITS 的网页界面（不是接口）。"
                    f"把服务地址改成 {found} 就行")
        return (f"{api} 是 GPT-SoVITS 的网页界面（webui.py / inference_webui.py），不是 API 接口。"
                "语音要连 api_v2.py（默认 9880）——点「启动 / 自动连接」会自动拉起并填好地址")
    if state == "proxy":
        return (f"{api} 上的不是 GPT-SoVITS（HTTP {info['status']}），端口可能被代理或别的程序占了；"
                "点「启动 / 自动连接」会自动换一个空闲端口")
    if state == "foreign":
        return (f"{api} 有响应但不是 GPT-SoVITS 接口"
                f"（HTTP {info['status']} {info['detail'][:80]}）；点「启动 / 自动连接」自动换端口")
    return f"连不上 {api}：{info['detail'][:90]}（点「启动 / 自动连接」自动拉起 api_v2.py）"


# ------------------------------------------------------------------ 合成
def _post_json(url: str, payload: dict, timeout: float) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with _open(req, timeout) as resp:
        return resp.read()


def _get(url: str, timeout: float) -> None:
    with _open(urllib.request.Request(url), timeout) as resp:
        resp.read()


def synthesize_ex(cfg: dict[str, Any], text: str, *,
                  timeout: float = 120.0) -> tuple[bytes | None, str]:
    """调 /tts 拿 wav 字节，同时把失败原因带回来（给设置页显示）。"""
    text = str(text or "").strip()
    if not text:
        return None, "没有要念的文字"
    api = str(cfg.get("api") or "").rstrip("/")
    if not api:
        return None, "还没填服务地址"
    if not cfg.get("ref_audio"):
        return None, "还没选参考音频（api_v2 必须有 ref_audio_path）"
    payload = {
        "text": text,
        "text_lang": cfg.get("text_lang") or "zh",
        "ref_audio_path": cfg.get("ref_audio") or "",
        "prompt_text": cfg.get("ref_text") or "",
        "prompt_lang": cfg.get("prompt_lang") or "zh",
        "speed_factor": float(cfg.get("speed") or 1.0),
        "media_type": "wav",
        "text_split_method": "cut5",
        "batch_size": 1,
        "temperature": 1.0,
        "top_k": 5,
        "top_p": 1.0,
        "seed": -1,
    }
    try:
        return _post_json(f"{api}/tts", payload, timeout), ""
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        body = _detail(exc, 400)
        log.info("GPT-SoVITS 返回 %s：%s", code, body)
        if code == 404:
            return None, (f"{api}/tts 不存在（404）——这个地址多半是网页界面，"
                          "不是 api_v2.py 的接口")
        if code in (502, 503, 504):
            return None, f"{api} 回 {code}，端口被代理或别的程序占了"
        if code == 400 and ("ref_audio" in body or "ref_audio_path" in body):
            return None, "参考音频路径不对或服务端读不到（换成绝对路径再试）"
        return None, f"GPT-SoVITS 返回 HTTP {code}：{body[:150]}"
    except Exception as exc:  # noqa: BLE001
        log.info("GPT-SoVITS 合成失败：%s", exc)
        return None, f"连不上 {api}：{exc}"


def synthesize(cfg: dict[str, Any], text: str, *, timeout: float = 120.0) -> bytes | None:
    """兼容旧调用：只要字节，失败返回 None（不抛）。"""
    data, _err = synthesize_ex(cfg, text, timeout=timeout)
    return data


def apply_weights(cfg: dict[str, Any], *, timeout: float = 60.0) -> bool:
    """按设置切换 GPT / SoVITS 权重；没配置就沿用服务当前加载的。"""
    ok = True
    for key, endpoint in (("gpt", "set_gpt_weights"), ("sovits", "set_sovits_weights")):
        value = str(cfg.get(key) or "").strip()
        if not value:
            continue
        try:
            _get(f"{cfg['api']}/{endpoint}?weights_path={urllib.parse.quote(value)}", timeout)
        except Exception as exc:  # noqa: BLE001
            log.info("切换 %s 权重失败（%s）：%s", key, value, exc)
            ok = False
    return ok


# ------------------------------------------------------------------ 启动
def server_python(cfg: dict[str, Any]) -> str:
    """安装目录自带的 python（GPT-SoVITS 整合包自带 runtime）。"""
    root = Path(str(cfg.get("dir") or DEFAULT_DIR))
    for rel in ("runtime/python.exe", "python.exe", "runtime/python", "venv/bin/python"):
        candidate = root / rel
        if candidate.is_file():
            return str(candidate)
    return ""


def _host_port(api: str) -> tuple[str, int]:
    raw = str(api or DEFAULT_API)
    parsed = urllib.parse.urlsplit(raw if "//" in raw else f"http://{raw}")
    host = parsed.hostname or "127.0.0.1"
    try:
        port = int(parsed.port or 9880)
    except ValueError:
        port = 9880
    return host, port


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口空不空。

    不能用 ``bind`` 判断：Windows 上 ``0.0.0.0:9874`` 被占时，
    绑 ``127.0.0.1:9874`` 仍然能成功，结果接口被塞进 webui 的端口、
    把网页顶掉。所以改成「能不能连上」——连得上就是有人监听。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, int(port)))
        except OSError:
            return True
        return False


def log_path(cfg: dict[str, Any]) -> Path:
    root = Path(str(cfg.get("dir") or DEFAULT_DIR))
    return root / "logs" / "teaggpet_api.log"


def launch_server(cfg: dict[str, Any], *, port: int | None = None) -> tuple[bool, str]:
    """在安装目录后台拉起 api_v2.py（脱离本进程，桌宠退出也不影响）。"""
    root = Path(str(cfg.get("dir") or DEFAULT_DIR))
    script = root / "api_v2.py"
    python = server_python(cfg)
    if not script.is_file():
        return False, f"没找到 {script}"
    if not python:
        return False, "安装目录下没找到 python（runtime/python.exe）"
    if port is None:
        port = _host_port(cfg.get("api") or DEFAULT_API)[1]
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    logfile = log_path(cfg)
    handle: Any = subprocess.DEVNULL
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handle = open(logfile, "ab")  # noqa: SIM115 —— 交给子进程，父进程不关
    except OSError:
        handle = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [python, "api_v2.py", "-a", "127.0.0.1", "-p", str(port)],
            cwd=str(root), close_fds=False, creationflags=flags,
            stdout=handle, stderr=subprocess.STDOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"启动失败：{exc}"
    finally:
        if hasattr(handle, "close"):
            try:
                handle.close()   # 子进程已经继承句柄，父进程这份可以关
            except Exception:  # noqa: BLE001
                pass
    return True, f"已后台启动 api_v2.py（端口 {port}，pid {proc.pid}），日志 {logfile}"


def ensure_server(cfg: dict[str, Any], *, wait: float = 150.0,
                  on_progress=None) -> tuple[bool, str, str]:
    """保证有一个能用的 api_v2，返回 (ok, api, 说明)。

    顺序：当前地址能用就用 → 扫候选端口看有没有已经在跑的 → 挑空闲端口拉起来 → 轮询等就绪。
    """
    def _say(text: str) -> None:
        if on_progress is not None:
            try:
                on_progress(text)
            except Exception:  # noqa: BLE001
                pass

    info = probe(cfg, timeout=3.0)
    if info["state"] == "api":
        return True, info["api"], f"{info['api']} 已经在跑"

    _say("正在找已经在跑的接口…")
    found = scan_api_ports(cfg, timeout=0.4)
    if found:
        return True, found, f"发现已在跑的接口 {found}"

    host, base_port = _host_port(cfg.get("api") or DEFAULT_API)
    port = 0
    for candidate in (base_port, *API_HINT_PORTS):
        if port_free(candidate, host):
            port = candidate
            break
    if not port:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])

    if info["state"] == "webui":
        _say(f"{info['api']} 是网页界面，改在 {port} 端口启动接口…")
    else:
        _say(f"正在 {port} 端口启动 api_v2.py…")
    ok, message = launch_server(cfg, port=port)
    if not ok:
        return False, info["api"], message

    api = f"http://{host}:{port}"
    deadline = time.monotonic() + max(10.0, float(wait))
    while time.monotonic() < deadline:
        time.sleep(1.5)
        state = probe({"api": api}, timeout=1.5)["state"]
        if state == "api":
            return True, api, f"已启动 api_v2：{api}"
        if state == "down":
            _say(f"等待 api_v2 加载模型…（{api}）")
    return False, api, (f"api_v2 启动后 {int(wait)} 秒还没响应，看一下日志 {log_path(cfg)}")


# ------------------------------------------------------------------ 播放
_weights_state: tuple = ()


def ensure_weights(cfg: dict[str, Any]) -> bool:
    """切到配置里的 GPT / SoVITS 权重（同一个服务 + 同一组权重只切一次）。

    api_v2 启动时加载的是它自己的默认权重，配置里选的"大肥鱼 / Deepseek"
    不切过去，念出来的就不是那个声音。每句都切会白花时间，所以按
    ``(api, gpt, sovits)`` 记住已经切好的组合。
    """
    global _weights_state
    if not (cfg.get("gpt") or cfg.get("sovits")):
        return True   # 没指定就用服务当前的
    key = (str(cfg.get("api") or ""), str(cfg.get("gpt") or ""), str(cfg.get("sovits") or ""))
    if _weights_state == key:
        return True
    if apply_weights(cfg):
        _weights_state = key
        return True
    _weights_state = ()
    return False


class _Synth(QThread):
    ready = Signal(bytes)
    failed = Signal(str)

    def __init__(self, cfg: dict[str, Any], text: str, parent=None) -> None:
        super().__init__(parent)
        self._cfg = dict(cfg)
        self._text = text

    def run(self) -> None:  # noqa: D102
        try:
            ensure_weights(self._cfg)
        except Exception:  # noqa: BLE001
            log.debug("切换权重失败（继续用服务当前权重）", exc_info=True)
        data, err = synthesize_ex(self._cfg, self._text)
        if data:
            self.ready.emit(data)
        else:
            self.failed.emit(err or "合成失败")


class VoicePlayer(QObject):
    """把合成好的 wav 播出来（QMediaPlayer，主线程）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._player = None
        self._out = None
        self._tmp: Path | None = None
        self._synth: _Synth | None = None

    def speak(self, text: str, cfg: dict[str, Any]) -> bool:
        if self._synth is not None and self._synth.isRunning():
            return False   # 上一句还没合成完，别叠
        synth = _Synth(cfg, text, self)
        synth.ready.connect(self._on_ready)
        synth.failed.connect(lambda msg: log.info("语音合成失败：%s", msg))
        synth.finished.connect(self._on_done)
        self._synth = synth
        synth.start()
        return True

    def _on_done(self) -> None:
        self._synth = None

    def _on_ready(self, data: bytes) -> None:
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            if self._player is not None:
                self._player.stop()
            tmp = Path(tempfile.gettempdir()) / "teaggpet_tts.wav"
            tmp.write_bytes(data)
            self._tmp = tmp
            if self._out is None:
                self._out = QAudioOutput(self)
            if self._player is None:
                self._player = QMediaPlayer(self)
                self._player.setAudioOutput(self._out)
            self._player.setSource(QUrl.fromLocalFile(str(tmp)))
            self._player.play()
        except Exception:  # noqa: BLE001
            log.debug("播放合成语音失败", exc_info=True)

    def stop(self) -> None:
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 统一播报
def split_for_speech(text: str, *, max_chars: int = 80) -> list[str]:
    """把一段回复切成一句一句能念的短句。

    直接复用宿主那套 ``chat_reply_voice`` 的清洗 + 断句（去代码块 / markdown 记号、
    按句末标点切、超长再按逗号就近切）；拿不到就退化成"压成一行整段念"。
    逐句合成是为了让长回复**第一句几秒内就出声**，而不是等整段合成完。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        from ..chat_reply_voice import split_sentences

        return split_sentences(raw, max_chars=max_chars)
    except Exception:  # noqa: BLE001
        log.debug("断句模块不可用，整段念", exc_info=True)
        flat = " ".join(raw.split())
        return [flat] if flat else []


class LocalSpeaker(QObject):
    """进程内**唯一**的本地语音播报出口：句子队列 → 串行合成 → 自动播放。

    与 :class:`VoicePlayer` 的分工：VoicePlayer 只管"一句"（设置页试听），
    这里管"一段对话"——按句切开、一句播完再合成下一句。
    串行有两个理由：
    - 并行合成会让几句同时开口，听不出在说什么；
    - GPT-SoVITS 是单进程推理，并发请求只会互相排队。

    新的对话进来默认**覆盖**旧队列（``interrupt=True``）：用户在聊天里已经看到
    新回复了，还在念上一轮的内容只会让人以为卡住了。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._queue: deque[str] = deque()
        self._cfg: dict[str, Any] = {}
        self._synth: _Synth | None = None
        self._player = None
        self._out = None
        self._playing = False
        self._last_heal = 0.0
        # 已合成、等这一句念完再播的下一句（播放中预合成，句间不留空档）
        self._buffered: bytes | None = None
        # 当前这句的临时文件（每句一个独立路径，见 _write_tmp 的注释）
        self._tmp_path: Path | None = None
        self._seq = 0
        # 代次：打断时 +1，让「已经飞在路上」的旧合成结果作废
        # （否则用户已经看到新回复，旧那句还会突然念出来）
        self._gen = 0

    # ---------------------------------------------------------------- 对外
    @property
    def busy(self) -> bool:
        """还有话没念完（队列非空 / 在合成 / 待播 / 在出声）。"""
        return (bool(self._queue) or self._playing or self._buffered is not None
                or (self._synth is not None and self._synth.isRunning()))

    @property
    def pending(self) -> int:
        return len(self._queue)

    def speak(self, text: str, cfg: dict[str, Any], *, interrupt: bool = True) -> bool:
        """把一段文字排进播报队列；已经有人在念时按 ``interrupt`` 决定是否打断。"""
        sentences = split_for_speech(text)
        if not sentences:
            return False
        if interrupt:
            self._gen += 1
            self._queue.clear()
            self._stop_playback()
        self._cfg = dict(cfg)
        self._queue.extend(sentences)
        self._pump()
        return True

    def play_data(self, data: bytes, cfg: dict[str, Any] | None = None) -> bool:
        """直接播一段**已经合成好**的音频（设置页「试听一句」走这里）。

        试听以前自己养了一个 QMediaPlayer + 一个固定文件名的临时文件，同一 URL
        二次 ``setSource`` 会沿用上一段媒体描述（duration 不刷新），听感就是
        「提示合成成功却没声音、或者只响半句」。这里统一走 ``_play`` 的独立文件
        路径，和对话朗读是同一条链路——试听听到的就是平时听到的。
        """
        if not data:
            return False
        self._gen += 1
        self._queue.clear()
        if cfg:
            self._cfg = dict(cfg)
        self._stop_playback()
        self._play(data)
        return True

    def stop(self) -> None:
        self._gen += 1
        self._queue.clear()
        self._stop_playback()

    # ---------------------------------------------------------------- 内部
    def _pump(self) -> None:
        """合成下一句。

        播放中也可以合成（``_playing`` 不再是闸门）：合成慢是句间空档的唯一原因，
        一边念一边把下一句备好，接得上。备好的那句放在 ``_buffered`` 等播。

        先查缓存：固定台词（「好女孩……」「欧鲸鲸……」这些）点第二次起就直接播，
        既不占合成线程也不用等模型。
        """
        if self._synth is not None and self._synth.isRunning():
            return
        if self._buffered is not None:
            return          # 已经备好一句在等，别再多合成
        if not self._queue:
            return
        sentence = self._queue.popleft()
        gen = self._gen
        cached = cache_get(self._cfg, sentence)
        if cached is not None:
            log.debug("语音缓存命中：%s", sentence[:24])
            self._on_ready(cached, gen, sentence, cached=True)
            return
        synth = _Synth(dict(self._cfg), sentence, self)
        synth.ready.connect(lambda data, g=gen, s=sentence: self._on_ready(data, g, s))
        synth.failed.connect(lambda msg, g=gen: self._on_failed(msg, g))
        synth.finished.connect(self._on_synth_done)
        self._synth = synth
        synth.start()

    def _on_synth_done(self) -> None:
        self._synth = None
        QTimer.singleShot(0, self._pump)

    def _on_failed(self, message: str, gen: int = 0) -> None:
        if gen != self._gen:
            return
        # 合成失败（服务没开 / 参考音频不对）：跳过这句继续，别把整段对话吞掉
        log.info("本地语音合成失败：%s", message)
        self._maybe_heal(message)

    def _maybe_heal(self, message: str) -> None:
        """服务没起来时顺手在后台拉一次（60 秒冷却），用户不用先去设置页点启动。"""
        if _heal_hook is None:
            return
        now = time.monotonic()
        if now - self._last_heal < 60.0:
            return
        self._last_heal = now
        try:
            _heal_hook(dict(self._cfg))
        except Exception:  # noqa: BLE001
            log.debug("触发语音自愈失败", exc_info=True)

    def _ensure_player(self):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        if self._out is None:
            self._out = QAudioOutput(self)
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._out)
            self._player.mediaStatusChanged.connect(self._on_media_status)
        return self._player

    def _on_ready(self, data: bytes, gen: int = 0, text: str = "", *,
                  cached: bool = False) -> None:
        if gen != self._gen or not data:
            return   # 这句已经被新对话作废了
        if not cached and text:
            # 顺手存一份：同一句下次直接播缓存（点击台词、溜达的固定播报都会命中）
            cache_put(self._cfg, text, data)
        if self._playing:
            # 上一句还在念：先存着，等它 EndOfMedia 再接上。
            # 播放中途换源会把上一句当场截断（「语音不全」的另一种成因）。
            self._buffered = data
            return
        self._play(data)

    def _write_tmp(self, data: bytes) -> Path:
        """把这一句写进**独立**的临时文件。

        不能复用一个固定文件名：Qt/WMF 在同一个 URL 上二次 ``setSource``
        会沿用上一句的媒体描述（实测 duration 一直是上一句的长度，
        4.9 秒的长句只播 2.3 秒就 EndOfMedia —— 听感就是「语音不全」）。
        每句一个新路径，播放器才会重新解析。
        """
        self._seq += 1
        folder = Path(tempfile.gettempdir()) / "teaggpet_tts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"speak_{os.getpid()}_{self._seq}_{time.monotonic_ns()}.wav"
        path.write_bytes(data)
        return path

    def _drop_tmp(self) -> None:
        """删掉上一条临时文件（播放器已释放句柄，删不掉就留给下次启动清）。"""
        path, self._tmp_path = self._tmp_path, None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            log.debug("临时语音文件没删掉：%s", path)

    def _play(self, data: bytes) -> None:
        path = None
        try:
            self._drop_tmp()          # 先清上一句，别在 %TEMP% 里堆文件
            path = self._write_tmp(data)
            player = self._ensure_player()
            self._tmp_path = path
            self._playing = True
            player.setSource(QUrl.fromLocalFile(str(path)))
            player.play()
        except Exception:  # noqa: BLE001
            self._playing = False
            log.debug("播放合成语音失败", exc_info=True)
            if path is not None and self._tmp_path is path:
                self._drop_tmp()
            QTimer.singleShot(0, self._pump)

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        done = (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia)
        if status not in done:
            return
        self._playing = False
        nxt, self._buffered = self._buffered, None
        if nxt:
            # 下一句已经合成好了：直接接上，中间不留合成等待的空档
            self._play(nxt)
            return
        QTimer.singleShot(0, self._pump)

    def _stop_playback(self) -> None:
        self._playing = False
        self._buffered = None
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:  # noqa: BLE001
            pass


_speaker: LocalSpeaker | None = None
#: 合成失败时的自愈回调（由 runtime 注入：后台 ensure_server 并把新地址写回配置）
_heal_hook = None

#: 播报场景 → 对应的配置开关。全部对话都过这张表，方便用户逐个关。
KIND_KEYS = {"chat": "read_chat", "proactive": "read_proactive",
             "screen": "read_screen", "self_talk": "read_self_talk",
             "walk": "read_walk"}


def set_heal_hook(fn) -> None:
    """注入「服务没起来就自动拉起」的回调（传 None 解除）。"""
    global _heal_hook
    _heal_hook = fn


def purge_speak_cache() -> int:
    """清掉**别的进程**留下的合成音频（%TEMP%/teaggpet_tts），返回删掉的个数。

    每句一个文件是为了让播放器重新解析媒体（见 ``LocalSpeaker._write_tmp``），
    代价是异常退出（被强杀）时会留下残file，启动时顺手扫一遍。
    当前进程自己的文件不动——正在播的可能就是它。
    """
    folder = Path(tempfile.gettempdir()) / "teaggpet_tts"
    if not folder.is_dir():
        return 0
    mine = f"speak_{os.getpid()}_"
    removed = 0
    for path in folder.glob("speak_*.wav"):
        if path.name.startswith(mine):
            continue
        try:
            path.unlink()
            removed += 1
        except Exception:  # noqa: BLE001
            log.debug("残留语音文件没删掉：%s", path)
    return removed


def precache_texts(cfg: dict[str, Any], texts, *, limit: int = 80,
                   on_progress=None) -> int:
    """把固定台词预先合成进缓存，返回本次新合成的句数。

    为什么要预合成：点击台词是**有限且固定**的几句（「好女孩……」「欧鲸鲸……」），
    现合成一句要 2~10 秒，用户点下去只会觉得「点了没反应」。启动后（或保存设置后）
    在后台把它们都灌进缓存，之后每次点击都是直接播。

    断句口径与播放必须完全一致（都用 :func:`split_for_speech`），否则预热写的是
    整句、播放时按短句查缓存 → 永远不命中。
    """
    sentences: list[str] = []
    for raw in texts or []:
        sentences.extend(split_for_speech(str(raw or "")))
    done = 0
    tried = 0
    seen: set[str] = set()
    for sentence in sentences:
        if sentence in seen or tried >= int(limit):
            continue
        seen.add(sentence)
        if cache_get(cfg, sentence) is not None:
            continue
        if not cfg.get("api"):
            break
        tried += 1
        data, err = synthesize_ex(cfg, sentence)
        if not data:
            log.info("预合成台词失败（跳过）：%s", err or "没有返回音频")
            continue
        cache_put(cfg, sentence, data)
        done += 1
        if on_progress is not None:
            try:
                on_progress(done, len(sentences))
            except Exception:  # noqa: BLE001
                pass
    return done


def speaker() -> LocalSpeaker:
    """进程级单例：所有对话共用一个播放器，避免两段语音同时出声。"""
    global _speaker
    if _speaker is None:
        _speaker = LocalSpeaker()
        purge_speak_cache()
    return _speaker


def speak_text(text: str, store, *, kind: str = "chat", interrupt: bool = True) -> bool:
    """按配置念一段对话（所有对话出口的统一入口）。

    任一条件不满足都静默返回 False：语音是锦上添花，不能因为它让对话本身出错。
    """
    try:
        cfg = tts_config(store)
        if not cfg.get("enabled"):
            return False
        gate = KIND_KEYS.get(str(kind or "chat"))
        if gate and not cfg.get(gate):
            return False
        if not cfg.get("ref_audio"):
            log.debug("本地语音没有参考音频，跳过朗读")
            return False
        return speaker().speak(text, cfg, interrupt=interrupt)
    except Exception:  # noqa: BLE001
        log.debug("本地语音播报失败", exc_info=True)
        return False


__all__ = ["API_HINT_PORTS", "CACHE_DIR_NAME", "CACHE_MAX_FILES", "DEFAULT_API", "DEFAULT_DIR",
           "KIND_KEYS", "LocalSpeaker",
           "VoicePlayer", "apply_weights", "cache_dir_of", "cache_get", "cache_key", "cache_put",
           "cache_stat", "cfg_from_form", "describe", "ensure_server",
           "ensure_weights", "launch_server", "log_path", "ping", "port_free", "precache_texts",
           "probe", "prune_cache", "purge_speak_cache", "scan_api_ports",
           "scan_models", "set_heal_hook", "speak_text", "speaker", "split_for_speech",
           "synthesize", "synthesize_ex", "tts_config"]

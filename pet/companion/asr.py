# -*- coding: utf-8 -*-
"""本地语音识别（Qwen3-ASR）。

设计目标
--------
* **完全本机**：不依赖任何云端 API，模型在本地 CPU 上跑（Qwen3-ASR-0.6B，
  transformers 后端 + torch CPU）。中文识别明显优于 whisper，实测 8.7 秒语音
  在 CPU 上约 4 秒出结果（≈0.5 倍实时），且标点/断句都准。
* **可选开关**：语音助手总开关关闭时本模块不会被加载，程序照常运行。
* **模型可下载**：内置 **ModelScope（魔搭）** 国内镜像下载，一键把
  ``Qwen/Qwen3-ASR-0.6B`` 拉到本地目录，无需翻墙、无需 HF 镜像。
* **模型路径可选**：用户也可以自己下好模型，把目录填进来；程序还能自动扫描本机
  （ModelScope 缓存 / 程序下载目录 / 常见目录）把找到的模型填进下拉。

模型（Qwen3-ASR）：
    0.6B  ~1.8GB   CPU 可跑，中文准确率高，推荐（默认）
    1.7B  ~3.5GB   更准，但 CPU 上偏慢

历史说明：v1.4.37 起从 faster-whisper 迁移到 Qwen3-ASR。配置键仍沿用
``companion_whisper_*``（为避免大范围改配置/兼容旧存档），语义已改为 Qwen 模型。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# ModelScope 国内镜像（魔搭官方，国内直连很快）
MODELSCOPE_ENDPOINT = "https://www.modelscope.cn"

# 可下载的模型（Qwen3-ASR）。值是 ModelScope 上的模型 repo id。
# (size, 中文标签, 大致体积说明)
MODEL_SIZES: list[tuple[str, str, str]] = [
    ("0.6B", "Qwen3-ASR-0.6B（约 1.8GB，CPU 可跑，中文准，推荐）", "约 1.8GB"),
    ("1.7B", "Qwen3-ASR-1.7B（约 3.5GB，更准，CPU 偏慢）", "约 3.5GB"),
]

_QSIZE_TO_REPO = {
    "0.6B": "Qwen/Qwen3-ASR-0.6B",
    "1.7B": "Qwen/Qwen3-ASR-1.7B",
}

_VALID_SIZES = {s for s, _label, _hint in MODEL_SIZES}


def model_label(size: str) -> str:
    for s, label, _hint in MODEL_SIZES:
        if s == size:
            return label
    return size


def repo_id_of(size: str) -> str:
    """ModelScope 上的模型 repo id（Qwen/Qwen3-ASR-<size>）。"""
    return _QSIZE_TO_REPO.get(size, f"Qwen/Qwen3-ASR-{size}")


def _size_from_repo(repo_id: str) -> str:
    tail = str(repo_id).rsplit("/", 1)[-1]  # Qwen3-ASR-0.6B
    for size in _VALID_SIZES:
        if tail.endswith(size):
            return size
    return tail


# ---------------------------------------------------------------- 下载

def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download_model(
    size: str,
    dest_root: str | Path,
    *,
    on_progress: Callable[[dict], None] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """把 ``Qwen/Qwen3-ASR-<size>`` 从 ModelScope 下载到 ``dest_root/<size>`` 并返回该目录。

    走 ModelScope 国内镜像（modelscope.hub.snapshot_download），不需要用户配代理。

    ``on_progress`` 收一个 **结构化进度字典**：
        {"done": 已下载字节, "total": 总字节, "percent": 0-100 或 None,
         "rate": 字节/秒 或 None, "text": 一句人类可读的话, "msg": 同 text}
    """
    if size not in _VALID_SIZES:
        raise ValueError(f"不支持的模型：{size}（可选：{'/'.join(sorted(_VALID_SIZES))}）")
    dest_root = Path(dest_root).expanduser()
    target = dest_root / size
    target.mkdir(parents=True, exist_ok=True)

    def _emit(msg: str, **extra) -> None:
        if on_progress is not None:
            payload = {"done": 0, "total": 0, "percent": None, "rate": None, "text": msg, "msg": msg}
            payload.update(extra)
            try:
                on_progress(payload)
            except Exception:  # noqa: BLE001
                pass

    def _is_cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    _emit(f"正在从魔搭（ModelScope）下载 {size} 模型…（{model_label(size)}）")

    repo_id = repo_id_of(size)
    last_exc: Exception | None = None
    try:
        # 延迟导入：只有真下载时才加载 modelscope（很重）
        os.environ.setdefault("MODELSCOPE_DOMAIN", "www.modelscope.cn")
        from modelscope import snapshot_download  # type: ignore

        # modelscope 自带进度条会往 stderr 写；windowed 下 sys.stderr 为 None 会崩，
        # 这里设环境变量关掉自带进度条，改用整体提示（不逐字节回报，稳）。
        os.environ["MODELSCOPE_LOG_LEVEL"] = os.environ.get("MODELSCOPE_LOG_LEVEL", "40")

        if _is_cancelled():
            _emit("下载已取消")
            return str(target)
        _emit(f"正在下载 {size}（约 1.8GB，请耐心等待，视网速几分钟…）", percent=1.0)

        path = snapshot_download(repo_id, local_dir=str(target), revision="master")
        last_exc = None
        _emit(f"下载完成：{path}", percent=100.0)
        return str(path or target)
    except Exception as exc:  # noqa: BLE001
        last_exc = exc
        log.warning("从 ModelScope 下载 %s(%s) 失败：%s", size, repo_id, exc)

    if last_exc is not None:
        raise RuntimeError(f"从魔搭下载 {size} 模型失败：{last_exc}")
    return str(target)


# ---------------------------------------------------------------- 本地扫描

def _looks_like_model_dir(path: Path) -> bool:
    """Qwen3-ASR 模型目录的标志：有 config.json + 任一 safetensors/bin 权重。"""
    try:
        if not (path / "config.json").is_file():
            return False
        for pat in ("*.safetensors", "*.bin", "*.gguf"):
            if any(path.glob(pat)):
                return True
        return False
    except OSError:
        return False


def scan_local_models(extra_dirs: list[str] | None = None) -> list[dict[str, Any]]:
    """扫描本机可能存在的 Qwen3-ASR 模型目录，返回 [{size,path,label}]。

    查这些地方：
    1. ``extra_dirs``（用户自填的模型根目录）
    2. ``<用户配置>/companion/qwen-models/*``（程序自己下载的位置）
    3. ModelScope 缓存 ``~/.cache/modelscope/hub/models--Qwen--Qwen3-ASR-*``
    4. 环境变量 ``TEAEGG_QWEN_ASR_DIR``

    找不到时返回空列表（不抛错）。
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(size: str, path: Path) -> None:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            return
        seen.add(key)
        found.append({"size": size, "path": str(path), "label": f"{size}  ·  {path}"})

    roots: list[Path] = []
    for d in extra_dirs or []:
        d = str(d or "").strip()
        if d:
            roots.append(Path(d).expanduser())
    env_dir = os.environ.get("TEAEGG_QWEN_ASR_DIR", "").strip()
    if env_dir:
        roots.append(Path(env_dir).expanduser())

    # 程序自己的下载目录（默认）
    try:
        roots.append(Path.home() / "AppData" / "Roaming" / "dsh-pet-standalone" / "companion" / "qwen-models")
    except Exception:  # noqa: BLE001
        pass

    for root in roots:
        try:
            if _looks_like_model_dir(root):
                _add(_guess_size_from_path(root), root)
            elif root.is_dir():
                for child in root.iterdir():
                    if child.is_dir() and _looks_like_model_dir(child):
                        _add(_guess_size_from_path(child), child)
        except OSError:
            continue

    # ModelScope 缓存
    try:
        cache = Path.home() / ".cache" / "modelscope" / "hub"
        if cache.is_dir():
            for repo in list(cache.glob("models--Qwen--Qwen3-ASR-*")) + \
                    list(cache.glob("Qwen/Qwen3-ASR-*")):
                size = _size_from_repo(repo.name)
                # 目录本身或其 snapshots 子目录
                if _looks_like_model_dir(repo):
                    _add(size, repo)
                else:
                    snap = repo / "snapshots"
                    if snap.is_dir():
                        for revision in snap.iterdir():
                            if revision.is_dir() and _looks_like_model_dir(revision):
                                _add(size, revision)
                                break
    except OSError:
        pass

    return found


def _guess_size_from_path(path: Path) -> str:
    """从目录名猜模型大小（0.6B/1.7B）；猜不到就原样返回目录名。"""
    name = path.name
    for size in _VALID_SIZES:
        if size in name:
            return size
    return name


# ---------------------------------------------------------------- 运行

_engine_cache: dict[str, Any] = {}
_engine_lock = threading.Lock()
# 引擎本身是 Qwen3ASRModel。torch 是懒加载的（只在真用语音时才 import torch）。
_cuda_available: "bool | None" = None


def _torch():
    import torch  # type: ignore
    return torch


def load_engine(model_path: str, *, device: str = "cpu", compute_type: str = "int8"):
    """加载（并缓存）一个 Qwen3-ASR 模型。同路径只加载一次。

    在 CPU 上用 float32（Qwen 在 CPU 上 bf16/float16 支持不佳，float32 最稳）。
    若用户选了 CUDA 但本机 CUDA 不可用/加载失败，自动回退 CPU（保证语音一定能用）。
    """
    device = (device or "cpu").strip().lower()
    global _cuda_available
    if device != "cpu" and _cuda_available is False:
        device = "cpu"
    key = f"{model_path}|{device}"
    with _engine_lock:
        if key in _engine_cache:
            return _engine_cache[key]
        for _k, _v in _engine_cache.items():
            if _k.rsplit("|", 1)[0] == str(model_path).rstrip("\\/"):
                _engine_cache[key] = _v
                return _v

        from qwen_asr import Qwen3ASRModel  # type: ignore

        def _make(dev: str):
            torch = _torch()
            if dev == "cuda":
                return Qwen3ASRModel.from_pretrained(
                    model_path, dtype=torch.float16, device_map="cuda:0",
                    max_inference_batch_size=8, max_new_tokens=256,
                )
            return Qwen3ASRModel.from_pretrained(
                model_path, dtype=torch.float32, device_map="cpu",
                max_inference_batch_size=8, max_new_tokens=256,
            )

        engine = None
        if device != "cpu":
            try:
                engine = _make("cuda")
                _cuda_available = True
            except Exception as exc:  # noqa: BLE001
                _cuda_available = False
                log.warning("Qwen3-ASR 以 CUDA 加载失败，自动回退 CPU：%s", exc)
                try:
                    del engine
                except Exception:  # noqa: BLE001
                    pass
                import gc as _gc
                _gc.collect()
                engine = None
        if engine is None:
            engine = _make("cpu")
        _engine_cache[key] = engine
        return engine


def unload_engines() -> None:
    with _engine_lock:
        _engine_cache.clear()


def resolve_model_dir(cfg_get: Callable[[str, Any], Any]) -> str:
    """按配置找到可用的模型目录；返回空串表示没有可用模型。

    优先级：用户手填路径 ``companion_whisper_model_path`` → 程序下载目录
    ``<companion>/qwen-models/<size>`` → 本机扫描到的第一个（优先匹配 size）。
    """
    custom = str(cfg_get("companion_whisper_model_path", "") or "").strip()
    if custom:
        p = Path(custom).expanduser()
        if _looks_like_model_dir(p):
            return str(p)
        if p.is_dir():
            for child in p.iterdir():
                if child.is_dir() and _looks_like_model_dir(child):
                    return str(child)
    size = str(cfg_get("companion_whisper_model_size", "0.6B") or "0.6B")
    download_root = str(cfg_get("companion_whisper_download_dir", "") or "").strip()
    if download_root:
        cand = Path(download_root).expanduser() / size
        if _looks_like_model_dir(cand):
            return str(cand)
    local = scan_local_models([download_root] if download_root else None)
    if local:
        for item in local:
            if item["size"] == size:
                return item["path"]
        return local[0]["path"]
    return ""


def transcribe(
    engine,
    audio,
    *,
    language: str = "zh",
    sample_rate: int = 16000,
    initial_prompt: str | None = None,
    vad_filter: bool = True,
    beam_size: int = 1,
    no_speech_threshold: float = 0.6,
) -> str:
    """把 numpy float32 单声道音频转成文字（纯文本，去空白）。

    ``audio`` 需 16kHz 单声道（numpy float32，范围 -1..1）。

    Qwen3-ASR 不接收 beam_size/vad_filter/initial_prompt 这些 whisper 专有参数
    （为兼容旧调用点，签名里保留但忽略）。语言统一传 "Chinese"（也支持自动识别，
    传 None 即可）。识别到什么就返回什么，不做逐段过滤（降噪已在录音重采样阶段完成）。
    """
    import numpy as _np

    audio = _np.asarray(audio, dtype=_np.float32).reshape(-1)
    # Qwen3-ASR 的语言名用全称；zh/中文 → "Chinese"
    lang = (language or "").strip().lower()
    if lang in ("", "zh", "zh-cn", "chinese", "中文", "cmn"):
        lang_arg = "Chinese"
    else:
        lang_arg = language
    results = engine.transcribe(audio=(audio, int(sample_rate or 16000)), language=lang_arg)
    if not results:
        return ""
    text = getattr(results[0], "text", "") or ""
    return text.strip()


__all__ = [
    "MODELSCOPE_ENDPOINT",
    "MODEL_SIZES",
    "download_model",
    "load_engine",
    "model_label",
    "repo_id_of",
    "resolve_model_dir",
    "scan_local_models",
    "transcribe",
    "unload_engines",
]

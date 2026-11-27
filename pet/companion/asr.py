# -*- coding: utf-8 -*-
"""本地语音识别（Whisper / faster-whisper）。

设计目标
--------
* **完全本机**：不依赖任何云端 API，模型在本地 CPU/GPU 上跑（faster-whisper +
  CTranslate2，比原版 whisper 快数倍）。
* **可选开关**：语音助手总开关关闭时本模块不会被加载，程序照常运行。
* **模型可下载**：内置国内镜像（hf-mirror.com）下载，用户在设置里选模型大小，
  一键下载到本地目录；下载走 ``huggingface_hub.download_model`` 并临时设
  ``HF_ENDPOINT`` 指向镜像，无需翻墙。
* **模型路径可选**：用户也可以自己下好模型，把目录填进来；程序还能自动扫描本机
  （HF 缓存 / 常见模型目录）把找到的模型填进下拉，省得手打。

模型大小参考（faster-whisper / CTranslate2，int8）：
    tiny   ~75MB   base ~145MB  small ~480MB  medium ~1.5GB  large-v3 ~3.1GB
中文识别质量：base 够日常用，small 明显更准；唤醒词/结束词这种短词识别用 tiny
就够（只求又快又准地认出「小鱼小鱼 / 休息一下」）。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# 国内镜像（HuggingFace 反代）；下载时设 HF_ENDPOINT 指向它
HF_MIRROR = "https://hf-mirror.com"

# 可下载的模型大小（按质量/体积递增）。值是 faster-whisper 的 size_or_id，
# 对应的 HF 仓库是 Systran/faster-whisper-<size>。
MODEL_SIZES: list[tuple[str, str, str]] = [
    # (size, 中文标签, 大致体积说明)
    ("tiny", "tiny（约 75MB，最快，够认唤醒词）", "约 75MB"),
    ("base", "base（约 145MB，日常对话够用）", "约 145MB"),
    ("small", "small（约 480MB，识别更准，推荐）", "约 480MB"),
    ("medium", "medium（约 1.5GB，很准但偏慢）", "约 1.5GB"),
    ("large-v3", "large-v3（约 3.1GB，最准，需要较好显卡）", "约 3.1GB"),
]

_VALID_SIZES = {s for s, _label, _hint in MODEL_SIZES}


def model_label(size: str) -> str:
    for s, label, _hint in MODEL_SIZES:
        if s == size:
            return label
    return size


def repo_id_of(size: str) -> str:
    """faster-whisper 的 CTranslate2 模型仓库名（Systran/faster-whisper-*）。"""
    return f"Systran/faster-whisper-{size}"


# ---------------------------------------------------------------- 下载

def _hf_hub_modules():
    """延迟导入 huggingface_hub（只在真正要下载时才用）。"""
    from huggingface_hub import snapshot_download  # type: ignore

    return snapshot_download


def download_model(
    size: str,
    dest_root: str | Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """把 ``Systran/faster-whisper-<size>`` 下载到 ``dest_root/<size>`` 并返回该目录。

    走国内镜像 ``HF_MIRROR``（只在下载期间设 ``HF_ENDPOINT``，下完还原），
    不需要用户配代理。``on_progress`` 收进度文字（供 UI 状态栏显示）。
    """
    if size not in _VALID_SIZES:
        raise ValueError(f"不支持的模型大小：{size}（可选：{'/'.join(_VALID_SIZES)}）")
    snapshot_download = _hf_hub_modules()
    dest_root = Path(dest_root).expanduser()
    target = dest_root / size
    target.mkdir(parents=True, exist_ok=True)

    # 临时把端点指到镜像；snapshot_download 内部用 SESSION 读环境变量
    old_endpoint = os.environ.get("HF_ENDPOINT")
    os.environ["HF_ENDPOINT"] = HF_MIRROR
    # 关掉软链接警告噪音（Windows 无开发者模式时会刷一屏）
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    def _emit(msg: str) -> None:
        if on_progress is not None:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    _emit(f"正在从国内镜像下载 {size} 模型…（{model_label(size)}）")
    try:
        # allow_patterns 只拉 CTranslate2 需要的文件，避免下 README/.git 之类
        snapshot_download(
            repo_id=repo_id_of(size),
            local_dir=str(target),
            allow_patterns=["*.bin", "*.json", "*.txt", "*.model"],
            max_workers=4,
        )
    finally:
        if old_endpoint is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = old_endpoint

    if cancel is not None and cancel.is_set():
        _emit("下载已取消")
        return str(target)
    _emit(f"下载完成：{target}")
    return str(target)


# ---------------------------------------------------------------- 本地扫描

def _looks_like_model_dir(path: Path) -> bool:
    """CTranslate2 模型目录的标志：有 model.bin 和 config.json。"""
    try:
        return (path / "model.bin").is_file() and (path / "config.json").is_file()
    except OSError:
        return False


def scan_local_models(extra_dirs: list[str] | None = None) -> list[dict[str, Any]]:
    """扫描本机可能存在的 faster-whisper 模型目录，返回 [{size,path,label}]。

    查这些地方：
    1. ``extra_dirs``（用户自填的模型根目录）
    2. ``<用户配置>/companion/whisper-models/*``（程序自己下载的位置）
    3. HuggingFace 缓存 ``~/.cache/huggingface/hub/models--Systran--faster-whisper-*``
    4. 环境变量 ``TEAEGG_WHISPER_DIR``

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
    env_dir = os.environ.get("TEAEGG_WHISPER_DIR", "").strip()
    if env_dir:
        roots.append(Path(env_dir).expanduser())

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

    # HF 缓存
    try:
        cache = Path.home() / ".cache" / "huggingface" / "hub"
        if cache.is_dir():
            for repo in cache.glob("models--Systran--faster-whisper-*"):
                size = repo.name.split("faster-whisper-", 1)[-1]
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
    """从目录名猜模型大小（tiny/base/...）；猜不到就原样返回目录名。"""
    name = path.name.lower()
    for size in sorted(_VALID_SIZES, key=len, reverse=True):
        if size in name:
            return size
    return path.name


# ---------------------------------------------------------------- 运行

_engine_cache: dict[str, Any] = {}
_engine_lock = threading.Lock()


def load_engine(model_path: str, *, device: str = "cpu", compute_type: str = "int8"):
    """加载（并缓存）一个 WhisperModel。同路径只加载一次。

    model_path 可以是本地目录，也可以是 tiny/base 这类 size 名（会自动走
    huggingface_hub 缓存/镜像下载，但正常情况下我们要求传本地目录）。
    """
    key = f"{model_path}|{device}|{compute_type}"
    with _engine_lock:
        if key in _engine_cache:
            return _engine_cache[key]
        from faster_whisper import WhisperModel  # type: ignore

        engine = WhisperModel(model_path, device=device, compute_type=compute_type)
        _engine_cache[key] = engine
        return engine


def unload_engines() -> None:
    with _engine_lock:
        _engine_cache.clear()


def resolve_model_dir(cfg_get: Callable[[str, Any], Any]) -> str:
    """按配置找到可用的模型目录；返回空串表示没有可用模型。

    优先级：用户手填路径 → 程序下载目录 ``<companion>/whisper-models/<size>``
    → 本机扫描到的第一个。
    """
    custom = str(cfg_get("companion_whisper_model_path", "") or "").strip()
    if custom:
        p = Path(custom).expanduser()
        if _looks_like_model_dir(p):
            return str(p)
        # 用户可能填的是父目录，里面有一个模型
        if p.is_dir():
            for child in p.iterdir():
                if child.is_dir() and _looks_like_model_dir(child):
                    return str(child)
    size = str(cfg_get("companion_whisper_model_size", "base") or "base")
    download_root = str(cfg_get("companion_whisper_download_dir", "") or "").strip()
    if download_root:
        cand = Path(download_root).expanduser() / size
        if _looks_like_model_dir(cand):
            return str(cand)
    local = scan_local_models([download_root] if download_root else None)
    if local:
        # 优先匹配配置的大小
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
    initial_prompt: str | None = None,
    vad_filter: bool = True,
    beam_size: int = 1,
) -> str:
    """把 numpy float32 / int16 单声道音频转成文字（纯文本，去空白）。

    ``audio`` 需 16kHz 单声道（numpy float32，范围 -1..1）。
    """
    segs, _info = engine.transcribe(
        audio,
        language=language or "zh",
        beam_size=int(beam_size or 1),
        vad_filter=bool(vad_filter),
        condition_on_previous_text=False,
        initial_prompt=initial_prompt or None,
    )
    return "".join(seg.text for seg in segs).strip()


__all__ = [
    "HF_MIRROR",
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

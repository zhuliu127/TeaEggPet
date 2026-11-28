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
    """延迟导入 huggingface_hub（只在真正要下载时才用）。

    注意：huggingface_hub 在 **import 时** 就把环境变量 ``HF_ENDPOINT`` 读进
    ``constants.ENDPOINT``，之后再改 env 不生效。所以调用方必须在真正 import
    之前把 env 设好（见 download_model），并额外把 ``endpoint=`` 显式传下去双保险。
    """
    from huggingface_hub import snapshot_download  # type: ignore

    return snapshot_download


def _make_progress_tqdm(on_tick):
    """构造一个「静默但可观测」的 tqdm 子类，用于把下载进度回报给 UI。

    windowed（console=False）的 PyInstaller 程序里 ``sys.stderr`` 是 ``None``，
    huggingface_hub 下载时若画进度条会往 stderr 写 →
    ``'NoneType' object has no attribute 'write'`` 直接崩，模型下不下来。
    所以这里始终 ``disable=True``（绝不碰 stderr），但重写 ``update``/``close``
    把 ``(已下载字节, 总字节, 速度字节/秒, 预计剩余秒)`` 交给 ``on_tick`` 回调。

    huggingface_hub 会用它 **每个文件** 各开一个 tqdm；这里把它累计到总量：
    第一次见到的 tqdm 记「基准已下载」，之后按 n 累加，配合一个可变 dict 汇总。
    """
    try:
        from tqdm.auto import tqdm as _tqdm  # type: ignore
    except Exception:  # noqa: BLE001
        _tqdm = None

    # 跨进程内所有文件、跨所有 tqdm 实例共享的累计状态
    agg = {"done": 0.0, "files_total": 0.0, "last": None}

    def _emit(n, total, rate):
        try:
            on_tick(agg, n, total, rate)
        except Exception:  # noqa: BLE001
            pass

    if _tqdm is not None:
        class _ProgTqdm(_tqdm):  # type: ignore[misc]
            def __init__(self, *args, total=None, **kwargs):
                kwargs["disable"] = True   # 绝不碰 stderr
                super().__init__(*args, total=total, **kwargs)
                self._own_total = float(total or 0)
                if self._own_total:
                    agg["files_total"] += self._own_total
                self._prev = 0.0

            def update(self, n=1):  # noqa: D102
                try:
                    r = super().update(n)
                except Exception:  # noqa: BLE001
                    r = None
                # 注意：disable=True 时 tqdm 的 self.n 不会前进，只能自己累加
                d = float(n or 0)
                if d > 0:
                    self._prev += d
                    agg["done"] += d
                _emit(agg["done"], agg["files_total"], getattr(self, "format_dict", {}) or {})
                return r

            def close(self):  # noqa: D102
                try:
                    return super().close()
                finally:
                    _emit(agg["done"], agg["files_total"], {})

        return _ProgTqdm

    # 连 tqdm 都没有：返回一个最简空壳（hub 需要的基础方法都实现）
    class _Null:
        def __init__(self, *a, total=None, **k):
            self.n = 0
            self.total = total
            self.format_dict = {}

        def update(self, *a, **k):
            pass

        def close(self):
            pass

        def set_description(self, *a, **k):
            pass

        def set_description_str(self, *a, **k):
            pass

        def set_postfix(self, *a, **k):
            pass

        def set_postfix_str(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    return _Null


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
    """把 ``Systran/faster-whisper-<size>`` 下载到 ``dest_root/<size>`` 并返回该目录。

    走国内镜像 ``HF_MIRROR``（只在下载期间设 ``HF_ENDPOINT``，下完还原），
    不需要用户配代理。

    ``on_progress`` 现在收一个 **结构化进度字典**（供 UI 画进度条/速度）：
        {"done": 已下载字节, "total": 总字节, "percent": 0-100 或 None,
         "rate": 字节/秒 或 None, "text": 一句人类可读的话}
    为了兼容旧调用（只当状态文字用），字典里也带 ``"msg": 同 text``。
    """
    if size not in _VALID_SIZES:
        raise ValueError(f"不支持的模型大小：{size}（可选：{'/'.join(_VALID_SIZES)}）")
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

    # —— 关键：在 import huggingface_hub 之前就把 env 设好 ——
    # 1) 端点指到国内镜像（constants.ENDPOINT 在 import 时读取）
    # 2) 关掉进度条（windowed 下 sys.stderr 是 None，tqdm 写它会崩）
    # 3) 关软链接警告噪音
    saved_env = {}
    for _k in ("HF_ENDPOINT", "HF_HUB_DISABLE_PROGRESS_BARS", "HF_HUB_DISABLE_SYMLINKS_WARNING",
               "HF_HUB_DISABLE_TELEMETRY"):
        saved_env[_k] = os.environ.get(_k)
    os.environ["HF_ENDPOINT"] = HF_MIRROR
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    _emit(f"正在从国内镜像下载 {size} 模型…（{model_label(size)}）")

    # 进度回报：把 tqdm 的累计状态换算成 百分比/速度/文字，节流到约每 0.15s 一次
    _last_report = [0.0]
    _rate_state = {"t": None, "done": 0.0, "rate": 0.0}

    def _on_tick(agg, n, total, fmt):
        import time as _time

        now = _time.time()
        done = float(agg.get("done") or n or 0)
        tot = float(agg.get("files_total") or total or 0)
        # 自己按时间差算速度（disable=True 时 tqdm 的 format_dict 里没有 rate）
        lt = _rate_state["t"]
        if lt is not None and now > lt:
            dbytes = done - _rate_state["done"]
            inst = dbytes / (now - lt)
            if inst > 0:
                # 指数平滑，避免数字乱跳
                _rate_state["rate"] = inst if _rate_state["rate"] <= 0 else (
                    _rate_state["rate"] * 0.7 + inst * 0.3)
        _rate_state["t"] = now
        _rate_state["done"] = done
        # 节流
        if now - _last_report[0] < 0.15:
            return
        _last_report[0] = now
        if not tot:
            return
        pct = max(0.0, min(100.0, done * 100.0 / tot))
        rate = _rate_state["rate"] or None
        rate_txt = (f" · {_fmt_bytes(rate)}/s" if rate else "")
        text = f"正在下载 {size}：{_fmt_bytes(done)} / {_fmt_bytes(tot)}（{pct:.0f}%{rate_txt}）"
        try:
            on_progress and on_progress({
                "done": done, "total": tot, "percent": pct, "rate": rate,
                "text": text, "msg": text,
            })
        except Exception:  # noqa: BLE001
            pass

    last_exc: Exception | None = None
    try:
        snapshot_download = _hf_hub_modules()
        # 先走国内镜像；万一镜像不可用，再退回官方站兜底（不用用户管）
        for endpoint in (HF_MIRROR, "https://huggingface.co"):
            if _is_cancelled():
                _emit("下载已取消")
                return str(target)
            try:
                # allow_patterns 只拉 CTranslate2 需要的文件，避免下 README/.git 之类
                snapshot_download(
                    repo_id=repo_id_of(size),
                    local_dir=str(target),
                    allow_patterns=["*.bin", "*.json", "*.txt", "*.model"],
                    max_workers=4,
                    endpoint=endpoint,
                    tqdm_class=_make_progress_tqdm(_on_tick),
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("从 %s 下载 %s 失败：%s", endpoint, size, exc)
                continue
    finally:
        for _k, _v in saved_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    if last_exc is not None:
        raise RuntimeError(f"下载 {size} 模型失败（镜像与官方站均不可用）：{last_exc}")

    if cancel is not None and cancel.is_set():
        _emit("下载已取消")
        return str(target)
    _emit(f"下载完成：{target}", percent=100.0)
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
# GPU 可用性探测结果缓存：None=未探过，True/False=已知。
# 一旦探明 CUDA 不可用（缺 cublas/cudart），后续所有加载直接走 CPU，
# 不再反复构造 CUDA 引擎——某些环境下对已存在的 CUDA 引擎再构造另一个模型会挂死。
_cuda_available: "bool | None" = None


def load_engine(model_path: str, *, device: str = "cpu", compute_type: str = "int8"):
    """加载（并缓存）一个 WhisperModel。同路径只加载一次。

    model_path 可以是本地目录，也可以是 tiny/base 这类 size 名（会自动走
    huggingface_hub 缓存/镜像下载，但正常情况下我们要求传本地目录）。

    **设备自动降级**：若用户选了 CUDA（或 compute 要求 GPU）但本机缺
    cublas64_12.dll / cudart 等 CUDA 运行库，构造模型不会立刻报错，而是等到
    第一次 encode 才抛 ``RuntimeError: Library cublas64_12.dll is not found or
    cannot be loaded`` —— 结果就是「每次都转写失败、唤醒词永远认不出」。
    这里在 GPU 加载失败时**自动回退到 CPU(int8)**，保证语音一定能用，绝不让
    一个装不起来的设备把整个语音助手拖死。
    """
    device = (device or "cpu").strip().lower()
    global _cuda_available
    # 已知 CUDA 不可用就别再试，直接 CPU——避免在坏 CUDA 上反复构造导致挂死。
    if device != "cpu" and _cuda_available is False:
        device = "cpu"
    key = f"{model_path}|{device}|{compute_type}"
    with _engine_lock:
        if key in _engine_cache:
            return _engine_cache[key]
        # ctranslate2 在同一进程里对【同一个模型目录】用不同 compute_type 再构造一次
        # 会死锁（实测第二次 load 永久挂起）。所以只要这个路径已经加载过任何引擎，
        # 就直接复用——反正最终都是 CPU/int8，没必要重复加载。
        for _k, _v in _engine_cache.items():
            if _k.rsplit("|", 2)[0] == str(model_path).rstrip("\\/"):
                _engine_cache[key] = _v
                return _v
        from faster_whisper import WhisperModel  # type: ignore

        def _make(dev: str, ctype: str):
            return WhisperModel(model_path, device=dev, compute_type=ctype)

        engine = None
        if device != "cpu":
            try:
                # 先用极短音频跑一次 encode 真正探活：缺 CUDA 库时构造不报、
                # encode 才报，所以必须预跑一次，否则坏引擎会缓存下来。
                cand = _make(device, compute_type)
                _probe_encode(cand)
                engine = cand
                _cuda_available = True
            except Exception as exc:  # noqa: BLE001
                _cuda_available = False
                log.warning(
                    "WhisperModel 以 %s/%s 加载失败，自动回退 CPU/int8：%s",
                    device, compute_type, exc,
                )
                # 显式释放坏的 CUDA 引擎，避免它残留的库句柄干扰后续 CPU 加载
                try:
                    del cand
                except Exception:  # noqa: BLE001
                    pass
                import gc as _gc

                _gc.collect()
                engine = None
                compute_type = "int8"
        if engine is None:
            # CPU 兜底：int8 在 CPU 上足够快（实测 1s 音频 0.01s）
            engine = _make("cpu", "int8")
        _engine_cache[key] = engine
        return engine


def _probe_encode(engine) -> None:
    """用 0.2 秒静音喂一次 encode，触发 CUDA 库缺失等懒加载错误。

    成功返回 None；缺库/设备不可用会抛异常（由 load_engine 捕获回退）。
    """
    try:
        import numpy as _np
    except Exception:  # noqa: BLE001
        return  # numpy 都没有就跳过探活（正常情况下不可能）
    silent = _np.zeros(int(0.2 * 16000), dtype=_np.float32)
    segs, _info = engine.transcribe(
        silent, language="zh", beam_size=1, vad_filter=False,
        condition_on_previous_text=False,
    )
    # 必须真正消费生成器，encode 才会被执行
    for _seg in segs:
        break


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
    no_speech_threshold: float = 0.6,
) -> str:
    """把 numpy float32 / int16 单声道音频转成文字（纯文本，去空白）。

    ``audio`` 需 16kHz 单声道（numpy float32，范围 -1..1）。
    ``no_speech_threshold``：whisper 判定「这段是无语音」的概率阈值，超过就丢弃该段，
    避免在静音/噪声上幻听（如把底噪听成「三星火」）。

    抗「提示词回声」关键点：``condition_on_previous_text=False`` 已关闭上下文续写，
    但 whisper 仍可能在静音段把 ``initial_prompt`` 当正文吐出来——因此调用方应尽量
    不传 initial_prompt，或传很简短的引导。此外用 ``temperature`` 回退链配合
    ``compression_ratio_threshold``，遇到「复读/复述」的高压缩比输出即视为无效。
    """
    def _run(prompt, temp):
        segs, _info = engine.transcribe(
            audio,
            language=language or "zh",
            beam_size=int(beam_size or 1),
            vad_filter=bool(vad_filter),
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            no_speech_threshold=float(no_speech_threshold),
            compression_ratio_threshold=2.4,
            temperature=temp,
        )
        parts = []
        had_speech = False
        for seg in segs:
            try:
                nsp = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
                cr = float(getattr(seg, "compression_ratio", 1.0) or 1.0)
            except Exception:  # noqa: BLE001
                nsp, cr = 0.0, 1.0
            # 丢弃「基本无语音」的段（幻听多来自这种段）。
            if nsp > no_speech_threshold:
                continue
            # 压缩比过高 = 复读/复述型输出（含提示词回声），丢弃。
            if cr > 2.4:
                continue
            if (seg.text or "").strip():
                had_speech = True
                parts.append(seg.text)
        return "".join(parts).strip(), had_speech

    # 第一遍：不传 initial_prompt（最不容易触发回声）。
    text, had_speech = _run(None, 0.0)
    if text:
        return text
    # 第二遍（仅当第一遍有语音活动但没出文本、且提供了 prompt）：带 prompt 用略高温度再试，
    # 让模型走「重采样纠错」路径，减少简单复述。
    if had_speech and initial_prompt:
        text2, _ = _run(initial_prompt, 0.2)
        if text2:
            return text2
    return text


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

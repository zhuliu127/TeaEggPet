# -*- coding: utf-8 -*-
"""本地 ComfyUI + Qwen-Image 2.1（Image Edit）生图通道。

参考实现：wonderandyou/dsh-qwen-paint 的 ``buildWorkflow``（节点与连线照抄，本机已跑通）。

链路：``POST /upload/image`` 上传参考图 → ``POST /prompt`` 提交工作流 →
轮询 ``GET /history/{id}`` → ``GET /view`` 取图。全程只走 127.0.0.1，不出网、不花钱。

图生图（有参考图）的四个要点，少一个就「不按参考图改」：
① 加 ``LoadImage`` 节点读参考图；
② ``TextEncodeQwenImage21`` **必须接 vae**，否则它不会编码参考图；
③ 参考图接 ``images.image_1``…（本机支持到 image_16）；
④ KSampler 的 latent 用该节点的**第 2 号输出**，并**删掉 EmptyLatentImage**
   （所以 ``resolution`` 直接决定参考图缩放与输出尺寸；输出 latent 取第一张参考图的尺寸）。
"""

from __future__ import annotations

import json
import logging
import re
import mimetypes
import random
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

log = logging.getLogger(__name__)

#: ComfyUI 永远在本机。用**空 ProxyHandler** 的 opener：机器上配了 http_proxy 时
#: urllib 会连 127.0.0.1 也走代理，结果报「目标计算机积极拒绝」——踩过一次。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_URL = "http://127.0.0.1:8188"
DEFAULT_UNET = "qwen_image_2.1_int8_convrot.safetensors"
DEFAULT_CLIP = "qwen3vl_8b_int8_convrot.safetensors"
DEFAULT_VAE = "qwen_image_2.1_vae_bf16.safetensors"

#: 默认负面词（去畸形/去水印/去塑料感，取自本机已验证的工作流）
#: 默认负面词。注意别再抄古风工作流那一套（里面有「微笑 / 现代服装 / 现代设备 /
#: 明亮阳光」）—— 我们是「日系插画 + 自拍 + 居家现代场景」，那些词会把想要的东西压掉。
DEFAULT_NEGATIVE = (
    "低质量，模糊，噪点，过曝，欠曝，死白，水印，签名，字幕，乱码文字，界面元素，"
    "多余人物，双人，重复的脸，畸形的手，缺指，多指，肢体变形，人体结构错误，"
    "透视错乱，塑料质感，3D 渲染，照片写实，真人照片，油画厚涂，粗糙线条，"
    # 自拍构图的常见崩坏：手臂被透视拉成几米长、人物被推远成全身像
    "超长手臂，拉长的胳膊，手臂占满画面，手臂比例失调，手臂从画面外一直延伸到身体，"
    "前景手臂，前臂横穿画面，伸向镜头的手臂，袖口出现在画面边缘，"
    "远景，全身像，人物过小，过度广角畸变"
)

#: 目标像素量（决定 resolution 与输出边长；Qwen-Edit 按第一张参考图比例出图）
TARGET_PIXELS = 780_000

#: 节点 schema 缓存（url → (时间戳, 输入键集合)）：/object_info 有几 MB，不能每张图都拉
_SCHEMA_CACHE: dict[str, tuple[float, set[str]]] = {}


class ComfyError(RuntimeError):
    """本地生图失败（消息可直接给用户看）。"""


# ---------------------------------------------------------------- 小工具
def _base(store) -> str:
    return str(store.get("companion_comfy_url", DEFAULT_URL) or DEFAULT_URL).strip().rstrip("/")


# ---------------------------------------------------------------- 本地安装目录
#: 整合包/便携版常见的 python 位置（相对用户填的根目录）
PYTHON_RELATIVE = (
    "python/python.exe",
    "python_embeded/python.exe",
    "venv/Scripts/python.exe",
    ".venv/Scripts/python.exe",
)
#: ComfyUI 里各类模型所在子目录（新版叫 diffusion_models，老版叫 unet）
MODEL_DIRS = {
    "unets": ("diffusion_models", "unet"),
    "clips": ("text_encoders", "clip"),
    "vaes": ("vae",),
}
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf")


def install_paths(dir_value) -> dict[str, Path] | None:
    """把用户填的目录解析成 {root, comfy, python}；解析不出来返回 None。

    两种目录都能填：
    - 整合包根目录（里面有 ``python/`` 和 ``ComfyUI/``）——绘世、官方便携版都是这样；
    - 直接指向 ComfyUI 目录本身（里面有 ``main.py``）。
    """
    raw = str(dir_value or "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.is_dir():
        return None
    comfy = None
    for candidate in (root, root / "ComfyUI"):
        if (candidate / "main.py").is_file():
            comfy = candidate
            break
    if comfy is None:
        return None
    python = None
    for relative in PYTHON_RELATIVE:
        for base in (root, comfy, comfy.parent):
            candidate = base / relative
            if candidate.is_file():
                python = candidate
                break
        if python is not None:
            break
    return {"root": root, "comfy": comfy, "python": python}


#: Qwen-Image 2.1 只能配 qwen3vl_8b（隐藏层 4096）；32B 的 Qwen3-VL 是 5120 维，接上去会在
#: KSampler 里报 "Given normalized_shape=[4096] ... got input of size [1, N, 5120]"。
QWEN_IMAGE_CLIP_HINT = "qwen3vl_8b_*.safetensors"
QWEN_IMAGE_VAE_HINT = "qwen_image_2.1_vae_*.safetensors"


def check_pairing(unet: str, clip: str, vae: str) -> str | None:
    """生成前检查 unet / clip / vae 是不是一套；不匹配就返回人话提示。"""
    u, c, v = (str(x or "").strip().lower() for x in (unet, clip, vae))
    if "qwen_image" not in u:
        return None
    if c and "qwen3vl" in c and "8b" not in c:
        return (f"clip 选的是「{clip}」——它是 Qwen3-VL 32B，输出 5120 维；"
                f"而 {unet} 要 4096 维。把 clip 换成 {QWEN_IMAGE_CLIP_HINT} 就好。")
    if c and "qwen3vl" not in c:
        return f"clip 选的是「{clip}」，Qwen-Image 2.1 需要 {QWEN_IMAGE_CLIP_HINT}。"
    if v and "qwen_image" not in v:
        return f"vae 选的是「{v}」，Qwen-Image 2.1 需要 {QWEN_IMAGE_VAE_HINT}。"
    return None


def local_models(store) -> dict[str, list[str]]:
    """直接扫目录列模型，不依赖 ComfyUI 是否在跑（设置页「扫描本地模型」用）。"""
    paths = install_paths(store.get("companion_comfy_dir", ""))
    if not paths:
        raise ComfyError("先设置 ComfyUI 目录（整合包根目录，或 ComfyUI 文件夹本身）")
    models = paths["comfy"] / "models"
    if not models.is_dir():
        raise ComfyError(f"这个目录下没有 models/：{models}")
    found: dict[str, list[str]] = {"unets": [], "clips": [], "vaes": []}
    for key, folders in MODEL_DIRS.items():
        for folder in folders:
            base = models / folder
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in MODEL_EXTS:
                    continue
                name = path.relative_to(base).as_posix()
                if name not in found[key]:
                    found[key].append(name)
    return found


def _alive(base: str, timeout: float = 3.0) -> bool:
    try:
        _read_json(urllib.request.Request(f"{base}/system_stats", method="GET"), timeout)
        return True
    except Exception:
        return False


def launch(store, on_status: Callable[[str], None] | None = None, wait: float = 150) -> str:
    """用设置里的目录把 ComfyUI 拉起来（独立进程，桌宠退出后它继续跑）。

    日志写到数据目录的 comfyui.log，起不来时让人直接去看那个文件。
    """
    base = _base(store)
    if _alive(base):
        return "ComfyUI 已经在运行了，直接用就行"
    paths = install_paths(store.get("companion_comfy_dir", ""))
    if not paths:
        raise ComfyError("先设置 ComfyUI 目录（整合包根目录，或 ComfyUI 文件夹本身）")
    python = paths.get("python")
    if python is None:
        raise ComfyError("这个目录里没找到 python（支持 python/、python_embeded/、venv/）")
    try:
        port = urlsplit(base).port or 8188
    except ValueError:
        port = 8188
    log_path = Path(store.root) / "comfyui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(python), "-s", str(paths["comfy"] / "main.py"),
           "--windows-standalone-build", "--listen", "127.0.0.1", "--port", str(port)]
    log.info("启动 ComfyUI：%s", " ".join(cmd))
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：桌宠退出后 ComfyUI 不被一起带走
    flags = 0x00000008 | 0x00000200
    with log_path.open("ab") as handle:
        handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n".encode("utf-8"))
        handle.flush()
        proc = subprocess.Popen(cmd, cwd=str(paths["comfy"]), stdout=handle,
                                stderr=subprocess.STDOUT, creationflags=flags,
                                close_fds=True)
    started = time.time()
    while time.time() - started < wait:
        if _alive(base):
            return f"已启动（PID {proc.pid}），日志：{log_path.name}"
        if proc.poll() is not None:
            raise ComfyError(f"进程退出了（code {proc.returncode}），看日志：{log_path}")
        if on_status:
            on_status(f"启动中… {int(time.time() - started)}s")
        time.sleep(2)
    raise ComfyError(f"启动超时（{int(wait)}s），它可能还在加载，过会儿点「检测 ComfyUI」；日志：{log_path}")


def _json_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    return _read_json(req, timeout)


def _read_json(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(600).decode("utf-8", "replace")
        except Exception:
            pass
        raise ComfyError(f"ComfyUI 返回 HTTP {exc.code}：{detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ComfyError(f"连不上 ComfyUI（{_host_of(req.full_url)}）：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ComfyError("ComfyUI 响应超时") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ComfyError("ComfyUI 返回了无法解析的内容") from exc


def _host_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _node_keys(info: dict[str, Any], node: str) -> set[str]:
    inputs = ((info or {}).get(node) or {}).get("input") or {}
    return set((inputs.get("required") or {}).keys()) | set((inputs.get("optional") or {}).keys())


def probe(store) -> dict[str, Any]:
    """探活 + 列模型（设置页「检测 ComfyUI」用）。失败抛 ComfyError。"""
    base = _base(store)
    info = _read_json(urllib.request.Request(f"{base}/object_info", method="GET"), 40)
    stats = _read_json(urllib.request.Request(f"{base}/system_stats", method="GET"), 20)
    def options(node: str, field: str) -> list[str]:
        spec = ((info.get(node) or {}).get("input") or {}).get("required", {}).get(field)
        try:
            return [str(x) for x in spec[0]]
        except Exception:
            return []
    return {
        "url": base,
        "version": str((stats.get("system") or {}).get("comfyui_version", "")),
        "unets": options("UNETLoader", "uname") or options("UNETLoader", "unet_name"),
        "clips": options("CLIPLoader", "clip_name"),
        "vaes": options("VAELoader", "vae_name"),
        "has_qwen_edit": "TextEncodeQwenImage21" in info,
        # 精确画幅能力：有的版本节点直接带 custom_size 字段；官方工作流则用
        # ComfySwitchNode 在「参考图 latent」和「EmptyLatentImage(w,h)」之间切。
        "has_custom_size": "custom_size" in _node_keys(info, "TextEncodeQwenImage21"),
        "has_switch": "ComfySwitchNode" in info,
    }


# ---------------------------------------------------------------- 上传参考图
def _pad_to_aspect(path: Path, aspect: str) -> Path | None:
    """把第一张参考图补边到目标画幅，返回临时文件（不需要补时返回 None）。

    Qwen-Edit 的 latent 尺寸 = 第一张参考图的尺寸，不补边就只能拿到人物图的比例
    （实测 836×1400 → 672×1152）。补白边比裁掉人物安全。
    """
    try:
        from PIL import Image

        want_w, want_h = (float(x) for x in str(aspect or "3:4").replace("x", ":").split(":", 1))
        if want_w <= 0 or want_h <= 0:
            return None
        target = want_w / want_h
        img = Image.open(path)
        cur = img.width / img.height
        if abs(cur - target) / target <= 0.06:
            return None
        if target > cur:
            new_w, new_h = int(round(img.height * target)), img.height
        else:
            new_w, new_h = img.width, int(round(img.width / target))
        canvas = Image.new("RGB", (new_w, new_h), (255, 255, 255))
        canvas.paste(img.convert("RGB"), ((new_w - img.width) // 2, (new_h - img.height) // 2))
        tmp = Path(tempfile.gettempdir()) / f"petdesk-ref-{int(time.time()*1000)}.png"
        canvas.save(tmp)
        return tmp
    except Exception:
        log.debug("补边参考图失败（按原图上传）", exc_info=True)
        return None
def _upload(base: str, path: Path, timeout: float = 120.0) -> str:
    boundary = f"----petdesk{random.randint(10**9, 10**10)}"
    name = f"petdesk_{int(time.time() * 1000)}_{random.randint(1000, 9999)}{path.suffix.lower() or '.png'}"
    ctype = mimetypes.guess_type(str(path))[0] or "image/png"
    data = path.read_bytes()
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{base}/upload/image", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    payload = _read_json(req, timeout)
    got = str(payload.get("name") or "")
    if not got:
        raise ComfyError(f"上传参考图失败：{str(payload)[:200]}")
    sub = str(payload.get("subfolder") or "")
    return f"{sub}/{got}" if sub else got


# ---------------------------------------------------------------- 工作流
def _size_for(aspect: str, pixels: int = TARGET_PIXELS) -> tuple[int, int]:
    """按画幅算一个接近 TARGET_PIXELS 的宽高（32 的倍数）。"""
    a = str(aspect or "3:4").strip()
    try:
        if ":" in a:
            w_ratio, h_ratio = (float(x) for x in a.split(":", 1))
        elif "x" in a.lower():
            w_ratio, h_ratio = (float(x) for x in a.lower().split("x", 1))
        else:
            w_ratio, h_ratio = 3.0, 4.0
    except ValueError:
        w_ratio, h_ratio = 3.0, 4.0
    if w_ratio <= 0 or h_ratio <= 0:
        w_ratio, h_ratio = 3.0, 4.0
    ratio = w_ratio / h_ratio
    pixels = max(120_000, min(4_000_000, int(pixels or TARGET_PIXELS)))
    width = round((pixels * ratio) ** 0.5 / 32) * 32
    height = round((pixels / ratio) ** 0.5 / 32) * 32
    return max(256, width), max(256, height)


def node_inputs(base: str, node: str = "TextEncodeQwenImage21", ttl: float = 600.0) -> set[str]:
    """这个 ComfyUI 上该节点暴露了哪些输入（带缓存）。

    新版本给 Image Edit 节点加了 ``custom_size``：勾上之后可以用 width/height 直接定
    画幅；老版本只有 ``resolution``（按面积缩放参考图）。两条路都要能走。
    """
    now = time.time()
    cached = _SCHEMA_CACHE.get(base)
    if cached and now - cached[0] < ttl:
        return cached[1]
    keys: set[str] = set()
    try:
        info = _read_json(urllib.request.Request(f"{base}/object_info/{node}", method="GET"), 30)
        spec = info.get(node) or next(iter(info.values()), {})
        inputs = spec.get("input") or {}
        keys = set((inputs.get("required") or {}).keys()) | set((inputs.get("optional") or {}).keys())
    except Exception:
        log.debug("取节点 schema 失败（按老版本处理）", exc_info=True)
    _SCHEMA_CACHE[base] = (now, keys)
    return keys


def build_workflow(*, prompt: str, negative: str, width: int, height: int, steps: int,
                   seed: int, unet: str, clip: str, vae: str, references: list[str],
                   inputs: set[str] | None = None, exact_size: bool = False) -> dict[str, Any]:
    """组装 Qwen-Image 2.1 工作流（有参考图 = 图生图，没有 = 文生图）。

    ``exact_size`` 对应官方工作流里的 custom_size / ComfySwitchNode：
    - True  → latent 用 EmptyLatentImage(width, height)，画幅精确到像素；
    - False → latent 用 TextEncode 的输出，尺寸等于编码后的参考图尺寸
      （所以这时要把首图补边到目标画幅，否则出来的是人物图的比例）。
    """
    with_ref = bool(references)
    resolution = int(round((width * height) ** 0.5))
    flow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "prompt": prompt,
                "negative_prompt": negative,
                "resolution": resolution,
            },
        },
        "7": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "petdesk"}},
    }
    if inputs is None:
        inputs = {"resolution"}
    if "custom_size" in inputs:
        # 新版节点：直接给宽高，画幅精确到像素（不再靠 resolution 反推）
        flow["5"]["inputs"]["custom_size"] = True
        flow["5"]["inputs"]["width"] = int(width)
        flow["5"]["inputs"]["height"] = int(height)
    if with_ref:
        for index, ref_name in enumerate(references[:16], start=1):
            node_id = str(100 + index)
            flow[node_id] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
            flow["5"]["inputs"][f"images.image_{index}"] = [node_id, 0]
        flow["5"]["inputs"]["vae"] = ["3", 0]
        latent = ["5", 2]
    else:
        latent = None
    if exact_size or not with_ref:
        flow["6"] = {"class_type": "EmptyLatentImage",
                     "inputs": {"width": int(width), "height": int(height), "batch_size": 1}}
        latent = ["6", 0]
    flow["8"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["7", 0],
            "seed": int(seed),
            "steps": max(1, min(60, int(steps))),
            "cfg": 1.0,
            "sampler_name": "euler",
            "scheduler": "simple",
            "positive": ["5", 0],
            "negative": ["5", 1],
            "latent_image": latent,
            "denoise": 1.0,
        },
    }
    return flow


def _wait(base: str, prompt_id: str, timeout: float,
          on_status: Callable[[str], None] | None) -> dict[str, Any]:
    started = time.time()
    last_note = 0.0
    while time.time() - started < timeout:
        hist = _read_json(urllib.request.Request(f"{base}/history/{prompt_id}", method="GET"), 30)
        entry = hist.get(prompt_id) if isinstance(hist, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            if str(status.get("status_str") or "") == "error":
                raise ComfyError(_error_text(status))
            outputs = entry.get("outputs") or {}
            if outputs:
                return outputs
        if on_status and time.time() - last_note > 3:
            last_note = time.time()
            done = _progress(base, prompt_id)
            on_status(f"本地生成中 {done}" if done else "本地生成中…")
        time.sleep(1.0)
    raise ComfyError(f"等了 {int(timeout)} 秒还没出图（本地模型偏慢，可在设置里调大超时）")


def _progress(base: str, prompt_id: str) -> str:
    try:
        queue = _read_json(urllib.request.Request(f"{base}/queue", method="GET"), 10)
    except Exception:
        return ""
    running = queue.get("queue_running") or []
    for item in running:
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
            return "…"
    return ""


def _error_text(status: dict[str, Any]) -> str:
    for message in status.get("messages") or []:
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        if message[0] in ("execution_error", "execution_interrupted"):
            body = message[1] if isinstance(message[1], dict) else {}
            node = str(body.get("node_type") or body.get("node_id") or "")
            text = str(body.get("exception_message") or "").strip().splitlines()
            detail = text[0] if text else "未知错误"
            friendly = _translate_error(detail)
            if friendly:
                return friendly
            return f"ComfyUI 执行出错{'（' + node + '）' if node else ''}：{detail[:200]}"
    return "ComfyUI 执行出错（没有拿到具体原因）"


#: 维度不匹配的典型报错：
#: Given normalized_shape=[4096], expected input with shape [*4096], but got input of size[1, 2984, 5120]
_SHAPE_RE = re.compile(
    r"normalized_shape=\[(\d+)\].*?size\s*\[\s*1\s*,\s*\d+\s*,\s*(\d+)\s*\]", re.S)


def _translate_error(detail: str) -> str | None:
    """把 ComfyUI 的底层报错翻译成「你该改哪个设置」。"""
    text = str(detail or "")
    match = _SHAPE_RE.search(text)
    if match:
        want, got = match.group(1), match.group(2)
        return (f"模型配错了：clip 输出 {got} 维，但 unet 要 {want} 维。\n"
                f"Qwen-Image 2.1 必须配 {QWEN_IMAGE_CLIP_HINT}（4096 维）；"
                "Qwen3-VL 32B 是 5120 维，接上去就会报这个错。\n"
                "改法：陪伴扩展设置 → 生图接口 → 本地模型 → clip，换成 qwen3vl_8b_*.safetensors。")
    if "out of memory" in text.lower() or "allocation" in text.lower():
        return ("显存不够了。把「像素档」调到 0.5MP、steps 降到 20，"
                "或把 unet/clip 换成 int8_convrot 版本。")
    if "not a valid model" in text.lower() or "safetensors" in text.lower() and "error" in text.lower():
        return f"模型文件读不了或不是这个节点的格式：{text[:160]}"
    return None


def _download(base: str, image: dict[str, Any]) -> bytes:
    query = urllib.parse.urlencode({
        "filename": image.get("filename", ""),
        "subfolder": image.get("subfolder", ""),
        "type": image.get("type", "output"),
    })
    try:
        with _OPENER.open(f"{base}/view?{query}", timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise ComfyError(f"取图失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ComfyError(f"取图失败：{exc.reason}") from exc


# ---------------------------------------------------------------- 入口
def generate(store, prompt: str, refs: list[Path], aspect: str,
             on_status: Callable[[str], None] | None = None) -> tuple[bytes, dict[str, Any]]:
    base = _base(store)
    unet = str(store.get("companion_comfy_unet", DEFAULT_UNET) or DEFAULT_UNET)
    clip = str(store.get("companion_comfy_clip", DEFAULT_CLIP) or DEFAULT_CLIP)
    vae = str(store.get("companion_comfy_vae", DEFAULT_VAE) or DEFAULT_VAE)
    # 自定义负面词是「追加」而不是「替换」：用户常常只写两三个词（比如「2个人」），
    # 整段顶掉内置那套反而更容易出手指畸形和乱码文字。
    extra_negative = str(store.get("companion_comfy_negative", "") or "").strip()
    negative = f"{DEFAULT_NEGATIVE}，{extra_negative}" if extra_negative else DEFAULT_NEGATIVE
    mismatch = check_pairing(unet, clip, vae)
    if mismatch:
        raise ComfyError(mismatch)
    try:
        steps = int(store.get("companion_comfy_steps", 50) or 50)
    except (TypeError, ValueError):
        steps = 50
    try:
        seed_cfg = int(store.get("companion_comfy_seed", -1) or -1)
    except (TypeError, ValueError):
        seed_cfg = -1
    seed = random.randint(1, 2**31 - 1) if seed_cfg < 0 else seed_cfg
    try:
        timeout = max(30, int(store.get("companion_comfy_timeout", 600) or 600))
    except (TypeError, ValueError):
        timeout = 600

    try:
        pixels = int(store.get("companion_comfy_pixels", TARGET_PIXELS) or TARGET_PIXELS)
    except (TypeError, ValueError):
        pixels = TARGET_PIXELS
    width, height = _size_for(aspect, pixels)
    schema = node_inputs(base)
    # 精确画幅的三条路，优先级从上到下：
    #   1) 节点自带 custom_size 字段（部分版本）→ 直接给它 width/height
    #   2) 官方工作流用 ComfySwitchNode 切到 EmptyLatentImage(width,height)（本机就是这样）
    #   3) 只能喂 resolution → latent 尺寸 = 编码后的参考图尺寸，得先把首图补边到目标画幅
    has_custom = "custom_size" in schema
    want_exact = bool(store.get("companion_comfy_exact_size", True))
    exact = has_custom or want_exact
    padded = None if exact else (_pad_to_aspect(Path(refs[0]), aspect) if refs else None)
    if padded is not None:
        refs = [padded] + [Path(x) for x in refs[1:]]
        log.info("首张参考图补边到 %s（%s）", aspect, padded.name)
    log.info("本地出图：目标 %s×%s，精确尺寸=%s（schema custom_size=%s，设置=%s）",
             width, height, exact, has_custom, want_exact)
    try:
        max_refs = max(1, min(16, int(store.get("companion_comfy_max_refs", 4) or 4)))
    except (TypeError, ValueError):
        max_refs = 4
    refs = list(refs)[:max_refs]
    uploaded: list[str] = []
    for path in refs:
        if on_status:
            on_status(f"上传参考图 {len(uploaded) + 1}/{len(refs)}…")
        uploaded.append(_upload(base, Path(path)))

    flow = build_workflow(prompt=prompt, negative=negative, width=width, height=height,
                          steps=steps, seed=seed, unet=unet, clip=clip, vae=vae,
                          references=uploaded, inputs=schema, exact_size=exact)
    if on_status:
        on_status("提交给本地 ComfyUI…")
    payload = _json_post(f"{base}/prompt",
                         {"prompt": flow, "client_id": f"petdesk-{random.randint(1000, 9999)}"}, 120)
    if payload.get("error"):
        err = payload["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise ComfyError(f"ComfyUI 拒绝了这次任务：{str(msg)[:200]}")
    prompt_id = payload.get("prompt_id")
    if not isinstance(prompt_id, str):
        raise ComfyError("ComfyUI 没有返回 prompt_id")

    outputs = _wait(base, prompt_id, timeout, on_status)
    for node_output in outputs.values():
        for image in (node_output or {}).get("images") or []:
            if image.get("type", "output") == "output":
                data = _download(base, image)
                return data, {
                    "channel": "comfyui",
                    "model": f"{unet}",
                    "seed": seed,
                    "size": f"{width}x{height}",
                    "exact_size": exact,
                    "steps": steps,
                    "prompt_id": prompt_id,
                    "refs_sent": len(uploaded),
                }
    raise ComfyError("ComfyUI 跑完了但没有输出图片")

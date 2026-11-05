# -*- coding: utf-8 -*-
"""陪伴扩展的本地存储与配置默认值。

数据全部在 ``<配置目录>/companion/`` 下：
``schedule/days/YYYY-MM-DD.json``、``outputs/YYYY-MM/*.png``、
``presets.json``、``image-library/{characters,outfits,scenes,props}``。
API Key 走宿主的 SecretStore（keyring），拿不到时退回 config 明文。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IMAGE_PROVIDERS = ("65535", "openai", "comfyui")

DEFAULTS: dict[str, Any] = {
    # ---- 生图接口 ----
    "companion_image_provider": "65535",  # 65535 | openai | comfyui
    "companion_image_base_url": "",
    "companion_image_api_key": "",
    "companion_image_model": "",
    "companion_image_aspect": "3:4",
    "companion_image_timeout": 240,
    "companion_style_prompt": "日系插画，柔和光感，精致细节",
    "companion_max_refs": 8,
    "companion_identity_only": True,
    "companion_follow_schedule": True,
    "companion_lib_characters": "",
    "companion_lib_outfits": "",
    "companion_lib_scenes": "",
    "companion_lib_props": "",
    # ---- 本地 ComfyUI（Qwen-Image 2.1 图生图）----
    "companion_comfy_url": "http://127.0.0.1:8188",
    "companion_comfy_dir": "",   # ComfyUI 安装目录（整合包根目录，或 ComfyUI 文件夹本身）
    "companion_comfy_unet": "qwen_image_2.1_int8_convrot.safetensors",
    "companion_comfy_clip": "qwen3vl_8b_int8_convrot.safetensors",
    "companion_comfy_vae": "qwen_image_2.1_vae_bf16.safetensors",
    "companion_comfy_steps": 30,
    "companion_comfy_seed": -1,
    "companion_comfy_negative": "",
    "companion_comfy_timeout": 900,
    "companion_comfy_max_refs": 4,
    "companion_comfy_pixels": 780000,   # 目标像素量（约等于画质档位）
    # True = 用 EmptyLatentImage(width,height) 精确出图（等价官方 custom_size）；
    # False = 用编码后参考图尺寸（要把首图补边到目标画幅）
    "companion_comfy_exact_size": True,
    "companion_comfy_use_outfit_ref": True,  # 关掉 = 服饰只用文字描述（避免模特图串人）
    "companion_qwen_enhance": True,          # 本地通道先用千问编辑模板重写提示词
    "companion_qwen_selfie_arm": False,      # True = 自拍手臂入镜（会显胳膊长，默认不露）
    "companion_qwen_prompt_template": "",    # 留空 = 用内置模板副本
    # ---- 每日日程 ----
    "companion_schedule_enabled": True,
    "companion_character_name": "茶叶蛋",
    "companion_partner_name": "",
    "companion_persona": "",
    "companion_extra_rules": "",
    "companion_generate_at": "22:00",
    "companion_persona_prompt": True,
    # ---- 溜达 ----
    "companion_walk_duration_mode": "schedule",  # schedule | fixed
    "companion_walk_fixed_minutes": 10,
    "companion_walk_min_minutes": 5,
    "companion_walk_max_minutes": 45,
    "companion_walk_message_every_min": 4,
    "companion_walk_auto_selfie": True,
    "companion_walk_random_after": 0,  # >0 表示每 N 分钟自己出门一次，0=关闭
    # ---- 主动对话 ----
    "companion_proactive_enabled": True,
    "companion_proactive_first_minutes": 5,    # 启动后多久可能开口
    "companion_proactive_min_minutes": 45,     # 两次之间最短
    "companion_proactive_max_minutes": 120,    # 两次之间最长
    "companion_proactive_quiet_hours": "0-8",  # 免打扰，支持 23:30-7:00
    "companion_proactive_max_unanswered": 3,   # 连着几条没回就停，0=不限
    "companion_proactive_daily_limit": 8,      # 每天最多几条，0=不限
    "companion_proactive_prompt": "",          # 留空用内置提示词
    "companion_proactive_use_memory": True,    # 注入长期记忆（她记得的用户信息）
    "companion_proactive_history_count": 12,   # 参考最近几条对话，0=不参考
    "companion_schedule_autofill": True,       # 启动时今天没日程就自动生成
    # ---- 语音（本地 GPT-SoVITS）----
    "companion_tts_enabled": False,
    "companion_tts_read_proactive": True,      # 主动消息念出来
    "companion_tts_read_chat": True,           # AI 对话回复念出来（全部对话入口）
    "companion_tts_read_screen": True,         # 识屏问答念出来
    "companion_tts_read_self_talk": True,      # 点击台词念出来
    "companion_tts_read_walk": True,           # 溜达播报念出来（出门 / 途中 / 回家）
    "companion_tts_api": "http://127.0.0.1:9880",
    "companion_tts_dir": "D:/AI/AI Voice",
    "companion_tts_gpt": "",                   # 留空 = 用服务当前加载的
    "companion_tts_sovits": "",
    "companion_tts_ref_audio": "",
    "companion_tts_ref_text": "",
    "companion_tts_text_lang": "zh",
    "companion_tts_prompt_lang": "zh",
    "companion_tts_speed": 1.0,
}


class CompanionStore:
    """配置 + 数据目录的小门面（所有模块只从这里拿路径和配置）。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.root = Path(cfg.dir) / "companion"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("创建陪伴扩展数据目录失败")

    # ---------------------------------------------------------------- 配置
    def get(self, key: str, default: Any = None) -> Any:
        if key in DEFAULTS:
            fallback = DEFAULTS[key]
        else:
            fallback = default
        try:
            return self.cfg.get(key, fallback)
        except Exception:
            return fallback

    def set(self, key: str, value: Any) -> None:
        try:
            self.cfg.set(key, value)
        except Exception:
            log.exception("写入配置失败：%s", key)

    def save(self) -> None:
        try:
            self.cfg.save()
        except Exception:
            log.exception("保存配置失败")

    def apply(self, patches: dict[str, Any]) -> None:
        for key, value in patches.items():
            self.set(key, value)
        self.save()

    # ---------------------------------------------------------------- 路径
    def dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def days_dir(self) -> Path:
        return self.dir("schedule") / "days"

    def outputs_dir(self) -> Path:
        return self.dir("outputs")

    def presets_path(self) -> Path:
        return self.root / "presets.json"

    def library_dir(self, category: str) -> Path:
        key = f"companion_lib_{category}"
        custom = str(self.get(key, "") or "").strip()
        if custom:
            path = Path(custom).expanduser()
        else:
            path = self.root / "image-library" / category
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------------------------------------------------------------- IO
    def read_json(self, path: Path, fallback: Any = None) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return fallback

    def write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- API Key
    def image_api_key(self) -> str:
        raw = str(self.get("companion_image_api_key", "") or "").strip()
        if raw:
            return raw
        try:
            from ..chat.models import SecretStore

            return str(SecretStore().get("companion/image") or "")
        except Exception:
            return ""

    def set_image_api_key(self, value: str) -> None:
        value = str(value or "").strip()
        self.set("companion_image_api_key", value)
        self.save()
        try:
            from ..chat.models import SecretStore

            if value:
                SecretStore().set("companion/image", value)
        except Exception:
            log.debug("SecretStore 不可用，生图 Key 只存在 config 里", exc_info=True)

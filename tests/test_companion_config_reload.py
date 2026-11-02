# -*- coding: utf-8 -*-
"""陪伴扩展的 companion_* 配置必须能跨重启读到。

背景（真机 bug）：reload 只搬白名单里的键，宿主不认识 companion_*，于是每重启一次
「生图接口地址 / 模型 / 角色名 / 素材库目录」就全部丢回默认值——设置页显示成空的，
发自拍报「还没填生图接口地址」。这里锁住「按前缀搬回来」的行为。
"""
import json
import pathlib

from pet.config import Config

APP_DIR = "dsh-pet-standalone"


def _write(tmp_path: pathlib.Path, payload: dict) -> None:
    folder = tmp_path / APP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_companion_keys_survive_restart(tmp_path):
    _write(tmp_path, {
        "companion_image_base_url": "https://api.example.com",
        "companion_image_model": "img-1",
        "companion_character_name": "小鱼",
        "companion_lib_scenes": "D:/lib/scenes",
        "hotkey_selfie_now": "Ctrl+Shift+S",
    })
    cfg = Config(tmp_path)
    assert cfg.get("companion_image_base_url") == "https://api.example.com"
    assert cfg.get("companion_image_model") == "img-1"
    assert cfg.get("companion_character_name") == "小鱼"
    assert cfg.get("companion_lib_scenes") == "D:/lib/scenes"
    assert cfg.get("hotkey_selfie_now") == "Ctrl+Shift+S"


def test_companion_keys_update_on_reload(tmp_path):
    _write(tmp_path, {"companion_image_base_url": "https://old.example.com"})
    cfg = Config(tmp_path)
    assert cfg.get("companion_image_base_url") == "https://old.example.com"

    _write(tmp_path, {"companion_image_base_url": "https://new.example.com"})
    cfg.reload()
    assert cfg.get("companion_image_base_url") == "https://new.example.com"


def test_companion_null_does_not_wipe_memory(tmp_path):
    _write(tmp_path, {"companion_character_name": "小鱼"})
    cfg = Config(tmp_path)
    assert cfg.get("companion_character_name") == "小鱼"

    _write(tmp_path, {"companion_character_name": None})
    cfg.reload()
    # 显式 null 视为「撤成默认」：不清内存现值（与本文件其它键的删除语义一致）
    assert cfg.get("companion_character_name") == "小鱼"

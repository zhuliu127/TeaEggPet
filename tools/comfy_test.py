# -*- coding: utf-8 -*-
"""真机自测：本地 ComfyUI + Qwen-Image 2.1 多图参考生图（会真的跑一次，本地免费）。

用法：``python tools/comfy_test.py``（ComfyUI 需在 127.0.0.1:8188）
"""
import pathlib
import shutil
import sys
import time


def main() -> int:
    from pet.config import Config
    from pet.companion.store import CompanionStore
    from pet.companion import comfy, compose, library

    store = CompanionStore(Config())
    lib = library.scan_all(store)
    print("素材库：", {k: len(v) for k, v in lib.items()})

    composed = compose.compose({
        "prompt": "晚上在客厅沙发上打游戏，一手握手柄一手举手机自拍，屏幕的光照在脸上",
        "characters": ["小鱼"],
        "outfits": ["居家毛衣"],
        "scene": "客厅",
        "props": ["游戏手柄"],
        "situation": "在家休息",
        "expression": "放松",
        "pose": "盘腿坐着",
        "shot": "自拍",
        "light": "屏幕冷光",
        "aspect": "3:4",
    }, store)
    refs = [pathlib.Path(r["path"]) for r in composed["refs"]]
    print("参考图：", [(r["category"], r["entry"], pathlib.Path(r["path"]).stat().st_size // 1024) for r in composed["refs"]])
    print("提示词长度：", len(composed["final_prompt"]))

    started = time.time()
    try:
        data, meta = comfy.generate(store, composed["final_prompt"], refs, "3:4",
                                    on_status=lambda s: print("  …", s, flush=True))
    except Exception as exc:  # noqa: BLE001
        print("FAIL:", type(exc).__name__, exc)
        return 1
    out = pathlib.Path("build/comfy_test.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"用时 {time.time() - started:.1f}s | 字节 {len(data)} | meta {meta}")
    print("落盘：", out.resolve())
    from PIL import Image

    print("尺寸：", Image.open(out).size)
    return 0


if __name__ == "__main__":
    sys.exit(main())

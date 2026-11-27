# -*- coding: utf-8 -*-
"""用一张立绘替换程序图标 assets/icon.ico + assets/icon.png。

用法：
    python tools/build_icon.py <源图片路径>

ICO 档位与帧顺序对齐 Windows 标准（256 在前，资源管理器预览才显示大图），
覆盖 16~256 全部 DPI 档。生成后 pet/app.py 启动时经
pet/application_icon.load_application_icon() 设为全局应用图标。

本脚本自带 ICO 写出逻辑（不依赖 scripts/make_icon.py，仓库里 scripts/ 不稳），
但 ICON_SIZES 与那份保持一致。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
# Windows 标准档位（与 scripts/make_icon.py 的 ICON_SIZES 完全一致）
ICON_SIZES = (256, 128, 96, 64, 48, 40, 32, 24, 20, 16)


def _save_ico_ordered(path: Path, source_img, sizes) -> None:
    """按指定顺序写出 ICO（首帧=最大尺寸），32bpp BMP-in-ICO。"""
    from PIL import Image

    blobs = []
    entries = []
    for size in sizes:
        frame = source_img.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
        w = h = int(size)
        header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
        pixels = bytearray()
        for y in range(h - 1, -1, -1):
            for x in range(w):
                r, g, b, a = frame.getpixel((x, y))
                pixels += bytes((b, g, r, a))
        row_and = ((w + 31) // 32) * 4
        blob = header + bytes(pixels) + bytes(row_and * h)
        blobs.append(blob)
        entries.append((w if w < 256 else 0, h if h < 256 else 0, 0, 0, 1, 32, len(blob)))

    count = len(entries)
    offset = 6 + 16 * count
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, count))
        for (w, h, cc, res, planes, bpp, size), blob in zip(entries, blobs):
            f.write(struct.pack("<BBBBHHII", w, h, cc, res, planes, bpp, size, offset))
            offset += size
        for blob in blobs:
            f.write(blob)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python tools/build_icon.py <源图片路径>")
        return 2
    src = Path(argv[1])
    if not src.is_file():
        print(f"源文件不存在: {src}")
        return 2
    from PIL import Image

    im = Image.open(src).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    canvas.alpha_composite(im.resize((256, 256), Image.Resampling.LANCZOS))

    ASSETS.mkdir(parents=True, exist_ok=True)
    _save_ico_ordered(ASSETS / "icon.ico", canvas, ICON_SIZES)
    canvas.save(ASSETS / "icon.png")
    print(f"已生成: {ASSETS / 'icon.ico'}（尺寸 {ICON_SIZES}）和 {ASSETS / 'icon.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

# -*- coding: utf-8 -*-
"""开发自测：造一点素材库 + 日程，把「日程 × 素材」联动表和照片卡截图下来。"""
import json
import os
import pathlib
import shutil
import sys

from pet.qt_plugin_sanitizer import sanitize_plugin_path

sanitize_plugin_path()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

OUT = pathlib.Path(os.environ.get("PET_SHOT_DIR", "shots_ui"))
ROOT = pathlib.Path("build/uitest")


class FakeCfg:
    def __init__(self, base: pathlib.Path) -> None:
        self.dir = base
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        (self.dir / "config.json").write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def _png(path: pathlib.Path, color, size=(240, 320)) -> None:
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, size[0] - 20, size[1] - 20], outline=(255, 255, 255), width=4)
    img.save(path)


def seed(base: pathlib.Path) -> None:
    lib = base / "companion" / "image-library"
    colors = {
        "characters": ((120, 170, 230), ["深深"]),
        "outfits": ((200, 130, 150), ["通勤套装", "家常睡衣"]),
        "scenes": ((150, 200, 160), ["公司", "卧室"]),
        "props": ((220, 200, 130), ["咖啡杯"]),
    }
    for category, (color, names) in colors.items():
        for index, name in enumerate(names):
            folder = lib / category / name
            _png(folder / "ref-01.png", tuple(min(255, c + index * 8) for c in color))
            meta = {"name": name, "aliases": [], "tags": [], "description": f"{name}的外观描述"}
            if category == "scenes":
                meta["defaultOutfit"] = "家常睡衣" if "卧室" in name else "通勤套装"
            if category == "characters":
                meta["defaultOutfit"] = "家常睡衣"
                meta["situationOutfits"] = {"工作学习": "通勤套装", "睡前": "家常睡衣"}
            (folder / "entry.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    from pet.companion.store import CompanionStore
    from pet.companion import library, schedule
    from pet.companion.settings_dialog import CompanionSettingsDialog
    from pet.companion.photo import PhotoWindow

    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    seed(ROOT)
    cfg = FakeCfg(ROOT)
    cfg.set("companion_character_name", "深深")
    cfg.set("companion_persona", "住在电脑里的蓝鲸女孩，白天写代码，话不多。")
    store = CompanionStore(cfg)
    library.ensure_dirs(store)

    day_type = {"id": "workday", "name": "上班日", "guide": ""}
    items = schedule.link_items(schedule.template_items(day_type), store, fill=True)
    schedule.write_day(store, {
        "date": schedule.date_key(), "dayType": {"id": "workday", "name": "上班日"},
        "title": "赶版本的一天", "mood": "有点累但踏实", "items": items, "source": "template",
    })

    app = QApplication.instance() or QApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)

    dialog = CompanionSettingsDialog(cfg)
    dialog.resize(900, 760)
    dialog.show()
    app.processEvents()
    dialog.select_page("schedule")
    for _ in range(3):
        app.processEvents()
    dialog.grab().save(str(OUT / "link-schedule.png"))

    # 联动表单独截一张
    dialog.panel.grab().save(str(OUT / "link-table.png"))
    dialog.panel.relink()
    app.processEvents()
    dialog.panel.grab().save(str(OUT / "link-table-relinked.png"))
    dialog.panel.save()
    saved = schedule.read_day(store, schedule.date_key())
    print("saved items:", len(saved["items"]),
          "| scene:", sum(1 for i in saved["items"] if i.get("scene")),
          "| outfit:", sum(1 for i in saved["items"] if i.get("outfit") or i.get("outfitNote")),
          flush=True)
    dialog.close()

    from pet.companion.library_dialog import LibraryDialog

    lib = LibraryDialog(cfg)
    lib.show()
    app.processEvents()
    tabs = lib.findChild(__import__("PySide6.QtWidgets", fromlist=["QTabWidget"]).QTabWidget)
    tabs.setCurrentIndex(2)  # 场景
    app.processEvents()
    lib.grab().save(str(OUT / "library-scenes.png"))
    lib.close()

    shot = ROOT / "companion" / "image-library" / "characters" / "深深" / "ref-01.png"
    photo = PhotoWindow(shot, "外面风好大～", sub="日程 · 12:00–13:00 吃午饭 @ 公司附近",
                        assets="人物 深深 · 服饰 通勤套装 · 场景 公司工位",
                        on_retake=lambda: None, on_library=lambda: None)
    photo.show()
    app.processEvents()
    photo.grab().save(str(OUT / "photo-card.png"))
    photo.close()
    print("saved to", OUT, flush=True)
    QTimer.singleShot(0, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""开发自测：日程里写的场景/穿搭，会不会真的进到自拍的参考图里。

模型故意返回不带 scene/outfits 的计划，看程序是否按日程补上。
"""
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path("build/linktest")
PLAN = {
    "caption": "风好大，头发都飞起来啦～",
    "monologue": "我回来啦，给你看照片！",
    "prompt": "午后的工位，她一手举着手机自拍，一手端着咖啡杯，身后是亮着的显示器",
    "situation": "",
    "expression": "打哈欠",
    "pose": "坐着",
    "shot": "自拍",
    "light": "窗光",
    "characters": [],
    "outfits": [],
    "scene": "",
    "props": [],
    "aspect": "3:4",
}
SCHEDULE = {
    "title": "赶版本的一天",
    "mood": "有点累但踏实",
    "items": [
        {"start": "00:00", "end": "09:00", "activity": "睡觉", "location": "家", "detail": ""},
        {"start": "09:00", "end": "12:30", "activity": "上班", "location": "公司", "detail": "在改一个卡了两天的 bug"},
        {"start": "12:30", "end": "13:30", "activity": "吃午饭", "location": "公司附近", "detail": ""},
        {"start": "13:30", "end": "18:00", "activity": "上班", "location": "公司", "detail": ""},
        {"start": "18:00", "end": "24:00", "activity": "在家休息", "location": "家", "detail": ""},
    ],
}


class FakeCfg:
    def __init__(self, base: pathlib.Path) -> None:
        self.dir = base
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


def _png(path: pathlib.Path, color) -> None:
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (240, 320), color)
    ImageDraw.Draw(img).rectangle([20, 20, 220, 300], outline=(255, 255, 255), width=4)
    img.save(path)


def seed(base: pathlib.Path) -> None:
    lib = base / "companion" / "image-library"
    entries = {
        "characters": {"深深": {}},
        "outfits": {"通勤套装": {}, "家常睡衣": {}},
        "scenes": {"公司": {"defaultOutfit": "通勤套装"}, "卧室": {"defaultOutfit": "家常睡衣"}},
        "props": {"咖啡杯": {}},
    }
    for category, items in entries.items():
        for index, (name, extra) in enumerate(items.items()):
            folder = lib / category / name
            _png(folder / "ref-01.png", (110 + index * 30, 150, 210))
            meta = {"name": name, "description": f"{name}的描述", **extra}
            (folder / "entry.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    seed(ROOT)

    from pet.companion.store import CompanionStore
    from pet.companion import imagegen, library, llm, schedule, selfie

    cfg = FakeCfg(ROOT)
    cfg.set("companion_character_name", "深深")
    cfg.set("companion_image_provider", "openai")
    cfg.set("companion_image_base_url", "http://127.0.0.1:8899")
    cfg.set("companion_image_api_key", "test-key")
    cfg.set("companion_image_model", "mock-image")
    cfg.set("companion_image_aspect", "3:4")
    cfg.set("companion_image_timeout", 60)
    store = CompanionStore(cfg)
    library.ensure_dirs(store)

    llm.ask = lambda *a, **k: json.dumps(PLAN, ensure_ascii=False)

    # 造今天：09:00–12:30 上班 @公司，用素材库把场景/穿搭补上
    from datetime import datetime

    now = datetime.now()
    items = schedule.link_items(schedule.normalize_items(SCHEDULE["items"]), store, fill=True)
    schedule.write_day(store, {
        "date": schedule.date_key(), "dayType": {"id": "workday", "name": "上班日"},
        "title": SCHEDULE["title"], "mood": "踏实", "items": items, "source": "test",
    })
    ctx = schedule.current_context(store)
    print("此刻：", (ctx or {}).get("item"))
    print("补前 scene/outfit：", [(i.get("scene"), i.get("outfit") or i.get("outfitNote")) for i in items][:4])

    rec = selfie.generate(store, "", reason="walk", on_status=lambda s: None)
    selected = rec.get("selected") or {}
    print("meta:", rec.get("meta"))
    print("selected:", selected)
    print("linked（模型没写、按日程补的）:", rec.get("linked"))
    print("schedule 行:", rec.get("schedule"))
    print("图:", rec.get("path"))

    ok = True
    if selected.get("scene") != "公司":
        ok = False
        print("FAIL: 场景没用日程里的「公司」")
    if "通勤套装" not in (selected.get("outfits") or []):
        ok = False
        print("FAIL: 穿搭没用日程里的「通勤套装」")
    if "深深" not in (selected.get("characters") or []):
        ok = False
        print("FAIL: 人物没带主角")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

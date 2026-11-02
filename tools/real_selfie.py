# -*- coding: utf-8 -*-
"""真机自测：用真实配置拍一张自拍（会真的调用生图接口，花钱，慎重复跑）。

验证三件事：
1. companion_* 配置重启后还在（生图接口地址/模型）；
2. 自拍用的是「此刻日程那一段」的场景/穿搭；
3. 参考图确实带上了（meta.refs > 0）。
"""
import sys


def main() -> int:
    from pet.config import Config
    from pet.companion.store import CompanionStore
    from pet.companion import imagegen, schedule, selfie

    cfg = Config()
    store = CompanionStore(cfg)
    print("生图配置：provider=%r base=%r model=%r key=%s" % (
        store.get("companion_image_provider"),
        store.get("companion_image_base_url"),
        store.get("companion_image_model"),
        "已填" if store.image_api_key() else "空",
    ))
    if not str(store.get("companion_image_base_url") or "").strip():
        print("FAIL: 生图接口地址读不到（companion_* 配置没有跨重启保留）")
        return 1

    ctx = schedule.current_context(store)
    item = (ctx or {}).get("item") or {}
    print("此刻：", item.get("start"), "-", item.get("end"), item.get("activity"),
          "|", item.get("scene"), "|", item.get("outfit") or item.get("outfitNote"))

    try:
        rec = selfie.generate(store, "", reason="manual", on_status=lambda s: print("  …", s))
    except Exception as exc:  # noqa: BLE001
        print("FAIL: 自拍失败：", exc)
        return 1

    meta = rec.get("meta") or {}
    print("落盘：", rec.get("path"))
    print("素材：", rec.get("selected"))
    print("参考图数量：", meta.get("refs"))
    print("配文：", rec.get("caption"))
    ok = bool(rec.get("path")) and int(meta.get("refs") or 0) > 0
    print("RESULT:", "PASS" if ok else "FAIL（参考图没带上）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

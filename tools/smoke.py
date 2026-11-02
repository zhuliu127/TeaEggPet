# -*- coding: utf-8 -*-
"""端到端自测：配好假接口 → 生成日程 → 溜达 → 自拍 → 截图退出。"""
import json
import os
import pathlib
import sys
import time

from pet.qt_plugin_sanitizer import sanitize_plugin_path

sanitize_plugin_path()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

OUT = pathlib.Path(os.environ.get("PET_SHOT_DIR", "shots"))
LOG = []


def log(*args):
    line = " ".join(str(a) for a in args)
    LOG.append(line)
    print("[smoke]", line, flush=True)


def configure(host):
    cfg = host.config
    cfg.set("hotkey_go_walk", "Alt+F1")
    cfg.set("companion_image_provider", "65535")
    cfg.set("companion_image_base_url", "http://127.0.0.1:8899")
    cfg.set("companion_image_model", "mock-image")
    cfg.set("companion_image_aspect", "3:4")
    cfg.set("companion_walk_min_minutes", 1)
    cfg.set("companion_walk_max_minutes", 3)
    cfg.set("companion_walk_fixed_minutes", 1)
    cfg.set("companion_walk_duration_mode", "fixed")
    cfg.set("companion_walk_message_every_min", 1)
    cfg.set("companion_character_name", "深深")
    store = None
    from pet.companion.store import CompanionStore

    store = CompanionStore(cfg)
    store.set_image_api_key("test-key")
    # 模型走同一个假接口
    settings = cfg.chat_settings()
    provider = settings.active_config
    provider.base_url = "http://127.0.0.1:8899"
    provider.chat_path = "/v1/chat/completions"
    provider.model = "mock-chat"
    provider.api_key = "test-key"
    provider.api_key_ref = ""
    cfg.set_chat_settings(settings)
    cfg.save()
    return store


def run():
    import logging

    from pet.companion import runtime, schedule, selfie

    warnings = []

    class _Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    logging.getLogger().addHandler(_Grab())

    host = runtime.app()
    if host is None:
        log("shell 还没起来")
        return
    store = configure(host)
    try:
        host._apply_global_hotkeys()
    except Exception as exc:  # noqa: BLE001
        log("hotkey apply failed:", exc)
    svc = getattr(host, "_hotkeys", None)
    log("hotkey callbacks:", "go_walk" in getattr(svc, "_callbacks", {}),
        "| warnings:", [w for w in warnings if "快捷键" in w or "hotkey" in w.lower()])
    # 菜单能建出来（不弹）
    try:
        from PySide6.QtWidgets import QMenu
        from pet.context_menus.registry import MenuActionRegistry
        m = QMenu()
        MenuActionRegistry()._specs["go_walk"].build(m, type("P", (), {})())
        log("menu ok:", [a.text() for a in m.actions()])
    except Exception as exc:  # noqa: BLE001
        log("menu failed:", exc)
    # 1) 日程
    day = schedule.generate_day(store)
    log("schedule source =", day.get("source"), "items =", len(day["items"]), "title =", day.get("title"))
    # 2) 自拍（不经过溜达，验证生图链路）
    try:
        rec = selfie.generate(store, reason="manual")
        log("selfie ok:", rec["caption"], "|", pathlib.Path(rec["path"]).name, "|", rec["selected"])
    except Exception as exc:  # noqa: BLE001
        log("selfie failed:", exc)
    # 3) 溜达
    service = runtime.service()
    log("service =", service)
    started = service.start()
    log("walk start:", started)
    QTimer.singleShot(4000, lambda: after_start(service))


def after_start(service):
    log("walking =", service.walking, "left =", service.minutes_left)
    result = service.stop("smoke")
    log("walk stop:", result)
    QTimer.singleShot(9000, finish)


def finish():
    app = QApplication.instance()
    OUT.mkdir(parents=True, exist_ok=True)
    for w in app.topLevelWidgets():
        try:
            if not w.isVisible():
                continue
            name = w.objectName() or w.__class__.__name__
            w.grab().save(str(OUT / f"{name}.png"))
            log("shot:", name)
        except Exception as exc:  # noqa: BLE001
            log("shot fail", exc)
    (OUT / "smoke.log").write_text("\n".join(LOG), encoding="utf-8")
    app.quit()


_orig_exec = QApplication.exec


def _exec(self=None):
    QTimer.singleShot(int(os.environ.get("PET_SMOKE_DELAY", "6000")), run)
    return _orig_exec()


QApplication.exec = _exec

if __name__ == "__main__":
    from pet.app import main

    sys.exit(main())

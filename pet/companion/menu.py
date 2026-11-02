# -*- coding: utf-8 -*-
"""右键菜单里的「陪伴」入口与「出去溜达」动作。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from . import runtime

log = logging.getLogger(__name__)


def _service():
    return runtime.service()


def add_go_walk(menu, pet=None):
    """出去溜达 / 叫她回家（同一个菜单位，标签跟状态翻转）。"""
    from ..context_menus.shared import add_action

    service = _service()
    walking = bool(service and service.walking)
    if walking:
        label = f"叫她回家（还有 {service.minutes_left} 分钟）"
    else:
        label = "出去溜达（回来发自拍）"
    try:
        from ..hotkeys import ACTIONS

        hotkey = ""
        host = runtime.app()
        if host is not None:
            hotkey = str(host.config.get(ACTIONS.get("go_walk", "hotkey_go_walk"), "") or "")
        if hotkey and not walking:
            label += f"　[{hotkey}]"
    except Exception:
        pass

    def _trigger():
        svc = _service()
        if svc is None:
            return
        result = svc.toggle()
        if isinstance(result, dict) and not result.get("ok", True):
            runtime.bubble(str(result.get("error") or "现在不方便出去"))

    return add_action(menu, label, "walk" if not walking else "home", _trigger, close_on_trigger=True)


def _hotkey_text(action: str) -> str:
    """配置里这个动作绑的组合键（没绑就是空串）。"""
    try:
        from ..hotkeys import ACTIONS

        host = runtime.app()
        if host is None:
            return ""
        return str(host.config.get(ACTIONS.get(action, ""), "") or "").strip()
    except Exception:
        return ""


def add_selfie(menu, pet=None):
    """「发一张自拍」：按此刻的日程拍一张，不溜达。"""
    from ..context_menus.shared import add_action

    service = _service()
    busy = bool(service and getattr(service, "_worker", None) is not None)
    label = "发一张自拍（按此刻的日程）"
    key = _hotkey_text("selfie_now")
    if key:
        label += f"　[{key}]"
    if busy:
        label = "正在拍…"

    def _trigger():
        svc = _service()
        if svc is None:
            return
        svc.take_selfie("manual")

    return add_action(menu, label, "image", _trigger, close_on_trigger=True)


def add_companion_submenu(menu, pet=None):
    """「陪伴扩展」子菜单：日程、设置、照片目录。"""
    from ..context_menus.shared import add_action, add_submenu

    sub = add_submenu(menu, "陪伴扩展", "sparkle")

    def _settings():
        runtime.open_settings()

    def _today_plan():
        from . import schedule

        svc = _service()
        if svc is None:
            runtime.bubble("陪伴扩展还没启动好，稍等一下再点～", 5000)
            return
        key = schedule.date_key()
        day = schedule.read_day(svc.store, key)
        if day is None:
            runtime.bubble("今天的安排我还没想好，点「重新想想今天」试试", 6000)
            return
        ctx = schedule.current_context(svc.store)
        item = (ctx or {}).get("item") or {}
        head = f"今天（{schedule.date_label(key)}）：{day.get('title') or (day.get('dayType') or {}).get('name', '')}"
        lines = [head]
        if item:
            where = item.get("scene") or item.get("location") or ""
            dressed = item.get("outfit") or item.get("outfitNote") or ""
            lines.append(f"现在 {item.get('start', '')}–{item.get('end', '')}：{item.get('activity', '')}"
                         + (f"（{where}）" if where else "")
                         + (f"，穿 {dressed}" if dressed else ""))
            nxt = (ctx or {}).get("next") or []
            if nxt:
                lines.append("接下来：" + "；".join(nxt))
            lines.append("点「发一张自拍」我就把这一刻拍给你。")
        else:
            lines.append("这会儿没有正在做的事（日程空隙），点「发一张自拍」也能拍。")
        runtime.bubble("\n".join(lines), 12000)

    def _make_today():
        from . import schedule

        svc = _service()
        if svc is None:
            runtime.bubble("陪伴扩展还没启动好，稍等一下再点～", 5000)
            return
        runtime.bubble("我想想今天怎么过…", 8000)
        try:
            day = schedule.generate_day(svc.store, hint="")
            runtime.bubble(f"想好了：{day.get('title') or (day.get('dayType') or {}).get('name', '')}", 7000)
        except Exception as exc:  # noqa: BLE001
            log.exception("生成日程失败")
            runtime.bubble(f"没想出来：{str(exc)[:60]}", 8000)

    def _make_tomorrow():
        from . import schedule

        svc = _service()
        if svc is None:
            runtime.bubble("陪伴扩展还没启动好，稍等一下再点～", 5000)
            return
        runtime.bubble("我想想明天怎么过…", 8000)
        try:
            key = schedule.add_days(schedule.date_key(), 1)
            day = schedule.generate_day(svc.store, key)
            runtime.bubble(f"明天的打算：{day.get('title') or (day.get('dayType') or {}).get('name', '')}", 7000)
        except Exception as exc:  # noqa: BLE001
            log.exception("生成明天的日程失败")
            runtime.bubble(f"没想出来：{str(exc)[:60]}", 8000)

    def _open_outputs():
        svc = _service()
        if svc is None:
            return
        folder = Path(svc.store.outputs_dir())
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _selfie():
        svc = _service()
        if svc is None:
            runtime.bubble("陪伴扩展还没启动好，稍等一下再点～", 5000)
            return
        svc.take_selfie("manual")

    selfie_label = "发一张自拍（按此刻的日程）"
    key = _hotkey_text("selfie_now")
    if key:
        selfie_label += f"　[{key}]"
    add_action(sub, selfie_label, "image", _selfie, close_on_trigger=True)
    add_action(sub, "看看今天在干什么", "plan", _today_plan, close_on_trigger=True)
    add_action(sub, "重新想想今天", "refresh", _make_today, close_on_trigger=True)
    add_action(sub, "想想明天", "refresh", _make_tomorrow, close_on_trigger=True)
    sub.addSeparator()
    def _open_library():
        runtime.open_library()

    add_action(sub, "素材库与关键词…", "image", _open_library, close_on_trigger=True)
    add_action(sub, "发给我的照片", "image", _open_outputs, close_on_trigger=True)
    add_action(sub, "陪伴扩展设置…", "settings", _settings, close_on_trigger=True)
    return sub

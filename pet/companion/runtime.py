# -*- coding: utf-8 -*-
"""进程内单例：把「陪伴扩展」的服务挂到宿主 AppShell 上，供菜单 / 热键 / 设置页共用。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_app = None
_service = None
_settings_dialog = None
_library_dialog = None


def attach(app) -> None:
    """宿主启动时调一次（幂等）。"""
    global _app, _service
    _app = app
    if _service is None and app is not None:
        try:
            from .walk import WalkService

            _service = WalkService(app)
            _ensure_default_hotkey(app)
            log.info("陪伴扩展已就绪")
        except Exception:
            log.exception("陪伴扩展初始化失败（溜达功能不可用）")


def _ensure_default_hotkey(app) -> None:
    """首次启用陪伴扩展时给一个默认快捷键（用户随时可以在设置里改掉）。

    只在「从没设过」时写一次：之后用户清空就是清空，不再自动填回去。
    """
    try:
        cfg = app.config
        changed = False
        if not cfg.get("companion_hotkey_seeded", False):
            if not str(cfg.get("hotkey_go_walk", "") or "").strip():
                cfg.set("hotkey_go_walk", "Alt+F1")
            cfg.set("companion_hotkey_seeded", True)
            changed = True
        if not cfg.get("companion_selfie_hotkey_seeded", False):
            # 「发自拍」也先给一个默认键；之后清空就是清空。
            # 不用 Ctrl+Alt+S：实测被 QQ/微信系截图占着，注册必失败。
            if not str(cfg.get("hotkey_selfie_now", "") or "").strip():
                cfg.set("hotkey_selfie_now", "Ctrl+Shift+S")
            cfg.set("companion_selfie_hotkey_seeded", True)
            changed = True
        if not changed:
            return
        cfg.save()
        apply = getattr(app, "_apply_global_hotkeys", None)
        if callable(apply):
            apply()
        if bubble_host() is not None:
            from PySide6.QtCore import QTimer

            walk_key = str(cfg.get("hotkey_go_walk", "") or "").strip()
            selfie_key = str(cfg.get("hotkey_selfie_now", "") or "").strip()
            tip = "，".join(x for x in (
                f"按 {walk_key} 我出去溜达" if walk_key else "",
                f"{selfie_key} 直接拍张自拍" if selfie_key else "",
            ) if x)
            if tip:
                QTimer.singleShot(4000, lambda: bubble(tip + "～", 8000))
    except Exception:
        log.debug("设置默认快捷键失败", exc_info=True)


def _candidates():
    """可能能冒泡/能动的宿主对象，按优先级排（去重）。

    overlay 拓扑下 ``instance.win`` 恒为 None，呈现面是 ``_overlay_shell``
    （见 app.py 的拓扑分支注释）——菜单/溜达以前只认 instance.win，于是点了
    菜单没反应、溜达也不冒泡。这里统一按能力找，而不是按身份找。
    """
    host = _app
    if host is None:
        return []
    out = []

    def push(obj):
        if obj is not None and obj not in out:
            out.append(obj)

    push(getattr(getattr(host, "instance", None), "win", None))
    push(getattr(host, "win", None))
    push(getattr(host, "_overlay_shell", None))
    return out


def bubble_host():
    """第一个真的能show_bubble 的宿主；没有就 None（调用方要能接受 None）。"""
    for obj in _candidates():
        if callable(getattr(obj, "show_bubble", None)):
            return obj
    return None


def move_host():
    """第一个能 trigger_move 的宿主（legacy 的 PetWindow / sprite 侧的 facade）。"""
    for obj in _candidates():
        if callable(getattr(obj, "trigger_move", None)):
            return obj
    shell = getattr(_app, "_overlay_shell", None) if _app is not None else None
    for name in ("current_sprite", "sprite", "main_sprite"):
        sprite = getattr(shell, name, None) if shell is not None else None
        if callable(getattr(sprite, "trigger_move", None)):
            return sprite
    return None


def bubble(text: str, ms: int = 6000) -> bool:
    """在当前拓扑下冒个泡；返回是否真的发出去了。"""
    win = bubble_host()
    if win is None:
        return False
    try:
        win.show_bubble(str(text), duration_ms=int(ms))
        return True
    except TypeError:
        try:
            win.show_bubble(str(text), int(ms))
            return True
        except Exception:
            log.debug("冒泡失败", exc_info=True)
            return False
    except Exception:
        log.debug("冒泡失败", exc_info=True)
        return False


def service():
    if _service is None and _app is not None:
        attach(_app)
    return _service


def app():
    return _app


def open_settings(parent=None, page: str = "") -> None:
    """打开陪伴扩展设置（单例窗口，关掉再开会重建）。"""
    global _settings_dialog
    host = _app
    if host is None:
        return
    try:
        if _settings_dialog is not None:
            _settings_dialog.raise_()
            _settings_dialog.activateWindow()
            return
        from .settings_dialog import CompanionSettingsDialog

        dialog = CompanionSettingsDialog(host.config, parent or getattr(getattr(host, "instance", None), "win", None))
        _settings_dialog = dialog

        def _closed(_result=None):
            global _settings_dialog
            _settings_dialog = None

        dialog.finished.connect(_closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if page:
            dialog.select_page(page)
    except Exception:
        log.exception("打开陪伴扩展设置失败")


def open_library(parent=None) -> None:
    """打开「素材库与关键词」窗口（单例）。"""
    global _library_dialog
    host = _app
    if host is None:
        return
    try:
        if _library_dialog is not None:
            _library_dialog.raise_()
            _library_dialog.activateWindow()
            return
        from .library_dialog import LibraryDialog

        dialog = LibraryDialog(host.config, parent or getattr(getattr(host, "instance", None), "win", None))
        _library_dialog = dialog

        def _closed(_result=None):
            global _library_dialog
            _library_dialog = None

        dialog.finished.connect(_closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
    except Exception:
        log.exception("打开素材库窗口失败")


def shutdown() -> None:
    global _service, _settings_dialog, _library_dialog
    if _service is not None:
        try:
            _service.shutdown()
        except Exception:
            log.debug("关闭陪伴扩展服务失败", exc_info=True)
    _service = None
    _settings_dialog = None
    _library_dialog = None
_library_dialog = None

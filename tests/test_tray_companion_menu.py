# -*- coding: utf-8 -*-
"""托盘里的「陪伴扩展」入口。

用户实测：她出去溜达后桌宠整窗隐藏，托盘右键却没有「叫她回家 / AI 对话 / 自拍」，
于是没有任何办法把她叫回来。这里锁住托盘的这组入口与文案翻转。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _FakeService:
    def __init__(self, *, walking: bool = False, minutes_left: int = 0) -> None:
        self.walking = walking
        self.minutes_left = minutes_left
        self.toggles = 0
        self.selfies: list[str] = []
        self.toggle_result: dict = {"ok": True}

    def toggle(self):
        self.toggles += 1
        self.walking = not self.walking
        return self.toggle_result

    def take_selfie(self, reason="manual"):
        self.selfies.append(reason)


def _shell():
    """只构造需要的属性：本测试只碰托盘菜单构建这一段。"""
    from pet.app import AppShell

    shell = AppShell.__new__(AppShell)
    shell.tray = None
    return shell


def _build(shell):
    from PySide6.QtWidgets import QMenu

    menu = QMenu()
    subs: list = []

    def track(child):          # 与 app 里的 track_menu 同一约定：登记并原样返回
        subs.append(child)
        return child

    action = shell._add_companion_tray_menu(menu, track)
    return menu, action, subs


def test_tray_has_companion_submenu_with_four_items(monkeypatch):
    _qapp()
    import pet.app as app_mod

    svc = _FakeService(walking=False)
    monkeypatch.setattr(app_mod.AppShell, "_companion_service", staticmethod(lambda: svc))
    menu, walk_action, subs = _build(_shell())

    sub = next(a.menu() for a in menu.actions() if a.menu() is not None)
    texts = [a.text() for a in sub.actions() if not a.isSeparator()]
    assert texts == [
        "出去溜达（回来发自拍）",
        "发一张自拍（按此刻的日程）",
        "素材库与关键词…",
        "陪伴扩展设置…",
    ]
    assert walk_action is sub.actions()[0]
    assert subs and subs[0] is sub


def test_walk_label_flips_when_outside(monkeypatch):
    _qapp()
    import pet.app as app_mod

    svc = _FakeService(walking=True, minutes_left=17)
    monkeypatch.setattr(app_mod.AppShell, "_companion_service", staticmethod(lambda: svc))
    menu, walk_action, _ = _build(_shell())
    assert walk_action.text() == "叫她回家（还有 17 分钟）"


def test_walk_action_toggles_service(monkeypatch):
    _qapp()
    import pet.app as app_mod

    svc = _FakeService(walking=True, minutes_left=5)
    monkeypatch.setattr(app_mod.AppShell, "_companion_service", staticmethod(lambda: svc))
    shell = _shell()
    bubbles: list[str] = []
    monkeypatch.setattr(app_mod.AppShell, "_companion_bubble", staticmethod(lambda t, ms=5000: bubbles.append(str(t))))

    from pet.context_menus import shared as shared_mod

    # 托盘条目的回调是「菜单关掉之后再执行」，测试里直接同步跑
    monkeypatch.setattr(shared_mod, "run_after_menu_closed", lambda menu, cb: cb())
    menu, walk_action, _ = _build(shell)
    walk_action.trigger()
    assert svc.toggles == 1, "托盘点一下要真的切换溜达状态（叫她回家）"
    assert bubbles == []


def test_walk_action_reports_error(monkeypatch):
    _qapp()
    import pet.app as app_mod

    svc = _FakeService(walking=False)
    svc.toggle_result = {"ok": False, "error": "现在没在溜达"}
    monkeypatch.setattr(app_mod.AppShell, "_companion_service", staticmethod(lambda: svc))
    shell = _shell()
    bubbles: list[str] = []
    monkeypatch.setattr(app_mod.AppShell, "_companion_bubble", staticmethod(lambda t, ms=5000: bubbles.append(str(t))))
    from pet.context_menus import shared as shared_mod

    # 托盘条目的回调是「菜单关掉之后再执行」，测试里直接同步跑
    monkeypatch.setattr(shared_mod, "run_after_menu_closed", lambda menu, cb: cb())

    menu, walk_action, _ = _build(shell)
    walk_action.trigger()
    assert bubbles == ["现在没在溜达"]


def test_companion_tray_items_no_service_do_not_crash(monkeypatch):
    _qapp()
    import pet.app as app_mod

    monkeypatch.setattr(app_mod.AppShell, "_companion_service", staticmethod(lambda: None))
    shell = _shell()
    bubbles: list[str] = []
    monkeypatch.setattr(app_mod.AppShell, "_companion_bubble", staticmethod(lambda t, ms=5000: bubbles.append(str(t))))
    from pet.context_menus import shared as shared_mod

    # 托盘条目的回调是「菜单关掉之后再执行」，测试里直接同步跑
    monkeypatch.setattr(shared_mod, "run_after_menu_closed", lambda menu, cb: cb())

    menu, walk_action, _ = _build(shell)
    assert walk_action.text() == "出去溜达（回来发自拍）"
    walk_action.trigger()
    assert bubbles and "还没启动好" in bubbles[0]

# -*- coding: utf-8 -*-
"""overlay 托盘的「陪伴扩展」入口。

用户实测（1.4.8）：托盘右键里根本没有陪伴扩展——因为 overlay 拓扑下用户看到的
是 OverlayShell 自己的托盘（AppShell._build_tray 那份不安装），而陪伴入口只加在了
AppShell 那份上。这条测试锁住「壳这份也必须挂」。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from tests.test_overlay_tray_routing import _make_nodir_shell, _submenus, _tray_menu


class _FakeService:
    def __init__(self, *, walking: bool = False, minutes_left: int = 0) -> None:
        self.walking = walking
        self.minutes_left = minutes_left
        self.toggles = 0
        self.selfies: list[str] = []

    def toggle(self):
        self.toggles += 1
        self.walking = not self.walking
        return {"ok": True}

    def take_selfie(self, reason="manual"):
        self.selfies.append(reason)


def test_overlay_tray_has_companion_submenu():
    shell = _make_nodir_shell()
    menu = _tray_menu(shell)
    subs = _submenus(menu)
    assert "陪伴扩展" in subs, "overlay 托盘必须挂陪伴扩展（她出门后就靠这条叫回来）"
    texts = [a.text() for a in subs["陪伴扩展"].actions() if not a.isSeparator()]
    assert texts == [
        "出去溜达（回来发自拍）",
        "发一张自拍（按此刻的日程）",
        "素材库与关键词…",
        "陪伴扩展设置…",
    ]


def test_overlay_tray_walk_label_follows_state(monkeypatch):
    from pet import overlay_shell as shell_mod

    svc = _FakeService(walking=True, minutes_left=23)
    monkeypatch.setattr(shell_mod.OverlayShell, "_companion_service", staticmethod(lambda: svc))
    shell = _make_nodir_shell()
    menu = _tray_menu(shell)
    label = _submenus(menu)["陪伴扩展"].actions()[0].text()
    assert label == "叫她回家（还有 23 分钟）"


def test_overlay_tray_toggle_switches_walking(monkeypatch):
    from pet import overlay_shell as shell_mod

    svc = _FakeService(walking=True, minutes_left=5)
    monkeypatch.setattr(shell_mod.OverlayShell, "_companion_service", staticmethod(lambda: svc))
    from pet.context_menus import shared as shared_mod

    monkeypatch.setattr(shared_mod, "run_after_menu_closed", lambda menu, cb: cb())
    shell = _make_nodir_shell()
    menu = _tray_menu(shell)
    walk = _submenus(menu)["陪伴扩展"].actions()[0]
    walk.trigger()
    assert svc.toggles == 1

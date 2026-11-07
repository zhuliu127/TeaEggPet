# -*- coding: utf-8 -*-
"""启动自动检查更新（静默）的行为约束。

要点：只在「确实有新版」时冒一句，不打开设置窗；已是最新版或连不上都安静。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _newer_tag() -> str:
    from pet.product import PRODUCT_VERSION

    parts = [int(x) for x in PRODUCT_VERSION.split(".")]
    parts[-1] += 1
    return ".".join(str(x) for x in parts)


class _FakeWin:
    def __init__(self, alive: bool = True):
        self.bubbles: list[str] = []
        self.alive = alive

    # _UpdateBridge 会先 shiboken6.isValid(parent) 探活；非 Qt 对象会返回 False，
    # 故这里用真实的 QWidget 子类在下面替换
    def show_bubble(self, text, duration_ms=6000):
        self.bubbles.append(str(text))


class _Owner:
    def __init__(self):
        self.instance = None


def _bridge(monkeypatch, alive=True, quiet=True):
    """构造一个 parent 是「活着的 QWidget」的桥，并让实例层可记录是否开设置页。"""
    import pet.app as app_mod

    calls: list[str] = []

    class Win(__import__("PySide6.QtWidgets", fromlist=["QWidget"]).QWidget):
        def show_bubble(self, text, duration_ms=6000):
            calls.append(str(text))

    win = Win()
    win.show()

    opened: list[str] = []
    owner = _Owner()

    class Instance:
        def open_modern_settings(self, initial_page=None):
            opened.append(initial_page or "")

    owner.instance = Instance()
    bridge = app_mod._UpdateBridge(win, owner=owner, quiet=quiet)
    return bridge, calls, opened, win


def test_quiet_bridge_only_speaks_when_newer(monkeypatch):
    _qapp()
    from pet import updater

    newer = _newer_tag()
    bridge, calls, opened, win = _bridge(monkeypatch)
    bridge.done.emit(True, {"version": newer, "notes": "x", "html_url": "https://e.x"})
    assert calls and newer in calls[0]
    # 静默模式绝不自己把设置窗拉起来
    assert opened == []


def test_quiet_bridge_is_silent_when_up_to_date():
    _qapp()
    from pet.product import PRODUCT_VERSION

    bridge, calls, opened, win = _bridge(None)
    bridge.done.emit(True, {"version": PRODUCT_VERSION, "notes": "", "html_url": ""})
    assert calls == []
    assert opened == []


def test_quiet_bridge_is_silent_on_failure():
    _qapp()
    bridge, calls, opened, win = _bridge(None)
    bridge.done.emit(False, "网络不通")
    assert calls == []
    assert opened == []


def test_manual_bridge_still_opens_update_page():
    """手动「检查更新」保持原行为：发现新版 → 打开「更新」页。"""
    _qapp()
    from PySide6.QtWidgets import QApplication

    newer = _newer_tag()
    bridge, calls, opened, win = _bridge(None, quiet=False)
    bridge.done.emit(True, {"version": newer, "notes": "x", "html_url": "https://e.x"})
    QApplication.processEvents()
    assert calls and "正在打开" in calls[0]
    assert opened == ["更新"]


def test_auto_check_runs_only_once(monkeypatch):
    _qapp()
    import pet.app as app_mod
    from pet.config import Config
    import tempfile
    import pathlib

    shell = app_mod.AppShell.__new__(app_mod.AppShell)
    shell.config = Config(pathlib.Path(tempfile.mkdtemp(prefix="upd-")))
    shell._update_checking = False
    shell._update_auto_checked = False
    shell._update_bridge = None
    shell.instance = None

    started: list[str] = []
    monkeypatch.setattr(app_mod.updater, "latest_release", lambda *a, **k: None)
    real_thread = app_mod.threading.Thread

    def fake_thread(target=None, **kw):
        started.append(kw.get("name", ""))
        return real_thread(target=lambda: None, daemon=True)

    monkeypatch.setattr(app_mod.threading, "Thread", fake_thread)
    shell._auto_check_update()
    shell._auto_check_update()
    assert len(started) == 1 and started[0] == "pet-update-auto-check"

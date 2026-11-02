# -*- coding: utf-8 -*-
"""开发自测：陪伴扩展菜单（发自拍 / 看看今天在干什么）在 **overlay 拓扑** 下真的有效。

背景：overlay 拓扑下 ``instance.win`` 恒为 None，菜单以前只认它，点了毫无反应。
这里用假的宿主复现同一个形状（instance.win=None + _overlay_shell 能冒泡），
然后真的把 QAction 触发一遍，看气泡/自拍调用有没有落到该去的地方。
"""
from __future__ import annotations

import pathlib
import sys

from pet.qt_plugin_sanitizer import sanitize_plugin_path

sanitize_plugin_path()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMenu


class FakeShell:
    def __init__(self) -> None:
        self.bubbles: list[tuple[str, int]] = []

    def show_bubble(self, text, duration_ms=3200, **kwargs):  # noqa: ANN001
        self.bubbles.append((str(text), int(duration_ms)))
        return True


class FakeInstance:
    win = None  # overlay 拓扑的关键特征


class FakeService:
    def __init__(self, store) -> None:
        self.store = store
        self.walking = False
        self.minutes_left = 0
        self._worker = None
        self.selfies: list[str] = []

    def toggle(self):
        return {"ok": True}

    def take_selfie(self, reason="manual"):
        self.selfies.append(reason)


class FakeApp:
    def __init__(self, config) -> None:
        self.config = config
        self.instance = FakeInstance()
        self._overlay_shell = FakeShell()


def run() -> int:
    from pet.config import Config
    from pet.companion import menu as companion_menu, runtime

    app = QApplication.instance() or QApplication(sys.argv)
    cfg = Config()
    cfg.set("hotkey_go_walk", "Alt+F1")
    cfg.set("hotkey_selfie_now", "Alt+F2")  # 只是给标签用，不真注册

    from pet.companion.store import CompanionStore

    host = FakeApp(cfg)
    service = FakeService(CompanionStore(cfg))
    runtime._app = host
    runtime._service = service

    print("bubble_host →", type(runtime.bubble_host()).__name__)

    ok = True

    # ① 主菜单两项
    menu = QMenu()
    companion_menu.add_go_walk(menu)
    companion_menu.add_selfie(menu)
    labels = [a.text() for a in menu.actions() if a.text()]
    print("主菜单项:", labels)
    if not any("发自拍" in t for t in labels):
        ok = False
        print("FAIL: 主菜单没有「发自拍」")
    if not any("[Alt+F2]" in t for t in labels):
        ok = False
        print("FAIL: 自拍项没显示快捷键")

    # ② 触发「发自拍」→ 应该调用 service.take_selfie
    for action in menu.actions():
        if "发一张自拍" in action.text():
            action.trigger()
    print("take_selfie 调用:", service.selfies)
    if service.selfies != ["manual"]:
        ok = False
        print("FAIL: 自拍没被触发")

    # ③ 子菜单：「看看今天在干什么」应该冒泡到 overlay shell
    root = QMenu()
    sub_menu = companion_menu.add_companion_submenu(root)
    sub_labels = [a.text() for a in sub_menu.actions() if a.text()]
    print("子菜单:", sub_labels)
    for action in sub_menu.actions():
        if "看看今天在干什么" in action.text():
            action.trigger()
    print("气泡:", host._overlay_shell.bubbles)
    if not host._overlay_shell.bubbles:
        ok = False
        print("FAIL: 「看看今天在干什么」还是没有气泡")
    else:
        text, ms = host._overlay_shell.bubbles[-1]
        if "现在" not in text:
            ok = False
            print("FAIL: 气泡里没有「现在」那条")
        if len(text) < 12:
            ok = False
            print("FAIL: 气泡内容太短")

    print("RESULT:", "PASS" if ok else "FAIL")
    QTimer.singleShot(0, app.quit)
    app.exec()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())

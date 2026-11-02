# -*- coding: utf-8 -*-
"""验证「全局快捷键 → 出去溜达」这条原生链路：直接给窗口投递 WM_HOTKEY。"""
import ctypes
import os
import sys

from pet.qt_plugin_sanitizer import sanitize_plugin_path

sanitize_plugin_path()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

WM_HOTKEY = 0x0312
BASE_ID = 0xA5D0


def log(*args):
    print("[hotkey]", *args, flush=True)


def run():
    from pet.companion import runtime
    from pet.hotkeys import ACTIONS

    host = runtime.app()
    if host is None:
        log("shell 未就绪")
        return
    host.config.set("hotkey_go_walk", "Alt+F1")
    host.config.save()
    host._apply_global_hotkeys()
    service = runtime.service()
    log("walking before =", service.walking)
    index = list(ACTIONS).index("go_walk")
    hotkey_id = BASE_ID + index
    win = getattr(getattr(host, "instance", None), "win", None)
    hwnd = int(win.winId()) if win is not None else 0
    log("hwnd =", hwnd, "id =", hex(hotkey_id))
    user32 = ctypes.windll.user32
    ok = user32.PostMessageW(hwnd, WM_HOTKEY, hotkey_id, 0)
    log("PostMessage =", ok)
    QTimer.singleShot(1200, lambda: after(service))


def after(service):
    log("walking after =", service.walking, "| minutes left =", service.minutes_left)
    if service.walking:
        service.stop("hotkey-test")
    QTimer.singleShot(800, lambda: QApplication.instance().quit())


_orig_exec = QApplication.exec


def _exec(self=None):
    QTimer.singleShot(int(os.environ.get("PET_HOTKEY_DELAY", "7000")), run)
    return _orig_exec()


QApplication.exec = _exec

if __name__ == "__main__":
    from pet.app import main

    sys.exit(main())

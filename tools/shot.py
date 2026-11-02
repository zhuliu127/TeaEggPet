# -*- coding: utf-8 -*-
"""开发自测：启动桌宠，N 秒后把所有可见窗口截图存盘再退出。"""
import os
import sys
import pathlib

from pet.qt_plugin_sanitizer import sanitize_plugin_path
sanitize_plugin_path()

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

OUT = pathlib.Path(os.environ.get("PET_SHOT_DIR", "shots"))


def grab():
    app = QApplication.instance()
    OUT.mkdir(parents=True, exist_ok=True)
    for w in app.topLevelWidgets():
        try:
            if not w.isVisible():
                continue
            name = w.objectName() or w.__class__.__name__
            pm = w.grab()
            pm.save(str(OUT / f"{name}.png"))
            print("shot:", name, w.width(), "x", w.height(), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("shot fail:", exc, flush=True)
    app.quit()


_orig_exec = QApplication.exec


def _exec(self=None):
    QTimer.singleShot(int(os.environ.get("PET_SHOT_DELAY", "8000")), grab)
    return _orig_exec() if self is None or isinstance(self, QApplication) else _orig_exec(self)


QApplication.exec = _exec

if __name__ == "__main__":
    from pet.app import main
    sys.exit(main())

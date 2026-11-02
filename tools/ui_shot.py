# -*- coding: utf-8 -*-
"""把陪伴扩展的两个窗口截图标一下（开发自测）。"""
import os
import pathlib
import sys

from pet.qt_plugin_sanitizer import sanitize_plugin_path

sanitize_plugin_path()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

OUT = pathlib.Path(os.environ.get("PET_SHOT_DIR", "shots_ui"))


def main() -> int:
    from pet.config import Config
    from pet.companion.settings_dialog import CompanionSettingsDialog
    from pet.companion.library_dialog import LibraryDialog

    app = QApplication.instance() or QApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = Config()

    settings = CompanionSettingsDialog(cfg)
    settings.show()
    app.processEvents()
    settings.grab().save(str(OUT / "settings-image.png"))
    for index, name in enumerate(("schedule", "walk", "about")):
        settings.select_page(name)
        app.processEvents()
        settings.grab().save(str(OUT / f"settings-{name}.png"))
    settings.close()

    lib = LibraryDialog(cfg)
    lib.show()
    app.processEvents()
    tabs = lib.findChild(__import__("PySide6.QtWidgets", fromlist=["QTabWidget"]).QTabWidget)
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        app.processEvents()
        lib.grab().save(str(OUT / f"library-{index}.png"))
    lib.close()
    print("saved to", OUT, flush=True)
    QTimer.singleShot(0, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

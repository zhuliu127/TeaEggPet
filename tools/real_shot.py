# -*- coding: utf-8 -*-
"""开发自测：用真实数据截「素材库」「日程×素材联动表」两张图。"""
import os
import pathlib
import sys

from pet.qt_plugin_sanitizer import sanitize_plugin_path

sanitize_plugin_path()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QTabWidget

OUT = pathlib.Path(os.environ.get("PET_SHOT_DIR", "shots_real"))


def main() -> int:
    from pet.config import Config
    from pet.companion.library_dialog import LibraryDialog
    from pet.companion.settings_dialog import CompanionSettingsDialog

    app = QApplication.instance() or QApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = Config()

    lib = LibraryDialog(cfg)
    lib.resize(980, 720)
    lib.show()
    app.processEvents()
    tabs = lib.findChild(QTabWidget)
    for index, name in enumerate(("characters", "outfits", "scenes", "props", "presets")):
        tabs.setCurrentIndex(index)
        for _ in range(3):
            app.processEvents()
        lib.grab().save(str(OUT / f"library-{name}.png"))
    lib.close()

    dialog = CompanionSettingsDialog(cfg)
    dialog.resize(980, 780)
    dialog.show()
    dialog.select_page("schedule")
    for _ in range(5):
        app.processEvents()
    dialog.grab().save(str(OUT / "link-table.png"))
    dialog.close()

    print("saved to", OUT, flush=True)
    QTimer.singleShot(0, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

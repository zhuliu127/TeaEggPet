# -*- coding: utf-8 -*-
"""Modern settings page for online updates.

只做两件事：查有没有新版本、给一个通往下载页的入口。
**不提供「下载并安装」**：桌面版发的是绿色 zip 包（不是 Inno Setup 安装器），
``updater.select_windows_installer`` 在这里永远选不到东西，按钮点了只会报
「没有找到匹配的 Windows 安装包」。留着比删掉更误导，故整条自动安装链路一并撤掉。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__ as kernel_version, updater
from .product import PRODUCT_VERSION
from .settings_about import build_author_section


class UpdatePage(QWidget):
    """检查更新 + 打开下载页；不写入 Config，避免把一次性命令变成设置项。"""

    check_finished = Signal(object)

    def __init__(self, config, *, include_chat: bool = True, parent=None):
        super().__init__(parent)
        self.config = config
        self.include_chat = bool(include_chat)
        self.release: dict | None = None
        self._busy = False
        self._build_ui()
        self.check_finished.connect(self._on_check_finished)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        intro = QLabel(
            f"当前版本 v{PRODUCT_VERSION}（内核 {kernel_version}）。更新检查会优先使用 GitHub API，"
            "连接失败时自动切换到 CDN 镜像。桌宠每次启动都会静默检查一次。",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("updateIntro")
        layout.addWidget(intro)

        status_card = QFrame(self)
        status_card.setObjectName("updateStatusCard")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(16, 14, 16, 14)
        status_layout.setSpacing(8)
        self.version_label = QLabel(f"当前版本  v{PRODUCT_VERSION}", status_card)
        self.version_label.setObjectName("updateCurrentVersion")
        self.status_label = QLabel("尚未检查更新", status_card)
        self.status_label.setObjectName("updateStatus")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.version_label)
        status_layout.addWidget(self.status_label)
        layout.addWidget(status_card)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.check_button = QPushButton("检查更新", self)
        self.check_button.setObjectName("checkUpdateButton")
        self.check_button.clicked.connect(self.check_for_updates)
        self.manual_button = QPushButton("打开下载页", self)
        self.manual_button.setObjectName("manualUpdateButton")
        self.manual_button.setEnabled(False)
        self.manual_button.clicked.connect(self.open_download_page)
        action_row.addWidget(self.check_button)
        action_row.addWidget(self.manual_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        notes_card = QFrame(self)
        notes_card.setObjectName("updateNotesCard")
        notes_layout = QVBoxLayout(notes_card)
        notes_layout.setContentsMargins(16, 14, 16, 14)
        notes_layout.setSpacing(6)
        notes_title = QLabel("新版本说明", notes_card)
        notes_title.setObjectName("updateNotesTitle")
        self.notes_label = QLabel("检查到新版本后，这里会显示版本说明。", notes_card)
        self.notes_label.setObjectName("updateNotes")
        self.notes_label.setWordWrap(True)
        self.notes_label.setTextInteractionFlags(
            self.notes_label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.notes_label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        notes_layout.addWidget(notes_title)
        notes_layout.addWidget(self.notes_label)
        layout.addWidget(notes_card)
        layout.addWidget(build_author_section(self))
        layout.addStretch(1)

    def check_for_updates(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_state("正在检查更新…", checking=True)

        def worker() -> None:
            try:
                result = updater.latest_release()
            except Exception as exc:  # noqa: BLE001
                logging.exception("设置页检查更新失败")
                result = exc
            self.check_finished.emit(result)

        threading.Thread(target=worker, daemon=True, name="pet-settings-update-check").start()

    def _on_check_finished(self, result) -> None:
        self._busy = False
        if isinstance(result, Exception) or result is None:
            self.release = None
            self._set_state("无法连接更新服务，请检查网络后重试。", checking=False)
            self.manual_button.setEnabled(True)
            return
        self.release = result
        tag = str(result.get("version") or "")
        self.manual_button.setEnabled(bool(result.get("html_url")))
        if not updater.is_newer(tag):
            self._set_state(f"已经是最新版本（v{PRODUCT_VERSION}）。", checking=False)
            self.notes_label.setText("当前没有可用更新。")
            return
        self.notes_label.setText(str(result.get("notes") or "暂无版本说明。"))
        self._set_state(f"发现新版本 v{tag}，点「打开下载页」取新版绿色包。", checking=False)

    def open_download_page(self) -> None:
        # 下载页固定指向作者自己的站点：GitHub Release 只放发布包，
        # 博客放软件 / 素材包 / 人物图，都是同一套东西的最新版。
        QDesktopServices.openUrl(QUrl(updater.DOWNLOAD_PAGE_URL))

    def _set_state(self, text: str, *, checking: bool) -> None:
        self.status_label.setText(text)
        self.check_button.setEnabled(not checking and not self._busy)


__all__ = ["UpdatePage"]

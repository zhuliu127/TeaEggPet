# -*- coding: utf-8 -*-
"""自拍照片弹窗：图片 + 配文 + 「再拍一张 / 关闭」。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

log = logging.getLogger(__name__)


class PhotoWindow(QWidget):
    """一张自拍的小卡片（无边框、可拖动、点关闭销毁）。"""

    def __init__(self, image_path: Path, caption: str = "", *, title: str = "她发来的自拍",
                 sub: str = "", assets: str = "",
                 on_retake: Callable[[], None] | None = None,
                 on_swap: Callable[[], None] | None = None,
                 on_library: Callable[[], None] | None = None,
                 on_closed: Callable[[QWidget], None] | None = None,
                 max_side: int = 420) -> None:
        super().__init__(None)
        self._on_closed = on_closed
        self._on_retake = on_retake
        self._on_swap = on_swap or on_library   # 旧调用方传 on_library 也还能用
        self._on_library = on_library
        self._drag_from: QPoint | None = None
        self.setObjectName("companionPhotoWindow")
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        card = QWidget(self)
        card.setObjectName("companionPhotoCard")
        card.setStyleSheet(
            "#companionPhotoCard { background: white; border: 1px solid #d9dfea; border-radius: 16px; }"
            "QLabel#companionPhotoCaption { color: #2b3350; font-size: 13px; }"
            "QLabel#companionPhotoSub { color: #66708c; font-size: 11px; }"
            "QLabel#companionPhotoAssets { color: #8a7ad0; font-size: 11px; }"
            "QPushButton { min-height: 30px; padding: 0 16px; border: 1px solid #d5dbe6; border-radius: 8px;"
            " background: #f7f9fd; color: #26304a; font-size: 13px; }"
            "QPushButton:hover { background: #eef2fb; }"
            "QPushButton#companionPhotoClose { background: #202020; color: white; border-color: #202020; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(card)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)
        card_layout.setSpacing(8)

        label = QLabel(card)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        source = QPixmap(str(image_path))
        if source.isNull():
            raise ValueError(f"读不出这张图：{image_path}")
        label.setPixmap(source.scaled(max_side, int(max_side * 1.35),
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        card_layout.addWidget(label)

        if caption:
            text = QLabel(str(caption), card)
            text.setObjectName("companionPhotoCaption")
            text.setWordWrap(True)
            text.setMaximumWidth(max_side)
            card_layout.addWidget(text)

        if sub:
            line = QLabel(str(sub), card)
            line.setObjectName("companionPhotoSub")
            line.setWordWrap(True)
            line.setMaximumWidth(max_side)
            card_layout.addWidget(line)

        if assets:
            line = QLabel("素材：" + str(assets), card)
            line.setObjectName("companionPhotoAssets")
            line.setWordWrap(True)
            line.setMaximumWidth(max_side)
            card_layout.addWidget(line)

        row = QHBoxLayout()
        row.addStretch(1)
        if callable(on_swap) or callable(on_library):
            swap_btn = QPushButton("换素材", card)
            swap_btn.setToolTip("换这一张用的参考素材（衣服 / 场景 / 道具），然后重拍")
            swap_btn.clicked.connect(self._open_swap)
            row.addWidget(swap_btn)
        if callable(on_retake):
            again = QPushButton("再拍一张", card)
            again.clicked.connect(self._retake)
            row.addWidget(again)
        close = QPushButton("关闭", card)
        close.setObjectName("companionPhotoClose")
        close.clicked.connect(self.close)
        row.addWidget(close)
        card_layout.addLayout(row)

        self.adjustSize()

    # ---------------------------------------------------------------- 交互
    def _open_swap(self) -> None:
        """换这一张的素材（不是打开素材库管理）。"""
        callback = self._on_swap
        if callable(callback):
            callback()

    def _open_library(self) -> None:
        callback = self._on_library
        if callable(callback):
            callback()

    def _retake(self) -> None:
        callback = self._on_retake
        self.close()
        if callable(callback):
            callback()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_from = None

    def closeEvent(self, event) -> None:  # noqa: N802
        if callable(self._on_closed):
            try:
                self._on_closed(self)
            except Exception:
                log.debug("照片窗关闭回调失败", exc_info=True)
        super().closeEvent(event)

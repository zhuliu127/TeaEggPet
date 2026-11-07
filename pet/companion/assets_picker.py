# -*- coding: utf-8 -*-
"""照片卡上的「换素材」：换这一张的参考素材（衣服 / 场景 / 道具），再拍一张。

用户语义（实测反馈）：照片卡上的「换素材」不是「打开素材库管理」，而是
**换掉这张图用的参考图**——比如裙子不合适换一条、场景换到卧室、加个道具。
所以这里只做三件事：列出素材库里的候选、按这张图当前用的素材预选、
确认后带着选择重拍一张。要增删条目再走「打开素材库…」。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from . import library, runtime

log = logging.getLogger(__name__)

_KEEP = "（跟随日程自动挑）"


class SwapAssetsDialog(QDialog):
    """只换这一张的素材；确认后返回 {"outfits": [...], "scene": str, "props": [...]}。"""

    def __init__(self, store, rec: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._rec = rec or {}
        self._chosen: dict[str, Any] = {}

        self.setWindowTitle("换素材重拍")
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        lib = library.scan_all(store)
        current = dict(self._rec.get("swap") or {})
        selected = dict(self._rec.get("selected") or {})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        tip = QLabel("只影响这一张。留「跟随日程」就还是按她此刻的日程自动搭配。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#66708c;font-size:12px;")
        layout.addWidget(tip)

        self.outfit = QComboBox(self)
        self.scene = QComboBox(self)
        layout.addLayout(self._row("服饰", self.outfit,
                                   _names(lib, "outfits"),
                                   current.get("outfits") or selected.get("outfits") or []))
        layout.addLayout(self._row("场景", self.scene,
                                   _names(lib, "scenes"),
                                   [current.get("scene") or selected.get("scene") or ""]))

        prop_box = QVBoxLayout()
        head = QLabel("道具（可多选）", self)
        head.setStyleSheet("color:#3a435c;font-size:12.5px;")
        prop_box.addWidget(head)
        self.props = QListWidget(self)
        self.props.setMaximumHeight(112)
        picked = {str(x) for x in (current.get("props") or selected.get("props") or [])}
        for name in _names(lib, "props"):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in picked else Qt.CheckState.Unchecked)
            self.props.addItem(item)
        if not _names(lib, "props"):
            hint = QListWidgetItem("道具库还空着")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.props.addItem(hint)
        prop_box.addWidget(self.props)
        layout.addLayout(prop_box)

        row = QHBoxLayout()
        open_lib = QPushButton("打开素材库…", self)
        open_lib.clicked.connect(self._open_library)
        row.addWidget(open_lib)
        row.addStretch(1)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("用这套重拍", self)
        ok.setDefault(True)
        ok.setStyleSheet("QPushButton { background:#202020; color:white; border-radius:8px;"
                         " padding:6px 16px; }")
        ok.clicked.connect(self._accept)
        row.addWidget(ok)
        layout.addLayout(row)

    # ------------------------------------------------------------------ 内部
    def _row(self, label: str, combo: QComboBox, names: list[str], preselected: list[str]) -> QHBoxLayout:
        row = QHBoxLayout()
        tag = QLabel(label, self)
        tag.setFixedWidth(36)
        tag.setStyleSheet("color:#3a435c;font-size:12.5px;")
        combo.addItem(_KEEP)
        combo.addItems(names)
        want = str((preselected or [""])[0] or "").strip()
        if want:
            index = combo.findText(want)
            if index >= 0:
                combo.setCurrentIndex(index)
        if not names:
            combo.setEnabled(False)
        row.addWidget(tag)
        row.addWidget(combo, 1)
        return row

    def _open_library(self) -> None:
        try:
            runtime.open_library(None)
        except Exception:  # noqa: BLE001
            log.exception("打开素材库失败")

    def _accept(self) -> None:
        outfit = self.outfit.currentText().strip()
        scene = self.scene.currentText().strip()
        props = [self.props.item(i).text() for i in range(self.props.count())
                 if self.props.item(i).checkState() == Qt.CheckState.Checked
                 and self.props.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable]
        self._chosen = {
            "outfits": [] if outfit in ("", _KEEP) else [outfit],
            "scene": "" if scene in ("", _KEEP) else scene,
            "props": props,
        }
        self.accept()

    def chosen(self) -> dict[str, Any]:
        return dict(self._chosen)


def _names(lib: dict[str, list[dict[str, Any]]], category: str) -> list[str]:
    return [str(entry.get("name") or "").strip()
            for entry in (lib.get(category) or [])
            if str(entry.get("name") or "").strip()]

# -*- coding: utf-8 -*-
"""日程 × 素材 联动表：她每一段在哪个场景、穿哪套、拿什么，一眼看清也能直接改。

这张表就是「每日日程」和「生图工作室」的交汇点：
日程决定她几点做什么，素材库决定这一段拍出来长什么样。
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime
from typing import Any

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import library, schedule
from .store import CompanionStore

log = logging.getLogger(__name__)

COLUMNS = ("时间", "活动", "地点", "场景", "穿搭", "道具", "细节")
NONE_TEXT = "（不指定）"


class ScheduleLinkPanel(QWidget):
    """一天一张表；改完点「保存」写回，下一张自拍立刻按它来。"""

    changed = Signal()

    def __init__(self, store: CompanionStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._lib: dict[str, list[dict[str, Any]]] = {}
        self._day: dict[str, Any] | None = None
        self._rows: list[dict[str, Any]] = []
        self._dirty = False
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.dateChanged.connect(lambda _d: self.reload())
        top.addWidget(QLabel("日期"))
        top.addWidget(self.date_edit)

        self.today_btn = QPushButton("今天")
        self.today_btn.clicked.connect(lambda: self.date_edit.setDate(QDate.currentDate()))
        self.tomorrow_btn = QPushButton("明天")
        self.tomorrow_btn.clicked.connect(
            lambda: self.date_edit.setDate(QDate.currentDate().addDays(1)))
        top.addWidget(self.today_btn)
        top.addWidget(self.tomorrow_btn)

        self.head_label = QLabel("")
        self.head_label.setWordWrap(True)
        top.addWidget(self.head_label, 1)
        root.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked
                                   | QTableWidget.EditTrigger.SelectedClicked)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COLUMNS.index("细节"), QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(260)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.relink_btn = QPushButton("按素材库重新匹配")
        self.relink_btn.setToolTip("把她自己没写的场景/穿搭按素材库补齐（已经填了的保留）")
        self.relink_btn.clicked.connect(self.relink)
        self.save_btn = QPushButton("保存这张表")
        self.save_btn.clicked.connect(self.save)
        self.library_btn = QPushButton("打开素材库与关键词…")
        self.library_btn.clicked.connect(self._open_library)
        self.reload_btn = QPushButton("重新读取")
        self.reload_btn.clicked.connect(self.reload)
        row.addWidget(self.relink_btn)
        row.addWidget(self.save_btn)
        row.addWidget(self.library_btn)
        row.addStretch(1)
        row.addWidget(self.reload_btn)
        root.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#66708c;")
        root.addWidget(self.status)

    # ---------------------------------------------------------------- 读
    def current_key(self) -> str:
        d = self.date_edit.date()
        return schedule.date_key(date_cls(d.year(), d.month(), d.day()))

    def reload(self) -> None:
        self._lib = library.scan_all(self.store)
        self._day = schedule.read_day(self.store, self.current_key())
        self._fill_table()

    def _names(self, category: str) -> list[str]:
        return [e["name"] for e in (self._lib.get(category) or [])]

    def _combo(self, choices: list[str], current: str, *, editable: bool = False) -> QComboBox:
        box = QComboBox()
        box.setEditable(editable)
        box.addItem(NONE_TEXT, "")
        for name in choices:
            box.addItem(name, name)
        index = box.findData(current) if current else 0
        if index < 0 and editable:
            box.setEditText(current)
        else:
            box.setCurrentIndex(max(0, index))
        if editable and current and index < 0:
            box.setEditText(current)
        box.currentIndexChanged.connect(lambda _i: self._mark_dirty())
        if editable:
            box.editTextChanged.connect(lambda _t: self._mark_dirty())
        return box

    def _fill_table(self) -> None:
        day = self._day
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._rows = []
        if not day:
            self.table.setRowCount(0)
            self.head_label.setText(self.current_key() + "　还没有日程")
            self.status.setText("点上面的「生成今天」让她想一想，或者先按素材库里的场景手写几段。")
            self.table.blockSignals(False)
            return

        items = list(day.get("items") or [])
        now_index = -1
        if self.current_key() == schedule.date_key():
            now_index = schedule.find_index(day, schedule.now_minutes())
        head = f"{schedule.date_label(day['date'])} · {(day.get('dayType') or {}).get('name', '')}"
        if day.get("title"):
            head += f"　{day['title']}"
        if day.get("mood"):
            head += f"（{day['mood']}）"
        self.head_label.setText(head)
        if not any(self._lib.get(c) for c in library.CATEGORIES):
            self.status.setText("素材库还空着：先点下面的「打开素材库与关键词…」加一个人物、几套衣服和常去的场景，"
                                "回来再点「按素材库重新匹配」。")
        elif day.get("error"):
            self.status.setText(f"上一版是模板兜底（模型没接上）：{day['error'][:120]}")
        else:
            self.status.setText("双击「活动 / 地点 / 细节」可以改字；场景、穿搭、道具从素材库里选。")

        outfits = self._names("outfits")
        scenes = self._names("scenes")
        props = self._names("props")

        for r, item in enumerate(items):
            self.table.insertRow(r)
            time_text = QTableWidgetItem(f"{item.get('start', '')}–{item.get('end', '')}")
            time_text.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(r, 0, time_text)

            for col, key, limit in ((1, "activity", 40), (2, "location", 30), (6, "detail", 240)):
                cell = QTableWidgetItem(str(item.get(key) or ""))
                cell.setToolTip(str(item.get(key) or ""))
                self.table.setItem(r, col, cell)

            scene_box = self._combo(scenes, str(item.get("scene") or ""))
            scene_box.currentIndexChanged.connect(lambda _i, row=r: self._auto_outfit(row))
            self.table.setCellWidget(r, 3, scene_box)

            outfit_current = str(item.get("outfit") or item.get("outfitNote") or "")
            outfit_box = self._combo(outfits, outfit_current, editable=True)
            outfit_box.setToolTip("可以下拉选库里的套装，也可以直接写一句「浅蓝睡裙」")
            self.table.setCellWidget(r, 4, outfit_box)

            prop_current = ""
            if isinstance(item.get("props"), list) and item["props"]:
                prop_current = str(item["props"][0])
            prop_box = self._combo(props, prop_current, editable=True)
            self.table.setCellWidget(r, 5, prop_box)

            if r == now_index:
                for col in range(len(COLUMNS)):
                    cell = self.table.item(r, col)
                    if cell is not None:
                        font = cell.font()
                        font.setBold(True)
                        cell.setFont(font)
                        cell.setBackground(QColor("#eaf1ff"))
                self.table.selectRow(r)
            self._rows.append(dict(item))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            COLUMNS.index("细节"), QHeaderView.ResizeMode.Stretch)
        self.table.blockSignals(False)
        self._dirty = False
        self.save_btn.setText("保存这张表")

    # ---------------------------------------------------------------- 联动
    def _auto_outfit(self, row: int) -> None:
        """选了场景、穿搭还空着 → 按场景默认 / 人物情境穿搭补一套。"""
        if not (0 <= row < self.table.rowCount()):
            return
        box = self.table.cellWidget(row, 4)
        if not isinstance(box, QComboBox):
            return
        if (box.currentData() or box.currentText().strip()) not in ("", NONE_TEXT):
            return
        scene_name = self._scene_of(row)
        if not scene_name:
            return
        outfit = self._outfit_for_scene(scene_name, row)
        if outfit:
            index = box.findData(outfit)
            if index >= 0:
                box.setCurrentIndex(index)
            else:
                box.setEditText(outfit)
            self.status.setText(f"按场景「{scene_name}」配上「{outfit}」")

    def _outfit_for_scene(self, scene_name: str, row: int) -> str:
        lib = self._lib
        her = schedule.her_entry(lib, self.store)
        scene = schedule.resolve_entry(lib.get("scenes") or [], scene_name)
        candidates = [
            (scene or {}).get("defaultOutfit"),
            (her or {}).get("defaultOutfit"),
        ]
        item = self._row_values(row)
        sit = schedule.guess_situation(item, [])
        for name in candidates:
            if name and schedule.resolve_entry(lib.get("outfits") or [], name):
                return schedule.resolve_entry(lib.get("outfits") or [], name)["name"]
        if sit:
            return ""
        return ""

    def _scene_of(self, row: int) -> str:
        box = self.table.cellWidget(row, 3)
        if isinstance(box, QComboBox):
            return str(box.currentData() or "")
        return ""

    def _row_values(self, row: int) -> dict[str, Any]:
        def text(col: int) -> str:
            cell = self.table.item(row, col)
            return str(cell.text()).strip() if cell is not None else ""

        outfit_box = self.table.cellWidget(row, 4)
        outfit_text = ""
        if isinstance(outfit_box, QComboBox):
            outfit_text = str(outfit_box.currentData() or outfit_box.currentText().strip())
            if outfit_text == NONE_TEXT:
                outfit_text = ""
        prop_box = self.table.cellWidget(row, 5)
        prop_text = ""
        if isinstance(prop_box, QComboBox):
            prop_text = str(prop_box.currentData() or prop_box.currentText().strip())
            if prop_text == NONE_TEXT:
                prop_text = ""
        return {
            "time": text(0),
            "activity": text(1),
            "location": text(2),
            "scene": self._scene_of(row),
            "outfit": outfit_text,
            "props": [prop_text] if prop_text else [],
            "detail": text(6),
        }

    def collect_items(self) -> list[dict[str, Any]]:
        """把表格读回成日程条目（形状和 day json 一致）。"""
        lib = self._lib
        items: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            values = self._row_values(row)
            time_text = values["time"].replace("—", "–")
            start, _, end = time_text.partition("–")
            item: dict[str, Any] = {
                "start": start.strip(),
                "end": end.strip(),
                "activity": values["activity"][:40],
                "location": values["location"][:30],
                "detail": values["detail"][:240],
            }
            if values["scene"]:
                item["scene"] = values["scene"]
            outfit_text = values["outfit"]
            if outfit_text:
                if schedule.resolve_entry(lib.get("outfits") or [], outfit_text):
                    item["outfit"] = outfit_text
                else:
                    item["outfitNote"] = outfit_text[:60]
            if values["props"]:
                item["props"] = values["props"][:3]
            items.append(item)
        return items

    def relink(self) -> None:
        """把她没写全的场景/穿搭按素材库补齐（已经填了的保留）。"""
        if not self._day:
            self.status.setText("还没有日程可以匹配。")
            return
        items = self.collect_items()
        linked = schedule.link_items(items, self.store, fill=True)
        self._rows = linked
        self._day = {**self._day, "items": linked}
        self._fill_table()
        filled = sum(1 for i in linked if i.get("scene")) + sum(1 for i in linked if i.get("outfit"))
        self.status.setText(f"按素材库重新匹配完成：{len(linked)} 段，共 {filled} 处场景/穿搭。记得点「保存这张表」。")
        self._mark_dirty()

    def save(self) -> None:
        if not self._day:
            self.status.setText("还没有日程，先点上面的「生成今天」。")
            return
        items = schedule.normalize_items(self.collect_items())
        day = {**self._day, "items": items, "editedAt": datetime.now().isoformat(timespec="seconds")}
        try:
            schedule.write_day(self.store, day)
        except Exception as exc:  # noqa: BLE001
            log.exception("保存日程失败")
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._day = day
        self._dirty = False
        self._fill_table()
        self.status.setText("已保存。下一张自拍就会按这张表来（场景 + 穿搭 + 道具当参考图）。")
        self.changed.emit()

    def regenerate(self) -> dict[str, Any] | None:
        """整天的活动重新生成（保留日期与人设）；失败抛异常。"""
        day = schedule.generate_day(self.store, self.current_key(), fallback=False)
        self._day = day
        self._fill_table()
        return day

    def refresh_library(self) -> None:
        self._lib = library.scan_all(self.store)

    def _open_library(self) -> None:
        from . import runtime

        runtime.open_library(self)

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.save_btn.setText("保存这张表 *")

    # ---------------------------------------------------------------- 交给外部
    def items_for_show(self) -> list[dict[str, Any]]:
        return list(self._day.get("items") if self._day else [])

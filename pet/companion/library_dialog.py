# -*- coding: utf-8 -*-
"""素材库与关键词管理（对应 dsh-image-studio 的「素材库 / 关键词」两页）。

左边选条目，右边改内容；参考图直接选文件复制进条目目录。
关键词按组编辑，可单组恢复默认。删除只挪进库里 `.trash`，不硬删。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import library, presets as presets_mod
from .store import CompanionStore

log = logging.getLogger(__name__)

THUMB = 76


def _split(text: str) -> list[str]:
    return [x.strip() for x in str(text or "").replace("，", ",").replace("、", ",").split(",") if x.strip()]


def _join(items) -> str:
    return "，".join(str(x) for x in items or [])


def _soft_placeholder(*widgets) -> None:
    """占位提示统一用浅灰，别让人误以为输入框里已经有内容。"""
    for widget in widgets:
        palette = widget.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#9aa3b8"))
        widget.setPalette(palette)


def _thumb(path: Path, size: int = THUMB) -> QPixmap:
    pix = QPixmap(str(path))
    if pix.isNull():
        return QPixmap()
    return pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


class LibraryTab(QWidget):
    """单个库（人物 / 服饰 / 场景 / 道具）的编辑页。"""

    def __init__(self, store: CompanionStore, category: str, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.category = category
        self.entries: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self._build()
        self.reload()

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.path_label = QLabel(str(self.store.library_dir(self.category)))
        self.path_label.setStyleSheet("color:#66708c;font-size:11px;")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(QLabel("库目录"))
        head.addWidget(self.path_label, 1)
        btn_dir = QPushButton("换目录…")
        btn_dir.clicked.connect(self._pick_dir)
        btn_open = QPushButton("打开目录")
        btn_open.clicked.connect(self._open_dir)
        btn_import = QPushButton("从 dsh-image-studio 导入…")
        btn_import.clicked.connect(self._import_existing)
        head.addWidget(btn_dir)
        head.addWidget(btn_open)
        head.addWidget(btn_import)
        root.addLayout(head)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名字 / 标签")
        self.search.textChanged.connect(self._fill_list)
        left_layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setIconSize(QSize(40, 40))
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._on_select)
        left_layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.btn_new = QPushButton("新建")
        self.btn_new.clicked.connect(self._new)
        row.addWidget(self.btn_new)
        row.addStretch(1)
        left_layout.addLayout(row)
        splitter.addWidget(left)

        right = QWidget(splitter)
        form = QFormLayout(right)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name = QLineEdit()
        form.addRow("名字", self.name)
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("用逗号隔开，模型写别名也认")
        form.addRow("别名", self.aliases)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("如：黑发，蓝眼，JK")
        form.addRow("标签", self.tags)
        self.description = QPlainTextEdit()
        self.description.setFixedHeight(84)
        self.description.setPlaceholderText("外观 / 款式 / 陈设，会写进生图提示词")
        form.addRow("外观描述", self.description)

        self.traits = QPlainTextEdit()
        self.traits.setFixedHeight(52)
        self.traits.setPlaceholderText("仅人物：开心时尾巴会摇（会写进提示词）")
        form.addRow("神态特点", self.traits)
        self.persona = QPlainTextEdit()
        self.persona.setFixedHeight(72)
        self.persona.setPlaceholderText("仅人物：性格、职业、说话方式（只给对话模型看）")
        form.addRow("人设", self.persona)
        self.default_outfit = QLineEdit()
        self.default_outfit.setPlaceholderText("服饰库里的名字")
        form.addRow("默认服饰", self.default_outfit)
        self.situation_outfits = QPlainTextEdit()
        self.situation_outfits.setFixedHeight(96)
        self.situation_outfits.setPlaceholderText("仅人物：一行一条「情境=服饰」，如 在家休息=居家针织服")
        form.addRow("情境穿搭", self.situation_outfits)

        self.images_box = QWidget()
        self.images_layout = QHBoxLayout(self.images_box)
        self.images_layout.setContentsMargins(0, 0, 0, 0)
        self.images_layout.setSpacing(6)
        self.images_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.images_box)
        scroll.setFixedHeight(THUMB + 24)
        form.addRow("参考图", scroll)

        self.usage = QLabel("")
        self.usage.setWordWrap(True)
        self.usage.setStyleSheet("color:#7a83a0;font-size:11px;")
        form.addRow("日程联动", self.usage)

        picks = QHBoxLayout()
        self.btn_add_img = QPushButton("添加图片…")
        self.btn_add_img.clicked.connect(self._add_images)
        self.btn_save = QPushButton("保存")
        self.btn_save.clicked.connect(self._save)
        self.btn_del = QPushButton("删除")
        self.btn_del.clicked.connect(self._delete)
        picks.addWidget(self.btn_add_img)
        picks.addStretch(1)
        picks.addWidget(self.btn_save)
        picks.addWidget(self.btn_del)
        form.addRow("", self._wrap(picks))

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 620])
        root.addWidget(splitter, 1)

        # 服饰/场景才有默认服饰；人物才有神态/人设/情境穿搭
        def hide_row(widget) -> None:
            widget.hide()
            label = form.labelForField(widget)
            if label is not None:
                label.hide()

        if self.category not in ("characters", "scenes"):
            hide_row(self.default_outfit)
        if self.category != "characters":
            hide_row(self.traits)
            hide_row(self.persona)
            hide_row(self.situation_outfits)
        _soft_placeholder(self.name, self.aliases, self.tags, self.description, self.traits,
                          self.persona, self.default_outfit, self.situation_outfits, self.search)

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ---------------------------------------------------------------- 数据
    def reload(self) -> None:
        library.ensure_dirs(self.store)
        self.entries = library.scan_category(self.store, self.category)
        self._fill_list()
        if self.entries:
            self.list.setCurrentRow(0)
        else:
            self._clear_form()

    def _fill_list(self) -> None:
        keyword = self.search.text().strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            haystack = " ".join([entry.get("name", ""), " ".join(entry.get("aliases") or []),
                                 " ".join(entry.get("tags") or []), entry.get("description", "")]).lower()
            if keyword and keyword not in haystack:
                continue
            item = QListWidgetItem(entry.get("name") or entry.get("id"))
            images = entry.get("images") or []
            if images:
                pix = _thumb(Path(images[0]), 40)
                if not pix.isNull():
                    item.setIcon(QIcon(pix))
            item.setData(Qt.ItemDataRole.UserRole, entry.get("id"))
            tip = entry.get("description") or ""
            if tip:
                item.setToolTip(tip[:200])
            self.list.addItem(item)
        if self.list.count() == 0:
            hint = QListWidgetItem("还空着，\n点下面的「新建」加一个")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            hint.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list.addItem(hint)
        self.list.blockSignals(False)

    def _entry_by_id(self, entry_id: str) -> dict[str, Any] | None:
        for entry in self.entries:
            if entry.get("id") == entry_id:
                return entry
        return None

    def _on_select(self, row: int) -> None:
        if row < 0:
            return
        item = self.list.item(row)
        entry = self._entry_by_id(str(item.data(Qt.ItemDataRole.UserRole))) if item else None
        if entry is None:
            return
        self.current = entry
        meta = library.entry_meta(entry)
        self.name.setText(meta["name"])
        self.aliases.setText(_join(meta["aliases"]))
        self.tags.setText(_join(meta["tags"]))
        self.description.setPlainText(meta["description"])
        self.traits.setPlainText(meta["traits"])
        self.persona.setPlainText(meta["persona"])
        self.default_outfit.setText(meta["defaultOutfit"])
        self.situation_outfits.setPlainText(
            "\n".join(f"{k}={v}" for k, v in (meta["situationOutfits"] or {}).items()))
        self._fill_images(entry)
        self._fill_usage(entry)

    def _fill_images(self, entry: dict[str, Any] | None) -> None:
        while self.images_layout.count():
            item = self.images_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        images = (entry or {}).get("images") or []
        if not images:
            hint = QLabel("还没有参考图（至少放一张，出图才会像她）")
            hint.setStyleSheet("color:#98a2bb;font-size:11px;")
            self.images_layout.addWidget(hint)
        for path in images:
            label = QLabel()
            pix = _thumb(Path(path))
            if pix.isNull():
                label.setText("读不出")
            else:
                label.setPixmap(pix)
            label.setToolTip(Path(path).name)
            self.images_layout.addWidget(label)
        self.images_layout.addStretch(1)

    def _fill_usage(self, entry: dict[str, Any] | None) -> None:
        """这个条目在最近的日程里被用到几次（反过来证明两张表是通的）。"""
        if not entry:
            self.usage.setText("")
            return

        field = {"outfits": "outfit", "scenes": "scene"}.get(entry["category"])
        if entry["category"] == "props":
            field = "props"
        if not field:
            self.usage.setText("人物条目：每张图默认都会带上，不用在日程里点名。")
            return
        name = str(entry.get("name") or "")
        days_dir = self.store.days_dir()
        files = sorted(days_dir.glob("*.json"))[-14:]
        hits = 0
        last = ""
        for path in files:
            day = self.store.read_json(path, None)
            for item in (day or {}).get("items") or []:
                if field == "props":
                    matched = name in (item.get("props") or [])
                elif field == "outfit":
                    matched = item.get("outfit") == name
                else:
                    matched = item.get("scene") == name
                if matched:
                    hits += 1
                    last = str((day or {}).get("date") or path.stem)
        if hits:
            self.usage.setText(f"最近 {len(files)} 天的日程里用到 {hits} 次（最后一次 {last}）。"
                               f"改动会立刻影响之后的照片。")
        else:
            self.usage.setText("最近 14 天的日程里还没用到它。可以在「每日日程」的表里给它安排一段。")

    def _clear_form(self) -> None:
        self.current = None
        for widget in (self.name, self.aliases, self.tags, self.default_outfit):
            widget.clear()
        for widget in (self.description, self.traits, self.persona, self.situation_outfits):
            widget.clear()
        self._fill_images(None)
        self._fill_usage(None)

    # ---------------------------------------------------------------- 动作
    def _new(self) -> None:
        self.list.clearSelection()
        self._clear_form()
        self.name.setFocus()

    def _collect(self) -> dict[str, Any]:
        outfits = {}
        for line in self.situation_outfits.toPlainText().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                if key.strip() and value.strip():
                    outfits[key.strip()] = value.strip()
        return {
            "name": self.name.text().strip(),
            "aliases": _split(self.aliases.text()),
            "tags": _split(self.tags.text()),
            "description": self.description.toPlainText().strip(),
            "traits": self.traits.toPlainText().strip(),
            "persona": self.persona.toPlainText().strip(),
            "defaultOutfit": self.default_outfit.text().strip(),
            "situationOutfits": outfits,
        }

    def _save(self) -> None:
        data = self._collect()
        if not data["name"]:
            QMessageBox.information(self, "还差一点", "先给它起个名字。")
            return
        try:
            entry = library.save_entry(self.store, self.category, self.current["id"] if self.current else None, data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.reload()
        for row in range(self.list.count()):
            if str(self.list.item(row).data(Qt.ItemDataRole.UserRole)) == entry["id"]:
                self.list.setCurrentRow(row)
                break

    def _delete(self) -> None:
        if not self.current:
            return
        name = self.current.get("name") or self.current.get("id")
        if QMessageBox.question(self, "删除条目", f"删除「{name}」？\n（会挪进库里的 .trash，不会真删）") != QMessageBox.StandardButton.Yes:
            return
        library.delete_entry(self.store, self.category, self.current["id"])
        self.reload()

    def _add_images(self) -> None:
        if not self.current:
            QMessageBox.information(self, "先保存", "先点「保存」建好这个条目，再添加参考图。")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择参考图", "", "图片 (*.png *.jpg *.jpeg *.webp *.gif)")
        if not files:
            return
        library.add_images(self.store, self.category, self.current["id"], files)
        self.reload()
        for row in range(self.list.count()):
            if str(self.list.item(row).data(Qt.ItemDataRole.UserRole)) == self.current["id"]:
                self.list.setCurrentRow(row)
                break

    def _pick_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择库目录", str(self.store.library_dir(self.category)))
        if not folder:
            return
        self.store.set(f"companion_lib_{self.category}", folder)
        self.store.save()
        self.path_label.setText(folder)
        self.reload()

    def _open_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.library_dir(self.category))))

    def _import_existing(self) -> None:
        """从已有的 dsh-image-studio 库目录导入（复制条目到当前库）。"""
        folder = QFileDialog.getExistingDirectory(self, "选择 dsh-image-studio 的库目录",
                                                  str(Path.home() / ".dsh" / "image-studio" / "library" / self.category))
        if not folder:
            return
        source = Path(folder)
        count = 0
        for child in sorted(source.iterdir() if source.is_dir() else []):
            if child.name.startswith(".") or not child.is_dir():
                continue
            meta = self.store.read_json(child / "entry.json", {}) or {}
            data = library.entry_meta({
                "name": meta.get("name") or child.name,
                "aliases": meta.get("aliases"), "tags": meta.get("tags"),
                "description": meta.get("description"), "persona": meta.get("persona"),
                "traits": meta.get("traits"), "defaultOutfit": meta.get("defaultOutfit"),
                "situationOutfits": meta.get("situationOutfits"),
            })
            files = [p for p in child.iterdir() if p.is_file() and p.suffix.lower() in library.IMAGE_EXT]
            library.save_entry(self.store, self.category, None, data, files)
            count += 1
        self.reload()
        QMessageBox.information(self, "导入完成", f"从「{source.name}」导入了 {count} 个条目。")


class KeywordTab(QWidget):
    """关键词（情境 / 神态 / 动作 / 镜头 / 光线）编辑。"""

    def __init__(self, store: CompanionStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.group = "situation"
        self.data: dict[str, list[dict[str, Any]]] = {}
        self._build()
        self.reload()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(QLabel("分组"))
        self._group_buttons: dict[str, QPushButton] = {}
        for group in presets_mod.PRESET_GROUPS:
            btn = QPushButton(presets_mod.PRESET_LABEL[group])
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, g=group: self._switch(g))
            head.addWidget(btn)
            self._group_buttons[group] = btn
        head.addStretch(1)
        btn_add = QPushButton("新增一行")
        btn_add.clicked.connect(self._add_row)
        btn_del = QPushButton("删除选中行")
        btn_del.clicked.connect(self._del_row)
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_reset = QPushButton("恢复本组默认")
        btn_reset.clicked.connect(self._reset)
        head.addWidget(btn_add)
        head.addWidget(btn_del)
        head.addWidget(btn_save)
        head.addWidget(btn_reset)
        root.addLayout(head)

        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#7d88a6;font-size:11px;")
        root.addWidget(self.hint)

        self.table = QTableWidget(0, 3)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        root.addWidget(self.table, 1)

    def _columns(self) -> list[str]:
        if self.group == "situation":
            return ["名字", "别名（逗号）", "画面描写", "默认场景", "默认服饰", "默认光线"]
        return ["名字", "别名（逗号）", "画面描写"]

    def _switch(self, group: str) -> None:
        if group == self.group:
            return
        self._read_table_into_data()
        self.group = group
        self._fill_table()

    def reload(self) -> None:
        self.data = presets_mod.load(self.store)
        self._fill_table()

    def _sync_buttons(self) -> None:
        for group, btn in self._group_buttons.items():
            btn.setChecked(group == self.group)

    def _fill_table(self) -> None:
        self._sync_buttons()
        columns = self._columns()
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        items = self.data.get(self.group) or []
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = [item.get("name", ""), _join(item.get("aliases"))]
            values.append(item.get("prompt", ""))
            if self.group == "situation":
                values += [item.get("scene", ""), item.get("outfit", ""), item.get("light", "")]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()
        if self.group == "situation":
            self.hint.setText("情境会自动带上默认场景 / 服饰 / 光线；名字要和素材库里的条目对得上。")
        else:
            self.hint.setText("模型只挑关键词名字，这里的描写决定画面。")
        self._sync_buttons()

    def _read_table_into_data(self) -> None:
        rows = []
        for row in range(self.table.rowCount()):
            def cell(col: int) -> str:
                widget = self.table.item(row, col)
                return widget.text().strip() if widget is not None else ""

            name = cell(0)
            if not name:
                continue
            item: dict[str, Any] = {"name": name, "aliases": _split(cell(1)), "prompt": cell(2)}
            if self.group == "situation":
                item["scene"] = cell(3)
                item["outfit"] = cell(4)
                item["light"] = cell(5)
            rows.append(item)
        if self.table.rowCount():
            self.data[self.group] = rows

    def _add_row(self) -> None:
        self.table.insertRow(self.table.rowCount())
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)

    def _del_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _save(self) -> None:
        self._read_table_into_data()
        presets_mod.save(self.store, self.data)
        QMessageBox.information(self, "已保存", "关键词已保存，下一张图就生效。")

    def _reset(self) -> None:
        if QMessageBox.question(self, "恢复默认", f"把「{presets_mod.PRESET_LABEL[self.group]}」恢复成内置默认？") != QMessageBox.StandardButton.Yes:
            return
        self.data = presets_mod.reset_group(self.store, self.group)
        self._fill_table()


class LibraryDialog(QDialog):
    """素材库 + 关键词 的管理窗口。"""

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.store = CompanionStore(config)
        self.setWindowTitle("素材库与关键词")
        self.setMinimumSize(920, 660)
        tabs = QTabWidget(self)
        for category in library.CATEGORIES:
            tabs.addTab(LibraryTab(self.store, category, self), library.CATEGORY_LABEL[category])
        tabs.addTab(KeywordTab(self.store, self), "关键词")
        layout = QVBoxLayout(self)
        tip = QLabel("参考图只负责「长得像」和服装设计；姿势、表情、取景由关键词决定。删除只会挪进 .trash。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#66708c;font-size:11.5px;")
        layout.addWidget(tip)
        layout.addWidget(tabs, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

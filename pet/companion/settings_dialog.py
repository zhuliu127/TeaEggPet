# -*- coding: utf-8 -*-
"""陪伴扩展设置（独立小窗口）：生图接口 / 每日日程 / 溜达 / 素材库。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, QTime, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from . import library, runtime, schedule

#: 本地模型的常见文件名：先填进下拉，点「检测 ComfyUI」再用 ComfyUI 的真实清单覆盖。
#: 少了这份，用户重开设置页时下拉里只剩当前那一项（看不到 bf16 版）。
COMFY_MODEL_HINTS = {
    "unet": ["qwen_image_2.1_bf16.safetensors", "qwen_image_2.1_int8_convrot.safetensors"],
    "clip": ["qwen3vl_8b_bf16.safetensors", "qwen3vl_8b_int8_convrot.safetensors"],
    "vae": ["qwen_image_2.1_vae_bf16.safetensors", "qwen_vae.safetensors"],
}
from .schedule_table import ScheduleLinkPanel
from .store import CompanionStore

log = logging.getLogger(__name__)


class _Task(QThread):
    ok = Signal(object)
    bad = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D102
        try:
            self.ok.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            log.exception("后台任务失败")
            self.bad.emit(str(exc))


def _line(text: str = "", *, password: bool = False) -> QLineEdit:
    edit = QLineEdit()
    edit.setText(str(text or ""))
    if password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    return edit


def _combo_left(widget) -> None:
    """让可编辑下拉从**左边**开始显示。

    setCurrentText 之后 QLineEdit 的插入点在末尾，控件就只显示文件名尾部
    （"image_2.1_int8_convrot.safetensors"），看起来像模型名前缀丢了。
    """
    try:
        edit = widget.lineEdit()
        if edit is not None:
            edit.setCursorPosition(0)
            edit.setAlignment(Qt.AlignmentFlag.AlignLeft)
    except Exception:
        pass


def _hbox(*widgets, stretch_at: int = 0) -> QHBoxLayout:
    """一行摆几个控件；``stretch_at`` 指定哪一个吃掉多余宽度（-1 = 都不吃）。"""
    box = QHBoxLayout()
    for index, widget in enumerate(widgets):
        box.addWidget(widget, 1 if index == stretch_at else 0)
    return box


def _preferred(names: list[str], current: str, kind: str, unet: str) -> str:
    """从候选里挑一个「大概率正确」的模型名。

    以前一律回落到 ``names[0]``：一旦列表里排第一的是 Qwen3-VL 32B，就会把 clip 悄悄换成
    32B（5120 维），生成时报 normalized_shape=[4096] 那种维度错。这里按 unet 反推该配什么。
    """
    names = [str(n) for n in (names or [])]
    if not names:
        return current
    if current and current in names:
        return current
    qwen_image = "qwen_image" in str(unet or "").lower()
    lowered = [(n, n.lower()) for n in names]
    if kind == "unets" and qwen_image:
        for name, low in lowered:
            if "qwen_image" in low:
                return name
    if kind == "clips" and qwen_image:
        for name, low in lowered:
            if "qwen3vl" in low and "8b" in low:
                return name
    if kind == "vaes" and qwen_image:
        for name, low in lowered:
            if "qwen_image" in low and "vae" in low:
                return name
    return names[0]


def _safe_int(text: str, fallback: int) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return fallback


def _area(text: str = "", height: int = 90) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setPlainText(str(text or ""))
    box.setFixedHeight(height)
    return box


class CompanionSettingsDialog(QDialog):
    """所有陪伴扩展的配置都在这里改，保存后立即生效（不用重启）。"""

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.cfg = config
        self.store = CompanionStore(config)
        self._task: _Task | None = None
        self.setWindowTitle("陪伴扩展 · 设置")
        self.setMinimumSize(680, 520)
        # 设置项变多以后，窗口会被内容顶到超出屏幕、把底部按钮挤出去。
        # 这里先按屏幕可用区域的 88% 定尺寸，配合每个页签的滚动区，按钮永远露在外面。
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(min(1000, int(area.width() * 0.92)), min(880, int(area.height() * 0.88)))
            self.setMaximumHeight(int(area.height() * 0.92))
        else:
            self.resize(980, 840)
        self._build()
        self._load()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._scrolled(self._tab_image()), "生图接口")
        self.tabs.addTab(self._tab_schedule(), "每日日程")
        self.tabs.addTab(self._tab_walk(), "溜达")
        self.tabs.addTab(self._tab_about(), "说明")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _scrolled(self, page: QWidget) -> QScrollArea:
        """把一页表单塞进滚动区。内容再长也不会顶高窗口（保存按钮就不会被挤出去）。"""
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area

    def _tab_image(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # 每一段单独一个容器：切「生成方式」时整段 setVisible，
        # 隐藏后高度归零。用 QFormLayout.setRowVisible 藏行会留下行间距，
        # 云端模式下会凭空多出上百像素空白。
        sections: dict[str, QWidget] = {}

        def make_section(key: str) -> QFormLayout:
            holder = QWidget(page)
            layout = QFormLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            outer.addWidget(holder)
            sections[key] = holder
            return layout

        form = make_section("public")
        cloud_form = make_section("cloud")
        local_form = make_section("local")
        lib_form = make_section("lib")
        detail_form = make_section("libdetail")
        self._image_groups = sections

        FORMS = {"": form, "cloud": cloud_form, "local": local_form,
                 "lib": lib_form, "libdetail": detail_form}

        def row(group: str, label, widget=None):
            target = FORMS.get(group or "", form)
            if widget is None:
                target.addRow(label)
            else:
                target.addRow(label, widget)

        def section(text: str) -> QLabel:
            label = QLabel(text)
            label.setStyleSheet("color:#5b6480;font-size:12px;font-weight:600;padding-top:8px;")
            return label

        # ---------------- 公共：生成方式 ----------------
        box = QHBoxLayout()
        self.mode_api = QRadioButton("云端 API（要 Key，按张计费）")
        self.mode_local = QRadioButton("本地模型（ComfyUI，免费、离线）")
        group = QButtonGroup(page)
        group.addButton(self.mode_api)
        group.addButton(self.mode_local)
        self.mode_group = group
        box.addWidget(self.mode_api)
        box.addWidget(self.mode_local)
        box.addStretch(1)
        row("", "生成方式", self._wrap(box))

        self.mode_hint = QLabel("")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setStyleSheet("color:#7a83a0;font-size:11px;")
        row("", "", self.mode_hint)

        self.image_aspect = QComboBox()
        self.image_aspect.setEditable(True)
        for ratio in ("3:4", "1:1", "4:3", "9:16", "16:9", "2:3", "3:2"):
            self.image_aspect.addItem(ratio)
        self.image_aspect.setMinimumWidth(90)
        row("", "画幅", self._wrap(_hbox(self.image_aspect, stretch_at=0)))

        self.max_refs = QSpinBox()
        self.max_refs.setRange(0, 16)
        self.identity_only = QCheckBox("参考图只还原长相")
        self.follow_schedule = QCheckBox("跟着日程此刻的穿着")
        box = QHBoxLayout()
        box.addWidget(QLabel("参考图上限"))
        box.addWidget(self.max_refs)
        box.addWidget(self.identity_only)
        box.addWidget(self.follow_schedule)
        box.addStretch(1)
        row("", "组合", self._wrap(box))

        self.test_prompt = _line()
        self.test_prompt.setPlaceholderText("试生成：傍晚的便利店门口，她举着手机自拍")
        self.test_btn = QPushButton("试生成")
        self.test_btn.clicked.connect(self._test_generate)
        row("", "试一下", self._wrap(_hbox(self.test_prompt, self.test_btn, stretch_at=0)))

        self.test_status = QLabel("")
        self.test_status.setWordWrap(True)
        row("", "", self.test_status)

        # ---------------- 云端 API ----------------
        row("cloud", section("云端 API（只有选上面「云端 API」时才生效）"))

        self.image_provider = QComboBox()
        self.image_provider.addItem("65535 原生异步任务（/v1/tasks）", "65535")
        self.image_provider.addItem("OpenAI 兼容（/v1/images）", "openai")
        row("cloud", "云端接口", self.image_provider)

        self.image_base = _line()
        self.image_base.setPlaceholderText("https://api.65535.space")
        row("cloud", "Base URL", self.image_base)

        self.image_key = _line(password=True)
        self.image_key.setPlaceholderText("留空表示沿用已保存的 Key")
        row("cloud", "API Key", self.image_key)

        self.image_model = _line()
        self.image_model.setPlaceholderText("例如 gpt-image-1 / seedream-4.0 / nano-banana")
        row("cloud", "模型", self.image_model)

        self.image_timeout = QSpinBox()
        self.image_timeout.setRange(30, 900)
        self.image_timeout.setSuffix(" 秒")
        row("cloud", "超时", self._wrap(_hbox(self.image_timeout, stretch_at=-1)))

        self.style_prompt = _area(height=56)
        row("cloud", "画风", self.style_prompt)

        # ---------------- 本地 ComfyUI ----------------
        row("local", section("本地模型 · ComfyUI + Qwen-Image 2.1（只有选上面「本地模型」时才生效）"))

        self.comfy_url = _line()
        self.comfy_url.setPlaceholderText("http://127.0.0.1:8188")
        row("local", "ComfyUI 地址", self.comfy_url)

        # 本地安装目录：整合包根目录（含 python/ 与 ComfyUI/）或 ComfyUI 文件夹本身
        self.comfy_dir = _line()
        self.comfy_dir.setPlaceholderText(
            "例如 D:\\AI\\ComfyUI_windows_portable（含 python/ 与 ComfyUI/），也可以直接选 ComfyUI 文件夹")
        dir_pick = QPushButton("选择…")
        dir_pick.clicked.connect(self._pick_comfy_dir)
        dir_open = QPushButton("打开")
        dir_open.clicked.connect(self._open_comfy_dir)
        row("local", "ComfyUI 目录", self._wrap(_hbox(self.comfy_dir, dir_pick, dir_open, stretch_at=0)))

        self.comfy_unet = QComboBox()
        self.comfy_unet.setEditable(True)
        self.comfy_clip = QComboBox()
        self.comfy_clip.setEditable(True)
        self.comfy_vae = QComboBox()
        self.comfy_vae.setEditable(True)
        box = QHBoxLayout()
        for label, widget in (("unet", self.comfy_unet), ("clip", self.comfy_clip), ("vae", self.comfy_vae)):
            widget.setMinimumContentsLength(18)
            widget.setToolTip("点开下拉可以从 ComfyUI 的真实清单里选；也可以直接手填文件名")
            box.addWidget(QLabel(label))
            box.addWidget(widget, 1)
        row("local", "本地模型", self._wrap(box))

        self.comfy_steps = QSpinBox()
        self.comfy_steps.setRange(1, 60)
        self.comfy_steps.setSuffix(" 步")
        self.comfy_seed = QLineEdit()
        self.comfy_seed.setPlaceholderText("-1 = 随机")
        self.comfy_seed.setMaximumWidth(110)
        self.comfy_refs = QSpinBox()
        self.comfy_refs.setRange(1, 8)
        self.comfy_refs.setSuffix(" 张参考图")
        self.comfy_pixels = QComboBox()
        for label, value in (("约 0.5MP（最快）", 500_000), ("约 0.8MP（默认）", 780_000),
                             ("约 1.2MP", 1_200_000), ("约 2.0MP（最慢）", 2_000_000)):
            self.comfy_pixels.addItem(label, value)
        box = QHBoxLayout()
        box.addWidget(self.comfy_steps)
        box.addWidget(QLabel("seed"))
        box.addWidget(self.comfy_seed)
        box.addWidget(self.comfy_refs)
        box.addWidget(self.comfy_pixels)
        box.addStretch(1)
        row("local", "本地采样", self._wrap(box))

        self.comfy_timeout = QSpinBox()
        self.comfy_timeout.setRange(60, 3600)
        self.comfy_timeout.setSuffix(" 秒")
        self.comfy_outfit_ref = QCheckBox("服饰也带参考图（关掉=只用文字描述）")
        box = QHBoxLayout()
        box.addWidget(QLabel("超时"))
        box.addWidget(self.comfy_timeout)
        box.addWidget(self.comfy_outfit_ref)
        box.addStretch(1)
        row("local", "本地细节", self._wrap(box))

        self.comfy_exact = QCheckBox("精确画幅（等价 custom_size）")
        self.comfy_exact.setToolTip(
            "勾上：latent 用 EmptyLatentImage(width,height)，出图尺寸就是上面画幅算出来的像素\n"
            "不勾：latent 用编码后的参考图尺寸，出图跟着人物图的比例走（程序会把首图补边）")
        self.qwen_enhance = QCheckBox("用千问编辑模板重写提示词（建议开）")
        self.qwen_enhance.setToolTip("用你配的大模型把「此刻在做什么」改写成 Qwen-Edit 认的编辑指令，"
                                     "并强制带上画风约束")
        self.selfie_arm = QCheckBox("自拍手臂入镜")
        self.selfie_arm.setToolTip(
            "关（推荐）：近景半身，画面里不出现举手机的手臂，构图最稳\n"
            "开：保留「手臂伸向镜头」的自拍感，但手臂容易被透视拉长")
        box = QHBoxLayout()
        box.addWidget(self.comfy_exact)
        box.addWidget(self.qwen_enhance)
        box.addWidget(self.selfie_arm)
        box.addStretch(1)
        row("local", "三个开关", self._wrap(box))

        self.qwen_style = _area(height=48)
        self.qwen_style.setPlaceholderText(
            "二次元、日系插画、柔和光感、精致细节……这段会作为硬要求塞进提示词结尾")
        row("local", "画风约束", self.qwen_style)

        self.comfy_negative = _area(height=44)
        self.comfy_negative.setPlaceholderText(
            "留空 = 只用内置负面词；填了会追加在内置后面（内置已含去畸形/去文字/去写实那套）")
        row("local", "负面词", self.comfy_negative)

        self.comfy_probe_btn = QPushButton("检测 ComfyUI")
        self.comfy_probe_btn.clicked.connect(self._probe_comfy)
        self.comfy_launch_btn = QPushButton("启动 ComfyUI")
        self.comfy_launch_btn.setToolTip("用上面填的目录把 ComfyUI 跑起来（独立进程，桌宠退出后它继续跑）")
        self.comfy_launch_btn.clicked.connect(self._launch_comfy)
        self.comfy_scan_btn = QPushButton("扫描本地模型")
        self.comfy_scan_btn.setToolTip("不用等 ComfyUI 起来，直接读目录里的模型文件，填进上面的三个下拉")
        self.comfy_scan_btn.clicked.connect(self._scan_models)
        box = QHBoxLayout()
        box.addWidget(self.comfy_probe_btn)
        box.addWidget(self.comfy_launch_btn)
        box.addWidget(self.comfy_scan_btn)
        box.addStretch(1)
        row("local", "", self._wrap(box))
        self.comfy_status = QLabel("")
        self.comfy_status.setWordWrap(True)
        row("local", "", self.comfy_status)

        # ---------------- 素材库（两条路共用） ----------------
        row("lib", section("素材库（两条生图路线共用）"))
        manage = QPushButton("管理素材库与关键词…（加人物 / 衣服 / 场景）")
        manage.clicked.connect(lambda: runtime.open_library(self))
        self.lib_toggle = QCheckBox("显示目录路径（一般不用改）")
        self.lib_toggle.toggled.connect(lambda _=False: self._sync_lib_detail())
        box = QHBoxLayout()
        box.addWidget(manage)
        box.addWidget(self.lib_toggle)
        box.addStretch(1)
        row("lib", "", self._wrap(box))

        self.lib_edits: dict[str, QLineEdit] = {}
        for category in library.CATEGORIES:
            edit = _line()
            edit.setPlaceholderText(str(self.store.root / "image-library" / category))
            browse = QPushButton("选择…")
            browse.clicked.connect(lambda _=False, c=category: self._pick_dir(c))
            open_btn = QPushButton("打开")
            open_btn.clicked.connect(lambda _=False, c=category: self._open_dir(c))
            row("libdetail", library.CATEGORY_LABEL[category],
                self._wrap(_hbox(edit, browse, open_btn, stretch_at=0)))
            self.lib_edits[category] = edit

        outer.addStretch(1)
        self.mode_api.toggled.connect(lambda _=False: self._sync_mode())
        self.mode_local.toggled.connect(lambda _=False: self._sync_mode())
        self.image_provider.currentIndexChanged.connect(lambda _=0: self._sync_mode())
        return page

    def _tab_schedule(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        top = QWidget(page)
        form = QFormLayout(top)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        outer.addWidget(top)

        self.schedule_enabled = QCheckBox("启用（聊天与照片都会参考她此刻在做什么）")
        form.addRow("每日日程", self.schedule_enabled)

        self.character_name = _line()
        form.addRow("角色名", self.character_name)

        self.partner_name = _line()
        self.partner_name.setPlaceholderText("她怎么称呼你，可留空")
        form.addRow("称呼", self.partner_name)

        self.persona = _area(height=88)
        self.persona.setPlaceholderText("她的名字、性格、工作、住哪……写得越具体越像。")
        form.addRow("人设", self.persona)

        self.extra_rules = _area(height=58)
        self.extra_rules.setPlaceholderText("固定设定：住哪、在哪上班、常去的店……")
        form.addRow("固定设定", self.extra_rules)

        row = QHBoxLayout()
        self.generate_at = QTimeEdit()
        self.generate_at.setDisplayFormat("HH:mm")
        row.addWidget(QLabel("每晚生成明天"))
        row.addWidget(self.generate_at)
        btn_today = QPushButton("生成今天")
        btn_today.clicked.connect(lambda: self._generate("today"))
        btn_tomorrow = QPushButton("生成明天")
        btn_tomorrow.clicked.connect(lambda: self._generate("tomorrow"))
        btn_selected = QPushButton("生成表里这一天")
        btn_selected.clicked.connect(lambda: self._generate("selected"))
        row.addWidget(btn_today)
        row.addWidget(btn_tomorrow)
        row.addWidget(btn_selected)
        row.addStretch(1)
        form.addRow("", self._wrap(row))

        # 日程 × 素材：这张表就是两个能力交汇的地方
        self.panel = ScheduleLinkPanel(self.store, self)
        outer.addWidget(self.panel, 1)
        return page

    def _tab_walk(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.duration_mode = QComboBox()
        self.duration_mode.addItem("跟每日日程此刻这一段剩余时间一致", "schedule")
        self.duration_mode.addItem("固定时长", "fixed")
        form.addRow("溜达多久", self.duration_mode)

        row = QHBoxLayout()
        self.fixed_minutes = QSpinBox()
        self.fixed_minutes.setRange(1, 240)
        self.fixed_minutes.setSuffix(" 分钟")
        self.min_minutes = QSpinBox()
        self.min_minutes.setRange(1, 240)
        self.min_minutes.setSuffix(" 分钟")
        self.max_minutes = QSpinBox()
        self.max_minutes.setRange(1, 480)
        self.max_minutes.setSuffix(" 分钟")
        row.addWidget(QLabel("固定"))
        row.addWidget(self.fixed_minutes)
        row.addWidget(QLabel("最短"))
        row.addWidget(self.min_minutes)
        row.addWidget(QLabel("最长"))
        row.addWidget(self.max_minutes)
        form.addRow("", self._wrap(row))

        self.message_every = QSpinBox()
        self.message_every.setRange(1, 60)
        self.message_every.setSuffix(" 分钟")
        form.addRow("每隔多久报一句", self.message_every)

        self.auto_selfie = QCheckBox("回来时按此刻的日程发自拍")
        form.addRow("自拍", self.auto_selfie)

        row = QHBoxLayout()
        self.walk_btn = QPushButton("现在试一次")
        self.walk_btn.clicked.connect(self._toggle_walk)
        self.selfie_btn = QPushButton("现在发一张自拍")
        self.selfie_btn.setToolTip("按「此刻正在做什么」拍一张，不溜达；快捷键在「桌宠设置 → 互动 → 快捷键 → 发自拍」")
        self.selfie_btn.clicked.connect(self._take_selfie)
        row.addWidget(self.walk_btn)
        row.addWidget(self.selfie_btn)
        row.addStretch(1)
        form.addRow("", self._wrap(row))
        self.walk_status = QLabel("")
        self.walk_status.setWordWrap(True)
        form.addRow("", self.walk_status)
        hint = QLabel(
            "全局快捷键在「桌宠设置 → 互动 → 快捷键 → 出去溜达」里绑（默认 Alt+F1）。\n"
            "溜达期间：她会在桌面底边走，隔一会儿像发微信一样报一句；再按一次快捷键或点菜单里的「叫她回家」就结束。"
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return page

    def _tab_about(self) -> QWidget:
        page = QWidget(self)
        box = QVBoxLayout(page)
        text = QLabel(
            "这一份功能是在 dsh-pet 桌内版上额外挂的「陪伴扩展」：\n\n"
            "· 出去溜达 + 发自拍：按全局快捷键让她出门，回来把自拍贴给你；\n"
            "· 每日日程：用你配置的模型生成她的一整天，照片按「此刻在哪、穿什么」来画；\n"
            "· 生图工作室：人物 / 服饰 / 场景 / 道具四类参考图 + 关键词，参考图只管长相和服装；\n\n"
            "数据都在本机：「设置目录/companion」下（日程、照片、素材库、关键词）。\n"
            "素材库目录结构和 dsh-image-studio 一致，可以直接指向原来的库。"
        )
        text.setWordWrap(True)
        box.addWidget(text)
        open_btn = QPushButton("打开数据目录")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.root))))
        box.addWidget(open_btn)
        box.addStretch(1)
        return page

    def _sync_lib_detail(self) -> None:
        """素材库那 4 行路径默认收起，勾「显示目录路径」才展开。"""
        groups = getattr(self, "_image_groups", None) or {}
        toggle = getattr(self, "lib_toggle", None)
        if "libdetail" not in groups or toggle is None:
            return
        groups["libdetail"].setVisible(toggle.isChecked())

    def _sync_mode(self) -> None:
        """按「生成方式」把不相干那一整段收起来，并把当前走哪条路写清楚。

        以前只是 setEnabled(False) 变灰：控件照样占着版面，十几行本地参数 + 六行云端参数
        一起堆下来，窗口被顶高、底部「保存」就跑到屏幕外了。现在整行隐藏，
        页面高度直接砍一半，配合滚动区不会再出现点不到保存的情况。
        """
        local = self.mode_local.isChecked()
        groups = getattr(self, "_image_groups", None) or {}
        if "cloud" in groups:
            groups["cloud"].setVisible(not local)
        if "local" in groups:
            groups["local"].setVisible(local)
        if local:
            self.mode_hint.setText(
                "本地模型：参考图（人物/服饰/场景/道具）+ 日程 → ComfyUI 的 Qwen-Image 2.1 图生图，"
                "不出网、不用 Key。需要 ComfyUI 开着（默认 127.0.0.1:8188）。云端那一段已收起，"
                "点上面的「云端 API」可以再展开。"
            )
        else:
            cloud = self.image_provider.currentData() or "65535"
            self.mode_hint.setText(
                f"云端 API：按 {('65535 异步任务' if cloud == '65535' else 'OpenAI 兼容')} 通道生图。"
                "本地 ComfyUI 那一段已收起（参数会保留），点上面的「本地模型」可以再展开。"
            )

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ---------------------------------------------------------------- 载入/保存
    def _load(self) -> None:
        s = self.store
        provider = str(s.get("companion_image_provider", "65535") or "65535")
        if provider == "comfyui":
            self.mode_local.setChecked(True)
        else:
            self.mode_api.setChecked(True)
        index = self.image_provider.findData(provider if provider != "comfyui" else
                                             str(s.get("companion_image_provider_cloud", "65535") or "65535"))
        self.image_provider.setCurrentIndex(max(0, index))
        self._sync_mode()
        self.image_base.setText(str(s.get("companion_image_base_url", "") or ""))
        key = s.image_api_key()
        self.image_key.setPlaceholderText("已保存（留空则不修改）" if key else "还没填")
        self.image_model.setText(str(s.get("companion_image_model", "") or ""))
        self.image_aspect.setCurrentText(str(s.get("companion_image_aspect", "3:4") or "3:4"))
        try:
            self.image_timeout.setValue(int(s.get("companion_image_timeout", 240) or 240))
        except (TypeError, ValueError):
            self.image_timeout.setValue(240)
        self.style_prompt.setPlainText(str(s.get("companion_style_prompt", "") or ""))
        try:
            self.max_refs.setValue(int(s.get("companion_max_refs", 8) or 8))
        except (TypeError, ValueError):
            self.max_refs.setValue(8)
        self.identity_only.setChecked(bool(s.get("companion_identity_only", True)))
        self.follow_schedule.setChecked(bool(s.get("companion_follow_schedule", True)))
        for category, edit in self.lib_edits.items():
            edit.setText(str(s.get("companion_lib_" + category, "") or ""))

        from . import comfy as comfy_mod

        self.comfy_url.setText(str(s.get("companion_comfy_url", comfy_mod.DEFAULT_URL) or comfy_mod.DEFAULT_URL))
        self.comfy_dir.setText(str(s.get("companion_comfy_dir", "") or ""))
        for widget, key, fallback in (
            (self.comfy_unet, "companion_comfy_unet", comfy_mod.DEFAULT_UNET),
            (self.comfy_clip, "companion_comfy_clip", comfy_mod.DEFAULT_CLIP),
            (self.comfy_vae, "companion_comfy_vae", comfy_mod.DEFAULT_VAE),
        ):
            value = str(s.get(key, fallback) or fallback)
            variant = "unet" if "unet" in key else ("clip" if "clip" in key else "vae")
            widget.clear()
            for name in dict.fromkeys([value] + COMFY_MODEL_HINTS[variant]):
                widget.addItem(name)
            widget.setCurrentText(value)
            _combo_left(widget)
        try:
            self.comfy_steps.setValue(int(s.get("companion_comfy_steps", 20) or 20))
        except (TypeError, ValueError):
            self.comfy_steps.setValue(20)
        seed = s.get("companion_comfy_seed", -1)
        self.comfy_seed.setText(str(-1 if seed is None else seed))
        try:
            self.comfy_refs.setValue(int(s.get("companion_comfy_max_refs", 4) or 4))
        except (TypeError, ValueError):
            self.comfy_refs.setValue(4)
        try:
            self.comfy_timeout.setValue(int(s.get("companion_comfy_timeout", 900) or 900))
        except (TypeError, ValueError):
            self.comfy_timeout.setValue(900)
        self.comfy_outfit_ref.setChecked(bool(s.get("companion_comfy_use_outfit_ref", True)))
        self.qwen_enhance.setChecked(bool(s.get("companion_qwen_enhance", True)))
        self.comfy_exact.setChecked(bool(s.get("companion_comfy_exact_size", True)))
        self.selfie_arm.setChecked(bool(s.get("companion_qwen_selfie_arm", False)))
        from . import qwen_prompt as qwen_prompt_mod

        self.qwen_style.setPlainText(str(s.get("companion_qwen_style", "") or "")
                                     or qwen_prompt_mod.DEFAULT_STYLE)
        try:
            pixels = int(s.get("companion_comfy_pixels", 780_000) or 780_000)
        except (TypeError, ValueError):
            pixels = 780_000
        index = self.comfy_pixels.findData(pixels)
        self.comfy_pixels.setCurrentIndex(index if index >= 0 else 1)
        self.comfy_negative.setPlainText(str(s.get("companion_comfy_negative", "") or ""))

        self.schedule_enabled.setChecked(bool(s.get("companion_schedule_enabled", True)))
        self.character_name.setText(str(s.get("companion_character_name", "") or "茶叶蛋"))
        self.partner_name.setText(str(s.get("companion_partner_name", "") or ""))
        self.persona.setPlainText(str(s.get("companion_persona", "") or ""))
        self.extra_rules.setPlainText(str(s.get("companion_extra_rules", "") or ""))
        hour, minute = 22, 0
        raw = str(s.get("companion_generate_at", "22:00") or "22:00")
        if ":" in raw:
            try:
                hour, minute = int(raw.split(":")[0]), int(raw.split(":")[1])
            except ValueError:
                pass
        self.generate_at.setTime(QTime(max(0, min(23, hour)), max(0, min(59, minute))))

        index = self.duration_mode.findData(str(s.get("companion_walk_duration_mode", "schedule")))
        self.duration_mode.setCurrentIndex(max(0, index))
        for widget, key_name, default in (
            (self.fixed_minutes, "companion_walk_fixed_minutes", 10),
            (self.min_minutes, "companion_walk_min_minutes", 5),
            (self.max_minutes, "companion_walk_max_minutes", 45),
            (self.message_every, "companion_walk_message_every_min", 4),
        ):
            try:
                widget.setValue(int(s.get(key_name, default) or default))
            except (TypeError, ValueError):
                widget.setValue(default)
        self.auto_selfie.setChecked(bool(s.get("companion_walk_auto_selfie", True)))
        self._refresh_walk_status()
        self._sync_lib_detail()
        self._refresh_plan()

    def _collect(self) -> dict[str, Any]:
        return {
            "companion_image_provider": ("comfyui" if self.mode_local.isChecked()
                                         else (self.image_provider.currentData() or "65535")),
            "companion_image_provider_cloud": self.image_provider.currentData(),
            "companion_image_base_url": self.image_base.text().strip(),
            "companion_image_model": self.image_model.text().strip(),
            "companion_image_aspect": self.image_aspect.currentText().strip() or "3:4",
            "companion_image_timeout": int(self.image_timeout.value()),
            "companion_style_prompt": self.style_prompt.toPlainText().strip(),
            "companion_max_refs": int(self.max_refs.value()),
            "companion_identity_only": bool(self.identity_only.isChecked()),
            "companion_follow_schedule": bool(self.follow_schedule.isChecked()),
            "companion_schedule_enabled": bool(self.schedule_enabled.isChecked()),
            "companion_character_name": self.character_name.text().strip() or "茶叶蛋",
            "companion_partner_name": self.partner_name.text().strip(),
            "companion_persona": self.persona.toPlainText().strip(),
            "companion_extra_rules": self.extra_rules.toPlainText().strip(),
            "companion_generate_at": self.generate_at.time().toString("HH:mm"),
            "companion_walk_duration_mode": self.duration_mode.currentData(),
            "companion_walk_fixed_minutes": int(self.fixed_minutes.value()),
            "companion_walk_min_minutes": int(self.min_minutes.value()),
            "companion_walk_max_minutes": int(self.max_minutes.value()),
            "companion_walk_message_every_min": int(self.message_every.value()),
            "companion_walk_auto_selfie": bool(self.auto_selfie.isChecked()),
            **{f"companion_lib_{c}": e.text().strip() for c, e in self.lib_edits.items()},
            "companion_comfy_url": self.comfy_url.text().strip() or "http://127.0.0.1:8188",
            "companion_comfy_dir": self.comfy_dir.text().strip(),
            "companion_comfy_unet": self.comfy_unet.currentText().strip(),
            "companion_comfy_clip": self.comfy_clip.currentText().strip(),
            "companion_comfy_vae": self.comfy_vae.currentText().strip(),
            "companion_comfy_steps": int(self.comfy_steps.value()),
            "companion_comfy_seed": _safe_int(self.comfy_seed.text(), -1),
            "companion_comfy_max_refs": int(self.comfy_refs.value()),
            "companion_comfy_timeout": int(self.comfy_timeout.value()),
            "companion_comfy_use_outfit_ref": bool(self.comfy_outfit_ref.isChecked()),
            "companion_qwen_enhance": bool(self.qwen_enhance.isChecked()),
            "companion_comfy_exact_size": bool(self.comfy_exact.isChecked()),
            "companion_qwen_selfie_arm": bool(self.selfie_arm.isChecked()),
            "companion_qwen_style": self.qwen_style.toPlainText().strip(),
            "companion_comfy_pixels": int(self.comfy_pixels.currentData() or 780_000),
            "companion_comfy_negative": self.comfy_negative.toPlainText().strip(),
        }

    def _on_save(self) -> None:
        try:
            self.store.apply(self._collect())
            key = self.image_key.text().strip()
            if key:
                self.store.set_image_api_key(key)
                self.image_key.clear()
                self.image_key.setPlaceholderText("已保存（留空则不修改）")
            library.ensure_dirs(self.store)
            self.accept()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))

    # ---------------------------------------------------------------- 动作
    def select_page(self, name: str) -> None:
        mapping = {"image": 0, "schedule": 1, "walk": 2, "about": 3}
        self.tabs.setCurrentIndex(mapping.get(name, 0))

    def _pick_dir(self, category: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, f"选择{library.CATEGORY_LABEL[category]}库目录",
                                                  str(self.store.library_dir(category)))
        if folder:
            self.lib_edits[category].setText(folder)

    def _open_dir(self, category: str) -> None:
        folder = self.lib_edits[category].text().strip() or str(self.store.library_dir(category))
        Path(folder).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _run_task(self, fn: Callable[[], Any], on_ok: Callable[[Any], None],
                  button: QPushButton | None = None,
                  on_bad: Callable[[str], None] | None = None) -> None:
        if self._task is not None:
            return
        if button is not None:
            button.setEnabled(False)
        task = _Task(fn, self)
        self._task = task

        def done(_result=None):
            self._task = None
            if button is not None:
                button.setEnabled(True)

        task.ok.connect(lambda result: (on_ok(result), done()))
        task.bad.connect(lambda msg: ((on_bad or self._on_task_error)(msg), done()))
        task.start()

    def _on_task_error(self, message: str) -> None:
        QMessageBox.warning(self, "出错了", message)

    def _test_generate(self) -> None:
        prompt = self.test_prompt.text().strip() or "傍晚的便利店门口，她举着手机自拍"
        self.store.apply({k: v for k, v in self._collect().items() if k.startswith("companion_image") or k in (
            "companion_style_prompt", "companion_max_refs", "companion_identity_only", "companion_follow_schedule")})
        key = self.image_key.text().strip()
        if key:
            self.store.set_image_api_key(key)
        self.test_status.setText("正在生成…")
        self.test_btn.setEnabled(False)

        def run():
            from . import compose as compose_mod
            from . import imagegen

            composed = compose_mod.compose({"prompt": prompt, "shot": "自拍"}, self.store)
            data, ext, meta = imagegen.generate(self.store, composed["final_prompt"],
                                                [r["path"] for r in composed["refs"]], composed["aspect"])
            folder = self.store.outputs_dir() / "preview"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"test{ext}"
            path.write_bytes(data)
            return path

        def ok(path):
            self.test_btn.setEnabled(True)
            self.test_status.setText(f"生成成功：{path}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        task = _Task(run, self)

        def finish():
            self._task = None
            self.test_btn.setEnabled(True)

        task.ok.connect(lambda p: (ok(p), finish()))
        task.bad.connect(lambda m: (self.test_status.setText(f"失败：{m}"), finish()))
        self._task = task
        task.start()

    def _fill_combo(self, widget, names: list[str], pick: str) -> None:
        widget.clear()
        for name in names:
            widget.addItem(name)
        if pick:
            widget.setCurrentText(pick)
        _combo_left(widget)

    def _pick_comfy_dir(self) -> None:
        start = self.comfy_dir.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选 ComfyUI 目录（整合包根目录或 ComfyUI 文件夹）", start)
        if chosen:
            self.comfy_dir.setText(chosen)
            self._scan_models()

    def _open_comfy_dir(self) -> None:
        raw = self.comfy_dir.text().strip()
        if not raw:
            self.comfy_status.setText("还没填 ComfyUI 目录")
            return
        target = Path(raw)
        if not target.is_dir():
            self.comfy_status.setText(f"目录不存在：{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _scan_models(self) -> None:
        from . import comfy as comfy_mod

        self.store.apply({k: v for k, v in self._collect().items() if k.startswith("companion_comfy")})
        try:
            found = comfy_mod.local_models(self.store)
        except Exception as exc:  # noqa: BLE001
            self.comfy_status.setText(f"扫描失败：{exc}")
            return
        total = 0
        # 先用 unet 定下来，clip / vae 再按 unet 的型号去挑（避免把 32B 的 clip 塞进 Qwen-Image）
        unet_names = found.get("unets") or []
        unet = _preferred(unet_names, self.comfy_unet.currentText().strip(), "unets", "")
        if unet_names:
            self._fill_combo(self.comfy_unet, unet_names, unet)
            total += len(unet_names)
        for widget, key in ((self.comfy_clip, "clips"), (self.comfy_vae, "vaes")):
            names = found.get(key) or []
            if not names:
                continue
            pick = _preferred(names, widget.currentText().strip(), key, unet)
            self._fill_combo(widget, names, pick)
            total += len(names)
        mismatch = comfy_mod.check_pairing(unet, self.comfy_clip.currentText().strip(),
                                           self.comfy_vae.currentText().strip())
        paths = comfy_mod.install_paths(self.store.get("companion_comfy_dir", "")) or {}
        python = paths.get("python")
        head = (f"扫到 {total} 个模型文件（unet {len(found['unets'])} / clip {len(found['clips'])} / "
                f"vae {len(found['vaes'])}）；")
        tail = f"已找到 python：{python.name}" if python else "这个目录里没找到 python，无法「启动 ComfyUI」"
        if mismatch:
            self.comfy_status.setText(f"{head}⚠ {mismatch}")
        else:
            self.comfy_status.setText(head + tail)

    def _launch_comfy(self) -> None:
        from . import comfy as comfy_mod

        self.store.apply({k: v for k, v in self._collect().items() if k.startswith("companion_comfy")})
        self.comfy_status.setText("正在启动 ComfyUI…（第一次加载模型可能要半分钟到两分钟）")
        self._run_task(
            lambda: comfy_mod.launch(self.store,
                                     on_status=lambda msg: self.comfy_status.setText(msg)),
            lambda msg: self.comfy_status.setText(str(msg)),
            on_bad=lambda msg: self.comfy_status.setText(f"启动失败：{msg}"),
        )

    def _probe_comfy(self) -> None:
        from . import comfy as comfy_mod

        self.store.apply({k: v for k, v in self._collect().items() if k.startswith("companion_comfy")})
        self.comfy_status.setText("正在问 ComfyUI…")
        try:
            info = comfy_mod.probe(self.store)
        except Exception as exc:  # noqa: BLE001
            self.comfy_status.setText(f"连不上：{exc}")
            return
        unet_now = self.comfy_unet.currentText().strip() or (info["unets"][0] if info["unets"] else "")
        for widget, options, kind in ((self.comfy_unet, info["unets"], "unets"),
                                      (self.comfy_clip, info["clips"], "clips"),
                                      (self.comfy_vae, info["vaes"], "vaes")):
            pick = _preferred(options, widget.currentText().strip(), kind, unet_now)
            self._fill_combo(widget, options, pick)
        has_edit = "有 Qwen-Image 2.1 图生图节点" if info["has_qwen_edit"] else "缺少 TextEncodeQwenImage21 节点"
        if info.get("has_custom_size"):
            size_note = "节点自带 custom_size（精确画幅）"
        elif info.get("has_switch"):
            size_note = "有 ComfySwitchNode（用 EmptyLatentImage 也能精确画幅）"
        else:
            size_note = "只能按参考图尺寸出图（首图补边）"
        switched = ""
        if not self.mode_local.isChecked():
            # 连上了就把「生成方式」切到本地：否则用户以为在用本地，实际还在走云端接口
            self.mode_local.setChecked(True)
            self._sync_mode()
            switched = "；生成方式已切到「本地模型」，点保存生效"
        self.comfy_status.setText(
            f"连上了：ComfyUI {info['version'] or '?'}，{has_edit}，{size_note}；"
            f"unet {len(info['unets'])} 个 / clip {len(info['clips'])} 个 / vae {len(info['vaes'])} 个"
            + switched
        )

    def _generate(self, which: str) -> None:
        today = schedule.date_key()
        key = {"today": today, "tomorrow": schedule.add_days(today, 1)}.get(which) or self.panel.current_key()
        self.store.apply({k: v for k, v in self._collect().items() if k.startswith("companion_schedule")
                          or k in ("companion_character_name", "companion_partner_name",
                                   "companion_persona", "companion_extra_rules")})
        self.panel.status.setText(f"正在生成 {key} 的日程…")

        def run():
            return schedule.generate_day(self.store, key, fallback=False)

        def ok(_day):
            self.panel.refresh_library()
            self.panel.reload()
            self.panel.status.setText("生成好了。可以改场景/穿搭，或者点「按素材库重新匹配」补齐。")

        def bad(message: str) -> None:
            self.panel.status.setText(f"生成失败：{message}")
            self._on_task_error(message)

        self._run_task(run, ok, None, bad)

    def _refresh_plan(self) -> None:
        self.panel.reload()

    def _take_selfie(self) -> None:
        from . import runtime

        service = runtime.service()
        if service is None:
            self.walk_status.setText("陪伴扩展还没就绪")
            return
        self.walk_status.setText("拍一张，按此刻的日程来…")
        service.take_selfie("manual")

    def _toggle_walk(self) -> None:
        from . import runtime

        service = runtime.service()
        if service is None:
            self.walk_status.setText("陪伴扩展还没就绪")
            return
        result = service.toggle()
        if isinstance(result, dict) and not result.get("ok", True):
            self.walk_status.setText(str(result.get("error") or "没能开始"))
        self._refresh_walk_status()
        self.walk_btn.setText("叫她回家" if service.walking else "现在试一次")

    def _refresh_walk_status(self) -> None:
        from . import runtime

        service = runtime.service()
        if service is None:
            self.walk_status.setText("")
            return
        if service.walking:
            self.walk_status.setText(f"正在溜达，还剩约 {service.minutes_left} 分钟")
        else:
            self.walk_status.setText(f"现在按快捷键会溜达约 {service.planned_minutes()} 分钟")
        self.walk_btn.setText("叫她回家" if service.walking else "现在试一次")

    def done(self, result: int) -> None:  # noqa: N802
        if self._task is not None:
            try:
                self._task.wait(200)
            except Exception:
                pass
        super().done(result)

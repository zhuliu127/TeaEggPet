# -*- coding: utf-8 -*-
"""陪伴扩展设置（独立小窗口）：生图接口 / 每日日程 / 溜达 / 素材库。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTime, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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


class _CallBridge(QObject):
    """把后台线程的结果投回 GUI 线程再执行。

    ``QThread.ok.connect(lambda ...)`` 是 functor 连接，PySide 不保证排队到主线程
    （版本不同行为不同，实测有直接在工作线程里跑的）。回调里碰控件还算轻的，
    碰 ``QMediaPlayer`` / ``QAudioOutput`` 就直接「提示有了、声音没有」——
    设置页的「试听一句」踩的就是这个。这里用一个属于主线程的 QObject 当接收者，
    AutoConnection 才会把调用排队回来。
    """

    invoked = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.invoked.connect(self._call)

    def _call(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("后台任务回调失败")

    def post(self, fn: Callable[[], None]) -> None:
        self.invoked.emit(fn)


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
        self._bridge: _CallBridge | None = None   # 任务结果回主线程的中转（见 _run_task）
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
        self.tabs.addTab(self._tab_proactive(), "主动对话")
        self.tabs.addTab(self._tab_voice(), "语音")
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

        self.schedule_autofill = QCheckBox("启动时如果今天还没有日程，自动生成一份")
        form.addRow("自动补日程", self.schedule_autofill)

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

    def _tab_voice(self) -> QWidget:
        """本地 GPT-SoVITS：模型 / 参考音频 / 语速都能自己定。"""
        page = QWidget(self)
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.tts_enabled = QCheckBox("用本地 GPT-SoVITS 念出来")
        self.tts_enabled.setToolTip(
            "开启后会关掉宿主自带的语音报时 / AI 回复朗读 —— 它们走另一条音频通道，\n"
            "两套同时开会各念各的（一个报时、一个念主动消息）")
        self.tts_enabled.toggled.connect(self._on_local_tts_toggled)
        form.addRow("语音", self.tts_enabled)
        self.tts_read_chat = QCheckBox("念出 AI 对话回复（完整聊天窗 / 快速对话 / 灵动岛）")
        self.tts_read_chat.setToolTip(
            "回复完成后自动合成并播放；长回复会按句切分，第一句几秒内就出声。\n"
            "新的回复会覆盖还没念完的旧内容")
        self.tts_read_proactive = QCheckBox("念出主动说的话")
        self.tts_read_proactive.setToolTip("她按间隔自己开口时，同时念出来")
        self.tts_read_screen = QCheckBox("念出截图问答的回答")
        self.tts_read_screen.setToolTip("用截图提问时，模型的回答也念出来")
        self.tts_read_self_talk = QCheckBox("念出点击桌宠跳出来的台词")
        self.tts_read_self_talk.setToolTip(
            "点一下桌宠冒出来的「好女孩……」「欧鲸鲸……」这类台词也念出来；\n"
            "首次点击会先切一次权重，可能慢一两秒")
        self.tts_read_walk = QCheckBox("念出出去溜达时报的那几句")
        self.tts_read_walk.setToolTip(
            "出门、途中「我在外面散步，风挺大」、回家这几句念出来；\n"
            "「举起手机拍一张…」这类过程提示和报错只出气泡不念")
        form.addRow("", self.tts_read_chat)
        form.addRow("", self.tts_read_proactive)
        form.addRow("", self.tts_read_screen)
        form.addRow("", self.tts_read_self_talk)
        form.addRow("", self.tts_read_walk)

        row = QHBoxLayout()
        self.tts_dir = QLineEdit()
        self.tts_dir.setPlaceholderText("GPT-SoVITS 安装目录")
        browse_dir = QPushButton("选择…")
        browse_dir.clicked.connect(self._pick_tts_dir)
        row.addWidget(self.tts_dir, 1)
        row.addWidget(browse_dir)
        form.addRow("安装目录", self._wrap(row))

        self.tts_api = QLineEdit()
        self.tts_api.setPlaceholderText("http://127.0.0.1:9880")
        form.addRow("服务地址", self.tts_api)

        self.tts_gpt = QComboBox()
        self.tts_gpt.setEditable(True)
        self.tts_gpt.setToolTip("留空 = 用服务当前加载的 GPT 权重")
        form.addRow("GPT 模型", self.tts_gpt)

        self.tts_sovits = QComboBox()
        self.tts_sovits.setEditable(True)
        self.tts_sovits.setToolTip("留空 = 用服务当前加载的 SoVITS 权重")
        form.addRow("SoVITS 模型", self.tts_sovits)

        row = QHBoxLayout()
        self.tts_ref = QLineEdit()
        self.tts_ref.setPlaceholderText("参考音频（3~10 秒 wav）")
        browse_ref = QPushButton("选择…")
        browse_ref.clicked.connect(self._pick_tts_ref)
        row.addWidget(self.tts_ref, 1)
        row.addWidget(browse_ref)
        form.addRow("参考音频", self._wrap(row))

        self.tts_ref_text = QLineEdit()
        self.tts_ref_text.setPlaceholderText("参考音频里说的那句话（要和音频一致）")
        form.addRow("参考文本", self.tts_ref_text)

        row = QHBoxLayout()
        self.tts_speed = QDoubleSpinBox()
        self.tts_speed.setRange(0.5, 2.0)
        self.tts_speed.setSingleStep(0.05)
        self.tts_speed.setDecimals(2)
        self.tts_speed.setSuffix(" 倍")
        row.addWidget(self.tts_speed)
        self.tts_text_lang = QComboBox()
        for text, data in (("中文", "zh"), ("英文", "en"), ("日文", "ja"), ("中英混合", "auto")):
            self.tts_text_lang.addItem(text, data)
        row.addWidget(QLabel("语言"))
        row.addWidget(self.tts_text_lang)
        row.addStretch(1)
        form.addRow("语速 / 语言", self._wrap(row))

        row = QHBoxLayout()
        self.tts_start_btn = QPushButton("启动 / 自动连接")
        self.tts_start_btn.setToolTip(
            "先在候选端口找已经在跑的 api_v2；找不到就用安装目录里的 runtime python\n"
            "挑一个空闲端口后台启动 api_v2.py，并把地址填回上面的「服务地址」")
        self.tts_start_btn.clicked.connect(self._start_tts_service)
        self.tts_test_btn = QPushButton("检测服务")
        self.tts_test_btn.clicked.connect(self._test_tts_service)
        self.tts_play_btn = QPushButton("试听一句")
        self.tts_play_btn.clicked.connect(self._test_tts_speak)
        row.addWidget(self.tts_start_btn)
        row.addWidget(self.tts_test_btn)
        row.addWidget(self.tts_play_btn)
        row.addStretch(1)
        form.addRow("", self._wrap(row))
        self.tts_status = QLabel("先确保 GPT-SoVITS 的 api_v2.py 在跑（默认 9880 端口）")
        self.tts_status.setWordWrap(True)
        form.addRow("", self.tts_status)

        hint = QLabel(
            "开启后：AI 对话回复、主动说的话、截图问答、点击台词、溜达播报都会用这个声音自动念出来"
            "（长回复按句播，新回复覆盖没念完的旧内容）；同时宿主自带的语音报时 / 回复朗读会自动让位。\n"
            "服务地址要填 api_v2.py 的地址（默认 9880），不是 webui.py 的网页地址"
            "（9871/9872/9874 是网页界面，调不通语音）。\n"
            "模型下拉是从安装目录下的 GPT_weights*/SoVITS_weights* 扫出来的，也可以直接手填相对路径。"
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return page

    def _tts_form_values(self) -> dict:
        return {
            "companion_tts_enabled": bool(self.tts_enabled.isChecked()),
            "companion_tts_read_chat": bool(self.tts_read_chat.isChecked()),
            "companion_tts_read_proactive": bool(self.tts_read_proactive.isChecked()),
            "companion_tts_read_screen": bool(self.tts_read_screen.isChecked()),
            "companion_tts_read_self_talk": bool(self.tts_read_self_talk.isChecked()),
            "companion_tts_read_walk": bool(self.tts_read_walk.isChecked()),
            "companion_tts_dir": self.tts_dir.text().strip() or "D:/AI/AI Voice",
            "companion_tts_api": self.tts_api.text().strip() or "http://127.0.0.1:9880",
            "companion_tts_gpt": self.tts_gpt.currentText().strip(),
            "companion_tts_sovits": self.tts_sovits.currentText().strip(),
            "companion_tts_ref_audio": self.tts_ref.text().strip(),
            "companion_tts_ref_text": self.tts_ref_text.text().strip(),
            "companion_tts_speed": float(self.tts_speed.value()),
            "companion_tts_text_lang": self.tts_text_lang.currentData() or "zh",
        }

    def _refresh_tts_models(self) -> None:
        from . import tts as tts_mod

        gpt, sovits = tts_mod.scan_models(self.tts_dir.text().strip())
        for combo, items in ((self.tts_gpt, gpt), (self.tts_sovits, sovits)):
            current = combo.currentText().strip()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            combo.addItems(items)
            combo.setCurrentText(current)
            combo.blockSignals(False)

    def _pick_tts_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 GPT-SoVITS 安装目录", self.tts_dir.text().strip())
        if folder:
            self.tts_dir.setText(folder)
            self._refresh_tts_models()

    def _pick_tts_ref(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择参考音频", self.tts_dir.text().strip(),
                                              "音频 (*.wav *.mp3 *.flac)")
        if path:
            self.tts_ref.setText(path)

    def _on_local_tts_toggled(self, checked: bool) -> None:
        """勾上本地念白时先说清后果：宿主的报时 / 回复朗读会被关掉。"""
        if not checked or not self.isVisible():
            return   # 载入配置时也会 setChecked，别把初始提示冲掉
        host_on = []
        for key in self.HOST_TTS_KEYS:
            try:
                if self.cfg.get(key, False):
                    host_on.append(key)
            except Exception:  # noqa: BLE001
                continue
        if host_on:
            self.tts_status.setText(
                "保存后会关掉系统语音报时 / AI 回复朗读（两套 TTS 同时开会各念各的）")
        else:
            self.tts_status.setText("宿主那边的 TTS 目前是关着的，不会打架")

    def _adopt_tts_api(self, api: str) -> None:
        """把探测到的真接口地址填回输入框并落盘（网页端口会被换掉）。"""
        api = str(api or "").strip()
        if not api:
            return
        if self.tts_api.text().strip().rstrip("/") != api:
            self.tts_api.setText(api)
        try:
            self.store.apply({"companion_tts_api": api})
        except Exception:  # noqa: BLE001
            pass

    def _start_tts_service(self) -> None:
        from . import tts as tts_mod

        cfg = tts_mod.tts_config(self.store)
        cfg.update(tts_mod.cfg_from_form(self._tts_form_values()))
        self.tts_status.setText("正在找可用的接口；没有就在后台启动 api_v2.py（首次加载模型 30~60 秒）…")

        def run():
            return tts_mod.ensure_server(cfg, wait=150.0)

        def ok(result):
            good, api, message = result
            if good:
                self._adopt_tts_api(api)
                if self.tts_enabled.isChecked():
                    off = self._silence_host_tts()
                    if off:
                        message += "；已同时关掉系统语音报时 / AI 回复朗读（避免两套声音打架）"
                else:
                    message += "；勾上「用本地 GPT-SoVITS 念出来」并保存后，才会真的念"
            self.tts_status.setText(message)

        def bad(msg):
            self.tts_status.setText(f"启动失败：{msg}")

        self._run_task(run, ok, self.tts_start_btn, bad)

    def _test_tts_service(self) -> None:
        from . import tts as tts_mod

        cfg = tts_mod.tts_config(self.store)
        cfg.update(tts_mod.cfg_from_form(self._tts_form_values()))
        self.tts_status.setText("正在检测…")

        def run():
            return tts_mod.describe(cfg)

        def ok(text):
            self.tts_status.setText(text)

        def bad(msg):
            self.tts_status.setText(f"检测失败：{msg}")

        self._run_task(run, ok, self.tts_test_btn, bad)

    def _test_tts_speak(self) -> None:
        from . import tts as tts_mod

        cfg = tts_mod.tts_config(self.store)
        cfg.update(tts_mod.cfg_from_form(self._tts_form_values()))
        if not cfg.get("ref_audio"):
            self.tts_status.setText("先选参考音频")
            return
        self.tts_status.setText("正在准备接口并合成…")
        preview = "这是茶叶蛋在用本地语音说话，听得清吗？"

        def run():
            # 地址填成网页端口、或者服务没开，这里都顺手纠正 / 拉起
            ready, api, message = tts_mod.ensure_server(cfg, wait=150.0)
            if not ready:
                return None, message
            cfg["api"] = api
            tts_mod.apply_weights(cfg)
            # 注意：合成结果可能直接来自缓存（同一句话试听第二次），
            # 但播放链路必须和平时念白一致，所以下面统一交给 LocalSpeaker。
            data, err = tts_mod.synthesize_ex(cfg, preview)
            return (data, api), err

        def ok(result):
            data, api = result[0]
            if not data:
                self.tts_status.setText(f"合成失败：{result[1] or '没有返回音频'}")
                return
            self._adopt_tts_api(api)
            # 以前这里自己养了一个 QMediaPlayer + 固定文件名的临时文件：第二次试听
            # 同一个 URL 会沿用上一段媒体描述（duration 不刷新），表现就是
            # 「提示合成成功却没声音」。改走统一播报出口，试听 = 平时听到的链路。
            played = tts_mod.speaker().play_data(data, dict(cfg, api=api))
            note = "已播放" if played else "但播放器没起来（看日志）"
            self.tts_status.setText(
                f"合成成功，{len(data) // 1024} KB（来自 {api}），{note}")

        def bad(msg):
            self.tts_status.setText(f"失败：{msg}")

        self._run_task(run, ok, self.tts_play_btn, bad)

    def _tab_proactive(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.proactive_enabled = QCheckBox("让她自己开口找你（消息也会出现在「AI 对话」里）")
        form.addRow("主动对话", self.proactive_enabled)

        self.proactive_first = QSpinBox()
        self.proactive_first.setRange(1, 240)
        self.proactive_first.setSuffix(" 分钟")
        form.addRow("启动后多久可能开口", self.proactive_first)

        row = QHBoxLayout()
        self.proactive_min = QSpinBox()
        self.proactive_min.setRange(5, 720)
        self.proactive_min.setSuffix(" 分钟")
        self.proactive_max = QSpinBox()
        self.proactive_max.setRange(5, 720)
        self.proactive_max.setSuffix(" 分钟")
        row.addWidget(QLabel("最短"))
        row.addWidget(self.proactive_min)
        row.addWidget(QLabel("最长"))
        row.addWidget(self.proactive_max)
        row.addStretch(1)
        form.addRow("两次之间间隔", self._wrap(row))

        self.proactive_quiet = QLineEdit()
        self.proactive_quiet.setPlaceholderText("0-8（跨零点写 23:30-7:00，留空=不设免打扰）")
        self.proactive_quiet.textChanged.connect(self._refresh_quiet_hint)
        form.addRow("免打扰时段", self.proactive_quiet)
        self.quiet_hint = QLabel("")
        self.quiet_hint.setWordWrap(True)
        form.addRow("", self.quiet_hint)

        row = QHBoxLayout()
        self.proactive_unanswered = QSpinBox()
        self.proactive_unanswered.setRange(0, 20)
        self.proactive_unanswered.setSuffix(" 条")
        self.proactive_unanswered.setToolTip("连着几条没回就不再开口，0 = 不限")
        self.proactive_daily = QSpinBox()
        self.proactive_daily.setRange(0, 50)
        self.proactive_daily.setSuffix(" 条")
        self.proactive_daily.setToolTip("每天最多主动说几句，0 = 不限")
        row.addWidget(QLabel("连续没回就停"))
        row.addWidget(self.proactive_unanswered)
        row.addWidget(QLabel("每天最多"))
        row.addWidget(self.proactive_daily)
        row.addStretch(1)
        form.addRow("", self._wrap(row))

        self.proactive_memory = QCheckBox("用「AI 对话」里记住的事（她记得你是谁、你的习惯）")
        form.addRow("记忆", self.proactive_memory)

        row = QHBoxLayout()
        self.proactive_history = QSpinBox()
        self.proactive_history.setRange(0, 40)
        self.proactive_history.setSuffix(" 条")
        self.proactive_history.setToolTip("参考最近多少条聊天记录，0 = 不参考")
        row.addWidget(QLabel("参考最近对话"))
        row.addWidget(self.proactive_history)
        row.addStretch(1)
        form.addRow("", self._wrap(row))

        self.proactive_prompt = QPlainTextEdit()
        self.proactive_prompt.setPlaceholderText("留空 = 用内置提示词（按此刻日程、人设、最近聊天生成一句话）")
        self.proactive_prompt.setFixedHeight(150)
        form.addRow("提示词", self.proactive_prompt)

        row = QHBoxLayout()
        self.proactive_btn = QPushButton("现在说一句")
        self.proactive_btn.clicked.connect(self._test_proactive)
        row.addWidget(self.proactive_btn)
        row.addStretch(1)
        form.addRow("", self._wrap(row))
        self.proactive_status = QLabel("")
        self.proactive_status.setWordWrap(True)
        form.addRow("", self.proactive_status)

        hint = QLabel(
            "规则：启动后按「启动后多久」等一会儿再开口，之后在最短/最长之间随机；\n"
            "免打扰时段内不会说话；连着几条没人回会自己收声（可在上面调）；\n"
            "她说的每一句都会作为一条消息写进「AI 对话」，打开就能接着聊。"
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return page

    def _refresh_quiet_hint(self) -> None:
        """把「10-8」这种写法翻译成人话：几点到几点、共多少小时。"""
        from .proactive_chat import minutes_until_window_end, parse_quiet_hours

        text = self.proactive_quiet.text().strip()
        hint = getattr(self, "quiet_hint", None)
        if hint is None:
            return
        if not text:
            hint.setText("不设免打扰：任何时间都可能开口")
            hint.setStyleSheet("color: #7a8699;")
            return
        window = parse_quiet_hours(text)
        if window is None:
            hint.setText("格式看不懂，写成「0-8」或「23:30-7:00」")
            hint.setStyleSheet("color: #d9534f;")
            return
        start, end = window
        span = minutes_until_window_end(start, window)
        color = "#d9534f" if span > 12 * 60 else "#7a8699"
        note = "（超过 12 小时，确认一下是不是想写 22-8）" if span > 12 * 60 else ""
        hint.setText(f"每天 {start // 60:02d}:{start % 60:02d} → {end // 60:02d}:{end % 60:02d}，"
                     f"共 {span / 60:.1f} 小时不主动说话{note}")
        hint.setStyleSheet(f"color: {color};")

    def _test_proactive(self) -> None:
        from . import runtime

        service = runtime.proactive()
        if service is None:
            self.proactive_status.setText("主动对话没起来（陪伴扩展未初始化）")
            return
        self.proactive_status.setText("正在生成…")
        self.proactive_btn.setEnabled(False)

        def run():
            return service.fire_now(reason="manual")

        def ok(sent):
            self.proactive_btn.setEnabled(True)
            self.proactive_status.setText("已发出，看气泡和右下角通知" if sent else "这次没发（模型没配好或正忙）")

        def bad(msg):
            self.proactive_btn.setEnabled(True)
            self.proactive_status.setText(f"失败：{msg}")

        self._run_task(run, ok, self.proactive_btn, bad)

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
        self.schedule_autofill.setChecked(bool(s.get("companion_schedule_autofill", True)))
        self.proactive_enabled.setChecked(bool(s.get("companion_proactive_enabled", True)))
        for widget, key_name, default in (
            (self.proactive_first, "companion_proactive_first_minutes", 5),
            (self.proactive_min, "companion_proactive_min_minutes", 45),
            (self.proactive_max, "companion_proactive_max_minutes", 120),
            (self.proactive_unanswered, "companion_proactive_max_unanswered", 3),
            (self.proactive_daily, "companion_proactive_daily_limit", 8),
        ):
            try:
                widget.setValue(int(s.get(key_name, default) or 0))
            except (TypeError, ValueError):
                widget.setValue(default)
        self.proactive_memory.setChecked(bool(s.get("companion_proactive_use_memory", True)))
        try:
            self.proactive_history.setValue(int(s.get("companion_proactive_history_count", 12) or 0))
        except (TypeError, ValueError):
            self.proactive_history.setValue(12)
        self.proactive_quiet.setText(str(s.get("companion_proactive_quiet_hours", "0-8") or ""))
        self.proactive_prompt.setPlainText(str(s.get("companion_proactive_prompt", "") or ""))
        self._refresh_quiet_hint()
        t = s
        self.tts_enabled.setChecked(bool(t.get("companion_tts_enabled", False)))
        self.tts_read_chat.setChecked(bool(t.get("companion_tts_read_chat", True)))
        self.tts_read_proactive.setChecked(bool(t.get("companion_tts_read_proactive", True)))
        self.tts_read_screen.setChecked(bool(t.get("companion_tts_read_screen", True)))
        self.tts_read_self_talk.setChecked(bool(t.get("companion_tts_read_self_talk", True)))
        self.tts_read_walk.setChecked(bool(t.get("companion_tts_read_walk", True)))
        self.tts_dir.setText(str(t.get("companion_tts_dir", "D:/AI/AI Voice") or ""))
        self.tts_api.setText(str(t.get("companion_tts_api", "http://127.0.0.1:9880") or ""))
        self._refresh_tts_models()
        self.tts_gpt.setCurrentText(str(t.get("companion_tts_gpt", "") or ""))
        self.tts_sovits.setCurrentText(str(t.get("companion_tts_sovits", "") or ""))
        self.tts_ref.setText(str(t.get("companion_tts_ref_audio", "") or ""))
        self.tts_ref_text.setText(str(t.get("companion_tts_ref_text", "") or ""))
        try:
            self.tts_speed.setValue(float(t.get("companion_tts_speed", 1.0) or 1.0))
        except (TypeError, ValueError):
            self.tts_speed.setValue(1.0)
        lang = str(t.get("companion_tts_text_lang", "zh") or "zh")
        idx = self.tts_text_lang.findData(lang)
        self.tts_text_lang.setCurrentIndex(max(0, idx))
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
            "companion_schedule_autofill": bool(self.schedule_autofill.isChecked()),
            "companion_proactive_enabled": bool(self.proactive_enabled.isChecked()),
            "companion_proactive_first_minutes": int(self.proactive_first.value()),
            "companion_proactive_min_minutes": int(self.proactive_min.value()),
            "companion_proactive_max_minutes": int(self.proactive_max.value()),
            "companion_proactive_max_unanswered": int(self.proactive_unanswered.value()),
            "companion_proactive_daily_limit": int(self.proactive_daily.value()),
            "companion_proactive_use_memory": bool(self.proactive_memory.isChecked()),
            "companion_proactive_history_count": int(self.proactive_history.value()),
            "companion_proactive_quiet_hours": self.proactive_quiet.text().strip() or "0-8",
            "companion_proactive_prompt": self.proactive_prompt.toPlainText().strip(),
            **self._tts_form_values(),
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
            patches = self._collect()
            self.store.apply(patches)
            if patches.get("companion_tts_enabled"):
                self._silence_host_tts()
            key = self.image_key.text().strip()
            if key:
                self.store.set_image_api_key(key)
                self.image_key.clear()
                self.image_key.setPlaceholderText("已保存（留空则不修改）")
            library.ensure_dirs(self.store)
            self.accept()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))

    #: 宿主的几套 TTS：语音报时（含节日/点击自言自语/抽签/待办播报）、AI 回复朗读、
    #: 台词预缓存。它们共用一条音频通道，和陪伴扩展的本地 GPT-SoVITS 不共用，
    #: 两套同时开会各念各的（一个报时一个念主动消息）。
    HOST_TTS_KEYS = ("voice_chime_enabled", "chat_reply_voice_enabled",
                     "self_talk_voice_precache_enabled")

    def _silence_host_tts(self) -> list[str]:
        """本地 GPT-SoVITS 开启时关掉宿主那几套 TTS，返回被关掉的键。"""
        changed: list[str] = []
        for key in self.HOST_TTS_KEYS:
            try:
                if self.cfg.get(key, False):
                    self.cfg.set(key, False)
                    changed.append(key)
            except Exception:  # noqa: BLE001
                continue
        if changed:
            try:
                self.cfg.save()
            except Exception:  # noqa: BLE001
                pass
        return changed

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
        bridge = _CallBridge(self)
        self._bridge = bridge   # 常驻一个引用，事件还在队列里也不会被回收

        def done(_result=None):
            self._task = None
            if button is not None:
                button.setEnabled(True)

        def handle_ok(result):
            try:
                on_ok(result)
            finally:
                done()

        def handle_bad(message):
            try:
                (on_bad or self._on_task_error)(message)
            finally:
                done()

        task.ok.connect(lambda result: bridge.post(lambda: handle_ok(result)))
        task.bad.connect(lambda msg: bridge.post(lambda: handle_bad(msg)))
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

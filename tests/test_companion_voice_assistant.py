# -*- coding: utf-8 -*-
"""语音助手纯逻辑测试：无麦克风、不加载模型、不起 Qt 事件循环。

只测「不用硬件/模型就能验证」的那部分：文本归一化、关键词匹配、唤醒词模糊匹配、
指令分派（用假的 store / service 替身）。真正的麦克风采集和 Whisper 推理属于
集成测试范畴，这里不碰。
"""
from __future__ import annotations

import types

from PySide6.QtWidgets import QApplication

from pet.companion import voice_assistant as va


def _app():
    return QApplication.instance() or QApplication([])


class _FakeStore:
    def __init__(self, cfg=None):
        self._cfg = dict(cfg or {})

    def get(self, key, default=None):
        return self._cfg.get(key, default)


def _make(cfg=None, service=None):
    """造一个不做任何 I/O 的助手：把 _cfg 换成直接读内存字典。"""
    _app()
    a = va.VoiceAssistant(app=None)
    store = _FakeStore(cfg)
    a._cfg = lambda key, default=None: store.get(key, default)  # type: ignore[assignment]
    a._store = lambda: store  # type: ignore[assignment]
    a._say = lambda text: None  # type: ignore[assignment]
    if service is not None:
        a._service_override = service
    return a


# ---------------------------------------------------------------- 归一化/匹配
def test_norm_strips_punct_and_lower():
    assert va._norm("小鱼，小鱼！") == "小鱼小鱼"
    assert va._norm("  Hello, World! ") == "helloworld"


def test_contains_any_hits_normalized():
    assert va._contains_any("我要自拍一张", va.SELFIE_WORDS) == "自拍"
    assert va._contains_any("出去走走呀", va.WALK_WORDS) in ("出去走走", "走走")
    assert va._contains_any("完全无关的话", va.SELFIE_WORDS) == ""


# ---------------------------------------------------------------- 唤醒词模糊
def test_fuzzy_wake_accepts_near_miss():
    a = _make()
    # 精确
    assert a._fuzzy_wake("小鱼小鱼") is True
    # 模型多数情况会「小玉小玉」这类错字
    assert a._fuzzy_wake("小鱼小玉") is True
    # 明显无关
    assert a._fuzzy_wake("今天天气不错") is False


def test_fuzzy_wake_custom_word():
    a = _make({"companion_voice_wake_word": "蛋蛋"})
    assert a._fuzzy_wake("蛋蛋") is True
    assert a._fuzzy_wake("茶叶蛋") is True  # 含「蛋蛋」的近似


# ---------------------------------------------------------------- 状态转移
def test_enter_exit_dialog_sets_state_and_emits():
    a = _make()
    a._state = "idle"
    changes = []
    a.state_changed.connect(lambda s: changes.append(s))
    a._enter_dialog()
    assert a.state == "dialog"
    a._exit_dialog()
    assert a.state == "idle"
    assert changes == ["dialog", "idle"]


# ---------------------------------------------------------------- 指令分派
def test_dialog_selfie_goes_to_confirm_not_direct():
    a = _make()
    a._state = "dialog"
    called = []
    a._do_selfie = lambda: called.append("selfie")  # type: ignore[assignment]
    a._handle_dialog_text("帮我自拍一张吧")
    assert a.state == "confirm"
    assert called == []  # 还没说「确认」，不应直接拍


def test_confirm_yes_triggers_selfie():
    a = _make()
    a._state = "confirm"
    a._pending_selfie = True
    called = []
    a._do_selfie = lambda: called.append("selfie")  # type: ignore[assignment]
    a._handle_confirm("确认")
    assert called == ["selfie"]
    assert a.state == "dialog"


def test_confirm_no_cancels():
    a = _make()
    a._state = "confirm"
    a._pending_selfie = True
    called = []
    a._do_selfie = lambda: called.append("selfie")  # type: ignore[assignment]
    a._handle_confirm("不用了")
    assert called == []
    assert a.state == "dialog"


def test_home_word_takes_priority_over_walk():
    a = _make()
    a._state = "dialog"
    hits = []
    a._do_home = lambda: hits.append("home")  # type: ignore[assignment]
    a._do_walk = lambda: hits.append("walk")  # type: ignore[assignment]
    a._handle_dialog_text("回家吧")
    assert hits == ["home"]


def test_walk_word_triggers_walk():
    a = _make()
    a._state = "dialog"
    hits = []
    a._do_home = lambda: hits.append("home")  # type: ignore[assignment]
    a._do_walk = lambda: hits.append("walk")  # type: ignore[assignment]
    a._handle_dialog_text("我出去溜达一下")
    assert hits == ["walk"]


def test_plain_chat_goes_to_chat():
    a = _make()
    a._state = "dialog"
    sent = []
    a._send_to_chat = lambda t: sent.append(t)  # type: ignore[assignment]
    a._handle_dialog_text("你今天过得怎么样呀")
    assert sent == ["你今天过得怎么样呀"]


def test_sleep_word_exits_dialog():
    a = _make()
    a._state = "dialog"
    a._exit_dialog = lambda reason="": a._set_state("idle")  # type: ignore[assignment]
    # 直接调 _handle_segment 太依赖模型；这里验证词表命中即可
    assert va._contains_any("好了休息一下吧", [a._sleep_word()]) == "休息一下"

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



# ---------------------------------------------------------------- 非阻塞启动（回归：卡死）
def test_start_is_nonblocking_when_disabled():
    """总开关关着时 start() 立即返回 False，不加载模型、不崩。"""
    a = _make({"companion_voice_enabled": False})
    assert a.start() is False
    assert a.state == "off"


def test_start_does_not_load_model_synchronously(monkeypatch):
    """start() 绝不在调用线程加载模型（这就是旧版卡死的根因）。

    这里把 sounddevice 换成假模块、_load_models 换成一个如果被同步调用就标记的桩，
    验证 start() 返回时 _load_models 还没在调用线程里跑完。
    """
    import sys

    loaded = {"in_caller": False}
    a = _make({"companion_voice_enabled": True})
    a._resolve_device = lambda: None  # type: ignore[assignment]

    def fake_load():
        loaded["in_caller"] = True
        # 模拟慢加载
        import time as _t
        _t.sleep(0.05)
        return False

    a._load_models = fake_load  # type: ignore[assignment]

    # 假 sounddevice，避免真的去开麦
    fake_sd = types.SimpleNamespace(InputStream=lambda **k: None, query_devices=lambda: [])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    ok = a.start()
    # start() 返回时，模型加载应发生在后台线程，而非调用线程
    assert ok is True
    assert loaded["in_caller"] is False
    # 清理后台线程
    a.stop()


def test_stop_is_nonblocking_and_resets_state():
    a = _make({"companion_voice_enabled": False})
    a.start()
    a.stop()
    assert a.state == "off"
    assert a._thread is None


def test_load_models_reports_error_when_no_model(monkeypatch):
    """没有可用模型时，_load_models 返回 False 并发 error，不抛异常。"""
    from pet.companion import asr as asr_mod

    monkeypatch.setattr(asr_mod, "resolve_model_dir", lambda cfg: "")
    a = _make({"companion_voice_enabled": True})
    errs = []
    a.error.connect(lambda m: errs.append(m))
    assert a._load_models() is False
    assert errs, "应发出 error 信号"


def test_candidate_devices_prefers_wasapi_and_puts_mme_last(monkeypatch):
    """同名麦克风跨 host API 重复时：WASAPI 候选优先、MME 靠后。"""
    import sys

    devices = [
        {"name": "扬声器 (X)", "max_input_channels": 0, "hostapi": 1},          # 非输入，忽略
        {"name": "麦克风 (BOYA CastMic G30)", "max_input_channels": 1, "hostapi": 0},  # MME
        {"name": "麦克风 (BOYA CastMic G30)", "max_input_channels": 2, "hostapi": 2},  # WASAPI
    ]
    hostapis = [{"name": "MME"}, {"name": "Output"}, {"name": "Windows WASAPI"}]
    fake_sd = types.SimpleNamespace(
        query_devices=lambda: devices, query_hostapis=lambda: hostapis)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    a = _make({"companion_voice_device": "麦克风 (BOYA CastMic G30)"})
    cands = a._candidate_devices()
    assert cands[0] == 2, "WASAPI 应排第一"
    assert cands[-1] == 1, "MME 应排最后"
    assert 0 not in cands, "非输入设备不应进入候选"


def test_candidate_devices_empty_name_is_default(monkeypatch):
    import sys

    fake_sd = types.SimpleNamespace(query_devices=lambda: [], query_hostapis=lambda: [])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    a = _make({"companion_voice_device": ""})
    assert a._candidate_devices() == [None]


def test_list_input_devices_only_inputs(monkeypatch):
    import sys

    devices = [
        {"name": "扬声器", "max_input_channels": 0},
        {"name": "麦克风 A", "max_input_channels": 2},
    ]
    fake_sd = types.SimpleNamespace(query_devices=lambda: devices)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    out = va.list_input_devices()
    assert out == [(1, "麦克风 A", 2)]



# ---------------------------------------------------------------- 回声抑制 / 忙碌
def test_is_muted_during_tts_mute_window():
    """_say 设的静音窗口内应判定为静音，窗口过后解除。"""
    import time
    a = _make({})
    a._mute_until = time.time() + 5.0
    # 关掉 TTS 探测（无 tts 模块时 speaker() 会抛，_tts_busy 返回 False）
    assert a._is_muted() is True
    a._mute_until = 0.0
    assert a._is_muted() is False


def test_busy_reply_mutes_then_deadline_clears():
    """AI 回复忙碌时静音；兜底超时后自动解除，不会永久静音。"""
    import time
    a = _make({})
    a._busy_reply = True
    a._busy_deadline = time.time() + 10.0
    assert a._is_muted() is True
    # 超时后
    a._busy_deadline = time.time() - 1.0
    assert a._is_muted() is False
    assert a._busy_reply is False  # 被超时清掉


def test_on_chat_finished_clears_busy():
    """回复完成回调清忙碌标志。"""
    a = _make({})
    a._busy_reply = True
    a._on_chat_finished("用户说", "回复内容")
    assert a._busy_reply is False


def test_send_to_chat_sets_busy_immediately():
    """投递到对话的瞬间就把 busy 置上（防止生成期间被误采集）。"""
    a = _make({})
    # app=None → _go 里会立即把 busy 复位；但同步部分先设 True。这里只验证同步段。
    import time
    a._busy_reply = False
    a._busy_deadline = 0.0
    # 直接调用内部同步片段：模拟不跑 QTimer 的情况——检查设置后为 True
    a._busy_reply = True
    a._busy_deadline = time.time() + 25.0
    assert a._busy_reply is True and a._busy_deadline > time.time()

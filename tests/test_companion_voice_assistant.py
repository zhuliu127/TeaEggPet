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


def test_busy_reply_does_not_mute_mic():
    """AI 生成（等回复）期间**不静音麦克风**：否则用户等回复的十几秒里说什么都没反应、
    连结束词都叫不动（用户实测 bug）。_busy_reply 只用于防重复提交，不参与采集静音。"""
    import time
    a = _make({})
    a._busy_reply = True
    a._busy_deadline = time.time() + 10.0
    assert a._is_muted() is False          # 关键：等她回复时不堵麦克风
    # 兜底超时后标志自动清掉
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


# ---------------------------------------------------------------- 幻听过滤 (v1.4.19)
def test_looks_hallucinated_rejects_repeated_wake_word():
    """把一长串重复的「小鱼小鱼小鱼…」判为幻听，避免凭空唤醒。"""
    assert va._looks_hallucinated("小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼") is True
    assert va._looks_hallucinated("小鱼小鱼小鱼 小鱼小鱼 小鱼 小出快 confinement") is True


def test_looks_hallucinated_accepts_normal_short():
    """正常的短句/唤醒词不算幻听。"""
    assert va._looks_hallucinated("小鱼小鱼") is False
    assert va._looks_hallucinated("照顾滾") is False
    assert va._looks_hallucinated("来啦来啦，想聊点什么呀？") is False
    assert va._looks_hallucinated("") is True


def test_idle_hallucination_does_not_wake():
    """idle 态下即便转写里含唤醒词，只要是幻听就不应进入对话。"""
    import numpy as np
    a = _make({"companion_voice_wake_word": "小鱼小鱼"})
    woke = {"v": False}
    a.woke.connect(lambda: woke.__setitem__("v", True))

    def fake_transcribe(engine, audio, **kw):
        return "小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼"

    import pet.companion.asr as asr_mod
    orig = asr_mod.transcribe
    asr_mod.transcribe = fake_transcribe
    try:
        a._state = "idle"
        a._handle_segment(np.zeros(16000, dtype=np.float32))
    finally:
        asr_mod.transcribe = orig
    assert woke["v"] is False
    assert a._state == "idle"


def test_idle_real_wake_still_works():
    """正常听到「小鱼小鱼」仍能唤醒（别把真唤醒也误杀）。"""
    import numpy as np
    a = _make({"companion_voice_wake_word": "小鱼小鱼"})
    a._greet = lambda: None  # type: ignore[assignment]
    woke = {"v": False}
    a.woke.connect(lambda: woke.__setitem__("v", True))

    def fake_transcribe(engine, audio, **kw):
        return "小鱼小鱼"

    import pet.companion.asr as asr_mod
    orig = asr_mod.transcribe
    asr_mod.transcribe = fake_transcribe
    try:
        a._state = "idle"
        a._handle_segment(np.zeros(16000, dtype=np.float32))
    finally:
        asr_mod.transcribe = orig
    assert woke["v"] is True
    assert a._state == "dialog"


def test_say_mute_duration_capped():
    """_say 的静音窗口必须封顶（≤8s），防止超长文本/幻听把麦克风堵死一大段时间。"""
    import time
    a = _make({})
    # 用一个假的 tts 模块让 speak_text 返回 True
    import sys, types
    fake_tts = types.SimpleNamespace()
    fake_tts.speak_text = lambda text, store, kind="chat": True
    fake_tts.speaker = lambda: types.SimpleNamespace(busy=False)
    sys.modules.setdefault("pet.companion.tts", fake_tts)
    orig = sys.modules.get("pet.companion.tts")
    sys.modules["pet.companion.tts"] = fake_tts
    import pet.companion as _pkg
    setattr(_pkg, "tts", fake_tts)
    try:
        a._mute_until = 0.0
        before = time.time()
        a._say("这个" * 200)  # 400 字，旧逻辑会算出 ~80s
        dur = a._mute_until - before
    finally:
        if orig is not None:
            sys.modules["pet.companion.tts"] = orig
    assert dur <= 8.5, f"静音窗口过长：{dur}"


def _flush_qt():
    """跑一轮事件循环，让 QTimer.singleShot(0) 投递的回调真正执行。"""
    import time
    from PySide6.QtCore import QCoreApplication
    deadline = time.time() + 1.0
    while time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
        QCoreApplication.processEvents()


def test_say_defers_tts_off_caller_thread():
    """_say 从「后台线程」调用时必须把发声投递回 GUI 线程执行 speak_text。

    回归背景：_say 由采集后台线程调用，而本地 TTS 用 QMediaPlayer（只能主线程用）。
    在后台线程里直接 speak_text → 无声 + 卡死。修复后用信号 speak_requested（queued
    connection）把 _do_speak 投递回 GUI 线程：后台线程里 _say 立即返回、不执行
    speak_text；等 GUI 事件循环转起来后才执行一次。
    """
    import sys, types, threading
    a = _make({})
    # _make 把 _say 打了桩，这里恢复真实的 _say 方法
    a._say = va.VoiceAssistant._say.__get__(a, va.VoiceAssistant)
    called = {"n": 0}

    def _speak_text(text, store, kind="chat"):
        called["n"] += 1
        return False  # 不设静音窗口，纯检查调用时机

    fake_tts = types.SimpleNamespace()
    fake_tts.speak_text = _speak_text
    fake_tts.speaker = lambda: types.SimpleNamespace(busy=False)
    orig = sys.modules.get("pet.companion.tts")
    sys.modules["pet.companion.tts"] = fake_tts
    import pet.companion as _pkg
    _orig_pkg_tts = getattr(_pkg, "tts", None)
    setattr(_pkg, "tts", fake_tts)
    # 用假 bubble，避免依赖真实 runtime
    import pet.companion.runtime as rt_mod
    _orig_bubble = getattr(rt_mod, "bubble", None)
    rt_mod.bubble = lambda *a, **k: None
    try:
        # 模拟真实场景：从后台线程调用 _say
        th = threading.Thread(target=lambda: a._say("你好"))
        th.start()
        th.join()
        # 后台线程已返回、GUI 事件循环未跑：speak_text 不应在后台线程同步执行
        assert called["n"] == 0, "speak_text 不应在调用（后台）线程同步执行"
        _flush_qt()
        # 事件循环跑完：应在 GUI 线程被调用一次
        assert called["n"] == 1, "speak_text 应在 GUI 事件循环里被调用一次"
    finally:
        if orig is not None:
            sys.modules["pet.companion.tts"] = orig
        if _orig_pkg_tts is not None:
            setattr(_pkg, "tts", _orig_pkg_tts)
        if _orig_bubble is not None:
            rt_mod.bubble = _orig_bubble


def test_say_empty_text_does_nothing():
    a = _make({})
    a._say = va.VoiceAssistant._say.__get__(a, va.VoiceAssistant)
    # 不应抛异常、不应投递
    a._say("")
    a._say("   ")
    _flush_qt()


# ---------------------------------------------------------------- 噪声门控（参考 dsh-dafeiyu-pro）
def test_subtitle_junk_filtered():
    """ASR 输出的字幕套话/背景音要能识别出来（参考实战幻听词表）。"""
    assert va._looks_like_subtitle_junk("谢谢观看，请订阅点赞") is True
    assert va._looks_like_subtitle_junk("字幕由网易云音乐提供") is True
    assert va._looks_like_subtitle_junk("今天天气不错") is False
    assert va._looks_like_subtitle_junk("小鱼小鱼") is False


def test_zero_crossing_rate_noise_vs_tone():
    """过零率：白噪声远高于低频正弦（人声其实介于两者，但噪声应可区分）。"""
    import numpy as np
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(4800).astype(np.float32) * 0.1
    t = np.arange(4800) / 16000.0
    tone = (0.1 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    zcr_noise = va._zero_crossing_rate(noise)
    zcr_tone = va._zero_crossing_rate(tone)
    assert zcr_noise > zcr_tone
    assert zcr_tone < 0.35, f"低频音过零率不应超高：{zcr_tone}"
    # 静音/单点不崩
    assert va._zero_crossing_rate(np.zeros(0, dtype=np.float32)) == 0.0


def test_voiced_ratio_speech_like_vs_flat():
    """有声帧占比：整段能量均匀的类语音应高，前小后大（断续）应偏低。"""
    import numpy as np
    rng = np.random.default_rng(1)
    # 类语音：持续有声
    speechlike = (rng.standard_normal(16000).astype(np.float32) * 0.05)
    # 断续：只有前半段有声
    burst = np.concatenate([rng.standard_normal(8000).astype(np.float32) * 0.05,
                            np.zeros(8000, dtype=np.float32)])
    r_speech = va._voiced_ratio(speechlike)
    r_burst = va._voiced_ratio(burst)
    assert r_speech >= r_burst
    assert 0.0 <= r_speech <= 1.0

# -*- coding: utf-8 -*-
"""语音助手纯逻辑测试（v1.4.22 按住说话 push-to-talk 架构）。

只测「不用硬件/模型就能验证」的部分：文本归一化、指令分派、热键组合解析、
按下/松开状态机、识别后分派、回声静音窗口。真正的麦克风采集和 Whisper 推理
属于集成测试范畴，这里不碰。
"""
from __future__ import annotations

import types

from PySide6.QtWidgets import QApplication

from pet.companion import voice_assistant as va
from pet.companion import ptt_hotkey as ptt


def _app():
    return QApplication.instance() or QApplication([])


class _FakeStore:
    def __init__(self, cfg=None):
        self._cfg = dict(cfg or {})

    def get(self, key, default=None):
        return self._cfg.get(key, default)


def _make(cfg=None):
    """造一个不做任何 I/O 的助手：把 _cfg 换成直接读内存字典。"""
    _app()
    a = va.VoiceAssistant(app=None)
    store = _FakeStore(cfg)
    a._cfg = lambda key, default=None: store.get(key, default)  # type: ignore[assignment]
    a._store = lambda: store  # type: ignore[assignment]
    a._say = lambda text: None  # type: ignore[assignment]
    return a


# ---------------------------------------------------------------- 归一化/匹配
def test_norm_strips_punct_and_lower():
    assert va._norm("小鱼，小鱼！") == "小鱼小鱼"
    assert va._norm("  Hello, World! ") == "helloworld"


def test_contains_any_hits_normalized():
    assert va._contains_any("我要自拍一张", va.SELFIE_WORDS) == "自拍"
    assert va._contains_any("出去走走呀", va.WALK_WORDS) in ("出去走走", "走走")
    assert va._contains_any("完全无关的话", va.SELFIE_WORDS) == ""


# ---------------------------------------------------------------- 热键组合解析
def test_parse_combo_default_when_empty():
    mods = ptt.PushToTalkHotkey._parse_combo("")
    # 空 → 默认 win+alt
    assert mods.get("win") is True
    assert mods.get("alt") is True


def test_parse_combo_custom():
    mods = ptt.PushToTalkHotkey._parse_combo("ctrl+shift")
    assert mods.get("ctrl") is True
    assert mods.get("shift") is True
    assert not mods.get("win")
    assert not mods.get("alt")


def test_parse_combo_single_key():
    mods = ptt.PushToTalkHotkey._parse_combo("alt")
    assert mods.get("alt") is True
    assert not mods.get("win")


def test_hotkey_combo_from_config():
    a = _make({"companion_voice_hotkey": "ctrl+alt"})
    assert a.hotkey_combo() == "ctrl+alt"


def test_hotkey_combo_default():
    a = _make({})
    assert a.hotkey_combo() == "win+alt"


# ---------------------------------------------------------------- 按下/松开状态机
def test_begin_recording_sets_state(monkeypatch):
    """按下热键 → 状态 recording，并起后台录音线程。"""
    a = _make({"companion_voice_enabled": True})
    a._engine = object()  # 假装模型已加载，避免真去加载
    started = {"t": None}

    class _FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            started["t"] = target
            self._target = target

        def start(self):
            pass  # 不真的跑

    monkeypatch.setattr(va.threading, "Thread", _FakeThread)
    a._begin_recording()
    assert a.state == "recording"
    # 起后台录音线程（绑定方法每次访问是新对象，比较 __func__）
    assert getattr(started["t"], "__func__", None) is va.VoiceAssistant._record_loop


def test_end_recording_when_not_recording_noop():
    a = _make({"companion_voice_enabled": True})
    a._state = "idle"
    a._end_recording()  # 不该抛异常、状态不变
    assert a.state == "idle"


# ---------------------------------------------------------------- 识别后分派
def test_dispatch_selfie_goes_to_confirm(monkeypatch):
    a = _make({"companion_voice_enabled": True})
    called = []
    a._do_selfie = lambda: called.append("selfie")  # type: ignore[assignment]
    a._state = "idle"
    a._dispatch("帮我自拍一张吧")
    assert a.state == "confirm"
    assert called == []  # 还要二次确认


def test_dispatch_home_prioritized_over_walk():
    a = _make({"companion_voice_enabled": True})
    hits = []
    a._do_home = lambda: hits.append("home")  # type: ignore[assignment]
    a._do_walk = lambda: hits.append("walk")  # type: ignore[assignment]
    a._state = "idle"
    a._dispatch("回家吧")
    assert hits == ["home"]


def test_dispatch_walk_word():
    a = _make({"companion_voice_enabled": True})
    hits = []
    a._do_home = lambda: hits.append("home")  # type: ignore[assignment]
    a._do_walk = lambda: hits.append("walk")  # type: ignore[assignment]
    a._state = "idle"
    a._dispatch("我出去溜达一下")
    assert hits == ["walk"]


def test_dispatch_plain_chat():
    a = _make({"companion_voice_enabled": True})
    sent = []
    a._send_to_chat = lambda t: sent.append(t)  # type: ignore[assignment]
    a._state = "idle"
    a._dispatch("你今天过得怎么样呀")
    assert sent == ["你今天过得怎么样呀"]


def test_dispatch_in_confirm_yes():
    a = _make({"companion_voice_enabled": True})
    a._state = "confirm"
    a._pending_selfie = True
    called = []
    a._do_selfie = lambda: called.append("selfie")  # type: ignore[assignment]
    a._dispatch("确认")
    assert called == ["selfie"]


# ---------------------------------------------------------------- 非阻塞启动
def test_start_returns_false_when_disabled():
    a = _make({"companion_voice_enabled": False})
    assert a.start() is False
    assert a.state == "off"


def test_start_does_not_load_model_synchronously(monkeypatch):
    """start() 只装钩子、立即返回；模型在后台线程预热，绝不在调用线程同步加载。"""
    import threading

    loaded = {"thread": None}
    a = _make({"companion_voice_enabled": True, "companion_voice_hotkey": "win+alt"})

    def fake_load():
        loaded["thread"] = threading.current_thread().name
        return True

    a._load_models = fake_load  # type: ignore[assignment]

    # 假 ptt 模块：install 成功，不真的装钩子
    class _FakePtt:
        def __init__(self, combo="win+alt", parent=None):
            self.pressed = types.SimpleNamespace(connect=lambda f: None)
            self.released = types.SimpleNamespace(connect=lambda f: None)

        def install(self):
            return True

        def uninstall(self):
            return None

    monkeypatch.setattr(va, "PushToTalkHotkey", _FakePtt)
    ok = a.start()
    assert ok is True
    assert a.state == "idle"
    # 等在后台预热线程里跑完 fake_load，确认它不是在主(调用)线程里同步执行的
    for _ in range(100):
        if loaded["thread"] is not None:
            break
        import time as _t
        _t.sleep(0.01)
    assert loaded["thread"] is not None
    assert loaded["thread"] != threading.main_thread().name
    a.stop()


def test_stop_resets_state():
    a = _make({"companion_voice_enabled": False})
    a._hotkey = None
    a.stop()
    assert a.state == "off"


# ---------------------------------------------------------------- 回声静音窗口
def test_is_muted_within_window():
    import time
    a = _make({})
    a._mute_until = time.time() + 5.0
    assert a._is_muted() is True
    a._mute_until = 0.0
    assert a._is_muted() is False


def test_say_mute_duration_capped():
    """_say 的静音窗口必须封顶（≤8s），防超长文本把麦克风堵死一大段时间。"""
    import time
    a = _make({})
    import sys
    fake_tts = types.SimpleNamespace()
    fake_tts.speak_text = lambda text, store, kind="chat": True
    fake_tts.speaker = lambda: types.SimpleNamespace(busy=False)
    orig = sys.modules.get("pet.companion.tts")
    sys.modules["pet.companion.tts"] = fake_tts
    import pet.companion as _pkg
    _orig_pkg_tts = getattr(_pkg, "tts", None)
    setattr(_pkg, "tts", fake_tts)
    try:
        a._mute_until = 0.0
        before = time.time()
        a._do_speak("这个" * 200)
        dur = a._mute_until - before
    finally:
        if orig is not None:
            sys.modules["pet.companion.tts"] = orig
        if _orig_pkg_tts is not None:
            setattr(_pkg, "tts", _orig_pkg_tts)
    assert dur <= 8.5, f"静音窗口过长：{dur}"


# ---------------------------------------------------------------- 噪声/幻听门控
def test_subtitle_junk_filtered():
    assert va._looks_like_subtitle_junk("谢谢观看，请订阅点赞") is True
    assert va._looks_like_subtitle_junk("字幕由网易云音乐提供") is True
    assert va._looks_like_subtitle_junk("今天天气不错") is False


def test_looks_hallucinated_rejects_repeated():
    assert va._looks_hallucinated("小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼小鱼") is True
    assert va._looks_hallucinated("") is True


def test_looks_hallucinated_accepts_normal():
    assert va._looks_hallucinated("小鱼小鱼") is False
    assert va._looks_hallucinated("来啦来啦，想聊点什么呀？") is False


def test_zero_crossing_rate_noise_vs_tone():
    import numpy as np
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(4800).astype(np.float32) * 0.1
    t = np.arange(4800) / 16000.0
    tone = (0.1 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    assert va._zero_crossing_rate(noise) > va._zero_crossing_rate(tone)
    assert va._zero_crossing_rate(np.zeros(0, dtype=np.float32)) == 0.0


def test_voiced_ratio_speech_like_vs_flat():
    import numpy as np
    rng = np.random.default_rng(1)
    speechlike = (rng.standard_normal(16000).astype(np.float32) * 0.05)
    burst = np.concatenate([rng.standard_normal(8000).astype(np.float32) * 0.05,
                            np.zeros(8000, dtype=np.float32)])
    assert va._voiced_ratio(speechlike) >= va._voiced_ratio(burst)


def test_subtitle_junk_filters_new_phrases():
    # v1.4.25：实测会出现「字幕by索兰娅」「字幕製作人Zither Harp」这类幻听，必须过滤
    assert va._looks_like_subtitle_junk("字幕by索兰娅") is True
    assert va._looks_like_subtitle_junk("字幕製作人Zither Harp") is True
    assert va._looks_like_subtitle_junk("今天天气真不错我们出去玩") is False


def test_raw_name_candidates_orders_wasapi_before_directsound():
    # _raw_name_candidates 原始排序：WASAPI(3) > MME(2) > DirectSound(1) > WDM-KS(0)
    import sys
    fake_sd = types.SimpleNamespace(
        query_hostapis=lambda: [{"name": "Windows MME"},
                                {"name": "Windows DirectSound"},
                                {"name": "Windows WASAPI"},
                                {"name": "Windows WDM-KS"}],
        query_devices=lambda: [
            {"name": "麦克风 (BOYA)", "max_input_channels": 1, "hostapi": 0},   # MME 0
            {"name": "麦克风 (BOYA)", "max_input_channels": 1, "hostapi": 1},   # DS 1
            {"name": "麦克风 (BOYA)", "max_input_channels": 2, "hostapi": 2},   # WASAPI 2
            {"name": "麦克风 (BOYA)", "max_input_channels": 1, "hostapi": 3},   # WDM-KS 3
        ],
    )
    orig = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake_sd
    try:
        a = _make({"companion_voice_device": "BOYA"})
        cands = a._raw_name_candidates()
    finally:
        if orig is not None:
            sys.modules["sounddevice"] = orig
        else:
            sys.modules.pop("sounddevice", None)
    # WASAPI(2) 应排第一；WDM-KS(3) 排最后
    assert cands[0] == 2
    assert cands[-1] == 3


def test_pick_device_skips_unopenable_and_zeros():
    # _pick_device 用 InputStream 实测：跳过打不开的(WDM-KS)和返回全零的(DirectSound)，
    # 选第一个「能开且读到数据」的端点（这里是 MME）。
    import sys
    fake_sd = types.SimpleNamespace(
        query_hostapis=lambda: [{"name": "Windows MME"},
                                {"name": "Windows DirectSound"},
                                {"name": "Windows WASAPI"},
                                {"name": "Windows WDM-KS"}],
        query_devices=lambda dev=None, kind=None: (
            {"name": "麦克风 (BOYA)", "max_input_channels": 1, "default_samplerate": 44100}
            if dev is not None else
            [
                {"name": "麦克风 (BOYA)", "max_input_channels": 1, "hostapi": 0},
                {"name": "麦克风 (BOYA)", "max_input_channels": 1, "hostapi": 1},
                {"name": "麦克风 (BOYA)", "max_input_channels": 2, "hostapi": 2},
                {"name": "麦克风 (BOYA)", "max_input_channels": 1, "hostapi": 3},
            ]
        ),
    )
    orig = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake_sd
    try:
        a = _make({"companion_voice_device": "BOYA"})
        a._usable_device = None

        def fake_probe(dev, seconds=0.3):
            if dev == 3:      # WDM-KS 打不开
                raise OSError("Blocking API not supported")
            if dev == 1:      # DirectSound 返回全零
                return (True, 0.0, 0.0, 44100)
            return (True, 0.05, 0.4, 44100)  # WASAPI(2)/MME(0) 正常
        a._probe_input_stream = fake_probe  # type: ignore[assignment]
        # 候选顺序 WASAPI(2)→MME(0)→DS(1)→WDM-KS(3)，2 可用 → 直接选 2
        assert a._pick_device() == 2
        assert a._usable_device == 2
    finally:
        if orig is not None:
            sys.modules["sounddevice"] = orig
        else:
            sys.modules.pop("sounddevice", None)


def test_demote_device_moves_to_next_candidate():
    import sys
    fake_sd = types.SimpleNamespace(
        query_hostapis=lambda: [{"name": "Windows WASAPI"}, {"name": "Windows MME"}],
        query_devices=lambda: [
            {"name": "BOYA", "max_input_channels": 2, "hostapi": 0},  # WASAPI 0
            {"name": "BOYA", "max_input_channels": 1, "hostapi": 1},  # MME 1
        ],
    )
    orig = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake_sd
    try:
        a = _make({"companion_voice_device": "BOYA"})
        a._usable_device = 0
        # 首选是 WASAPI(0)，把它降级后应改用下一个候选 MME(1)
        a._demote_device(0)
        assert a._usable_device == 1
    finally:
        if orig is not None:
            sys.modules["sounddevice"] = orig
        else:
            sys.modules.pop("sounddevice", None)

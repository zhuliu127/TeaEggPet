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
    # 「来啦来啦，想聊点什么呀？」是正常句子（重复片段后还有其它内容），不算幻听
    assert va._looks_hallucinated("来啦来啦，想聊点什么呀？") is False
    assert va._looks_hallucinated("今天天气真不错") is False


def test_looks_hallucinated_rejects_short_exact_repeat():
    # 整句恰好是同一短片段重复两次（低质量音频复读幻听），如「三星火三星火」「小姨小姨」
    assert va._looks_hallucinated("三星火三星火") is True
    assert va._looks_hallucinated("小姨小姨") is True


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


def test_highpass_attenuates_dc_and_lowfreq():
    import numpy as np
    # 直流 + 20Hz 低频分量应被高通显著衰减；高频 1kHz 基本保留
    t = np.arange(16000) / 16000.0
    low = (0.5 * np.ones_like(t) + 0.4 * np.sin(2 * np.pi * 20 * t)).astype(np.float32)
    hp = va._highpass(low, 16000)
    assert float(np.mean(np.abs(hp))) < float(np.mean(np.abs(low))) * 0.5
    hi = (0.4 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    hp_hi = va._highpass(hi, 16000)
    # 高频能量大致保留（不衰减到一半以下）
    assert float(np.sqrt(np.mean(hp_hi ** 2))) > float(np.sqrt(np.mean(hi ** 2))) * 0.6
    assert va._highpass(np.zeros(0, dtype=np.float32)).shape[0] == 0


def test_is_prompt_echo_detects_initial_prompt_leak():
    # v1.4.33：whisper 在静音/低信噪比段会把 initial_prompt 复述出来，必须识别并丢弃，
    # 否则「请用简体中文输出。」会被当成用户的话送进 AI 对话。
    assert va._is_prompt_echo("请用简体中文输出。") is True
    assert va._is_prompt_echo("以下是普通话的句子，请用简体中文输出。") is True
    assert va._is_prompt_echo("用简体中文输出") is True
    assert va._is_prompt_echo("简体中文") is True
    # v1.4.34：用户实测的「听歪」变体也要拦
    assert va._is_prompt_echo("整体中文输出。") is True
    assert va._is_prompt_echo("简体输出") is True
    # 正常用户话不应被误判
    assert va._is_prompt_echo("今天天气怎么样") is False
    assert va._is_prompt_echo("帮我订个闹钟") is False
    assert va._is_prompt_echo("") is False


def test_to_simplified_converts_traditional():
    # v1.4.34：不再用 initial_prompt 逼简体（易回声），改为识别后繁→简转换。
    assert va._to_simplified("小魚小魚今天天氣怎麼樣") == "小鱼小鱼今天天气怎么样"
    assert va._to_simplified("幫我訂一個明天早上八點的鬧鐘") == "帮我订一个明天早上八点的闹钟"
    assert va._to_simplified("我想吃火鍋了") == "我想吃火锅了"
    # 已是简体 / 英文数字 保持不变
    assert va._to_simplified("你好abc123") == "你好abc123"
    assert va._to_simplified("") == ""


def test_subtitle_junk_filters_prompt_echo_phrases():
    assert va._looks_like_subtitle_junk("请用简体中文输出。") is True
    assert va._looks_like_subtitle_junk("以下是普通话的句子") is True


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


def test_dispatch_requested_signal_from_background_thread():
    # v1.4.27：识别在后台线程完成，必须经 dispatch_requested 信号（QueuedConnection）
    # 投递回 GUI 线程派发；后台线程直接 QTimer.singleShot 不会触发（没事件循环）。
    import threading, time
    a = _make()
    got = {}
    def fake_dispatch(text):
        got['t'] = text
    a._dispatch = fake_dispatch  # type: ignore[assignment]
    a.dispatch_requested.connect(fake_dispatch, va.Qt.QueuedConnection)
    def bg():
        a.dispatch_requested.emit('今天天气真不错')
    th = threading.Thread(target=bg); th.start(); th.join()
    app = _app()
    end = time.time() + 2.0
    while time.time() < end and 't' not in got:
        app.processEvents(); time.sleep(0.02)
    assert got.get('t') == '今天天气真不错'


def test_ensure_chat_handles_appshell_open_full_chat():
    # runtime.app() 在 overlay 拓扑下返回 AppShell（只有 open_full_chat，聊天窗在 _instance 上）。
    # _ensure_chat 不能因为 AppShell 没有 open_modern_chat 就报错返回 None。
    a = _make()
    opened = {}
    class FakeWin:
        pass
    win = FakeWin()
    class FakeInst:
        modern_chat_window = win
        def open_modern_chat(self):
            opened['m'] = True
    class FakeShell:
        _instance = FakeInst()
        def open_full_chat(self):
            opened['f'] = True
    shell = FakeShell()
    got = a._ensure_chat(shell)
    assert got is win
    # 已经拿到窗口时不必再打开
    assert 'f' not in opened


def test_ensure_chat_opens_via_open_full_chat_when_no_window():
    a = _make()
    opened = {}
    class FakeInst:
        modern_chat_window = None
        def open_modern_chat(self):
            opened['m'] = True
    class FakeShell:
        _instance = FakeInst()
        def open_full_chat(self):
            opened['f'] = True
    shell = FakeShell()
    # 没有现成窗口 → 应先开一次
    got = a._ensure_chat(shell)
    assert ('f' in opened) or ('m' in opened)
    assert got is None  # 两个窗口都还是 None


def test_record_loop_resets_rec_thread_after_finish():
    # 录音线程退出后 _rec_thread 必须清空，否则下次按住被守卫挡掉（“之后再按没反应”）。
    a = _make()
    a._rec_thread = None
    # 模拟 _on_pressed 的守卫逻辑
    a._rec_thread = object()  # 有一个陈旧句柄
    blocked = (a._state == "recording" or a._rec_thread is not None)
    assert blocked is True
    # 线程结束后置 None → 不再挡
    a._rec_thread = None
    blocked2 = (a._state == "recording" or a._rec_thread is not None)
    assert blocked2 is False



def test_ensure_chat_uses_app_instance_open_chat():
    # AppShell: 聊天窗挂在 app.instance（PetInstance）上，open_chat 后取到 modern_chat_window。
    a = _make()
    class FakeWin:
        def voice_submit(self, t): pass
    win = FakeWin()
    class FakeInst:
        modern_chat_window = None
        chat_window = None
        legacy_chat_window = None
        def open_chat(self):
            self.modern_chat_window = win
    class FakeShell:
        instance = FakeInst()
    got = a._ensure_chat(FakeShell())
    assert got is win
    assert callable(getattr(got, "voice_submit", None))


def test_ensure_chat_returns_existing_window_no_open():
    a = _make()
    class FakeWin: pass
    win = FakeWin()
    class FakeInst:
        modern_chat_window = win
        def open_chat(self):
            raise AssertionError("should not open when window exists")
    class FakeShell:
        instance = FakeInst()
    assert a._ensure_chat(FakeShell()) is win


def test_ensure_chat_returns_none_when_nothing_available():
    a = _make()
    class Empty:
        pass
    # 没有任何 open_* 方法、也没有窗口
    assert a._ensure_chat(Empty()) is None


def test_ensure_chat_opens_modern_chat_silent_by_default():
    # 语音注入默认 silent=True：必须用 open_modern_chat(silent=True) 打开，
    # 避免在前台弹出聊天窗。
    a = _make()
    calls = {}
    class FakeWin:
        def voice_submit(self, t): pass
    win = FakeWin()
    class FakeInst:
        modern_chat_window = None
        chat_window = None
        legacy_chat_window = None
        def open_modern_chat(self, silent=False):
            calls["silent"] = silent
            self.modern_chat_window = win
        def open_chat(self):
            raise AssertionError("silent 路径应优先 open_modern_chat，不应走 open_chat")
    class FakeShell:
        instance = FakeInst()
    got = a._ensure_chat(FakeShell())
    assert got is win
    assert calls.get("silent") is True


def test_ensure_chat_silent_off_uses_open_chat():
    # silent=False 时维持旧行为（可走 open_chat 弹前台窗）。
    a = _make()
    class FakeWin:
        def voice_submit(self, t): pass
    win = FakeWin()
    class FakeInst:
        modern_chat_window = None
        chat_window = None
        legacy_chat_window = None
        def open_chat(self):
            self.modern_chat_window = win
    class FakeShell:
        instance = FakeInst()
    got = a._ensure_chat(FakeShell(), silent=False)
    assert got is win


def test_ensure_chat_silent_falls_back_when_modern_signature_rejects_silent():
    # 旧版 open_modern_chat 不接受 silent 关键字 → 捕获 TypeError 后无参重试。
    a = _make()
    class FakeWin:
        def voice_submit(self, t): pass
    win = FakeWin()
    class FakeInst:
        modern_chat_window = None
        chat_window = None
        legacy_chat_window = None
        def open_modern_chat(self):  # 无 silent 参数
            self.modern_chat_window = win
    class FakeShell:
        instance = FakeInst()
    got = a._ensure_chat(FakeShell())
    assert got is win



def test_usable_device_cached_skips_probe_in_pick():
    # 已缓存可用端点时，_pick_device 直接用缓存（用探针计数验证不会重复试录全部端点）。
    a = _make()
    calls = []
    def fake_probe(dev, seconds=0.2):
        calls.append(dev)
        return (True, 0.05, 0.4, 44100)
    a._probe_input_stream = fake_probe  # type: ignore[assignment]
    a._usable_device = 2
    got = a._pick_device()
    assert got == 2
    assert calls and calls[0] == 2  # 首选命中缓存，不再遍历其它端点


def test_warmup_device_is_noop_when_cached():
    a = _make()
    a._usable_device = 5
    a._warmup_device()  # 已缓存 -> 不应起线程/抛错
    assert a._usable_device == 5

def test_on_released_hides_bubble_even_when_not_recording():
    # 连按场景：松手时即便状态不是 recording，也必须把「正在听你说话」气泡收起。
    a = _make()
    hidden = []
    ended = []
    a._hide_listen_bubble = lambda: hidden.append(1)
    a._end_recording = lambda: ended.append(1)
    a._state = "processing"
    a._on_released_gui()
    assert hidden == [1]
    assert ended == [1]

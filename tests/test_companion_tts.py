# -*- coding: utf-8 -*-
"""本地 GPT-SoVITS 语音 + 动态情绪。"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pet.companion import tts
from pet.companion.proactive_chat import ProactiveChatService
from pet.config import Config

_qapp = QApplication.instance() or QApplication([])


class _FakeApp:
    def __init__(self, cfg) -> None:
        self.config = cfg

    def system_notify(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        return None


def _service(tmp_path, **overrides):
    cfg = Config(base=tmp_path)
    for key, value in overrides.items():
        cfg.set(key, value)
    return ProactiveChatService(_FakeApp(cfg))


# ------------------------------------------------------------------ 模型扫描
def test_scan_models_reads_weight_folders(tmp_path):
    root = tmp_path / "AI Voice"
    (root / "GPT_weights_v2Pro").mkdir(parents=True)
    (root / "SoVITS_weights_v2Pro").mkdir(parents=True)
    (root / "GPT_weights_v2Pro" / "Bei-e10.ckpt").write_bytes(b"x")
    (root / "SoVITS_weights_v2Pro" / "Bei_e4_s88.pth").write_bytes(b"x")
    gpt, sovits = tts.scan_models(str(root))
    assert gpt == ["GPT_weights_v2Pro/Bei-e10.ckpt"]
    assert sovits == ["SoVITS_weights_v2Pro/Bei_e4_s88.pth"]
    assert tts.scan_models(str(tmp_path / "缺失")) == ([], [])


def test_tts_config_defaults_and_clamp(tmp_path):
    svc = _service(tmp_path, companion_tts_speed=9.9, companion_tts_api="http://x:1/")
    cfg = tts.tts_config(svc.store)
    assert cfg["api"] == "http://x:1", "结尾斜杠要吃掉"
    assert cfg["speed"] == 2.0, "语速夹到 2.0"
    assert cfg["enabled"] is False, "默认不启用语音"
    assert cfg["text_lang"] == "zh"


def test_server_python_finds_runtime(tmp_path):
    root = tmp_path / "AI Voice"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime" / "python.exe").write_bytes(b"x")
    assert tts.server_python({"dir": str(root)}).endswith("python.exe")
    assert tts.server_python({"dir": str(tmp_path / "nope")}) == ""


def test_launch_server_reports_missing_script(tmp_path):
    ok, message = tts.launch_server({"dir": str(tmp_path)})
    assert ok is False and "api_v2.py" in message


def test_synthesize_degrades_when_service_down():
    cfg = {"api": "http://127.0.0.1:9", "text_lang": "zh", "ref_audio": "a.wav",
           "ref_text": "hi", "prompt_lang": "zh", "speed": 1.0}
    assert tts.synthesize(cfg, "测试", timeout=1.5) is None
    assert tts.ping(cfg, timeout=1.0) is False
    assert "连不上" in tts.describe(cfg, timeout=1.0)


# ------------------------------------------------------------------ 端口形态识别
class _WebuiHandler(BaseHTTPRequestHandler):
    """Gradio 网页：POST /tts 回 404 JSON，GET / 回 HTML。"""

    def do_POST(self) -> None:  # noqa: N802
        body = b'{"detail":"Not Found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        body = b"<html><body>gradio</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002
        return None


class _ApiHandler(_WebuiHandler):
    """api_v2：/tts 路由在，缺参数回 400。"""

    def do_POST(self) -> None:  # noqa: N802
        body = b'{"message":"ref_audio_path is required"}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def _serve():
    servers = []

    def start(handler):
        srv = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_probe_tells_webui_from_api(_serve):
    webui = _serve(_WebuiHandler)
    api = _serve(_ApiHandler)
    assert tts.probe({"api": webui}, timeout=2.0)["state"] == "webui"
    assert tts.probe({"api": api}, timeout=2.0)["state"] == "api"
    assert tts.ping({"api": api}, timeout=2.0) is True
    assert tts.ping({"api": webui}, timeout=2.0) is False, "网页不能算在线"


def test_describe_explains_webui_port(_serve, monkeypatch):
    # 断掉「扫候选端口」：开发机上 api_v2 真在 9880 跑着时，这里会找到真接口，
    # 测的就不是「地址填成网页」这条分支了。
    monkeypatch.setattr(tts, "scan_api_ports", lambda cfg, **kw: "")
    webui = _serve(_WebuiHandler)
    text = tts.describe({"api": webui}, timeout=2.0)
    assert "网页界面" in text and "api_v2" in text


def test_port_free_detects_listening_socket(_serve):
    api = _serve(_ApiHandler)
    port = int(api.rsplit(":", 1)[1])
    assert tts.port_free(port) is False, "被占用时必须判为不空闲（Windows 上 bind 会骗人）"


def test_ensure_server_adopts_running_api(_serve, monkeypatch):
    webui = _serve(_WebuiHandler)
    api = _serve(_ApiHandler)
    port = int(api.rsplit(":", 1)[1])
    monkeypatch.setattr(tts, "API_HINT_PORTS", (port,))
    ok, found, message = tts.ensure_server({"api": webui}, wait=5.0)
    assert ok is True and found == api
    assert "发现" in message


def test_ensure_server_reports_missing_install(tmp_path, _serve, monkeypatch):
    monkeypatch.setattr(tts, "scan_api_ports", lambda cfg, **kw: "")
    webui = _serve(_WebuiHandler)
    ok, api, message = tts.ensure_server({"api": webui, "dir": str(tmp_path)}, wait=5.0)
    assert ok is False and "api_v2.py" in message
    assert api == webui


def test_synthesize_ex_mentions_404_for_webui(_serve):
    webui = _serve(_WebuiHandler)
    data, err = tts.synthesize_ex({"api": webui, "ref_audio": "a.wav", "text_lang": "zh",
                                   "prompt_lang": "zh", "speed": 1.0}, "你好", timeout=3.0)
    assert data is None
    assert "404" in err and "api_v2" in err


# ------------------------------------------------------------------ 与宿主 TTS 共存
class _CfgStub:
    def __init__(self, **kw) -> None:
        self.data = dict(kw)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        return None


def test_form_values_map_to_config_keys():
    """设置页表单是 companion_tts_*，配置字典是短键——键名对不上就等于没改。"""
    mapped = tts.cfg_from_form({"companion_tts_api": "http://127.0.0.1:9874",
                                "companion_tts_ref_audio": "r.wav",
                                "companion_tts_gpt": "GPT_weights_v2Pro/GM-e15.ckpt",
                                "companion_image_aspect": "3:4"})
    assert mapped == {"api": "http://127.0.0.1:9874", "ref_audio": "r.wav",
                      "gpt": "GPT_weights_v2Pro/GM-e15.ckpt"}


def test_local_tts_takes_over_host_chime(monkeypatch):
    from pet import app as app_mod

    monkeypatch.setattr(app_mod.feature_activation_mod, "is_active", lambda name: True)

    class _Host:
        def __init__(self, **kw) -> None:
            self.config = _CfgStub(**kw)

    class _HostOn(_Host):
        def _local_tts_taken_over(self):
            return True

    on, off = _Host(companion_tts_enabled=True), _Host(companion_tts_enabled=False)
    assert app_mod.PetApp._local_tts_taken_over(on) is True
    assert app_mod.PetApp._local_tts_taken_over(off) is False

    # 宿主那六路语音（报时/节日/自言自语/抽签/待办/回复朗读）都要让位
    assert app_mod.PetApp._chime_wanted(_HostOn(voice_chime_enabled=True)) is False
    assert app_mod.PetApp._chat_reply_voice_wanted(_HostOn(chat_reply_voice_enabled=True)) is False


def test_saving_local_tts_turns_off_host_tts(tmp_path):
    from pet.companion.settings_dialog import CompanionSettingsDialog

    cfg = _CfgStub(voice_chime_enabled=True, chat_reply_voice_enabled=True,
                   self_talk_voice_precache_enabled=True)
    dlg = CompanionSettingsDialog.__new__(CompanionSettingsDialog)
    dlg.cfg = cfg
    changed = CompanionSettingsDialog._silence_host_tts(dlg)
    assert set(changed) == {"voice_chime_enabled", "chat_reply_voice_enabled",
                            "self_talk_voice_precache_enabled"}
    assert cfg.data["voice_chime_enabled"] is False
    assert cfg.data["chat_reply_voice_enabled"] is False


# ------------------------------------------------------------------ 动态情绪
def test_mood_follows_unanswered(tmp_path):
    svc = _service(tmp_path)
    svc._unanswered = lambda: 3
    svc._last_user_at = lambda: None
    assert svc.mood()[0] == "委屈"

    svc._unanswered = lambda: 2
    assert svc.mood()[0] == "小情绪"

    svc._unanswered = lambda: 0
    assert svc.mood()[0] in ("平常", "雀跃", "想念", "困")


def test_mood_missing_you_after_long_gap(tmp_path):
    svc = _service(tmp_path)
    svc._unanswered = lambda: 0
    svc._last_user_at = lambda: datetime.now().astimezone() - timedelta(hours=30)
    label, hint = svc.mood()
    # 深夜/凌晨会先命中「困」，其余时间命中「想念」
    assert label in ("想念", "困")
    assert isinstance(hint, str)


def test_mood_is_persisted_with_spoken_lines(tmp_path):
    svc = _service(tmp_path)
    svc._unanswered = lambda: 0
    svc._last_user_at = lambda: None
    svc._remember_spoken("第一句")
    svc._remember_spoken("第二句")
    again = ProactiveChatService(svc.app)
    assert again._state.get("spoken", [])[-2:] == ["第一句", "第二句"]
    assert again._state.get("mood")


def test_spoken_lines_are_injected(tmp_path):
    svc = _service(tmp_path, companion_proactive_history_count=0)
    svc._remember_spoken("我刚把咖啡洒键盘上了")
    prompt = svc._build_prompt()
    assert "你最近说过" in prompt
    assert "咖啡洒键盘" in prompt


# ---------------------------------------------------------------- 全对话播报
def test_split_for_speech_cleans_markdown_and_breaks_long_reply():
    text = "# 标题\n第一句。第二句！\n```py\nprint(1)\n```\n" + "很长的一段话" * 20
    parts = tts.split_for_speech(text)
    assert any("第一句" in p for p in parts)
    assert all("print" not in p for p in parts), "代码不该念出来"
    assert all(len(p) <= 80 for p in parts), "单句不能无限长"
    assert len(parts) >= 3, "长回复要拆成多句，才能边播边合成"
    assert tts.split_for_speech("   ") == []


class _FakeSpeaker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def speak(self, text, cfg, *, interrupt=True):  # noqa: ANN001
        self.calls.append((text, interrupt))
        return True


def test_speak_text_gates_every_kind(tmp_path, monkeypatch):
    """三个开关各管一路对话；总开关一关全静音。"""
    fake = _FakeSpeaker()
    monkeypatch.setattr(tts, "speaker", lambda: fake)
    svc = _service(tmp_path, companion_tts_enabled=True,
                   companion_tts_ref_audio="ref.wav")

    assert tts.speak_text("你好", svc.store, kind="chat") is True
    assert tts.speak_text("截图里是啥", svc.store, kind="screen") is True
    assert tts.speak_text("在干嘛", svc.store, kind="proactive", interrupt=False) is True
    assert fake.calls[-1][1] is False, "主动消息不该打断正在念的回复"

    svc.store.set("companion_tts_read_chat", False)
    assert tts.speak_text("你好", svc.store, kind="chat") is False
    assert tts.speak_text("在干嘛", svc.store, kind="proactive") is True, "关一路不影响其他"

    svc.store.set("companion_tts_enabled", False)
    assert tts.speak_text("你好", svc.store, kind="chat") is False
    assert fake.calls, "前面几次确实送到播放器了"


def test_speak_text_needs_ref_audio(tmp_path, monkeypatch):
    fake = _FakeSpeaker()
    monkeypatch.setattr(tts, "speaker", lambda: fake)
    svc = _service(tmp_path, companion_tts_enabled=True)
    assert tts.speak_text("你好", svc.store, kind="chat") is False
    assert fake.calls == []


def test_local_speaker_queues_and_new_reply_overrides(monkeypatch):
    sp = tts.LocalSpeaker()
    monkeypatch.setattr(sp, "_pump", lambda: None)   # 不真起合成线程
    cfg = {"api": "http://127.0.0.1:9", "ref_audio": "a.wav"}

    assert sp.speak("第一句。第二句。", cfg) is True
    assert sp.pending == 2
    assert sp.speak("新的一句。", cfg) is True
    assert sp.pending == 1, "新对话覆盖还没念完的旧内容"
    sp.stop()
    assert sp.pending == 0


def test_chat_reply_goes_through_speak_text(tmp_path, monkeypatch):
    from pet.companion import runtime

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(tts, "speak_text", lambda text, store, **kw: captured.append((text, kw)))
    runtime._app = type("A", (), {"config": Config(base=tmp_path)})()
    runtime._store = None
    try:
        runtime._on_chat_reply("今天想吃什么？")
        assert captured == [("今天想吃什么？", {"kind": "chat"})]
    finally:
        runtime._app = None
        runtime._store = None


def test_chat_voice_subscribes_once(monkeypatch):
    from pet.chat.service import ChatService
    from pet.companion import runtime

    runtime._chat_voice_hooked = False
    before = list(ChatService._global_finished_listeners)
    try:
        runtime._start_chat_voice(None)
        runtime._start_chat_voice(None)
        added = [cb for cb in ChatService._global_finished_listeners if cb not in before]
        assert added == [runtime._on_chat_reply], "多窗/多次 attach 只订阅一次"
    finally:
        ChatService.unregister_global_finished(runtime._on_chat_reply)
        runtime._chat_voice_hooked = False


def test_interrupt_drops_stale_synth_result(monkeypatch):
    """新回复打断后，旧那句「已经合成好」的结果不能再念出来。"""
    sp = tts.LocalSpeaker()
    monkeypatch.setattr(sp, "_pump", lambda: None)
    monkeypatch.setattr(sp, "_stop_playback", lambda: None)
    cfg = {"api": "http://127.0.0.1:9", "ref_audio": "a.wav"}

    sp.speak("旧的一句。", cfg)
    old_gen = sp._gen
    sp.speak("新的一句。", cfg)
    assert sp._gen > old_gen

    sp._on_ready(b"stale", old_gen)          # 迟到的旧音频
    assert sp._playing is False, "旧音频不该播放"
    sp.stop()


# ---------------------------------------------------------------- 点击台词
def test_speak_text_self_talk_gate(tmp_path, monkeypatch):
    fake = _FakeSpeaker()
    monkeypatch.setattr(tts, "speaker", lambda: fake)
    svc = _service(tmp_path, companion_tts_enabled=True, companion_tts_ref_audio="ref.wav")

    assert tts.speak_text("好女孩……", svc.store, kind="self_talk") is True
    svc.store.set("companion_tts_read_self_talk", False)
    assert tts.speak_text("欧鲸鲸……", svc.store, kind="self_talk") is False
    assert tts.speak_text("你好", svc.store, kind="chat") is True, "关点击台词不影响对话"


def test_local_self_talk_goes_through_companion_tts(tmp_path, monkeypatch):
    from pet import app as app_mod
    from pet.companion import runtime

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(tts, "speak_text",
                        lambda text, store, **kw: seen.append((text, kw)) or True)
    runtime._app = type("A", (), {"config": Config(base=tmp_path)})()
    runtime._store = None
    try:
        assert app_mod.AppShell._speak_local_self_talk(object(), "好女孩……") is True
        assert seen == [("好女孩……", {"kind": "self_talk"})]
    finally:
        runtime._app = None
        runtime._store = None


def test_speak_self_talk_prefers_local_over_host_channel(monkeypatch):
    """本地接管时点击台词走本地，不再去建宿主那条音频通道。"""
    from pet import app as app_mod

    class _Host:
        spoken = ""

        def _local_tts_taken_over(self):
            return True

        def _speak_local_self_talk(self, text):
            self.spoken = text
            return True

        def ensure_audio_channel(self):  # pragma: no cover - 走到就是错
            raise AssertionError("本地接管时不该再建宿主音频通道")

    host = _Host()
    assert app_mod.AppShell.speak_self_talk(host, "欧鲸鲸……") is True
    assert host.spoken == "欧鲸鲸……"


def test_speak_self_talk_falls_back_when_local_unavailable():
    """本地走不通（没配参考音频等）仍要落回宿主通道，别变成静默。"""
    from pet import app as app_mod

    class _Channel:
        def __init__(self):
            self.spoken = []

        def is_running(self):
            return True

        def speak(self, text, **kw):
            self.spoken.append(text)
            return True

    class _Host:
        def __init__(self):
            self.channel = _Channel()

        def _local_tts_taken_over(self):
            return True

        def _speak_local_self_talk(self, text):
            return False

        def ensure_audio_channel(self):
            return self.channel

        def self_talk_voice_file(self, text):
            return None

    host = _Host()
    assert app_mod.AppShell.speak_self_talk(host, "好模型……") is True
    assert host.channel.spoken == ["好模型……"]


def test_chat_service_finished_reaches_local_tts(tmp_path, monkeypatch):
    """走真实的 ChatService 收尾路径：「AI 对话」里 AI 说的话要进本地语音。"""
    from pet.chat.service import ChatService
    from pet.companion import runtime

    seen: list[str] = []
    monkeypatch.setattr(tts, "speak_text", lambda text, store, **kw: seen.append(text))
    runtime._app = type("A", (), {"config": Config(base=tmp_path)})()
    runtime._store = None
    runtime._chat_voice_hooked = False
    try:
        runtime._start_chat_voice(None)
        service = ChatService()
        service._request_id = "rid-1"
        service._detached = False
        service._finished("rid-1", "我把今天的日程排好了，晚上七点提醒你。")
        assert seen == ["我把今天的日程排好了，晚上七点提醒你。"]
    finally:
        ChatService.unregister_global_finished(runtime._on_chat_reply)
        runtime._chat_voice_hooked = False
        runtime._app = None
        runtime._store = None


# ---------------------------------------------------------------- 溜达播报
def test_speak_text_walk_gate(tmp_path, monkeypatch):
    fake = _FakeSpeaker()
    monkeypatch.setattr(tts, "speaker", lambda: fake)
    svc = _service(tmp_path, companion_tts_enabled=True, companion_tts_ref_audio="ref.wav")

    assert tts.speak_text("我出门啦～", svc.store, kind="walk") is True
    svc.store.set("companion_tts_read_walk", False)
    assert tts.speak_text("我出门啦～", svc.store, kind="walk") is False
    assert tts.speak_text("你好", svc.store, kind="chat") is True, "关溜达不影响对话"


def test_walk_say_speaks_without_interrupting(tmp_path, monkeypatch):
    """溜达那句要念，而且不能打断正在念的 AI 回复（排队）。"""
    from pet.companion import runtime
    from pet.companion.walk import WalkService

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(tts, "speak_text",
                        lambda text, store, **kw: seen.append((text, kw)) or True)
    monkeypatch.setattr(runtime, "bubble", lambda *a, **k: True)
    svc = WalkService(_FakeApp(Config(base=tmp_path)))

    svc._say("我在外面散步，风挺大～")
    assert seen == [("我在外面散步，风挺大～", {"kind": "walk", "interrupt": False})]

    seen.clear()
    svc._say("举起手机拍一张…", speak=False)
    assert seen == [], "过程提示只出气泡不念"


def test_walk_narrate_message_reaches_local_tts(tmp_path, monkeypatch):
    """走真实 _narrate：LLM 那句要进本地语音。"""
    from datetime import datetime, timedelta

    from pet.companion import llm, runtime, schedule
    from pet.companion.walk import WalkService

    spoken: list[str] = []
    monkeypatch.setattr(tts, "speak_text", lambda text, store, **kw: spoken.append(text) or True)
    monkeypatch.setattr(runtime, "bubble", lambda *a, **k: True)
    monkeypatch.setattr(schedule, "current_context", lambda store: {})
    monkeypatch.setattr(llm, "ask", lambda *a, **k: "在楼下便利店门口，风有点大～")

    svc = WalkService(_FakeApp(Config(base=tmp_path)))
    svc._started_at = datetime.now()
    svc._ends_at = svc._started_at + timedelta(minutes=10)
    svc._narrate("during")

    assert spoken == ["在楼下便利店门口，风有点大～"]


def test_walk_narrate_speaks_fallback_when_llm_fails(tmp_path, monkeypatch):
    """LLM 挂掉时的兜底文案同样要念（不然溜达就彻底没声了）。"""
    from datetime import datetime, timedelta

    from pet.companion import llm, runtime, schedule
    from pet.companion.walk import WalkService

    spoken: list[str] = []
    monkeypatch.setattr(tts, "speak_text", lambda text, store, **kw: spoken.append(text) or True)
    monkeypatch.setattr(runtime, "bubble", lambda *a, **k: True)
    monkeypatch.setattr(schedule, "current_context",
                        lambda store: {"item": {"activity": "随便走走", "location": "河边"}})

    def boom(*a, **k):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(llm, "ask", boom)

    svc = WalkService(_FakeApp(Config(base=tmp_path)))
    svc._started_at = datetime.now()
    svc._ends_at = svc._started_at + timedelta(minutes=10)
    svc._narrate("during")

    assert spoken and "河边" in spoken[0]


def test_walk_start_line_is_not_spoken_twice(tmp_path, monkeypatch):
    """开场那句和「我出门啦」重复，只出气泡不念；出门本身有 opening 那句。"""
    from datetime import datetime, timedelta

    from pet.companion import llm, runtime, schedule
    from pet.companion.walk import WalkService

    spoken: list[str] = []
    monkeypatch.setattr(tts, "speak_text", lambda text, store, **kw: spoken.append(text) or True)
    monkeypatch.setattr(runtime, "bubble", lambda *a, **k: True)
    monkeypatch.setattr(schedule, "current_context", lambda store: {})
    lines = iter(["出门啦，去河边走走～", "河边风挺大，看见一只白鹭～"])
    monkeypatch.setattr(llm, "ask", lambda *a, **k: next(lines, "风挺大～"))

    svc = WalkService(_FakeApp(Config(base=tmp_path)))
    svc._started_at = datetime.now()
    svc._ends_at = svc._started_at + timedelta(minutes=10)
    svc._narrate("start")
    assert spoken == [], "开场不念，避免和 opening 重复"

    svc._narrate("during")
    assert spoken == ["河边风挺大，看见一只白鹭～"], "途中那句照念"


# ---------------------------------------------------------------- 播放完整性
def test_each_sentence_gets_its_own_tmp_file():
    """每句必须写不同路径。

    复用同一个 wav 路径时，QMediaPlayer 二次 setSource 会沿用上一段的媒体描述，
    长句只播上一句那么长（实测 4.9s 的句子只播 2.3s）—— 就是「语音不全」。
    """
    sp = tts.LocalSpeaker()
    paths = []
    try:
        for i in range(3):
            paths.append(sp._write_tmp(b"RIFF" + bytes([i])))
        assert len({str(p) for p in paths}) == 3
        assert all(p.exists() for p in paths)
        assert paths[0].parent.name == "teaggpet_tts"
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


def test_playing_sentence_is_not_cut_by_the_next_one(monkeypatch):
    """下一句合成好了也不能中途换源（会把正在念的那句截断），只排队等播。"""
    sp = tts.LocalSpeaker()
    played: list[bytes] = []
    monkeypatch.setattr(sp, "_play", lambda data: played.append(data))

    sp._playing = True
    sp._on_ready(b"second", sp._gen)
    assert sp._buffered == b"second"
    assert played == [], "正在播的时候不能换源"

    sp._playing = False
    sp._on_ready(b"third", sp._gen)
    assert played == [b"third"], "空闲时才直接播"


def test_end_of_media_plays_buffered_sentence(monkeypatch):
    """一句刚播完，已经合成好的下一句立刻接上（不再等一次合成）。"""
    from PySide6.QtMultimedia import QMediaPlayer

    sp = tts.LocalSpeaker()
    played: list[bytes] = []
    monkeypatch.setattr(sp, "_play", lambda data: played.append(data))
    monkeypatch.setattr(sp, "_pump", lambda: None)

    sp._playing = True
    sp._buffered = b"ready"
    sp._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)

    assert played == [b"ready"]
    assert sp._buffered is None
    assert sp._playing is False


def test_pump_keeps_synthesizing_while_playing(monkeypatch):
    """播放中也要继续合成下一句，否则每句之间都要干等一次合成。"""
    captured: list[str] = []

    class _Sig:
        def connect(self, fn):
            return None

    class _FakeSynth:
        def __init__(self, cfg, text, parent):
            captured.append(text)
            self.ready = self.failed = self.finished = _Sig()

        def start(self):
            return None

        def isRunning(self):
            return True     # 还在跑：这期间不该再起第二个合成线程

    monkeypatch.setattr(tts, "_Synth", _FakeSynth)
    sp = tts.LocalSpeaker()
    sp._playing = True
    sp._queue.extend(["第一句。", "第二句。"])

    sp._pump()
    assert captured == ["第一句。"], "播放中要开始合成下一句"

    sp._pump()
    assert captured == ["第一句。"], "已经在合成了，别并发"


def test_stop_clears_buffered_audio():
    sp = tts.LocalSpeaker()
    sp._buffered = b"stale"
    sp._playing = True
    sp.stop()
    assert sp._buffered is None
    assert sp._playing is False
    assert sp.pending == 0


def test_purge_speak_cache_keeps_current_process_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tts.tempfile, "gettempdir", lambda: str(tmp_path))
    folder = tmp_path / "teaggpet_tts"
    folder.mkdir()
    mine = folder / f"speak_{tts.os.getpid()}_1_1.wav"
    other = folder / "speak_999999_1_1.wav"
    mine.write_bytes(b"x")
    other.write_bytes(b"x")

    removed = tts.purge_speak_cache()
    assert removed == 1
    assert mine.exists(), "自己进程的可能正在播，不能动"
    assert not other.exists()


def test_walk_narration_clips_on_sentence_boundary():
    """超长文案要收在标点上，不能硬切出半句话。"""
    from pet.companion.walk import MAX_NARRATION_CHARS, _clip_sentence

    short = "河边风挺大～"
    assert _clip_sentence(short) == short

    long_text = "我在外面走了好一会儿，" + "看见河边的柳树被风吹得直晃，" * 12 + "就想着该回去了。"
    out = _clip_sentence(long_text)
    assert len(out) <= MAX_NARRATION_CHARS
    assert out.endswith(("。", "！", "？", "～", "…", "，")), f"切在半句上了：{out!r}"

    # 完全没有标点时才退化成硬切（总比整段不念好）
    no_punct = "啊" * 300
    assert len(_clip_sentence(no_punct)) <= MAX_NARRATION_CHARS


# ------------------------------------------------------------------ 合成缓存（固定台词复用）
def _cache_cfg(tmp_path, **overrides):
    cfg = {"api": "http://127.0.0.1:9880", "ref_audio": "ref.wav", "ref_text": "参考",
           "speed": 1.0, "text_lang": "zh", "prompt_lang": "zh",
           "cache_dir": str(tmp_path / "tts_cache")}
    cfg.update(overrides)
    return cfg


def test_cache_roundtrip_and_key_ignores_port(tmp_path):
    cfg = _cache_cfg(tmp_path)
    assert tts.cache_get(cfg, "好女孩……") is None
    tts.cache_put(cfg, "好女孩……", b"RIFF" + b"0" * 100)
    assert tts.cache_get(cfg, "好女孩……") == b"RIFF" + b"0" * 100

    # 换了端口（服务重拉）不该让缓存失效：权重没变，声音就是同一个
    moved = dict(cfg, api="http://127.0.0.1:9899")
    assert tts.cache_get(moved, "好女孩……") is not None

    # 换了参考音频 / 语速必须重新合成，不能拿旧声音糊弄
    assert tts.cache_get(dict(cfg, ref_audio="other.wav"), "好女孩……") is None
    assert tts.cache_get(dict(cfg, speed=1.3), "好女孩……") is None
    assert tts.cache_get(cfg, "别的台词") is None


def test_cache_put_prunes_oldest(tmp_path, monkeypatch):
    cfg = _cache_cfg(tmp_path)
    for index in range(5):
        tts.cache_put(cfg, f"句子{index}", b"x" * 100)
        # 让 mtime 拉开，避免同秒内排序不稳
        path = tts._cache_path(cfg, f"句子{index}")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    removed = tts.prune_cache(tmp_path / "tts_cache", max_files=2)
    assert removed == 3
    left = sorted(p.name for p in (tmp_path / "tts_cache").glob("*.wav"))
    assert len(left) == 2
    assert tts.cache_get(cfg, "句子4") is not None, "最新的要留下"
    assert tts.cache_get(cfg, "句子0") is None


def test_cache_hit_skips_synthesis(tmp_path, monkeypatch):
    cfg = _cache_cfg(tmp_path)
    calls: list[str] = []

    def fake_synth(_cfg, text, **_kw):
        calls.append(text)
        return b"RIFF" + b"1" * 80, ""

    monkeypatch.setattr(tts, "synthesize_ex", fake_synth)
    texts = ["好女孩……", "欧鲸鲸……"]

    first = tts.precache_texts(cfg, texts)
    assert first == 2 and len(calls) == 2

    calls.clear()
    second = tts.precache_texts(cfg, texts)
    assert second == 0, "第二次全部命中缓存"
    assert calls == [], "命中缓存就不该再调模型"


def test_precache_dedupes_and_splits_like_playback(tmp_path, monkeypatch):
    cfg = _cache_cfg(tmp_path)
    calls: list[str] = []

    def fake_synth(_cfg, text, **_kw):
        calls.append(text)
        return b"RIFF" + b"1" * 80, ""

    monkeypatch.setattr(tts, "synthesize_ex", fake_synth)
    long_text = "今天也要认真工作呀。再陪你一会儿。"
    tts.precache_texts(cfg, [long_text, long_text])
    assert calls == tts.split_for_speech(long_text), "预热断句必须和播放一致"
    assert len(set(calls)) == len(calls), "重复台词只合成一次"


def test_speaker_plays_cached_sentence_without_synth(tmp_path, monkeypatch):
    cfg = _cache_cfg(tmp_path)
    # 播放侧查的是「断句之后」的句子，缓存键也按它算（预热与播放同一口径）
    sentence = tts.split_for_speech("好女孩……")[0]
    tts.cache_put(cfg, sentence, b"RIFF" + b"2" * 60)

    sp = tts.LocalSpeaker()
    played: list[bytes] = []
    monkeypatch.setattr(sp, "_play", lambda data: played.append(data))
    monkeypatch.setattr(tts, "_Synth", lambda *a, **kw: pytest.fail("命中缓存不该起合成线程"))

    assert sp.speak("好女孩……", cfg) is True
    assert played == [b"RIFF" + b"2" * 60]


def test_speaker_writes_audio_back_to_cache(tmp_path, monkeypatch):
    cfg = _cache_cfg(tmp_path)
    sentence = tts.split_for_speech("欧鲸鲸……")[0]
    sp = tts.LocalSpeaker()
    sp._cfg = dict(cfg)
    monkeypatch.setattr(sp, "_play", lambda data: None)

    sp._on_ready(b"RIFF" + b"3" * 70, sp._gen, sentence)
    assert tts.cache_get(cfg, sentence) == b"RIFF" + b"3" * 70, "合成结果要顺手存缓存"


def test_play_data_replaces_queue_and_plays(tmp_path, monkeypatch):
    """「试听一句」走统一播报出口，不再自己养播放器。"""
    cfg = _cache_cfg(tmp_path)
    sp = tts.LocalSpeaker()
    played: list[bytes] = []
    monkeypatch.setattr(sp, "_play", lambda data: played.append(data))
    sp._queue.extend(["旧的一句。"])

    assert sp.play_data(b"RIFF-preview", cfg) is True
    assert played == [b"RIFF-preview"]
    assert sp.pending == 0, "试听要打断而不是排队"
    assert sp.play_data(b"", cfg) is False


# ------------------------------------------------------------------ 设置页任务回调线程
def test_settings_task_result_is_delivered_on_gui_thread():
    """后台任务的 ok 回调必须回到 GUI 线程。

    回调里要碰 QMediaPlayer（试听）和控件；在工作线程里建播放器就是
    「提示合成成功、但一点声音都没有」的原因。
    """
    import time

    from PySide6.QtCore import QObject

    from pet.companion.settings_dialog import CompanionSettingsDialog

    class _Host(QObject):
        def __init__(self):
            super().__init__()
            self._task = None
            self._bridge = None

        def _on_task_error(self, message):  # noqa: ANN001
            raise AssertionError(f"不该走失败分支：{message}")

    host = _Host()
    run_task = CompanionSettingsDialog._run_task.__get__(host, _Host)
    main_ident = threading.get_ident()
    seen: list[int] = []
    started = threading.Event()

    def work():
        started.set()
        return threading.get_ident()

    run_task(work, lambda result: seen.append(threading.get_ident()))
    deadline = time.monotonic() + 5.0
    while not seen and time.monotonic() < deadline:
        _qapp.processEvents()
        time.sleep(0.01)
    assert seen == [main_ident], "回调跑到工作线程去了"
    assert host._task is None, "任务收尾要清掉引用"


def test_settings_task_error_is_delivered_on_gui_thread():
    import time

    from PySide6.QtCore import QObject

    from pet.companion.settings_dialog import CompanionSettingsDialog

    class _Host(QObject):
        def __init__(self):
            super().__init__()
            self._task = None
            self._bridge = None
            self.errors = []

        def _on_task_error(self, message):  # noqa: ANN001
            self.errors.append((message, threading.get_ident()))

    host = _Host()
    run_task = CompanionSettingsDialog._run_task.__get__(host, _Host)
    main_ident = threading.get_ident()

    def boom():
        raise RuntimeError("接口没起来")

    run_task(boom, lambda _r: pytest.fail("不该走成功分支"))
    deadline = time.monotonic() + 5.0
    while not host.errors and time.monotonic() < deadline:
        _qapp.processEvents()
        time.sleep(0.01)
    assert host.errors and host.errors[0][0].find("接口没起来") >= 0
    assert host.errors[0][1] == main_ident
    assert host._task is None


def test_precache_self_talk_voice_hands_over_to_local_cache(monkeypatch):
    """本地语音接管后，宿主那份预缓存不再跑——改由本地缓存预热接手。

    以前这里直接 return False：点击台词每次都要现合成 2~10 秒，
    表现就是「点了半天才说话」。
    """
    from types import SimpleNamespace

    from pet.app import AppShell
    from pet.companion import runtime

    calls: list[str] = []
    monkeypatch.setattr(runtime, "precache_self_talk_texts",
                        lambda app, reason="": calls.append(reason) or True)
    stub = SimpleNamespace(_local_tts_taken_over=lambda: True)
    fn = AppShell.precache_self_talk_voice.__get__(stub, SimpleNamespace)

    assert fn("保存设置") is True
    assert calls == ["保存设置"], "本地接管时要走本地缓存预热"


def test_self_talk_texts_collects_every_slot(monkeypatch):
    """点击台词名单按每只宠的 slot 档案收集（seam 与宿主既有口径一致）。"""
    from types import SimpleNamespace

    from pet.companion import runtime

    class _View(dict):
        pass

    app = SimpleNamespace(_self_talk_voice_configs=lambda: [
        _View(self_talk_texts=["好女孩……", "欧鲸鲸……"]),
        _View(self_talk_texts=["欧鲸鲸……", "再陪你一会儿。"]),
    ])
    assert runtime._self_talk_texts(app) == ["好女孩……", "欧鲸鲸……", "再陪你一会儿。"]

    app_broken = SimpleNamespace(_self_talk_voice_configs=lambda: [object()])
    assert runtime._self_talk_texts(app_broken) == []

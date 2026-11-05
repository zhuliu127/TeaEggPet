# -*- coding: utf-8 -*-
"""主动对话：间隔/免打扰/上限的纯逻辑，以及「说出来 → 落进 AI 对话」的落盘行为。"""
from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pet.companion import bootstrap, proactive_chat as pc
from pet.companion.proactive_chat import (
    ProactiveChatService, clean_reply, clamp_interval, in_quiet,
    minutes_until_window_end, parse_quiet_hours, pick_delay_minutes,
)
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


# ------------------------------------------------------------------ 免打扰
@pytest.mark.parametrize("text,expected", [
    ("0-8", (0, 480)),
    ("23:30-7:00", (1410, 420)),
    ("1 - 7", (60, 420)),
    ("0-24", None),   # 全天静音没有意义，按「没配」处理
    ("8-8", None),
    ("", None),
    ("abc", None),
    ("25-30", None),
])
def test_parse_quiet_hours(text, expected):
    got = parse_quiet_hours(text)
    if expected is None:
        assert got is None
    else:
        # 0-24 会被判成等长区间 → None，其它按分钟
        assert got == expected


def test_in_quiet_normal_and_cross_midnight():
    day = parse_quiet_hours("0-8")
    assert in_quiet(0, day) and in_quiet(7 * 60 + 59, day)
    assert not in_quiet(8 * 60, day) and not in_quiet(12 * 60, day)

    night = parse_quiet_hours("23:30-7:00")
    assert in_quiet(23 * 60 + 45, night) and in_quiet(3 * 60, night)
    assert not in_quiet(12 * 60, night) and not in_quiet(7 * 60, night)


def test_minutes_until_window_end():
    night = parse_quiet_hours("23:30-7:00")
    assert minutes_until_window_end(23 * 60 + 30, night) == 450  # 到 07:00
    assert minutes_until_window_end(6 * 60, night) == 60


# ------------------------------------------------------------------ 间隔
def test_clamp_interval():
    assert clamp_interval(45, 120) == (45, 120)
    assert clamp_interval(200, 50) == (50, 200), "反过来填也要能用"
    assert clamp_interval(0, 0) == (1, 1)
    assert clamp_interval(9999, 3) == (3, 720)
    assert clamp_interval("x", None) == (45, 120)


def test_pick_delay_within_bounds():
    for _ in range(50):
        value = pick_delay_minutes(30, 60)
        assert 30 <= value <= 60
    assert pick_delay_minutes(40, 40) == 40.0


# ------------------------------------------------------------------ 文案
def test_clean_reply_strips_wrap():
    assert clean_reply("「我出门啦～」") == "我出门啦"
    assert clean_reply('"  在楼下  买咖啡 "') == "在楼下 买咖啡"  # 空白统一压成一个
    assert clean_reply("一\n二") == "一 二"
    assert len(clean_reply("啊" * 300)) == 120


# ------------------------------------------------------------------ 服务
def test_fire_writes_to_chat_and_counts(tmp_path):
    svc = _service(tmp_path, companion_proactive_daily_limit=2)
    svc._generate = lambda: "刚从楼下买了杯冰美式"
    written = []
    svc._append_to_chat = lambda text: written.append(text) or True
    svc._notify = lambda text: None

    assert svc.fire_now() is True
    assert written == ["刚从楼下买了杯冰美式"]
    assert svc._count_today() == 1
    assert svc.fire_now() is True
    assert svc._daily_cap_reached() is True, "到每日上限后应停下"

    svc._msg_for_test = None
    svc._state["date"] = "2000-01-01"  # 换天：计数归零
    assert svc._count_today() == 0


def test_service_reads_intervals_from_config(tmp_path):
    svc = _service(tmp_path, companion_proactive_min_minutes=10,
                   companion_proactive_max_minutes=20,
                   companion_proactive_quiet_hours="1-6",
                   companion_proactive_daily_limit=5,
                   companion_proactive_max_unanswered=2)
    assert svc.interval() == (10, 20)
    assert svc.quiet_window() == (60, 360)
    assert svc.daily_limit() == 5
    assert svc.max_unanswered() == 2
    assert svc.enabled is True


def test_unanswered_counts_tail_assistant_messages(tmp_path):
    from pet.chat.models import ChatMessage, ChatSession
    from pet.chat.session_store import SessionStore

    svc = _service(tmp_path)
    store = SessionStore(svc.cfg.dir, getattr(svc.cfg, "instance_id", ""))
    character = svc._character_id()
    session = ChatSession.create(character, "p", "s")
    session.messages.extend([
        ChatMessage(role="user", content="在吗"),
        ChatMessage(role="assistant", content="在的"),
        ChatMessage(role="assistant", content="刚才看到一只猫"),
    ])
    store.save(session)
    assert svc._unanswered() == 2

    updated = store.load(session.session_id, character)
    store.append_messages(updated, [ChatMessage(role="user", content="回来啦")])
    assert svc._unanswered() == 0


def test_append_to_chat_creates_session_when_missing(tmp_path):
    from pet.chat.session_store import SessionStore

    svc = _service(tmp_path)
    store = SessionStore(svc.cfg.dir, getattr(svc.cfg, "instance_id", ""))
    assert store.list(svc._character_id()) == []

    assert svc._append_to_chat("喂，我回来啦") is True
    sessions = store.list(svc._character_id())
    assert len(sessions) == 1
    assert sessions[0].messages[-1].role == "assistant"
    assert sessions[0].messages[-1].content == "喂，我回来啦"


def test_disabled_service_does_not_start(tmp_path):
    svc = _service(tmp_path, companion_proactive_enabled=False)
    svc.start()
    assert svc._timer.isActive() is False
    svc.reload()
    assert svc._timer.isActive() is False


# ------------------------------------------------------------------ 启动补日程
def test_needs_schedule_detects_empty_day(tmp_path):
    from pet.companion.store import CompanionStore
    from pet.companion import schedule

    store = CompanionStore(Config(base=tmp_path))
    key = schedule.date_key(datetime.now())
    assert bootstrap.needs_schedule(store, key) is True

    schedule.write_day(store, {"date": key, "title": "正常的一天",
                               "items": [{"start": "00:00", "end": "24:00", "activity": "睡觉"}]})
    assert bootstrap.needs_schedule(store, key) is False


def test_ensure_today_respects_switches(tmp_path):
    cfg = Config(base=tmp_path)
    cfg.set("companion_schedule_autofill", False)
    assert bootstrap.ensure_today(_FakeApp(cfg)) is False

    cfg.set("companion_schedule_autofill", True)
    cfg.set("companion_schedule_enabled", False)
    assert bootstrap.ensure_today(_FakeApp(cfg)) is False


# ------------------------------------------------------------------ 识屏问答落盘
def test_character_for_chat_sync_never_raises():
    """PetInstance 没有 _identity_config 时不能炸（历史 AttributeError）。"""
    from pet.app import _character_for_chat_sync

    class _Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    class _Instance:
        def __init__(self):
            self.config = _Cfg(character="shenshen")

    class _Shell:
        def __init__(self, character):
            self._character = character

        def _identity_config(self):
            return _Cfg(character=self._character)

    # 1) 自己就有身份视图
    inst = _Instance()
    inst._identity_config = lambda: _Cfg(character="ali")
    assert _character_for_chat_sync(inst) == "ali"

    # 2) 自己没方法、壳上有（真实拓扑：PetInstance.shell → AppShell）
    inst2 = _Instance()
    inst2.shell = _Shell("dafeiyu")
    assert _character_for_chat_sync(inst2) == "dafeiyu"

    # 3) 都没有 → 退回主配置
    assert _character_for_chat_sync(_Instance()) == "shenshen"


# ------------------------------------------------------------------ 记忆注入
def test_memory_block_is_injected_into_prompt(tmp_path, monkeypatch):
    from pet.chat import memory as memory_mod

    monkeypatch.setattr(memory_mod, "injection_text",
                        lambda *a, **k: "主人是 A 型血，怕苦，喝咖啡要加奶。")
    svc = _service(tmp_path, companion_proactive_use_memory=True,
                   companion_proactive_history_count=0)
    prompt = svc._build_prompt()
    assert "【你记得的事】" in prompt
    assert "A 型血" in prompt


def test_memory_block_respects_switch(tmp_path, monkeypatch):
    from pet.chat import memory as memory_mod

    monkeypatch.setattr(memory_mod, "injection_text", lambda *a, **k: "不该出现")
    svc = _service(tmp_path, companion_proactive_use_memory=False)
    assert svc._memory_block() == ""

    # 聊天里关了记忆，这里也要关
    svc2 = _service(tmp_path, companion_proactive_use_memory=True)
    svc2.cfg.set("chat_memory_enabled", False)
    assert svc2._memory_block() == ""


def test_history_count_limits_tail(tmp_path):
    from pet.chat.models import ChatMessage, ChatSession
    from pet.chat.session_store import SessionStore

    svc = _service(tmp_path, companion_proactive_history_count=2)
    store = SessionStore(svc.cfg.dir, getattr(svc.cfg, "instance_id", ""))
    session = ChatSession.create(svc._character_id(), "p", "s")
    session.messages.extend([ChatMessage(role="user" if i % 2 else "assistant",
                                         content=f"第{i}条") for i in range(6)])
    store.save(session)

    tail = svc._recent_tail(svc._history_count())
    lines = tail.split("你：") if tail else []
    assert "第4条" in tail and "第5条" in tail
    assert "第0条" not in tail and "第1条" not in tail
    assert len(lines) == 2


# ------------------------------------------------------------------ 截图提问（numpy 可选）
def test_hdr_capture_degrades_without_numpy(monkeypatch):
    """打包漏收 numpy 时，截图链路必须还能 import 并回落到 GDI。"""
    from pet.screen_capture import hdr_capture as h

    monkeypatch.setattr(h, "np", None)
    assert h.hdr_capture_disabled() is True
    assert h.grab_hdr_sdr(None) is None


def test_screen_capture_actions_import_without_numpy(monkeypatch):
    import importlib
    import sys

    for name in [n for n in list(sys.modules) if n.startswith("pet.screen_capture")]:
        sys.modules.pop(name, None)
    from pet.screen_capture import hdr_capture as h

    monkeypatch.setattr(h, "np", None)
    mod = importlib.import_module("pet.screen_capture.actions")
    assert hasattr(mod, "actions_for")

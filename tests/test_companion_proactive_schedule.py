# -*- coding: utf-8 -*-
"""主动对话的排程：别让「打开过一次设置」或「一直没回」把频率吃光。

用户实测：设置是 45–120 分钟一次，实际一天只说一两次。两个原因都在这儿：

1. ``reload()`` 每次设置页关闭都无条件重排，把还剩 50 分钟的计时器又推回 120 分钟，
   多开几次设置就永远等不到；
2. ``_unanswered() >= 上限`` 直接 ``_schedule_at_next_morning()``，等于当天封口；
   而且未回复只数「最近一个会话」，用户在快速对话/灵动岛里回了也不算。

钉住：窗口内保留原计划、窗口外才重排、任意会话有回复就算已回、超上限只跳过这一轮。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pet.companion.proactive_chat import ProactiveChatService
from pet.companion.store import CompanionStore
from pet.config import Config


class _FakeApp:
    def __init__(self, config):
        self.config = config


class _Msg:
    def __init__(self, role: str, content: str = "x"):
        self.role = role
        self.content = content
        self.created_at = datetime.now().isoformat(timespec="seconds")


class _Session:
    def __init__(self, messages):
        self.messages = messages
        self.updated_at = datetime.now().isoformat(timespec="seconds")


@pytest.fixture
def svc(tmp_path):
    cfg = Config(base=tmp_path)
    store = CompanionStore(cfg)
    store.set("companion_proactive_enabled", True)
    store.set("companion_proactive_min_minutes", 45)
    store.set("companion_proactive_max_minutes", 120)
    store.set("companion_proactive_max_unanswered", 4)
    store.set("companion_proactive_quiet_hours", "22-8")
    service = ProactiveChatService(_FakeApp(cfg))
    service._stopped = False
    yield service
    service.stop()


def test_reload_keeps_plan_inside_window(svc, monkeypatch):
    """还剩 50 分钟（在 45–120 之内）：不该重排。"""
    calls = []
    monkeypatch.setattr(svc, "_schedule_next", lambda: calls.append(1))
    monkeypatch.setattr(svc, "quiet_window", lambda: None)
    svc._next_at = datetime.now() + timedelta(minutes=50)
    svc.reload()
    assert calls == [], "设置页关一次就重排，会把这 50 分钟又推回 120 分钟"


def test_reload_reschedules_outside_window(svc, monkeypatch):
    """还剩 200 分钟（超出 120）：必须按新参数重排。"""
    calls = []
    monkeypatch.setattr(svc, "_schedule_next", lambda: calls.append(1))
    monkeypatch.setattr(svc, "quiet_window", lambda: None)
    svc._next_at = datetime.now() + timedelta(minutes=200)
    svc.reload()
    assert calls == [1]


def test_unanswered_counts_zero_when_user_replied_elsewhere(svc, monkeypatch):
    """快速对话/灵动岛里回过了，就算已回复，不能当成「4 条没人理」。"""
    monkeypatch.setattr(svc, "_latest_session",
                        lambda: _Session([_Msg("assistant"), _Msg("assistant"), _Msg("assistant")]))
    monkeypatch.setattr(svc, "_replied_since", lambda stamp: True)
    assert svc._unanswered() == 0


def test_unanswered_counts_when_silent(svc, monkeypatch):
    monkeypatch.setattr(svc, "_latest_session", lambda: _Session([_Msg("user"), _Msg("assistant"),
                                                                  _Msg("assistant")]))
    monkeypatch.setattr(svc, "_replied_since", lambda stamp: False)
    assert svc._unanswered() == 2


def test_tick_skips_only_this_round(svc, monkeypatch):
    """到上限时只跳过这一轮，不能再排到明天早上。"""
    scheduled = []
    monkeypatch.setattr(svc, "_unanswered", lambda: 9)
    monkeypatch.setattr(svc, "_daily_cap_reached", lambda: False)
    monkeypatch.setattr(svc, "quiet_window", lambda: None)
    monkeypatch.setattr(svc, "_schedule_next", lambda: scheduled.append(1))
    monkeypatch.setattr(svc, "fire_now", lambda **k: pytest.fail("到上限就不该开口"))
    monkeypatch.setattr(svc, "_schedule_at_next_morning",
                        lambda: pytest.fail("不该封口到明天"))
    svc._tick()
    assert scheduled == [1], "跳过这一轮后要继续排下一轮"


def test_arm_logs_next_time(svc, caplog):
    svc._arm(30)
    assert svc._next_at is not None
    assert (svc._next_at - datetime.now()).total_seconds() > 60

# -*- coding: utf-8 -*-
"""溜达的两件「看起来才像真的出门」的事。

1) 出门先播奔跑动画、再藏起桌宠；回家先回桌面、再跑一段、然后才说话。
2) 溜达期间 AI 对话要知道她在外面（不再按日程里「此刻在公司」回答）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QApplication

from pet.companion import runtime as runtime_mod
from pet.companion.walk import WalkService
from pet.config import Config


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    def __init__(self, cfg) -> None:
        self.config = cfg
        self.notes: list[tuple[str, str]] = []

    def system_notify(self, title, message, duration_ms=7000):
        self.notes.append((title, message))


class _FakeHost:
    """够用的假宿主：能播动画 + 能报出可播的素材名。"""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.moves = ["漂浮踏步", "左转奔跑", "螃蟹走路"]

    def switch_clip(self, name):
        self.played.append(str(name))
        return True

    def trigger_move(self, name):
        self.played.append(str(name))
        return True


@pytest.fixture()
def service(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    app = _FakeApp(cfg)
    svc = WalkService(app)
    host = _FakeHost()
    visible: list[bool] = []
    monkeypatch.setattr(runtime_mod, "move_host", lambda: host)
    monkeypatch.setattr(runtime_mod, "set_pet_visible", lambda v: visible.append(bool(v)) or True)
    svc._say = lambda *a, **k: None            # 不真的念
    svc._notify = lambda *a, **k: None         # 不真的弹通知
    svc._narrate = lambda *a, **k: None        # 不真的调 LLM
    svc._take_selfie = lambda *a, **k: None    # 不真的画图
    yield svc, host, visible, cfg
    svc.shutdown()


# ---------------------------------------------------------------- 动画与显隐
def test_start_plays_run_clip_then_hides(service):
    svc, host, visible, _ = service
    assert svc.start()["ok"] is True
    assert host.played == ["左转奔跑"], "出门要先播奔跑动画"
    assert visible == [], "动画播完之前不能先藏"
    svc._hide_pet(svc._vis_token)
    assert visible == [False], "跑完就藏起来（像真的走出去了）"
    assert svc._hidden is True


def test_stop_shows_pet_back(service, qapp):
    svc, host, visible, _ = service
    svc.start()
    svc._hide_pet(svc._vis_token)
    host.played.clear()
    assert svc.stop("user")["ok"] is True
    assert visible == [False, True], "回家先把桌宠放回桌面"
    from PySide6.QtTest import QTest

    QTest.qWait(200)              # 等「回来跑一段」那条 60ms 单次定时器
    svc._on_back_home(svc._vis_token, "user")
    assert host.played == ["左转奔跑"], "回到桌面后要跑一段进来"
    assert svc._hidden is False


def test_stale_exit_timer_does_not_hide_after_return(service):
    svc, host, visible, _ = service
    svc.start()
    stale = svc._vis_token
    svc._hide_pet(stale)
    svc.stop("user")
    # 旧的那次「跑完就藏」回调迟到（换了一轮），不能把刚放回来的她再藏一次
    svc._hide_pet(stale)
    assert visible == [False, True]


def test_shutdown_restores_visibility(service):
    svc, host, visible, _ = service
    svc.start()
    svc._hide_pet(svc._vis_token)
    svc.shutdown()
    assert visible == [False, True], "退出前必须把桌宠放回来"


def test_run_clip_prefers_running_material(service):
    svc, host, _, _ = service
    assert svc._run_clip_name() == "左转奔跑"
    host.moves = ["漂浮踏步"]            # 角色包没有「跑」的素材就退回任意移动素材
    assert svc._run_clip_name() == "漂浮踏步"


# ---------------------------------------------------------------- 对话上下文
def test_chat_context_empty_when_not_walking(service):
    svc, *_ = service
    assert svc.chat_context() == ""


def test_chat_context_says_she_is_out(service):
    svc, host, _, _ = service
    svc._started_at = datetime.now()
    svc._ends_at = datetime.now() + timedelta(minutes=20)
    svc._minutes = 20
    text = svc.chat_context()
    assert "外面溜达" in text
    assert "不在电脑前" in text
    assert "分钟" in text
    assert "日程" in text, "要说明别按日程里此刻的活动回答"


def test_prompt_provider_puts_walk_state_before_schedule(monkeypatch):
    """runtime 的 provider 把「溜达状态」和「日程」拼起来，溜达那段在前。"""
    from pet.chat import prompt as chat_prompt
    from pet.companion import schedule as schedule_mod

    class _Svc:
        def chat_context(self):
            return "【此刻状态】你现在正在外面溜达。"

    monkeypatch.setattr(runtime_mod, "_service", _Svc())
    monkeypatch.setattr(runtime_mod, "companion_store", lambda: object())
    monkeypatch.setattr(schedule_mod, "chat_context", lambda store: "【你今天的生活】此刻在公司。")
    runtime_mod._hook_chat_schedule()
    text = chat_prompt.companion_section()
    assert "外面溜达" in text
    assert "你今天的生活" in text
    assert text.index("外面溜达") < text.index("你今天的生活")
    chat_prompt.set_companion_context(None)


def test_prompt_provider_is_schedule_only_when_not_walking(monkeypatch):
    """没在溜达时注入内容必须与「只有日程」逐字节一致（老行为不变）。"""
    from pet.chat import prompt as chat_prompt
    from pet.companion import schedule as schedule_mod

    class _Svc:
        def chat_context(self):
            return ""

    monkeypatch.setattr(runtime_mod, "_service", _Svc())
    monkeypatch.setattr(runtime_mod, "companion_store", lambda: object())
    monkeypatch.setattr(schedule_mod, "chat_context", lambda store: "【你今天的生活】此刻在公司。")
    runtime_mod._hook_chat_schedule()
    assert chat_prompt.companion_section() == "【你今天的生活】此刻在公司。"
    chat_prompt.set_companion_context(None)

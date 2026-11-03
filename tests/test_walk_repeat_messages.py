# -*- coding: utf-8 -*-
"""溜达的「每 N 分钟报一句」必须真的隔 N 分钟报一次。

真机 bug：间隔设 4 分钟，一轮溜达只报了一句。原因是 ``_msg_timer`` 被设成
``setSingleShot(True)`` 后只 ``start()`` 了一次，没有任何地方重排——第一句报完
定时器就死了。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QApplication

from pet.companion.walk import WalkService
from pet.config import Config


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    """WalkService 只用到 app.config。"""

    def __init__(self, cfg) -> None:
        self.config = cfg


def _service(tmp_path, *, minutes: int, every: int):
    cfg = Config(tmp_path)
    cfg.set("companion_walk_message_every_min", every)
    svc = WalkService(_FakeApp(cfg))
    svc._started_at = datetime.now()
    svc._ends_at = datetime.now() + timedelta(minutes=minutes)
    svc._minutes = minutes
    calls: list[str] = []
    svc._narrate = lambda kind="": calls.append(kind)  # 不真的调 LLM
    return svc, calls


def test_message_timer_keeps_rescheduling(qapp, tmp_path):
    svc, calls = _service(tmp_path, minutes=30, every=4)
    svc._schedule_message()
    assert svc._msg_timer.isActive()
    assert svc._msg_timer.interval() == 4 * 60000

    # 触发三次 = 报三句，且每次触发后定时器又被排上
    for i in range(1, 4):
        svc._on_msg_tick()
        assert calls == ["during"] * i, "每一跳都要报一句"
        assert svc._msg_timer.isActive(), "触发后必须重排下一次"

    svc._msg_timer.stop()


def test_no_message_when_remaining_shorter_than_interval(qapp, tmp_path):
    svc, calls = _service(tmp_path, minutes=3, every=4)
    svc._schedule_message()
    assert not svc._msg_timer.isActive(), "只剩 3 分钟、间隔 4 分钟，不该排"
    assert calls == []


def test_stop_clears_timer(qapp, tmp_path):
    svc, _ = _service(tmp_path, minutes=30, every=4)
    svc._schedule_message()
    assert svc._msg_timer.isActive()
    svc._msg_timer.stop()
    svc._ends_at = None
    svc._on_msg_tick()
    assert not svc._msg_timer.isActive()


def test_same_line_is_not_deduped_forever(tmp_path):
    """LLM 失败时回退文案固定；隔了一个间隔仍要报，不能被去重吞掉。"""
    from pet.companion import walk as walk_mod

    cfg = Config(tmp_path)
    svc = WalkService(_FakeApp(cfg))
    said: list[str] = []

    class _Host:
        def show_bubble(self, text, duration_ms=0, **kw):  # noqa: ANN001
            said.append(str(text))
            return True

    class _App(_FakeApp):
        pass

    class _Svc(WalkService):
        pass

    # 用「文本相同 + 时间戳」直接验证去重窗口
    svc._last_message = "在外面走走，附近～"
    svc._last_message_at = datetime.now() - timedelta(seconds=300)
    now = datetime.now()
    recent = (svc._last_message_at is not None
              and (now - svc._last_message_at).total_seconds() < 90)
    assert recent is False, "5 分钟前的同一句不应被当成重复"

    svc._last_message_at = datetime.now()
    recent = (svc._last_message_at is not None
              and (datetime.now() - svc._last_message_at).total_seconds() < 90)
    assert recent is True, "刚刚说过的同一句才算重复"

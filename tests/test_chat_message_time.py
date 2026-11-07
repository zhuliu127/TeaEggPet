# -*- coding: utf-8 -*-
"""对话气泡的时间戳（微信那种「每句下面一小行时间」）。

用户需求：AI 对话里的记录要能看到每句的日期时间。
- 今天只显示 ``15:20``；今年内 ``11-06 15:20``；跨年补年份；
- 脏时间/空时间一律不显示（不能因为一条坏数据把整段对话渲染搞崩）；
- 气泡：给了时间就显示在气泡下方，用户靠右、她靠左；空串整行隐藏。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QApplication

from pet.chat.utils import format_message_time


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_today_shows_clock_only():
    now = datetime.now().astimezone()
    assert format_message_time(_utc(now), now=now) == now.strftime("%H:%M")


def test_this_year_shows_month_day():
    now = datetime(2026, 11, 7, 12, 0).astimezone()
    earlier = now - timedelta(days=3)
    assert format_message_time(_utc(earlier), now=now) == earlier.strftime("%m-%d %H:%M")


def test_other_year_includes_year():
    now = datetime(2026, 11, 7, 12, 0).astimezone()
    old = datetime(2025, 12, 31, 23, 10).astimezone()
    assert format_message_time(_utc(old), now=now) == "2025-12-31 23:10"


def test_bad_or_empty_returns_blank():
    assert format_message_time("") == ""
    assert format_message_time("not-a-time") == ""


@pytest.fixture
def app():
    inst = QApplication.instance() or QApplication([])
    yield inst


def test_bubble_shows_time_label(app):
    from pet.chat.widgets import MessageBubble

    bubble = MessageBubble("user", "在吗", "test-character", time_text="15:20")
    try:
        assert bubble.time.text() == "15:20"
        assert not bubble.time.isHidden()
    finally:
        bubble.deleteLater()


def test_bubble_hides_label_when_empty(app):
    from pet.chat.widgets import MessageBubble

    bubble = MessageBubble("assistant", "在的", "test-character", time_text="")
    try:
        assert bubble.time.isHidden(), "没有时间就不该留一行空白"
    finally:
        bubble.deleteLater()

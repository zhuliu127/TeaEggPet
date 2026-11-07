# -*- coding: utf-8 -*-
"""AI 对话与每日日程联动。

背景（用户实测）：问「在干什么」，回答和当天日程（比如「赶新需求加班」）无关——
日程以前只喂给了主动对话和自拍，聊天侧 system 里只有时间和人设。

钉住这些事：
- ``schedule.chat_context`` 把「此刻在做什么」压成一段事实：时段、地点、活动、
  穿着、接下来两段，外加一句「按这段回答」的约束；
- 没日程 / 读失败一律返回空串，聊天提示词逐字节回到接钩子之前；
- 注入位置在时间信息之后、记忆之前；扮演场景开启时不注入（那是另一段人生）；
- 显式传 ``companion_context=""`` 的入口（文件解读）不注入；
- ``runtime._hook_chat_schedule`` 注册的 provider 取的就是日程，没有 store 时为空。
"""
from __future__ import annotations

import pathlib
from datetime import datetime

import pytest

from pet.chat.models import ChatMessage, ChatSettings
from pet.chat.prompt import PromptBuilder, set_companion_context
from pet.companion import runtime as companion_runtime
from pet.companion import schedule
from pet.companion.store import CompanionStore
from pet.config import Config

NOW = datetime(2026, 11, 6, 15, 20).astimezone()
DAY_KEY = "2026-11-06"

ITEMS = [
    {"start": "00:00", "end": "07:30", "activity": "睡觉", "location": "家", "detail": ""},
    {"start": "07:30", "end": "09:00", "activity": "洗漱通勤", "location": "路上", "detail": "地铁很挤"},
    {"start": "09:00", "end": "18:00", "activity": "赶新需求加班", "location": "公司",
     "detail": "第三版方案又被推翻，晚饭点了外卖", "outfit": "米色针织衫",
     "outfitNote": "外套搭在椅背上"},
    {"start": "18:00", "end": "19:30", "activity": "收尾交接", "location": "公司", "detail": "把测试用例补完"},
    {"start": "19:30", "end": "20:10", "activity": "通勤回家", "location": "路上", "detail": "打车"},
    {"start": "20:10", "end": "23:30", "activity": "窝在房间", "location": "家", "detail": "躺平刷手机"},
    {"start": "23:30", "end": "24:00", "activity": "睡觉", "location": "家", "detail": ""},
]


@pytest.fixture(autouse=True)
def _clean_provider():
    """provider 是模块级全局：每个用例前后都清掉，别互相污染。"""
    set_companion_context(None)
    yield
    set_companion_context(None)


def _store(tmp_path, title: str = "赶新需求加班", day_key: str = DAY_KEY) -> CompanionStore:
    cfg = Config(base=tmp_path)
    store = CompanionStore(cfg)
    schedule.write_day(store, {
        "date": day_key, "title": title, "mood": "有点累但还行",
        "dayType": {"id": "overtime", "name": "加班日"}, "items": ITEMS,
    })
    return store


def _system(**kwargs) -> str:
    kwargs.setdefault("role_prompt", "你是测试角色。")
    kwargs.setdefault("now", NOW)
    messages = PromptBuilder().build_messages(
        ChatSettings(), "test-character", [], "在干什么", **kwargs
    )
    return messages[0]["content"]


# ------------------------------------------------------------------ chat_context
def test_chat_context_has_time_place_activity(tmp_path):
    text = schedule.chat_context(_store(tmp_path), NOW)
    assert "15:20" in text, "要报当前时间"
    assert "09:00–18:00" in text, "要给出当前时段的起止"
    assert "公司" in text and "赶新需求加班" in text, "地点与活动是回答的核心"
    assert "第三版方案又被推翻" in text, "detail 决定回答像不像真的在过这一天"
    assert "米色针织衫" in text, "穿什么也要能答"
    assert "18:00 收尾交接" in text, "接下来两段让人感到这一天在往前走"
    assert "第一人称" in text and "不要声称自己没有生活" in text, "要有防跑偏的约束"


def test_chat_context_skips_repeated_theme(tmp_path):
    """title 与 activity 相同时不重复说一遍。"""
    text = schedule.chat_context(_store(tmp_path), NOW)
    assert text.count("赶新需求加班") >= 1
    assert "今天的主题" not in text


def test_chat_context_keeps_distinct_theme(tmp_path):
    text = schedule.chat_context(_store(tmp_path, title="被需求追着跑的一天"), NOW)
    assert "今天的主题：被需求追着跑的一天" in text


def test_chat_context_empty_without_schedule(tmp_path):
    cfg = Config(base=tmp_path)
    store = CompanionStore(cfg)
    assert schedule.chat_context(store, NOW) == "", "没生成日程就不该注入"


def test_chat_context_other_day_returns_empty(tmp_path):
    """别的日期（跨天）不该拿昨天的日程回答。"""
    store = _store(tmp_path)
    assert schedule.chat_context(store, datetime(2026, 11, 7, 15, 20)) == ""


def test_chat_context_no_double_period(tmp_path):
    """detail 自带句号时不要叠成「。。」（日程生成器常带）。"""
    text = schedule.chat_context(_store(tmp_path), NOW)
    assert "走。。" not in text and "。。" not in text
    assert "临下班" in text or "第三版方案又被推翻" in text


def test_chat_context_night_slot(tmp_path):
    night = datetime(2026, 11, 6, 23, 50).astimezone()
    text = schedule.chat_context(_store(tmp_path), night)
    assert "23:30–24:00" in text and "睡觉" in text


# ------------------------------------------------------------ 注入 / 不注入
def test_prompt_byte_identical_without_provider():
    baseline = _system()
    assert "【你今天的生活】" not in baseline
    set_companion_context(lambda: "")
    assert _system() == baseline, "provider 返回空串时输出必须一字不差"


def test_prompt_injects_schedule_after_time_section(tmp_path):
    store = _store(tmp_path)
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    system = _system()
    assert "【你今天的生活】" in system
    assert "【时间信息】" in system
    assert system.index("【时间信息】") < system.index("【你今天的生活】"), "日程要排在时间之后"
    assert "赶新需求加班" in system


def test_prompt_schedule_before_memory(tmp_path):
    store = _store(tmp_path)
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    system = _system(memory_text="【记忆】用户喜欢猫。")
    assert system.index("【你今天的生活】") < system.index("【记忆】")


def test_prompt_survives_provider_error(tmp_path):
    store = _store(tmp_path)
    baseline = _system()
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    with_schedule = _system()

    def boom():
        raise OSError("磁盘读不到")

    set_companion_context(boom)
    assert _system() == baseline, "provider 抛异常要静默退回，且不影响原有内容"
    assert with_schedule != baseline


def test_prompt_roleplay_skips_schedule(tmp_path):
    store = _store(tmp_path)
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    system = _system(roleplay_enabled=True, roleplay_scene="雨夜的便利店", roleplay_cast="店员：老是打瞌睡")
    assert "【你今天的生活】" not in system, "扮演场景与日常日程是两段人生，别互相打架"
    assert "雨夜的便利店" in system


def test_prompt_explicit_blank_disables_schedule(tmp_path):
    store = _store(tmp_path)
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    system = _system(companion_context="")
    assert "【你今天的生活】" not in system, "文件解读这类入口要能单独关掉"


def test_prompt_explicit_text_wins(tmp_path):
    """显式传入优先于 provider（测试与将来别的入口用）。"""
    store = _store(tmp_path)
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    system = _system(companion_context="【你今天的生活】你现在在楼下便利店。")
    assert "楼下便利店" in system and "赶新需求加班" not in system


# ---------------------------------------------------------------- runtime 钩子
def test_runtime_hook_registers_schedule_provider(tmp_path, monkeypatch):
    # provider 取的是「真实此刻」的日程，所以这份日程必须写成今天，否则隔天跑必红
    store = _store(tmp_path, day_key=datetime.now().strftime("%Y-%m-%d"))
    monkeypatch.setattr(companion_runtime, "companion_store", lambda: store)
    companion_runtime._hook_chat_schedule()
    system = _system()
    assert "【你今天的生活】" in system and "赶新需求加班" in system


def test_runtime_hook_without_store_is_silent(monkeypatch):
    monkeypatch.setattr(companion_runtime, "companion_store", lambda: None)
    companion_runtime._hook_chat_schedule()
    baseline = _system()
    assert "【你今天的生活】" not in baseline


def test_prompt_history_untouched_by_schedule(tmp_path):
    """日程只进 system，不往历史消息里塞东西。"""
    store = _store(tmp_path)
    set_companion_context(lambda: schedule.chat_context(store, NOW))
    history = [ChatMessage("user", "早上好"), ChatMessage("assistant", "早呀")]
    messages = PromptBuilder().build_messages(
        ChatSettings(), "test-character", history, "在干什么",
        role_prompt="你是测试角色。", now=NOW,
    )
    assert len(messages) == 4
    assert messages[1]["content"].endswith("早上好"), "历史消息只允许带既有时间戳，不该掺日程"
    assert all("【你今天的生活】" not in m["content"] for m in messages[1:])
    assert "【你今天的生活】" in messages[0]["content"]


def test_page_root_exists():
    """防止误建到别的目录（本文件所在测试根的合理性自检）。"""
    assert (pathlib.Path(__file__).parent / "conftest.py").is_file()

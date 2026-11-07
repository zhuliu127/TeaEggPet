# -*- coding: utf-8 -*-
"""日程生成的容错：模型输出被截断 / 空内容 / 不支持 json_object。

用户实测：生成明天的日程弹「模型没有返回 JSON」。实测原因是推理模型把 4000 token
的预算烧在思维链上，正文只剩半截 JSON（或空串）。这里锁住三层兜底：
容错解析 → 加预算重试 → 换提示重试。
"""

from __future__ import annotations

import pytest

from pet.companion import llm, schedule
from pet.companion.store import CompanionStore
from pet.config import Config

# 真实截断样本（在 1254 字处被切断，最后一段 detail 只写了一半）
TRUNCATED = (
    '{"title": "白天在家慢慢充电，晚上去看演唱会的一天", "mood": "松弛又带点兴奋", "items": ['
    '{"start": "00:00", "end": "08:40", "activity": "睡觉", "location": "家", "outfit": "粉色睡衣", '
    '"outfit_note": "", "scene": "卧室", "props": [], "detail": "睡得沉。"}, '
    '{"start": "08:40", "end": "09:20", "activity": "起床洗漱", "location": "家", "outfit": "粉色睡衣", '
    '"outfit_note": "", "scene": "卫生间", "props": [], "detail": "洗了把脸。"}, '
    '{"start": "09:20", "end": "10:30", "activity": "手冲咖啡", "location": "家", "outfit": "居家毛衣", '
    '"outfit_note": "", "scene": "厨房", "props": ["手冲咖啡壶"], "detail": "翻出很久没戴的耳环'
)


@pytest.fixture()
def store(tmp_path):
    return CompanionStore(Config(base=tmp_path))


def _valid_day(n: int = 6) -> str:
    items = []
    for i in range(n):
        items.append({
            "start": f"{i:02d}:00", "end": f"{i + 1:02d}:00",
            "activity": "在做事", "location": "家", "scene": "卧室",
            "outfit": "", "outfit_note": "", "props": [], "detail": "一句。",
        })
    import json

    return json.dumps({"title": "一天", "mood": "还行", "items": items}, ensure_ascii=False)


# ---------------------------------------------------------------- 解析容错
def test_extract_json_salvages_truncated_output():
    data = llm.extract_json(TRUNCATED)
    assert isinstance(data, dict)
    assert 2 <= len(data["items"]) <= 3, "只救完整的段落，不能凭空造"
    # 被截断的那半句 detail 不能带出来（要么整段丢掉，要么该字段为空）
    tail = data["items"][-1]
    assert "翻出很久没戴" not in str(tail.get("detail", ""))


def test_extract_json_handles_fence_and_trailing_comma():
    text = '```json\n{"a": 1, "b": [1, 2,],}\n```'
    assert llm.extract_json(text) == {"a": 1, "b": [1, 2]}


def test_extract_json_still_rejects_plain_text():
    with pytest.raises(llm.CompanionLLMError):
        llm.extract_json("抱歉，我没办法生成这个。")


# ---------------------------------------------------------------- 重试阶梯
def test_ask_day_retries_with_bigger_budget(store, monkeypatch):
    calls: list[dict] = []

    def fake(cfg, messages, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return "", {"finish_reason": "length", "truncated": True, "usage": {}}
        return _valid_day(), {"finish_reason": "stop", "truncated": False, "usage": {}}

    monkeypatch.setattr(llm, "ask_with_info", fake)
    parsed, _text = schedule._ask_day(store, "prompt")
    assert len(parsed["items"]) >= 6          # normalize_items 会把首尾空档补齐
    assert calls[0]["max_tokens"] == schedule._SCHEDULE_BUDGETS[0]
    assert calls[1]["max_tokens"] == schedule._SCHEDULE_BUDGETS[1], "空内容要走下一档预算"


def test_ask_day_falls_back_when_json_mode_unsupported(store, monkeypatch):
    calls: list[dict] = []

    def fake(cfg, messages, **kw):
        calls.append(kw)
        if kw.get("json_mode"):
            raise llm.CompanionLLMError("模型返回错误（HTTP 400）：response_format not supported")
        return _valid_day(), {"finish_reason": "stop", "truncated": False, "usage": {}}

    monkeypatch.setattr(llm, "ask_with_info", fake)
    parsed, _text = schedule._ask_day(store, "prompt")
    assert parsed["title"] == "一天"
    assert calls[0]["json_mode"] is True and calls[1]["json_mode"] is False


def test_ask_day_retries_after_bad_json(store, monkeypatch):
    calls: list[dict] = []

    def fake(cfg, messages, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return "抱歉，我先想想。", {"finish_reason": "stop", "truncated": False}
        return _valid_day(), {"finish_reason": "stop", "truncated": False}

    monkeypatch.setattr(llm, "ask_with_info", fake)
    parsed, _text = schedule._ask_day(store, "prompt")
    assert len(parsed["items"]) >= 6, "换一次提示后应该成功"
    # 第一档先 json_object 再普通模式（同预算）；再失败才升预算，故这里只要求有第二次
    assert len(calls) >= 2 and calls[1]["max_tokens"] >= calls[0]["max_tokens"]


def test_generate_day_reports_reason_when_all_attempts_fail(store, monkeypatch):
    def always_empty(cfg, messages, **kw):
        return "", {"finish_reason": "length", "truncated": True}

    monkeypatch.setattr(llm, "ask_with_info", always_empty)
    day = schedule.generate_day(store, "2026-11-08")
    assert day["source"] == "template", "全都失败时要走模板兜底"
    assert "空内容" in day["error"]

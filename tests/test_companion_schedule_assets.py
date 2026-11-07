# -*- coding: utf-8 -*-
"""每日日程要真的用上素材库（含后加的素材），而且用得合理。

用户实测：库里有演唱会、猫耳耳机这类素材，日程里一次都没出现——模型只挑最顺手的
那几样；而且提示词只平铺前 N 条时，后加的素材永远进不去。
"""

from __future__ import annotations

import pytest

from pet.companion import library, schedule
from pet.companion.store import CompanionStore
from pet.config import Config

LIB = {
    "scenes": [("卧室", "铺着地毯的小房间"), ("客厅", ""), ("公司", ""),
               ("公园", ""), ("商场", ""), ("演唱会", "livehouse，舞台灯光")],
    "props": [("猫耳耳机", "粉白色猫耳造型的头戴耳机"), ("应援棒", "演唱会用的荧光棒"),
              ("游戏手柄", ""), ("锅铲", ""), ("望远镜", ""), ("痛包", "")],
    "outfits": [("居家毛衣", ""), ("JK套装", ""), ("粉色洛丽塔", ""), ("运动服", "")],
    "characters": [("小鱼", "蓝发女孩")],
}


@pytest.fixture()
def store(tmp_path):
    cfg = Config(base=tmp_path)
    st = CompanionStore(cfg)
    for cat, rows in LIB.items():
        for name, desc in rows:
            library.save_entry(st, cat, None, {"name": name, "description": desc})
    return st


# ---------------------------------------------------------------- 轮换
def test_spotlight_is_deterministic_and_rotates(store):
    scenes = library.scan_all(store)["scenes"]
    a = schedule.spotlight(scenes, "2026-11-07", 3)
    b = schedule.spotlight(scenes, "2026-11-07", 3)
    assert [x["name"] for x in a] == [x["name"] for x in b], "同一天要稳定"

    seen = set()
    for day in range(1, 29):
        key = f"2026-11-{day:02d}"
        seen.update(x["name"] for x in schedule.spotlight(scenes, key, 3))
    assert seen == {n for n, _ in LIB["scenes"]}, "一个月内库里的场景都该轮到"


def test_spotlight_puts_recently_used_last(store):
    props = library.scan_all(store)["props"]
    # 假设「猫耳耳机/应援棒/游戏手柄」最近三天都在用：今日重点不该还是它们
    picked = [x["name"] for x in schedule.spotlight(
        props, "2026-11-07", 3, avoid={"猫耳耳机", "应援棒", "游戏手柄"})]
    assert not (set(picked) & {"猫耳耳机", "应援棒", "游戏手柄"})


def test_new_asset_enters_rotation(store):
    """后加的素材不用改代码就会进提示词的「今日重点」。"""
    before = set()
    for day in range(1, 15):
        before.update(x["name"] for x in schedule.spotlight(
            library.scan_all(store)["props"], f"2026-11-{day:02d}", 30))
    assert "魔法杖" not in before
    library.save_entry(store, "props", None, {"name": "魔法杖", "description": "cos 用的道具杖"})
    names = [x["name"] for x in schedule.spotlight(
        library.scan_all(store)["props"], "2026-11-07", 30)]
    assert "魔法杖" in names


# ---------------------------------------------------------------- 提示词
def test_prompt_lists_props_with_description(store):
    text = schedule.build_prompt(store, "2026-11-07", {"name": "工作日", "guide": ""}, "")
    assert "猫耳耳机" in text
    assert "粉白色猫耳造型的头戴耳机" in text, "道具要带上描述，模型才知道是干嘛用的"


def test_prompt_has_spotlight_section(store):
    text = schedule.build_prompt(store, "2026-11-07", {"name": "工作日", "guide": ""}, "")
    assert "【今天可以重点安排的素材】" in text
    # 轮换窗口里既有场景也有道具
    assert "场景「" in text and "道具「" in text


def test_prompt_demands_plausible_pairing(store):
    text = schedule.build_prompt(store, "2026-11-07", {"name": "工作日", "guide": ""}, "")
    assert "对得上" in text and "别硬塞" in text
    assert "演唱会" in text, "要说明演唱会这类场景需要出门/回家"
    assert "至少用到 3 个不同场景" in text


def test_prompt_survives_empty_library(tmp_path):
    st = CompanionStore(Config(base=tmp_path))
    text = schedule.build_prompt(st, "2026-11-07", {"name": "工作日", "guide": ""}, "")
    assert "【今天可以重点安排的素材】" not in text
    assert "至少用到 3 个不同场景" not in text
    assert "只输出一个 JSON 对象" in text

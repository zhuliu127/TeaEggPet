# -*- coding: utf-8 -*-
"""「自拍手臂入镜」关掉时，提示词里不许再出现举着手机的自拍。

用户实测：开关关掉后出图还是举着手机。根因是**只改了指令、没洗数据**——
大模型在 `prompt` 里自己补「举起手机」，这条原文既进了【画面】，
也进了千问那层的「取景」，两层都会把手机画出来。这里钉住三道闸：

1. `compose.strip_phone_wording`：按句丢掉含手机动作的描写；
2. `selfie.plan`：模型返回的 prompt / shot 在分流前就洗干净；
3. `qwen_prompt.enhance`：重写结果出场前再洗一遍。
"""
from __future__ import annotations

import json

import pytest

from pet.companion import compose, presets, qwen_prompt, selfie
from pet.companion.store import CompanionStore
from pet.config import Config


@pytest.fixture
def store(tmp_path):
    return CompanionStore(Config(base=tmp_path))


def test_strip_phone_wording_drops_phone_clauses():
    raw = "她一手提着关东煮，一手举着手机自拍，暖黄的灯牌在身后"
    out = compose.strip_phone_wording(raw)
    assert "手机" not in out and "举" not in out
    assert "关东煮" in out and "灯牌" in out


def test_strip_phone_wording_falls_back_when_all_phone():
    assert compose.strip_phone_wording("她举起手机对着镜头") == "她对着镜头拍了一张照片"


#: arm-off 时会在末尾追加一段「参考图姿势不要照搬」的强约束，那段里会合法地
#: 出现「手机」「举着手机」等字样（用来否定它们）。断言「画面里没有手机」时
#: 需要先把这段警告剔掉，只检查真正的画面/镜头描述。
_WARN_MARK = "【重要·参考图姿势不要照搬】"


def _body(out: str) -> str:
    idx = out.find(_WARN_MARK)
    return out[:idx] if idx >= 0 else out


def test_compose_plain_shot_and_clean_picture_when_arm_off(store):
    store.set("companion_qwen_selfie_arm", False)
    out = compose.compose({"prompt": "她一手举着手机自拍，背景是便利店",
                           "shot": "自拍"}, store)["final_prompt"]
    assert "手机" not in _body(out), out
    assert presets.PLAIN_SHOT_TEXT in out
    assert _WARN_MARK in out


def test_compose_keeps_arm_when_switch_on(store):
    store.set("companion_qwen_selfie_arm", True)
    out = compose.compose({"prompt": "她一手举着手机自拍", "shot": "自拍"}, store)["final_prompt"]
    assert "举着手机" in out


def test_plan_cleans_llm_output(store, monkeypatch):
    store.set("companion_qwen_selfie_arm", False)
    payload = {"prompt": "她举起手机，走在街上，一手举着手机自拍", "shot": "自拍",
               "caption": "出来走走～", "monologue": "我回来啦"}
    monkeypatch.setattr(selfie.llm, "ask", lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    planned = selfie.plan(store, None, "", "walk")
    assert "手机" not in planned["prompt"], planned["prompt"]
    assert planned["shot"] == presets.PLAIN_SHOT_NAME


def test_enhance_strips_rewritten_prompt(store, monkeypatch):
    store.set("companion_qwen_selfie_arm", False)
    rewritten = ("把<image1>的人物放进<image2>的场景，她举起手机自拍，脸侧向一边；"
                 "保持背景不变")
    monkeypatch.setattr(qwen_prompt.llm, "ask",
                        lambda *a, **k: json.dumps({"rewritten_prompt": rewritten,
                                                    "wh_ratio": "3:4",
                                                    "ratio_follow": ""}, ensure_ascii=False))
    out = qwen_prompt.enhance(store, refs=[], item={}, plan={"prompt": "走在街上", "shot": "自拍"},
                              aspect="3:4", arm_visible=False)
    assert "手机" not in out["rewritten_prompt"], out["rewritten_prompt"]


def test_enhance_keeps_arm_when_enabled(store, monkeypatch):
    rewritten = "把<image1>的人物放进<image2>的场景，她举起手机自拍"
    monkeypatch.setattr(qwen_prompt.llm, "ask",
                        lambda *a, **k: json.dumps({"rewritten_prompt": rewritten,
                                                    "wh_ratio": "3:4",
                                                    "ratio_follow": ""}, ensure_ascii=False))
    out = qwen_prompt.enhance(store, refs=[], item={}, plan={"prompt": "走在街上", "shot": "自拍"},
                              aspect="3:4", arm_visible=True)
    assert "举起手机" in out["rewritten_prompt"]


# ---------------------------------------------------------------------------
# 云端链路（不走千问重写，直接吃 compose.final_prompt + 原参考图）
# ---------------------------------------------------------------------------

def test_strip_phone_phrases_cleans_traits_but_keeps_personality():
    raw = ("开心时会摇尾巴；害羞时会低头捏裙角；"
           "自拍时会自然对着镜头笑、身体前倾，一只手伸向镜头（手机视角）")
    out = compose.strip_phone_phrases(raw)
    assert "手机" not in out and "伸向镜头" not in out, out
    assert "摇尾巴" in out and "捏裙角" in out and "对着镜头笑" in out


def test_strip_phone_wording_extra_variants():
    for raw in (
        "她拿起手机拍了一张",
        "手机视角，近景",
        "一条手臂从画面外伸进来",
        "对着后置镜头自拍",
    ):
        out = compose.strip_phone_wording(raw)
        assert "手机" not in out and "手臂" not in out and "镜头" not in out or out == "她对着镜头拍了一张照片", raw + " -> " + out


def test_compose_cloud_final_prompt_has_no_phone_and_warns_pose(store):
    """云端直接吃 final_prompt：关掉开关后，整段里不能有手机/手臂，
    并且要带一句「参考图姿势不要照搬」的强约束。"""
    store.set("companion_qwen_selfie_arm", False)
    out = compose.compose({
        "prompt": "她坐在工位前，一手举着手机自拍，另一手拿着咖啡",
        "shot": "自拍",
        "characters": ["小鱼"],
    }, store)
    fp = out["final_prompt"]
    assert "举着手机" not in _body(fp) and "手机视角" not in _body(fp), fp
    assert _WARN_MARK in fp, fp
    assert presets.PLAIN_SHOT_TEXT in fp


def test_compose_traits_phone_cleaned_in_final_prompt(store):
    """人物 traits 里带「一只手伸向镜头（手机视角）」，关掉开关后 final_prompt
    的【人物神态特点】里不许再出现手机/伸向镜头。"""
    store.set("companion_qwen_selfie_arm", False)
    out = compose.compose({"prompt": "她在街上走着", "characters": ["小鱼"]}, store)
    fp = out["final_prompt"]
    assert "手机视角" not in _body(fp), fp
    assert "一只手伸向镜头" not in _body(fp), fp

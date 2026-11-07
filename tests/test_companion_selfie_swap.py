# -*- coding: utf-8 -*-
"""照片卡「换素材」= 换这一张的参考素材（不是打开素材库管理）。

用户实测的语义纠正：点「换素材」应当能换掉这张图用的衣服 / 场景 / 道具，
再带着这套重拍——而不是弹出一个素材库管理窗口。

钉住这些事：
- ``selfie.generate(overrides=...)`` 里手选的衣服 / 场景 / 道具**优先于**模型与日程联动；
- 没传 overrides 时行为完全不变（日程联动那套照旧）；
- 换过素材的记录里留 ``swap`` 痕迹，下次再换能预选；
- 选择面板能把素材库条目列出来并预选当前这张用的。
"""
from __future__ import annotations

import pytest

from pet.companion import library
from pet.companion import selfie
from pet.companion.store import CompanionStore
from pet.config import Config


@pytest.fixture
def store(tmp_path):
    return CompanionStore(Config(base=tmp_path))


def _fake_pipeline(monkeypatch, captured: dict):
    """把「模型想画面 + 拼提示词 + 生图 + 落盘」全换成假的，只看素材选择。"""
    monkeypatch.setattr(selfie, "plan", lambda *a, **k: {
        "caption": "", "monologue": "", "prompt": "随手一张",
        "characters": [], "outfits": [], "scene": "", "props": [],
    })
    monkeypatch.setattr(selfie.schedule, "current_context", lambda *a, **k: None)
    monkeypatch.setattr(selfie.schedule, "ensure_day", lambda *a, **k: None)
    monkeypatch.setattr(selfie.library, "scan_all", lambda *a, **k: {})

    def fake_compose(args, *a, **k):
        captured.update(args)
        return {"final_prompt": "p", "refs": [], "aspect": "3:4", "keywords": [],
                "selected": {key: (list(value) if isinstance(value, list) else value)
                             for key, value in args.items()
                             if key in ("characters", "outfits", "scene", "props")},
                "notes": [], "unresolved": []}

    monkeypatch.setattr(selfie.compose_mod, "compose", fake_compose)
    monkeypatch.setattr(selfie.imagegen, "generate",
                        lambda *a, **k: (b"img", "png", {}))
    monkeypatch.setattr(selfie, "_save", lambda *a, **k: ("C:/tmp/x.png", "x.png"))


def test_overrides_win_over_schedule_link(store, monkeypatch):
    captured: dict = {}
    _fake_pipeline(monkeypatch, captured)
    rec = selfie.generate(store, overrides={"outfits": ["牛仔外套"], "scene": "教室",
                                           "props": ["雨伞", "奶茶"]})
    assert captured["outfits"] == ["牛仔外套"], "手选衣服必须覆盖模型/日程"
    assert captured["scene"] == "教室"
    assert captured["props"] == ["雨伞", "奶茶"]
    assert rec["swap"]["outfits"] == ["牛仔外套"], "记录里要留痕，便于再次换"


def test_no_overrides_keeps_old_behaviour(store, monkeypatch):
    captured: dict = {}
    _fake_pipeline(monkeypatch, captured)
    rec = selfie.generate(store)
    assert captured["outfits"] == [], "没换素材时不该凭空塞衣服"
    assert captured["scene"] == ""
    assert rec["swap"] == {}


def test_empty_override_items_ignored(store, monkeypatch):
    """面板里留空 = 跟随日程，不能把空串写进素材名。"""
    captured: dict = {}
    _fake_pipeline(monkeypatch, captured)
    selfie.generate(store, overrides={"outfits": ["", "  "], "scene": "  ", "props": [""]})
    assert captured["outfits"] == []
    assert captured["scene"] == ""
    assert captured["props"] == []


def test_picker_lists_library_and_preselects(store):
    """面板：列出入库条目，并按这张图当前素材预选。"""
    from PySide6.QtWidgets import QApplication

    from pet.companion.assets_picker import SwapAssetsDialog

    QApplication.instance() or QApplication([])
    library.save_entry(store, "outfits", None, {"name": "牛仔外套"})
    library.save_entry(store, "outfits", None, {"name": "居家毛衣"})
    library.save_entry(store, "scenes", None, {"name": "教室"})
    library.save_entry(store, "props", None, {"name": "雨伞"})

    dialog = SwapAssetsDialog(store, {"selected": {"outfits": ["居家毛衣"], "scene": "教室",
                                                   "props": ["雨伞"]}})
    try:
        assert dialog.outfit.count() == 3, "含「跟随日程」占位 + 两个服饰条目"
        assert dialog.outfit.currentText() == "居家毛衣", "要预选这张图用的衣服"
        assert dialog.scene.currentText() == "教室"
        checked = [dialog.props.item(i).text() for i in range(dialog.props.count())
                   if dialog.props.item(i).checkState().value == 2]
        assert checked == ["雨伞"]
        dialog._accept()
        assert dialog.chosen() == {"outfits": ["居家毛衣"], "scene": "教室", "props": ["雨伞"]}
    finally:
        dialog.deleteLater()

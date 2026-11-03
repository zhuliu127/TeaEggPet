# -*- coding: utf-8 -*-
"""产品版本（对外）与内核版本（角色包门禁）必须分开。

背景：``pet.__version__`` 是宿主内核 dsh-pet-indesktop 的版本，角色包 app_min
卡在它上面（4.4.0）。TeaEggPet 自己按 1.x 发版，若更新检测仍用内核版本，
就会永远认为「已是最新」，Release 出了也检测不到。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pet import __version__ as kernel_version
from pet.character_packs_page import app_supports
from pet.product import PRODUCT_NAME, PRODUCT_VERSION
from pet.updater import APP_VERSION, is_newer


def test_product_version_is_what_updates_compare():
    assert PRODUCT_NAME == "TeaEggPet"
    assert APP_VERSION == PRODUCT_VERSION, "更新检测基准必须是产品版本"
    assert PRODUCT_VERSION != kernel_version, "两个版本号不能是同一个值，否则等于没分"


def test_newer_release_is_detected_by_product_version():
    major, minor = (int(x) for x in PRODUCT_VERSION.split(".")[:2])
    assert is_newer(f"v{major}.{minor + 1}"), "下一版必须被识别为更新"
    assert is_newer(f"v{major + 1}.0")
    assert not is_newer(f"v{major}.{minor}"), "当前版本不算更新"
    assert not is_newer(f"v{major}.{minor}.0"), "同一版本的不同写法不算更新"


def test_pack_gate_still_follows_kernel_version():
    # 素材包声明的 app_min 是内核版本；产品版本降到 1.x 不能把它们判成不可用
    assert app_supports(kernel_version) is True
    assert app_supports("4.4.0") is True
    assert app_supports("9.9.9") is False

# -*- coding: utf-8 -*-
"""照片卡上的「换素材」按钮。

背景（用户实测）：自拍出图后点「换素材」没有任何反应。日志里是

    TypeError: 'PySide6.QtWidgets.QDialog.__init__' called with wrong argument types:
      PySide6.QtWidgets.QDialog.__init__(OverlayShell)

overlay 拓扑下 ``bubble_host()`` 返回的是 OverlayShell（壳对象，不是 QWidget），
被当成 parent 塞进 QDialog，窗口根本建不出来；异常又被 open_library 的
try/except 吞成一行日志，界面上就只剩「点了没反应」。

钉住这些事：
- 传进来不是 QWidget 的 parent（OverlayShell 这种）不会炸，窗口照样建出来；
- 窗口是置顶的 —— 照片卡自己是 WindowStaysOnTop，不置顶就会被它压住；
- 关过之后再点，窗口能被叫回来（只 raise 不 show 是叫不回来的）；
- 关掉之后单例被清空，下次是新建而不是复用死对象。
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from pet.companion import runtime as companion_runtime
from pet.config import Config


class _FakeShell:
    """模拟 OverlayShell：不是 QWidget，没有 Qt 父窗口能力。"""


class _FakeHost:
    def __init__(self, config):
        self.config = config


@pytest.fixture
def app():
    inst = QApplication.instance() or QApplication([])
    yield inst


@pytest.fixture(autouse=True)
def _clean_runtime():
    yield
    dlg = companion_runtime._library_dialog
    companion_runtime._library_dialog = None
    companion_runtime._app = None
    if dlg is not None:
        try:
            dlg.close()
        except RuntimeError:
            pass


def _open(tmp_path, parent=None, **kwargs):
    companion_runtime._app = _FakeHost(Config(base=tmp_path))
    companion_runtime.open_library(parent, **kwargs)
    return companion_runtime._library_dialog


def test_non_widget_parent_does_not_break_creation(app, tmp_path):
    """OverlayShell 当 parent 也不该炸（这就是用户遇到的那次 TypeError）。"""
    dialog = _open(tmp_path, _FakeShell())
    assert dialog is not None, "窗口必须建出来，不能只剩一行日志"
    assert isinstance(dialog, QWidget)
    assert dialog.isVisible()


def test_window_is_topmost(app, tmp_path):
    """照片卡是 WindowStaysOnTop，素材库不置顶就会被压住（看着也像没反应）。"""
    dialog = _open(tmp_path, _FakeShell())
    assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_reopen_shows_hidden_dialog(app, tmp_path):
    """关过（隐藏）之后再点，要能被 show 回来，而不是只 raise。"""
    dialog = _open(tmp_path, _FakeShell())
    dialog.hide()
    app.processEvents()
    assert not dialog.isVisible()

    companion_runtime.open_library(None)
    app.processEvents()
    assert companion_runtime._library_dialog is dialog, "同一个实例复用"
    assert dialog.isVisible(), "复用分支必须 show，否则点了没反应"


def test_close_clears_singleton(app, tmp_path):
    """关掉后单例置空，下次新建（避免拿着已销毁的 C++ 对象）。"""
    dialog = _open(tmp_path, _FakeShell())
    dialog.reject()
    app.processEvents()
    assert companion_runtime._library_dialog is None


def test_on_closed_callback_runs(app, tmp_path):
    """关窗回调（照片卡要靠它把自己放回来）必须被调用。"""
    hits = []
    dialog = _open(tmp_path, _FakeShell(), on_closed=lambda: hits.append(1))
    dialog.reject()
    app.processEvents()
    assert hits == [1]


def test_no_host_logs_and_returns(app, tmp_path, caplog):
    """宿主还没挂上时不抛异常，只留一条 warning。"""
    companion_runtime._app = None
    companion_runtime._library_dialog = None
    companion_runtime.open_library(None)
    assert companion_runtime._library_dialog is None

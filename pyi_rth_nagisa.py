# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：让 nagisa 的「扁平子模块」在冻结包里能被 import。

背景：nagisa 这个包把 ``prepro.py`` / ``model.py`` / ``mecab_system_eval.py`` 放在
``site-packages/nagisa/`` 目录里，但包内代码用的是**裸导入** ``import prepro``
（以及 ``import nagisa_utils``）。开发环境能跑，是因为 nagisa 目录本身被塞进了
``sys.path``，于是这些文件能当作顶层模块 ``prepro`` 被找到。

冻结后 ``nagisa/`` 被收进 ``sys._MEIPASS/nagisa``，但那个目录**不在** sys.path，
所以 ``import prepro`` 报 ``ModuleNotFoundError: No module named 'prepro'``，
连累 qwen_asr（它的 forced aligner 依赖 nagisa）整个 import 失败、语音助手加载不了。

这里把冻结包里的 nagisa 目录补进 sys.path，恢复开发环境的解析行为。
"""
import os
import sys

try:
    _base = getattr(sys, "_MEIPASS", None)
    if _base:
        for _cand in (os.path.join(_base, "nagisa"), _base):
            if os.path.isdir(_cand) and _cand not in sys.path:
                sys.path.insert(0, _cand)
except Exception:  # noqa: BLE001
    pass

# -*- coding: utf-8 -*-
"""TeaEggPet 自己的版本号（对外版本 / 更新检测基准）。

为什么要跟 ``pet.__version__`` 分开：
``pet.__version__`` 是**宿主内核**（dsh-pet-indesktop）的版本，角色包的
``app_min`` 卡在它上面——内核不跟着 TeaEggPet 发版走，把它当产品版本用会让
「检查更新」永远认为已经是最新。

- 对外展示、GitHub Release tag、``update.json`` 的 ``version``：``PRODUCT_VERSION``
- 角色包门禁 ``app_min``：仍然用 ``pet.__version__``（内核版本）
"""

from __future__ import annotations

from . import __version__ as KERNEL_VERSION

PRODUCT_NAME = 'TeaEggPet'
PRODUCT_VERSION = '1.4.7'

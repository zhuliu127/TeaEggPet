# -*- coding: utf-8 -*-
"""启动自检：今天没有日程就自动生成一份并保存。

只在「今天这一天确实没有内容」时动手：
- 文件不存在 / items 为空 → 生成（走 LLM，失败自动退模板，见 schedule.generate_day）；
- 已经有内容 → 一个字节都不碰。
生成放在后台线程：LLM 最长可能跑几分钟，不能卡住启动。
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QObject, QThread, Signal

from . import schedule
from .store import CompanionStore

log = logging.getLogger(__name__)

_worker: "_DayBootstrap | None" = None


class _DayBootstrap(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, store: CompanionStore, key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._key = key

    def run(self) -> None:  # noqa: D102
        try:
            day = schedule.generate_day(self._store, self._key)
            self.done.emit(day)
        except Exception as exc:  # noqa: BLE001
            log.exception("自动生成日程失败")
            self.failed.emit(str(exc))


def needs_schedule(store: CompanionStore, key: str | None = None) -> bool:
    """今天这一天是不是空的（没有文件，或 items 为空）。"""
    key = key or schedule.date_key()
    return schedule.read_day(store, key) is None


def ensure_today(app, *, delay_ms: int = 2500) -> bool:
    """启动时调用一次。返回是否真的排了一次生成。"""
    global _worker
    try:
        store = CompanionStore(app.config)
        if not bool(store.get("companion_schedule_enabled", True)):
            log.info("每日日程未启用，跳过自动补日程")
            return False
        if not bool(store.get("companion_schedule_autofill", True)):
            log.info("自动补日程已关闭")
            return False
        key = schedule.date_key(datetime.now())
        if not needs_schedule(store, key):
            return False
        if _worker is not None and _worker.isRunning():
            return False
        log.info("今天（%s）还没有日程，后台生成一份", key)
        worker = _DayBootstrap(store, key, app if isinstance(app, QObject) else None)
        worker.done.connect(lambda day: log.info(
            "自动日程已保存：%s（%d 段，来源 %s）",
            (day or {}).get("title", ""), len((day or {}).get("items") or []),
            (day or {}).get("source", "")))
        worker.failed.connect(lambda msg: log.info("自动日程失败：%s", msg))
        worker.finished.connect(lambda: _clear())
        _worker = worker
        worker.start()
        return True
    except Exception:  # noqa: BLE001
        log.exception("启动补日程失败（不影响启动）")
        return False


def _clear() -> None:
    global _worker
    _worker = None


def shutdown() -> None:
    global _worker
    worker = _worker
    _worker = None
    if worker is not None and worker.isRunning():
        worker.wait(300)

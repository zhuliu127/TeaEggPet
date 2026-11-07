# -*- coding: utf-8 -*-
"""出去溜达 + 回来发自拍。

一次溜达 = 按快捷键（或右键）触发：
1. 她现在在桌面底边到处走（复用宿主自己的移动动画，不自己抢窗口位置）；
2. 隔几分钟像发微信一样报一句她在干什么（右下角通知 + 头顶气泡）；
3. 到点（或再按一次快捷键）回家，按此刻的日程生成一张自拍并弹出照片。

时长默认跟「每日日程」此刻这一段剩余的时间走，有最短 / 最长兜底。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from . import selfie as selfie_mod
from . import schedule
from .store import CompanionStore

log = logging.getLogger(__name__)


#: 文案上限：超过就收在最后一个句末标点上，别把半句话直接播出去。
MAX_NARRATION_CHARS = 120
_SENTENCE_END = "。！？～…，"

#: 出门/回家动画的兜底时长（毫秒）。真正用哪个数看素材实际时长——
#: 固定 1.4s 会把「刚起步还没进奔跑姿势」的那一下就砍掉（用户实测），
#: 所以改成按 clip 时长 × 系数，取不到时长才用这里的兜底值。
_EXIT_ANIM_MS = 2600
_ENTER_ANIM_MS = 1800
#: 至少让她跑够这么久（素材很短时也不至于一闪而过）。
_ANIM_MIN_MS = 2000
#: 最长不超过这么久（素材特别长时别让桌宠僵在台上）。
_ANIM_MAX_MS = 3600
#: 挑「奔跑感」素材的名字线索（角色包里有一条「左转奔跑」）。
_RUN_HINTS = ("奔跑", "跑", "run", "dash", "冲刺", "冲")


def _clip_sentence(text: str, limit: int = MAX_NARRATION_CHARS) -> str:
    """超长文案按标点收尾。

    原来直接 ``text[:120]``，一旦模型话多就会在句子中间硬切，
    播出来是半句话（听感就是「语音不全」）。宁可少说一句，也不说半句。
    """
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _SENTENCE_END)
    return head[:cut + 1].strip() if cut >= limit // 3 else head.strip()


class _SelfieWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, store: CompanionStore, hint: str, reason: str, parent=None,
                 overrides: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._hint = hint
        self._reason = reason
        self._overrides = dict(overrides or {})

    def run(self) -> None:  # noqa: D102
        try:
            rec = selfie_mod.generate(self._store, self._hint, self._reason,
                                      on_status=lambda text: self.status.emit(str(text)),
                                      overrides=self._overrides or None)
            self.done.emit(rec)
        except Exception as exc:  # noqa: BLE001
            log.exception("自拍失败")
            self.failed.emit(str(exc))


class WalkService(QObject):
    """一次溜达的生命周期（定时器 + 消息 + 收尾自拍）。"""

    changed = Signal()

    def __init__(self, app) -> None:
        super().__init__(app if isinstance(app, QObject) else None)
        self.app = app
        self.store = CompanionStore(app.config)
        self._started_at: datetime | None = None
        self._ends_at: datetime | None = None
        self._minutes = 0
        self._hint = ""
        self._msg_timer = QTimer(self)
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(self._on_msg_tick)
        self._end_timer = QTimer(self)
        self._end_timer.setSingleShot(True)
        self._end_timer.timeout.connect(lambda: self.stop("timeout"))
        self._walk_timer = QTimer(self)
        self._walk_timer.timeout.connect(self._nudge_walk)
        self._worker: _SelfieWorker | None = None
        self._popping_selfie = False
        self._closed = False
        self._last_message = ""
        self._last_message_at: datetime | None = None
        # 出门动画/隐藏的时序令牌：连续 start→stop 时旧 singleShot 不能把新状态改回去
        self._vis_token = 0
        # 这轮溜达是否已经把桌宠藏起来了（回来要恢复，退出进程前也要恢复）
        self._hidden = False

    # ---------------------------------------------------------------- 状态
    @property
    def walking(self) -> bool:
        return self._ends_at is not None

    @property
    def minutes_left(self) -> int:
        if not self._ends_at:
            return 0
        return max(0, int(round((self._ends_at - datetime.now()).total_seconds() / 60)))

    def _window(self):
        """当前拓扑下真正能冒泡的窗口（overlay 下 instance.win 是 None）。"""
        from . import runtime

        return runtime.bubble_host()

    def _pet_name(self) -> str:
        try:
            return str(self.store.get("companion_character_name", "") or "").strip() or "茶叶蛋"
        except Exception:
            return "茶叶蛋"

    # ---------------------------------------------------------------- 时长
    def planned_minutes(self) -> int:
        mode = str(self.store.get("companion_walk_duration_mode", "schedule") or "schedule")
        try:
            low = max(1, int(self.store.get("companion_walk_min_minutes", 5) or 5))
            high = max(low, int(self.store.get("companion_walk_max_minutes", 45) or 45))
        except (TypeError, ValueError):
            low, high = 5, 45
        if mode == "fixed":
            try:
                fixed = int(self.store.get("companion_walk_fixed_minutes", 10) or 10)
            except (TypeError, ValueError):
                fixed = 10
            return max(low, min(high, fixed))
        left = schedule.minutes_left(self.store)
        if left is None:
            return max(low, min(high, 10))
        return max(low, min(high, int(left)))

    # ---------------------------------------------------------------- 显隐与动画
    def _bump_vis_token(self) -> int:
        self._vis_token += 1
        return self._vis_token

    def _anim_host(self):
        """播动画该找谁：overlay 是壳的 switch_clip，legacy 是窗自己的。"""
        from . import runtime

        return runtime.anim_host() or runtime.move_host()

    def _clip_names(self) -> list[str]:
        """当前宿主可播放的动画名（legacy 的窗直接有 moves；overlay 走 sprite 库）。"""
        host = self._anim_host()
        if host is None:
            return []
        moves = getattr(host, "moves", None)
        if moves:
            return [str(n) for n in moves]
        sprite = getattr(host, "sprite", None)
        lib = getattr(sprite, "library", None) or getattr(host, "library", None)
        names = getattr(lib, "names", None)
        if callable(names):
            try:
                names = names()
            except Exception:  # noqa: BLE001
                names = None
        return [str(n) for n in (names or [])]

    def _run_clip_name(self) -> str:
        """挑一条「跑」的素材；找不到跑的就退回任意移动素材。"""
        names = self._clip_names()
        for name in names:
            low = name.lower()
            if any(h in name or h in low for h in _RUN_HINTS):
                return name
        return names[0] if names else ""

    def _play_run(self) -> None:
        """播放一段奔跑动画（只做画面，不抢窗口位置）。"""
        from . import runtime

        host = self._anim_host()
        if host is None:
            log.info("播放出门/回家动画：没有可播动画的宿主（拓扑不支持），跳过")
            return
        name = self._run_clip_name()
        if not name:
            log.info("播放出门/回家动画：角色包没有可用的动作素材，跳过")
            return
        switch = getattr(host, "switch_clip", None)
        if callable(switch):
            try:
                played = switch(name)
                log.info("播放出门/回家动画：%s（%s）", name, "ok" if played is not False else "宿主拒绝")
                return
            except Exception:  # noqa: BLE001
                log.warning("播放奔跑动画失败（%s）", name, exc_info=True)
        nudge = getattr(host, "trigger_move", None)
        if callable(nudge):
            try:
                nudge(name)
                log.info("改用移动动画代替：%s", name)
            except Exception:  # noqa: BLE001
                log.warning("触发移动（代替奔跑动画）失败", exc_info=True)

    def _run_anim_ms(self, fallback: int, *, factor: float = 1.25,
                     cap: int = _ANIM_MAX_MS, floor: int = _ANIM_MIN_MS) -> int:
        """这段奔跑动画该放多久：按素材真实时长算，取不到就用兜底值。

        ``MovieLibrary.duration(name)`` 给的是毫秒；乘以系数是为了让她至少跑完
        一个完整循环（用户反馈固定 1.4s 时「还没进入奔跑姿势」就被藏了）。
        """
        name = self._run_clip_name()
        try:
            host = self._anim_host()
            sprite = getattr(host, "sprite", None)
            lib = getattr(sprite, "library", None) or getattr(host, "library", None)
            duration = getattr(lib, "duration", None)
            if name and callable(duration):
                ms = float(duration(name) or 0.0)
                if ms > 100:                     # 毫秒口径（<100 多半是秒口径或脏值）
                    return int(min(cap, max(floor, ms * float(factor))))
        except Exception:  # noqa: BLE001
            log.debug("读动画时长失败，用兜底值", exc_info=True)
        return int(min(cap, max(floor, fallback)))

    def _hide_pet(self, token: int) -> None:
        """跑完出门动画后把桌宠藏起来——就当真的出门了。"""
        if token != self._vis_token or not self.walking:
            return
        from . import runtime

        if runtime.set_pet_visible(False):
            self._hidden = True
            log.info("溜达开始：奔跑动画播完，桌宠已隐藏")

    def _show_pet(self) -> None:
        """回到桌面（本来没藏就什么都不做）。"""
        if not self._hidden:
            return
        from . import runtime

        if runtime.set_pet_visible(True):
            self._hidden = False
            log.info("溜达结束：桌宠回到桌面")

    def chat_context(self) -> str:
        """给 AI 对话的一段事实：她此刻在外面溜达，不在电脑前。

        没在溜达返回空串（调用方据此不注入，提示词与不带溜达时逐字节一致）。
        """
        if not self.walking:
            return ""
        left = self.minutes_left
        tail = f"大约 {left} 分钟后回家。" if left else "快回家了。"
        hint = str(self._hint or "").strip()
        head = "【此刻状态】你现在正在外面溜达，人不在电脑前，也不在桌面上。"
        head += tail
        if hint:
            head += f"这趟出来的由头：{hint}。"
        return "\n".join([
            head,
            "用户问你在做什么、在哪、忙不忙、吃了没、什么时候回来这类问题时，"
            "以「在外面溜达」为事实，用第一人称口语回答：可以说路上看到的、天气、心情、顺手买了什么；"
            "不要说自己坐在桌面上、一直在陪聊天，也不要按日程里此刻的活动回答"
            "（那是出门前的安排，只有用户明确问日程时才可以提）。",
        ])

    # ---------------------------------------------------------------- 开始
    def start(self, hint: str = "") -> dict[str, Any]:
        if self.walking:
            return {"ok": False, "error": "已经在溜达了", "minutes_left": self.minutes_left}
        minutes = self.planned_minutes()
        self._started_at = datetime.now()
        self._ends_at = self._started_at + timedelta(minutes=minutes)
        self._minutes = minutes
        self._hint = str(hint or "")
        self._last_message = ""
        name = self._pet_name()
        opening = f"我出门啦～大概 {minutes} 分钟后回来"
        self._say(opening, sticky_ms=9000)
        self._notify(name, opening)
        # 出门：先跑一段给她看，跑完就藏起来（像真的走出去了）
        self._play_run()
        token = self._bump_vis_token()
        wait_ms = self._run_anim_ms(_EXIT_ANIM_MS, factor=1.3)
        QTimer.singleShot(wait_ms, lambda: self._hide_pet(token))
        log.info("出门动画时长：%s ms", wait_ms)
        QTimer.singleShot(5000, lambda: self._narrate("start"))
        self._schedule_message()
        self._end_timer.start(minutes * 60000)
        self.changed.emit()
        log.info("开始溜达：%s 分钟", minutes)
        return {"ok": True, "minutes": minutes}

    # ---------------------------------------------------------------- 结束
    def stop(self, reason: str = "user") -> dict[str, Any]:
        if not self.walking:
            return {"ok": False, "error": "现在没在溜达"}
        minutes = self._minutes
        self._ends_at = None
        self._started_at = None
        self._msg_timer.stop()
        self._end_timer.stop()
        self._walk_timer.stop()
        self.changed.emit()
        token = self._bump_vis_token()
        # 回家：先回到桌面，再跑一段进来，站稳了才说话（不然气泡跟着动画乱飘）
        self._show_pet()
        QTimer.singleShot(60, self._play_run)
        wait_ms = self._run_anim_ms(_ENTER_ANIM_MS, factor=1.15, cap=2800)
        QTimer.singleShot(wait_ms, lambda: self._on_back_home(token, reason))
        log.info("回家动画时长：%s ms", wait_ms)
        log.info("结束溜达（%s），本轮 %s 分钟", reason, minutes)
        return {"ok": True, "minutes": minutes}

    def _on_back_home(self, token: int, reason: str) -> None:
        """回家奔跑动画播完后的收尾：招呼一句 + 按设置自拍。"""
        if token != self._vis_token or self.walking:
            return
        name = self._pet_name()
        self._say("我回来啦～", sticky_ms=9000)
        self._notify(name, "溜达一圈回来啦，准备给你拍张照～")
        if bool(self.store.get("companion_walk_auto_selfie", True)):
            QTimer.singleShot(1400, lambda: self._take_selfie(reason))
        else:
            self._notify("我回来了", "溜达一圈，回来啦。")

    def toggle(self) -> dict[str, Any]:
        return self.stop("hotkey") if self.walking else self.start()

    # ------------------------------------------------------------ 途中消息
    def _message_interval_ms(self) -> int:
        """隔多久报一句：分钟 → 毫秒（最小 1 分钟）。"""
        try:
            every = int(self.store.get("companion_walk_message_every_min", 4) or 4)
        except (TypeError, ValueError):
            every = 4
        return max(1, every) * 60000

    def _schedule_message(self) -> None:
        """排下一次「报一句」。

        单次定时器 + 触发后重排：``_narrate`` 里有同步的 LLM 调用（最长 45s），
        周期定时器会在调用期间排队重入。此前这里只 ``start()`` 了一次单次定时器、
        没有任何地方重排，于是「每 4 分钟报一句」实际只报了一句（真机 bug）。
        """
        if not self.walking or self._ends_at is None:
            return
        every = self._message_interval_ms()
        left_ms = (self._ends_at - datetime.now()).total_seconds() * 1000
        # 剩余时间还不够一个间隔就不排了，免得刚回家又补一句
        if left_ms <= every * 1.2:
            return
        self._msg_timer.start(every)

    def _on_msg_tick(self) -> None:
        if not self.walking:
            return
        try:
            self._narrate("during")
        finally:
            self._schedule_message()

    # ---------------------------------------------------------------- 走路
    def _nudge_walk(self) -> None:
        """让她真的走两步：复用宿主自己的移动动画与可达性判定。"""
        from . import runtime

        if self._hidden:      # 人在外面，桌面上没有她，别再驱动走动
            return
        host = runtime.move_host() or self._window()
        if host is None:
            return
        moves = getattr(host, "moves", None) or []
        if not moves:
            return
        try:
            host.trigger_move(random.choice(list(moves)))
        except Exception:
            log.debug("触发走动失败", exc_info=True)

    # ---------------------------------------------------------------- 说话
    def _say(self, text: str, sticky_ms: int = 5000, *, speak: bool = True) -> None:
        from . import runtime

        if speak:
            self._speak(text)
        if not runtime.bubble(str(text), int(sticky_ms)):
            log.debug("气泡没发出去（没有可用的宿主窗口）")
            log.info("%s", text)

    def _speak(self, text: str) -> None:
        """念出来：和 AI 对话共用同一个本地播放器（不打断正在念的回复）。

        没开本地语音 / 没配参考音频时静默返回，气泡和通知照旧。
        """
        text = str(text or "").strip()
        if not text:
            return
        try:
            from . import tts as tts_mod

            tts_mod.speak_text(text, self.store, kind="walk", interrupt=False)
        except Exception:  # noqa: BLE001
            log.debug("溜达播报失败", exc_info=True)

    def _notify(self, title: str, message: str) -> None:
        fn = getattr(self.app, "system_notify", None)
        if callable(fn):
            try:
                fn(title, message, duration_ms=7000)
                return
            except TypeError:
                try:
                    fn(title, message)
                    return
                except Exception:
                    log.debug("通知失败", exc_info=True)
            except Exception:
                log.debug("通知失败", exc_info=True)
        log.info("%s：%s", title, message)

    def _narrate(self, kind: str) -> None:
        if not self.walking and kind != "end":
            return
        ctx = schedule.current_context(self.store)
        text = ""
        try:
            from . import llm

            now = datetime.now()
            item = (ctx or {}).get("item") or {}
            where = item.get("scene") or item.get("location") or "外面"
            task = {
                "start": "这是你刚出门时发的：说一声你出门了、准备去哪。",
                "end": "这是你回来时发的：说你回来了，马上把照片给他看。",
            }.get(kind, "这是你在外面走了一会儿之后发的：带一点此刻的小细节（天气、吃的、看到的东西）。")
            prompt = "\n".join(x for x in [
                f"你是「{self._pet_name()}」，正在外面溜达。给家里的人发一条微信消息，说你此刻在做什么、看到了什么。",
                f"你原本的安排：{item.get('start')}–{item.get('end')} {item.get('activity')}（{where}）" if item else "你就在附近随便走走。",
                f"现在是 {now:%H:%M}。",
                task,
                "要求：一句口语，15–40 字，可以带「～」，不要说「日程」「计划」，不要解释，也不要加引号。只输出这句话。",
            ] if x)
            text = llm.ask(self.store.cfg, [
                {"role": "system", "content": "你只回一句话，不要任何解释。"},
                {"role": "user", "content": prompt},
            ], max_tokens=160, temperature=1.1, timeout=45).strip()
        except Exception:
            text = ""
        text = "".join(ch for ch in text if ch not in "「」\"“”").strip()
        text = _clip_sentence(text)
        if not text:
            item = (ctx or {}).get("item") or {}
            text = f"{item.get('activity') or '在外面走走'}，{item.get('scene') or item.get('location') or '附近'}～"
        now = datetime.now()
        recent = (self._last_message_at is not None
                  and (now - self._last_message_at).total_seconds() < 90)
        if text == self._last_message and recent:
            # 同一句只在 90s 内去重：LLM 失败时回退文案是固定的，
            # 按无限期去重会让后续每一句都被吞掉（真机表现＝只报一次）
            return
        self._last_message = text
        self._last_message_at = now
        self._notify(self._pet_name(), text)
        # kind="start" 那句和刚出门时念的「我出门啦～大概 X 分钟后回来」说的是同一件事，
        # 两句只隔 5 秒，念出来会连成啰嗦的一段；所以开场只出气泡 / 通知。
        self._say(text, sticky_ms=8000, speak=(kind != "start"))

    # ---------------------------------------------------------------- 自拍
    def take_selfie(self, reason: str = "manual", overrides: dict[str, Any] | None = None) -> None:
        """单独拍一张（不溜达），给照片窗的「再拍一张 / 换素材重拍」和菜单用。"""
        self._take_selfie(reason, overrides)

    def _take_selfie(self, reason: str, overrides: dict[str, Any] | None = None) -> None:
        if self._worker is not None:
            return
        # 拍照的过程提示只出气泡不念：这类「正在生成…」念出来很吵，
        # 真正要念的是回来那句和照片文案
        self._say("举起手机拍一张…", sticky_ms=8000, speak=False)
        worker = _SelfieWorker(self.store, self._hint,
                               "walk" if reason != "manual" else "manual", self,
                               overrides=overrides)
        worker.status.connect(lambda text: self._say(text, sticky_ms=6000, speak=False))
        worker.done.connect(self._on_selfie_done)
        worker.failed.connect(self._on_selfie_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self) -> None:
        self._worker = None

    def _on_selfie_done(self, rec: Any) -> None:
        caption = str((rec or {}).get("caption") or "拍好啦～")
        self._say(caption, sticky_ms=8000)
        try:
            self._show_photo(rec)
        except Exception:
            log.exception("弹出照片失败")

    def _on_selfie_failed(self, error: str) -> None:
        self._say(f"照片没拍成：{str(error)[:60]}", sticky_ms=9000, speak=False)
        self._notify("自拍失败", str(error)[:120])

    def _show_photo(self, rec: Any) -> None:
        from pathlib import Path

        from .photo import PhotoWindow

        path = Path(str((rec or {}).get("path") or ""))
        if not path.is_file():
            return
        windows = getattr(self, "_photo_windows", None)
        if windows is None:
            windows = []
            self._photo_windows = windows
        # 同一时刻只留最新一张
        for old in list(windows):
            try:
                old.close()
            except Exception:
                pass
        windows.clear()

        def forget(widget):
            if widget in windows:
                windows.remove(widget)

        window = PhotoWindow(path, str((rec or {}).get("caption") or ""),
                             title="她发来的自拍",
                             sub=self._photo_sub(rec), assets=self._photo_assets(rec),
                             on_retake=lambda: self.take_selfie("manual"),
                             on_swap=lambda rec=rec: self._open_swap(rec), on_closed=forget)
        windows.append(window)
        host = self._window()
        screen = None
        if host is not None:
            getter = getattr(host, "screen", None)
            if callable(getter):
                try:
                    screen = getter()
                except Exception:
                    screen = None
        if screen is None:
            from PySide6.QtGui import QGuiApplication

            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            win = self._window()
            anchor_x = win.x() if win is not None and hasattr(win, "x") else area.center().x()
            anchor_y = win.y() if win is not None and hasattr(win, "y") else area.center().y()
            x = max(area.left() + 8, min(anchor_x - window.width() // 2, area.right() - window.width() - 8))
            y = max(area.top() + 8, min(anchor_y - window.height() + 140, area.bottom() - window.height() - 8))
            window.move(x, y)
        window.show()
        window.raise_()

    @staticmethod
    def _photo_sub(rec: Any) -> str:
        """照片卡上那行「她那会儿在做什么」。"""
        data = (rec or {}).get("schedule") or {}
        if not data:
            return ""
        bits = []
        span = str(data.get("span") or "").strip()
        activity = str(data.get("activity") or "").strip()
        where = str(data.get("location") or "").strip()
        head = " ".join(x for x in (span, activity) if x)
        if head:
            bits.append(head + (f" @ {where}" if where else ""))
        day_type = str(data.get("dayType") or "").strip()
        if day_type:
            bits.append(day_type)
        return "日程 · " + " · ".join(bits)

    @staticmethod
    def _photo_assets(rec: Any) -> str:
        """照片卡上那行「这张图用了素材库里的哪些东西」。"""
        selected = (rec or {}).get("selected") or {}
        bits = []
        for key, label in (("characters", "人物"), ("outfits", "服饰"), ("scene", "场景"), ("props", "道具")):
            value = selected.get(key)
            if isinstance(value, list) and value:
                bits.append(f"{label} {'、'.join(str(v) for v in value[:2])}")
            elif isinstance(value, str) and value:
                bits.append(f"{label} {value}")
        return " · ".join(bits)

    def _open_swap(self, rec: Any = None, _card: Any = None) -> None:
        """照片卡「换素材」：选衣服 / 场景 / 道具，然后带这套重拍。"""
        from PySide6.QtWidgets import QDialog

        from .assets_picker import SwapAssetsDialog

        try:
            dialog = SwapAssetsDialog(self.store, rec or {})
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            chosen = dialog.chosen() if accepted else {}
            if accepted:
                self.take_selfie("swap", chosen)
        except Exception:  # noqa: BLE001
            log.exception("换素材失败")

    def _open_library(self) -> None:
        from . import runtime

        runtime.open_library(self._window())

    # ---------------------------------------------------------------- 退场
    def shutdown(self) -> None:
        self._closed = True
        self._vis_token += 1
        self._show_pet()
        self._msg_timer.stop()
        self._end_timer.stop()
        self._walk_timer.stop()
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                worker.wait(400)
            except Exception:
                pass

# -*- coding: utf-8 -*-
"""主动对话：桌宠按随机间隔自己开口，消息同时进「AI 对话」。

设计参考 astrbot_plugin_proactive_chat 的思路（沉默一段时间后主动发起、
随机间隔、免打扰时段、未回复计数驱动情绪、上下文感知），但落到桌宠上：
- 间隔与免打扰、每日上限、未回复上限都在「桌宠设置 → 主动对话」里配；
- 生成的消息作为一条 assistant 消息写进该角色最近的会话文件，
  打开「AI 对话」就能看到上下文；
- 同时弹头顶气泡 + 右下角桌面通知。
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from . import llm, schedule
from .store import CompanionStore

log = logging.getLogger(__name__)

_QUIET_RE = re.compile(
    r"^\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?\s*[-~—－到至]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?\s*$"
)

DEFAULT_PROMPT = (
    "现在是 {clock}，你正在{activity}{location_part}。\n"
    "你是「{name}」。{persona_line}"
    "{partner_line}{memory_block}{tail_line}{spoken_line}"
    "像发微信一样主动给对方发一条消息，一到两句话，30 字以内。\n"
    "第一句就要有内容（手上的动作、刚才的小插曲、突然想到的事），"
    "不要「在吗」「忙吗」这种问法，不要重复上面聊过的，最多问一个问题。{mood_line}\n"
    "【记忆】只当参考，别把记得的事原样复述成清单。\n"
    "只输出你要说的那句话，不要引号、不要解释。"
)


# ------------------------------------------------------------------ 纯工具
def parse_quiet_hours(text: Any) -> tuple[int, int] | None:
    """``0-8`` / ``23:30-7:00`` → (起始分钟, 结束分钟)；解析不了返回 None。"""
    m = _QUIET_RE.match(str(text or "").strip())
    if not m:
        return None
    sh, sm, eh, em = m.groups()
    start_h, end_h = int(sh), int(eh)
    if not (0 <= start_h <= 23 and 0 <= end_h <= 24):
        return None
    start = start_h * 60 + (int(sm) if sm else 0)
    end = (end_h % 24) * 60 + (int(em) if em else 0)
    start = max(0, min(24 * 60 - 1, start))
    end = max(0, min(24 * 60 - 1, end))
    if start == end:
        return None  # 全时段静音没意义，按「没配」处理
    return start, end


def in_quiet(minutes: int, window: tuple[int, int] | None) -> bool:
    """落在免打扰区间内（支持跨零点，如 23-7）。"""
    if not window:
        return False
    start, end = window
    minutes %= 24 * 60
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def minutes_until_window_end(minutes: int, window: tuple[int, int]) -> int:
    """还要多久出免打扰。"""
    start, end = window
    minutes %= 24 * 60
    return (end - minutes) % (24 * 60) or (24 * 60)


def clamp_interval(lo: Any, hi: Any) -> tuple[int, int]:
    """把 min/max 间隔夹到 1..720 分钟，并保证 lo <= hi。"""
    def _i(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d
    a = max(1, min(720, _i(lo, 45)))
    b = max(1, min(720, _i(hi, 120)))
    return (a, b) if a <= b else (b, a)


def pick_delay_minutes(lo: int, hi: int, rng: random.Random | None = None) -> float:
    """在 [lo, hi] 里挑一个间隔（分钟，带秒级抖动）。"""
    r = rng or random
    if hi <= lo:
        return float(lo)
    return r.uniform(float(lo), float(hi))


# ------------------------------------------------------------------ 服务
class ProactiveChatService(QObject):
    """定时主动开口。所有失败都只记日志，不影响桌宠本体。"""

    spoke = Signal(str)

    def __init__(self, app) -> None:
        super().__init__(app if isinstance(app, QObject) else None)
        self.app = app
        self.cfg = app.config
        self.store = CompanionStore(self.cfg)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._busy = False
        self._stopped = True
        self._next_at: datetime | None = None
        self._rng = random.Random()
        self._state_path = self.store.root / "proactive_state.json"
        self._state = self._read_state()
        # 通知必须回主线程做（后台线程里弹气泡/桌面通知不安全）
        self.spoke.connect(self._on_spoke)

    # -------------------------------------------------------------- 配置
    def _get(self, key: str, default: Any) -> Any:
        try:
            value = self.store.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None or value == "" else value

    @property
    def enabled(self) -> bool:
        return bool(self._get("companion_proactive_enabled", True))

    def interval(self) -> tuple[int, int]:
        return clamp_interval(self._get("companion_proactive_min_minutes", 45),
                              self._get("companion_proactive_max_minutes", 120))

    def quiet_window(self) -> tuple[int, int] | None:
        return parse_quiet_hours(self._get("companion_proactive_quiet_hours", "0-8"))

    def daily_limit(self) -> int:
        try:
            return max(0, int(self._get("companion_proactive_daily_limit", 8)))
        except (TypeError, ValueError):
            return 8

    def max_unanswered(self) -> int:
        try:
            return max(0, int(self._get("companion_proactive_max_unanswered", 3)))
        except (TypeError, ValueError):
            return 3

    # -------------------------------------------------------------- 状态
    def _read_state(self) -> dict[str, Any]:
        data = self.store.read_json(self._state_path, {})
        return data if isinstance(data, dict) else {}

    def _write_state(self) -> None:
        try:
            self.store.write_json(self._state_path, self._state)
        except Exception:  # noqa: BLE001
            log.debug("主动对话状态写盘失败", exc_info=True)

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _count_today(self) -> int:
        return int(self._state.get("count", 0)) if self._state.get("date") == self._today() else 0

    def _bump_count(self) -> None:
        today = self._today()
        if self._state.get("date") != today:
            self._state = {"date": today, "count": 0}
        self._state["count"] = int(self._state.get("count", 0)) + 1
        self._state["last_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_state()

    # -------------------------------------------------------------- 排程
    def start(self) -> None:
        """启动排程：第一次按「启动后多久」推，之后按随机间隔。"""
        self._stopped = False
        if not self.enabled:
            log.info("主动对话未启用")
            return
        if self._daily_cap_reached():
            self._schedule_at_next_morning()
            return
        try:
            first = max(1, int(self._get("companion_proactive_first_minutes", 5)))
        except (TypeError, ValueError):
            first = 5
        delay = self._rng.uniform(0.6 * first, first + 0.6 * first)
        self._arm(max(1.0, delay))
        log.info("主动对话已排程：%.0f 分钟后第一次", delay)

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()

    def reload(self) -> None:
        """设置改完调一次：开关变了就起停，间隔变了重排下一次。"""
        if not self.enabled:
            self.stop()
            log.info("主动对话已关闭")
            return
        if self._stopped:
            self.start()
            return
        if self._busy:
            return
        # 设置页关一次就重排一次的话，45–120 分钟的间隔永远等不到：
        # 只要新参数还容得下原计划就原样保留（用户实测「一天只说一两次」的元凶之一）。
        lo, hi = self.interval()
        remain = ((self._next_at - datetime.now()).total_seconds() / 60.0
                  if self._next_at is not None else None)
        window = self.quiet_window()
        now = datetime.now()
        quiet_now = bool(window and in_quiet(now.hour * 60 + now.minute, window))
        if remain is not None and lo <= remain <= hi and not quiet_now:
            log.info("主动对话保持原计划：%s 再说（还有 %.0f 分钟）",
                     self._next_at.strftime("%H:%M"), remain)
            return
        self._schedule_next()
        log.info("主动对话已按新设置重排")

    def _arm(self, minutes: float) -> None:
        minutes = max(1.0, float(minutes))
        self._next_at = datetime.now() + timedelta(minutes=minutes)
        self._timer.start(int(minutes * 60000))
        log.info("下次主动对话：%s（约 %.0f 分钟后）", self._next_at.strftime("%H:%M"), minutes)

    def _schedule_next(self) -> None:
        lo, hi = self.interval()
        delay = pick_delay_minutes(lo, hi, self._rng)
        # 落在免打扰里就挪到免打扰结束
        window = self.quiet_window()
        now = datetime.now()
        if window and in_quiet(now.hour * 60 + now.minute, window):
            delay = minutes_until_window_end(now.hour * 60 + now.minute, window) + self._rng.uniform(0.5, 3)
        self._arm(delay)

    def _schedule_at_next_morning(self) -> None:
        window = self.quiet_window()
        now = datetime.now()
        if window:
            delay = minutes_until_window_end(now.hour * 60 + now.minute, window) + self._rng.uniform(1, 5)
        else:
            delay = 8 * 60
        self._arm(delay)

    def _daily_cap_reached(self) -> bool:
        cap = self.daily_limit()
        return bool(cap) and self._count_today() >= cap

    # -------------------------------------------------------------- 触发
    def _tick(self) -> None:
        if self._stopped:
            return
        if not self.enabled:
            log.info("主动对话已关闭，停止排程")
            self.stop()
            return
        if self._daily_cap_reached():
            self._schedule_at_next_morning()
            return
        now = datetime.now()
        window = self.quiet_window()
        if window and in_quiet(now.hour * 60 + now.minute, window):
            self._schedule_at_next_morning()
            return
        if self._busy:
            self._schedule_next()
            return
        unanswered = self._unanswered()
        if unanswered >= self.max_unanswered() > 0:
            # 以前这里直接排到明天早上，等于「一天只主动一两次」。
            # 现在只跳过这一轮：对方可能只是暂时不在，下一轮再试。
            log.info("连续 %d 条没被回复，这一轮先不说（下轮再试）", unanswered)
            self._schedule_next()
            return
        try:
            self.fire_now(reason="timer")
        finally:
            self._schedule_next()

    def fire_now(self, *, reason: str = "manual") -> bool:
        """立刻生成并投递一条主动消息。返回是否真的发出去了。"""
        if self._busy:
            return False
        self._busy = True
        try:
            text = self._generate()
            if not text:
                return False
            if self._append_to_chat(text):
                self._bump_count()
            self._remember_spoken(text)
            self.spoke.emit(text)
            log.info("[主动对话/%s][%s] %s", reason, self.mood()[0], text)
            return True
        except llm.CompanionLLMError as exc:
            log.info("主动对话跳过：%s", exc)
            return False
        except Exception:  # noqa: BLE001
            log.exception("主动对话失败")
            return False
        finally:
            self._busy = False

    # -------------------------------------------------------------- 内容
    def _unanswered(self) -> int:
        """会话末尾连续「她说的、用户没回」的条数。

        快速对话 / 灵动岛 / 完整窗口可能落在不同会话文件里：只要她上次开口之后，
        **任何**一个会话里出现过用户消息，就当他回过了（否则会误判成 4 条没人理，
        然后一整天不再开口）。"""
        session = self._latest_session()
        if session is None:
            return 0
        count = 0
        for message in reversed(list(session.messages or [])):
            if str(getattr(message, "role", "")) == "user":
                break
            count += 1
        if count and self._replied_since(str(self._state.get("last_at") or "")):
            return 0
        return count

    def _replied_since(self, stamp: str) -> bool:
        """她上次开口之后，用户有没有在任意会话里说过话。"""
        raw = str(stamp or "").strip()
        if not raw:
            return False
        try:
            since = datetime.fromisoformat(raw)
        except ValueError:
            return False
        try:
            sessions = self._chat_store().list(self._character_id())
        except Exception:  # noqa: BLE001
            log.debug("读会话列表失败", exc_info=True)
            return False
        for session in sessions:
            for message in list(getattr(session, "messages", []) or []):
                if str(getattr(message, "role", "")) != "user":
                    continue
                try:
                    when = datetime.fromisoformat(
                        str(getattr(message, "created_at", "") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when.tzinfo is None and since.tzinfo is not None:
                    when = when.astimezone()
                if when.tzinfo is not None and since.tzinfo is None:
                    since = since.astimezone()
                if when > since:
                    return True
        return False

    def _latest_session(self):
        try:
            sessions = self._chat_store().list(self._character_id())
        except Exception:  # noqa: BLE001
            log.debug("读会话列表失败", exc_info=True)
            return None
        if not sessions:
            return None
        return max(sessions, key=lambda s: str(getattr(s, "updated_at", "")))

    def _build_prompt(self) -> str:
        name = str(self.store.get("companion_character_name", "") or "茶叶蛋").strip() or "茶叶蛋"
        partner = str(self.store.get("companion_partner_name", "") or "").strip()
        persona = str(self.store.get("companion_persona", "") or "").strip()
        ctx = schedule.current_context(self.store) or {}
        item = ctx.get("item") or {}
        activity = str(item.get("activity") or "发呆").strip()
        location = str(item.get("location") or "").strip()
        detail = str(item.get("detail") or "").strip()
        unanswered = self._unanswered()
        tail = self._recent_tail(self._history_count()) if self._history_count() else ""
        memory_block = self._memory_block()
        mood_label, mood_hint = self.mood()
        spoken = self._spoken_recent()
        spoken_line = f"【你最近说过】别重复这些：\n{spoken}\n" if spoken else ""

        # 人设可能是一整篇长文，截断后再塞进提示词，否则「此刻在做什么」会被淹没
        persona = persona.replace("\n", " ").strip()
        if len(persona) > 320:
            persona = persona[:320] + "…"
        template = str(self._get("companion_proactive_prompt", "") or "").strip() or DEFAULT_PROMPT
        try:
            return template.format(
                name=name,
                persona=persona,
                persona_line=(persona + "\n") if persona else "",
                clock=datetime.now().strftime("%H:%M"),
                activity=activity,
                location_part=f"（{location}）" if location else "",
                detail=detail,
                partner_line=f"你叫对方「{partner}」。\n" if partner else "",
                memory_block=memory_block,
                memory_line=f"刚才的小插曲：{detail}\n" if detail else "",
                tail_line=f"你们最近聊过：\n{tail}\n" if tail else "",
                mood_line=mood_hint,
                spoken_line=spoken_line,
            )
        except (KeyError, IndexError, ValueError):
            return DEFAULT_PROMPT.format(
                name=name, persona=persona, persona_line=(persona + "\n") if persona else "",
                clock=datetime.now().strftime("%H:%M"),
                activity=activity, location_part=f"（{location}）" if location else "",
                detail=detail, partner_line="", memory_block="", memory_line="",
                tail_line="", mood_line="", spoken_line="",
            )

    def _history_count(self) -> int:
        try:
            return max(0, min(40, int(self._get("companion_proactive_history_count", 12))))
        except (TypeError, ValueError):
            return 12

    def _memory_block(self) -> str:
        """长期记忆（她记得的用户信息）：与「AI 对话」共用同一份 memory.json。"""
        if not bool(self._get("companion_proactive_use_memory", True)):
            return ""
        try:
            if not bool(self.cfg.get("chat_memory_enabled", True)):
                return ""   # 聊天里关了记忆，这里也不该偷偷用
            from ..chat import memory as memory_mod

            text = memory_mod.injection_text(
                True, self.cfg.dir, getattr(self.cfg, "instance_id", ""), self._character_id())
        except Exception:  # noqa: BLE001
            log.debug("读取长期记忆失败", exc_info=True)
            return ""
        text = " ".join(str(text or "").split())
        if len(text) > 500:
            text = text[:500] + "…"
        return f"【你记得的事】{text}\n" if text else ""

    # ------------------------------------------------------------ 情绪（持久化）
    def _last_user_at(self) -> datetime | None:
        session = self._latest_session()
        if session is None:
            return None
        for message in reversed(list(session.messages or [])):
            if str(getattr(message, "role", "")) == "user":
                raw = str(getattr(message, "created_at", "") or "")
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    return None
        return None

    def mood(self) -> tuple[str, str]:
        """当前情绪 → (标签, 注入提示词的一句)。跟着「没被回应 + 隔了多久 + 几点」走。"""
        unanswered = self._unanswered()
        last_user = self._last_user_at()
        hours = None
        if last_user is not None:
            hours = max(0.0, (datetime.now().astimezone() - last_user).total_seconds() / 3600)
        hour = datetime.now().hour

        if unanswered >= 3:
            return "委屈", "你已经连着说了几句都没人接，语气收一点、别追问，可以有点小失落但不要抱怨。"
        if unanswered == 2:
            return "小情绪", "你已经连着说了两句没人回，这次说短一点，别重复上一句的意思。"
        if hours is not None and hours >= 10:
            return "想念", "你们有一阵子没说话了，可以自然地提一句，像随口想起对方。"
        if hour >= 23 or hour < 6:
            return "困", "你已经很困了，句子更短更软，别兴奋。"
        if hours is not None and hours <= 1:
            return "雀跃", "刚聊过没多久，语气轻快一点，接着刚才的气氛。"
        return "平常", ""

    def _spoken_recent(self, limit: int = 4) -> str:
        items = [str(x) for x in (self._state.get("spoken") or []) if str(x).strip()]
        return "\n".join(f"- {x}" for x in items[-limit:])

    def _remember_spoken(self, text: str) -> None:
        items = [str(x) for x in (self._state.get("spoken") or []) if str(x).strip()]
        items.append(text)
        self._state["spoken"] = items[-8:]
        self._state["mood"] = self.mood()[0]
        self._write_state()

    def _recent_tail(self, limit: int = 6) -> str:
        session = self._latest_session()
        if session is None:
            return ""
        lines = []
        for message in list(session.messages or [])[-limit:]:
            role = "我" if str(getattr(message, "role", "")) == "user" else "你"
            text = " ".join(str(getattr(message, "content", "")).split())[:60]
            if text:
                lines.append(f"{role}：{text}")
        return "\n".join(lines)

    def _generate(self) -> str:
        prompt = self._build_prompt()
        text = llm.ask(self.cfg, [{"role": "user", "content": prompt}],
                       max_tokens=200, temperature=1.0, timeout=60)
        return clean_reply(text)

    # -------------------------------------------------------------- 投递
    def _character_id(self) -> str:
        try:
            from .. import catalog
            return str(self.cfg.get("character", catalog.DEFAULT_CHARACTER))
        except Exception:  # noqa: BLE001
            return "shenshen"

    def _chat_store(self):
        from ..chat.session_store import SessionStore
        return SessionStore(self.cfg.dir, getattr(self.cfg, "instance_id", ""))

    def _append_to_chat(self, text: str) -> bool:
        """写进该角色最近的会话；没有会话就新建一个，等同「AI 对话」里的新对话。"""
        from ..chat.models import ChatMessage, ChatSession

        store = self._chat_store()
        character = self._character_id()
        session = self._latest_session()
        message = ChatMessage(role="assistant", content=text)
        if session is None:
            try:
                settings = self.cfg.chat_settings()
                provider = str(getattr(settings, "active_provider", "") or "")
                system_prompt = str(getattr(settings, "default_system_prompt", "") or "")
            except Exception:  # noqa: BLE001
                provider, system_prompt = "", ""
            session = ChatSession.create(character, provider, system_prompt)
            session.messages.append(message)
            store.save(session)
            return True
        updated, _ = store.append_messages(session, [message])
        return updated is not None

    def _on_spoke(self, text: str) -> None:
        """主线程里做提示（气泡 + 桌面通知 + 可选语音）。"""
        self._notify(str(text))
        self._speak(str(text))

    def _speak(self, text: str) -> None:
        # 走进程级同一个播放器：主动消息和聊天回复共用一个队列，
        # 不会出现"一句主动消息 + 一句回复"两个声音打架。
        try:
            from . import tts as tts_mod

            tts_mod.speak_text(text, self.store, kind="proactive", interrupt=False)
        except Exception:  # noqa: BLE001
            log.debug("朗读失败", exc_info=True)

    def _notify(self, text: str) -> None:
        from . import runtime

        try:
            runtime.bubble(text, 7000)
        except Exception:  # noqa: BLE001
            log.debug("气泡发送失败", exc_info=True)
        fn = getattr(self.app, "system_notify", None)
        if callable(fn):
            name = str(self.store.get("companion_character_name", "") or "茶叶蛋")
            try:
                fn(name, text, duration_ms=8000)
            except TypeError:
                fn(name, text)
            except Exception:  # noqa: BLE001
                log.debug("桌面通知失败", exc_info=True)


def clean_reply(text: Any, limit: int = 120) -> str:
    """去掉引号/换行/自言自语的前后缀，压成一句话。"""
    value = " ".join(str(text or "").split())
    for ch in "「」\"'“”":
        value = value.replace(ch, "")
    value = value.strip(" ：:，,。.~～")
    return value[:limit]


__all__ = [
    "ProactiveChatService",
    "DEFAULT_PROMPT",
    "clean_reply",
    "clamp_interval",
    "in_quiet",
    "minutes_until_window_end",
    "parse_quiet_hours",
    "pick_delay_minutes",
]

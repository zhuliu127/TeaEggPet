# -*- coding: utf-8 -*-
"""进程内单例：把「陪伴扩展」的服务挂到宿主 AppShell 上，供菜单 / 热键 / 设置页共用。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_app = None
_service = None
_proactive = None
_settings_dialog = None
_library_dialog = None
_store = None
_chat_voice_hooked = False


def attach(app) -> None:
    """宿主启动时调一次（幂等）。"""
    global _app, _service
    _app = app
    if _service is None and app is not None:
        try:
            from .walk import WalkService

            _service = WalkService(app)
            _ensure_default_hotkey(app)
            _start_proactive(app)
            _start_schedule_bootstrap(app)
            _start_chat_voice(app)
            _start_tts_warmup(app)
            _hook_chat_schedule()
            log.info("陪伴扩展已就绪")
        except Exception:
            log.exception("陪伴扩展初始化失败（溜达功能不可用）")


def _start_proactive(app) -> None:
    """把「主动对话」挂起来（启动后过几秒才排第一条，别和开机抢资源）。"""
    global _proactive
    if _proactive is not None:
        return
    try:
        from PySide6.QtCore import QTimer

        from .proactive_chat import ProactiveChatService

        _proactive = ProactiveChatService(app)
        QTimer.singleShot(6000, _proactive.start)
    except Exception:
        log.exception("主动对话初始化失败（其余功能不受影响）")


def _start_chat_voice(app) -> None:
    """让「所有对话」都走本地语音：订阅进程级「回复完成」。

    宿主的 ``ChatService.register_global_finished`` 是完整聊天窗 / 快速对话 /
    灵动岛对话**共同的**收口点（``pet/chat_reply_voice.py`` 也是接在这里），
    所以订阅一次就覆盖全部对话入口；这里不再往每个窗口里各埋一份。

    注册的是一次性类级订阅（多窗共用），回调里**每次**现读配置，
    所以用户在设置里开关「念出回复」不需要重新订阅。
    """
    global _chat_voice_hooked
    if _chat_voice_hooked:
        return
    try:
        from ..chat.service import ChatService
    except Exception:  # noqa: BLE001
        log.debug("没有聊天模块（无聊天打包变体），本地语音不订阅回复")
        return
    try:
        ChatService.register_global_finished(_on_chat_reply)
        _chat_voice_hooked = True
    except Exception:  # noqa: BLE001
        log.exception("订阅「AI 回复朗读」失败")


def _on_chat_reply(text: str) -> None:
    """ChatService 发来一条完整回复 → 交给统一播报队列（自动播放）。"""
    try:
        from . import tts as tts_mod

        store = companion_store()
        if store is None:
            return
        tts_mod.speak_text(text, store, kind="chat")
    except Exception:  # noqa: BLE001
        log.debug("朗读 AI 回复失败", exc_info=True)


def _hook_chat_schedule() -> None:
    """让「AI 对话」知道她此刻在做什么：日程段注入 system。

    背景（用户实测）：问「在干什么」，回答和当天日程（「赶新需求加班」）无关——
    日程以前只喂给了主动对话与自拍，聊天侧只有时间和人设。
    注册一个 provider 而不是 import 到 chat 里，是为了让 chat 保持对陪伴扩展零依赖。
    日程没生成 / 读失败时 provider 返回空串，聊天提示词与以前逐字节一致。
    """
    try:
        from ..chat import prompt as chat_prompt
        from . import schedule as schedule_mod
    except Exception:  # noqa: BLE001 —— 无聊天变体时跳过
        log.debug("没有聊天模块，日程不注入对话")
        return

    def provider() -> str:
        store = companion_store()
        if store is None:
            return ""
        return schedule_mod.chat_context(store)

    try:
        chat_prompt.set_companion_context(provider)
        log.info("日程已接入 AI 对话（问「在干什么」按当天日程回答）")
    except Exception:  # noqa: BLE001
        log.exception("日程接入对话失败（对话仍可用）")


def companion_store():
    """进程级 CompanionStore（聊天回复/识屏问答都要读同一份配置）。"""
    global _store
    if _store is None and _app is not None:
        try:
            from .store import CompanionStore

            _store = CompanionStore(_app.config)
        except Exception:  # noqa: BLE001
            log.debug("创建 CompanionStore 失败", exc_info=True)
            return None
    return _store


def _start_schedule_bootstrap(app) -> None:
    """今天没有日程就后台补一份（不阻塞启动）。"""
    try:
        from PySide6.QtCore import QTimer

        from . import bootstrap

        QTimer.singleShot(2500, lambda: bootstrap.ensure_today(app))
    except Exception:
        log.exception("日程自检初始化失败")


def _start_tts_warmup(app) -> None:
    """念白开着时，启动后顺手把 api_v2 准备好（地址填成网页端口也能自愈）。"""
    try:
        from PySide6.QtCore import QTimer

        from . import tts as tts_mod

        # 首次合成失败（服务还没起来）也走同一条自愈路径，不用用户先去设置页
        tts_mod.set_heal_hook(_heal_tts)
        QTimer.singleShot(20000, lambda: _warmup_tts(app))
    except Exception:
        log.debug("语音预热初始化失败", exc_info=True)


_healing = False


def _heal_tts(cfg: dict | None = None) -> None:
    """朗读时发现服务没起来：后台 ensure 一次，起来了就把地址写回配置。

    只做一次（``_healing``），失败也静默——下一句朗读还会再触发（tts 侧 60 秒冷却）。
    """
    global _healing
    if _healing:
        return
    store = companion_store()
    if store is None:
        return
    _healing = True
    bridge = _WarmupBridge(store)

    def run() -> None:
        global _healing
        try:
            from . import tts as tts_mod

            conf = dict(cfg or tts_mod.tts_config(store))
            ok, api, message = tts_mod.ensure_server(conf, wait=150.0)
            log.info("本地语音自愈：%s", message)
            if ok and api and api != conf.get("api"):
                bridge.report(api)
        except Exception:  # noqa: BLE001
            log.debug("本地语音自愈失败", exc_info=True)
        finally:
            _healing = False

    import threading

    threading.Thread(target=run, name="companion-tts-heal", daemon=True).start()


class _WarmupBridge:
    """后台线程只负责探测/启动，回填配置这件事回主线程做。"""

    def __init__(self, store) -> None:
        from PySide6.QtCore import QObject, Signal

        class _Bridge(QObject):
            picked = Signal(str)

        self._obj = _Bridge()
        self._store = store
        self._obj.picked.connect(self._save)

    def report(self, api: str) -> None:
        try:
            self._obj.picked.emit(str(api or ""))
        except Exception:  # noqa: BLE001
            pass

    def _save(self, api: str) -> None:
        if not api:
            return
        try:
            self._store.apply({"companion_tts_api": api})
            log.info("本地语音已就绪：%s", api)
        except Exception:  # noqa: BLE001
            log.debug("保存语音地址失败", exc_info=True)


def _warmup_tts(app) -> None:
    import threading

    try:
        from . import tts as tts_mod
        from .store import CompanionStore

        store = CompanionStore(app.config)
        cfg = tts_mod.tts_config(store)
        if not cfg.get("enabled"):
            log.info("本地语音预热跳过：没开「用本地 GPT-SoVITS 念出来」")
            return
        log.info("本地语音预热开始（服务 %s）", cfg.get("api") or "未填")
        bridge = _WarmupBridge(store)

        def run() -> None:
            try:
                if tts_mod.probe(cfg, timeout=2.0)["state"] == "api":
                    # 顺手把用户选的权重切好：否则第一句念白要等切权重，
                    # 点击台词这种"点了就要出声"的场景会明显卡一下。
                    tts_mod.ensure_weights(cfg)
                    _precache_guarded(app, store, cfg, reason="启动预热")
                    return
                ok, api, message = tts_mod.ensure_server(cfg, wait=150.0)
                log.info("本地语音预热：%s", message)
                if ok:
                    bridge.report(api)
                    ready = {**cfg, "api": api}
                    tts_mod.ensure_weights(ready)
                    _precache_guarded(app, store, ready, reason="启动预热")
            except Exception:  # noqa: BLE001
                log.info("本地语音预热失败", exc_info=True)

        threading.Thread(target=run, name="companion-tts-warmup", daemon=True).start()
    except Exception:  # noqa: BLE001
        log.info("本地语音预热失败", exc_info=True)


def _self_talk_texts(app) -> list[str]:
    """点击台词的名单（每只宠一份；legacy / overlay 两种拓扑都覆盖）。

    本地语音接管后，这些句子是**固定且有限**的，值得预先合成进缓存。
    """
    getter = getattr(app, "_self_talk_voice_configs", None)
    if not callable(getter):
        return []
    try:
        views = getter() or []
    except Exception:  # noqa: BLE001
        log.debug("读取点击台词名单失败", exc_info=True)
        return []
    texts: list[str] = []
    for view in views:
        try:
            values = view.get("self_talk_texts", []) or []
        except Exception:  # noqa: BLE001
            continue
        for item in values:
            text = str(item or "").strip()
            if text and text not in texts:
                texts.append(text)
    return texts


_precaching = False
_precache_pending = False


def _precache_guarded(app, store, cfg: dict, *, reason: str = "") -> int:
    """带全局互斥的预合成：启动预热与保存设置两条路不会同时灌同一批台词。"""
    global _precaching
    if _precaching:
        return 0
    _precaching = True
    try:
        return _precache_self_talk(app, store, cfg, reason=reason)
    finally:
        _precaching = False


def _precache_self_talk(app, store, cfg: dict, *, reason: str = "") -> int:
    """把点击台词灌进合成缓存，返回新合成句数（调用方通常在后台线程里）。

    只写缓存、不出声：用户此刻可能在干别的，不能因为预热突然开口。
    """
    try:
        from . import tts as tts_mod

        texts = _self_talk_texts(app)
        if not texts:
            log.info("点击台词预合成跳过：没读到台词名单")
            return 0
        done = tts_mod.precache_texts(cfg, texts)
        if done:
            log.info("点击台词已预合成 %d 句，之后点击直接播缓存%s",
                     done, f"（{reason}）" if reason else "")
        else:
            log.info("点击台词缓存已是最新（%d 句名单）%s",
                     len(texts), f"（{reason}）" if reason else "")
        return done
    except Exception:  # noqa: BLE001
        log.info("预合成点击台词失败", exc_info=True)
        return 0


def precache_self_talk_texts(app, *, reason: str = "") -> bool:
    """对外：后台把点击台词预合成进缓存（非阻塞，重入直接返回 False）。

    保存设置后调用一次，用户点开就能秒出——不用等一句 2~10 秒的合成。
    """
    global _precache_pending
    if _precache_pending:
        return False
    store = companion_store()
    if store is None:
        return False
    try:
        from . import tts as tts_mod

        cfg = tts_mod.tts_config(store)
    except Exception:  # noqa: BLE001
        return False
    if not (cfg.get("enabled") and cfg.get("ref_audio")):
        return False
    if not _self_talk_texts(app):
        return False
    _precache_pending = True
    import threading

    def run() -> None:
        global _precache_pending
        try:
            from . import tts as tts_mod

            ok, api, message = tts_mod.ensure_server(cfg, wait=150.0)
            if not ok:
                log.info("点击台词预合成跳过：%s", message)
                return
            ready = {**cfg, "api": api}
            tts_mod.ensure_weights(ready)
            _precache_guarded(app, store, ready, reason=reason)
        except Exception:  # noqa: BLE001
            log.debug("点击台词预合成失败", exc_info=True)
        finally:
            _precache_pending = False

    threading.Thread(target=run, name="companion-tts-precache", daemon=True).start()
    return True


def proactive():
    """给设置页/菜单用的主动对话服务。"""
    return _proactive


def reload_proactive() -> None:
    if _proactive is not None:
        try:
            _proactive.reload()
        except Exception:
            log.debug("重排主动对话失败", exc_info=True)


def _ensure_default_hotkey(app) -> None:
    """首次启用陪伴扩展时给一个默认快捷键（用户随时可以在设置里改掉）。

    只在「从没设过」时写一次：之后用户清空就是清空，不再自动填回去。
    """
    try:
        cfg = app.config
        changed = False
        if not cfg.get("companion_hotkey_seeded", False):
            if not str(cfg.get("hotkey_go_walk", "") or "").strip():
                cfg.set("hotkey_go_walk", "Alt+F1")
            cfg.set("companion_hotkey_seeded", True)
            changed = True
        if not cfg.get("companion_selfie_hotkey_seeded", False):
            # 「发自拍」也先给一个默认键；之后清空就是清空。
            # 不用 Ctrl+Alt+S：实测被 QQ/微信系截图占着，注册必失败。
            if not str(cfg.get("hotkey_selfie_now", "") or "").strip():
                cfg.set("hotkey_selfie_now", "Ctrl+Shift+S")
            cfg.set("companion_selfie_hotkey_seeded", True)
            changed = True
        if not changed:
            return
        cfg.save()
        apply = getattr(app, "_apply_global_hotkeys", None)
        if callable(apply):
            apply()
        if bubble_host() is not None:
            from PySide6.QtCore import QTimer

            walk_key = str(cfg.get("hotkey_go_walk", "") or "").strip()
            selfie_key = str(cfg.get("hotkey_selfie_now", "") or "").strip()
            tip = "，".join(x for x in (
                f"按 {walk_key} 我出去溜达" if walk_key else "",
                f"{selfie_key} 直接拍张自拍" if selfie_key else "",
            ) if x)
            if tip:
                QTimer.singleShot(4000, lambda: bubble(tip + "～", 8000))
    except Exception:
        log.debug("设置默认快捷键失败", exc_info=True)


def _candidates():
    """可能能冒泡/能动的宿主对象，按优先级排（去重）。

    overlay 拓扑下 ``instance.win`` 恒为 None，呈现面是 ``_overlay_shell``
    （见 app.py 的拓扑分支注释）——菜单/溜达以前只认 instance.win，于是点了
    菜单没反应、溜达也不冒泡。这里统一按能力找，而不是按身份找。
    """
    host = _app
    if host is None:
        return []
    out = []

    def push(obj):
        if obj is not None and obj not in out:
            out.append(obj)

    push(getattr(getattr(host, "instance", None), "win", None))
    push(getattr(host, "win", None))
    push(getattr(host, "_overlay_shell", None))
    return out


def bubble_host():
    """第一个真的能show_bubble 的宿主；没有就 None（调用方要能接受 None）。"""
    for obj in _candidates():
        if callable(getattr(obj, "show_bubble", None)):
            return obj
    return None


def move_host():
    """第一个能 trigger_move 的宿主（legacy 的 PetWindow / sprite 侧的 facade）。"""
    for obj in _candidates():
        if callable(getattr(obj, "trigger_move", None)):
            return obj
    shell = getattr(_app, "_overlay_shell", None) if _app is not None else None
    for name in ("current_sprite", "sprite", "main_sprite"):
        sprite = getattr(shell, name, None) if shell is not None else None
        if callable(getattr(sprite, "trigger_move", None)):
            return sprite
    return None


def bubble(text: str, ms: int = 6000) -> bool:
    """在当前拓扑下冒个泡；返回是否真的发出去了。"""
    win = bubble_host()
    if win is None:
        return False
    try:
        win.show_bubble(str(text), duration_ms=int(ms))
        return True
    except TypeError:
        try:
            win.show_bubble(str(text), int(ms))
            return True
        except Exception:
            log.debug("冒泡失败", exc_info=True)
            return False
    except Exception:
        log.debug("冒泡失败", exc_info=True)
        return False


def service():
    if _service is None and _app is not None:
        attach(_app)
    return _service


def app():
    return _app


def open_settings(parent=None, page: str = "") -> None:
    """打开陪伴扩展设置（单例窗口，关掉再开会重建）。"""
    global _settings_dialog
    host = _app
    if host is None:
        return
    try:
        if _settings_dialog is not None:
            _settings_dialog.raise_()
            _settings_dialog.activateWindow()
            return
        from .settings_dialog import CompanionSettingsDialog

        dialog = CompanionSettingsDialog(host.config, parent or getattr(getattr(host, "instance", None), "win", None))
        _settings_dialog = dialog

        def _closed(_result=None):
            global _settings_dialog
            _settings_dialog = None
            # 设置改完按新参数重排（开关 / 间隔 / 免打扰都可能变）
            reload_proactive()
            # 本地念白开关会决定宿主那条 TTS 通道要不要让位：立刻重算，
            # 否则已经建好的报时 / 回复朗读会继续念，和本地语音叠在一起。
            try:
                host._sync_chime_service()
            except Exception:  # noqa: BLE001
                log.debug("重算宿主语音通道失败", exc_info=True)

        dialog.finished.connect(_closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if page:
            dialog.select_page(page)
    except Exception:
        log.exception("打开陪伴扩展设置失败")


def open_library(parent=None, *, topmost: bool = True, on_closed=None) -> None:
    """打开「素材库与关键词」窗口（单例）。

    parent 只有在**确实是 QWidget** 时才当父窗口用：overlay 拓扑下
    ``bubble_host()`` 返回的是 OverlayShell（壳，不是 QWidget），塞给 QDialog 会抛
    ``TypeError: QDialog.__init__(OverlayShell)``，窗口根本建不出来——照片卡上的
    「换素材」就是这么点不动的。
    topmost 默认开着：照片卡本身是 WindowStaysOnTop，普通窗口会被它压住，
    看着也像「没反应」。
    """
    global _library_dialog
    host = _app
    if host is None:
        log.warning("素材库窗口打不开：陪伴扩展还没挂到宿主")
        return
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        if _library_dialog is not None:
            dialog = _library_dialog
            try:
                if not dialog.isVisible():
                    dialog.show()          # 关过又被复用：只 raise 是叫不回来的
                dialog.raise_()
                dialog.activateWindow()
                if topmost:
                    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
                return
            except RuntimeError:
                _library_dialog = None     # C++ 侧已销毁，重建

        parent_widget = parent if isinstance(parent, QWidget) else None
        from .library_dialog import LibraryDialog

        dialog = LibraryDialog(host.config, parent_widget)
        if topmost:
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        _library_dialog = dialog

        def _closed(_result=None):
            global _library_dialog
            _library_dialog = None
            if callable(on_closed):
                try:
                    on_closed()
                except Exception:
                    log.debug("素材库关闭回调失败", exc_info=True)

        dialog.finished.connect(_closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        log.info("素材库窗口已打开（parent=%s, topmost=%s）",
                 type(parent_widget).__name__ if parent_widget else "None", topmost)
    except Exception:
        log.exception("打开素材库窗口失败")


def shutdown() -> None:
    global _service, _settings_dialog, _library_dialog, _proactive
    if _proactive is not None:
        try:
            _proactive.stop()
        except Exception:
            log.debug("停止主动对话失败", exc_info=True)
        _proactive = None
    try:
        from . import bootstrap as bootstrap_mod

        bootstrap_mod.shutdown()
    except Exception:
        log.debug("关闭日程自检失败", exc_info=True)
    if _service is not None:
        try:
            _service.shutdown()
        except Exception:
            log.debug("关闭陪伴扩展服务失败", exc_info=True)
    _service = None
    _settings_dialog = None
    _library_dialog = None
_library_dialog = None

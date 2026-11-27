# -*- coding: utf-8 -*-
"""全局「按住说话」热键监听（低层键盘钩子 WH_KEYBOARD_LL）。

为什么不用 RegisterHotKey
-------------------------
``RegisterHotKey`` 有两处硬限制，正好都不适合「按住 Win+Alt 说话」：

1. **必须带一个非修饰键**（如 Ctrl+Alt+P 里的 P），单按修饰键（Win、Alt）无法注册；
2. **只有「按下」没有「松开」**——它只回 ``WM_HOTKEY``，拿不到 key-up，
   而推挽式（push-to-talk）恰恰要在**松开时**结束录音。

所以这里改用 ``SetWindowsHookExW(WH_KEYBOARD_LL)`` 低层键盘钩子，实时看
「修饰键组合是否同时按住」，据此发出 ``pressed`` / ``released`` 两个信号。

线程模型
--------
低层钩子回调必须在**装有消息循环**的线程上跑，且不能阻塞太久（否则系统会
把钩子摘掉）。这里开一条专用守护线程：``GetMessageW`` 抽消息 → 回调里只
判断按键并 emit Qt 信号（queued connection，跨线程安全），绝不做重活。

失败如实报
----------
非 Windows、装钩子失败（被安全软件拦等）都返回 ``False``，由上层提示用户
「按住说话不可用」而不是假装生效。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

# ---- Win32 常量 ----
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LMENU = 0xA4          # 左 Alt
VK_RMENU = 0xA5          # 右 Alt
VK_MENU = 0x12           # Alt（通用）
VK_CONTROL = 0x11
VK_SHIFT = 0x10


def is_supported() -> bool:
    return os.name == "nt"


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class PushToTalkHotkey(QObject):
    """低层键盘钩子：当目标修饰键组合「同时按住」→ pressed；全部松开 → released。

    只关心「修饰键」组合（例如 Win+Alt），不吞按键（返回 CallNextHookEx），
    不影响系统正常功能——只是旁听。
    """

    pressed = Signal()
    released = Signal()

    def __init__(self, combo: str = "win+alt", parent=None) -> None:
        super().__init__(parent)
        self._combo = self._parse_combo(combo)
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._hook = None
        self._hook_proc = None
        self._active = False
        self._held = False          # 当前是否处于「按住」状态
        self._stop = threading.Event()

    # ------------------------------------------------------------ 组合键解析
    @staticmethod
    def _parse_combo(combo: str) -> dict:
        """把 'win+alt' 解析成 {win:True, alt:True, ctrl:.., shift:..}。"""
        slots = {"win": False, "alt": False, "ctrl": False, "shift": False}
        for part in str(combo or "").lower().replace(" ", "").split("+"):
            if part in ("win", "meta", "super", "windows"):
                slots["win"] = True
            elif part in ("alt", "menu"):
                slots["alt"] = True
            elif part in ("ctrl", "control"):
                slots["ctrl"] = True
            elif part in ("shift",):
                slots["shift"] = True
        if not any(slots.values()):
            # 兜底：默认 win+alt
            slots["win"] = True
            slots["alt"] = True
        return slots

    def set_combo(self, combo: str) -> None:
        self._combo = self._parse_combo(combo)

    @property
    def installed(self) -> bool:
        return self._active

    @property
    def combo(self) -> str:
        return "+".join(k for k, v in self._combo.items() if v) or "win+alt"

    # ------------------------------------------------------------ 生命周期
    def install(self) -> bool:
        if self._active:
            return True
        if not is_supported():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="PushToTalkHook", daemon=True)
        self._thread.start()
        # 等钩子装好（最多 ~1s）
        for _ in range(100):
            if self._active:
                return True
            threading.Event().wait(0.01)
        return self._active

    def uninstall(self) -> None:
        self._stop.set()
        # 往钩子线程的队列投一个消息，唤醒 GetMessageW
        try:
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        except Exception:  # noqa: BLE001
            pass
        self._active = False

    # ------------------------------------------------------------ 钩子线程
    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.POINTER(_KBDLLHOOKSTRUCT))
        user32.SetWindowsHookExW.restype = ctypes.c_void_p

        def _proc(nCode, wParam, lParam):
            try:
                if nCode == 0:
                    kb = lParam.contents
                    if self._matches(kb.vkCode):
                        is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)
                        if is_down:
                            self._on_key_change()
                        elif is_up:
                            self._on_key_change()
            except Exception:  # noqa: BLE001
                pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_proc = HOOKPROC(_proc)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, None, 0)
        if not self._hook:
            log.warning("按住说话：安装键盘钩子失败（可能被安全软件拦截）")
            self._active = False
            return
        self._active = True
        log.info("按住说话：键盘钩子已安装（组合键=%s）", self.combo)
        # 消息循环
        msg = ctypes.wintypes.MSG() if hasattr(ctypes, "wintypes") else _MSG()
        while not self._stop.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        try:
            user32.UnhookWindowsHookEx(self._hook)
        except Exception:  # noqa: BLE001
            pass
        self._hook = None
        self._active = False
        log.info("按住说话：键盘钩子已卸载")

    def _matches(self, vk: int) -> bool:
        """这个虚拟键是否属于我们要盯的修饰键集合。"""
        if self._combo.get("win") and vk in (VK_LWIN, VK_RWIN):
            return True
        if self._combo.get("alt") and vk in (VK_LMENU, VK_RMENU, VK_MENU):
            return True
        if self._combo.get("ctrl") and vk in (VK_CONTROL, 0xA2, 0xA3):
            return True
        if self._combo.get("shift") and vk in (VK_SHIFT, 0xA0, 0xA1):
            return True
        return False

    def _on_key_change(self) -> None:
        """任一目标修饰键按下/松开 → 复核「是否全部同时按住」，只在状态翻转时发信号。"""
        held = self._query_combo_held()
        if held and not self._held:
            self._held = True
            try:
                self.pressed.emit()
            except Exception:  # noqa: BLE001
                pass
        elif not held and self._held:
            self._held = False
            try:
                self.released.emit()
            except Exception:  # noqa: BLE001
                pass

    def _query_combo_held(self) -> bool:
        """用 GetAsyncKeyState 查当前组合键是否都在按下状态。"""
        try:
            user32 = ctypes.windll.user32
            def down(vk):
                return (user32.GetAsyncKeyState(vk) & 0x8000) != 0
            if self._combo.get("win") and not (down(VK_LWIN) or down(VK_RWIN)):
                return False
            if self._combo.get("alt") and not (down(VK_LMENU) or down(VK_RMENU) or down(VK_MENU)):
                return False
            if self._combo.get("ctrl") and not (down(VK_CONTROL) or down(0xA2) or down(0xA3)):
                return False
            if self._combo.get("shift") and not (down(VK_SHIFT) or down(0xA0) or down(0xA1)):
                return False
            return True
        except Exception:  # noqa: BLE001
            return False


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


__all__ = ["PushToTalkHotkey", "is_supported"]

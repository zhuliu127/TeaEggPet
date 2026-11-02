# -*- coding: utf-8 -*-
"""开发自测：**改快捷键 → 配置重载 → 重新注册**这条链是否真的换了键。

不按真键盘：用假 user32 记录 RegisterHotKey 收到的 (id, modifiers, vk)，
足以证明「主进程读到的绑定」已经从 Alt+F1 变成新键，而不是继续用旧值。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pet import hotkey_spec
from pet.config import Config
from pet.hotkeys import ACTIONS, GlobalHotkeyService


class FakeUser32:
    def __init__(self) -> None:
        self.registered: dict[int, tuple[int, int]] = {}
        self.unregistered: list[int] = []

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, vk):  # noqa: N802
        self.registered[int(hotkey_id)] = (int(modifiers), int(vk))
        return True

    def UnregisterHotKey(self, hwnd, hotkey_id):  # noqa: N802
        self.unregistered.append(int(hotkey_id))
        return True


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hotkeytest-"))
    conf_dir = tmp / "dsh-pet-standalone"
    conf_dir.mkdir(parents=True)
    conf = conf_dir / "config.json"
    conf.write_text(json.dumps({"hotkey_go_walk": "Alt+F1", "hotkey_selfie_now": "Alt+F2"}),
                    encoding="utf-8")

    cfg = Config(tmp)
    user32 = FakeUser32()
    service = GlobalHotkeyService(supported=True, install_native_filter=False, user32=user32)
    service.install()

    def apply_like_app():
        bindings = {action: cfg.get(key) for action, key in ACTIONS.items()}
        service.apply(bindings, {action: (lambda: None) for action in ACTIONS})

    apply_like_app()
    print("① 启动时:", {k: v for k, v in service.active_bindings().items() if k in ("go_walk", "selfie_now")})

    # 用户在设置里改成 Ctrl+Alt+P：设置进程写盘 → 主进程 watcher reload → 重新 apply
    conf.write_text(json.dumps({"hotkey_go_walk": "Ctrl+Alt+P", "hotkey_selfie_now": "Ctrl+Alt+S"}),
                    encoding="utf-8")
    cfg.reload()
    print("  reload 读到:", repr(cfg.get("hotkey_go_walk")))
    old_ids = set(user32.registered)
    apply_like_app()
    print("② 改键后:", {k: v for k, v in service.active_bindings().items() if k in ("go_walk", "selfie_now")})
    print("  注销了旧 id:", sorted(user32.unregistered), "| 新注册表:", user32.registered)

    want = hotkey_spec.parse("Ctrl+Alt+P")
    got = service.active_bindings().get("go_walk")
    ok = got == "Ctrl+Alt+P" and service.active_bindings().get("selfie_now") == "Ctrl+Alt+S"

    # 再清空：应该一个都不注册
    conf.write_text(json.dumps({"hotkey_go_walk": "", "hotkey_selfie_now": ""}), encoding="utf-8")
    cfg.reload()
    apply_like_app()
    print("③ 清空后:", service.active_bindings().get("go_walk"), "| 已注册表:", user32.registered)
    ok = ok and not service.active_bindings()

    print("RESULT:", "PASS" if ok else "FAIL", f"(期望 Ctrl+Alt+P，实际 {got})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

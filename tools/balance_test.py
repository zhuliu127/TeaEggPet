# -*- coding: utf-8 -*-
"""开发自测：余额地址归一化（离线）+ 真实余额（--live，用本机钥匙串里的 Key）。"""
import sys

CASES = [
    ("https://api.deepseek.com", "https://api.deepseek.com"),
    ("https://api.deepseek.com/", "https://api.deepseek.com"),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com"),
    ("https://api.deepseek.com/beta", "https://api.deepseek.com/beta"),
    ("https://proxy.example.com/openai/v1", "https://proxy.example.com/openai/v1"),
    ("https://proxy.example.com/v1/chat/completions", "https://proxy.example.com/v1"),
    ("", ""),
]


def main() -> int:
    from pet.island import balance as balance_mod

    ok = True
    for raw, expect in CASES:
        got = balance_mod.api_root(raw)
        flag = "ok " if got == expect else "BAD"
        if got != expect:
            ok = False
        print(f"{flag} api_root({raw!r}) = {got!r}（期望 {expect!r}）")

    print("\n候选地址：")
    for raw in ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com", ""):
        print(f"  {raw!r} → {balance_mod.balance_endpoints(raw)}")

    if "--live" in sys.argv:
        import keyring

        key = keyring.get_password("dsh-pet-standalone", "provider/openai-main")
        from pet.config import Config

        conf = Config().chat_settings().active_config
        print(f"\n真实查询：base={conf.base_url!r} verify_ssl={conf.verify_ssl}")
        try:
            info = balance_mod.fetch_balance(conf.base_url, key or "", verify_ssl=bool(conf.verify_ssl))
        except Exception as exc:  # noqa: BLE001
            print("失败：", exc)
            ok = False
        else:
            print("成功：", info)
            print("气泡文案：", balance_mod.format_balance(info))
            print("已用百分比：", balance_mod.balance_percent(info.get("total")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

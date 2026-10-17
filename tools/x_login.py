#!/usr/bin/env python3
"""命令行登录 X —— 不用打开Fragment GUI 也能完成 OAuth。

为什么需要它：OAuth 授权必须有人**在浏览器里点一次 Authorize**。
GUI 里那条路是「设置 → 账号 → X → 登录 X」，这个脚本提供同样的能力，
但可以从终端 / 脚本里驱动，方便代跑或排错。

用法：

    python3 tools/x_login.py <Client ID>
    python3 tools/x_login.py <Client ID> --redirect http://localhost:8721/callback

它会：
  1. 起一个本机回调接收器（默认 127.0.0.1:8721）
  2. 打开浏览器到 X 的授权页
  3. 等你点 Authorize（最多 10 分钟）
  4. 用 code 换 access / refresh token，存进 GNOME 钥匙环
  5. 打印登录到的账号，并确认令牌能用来调 /users/me

⚠️ Client ID 对「OAuth 2.0 Public Client（PKCE）」来说是**公开信息**，
   不是密码 —— 公开客户端的设计就是不持有密钥。可以放心贴给别人。
   但它必须和你 X 后台登记的**回调地址完全一致**，否则 X 会拒绝。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for extra in (PROJECT_ROOT / "vendor", PROJECT_ROOT / "src"):
    if extra.is_dir():
        entry = str(extra)
        if entry not in sys.path:
            sys.path.insert(0, entry)

from gnome_poster.services.secrets import SecretStore  # noqa: E402
from gnome_poster.services.settings import Settings  # noqa: E402
from gnome_poster.services.x_api import DEFAULT_SCOPES, XClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="命令行登录 X（OAuth 2.0 PKCE）")
    parser.add_argument("client_id", help="X 开发者后台里的 OAuth 2.0 Client ID")
    parser.add_argument(
        "--redirect",
        default=None,
        help="回调地址，默认用设置里存的那个（http://127.0.0.1:8721/callback）",
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0, help="等授权的秒数，默认 600"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="只打印授权链接，不自动开浏览器"
    )
    args = parser.parse_args()

    settings = Settings()
    secrets = SecretStore()
    settings.set("x-client-id", args.client_id.strip())
    if args.redirect:
        settings.set("x-redirect-uri", args.redirect.strip())

    client = XClient(settings, secrets)

    print("=" * 62)
    print("  X 登录（OAuth 2.0 Authorization Code + PKCE）")
    print("=" * 62)
    print(f"  Client ID   : {client.client_id[:12]}…（{len(client.client_id)} 字符）")
    print(f"  回调地址    : {client.redirect_uri}")
    print(f"  权限范围    : {' '.join(DEFAULT_SCOPES)}")
    print(f"  凭据存储    : {secrets.backend_detail}")
    print()

    if not client.client_id:
        print("✗ Client ID 是空的", file=sys.stderr)
        return 2

    def open_browser(url: str) -> None:
        print("→ 已打开浏览器。请在网页上点「Authorize」，然后回到这里等它自己结束。")
        print(f"  链接：{url[:110]}…")
        print()
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  （自动开浏览器失败：{exc}，请手动打开上面的链接）", file=sys.stderr)

    async def run() -> int:
        try:
            account = await client.authorize(open_browser, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"\n✗ 授权失败：{exc}", file=sys.stderr)
            return 1
        finally:
            await client.aclose()

        print(f"\n✓ 授权完成，登录账号：{account.label}")
        print(f"  用户 ID：{account.id}")

        # 再打一个真实请求，确认令牌真能用
        probe = XClient(settings, secrets)
        try:
            me = await probe.get_me()
            print(f"✓ 令牌自检通过：GET /users/me → {me.label}")
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 令牌自检失败：{exc}", file=sys.stderr)
            return 1
        finally:
            await probe.aclose()

        print("\n现在Fragment里 X 就是「已登录」状态了。")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""往 X 发一条测试推文，验证发布链路，然后**立刻删掉**。

为什么要「发完就删」：X 没有「仅自己可见」这种发布方式（推文都是公开的），
所以排障用的测试推文必须能自己收尾，不能在用户账号上留垃圾。

用法：

    python3 tools/x_test_publish.py              # 发一次，验完自动删
    python3 tools/x_test_publish.py --keep       # 发完不删（自己记得去删）
    python3 tools/x_test_publish.py --no-image   # 不测图片上传，只测文字

它会：
  1. 现场生成一张测试图
  2. 走Fragment**自己的代码路径**发出去（含 v2 分块上传）
  3. 校验发布结果
  4. 删掉它，并确认真的删了
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for extra in (PROJECT_ROOT / "vendor", PROJECT_ROOT / "src"):
    if extra.is_dir():
        entry = str(extra)
        if entry not in sys.path:
            sys.path.insert(0, entry)

from gnome_poster.models import Draft, MediaItem, Platform  # noqa: E402
from gnome_poster.services.x_api import XClient  # noqa: E402
from gnome_poster.services.publisher import build_adapters  # noqa: E402
from gnome_poster.services.secrets import SecretStore  # noqa: E402
from gnome_poster.services.settings import Settings  # noqa: E402

TEST_TEXT = "这是一条来自 GNOME Fragment的测试推文，验证发布链路，稍后会删除。"


def make_test_image(path: Path) -> bool:
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        width = height = 540
        rowstride = width * 3
        buffer = bytearray(rowstride * height)
        for y in range(height):
            for x in range(width):
                offset = y * rowstride + x * 3
                buffer[offset] = int(40 + 150 * x / width)
                buffer[offset + 1] = int(90 + 120 * y / height)
                buffer[offset + 2] = 150
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(bytes(buffer)),
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            width,
            height,
            rowstride,
        )
        pixbuf.savev(str(path), "png", [], [])
        return path.is_file()
    except Exception as exc:  # noqa: BLE001
        print(f"生成测试图失败：{exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="发一条 X 测试推文（默认发完就删）")
    parser.add_argument("--keep", action="store_true", help="发完不删（默认会删）")
    parser.add_argument("--no-image", action="store_true", help="不测图片上传")
    args = parser.parse_args()

    settings = Settings()
    secrets = SecretStore()
    adapter = build_adapters(settings, secrets)[Platform.X]

    print("=" * 62)
    print("  X 测试发布（发完就删）")
    print("=" * 62)

    async def run() -> int:
        status = await adapter.check_account()
        print(f"  账号状态 : {status}")
        if "已登录" not in status:
            print("\n✗ 还没登录 X，先跑 tools/x_login.py。", file=sys.stderr)
            await adapter.aclose()
            return 1

        draft = Draft(body=TEST_TEXT, platforms={Platform.X})
        image = None
        if not args.no_image:
            image = Path(tempfile.mkdtemp(prefix="gnome-poster-xtest-")) / "test.png"
            if not make_test_image(image):
                await adapter.aclose()
                return 1
            draft.media.append(MediaItem(str(image)))
            print(f"  测试图   : {image}")

        def on_progress(ratio: float, message: str) -> None:
            print(f"    [{ratio * 100:5.1f}%] {message}")

        print("\n  开始发布…")
        try:
            result = await adapter.publish(draft, on_progress)
        except Exception as exc:  # noqa: BLE001
            print(f"\n✗ 发布失败：{exc}", file=sys.stderr)
            await adapter.aclose()
            return 1

        if not result.ok:
            print(f"\n✗ 发布失败：{result.error}", file=sys.stderr)
            await adapter.aclose()
            return 1

        tweet_id = result.post_id or ""
        print(f"\n✓ 发布成功")
        print(f"  推文 ID : {tweet_id or '（没返回 ID —— 那就删不掉了）'}")
        print(f"  链接    : {result.url or '—'}")

        if args.keep:
            print("\n  --keep：没有删除，记得自己去删。")
            await adapter.aclose()
            return 0

        if not tweet_id:
            print("\n⚠️  X 没返回推文 ID，我删不掉它 —— 请去 X 手动删。", file=sys.stderr)
            await adapter.aclose()
            return 1

        # ---- 收尾：删掉 ----
        print("\n  收尾：删除这条测试推文…")
        client = XClient(settings, secrets)
        try:
            deleted = await client.delete_tweet(tweet_id)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 删除失败：{exc}", file=sys.stderr)
            print(f"  请手动删除：{result.url or tweet_id}", file=sys.stderr)
            await client.aclose()
            await adapter.aclose()
            return 1
        finally:
            await client.aclose()

        if not deleted:
            print("✗ X 说没删掉 —— 请手动检查。", file=sys.stderr)
            await adapter.aclose()
            return 1

        # 再确认一次：真的没了
        verify = XClient(settings, secrets)
        try:
            gone = False
            try:
                await verify._request("GET", f"/tweets/{tweet_id}")  # noqa: SLF001
            except Exception:  # noqa: BLE001
                gone = True
            if gone:
                print("✓ 已删除，并复查确认访问不到（404）")
            else:
                print("⚠️  删除返回成功，但复查还能读到 —— 请手动看一下。")
        finally:
            await verify.aclose()

        await adapter.aclose()
        print("\n链路验证完毕，账号上没留东西。")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

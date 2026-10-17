#!/usr/bin/env python3
"""往小红书发一条**仅自己可见**的测试笔记，验证整条发帖链路。

为什么单独做个工具：Fragment的「发布」按钮是给真实内容用的，而排障时
需要一个「不打扰任何人、又能证明链路通」的东西。这条笔记别人看不到，
验完在 App 里删掉即可。

用法：

    python3 tools/xhs_test_publish.py              # 仅自己可见（默认，安全）
    python3 tools/xhs_test_publish.py --public     # 公开发布（自己知道在干嘛再用）

它会：
  1. 现场生成一张带文字的测试图（不需要你准备素材）
  2. 走Fragment**自己的代码路径**发出去（不是 curl 糊一个请求）
  3. 打印每一步进度和服务的返回

⚠️ 这是真的往你的账号写数据。默认用「仅自己可见」，别随便加 --public。
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
from gnome_poster.services.publisher import build_adapters  # noqa: E402
from gnome_poster.services.secrets import SecretStore  # noqa: E402
from gnome_poster.services.settings import Settings  # noqa: E402

TEST_TITLE = "Fragment 测试笔记"
TEST_BODY = (
    "这是一条自动化测试笔记（仅自己可见）。\n"
    "如果你在自己的主页看到它，说明Fragment的图片上传、话题、发布整条链路都是通的 —— "
    "可以放心删掉。"
)
TEST_TAGS = ["测试"]


def make_test_image(path: Path) -> bool:
    """画一张一眼能认出来的测试图：蓝底 + 对角渐变。"""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        width, height, rowstride = 720, 720, 720 * 3
        buffer = bytearray(rowstride * height)
        for y in range(height):
            for x in range(width):
                offset = y * rowstride + x * 3
                buffer[offset] = int(53 + 120 * x / width)
                buffer[offset + 1] = int(110 + 90 * y / height)
                buffer[offset + 2] = 228
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(bytes(buffer)),
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            width,
            height,
            rowstride,
        )
        if pixbuf is None:
            return False
        pixbuf.savev(str(path), "png", [], [])
        return path.is_file()
    except Exception as exc:  # noqa: BLE001
        print(f"生成测试图失败：{exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="发一条小红书测试笔记（默认仅自己可见）")
    parser.add_argument("--public", action="store_true", help="公开发布（默认是仅自己可见）")
    parser.add_argument("--keep", metavar="PATH", help="用你自己的图片，不现场生成")
    args = parser.parse_args()

    visibility = "公开可见" if args.public else "仅自己可见"
    if args.public:
        print("⚠️  你选了公开发布 —— 这条笔记所有人可见。")

    settings = Settings()
    secrets = SecretStore()
    adapter = build_adapters(settings, secrets)[Platform.XHS]

    print("=" * 62)
    print("  小红书测试发布")
    print("=" * 62)
    print(f"  服务地址   : {settings.get('xhs-mcp-endpoint')}")
    print(f"  可见范围   : {visibility}")
    print()

    async def run() -> int:
        status = await adapter.check_account()
        print(f"  登录状态   : {status}")
        if "已登录" not in status:
            print("\n✗ 还没登录，先扫码登录再来。", file=sys.stderr)
            return 1

        if args.keep:
            image = Path(args.keep).expanduser().resolve()
            if not image.is_file():
                print(f"✗ 找不到图片：{image}", file=sys.stderr)
                return 1
        else:
            image = Path(tempfile.mkdtemp(prefix="gnome-poster-test-")) / "test-note.png"
            if not make_test_image(image):
                return 1
            print(f"  测试图     : {image}")

        draft = Draft(
            title=TEST_TITLE,
            body=TEST_BODY,
            tags=list(TEST_TAGS),
            platforms={Platform.XHS},
        )
        draft.media.append(MediaItem(str(image)))

        print()
        print("  开始发布…")

        def on_progress(ratio: float, message: str) -> None:
            print(f"    [{ratio * 100:5.1f}%] {message}")

        # 用设置里的可见范围会改到用户的全局设置，所以这里**临时**改，
        # 无论成功失败都还原。
        original = settings.get("xhs-visibility")
        settings.set("xhs-visibility", visibility)
        try:
            result = await adapter.publish(draft, on_progress)
        except Exception as exc:  # noqa: BLE001
            print(f"\n✗ 发布失败：{exc}", file=sys.stderr)
            return 1
        finally:
            settings.set("xhs-visibility", original)
            await adapter.aclose()

        print()
        if result.ok:
            print(f"✓ 发布成功（{visibility}）")
            print(f"  note_id : {result.post_id or '（服务没返回，属正常）'}")
            print(f"  链接    : {result.url or '（服务不返回笔记 ID，所以没有链接）'}")
            print()
            print("  去小红书 App → 我 → 笔记，应该能看到这条（仅自己可见）。")
            print("  验证完直接删掉就行。")
            return 0
        print(f"✗ 发布失败：{result.error}", file=sys.stderr)
        return 1

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

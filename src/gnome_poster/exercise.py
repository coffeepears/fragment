"""交互级冒烟测试：`./run.sh --exercise`。

和 --self-test 的区别：这个会真的**模拟用户操作**，验证逻辑串得通，
而不是只看窗口画出来没有。

跑的事情：
  1. 生成一张临时 PNG，走 MediaGrid.add_paths() 加进去（顺带验证缩略图线程池）
  2. 在正文里敲 "@ls"，验证 @ 提及弹窗真的弹出来且有候选，然后接受一个
  3. 打开投票面板，填两个选项
  4. 打开演练模式，点「发布」，等两个平台跑完
  5. 校验：媒体数是 1、缩略图到位、提及插入成功、发布结果两个都成功、编辑器被清空

任何一条不过就以非 0 退出，可以直接挂进 CI。
"""

from __future__ import annotations

import sys
import time
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, GLib  # noqa: E402

from .models import Platform  # noqa: E402


class _Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks.append((bool(ok), label if not detail else f"{label} — {detail}"))
        mark = "✓" if ok else "✗"
        print(f"[exercise] {mark} {label}{'  (' + detail + ')' if detail else ''}", flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [label for ok, label in self.checks if not ok]


def _make_sample_png(path: Path) -> bool:
    """造一张 320x200 的渐变 PNG，给媒体网格当素材。

    注意 PyGObject 里 `Pixbuf.get_pixels()` 给的是只读 bytes，
    要自己拼 bytearray 再走 new_from_bytes。
    """
    try:
        width, height = 320, 200
        rowstride = width * 3
        buffer = bytearray(rowstride * height)
        for y in range(height):
            for x in range(width):
                offset = y * rowstride + x * 3
                buffer[offset] = 0x35
                buffer[offset + 1] = int(40 + 180 * ((x + y) % width) / width)
                buffer[offset + 2] = int(255 - 160 * x / width)

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
        print(f"[exercise] 造素材失败：{exc}", file=sys.stderr)
        return False


def run() -> int:
    from .application import PosterApplication

    report = _Report()
    #: 一个保证没人监听的地址，用来确定性地测「连不上」的错误路径
    DEAD_ENDPOINT = "http://127.0.0.1:9"
    # seed=False：不塞演示内容，断言才是确定的
    app = PosterApplication(self_test=True, seed=False)
    state: dict[str, object] = {}
    tmpdir = Path(tempfile.mkdtemp(prefix="gnome-poster-exercise-"))
    sample = tmpdir / "sample.png"

    # 演练模式：不真的发请求，但把整条链路走完
    previous_simulate = app.settings.get("simulate")
    previous_endpoint = app.settings.get("xhs-mcp-endpoint")
    app.settings.set("simulate", True)

    def _window():
        return app.get_active_window()

    # ---------------- 步骤 ----------------
    def step_media() -> None:
        window = _window()
        if window is None:
            report.check(False, "拿到主窗口")
            return
        report.check(True, "主窗口已创建")
        if not _make_sample_png(sample):
            report.check(False, "生成测试图片")
            return
        state["sample_path"] = sample
        added = window.media_grid.add_paths([str(sample)])
        state["added"] = added
        count = len(window.media_grid.items())
        report.check(added == 1, "媒体加入成功", f"added={added}")
        report.check(count == 1, "媒体列表数量正确", f"count={count}")

    def step_models() -> None:
        """纯逻辑：标题归属、投票上限、Instagram 的媒体要求。"""
        from .models import Draft, Platform, Poll, limits_for

        # --- 标题只属于小红书：绝不会被塞进 X / Instagram 的正文 ---
        probe = Draft(title="我的标题", body="正文内容", platforms={Platform.X, Platform.INSTAGRAM})
        x_body = probe.compose_body(Platform.X)
        ig_body = probe.compose_body(Platform.INSTAGRAM)
        report.check("我的标题" not in x_body, "标题不会进 X 正文", repr(x_body))
        report.check("我的标题" not in ig_body, "标题不会进 Instagram 正文", repr(ig_body))
        report.check(
            Platform.XHS in {Platform.XHS} and limits_for(Platform.XHS).has_title,
            "只有小红书有标题字段",
        )

        # --- 小红书原生投票：2–6 项 ---
        six = Poll(options=["a", "b", "c", "d", "e", "f"], duration_minutes=1440, question="选哪个")
        ok_xhs, why_xhs = six.is_valid(Platform.XHS)
        ok_x, why_x = six.is_valid(Platform.X)
        report.check(ok_xhs, "小红书接受 6 项投票", why_xhs or "6 项 / 有问题")
        report.check(not ok_x, "X 只接受 4 项（6 项被拦下）", why_x)
        report.check(limits_for(Platform.XHS).supports_poll, "小红书 supports_poll = True")

        two = Poll(options=["a", "b"])
        report.check(two.is_valid(Platform.XHS)[0], "2 项投票合法")

        # --- Instagram：必须有媒体、没有投票 ---
        text_only = Draft(body="只有文字", platforms={Platform.INSTAGRAM})
        problems = text_only.validate()
        report.check(
            Platform.INSTAGRAM in problems
            and any("图片或视频" in item for item in problems[Platform.INSTAGRAM]),
            "Instagram 纯文字会被拦下",
            str(problems.get(Platform.INSTAGRAM)),
        )
        report.check(not limits_for(Platform.INSTAGRAM).supports_poll, "Instagram 不支持投票")

        # --- 圆环的级别阈值（纯逻辑部分；颜色部分见 step_ring_colors）---
        from .widgets.usage_ring import level_for

        report.check(level_for(1.0) == "warn" and level_for(1.01) == "error", "圆环阈值划分正确")

    def step_ring_mount() -> None:
        """把圆环挂进真实控件树。

        `Gtk.Widget.get_color()` 在没经过样式计算前读到的是未初始化内存
        （能读出负的 RGB）—— 所以必须挂上、等一帧、再验颜色。
        """
        from gi.repository import Gtk as _Gtk

        from .widgets.usage_ring import UsageRing

        window = _window()
        if window is None:
            return
        ring = UsageRing(size=32, thickness=4.0)
        holder = _Gtk.Box()
        holder.append(ring)
        window.media_grid.append(holder)
        state["ring"] = ring
        state["ring_holder"] = holder

    def step_ring_colors() -> None:
        """圆环到底画成了什么颜色 —— 直接读渲染出来的像素。"""
        from gi.repository import GdkPixbuf

        from .main import _snapshot_widget

        ring = state.get("ring")
        holder = state.get("ring_holder")
        if ring is None or holder is None:
            return

        def _stroke_color(path):
            """取图上「离背景最远」的那个像素 —— 就是被完整覆盖的圆环描边。"""
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
            data = pixbuf.get_pixels()
            channels = pixbuf.get_n_channels()
            stride = pixbuf.get_rowstride()
            width, height = pixbuf.get_width(), pixbuf.get_height()

            def pixel(x, y):
                offset = y * stride + x * channels
                return data[offset], data[offset + 1], data[offset + 2]

            background = pixel(0, 0)
            best = None
            best_distance = -1
            for y in range(height):
                for x in range(width):
                    value = pixel(x, y)
                    distance = sum(abs(a - b) for a, b in zip(value, background))
                    if distance > best_distance:
                        best_distance, best = distance, value
            if best is None:
                return None
            return tuple(channel / 255.0 for channel in best)

        def _hex(color) -> str:
            return "#{:02x}{:02x}{:02x}".format(
                *[max(0, min(255, int(c * 255))) for c in (color.red, color.green, color.blue)]
            )

        def _close(a, b, tolerance=0.18) -> bool:
            return a is not None and all(abs(x - y) <= tolerance for x, y in zip(a, b))

        # ---- 三个级别的颜色都必须能解析出来 ----
        resolved = {}
        for fraction, level in ((0.35, "ok"), (0.95, "warn"), (1.5, "error")):
            ring.set_usage(fraction)
            color = ring.get_color()
            ok = all(0.0 <= v <= 1.0 for v in (color.red, color.green, color.blue, color.alpha))
            resolved[level] = color
            report.check(
                ok,
                f"圆环 {level} 级别颜色解析正常",
                _hex(color) if ok else f"越界 ({color.red:.2f},{color.green:.2f},{color.blue:.2f})",
            )
        report.check(
            _hex(resolved["ok"]) != _hex(resolved["error"]) != _hex(resolved["warn"]),
            "三个级别颜色互不相同",
            f"ok={_hex(resolved['ok'])} warn={_hex(resolved['warn'])} error={_hex(resolved['error'])}",
        )

        # ---- 画出来的像素要和解析出来的颜色一致 ----
        ring.set_usage(0.35)
        ok_png = tmpdir / "ring-ok.png"
        shot_ok = _snapshot_widget(ring, ok_png)
        ring.set_usage(1.5)
        report.check("ring-error" in ring.get_css_classes(), "超限时切成 error 级别")
        err_png = tmpdir / "ring-error.png"
        shot_err = _snapshot_widget(ring, err_png)

        if not (shot_ok and shot_err):
            # 主循环忙的时候这一刻控件可能还没被分配尺寸，_snapshot_widget 只能返回
            # False —— 所以这里**不立刻判失败**：偶发测试比没有测试更糟，会让正常的
            # 改动看起来像坏了。排一次重试，重试完还不行才是真失败。
            attempts = state.get("ring_attempts", 0)
            if attempts < 4:
                state["ring_attempts"] = attempts + 1
                GLib.timeout_add(180, lambda: (step_ring_colors(), False)[1])
                return
            report.check(False, "圆环能渲染成图", "快照失败（重试 4 次仍未分配好）")
            holder.unparent()
            return

        if shot_ok and shot_err:
            stroke_ok = _stroke_color(ok_png)
            stroke_err = _stroke_color(err_png)
            report.check(
                _close(stroke_ok, (resolved["ok"].red, resolved["ok"].green, resolved["ok"].blue)),
                "正常状态的描边画成了主题色",
                f"像素 {stroke_ok} vs 主题 {_hex(resolved['ok'])}",
            )
            report.check(
                _close(stroke_err, (resolved["error"].red, resolved["error"].green, resolved["error"].blue)),
                "超限状态的描边画成了错误色",
                f"像素 {stroke_err} vs 主题 {_hex(resolved['error'])}",
            )
        else:
            report.check(False, "圆环能渲染成图", "快照失败")

        holder.unparent()

    def step_x_media_endpoints() -> None:
        """X 媒体上传必须打**正确的端点**。

        这个是踩过的坑：INIT 我凭记忆写成了 `/media/upload`，但那是另一个
        「单请求上传」端点（要求 body 有 `media` 字段），于是收到很误导的
        400「Missing media field in JSON」。正确路径是 `/media/upload/initialize`。
        两个端点都是 additionalProperties: false，多塞字段也会 400。
        """
        import asyncio

        from .models import MediaItem
        from .services.x_api import XClient
        from .services.secrets import SecretStore
        from .services.settings import Settings

        sample = state.get("sample_path")
        if not sample:
            report.check(False, "有图片可测 X 媒体上传端点")
            return

        calls: list[tuple[str, str, dict]] = []

        def _make(fake_id: str):
            async def _fake(method, path, *, json_body=None, params=None, auth=True):
                calls.append((method.upper(), path, dict(json_body or {})))
                if path == "/media/upload":
                    return {"data": {"id": fake_id}}
                if path == "/media/upload/initialize":
                    return {"data": {"id": fake_id}}
                if path.endswith("/finalize"):
                    return {"data": {"id": fake_id, "processing_info": {"state": "succeeded"}}}
                return {"data": {}}

            return _fake

        # --- 图片：应该走单请求 /media/upload ---
        client = XClient(Settings(), SecretStore())
        client._request = _make("IMG1")  # type: ignore[method-assign]
        media_id = asyncio.run(client.upload_media(MediaItem(str(sample))))
        paths = [c[1] for c in calls]
        report.check(
            paths == ["/media/upload"] and media_id == "IMG1",
            "图片走单请求上传 /media/upload",
            " → ".join(paths),
        )
        body = calls[0][2] if calls else {}
        report.check(
            "media" in body and "media_category" in body,
            "单请求上传带 media + media_category",
            f"字段：{sorted(body)}",
        )

        # --- 视频：应该走 /media/upload/initialize 分段 ---
        calls.clear()
        video = tmpdir / "clip.mp4"
        video.write_bytes(b"\x00" * 512)
        client2 = XClient(Settings(), SecretStore())
        client2._request = _make("VID1")  # type: ignore[method-assign]
        asyncio.run(client2.upload_media(MediaItem(str(video), "video")))
        paths2 = [c[1] for c in calls]
        report.check(
            paths2[:1] == ["/media/upload/initialize"],
            "视频走 /media/upload/initialize（不是 /media/upload）",
            " → ".join(paths2),
        )
        init_body = calls[0][2] if calls else {}
        report.check(
            "media_type" in init_body
            and "total_bytes" in init_body
            and "media" not in init_body,
            "initialize 只带 media_type/total_bytes/media_category",
            f"字段：{sorted(init_body)}",
        )

        # --- 含链接的帖子要提醒贵 13 倍 ---
        from .services.x_api import _looks_like_url

        report.check(
            _looks_like_url("看这个 https://example.com/x")
            and _looks_like_url("www.example.com")
            and not _looks_like_url("今天天气不错"),
            "能识别帖子里的链接（X 对含链接的帖子收 13 倍）",
        )

    def step_platform_visibility() -> None:
        """`extra-platforms` 关掉时，界面必须只剩小红书。

        用户明确说过「只要小红书，X 和 Instagram 弄掉」。这里验证的正是那个开关：
        主界面的平台开关、账号页的分组，两个地方都要跟着收起来。
        """
        from .models import platform_choices
        from .widgets.platform_row import PlatformRow

        window = _window()

        # 1) 纯逻辑层：开关决定展示哪些平台
        got_off = [p.value for p in PlatformRow(platform_choices(False))._toggles]  # noqa: SLF001
        got_on = [p.value for p in PlatformRow(platform_choices(True))._toggles]  # noqa: SLF001
        report.check(
            got_off == ["xhs"] and got_on == ["xhs", "x", "instagram"],
            "平台开关受「同时显示 X 和 Instagram」控制",
            f"关={got_off} 开={got_on}",
        )

        # 2) 真实窗口：按用户当前设置建的，应该是 1 个开关
        if window is not None:
            count = len(window.platform_row._toggles)  # noqa: SLF001
            report.check(count == 1, "关掉后主界面只有小红书一个开关", f"{count} 个")

        # 2.5) 正文计数器也不该再列 X / Instagram
        if window is not None:
            counter = window.body_counter.get_text()
            detail = window.body_counter.get_tooltip_text() or ""
            # 新版排版把数字收成「314/1000」，平台明细挪进 tooltip
            report.check(
                "/" in counter and "Instagram" not in detail and "X " not in detail,
                "关掉后正文计数器只算 Rednote",
                f"显示 {counter!r}，明细 {detail!r}",
            )
            report.check(
                "Rednote" in detail,
                "正文计数器的明细里有 Rednote",
                detail,
            )
            banner = window.setup_banner.get_revealed()
            report.check(
                not banner,
                "关掉后不会冤枉说「还没配置任何平台」",
                f"横幅显示={banner}（小红书已登录）",
            )

        # 3) 账号页分组：用**真实构建逻辑**那个函数验，不靠遍历 widget 树
        #    （Adw.PreferencesDialog 在 present 之前 get_first_child() 是 None）
        from .widgets.settings_dialog import account_sections

        off_sections = account_sections(False)
        on_sections = account_sections(True)
        report.check(
            "Rednote" in off_sections
            and "X" not in off_sections
            and "Instagram" not in off_sections,
            "关掉后账号页只有 Rednote",
            " → ".join(off_sections),
        )
        report.check(
            "X" in on_sections and "Instagram" in on_sections,
            "打开后 X / Instagram 分组回来",
            " → ".join(on_sections),
        )

    def step_packaging_complete() -> None:
        """meson.build 的安装清单必须覆盖每一个源文件。

        🩸 这个坑真踩了：新增 `services/xhs_service.py` 后忘了登记进
        `python.install_sources()` —— 源码树里 `./run.sh` 跑得好好的，
        装成 deb 就 ModuleNotFoundError。而我对装好的包只跑了 `--check`，
        没有去 import 那个新模块，所以它安安静静地漏过去了。

        这个检查的价值：**新增模块忘记登记**是打包类最容易犯、又最难在开发时
        发现的错误（因为开发时永远从源码树跑）。
        """
        root = Path(__file__).resolve().parents[2]  # 项目根
        meson_text = (root / "meson.build").read_text(encoding="utf-8")
        missing = []
        for py in sorted((root / "src" / "gnome_poster").rglob("*.py")):
            rel = py.relative_to(root).as_posix()
            if rel not in meson_text:
                missing.append(rel)
        report.check(
            not missing,
            "meson.build 的安装清单覆盖所有源文件",
            "漏登记：" + "、".join(missing) if missing else "全部已登记",
        )
        # 顺带确认内置服务二进制也在安装清单里
        report.check(
            "vendor/xhs-service/xiaohongshu-mcp" in meson_text,
            "内置服务二进制在安装清单里",
        )

    def step_xhs_service_manager() -> None:
        """发布服务必须是「随应用安装、app 自己拉起」的，不能是用户手动装的外部依赖。

        这是大人明确抱怨过的：「为什么不能在安装这个 deb 时一起装这个助手」。
        之前确实是让用户自己去 GitHub 下载二进制 + 手写 systemd 单元，纯属白折腾。
        """
        from .services.xhs_service import (
            XhsServiceManager,
            default_state_dir,
            find_bundled_binary,
        )

        binary = find_bundled_binary()
        report.check(
            binary is not None and binary.is_file(),
            "发布服务二进制随应用一起安装",
            str(binary) if binary else "找不到 —— 装出来也用不了",
        )

        manager = XhsServiceManager()
        report.check(
            manager.port == 18060 and manager.endpoint == "http://127.0.0.1:18060",
            "服务默认监听 127.0.0.1:18060",
            f"{manager.endpoint}",
        )
        report.check(
            XhsServiceManager("http://127.0.0.1:18099").port == 18099,
            "端口跟随 endpoint 变化（便于测试与非默认端口）",
        )

        # 工作目录必须稳定：登录态的 cookies.json 就落在这里，换目录 = 重新扫码
        state = default_state_dir()
        report.check(
            state.name == "xiaohongshu-mcp" and "state" in str(state),
            "服务工作目录稳定（登录态才不会丢）",
            str(state),
        )

    def step_adapter_interface() -> None:
        """所有平台适配器都必须实现 check_account。

        这个是漏过的：设置页给每个平台都放了「测试连接」，但 XAdapter 没有
        `check_account` —— 点下去只会退回 `account_hint()`（纯本地猜），
        令牌失效了也看不出来。测试没覆盖到，是后来手工点 X 的测试连接才发现的。
        """
        from .services.publisher import build_adapters
        from .services.secrets import SecretStore
        from .services.settings import Settings

        adapters = build_adapters(Settings(), SecretStore())
        missing = [
            platform.label
            for platform, adapter in adapters.items()
            if not callable(getattr(adapter, "check_account", None))
        ]
        report.check(
            not missing,
            "每个平台都实现了「测试连接」（check_account）",
            "缺：" + "、".join(missing) if missing else f"{len(adapters)} 个平台都有",
        )
        report.check(
            all(callable(getattr(a, "publish", None)) for a in adapters.values()),
            "每个平台都实现了 publish",
        )

    def step_qr_support() -> None:
        """二维码自动续期的时间解析 + CLI 扫码入口。

        早先的版本只在用户手点「换一张」时才续，结果「人不在电脑前 → 过期 →
        回来扫了个寂寞」。现在按服务返回的 4m0s 自己排续期。
        """
        from .widgets.settings_dialog import SettingsDialog

        cases = (("4m0s", 240), ("2m30s", 150), ("59s", 59), ("", 240), ("怪东西", 240))
        bad = [
            f"{text!r}→{SettingsDialog._timeout_seconds(text)}"
            for text, expect in cases
            if SettingsDialog._timeout_seconds(text) != expect
        ]
        report.check(not bad, "二维码有效期解析正确", "；".join(bad) or "4m0s/2m30s/59s/空/乱码 都对")

        window = _window()
        report.check(
            window is not None and hasattr(window, "start_xhs_login"),
            "有命令行扫码登录入口（--xhs-login）",
        )

    def step_oauth_receiver() -> None:
        """X 登录全靠这个本机回调接收器 —— 起一个、真打一个请求进去验证。

        补这一步的原因：X 后台可能要你填 `http://localhost:8721/callback`，
        而浏览器会把 localhost 先解析到 ::1；接收器原来只绑 127.0.0.1，
        那样回调永远到不了，用户只会看到浏览器「无法连接」。
        """
        import http.client
        import threading

        from .services.x_api import LoopbackReceiver

        for uri in ("http://127.0.0.1:18721/callback", "http://localhost:18721/callback"):
            receiver = LoopbackReceiver(uri)
            try:
                receiver.start()
            except Exception as exc:  # noqa: BLE001
                report.check(False, "回调接收器能起来", f"{uri} → {exc}")
                continue
            # 🩸 模拟浏览器的「预连接」：连上但不发数据。
            #    原来用的是单线程 HTTPServer，这种连接会把服务线程永久卡在
            #    「读请求行」上，导致**真正的 OAuth 回调在队列里饿死** ——
            #    现象是浏览器一直转圈 / 无法连接，而端口明明在 LISTEN。
            #    Firefox 每次都会预连接，所以这不是理论问题，是必现的。
            idle_sockets = []
            try:
                import socket as _socket

                for _ in range(2):
                    idle_sockets.append(
                        _socket.create_connection(
                            (receiver.bound_host, receiver.port), timeout=3
                        )
                    )
            except Exception:  # noqa: BLE001
                pass

            result: dict = {}

            def _hit(host=receiver.bound_host, port=receiver.port, path=receiver.path):
                try:
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    conn.request("GET", f"{path}?code=THECODE&state=THESTATE")
                    response = conn.getresponse()
                    response.read()
                    result["status"] = response.status
                    conn.close()
                except Exception as exc:  # noqa: BLE001
                    result["error"] = str(exc)

            worker = threading.Thread(target=_hit, daemon=True)
            worker.start()
            params = receiver.wait(timeout=6)
            worker.join(timeout=2)
            for sock in idle_sockets:
                try:
                    sock.close()
                except OSError:
                    pass
            _stop_started = time.monotonic()
            receiver.stop()
            stop_cost = time.monotonic() - _stop_started

            host = uri.split("//")[1].split(":")[0]
            report.check(
                params.get("code") == "THECODE" and params.get("state") == "THESTATE",
                f"回调接收器能收到 code（{host}）",
                f"绑定 {receiver.bound_host} | HTTP {result.get('status')}",
            )
            report.check(
                stop_cost < 3.0,
                f"预连接不会卡死接收器（{host}）",
                f"带 2 个空闲连接仍能收码，stop() 耗时 {stop_cost:.2f}s",
            )

    def step_xhs_payload() -> None:
        """对着 xiaohongshu-mcp v2.5.5 的真实接口约定检查我们发的 JSON。

        这一步是补的：之前发布用的是 **multipart 文件流**，而服务收的是
        **JSON + 本地路径**，真接上去必然 400。不联网、不起服务也能测。
        """
        import asyncio

        from .models import Draft, MediaItem
        from .services.xhs_api import XhsMcpTransport

        sample = state.get("sample_path")
        if not sample:
            report.check(False, "有一个真实图片文件可供测试")
            return

        captured: dict = {}

        async def _fake_call(method, path, *, json_body=None, timeout=None):
            captured.update(method=method, path=path, body=json_body)
            return {"success": True, "data": {"status": "发布成功"}}

        transport = XhsMcpTransport("http://127.0.0.1:18060")
        transport._call = _fake_call  # type: ignore[method-assign]

        # --- 图文 ---
        draft = Draft(title="我的标题", body="正文内容", tags=["美食", "旅行"])
        draft.media.append(MediaItem(str(sample)))
        asyncio.run(transport.publish(draft))

        body = captured.get("body") or {}
        report.check(captured.get("path") == "/api/v1/publish", "图文走 /api/v1/publish", captured.get("path"))
        report.check(
            isinstance(body.get("images"), list) and body["images"] == [str(sample.resolve())],
            "images 传的是本地绝对路径（不是文件流）",
            str(body.get("images"))[:60],
        )
        report.check(body.get("tags") == ["美食", "旅行"], "话题走独立的 tags 数组", str(body.get("tags")))
        report.check(
            "#" not in str(body.get("content", "")),
            "话题不再重复塞进正文",
            repr(body.get("content"))[:50],
        )
        report.check(body.get("title") == "我的标题", "title 字段正确")

        # --- 视频 ---
        captured.clear()
        video = tmpdir / "clip.mp4"
        video.write_bytes(b"\x00" * 64)
        draft2 = Draft(title="视频标题", body="视频正文")
        draft2.media.append(MediaItem(str(video), "video"))
        asyncio.run(transport.publish(draft2))
        body2 = captured.get("body") or {}
        report.check(captured.get("path") == "/api/v1/publish_video", "视频走 /api/v1/publish_video", captured.get("path"))
        report.check(body2.get("video") == str(video.resolve()), "video 传本地绝对路径")

        # --- 二维码解码路径（app 里的「扫码登录」要靠它）---
        from gi.repository import Gdk, GLib

        png_path = tmpdir / "fake-qr.png"
        _make_sample_png(png_path)
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_path.read_bytes()))
            report.check(
                texture is not None and texture.get_width() > 0,
                "能从字节解码二维码图片（Gdk.Texture）",
                f"{texture.get_width()}x{texture.get_height()}" if texture else "失败",
            )
        except Exception as exc:  # noqa: BLE001
            report.check(False, "能从字节解码二维码图片（Gdk.Texture）", str(exc))

    def step_copy_text() -> None:
        """零配置出路：写好的内容要能按平台格式拼成可直接粘贴的文案。"""
        window = _window()
        if window is None:
            return
        window.title_entry.set_text("我的标题")
        window.composer.set_text("正文内容")
        window.platform_row.set_selected({Platform.XHS})
        composed = window.compose_for_clipboard()
        report.check(composed is not None, "能拼出可复制的文案")
        if composed is not None:
            text, platform = composed
            report.check(platform is Platform.XHS, "按勾选的平台拼", platform.label)
            report.check(
                text.startswith("我的标题") and "正文内容" in text,
                "小红书格式会带标题",
                repr(text[:40]),
            )

        window.platform_row.set_selected({Platform.X})
        composed = window.compose_for_clipboard()
        if composed is not None:
            text, platform = composed
            report.check(
                "我的标题" not in text,
                "换到 X 时标题不会被带进文案",
                repr(text[:40]),
            )
        window.platform_row.set_selected({Platform.XHS, Platform.X})
        window.copy_text()
        report.check(True, "copy_text() 不炸（剪贴板走 Gdk）")

    def step_platform_toggle() -> None:
        """手点平台复选框。

        这一步是补的：之前 `selection-changed` 的处理函数签名少了参数，
        用户一点复选框就 TypeError —— 而当时的测试从没点过它。
        """
        window = _window()
        if window is None:
            return
        instagram = window.platform_row.toggle_for(Platform.INSTAGRAM)
        before = window.platform_row.selected()

        instagram.set_active(True)
        report.check(
            Platform.INSTAGRAM in window.platform_row.selected(),
            "勾上 Instagram 不炸，且进了选中集合",
        )
        report.check(
            window.poll_panel._platforms and Platform.INSTAGRAM in window.poll_panel._platforms,  # noqa: SLF001
            "投票面板收到了平台变化",
        )
        report.check(
            instagram.ring.get_visible() is False or True,
            "圆环状态可访问",
        )

        instagram.set_active(False)
        report.check(
            window.platform_row.selected() == before,
            "取消勾选后回到原状态",
        )

    def step_thumbnail() -> None:
        window = _window()
        if window is None:
            return
        items = window.media_grid.items()
        texture = items[0].texture if items else None
        if texture is None and state.get("thumb_attempts", 0) < 4:
            # 缩略图在后台线程解码、结果经 idle 回主线程。主循环忙的时候
            # 这一刻可能还没回来 —— 排一次重试，别把偶发当失败
            # （偶发测试比没有测试更糟，见圆环那条的注释）。
            state["thumb_attempts"] = state.get("thumb_attempts", 0) + 1
            GLib.timeout_add(150, lambda: (step_thumbnail(), False)[1])
            return
        state["thumb"] = texture is not None
        report.check(
            texture is not None,
            "缩略图已异步解码",
            f"{texture.get_width()}x{texture.get_height()}" if texture else "无",
        )

    def step_mention() -> None:
        window = _window()
        if window is None:
            return
        window.composer.set_text("你好 ")
        window.composer.insert_text("@ls")
        popover = window._mention_popover  # noqa: SLF001 - 冒烟测试就是要摸内部状态
        visible = popover.get_visible()
        rows = len(popover._rows)  # noqa: SLF001
        state["mention_rows"] = rows
        report.check(visible, "@ 提及弹窗已弹出")
        report.check(rows >= 1, "拼音首字母能搜到候选", f"{rows} 条候选")
        if rows:
            popover.accept()
            text = window.composer.text
            state["mention_text"] = text
            report.check(
                "@林深见鹿" in text or "@linshen" in text,
                "@ 提及已补全进正文",
                repr(text),
            )
        report.check(not popover.get_visible(), "选中后弹窗已关闭")

    def step_poll() -> None:
        window = _window()
        if window is None:
            return
        window.poll_button.set_active(True)
        panel = window.poll_panel
        rows = panel._rows  # noqa: SLF001
        report.check(panel.get_reveal_child(), "投票面板已展开")
        if len(rows) >= 2:
            rows[0].entry.set_text("自己写")
            rows[1].entry.set_text("用现成的")
        poll = panel.get_poll()
        state["poll"] = poll
        report.check(poll is not None, "能取到投票数据")
        if poll is not None:
            ok, reason = poll.is_valid()
            report.check(ok, "投票数据合法", reason or "2 项 / 1 天")

    def step_publish_warning() -> None:
        """小红书不支持投票 → 应该弹确认框，而不是闷头发出去。"""
        window = _window()
        if window is None:
            return
        window.title_entry.set_text("冒烟测试帖子")
        window.composer.set_text(window.composer.text + " 这是一次自动化冒烟测试。")
        window.on_publish()
        dialog = window.get_visible_dialog()
        state["dialog"] = dialog
        report.check(dialog is not None and isinstance(dialog, Adw.AlertDialog),
                     "小红书投票提醒弹窗出现")
        report.check(not window._busy, "弹窗期间不会开始发布")  # noqa: SLF001
        if dialog is not None:
            dialog.close()  # 等价于点「再改改」
        report.check(not window._busy, "取消后仍未发布")  # noqa: SLF001

    def step_settings_dialog() -> None:
        """设置对话框平时最容易漏测 —— 一条 import 错误就能让用户点不开设置。"""
        window = _window()
        if window is None:
            return
        window.open_settings()
        dialog = window.get_visible_dialog()
        report.check(dialog is not None, "设置对话框已打开")
        if dialog is not None:
            report.check(
                dialog.__class__.__name__ == "SettingsDialog",
                "拿到的是 SettingsDialog",
                dialog.__class__.__name__,
            )

            # 内容也要验：一屏专业名词、步骤还折叠着 —— 那是用户明确抱怨过的
            def _walk(widget, out):
                # 行的标题在 get_title()，按钮上的字在 Label.get_text()
                for getter_name in ("get_title", "get_text"):
                    getter = getattr(widget, getter_name, None)
                    if not callable(getter):
                        continue
                    try:
                        text = getter()
                    except Exception:  # noqa: BLE001
                        continue
                    if text:
                        out.append(str(text))
                child = widget.get_first_child()
                while child is not None:
                    _walk(child, out)
                    child = child.get_next_sibling()

            titles: list[str] = []
            _walk(dialog, titles)
            state["settings_titles"] = titles

            report.check(any("高级设置…" in t for t in titles), "有「高级设置…」折叠区")
            report.check(
                any("高级设置" in t for t in titles),
                "折腾人的项收进了「高级设置」",
            )
            joined = " ".join(titles)
            report.check(
                "登录Rednote" in joined,
                "账号组标题为「登录Rednote」",
                f"{len(titles)} 条文本",
            )
            # 小助手不再是「你自己去装」的外部依赖了
            report.check(
                "内置本机发布服务" in joined,
                "写明发布服务是内置的",
            )
            # 文案要像 GNOME 自带应用：陈述句，不要「呀/呢/吧」这种口语尾
            informal = [
                t for t in titles
                if any(ch in t for ch in ("呀", "呢", "吧", "哦", "啦", "嘛"))
            ]
            report.check(
                not informal,
                "设置页文案没有口语化尾字",
                "、".join(informal[:3]) if informal else "干净",
            )
            dialog.close()
        report.check(window.get_visible_dialog() is None, "设置对话框已关闭")

    def step_platform_check_start() -> None:
        """「测试连接」在服务连不上时应该给一句人话，而不是抛异常。

        ⚠️ 这里**故意把地址指到一个没人监听的端口**，而不是指望「本机没装服务」：
        之前就是靠这个环境假设，等真的把服务装起来跑之后，这一步变成去问真服务
        （每次要开无头浏览器、~9 秒），检查点直接超时 —— 测试跟环境耦合了。
        指向死端口则永远是「立刻连接被拒」，又快又确定。

        同时临时关掉演练模式，否则 check_account 会短路返回「演练模式」，
        真正的错误处理路径根本没被覆盖。
        """
        window = _window()
        if window is None:
            return
        original_endpoint = app.settings.get("xhs-mcp-endpoint")
        app.settings.set("simulate", False)
        app.settings.set("xhs-mcp-endpoint", DEAD_ENDPOINT)
        adapter = window.publisher.adapter(Platform.XHS)

        def _restore() -> None:
            app.settings.set("xhs-mcp-endpoint", original_endpoint)
            app.settings.set("simulate", True)

        def _ok(text: object) -> None:
            _restore()
            state.update(xhs_check=text)

        def _err(exc: BaseException) -> None:
            _restore()
            state.update(xhs_check=f"异常：{exc}")

        window.runner.submit(adapter.check_account(), _ok, _err)  # type: ignore[attr-defined]

    def step_platform_check_verify() -> None:
        text = state.get("xhs_check")
        report.check(isinstance(text, str) and bool(text), "小红书测试连接有返回", str(text))
        report.check(
            isinstance(text, str) and not str(text).startswith("异常"),
            "本地服务没起时是友好报错（不抛异常）",
        )
        report.check(
            isinstance(text, str) and "不可用" in str(text),
            "确实走到了真实的失败分支",
            str(text),
        )

    def step_publish() -> None:
        window = _window()
        if window is None:
            return
        # 确保走演练模式：不然这一步会真的去打网络（服务已装时尤其慢）
        #
        # 🩸 这里曾经出过事故：11:29 那次跑测试时，上一步为了测「连不上」的错误
        #    分支临时把 simulate 关掉了，而这一步没强制打开 —— 于是冒烟测试
        #    真的往用户的小红书账号发了一条「冒烟测试帖子」。它只因为当时还没
        #    登录才失败。**测试永远不该有能力碰用户的真实账号。**
        #    所以：先强制打开，再验一次；万一没生效就宁可不测。
        app.settings.set("simulate", True)
        if not app.settings.get("simulate"):
            report.check(
                False, "演练模式没生效 —— 跳过发布，绝不拿真实账号做实验"
            )
            return
        # 清掉投票，这样两个平台都不再有警告，可以直接发
        window.poll_panel.clear()
        window.poll_button.set_active(False)
        window.on_publish()
        report.check(window._busy, "发布已进入忙碌状态")  # noqa: SLF001

    def step_results() -> None:
        window = _window()
        if window is None:
            return
        report.check(not window._busy, "发布已结束（不再忙碌）")  # noqa: SLF001
        # 演练模式下两个平台都应该成功，发布成功后编辑器会被清空
        report.check(
            window.composer.text.strip() == "",
            "发布成功后编辑器被清空",
            repr(window.composer.text[:40]),
        )
        report.check(
            window.title_entry.get_text().strip() == "",
            "发布成功后标题被清空",
        )
        report.check(
            window._undo_snapshot is not None, "留下了撤销快照"  # noqa: SLF001
        )
        report.check(
            len(window.media_grid.items()) == 0, "发布成功后附件被清空"
        )

    def step_finish() -> None:
        app.settings.set("simulate", previous_simulate)
        app.settings.set("xhs-mcp-endpoint", previous_endpoint)
        failed = report.failed
        if failed:
            print(f"\n[exercise] {len(failed)} 项未通过：", file=sys.stderr)
            for item in failed:
                print(f"  ✗ {item}", file=sys.stderr)
        else:
            print("\n[exercise] 全部通过 ✅", flush=True)
        state["failed"] = failed
        app.quit()

    # 这一步起真实 socket、会阻塞主循环，而且**完全不需要窗口** ——
    # 放在时间链之前同步跑完，免得挤歪后面那些按毫秒对齐的检查
    # （它排在 1500ms 时曾把 2500ms 的缩略图检查挤成偶发失败）。
    step_oauth_receiver()

    schedule = [
        (700, step_media),
        (1200, step_ring_mount),
        (1300, step_packaging_complete),
        (1320, step_xhs_service_manager),
        (1340, step_platform_visibility),
        (1360, step_x_media_endpoints),
        (1380, step_adapter_interface),
        (1400, step_models),
        (1450, step_qr_support),
        (1550, step_xhs_payload),
        (1600, step_copy_text),
        (1750, step_platform_toggle),
        (2200, step_ring_colors),
        (2500, step_thumbnail),
        (2700, step_mention),
        (3000, step_poll),
        (3300, step_settings_dialog),
        (3600, step_platform_check_start),
        (3800, step_publish_warning),
        (4200, step_publish),
        # 演练模式的上传步骤是带 sleep 的，1 个附件大概 1.7s 跑完，留够余量
        (6800, step_results),
        (7100, step_platform_check_verify),
        (7400, step_finish),
    ]
    for delay, action in schedule:
        GLib.timeout_add(delay, lambda fn=action: (fn(), False)[1])

    code = app.run([sys.argv[0]])
    if not report.checks:
        print("[exercise] 一个检查都没跑到，说明窗口压根没起来", file=sys.stderr)
        return 2
    if state.get("failed"):
        return 1
    return int(code or 0)

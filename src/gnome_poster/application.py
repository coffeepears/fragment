"""GtkApplication / Adw.Application 的装配处。

进程级的对象都在这儿创建，然后注入窗口：
  Settings / SecretStore / AsyncRunner / Publisher / ContactBook / ThumbnailLoader / DraftStore

窗口关闭 ≠ 应用退出（可以再开一个窗口），所以清理放在 do_shutdown。
"""

from __future__ import annotations

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .models import Poll  # noqa: E402
from .services.contacts import ContactBook  # noqa: E402
from .services.draftstore import DraftStore  # noqa: E402
from .services.publisher import Publisher  # noqa: E402
from .services.secrets import SecretStore  # noqa: E402
from .services.settings import Settings  # noqa: E402
from .utils.asyncio_bridge import AsyncRunner  # noqa: E402
from .utils.paths import data_file, icon_search_paths  # noqa: E402
from .utils.thumbnails import ThumbnailLoader  # noqa: E402
from .window import PosterWindow  # noqa: E402

APP_ID = "com.pear.GnomePoster"
VERSION = "0.4.5"


class PosterApplication(Adw.Application):
    __gtype_name__ = "GPApplication"

    def __init__(
        self,
        self_test: bool = False,
        forced_scheme: str | None = None,
        seed: bool | None = None,
        open_xhs_login: bool = False,
    ) -> None:
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS | Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if self_test:
            # ⚠️ 必需：不然用户正开着应用时，--self-test / --exercise 会被 GApplication
            # 当成「第二个实例」，把激活转给已有进程然后自己退出 —— 测试什么都不做
            # 却报告「通过」。真发生过（而且自检还骗了我一次）。
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.version = VERSION
        self.self_test = self_test
        #: 只在 --self-test 用：启动时就定死浅色/深色，避免拍到主题过渡动画
        self.forced_scheme = forced_scheme
        #: 要不要塞演示内容。--exercise 要跑确定性断言，所以显式传 seed=False
        self.seed = self_test if seed is None else seed
        #: --xhs-login：启动后直接弹小红书扫码登录
        self.open_xhs_login = open_xhs_login

        self.settings = Settings()
        self.secrets = SecretStore()
        self.runner = AsyncRunner()
        self.publisher = Publisher(self.settings, self.secrets)
        self.book = ContactBook()
        self.loader = ThumbnailLoader(workers=2)
        self.draft_store = DraftStore()
        self._window: PosterWindow | None = None

    # ------------------------------------------------------------------ #
    def do_startup(self) -> None:  # noqa: N802 - GObject 虚函数命名
        Adw.Application.do_startup(self)

        self.settings.apply_color_scheme()
        if self.forced_scheme == "light":
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif self.forced_scheme == "dark":
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self._load_css()
        self._load_icons()
        self._install_app_actions()
        self._init_multimedia()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:  # noqa: N802
        # 大部分参数由 main.py 处理过了；这里处理「已经有实例在跑」的情况：
        # 单实例的 GApplication 会把这个请求转给已有进程，所以 --xhs-login
        # 必须在这里再判断一次，否则第二次执行只会把窗口顶到前面。
        args = list(command_line.get_arguments() or [])
        if "--xhs-login" in args and self._window is not None:
            self._window.present()
            self._window.start_xhs_login()
            return 0
        self.activate()
        return 0

    def do_activate(self) -> None:  # noqa: N802
        if self._window is None:
            self._window = PosterWindow(
                application=self,
                settings=self.settings,
                secrets_store=self.secrets,
                runner=self.runner,
                publisher=self.publisher,
                contact_book=self.book,
                thumbnail_loader=self.loader,
                draft_store=self.draft_store,
                restore_draft=not self.self_test,
            )
            if self.seed:
                self._seed_self_test(self._window)
        self._window.present()
        if self.open_xhs_login:
            # 等窗口真的画出来再弹对话框，不然会闪一下
            GLib.timeout_add(600, lambda: (self._window.start_xhs_login(), False)[1])

    def do_shutdown(self) -> None:  # noqa: N802
        # 先礼貌地关掉 HTTP 连接（给 2 秒），再停事件循环
        try:
            future = self.runner.submit(self.publisher.aclose())
            future.result(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        self.runner.shutdown()
        self.loader.shutdown()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------ #
    # 启动期准备
    # ------------------------------------------------------------------ #
    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        css = data_file("style.css")
        if css is None:
            print("[gnome-poster] 找不到 style.css，将使用 Adwaita 默认样式", file=sys.stderr)
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(css))
        except GLib.Error as exc:  # CSS 写错了不该让应用起不来
            print(f"[gnome-poster] 样式表加载失败 {css}: {exc}", file=sys.stderr)
            return
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._css_path = css

    def _load_icons(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        theme = Gtk.IconTheme.get_for_display(display)
        for path in icon_search_paths():
            theme.add_search_path(str(path))

    def _install_app_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", lambda *_: self._show_about())
        self.add_action(about_action)

        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect(
            "activate", lambda *_: self._window.open_settings() if self._window else None
        )
        self.add_action(settings_action)

        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _init_multimedia(self) -> None:
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            if not Gst.is_initialized():
                Gst.init(None)
        except (ValueError, ImportError):
            # 没有 GStreamer 也能用，只是视频不给缩略图
            pass

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Fragment",
            application_icon=APP_ID,
            developer_name="pear",
            version=self.version,
            # 不写括号说明：没有「行为」分页了，X/Instagram 开关也已从设置里删掉
            comments="一次撰写，发布到Rednote。",
            license_type=Gtk.License.GPL_3_0,
        )
        if self._window is not None:
            about.present(self._window)
        else:
            about.present(None)

    # ------------------------------------------------------------------ #
    # 自检模式：塞点示例内容，方便截图 / 冒烟测试
    # ------------------------------------------------------------------ #
    def _sample_images(self, count: int = 2) -> list[str]:
        """造几张渐变色 PNG 当附件（顺带把缩略图流水线跑一遍）。"""
        try:
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            out: list[str] = []
            target = Path(GLib.get_tmp_dir()) / "gnome-poster-samples"
            target.mkdir(exist_ok=True)
            palettes = ((0x35, 0x84, 0xE4), (0xE0, 0x6C, 0x2B), (0x2E, 0xC2, 0x7E))
            for index in range(count):
                width, height = 480, 320
                rowstride = width * 3
                buffer = bytearray(rowstride * height)
                base = palettes[index % len(palettes)]
                for y in range(height):
                    for x in range(width):
                        offset = y * rowstride + x * 3
                        shade = 0.55 + 0.45 * ((x / width) * 0.6 + (y / height) * 0.4)
                        buffer[offset] = int(base[0] * shade)
                        buffer[offset + 1] = int(base[1] * shade)
                        buffer[offset + 2] = int(base[2] * shade)
                pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                    GLib.Bytes.new(bytes(buffer)),
                    GdkPixbuf.Colorspace.RGB,
                    False,
                    8,
                    width,
                    height,
                    rowstride,
                )
                path = target / f"sample-{index + 1}.png"
                pixbuf.savev(str(path), "png", [], [])
                out.append(str(path))
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"[self-test] 生成示例图片失败：{exc}", file=sys.stderr)
            return []

    def _seed_self_test(self, window: PosterWindow) -> None:
        # 把启动阶段的 toast 清掉，别挡住底部状态栏
        window._toast_overlay.dismiss_all()  # noqa: SLF001
        window.title_entry.set_text("GTK4 写一个原生应用")
        window.composer.set_text(
            "用 Libadwaita 写桌面应用，组件已经替你把 HIG 的细节想好了。\n"
            "正文里敲 @ 会弹出好友列表，输入拼音首字母就能搜到人；"
            "下面的智能话题点一下就插进正文。\n"
            "@林深见鹿 你也来写 GNOME 应用吧。\n"
            "#GNOME #GTK4 #桌面开发"
        )
        window.media_grid.add_paths(self._sample_images(2))
        window.tag_flow.add_custom_many(
            ["GNOME", "GTK4", "Libadwaita", "桌面开发", "Python"], selected=True
        )
        window.poll_panel.set_poll(
            Poll(options=["自己写", "用现成的", "再想想"], duration_minutes=1440)
        )
        window.poll_button.set_active(True)
        window._set_status("演练模式：内容不会真的发出去", sticky=True)  # noqa: SLF001
        window._update_counters()  # noqa: SLF001

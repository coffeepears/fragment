"""主窗口：Adw.ApplicationWindow + Adw.HeaderBar + 垂直流式布局。

结构：

    Adw.ToastOverlay
    └─ Adw.ToolbarView
       ├─ [top]    Adw.HeaderBar      设置 / 标题 / 发布 / 菜单
       ├─ [body]   Gtk.ScrolledWindow → Adw.Clamp → Gtk.Box(vertical)
       │             标题 → 媒体 → 正文 → 话题 → 投票
       └─ [bottom] 平台开关 + 进度条 + 状态

所有网络动作都通过 AsyncRunner 丢给 asyncio 线程，UI 永远不阻塞。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .models import (  # noqa: E402
    PLATFORM_VALUES,
    Draft,
    Platform,
    limits_for,
    platform_choices,
    weighted_length,
)
from .services.base import ProgressEvent, PublishResult  # noqa: E402
from .services.contacts import ContactBook  # noqa: E402
from .services.draftstore import DraftStore  # noqa: E402
from .services.publisher import Publisher  # noqa: E402
from .services.secrets import SecretStore  # noqa: E402
from .services.settings import Settings  # noqa: E402
from .services.xhs_service import XhsServiceManager  # noqa: E402
from .services.tag_engine import TagEngine  # noqa: E402
from .utils.asyncio_bridge import AsyncRunner  # noqa: E402
from .utils.text import HAS_JIEBA, warm_up  # noqa: E402
from .utils.thumbnails import ThumbnailLoader  # noqa: E402
from .widgets.composer import Composer  # noqa: E402
from .widgets.media_grid import MediaGrid  # noqa: E402
from .widgets.mention_popover import MentionPopover  # noqa: E402
from .widgets.platform_row import PlatformRow  # noqa: E402
from .widgets.usage_ring import UsageRing  # noqa: E402
from .widgets.poll_panel import PollPanel  # noqa: E402
from .widgets.settings_dialog import SettingsDialog  # noqa: E402
from .widgets.tag_flow import TagFlow  # noqa: E402

TAG_DEBOUNCE_MS = 700
STATUS_RESET_MS = 2500


class PosterWindow(Adw.ApplicationWindow):
    __gtype_name__ = "GPPosterWindow"

    def __init__(
        self,
        *,
        application: Adw.Application,
        settings: Settings,
        secrets_store: SecretStore,
        runner: AsyncRunner,
        publisher: Publisher,
        contact_book: ContactBook,
        thumbnail_loader: ThumbnailLoader,
        draft_store: DraftStore,
        restore_draft: bool = True,
    ) -> None:
        super().__init__(application=application)

        self.settings = settings
        self.secrets = secrets_store
        self.runner = runner
        self.publisher = publisher
        self.book = contact_book
        self.loader = thumbnail_loader
        self.draft_store = draft_store
        #: --self-test / --exercise 传 False：不能把用户真实的草稿
        #: 恢复进测试（之前真发生过，用户的图片被拍进了截图）
        self._restore_draft_on_start = restore_draft
        self.tag_engine = TagEngine(settings, publisher)

        self._busy = False
        #: 本地发布服务在不在（None = 还没探过）
        self._xhs_reachable: bool | None = None
        self._tag_timeout_id = 0
        self._status_timeout_id = 0
        self._undo_snapshot: dict[str, Any] | None = None

        self.set_title("Fragment")
        self.set_default_size(
            int(settings.get("window-width")), int(settings.get("window-height"))
        )

        self._toast_overlay = Adw.ToastOverlay()
        self._toolbar = Adw.ToolbarView()
        self._toolbar.add_top_bar(self._build_header())
        # 横幅贴在标题栏下面、内容区上面 —— 这才是 Adw.Banner 该待的地方
        # （塞进 Clamp 里会被包成一个奇怪的框）
        self._toolbar.add_top_bar(self._build_setup_banner())
        self._toolbar.add_top_bar(self._build_service_banner())
        self._toolbar.set_content(self._build_content())
        self._toolbar.add_bottom_bar(self._build_bottom_bar())
        self._toast_overlay.set_child(self._toolbar)
        self.set_content(self._toast_overlay)

        self._mention_popover = MentionPopover(self.book)
        #: 本机发布服务的生命周期管理（服务随 deb 安装，app 负责拉起）
        self.xhs_service_manager = XhsServiceManager()
        self._service_starting = False

        self._install_actions()
        self._connect_signals()
        self.connect("close-request", self._on_close_request)

        # 退出时收掉「自己起的」那个服务进程（用户自己的 systemd 服务不动）
        self.connect("destroy", lambda *_: self.xhs_service_manager.stop())

        self._maybe_restore_draft()
        self._refresh_setup_banner()
        self._update_counters()
        self._refresh_title_subtitle()
        # jieba 首次要建前缀词典（~0.5s），挪到后台线程，别卡第一次输入
        self.runner.submit(asyncio.to_thread(warm_up))
        # 顺便探一下本地发布服务，好让顶部横幅说实话
        self._probe_xhs_service()
        GLib.idle_add(lambda: (self.composer.grab_focus(), False)[1])

    # ================================================================== #
    # 构建 UI
    # ================================================================== #
    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()

        # 左侧故意留空：设置入口统一收在右边的「更多」菜单里
        # （菜单第一项就是「打开设置」，Ctrl+, 也还是它）
        self.window_title = Adw.WindowTitle(title="新帖子", subtitle="")
        header.set_title_widget(self.window_title)

        self.publish_spinner = Gtk.Spinner()
        self.publish_spinner.set_visible(False)

        self.publish_button = Gtk.Button(label="发布")
        self.publish_button.add_css_class("suggested-action")
        self.publish_button.set_tooltip_text("Ctrl+Enter")
        self.publish_button.connect("clicked", lambda *_: self.on_publish())

        menu = Gio.Menu()
        menu.append("恢复上次草稿", "win.restore-draft")
        menu.append("首选项", "win.settings")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.set_menu_model(menu)
        menu_button.set_tooltip_text("更多")

        # 「复制文案」收进 ⋮ 菜单：它是备用出路（零配置），不该跟主操作抢视线。
        # self.copy_button 保留成菜单里的那个动作的替身，外部代码仍可直接调 copy_text()。
        self.copy_button = None

        header.pack_end(self.publish_button)
        header.pack_end(self.publish_spinner)
        header.pack_end(menu_button)
        return header

    def _build_content(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        box.set_margin_top(20)
        box.set_margin_bottom(28)
        box.set_margin_start(24)
        box.set_margin_end(24)

        box.append(self._build_title_section())
        box.append(self._build_media_section())
        box.append(self._build_body_section())
        box.append(self._build_tag_section())
        box.append(self._build_poll_section())

        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_tightening_threshold(560)
        clamp.set_child(box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(clamp)
        return scroller

    # ---- 未配置提示横幅 ---- #
    def _build_setup_banner(self) -> Gtk.Widget:
        """一个平台都没配、又没开演练模式时出现。

        比把提示塞进设置页有用得多：用户会先看到这条，而不是点完发布才被
        「未配置」的 toast 弹回来。
        """
        self.setup_banner = Adw.Banner(
            title="还没配置任何平台 —— 可以先开「演练模式」走一遍，"
            "或者用右上角的「复制文案」贴到手机 App 里发。"
        )
        self.setup_banner.set_button_label("打开设置")
        self.setup_banner.connect("button-clicked", lambda *_: self.open_settings())
        self.setup_banner.set_revealed(False)
        return self.setup_banner

    def _build_service_banner(self) -> Gtk.Widget:
        """本机发布服务的启动进度 / 失败提示。

        为什么要它：服务随 deb 安装，但**第一次启动要下载一个约 150MB 的
        内置浏览器**（那是打过反检测补丁的 Chromium，塞不进 deb）。没有提示的话，
        用户会以为卡死了。这里给一句话 + 一个「重试」按钮。
        """
        self.service_banner = Adw.Banner(title="")
        self.service_banner.set_revealed(False)
        self.service_banner.set_button_label("重试")
        self.service_banner.connect("button-clicked", lambda *_: self._probe_xhs_service())
        return self.service_banner

    def _set_service_status(self, message: str, *, can_retry: bool = False) -> None:
        """在主线程里更新服务横幅。message 为空表示收起。"""
        banner = getattr(self, "service_banner", None)
        if banner is None:
            return
        if message:
            # Adw.Banner 的 title 是纯文本（内部走 gtk_label_set_label，不解析
            # markup），所以这里**不能**转义，否则会显示成 &amp; 之类。
            banner.set_title(message)
            banner.set_revealed(True)
            # Adw.Banner 没有「隐藏按钮」的公开接口，只能改标签
            banner.set_button_label("重试" if can_retry else "打开设置")
        else:
            banner.set_revealed(False)

    def _nothing_configured(self) -> bool:
        """一个「确实能发」的平台都没有时返回 True。

        小坑：MCP 通道的 is_configured() 恒为真（能不能发取决于服务在不在），
        所以那条路要靠启动时的后台探测（self._xhs_reachable）来判断，
        否则横幅永远不显示、等于没有。
        """
        if bool(self.settings.get("simulate")):
            return False
        if self.settings.xhs_transport == "mcp":
            if self._xhs_reachable is None:
                # 后台探测还没回来。这时候喊「还没配置任何平台」**大概率是错的**
                # （用户明明配好了，只是我们还没问完），画面还会闪一下假横幅。
                # 宁可先不说，等真结果出来再说。
                return False
            xhs_ready = self._xhs_reachable is True
        else:
            xhs_ready = self.publisher.adapter(Platform.XHS).is_configured()
        if xhs_ready:
            return False
        return not any(
            self.publisher.adapter(platform).is_configured()
            for platform in (Platform.X, Platform.INSTAGRAM)
        )

    def _refresh_setup_banner(self) -> None:
        self.setup_banner.set_revealed(self._nothing_configured())

    def _probe_xhs_service(self) -> None:
        """确保本机发布服务在跑，并据此刷新顶部横幅。

        🩸 早期版本这里只「探测」不「拉起」：服务没装 / 没起就干瞪眼，
        还要用户自己去 GitHub 下载二进制、手写 systemd 单元、`systemctl enable`。
        对「只想发Rednote」的人来说这纯属白折腾 —— 那一步本来就该由包和 app 负责。
        现在服务二进制随 deb 安装（见 vendor/xhs-service/），这里负责把它拉起来。
        """
        if self.settings.xhs_transport != "mcp":
            self._refresh_setup_banner()
            return
        if self._service_starting:
            return
        self._service_starting = True

        manager = self.xhs_service_manager
        manager.endpoint = str(
            self.settings.get("xhs-mcp-endpoint") or manager.endpoint
        ).rstrip("/")

        def _report(message: str) -> None:
            # 从后台线程来 → 丢回主线程改 UI
            self.runner.dispatch(lambda: self._set_service_status(message))

        def _done(result: object) -> None:
            self._service_starting = False
            ok, message = result if isinstance(result, tuple) else (False, str(result))
            self._xhs_reachable = bool(ok)
            self._set_service_status(
                "" if ok else str(message), can_retry=not ok
            )
            self._refresh_setup_banner()
            if not ok:
                self.toast(f"Rednote发布服务没起来：{message}")

        def _fail(exc: BaseException) -> None:
            self._service_starting = False
            self._xhs_reachable = False
            self._set_service_status(f"发布服务启动出错：{exc}", can_retry=True)
            self._refresh_setup_banner()

        self.runner.submit(manager.ensure_running(_report), _done, _fail)

    # ---- 标题（只属于小红书）---- #
    def _build_title_section(self) -> Gtk.Widget:
        """标签在上，输入框 + 用量圆环 + 字数在一行。

        之前是把「标题」和输入框包进一张 card 里 —— 视觉上多一层框，
        放到整列里显得吵。现在只有一条细标签，和正文、话题完全对齐。
        """
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.title_caption = Gtk.Label(label="标题", xalign=0.0)
        self.title_caption.add_css_class("section-caption")

        self.title_entry = Gtk.Entry()
        self.title_entry.set_placeholder_text("给笔记起个标题")
        self.title_entry.set_hexpand(True)
        self.title_entry.set_max_length(60)

        self.title_ring = UsageRing(size=20, thickness=2.5)
        self.title_counter = Gtk.Label(label="0/20", xalign=1.0)
        self.title_counter.add_css_class("counter")
        self.title_counter.add_css_class("dim-label")
        self.title_counter.set_width_chars(9)

        # 圆环和计数跟「标题」标签同一行、靠右 —— 与正文、话题的标签行对齐
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.title_caption.set_hexpand(True)
        header.append(self.title_caption)
        header.append(self.title_ring)
        header.append(self.title_counter)

        section.append(header)
        section.append(self.title_entry)
        # 名字沿用 title_card：外部（测试 / 计数逻辑）按这个引用整块
        self.title_card = section
        return section

    # ---- 媒体 ---- #
    def _build_media_section(self) -> Gtk.Widget:
        self.media_grid = MediaGrid(self.loader, max_items=18)
        return self.media_grid

    # ---- 正文 ---- #
    def _build_body_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        caption = Gtk.Label(label="正文", xalign=0.0)
        caption.add_css_class("section-caption")
        caption.set_hexpand(True)

        self.body_ring = UsageRing(size=20, thickness=2.5)
        self.body_counter = Gtk.Label(label="0/1000", xalign=1.0)
        self.body_counter.add_css_class("counter")
        self.body_counter.add_css_class("dim-label")
        self.body_counter.set_width_chars(9)
        # 多平台时一句话说不清，明细挂 tooltip
        self.body_counter.set_tooltip_text("")

        header.append(caption)
        header.append(self.body_ring)
        header.append(self.body_counter)

        self.composer = Composer()
        section.append(header)
        section.append(self.composer)
        return section

    # ---- 话题 ---- #
    def _build_tag_section(self) -> Gtk.Widget:
        self.tag_flow = TagFlow()
        return self.tag_flow

    # ---- 投票 ---- #
    def _build_poll_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.poll_button = Gtk.ToggleButton(label="添加投票")
        self.poll_button.set_tooltip_text("插入一个Rednote原生投票")
        self.poll_button.set_halign(Gtk.Align.START)

        self.poll_panel = PollPanel()
        self.poll_button.connect("toggled", self._on_poll_toggled)

        section.append(self.poll_button)
        section.append(self.poll_panel)
        return section

    # ---- 底部 ---- #
    def _build_bottom_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar.add_css_class("bottom-bar")
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        self._bottom_bar = bar

        # 只给界面展示「被允许的平台」；extra-platforms 关掉时这里只有小红书
        self.platform_row = PlatformRow(
            platform_choices(bool(self.settings.get("extra-platforms")))
        )

        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("publish-progress")
        self.progress.set_visible(False)
        self._refresh_bottom_bar()
        self.progress.set_valign(Gtk.Align.CENTER)

        self.status_label = Gtk.Label(label="", xalign=0.0)
        self.status_label.add_css_class("caption")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_ellipsize(3)
        self.status_label.set_visible(False)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.append(self.platform_row)
        top.append(self.progress)
        self.progress.set_hexpand(True)

        bar.append(top)
        bar.append(self.status_label)

        # 只有一个平台时，那一排开关没有任何信息量 —— 直接藏掉。
        # 整个底部条也只在真有内容（发布进度 / 状态）时才占位，平时不留空带子。
        if len(platform_choices(bool(self.settings.get("extra-platforms")))) <= 1:
            self.platform_row.set_visible(False)
        bar.set_visible(False)
        return bar

    def _refresh_bottom_bar(self) -> None:
        """底部条只在真的有东西时显示。"""
        bar = getattr(self, "_bottom_bar", None)
        if bar is None:
            return
        bar.set_visible(
            self.platform_row.get_visible()
            or self.progress.get_visible()
            or self.status_label.get_visible()
        )

    # ================================================================== #
    # 信号接线
    # ================================================================== #
    def _connect_signals(self) -> None:
        # --- 标题 / 正文 ---
        self.title_entry.connect("changed", lambda *_: self._on_text_changed())
        self.composer.connect("changed", lambda *_: self._on_text_changed())
        self.composer.connect("activate", lambda *_: self.on_publish())

        # --- 媒体 ---
        self.media_grid.connect("changed", lambda *_: self._on_draft_changed())
        self.media_grid.connect("rejected", lambda _w, msg: self.toast(msg))
        self.composer.connect("files-dropped", self._on_files_dropped)

        # --- @ 提及 ---
        self.composer.connect("mention-query", self._on_mention_query)
        self.composer.connect("mention-cancel", lambda *_: self._close_mention())
        self.composer.connect("mention-dismiss", lambda *_: self._close_mention())
        self.composer.connect("mention-nav", lambda _w, delta: self._mention_popover.move(delta))
        self.composer.connect("mention-accept", lambda *_: self._mention_popover.accept())
        self._mention_popover.connect("selected", self._on_mention_selected)

        # --- 话题 ---
        self.tag_flow.connect("tag-toggled", lambda *_: self._on_draft_changed())
        self.tag_flow.connect("refresh-requested", lambda *_: self._refresh_tags(force=True))

        # --- 投票 ---
        self.poll_panel.connect("changed", lambda *_: self._on_draft_changed())

        # --- 平台 ---
        self.platform_row.connect("selection-changed", self._on_platform_changed)

        # --- 关注列表更新后，@ 弹窗里的候选也要跟着变 ---
        self.book.connect("changed", lambda *_: self._mention_popover.refresh())

        # --- 设置变化 ---
        self.settings.connect("changed", self._on_setting_changed)

        # 初始平台勾选
        stored = {Platform(v) for v in self.settings.get("platforms") if v in PLATFORM_VALUES}
        self.platform_row.set_selected(stored or {Platform.XHS, Platform.X})
        self.poll_panel.set_platforms(self.platform_row.selected())
        self.platform_row.refresh_ring_visibility()

    def _install_actions(self) -> None:
        actions = {
            "settings": lambda *_: self.open_settings(),
            "copy-text": lambda *_: self.copy_text(),
            "publish": lambda *_: self.on_publish(),
            "clear-all": lambda *_: self.clear_all(),
            "save-draft": lambda *_: self.save_draft(manual=True),
            "restore-draft": lambda *_: self.restore_draft(manual=True),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.publish", ["<Control>Return", "<Control>KP_Enter"])
            application.set_accels_for_action("win.settings", ["<Control>comma"])
            application.set_accels_for_action("win.copy-text", ["<Control><Shift>c"])
            application.set_accels_for_action("win.clear-all", ["<Control><Shift>Delete"])
            application.set_accels_for_action("app.quit", ["<Control>q"])

    # ================================================================== #
    # 编辑状态
    # ================================================================== #
    def _on_text_changed(self) -> None:
        self._update_counters()
        self._refresh_title_subtitle()
        self._schedule_tag_refresh()

    def _on_draft_changed(self) -> None:
        self._update_counters()
        self._refresh_title_subtitle()

    def _on_platform_changed(self, *_args: object) -> None:
        # 注意：GObject 信号会把发出者当第一个参数塞进来，所以这里必须收 *args。
        # （踩过：写成无参，用户手点平台复选框时直接 TypeError。）
        platforms = self.platform_row.selected()
        self.settings.set("platforms", sorted(p.value for p in platforms))
        # 投票面板的上限（2–4 项还是 2–6 项、要不要问题框）跟着平台走
        self.poll_panel.set_platforms(platforms)
        self.platform_row.refresh_ring_visibility()
        self._update_counters()
        self._refresh_title_subtitle()

    def _on_setting_changed(self, _settings: Settings, key: str) -> None:
        if key in ("x-premium", "xhs-native-poll", "simulate", "extra-platforms"):
            self._update_counters()
        if key in (
            "simulate",
            "xhs-transport",
            "instagram-user-id",
            "x-client-id",
            "extra-platforms",
        ):
            self._refresh_setup_banner()
        if key in ("xhs-transport", "xhs-mcp-endpoint"):
            self._xhs_reachable = None
            self._probe_xhs_service()

    def _on_files_dropped(self, _widget: Gtk.Widget, paths: object) -> None:
        if isinstance(paths, list):
            self.media_grid.add_paths([str(p) for p in paths])

    def _poll_native_map(self, draft: Draft) -> dict[Platform, bool]:
        """每个平台这次发布能不能把投票当原生组件发出去。"""
        native: dict[Platform, bool] = {}
        for platform in platform_choices(bool(self.settings.get("extra-platforms"))):
            adapter = self.publisher.adapter(platform)
            limits = limits_for(platform, self.settings.x_premium)
            # 平台压根没有投票 → False；通道说能发 → True
            native[platform] = bool(limits.supports_poll and adapter.native_poll_available())
        return native

    def _update_counters(self) -> None:
        draft = self._collect_draft()
        premium = self.settings.x_premium
        native = self._poll_native_map(draft)

        # ---- 标题：只属于小红书 ----
        title_len = len(draft.title.strip())
        self.title_counter.set_text(f"{title_len}/20")
        self._set_counter_level(self.title_counter, title_len, 20)
        self.title_ring.set_usage(title_len / 20)
        xhs_selected = Platform.XHS in draft.platforms
        self.title_card.set_sensitive(xhs_selected or not draft.platforms)
        self.title_card.set_opacity(1.0 if xhs_selected else 0.55)
        # 只有一个平台时不必再写「（小红书）」；多平台时保留归属说明
        multiple = len(platform_choices(bool(self.settings.get("extra-platforms")))) > 1
        if xhs_selected:
            self.title_caption.set_text("标题（Rednote）" if multiple else "标题")
        else:
            self.title_caption.set_text("标题（Rednote没勾选，发不出去）")
        self.title_entry.set_sensitive(xhs_selected)

        # ---- 正文：一个圆环 + 一组数字 ----
        # 单平台时就是「314/1000」；多平台时显示最紧张的那个，明细挂 tooltip。
        # 早期是把所有平台「小红书 314/1000 · X 215/280」平铺在标签里，
        # 一多就变成一行读不下去的字。
        worst_fraction = 0.0
        headline = ""
        details: list[str] = []
        for platform in platform_choices(bool(self.settings.get("extra-platforms"))):
            limits = limits_for(platform, premium)
            length = weighted_length(
                draft.compose_body(platform, poll_as_text=not native.get(platform, True))
            )
            details.append(f"{platform.label} {length}/{limits.body_max}")
            if limits.body_max <= 0:
                continue
            fraction = length / limits.body_max
            if platform in draft.platforms and fraction >= worst_fraction:
                worst_fraction = fraction
                headline = f"{length}/{limits.body_max}"

        self.body_counter.set_text(headline or "0/0")
        self.body_counter.set_tooltip_text("、".join(details))
        self.body_ring.set_usage(worst_fraction)
        self._apply_level(
            self.body_counter,
            "error" if worst_fraction > 1.0 else ("warn" if worst_fraction > 0.9 else "ok"),
        )

        # ---- 平台状态行 + 用量圆环 ----
        self.platform_row.update(draft, premium, poll_native=native)
        self.platform_row.refresh_ring_visibility()

        self.tag_flow.set_visible(True)

    def _set_counter_level(self, label: Gtk.Label, value: int, maximum: int) -> None:
        if value > maximum:
            self._apply_level(label, "error")
        elif value > maximum * 0.9:
            self._apply_level(label, "warn")
        else:
            self._apply_level(label, "ok")

    def _apply_level(self, label: Gtk.Label, level: str) -> None:
        for css in ("dim-label", "near-limit", "over-limit", "error", "warning"):
            label.remove_css_class(css)
        if level == "error":
            label.add_css_class("over-limit")
        elif level == "warn":
            label.add_css_class("near-limit")
        else:
            label.add_css_class("dim-label")

    def _refresh_title_subtitle(self) -> None:
        platforms = self.platform_row.selected()
        if not platforms:
            self.window_title.set_subtitle("没有选择平台")
            return
        enabled = platform_choices(bool(self.settings.get("extra-platforms")))
        if len(enabled) <= 1:
            # 只有一个平台时「发布到 小红书」是废话，附件数「媒体 2/18」已经说了
            self.window_title.set_subtitle("")
            return
        names = " + ".join(p.label for p in enabled if p in platforms)
        count = len(self.media_grid.items())
        media = f" · {count} 个附件" if count else ""
        self.window_title.set_subtitle(f"发布到 {names}{media}")

    # ================================================================== #
    # 草稿
    # ================================================================== #
    def _collect_draft(self) -> Draft:
        return Draft(
            title=self.title_entry.get_text(),
            body=self.composer.text,
            media=self.media_grid.items(),
            tags=self.tag_flow.selected_tags(),
            poll=self.poll_panel.get_poll(),
            platforms=self.platform_row.selected(),
        )

    def _apply_draft(self, draft: Draft) -> None:
        self.title_entry.set_text(draft.title)
        self.composer.set_text(draft.body)
        self.media_grid.clear()
        for item in draft.media:
            self.media_grid.add_paths([item.path])
        self.platform_row.set_selected(draft.platforms)
        # 话题 chip 在标签刷新时会被重建，所以先塞进来占位并选中
        self.tag_flow.add_custom_many(draft.tags, selected=True)
        if draft.poll is not None:
            self.poll_panel.set_poll(draft.poll)
            self.poll_button.set_active(True)
        self._update_counters()
        self._refresh_title_subtitle()

    def _maybe_restore_draft(self) -> None:
        if not self._restore_draft_on_start:
            return
        if not self.settings.get("restore-draft"):
            return
        draft = self.draft_store.load()
        if draft is None:
            return
        self._apply_draft(draft)
        age = time.time() - self.draft_store.mtime()
        when = "刚刚" if age < 3600 else f"{int(age // 3600)} 小时前"
        self.toast(f"已恢复 {when} 的草稿")

    def save_draft(self, manual: bool = False) -> None:
        written = self.draft_store.save(self._collect_draft())
        if manual:
            self.toast("草稿已保存" if written else "空草稿，没什么可存的")

    def restore_draft(self, manual: bool = False) -> None:
        draft = self.draft_store.load()
        if draft is None:
            self.toast("没有找到草稿")
            return
        self._apply_draft(draft)
        if manual:
            self.toast("草稿已恢复")

    def clear_all(self) -> None:
        self._snapshot_for_undo()
        self.title_entry.set_text("")
        self.composer.set_text("")
        self.media_grid.clear()
        self.tag_flow.clear()
        self.poll_panel.clear()
        self.poll_button.set_active(False)
        self._update_counters()
        self._show_undo_toast("已清空")

    # ---- 复制文案（零配置出路）----------------------------------------- #
    def compose_for_clipboard(self) -> tuple[str, Platform] | None:
        """按「第一个勾选的平台」的格式拼一份可直接粘贴的文案。

        为什么按第一个而不是全部：每个平台的格式其实不一样
        （Rednote是 `#话题[话题]#`、X 是 `#话题`），混在一起反而不好用。
        想换格式就改勾选顺序 —— 界面上就是从右往左数第一个勾上的。
        """
        draft = self._collect_draft()
        targets = [platform for platform in Platform.ordered() if platform in draft.platforms]
        if not targets:
            return None
        platform = targets[0]
        native = self._poll_native_map(draft).get(platform, True)
        body = draft.compose_body(platform, poll_as_text=not native)

        parts: list[str] = []
        # 标题只有小红书有；复制给别的平台时不带标题
        if platform is Platform.XHS and draft.title.strip():
            parts.append(draft.title.strip())
        if body.strip():
            parts.append(body.strip())
        if not parts:
            return None
        return "\n\n".join(parts), platform

    def copy_text(self) -> None:
        composed = self.compose_for_clipboard()
        if composed is None:
            self.toast("先写点内容、勾一个平台，才有东西可复制")
            return
        text, platform = composed
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
        selected = self.platform_row.selected()
        extra = (
            f"（还勾着 {'、'.join(p.label for p in Platform.ordered() if p in selected and p is not platform)}，"
            f"这份是按「{platform.label}」的格式拼的）"
            if len(selected) > 1
            else ""
        )
        self.toast(f"已复制，粘到 {platform.label} 里就能发 {extra}")

    def _snapshot_for_undo(self) -> None:
        self._undo_snapshot = self._collect_draft().to_json()

    def _show_undo_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_button_label("撤销")
        toast.set_timeout(6)

        def _undo(*_args) -> None:
            if self._undo_snapshot is None:
                return
            self._apply_draft(Draft.from_json(self._undo_snapshot))
            self._undo_snapshot = None

        toast.connect("button-clicked", _undo)
        self._toast_overlay.add_toast(toast)

    # ================================================================== #
    # 智能标签
    # ================================================================== #
    def _schedule_tag_refresh(self) -> None:
        if self._tag_timeout_id:
            GLib.source_remove(self._tag_timeout_id)
        self._tag_timeout_id = GLib.timeout_add(TAG_DEBOUNCE_MS, self._on_tag_timeout)

    def _on_tag_timeout(self) -> bool:
        self._tag_timeout_id = 0
        self._refresh_tags()
        return False

    def _refresh_tags(self, force: bool = False) -> None:
        text = self.composer.text
        if not self.settings.get("auto-tags"):
            return
        if not text.strip():
            # 空正文时给一句**固定**说明（截图里那行「先写点正文吧」），
            # 但按刷新键**不做任何反应** —— 之前是按了才冒出提示，
            # 看起来像「点了没反应又突然说话」，很怪。
            self.tag_flow.clear()
            self.tag_flow.set_status("先写点正文吧")
            return

        keep = set(self.tag_flow.selected_tags())
        batch = self.tag_engine.build_batch(text, limit=18)
        self.tag_flow.set_suggestions(batch.local)
        self.tag_flow.set_selected(sorted(keep))
        hint = "" if HAS_JIEBA else "（装 jieba 分词会更准）"
        self.tag_flow.set_status(
            "" if batch.local else f"本地没有合适的候选{hint}"
        )

        # 远端推荐异步补进来；连不上就静默跳过，不打扰用户
        platforms = sorted(
            (p for p in self.platform_row.selected() if self.publisher.adapter(p).is_configured()),
            key=lambda p: p.value,
        )
        if not platforms:
            return
        self.tag_flow.set_loading(True)

        def _done(_result: Any) -> None:
            self.tag_flow.set_loading(False)
            remote = list(batch.remote)
            if not remote:
                return
            before = self.tag_flow.suggestion_count()
            keep_selected = set(self.tag_flow.selected_tags())
            self.tag_flow.set_suggestions(remote, append=True)
            self.tag_flow.set_selected(sorted(keep_selected))
            added = self.tag_flow.suggestion_count() - before
            if added > 0:
                self.tag_flow.set_status(f"{len(batch.local)} 个本地 + {added} 个平台推荐")

        def _fail(_exc: BaseException) -> None:
            # 平台侧推荐失败完全正常（没登录 / 服务没起），保持本地候选就好
            self.tag_flow.set_loading(False)

        self.runner.submit(self.tag_engine.enrich(text, batch, platforms), _done, _fail)

    # ================================================================== #
    # @ 提及
    # ================================================================== #
    def _on_mention_query(self, _widget: Gtk.Widget, query: str, x: int, y: int) -> None:
        platforms = sorted(self.platform_row.selected(), key=lambda p: p.value)
        self.composer.set_mention_active(True)
        self._mention_popover.open_at(self.composer.textview, x, y, query, platforms)
        # 弹窗显示后把焦点还给正文框（弹窗自身 can_focus=False，这里只是保险）
        self.composer.grab_focus()

    def _close_mention(self) -> None:
        self.composer.set_mention_active(False)
        if self._mention_popover.get_visible():
            self._mention_popover.popdown()

    def _on_mention_selected(self, _popover: Gtk.Widget, contact: Any) -> None:
        platforms = self.platform_row.selected()
        if len(platforms) == 1:
            target = next(iter(platforms))
        else:
            target = getattr(contact, "platform", None)
        mention = contact.mention_text(target)
        self.composer.complete_mention(mention)
        self._close_mention()
        self.composer.grab_focus()

    # ================================================================== #
    # 投票
    # ================================================================== #
    def _on_poll_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self.poll_panel.activate()
        else:
            self.poll_panel.set_reveal_child(False)
        self._on_draft_changed()

    # ================================================================== #
    # 发布
    # ================================================================== #
    def on_publish(self) -> None:
        if self._busy:
            self.toast("正在发布中，稍等")
            return

        draft = self._collect_draft()
        if not draft.platforms:
            self.toast("先勾选至少一个平台")
            return

        problems = draft.validate(self.settings.x_premium)
        if problems:
            summary = "；".join(
                f"{platform.label}：{'、'.join(issues)}" for platform, issues in problems.items()
            )
            self._set_status(summary, sticky=True)
            self.toast(f"还不能发 · {summary}")
            return

        native = self._poll_native_map(draft)
        fallbacks = {
            platform
            for platform in draft.platforms
            if draft.poll is not None
            and limits_for(platform, self.settings.x_premium).supports_poll
            and not native.get(platform, False)
        }
        warnings = draft.warnings(self.settings.x_premium, poll_fallbacks=fallbacks)
        if warnings:
            body = "\n".join(
                f"• {platform.label}：{'；'.join(items)}" for platform, items in warnings.items()
            )
            dialog = Adw.AlertDialog(heading="发布前确认", body=body)
            dialog.add_response("cancel", "再改改")
            dialog.add_response("go", "继续发布")
            dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("go")
            dialog.set_close_response("cancel")
            dialog.connect(
                "response",
                lambda _d, response: self._start_publish(draft) if response == "go" else None,
            )
            dialog.present(self)
            return

        self._start_publish(draft)

    def _start_publish(self, draft: Draft) -> None:
        self._set_busy(True)
        self.progress.set_visible(True)
        self.progress.set_fraction(0.0)
        self._refresh_bottom_bar()
        self._set_status("正在发布…", sticky=True)

        emitted = self.runner.dispatcher(self._on_progress_event)
        coro = self.publisher.publish(draft, draft.platforms, emitted)
        self.runner.submit(coro, lambda results: self._on_publish_done(draft, results), self._on_publish_error)

    def _on_progress_event(self, event: ProgressEvent) -> None:
        self.progress.set_fraction(max(0.0, min(1.0, event.ratio)))
        text = event.message or event.stage
        self._set_status(f"{event.platform.label} · {text}", sticky=True)

    def _on_publish_done(self, draft: Draft, results: list[PublishResult]) -> None:
        self._set_busy(False)
        self.progress.set_fraction(1.0)

        ok_results = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]

        for result in results:
            toast = Adw.Toast.new(
                f"{result.platform.label}：{'已发布' if result.ok else result.error}"
                + ("（演练）" if result.simulated else "")
            )
            toast.set_timeout(8)
            if result.url:
                toast.set_button_label("打开")

                def _open(_toast: Adw.Toast, url: str = result.url) -> None:
                    Gtk.UriLauncher.new(url).launch(self, None, None)

                toast.connect("button-clicked", _open)
            self._toast_overlay.add_toast(toast)

        if failed and not ok_results:
            self._set_status("发布失败", sticky=True)
        elif failed:
            self._set_status(f"{len(ok_results)} 个成功，{len(failed)} 个失败", sticky=True)
        else:
            self._set_status("全部发布成功 🎉")

        if ok_results:
            self._snapshot_for_undo()
            self._reset_after_publish()

        GLib.timeout_add(STATUS_RESET_MS, self._clear_status_if_idle)

    def _on_publish_error(self, exc: BaseException) -> None:
        self._set_busy(False)
        self.progress.set_visible(False)
        self._set_status(f"发布出错：{exc}", sticky=True)
        self.toast(f"发布出错：{exc}")

    def _reset_after_publish(self) -> None:
        """发布成功后清空编辑器，并留一个撤销。"""
        if self._undo_snapshot is None:
            return
        self.title_entry.set_text("")
        self.composer.set_text("")
        self.media_grid.clear()
        self.tag_flow.clear()
        self.poll_panel.clear()
        self.poll_button.set_active(False)
        self.draft_store.clear()
        self._update_counters()
        self._refresh_title_subtitle()
        self._show_undo_toast("已发布，编辑器已清空")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.publish_button.set_sensitive(not busy)
        self.publish_button.set_label("发布中…" if busy else "发布")
        self.publish_spinner.set_visible(busy)
        if busy:
            self.publish_spinner.start()
        else:
            self.publish_spinner.stop()

    # ================================================================== #
    # 状态 / Toast
    # ================================================================== #
    def _set_status(self, text: str, sticky: bool = False) -> None:
        if self._status_timeout_id:
            GLib.source_remove(self._status_timeout_id)
            self._status_timeout_id = 0
        self.status_label.set_text(text)
        self.status_label.set_visible(bool(text))
        self._refresh_bottom_bar()
        if text and not sticky:
            self._status_timeout_id = GLib.timeout_add(STATUS_RESET_MS, self._clear_status_if_idle)

    def _clear_status_if_idle(self) -> bool:
        self._status_timeout_id = 0
        if not self._busy:
            self.status_label.set_text("")
            self.status_label.set_visible(False)
            if self.progress.get_fraction() >= 1.0:
                self.progress.set_visible(False)
                self.progress.set_fraction(0.0)
        return False

    def toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(5)
        self._toast_overlay.add_toast(toast)

    # ================================================================== #
    # 设置 / 关闭
    # ================================================================== #
    def start_xhs_login(self) -> None:
        """直接跳到「Rednote → 扫码登录」。

        给 `gnome-poster --xhs-login` 用：把二维码**在用户自己的屏幕上**
        现取现扫 —— 比在聊天里发一张图靠谱得多，那玩意儿 4 分钟就过期。
        """
        dialog = self.open_settings()
        GLib.timeout_add(500, lambda: (dialog.trigger_xhs_login(), False)[1])

    def open_settings(self) -> "SettingsDialog":
        dialog = SettingsDialog(
            settings=self.settings,
            secrets_store=self.secrets,
            runner=self.runner,
            publisher=self.publisher,
            contact_book=self.book,
            toast_callback=self.toast,
        )
        # 用户可能刚在设置里登录完 / 打开了演练模式，关掉对话框后刷新横幅
        dialog.connect("closed", lambda *_: self._refresh_setup_banner())
        dialog.present(self)
        return dialog

    def _on_close_request(self, *_args) -> bool:
        width = self.get_width()
        height = self.get_height()
        if width > 200:
            self.settings.set("window-width", width)
        if height > 200:
            self.settings.set("window-height", height)

        if self.settings.get("restore-draft"):
            self.draft_store.save(self._collect_draft())
        return False  # 继续正常关闭

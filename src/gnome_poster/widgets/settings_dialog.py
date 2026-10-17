"""设置对话框（Adw.PreferencesDialog）。

四页：账号 / 外观 / 行为 / 关于。

账号页的设计目标是「没人配过也知道下一步点哪」。第一版做错了：把步骤塞进
默认折叠的展开器里，用户看到一屏「MCP / 签名器 / Cookie」完全不知道从哪下手。
现在是：

  * 「先看这里」一组在最上面：**不配账号也能用**（复制文案贴到手机 App 发），
    以及一键打开演练模式
  * 每个平台**按 1-2-3 步竖着排，全部默认可见**，每一步都写清「做什么、去哪点」，
    动作按钮直接跟在那一行右边
  * 真正折腾人的东西（换通道、签名器、Cookie、Client Secret、图床上传命令）
    统一收进折叠的「高级设置」里
  * 需要去外部网站的地方都给「打开 …」按钮，需要复制的都给复制按钮

所有网络动作（X 登录、Rednote测试、Instagram 验证、同步关注）都走 AsyncRunner，
界面不会因为等网络而卡住，结果通过 Toast / 状态行回报。
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..models import Platform  # noqa: E402
from ..services.contacts import ContactBook  # noqa: E402
from ..services.instagram_api import KEY_ACCESS_TOKEN as IG_TOKEN_KEY  # noqa: E402
from ..services.publisher import Publisher  # noqa: E402
from ..services.secrets import KEY_XHS_COOKIES, SecretStore  # noqa: E402
from ..services.settings import Settings, config_dir  # noqa: E402
from ..utils.asyncio_bridge import AsyncRunner  # noqa: E402
from ..utils.text import HAS_JIEBA, HAS_PYPINYIN  # noqa: E402

def _esc(text: object) -> str:
    """把要放进 Adw 行 title/subtitle 的文字转义。

    ⚠️ Adw.ActionRow 的 title / subtitle 在字符串里出现 `<` 或 `&` 时会走
    Pango markup 解析 —— 不转义的话「Projects & Apps」这种文案会整条渲染失败
    （Gtk-WARNING: Failed to set text ... from markup）。
    这个坑是在 exercise 里看到一大片警告才发现的。
    """
    return GLib.markup_escape_text(str(text if text is not None else ""))


X_DEVELOPER_URL = "https://developer.x.com/en/portal/dashboard"
X_APP_TYPES_DOC = "https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code"
XHS_MCP_URL = "https://github.com/xpzouying/xiaohongshu-mcp"
IG_EXPLORER_URL = "https://developers.facebook.com/tools/explorer/"
IG_DOCS_URL = "https://developers.facebook.com/docs/instagram-platform/content-publishing"


#: 账号页「通讯录与关注」永远在最后
_ACCOUNT_TAIL = "通讯录与关注"


def account_sections(include_extras: bool) -> tuple[str, ...]:
    """账号页要建哪些分组（顺序即展示顺序）。

    `extra-platforms` 关掉时只剩Rednote —— 对只想发Rednote的人来说，
    X 的「Client ID / 回调地址」和 Instagram 的「访问令牌 / 图床上传命令」
    全是干扰（而且 X 现在发帖还要按量付费）。
    代码一行没删，开关打开就回来。
    """
    middle = ("X", "Instagram") if include_extras else ()
    return ("Rednote", *middle, _ACCOUNT_TAIL)


class SettingsDialog(Adw.PreferencesDialog):
    __gtype_name__ = "GPSettingsDialog"

    def __init__(
        self,
        settings: Settings,
        secrets_store: SecretStore,
        runner: AsyncRunner,
        publisher: Publisher,
        contact_book: ContactBook,
        toast_callback=None,
    ) -> None:
        super().__init__()
        self.set_title("设置")
        self.set_search_enabled(True)

        self._settings = settings
        self._secrets = secrets_store
        self._runner = runner
        self._publisher = publisher
        self._book = contact_book
        self._toast = toast_callback or (lambda _msg: None)
        self._loading = False

        # 单页「首选项」：登录 / 主题 / 发布 / 关于，从上到下一条线读完。
        # 分页（账号·外观·行为·关于）对这么点内容来说层级过重。
        self.add(self._build_preferences_page())

    # ================================================================== #
    # 小工具：行工厂
    # ================================================================== #
    def _link_row(self, title: str, subtitle: str, url: str) -> Adw.ActionRow:
        """点一下就用默认浏览器打开的行。"""
        row = Adw.ActionRow()
        row.set_title(_esc(title))
        row.set_subtitle(_esc(subtitle))
        button = Gtk.Button.new_from_icon_name("external-link-symbolic")
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(f"打开 {url}")
        button.connect("clicked", lambda *_: self._open_url(url))
        row.add_suffix(button)
        row.set_activatable_widget(button)
        return row

    def _copy_row(self, title: str, value: str, subtitle: str = "") -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(_esc(title))
        row.set_subtitle(_esc(subtitle or value))
        button = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("复制")
        button.connect("clicked", lambda *_: self._copy(value, title))
        row.add_suffix(button)
        row.set_activatable_widget(button)
        return row

    def _button(
        self,
        label: str | None,
        callback,
        *,
        icon: str | None = None,
        suggested: bool = False,
        tooltip: str | None = None,
    ) -> Gtk.Button:
        """带图标 / 文字的按钮。label 为 None 时只显示图标。

        回调统一收一个参数（按钮本身），这样像「测试连接」那种需要在等待期间
        禁用按钮的地方可以直接用。
        """
        if label and icon:
            button = Gtk.Button()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=label))
            button.set_child(box)
        elif icon:
            button = Gtk.Button.new_from_icon_name(icon)
            button.add_css_class("flat")
        else:
            button = Gtk.Button(label=label or "")
        button.set_valign(Gtk.Align.CENTER)
        if suggested:
            button.add_css_class("suggested-action")
        if tooltip:
            button.set_tooltip_text(tooltip)
        if callback is not None:
            button.connect("clicked", lambda btn: callback(btn))
        return button

    def _step_row(
        self,
        number: int,
        title: str,
        subtitle: str,
        suffixes: list[Gtk.Widget] | None = None,
    ) -> Adw.ActionRow:
        """编号一步。**默认全部展开**，不做折叠 —— 之前把步骤藏进折叠展开器里，
        用户看到一屏专业名词完全不知道从哪下手（真实反馈）。
        """
        row = Adw.ActionRow()
        row.set_title(_esc(f"{number}. {title}"))
        row.set_subtitle(_esc(subtitle))
        row.set_activatable(False)
        for widget in suffixes or ():
            row.add_suffix(widget)
        return row

    def _open_url(self, url: str) -> None:
        launcher = Gtk.UriLauncher.new(url)
        launcher.launch(self.get_root(), None, None)

    def _copy(self, text: str, label: str = "内容") -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
        self._toast(f"{label}已复制到剪贴板")

    # ================================================================== #
    # 账号
    # ================================================================== #
    def _build_preferences_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.add(self._build_xhs_group())
        page.add(self._build_theme_group())
        page.add(self._build_publish_group())
        page.add(self._build_about_group())
        # 演练模式与「显示 X / Instagram」不常用，收进小红书的「高级设置」
        advanced = getattr(self, "_xhs_advanced", None)
        if advanced is not None:
            advanced.add_row(self.simulate_row)
        return page

    def _build_accounts_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("账号")
        page.set_icon_name("avatar-default-symbolic")
        # 该建哪些分组由 account_sections() 决定 —— 真实构建和冒烟测试用的是
        # **同一个函数**，测试才真的在测生产逻辑，而不是测一份抄来的副本。
        builders = {
            "Rednote": self._build_xhs_group,
            "X": self._build_x_group,
            "Instagram": self._build_instagram_group,
            "通讯录与关注": self._build_contacts_group,
        }
        include_extras = bool(self._settings.get("extra-platforms"))
        for name in account_sections(include_extras):
            page.add(builders[name]())
        return page

    # ---- 「先看这里」 -------------------------------------------------- #
    def _build_start_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("先看这里")
        group.set_description(
            "这个应用不会替你登录任何平台，它负责把内容排好、一次发到几个地方。"
            "下面每个平台都写清了「几步、点哪里」。"
        )

        row = Adw.ActionRow()
        row.set_title("不想配账号？也能用")
        row.set_subtitle(
            "用它写标题、正文、话题和图片，再点右上角的「复制文案」，"
            "直接粘到手机 App 里发 —— 全程不用配任何东西。"
        )
        row.set_activatable(False)
        group.add(row)

        simulate_row = Adw.ActionRow()
        simulate_row.set_title("想先看看发帖流程？")
        simulate_row.set_subtitle("打开演练模式：整条流程照跑，但一个请求都不会发出去。")
        simulate_row.set_activatable(False)
        simulate_row.add_suffix(
            self._button("打开演练模式", self._enable_simulate)
        )
        group.add(simulate_row)

        return group

    def _enable_simulate(self, _button: Gtk.Button | None = None) -> None:
        self._settings.set("simulate", True)
        if hasattr(self, "simulate_row"):
            self.simulate_row.set_active(True)
        self._toast("已打开演练模式：流程照跑，但不会真的发出去")

    # ---- 小红书 ------------------------------------------------------- #
    def _build_xhs_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title(_esc("登录Rednote"))
        group.set_description(
            "Rednote没有公开的发布接口。应用内置本机发布服务，扫码登录后即可发布。"
        )

        self.xhs_login_button = self._button(
            "扫码登录", lambda b: self._start_xhs_qr_login(b), suggested=True
        )
        self.xhs_status_row = Adw.ActionRow(title=_esc("登录"))
        self.xhs_status_row.set_subtitle(_esc("未登录"))
        self.xhs_status_row.add_suffix(self.xhs_login_button)
        group.add(self.xhs_status_row)

        test_row = Adw.ActionRow(title=_esc("连接"))
        test_row.set_subtitle(_esc("确认本机发布服务可用"))
        test_row.add_suffix(
            self._button("测试…", lambda b: self._test_platform(Platform.XHS, b))
        )
        group.add(test_row)

        # ---- 高级：一般不用动 ----
        advanced = Adw.ExpanderRow()
        self._xhs_advanced = advanced
        advanced.set_title(_esc("高级设置…"))
        advanced.set_subtitle("")

        self.xhs_transport_row = Adw.ComboRow(title=_esc("发布通道"))
        self.xhs_transport_row.set_subtitle(_esc("默认用内置小助手（随应用安装）；Web 通道要自备签名器"))
        self.xhs_transport_row.set_model(
            Gtk.StringList.new(["本地小助手（MCP，推荐）", "创作者平台 Web（需签名器）"])
        )
        self.xhs_transport_row.set_selected(0 if self._settings.xhs_transport == "mcp" else 1)
        self.xhs_transport_row.connect("notify::selected", self._on_transport_changed)
        advanced.add_row(self.xhs_transport_row)

        self.xhs_endpoint_row = Adw.EntryRow(title=_esc("小助手地址（内置的不用改）"))
        self.xhs_endpoint_row.set_text(str(self._settings.get("xhs-mcp-endpoint")))
        self.xhs_endpoint_row.set_tooltip_text("小助手监听的地址，默认 127.0.0.1:18060")
        self._bind_entry(self.xhs_endpoint_row, "xhs-mcp-endpoint")
        advanced.add_row(self.xhs_endpoint_row)

        self.xhs_signer_row = Adw.EntryRow(title=_esc("签名器命令"))
        self.xhs_signer_row.set_text(str(self._settings.get("xhs-signer-cmd")))
        self.xhs_signer_row.set_tooltip_text(
            '仅 Web 通道需要。例如 node /usr/share/gnome-poster/xhs_sign.js；'
            'stdin 收 JSON，stdout 返回 {"x-s": ..., "x-t": ...}'
        )
        self._bind_entry(self.xhs_signer_row, "xhs-signer-cmd")
        advanced.add_row(self.xhs_signer_row)

        self.xhs_cookie_row = Adw.PasswordEntryRow(title=_esc("登录 Cookie"))
        self.xhs_cookie_row.set_text(self._secrets.lookup(KEY_XHS_COOKIES) or "")
        self.xhs_cookie_row.set_tooltip_text("仅 Web 通道需要，至少要有 a1 与 web_session 两项")
        self.xhs_cookie_row.connect(
            "changed", lambda row: self._secrets.store(KEY_XHS_COOKIES, row.get_text())
        )
        advanced.add_row(self.xhs_cookie_row)

        self.xhs_visibility_row = Adw.ComboRow(title=_esc("笔记可见范围"))
        self.xhs_visibility_row.set_subtitle(
            _esc("想先自己看一眼效果就选「仅自己可见」；接口支持这个参数")
        )
        self.xhs_visibility_row.set_model(
            Gtk.StringList.new(["公开可见", "仅自己可见", "仅互关好友可见"])
        )
        _vis = str(self._settings.get("xhs-visibility") or "公开可见")
        self.xhs_visibility_row.set_selected(
            {"公开可见": 0, "仅自己可见": 1, "仅互关好友可见": 2}.get(_vis, 0)
        )

        def _on_visibility(row: Adw.ComboRow, _param) -> None:
            if self._loading:
                return
            value = {0: "公开可见", 1: "仅自己可见", 2: "仅互关好友可见"}.get(
                row.get_selected(), "公开可见"
            )
            self._settings.set("xhs-visibility", value)

        self.xhs_visibility_row.connect("notify::selected", _on_visibility)
        advanced.add_row(self.xhs_visibility_row)

        self.xhs_poll_row = Adw.SwitchRow(title=_esc("尝试原生投票组件"))
        self.xhs_poll_row.set_subtitle(
            _esc(
                "Rednote有原生投票（App 端 2–6 项），但它是 App 组件、没有公开写入接口。"
                "打开后会把投票字段发给小助手；它不支持就自动降级成正文文字。"
            )
        )
        self.xhs_poll_row.set_active(bool(self._settings.get("xhs-native-poll")))
        self._bind_switch(self.xhs_poll_row, "xhs-native-poll")
        advanced.add_row(self.xhs_poll_row)

        group.add(advanced)
        group.add(
            self._link_row(
                "打开小助手的项目页",
                "xiaohongshu-mcp：安装说明和用法都在这里",
                XHS_MCP_URL,
            )
        )

        self._on_transport_changed(self.xhs_transport_row, None, initial=True)
        return group

    # ---- 扫码登录（二维码直接显示在 app 里）--------------------------- #
    def trigger_xhs_login(self) -> None:
        """供外部（命令行 --xhs-login）调用，等价于点「扫码登录」。"""
        button = getattr(self, "xhs_login_button", None)
        if button is None:
            self._toast("找不到扫码登录入口")
            return
        self._start_xhs_qr_login(button)

    def _start_xhs_qr_login(self, button: Gtk.Button) -> None:
        """拉二维码 → 显示 → 轮询登录状态。

        服务内部会为这次请求开页面、抓二维码，并在后台等 4 分钟扫码，
        所以拿到图之后我们只要每 4 秒问一次 /login/status 就够了。
        """
        adapter = self._publisher.adapter(Platform.XHS)
        probe = getattr(adapter, "login_qrcode", None)
        if not callable(probe):
            self._toast("当前通道不支持扫码登录，请把发布通道改成 MCP")
            return

        button.set_sensitive(False)
        button.set_label("取二维码…")
        self._set_status(self.xhs_status_row, "正在向本地服务要二维码…")

        def _done(result: tuple[bytes | None, bool, str]) -> None:
            button.set_sensitive(True)
            button.set_label("扫码登录")
            image, logged_in, timeout = result
            if logged_in:
                self._set_status(self.xhs_status_row, "已经登录过了 ✓")
                self._toast("Rednote已经是登录状态")
                return
            if not image:
                self._set_status(self.xhs_status_row, "服务没返回二维码图片")
                self._toast("没拿到二维码，点「测试连接」看看服务状态")
                return
            self._show_qr_dialog(image, timeout)

        def _fail(exc: BaseException) -> None:
            button.set_sensitive(True)
            button.set_label("扫码登录")
            self._set_status(self.xhs_status_row, f"取二维码失败：{exc}")
            self._toast(f"取二维码失败：{exc}")

        self._runner.submit(probe(), _done, _fail)

    @staticmethod
    def _timeout_seconds(text: str, default: int = 240) -> int:
        """把服务给的 `4m0s` 解析成秒数。"""
        import re

        match = re.fullmatch(r"\s*(?:(\d+)m)?(?:(\d+)s)?\s*", text or "")
        if not match or not any(match.groups()):
            return default
        minutes = int(match.group(1) or 0)
        seconds = int(match.group(2) or 0)
        total = minutes * 60 + seconds
        return total if total > 0 else default

    def _show_qr_dialog(self, png: bytes, timeout: str) -> None:
        """显示二维码，并且**自动循环换新**。

        为什么必须自动换：服务那边的二维码只管 4 分钟（它自己的后台会话也是
        4 分钟）。早先版本只在用户手点「换一张」时才换，结果就是「人不在电脑前
        → 二维码过期 → 回来一看扫了个寂寞」。现在到点自动续，用户什么时候
        想起来扫都有效。
        """
        from gi.repository import Gdk, GLib

        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        except Exception as exc:  # noqa: BLE001
            self._toast(f"二维码解码失败：{exc}")
            return

        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_size_request(260, 260)
        picture.set_can_shrink(True)
        picture.set_margin_top(6)
        picture.set_margin_bottom(6)

        dialog = Adw.AlertDialog(
            heading="扫码登录Rednote",
            body="用Rednote App 的「扫一扫」扫上面这个码。\n"
            "二维码会自动续期，不用管过期；扫成功后这里会自动关闭。",
        )
        dialog.set_extra_child(picture)
        dialog.add_response("cancel", "关掉")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")

        # 自适应轮询：用户大概率就在电脑前的那几分钟里勤问几次，
        # 之后就不再打服务了 —— 因为**每次 /login/status 都会开一个无头浏览器
        # 页面**去访问小红书，13 秒一次挂一小时就是几百次页面加载，
        # 既浪费又容易被 platform 风控盯上。
        # 5 分钟之后只靠「续二维码」那一次调用顺带拿到的 is_logged_in 判断。
        state = {"alive": True, "poll_id": 0, "refresh_id": 0, "polls": 0}
        MAX_ACTIVE_POLLS = 15
        POLL_INTERVAL_MS = 20000

        def _stop() -> None:
            state["alive"] = False
            for key in ("poll_id", "refresh_id"):
                if state[key]:
                    GLib.source_remove(state[key])
                    state[key] = 0

        def _finish() -> None:
            _stop()
            dialog.set_body("登录成功 ✅")
            self._toast("Rednote登录成功")
            GLib.timeout_add(700, dialog.close)

        def _schedule_poll() -> None:
            if not state["alive"] or state["polls"] >= MAX_ACTIVE_POLLS:
                return
            # 下一轮在上一轮回来之后再排（每次要 5-9 秒），别把它打爆
            state["poll_id"] = GLib.timeout_add(POLL_INTERVAL_MS, _tick)

        def _tick() -> bool:
            if not state["alive"]:
                return False
            state["polls"] += 1
            self._runner.submit(
                self._publisher.adapter(Platform.XHS).check_account(),
                _checked,
                lambda _e: _schedule_poll(),
            )
            return False

        def _checked(text: object) -> None:
            message = str(text)
            self._set_status(self.xhs_status_row, message)
            if "已登录" in message or "已经登录" in message:
                _finish()
                return
            if state["polls"] >= MAX_ACTIVE_POLLS:
                # 不主动问了，等下一轮续二维码时顺带判断
                self._set_status(
                    self.xhs_status_row, "二维码会自动续期 —— 随时扫都行"
                )
                return
            _schedule_poll()

        def _schedule_refresh(seconds: int) -> None:
            if not state["alive"]:
                return
            # 留 40 秒余量，别等它彻底过期才换
            delay = max(30, seconds - 40) * 1000
            state["refresh_id"] = GLib.timeout_add(delay, _refetch)

        def _refetch() -> bool:
            state["refresh_id"] = 0
            if not state["alive"]:
                return False

            def _ok(result: tuple[bytes | None, bool, str]) -> None:
                if not state["alive"]:
                    return
                new_png, logged_in, new_timeout = result
                if logged_in:
                    _finish()
                    return
                # 续期这一次顺带就是一次登录检查，不用额外再打一次服务
                if state["polls"] < MAX_ACTIVE_POLLS:
                    _tick()
                if new_png:
                    try:
                        picture.set_paintable(
                            Gdk.Texture.new_from_bytes(GLib.Bytes.new(new_png))
                        )
                    except Exception:  # noqa: BLE001
                        pass
                _schedule_refresh(self._timeout_seconds(new_timeout))

            def _fail(_exc: BaseException) -> None:
                if state["alive"]:
                    # 服务可能抖了一下，过一会儿再试
                    _schedule_refresh(90)

            self._runner.submit(
                self._publisher.adapter(Platform.XHS).login_qrcode(), _ok, _fail
            )
            return False

        dialog.connect("closed", lambda *_: _stop())
        dialog.present(self)
        _schedule_poll()
        _schedule_refresh(self._timeout_seconds(timeout))

    def _on_transport_changed(self, row: Adw.ComboRow, _param, initial: bool = False) -> None:
        value = "mcp" if row.get_selected() == 0 else "web"
        if not initial:
            self._settings.set("xhs-transport", value)
        use_mcp = value == "mcp"
        self.xhs_endpoint_row.set_sensitive(use_mcp)
        self.xhs_signer_row.set_sensitive(not use_mcp)
        self.xhs_cookie_row.set_sensitive(not use_mcp)

    # ---- X ------------------------------------------------------------ #
    def _build_x_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("X (Twitter)")
        group.set_description("用官方接口，免费。需要你自己建一个应用，大约 5 分钟。四步：")

        group.add(
            self._step_row(
                1,
                "打开开发者后台，建一个应用（免费，不用绑卡）",
                "类型选 **Native App**（桌面应用的正道：公开客户端 + PKCE，只给 Client ID，"
                "没有密钥要管）。建完在 Keys and tokens 里能看到 Client ID。",
                [self._button("打开 developer.x.com", lambda _b: self._open_url(X_DEVELOPER_URL))],
            )
        )

        self.x_redirect_row = Adw.EntryRow(title=_esc("2. 复制这个地址，填进 X 后台的 Callback URI"))
        self.x_redirect_row.set_text(str(self._settings.get("x-redirect-uri")))
        self.x_redirect_row.set_tooltip_text(
            "X 要求「完全一致」（exact match）：大小写、端口、结尾的 /callback 都不能差"
        )
        self._bind_entry(self.x_redirect_row, "x-redirect-uri")
        self.x_redirect_row.add_suffix(
            self._button(
                None,
                lambda _b: self._copy(self.x_redirect_row.get_text().strip(), "回调地址"),
                icon="edit-copy-symbolic",
                tooltip="复制回调地址",
            )
        )
        group.add(self.x_redirect_row)

        # 好几个人（包括本机这位）会把这个地址直接粘进浏览器去「看看是什么」，
        # 然后看到「无法连接」就以为坏了。这里干脆把话说明白。
        warn = Adw.ActionRow()
        warn.set_title(_esc("⚠️ 上面这个地址不是网页，别用浏览器打开"))
        warn.set_subtitle(
            _esc(
                "它只是「X 授权完之后跳回来的收货地址」。平时打开一定是「无法连接」，"
                "那是正常的 —— 只有在点「登录 X」之后的那几分钟里，才有程序在等它。"
            )
        )
        warn.set_activatable(False)
        group.add(warn)

        # 官方文档原话：「For local development, use http://127.0.0.1 (not localhost)」
        # —— localhost 是**不被允许**的，别拿它当备选。
        rule = Adw.ActionRow()
        rule.set_title(_esc("📌 X 官方对回调地址的硬性要求"))
        rule.set_subtitle(
            _esc(
                "本地开发必须用 http://127.0.0.1，官方明确写了**不能用 localhost**；"
                "地址必须和后台登记的完全一致（含协议、端口、结尾斜杠）。"
                "另外 App permissions 要选「Read and write」，只读的话发不了帖。"
            )
        )
        rule.set_activatable(False)
        group.add(rule)

        self.x_client_id_row = Adw.EntryRow(title=_esc("3. 把 X 后台的 Client ID 粘到这里"))
        self.x_client_id_row.set_text(str(self._settings.get("x-client-id")))
        self.x_client_id_row.set_tooltip_text("在开发者后台的 Keys and tokens → OAuth 2.0 Client ID")
        self._bind_entry(self.x_client_id_row, "x-client-id")
        group.add(self.x_client_id_row)

        self.x_login_button = self._button(
            "登录 X", lambda b: self._login_x(b), suggested=True
        )
        self.x_status_row = self._step_row(
            4,
            "点登录，在浏览器里同意",
            "未登录",
            [
                self._button("测试连接", lambda b: self._test_platform(Platform.X, b)),
                self._button("退出", lambda _b: self._logout_x()),
                self.x_login_button,
            ],
        )
        group.add(self.x_status_row)

        advanced = Adw.ExpanderRow()
        advanced.set_title(_esc("高级设置"))
        advanced.set_subtitle(_esc("Client Secret、长文上限"))

        self.x_secret_row = Adw.PasswordEntryRow(title=_esc("Client Secret（可留空）"))
        self.x_secret_row.set_text(self._secrets.lookup("x/client_secret") or "")
        self.x_secret_row.connect(
            "changed", lambda row: self._secrets.store("x/client_secret", row.get_text())
        )
        advanced.add_row(self.x_secret_row)

        self.x_premium_row = Adw.SwitchRow(title=_esc("使用 Premium 长文上限"))
        self.x_premium_row.set_subtitle(_esc("打开后正文上限按 25000 加权字符算"))
        self.x_premium_row.set_active(bool(self._settings.get("x-premium")))
        self._bind_switch(self.x_premium_row, "x-premium")
        advanced.add_row(self.x_premium_row)

        group.add(advanced)
        group.add(
            self._link_row("OAuth 2.0 PKCE 说明", "想确认权限范围时看这个", X_APP_TYPES_DOC)
        )
        return group

    def _login_x(self, button: Gtk.Button) -> None:
        # 先把输入框里的 Client ID/Secret 落盘，否则授权时会读不到
        self._settings.set("x-client-id", self.x_client_id_row.get_text().strip())
        self._secrets.store("x/client_secret", self.x_secret_row.get_text().strip())

        if not self.x_client_id_row.get_text().strip():
            self._toast("先在第 3 步填 Client ID，再点登录")
            return

        adapter = self._publisher.adapter(Platform.X)
        button.set_sensitive(False)
        button.set_label("等待浏览器…")
        self._set_status(
            self.x_status_row, "已打开浏览器 —— 在网页上点 Authorize 就行，最长等你 10 分钟"
        )

        def open_browser(url: str) -> None:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(self.get_root(), None, None)

        # open_browser 会在 asyncio 线程里被调用，必须派回主线程
        coro = adapter.client.authorize(self._runner.dispatcher(open_browser))  # type: ignore[attr-defined]

        def _done(account: Any) -> None:
            button.set_sensitive(True)
            button.set_label("登录 X")
            label = getattr(account, "label", "") or "已登录"
            self._set_status(self.x_status_row, label)
            self._toast(f"X 登录成功：{label}")

        def _fail(exc: BaseException) -> None:
            button.set_sensitive(True)
            button.set_label("登录 X")
            self._set_status(self.x_status_row, f"失败：{exc}")
            self._toast(f"X 登录失败：{exc}")

        self._runner.submit(coro, on_success=_done, on_error=_fail)

    def _logout_x(self) -> None:
        adapter = self._publisher.adapter(Platform.X)
        adapter.client.forget()  # type: ignore[attr-defined]
        self._set_status(self.x_status_row, "未登录（已清除凭据）")
        self._toast("已清除 X 登录凭据")

    # ---- Instagram ---------------------------------------------------- #
    def _build_instagram_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Instagram")
        group.set_description(
            "官方接口，但门槛比前两个高：要企业号 / 创作者号，图片还得先放到公网上。"
            "个人号用不了 —— 这种情况下请用「复制文案」。四步："
        )

        group.add(
            self._step_row(
                1,
                "把 Instagram 切成专业账号",
                "在 Instagram 里切成企业号或创作者号，并绑定一个 Facebook 主页。",
            )
        )
        group.add(
            self._step_row(
                2,
                "建 Meta 应用并加上发布权限",
                "在 developers.facebook.com 建应用，加上 instagram_content_publish 权限。",
                [self._button("打开 Graph API Explorer", lambda _b: self._open_url(IG_EXPLORER_URL))],
            )
        )
        group.add(
            self._step_row(
                3,
                "取两样东西：访问令牌 + 用户 ID",
                "在 Explorer 里选到你的主页生成令牌（换成长期令牌），"
                "再查 me/accounts 拿到 Instagram 用户 ID。",
            )
        )

        self.ig_token_row = Adw.PasswordEntryRow(title=_esc("粘贴访问令牌"))
        self.ig_token_row.set_text(self._secrets.lookup(IG_TOKEN_KEY) or "")
        self.ig_token_row.set_tooltip_text("长期有效的 Page / User access token")
        self.ig_token_row.connect(
            "changed", lambda row: self._secrets.store(IG_TOKEN_KEY, row.get_text())
        )
        group.add(self.ig_token_row)

        self.ig_user_row = Adw.EntryRow(title=_esc("粘贴 Instagram 用户 ID"))
        self.ig_user_row.set_text(str(self._settings.get("instagram-user-id")))
        self.ig_user_row.set_tooltip_text("Graph API Explorer 里 me/accounts 返回的 Instagram 账号 id")
        self._bind_entry(self.ig_user_row, "instagram-user-id")
        group.add(self.ig_user_row)

        self.ig_status_row = self._step_row(
            4,
            "回到这里点一下测试",
            "未配置",
            [self._button("测试连接", lambda b: self._test_platform(Platform.INSTAGRAM, b))],
        )
        group.add(self.ig_status_row)

        # 一条很硬的限制，值得单独摆出来
        limit_row = Adw.ActionRow()
        limit_row.set_title(_esc("⚠️ 图片必须先在公网上"))
        limit_row.set_subtitle(
            _esc(
                "Graph API 只收公开链接，抓不到你电脑里的文件。所以要么在图片的 "
                "ALT 弹窗里填「公开链接」，要么在下面配一个图床上传命令。两个都没有会直接报错。"
            )
        )
        limit_row.set_activatable(False)
        group.add(limit_row)

        advanced = Adw.ExpanderRow()
        advanced.set_title(_esc("高级设置"))
        advanced.set_subtitle(_esc("图床上传命令"))

        self.ig_upload_row = Adw.EntryRow(title=_esc("图床上传命令"))
        self.ig_upload_row.set_text(str(self._settings.get("instagram-upload-cmd")))
        self.ig_upload_row.set_tooltip_text(
            'stdin 收 JSON（含 path），stdout 返回 {"url": "https://..."}'
        )
        self._bind_entry(self.ig_upload_row, "instagram-upload-cmd")
        advanced.add_row(self.ig_upload_row)

        group.add(advanced)
        group.add(
            self._link_row("Content Publishing 文档", "接口能力与限制的官方说明", IG_DOCS_URL)
        )
        return group

    # ---- 通用：测试连接 ------------------------------------------------ #
    def _test_platform(self, platform: Platform, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        row = {
            Platform.XHS: self.xhs_status_row,
            Platform.X: self.x_status_row,
            Platform.INSTAGRAM: self.ig_status_row,
        }[platform]
        self._set_status(row, "测试中…")
        adapter = self._publisher.adapter(platform)
        probe = getattr(adapter, "check_account", None)

        def _done(text: Any) -> None:
            button.set_sensitive(True)
            self._set_status(row, str(text))

        def _fail(exc: BaseException) -> None:
            button.set_sensitive(True)
            self._set_status(row, f"失败：{exc}")

        if callable(probe):
            self._runner.submit(probe(), _done, _fail)
        else:
            _done(adapter.account_hint())

    def _set_status(self, row: Adw.ActionRow, text: str) -> None:
        row.set_subtitle(_esc(text))

    # ---- 关注列表 ------------------------------------------------------ #
    def _build_contacts_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("关注列表（@ 提及用）")
        group.set_description(
            "这个跟发帖配置无关 —— 只是让打 @ 的时候能弹出好友列表。"
            "X 登录后可以自动同步；Rednote没有公开接口，只能本地维护。"
        )

        self.contacts_row = Adw.ActionRow()
        self.contacts_row.set_title(_esc("已收录好友"))
        self.contacts_row.set_subtitle(_esc(f"{len(self._book)} 人"))
        self.contacts_row.set_activatable(False)
        self.contacts_row.add_suffix(
            self._button(None, lambda _b: self._clear_contacts(),
                         icon="user-trash-symbolic", tooltip="清空本地列表")
        )
        self.contacts_row.add_suffix(
            self._button("同步 X 关注", lambda b: self._sync_contacts(b))
        )
        group.add(self.contacts_row)

        open_row = Adw.ActionRow()
        open_row.set_title(_esc("打开配置目录"))
        open_row.set_subtitle(_esc(str(config_dir())))
        open_row.set_activatable(False)
        open_row.add_suffix(
            self._button(None, lambda _b: self._open_config_dir(),
                         icon="folder-open-symbolic", tooltip="在文件管理器里打开")
        )
        group.add(open_row)

        return group

    def _sync_contacts(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        self.contacts_row.set_subtitle(_esc("同步中…"))

        def _done(report: dict) -> None:
            button.set_sensitive(True)
            self.contacts_row.set_subtitle(_esc(f"{len(self._book)} 人"))
            summary = "；".join(f"{p.label}: {v}" for p, v in report.items())
            self._toast(f"关注列表已更新 · {summary}")

        def _fail(exc: BaseException) -> None:
            button.set_sensitive(True)
            self.contacts_row.set_subtitle(_esc(f"{len(self._book)} 人"))
            self._toast(f"同步失败：{exc}")

        self._runner.submit(self._book.sync_from(self._publisher), _done, _fail)

    def _clear_contacts(self) -> None:
        self._book.clear()
        self.contacts_row.set_subtitle(_esc("0 人"))
        self._toast("关注列表已清空")

    def _open_config_dir(self) -> None:
        Gio.AppInfo.launch_default_for_uri(config_dir().as_uri(), None)

    # ================================================================== #
    # 外观
    # ================================================================== #
    def _build_theme_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("主题")

        self.color_row = Adw.ComboRow(title="颜色方案")
        self.color_row.set_subtitle(_esc("跟随系统时会随 GNOME 的深色模式自动切换"))
        self.color_row.set_model(Gtk.StringList.new(["跟随系统", "浅色", "深色"]))
        mapping = {"default": 0, "light": 1, "dark": 2}
        self.color_row.set_selected(mapping.get(str(self._settings.get("color-scheme")), 0))
        self.color_row.connect("notify::selected", self._on_color_changed)
        group.add(self.color_row)
        return group

    def _on_color_changed(self, row: Adw.ComboRow, _param) -> None:
        if self._loading:
            return
        value = {0: "default", 1: "light", 2: "dark"}.get(row.get_selected(), "default")
        self._settings.set("color-scheme", value)
        self._settings.apply_color_scheme()

    # ================================================================== #
    # 行为
    # ================================================================== #
    def _build_publish_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("发布")

        # 演练模式与额外平台开关收进「登录 → 高级设置」，这一组只留日常两项
        self.extra_platforms_row = Adw.SwitchRow(title="显示 X 和 Instagram")
        self.extra_platforms_row.set_active(bool(self._settings.get("extra-platforms")))
        self.extra_platforms_row.connect("notify::active", self._on_extra_platforms)

        self.simulate_row = Adw.SwitchRow(title="演练模式")
        self.simulate_row.set_subtitle(_esc("不发送请求，仅模拟发布流程"))
        self.simulate_row.set_active(bool(self._settings.get("simulate")))
        self._bind_switch(self.simulate_row, "simulate")

        self.autotags_row = Adw.SwitchRow(title="自动推荐话题")
        self.autotags_row.set_subtitle(_esc("根据正文，自动推荐合适的“#”话题"))
        self.autotags_row.set_active(bool(self._settings.get("auto-tags")))
        self._bind_switch(self.autotags_row, "auto-tags")
        group.add(self.autotags_row)

        self.restore_row = Adw.SwitchRow(title="退出时保存草稿")
        self.restore_row.set_subtitle(_esc("启动时恢复未发布的内容"))
        self.restore_row.set_active(bool(self._settings.get("restore-draft")))
        self._bind_switch(self.restore_row, "restore-draft")
        group.add(self.restore_row)
        return group

    # ================================================================== #
    # 关于
    # ================================================================== #
    def _build_about_group(self) -> Adw.PreferencesGroup:

        group = Adw.PreferencesGroup()

        about_row = Adw.ActionRow()
        about_row.set_title(_esc("关于 Fragment"))
        about_row.set_subtitle(_esc("版本、致谢与许可证"))
        about_button = Gtk.Button.new_from_icon_name("go-next-symbolic")
        about_button.add_css_class("flat")
        about_button.set_valign(Gtk.Align.CENTER)
        about_button.connect("clicked", lambda *_: self._show_about())
        about_row.add_suffix(about_button)
        about_row.set_activatable_widget(about_button)
        group.add(about_row)

        return group

    def _info_row(self, title: str, subtitle: str) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(_esc(title))
        row.set_subtitle(_esc(subtitle))
        row.set_activatable(False)
        return row

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Fragment",
            application_icon="com.pear.GnomePoster",
            developer_name="pear",
            version="0.4.5",
            # 不再带括号说明：那句话也已经过时了（没有「行为」分页了，
            # X/Instagram 开关也从高级设置里删掉了）
            comments="一次撰写，发布到Rednote。",
            website="https://example.org/gnome-poster",
            issue_url="https://example.org/gnome-poster/issues",
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_acknowledgement_section(
            "构建于",
            ["GTK 4", "Libadwaita", "PyGObject", "pypinyin", "jieba", "httpx", "GStreamer"],
        )
        about.present(self)

    # ================================================================== #
    # 绑定小工具
    # ================================================================== #
    def _bind_entry(self, row: Adw.EntryRow, key: str) -> None:
        def _on_changed(entry: Adw.EntryRow) -> None:
            if self._loading:
                return
            self._settings.set(key, entry.get_text().strip())

        row.connect("changed", _on_changed)

    def _on_extra_platforms(self, row: Adw.SwitchRow, _param) -> None:
        """关掉 X/Instagram 时，顺手把发布目标里那两个摘掉。

        否则会留下一堆「看不见但还勾着」的平台，发布时行为诡异。
        """
        if self._loading:
            return
        enabled = row.get_active()
        self._settings.set("extra-platforms", enabled)
        if enabled:
            return
        kept = [v for v in self._settings.get("platforms") if v == Platform.XHS.value]
        self._settings.set("platforms", kept or [Platform.XHS.value])

    def _bind_switch(self, row: Adw.SwitchRow, key: str) -> None:
        def _on_active(switch: Adw.SwitchRow, _param) -> None:
            if self._loading:
                return
            self._settings.set(key, switch.get_active())

        row.connect("notify::active", _on_active)

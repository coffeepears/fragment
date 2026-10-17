"""底部平台开关。

每个平台一个 pill 式复选框 + 一个用量圆环，下面跟一行实时状态：
「标题 8/20 · 图 2/18 · 正文 142/1000」这种，超限就变红。
用户不用点发布就知道会不会被打回 —— 圆环给一眼可见的总量，文字给明细。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from ..models import Draft, Platform, limits_for, weighted_length  # noqa: E402
from .usage_ring import UsageRing, worst_fraction  # noqa: E402


class PlatformToggle(Gtk.Box):
    __gtype_name__ = "GPPlatformToggle"

    __gsignals__ = {
        "toggled": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, platform: Platform) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.platform = platform

        self.check = Gtk.CheckButton(label=platform.label)
        self.check.add_css_class("platform-pill")
        self.check.set_active(True)
        self.check.connect("toggled", lambda *_: self.emit("toggled"))

        self.ring = UsageRing(size=24, thickness=3.0)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.append(self.check)
        top.append(self.ring)

        # 明细可能很长（三个平台各自的限额），所以：省略号 + tooltip
        self.status = Gtk.Label(label="", xalign=0.0)
        self.status.add_css_class("caption")
        self.status.add_css_class("dim-label")
        self.status.set_ellipsize(3)
        self.status.set_max_width_chars(30)
        self.status.set_width_chars(1)

        self.append(top)
        self.append(self.status)

    @property
    def active(self) -> bool:
        return self.check.get_active()

    def set_active(self, value: bool) -> None:
        self.check.set_active(bool(value))

    def set_status(self, text: str, level: str = "ok", usage: tuple[float, str] | None = None) -> None:
        self.status.set_text(text)
        self.set_tooltip_text(f"{self.platform.label}　{text}" if text else None)
        for css in ("dim-label", "warning", "error"):
            self.status.remove_css_class(css)
        if level == "warn":
            self.status.add_css_class("warning")
        elif level == "error":
            self.status.add_css_class("error")
        else:
            self.status.add_css_class("dim-label")

        if usage is None:
            self.ring.set_usage(0.0, "ok")
            self.ring.set_tooltip_text(None)
        else:
            self.ring.set_usage(*usage)
            fraction, level = usage
            note = {"ok": "还很宽松", "warn": "快满了", "error": "已经超了"}[level]
            self.ring.set_tooltip_text(
                f"{self.platform.label} 最紧的那项限额已用 {fraction * 100:.0f}% —— {note}"
            )
        self.ring.set_visible(self.check.get_active())


class PlatformRow(Gtk.FlowBox):
    """底部平台选择区。

    用 FlowBox 而不是 Gtk.Box：三个平台（小红书 / X / Instagram）横着放
    已经快到窗口宽度极限了，窗口一窄就会被挤到看不见 —— FlowBox 会自动换行，
    以后再加平台也不用动布局代码。

    `selection-changed` 在选择变化时触发。
    """

    __gtype_name__ = "GPPlatformRow"

    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, platforms: tuple[Platform, ...] | None = None) -> None:
        super().__init__()
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.set_column_spacing(20)
        self.set_row_spacing(8)
        self.set_max_children_per_line(3)
        self.set_min_children_per_line(1)
        self.set_homogeneous(False)
        self.add_css_class("flow-box-compact")
        self._toggles: dict[Platform, PlatformToggle] = {}

        for platform in (platforms if platforms is not None else Platform.ordered()):
            toggle = PlatformToggle(platform)
            toggle.connect("toggled", lambda *_: self.emit("selection-changed"))
            self.append(toggle)
            self._toggles[platform] = toggle

    # ------------------------------------------------------------------ #
    def selected(self) -> set[Platform]:
        return {platform for platform, toggle in self._toggles.items() if toggle.active}

    def set_selected(self, platforms: set[Platform]) -> None:
        for platform, toggle in self._toggles.items():
            toggle.set_active(platform in platforms)

    def toggle_for(self, platform: Platform) -> PlatformToggle:
        return self._toggles[platform]

    def refresh_ring_visibility(self) -> None:
        for toggle in self._toggles.values():
            toggle.ring.set_visible(toggle.active)

    # ------------------------------------------------------------------ #
    def update(
        self,
        draft: Draft,
        x_premium: bool,
        platforms: set[Platform] | None = None,
        poll_native: dict[Platform, bool] | None = None,
    ) -> None:
        """按当前草稿刷新每个平台的状态行与圆环。"""
        active = platforms if platforms is not None else self.selected()
        native = poll_native or {}

        for platform, toggle in self._toggles.items():
            limits = limits_for(platform, x_premium)
            if platform not in active:
                toggle.set_status("未勾选", "ok", usage=None)
                continue

            issues: list[str] = []
            body = draft.compose_body(platform, poll_as_text=not native.get(platform, True))
            body_len = weighted_length(body)
            title_len = len(draft.title.strip())
            media_count = len(draft.media)

            bits: list[str] = []
            if limits.has_title:
                bits.append(f"标题 {title_len}/{limits.title_max}")
                if not draft.title.strip() and limits.requires_title:
                    issues.append("缺标题")
                elif limits.title_max and title_len > limits.title_max:
                    issues.append("标题超长")
            bits.append(f"正文 {body_len}/{limits.body_max}")
            if body_len > limits.body_max:
                issues.append("正文超长")
            if limits.images_max:
                bits.append(f"媒体 {media_count}/{limits.images_max}")
                if media_count > limits.images_max:
                    issues.append("媒体超量")
                if limits.requires_media and media_count == 0:
                    issues.append("缺媒体")
            if draft.poll is not None and limits.supports_poll:
                bits.append("投票")
                if not native.get(platform, False):
                    bits.append("（文字）")

            level = "error" if issues else "ok"
            suffix = f" · {'、'.join(issues)}" if issues else ""

            # 圆环看「最紧的那个限额」
            pairs: list[tuple[float, int]] = [(body_len, limits.body_max)]
            if limits.has_title and limits.title_max:
                pairs.append((title_len, limits.title_max))
            if limits.images_max:
                pairs.append((media_count, limits.images_max))
            usage = worst_fraction(*pairs)

            toggle.set_status(" · ".join(bits) + suffix, level, usage=usage)

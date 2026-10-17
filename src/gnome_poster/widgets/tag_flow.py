"""智能标签栏：Gtk.FlowBox 里的 Tag Chips。

为什么用 FlowBox：标签数量不定、宽度不一，FlowBox 会自动折行，
不需要自己去算布局（GtkBox + 手动换行是最常见的过度设计）。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from ..services.tag_engine import TagSuggestion  # noqa: E402

MAX_CHIPS = 24


class _TagChip(Gtk.ToggleButton):
    """一个话题 chip。左边 #名称，右边可选的热度。"""

    def __init__(self, suggestion: TagSuggestion) -> None:
        super().__init__()
        self.suggestion = suggestion
        self.add_css_class("tag-chip")
        self.set_tooltip_text(self._tooltip())

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)

        name_label = Gtk.Label(label=f"#{suggestion.name}")
        name_label.set_ellipsize(3)
        name_label.set_max_width_chars(18)
        box.append(name_label)

        if suggestion.hot:
            hot = Gtk.Label(label=f"·{_human_hot(suggestion.hot)}")
            hot.add_css_class("hot")
            box.append(hot)
        elif suggestion.source == "platform":
            star = Gtk.Label(label="·平台")
            star.add_css_class("hot")
            box.append(star)

        self.set_child(box)

    def _tooltip(self) -> str:
        source = "平台话题推荐" if self.suggestion.source == "platform" else "根据正文抽取"
        platform = f"\n来源：{self.suggestion.platform.label}" if self.suggestion.platform else ""
        return f"#{self.suggestion.name}\n{source}{platform}"


def _human_hot(value: int) -> str:
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return str(value)


class TagFlow(Gtk.Box):
    """标签栏。

    信号 tag-toggled(str, bool)：名称 + 是否选中。
    """

    __gtype_name__ = "GPTagFlow"

    __gsignals__ = {
        "tag-toggled": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        "refresh-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._chips: dict[str, _TagChip] = {}
        self._order: list[str] = []

        # ---- 头部 ----
        title = Gtk.Label(label="智能话题", xalign=0.0)
        title.add_css_class("section-caption")
        title.set_hexpand(True)

        # 「14 个候选」这种数字对用户没有决策价值，收成空标签只做占位
        # （外部代码仍可往里写状态，比如「正在推荐…」）
        self.status = Gtk.Label(label="", xalign=0.0)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.add_css_class("flat")
        self.refresh_button.set_tooltip_text("重新推荐")
        self.refresh_button.connect("clicked", lambda *_: self.emit("refresh-requested"))

        self.add_entry = Gtk.Entry()
        self.add_entry.set_placeholder_text("添加话题…")
        self.add_entry.set_max_length(40)
        self.add_entry.set_width_chars(12)
        self.add_entry.connect("activate", self._on_manual_add)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(title)
        header.append(self.status)
        header.append(self.spinner)
        header.append(self.add_entry)
        header.append(self.refresh_button)
        self.append(header)

        self.description = Gtk.Label(
            label="写点内容，这里会自动冒出合适的话题标签；也可以手动加。",
            xalign=0.0,
        )
        self.description.add_css_class("dim-label")
        self.description.add_css_class("caption")
        self.description.set_wrap(True)
        self.append(self.description)

        # ---- chips ----
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_homogeneous(False)
        self.flow.set_column_spacing(6)
        self.flow.set_row_spacing(6)
        self.flow.set_max_children_per_line(20)
        self.flow.set_min_children_per_line(1)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.add_css_class("flow-box-compact")
        self.append(self.flow)

        self.empty_hint = Gtk.Label(
            label="写点内容，这里会自动冒出合适的话题标签；也可以手动加。", xalign=0.0
        )
        self.empty_hint.add_css_class("dim-label")
        self.empty_hint.add_css_class("caption")
        self.empty_hint.set_wrap(True)
        self.empty_hint.set_visible(False)
        self.append(self.empty_hint)

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    def set_suggestions(self, suggestions: list[TagSuggestion], append: bool = False) -> None:
        if not append:
            self._clear_chips()

        for suggestion in suggestions:
            key = suggestion.key
            if key in self._chips or len(self._chips) >= MAX_CHIPS:
                continue
            chip = _TagChip(suggestion)
            chip.connect("toggled", self._on_chip_toggled)
            self.flow.append(chip)
            self._chips[key] = chip
            self._order.append(key)
        self._sync_empty_hint()

    def add_custom(self, name: str, selected: bool = True) -> None:
        name = (name or "").strip().lstrip("#＃").strip()
        if not name:
            return
        key = name.casefold()
        if key in self._chips:
            self._chips[key].set_active(selected)
            return
        chip = _TagChip(TagSuggestion(name=name, score=0.0, source="local"))
        chip.set_active(selected)
        chip.connect("toggled", self._on_chip_toggled)
        self.flow.append(chip)
        self._chips[key] = chip
        self._order.append(key)
        self._sync_empty_hint()

    def add_custom_many(self, names, selected: bool = True) -> None:
        for name in names or ():
            self.add_custom(str(name), selected=selected)

    def clear(self) -> None:
        self._clear_chips()
        self._sync_empty_hint()

    def _clear_chips(self) -> None:
        while (child := self.flow.get_child_at_index(0)) is not None:
            self.flow.remove(child)
        self._chips.clear()
        self._order.clear()

    def selected_tags(self) -> list[str]:
        """按加入顺序返回被选中的话题名。"""
        out: list[str] = []
        for key in self._order:
            chip = self._chips.get(key)
            if chip is not None and chip.get_active():
                out.append(chip.suggestion.name)
        return out

    def set_selected(self, names: list[str]) -> None:
        wanted = {n.strip().lstrip("#＃").casefold() for n in names if n.strip()}
        for key, chip in self._chips.items():
            chip.set_active(key in wanted)
        # 不在候选里的话题补成 chip 并选中
        for name in names:
            clean = name.strip().lstrip("#＃")
            if clean and clean.casefold() not in self._chips:
                self.add_custom(clean, selected=True)

    def suggestion_count(self) -> int:
        return len(self._chips)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def set_loading(self, loading: bool) -> None:
        self.spinner.set_visible(loading)
        if loading:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.refresh_button.set_sensitive(not loading)

    def set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _sync_empty_hint(self) -> None:
        self.empty_hint.set_visible(not self._chips)

    # ------------------------------------------------------------------ #
    # 回调
    # ------------------------------------------------------------------ #
    def _on_chip_toggled(self, chip: Gtk.ToggleButton) -> None:
        if isinstance(chip, _TagChip):
            self.emit("tag-toggled", chip.suggestion.name, chip.get_active())

    def _on_manual_add(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        if not text:
            return
        # 支持一次贴多个：#旅行 #咖啡
        parts = [p for p in text.replace("，", " ").replace(",", " ").split() if p]
        for part in parts:
            self.add_custom(part, selected=True)
        entry.set_text("")

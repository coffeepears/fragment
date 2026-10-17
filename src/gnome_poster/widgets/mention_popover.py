"""@ 提及候选弹窗。

设计要点（这几条决定了它好不好用）：
  * 用 Gtk.Popover 挂在 Gtk.TextView 上，`set_pointing_to()` 指到 @ 的位置
  * `set_autohide(False)`：打字的时候不能让弹窗自己消失
  * 搜索框 `can_focus=False`：焦点必须留在正文框，方向键 / 回车才不会丢
  * 拼音、首字母、昵称、handle 四路模糊匹配（utils/text.py），命中处加粗
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from ..models import Platform  # noqa: E402
from ..services.contacts import Contact, ContactBook  # noqa: E402
from ..utils.text import normalize  # noqa: E402

MAX_ROWS = 8
POPOVER_WIDTH = 320
LIST_HEIGHT = 264


class _MentionRow(Gtk.ListBoxRow):
    """一行候选：头像 + 昵称 + @handle / 平台。"""

    def __init__(self) -> None:
        super().__init__()
        self.set_activatable(True)
        self.add_css_class("mention-row")

        self.avatar = Adw.Avatar(size=32, text="?", show_initials=True)
        self.name_label = Gtk.Label(xalign=0.0, ellipsize=3)  # 3 = PANGO_ELLIPSIZE_END
        self.name_label.set_use_markup(True)
        self.sub_label = Gtk.Label(xalign=0.0, ellipsize=3)
        self.sub_label.add_css_class("handle")
        self.sub_label.set_use_markup(True)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text_box.set_hexpand(True)
        text_box.append(self.name_label)
        text_box.append(self.sub_label)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row_box.append(self.avatar)
        row_box.append(text_box)
        self.set_child(row_box)

        self.contact: Contact | None = None

    def bind(self, contact: Contact, query: str) -> None:
        self.contact = contact
        self.avatar.set_text(contact.initial_letter or "?")
        # 头像 URL 是懒加载的：先把首字母画上，远端图片回来了再换（见 set_avatar_texture）
        self.name_label.set_markup(_highlight(contact.name or contact.handle, query))
        bits: list[str] = []
        if contact.handle:
            bits.append(_highlight(f"@{contact.handle}", query))
        if contact.platform is not None:
            bits.append(contact.platform.label)
        if contact.pinyin and normalize(query) and normalize(query) in contact.pinyin:
            bits.append(f"拼音 {contact.pinyin}")
        self.sub_label.set_markup(" · ".join(bits))


def _highlight(text: str, query: str) -> str:
    """把 query 命中的部分加粗。用 Pango markup，所以必须先转义。"""
    escaped = GLib.markup_escape_text(text or "")
    needle = (query or "").strip()
    if not needle:
        return escaped
    # 大小写不敏感地在「原始文本」上定位，再映射回转义后的串
    lowered = (text or "").casefold()
    idx = lowered.find(needle.casefold())
    if idx < 0:
        return escaped
    before = GLib.markup_escape_text((text or "")[:idx])
    hit = GLib.markup_escape_text((text or "")[idx : idx + len(needle)])
    after = GLib.markup_escape_text((text or "")[idx + len(needle) :])
    return f"{before}<b>{hit}</b>{after}"


class MentionPopover(Gtk.Popover):
    """挂在正文框上的候选列表。`selected` 信号带一个 Contact。"""

    __gtype_name__ = "GPMentionPopover"

    __gsignals__ = {
        "selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, book: ContactBook) -> None:
        super().__init__()
        self._book = book
        self._query = ""
        self._platforms: list[Platform] = []
        self._rows: list[_MentionRow] = []
        self._cursor = 0

        self.set_autohide(False)
        self.set_has_arrow(True)
        self.set_position(Gtk.PositionType.BOTTOM)
        self.set_offset(0, 4)
        self.set_can_focus(False)  # 绝不抢正文框的焦点
        self.add_css_class("mention-popover")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_size_request(POPOVER_WIDTH, -1)

        header = Gtk.Label(label="提及好友", xalign=0.0)
        header.add_css_class("mention-header")
        root.append(header)

        # 搜索框：只做展示，焦点永远留在正文框
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("拼音 / 首字母 / 昵称")
        self.search.set_can_focus(False)
        self.search.set_focus_on_click(False)
        self.search.set_margin_start(8)
        self.search.set_margin_end(8)
        self.search.set_margin_bottom(6)
        self.search.connect("search-changed", lambda *_: self.refresh())
        root.append(self.search)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_activate_on_single_click(False)  # 单击只选中，双击/回车才插入
        self.listbox.connect("row-activated", self._on_row_activated)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(80)
        scroller.set_max_content_height(LIST_HEIGHT)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(self.listbox)
        root.append(scroller)

        self.empty_label = Gtk.Label(label="没找到匹配的好友")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_margin_top(8)
        self.empty_label.set_margin_bottom(12)
        root.append(self.empty_label)

        self.set_child(root)

    # ------------------------------------------------------------------ #
    # 内容
    # ------------------------------------------------------------------ #
    def refresh(self, query: str | None = None) -> None:
        if query is not None:
            self._query = query
            if self.search.get_text() != query:
                self.search.set_text(query)
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        current_row = self.listbox.get_selected_row()
        current_contact = current_row.contact if isinstance(current_row, _MentionRow) else None

        while (child := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(child)
        self._rows.clear()

        matches = self._book.search(
            self._query, limit=MAX_ROWS, platforms=self._platforms or None
        )
        for contact in matches:
            row = _MentionRow()
            row.bind(contact, self._query)
            self.listbox.append(row)
            self._rows.append(row)

        self.empty_label.set_visible(not matches)

        # 尽量保持选中不变，否则默认选第一条
        target_index = 0
        if current_contact is not None:
            for index, row in enumerate(self._rows):
                if row.contact is not None and row.contact.id == current_contact.id:
                    target_index = index
                    break
        self._cursor = target_index
        self._select(target_index)

    def _select(self, index: int) -> None:
        if not self._rows:
            self._cursor = 0
            return
        index = max(0, min(index, len(self._rows) - 1))
        self._cursor = index
        row = self._rows[index]
        self.listbox.select_row(row)
        self._scroll_into_view(row)
        # 注意：这里**不能** grab_focus()，否则键盘焦点会被抢走，
        # 用户就没法继续在正文框里打字了。

    def _scroll_into_view(self, row: Gtk.Widget) -> None:
        scroller = self.listbox.get_ancestor(Gtk.ScrolledWindow)
        if scroller is None:
            return
        adjustment = scroller.get_vadjustment()
        if adjustment is None:
            return
        ok, bounds = row.compute_bounds(self.listbox)
        if not ok or bounds is None:
            return
        top = bounds.origin.y
        bottom = top + bounds.size.height
        value = adjustment.get_value()
        page = adjustment.get_page_size()
        if top < value:
            adjustment.set_value(top)
        elif bottom > value + page:
            adjustment.set_value(max(0.0, bottom - page))

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def move(self, delta: int) -> None:
        if not self._rows:
            return
        target = self._cursor + delta
        if target < 0:
            target = len(self._rows) - 1
        elif target >= len(self._rows):
            target = 0
        self._select(target)

    def accept(self) -> bool:
        if not self._rows:
            return False
        row = self._rows[max(0, min(self._cursor, len(self._rows) - 1))]
        if row.contact is None:
            return False
        self.emit("selected", row.contact)
        return True

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, _MentionRow) and row.contact is not None:
            self.emit("selected", row.contact)

    # ------------------------------------------------------------------ #
    # 显示
    # ------------------------------------------------------------------ #
    def open_at(
        self,
        parent: Gtk.Widget,
        x: int,
        y: int,
        query: str,
        platforms: list[Platform] | None = None,
    ) -> None:
        self._platforms = list(platforms or [])
        self._query = query
        if self.search.get_text() != query:
            self.search.set_text(query)
        self._rebuild_rows()

        if self.get_parent() is not parent:
            if self.get_parent() is not None:
                self.unparent()
            self.set_parent(parent)

        # 定位到光标那一行的左下角
        rect = Gdk.Rectangle()
        rect.x = max(0, int(x) - 6)
        rect.y = max(0, int(y))
        rect.width = 1
        rect.height = 1
        self.set_pointing_to(rect)
        if not self.get_visible():
            self.popup()

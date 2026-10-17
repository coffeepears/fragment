"""投票编辑面板。

默认折叠在「插入投票」按钮下面（Gtk.Revealer 展开），因为它多数时候用不到。

三家平台的能力不一样，面板按「当前勾选的平台」动态改上限：

  * X          —— 2–4 项、单选、无独立问题（正文就是问题）、5 分钟–7 天
  * 小红书     —— 2–6 项、**有独立问题**（建议 ≤15 字）
  * Instagram  —— 不支持

小红书这一条以前写错了（我原先以为它没有投票）。实际上 App 发布页工具栏里
就有「投票」组件：问题 + 2–6 个选项 + 时长。参考：
https://m.3835.com/news/23398.html
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from ..models import (  # noqa: E402
    POLL_DEFAULT_MINUTES,
    POLL_DURATIONS,
    POLL_MAX_OPTIONS,
    POLL_MAX_OPTION_CHARS,
    POLL_MIN_OPTIONS,
    Platform,
    Poll,
    limits_for,
)


class _OptionRow(Gtk.Box):
    def __init__(self, index: int, on_remove) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._on_remove = on_remove

        self.index_label = Gtk.Label(label=str(index), xalign=0.5)
        self.index_label.add_css_class("poll-option-index")
        self.index_label.set_width_chars(2)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(f"选项 {index}")
        self.entry.set_max_length(POLL_MAX_OPTION_CHARS)
        self.entry.set_hexpand(True)

        self.counter = Gtk.Label()
        self.counter.add_css_class("counter")
        self.counter.add_css_class("dim-label")

        self.remove_button = Gtk.Button.new_from_icon_name("list-remove-symbolic")
        self.remove_button.add_css_class("flat")
        self.remove_button.set_tooltip_text("删除这个选项")
        self.remove_button.connect("clicked", lambda *_: self._on_remove(self))

        self.append(self.index_label)
        self.append(self.entry)
        self.append(self.counter)
        self.append(self.remove_button)
        self._sync_counter()
        self.entry.connect("changed", lambda *_: self._sync_counter())

    def _sync_counter(self) -> None:
        length = len(self.entry.get_text())
        too_long = length > POLL_MAX_OPTION_CHARS
        self.counter.set_text(f"{length}/{POLL_MAX_OPTION_CHARS}")
        for css in ("dim-label", "over-limit"):
            self.counter.remove_css_class(css)
        self.counter.add_css_class("over-limit" if too_long else "dim-label")

    def set_index(self, index: int) -> None:
        self.index_label.set_label(str(index))
        self.entry.set_placeholder_text(f"选项 {index}")

    def text(self) -> str:
        return self.entry.get_text().strip()


class PollPanel(Gtk.Revealer):
    __gtype_name__ = "GPPollPanel"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(180)
        self._platforms: list[Platform] = []

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("poll-panel")
        card.set_margin_top(4)

        # ---- 标题行 ----
        title = Gtk.Label(label="投票", xalign=0.0)
        title.add_css_class("heading")
        title.set_hexpand(True)
        self.hint_label = Gtk.Label(label="", xalign=1.0)
        self.hint_label.add_css_class("dim-label")
        self.hint_label.add_css_class("caption")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(title)
        header.append(self.hint_label)
        card.append(header)

        # ---- 问题（只有小红书有）----
        self.question_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        question_label = Gtk.Label(label="问题", xalign=0.0)
        question_label.add_css_class("dim-label")
        question_label.set_width_chars(4)
        self.question_entry = Gtk.Entry()
        self.question_entry.set_placeholder_text("想让别人选什么？（建议 ≤15 字）")
        self.question_entry.set_max_length(20)
        self.question_entry.set_hexpand(True)
        self.question_counter = Gtk.Label()
        self.question_counter.add_css_class("counter")
        self.question_counter.add_css_class("dim-label")
        self.question_entry.connect("changed", lambda *_: self._on_question_changed())
        self.question_box.append(question_label)
        self.question_box.append(self.question_entry)
        self.question_box.append(self.question_counter)
        self.question_box.set_visible(False)
        card.append(self.question_box)

        # ---- 选项 ----
        self._options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.append(self._options_box)

        self.add_option_button = Gtk.Button()
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_box.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_box.append(Gtk.Label(label="添加选项"))
        self.add_option_button.set_child(add_box)
        self.add_option_button.add_css_class("flat")
        self.add_option_button.connect("clicked", lambda *_: self.add_option(focus=True))
        card.append(self.add_option_button)

        # ---- 时长 ----
        self.duration_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        duration_label = Gtk.Label(label="持续", xalign=0.0)
        duration_label.set_hexpand(True)
        self.duration_dropdown = Gtk.DropDown.new_from_strings([label for label, _ in POLL_DURATIONS])
        self.duration_dropdown.set_selected(self._duration_index(POLL_DEFAULT_MINUTES))
        self.duration_dropdown.connect("notify::selected", lambda *_: self._validate())
        self.duration_row.append(duration_label)
        self.duration_row.append(self.duration_dropdown)
        card.append(self.duration_row)

        # ---- 校验 ----
        self.validity_label = Gtk.Label(xalign=0.0)
        self.validity_label.add_css_class("caption")
        self.validity_label.set_wrap(True)
        card.append(self.validity_label)

        self._rows: list[_OptionRow] = []
        self.set_child(card)

        for _ in range(POLL_MIN_OPTIONS):
            self.add_option(focus=False)
        self._sync_platform_hints()

    # ------------------------------------------------------------------ #
    # 平台能力
    # ------------------------------------------------------------------ #
    def set_platforms(self, platforms: set[Platform] | None) -> None:
        """跟着底部勾选的平台走：决定上限、是否显示问题框、提示语。"""
        wanted = [p for p in Platform.ordered() if platforms and p in platforms]
        if wanted == self._platforms:
            return
        self._platforms = wanted
        self._sync_platform_hints()
        self._trim_options()
        self._validate()

    def _limits(self):
        """取所有已选平台里「最宽」的那个上限（用户能填到哪由最宽的决定），
        同时记下最严的，用来给出提示。"""
        if not self._platforms:
            return None
        return limits_for(self._platforms[0])

    def _max_options(self) -> int:
        caps = [limits_for(p).poll_max_options for p in self._platforms]
        caps = [c for c in caps if c]
        return max(caps) if caps else POLL_MAX_OPTIONS

    def _supports_poll(self, platform: Platform) -> bool:
        return limits_for(platform).supports_poll

    def _sync_platform_hints(self) -> None:
        supporting = [p for p in self._platforms if self._supports_poll(p)]
        question_max = max(
            (limits_for(p).poll_question_max for p in supporting), default=0
        )
        self.question_box.set_visible(question_max > 0)
        self.question_entry.set_max_length(question_max or 20)
        self._on_question_changed()

        if not supporting:
            self.hint_label.set_text("当前勾选的平台都不支持投票")
        else:
            parts = []
            for platform in supporting:
                limits = limits_for(platform)
                parts.append(f"{platform.label} {POLL_MIN_OPTIONS}–{limits.poll_max_options} 项")
            if question_max:
                parts.append(f"问题 ≤{question_max} 字")
            self.hint_label.set_text(" · ".join(parts))
        self.add_option_button.set_sensitive(len(self._rows) < self._max_options())

    def _on_question_changed(self) -> None:
        maximum = self.question_entry.get_max_length()
        length = len(self.question_entry.get_text())
        self.question_counter.set_text(f"{length}/{maximum}" if maximum else "")
        for css in ("dim-label", "over-limit"):
            self.question_counter.remove_css_class(css)
        self.question_counter.add_css_class(
            "over-limit" if maximum and length > maximum else "dim-label"
        )
        self.emit("changed")

    # ------------------------------------------------------------------ #
    # 选项管理
    # ------------------------------------------------------------------ #
    def add_option(self, focus: bool = False) -> _OptionRow | None:
        if len(self._rows) >= self._max_options():
            return None
        row = _OptionRow(len(self._rows) + 1, self._remove_option)
        row.entry.connect("changed", lambda *_: self._validate())
        self._options_box.append(row)
        self._rows.append(row)
        self._renumber()
        self._validate()
        if focus:
            row.entry.grab_focus()
        return row

    def _remove_option(self, row: _OptionRow) -> None:
        if len(self._rows) <= POLL_MIN_OPTIONS:
            return
        self._options_box.remove(row)
        self._rows.remove(row)
        self._renumber()
        self._validate()

    def _trim_options(self) -> None:
        """平台变了之后，选项数可能超出新上限，砍掉多的。"""
        cap = self._max_options()
        while len(self._rows) > max(POLL_MIN_OPTIONS, cap):
            row = self._rows.pop()
            self._options_box.remove(row)
        self._renumber()

    def _renumber(self) -> None:
        for index, row in enumerate(self._rows, start=1):
            row.set_index(index)
        self.add_option_button.set_sensitive(len(self._rows) < self._max_options())

    # ------------------------------------------------------------------ #
    # 取值 / 校验
    # ------------------------------------------------------------------ #
    @staticmethod
    def _duration_index(minutes: int) -> int:
        for index, (_label, value) in enumerate(POLL_DURATIONS):
            if value == minutes:
                return index
        return 2  # 默认 1 天

    @property
    def duration_minutes(self) -> int:
        selected = self.duration_dropdown.get_selected()
        if selected < 0 or selected >= len(POLL_DURATIONS):
            return POLL_DEFAULT_MINUTES
        return POLL_DURATIONS[selected][1]

    def get_poll(self) -> Poll | None:
        if not self.get_reveal_child():
            return None
        options = [row.text() for row in self._rows]
        if not any(options):
            return None
        return Poll(
            options=options,
            duration_minutes=self.duration_minutes,
            question=self.question_entry.get_text().strip(),
        )

    def clear(self) -> None:
        for row in list(self._rows):
            self._options_box.remove(row)
        self._rows.clear()
        for _ in range(POLL_MIN_OPTIONS):
            self.add_option(focus=False)
        self.question_entry.set_text("")
        self.duration_dropdown.set_selected(self._duration_index(POLL_DEFAULT_MINUTES))
        self.set_reveal_child(False)
        self.emit("changed")

    def set_poll(self, poll: Poll) -> None:
        """用一份已有的 poll 填满面板（恢复草稿时用）。"""
        for row in list(self._rows):
            self._options_box.remove(row)
        self._rows.clear()

        options = list(poll.options) or ["", ""]
        while len(options) < POLL_MIN_OPTIONS:
            options.append("")
        for _ in options[: self._max_options()]:
            self.add_option(focus=False)
        for row, text in zip(self._rows, options):
            row.entry.set_text(text)

        self.question_entry.set_text(poll.question)
        self.duration_dropdown.set_selected(self._duration_index(poll.duration_minutes))
        self.set_reveal_child(True)
        self._validate()

    def activate(self) -> None:
        self.set_reveal_child(True)
        self._validate()
        if self.question_box.get_visible():
            self.question_entry.grab_focus()
        elif self._rows:
            self._rows[0].entry.grab_focus()

    def _validate(self) -> None:
        poll = Poll(
            options=[row.text() for row in self._rows],
            duration_minutes=self.duration_minutes,
            question=self.question_entry.get_text().strip(),
        )
        filled = any(row.text() for row in self._rows) or bool(poll.question)

        # 按每家平台各校验一次，取第一条错；顺便知道「谁能收这个投票」
        problems: list[str] = []
        for platform in self._platforms:
            if not self._supports_poll(platform):
                continue
            ok, reason = poll.is_valid(platform)
            if not ok:
                problems.append(f"{platform.label}：{reason}")

        if filled and problems:
            self.validity_label.set_text("⚠ " + "；".join(problems))
            self.validity_label.remove_css_class("dim-label")
        else:
            count = len([r for r in self._rows if r.text()])
            self.validity_label.set_text(f"{count}/{self._max_options()} 项已填")
            self.validity_label.add_css_class("dim-label")
        self.emit("changed")

"""正文编辑器。

除了「多行文本 + 占位符 + 自动换行」这些基本项，这里负责一件有难度的事：
**实时识别正在输入的 @ 提及**，并把「光标在屏幕上的位置」算出来交给上层去挂 Popover。

信号：
  changed()                        文本变了
  mention-query(str, int, int)     出现了 @查询词，附带光标左下角坐标（相对 textview）
  mention-cancel()                 @ 查询结束（空格 / 换行 / 超长）
  mention-nav(int)                 -1 上移 / +1 下移（交给 Popover）
  mention-accept()                 Enter / Tab 接受当前候选
  mention-dismiss()                Esc 关掉候选
  activate()                       Ctrl+Enter：上层拿去触发发布
  files-dropped(object)            拖进来一批文件路径
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GObject, Gtk  # noqa: E402

#: @ 后面允许的最大查询长度，超了就认为用户不是在提人
MAX_QUERY_LEN = 32


def _is_boundary(ch: str) -> bool:
    """@ 前面是这些字符（或行首）才算一次「提及」。"""
    if ch.isspace():
        return True
    return ch in "，。！？；：、,.!?;:()[]{}<>\"'“”‘’《》【】…—-+*/=|\\"


class Composer(Gtk.Box):
    __gtype_name__ = "GPComposer"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "mention-query": (GObject.SignalFlags.RUN_FIRST, None, (str, int, int)),
        "mention-cancel": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "mention-nav": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "mention-accept": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "mention-dismiss": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "activate": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "files-dropped": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, placeholder: str = "@提及好友，Ctrl+Enter 发布") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.textview = Gtk.TextView()
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_accepts_tab(False)          # Tab 留给焦点切换
        self.textview.set_top_margin(8)
        self.textview.set_bottom_margin(8)
        self.textview.set_left_margin(6)
        self.textview.set_right_margin(6)
        self.textview.set_pixels_below_lines(3)
        self.textview.add_css_class("composer-body")
        self.buffer = self.textview.get_buffer()

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(150)
        scroller.set_max_content_height(420)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(self.textview)

        self._placeholder_label = Gtk.Label(label=placeholder)
        self._placeholder_label.set_xalign(0.0)
        self._placeholder_label.set_yalign(0.0)
        self._placeholder_label.set_wrap(True)
        self._placeholder_label.set_margin_start(12)
        self._placeholder_label.set_margin_top(10)
        self._placeholder_label.set_can_target(False)
        self._placeholder_label.add_css_class("composer-placeholder")

        overlay = Gtk.Overlay()
        overlay.set_child(scroller)
        overlay.add_overlay(self._placeholder_label)
        overlay.add_css_class("composer-frame")
        self.append(overlay)

        # 状态
        self._mention_start: int | None = None
        self._mention_active = False

        # 拖拽文件进来
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.textview.add_controller(drop)

        self.buffer.connect("changed", self._on_buffer_changed)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.textview.add_controller(keys)

        self._refresh_placeholder()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def text(self) -> str:
        return self.buffer.get_text(
            self.buffer.get_start_iter(), self.buffer.get_end_iter(), True
        )

    def set_text(self, text: str) -> None:
        self.buffer.set_text(text or "")
        self.buffer.place_cursor(self.buffer.get_end_iter())

    def insert_text(self, text: str) -> None:
        self.buffer.insert_at_cursor(text or "")

    def grab_focus(self) -> None:  # noqa: D102 - 覆盖 Gtk.Widget 的签名
        self.textview.grab_focus()

    def num_lines(self) -> int:
        return self.buffer.get_line_count()

    def set_mention_active(self, active: bool) -> None:
        """上层告诉编辑器「候选弹窗现在是开着的」，用来决定方向键归谁。"""
        self._mention_active = active

    def complete_mention(self, display: str) -> None:
        """把当前正在输入的 @xxx 替换成选中的提及。"""
        start_offset = self._mention_start
        if start_offset is None:
            self.insert_text(f"{display} ")
            return
        start = self.buffer.get_iter_at_offset(start_offset)
        end = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        self.buffer.delete(start, end)
        # delete 之后 insert mark 已经落在正确位置了
        self.buffer.insert_at_cursor(f"{display} ")
        self._mention_start = None
        self._mention_active = False

    def wrap_selection_or_insert(self, before: str, after: str = "") -> None:
        """给选中文字加壳（预留：加粗 / 引用）。"""
        bounds = self.buffer.get_selection_bounds()
        if not bounds:
            self.insert_text(before + after)
            return
        start, end = bounds
        selected = self.buffer.get_text(start, end, True)
        self.buffer.delete(start, end)
        self.buffer.insert_at_cursor(f"{before}{selected}{after}")

    # ------------------------------------------------------------------ #
    # 内部：占位符 / @ 检测
    # ------------------------------------------------------------------ #
    def _refresh_placeholder(self) -> None:
        empty = self.buffer.get_char_count() == 0
        self._placeholder_label.set_visible(empty)

    def _on_buffer_changed(self, _buffer: Gtk.TextBuffer) -> None:
        self._refresh_placeholder()
        self._update_mention_state()
        self.emit("changed")

    def _update_mention_state(self) -> None:
        if self.buffer.get_char_count() == 0:
            self._cancel_mention()
            return

        cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        offset = cursor.get_offset()
        text = self.buffer.get_text(
            self.buffer.get_start_iter(), self.buffer.get_end_iter(), True
        )

        at_index = text.rfind("@", 0, offset)
        if at_index < 0:
            self._cancel_mention()
            return
        if at_index > 0 and not _is_boundary(text[at_index - 1]):
            # 邮箱之类，不算提及
            self._cancel_mention()
            return

        query = text[at_index + 1 : offset]
        if len(query) > MAX_QUERY_LEN or any(ch.isspace() for ch in query):
            self._cancel_mention()
            return

        self._mention_start = at_index
        x, y = self._cursor_point(at_index)
        self.emit("mention-query", query, x, y)

    def _cancel_mention(self) -> None:
        if self._mention_start is None:
            return
        self._mention_start = None
        self._mention_active = False
        self.emit("mention-cancel")

    def _cursor_point(self, buffer_offset: int) -> tuple[int, int]:
        """把缓冲区里的偏移换算成 textview 内的窗口坐标（用于 Popover 定位）。

        GTK4 里没有 GdkWindow 了，`buffer_to_window_coords` 返回的就是相对控件
        的坐标，正好可以喂给 `Gtk.Popover.set_pointing_to()`。
        """
        try:
            iterator = self.buffer.get_iter_at_offset(buffer_offset)
            rect = self.textview.get_iter_location(iterator)
            converted = self.textview.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET, rect.x, rect.y
            )
            if isinstance(converted, tuple):
                x, y = converted[0], converted[1]
            else:  # 极少数绑定版本只返回 x
                x, y = int(converted), 0
            return int(x), int(y + rect.height)
        except Exception:  # noqa: BLE001 - 定位失败就退回控件左上角，不该崩
            return 0, 0

    # ------------------------------------------------------------------ #
    # 内部：键盘 / 拖拽
    # ------------------------------------------------------------------ #
    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and ctrl:
            self.emit("activate")
            return True

        if not self._mention_active:
            return False

        if keyval == Gdk.KEY_Escape:
            self.emit("mention-dismiss")
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self.emit("mention-nav", -1)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self.emit("mention-nav", 1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            self.emit("mention-accept")
            return True
        return False

    def _on_drop(self, _target: Gtk.DropTarget, value: object, _x: int, _y: int) -> bool:
        if not isinstance(value, Gdk.FileList):
            return False
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        self.emit("files-dropped", paths)
        return True


def make_drop_target_for_files(callback) -> Gtk.DropTarget:
    """给任意控件加「拖文件进来」的便捷构造器。"""
    target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

    def _on_drop(_target: Gtk.DropTarget, value: object, _x: int, _y: int) -> bool:
        if not isinstance(value, Gdk.FileList):
            return False
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        callback(paths)
        return True

    target.connect("drop", _on_drop)
    return target


def files_from_gio(files: list[Gio.File]) -> list[str]:
    return [f.get_path() for f in files if f.get_path()]

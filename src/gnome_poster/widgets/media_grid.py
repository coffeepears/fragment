"""多媒体附件区：Gtk.GridView + 缩略图 + 拖拽 + 一键删除 + ALT 文本。

关于缩略图：图片走 gdk-pixbuf，视频用 GStreamer 抽第一帧。抽帧在工作线程里做，
结果通过 GLib.idle_add 回主线程，所以拖 20 张图进去界面也不会卡。
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GObject, Gtk  # noqa: E402

from ..models import MediaItem, guess_kind, human_size  # noqa: E402
from ..utils.thumbnails import ThumbnailLoader  # noqa: E402

TILE_SIZE = 104
ICON_FALLBACK = {"image": "image-x-generic-symbolic", "video": "video-x-generic-symbolic"}


class _MediaTile(Gtk.Overlay):
    """一个媒体瓦片：缩略图 + 类型角标 + ALT 按钮 + 删除按钮 + 文件名。"""

    def __init__(self, on_remove, on_alt) -> None:
        super().__init__()
        self._on_remove = on_remove
        self._on_alt = on_alt
        self.item: MediaItem | None = None

        self.stack = Gtk.Stack()
        self.stack.set_size_request(TILE_SIZE, TILE_SIZE)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.placeholder = Gtk.Image.new_from_icon_name(ICON_FALLBACK["image"])
        self.placeholder.set_pixel_size(32)
        self.placeholder.add_css_class("dim-label")
        self.stack.add_named(self.placeholder, "placeholder")

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.set_can_shrink(True)
        self.stack.add_named(self.picture, "picture")
        self.stack.set_visible_child_name("placeholder")

        frame = Gtk.Box()
        frame.add_css_class("media-tile")
        frame.append(self.stack)
        self.set_child(frame)

        # 左上角：类型 / ALT 状态
        self.badge = Gtk.Label()
        self.badge.add_css_class("media-badge")
        self.badge.add_css_class("caption")
        top_left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        top_left.set_halign(Gtk.Align.START)
        top_left.set_valign(Gtk.Align.START)
        top_left.set_margin_start(4)
        top_left.set_margin_top(4)
        top_left.append(self.badge)
        self.add_overlay(top_left)

        # 右上角：删除
        remove_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        remove_button.add_css_class("remove-fab")
        remove_button.set_valign(Gtk.Align.START)
        remove_button.set_halign(Gtk.Align.END)
        remove_button.set_margin_top(4)
        remove_button.set_margin_end(4)
        remove_button.set_tooltip_text("移除")
        remove_button.connect("clicked", lambda *_: self._remove())
        self.add_overlay(remove_button)

        # 底部：文件名 + ALT
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bottom.set_valign(Gtk.Align.END)
        bottom.set_margin_start(4)
        bottom.set_margin_end(4)
        bottom.set_margin_bottom(4)
        self.alt_button = Gtk.Button(label="ALT")
        self.alt_button.add_css_class("media-badge")
        self.alt_button.add_css_class("flat")
        self.alt_button.set_tooltip_text(
            "替代文本（无障碍）与公开链接（Instagram 需要公网 URL）"
        )
        self.alt_button.connect("clicked", lambda *_: self._alt())
        bottom.append(self.alt_button)
        self.add_overlay(bottom)

        self.set_tooltip_text("")

    def _remove(self) -> None:
        if self.item is not None:
            self._on_remove(self.item)

    def _alt(self) -> None:
        if self.item is not None:
            self._on_alt(self.item, self.alt_button)

    def bind(self, item: MediaItem) -> None:
        self.item = item
        self.placeholder.set_from_icon_name(ICON_FALLBACK.get(item.kind, ICON_FALLBACK["image"]))
        self.badge.set_label("视频" if item.is_video else "图片")
        self.set_tooltip_text(f"{item.filename}\n{human_size(item.size_bytes)}")
        self.update_texture(item.texture)
        self.update_alt(item)

    def update_texture(self, texture: object) -> None:
        if texture is None:
            self.stack.set_visible_child_name("placeholder")
        else:
            self.picture.set_paintable(texture)
            self.stack.set_visible_child_name("picture")

    def update_alt(self, item: MediaItem) -> None:
        has_alt = bool(item.alt_text.strip())
        has_url = bool(item.remote_url.strip())
        marks = []
        if has_alt:
            marks.append("ALT")
        if has_url:
            marks.append("URL")
        self.alt_button.set_label(("+".join(marks) + " ✓") if marks else "ALT")
        self.alt_button.set_opacity(1.0 if marks else 0.75)


class MediaGrid(Gtk.Box):
    """附件区。用法：

        grid.add_paths([...])      # 拖拽 / 文件对话框都走这里
        grid.items()               # 拿全部媒体
    """

    __gtype_name__ = "GPMediaGrid"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "rejected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, loader: ThumbnailLoader, max_items: int = 18) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._loader = loader
        self._max_items = max_items
        self._tiles: dict[str, _MediaTile] = {}

        self.store = Gio.ListStore.new(MediaItem)
        self.store.connect("items-changed", lambda *_: self.emit("changed"))

        # ---- 头部 ----
        title = Gtk.Label(label="媒体", xalign=0.0)
        title.add_css_class("section-caption")
        self.count_label = Gtk.Label(label="0/18", xalign=1.0)
        self.count_label.add_css_class("dim-label")
        self.count_label.add_css_class("caption")
        self.count_label.set_hexpand(True)

        self.add_button = Gtk.Button()
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_box.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_box.append(Gtk.Label(label="添加"))
        self.add_button.set_child(add_box)
        self.add_button.add_css_class("flat")
        self.add_button.connect("clicked", lambda *_: self._open_file_dialog())

        add_menu = Gio.Menu()
        add_menu.append("从文件选择…", "media.pick")
        add_menu.append("清空全部", "media.clear")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.add_css_class("flat")
        menu_button.set_menu_model(add_menu)

        actions = Gio.SimpleActionGroup()
        pick = Gio.SimpleAction.new("pick", None)
        pick.connect("activate", lambda *_: self._open_file_dialog())
        actions.add_action(pick)
        clear = Gio.SimpleAction.new("clear", None)
        clear.connect("activate", lambda *_: self.clear())
        actions.add_action(clear)
        self.insert_action_group("media", actions)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(title)
        header.append(self.count_label)
        header.append(self.add_button)
        header.append(menu_button)
        self.append(header)

        # ---- 空状态 / 网格 ----
        self.drop_hint = Gtk.Label(
            label="拖入图片或视频，或轻点“添加”"
        )
        self.drop_hint.set_justify(Gtk.Justification.CENTER)
        self.drop_hint.add_css_class("media-drop-hint")
        self.drop_hint.set_wrap(True)

        self.grid = Gtk.GridView()
        self.grid.set_model(Gtk.NoSelection.new(self.store))
        self.grid.set_max_columns(6)
        self.grid.set_min_columns(2)
        self.grid.set_single_click_activate(False)
        self.grid.add_css_class("navigation-sidebar")

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", self._on_unbind)
        self.grid.set_factory(factory)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.drop_hint, "empty")
        self.stack.add_named(self.grid, "grid")

        self.add_controller(self._make_drop_target())
        self.append(self.stack)

        self.set_max_items(max_items)
        self._sync_state()

    # ------------------------------------------------------------------ #
    # 列表工厂
    # ------------------------------------------------------------------ #
    def _on_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        tile = _MediaTile(self.remove_item, self._edit_alt)
        list_item.set_child(tile)

    def _on_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        tile = list_item.get_child()
        if not isinstance(item, MediaItem) or not isinstance(tile, _MediaTile):
            return
        tile.bind(item)
        self._tiles[item.path] = tile
        if item.texture is None:
            self._request_thumbnail(item)

    def _on_unbind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        if isinstance(item, MediaItem):
            self._tiles.pop(item.path, None)

    # ------------------------------------------------------------------ #
    # 缩略图
    # ------------------------------------------------------------------ #
    def _request_thumbnail(self, item: MediaItem) -> None:
        self._loader.request(item.path, item.path, item.kind, self._on_thumbnail)

    def _on_thumbnail(self, key: str, texture: object) -> None:
        for index in range(self.store.get_n_items()):
            candidate = self.store.get_item(index)
            if isinstance(candidate, MediaItem) and candidate.path == key:
                candidate.texture = texture
                tile = self._tiles.get(key)
                if tile is not None:
                    tile.update_texture(texture)
                break

    # ------------------------------------------------------------------ #
    # 增删
    # ------------------------------------------------------------------ #
    def _make_drop_target(self) -> Gtk.DropTarget:
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

        def _on_enter(*_args) -> int:
            self.drop_hint.add_css_class("media-drop-active")
            return Gdk.DragAction.COPY

        def _on_leave(*_args) -> None:
            self.drop_hint.remove_css_class("media-drop-active")

        def _on_drop(_target, value, _x, _y) -> bool:
            _on_leave()
            if not isinstance(value, Gdk.FileList):
                return False
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            self.add_paths(paths)
            return True

        target.connect("enter", _on_enter)
        target.connect("leave", _on_leave)
        target.connect("drop", _on_drop)
        return target

    def _open_file_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("选择图片或视频")
        dialog.set_modal(True)

        filters = Gio.ListStore.new(Gtk.FileFilter)
        media_filter = Gtk.FileFilter()
        media_filter.set_name("图片与视频")
        for mime in ("image/*", "video/*"):
            media_filter.add_mime_type(mime)
        filters.append(media_filter)

        image_filter = Gtk.FileFilter()
        image_filter.set_name("只选图片")
        image_filter.add_mime_type("image/*")
        filters.append(image_filter)

        dialog.set_filters(filters)
        dialog.set_default_filter(media_filter)
        dialog.open_multiple(self.get_root(), None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except Exception:  # noqa: BLE001 - 用户点了取消
            return
        paths = [f.get_path() for f in files if f.get_path()]
        self.add_paths(paths)

    def add_paths(self, paths: list[str]) -> int:
        """加入一批文件。返回真正加进去的数量。"""
        existing = {item.path for item in self.items()}
        rejected: list[str] = []
        allowed = max(0, self._max_items - len(existing))
        added = 0

        for raw in paths:
            path = str(Path(raw).expanduser())
            if path in existing:
                continue
            if allowed <= 0:
                rejected.append(Path(path).name)
                continue
            kind = guess_kind(path)
            if kind is None:
                rejected.append(f"{Path(path).name}（不支持的类型）")
                continue
            if not Path(path).is_file():
                rejected.append(f"{Path(path).name}（读不到）")
                continue
            try:
                item = MediaItem(path, kind)
            except ValueError:
                rejected.append(Path(path).name)
                continue
            # 视频独占：已经有视频 / 想加视频但已有图片，都不允许混排
            current = self.items()
            if kind == "video" and current:
                rejected.append("视频不能和图片混排，请先清空")
                continue
            if any(m.is_video for m in current):
                rejected.append("已有视频，不能再加图片")
                continue

            self.store.append(item)
            existing.add(path)
            allowed -= 1
            added += 1

        if rejected:
            head = "；".join(rejected[:3])
            more = f" 等 {len(rejected)} 个" if len(rejected) > 3 else ""
            self.emit("rejected", f"已跳过：{head}{more}")
        if added:
            self._sync_state()
            self.emit("changed")
        return added

    def remove_item(self, item: MediaItem) -> None:
        for index in range(self.store.get_n_items()):
            candidate = self.store.get_item(index)
            if candidate is item or (
                isinstance(candidate, MediaItem) and candidate.path == item.path
            ):
                self._tiles.pop(item.path, None)
                self.store.remove(index)
                break
        self._sync_state()
        self.emit("changed")

    def clear(self) -> None:
        if self.store.get_n_items() == 0:
            return
        self._tiles.clear()
        self.store.remove_all()
        self._sync_state()
        self.emit("changed")

    # ------------------------------------------------------------------ #
    # 每个媒体的属性（替代文本 + 公开链接）
    # ------------------------------------------------------------------ #
    def _edit_alt(self, item: MediaItem, anchor: Gtk.Widget) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_autohide(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_size_request(330, -1)

        heading = Gtk.Label(label=item.filename, xalign=0.0)
        heading.add_css_class("heading")
        heading.set_ellipsize(3)
        box.append(heading)

        # ---- 替代文本 ----
        alt_label = Gtk.Label(label="替代文本（无障碍，X 支持）", xalign=0.0)
        alt_label.add_css_class("caption")
        alt_label.add_css_class("dim-label")
        alt_entry = Gtk.Entry()
        alt_entry.set_text(item.alt_text)
        alt_entry.set_placeholder_text("描述这张图的内容")
        alt_entry.set_max_length(1000)
        box.append(alt_label)
        box.append(alt_entry)

        # ---- 公开链接 ----
        url_label = Gtk.Label(label="公开链接（Instagram 必需）", xalign=0.0)
        url_label.add_css_class("caption")
        url_label.add_css_class("dim-label")
        url_entry = Gtk.Entry()
        url_entry.set_text(item.remote_url)
        url_entry.set_placeholder_text("https://…  Meta 服务器要能直接抓到")
        url_entry.set_input_purpose(Gtk.InputPurpose.URL)
        box.append(url_label)
        box.append(url_entry)

        note = Gtk.Label(
            label="Instagram 的 Graph API 只收公网 URL，本地文件它抓不到。"
            "留空的话可以在 设置 → 账号 → Instagram 里配一个图床上传命令。",
            xalign=0.0,
        )
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        note.set_wrap(True)
        note.set_max_width_chars(42)
        box.append(note)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.END)
        clear_button = Gtk.Button(label="清除")
        save_button = Gtk.Button(label="保存")
        save_button.add_css_class("suggested-action")
        buttons.append(clear_button)
        buttons.append(save_button)
        box.append(buttons)

        def _close() -> None:
            popover.popdown()
            popover.unparent()

        def _save() -> None:
            item.alt_text = alt_entry.get_text().strip()
            item.remote_url = url_entry.get_text().strip()
            tile = self._tiles.get(item.path)
            if tile is not None:
                tile.update_alt(item)
            self.emit("changed")
            _close()

        def _clear_and_close() -> None:
            alt_entry.set_text("")
            url_entry.set_text("")
            _close()

        clear_button.connect("clicked", lambda *_: _clear_and_close())
        alt_entry.connect("activate", lambda *_: _save())
        url_entry.connect("activate", lambda *_: _save())
        save_button.connect("clicked", lambda *_: _save())
        popover.set_child(box)
        popover.popup()
        alt_entry.grab_focus()

    # ------------------------------------------------------------------ #
    def items(self) -> list[MediaItem]:
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]  # type: ignore[misc]

    def set_max_items(self, maximum: int) -> None:
        self._max_items = max(1, int(maximum))
        self._sync_state()

    def _sync_state(self) -> None:
        count = self.store.get_n_items()
        self.count_label.set_text(f"{count} / {self._max_items}")
        self.stack.set_visible_child_name("grid" if count else "empty")
        self.add_button.set_sensitive(count < self._max_items)

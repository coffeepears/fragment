"""设置层。

优先用 GSettings（装了 schema 就是正常的 GNOME 行为：dconf 存、可被 dconf-editor 改、
`GSETTINGS_SCHEMA_DIR` 指向源码目录就能跑）。schema 没编译出来时自动退化成
`~/.config/gnome-poster/settings.json`，界面照样能用，只是不会 notify。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

SCHEMA_ID = "com.pear.GnomePoster"

#: 键 → 默认值（也是 JSON 后备模式的类型依据）
DEFAULTS: dict[str, Any] = {
    "color-scheme": "default",
    "window-width": 780,
    "window-height": 900,
    "x-client-id": "",
    "x-redirect-uri": "http://127.0.0.1:8721/callback",
    "x-premium": False,
    "xhs-transport": "mcp",
    "xhs-mcp-endpoint": "http://127.0.0.1:18060",
    "xhs-signer-cmd": "",
    #: 小红书投票是 App 端「组件」，没有公开写入接口。
    #: 打开这个开关才会把投票字段发给发布通道（你的服务支持才有用）。
    "xhs-native-poll": False,
    #: 小红书笔记可见范围：公开可见 / 仅自己可见 / 仅互关好友可见
    "xhs-visibility": "公开可见",
    #: 界面上是否同时显示 X 和 Instagram（关掉就只剩小红书）
    "extra-platforms": True,
    "instagram-user-id": "",
    #: 本地文件 → 公网 URL 的外部命令（Instagram 只收 URL）
    "instagram-upload-cmd": "",
    "simulate": False,
    "platforms": ["xhs", "x"],
    "auto-tags": True,
    "restore-draft": True,
}


def config_dir() -> Path:
    path = Path(GLib.get_user_config_dir()) / "gnome-poster"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir() -> Path:
    getter = getattr(GLib, "get_user_state_dir", None)
    base = Path(getter()) if callable(getter) else Path.home() / ".local" / "state"
    path = base / "gnome-poster"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings(GObject.Object):
    """带 `changed` 信号的设置门面。键名与 GSettings schema 一致。"""

    __gtype_name__ = "GPSettings"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, schema_id: str = SCHEMA_ID) -> None:
        super().__init__()
        self._schema_id = schema_id
        self._gs: Gio.Settings | None = None
        self._json_path = config_dir() / "settings.json"
        self._json: dict[str, Any] = {}

        try:
            source = Gio.SettingsSchemaSource.get_default()
            schema = source.lookup(schema_id, True) if source is not None else None
            if schema is not None:
                self._gs = Gio.Settings.new_full(schema, None, None)
        except GLib.Error:
            self._gs = None

        if self._gs is not None:
            self._gs.connect("changed", self._on_gsettings_changed)
        else:
            self._load_json()

    # -- 后端信息 ---------------------------------------------------------- #
    @property
    def backend(self) -> str:
        return "gsettings" if self._gs is not None else "json"

    @property
    def backend_detail(self) -> str:
        if self._gs is not None:
            return f"GSettings ({self._schema_id})"
        return f"JSON 后备文件 {self._json_path}"

    # -- JSON 后备 --------------------------------------------------------- #
    def _load_json(self) -> None:
        self._json = dict(DEFAULTS)
        if not self._json_path.exists():
            return
        try:
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            for key, value in data.items():
                if key in DEFAULTS:
                    self._json[key] = value

    def _save_json(self) -> None:
        try:
            tmp = self._json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._json, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._json_path)
        except OSError:
            pass

    # -- 读写 -------------------------------------------------------------- #
    def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(f"未知设置项：{key}")
        if self._gs is not None:
            try:
                return self._gs.get_value(key).unpack()
            except Exception:  # noqa: BLE001
                return DEFAULTS[key]
        return self._json.get(key, DEFAULTS[key])

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"未知设置项：{key}")
        if self._gs is not None:
            default = DEFAULTS[key]
            if isinstance(default, bool):
                self._gs.set_boolean(key, bool(value))
            elif isinstance(default, int):
                self._gs.set_int(key, int(value))
            elif isinstance(default, list):
                self._gs.set_strv(key, [str(v) for v in value])
            else:
                self._gs.set_string(key, str(value))
            return
        self._json[key] = value
        self._save_json()
        self.emit("changed", key)

    def reset(self, key: str) -> None:
        self.set(key, DEFAULTS[key])

    def _on_gsettings_changed(self, _settings: Gio.Settings, key: str) -> None:
        self.emit("changed", key)

    # -- 便捷读取 ---------------------------------------------------------- #
    @property
    def x_premium(self) -> bool:
        return bool(self.get("x-premium"))

    @property
    def simulate(self) -> bool:
        return bool(self.get("simulate"))

    @property
    def xhs_transport(self) -> str:
        return str(self.get("xhs-transport"))

    def apply_color_scheme(self) -> None:
        """把 color-scheme 应用到 Adw.StyleManager —— 深/浅色切换的唯一入口。"""
        import gi as _gi

        _gi.require_version("Adw", "1")
        from gi.repository import Adw

        mapping = {
            "default": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        scheme = str(self.get("color-scheme"))
        Adw.StyleManager.get_default().set_color_scheme(
            mapping.get(scheme, Adw.ColorScheme.DEFAULT)
        )

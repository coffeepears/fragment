"""凭据存储。

首选 libsecret（GNOME Keyring，登录后自动解锁）。拿不到 Secret 服务时退化成
`~/.config/gnome-poster/secrets.json`（0600），并且在设置界面里明确告知用户
「现在存的是明文」，不偷偷降级。
"""

from __future__ import annotations

import json
import os
import stat

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

from .settings import config_dir  # noqa: E402

APP_ID = "com.pear.GnomePoster"

#: 约定好的键名（值都是字符串）
KEY_X_ACCESS_TOKEN = "x/access_token"
KEY_X_REFRESH_TOKEN = "x/refresh_token"
KEY_X_EXPIRES_AT = "x/expires_at"
KEY_X_USER_ID = "x/user_id"
KEY_X_USERNAME = "x/username"
KEY_XHS_COOKIES = "xhs/cookies"
KEY_XHS_MCP_TOKEN = "xhs/mcp_token"


class SecretStore:
    def __init__(self, app_id: str = APP_ID) -> None:
        self._schema = None
        self._cache: dict[str, str] = {}
        self._file = config_dir() / "secrets.json"
        try:
            self._schema = Secret.Schema.new(
                app_id,
                Secret.SchemaFlags.NONE,
                {"key": Secret.SchemaAttributeType.STRING},
            )
        except Exception:  # noqa: BLE001 - 没有 keyring 守护进程时这里会失败
            self._schema = None
        if self._schema is None:
            self._load_file()

    # -- 后端信息 ---------------------------------------------------------- #
    @property
    def backend(self) -> str:
        return "libsecret" if self._schema is not None else "file"

    @property
    def backend_detail(self) -> str:
        if self._schema is not None:
            return "libsecret（GNOME 钥匙环）"
        return f"明文文件 {self._file}（权限 0600，未找到可用的钥匙环）"

    # -- JSON 后备 --------------------------------------------------------- #
    def _load_file(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._cache = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def _save_file(self) -> None:
        try:
            fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle, ensure_ascii=False, indent=2)
            os.chmod(self._file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    # -- 读写 -------------------------------------------------------------- #
    def store(self, key: str, value: str, label: str = "") -> bool:
        self._cache[key] = value
        if self._schema is None:
            self._save_file()
            return True
        try:
            return bool(
                Secret.password_store_sync(
                    self._schema,
                    {"key": key},
                    Secret.COLLECTION_DEFAULT,
                    label or f"GnomePoster: {key}",
                    value,
                    None,
                )
            )
        except Exception:  # noqa: BLE001
            self._save_file()
            return False

    def lookup(self, key: str) -> str | None:
        if key in self._cache:
            return self._cache[key]
        if self._schema is None:
            return None
        try:
            value = Secret.password_lookup_sync(self._schema, {"key": key}, None)
        except Exception:  # noqa: BLE001
            return None
        if value:
            self._cache[key] = value
        return value

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)
        if self._schema is None:
            self._save_file()
            return
        try:
            Secret.password_clear_sync(self._schema, {"key": key}, None)
        except Exception:  # noqa: BLE001
            pass

    def clear_all(self, keys: tuple[str, ...]) -> None:
        for key in keys:
            self.delete(key)

"""@ 提及的关注列表。

数据来源三条，优先级从低到高：
  1. 内置示例（第一次跑起来时 @ 弹窗不该是空的）
  2. 本地缓存 ~/.config/gnome-poster/contacts.json
  3. 各平台同步（X 可以拉 following；小红书创作者平台没有公开的关注列表接口，
     所以小红书侧只支持本地维护 / 手动导入）

搜索打分在 utils/text.py（拼音全拼、首字母、昵称、handle 四路取最大）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import gi

gi.require_version("GObject", "2.0")
from gi.repository import GObject  # noqa: E402

from ..models import Platform
from ..utils.paths import data_file
from ..utils.text import match_score, pinyin_forms
from .publisher import Publisher
from .settings import config_dir


@dataclass
class Contact:
    id: str
    name: str
    handle: str
    platform: Platform | None = None
    avatar_url: str = ""
    source: str = "local"          # local | x | example
    _pinyin: str = field(default="", repr=False)
    _initials: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self._pinyin and not self._initials:
            self._pinyin, self._initials = pinyin_forms(self.name)

    @property
    def pinyin(self) -> str:
        if not self._pinyin:
            self._pinyin, self._initials = pinyin_forms(self.name)
        return self._pinyin

    @property
    def initials(self) -> str:
        if not self._initials:
            self._pinyin, self._initials = pinyin_forms(self.name)
        return self._initials

    @property
    def display_handle(self) -> str:
        return f"@{self.handle}" if self.handle else ""

    @property
    def initial_letter(self) -> str:
        source = self.name or self.handle
        return source[:1].upper() if source else "?"

    def mention_text(self, platform: Platform | None = None) -> str:
        """插入正文时用哪种写法。

        小红书正文里 @ 走的是昵称，X 走的是 handle。
        """
        target = platform or self.platform
        if target is Platform.XHS:
            return f"@{self.name}"
        if target is Platform.X:
            return f"@{self.handle}" if self.handle else f"@{self.name}"
        return self.display_handle or f"@{self.name}"

    def score(self, query: str) -> int:
        return match_score(
            query,
            name=self.name,
            handle=self.handle,
            pinyin=self.pinyin,
            initials=self.initials,
        )

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "handle": self.handle,
            "platform": self.platform.value if self.platform else None,
            "avatar_url": self.avatar_url,
        }

    @classmethod
    def from_json(cls, data: dict, source: str = "local") -> "Contact | None":
        name = str(data.get("name") or "").strip()
        handle = str(data.get("handle") or "").strip()
        if not name and not handle:
            return None
        platform_raw = data.get("platform")
        try:
            platform = Platform(platform_raw) if platform_raw else None
        except ValueError:
            platform = None
        return cls(
            id=str(data.get("id") or handle or name),
            name=name or handle,
            handle=handle,
            platform=platform,
            avatar_url=str(data.get("avatar_url") or ""),
            source=source,
        )


class ContactBook(GObject.Object):
    """关注列表的内存索引 + 落盘。"""

    __gtype_name__ = "GPContactBook"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, seed_example: bool = True) -> None:
        super().__init__()
        self._path = config_dir() / "contacts.json"
        self._contacts: list[Contact] = []
        self._by_id: dict[str, Contact] = {}
        self._load(seed_example=seed_example)

    # -- 持久化 ------------------------------------------------------------ #
    def _load(self, seed_example: bool = True) -> None:
        entries: list[dict] = []
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("contacts"), list):
                    entries = [e for e in data["contacts"] if isinstance(e, dict)]
            except (OSError, json.JSONDecodeError):
                entries = []

        if not entries and seed_example:
            example = data_file("contacts.example.json")
            if example is not None:
                try:
                    data = json.loads(example.read_text(encoding="utf-8"))
                    entries = [e for e in data.get("contacts", []) if isinstance(e, dict)]
                except (OSError, json.JSONDecodeError):
                    entries = []

        self._contacts = []
        self._by_id = {}
        for entry in entries:
            contact = Contact.from_json(entry)
            if contact is not None:
                self._insert(contact)

    def save(self) -> None:
        payload = {
            "_comment": "gnome-poster 的关注列表缓存，可手工编辑；X 可自动同步。",
            "contacts": [c.to_json() for c in self._contacts],
        }
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    # -- 索引 -------------------------------------------------------------- #
    def _insert(self, contact: Contact) -> None:
        key = f"{contact.platform.value if contact.platform else 'any'}:{contact.id}"
        if key in self._by_id:
            existing = self._by_id[key]
            # 后写的覆盖先写的（同步数据 > 本地缓存 > 示例）
            self._contacts[self._contacts.index(existing)] = contact
        else:
            self._contacts.append(contact)
        self._by_id[key] = contact

    def add_many(self, contacts: Iterable[Contact], replace_platform: Platform | None = None) -> int:
        """批量写入。replace_platform 不为空时，先清掉该平台的旧数据。"""
        if replace_platform is not None:
            self._contacts = [c for c in self._contacts if c.platform is not replace_platform]
            self._by_id = {
                f"{c.platform.value if c.platform else 'any'}:{c.id}": c for c in self._contacts
            }
        added = 0
        for contact in contacts:
            before = len(self._contacts)
            self._insert(contact)
            if len(self._contacts) > before:
                added += 1
        self.save()
        self.emit("changed", len(self._contacts))
        return added

    def clear(self) -> None:
        self._contacts.clear()
        self._by_id.clear()
        self.save()
        self.emit("changed", 0)

    # -- 查询 -------------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._contacts)

    def all(self) -> list[Contact]:
        return list(self._contacts)

    def search(
        self,
        query: str,
        limit: int = 8,
        platforms: Sequence[Platform] | None = None,
    ) -> list[Contact]:
        """模糊搜索。空查询就返回列表前几个（按平台优先）。"""
        allowed = set(platforms) if platforms else None
        candidates = [
            c for c in self._contacts if allowed is None or c.platform is None or c.platform in allowed
        ]
        if not (query or "").strip():
            # 平台相关 + 有 handle 的排前面
            candidates.sort(
                key=lambda c: (
                    -(1 if c.platform in (allowed or set()) else 0),
                    0 if c.handle else 1,
                    c.name,
                )
            )
            return candidates[:limit]

        scored = [(c.score(query), c) for c in candidates]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [contact for _, contact in scored[:limit]]

    # -- 同步 -------------------------------------------------------------- #
    async def sync_from(self, publisher: Publisher) -> dict[Platform, int | str]:
        """从各平台拉关注列表。返回 {平台: 新增数 或 错误信息}。"""
        report: dict[Platform, int | str] = {}
        x = publisher.adapter(Platform.X)

        # 小红书：没有公开的关注列表接口
        report[Platform.XHS] = "小红书未提供关注列表接口，仅保留本地维护"

        try:
            following = await x.fetch_contacts()
        except Exception as exc:  # noqa: BLE001
            report[Platform.X] = f"失败：{exc}"
            return report

        contacts = [
            Contact(
                id=str(getattr(account, "id", "") or getattr(account, "username", "")),
                name=str(getattr(account, "name", "") or getattr(account, "username", "")),
                handle=str(getattr(account, "username", "")),
                platform=Platform.X,
                avatar_url=str(getattr(account, "profile_image_url", "") or ""),
                source="x",
            )
            for account in following
        ]
        added = self.add_many(contacts, replace_platform=Platform.X)
        report[Platform.X] = added
        return report

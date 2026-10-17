"""草稿自动保存 / 恢复。

存在 XDG state 目录（`~/.local/state/gnome-poster/draft.json`），
退出时写、启动时读。写之前先判空 —— 空草稿不该把上一次的内容冲掉。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Draft
from .settings import state_dir


class DraftStore:
    def __init__(self, filename: str = "draft.json") -> None:
        self.path: Path = state_dir() / filename

    def save(self, draft: Draft) -> bool:
        """保存。返回是否真的写了（空草稿不写）。"""
        empty = (
            not draft.title.strip()
            and not draft.body.strip()
            and not draft.media
            and not draft.tags
            and draft.poll is None
        )
        if empty:
            return False
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(draft.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            return False
        return True

    def load(self) -> Draft | None:
        if not self.path.exists():
            return None
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return Draft.from_json(data)
        except Exception:  # noqa: BLE001 - 坏草稿就当没有
            return None

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

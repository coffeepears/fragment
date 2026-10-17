"""智能标签引擎。

两路来源合并：
  * 本地：从正文抽关键词（jieba 有就用，没有就 n-gram 兜底），**永远可用、零延迟**
  * 平台：小红书的话题搜索接口（需要 web 通道 + 登录态），有热度信息

策略是「本地先出、远端再补」：界面点开就有东西，远端结果回来了再追加，
不会转圈等网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..models import Platform
from ..utils.text import extract_hashtags, extract_keywords, normalize
from .publisher import Publisher
from .settings import Settings


@dataclass(frozen=True)
class TagSuggestion:
    name: str
    score: float = 0.0
    source: str = "local"       # local | platform
    platform: Platform | None = None
    hot: int = 0                # 平台侧热度（有则显示）

    @property
    def key(self) -> str:
        return normalize(self.name)

    @property
    def display(self) -> str:
        return f"#{self.name}"


@dataclass
class TagBatch:
    """一次推荐结果。local 先给，remote 后到，UI 分两次渲染。"""

    local: list[TagSuggestion] = field(default_factory=list)
    remote: list[TagSuggestion] = field(default_factory=list)

    def merged(self, limit: int = 18) -> list[TagSuggestion]:
        seen: set[str] = set()
        out: list[TagSuggestion] = []
        # 远端优先（带热度、更贴合平台生态），同名的把本地那条挤掉
        for item in sorted(self.remote, key=lambda s: (-s.hot, -s.score)):
            if item.key in seen:
                continue
            seen.add(item.key)
            out.append(item)
        for item in self.local:
            if item.key in seen:
                continue
            seen.add(item.key)
            out.append(item)
        return out[:limit]


class TagEngine:
    def __init__(self, settings: Settings, publisher: Publisher) -> None:
        self._settings = settings
        self._publisher = publisher

    # -- 本地 -------------------------------------------------------------- #
    def local_suggestions(self, text: str, limit: int = 12) -> list[TagSuggestion]:
        """正文里已经写了的 #标签 优先，其次是抽出来的关键词。"""
        if not self._settings.get("auto-tags"):
            return []

        existing = extract_hashtags(text)
        tags: list[TagSuggestion] = [
            TagSuggestion(name=name, score=100.0, source="local") for name in existing
        ]
        seen = {tag.key for tag in tags}
        for index, word in enumerate(extract_keywords(text, limit=limit * 2)):
            key = normalize(word)
            if not key or key in seen:
                continue
            seen.add(key)
            tags.append(TagSuggestion(name=word, score=20.0 - index * 0.5, source="local"))
            if len(tags) >= limit:
                break
        return tags[:limit]

    def build_batch(self, text: str, limit: int = 18) -> TagBatch:
        return TagBatch(local=self.local_suggestions(text, limit))

    # -- 远端 -------------------------------------------------------------- #
    async def enrich(self, text: str, batch: TagBatch, platforms: Iterable[Platform]) -> TagBatch:
        """把平台侧话题补进 batch（原地改，返回同一个对象方便链式）。"""
        if not self._settings.get("auto-tags"):
            return batch
        wanted = [p for p in Platform.ordered() if p in set(platforms)]
        if not wanted:
            return batch

        collected = await self._publisher.collect_tags(text, wanted, limit=16)
        seen = {item.key for item in batch.local}
        for platform, names in collected.items():
            for index, raw in enumerate(names or ()):
                name = str(raw).strip().lstrip("#＃")
                if not name or normalize(name) in seen:
                    continue
                seen.add(normalize(name))
                batch.remote.append(
                    TagSuggestion(
                        name=name,
                        score=10.0 - index * 0.2,
                        source="platform",
                        platform=platform,
                        hot=max(0, 100 - index * 3),
                    )
                )
        batch.remote.sort(key=lambda s: (-s.hot, -s.score))
        return batch

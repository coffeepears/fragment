"""多平台并行分发。

一层的职责只有三件事：
  1. 按平台造适配器（谁是 X、谁是小红书）
  2. `asyncio.gather` 并行发，一个平台炸了不影响另一个
  3. 把进度打包成 ProgressEvent 往外吐（UI 层负责把它转成 Toast / 进度条）
"""

from __future__ import annotations

import asyncio
from typing import Callable, Iterable, Sequence

from ..models import Draft, Platform
from .base import (
    NotConfigured,
    PlatformAdapter,
    PlatformError,
    ProgressEvent,
    PublishResult,
)
from .secrets import SecretStore
from .settings import Settings
from .instagram_api import InstagramAdapter
from .x_api import XAdapter
from .xhs_api import XhsAdapter

EventCallback = Callable[[ProgressEvent], None]


def build_adapters(settings: Settings, secrets_store: SecretStore) -> dict[Platform, PlatformAdapter]:
    return {
        Platform.XHS: XhsAdapter(settings, secrets_store),
        Platform.X: XAdapter(settings, secrets_store),
        Platform.INSTAGRAM: InstagramAdapter(settings, secrets_store),
    }


class Publisher:
    def __init__(self, settings: Settings, secrets_store: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets_store
        self._adapters = build_adapters(settings, secrets_store)

    # -- 访问器 ------------------------------------------------------------ #
    def adapter(self, platform: Platform) -> PlatformAdapter:
        return self._adapters[platform]

    @property
    def adapters(self) -> dict[Platform, PlatformAdapter]:
        return dict(self._adapters)

    # -- 发布 -------------------------------------------------------------- #
    async def publish(
        self,
        draft: Draft,
        platforms: Iterable[Platform] | None = None,
        on_event: EventCallback | None = None,
    ) -> list[PublishResult]:
        """并行发布。永远返回结果列表，不抛异常（异常被收进 PublishResult）。"""
        targets = list(platforms if platforms is not None else draft.platforms)
        if not targets:
            raise PlatformError("没有选择任何平台")

        total = len(targets)
        done_counter = {"n": 0}

        async def run_one(platform: Platform, index: int) -> PublishResult:
            adapter = self._adapters[platform]

            def emit(ratio: float, message: str, stage: str = "") -> None:
                if on_event is None:
                    return
                # 每个平台占整条进度条的 1/total，且只在全部完成时才补满
                overall = (index + max(0.0, min(1.0, ratio))) / total
                on_event(
                    ProgressEvent(
                        platform=platform,
                        stage=stage or message,
                        message=message,
                        ratio=overall,
                    )
                )

            emit(0.0, "开始", stage="排队")
            try:
                result = await adapter.publish(draft, emit)
            except NotConfigured as exc:
                result = PublishResult.failure(platform, f"未配置：{exc}")
            except PlatformError as exc:
                result = PublishResult.failure(platform, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底：绝不静默
                result = PublishResult.failure(platform, f"意外错误：{exc.__class__.__name__}: {exc}")
            finally:
                done_counter["n"] += 1
                if on_event is not None:
                    on_event(
                        ProgressEvent(
                            platform=platform,
                            stage="完成" if result.ok else "失败",
                            message=result.error or "已发布",
                            ratio=done_counter["n"] / total,
                        )
                    )
            return result

        tasks = [asyncio.create_task(run_one(p, i)) for i, p in enumerate(targets)]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[PublishResult] = []
        for platform, item in zip(targets, gathered):
            if isinstance(item, BaseException):
                results.append(PublishResult.failure(platform, f"{item.__class__.__name__}: {item}"))
            else:
                results.append(item)
        return results

    # -- 辅助能力（供 @ 提及、标签引擎用） -------------------------------- #
    async def collect_contacts(self) -> dict[Platform, Sequence]:
        out: dict[Platform, Sequence] = {}
        for platform, adapter in self._adapters.items():
            try:
                out[platform] = await adapter.fetch_contacts()
            except Exception:  # noqa: BLE001
                out[platform] = ()
        return out

    async def collect_tags(self, text: str, platforms: Sequence[Platform], limit: int = 12):
        out: dict[Platform, Sequence] = {}
        for platform in platforms:
            adapter = self._adapters.get(platform)
            if adapter is None:
                continue
            try:
                out[platform] = await adapter.suggest_tags(text, limit)
            except Exception:  # noqa: BLE001
                out[platform] = ()
        return out

    async def status_report(self) -> dict[Platform, str]:
        out: dict[Platform, str] = {}
        for platform, adapter in self._adapters.items():
            probe = getattr(adapter, "check_account", None)
            if callable(probe):
                out[platform] = await probe()
            else:
                out[platform] = adapter.account_hint()
        return out

    async def aclose(self) -> None:
        await asyncio.gather(
            *(adapter.aclose() for adapter in self._adapters.values()), return_exceptions=True
        )

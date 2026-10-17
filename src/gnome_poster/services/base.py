"""平台适配器的公共契约。

所有平台实现都长这样：

    adapter = XAdapter(...)
    if not adapter.is_configured(): ...
    result = await adapter.publish(draft, on_progress=emit)

UI 只认 `PlatformAdapter`，加新平台（微博 / Mastodon / Bluesky）只需要写一个子类
再注册进 `publisher.py` 的工厂，界面代码一行都不用改。
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import httpx

from ..models import Draft, MediaItem, Platform

#: 进度回调签名：on_progress(ratio: float, message: str) -> None
ProgressCallback = Callable[[float, str], None]


# --------------------------------------------------------------------------- #
# 代理：从环境变量里挑 httpx **真能用**的那个
# --------------------------------------------------------------------------- #
#: httpx 认的代理 scheme。注意没有 `socks` —— 只有 socks5 / socks5h。
_HTTP_PROXY_SCHEMES = ("http", "https")
_SOCKS_PROXY_SCHEMES = ("socks5", "socks5h")


def _has_socks_support() -> bool:
    """httpx 走 SOCKS 需要额外的 socksio 包（Debian: python3-socksio）。

    用 find_spec 而不是 import：只要探测「在不在」，别真去导入。
    """
    import importlib.util

    try:
        return importlib.util.find_spec("socksio") is not None
    except (ImportError, ValueError):
        return False


def _normalize_proxy(raw: str | None) -> str | None:
    """把环境变量里的代理地址收拾成 httpx 能接受的写法。

    `socks://` 要改写成 `socks5://` —— httpx 不认前者；而后者还得有 socksio。
    认不出来的 scheme 一律返回 None（宁可不走代理，也别让 httpx 在建客户端的
    时候直接抛异常）。
    """
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"http://{url}"
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme == "socks":
        scheme = "socks5"
    if scheme in _HTTP_PROXY_SCHEMES:
        return f"{scheme}://{rest}"
    if scheme in _SOCKS_PROXY_SCHEMES:
        return f"{scheme}://{rest}" if _has_socks_support() else None
    return None


def proxy_mounts_from_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """挑出可用的代理，返回 httpx 的 mounts 映射（没有就返回空字典）。

    为什么非要有这个函数：本机（以及很多国内环境）会设

        ALL_PROXY=socks://127.0.0.1:7890/

    而 httpx 对它有两重意见：不认 `socks://` 这个写法，而且 `socks5://`
    还得额外装 socksio。结果是 httpx **在建客户端的时候**就抛

        Unknown scheme for proxy URL URL('socks://127.0.0.1:7890/')

    X 的登录、X 的发布、Instagram 的发布、小红书 Web 通道会全部当场失败，
    而报错信息完全看不出跟代理有什么关系（我第一次就栽在这上面）。

    同一个环境里 `http_proxy` / `https_proxy` 通常是好的 `http://` 地址，
    httpx 原生支持。所以这里优先用它们，只在只剩 SOCKS 时才规范化 + 依赖 socksio。
    """
    env = environ if environ is not None else os.environ

    def pick(name: str) -> str | None:
        for candidate in (name, name.upper(), name.lower()):
            value = env.get(candidate)
            if value:
                return value
        return None

    http = _normalize_proxy(pick("http_proxy"))
    https = _normalize_proxy(pick("https_proxy"))
    fallback = _normalize_proxy(pick("all_proxy")) or http or https

    mounts: dict[str, str] = {}
    if http or fallback:
        mounts["http://"] = http or fallback
    if https or fallback:
        mounts["https://"] = https or fallback
    return mounts


def proxied_client(**kwargs: Any) -> httpx.AsyncClient:
    """建一个「按环境变量走代理」的 httpx 客户端。

    一律 `trust_env=False`：代理已经由 `proxy_mounts_from_env()` 挑好并显式传入，
    不能再让 httpx 自己去读那份带 `socks://` 的环境变量，否则又会炸。

    注意本机的 127.0.0.1 服务（小红书那个本地发布服务）**不能**走代理，
    它那边是单独用 `trust_env=False` 且不带 mounts 的。
    """
    mounts = proxy_mounts_from_env()
    if not mounts:
        # 环境里没有可用代理 —— 直连
        return httpx.AsyncClient(trust_env=False, **kwargs)

    if len(set(mounts.values())) == 1:
        # http / https 共用同一个代理（本机就是这种），直接 proxy= 最省事
        kwargs.setdefault("proxy", next(iter(mounts.values())))
        return httpx.AsyncClient(trust_env=False, **kwargs)

    # 两种协议代理不同，才需要 mounts。注意 mounts 的值必须是 **transport 对象**，
    # 传字符串会在 aenter 时炸成 `'str' object has no attribute '__aenter__'`。
    kwargs.setdefault(
        "mounts",
        {
            pattern: httpx.AsyncHTTPTransport(proxy=url, trust_env=False)
            for pattern, url in mounts.items()
        },
    )
    return httpx.AsyncClient(trust_env=False, **kwargs)



# --------------------------------------------------------------------------- #
# 结果与事件
# --------------------------------------------------------------------------- #
@dataclass
class PublishResult:
    platform: Platform
    ok: bool
    post_id: str | None = None
    url: str | None = None
    error: str | None = None
    simulated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.ok:
            return f"{self.platform.label} 已发布"
        return f"{self.platform.label} 失败"

    @classmethod
    def success(
        cls,
        platform: Platform,
        post_id: str | None = None,
        url: str | None = None,
        simulated: bool = False,
        **extra: Any,
    ) -> "PublishResult":
        return cls(platform=platform, ok=True, post_id=post_id, url=url, simulated=simulated, extra=extra)

    @classmethod
    def failure(cls, platform: Platform, error: str, simulated: bool = False) -> "PublishResult":
        return cls(platform=platform, ok=False, error=error, simulated=simulated)


@dataclass
class ProgressEvent:
    platform: Platform
    stage: str          # 例如 "校验" / "上传 media" / "提交"
    message: str = ""
    ratio: float = 0.0  # 该平台整体进度 0..1


class PlatformError(Exception):
    """发布过程中的可预期错误（鉴权失败、限流、内容不合规……）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retryable = retryable
        self.payload = payload

    def __str__(self) -> str:  # pragma: no cover
        bits = [self.message]
        if self.status:
            bits.append(f"HTTP {self.status}")
        if self.code:
            bits.append(f"code={self.code}")
        return " · ".join(bits)


class NotConfigured(PlatformError):
    """还没登录 / 没填凭据。"""

    def __init__(self, message: str = "尚未配置账号") -> None:
        super().__init__(message, code="not_configured")


def network_error(exc: Exception, what: str = "请求") -> PlatformError:
    """把五花八门的网络异常翻译成一句人话。

    踩过的坑：本机 `ALL_PROXY=socks://127.0.0.1:7890`，httpx 在没有 socksio 的情况下
    会抛一句 `Unknown scheme for proxy URL`，直接冒到界面上用户完全看不懂。
    """
    text = str(exc)
    lowered = text.lower()

    if "socks" in lowered and "proxy" in lowered:
        return PlatformError(
            f"{what}失败：不支持 SOCKS 代理（{text}）。\n"
            "要么装上 socks 支持（pip install --user 'httpx[socks]'），"
            "要么临时清掉 ALL_PROXY / all_proxy 环境变量再启动。",
            code="proxy_unsupported",
        )
    if "proxy" in lowered:
        return PlatformError(f"{what}失败：代理配置有问题（{text}）", code="proxy")

    if isinstance(exc, (TimeoutError,)):
        return PlatformError(f"{what}超时", code="timeout", retryable=True)

    # httpx 的各类连接异常都归到这里，不再往 UI 上抛原始类名
    return PlatformError(f"{what}失败：{exc.__class__.__name__}: {exc}", retryable=True)


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class PlatformAdapter(abc.ABC):
    """一个平台的全部能力。"""

    platform: Platform

    @property
    def label(self) -> str:
        return self.platform.label

    # -- 状态 -------------------------------------------------------------- #
    @abc.abstractmethod
    def is_configured(self) -> bool:
        """凭据是否足够发起发布。"""

    def account_hint(self) -> str:
        """给设置页显示的一行账号信息。"""
        return "未登录" if not self.is_configured() else "已配置"

    # -- 能力协商 ---------------------------------------------------------- #
    def native_poll_available(self) -> bool:
        """投票能不能作为**原生组件**发出去（通道能力，不是平台有没有这功能）。

        默认 False：宁可降级成文字并明确告诉用户，也不假装能发。
        """
        return False

    # -- 发布 -------------------------------------------------------------- #
    @abc.abstractmethod
    async def publish(self, draft: Draft, on_progress: ProgressCallback | None = None) -> PublishResult:
        """把草稿发出去。异常请抛 PlatformError（不要吞）。"""

    async def upload_media(self, item: MediaItem, on_progress: ProgressCallback | None = None) -> str:
        """上传单个媒体，返回平台侧 id。默认不支持。"""
        raise NotConfigured(f"{self.label} 未实现媒体上传")

    # -- 辅助能力（都是可选的） -------------------------------------------- #
    async def fetch_contacts(self) -> Sequence[Any]:
        """拉取关注列表，供 @ 提及补全用。"""
        return ()

    async def suggest_tags(self, text: str, limit: int = 12) -> Sequence[Any]:
        """从平台侧拿话题推荐。"""
        return ()

    async def aclose(self) -> None:
        """释放连接。"""
        return None

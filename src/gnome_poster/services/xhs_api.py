"""Rednote发布适配器。

❗先把现实说清楚（别被网上的“开放平台”忽悠）：

  * Rednote**没有**面向个人开发者的公开内容发布 OpenAPI。企业号 / 服务商有「开放平台」，
    但需要企业资质 + 审核，个人拿不到 key。
  * 所以第三方工具只有两条路：
      ① 走**创作者平台 Web 接口**（creator.xiaohongshu.com），需要登录 Cookie
         以及请求头 `x-s` / `x-t` / `x-s-common` 签名（由一段混淆 JS 算出）。
      ② 走**本机浏览器自动化 / 现成的发布服务**（本项目推荐：xiaohongshu-mcp），
         把登录态和签名都交给它，我们只发一个 multipart 请求。

  本文件把两条路都实现了，用 `xhs-transport` 设置切换（默认 mcp）。
  路由和字段名都集中在各 transport 类里，平台一改版只动那一个类。

  任何一条路都只应发布你自己账号的内容，请遵守平台条款，别做自动化刷量。
"""

from __future__ import annotations

import abc
import asyncio
import base64
import binascii
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from .base import (
    NotConfigured,
    PlatformAdapter,
    PlatformError,
    ProgressCallback,
    PublishResult,
    network_error,
    proxied_client,
)
from ..models import Draft, MediaItem, Platform, Poll
from .secrets import KEY_XHS_COOKIES, KEY_XHS_MCP_TOKEN, SecretStore
from .settings import Settings
from .signer import NullSigner, Signer, build_signer

CREATOR_BASE = "https://creator.xiaohongshu.com"
EDITH_BASE = "https://edith.xiaohongshu.com"

#: 假装自己是浏览器——这些头缺一个都可能被风控
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": CREATOR_BASE,
    "Referer": f"{CREATOR_BASE}/publish/publish",
}

#: 小红书标题硬上限（一个字符算一个，emoji 也算一个）
XHS_TITLE_MAX = 20
XHS_BODY_MAX = 1000


# --------------------------------------------------------------------------- #
# transport 接口
# --------------------------------------------------------------------------- #
class XhsTransport(abc.ABC):
    name = "base"

    #: 这个通道能不能把投票作为**原生组件**发出去。
    #: 小红书的投票是 App 发布页里的一个组件，没有公开的写入接口，
    #: 所以默认是 False —— 能不能原生发完全取决于你用的通道。
    #: 用户在设置里打开「尝试原生投票组件」后，adapter 才会把它当 native。
    supports_native_poll = False

    @abc.abstractmethod
    async def publish(
        self,
        draft: Draft,
        on_progress: ProgressCallback | None = None,
        *,
        visibility: str | None = None,
        native_poll: bool = False,
    ) -> tuple[str | None, str | None]:
        """返回 (note_id, 分享链接)。"""

    async def check(self) -> str:
        """返回一句账号状态，供设置页显示。"""
        return "未知"

    async def suggest_tags(self, text: str, limit: int = 12) -> Sequence[str]:
        return ()

    async def aclose(self) -> None:
        return None


def poll_payload(poll: Poll) -> dict[str, object]:
    """投票 → 发布通道的字段（尽力而为的约定，不是官方文档）。

    本地发布服务 / 创作者平台如果认这几个键，就是原生投票；不认的话
    adapter 会走文字降级，用户也会在发布前看到提示。
    """
    return {
        "poll_question": poll.question.strip(),
        "poll_options": json.dumps(poll.cleaned_options(), ensure_ascii=False),
        "poll_duration_minutes": poll.duration_minutes,
    }


# --------------------------------------------------------------------------- #
# 通道一：本地 xiaohongshu-mcp（推荐）
# --------------------------------------------------------------------------- #
class XhsMcpTransport(XhsTransport):
    """对接本地发布服务（[xiaohongshu-mcp] 的 HTTP 接口）。

    为什么推荐它：登录态、签名、验证码这些脏活都由那个服务处理，
    我们只需要发几个 JSON 请求。

    接口（对着 v2.5.5 的 routes.go / service.go 抄的，不是猜的）：

      GET    /health
      GET    /api/v1/login/status    → {"success":true,"data":{"is_logged_in":bool,"username":str,"user_id":str}}
      GET    /api/v1/login/qrcode    → {"success":true,"data":{"timeout":"4m0s","is_logged_in":bool,"img":"data:image/png;base64,..."}}
      DELETE /api/v1/login/cookies
      POST   /api/v1/publish         → JSON {title, content, images[], tags[], ...}
      POST   /api/v1/publish_video   → JSON {title, content, video, tags[], ...}

    ⚠️ 两个当初写错、后来对着源码纠正的地方：

      1. `/api/v1/publish` 收的是 **JSON**，不是 multipart 文件流；
         `images` 传的是**本地绝对路径**（或 HTTP 链接），服务自己去读文件。
         原来发 multipart 会直接 400。
      2. 话题不用手工塞进正文 —— 服务有独立的 `tags` 数组，它自己会生成
         话题链接。所以这里用 `compose_body(..., embed_tags=False)`。

    投票：服务目前没有投票参数，所以 `supports_native_poll` 保持 False，
          投票会走正文文字降级（设置里那个开关留给以后支持的服务）。
    """

    name = "mcp"
    supports_native_poll = False

    def __init__(self, endpoint: str, token: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"Accept": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._http = httpx.AsyncClient(
                base_url=self.endpoint,
                headers=headers,
                # 发图片时服务要读本地文件，给它留足时间
                timeout=httpx.Timeout(300.0, connect=10.0),
                # 这是本机 127.0.0.1 上的服务，绝不该走系统代理：
                # 不然 ALL_PROXY=socks://… 会让 httpx 直接抛「Unknown scheme for proxy URL」
                trust_env=False,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- 请求封装 ---------------------------------------------------------- #
    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.endpoint}{path}"
        try:
            response = await self._client().request(
                method.upper(), url, json=json_body, timeout=timeout
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise PlatformError(
                f"连不上本机发布服务 {self.endpoint}：{exc}。\n"
                "小助手本该由应用自动启动。看看应用顶部有没有提示条，"
                "点一下「重试」；还不行就重启一下应用。"
            ) from exc

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"error": response.text[:300]}

        if response.status_code >= 400 or (
            isinstance(data, dict) and data.get("error") and not data.get("success")
        ):
            # 服务的错误体：{"error": "...", "code": "...", "details": "..."}
            message = "未知错误"
            if isinstance(data, dict):
                message = str(data.get("error") or data.get("message") or message)
                details = data.get("details")
                if details:
                    message = f"{message}（{details}）"
            raise PlatformError(
                message,
                code=str(data.get("code")) if isinstance(data, dict) and data.get("code") else None,
                status=response.status_code,
                payload=data,
            )
        return data

    @staticmethod
    def _payload(data: Any) -> Any:
        """统一剥掉 {"success":…, "data":…} 外壳。"""
        if isinstance(data, dict) and "data" in data:
            return data.get("data")
        return data

    # -- 状态 / 登录 ------------------------------------------------------- #
    async def check(self) -> str:
        data = await self._call("GET", "/api/v1/login/status")
        payload = self._payload(data)
        if isinstance(payload, dict):
            if payload.get("is_logged_in"):
                return f"已登录（{payload.get('username') or '未知账号'}）"
            return "服务在跑，但Rednote还没登录 —— 点「扫码登录」"
        return "服务在跑"

    async def login_qrcode(self) -> tuple[bytes | None, bool, str]:
        """拿登录二维码，返回 (PNG 字节, 是否已登录, 超时说明)。

        服务会为这次请求开一个浏览器页面、抓下二维码，并在后台等最多 4 分钟
        让人扫码（见它的 waitScanInBackground），所以这个请求会慢几秒。
        拿到图之后轮询 `check()` 就能知道扫没扫成功。

        `img` 可能是 `data:image/png;base64,...`，也可能是 http 链接 —— 两种都处理。
        """
        data = await self._call("GET", "/api/v1/login/qrcode", timeout=90.0)
        payload = self._payload(data)
        if not isinstance(payload, dict):
            raise PlatformError(f"二维码返回格式不对：{str(data)[:200]}")

        is_logged_in = bool(payload.get("is_logged_in"))
        timeout = str(payload.get("timeout") or "")
        img = str(payload.get("img") or "")
        if not img:
            return None, is_logged_in, timeout

        if img.startswith("data:"):
            _header, _, payload_b64 = img.partition(",")
            try:
                return base64.b64decode(payload_b64), is_logged_in, timeout
            except (ValueError, binascii.Error) as exc:
                raise PlatformError(f"二维码图片解不出来：{exc}") from exc
        if img.startswith("http"):
            try:
                response = await self._client().get(img)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                raise PlatformError(f"下载二维码失败：{exc}") from exc
            if response.status_code >= 400:
                raise PlatformError(f"下载二维码失败：HTTP {response.status_code}")
            return response.content, is_logged_in, timeout
        raise PlatformError(f"认不出这个二维码格式：{img[:80]}")

    async def logout(self) -> None:
        await self._call("DELETE", "/api/v1/login/cookies")

    # -- 发布 -------------------------------------------------------------- #
    @staticmethod
    def _media_arg(item: MediaItem) -> str:
        """服务要的是**绝对路径**（或 http 链接）。

        它的 FAQ 里有一条「确认图片路径中没有中文字符」，所以路径带非 ASCII
        字符时，先复制一份到临时目录用纯 ASCII 名，别让它栽在这上面。
        """
        path = Path(item.path).resolve()
        if not path.is_file():
            raise PlatformError(f"文件不存在：{path}")
        raw = str(path)
        if raw.isascii():
            return raw
        safe_dir = Path(tempfile.mkdtemp(prefix="gnome-poster-xhs-"))
        safe_name = f"media-{abs(hash(raw)) % 10**8}{path.suffix.lower()}"
        target = safe_dir / safe_name
        shutil.copy2(path, target)
        return str(target)

    async def publish(
        self,
        draft: Draft,
        on_progress: ProgressCallback | None = None,
        *,
        visibility: str | None = None,
        native_poll: bool = False,
    ) -> tuple[str | None, str | None]:
        def report(ratio: float, message: str) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, ratio)), message)

        title = draft.title.strip()[:XHS_TITLE_MAX]
        # 话题走服务自己的 tags 参数，正文里不要再塞一遍
        content = draft.compose_body(Platform.XHS, poll_as_text=not native_poll, embed_tags=False)
        if not title:
            raise PlatformError("Rednote笔记需要标题")
        if not draft.media:
            raise PlatformError("Rednote发笔记至少需要 1 张图或 1 个视频")

        tags = [t.strip().lstrip("#＃") for t in draft.tags if t.strip()]

        videos = [m for m in draft.media if m.is_video]
        images = [m for m in draft.media if not m.is_video]
        if videos and images:
            raise PlatformError("视频笔记不能混入图片，请二选一")

        if videos:
            report(0.1, "准备视频")
            video = self._media_arg(videos[0])
            body: dict[str, Any] = {"title": title, "content": content, "video": video}
            path = "/api/v1/publish_video"
        else:
            report(0.1, "准备图片")
            media = [self._media_arg(item) for item in images[:18]]
            body = {"title": title, "content": content, "images": media}
            path = "/api/v1/publish"

        if tags:
            body["tags"] = tags
        # 服务的 visibility：公开可见（默认）/ 仅自己可见 / 仅互关好友可见
        if visibility:
            body["visibility"] = visibility
        if native_poll and draft.poll is not None:
            # 目前服务不认这些字段，打开开关才发（认不认是它的事）
            body.update(poll_payload(draft.poll))

        report(0.35, "交给本地服务发布")
        data = await self._call("POST", path, json_body=body)
        report(1.0, "已发布")

        payload = self._payload(data)
        note_id = None
        if isinstance(payload, dict):
            note_id = payload.get("note_id") or payload.get("id") or payload.get("noteId")
        # 服务的响应里只有 title/content/images/status，没有笔记 ID，
        # 所以这里多半拿不到分享链接 —— 如实返回 None，不编一个假的。
        url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else None
        return (str(note_id) if note_id else None), url


# --------------------------------------------------------------------------- #
# 通道二：直连创作者平台（需要 Cookie + 签名器）
# --------------------------------------------------------------------------- #
class XhsWebTransport(XhsTransport):
    """直连 creator.xiaohongshu.com。

    ⚠️ 下面的 URL 与字段是社区实践中流传的形态，**平台随时可能改动**，
       第一次使用请先用「测试连接」验证 `check()`，再小号试发。
       需要：Cookie（至少 a1 / web_session）+ 签名器（x-s / x-t）。
    """

    name = "web"
    #: 创作者平台网页版**没有**投票组件（投票是 App 发布页才有的东西），
    #: 所以这里明确 False —— 打开开关也没用，会老实地走文字降级。
    supports_native_poll = False

    #: 上传凭证（拿到 uploadHost 和 fileId）
    PATH_PERMIT = "/api/media/v1/upload/web/permit"
    #: 发布图文 / 视频笔记
    PATH_NOTE = "/web_api/sns/v2/note"
    #: 话题搜索（做智能标签推荐用）
    PATH_TOPIC = "/web_api/sns/v1/search/topic"
    #: 当前登录用户
    PATH_SELF = "/api/sns/web/v1/user/selfinfo"

    def __init__(self, cookies: str, signer: Signer | None = None) -> None:
        self._cookies = parse_cookies(cookies)
        self._signer = signer or NullSigner()
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = proxied_client(
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=True,
                cookies=self._cookies,
                headers=WEB_HEADERS,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        await self._signer.aclose()

    @property
    def has_cookies(self) -> bool:
        return bool(self._cookies.get("a1") and self._cookies.get("web_session"))

    # -- 请求封装 ---------------------------------------------------------- #
    async def _post(self, base: str, path: str, payload: Mapping[str, Any]) -> Any:
        headers = await self._signer.sign("POST", path, payload, self._cookies)
        try:
            response = await self._client().post(f"{base}{path}", json=payload, headers=headers)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise network_error(exc, f"POST {path}") from exc
        return _json_or_raise(response, path)

    async def _get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        uri = path
        if params:
            from urllib.parse import urlencode

            uri = f"{path}?{urlencode(params)}"
        headers = await self._signer.sign("GET", uri, None, self._cookies)
        try:
            response = await self._client().get(f"{base}{uri}", headers=headers)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise network_error(exc, f"GET {path}") from exc
        return _json_or_raise(response, path)

    async def check(self) -> str:
        if not self.has_cookies:
            return "缺 Cookie（需要 a1 与 web_session）"
        data = await self._get(EDITH_BASE, self.PATH_SELF)
        nickname = _dig(data, ("data", "basic_info", "nickname")) or _dig(data, ("data", "nickname"))
        if nickname:
            return f"已登录（{nickname}）"
        return "Cookie 可能已过期"

    # -- 上传 -------------------------------------------------------------- #
    async def _upload_image(self, item: MediaItem) -> str:
        """拿上传凭证 → PUT 到对象存储 → 返回 file_id。"""
        params = {
            "biz_name": "spectrum",
            "scene": "image",
            "file_count": "1",
            "version": "1",
            "source": "web",
        }
        data = await self._get(CREATOR_BASE, self.PATH_PERMIT, params)
        permits = _dig(data, ("data", "uploadTempPermits")) or []
        host = _dig(data, ("data", "uploadHost"))
        if not permits or not host:
            raise PlatformError(f"拿上传凭证失败：{json.dumps(data, ensure_ascii=False)[:200]}")

        permit = permits[0]
        file_id = permit.get("fileId")
        token = permit.get("token")
        if not file_id or not token:
            raise PlatformError("上传凭证字段缺失（fileId / token）")

        raw = Path(item.path).read_bytes()
        headers = {
            "X-Cos-Security-Token": str(token),
            "Content-Type": item.mime,
            "Content-Length": str(len(raw)),
        }
        # 对象存储地址不在 API 域下，用绝对 URL
        response = await self._client().put(f"https://{host}/{file_id}", content=raw, headers=headers)
        if response.status_code >= 400:
            raise PlatformError(
                f"上传 {item.filename} 失败：HTTP {response.status_code} {response.text[:200]}",
                status=response.status_code,
                retryable=response.status_code >= 500,
            )
        return str(file_id)

    # -- 发布 -------------------------------------------------------------- #
    async def publish(
        self,
        draft: Draft,
        on_progress: ProgressCallback | None = None,
        *,
        visibility: str | None = None,
        native_poll: bool = False,
    ) -> tuple[str | None, str | None]:
        # 注：创作者平台 Web 接口的 payload 里没有 visibility 字段，
        # 这里接受参数但不发送（静默忽略，由适配器在需要时提示）。
        del visibility

        def report(ratio: float, message: str) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, ratio)), message)

        if not self.has_cookies:
            raise NotConfigured(
                "Rednote web 通道需要登录 Cookie（至少 a1 与 web_session）。"
                "在『设置 → 账号 → Rednote』里粘贴，或改用 mcp 通道。"
            )
        title = draft.title.strip()[:XHS_TITLE_MAX]
        desc = draft.compose_body(Platform.XHS, poll_as_text=not native_poll)
        if not title:
            raise PlatformError("Rednote笔记需要标题")
        if not draft.media:
            raise PlatformError("Rednote发笔记至少需要 1 张图或 1 个视频")

        videos = [m for m in draft.media if m.is_video]
        images = [m for m in draft.media if not m.is_video]
        if videos and images:
            raise PlatformError("视频笔记不能混入图片，请二选一")

        # 1) 上传媒体
        uploaded: list[str] = []
        targets = videos[:1] if videos else images[:18]
        for index, item in enumerate(targets):
            report(0.05 + 0.6 * index / max(1, len(targets)), f"上传 {item.filename}")
            uploaded.append(await self._upload_image(item))
        report(0.7, "提交笔记")

        # 2) 组装 payload
        payload: dict[str, Any] = {
            "title": title,
            "desc": desc,
            "type": "video" if videos else "normal",
            "note_id": "",
            "at_mentions": [],
            "hash_tags": [],
            "image_info": {"images": [{"file_id": fid} for fid in uploaded]},
        }
        if videos:
            payload["video_info"] = {"file_id": uploaded[0]}
        if native_poll and draft.poll is not None:
            # 尽力而为：创作者平台若认这个组件就成原生投票，不认则忽略
            payload["poll"] = poll_payload(draft.poll)

        data = await self._post(CREATOR_BASE, self.PATH_NOTE, payload)
        if not _dig(data, ("success",)) and _dig(data, ("code",)) not in (0, None):
            raise PlatformError(
                f"发布被拒：{_dig(data, ('msg',)) or _dig(data, ('message',)) or '未知原因'}",
                payload=data,
            )
        note_id = _dig(data, ("data", "id")) or _dig(data, ("data", "note_id"))
        report(1.0, "已发布")
        url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else None
        return (str(note_id) if note_id else None), url

    # -- 话题推荐 ---------------------------------------------------------- #
    async def suggest_tags(self, text: str, limit: int = 12) -> Sequence[str]:
        """用正文里的关键词去问平台的「话题」搜索接口。"""
        keywords = _seed_keywords(text)
        found: list[str] = []
        for keyword in keywords[:4]:
            try:
                data = await self._get(
                    CREATOR_BASE, self.PATH_TOPIC, {"keyword": keyword, "page": 1, "page_size": 20}
                )
            except PlatformError:
                continue
            for node in _dig(data, ("data", "topic_info_dtos")) or []:
                name = node.get("name") or node.get("topic_name")
                if name and name not in found:
                    found.append(str(name))
            if len(found) >= limit:
                break
        return found[:limit]


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class XhsAdapter(PlatformAdapter):
    platform = Platform.XHS

    def __init__(self, settings: Settings, secrets_store: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets_store
        self._transport: XhsTransport | None = None
        self._transport_key_cached: tuple | None = None
        #: 被换下来的旧 transport，统一在 aclose() 里关掉
        self._retired: list[XhsTransport] = []

    # -- transport 懒加载（**任何**相关设置改了都要重建） ------------------- #
    @property
    def transport_name(self) -> str:
        return str(self._settings.get("xhs-transport") or "mcp")

    def _transport_key(self) -> tuple:
        """决定 transport 要不要重建的「配置指纹」。

        ⚠️ 只比通道名是不够的 —— 原来就是这么写的，于是**改了服务地址之后
        点「测试连接」，问的还是旧地址**（httpx client 里 base_url 是建实例时
        定死的）。这个 bug 是写测试时偶然发现的：把地址指到死端口，结果它照样
        去问真服务。现在端口 / 签名器 / Cookie / Token 变了都会重建。
        """
        name = self.transport_name
        if name == "web":
            return (
                name,
                str(self._settings.get("xhs-signer-cmd") or ""),
                bool(self._secrets.lookup(KEY_XHS_COOKIES)),
            )
        return (
            name,
            str(self._settings.get("xhs-mcp-endpoint") or "http://127.0.0.1:18060"),
            bool(self._secrets.lookup(KEY_XHS_MCP_TOKEN)),
        )

    def transport(self) -> XhsTransport:
        key = self._transport_key()
        if self._transport is not None and self._transport_key_cached == key:
            return self._transport

        if self._transport is not None:
            # 旧的别直接丢：它可能还开着连接。留着，在 aclose() 里统一关。
            self._retired.append(self._transport)
            self._transport = None

        name = self.transport_name
        if name == "web":
            command = str(self._settings.get("xhs-signer-cmd") or "")
            self._transport = XhsWebTransport(
                self._secrets.lookup(KEY_XHS_COOKIES) or "", build_signer(command)
            )
        else:
            self._transport = XhsMcpTransport(
                str(self._settings.get("xhs-mcp-endpoint") or "http://127.0.0.1:18060"),
                self._secrets.lookup(KEY_XHS_MCP_TOKEN),
            )
        self._transport_key_cached = key
        return self._transport

    # -- 状态 -------------------------------------------------------------- #
    def is_configured(self) -> bool:
        if self.transport_name == "web":
            cookies = self._secrets.lookup(KEY_XHS_COOKIES) or ""
            parsed = parse_cookies(cookies)
            has_signer = bool(str(self._settings.get("xhs-signer-cmd") or "").strip())
            return bool(parsed.get("a1") and parsed.get("web_session") and has_signer)
        return True  # mcp 通道：能不能发取决于服务在不在，发布时才知道

    def account_hint(self) -> str:
        if self.transport_name == "web":
            if not (self._secrets.lookup(KEY_XHS_COOKIES) or "").strip():
                return "未填 Cookie"
            if not str(self._settings.get("xhs-signer-cmd") or "").strip():
                return "未配签名器"
            return "Cookie 已填（未验证）"
        return f"本地服务 {self._settings.get('xhs-mcp-endpoint')}"

    # -- 投票能力 ---------------------------------------------------------- #
    def native_poll_available(self) -> bool:
        """当前配置下，投票能不能作为原生组件发出去。

        需要两个条件同时成立：
          1. 用户在设置里打开了「尝试原生投票组件」
             （Rednote投票是 App 端组件，没有公开写入接口，默认不冒险）
          2. 通道本身声明支持（见各 transport 的 supports_native_poll）
        """
        if not bool(self._settings.get("xhs-native-poll")):
            return False
        try:
            return bool(self.transport().supports_native_poll)
        except Exception:  # noqa: BLE001
            return False

    # -- 发布 -------------------------------------------------------------- #
    async def publish(
        self, draft: Draft, on_progress: ProgressCallback | None = None
    ) -> PublishResult:
        def report(ratio: float, message: str) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, ratio)), message)

        if self._settings.simulate:
            return await self._simulate(draft, report)

        if not draft.title.strip():
            raise PlatformError("Rednote笔记需要标题")
        if not draft.media:
            raise PlatformError("Rednote发笔记至少需要 1 张图或 1 个视频")

        native = self.native_poll_available()
        visibility = str(self._settings.get("xhs-visibility") or "公开可见")
        try:
            note_id, url = await self.transport().publish(
                draft, report, visibility=visibility, native_poll=native
            )
        except NotConfigured:
            raise
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，别让发布线程静默死掉
            raise PlatformError(f"Rednote发布异常：{exc}") from exc
        return PublishResult.success(
            Platform.XHS,
            post_id=note_id,
            url=url,
            mode=self.transport_name,
            native_poll=native,
        )

    async def _simulate(self, draft: Draft, report: Callable[[float, str], None]) -> PublishResult:
        report(0.1, "校验内容")
        await asyncio.sleep(0.4)
        for index, item in enumerate(draft.media):
            report(0.15 + 0.5 * index / max(1, len(draft.media)), f"模拟上传 {item.filename}")
            await asyncio.sleep(0.3)
        report(0.8, "模拟提交")
        await asyncio.sleep(0.5)
        report(1.0, "已发布（演练）")
        return PublishResult.success(
            Platform.XHS,
            post_id="simulated",
            url="https://www.xiaohongshu.com/explore/simulated",
            simulated=True,
            mode=self.transport_name,
        )

    # -- 辅助 -------------------------------------------------------------- #
    async def suggest_tags(self, text: str, limit: int = 12) -> Sequence[str]:
        try:
            return await self.transport().suggest_tags(text, limit)
        except Exception:  # noqa: BLE001 - 推荐失败不能影响发帖
            return ()

    async def login_qrcode(self) -> tuple[bytes | None, bool, str]:
        """拿扫码登录的二维码（只有 mcp 通道支持）。返回 (PNG 字节, 是否已登录, 超时)。"""
        transport = self.transport()
        probe = getattr(transport, "login_qrcode", None)
        if not callable(probe):
            raise PlatformError("当前发布通道不支持扫码登录，请改用 MCP 通道")
        return await probe()

    async def logout(self) -> None:
        transport = self.transport()
        probe = getattr(transport, "logout", None)
        if callable(probe):
            await probe()

    async def check_account(self) -> str:
        if self._settings.simulate:
            return "演练模式"
        try:
            return await self.transport().check()
        except PlatformError as exc:
            return f"不可用：{exc}"

    async def aclose(self) -> None:
        for transport in [self._transport, *self._retired]:
            if transport is not None:
                await transport.aclose()
        self._transport = None
        self._retired.clear()


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def parse_cookies(raw: str) -> dict[str, str]:
    """吃三种格式：'a1=xxx; web_session=yyy' / JSON 对象 / 浏览器扩展导出的 JSON。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            if isinstance(data.get("cookies"), list):  # EditThisCookie 风格
                return {
                    str(c.get("name")): str(c.get("value"))
                    for c in data["cookies"]
                    if isinstance(c, dict) and c.get("name")
                }
            return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}
        return {}
    jar: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            jar[key.strip()] = value.strip()
    return jar


def _seed_keywords(text: str) -> list[str]:
    from ..utils.text import extract_keywords

    return extract_keywords(text, limit=6)


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    """安全地按路径取值，任何一层不是 dict / 不存在都返回 None。"""
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _json_or_raise(response: httpx.Response, what: str) -> Any:
    if response.status_code >= 400:
        raise PlatformError(
            f"{what} 失败：HTTP {response.status_code} {response.text[:200]}",
            status=response.status_code,
            retryable=response.status_code in (429, 500, 502, 503, 504),
        )
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlatformError(f"{what} 返回的不是 JSON：{response.text[:200]}") from exc


def host_of(url: str) -> str:
    return urlparse(url).netloc

"""Instagram 适配器 —— 走官方 Content Publishing API（Graph API）。

流程（官方文档口径，没有逆向成分）：

    图片：POST /{ig-user-id}/media      {image_url, caption}          → creation_id
    轮播：POST /{ig-user-id}/media      {image_url, is_carousel_item} → 每个子项一个 id
          POST /{ig-user-id}/media      {media_type: CAROUSEL, children: id1,id2, caption}
    视频：POST /{ig-user-id}/media      {media_type: REELS, video_url, caption} → 轮询状态
    发布：POST /{ig-user-id}/media_publish {creation_id}              → media_id
    链接：GET  /{media_id}?fields=permalink                          → 可分享链接

⚠️ 三个现实，代码里都处理了，用之前先知道：

1. **必须有企业号 / 创作者号**，并且绑定一个 Facebook 公共主页，
   再建 Meta 应用拿 `instagram_content_publish` 权限。个人号用不了。
2. **媒体必须是公网可访问的 URL** —— Graph API 只收 `image_url` / `video_url`，
   不接受本地文件上传。所以本地文件要靠 `_public_url()`：
     ① 媒体条目上直接填公开链接，或
     ② 在设置里配一个「图床上传命令」（stdin 收 JSON、stdout 吐 URL）
   两条都没有就明确报错，不假装能发。
3. **图片只吃 JPEG**。PNG / WebP 会用 gdk-pixbuf 自动转成临时 JPEG 再交给上传命令。

另外：Content Publishing 的额度是 25 条 / 24 小时。
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
from ..models import Draft, MediaItem, Platform
from .secrets import SecretStore
from .settings import Settings

#: Graph API 版本。Meta 大约每 3 个月推一版；出问题先看这里要不要抬。
GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

KEY_ACCESS_TOKEN = "instagram/access_token"
KEY_USERNAME = "instagram/username"

#: 轮播最多 10 个媒体
CAROUSEL_MAX = 10
#: 视频处理状态轮询
STATUS_POLL_SECONDS = 4.0
STATUS_MAX_POLLS = 45


# --------------------------------------------------------------------------- #
# 账号信息
# --------------------------------------------------------------------------- #
@dataclass
class InstagramAccount:
    id: str = ""
    username: str = ""

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else "未登录"


def _describe_graph_error(status: int, payload: Any) -> tuple[str, str | None]:
    """Graph API 的错误体长这样：
    {"error": {"message": ..., "type": ..., "code": 190, "error_user_msg": ...}}
    """
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("error_user_msg") or error.get("message") or "未知错误"
            code = error.get("code")
            subcode = error.get("error_subcode")
            bits = [str(message)]
            if code is not None:
                bits.append(f"code={code}")
            if subcode:
                bits.append(f"subcode={subcode}")
            return " · ".join(bits), str(code) if code is not None else None
    return (f"HTTP {status}", None)


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class InstagramClient:
    def __init__(self, settings: Settings, secrets_store: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets_store
        self._http: httpx.AsyncClient | None = None
        #: get_account() 顺手记下来的账号类型（BUSINESS / MEDIA_CREATOR / PERSONAL）
        self._account_type = ""

    # -- 配置 -------------------------------------------------------------- #
    @property
    def token(self) -> str:
        return (self._secrets.lookup(KEY_ACCESS_TOKEN) or "").strip()

    @property
    def user_id(self) -> str:
        return str(self._settings.get("instagram-user-id") or "").strip()

    @property
    def account_type(self) -> str:
        return self._account_type

    @property
    def upload_command(self) -> str:
        return str(self._settings.get("instagram-upload-cmd") or "").strip()

    def is_configured(self) -> bool:
        return bool(self.token and self.user_id)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = proxied_client(
                timeout=httpx.Timeout(120.0, connect=15.0),
                headers={"User-Agent": "gnome-poster/0.1"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def forget(self) -> None:
        self._secrets.clear_all((KEY_ACCESS_TOKEN, KEY_USERNAME))

    # -- 请求 -------------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        base: str = GRAPH_BASE,
    ) -> Any:
        if not self.token:
            raise NotConfigured("还没填 Instagram 访问令牌（设置 → 账号 → Instagram）")
        query = dict(params or {})
        query.setdefault("access_token", self.token)
        url = f"{base}{path}"
        try:
            response = await self._client().request(method.upper(), url, params=query, data=data)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise network_error(exc, f"{method.upper()} {path}") from exc
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"raw": response.text[:400]}
        if response.status_code >= 400:
            message, code = _describe_graph_error(response.status_code, payload)
            raise PlatformError(
                message,
                code=code,
                status=response.status_code,
                retryable=response.status_code in (429, 500, 502, 503, 504),
                payload=payload,
            )
        return payload

    # -- 账号 -------------------------------------------------------------- #
    async def get_account(self) -> InstagramAccount:
        if not self.user_id:
            raise NotConfigured("还没填 Instagram 用户 ID")
        payload = await self._request(
            "GET", f"/{self.user_id}", params={"fields": "id,username,account_type,media_count"}
        )
        account = InstagramAccount(
            id=str(payload.get("id", "")), username=str(payload.get("username", ""))
        )
        if account.username:
            self._secrets.store(KEY_USERNAME, account.username, "Instagram username")
        self._account_type = str(payload.get("account_type", ""))
        return account

    # ------------------------------------------------------------------ #
    # 媒体 → 公网 URL
    # ------------------------------------------------------------------ #
    def _ensure_jpeg(self, item: MediaItem) -> Path:
        """Instagram 的 image_url 只吃 JPEG；其它格式转成临时文件。"""
        source = Path(item.path)
        if item.is_video or source.suffix.lower() in (".jpg", ".jpeg"):
            return source
        try:
            import gi

            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(source))
            if pixbuf is None:
                raise PlatformError(f"读不出来：{source.name}")
            # 去掉 alpha（JPEG 不支持透明通道）
            if pixbuf.get_has_alpha():
                flat = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, *pixbuf.get_width_height())
                flat.fill(0xFFFFFFFF)
                pixbuf.composite(
                    flat, 0, 0, pixbuf.get_width(), pixbuf.get_height(), 0, 0, 1.0, 1.0,
                    GdkPixbuf.InterpType.BILINEAR, 255,
                )
                pixbuf = flat
            temp_dir = Path(tempfile.mkdtemp(prefix="gnome-poster-ig-"))
            target = temp_dir / (source.stem + ".jpg")
            pixbuf.savev(str(target), "jpeg", ["quality"], ["92"])
            return target
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"把 {source.name} 转成 JPEG 失败：{exc}") from exc

    async def _public_url(self, item: MediaItem) -> str:
        """拿到 Meta 服务器能抓到的 URL。三条路，都没有就诚实报错。"""
        if item.remote_url.strip():
            return item.remote_url.strip()

        command = self.upload_command
        if not command:
            raise NotConfigured(
                f"Instagram 只收公网 URL，本地文件它抓不到。请二选一：\n"
                f"① 在「{item.filename}」上填一个公开链接；\n"
                "② 在 设置 → 账号 → Instagram 里配一个「图床上传命令」，"
                "把本地文件传上去并返回 URL。"
            )

        prepared = self._ensure_jpeg(item)
        request = json.dumps(
            {"path": str(prepared), "original_path": item.path, "kind": item.kind},
            ensure_ascii=False,
        ).encode("utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise NotConfigured(f"找不到图床上传命令：{shlex.split(command)[0]}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(request), timeout=180)
        except asyncio.TimeoutError as exc:
            process.kill()
            raise PlatformError("图床上传超时（180s）") from exc

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:300]
            raise PlatformError(f"图床上传失败（返回码 {process.returncode}）：{detail or '无输出'}")

        raw = (stdout or b"").decode("utf-8", "replace").strip()
        if not raw:
            raise PlatformError("图床上传命令没有输出")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            # 也接受「直接吐一行 URL」的偷懒实现
            if raw.startswith("http"):
                return raw
            raise PlatformError(f"图床上传命令的输出不是 JSON 也不是 URL：{raw[:200]}") from exc

        for key in ("url", "image_url", "video_url", "link"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        if isinstance(payload.get("data"), dict):
            inner = payload["data"]
            for key in ("url", "image_url", "video_url"):
                value = inner.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
        raise PlatformError(f"图床上传命令的返回里没有 URL：{raw[:200]}")

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    async def _wait_ready(self, container_id: str, on_progress: ProgressCallback | None) -> None:
        """视频（Reels）要等 Meta 转码完才能 publish。"""
        for attempt in range(STATUS_MAX_POLLS):
            payload = await self._request(
                "GET", f"/{container_id}", params={"fields": "status_code,status"}
            )
            state = str(payload.get("status_code", "")).upper()
            if state == "FINISHED":
                return
            if state in ("ERROR", "EXPIRED"):
                raise PlatformError(
                    f"Instagram 处理媒体失败：{payload.get('status') or state}"
                )
            if on_progress is not None:
                on_progress(0.6 + 0.2 * attempt / STATUS_MAX_POLLS, f"Instagram 处理中（{state}）…")
            await asyncio.sleep(STATUS_POLL_SECONDS)
        raise PlatformError("Instagram 处理媒体超时")

    async def publish(
        self,
        caption: str,
        media: Sequence[MediaItem],
        on_progress: ProgressCallback | None = None,
    ) -> str:
        """返回 permalink。"""
        if not self.is_configured():
            raise NotConfigured("还没配置 Instagram（设置 → 账号 → Instagram）")
        if not media:
            raise PlatformError("Instagram 必须有至少一张图或一个视频")

        images = [m for m in media if not m.is_video][:CAROUSEL_MAX]
        videos = [m for m in media if m.is_video][:1]

        def report(ratio: float, message: str) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, ratio)), message)

        report(0.05, "解析媒体公网地址")
        if videos:
            item = videos[0]
            url = await self._public_url(item)
            report(0.2, "创建 Reel 容器")
            container = await self._request(
                "POST",
                f"/{self.user_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": url,
                    "caption": caption,
                    "share_to_feed": "true",
                },
            )
            creation_id = str(container.get("id", ""))
            if not creation_id:
                raise PlatformError("创建 Reel 容器没有返回 id", payload=container)
            await self._wait_ready(creation_id, report)
        elif len(images) == 1:
            url = await self._public_url(images[0])
            report(0.25, "创建图片容器")
            container = await self._request(
                "POST",
                f"/{self.user_id}/media",
                data={"image_url": url, "caption": caption},
            )
            creation_id = str(container.get("id", ""))
            if not creation_id:
                raise PlatformError("创建图片容器没有返回 id", payload=container)
        else:
            children: list[str] = []
            for index, item in enumerate(images):
                report(0.05 + 0.45 * index / len(images), f"上传第 {index + 1} 张")
                url = await self._public_url(item)
                child = await self._request(
                    "POST",
                    f"/{self.user_id}/media",
                    data={"image_url": url, "is_carousel_item": "true"},
                )
                child_id = str(child.get("id", ""))
                if not child_id:
                    raise PlatformError(f"第 {index + 1} 张创建容器失败", payload=child)
                children.append(child_id)
            report(0.6, "创建轮播容器")
            container = await self._request(
                "POST",
                f"/{self.user_id}/media",
                data={
                    "media_type": "CAROUSEL",
                    "children": ",".join(children),
                    "caption": caption,
                },
            )
            creation_id = str(container.get("id", ""))
            if not creation_id:
                raise PlatformError("创建轮播容器没有返回 id", payload=container)

        report(0.85, "发布")
        published = await self._request(
            "POST", f"/{self.user_id}/media_publish", data={"creation_id": creation_id}
        )
        media_id = str(published.get("id", ""))
        if not media_id:
            raise PlatformError("发布接口没有返回 media id", payload=published)

        report(0.95, "取链接")
        permalink = ""
        try:
            info = await self._request("GET", f"/{media_id}", params={"fields": "permalink"})
            permalink = str(info.get("permalink", "") or "")
        except PlatformError:
            pass
        report(1.0, "已发布")
        return permalink or "https://www.instagram.com/"


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class InstagramAdapter(PlatformAdapter):
    platform = Platform.INSTAGRAM

    def __init__(self, settings: Settings, secrets_store: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets_store
        self.client = InstagramClient(settings, secrets_store)

    # -- 状态 -------------------------------------------------------------- #
    def is_configured(self) -> bool:
        return self.client.is_configured()

    def account_hint(self) -> str:
        if not self.client.token:
            return "未填访问令牌"
        if not self.client.user_id:
            return "未填用户 ID"
        username = self._secrets.lookup(KEY_USERNAME)
        return f"@{username}" if username else "已配置（未验证）"

    async def check_account(self) -> str:
        if self._settings.simulate:
            return "演练模式"
        if not self.client.token:
            return "未填访问令牌"
        if not self.client.user_id:
            return "未填用户 ID"
        try:
            account = await self.client.get_account()
        except PlatformError as exc:
            return f"不可用：{exc}"
        account_type = self.client.account_type
        suffix = f"（{account_type}）" if account_type else ""
        return f"已连接 {account.label}{suffix}"

    # -- 发布 -------------------------------------------------------------- #
    async def publish(
        self, draft: Draft, on_progress: ProgressCallback | None = None
    ) -> PublishResult:
        def report(ratio: float, message: str) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, ratio)), message)

        if self._settings.simulate:
            return await self._simulate(draft, report)

        if not draft.media:
            raise PlatformError("Instagram 必须有至少一张图或视频")
        caption = draft.compose_body(Platform.INSTAGRAM)
        if self._secrets.lookup(KEY_USERNAME) is None and self.client.user_id:
            # 首次发布顺手把账号信息拉一次，好让设置页显示 @handle
            try:
                await self.client.get_account()
            except PlatformError:
                pass
        permalink = await self.client.publish(caption, draft.media, report)
        media_id = permalink.rstrip("/").split("/")[-1] if permalink else ""
        return PublishResult.success(Platform.INSTAGRAM, post_id=media_id, url=permalink)

    async def _simulate(self, draft: Draft, report: Callable[[float, str], None]) -> PublishResult:
        report(0.1, "校验内容")
        await asyncio.sleep(0.4)
        for index, item in enumerate(draft.media):
            report(0.15 + 0.6 * index / max(1, len(draft.media)), f"模拟上传 {item.filename}")
            await asyncio.sleep(0.3)
        report(0.85, "模拟发布")
        await asyncio.sleep(0.5)
        report(1.0, "已发布（演练）")
        return PublishResult.success(
            Platform.INSTAGRAM,
            post_id="simulated",
            url="https://www.instagram.com/",
            simulated=True,
        )

    # -- 辅助 -------------------------------------------------------------- #
    async def suggest_tags(self, text: str, limit: int = 12) -> Sequence[Any]:
        # Instagram 的 hashtag 没有官方推荐接口
        return ()

    async def aclose(self) -> None:
        await self.client.aclose()

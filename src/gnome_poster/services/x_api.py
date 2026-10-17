"""X (Twitter) API v2 客户端 + OAuth 2.0 PKCE 授权。

只依赖公开官方接口，没有逆向成分：
  * 授权：OAuth 2.0 Authorization Code + PKCE（Public Client），回调走本机 loopback
  * 发帖：POST /2/tweets （支持 media_ids 与原生 poll）
  * 媒体：POST /2/media/upload 的 INIT / APPEND / FINALIZE 三段式（OAuth2 用户上下文）
  * 关注：GET /2/users/{id}/following （给 @ 提及补全供数据）

⚠️ 媒体上传这一段（v2 media upload 的字段名与状态查询路径）平台一直在演进，
   第一次接通时建议先在设置里点「测试连接」并按返回的报错微调；
   代码里把所有端点都收在文件顶部的常量区，改起来只动一处。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import secrets as pysecrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse

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
from .secrets import (
    KEY_X_ACCESS_TOKEN,
    KEY_X_EXPIRES_AT,
    KEY_X_REFRESH_TOKEN,
    KEY_X_USER_ID,
    KEY_X_USERNAME,
    SecretStore,
)
from .settings import Settings

# --------------------------------------------------------------------------- #
# 端点（改版只需要动这里）
# --------------------------------------------------------------------------- #
AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API_BASE = "https://api.x.com/2"

DEFAULT_SCOPES: tuple[str, ...] = (
    "tweet.read",
    "tweet.write",
    "users.read",
    "follows.read",
    "offline.access",
    "media.write",
)

#: v2 media upload 单次 APPEND 的原始字节数（base64 后约 ×1.37，别太大）
#: 图片走单请求上传；超过这个大小（X 对图片的硬上限是 5MB）就走分段
SIMPLE_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
MEDIA_CHUNK_BYTES = 4 * 1024 * 1024
MEDIA_STATUS_POLL_SECONDS = 2.0
MEDIA_STATUS_MAX_POLLS = 60

USERNAME_FIELDS = "id,name,username,profile_image_url,description"


# --------------------------------------------------------------------------- #
# PKCE / 小工具
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。S256。"""
    verifier = _b64url(pysecrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


#: 够用的「看起来像链接」判断：http(s):// 或 www. 开头、或裸域名
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|org|net|cn|io|dev|me|co|xyz|app|tv)(?:/\S*)?",
    re.IGNORECASE,
)


def _looks_like_url(text: str) -> bool:
    """X 会因为帖子里有链接而按「Create with URL」计价（贵 13 倍）。"""
    return bool(_URL_RE.search(text or ""))


def _describe_x_error(status: int, payload: Any) -> tuple[str, str | None]:
    """把 X 的各种错误体揉成一句人话 + 一个错误码。"""
    if isinstance(payload, dict):
        # OAuth 错误体
        if "error" in payload:
            code = str(payload.get("error"))
            desc = payload.get("error_description") or ""
            return (f"{code}{'：' + desc if desc else ''}", code)
        # 新版 problem+json
        if "detail" in payload or "title" in payload:
            detail = payload.get("detail") or payload.get("title") or ""
            code = payload.get("type")
            return (str(detail), str(code) if code else None)
        # 老式 errors 数组
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or first.get("detail") or errors[0])
            code = first.get("code")
            return (message, str(code) if code is not None else None)
    return (json.dumps(payload, ensure_ascii=False)[:300] if payload else f"HTTP {status}", None)


# --------------------------------------------------------------------------- #
# Loopback 回调服务器
# --------------------------------------------------------------------------- #
class LoopbackReceiver:
    """一个只服务一次的本地 HTTP 服务器，用来接 OAuth 回调。

    redirect_uri 的 host/port/path 必须和开发者后台登记的完全一致，所以这里
    从配置的 redirect_uri 里解析出端口，而不是自己乱挑。
    """

    def __init__(self, redirect_uri: str) -> None:
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in ("http", "https"):
            raise PlatformError(f"回调地址必须是 http(s)：{redirect_uri}")
        host = parsed.hostname or "127.0.0.1"
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise PlatformError("Loopback 回调必须指向本机（127.0.0.1 / localhost）")
        self.host = host
        self.port = parsed.port or 80
        self.path = parsed.path or "/callback"
        self._params: dict[str, str] | None = None
        self._event = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> None:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # 浏览器（尤其 Firefox）会对这个端口做**预连接**：连上但不发数据。
            # 没有超时的话，处理线程会永久卡在「读请求行」上；而 HTTPServer 是
            # 单线程的，于是后面所有连接（包括真正的 OAuth 回调）全部饿死 ——
            # 表现就是浏览器「无法连接」/ 一直转圈，而端口明明在 LISTEN。
            timeout = 15

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
                parsed = urlparse(self.path)
                if parsed.path != receiver.path:
                    self.send_error(404, "Not Found")
                    return
                receiver._params = dict(parse_qsl(parsed.query))
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>GnomePoster</title>"
                    "<style>body{font-family:system-ui,sans-serif;display:flex;"
                    "align-items:center;justify-content:center;height:100vh;margin:0;"
                    "background:#1c1c1e;color:#eee}div{text-align:center}"
                    "p{opacity:.7;font-size:14px}</style>"
                    "<div><h2>授权完成 ✅</h2><p>可以把这个标签页关掉了，"
                    "回到 Fragment 继续。</p></div>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                # 回完立刻断，别在这个连接上等下一个请求（HTTP/1.1 默认 keep-alive）
                self.close_connection = True
                receiver._event.set()

            def log_message(self, *_args: Any) -> None:  # 别往 stderr 喷日志
                return

        # 监听地址要跟着回调地址里的主机名走：
        # 写 `localhost` 的话，浏览器可能先解析到 ::1（IPv6）；
        # 我们只绑 127.0.0.1 就会「连不上」。所以按顺序都试一遍。
        if self.host == "localhost":
            candidates = ["::1", "127.0.0.1"]
        elif self.host == "::1":
            candidates = ["::1"]
        else:
            candidates = ["127.0.0.1"]

        last_error: OSError | None = None
        for bind_host in candidates:
            family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET

            class _Server(ThreadingHTTPServer):
                address_family = family
                # 每个连接一个线程：一个卡住（预连接、慢客户端）不影响其他连接。
                # daemon_threads 让 server_close() 不等这些线程，退出不会被拖住。
                daemon_threads = True
                allow_reuse_address = True

            try:
                self._server = _Server((bind_host, self.port), Handler)
                self.bound_host = bind_host
                break
            except OSError as exc:
                last_error = exc
        if self._server is None:
            raise PlatformError(
                f"本地端口 {self.port} 起不来（{last_error}）。"
                "换个端口，或把占用端口的程序关掉再试。"
            )
        self._thread = threading.Thread(target=self._server.serve_forever, name="x-oauth-cb", daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 600.0) -> dict[str, str]:
        if not self._event.wait(timeout):
            raise PlatformError(
                "授权超时：等了 10 分钟没等到 X 的回调。"
                "通常是还没走完网页上的授权，或者浏览器把回调地址拦住了。再点一次「登录 X」就行。"
            )
        return self._params or {}

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    def __enter__(self) -> "LoopbackReceiver":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
@dataclass
class XAccount:
    id: str = ""
    name: str = ""
    username: str = ""

    @property
    def label(self) -> str:
        if not self.username:
            return "未登录"
        return f"@{self.username}" + (f"（{self.name}）" if self.name else "")


class XClient:
    def __init__(self, settings: Settings, secrets_store: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets_store
        self._http: httpx.AsyncClient | None = None
        self._refresh_lock = asyncio.Lock()
        self._account: XAccount | None = None

    # -- 基础 -------------------------------------------------------------- #
    @property
    def client_id(self) -> str:
        return str(self._settings.get("x-client-id")).strip()

    @property
    def redirect_uri(self) -> str:
        return str(self._settings.get("x-redirect-uri")).strip()

    @property
    def client_secret(self) -> str:
        return (self._secrets.lookup("x/client_secret") or "").strip()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            # 必须走代理（国内直连不了 api.x.com），而环境里的 ALL_PROXY
            # 是 socks://… 会让 httpx 直接抛 Unknown scheme —— 用 proxied_client
            # 挑出真能用的那个（详见 base.proxy_mounts_from_env 的注释）。
            self._http = proxied_client(
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=False,
                headers={"User-Agent": "gnome-poster/0.1 (+https://example.org/gnome-poster)"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- 令牌 -------------------------------------------------------------- #
    @property
    def access_token(self) -> str | None:
        return self._secrets.lookup(KEY_X_ACCESS_TOKEN)

    @property
    def refresh_token(self) -> str | None:
        return self._secrets.lookup(KEY_X_REFRESH_TOKEN)

    def _expires_at(self) -> float:
        raw = self._secrets.lookup(KEY_X_EXPIRES_AT)
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def is_authorized(self) -> bool:
        return bool(self.access_token and self.client_id)

    def _store_tokens(self, payload: Mapping[str, Any]) -> None:
        token = payload.get("access_token")
        if not token:
            raise PlatformError("令牌响应里没有 access_token", payload=payload)
        self._secrets.store(KEY_X_ACCESS_TOKEN, str(token), "X access token")
        if payload.get("refresh_token"):
            self._secrets.store(KEY_X_REFRESH_TOKEN, str(payload["refresh_token"]), "X refresh token")
        expires_in = payload.get("expires_in")
        if expires_in:
            import time

            self._secrets.store(
                KEY_X_EXPIRES_AT, str(time.time() + float(expires_in)), "X token expiry"
            )

    async def _ensure_token(self) -> str:
        if not self.client_id:
            raise NotConfigured("还没填 X 的 Client ID（设置 → 账号 → X）")
        token = self.access_token
        import time

        if token and self._expires_at() - 60 > time.time():
            return token
        if not self.refresh_token:
            if token:  # 老令牌，可能还能用（比如没返回 expires_in）
                return token
            raise NotConfigured("还没登录 X，请先在设置里点『登录 X』")
        async with self._refresh_lock:
            # 二次检查：可能别的协程刚刷新完
            fresh = self.access_token
            if fresh and self._expires_at() - 60 > time.time():
                return fresh
            return await self._refresh()

    async def _refresh(self) -> str:
        assert self.refresh_token
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        response = await self._client().post(
            TOKEN_URL,
            data=data,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = _maybe_json(response)
        if response.status_code >= 400:
            message, code = _describe_x_error(response.status_code, payload)
            raise PlatformError(f"刷新 X 令牌失败：{message}", code=code, status=response.status_code)
        self._store_tokens(payload)
        return str(payload["access_token"])

    # -- 授权流程 ---------------------------------------------------------- #
    def build_authorize_url(self, verifier: str, state: str) -> str:
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(DEFAULT_SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def authorize(self, open_browser: Callable[[str], None], timeout: float = 600.0) -> XAccount:
        """跑完整的 PKCE 流程。open_browser 由调用方提供（Gtk.UriLauncher）。"""
        if not self.client_id:
            raise NotConfigured("请先填写 X 的 Client ID")

        verifier, _ = make_pkce_pair()
        state = _b64url(pysecrets.token_bytes(16))
        url = self.build_authorize_url(verifier, state)

        receiver = LoopbackReceiver(self.redirect_uri)
        receiver.start()
        try:
            open_browser(url)
            loop = asyncio.get_running_loop()
            params = await loop.run_in_executor(None, receiver.wait, timeout)
        finally:
            receiver.stop()

        if params.get("state") != state:
            raise PlatformError("授权回调的 state 不匹配，出于安全已中止")
        if "error" in params:
            raise PlatformError(
                f"授权被拒绝：{params.get('error')} {params.get('error_description', '')}".strip()
            )
        code = params.get("code")
        if not code:
            raise PlatformError("回调里没有 code")

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
            "client_id": self.client_id,
        }
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        response = await self._client().post(
            TOKEN_URL,
            data=payload,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = _maybe_json(response)
        if response.status_code >= 400:
            message, error_code = _describe_x_error(response.status_code, body)
            raise PlatformError(f"换取令牌失败：{message}", code=error_code, status=response.status_code)
        self._store_tokens(body)
        return await self.get_me()

    def forget(self) -> None:
        self._secrets.clear_all(
            (KEY_X_ACCESS_TOKEN, KEY_X_REFRESH_TOKEN, KEY_X_EXPIRES_AT, KEY_X_USER_ID, KEY_X_USERNAME)
        )
        self._account = None

    # -- 通用请求 ---------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {await self._ensure_token()}"

        url = path if path.startswith("http") else f"{API_BASE}{path}"
        try:
            response = await self._client().request(
                method.upper(), url, json=json_body, params=params, headers=headers
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            # 代理配置错误 / DNS / 连接超时都走这里，翻译成人话再往上抛
            raise network_error(exc, f"{method.upper()} {path}") from exc
        payload = _maybe_json(response)
        if response.status_code >= 400:
            message, code = _describe_x_error(response.status_code, payload)
            retryable = response.status_code in (429, 500, 502, 503, 504)
            raise PlatformError(
                message,
                code=code,
                status=response.status_code,
                retryable=retryable,
                payload=payload,
            )
        return payload

    # -- 账号 -------------------------------------------------------------- #
    async def get_me(self) -> XAccount:
        data = await self._request("GET", "/users/me", params={"user.fields": USERNAME_FIELDS})
        node = (data or {}).get("data") or {}
        account = XAccount(
            id=str(node.get("id", "")),
            name=str(node.get("name", "")),
            username=str(node.get("username", "")),
        )
        if account.id:
            self._secrets.store(KEY_X_USER_ID, account.id, "X user id")
            self._secrets.store(KEY_X_USERNAME, account.username, "X username")
        self._account = account
        return account

    async def cached_account(self) -> XAccount | None:
        if self._account is not None:
            return self._account
        user_id = self._secrets.lookup(KEY_X_USER_ID)
        username = self._secrets.lookup(KEY_X_USERNAME)
        if user_id or username:
            self._account = XAccount(id=user_id or "", username=username or "")
        return self._account

    async def get_following(self, user_id: str | None = None, limit: int = 800) -> list[XAccount]:
        if user_id is None:
            account = await self.cached_account()
            if account is None or not account.id:
                account = await self.get_me()
            user_id = account.id
        if not user_id:
            raise PlatformError("拿不到自己的 user id")

        found: list[XAccount] = []
        token: str | None = None
        page_size = min(1000, max(1, limit))
        while len(found) < limit:
            params: dict[str, Any] = {
                "max_results": page_size,
                "user.fields": USERNAME_FIELDS,
            }
            if token:
                params["pagination_token"] = token
            data = await self._request("GET", f"/users/{user_id}/following", params=params)
            for node in (data or {}).get("data") or []:
                found.append(
                    XAccount(
                        id=str(node.get("id", "")),
                        name=str(node.get("name", "")),
                        username=str(node.get("username", "")),
                    )
                )
                if len(found) >= limit:
                    break
            token = ((data or {}).get("meta") or {}).get("next_token")
            if not token:
                break
        return found

    # -- 媒体 -------------------------------------------------------------- #
    async def _upload_chunked(
        self,
        raw: bytes,
        item: MediaItem,
        category: str,
        path: Path,
        on_progress: ProgressCallback | None,
    ) -> str:
        """视频 / 大文件的分段上传：initialize → append(*) → finalize → 轮询状态。"""
        init = await self._request(
            "POST",
            "/media/upload/initialize",
            json_body={
                "media_type": item.mime,
                "total_bytes": len(raw),
                "media_category": category,
            },
        )
        media_id = str(((init or {}).get("data") or {}).get("id") or "")
        if not media_id:
            raise PlatformError("媒体 INIT 没有返回 id", payload=init)

        total_chunks = max(1, math.ceil(len(raw) / MEDIA_CHUNK_BYTES))
        for index in range(total_chunks):
            chunk = raw[index * MEDIA_CHUNK_BYTES : (index + 1) * MEDIA_CHUNK_BYTES]
            await self._request(
                "POST",
                f"/media/upload/{media_id}/append",
                json_body={
                    "segment_index": index,
                    "media": base64.b64encode(chunk).decode("ascii"),
                },
            )
            if on_progress is not None:
                on_progress(
                    (index + 1) / total_chunks * 0.7,
                    f"上传 {path.name}（{index + 1}/{total_chunks}）",
                )

        finalized = await self._request("POST", f"/media/upload/{media_id}/finalize")
        processing = (((finalized or {}).get("data") or {}).get("processing_info")) or {}

        if on_progress is not None:
            on_progress(0.8, f"{path.name} 服务端处理中…")

        for _ in range(MEDIA_STATUS_MAX_POLLS):
            state = processing.get("state")
            if state in (None, "succeeded"):
                break
            if state == "failed":
                reason = ((processing.get("error") or {}).get("message")) or "未知原因"
                raise PlatformError(f"{path.name} 处理失败：{reason}")
            await asyncio.sleep(MEDIA_STATUS_POLL_SECONDS)
            status = await self._request("GET", "/media/upload", params={"media_id": media_id})
            processing = ((status or {}).get("data") or {}).get("processing_info") or {}
        else:
            raise PlatformError(f"{path.name} 处理超时")
        return media_id

    async def upload_media(
        self, item: MediaItem, on_progress: ProgressCallback | None = None
    ) -> str:
        """v2 media upload：INIT → APPEND(*n) → FINALIZE →（视频）轮询处理状态。"""
        path = Path(item.path)
        if not path.is_file():
            raise PlatformError(f"文件不存在：{path}")
        raw = path.read_bytes()
        if not raw:
            raise PlatformError(f"文件是空的：{path.name}")

        category = "tweet_video" if item.is_video else "tweet_image"

        # 两条路（端点名字照官方文档抄，别凭记忆）：
        #   图片 → 单请求 POST /2/media/upload {media: base64, media_category}
        #   视频 → 分段 POST /2/media/upload/initialize → /{id}/append → /{id}/finalize
        #
        # 🩸 原来 INIT 打的是 `/media/upload`，那是**另一个端点**（单请求上传，
        #    要求 body 里带 `media` 字段），于是收到一个很误导的 400：
        #    「Missing media field in JSON」。加上 `initialize` 才对。
        #    两个端点都设了 additionalProperties: false，多塞字段也会 400。
        if item.is_video or len(raw) > SIMPLE_UPLOAD_MAX_BYTES:
            media_id = await self._upload_chunked(raw, item, category, path, on_progress)
        else:
            if on_progress is not None:
                on_progress(0.4, f"上传 {path.name}")
            simple = await self._request(
                "POST",
                "/media/upload",
                json_body={
                    "media": base64.b64encode(raw).decode("ascii"),
                    "media_category": category,
                },
            )
            media_id = str(((simple or {}).get("data") or {}).get("id") or "")
            if not media_id:
                raise PlatformError("媒体上传没有返回 id", payload=simple)

        if item.alt_text.strip():
            try:
                await self._request(
                    "POST",
                    "/media/metadata",
                    json_body={"id": media_id, "metadata": {"alt_text": {"text": item.alt_text[:1000]}}},
                )
            except PlatformError:
                # alt text 失败不该让整条帖子挂掉
                pass

        if on_progress is not None:
            on_progress(1.0, f"{path.name} 上传完成")
        return media_id

    # -- 发帖 -------------------------------------------------------------- #
    async def create_tweet(
        self,
        text: str,
        *,
        media_ids: Sequence[str] = (),
        poll: Poll | None = None,
        reply_settings: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": list(media_ids)}
        if poll is not None:
            ok, reason = poll.is_valid()
            if not ok:
                raise PlatformError(f"投票不合法：{reason}")
            payload["poll"] = {
                "options": poll.cleaned_options(),
                "duration_minutes": int(poll.duration_minutes),
            }
        if reply_settings:
            payload["reply_settings"] = reply_settings

        data = await self._request("POST", "/tweets", json_body=payload)
        return (data or {}).get("data") or {}

    async def delete_tweet(self, tweet_id: str) -> bool:
        """删掉一条推文，返回是否真的删了。

        DELETE /2/tweets/:id 属于 `tweet.write` 权限，我们申请的 scope 已经涵盖。

        为什么要它：**测试发出去的推文必须能自己收尾** —— 不能只留垃圾在用户账号上。
        用户在应用里想撤掉刚发的内容也用得上。
        """
        if not tweet_id:
            raise PlatformError("没有推文 ID，删不了")
        data = await self._request("DELETE", f"/tweets/{tweet_id}")
        return bool(((data or {}).get("data") or {}).get("deleted"))


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class XAdapter(PlatformAdapter):
    platform = Platform.X

    def __init__(self, settings: Settings, secrets_store: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets_store
        self.client = XClient(settings, secrets_store)

    # -- 状态 -------------------------------------------------------------- #
    def is_configured(self) -> bool:
        return self.client.is_authorized()

    def native_poll_available(self) -> bool:
        # X 的投票是官方 v2 接口的一等字段，永远可用
        return True

    def account_hint(self) -> str:
        if not self.client.client_id:
            return "未填 Client ID"
        if not self.client.access_token:
            return "未登录"
        username = self._secrets.lookup(KEY_X_USERNAME)
        return f"@{username}" if username else "已登录"

    async def check_account(self) -> str:
        """给设置页的「测试连接」用。

        别的平台一直有这个方法，X 漏了 —— 于是 X 那栏没法验证登录，
        `_test_platform()` 只能退回去显示 `account_hint()`（纯本地猜测，
        令牌其实早就失效了也看不出来）。这里**真的打一次 /users/me**。
        """
        if self._settings.simulate:
            return "演练模式"
        if not self.client.client_id:
            return "还没填 Client ID"
        if not self.client.access_token:
            return "还没登录"
        try:
            account = await self.client.get_me()
        except PlatformError as exc:
            return f"不可用：{exc}"
        if not account.id:
            return "已登录（但 X 没返回账号信息）"
        return f"已登录 {account.label}"

    # -- 发帖 -------------------------------------------------------------- #
    async def publish(
        self, draft: Draft, on_progress: ProgressCallback | None = None
    ) -> PublishResult:
        def report(ratio: float, message: str) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, ratio)), message)

        if self._settings.simulate:
            return await self._simulate(draft, report)

        if not self.is_configured():
            raise NotConfigured("还没登录 X（设置 → 账号 → X）")

        report(0.02, "校验内容")
        text = draft.compose_body(Platform.X)
        if not text.strip():
            raise PlatformError("正文是空的")

        # X 的按量计费里，**含链接的帖子是普通帖的 13 倍**：
        #   Post: Create             $0.015 / 次
        #   Post: Create (with URL)  $0.200 / 次
        # 不说一声就替人花 13 倍的钱不合适，所以这里明确报出来。
        if _looks_like_url(text) and on_progress is not None:
            on_progress(
                0.03,
                "⚠️ 这条含链接：X 收 $0.20（普通帖 $0.015，贵 13 倍）",
            )

        # 1) 媒体：X 单条最多 4 图，或 1 视频
        media_items = list(draft.media)
        videos = [m for m in media_items if m.is_video][:1]
        images = [m for m in media_items if not m.is_video][:4]
        selected = videos if videos else images

        media_ids: list[str] = []
        span = 0.7 / max(1, len(selected))
        for index, item in enumerate(selected):
            base = 0.05 + span * index

            def sub(ratio: float, message: str, _base: float = base, _span: float = span) -> None:
                report(_base + ratio * _span, message)

            media_ids.append(await self.client.upload_media(item, sub))

        # 2) 发帖
        report(0.8, "提交帖子")
        node = await self.client.create_tweet(text, media_ids=media_ids, poll=draft.poll)
        post_id = str(node.get("id", ""))
        username = self._secrets.lookup(KEY_X_USERNAME) or "i"
        url = f"https://x.com/{username}/status/{post_id}" if post_id else None
        report(1.0, "已发布")
        return PublishResult.success(Platform.X, post_id=post_id, url=url)

    async def _simulate(self, draft: Draft, report: Callable[[float, str], None]) -> PublishResult:
        """演练模式：走完流程但不发请求，方便调界面。"""
        report(0.1, "校验内容")
        await asyncio.sleep(0.4)
        for index, item in enumerate(draft.media):
            for step in range(1, 4):
                report(0.15 + 0.5 * index / max(1, len(draft.media)) + step * 0.05, f"模拟上传 {item.filename}")
                await asyncio.sleep(0.25)
        report(0.8, "模拟提交")
        await asyncio.sleep(0.5)
        report(1.0, "已发布（演练）")
        return PublishResult.success(
            Platform.X, post_id="simulated", url="https://x.com/i/web/status/simulated", simulated=True
        )

    # -- 辅助 -------------------------------------------------------------- #
    async def fetch_contacts(self) -> Sequence[XAccount]:
        account = await self.client.cached_account()
        if account is None or not account.id:
            account = await self.client.get_me()
        return await self.client.get_following(account.id)

    async def suggest_tags(self, text: str, limit: int = 12) -> Sequence[Any]:
        # X 没有公开的话题推荐接口，标签只能靠本地抽取。
        return ()

    async def aclose(self) -> None:
        await self.client.aclose()


def _maybe_json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return {"raw": response.text[:500]}


async def verify_media_upload(item: MediaItem, settings: Settings, secrets_store: SecretStore) -> str:
    """调试用：只跑媒体上传，验证端点/字段是否还对得上。"""
    client = XClient(settings, secrets_store)
    try:
        return await client.upload_media(item)
    finally:
        await client.aclose()

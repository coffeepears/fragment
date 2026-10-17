"""小红书 Web 通道的请求签名器。

小红书（以及大部分国内平台）的 Web 接口要求请求头里带 `x-s` / `x-t`，
它们由一段混淆过的 JS 用页面里的 `window._webmsxyw` 算出来。纯 Python 重写
这段算法既违法平台意愿、也极度脆弱，所以这里做成**可插拔的外部签名器**：

    xhs-signer-cmd = "node /path/to/xhs_sign.js"

协议非常简单：父进程把 JSON 从 stdin 塞进去，签名器把结果 JSON 从 stdout 吐出来。

    → {"method":"POST","uri":"/web_api/sns/v2/note","payload":{...},"cookies":{"a1":"..."}}
    ← {"x-s":"XYS_...","x-t":"1712345678901","x-s-common":"..."}

自己实现签名器的最省事路线：把浏览器里能跑通的那段 JS 抽出来，用 node 包一层
（`tools/xhs_sign.js` 里留了骨架和注释）。或者干脆用小红的 MCP 通道，把签名交给它。
"""

from __future__ import annotations

import abc
import asyncio
import json
import shlex
from typing import Any, Mapping

from .base import NotConfigured, PlatformError


class Signer(abc.ABC):
    @abc.abstractmethod
    async def sign(
        self,
        method: str,
        uri: str,
        payload: Mapping[str, Any] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """返回需要塞进请求头的字典（至少包含 x-s / x-t）。"""

    async def aclose(self) -> None:
        return None


class NullSigner(Signer):
    """没配签名器时用它，报错信息直接告诉用户怎么办。"""

    async def sign(self, method, uri, payload=None, cookies=None) -> dict[str, str]:
        raise NotConfigured(
            "小红书 web 通道需要签名器：请在『设置 → 账号 → 小红书』里填写签名器命令，"
            "或把发布通道改成 mcp。"
        )


class CommandSigner(Signer):
    """把签名交给外部进程（node / python / 任何可执行文件）。"""

    def __init__(self, command: str, timeout: float = 15.0) -> None:
        if not command.strip():
            raise ValueError("签名器命令为空")
        self.command = shlex.split(command)
        self.timeout = timeout

    async def sign(
        self,
        method: str,
        uri: str,
        payload: Mapping[str, Any] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        request = {
            "method": method.upper(),
            "uri": uri,
            "payload": dict(payload or {}),
            "cookies": dict(cookies or {}),
        }
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")

        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise NotConfigured(f"找不到签名器可执行文件：{self.command[0]}") from exc
        except OSError as exc:
            raise PlatformError(f"签名器启动失败：{exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(body), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise PlatformError(f"签名器超时（{self.timeout:.0f}s）") from exc

        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:400]
            raise PlatformError(f"签名器返回码 {proc.returncode}：{detail or '（无输出）'}")

        raw = (stdout or b"").decode("utf-8", "replace").strip()
        if not raw:
            raise PlatformError("签名器没有输出")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlatformError(f"签名器输出不是合法 JSON：{raw[:200]}") from exc

        # 容忍几种常见包装：{"x-s":...} / {"headers":{...}} / {"data":{...}}
        for wrapper in ("headers", "data", "result"):
            inner = data.get(wrapper)
            if isinstance(inner, dict):
                data = inner
                break

        headers = {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, (str, int))}
        if "x-s" not in headers or "x-t" not in headers:
            raise PlatformError(f"签名器输出缺少 x-s / x-t：{sorted(headers)}")
        return headers


def build_signer(command: str) -> Signer:
    return CommandSigner(command) if command.strip() else NullSigner()

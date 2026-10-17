"""本机小红书发布服务的生命周期管理。

## 为什么需要这个模块

发布服务（xiaohongshu-mcp）不是「可选的第三方依赖」—— 对小红书来说它就是后端。
但第一版把它当外部依赖处理：让用户自己去 GitHub 下载二进制、自己写 systemd 单元、
自己 `systemctl enable`。对「只想发小红书」的人来说，这纯粹是白折腾。

现在二进制随 deb 一起安装（见 `vendor/xhs-service/`），app 负责把它拉起来：

  1. 已经在跑（用户自己的 systemd 服务 / 手动起的）→ 直接用，不动它
  2. 没在跑 → 先试 `systemctl --user start`（装过用户单元的话最快）
  3. 还不行 → 直接 spawn 打包进来的二进制；app 退出时只收掉**自己起的那个**

## 一个改不了的约束

服务首次启动会自己下载一个**打过反检测补丁的 Chromium**（压缩约 150MB，
展开约 700MB）。塞不进 deb，也没法用系统 chromium —— 它给浏览器传了自定义的
指纹 flag，官方注释写得很清楚：「宁可不启动，也不启动一个不对的浏览器」。
所以第一次会慢几分钟，之后有缓存就快。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

#: 服务默认监听的本机地址
DEFAULT_ENDPOINT = "http://127.0.0.1:18060"

#: 服务的工作目录。**必须稳定**：它的 cookies（登录态）就落在这里的
#: cookies.json，换目录等于让用户重新扫码。
def default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "xiaohongshu-mcp"


#: 可能的安装位置，按优先级排。开发态（源码树）在最前面，装成 deb 后在 libexec。
_CANDIDATE_DIRS = (
    "libexec/gnome-poster",
    "lib/gnome-poster",
)


def _binary_candidates() -> list[Path]:
    import sysconfig

    paths: list[Path] = []

    # 1) 源码树直接跑：<project>/vendor/xhs-service/xiaohongshu-mcp
    here = Path(__file__).resolve()
    if len(here.parents) >= 4:
        paths.append(here.parents[3] / "vendor" / "xhs-service" / "xiaohongshu-mcp")

    # 2) 装成 deb：<libexecdir>/gnome-poster/xiaohongshu-mcp
    libexec = sysconfig.get_path("data")  # 通常是 /usr
    for rel in _CANDIDATE_DIRS:
        paths.append(Path(libexec) / rel / "xiaohongshu-mcp")

    # 3) 兜底：常见绝对路径（Arch/自编译等）
    for base in ("/usr/libexec", "/usr/lib/x86_64-linux-gnu", "/usr/lib"):
        paths.append(Path(base) / "gnome-poster" / "xiaohongshu-mcp")

    # 4) 用户自己装的（早先版本就是手动装到 ~/.local/bin）
    paths.append(Path.home() / ".local" / "bin" / "xiaohongshu-mcp")

    return paths


def find_bundled_binary() -> Path | None:
    """找到可执行的服务二进制，找不到返回 None。"""
    for path in _binary_candidates():
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None


class XhsServiceManager:
    """拉起 / 停掉本机发布服务。"""

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        #: 端口从 endpoint 里取，这样测试可以在备用端口上验证「自己拉起」
        #: 这条路径，不会碰到用户正在用的那个服务。
        parsed = urlparse(self.endpoint)
        self.port = parsed.port or 18060
        self.state_dir = default_state_dir()
        self._child: subprocess.Popen | None = None
        #: 自己起的进程的进程组号，用来连浏览器一起收掉
        self._child_pgid: int | None = None

    # -- 探测 -------------------------------------------------------------- #
    async def _healthy(self, timeout: float = 2.0) -> bool:
        """问一下 /health。这个地址是本机的，**绝不能走代理**。"""
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                response = await client.get(f"{self.endpoint}/health")
                return response.status_code == 200
        except (httpx.HTTPError, ValueError, OSError):
            return False

    async def _wait_healthy(self, seconds: float, report=None, hint: str = "") -> bool:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self._healthy():
                return True
            if report is not None:
                report(hint or "正在启动发布服务…")
            await asyncio.sleep(1.0)
        return False

    # -- 启动 -------------------------------------------------------------- #
    async def ensure_running(self, report=None) -> tuple[bool, str]:
        """确保服务可用。返回 (是否可用, 一句人话说明)。

        `report` 是可选的进度回调（str -> None），会被频繁调用；
        首次下载浏览器时可能要调好几分钟。
        """
        # 1) 已经有人跑起来了？（用户自己的 systemd 服务 / 手动起的）
        if await self._healthy():
            return True, "发布服务已在运行"

        # 2) 试试用户自己装了 systemd 单元的情况（本机就是这么起的）
        if shutil.which("systemctl"):
            try:
                started = await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "--user", "start", "xiaohongshu-mcp.service"],
                    capture_output=True,
                    timeout=15,
                )
                if started.returncode == 0 and await self._wait_healthy(10.0, report):
                    return True, "发布服务已启动（systemd 用户服务）"
            except (OSError, subprocess.SubprocessError):
                pass

        # 3) 还不行就自己起打包进来的那个
        binary = find_bundled_binary()
        if binary is None:
            return False, (
                "找不到发布服务二进制。装的是 deb 的话应该是环境有问题；"
                "从源码跑的话确认 vendor/xhs-service/ 下有 xiaohongshu-mcp。"
            )
        if self._child is not None and self._child.poll() is None:
            # 已经在起了，等它
            if await self._wait_healthy(20.0, report):
                return True, "发布服务已启动"
            return False, "发布服务启动超时"

        self.state_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.state_dir / "service.log"
        try:
            log_file = open(log_path, "ab")  # noqa: SIM115 - 要交给子进程长期持有
        except OSError:
            log_file = subprocess.DEVNULL  # type: ignore[assignment]

        try:
            self._child = subprocess.Popen(
                [str(binary), "-headless=true", "-port", f":{self.port}"],
                cwd=str(self.state_dir),
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
                # 独立进程组：退出时能连它拉起的浏览器一起收掉
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"发布服务启动失败：{exc}"
        finally:
            if hasattr(log_file, "close"):
                log_file.close()

        try:
            self._child_pgid = os.getpgid(self._child.pid)
        except OSError:
            self._child_pgid = None

        # 首次启动要下浏览器（约 150MB），给足时间
        if await self._wait_healthy(
            600.0,
            report,
            "首次启动：正在下载内置浏览器（约 150MB），需要几分钟…",
        ):
            return True, "发布服务已启动"
        return False, f"发布服务启动超时，看日志：{log_path}"

    # -- 停止 -------------------------------------------------------------- #
    def stop(self) -> None:
        """只收掉**自己起的**那个进程（含它拉起的浏览器）。"""
        child = self._child
        self._child = None
        if child is None or child.poll() is not None:
            self._child_pgid = None
            return

        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if self._child_pgid is not None:
                    os.killpg(self._child_pgid, sig)
                else:
                    child.send_signal(sig)
            except (OSError, ProcessLookupError):
                pass
            try:
                child.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        self._child_pgid = None

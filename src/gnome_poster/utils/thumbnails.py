"""缩略图：图片走 gdk-pixbuf，视频走 GStreamer 抽一帧。

注意三点：
1. 这些函数是阻塞的，必须在工作线程调用（ThumbnailLoader 已经处理）。
2. 视频抽帧一定带超时，坏文件不能把线程挂死。
3. 任何失败都返回 None，由 UI 退化成图标占位，不弹错误。
"""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gst, GstApp  # noqa: E402

__all__ = ["load_thumbnail", "ThumbnailLoader"]

_GST_READY = False


def _ensure_gst() -> bool:
    global _GST_READY
    if _GST_READY:
        return True
    try:
        if not Gst.is_initialized():
            Gst.init(None)
        _GST_READY = True
    except Exception:  # noqa: BLE001
        _GST_READY = False
    return _GST_READY


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #
def load_image_thumbnail(path: str, size: int = 512) -> Gdk.Texture | None:
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
    except GLib.Error:
        return None
    except Exception:  # noqa: BLE001
        return None
    if pixbuf is None:
        return None
    return Gdk.Texture.new_for_pixbuf(pixbuf)


# --------------------------------------------------------------------------- #
# 视频
# --------------------------------------------------------------------------- #
def _structure_int(structure, field: str, default: int = 0) -> int:
    """Gst.Structure 取值在不同 PyGObject 版本里返回 GValue 或 Python int，统一一下。"""
    try:
        value = structure.get_value(field)
    except Exception:  # noqa: BLE001
        return default
    if value is None:
        return default
    for attr in ("get_int", "get_uint", "get_uint64"):
        getter = getattr(value, attr, None)
        if callable(getter):
            try:
                return int(getter())
            except Exception:  # noqa: BLE001
                continue
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return default


def _pull_sample(appsink, timeout_ns: int):
    """appsink 的 Python 包装有时是 Gst.Element 而不是 GstApp.AppSink，两条路都试。"""
    puller = getattr(appsink, "try_pull_sample", None)
    if callable(puller):
        return puller(timeout_ns)
    return GstApp.AppSink.try_pull_sample(appsink, timeout_ns)


def load_video_thumbnail(path: str, size: int = 512, timeout_ns: int | None = None) -> Gdk.Texture | None:
    """解出第一帧可用画面。用 appsink + 固定次数 try_pull_sample，绝不无限等。"""
    if not _ensure_gst():
        return None
    if timeout_ns is None:
        timeout_ns = Gst.SECOND  # 每次拉样本最多等 1 秒

    pipeline = None
    try:
        pipeline = Gst.Pipeline.new(None)

        filesrc = Gst.ElementFactory.make("filesrc", None)
        decodebin = Gst.ElementFactory.make("decodebin", None)
        videoconvert = Gst.ElementFactory.make("videoconvert", None)
        videoscale = Gst.ElementFactory.make("videoscale", None)
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        appsink = Gst.ElementFactory.make("appsink", None)
        for element in (filesrc, decodebin, videoconvert, videoscale, capsfilter, appsink):
            if element is None:
                return None
            pipeline.add(element)

        filesrc.set_property("location", str(Path(path).resolve()))
        # 只取 RGB，直接喂给 Gdk.MemoryTexture，绕开 GstVideo 的色彩空间转换
        caps = Gst.Caps.from_string(
            f"video/x-raw,format=RGB,pixel-aspect-ratio=1/1,width={size}"
        )
        capsfilter.set_property("caps", caps)
        appsink.set_property("max-buffers", 2)
        appsink.set_property("drop", True)
        appsink.set_property("sync", False)
        appsink.set_property("emit-signals", False)

        filesrc.link(decodebin)
        videoconvert.link(videoscale)
        videoscale.link(capsfilter)
        capsfilter.link(appsink)

        def _on_pad_added(_bin, pad):  # decodebin 是动态 pad
            try:
                caps_now = pad.get_current_caps() or pad.query_caps(None)
                name = caps_now.get_structure(0).get_name()
            except Exception:  # noqa: BLE001
                return
            if not name.startswith("video/"):
                return
            sink_pad = videoconvert.get_static_pad("sink")
            if sink_pad is not None and not sink_pad.is_linked():
                pad.link(sink_pad)

        decodebin.connect("pad-added", _on_pad_added)

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return None

        sample = None
        for _ in range(40):  # 最多等 ~2.5 秒（首帧可能关键帧较远）
            pulled = _pull_sample(appsink, timeout_ns)
            if pulled is None:
                # None 可能是超时也可能是 EOS，看一下 bus
                msg = pipeline.get_bus().pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS
                )
                if msg is not None:
                    break
                continue
            sample = pulled
            break

        if sample is None:
            return None

        buffer = sample.get_buffer()
        caps_out = sample.get_caps()
        if buffer is None or caps_out is None or caps_out.get_size() == 0:
            return None
        structure = caps_out.get_structure(0)
        width = _structure_int(structure, "width")
        height = _structure_int(structure, "height")
        if width <= 0 or height <= 0:
            return None

        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            data = bytes(info.data)
        finally:
            buffer.unmap(info)

        stride = width * 3
        expected = stride * height
        if len(data) < expected:
            return None
        return Gdk.MemoryTexture.new(
            width,
            height,
            Gdk.MemoryFormat.R8G8B8,
            GLib.Bytes.new(data[:expected]),
            stride,
        )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return None
    finally:
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                pass


def load_thumbnail(path: str, kind: str = "image", size: int = 512) -> Gdk.Texture | None:
    if kind == "video":
        return load_video_thumbnail(path, size)
    return load_image_thumbnail(path, size)


# --------------------------------------------------------------------------- #
# 异步加载器
# --------------------------------------------------------------------------- #
class ThumbnailLoader:
    """小线程池 + 去重 + 结果派回主线程。

    callback(key, texture) 在主线程被调用；texture 为 None 表示加载失败。
    """

    def __init__(self, workers: int = 2, size: int = 512) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumb")
        self._inflight: set[str] = set()
        self._size = size

    def request(
        self,
        key: str,
        path: str,
        kind: str,
        callback: Callable[[str, object], None],
        size: int | None = None,
    ) -> None:
        if key in self._inflight:
            return
        self._inflight.add(key)
        future = self._pool.submit(load_thumbnail, path, kind, size or self._size)

        def _done(fut) -> None:
            self._inflight.discard(key)
            try:
                texture = fut.result()
            except Exception:  # noqa: BLE001
                texture = None
            GLib.idle_add(_deliver, callback, key, texture)

        future.add_done_callback(_done)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _deliver(callback: Callable[[str, object], None], key: str, texture: object) -> bool:
    try:
        callback(key, texture)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return False

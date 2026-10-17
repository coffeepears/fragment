"""用量圆环 —— GNOME 风格的小进度环。

用法：

    ring = UsageRing(size=22)
    ring.set_usage(0.42)            # 42%，正常色
    ring.set_usage(0.95, "warn")    # 快满了
    ring.set_usage(1.2, "error")    # 超了（画满 + 红色）

为什么用自绘而不是 `Gtk.LevelBar`：LevelBar 是条状的，塞在复选框右边不协调；
GNOME 自己的界面（设置里的存储占用、Files 的容量环）用的就是这种细圆环。

颜色不写死 —— 靠 CSS 的 `color` + `Gtk.Widget.get_color()`，
所以深色 / 浅色 / 高对比主题都会自动跟着走。
"""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

try:
    import cairo

    _LINE_CAP_ROUND = cairo.LINE_CAP_ROUND
except ImportError:  # pragma: no cover - pycairo 一般都在
    _LINE_CAP_ROUND = 1  # cairo.LINE_CAP_ROUND 的数值

LEVELS = ("ok", "warn", "error")

#: 兜底调色板（Adwaita 官方值）。只有在 CSS 取不到有效颜色时才用 ——
#: 见下面的 `_resolve_color`：某些 GTK + libadwaita 组合会把颜色变量
#: 解析成越界垃圾值，那时候宁可退回固定色也不能画出随机色。
#: 索引是 level，值是 (浅色, 深色)。
FALLBACK_COLORS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "ok": ((0.208, 0.518, 0.894), (0.384, 0.627, 0.918)),      # #3584e4 / #62a0ea
    "warn": ((0.898, 0.647, 0.039), (0.804, 0.576, 0.035)),    # #e5a50a / #cd9309
    "error": ((0.878, 0.106, 0.141), (0.753, 0.110, 0.157)),   # #e01b24 / #c01c28
}


class UsageRing(Gtk.DrawingArea):
    """一个细圆环：底圈 + 按比例画的进度弧。"""

    __gtype_name__ = "GPUsageRing"

    def __init__(self, size: int = 22, thickness: float = 3.0) -> None:
        super().__init__()
        self._size = max(12, int(size))
        self._thickness = float(thickness)
        self._fraction = 0.0
        self._level = "ok"

        self.set_content_width(self._size)
        self.set_content_height(self._size)
        self.set_valign(Gtk.Align.CENTER)
        self.add_css_class("usage-ring")
        self.set_draw_func(self._draw, None)
        self._apply_level()

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    @property
    def fraction(self) -> float:
        return self._fraction

    @property
    def level(self) -> str:
        return self._level

    def set_usage(self, fraction: float, level: str | None = None) -> None:
        """fraction 可以 > 1（超限）；level 不给就按 fraction 自动判。"""
        fraction = max(0.0, float(fraction))
        if level is None:
            level = level_for(fraction)
        changed = (abs(fraction - self._fraction) > 1e-4) or (level != self._level)
        self._fraction = fraction
        self._level = level if level in LEVELS else "ok"
        self._apply_level()
        if changed:
            self.queue_draw()

    def reset(self) -> None:
        self.set_usage(0.0, "ok")

    def _apply_level(self) -> None:
        for name in LEVELS:
            self.remove_css_class(f"ring-{name}")
        self.add_css_class(f"ring-{self._level}")

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def _resolve_color(self):
        """拿到 CSS 的 color，取不到就退回固定调色板。

        两道防线：
          1. 控件还没经过样式计算时 `get_color()` 读的是未初始化内存（能读出负 RGB）；
          2. 某些 GTK + libadwaita 组合会把 `color: var(--xxx-color)` 解析成越界值。
        两种情况下都必须是「退回调色板」，绝不能把垃圾值喂给 Cairo ——
        否则圆环会画出随机颜色。
        """
        from gi.repository import Gdk

        try:
            color = self.get_color()  # GTK 4.10+
        except Exception:  # noqa: BLE001
            color = None
        if color is not None and all(
            0.0 <= value <= 1.0 for value in (color.red, color.green, color.blue, color.alpha)
        ):
            return color

        dark = False
        try:
            import gi as _gi

            _gi.require_version("Adw", "1")
            from gi.repository import Adw

            dark = bool(Adw.StyleManager.get_default().get_dark())
        except Exception:  # noqa: BLE001
            dark = False
        light, dark_rgb = FALLBACK_COLORS.get(self._level, FALLBACK_COLORS["ok"])
        red, green, blue = dark_rgb if dark else light
        fallback = Gdk.RGBA()
        fallback.red, fallback.green, fallback.blue, fallback.alpha = red, green, blue, 1.0
        return fallback

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int, _data) -> None:
        if width <= 0 or height <= 0:
            return
        color = self._resolve_color()

        center_x, center_y = width / 2.0, height / 2.0
        radius = max(1.0, min(width, height) / 2.0 - self._thickness / 2.0 - 0.5)

        cr.set_line_width(self._thickness)
        cr.set_line_cap(_LINE_CAP_ROUND)

        # 底圈：同色但很淡，这样深浅主题都不用单独配色
        cr.set_source_rgba(color.red, color.green, color.blue, 0.18)
        cr.arc(center_x, center_y, radius, 0.0, 2.0 * math.pi)
        cr.stroke()

        # 进度弧：从 12 点方向顺时针
        if self._fraction <= 0.0:
            return
        sweep = 2.0 * math.pi * min(self._fraction, 1.0)
        start = -math.pi / 2.0
        if sweep >= 2.0 * math.pi - 1e-6:
            cr.set_source_rgba(color.red, color.green, color.blue, 1.0)
            cr.arc(center_x, center_y, radius, 0.0, 2.0 * math.pi)
            cr.stroke()
            return
        cr.set_source_rgba(color.red, color.green, color.blue, 1.0)
        cr.arc(center_x, center_y, radius, start, start + sweep)
        cr.stroke()


def level_for(fraction: float) -> str:
    """按占用比例给个级别，和界面上的文字提示口径一致。"""
    if fraction > 1.0:
        return "error"
    if fraction > 0.9:
        return "warn"
    return "ok"


def worst_fraction(*pairs: tuple[float, int]) -> tuple[float, str]:
    """给一组 (当前值, 上限) 求「最紧的那个」占比与级别。上限 <= 0 的忽略。"""
    ratios = [used / limit for used, limit in pairs if limit and limit > 0]
    if not ratios:
        return 0.0, "ok"
    fraction = max(ratios)
    return fraction, level_for(fraction)

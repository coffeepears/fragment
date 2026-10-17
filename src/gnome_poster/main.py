"""命令行入口。

    python3 -m gnome_poster.main             正常启动
    python3 -m gnome_poster.main --check     只做环境 / 依赖自检，不开窗口（可无显示器）
    python3 -m gnome_poster.main --self-test 起窗口、塞示例内容、渲染快照后退出
    python3 -m gnome_poster.main --exercise  交互级冒烟测试：模拟用户把整条链路跑一遍
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
PROJECT_ROOT = _HERE.parent.parent.parent
#: 是不是在源码树里（装到系统里跑的时候不成立）
_IN_SOURCE_TREE = (PROJECT_ROOT / "src" / "gnome_poster" / "models.py").is_file()

# 源码树里 `python3 -m gnome_poster.main` 时不装包也能跑：
# 1) 项目自带的 vendor/（pypinyin / jieba）放到最前面
# 2) src/ 放到最前面
if _IN_SOURCE_TREE:
    for extra in (PROJECT_ROOT / "vendor", PROJECT_ROOT / "src"):
        if extra.is_dir():
            entry = str(extra)
            if entry not in sys.path:
                sys.path.insert(0, entry)


def _output_dir() -> Path:
    """截图 / 自检产物往哪放：源码树优先，否则当前目录。"""
    return PROJECT_ROOT if _IN_SOURCE_TREE else Path.cwd()


def _check() -> int:
    """自检：不创建任何 GTK 控件，所以没有显示器也能跑。"""
    import importlib
    import platform

    problemas: list[str] = []
    print(f"Python      : {platform.python_version()}")

    gi_version = "?"
    try:
        import gi

        gi_version = gi.__version__
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        print(f"PyGObject   : {gi_version}")
        print(f"GTK         : {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
        print(f"Libadwaita  : {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}.{Adw.MICRO_VERSION}")
        if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 10):
            problemas.append("GTK 需要 4.10 以上（Gtk.FileDialog / UriLauncher）")
        if (Adw.MAJOR_VERSION, Adw.MINOR_VERSION) < (1, 5):
            problemas.append("Libadwaita 需要 1.5 以上（Adw.AlertDialog / PreferencesDialog）")
    except (ImportError, ValueError) as exc:
        problemas.append(f"GTK4 / Libadwaita 不可用：{exc}")

    for module, hint in (
        ("httpx", "pip install --user httpx 或 apt install python3-httpx"),
        ("pypinyin", "拼音搜索需要它：pip install --user pypinyin（或忽略，功能自动降级）"),
    ):
        try:
            importlib.import_module(module)
            print(f"{module:<12}: OK")
        except ImportError:
            level = "必需" if module == "httpx" else "可选"
            print(f"{module:<12}: 缺失（{level}）—— {hint}")
            if module == "httpx":
                problemas.append("缺少 httpx")

    for module, version, label in (
        ("Gst", "1.0", "视频缩略图"),
        ("Secret", "1", "钥匙环存凭据"),
    ):
        try:
            import gi

            gi.require_version(module, version)
            importlib.import_module(f"gi.repository.{module}")
            print(f"{module:<12}: OK（{label}）")
        except (ImportError, ValueError):
            print(f"{module:<12}: 缺失（可选，{label} 会退化成简化实现）")

    # 自己的模块挨个 import 一遍，最容易在这里抓出语法 / 循环引用问题
    for name in (
        "gnome_poster.models",
        "gnome_poster.utils.text",
        "gnome_poster.utils.asyncio_bridge",
        "gnome_poster.utils.thumbnails",
        "gnome_poster.services.settings",
        "gnome_poster.services.secrets",
        "gnome_poster.services.signer",
        "gnome_poster.services.base",
        "gnome_poster.services.x_api",
        "gnome_poster.services.xhs_api",
        "gnome_poster.services.publisher",
        "gnome_poster.services.tag_engine",
        "gnome_poster.services.contacts",
        "gnome_poster.services.draftstore",
        "gnome_poster.widgets.composer",
        "gnome_poster.widgets.mention_popover",
        "gnome_poster.widgets.media_grid",
        "gnome_poster.widgets.tag_flow",
        "gnome_poster.widgets.poll_panel",
        "gnome_poster.widgets.platform_row",
        "gnome_poster.widgets.settings_dialog",
        "gnome_poster.window",
        "gnome_poster.application",
    ):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            problemas.append(f"import {name} 失败：{exc.__class__.__name__}: {exc}")

    from gnome_poster.services.settings import Settings as _S

    settings = _S()
    print(f"设置后端    : {settings.backend_detail}")

    from gnome_poster.utils.text import HAS_JIEBA, HAS_PYPINYIN, pinyin_forms

    print(f"拼音        : {'pypinyin ' + str(pinyin_forms('林深见鹿')) if HAS_PYPINYIN else '未启用'}")
    print(f"分词        : {'jieba' if HAS_JIEBA else 'n-gram 兜底'}")

    if problemas:
        print("\n有问题：")
        for item in problemas:
            print(f"  ✗ {item}")
        return 1
    print("\n全部通过 ✅")
    return 0


def _flatten_to_png(texture, path) -> bool:
    """把（可能带透明通道的）渲染结果合成到窗口底色上再存盘。

    深色模式下 render_texture 出来的纹理大部分像素是透明的，
    直接 save_to_png 会得到一张「看起来什么都没有」的图。
    """
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Adw, GdkPixbuf, GLib

        raw = texture.save_to_png_bytes()
        loader = GdkPixbuf.PixbufLoader()
        loader.write(raw.get_data())
        loader.close()
        source = loader.get_pixbuf()
        if source is None:
            return False

        width, height = source.get_width(), source.get_height()
        dark = Adw.StyleManager.get_default().get_dark()
        background = (0.13, 0.13, 0.15) if dark else (0.98, 0.98, 0.98)

        canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
        canvas.fill(
            (int(background[0] * 255) << 24)
            | (int(background[1] * 255) << 16)
            | (int(background[2] * 255) << 8)
            | 0xFF
        )
        source.composite(
            canvas,
            0,
            0,
            width,
            height,
            0,
            0,
            1.0,
            1.0,
            GdkPixbuf.InterpType.BILINEAR,
            255,
        )
        canvas.savev(str(path), "png", [], [])
        del GLib
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[self-test] 合成 PNG 失败：{exc}", file=sys.stderr)
        return False


def _snapshot_widget(widget, path) -> bool:
    """把一棵已分配布局的控件树渲染成 PNG。

    GTK4 没有「截自己的窗口」这种公开 API，但 `Gtk.Widget.snapshot_child()`
    和 `Gsk.Renderer.render_texture()` 都是公开的，串起来就能在进程内出图，
    不需要 gnome-screenshot / grim / 合成器权限。
    """
    try:
        import gi

        from gi.repository import Gtk

        try:
            gi.require_version("Graphene", "1.0")
            from gi.repository import Graphene
        except (ImportError, ValueError):
            Graphene = None

        parent = widget.get_parent()
        if parent is None:
            return False

        snapshot = Gtk.Snapshot.new()
        Gtk.Widget.snapshot_child(parent, widget, snapshot)
        node = snapshot.to_node()
        if node is None:
            return False

        native = widget.get_native()
        renderer = native.get_renderer() if native is not None else None
        if renderer is None:
            return False

        width, height = widget.get_width(), widget.get_height()
        if width <= 0 or height <= 0:
            return False

        viewport = None
        if Graphene is not None:
            viewport = Graphene.Rect()
            viewport.init(0, 0, width, height)

        texture = renderer.render_texture(node, viewport)
        if texture is None:
            return False
        return _flatten_to_png(texture, path)
    except Exception as exc:  # noqa: BLE001 - 截图失败不该让自检失败
        print(f"[self-test] 渲染快照失败：{exc}", file=sys.stderr)
        return False


def _describe(widget, depth: int = 0, budget: list[int] | None = None) -> list[str]:
    """把控件树打平成文本，用来确认该有的零件都真的建出来了。"""
    if budget is None:
        budget = [400]
    if budget[0] <= 0:
        return []
    budget[0] -= 1
    name = type(widget).__name__
    label = ""
    # Adw 的行用 title 而不是 text，所以两个都要看
    for getter_name in ("get_title", "get_text"):
        getter = getattr(widget, getter_name, None)
        if not callable(getter):
            continue
        try:
            text = getter()
        except Exception:  # noqa: BLE001
            continue
        if text:
            label = f" {str(text)[:32]!r}"
            break
    lines = ["  " * depth + f"{name}{label}"]
    child = widget.get_first_child()
    while child is not None and budget[0] > 0:
        lines.extend(_describe(child, depth + 1, budget))
        child = child.get_next_sibling()
    return lines


def _self_test(scheme: str = "light") -> int:
    """起真实窗口 → 塞示例内容 → 渲染一张快照 → 退出。

    只在启动时定死主题（不做运行时切换），否则会拍到 libadwaita 的主题过渡动画。
    注意：会真的在屏幕上闪一下窗口 —— 这是验证「真的能跑」最直接的办法。
    """
    import os as _os
    import sys as _sys

    import gi

    gi.require_version("Adw", "1")
    from gi.repository import GLib

    from .application import PosterApplication

    # scheme 取值：light / dark / settings / settings-dark
    # settings* 会顺便把设置对话框打开再拍 —— Adw.Dialog 是渲染在窗口内部的
    # AdwDialogHost 里，所以能一起截进来。
    dark = "dark" in scheme
    with_settings = "settings" in scheme
    shot_name = "settings" if with_settings else ("dark" if dark else "light")

    shot_dir = _output_dir() / "screenshots"
    shot_dir.mkdir(exist_ok=True)
    failures: list[str] = []
    app = PosterApplication(self_test=True, forced_scheme="dark" if dark else "light")
    ticks = {"n": 0}
    stable = {"width": 0, "height": 0, "count": 0}
    shots = {"done": False}
    # 轮询上限 ~15s：宁可等，也不要拍一张没有 alloc 的空图
    max_ticks = int(_os.environ.get("GNOME_POSTER_SELFTEST_TICKS", "100"))
    tick_ms = 150
    #: 布局定格后再等这么久，给缩略图解码 / 标签刷新留时间
    settle_ms = int(_os.environ.get("GNOME_POSTER_SELFTEST_SETTLE", "900"))

    dialog_state = {"opened": False}

    def _outermost():
        """挂在窗口下面最外层的控件，用来 dump 完整控件树（Adw 中间夹了几层内部 bin）。"""
        window = app.get_active_window()
        if window is None:
            return None
        content = window.get_content()
        if content is None:
            return None
        node = content
        while node.get_parent() is not None and node.get_parent() is not window:
            node = node.get_parent()
        return node

    def _target():
        """挑出「已经稳定布局」的那个控件，没稳定就返回 None 继续等。

        两个坑叠在一起：
          1. 原来用固定延时 —— jieba 词性模型冷加载把主线程卡住时，
             窗口还没 alloc 就去 snapshot，只拿到
             "Trying to snapshot AdwDialogHost without a current allocation"。
          2. 只看 get_width() > 0 不够：那个值可能是上一轮布局留下的，
             控件本身仍被标记为 alloc-needed（AdwDialogHost 就是这样）。
        所以这里：只要我们自己的 content（ToastOverlay）+ 要求 mapped +
        连续两次轮询尺寸不变。
        """
        window = app.get_active_window()
        if window is None:
            return None
        content = window.get_content()
        if content is None:
            return None
        width, height = content.get_width(), content.get_height()
        if not content.get_mapped() or width <= 0 or height <= 0:
            stable["count"] = 0
            return None
        if (width, height) == (stable["width"], stable["height"]):
            stable["count"] += 1
        else:
            stable.update(width=width, height=height, count=1)
        return content if stable["count"] >= 2 else None

    def _shoot(target) -> None:
        style = __import__("gi.repository", fromlist=["Adw"]).Adw.StyleManager.get_default()
        print(
            f"[self-test] 主题 dark={style.get_dark()} scheme={style.get_color_scheme().value_nick}",
            flush=True,
        )
        path = shot_dir / f"{shot_name}.png"
        if _snapshot_widget(target, path):
            print(
                f"[self-test] 已渲染 {path}  ({target.get_width()}x{target.get_height()})", flush=True
            )
        else:
            failures.append(f"{scheme} 快照失败")

    def _report_tree() -> None:
        root = _outermost()
        if root is None:
            return
        tree = _describe(root)
        (shot_dir / f"widget-tree-{shot_name}.txt").write_text("\n".join(tree), encoding="utf-8")
        print(
            f"[self-test] 控件树 {len(tree)} 个节点 → screenshots/widget-tree-{shot_name}.txt",
            flush=True,
        )

    def _finish() -> bool:
        """布局定格之后再多等一会儿，让异步缩略图落地，然后拍。"""
        if with_settings:
            # Adw.Dialog 在桌面端是独立的 native surface，进程内 snapshot 拿不到它。
            # 不装作拍到了：改成验证它真的打开 + dump 它自己的控件树（那棵树是能走的）。
            window = app.get_active_window()
            dialog = window.get_visible_dialog() if window is not None else None
            if dialog is None:
                failures.append("设置对话框没有打开")
            else:
                tree = _describe(dialog)
                (shot_dir / "settings-tree.txt").write_text("\n".join(tree), encoding="utf-8")
                print(
                    f"[self-test] 设置对话框已打开（独立 surface，无法进程内截图）；"
                    f"控件树 {len(tree)} 节点 → screenshots/settings-tree.txt",
                    flush=True,
                )
                shots["done"] = True
            GLib.timeout_add(300, app.quit)
            return False

        target = _target()
        if target is None:
            failures.append("布局在定格后又变了")
            app.quit()
            return False
        _shoot(target)
        _report_tree()
        shots["done"] = True
        GLib.timeout_add(400, app.quit)  # 让 PNG 落盘再退
        return False

    def _poll() -> bool:
        ticks["n"] += 1
        if with_settings:
            # 这条路径不拍图，只要把对话框打开、确认它没炸
            if not dialog_state["opened"] and ticks["n"] >= 3:
                window = app.get_active_window()
                if window is not None:
                    window.open_settings()
                    dialog_state["opened"] = True
                    GLib.timeout_add(1200, _finish)  # 等它的动画走完
                    return False
            return ticks["n"] < 6
        target = _target()
        if target is not None:
            GLib.timeout_add(settle_ms, _finish)
            return False
        if ticks["n"] >= max_ticks:
            failures.append(f"窗口 {max_ticks * tick_ms / 1000:.0f}s 内没有完成布局")
            app.quit()
            return False
        return True

    def _guard() -> None:
        """兜底：万一应用压根没 activate（比如被当成第二个实例），
        这里必须报错退出 —— 不能像以前那样默默印一句「通过」。"""
        window = app.get_active_window()
        if window is None and not failures:
            failures.append("应用没有激活出窗口（是不是有另一个实例在跑？）")
            app.quit()

    GLib.timeout_add(tick_ms, _poll)
    # 比轮询上限再晚一点检查一次：到那时还一张图没拍出来就是真出问题了
    GLib.timeout_add(max_ticks * tick_ms + 1500, _guard)

    code = app.run([_sys.argv[0]])
    if not shots["done"] and not failures:
        failures.append("没有产出任何快照")
    if failures:
        for item in failures:
            print(f"[self-test] ✗ {item}", file=sys.stderr)
        return 1
    label = shot_name if not with_settings else "设置对话框（仅功能验证）"
    print(f"[self-test] {label} 通过 ✅", flush=True)
    return int(code or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--check" in args:
        return _check()

    self_test = any(a == "--self-test" or a.startswith("--self-test=") for a in args)
    exercise = "--exercise" in args
    xhs_login = "--xhs-login" in args
    scheme = "light"
    for item in args:
        if item.startswith("--self-test="):
            scheme = item.split("=", 1)[1].strip() or "light"
    # 把自定义参数摘掉，剩下的交给 Gtk.Application
    forwarded = [
        a
        for a in args
        if a not in ("--check", "--exercise", "--xhs-login") and not a.startswith("--self-test")
    ]
    if xhs_login:
        # 交给 GApplication 的命令行处理，这样「已有实例在跑」时也能生效
        forwarded.append("--xhs-login")

    # 没有显示器时给一句人话，而不是 GTK 的段错误式报错
    if not self_test and not exercise:
        import os

        if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
            print(
                "没有可用的显示服务（WAYLAND_DISPLAY / DISPLAY 都没有）。\n"
                "在 GNOME 会话里运行 ./run.sh；只做环境自检可以加 --check。",
                file=sys.stderr,
            )
            return 2

    if self_test:
        return _self_test(scheme)

    if exercise:
        from .exercise import run as _run_exercise

        return _run_exercise()

    from .application import PosterApplication

    app = PosterApplication(open_xhs_login=xhs_login)
    return app.run([sys.argv[0], *forwarded])


if __name__ == "__main__":
    sys.exit(main())

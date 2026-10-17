"""路径解析：既要能在源码树里跑，也要能在 meson 装完之后跑。

源码树：`data/style.css` 就在项目根下，run.sh 会通过 `GNOME_POSTER_SOURCE_ROOT`
        告诉我们在哪；没设环境变量就从包文件往上找。
装到系统：包在 `<prefix>/lib/python3/dist-packages/gnome_poster/`，
        资源在 `<prefix>/share/gnome-poster/`，从包文件往上走也能找到。
"""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_ROOT_ENV = "GNOME_POSTER_SOURCE_ROOT"
_DATA_SUBDIR = "gnome-poster"


def source_root() -> Path | None:
    raw = os.environ.get(SOURCE_ROOT_ENV, "").strip()
    if raw:
        candidate = Path(raw)
        if (candidate / "data" / "style.css").is_file():
            return candidate
    # 源码树里直接 `python -m gnome_poster.main`：src/gnome_poster/paths.py → 项目根
    guessed = Path(__file__).resolve().parent.parent.parent
    if (guessed / "data" / "style.css").is_file():
        return guessed
    return None


def installed_datadir() -> Path | None:
    """从包文件位置往上找 `<prefix>/share/gnome-poster/`。"""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "share" / _DATA_SUBDIR
        if (candidate / "style.css").is_file():
            return candidate
    return None


def data_file(name: str) -> Path | None:
    """按「源码树 → 已安装」的顺序找一个数据文件。"""
    root = source_root()
    if root is not None:
        candidate = root / "data" / name
        if candidate.exists():
            return candidate
    datadir = installed_datadir()
    if datadir is not None:
        candidate = datadir / name
        if candidate.exists():
            return candidate
    # 兜底：常见系统路径
    for prefix in ("/usr/local/share", "/usr/share"):
        candidate = Path(prefix) / _DATA_SUBDIR / name
        if candidate.exists():
            return candidate
    return None


def icon_search_paths() -> list[Path]:
    out: list[Path] = []
    root = source_root()
    if root is not None and (root / "data" / "icons").is_dir():
        out.append(root / "data" / "icons")
    for prefix in ("/usr/local/share", "/usr/share"):
        candidate = Path(prefix) / "icons"
        if candidate.is_dir():
            out.append(candidate)
    return out

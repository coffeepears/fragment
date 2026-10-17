#!/usr/bin/env bash
# 从源码目录直接运行，不安装任何东西到系统。
#
#   ./run.sh              启动应用
#   ./run.sh --self-test  启动后自动截图并退出（用于冒烟测试，需要 GNOME 会话）
#   ./run.sh --check      只做导入 / 语法自检，不开窗口
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA_XML="$HERE/data/com.pear.GnomePoster.gschema.xml"
SCHEMA_BIN="$HERE/data/gschemas.compiled"

# --- 1. 编译 GSettings schema 到源码目录（默认 GSETTINGS_SCHEMA_DIR 会覆盖系统 schema 路径？不会，
#        它只是“追加”查找路径，所以把系统路径也带上，避免 Gtk/Adw 的 schema 找不到。）
if [[ ! -f "$SCHEMA_BIN" || "$SCHEMA_XML" -nt "$SCHEMA_BIN" ]]; then
  glib-compile-schemas --strict "$HERE/data"
fi
SCHEMA_DIRS="$HERE/data"
if [[ -d /usr/share/glib-2.0/schemas ]]; then
  SCHEMA_DIRS="$SCHEMA_DIRS:/usr/share/glib-2.0/schemas"
fi
if [[ -d /usr/local/share/glib-2.0/schemas ]]; then
  SCHEMA_DIRS="$SCHEMA_DIRS:/usr/local/share/glib-2.0/schemas"
fi
export GSETTINGS_SCHEMA_DIR="${GSETTINGS_SCHEMA_DIR:+$GSETTINGS_SCHEMA_DIR:}$SCHEMA_DIRS"

# --- 2. 自带依赖（vendor/ 里的 pypinyin）与源码
export PYTHONPATH="$HERE/vendor:$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

# --- 3. 图标：源码运行时也能用上应用图标
export GNOME_POSTER_SOURCE_ROOT="$HERE"

exec python3 -m gnome_poster.main "$@"

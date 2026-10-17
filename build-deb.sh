#!/usr/bin/env bash
#
# 打出 .deb。
#
#   ./build-deb.sh              构建（产物在项目上一级目录）
#   ./build-deb.sh --install    构建完顺便让你确认安装命令
#
# 只依赖 dpkg-dev + meson + ninja（这台机器都有），不需要 debhelper、不需要 sudo。
# 真正安装到系统才需要 root：
#
#   sudo apt install ../gnome-poster_0.1.0_all.deb
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

command -v dpkg-buildpackage >/dev/null || {
  echo "缺少 dpkg-dev：sudo apt install dpkg-dev" >&2
  exit 1
}
command -v meson >/dev/null || {
  echo "缺少 meson：sudo apt install meson ninja-build" >&2
  exit 1
}

echo "==> 构建 binary 包（不做源码包）"
# -b  只打二进制包
# -uc -us  不签 .buildinfo / .changes（本地自用，没有 GPG key）
# -d  跳过 Build-Depends 检查（依赖已按 debian/control 列全；
#     真缺东西后面编译会直接报错，不会悄悄出一个坏包）
dpkg-buildpackage -b -uc -us -d

shopt -s nullglob
DEBS=("$HERE"/../gnome-poster_*.deb)
if [ ${#DEBS[@]} -eq 0 ]; then
  echo "没有产出 .deb，检查上面的日志" >&2
  exit 1
fi

echo
echo "==> 产物"
for deb in "${DEBS[@]}"; do
  echo "  $deb  ($(du -h "$deb" | cut -f1))"
done

echo
echo "==> 安装（需要 sudo）"
echo "  sudo apt install \"${DEBS[0]}\""
echo
echo "  想先看看里面装了什么："
echo "  dpkg-deb -c \"${DEBS[0]}\" | less"

if [ "${1:-}" = "--install" ]; then
  echo
  echo "上面那条 apt 命令复制走就行 —— 我不替你输 sudo 密码。"
fi

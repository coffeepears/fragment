# GNOME 标准构建（可选）：meson setup build && ninja -C build
# 只想跑起来的话直接用 ./run.sh，不需要 meson。
project(
  'gnome-poster',
  version: '0.4.5',
  meson_version: '>= 0.62.0',
  license: 'GPL-3.0-or-later',
)

python = import('python').find_installation('python3')
gnome = import('gnome')
i18n = import('i18n')
prefix = get_option('prefix')
datadir = get_option('datadir')
bindir = get_option('bindir')

# ---- Python 包 ----
# 注意：`subdir` 必须按包分层写，否则 widgets/ services/ utils/ 会被拍平到
# gnome_poster/ 下面，装完之后 `from .widgets.composer import ...` 直接崩。
python.install_sources(
  [
    'src/gnome_poster/__init__.py',
    'src/gnome_poster/main.py',
    'src/gnome_poster/application.py',
    'src/gnome_poster/window.py',
    'src/gnome_poster/models.py',
    'src/gnome_poster/exercise.py',
  ],
  subdir: 'gnome_poster',
  pure: true,
)

python.install_sources(
  [
    'src/gnome_poster/utils/__init__.py',
    'src/gnome_poster/utils/asyncio_bridge.py',
    'src/gnome_poster/utils/paths.py',
    'src/gnome_poster/utils/text.py',
    'src/gnome_poster/utils/thumbnails.py',
  ],
  subdir: 'gnome_poster/utils',
  pure: true,
)

python.install_sources(
  [
    'src/gnome_poster/services/__init__.py',
    'src/gnome_poster/services/settings.py',
    'src/gnome_poster/services/secrets.py',
    'src/gnome_poster/services/base.py',
    'src/gnome_poster/services/x_api.py',
    'src/gnome_poster/services/xhs_api.py',
    'src/gnome_poster/services/signer.py',
    'src/gnome_poster/services/publisher.py',
    'src/gnome_poster/services/tag_engine.py',
    'src/gnome_poster/services/contacts.py',
    'src/gnome_poster/services/draftstore.py',
    'src/gnome_poster/services/instagram_api.py',
    'src/gnome_poster/services/xhs_service.py',
  ],
  subdir: 'gnome_poster/services',
  pure: true,
)

python.install_sources(
  [
    'src/gnome_poster/widgets/__init__.py',
    'src/gnome_poster/widgets/composer.py',
    'src/gnome_poster/widgets/mention_popover.py',
    'src/gnome_poster/widgets/media_grid.py',
    'src/gnome_poster/widgets/tag_flow.py',
    'src/gnome_poster/widgets/poll_panel.py',
    'src/gnome_poster/widgets/platform_row.py',
    'src/gnome_poster/widgets/settings_dialog.py',
    'src/gnome_poster/widgets/usage_ring.py',
  ],
  subdir: 'gnome_poster/widgets',
  pure: true,
)

# ---- 自带的纯 Python 依赖（pypinyin / jieba）----
# 打 .deb 时关掉（-Dvendored_deps=false），改用 Debian 自己的
# python3-pypinyin / python3-jieba，别把同一份代码装两处。
if get_option('vendored_deps')
  foreach vendored : ['pypinyin', 'jieba']
    install_subdir(
      'vendor' / vendored,
      install_dir: python.get_install_dir(),
      exclude_files: ['__pycache__', '*.pyc'],
    )
  endforeach
endif

# ---- GSettings ----
gnome.compile_schemas(build_by_default: true, depend_files: 'data/com.pear.GnomePoster.gschema.xml')
install_data(
  'data/com.pear.GnomePoster.gschema.xml',
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

# ---- 桌面入口 / 元数据 / 图标 ----
desktop_file = i18n.merge_file(
  input: 'data/com.pear.GnomePoster.desktop.in',
  output: 'com.pear.GnomePoster.desktop',
  type: 'desktop',
  po_dir: 'po',
  install: true,
  install_dir: datadir / 'applications',
)
appstream_file = i18n.merge_file(
  input: 'data/com.pear.GnomePoster.metainfo.xml',
  output: 'com.pear.GnomePoster.metainfo.xml',
  type: 'xml',
  po_dir: 'po',
  install: true,
  install_dir: datadir / 'metainfo',
)
install_data(
  'data/icons/hicolor/scalable/apps/com.pear.GnomePoster.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'apps',
)
install_data(
  'data/icons/hicolor/symbolic/apps/com.pear.GnomePoster-symbolic.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)
install_data(
  'data/contacts.example.json',
  install_dir: datadir / 'gnome-poster',
)
install_data('data/style.css', install_dir: datadir / 'gnome-poster')

# ---- 内置的小红书发布服务 ----
# 它不是一个「可选的第三方依赖」，对小红书来说就是后端。早期版本让用户自己去
# 下载、自己写 systemd 单元，纯属白折腾。现在随包安装，app 负责拉起（见
# services/xhs_service.py）。
# 注意：这是 linux-amd64 的静态二进制 → 本包必须是 Architecture: amd64。
xhs_service_install_dir = get_option('libexecdir') / 'gnome-poster'
install_data(
  'vendor/xhs-service/xiaohongshu-mcp',
  install_dir: xhs_service_install_dir,
  install_mode: 'rwxr-xr-x',
)
install_data(
  'vendor/xhs-service/LICENSE',
  install_dir: xhs_service_install_dir,
  rename: 'LICENSE.xiaohongshu-mcp',
)
install_data(
  'vendor/xhs-service/README.md',
  install_dir: xhs_service_install_dir,
  rename: 'README.xiaohongshu-mcp.md',
)
# 小红书 web 通道的外部签名器骨架（用户要自己补 tools/xhs_sign_impl.js）
install_data('tools/xhs_sign.js', install_dir: datadir / 'gnome-poster')

# i18n
i18n.gettext('gnome-poster', preset: 'glib')

# ---- 源代码树之外的启动器（run.sh 只能在源码目录用） ----
configure_file(
  input: 'data/gnome-poster.in',
  output: 'gnome-poster',
  configuration: {'PYTHON': python.full_path()},
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)

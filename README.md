# Fragment

> 写下来，就发出去。

Fragment 是一个原生 GNOME 的 **Rednote** 笔记客户端。写好标题、正文、图片、
话题和投票，扫码登录后一键发布，不用在电脑和手机之间来回倒腾。

Rednote 没有公开的发布接口，所以 Fragment **内置了一个本机发布服务**
（[xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)，Apache-2.0）。
服务随 deb 一起安装、由应用自动启动，退出时一并收掉 —— 不需要你自己下载二进制、
不需要手写 systemd 单元。

## 特性

- 单页撰写：标题 / 媒体 / 正文 / 智能话题 / 投票，一屏到底
- 图片、视频拖拽上传，缩略图异步解码，可写替代文本
- 输入 `@` 弹出关注列表，支持拼音与昵称模糊搜索
- 话题推荐：本地分词（jieba 词性过滤）+ Rednote 话题接口
- 原生投票，按 Rednote 上限自动裁剪；通道不支持时降级为正文文字
- 用量圆环实时显示字数与媒体占用，超限当场变色
- 跟随系统深色 / 浅色模式
- 可选 X / Instagram 适配器（默认隐藏，代码保留）

## 技术栈

Python 3 + PyGObject + **GTK 4.18** + **Libadwaita 1.7**，跟随
[GNOME HIG](https://developer.gnome.org/hig/)。

## 1. 快速开始

### 只想先试试？零配置也行

**不用配任何账号**：写好标题、正文、话题，然后点窗口右上角的「复制文案」
（`Ctrl+Shift+C`），粘到手机 App 里就能发。发小红书这条其实最省事 ——
毕竟它没有公开的发布接口。

或者打开 设置 → 先看这里 → 「打开演练模式」，整条发布流程照跑，但一个请求都不发。

### 装成 deb（推荐）

```bash
cd ~/Documents/gnome-poster
./build-deb.sh                                  # 打出 ../gnome-poster_0.1.0_all.deb
sudo apt install ../gnome-poster_0.1.0_all.deb  # 依赖会被一起装上
```

装完菜单里就有「Fragment」，或者直接 `gnome-poster`。
`apt` 会按包里的 `Depends` 自动拉 `python3-pypinyin` / `python3-httpx` 等，
`Recommends` 里的 `python3-jieba` 和 GStreamer 组件默认也会装
（不想要就 `--no-install-recommends`，功能会退化成 n-gram 抽词、视频没缩略图）。

包的信息（`dpkg-deb -I gnome-poster_0.1.0_all.deb`）：

```
Package: gnome-poster          Version: 0.1.0        Architecture: all
Installed-Size: 355            Section: net          Priority: optional
Depends: python3, python3-gi, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.5),
         python3-httpx, python3-pypinyin
Recommends: python3-jieba, gir1.2-secret-1, gir1.2-gstreamer-1.0,
            gir1.2-gst-plugins-base-1.0, gstreamer1.0-plugins-base
```

42 个文件：Python 包进 `/usr/lib/python3/dist-packages/gnome_poster/`，
启动器 `/usr/bin/gnome-poster`，桌面入口、AppStream 元数据、图标、
GSettings schema、`/usr/share/gnome-poster/`（样式表 + 签名器骨架 + 关注列表示例）。

**关于 gschema / 图标 / desktop 缓存**：包里**没有** postinst 去跑
`glib-compile-schemas`、`gtk-update-icon-cache`、`update-desktop-database` ——
这些由 `libglib2.0-0t64`、`hicolor-icon-theme`、`desktop-file-utils` 注册的
**dpkg interest 触发器**自动完成，自己写反而容易漏。postinst 只干一件事：
`py3compile` 编译字节码（包里不带 `.pyc`，postrm 里 `py3clean` 清掉）。

**为什么没有 debhelper**：写这套的时候这台机器上没装 `debhelper`，装它要 sudo。
所以 `debian/rules` 是手写的，只依赖 `dpkg-dev + meson + ninja`，
`dpkg-buildpackage -b -uc -us` 在干净环境里直接能出包。
想换回标准流程，`debian/rules` 顶部注释写了三步改法。

### 源码直跑（开发用）

```bash
./run.sh --check        # 环境自检（不需要显示器，可以进 CI）
./run.sh                # 启动应用
./run.sh --self-test    # 起窗口 → 渲染快照到 screenshots/ → 退出
./run.sh --exercise     # 交互级冒烟测试：模拟用户把整条链路跑一遍
```

`run.sh` 不往系统里装任何东西：它只把 `vendor/`（自带的 pypinyin、jieba）和
`src/` 加进 `PYTHONPATH`，把 GSettings schema 编译到 `data/` 里，
再通过 `GSETTINGS_SCHEMA_DIR` 指过去。

### 依赖

| 组件 | 必需 | 说明 |
|---|---|---|
| Python ≥ 3.11 | ✅ | Debian 13 自带 3.13 |
| `python3-gi` + `gir1.2-gtk-4.0` | ✅ | GTK 4.10 起（用到了 `Gtk.FileDialog` / `UriLauncher`） |
| `gir1.2-adw-1` | ✅ | Libadwaita 1.5 起（`Adw.AlertDialog` / `PreferencesDialog`） |
| `python3-httpx` | ✅ | 网络层 |
| `python3-pypinyin` | ✅ | @ 提及的拼音搜索（源码直跑时用自带的 `vendor/` 副本） |
| `python3-jieba` | 推荐 | 智能标签的词性过滤；缺了退化成 n-gram |
| `gir1.2-gstreamer-1.0` + `gir1.2-gst-plugins-base-1.0` + `gstreamer1.0-plugins-base` | 可选 | 视频附件抽帧缩略图 |
| `gir1.2-secret-1` | 可选 | 凭据存 GNOME 钥匙环；缺了就退化成 0600 明文文件并明确告知 |

一条命令补齐（Debian / Ubuntu）：

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-httpx \
                 python3-pypinyin python3-jieba \
                 gstreamer1.0-plugins-base gir1.2-gstreamer-1.0 gir1.2-secret-1
```

### 用 meson 装（不打包，可选）

```bash
meson setup build && ninja -C build && sudo ninja -C build install
sudo glib-compile-schemas /usr/share/glib-2.0/schemas
```

这条路会把 `vendor/` 里的 pypinyin / jieba 一起装进 site-packages
（`-Dvendored_deps=true`，默认）；打 deb 时用 `-Dvendored_deps=false` 改成依赖系统包。

---

## 2. 项目结构

```
gnome-poster/
├── run.sh                        源码直跑（编译 schema、拼 PYTHONPATH、启动）
├── build-deb.sh                  打 .deb 的一键脚本
├── pyproject.toml / meson.build  打包与构建
├── meson_options.txt             vendored_deps 开关（打 deb 时关掉）
├── debian/
│   ├── control                   Depends / Recommends 都在这里
│   ├── rules                     手写规则，只依赖 dpkg-dev + meson + ninja
│   ├── changelog / copyright     native 包（3.0 (native)）
│   ├── gnome-poster.postinst     py3compile 编译字节码
│   └── gnome-poster.postrm       py3clean 清理字节码
├── po/                           gettext 骨架（目前只有 POTFILES.in）
├── vendor/                       自带依赖（pypinyin、jieba），不污染系统 Python
├── data/
│   ├── com.pear.GnomePoster.gschema.xml    设置 schema（14 个键）
│   ├── com.pear.GnomePoster.desktop.in    桌面入口
│   ├── com.pear.GnomePoster.metainfo.xml  AppStream 元数据
│   ├── gnome-poster.in                     meson 生成的启动器模板
│   ├── style.css                          只补 Adwaita 没覆盖的部件
│   ├── icons/hicolor/{scalable,symbolic}/apps/…svg
│   └── contacts.example.json              关注列表示例（首次运行的 @ 候选）
├── tools/
│   └── xhs_sign.js              小红书签名器骨架（协议实现 + 自检）
└── src/
    ├── main.py                  源码树里的 shim
    └── gnome_poster/
        ├── application.py       进程装配：Settings/Runner/Publisher → Window
        ├── window.py            主窗口：AdwWindow + HeaderBar + 垂直流式布局
        ├── models.py            草稿 / 媒体 / 投票 / 平台约束（纯数据，可单测）
        ├── exercise.py          交互级冒烟测试
        ├── widgets/             自绘控件
        │   ├── composer.py          GtkTextView + @ 实时识别 + 光标定位
        │   ├── mention_popover.py   @ 候选弹窗（拼音 / 首字母 / 昵称 / handle）
        │   ├── media_grid.py        GtkGridView + 缩略图 + 拖拽 + ALT 文本
        │   ├── tag_flow.py          GtkFlowBox 的 Tag Chips
        │   ├── poll_panel.py        投票编辑面板（Gtk.Revealer）
        │   ├── platform_row.py      底部平台开关 + 实时限额状态
        │   ├── usage_ring.py        Cairo 自绘的用量圆环
        │   └── settings_dialog.py   Adw.PreferencesDialog（分步指引 + 直达链接）
        ├── services/
        │   ├── x_api.py         X v2 + OAuth2 PKCE（官方接口，无逆向）
        │   ├── xhs_api.py       小红书双通道（本地服务 / 创作者平台 Web）
        │   ├── instagram_api.py Instagram Graph API（容器 → 发布）
        │   ├── signer.py        可插拔的外部签名器
        │   ├── publisher.py     多平台并行分发 + 结果归集
        │   ├── tag_engine.py    本地关键词 + 平台话题合并
        │   ├── contacts.py      关注列表与模糊搜索
        │   ├── settings.py      GSettings（带 JSON 后备）
        │   ├── secrets.py       libsecret（带 0600 文件后备）
        │   └── draftstore.py    草稿自动保存 / 恢复
        └── utils/
            ├── asyncio_bridge.py  asyncio ⇄ GLib 主循环
            ├── text.py            拼音 / 模糊匹配 / 关键词抽取
            └── thumbnails.py      图片 + 视频缩略图
```

---

## 3. 界面

自上而下（`Gtk.ScrolledWindow` → `Adw.Clamp(max 720)` → `Gtk.Box` 垂直流式）：

1. **标题**（`Gtk.Entry`）—— 计数器 `14/20`，超限变红；**只给小红书用**，小红书没勾选时会变灰
2. **图片 / 视频**（`Gtk.GridView` + `Gio.ListStore`）—— 拖拽或点选、缩略图、一键删除、ALT 文本
3. **正文**（`Gtk.TextView`）—— 占位符、自动换行、`Gtk.ScrolledWindow` 自增高、加权字数双平台同时显示
4. **智能话题**（`Gtk.FlowBox`）—— Tag Chips，点击插入 / 取消，可手动添加
5. **插入投票**（`Gtk.ToggleButton` + `Gtk.Revealer`）—— 上限跟着勾选的平台变；
   小红书会多出「问题」输入框（2–6 项），X 是 2–4 项且没有独立问题

底部固定条：平台开关（`Gtk.FlowBox`，窄窗口自动换行）+ 每平台一个**用量圆环**
+ 实时限额明细 + 进度条 + 状态行。
标题栏：中间 `Adw.WindowTitle`，右侧「发布」+「更多」菜单（设置在最上面）+「**复制文案**」。
一个平台都没配时，标题栏下面会出现一条 `Adw.Banner` 引导横幅。

**标题只属于小红书**：小红书有独立的标题字段，所以标题不进正文；
X 和 Instagram 没有标题字段，标题也**不会**被塞进正文（输入框会变灰提示）。

**复制文案**（零配置出路）：按「第一个勾选的平台」的格式把标题 + 正文拼好放进剪贴板
（小红书是 `#话题[话题]#`、X 是 `#话题`，混在一起反而不好用）。
`compose_for_clipboard()` 是纯函数，测试里直接断言「换到 X 时标题不会被带进去」。

**设置 → 账号页**是按「1-2-3 步、全部展开、每步旁边有按钮」重排的：
第一版把步骤藏进默认折叠的展开器里，用户看到一屏「MCP / 签名器 / Cookie」
完全不知道从哪下手（真实反馈）。真正折腾人的项（换通道、签名器、Cookie、
Client Secret、图床上传命令）统一收进折叠的「高级设置」。

**用量圆环**（`widgets/usage_ring.py`）是 Cairo 自绘的 `Gtk.DrawingArea`：
底圈 + 按比例画的弧，颜色走 CSS 的 `color` + `Gtk.Widget.get_color()`，
所以深浅主题、用户自定义强调色都会跟着走；超限时描边变红。

### 关键交互实现

**@ 好友自动补全** —— 难点在「光标在哪儿」。

```python
# composer.py
rect = self.textview.get_iter_location(buffer.get_iter_at_offset(at_index))
x, y = self.textview.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
self.emit("mention-query", query, x, y + rect.height)   # → Popover.set_pointing_to()
```

几个必须踩过的坑，代码里都处理了：
* `Gtk.Popover.set_autohide(False)` —— 否则一打字弹窗就消失
* `Gtk.Popover.set_can_focus(False)` + 搜索框 `set_can_focus(False)` —— 焦点必须留在正文框，
  方向键 / 回车才不会被抢走
* 候选行的 `_select()` **不能** `grab_focus()`
* `_is_boundary()` 判断 `@` 前面是不是行首 / 空白 / 标点，避免把邮箱当提及

搜索打分（`utils/text.py`）四路取最大值，分层是「完全相等 > 前缀 > 包含 > 子序列」：

```python
score = max(fuzzy_score(q, name),                    # 昵称
            fuzzy_score(q, pinyin) * 0.95,           # 全拼  linshenjianlu
            fuzzy_score(q, initials) * 1.05,         # 首字母 lsjl  ← 敲 "lsjl" 的主场景
            fuzzy_score(q, handle) * 0.9)            # @handle
```

**智能标签** —— 两路来源合并，本地先出、远端再补，界面从不转圈等网络：

* 本地：`jieba.posseg` 词性标注，**只保留名词类**（`n/nr/ns/nt/nz/vn/an/eng`），
  再用停用词表和「首尾功能字」过滤（这一步挡掉了 `#的事做顺` 这类垃圾）
* 平台：小红书话题搜索接口，带热度
* 没有 jieba 时降级为**精度优先**的 n-gram（只信重复出现的组合），宁缺毋滥

**多媒体附件** —— 缩略图在 `ThreadPoolExecutor` 里解码（图片走 gdk-pixbuf，
视频用 GStreamer `appsink.try_pull_sample()` 抽第一帧再转 `Gdk.MemoryTexture`），
结果经 `GLib.idle_add` 回主线程。视频抽帧带硬超时，坏文件不会挂死线程。

**多平台并行发布** —— `asyncio.gather` 同时发，一个平台炸了不影响另一个：

```python
# publisher.py
tasks = [asyncio.create_task(run_one(p, i)) for i, p in enumerate(targets)]
gathered = await asyncio.gather(*tasks, return_exceptions=True)
```

GTK 只能在主线程碰，所以 asyncio 跑在一个守护线程里，往 UI 推进度时统一过一层：

```python
emit = runner.dispatcher(self._on_progress_event)   # 内部是 GLib.idle_add
```

---

## 4. 平台接入

### X (Twitter) —— 官方 API，全流程可跑通

用的是官方 v2 接口，**没有任何逆向成分**：

| 用途 | 接口 |
|---|---|
| 授权 | OAuth 2.0 Authorization Code + **PKCE**（Public Client），回调走本机 loopback |
| 令牌 | `POST https://api.x.com/2/oauth2/token`（`offline.access` 换 refresh token） |
| 发帖 | `POST /2/tweets`，支持 `media` 与原生 `poll` |
| 媒体 | `POST /2/media/upload` 的 INIT / APPEND / FINALIZE 三段式 |
| 关注列表 | `GET /2/users/{id}/following`（喂给 @ 提及补全） |

配置步骤：

1. <https://developer.x.com> 建一个 **OAuth 2.0 Public Client**
2. 回调地址填 `http://127.0.0.1:8721/callback`
3. 把 Client ID 填进「设置 → 账号 → X」，点「登录 X」，浏览器里授权
4. 令牌存进 GNOME 钥匙环，之后自动刷新

需要 `tweet.read tweet.write users.read follows.read offline.access media.write` 这些 scope
（代码里已写死，见 `services/x_api.py` 顶部）。

> ⚠️ v2 media upload 的字段名平台一直在演进。所有端点都收在 `x_api.py` 的常量区，
> 第一次接通如果报错，改那一处即可。

### 小红书 —— 先说清楚现实

**小红书没有面向个人开发者的公开发布 OpenAPI。** 企业号 / 服务商有「开放平台」，
但要企业资质 + 审核，个人拿不到 key。第三方工具只有两条路，本项目两条都实现了，
在「设置 → 账号 → 小红书 → 发布通道」切换：

**通道一：本地发布服务（`mcp`，默认，推荐）**

对接本机跑着的发布服务（[xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)
一类），签名和登录态都由它处理，我们只发一个 multipart：

```
GET  {endpoint}/api/v1/login/status
POST {endpoint}/api/v1/publish        title / content / images[] / video
```

字段若随版本变化，只改 `XhsMcpTransport` 这一个类。

**通道二：直连创作者平台（`web`，需要自备签名器）**

走 `creator.xiaohongshu.com`，需要登录 Cookie（`a1` + `web_session`）和请求头签名
`x-s` / `x-t`。签名算法由一段混淆 JS 算出，纯 Python 复刻既不现实也不合适，所以
做成了外部进程：

```bash
echo '{"method":"POST","uri":"/web_api/sns/v2/note","payload":{},"cookies":{"a1":"x"}}' \
  | node tools/xhs_sign.js
# → {"x-s":"XYS_...","x-t":"1712345678901"}
```

`tools/xhs_sign.js` 里写好了协议、参数校验和 `--selftest`，真正的算法放进
同目录的 `xhs_sign_impl.js`（怎么从浏览器里抽出来，文件顶部注释写得很细）。

> 免责：**只用来发布你自己账号的内容**，请遵守平台用户协议，不要做自动化刷量。

### Instagram —— 官方 API，但有三个硬门槛

走官方 **Content Publishing API**（Graph API），流程是标准的「先建容器、再发布」：

```
图片：  POST /{ig-user-id}/media          {image_url, caption}            → creation_id
轮播：  POST /{ig-user-id}/media          {image_url, is_carousel_item}   → 每个子项一个 id
        POST /{ig-user-id}/media          {media_type: CAROUSEL, children, caption}
视频：  POST /{ig-user-id}/media          {media_type: REELS, video_url} → 轮询 status_code
发布：  POST /{ig-user-id}/media_publish  {creation_id}                   → media_id
链接：  GET  /{media_id}?fields=permalink
```

三个必须先知道的限制（代码里都如实处理了，不糊弄）：

1. **必须是企业号 / 创作者号**，且绑定一个 Facebook 主页，再建 Meta 应用拿
   `instagram_content_publish` 权限。个人号用不了这个接口。
2. **媒体必须是公网可访问的 URL** —— Graph API 只收 `image_url` / `video_url`，
   **不接受本地文件上传**。所以本项目给了两条路：
   * 在媒体条目的 `ALT` 弹窗里填「公开链接」；
   * 或者在 设置 → 账号 → Instagram 里配一个「**图床上传命令**」
     （stdin 收 `{"path": ...}`，stdout 返回 `{"url": "https://..."}`）。
   两条都没有就明确报错，绝不假装能发。
3. **图片只吃 JPEG**。PNG / WebP 会用 gdk-pixbuf 自动转成临时 JPEG 再交给上传命令。

另外额度是 25 条 / 24 小时。配置入口在 设置 → 账号 → Instagram，
页面上有分步说明和直达 Graph API Explorer 的按钮。

### 平台能力对照

| | 小红书 | X | Instagram |
|---|---|---|---|
| 标题 | 必填，≤ 20 字 | 无此字段 | 无此字段 |
| 正文 | ≤ 1000 字 | 280 加权字符（中文算 2；Premium 25000） | caption ≤ 2200 字 |
| 图片 | ≤ 18 张 | ≤ 4 张 | ≤ 10 张（轮播） |
| 视频 | 1 个（与图文互斥） | 1 个（与图片互斥） | 1 个 Reel |
| 必须带媒体 | 否 | 否 | **是**（纯文字发不了） |
| 原生投票 | ✅ 2–6 项 + 独立问题 | ✅ 2–4 项、单选、无独立问题 | ❌ |
| 话题 | `#话题[话题]#` 内嵌正文 | `#话题` | `#hashtag` 内嵌 caption |
| 接入方式 | 本地发布服务 / 创作者平台 | 官方 API v2 | 官方 Content Publishing API |

限额定义全部集中在 `models.py`（`XHS_LIMITS` / `X_LIMITS` / `INSTAGRAM_LIMITS`），
改版只动那一处。

### 小红书投票：可以原生发，但要看你用的通道

小红书**确实有原生投票组件**（App 发布页工具栏里就有，问题 + 2–6 个选项 + 时长，
见 [这篇说明](https://m.3835.com/news/23398.html)）。但它是个**App 端的组件，
没有公开的写入接口**，所以本项目的做法是能力协商：

* 模型层：`XHS_LIMITS.supports_poll = True`、`poll_max_options = 6`、
  `poll_question_max = 20` —— 界面上小红书就是一等公民，能填 6 个选项和独立问题。
* 通道层：`XhsTransport.supports_native_poll` 默认 `False`。
  当前两个通道（MCP 服务 / 创作者平台 Web）都不声明支持。
* 于是默认**安全降级**：投票变成正文末尾的文字版，并在发布前弹一次确认。
* 如果你的本地发布服务能点 App 里的投票组件，去
  **设置 → 小红书 → 尝试原生投票组件** 打开它，就会把 `poll_*` 字段发给服务。

这样既不假装能发，也不会把用户的投票悄悄丢掉。

---

## 5. 自检与测试

```bash
./run.sh --check       # 依赖 / 版本 / 所有模块 import，无需显示器
./run.sh --exercise    # 20 项交互断言，任何一条不过就非 0 退出
```

`--exercise` 会真的模拟用户操作，跑 **56 项断言**，任何一条不过就非 0 退出：

```
✓ 媒体加入成功 → ✓ 列表数量正确 → ✓ 缩略图已异步解码 (512x320)
✓ 标题不会进 X 正文 / 不会进 Instagram 正文
✓ 小红书接受 6 项投票 → ✓ X 只接受 4 项（6 项被拦下）
✓ Instagram 纯文字会被拦下 → ✓ Instagram 不支持投票
✓ 圆环 ok/warn/error 三级颜色解析正常 (#c88800 / #e5a50a / #e01b24)
✓ 圆环描边像素与主题色一致 → ✓ 超限状态的描边画成错误色
✓ 勾上 Instagram 不炸，且进了选中集合 → ✓ 取消勾选后回到原状态
✓ @ 提及弹窗已弹出 → ✓ 拼音首字母能搜到候选 → ✓ 已补全进正文
✓ 投票面板已展开 → ✓ 投票数据合法
✓ 设置对话框已打开 → ✓ 拿到的是 SettingsDialog
✓ 小红书投票提醒弹窗出现 → ✓ 取消后仍未发布
✓ 发布已进入忙碌状态 → ✓ 发布成功 → ✓ 编辑器被清空 → ✓ 留下撤销快照
✓ 小红书测试连接走真实失败分支且是友好报错（"连不上本地发布服务 …"）
```

这套测试写完当天就抓到一个真 bug：本机 `ALL_PROXY=socks://127.0.0.1:7890`，
httpx 在没有 `socksio` 时会抛 `Unknown scheme for proxy URL`，而原来的代码只
`except httpx.HTTPError`，用户看到的就是这句天书。现在本地服务一律 `trust_env=False`，
其余网络异常统一过 `services/base.py: network_error()` 翻译成人话。

测试工具自己也修过两次，都挺值得记的：

* **`--self-test` / `--exercise` 曾经静默地什么都不做却报「通过」。**
  `GApplication` 是单实例的 —— 用户正开着应用时，测试进程被当成「第二个实例」，
  把激活转给已有进程然后自己退出。现在测试模式带 `Gio.ApplicationFlags.NON_UNIQUE`，
  并且没产出快照就直接判失败。
* **自检不再恢复用户真实草稿**。之前它会把 `~/.local/state` 里正在写的草稿
  恢复进测试窗口，把用户的图片拍进了截图。
* `Adw.Dialog`（设置对话框）在桌面端是**独立 native surface**，进程内 snapshot
  拿不到它。所以 `--self-test=settings` 不假装截图，改成验证它真的打开并把
  **对话框自己的控件树** dump 到 `screenshots/settings-tree.txt`。

另外几个是补测试 / 装包时才暴露的：

* **`selection-changed` 信号处理函数少收一个参数** —— GObject 会把发出者当第一个
  参数塞进来，而原来的处理函数是无参的。也就是说**用户手点平台复选框就 TypeError**，
  而当时的测试从没点过它。现在 `--exercise` 里有一步专门手点复选框。
* **libadwaita 的独立色变量在这个 GTK 上解析成垃圾值**：
  `var(--accent-color)` / `--error-color` / `--warning-color`（还有 `@accent_color`）
  会返回越界数值（实测 `--error-color` → `rgb(195,-51,-1)`），
  导致「超限字数变红」这类样式随机上色。`--*-bg-color` / `--*-fg-color` 是好的，
  已经全部换过去。这个坑是靠圆环的像素级断言挖出来的。
* **Adw 行的 title/subtitle 会按 Pango markup 解析** —— 文案里带 `&`
  （比如 "Projects & Apps"）整条会渲染失败，现在统一过 `markup_escape_text`。

另外两个是装包时才暴露的：

* `jieba.posseg` 冷加载（~0.5s 磁盘 IO）发生在主线程，把窗口首帧都卡住了。
  现在模型加载**只**由后台的 `warm_up()` 触发，UI 路径拿不到词性就先退回普通分词，
  预热完成后自动生效 —— 用户再也看不到那个 1 秒的假死。
* `--self-test` 原来用固定延时截图，主线程被卡的时候会拍到
  `Trying to snapshot AdwDialogHost without a current allocation`。
  改成轮询「布局连续两次稳定」再拍，并额外等 900ms 让异步缩略图落地，现在连跑 4 次都稳。

### deb 的验收方式

没有 root 也能验：用 `dpkg` 装进一个 fake root，再从**安装后的文件**把程序跑起来。

```bash
ROOT=/tmp/instroot
dpkg --root=$ROOT --force-not-root --force-script-chrootless -i ../gnome-poster_0.1.0_all.deb
glib-compile-schemas $ROOT/usr/share/glib-2.0/schemas
PYTHONPATH=$ROOT/usr/lib/python3/dist-packages \
GSETTINGS_SCHEMA_DIR=$ROOT/usr/share/glib-2.0/schemas:/usr/share/glib-2.0/schemas \
  $ROOT/usr/bin/gnome-poster --check
```

跑过的结果：42 个文件全部落在正确位置、`dpkg -l` 到 `ii`、
从安装副本 `--check` 全绿、浅色/深色两张真机截图都能渲染出来、
`desktop-file-validate` / `appstreamcli validate` / `glib-compile-schemas --strict` 全过、
`dpkg -r` 能干净卸载。

`--self-test` 用 `Gtk.Widget.snapshot_child()` + `Gsk.Renderer.render_texture()`
在进程内把窗口渲染成 PNG（不需要 gnome-screenshot / grim / 合成器权限），
并输出 `screenshots/widget-tree.txt` 方便核对控件树。

> 这两个模式都会真的在屏幕上闪一下窗口 —— 这是验证「真的能跑」最直接的办法。

---

## 6. 快捷键

| 快捷键 | 作用 |
|---|---|
| `Ctrl+Enter` | 发布 |
| `@` | 唤起提及补全；`↑` `↓` 选择，`Enter` / `Tab` 接受，`Esc` 取消 |
| `Ctrl+,` | 设置 |
| `Ctrl+Shift+Delete` | 清空（可撤销） |
| `Ctrl+Q` | 退出 |

---

## 7. 已知限制

* **小红书没有官方发布 API**，`web` 通道的 URL 与字段是社区实践形态，平台改版后需要复核；
  默认的 `mcp` 通道更稳。这是项目最脆弱的一环，README 不打算掩饰。
* 小红书**投票能原生发，但取决于发布通道**：默认走文字降级（见上一节）。
* X 的**多选投票**不支持 —— 官方只有单选，界面按单选实现。
* **Instagram 只收公网 URL**，本地文件 Graph API 抓不到。要在
  媒体条目的「公开链接」里填 URL，或在设置里配一个「图床上传命令」；
  两条都没有会明确报错，不会假装能发。另外只有企业号 / 创作者号能用这个接口。
* 媒体**拖拽排序**未实现（顺序 = 添加顺序）。
* 关注列表：X 可自动同步；小红书没有公开的关注列表接口，只能本地维护
  `~/.config/gnome-poster/contacts.json`。
* `--self-test` 的快照只覆盖窗口控件树，**不包含 `Gtk.Popover` 和 `Adw.Dialog`**
  （它们是独立 surface）。设置对话框改用控件树 dump + 断言来验证。
* 自动标签的本地部分是统计式的：有 jieba 时按名词抽，没有时保守给候选。
  真正贴合平台生态的话题来自小红书话题接口，那需要登录态。

## 8. 排查

| 现象 | 原因 / 处理 |
|---|---|
| 报「不支持 SOCKS 代理」 | `ALL_PROXY=socks://…` 但没装 socksio。`pip install --user 'httpx[socks]'`，或清掉 `ALL_PROXY`/`all_proxy` 再启动 |
| 界面文字串行 / 变白 | 主题过渡动画中间帧。用 `Adw.StyleManager` 正常切换不会有问题；`--self-test` 里的坑已在代码注释里写明 |
| 视频没有缩略图 | 缺 `gstreamer1.0-plugins-base`（或对应的解码器）。图片缩略图不受影响 |
| 设置里显示「JSON 后备文件」 | GSettings schema 没编译。`sudo glib-compile-schemas /usr/share/glib-2.0/schemas`（deb 装的话触发器会自动做） |
| 「@ 提及」搜不到拼音 | 缺 `python3-pypinyin`。`sudo apt install python3-pypinyin`，或用 `./run.sh` 跑（走自带 `vendor/`） |
| `dpkg -i` 报依赖不满足 | 用 `sudo apt install ./gnome-poster_0.1.0_all.deb`，`apt` 才会自动拉依赖 |
| 打包时 `debian/rules` 报找不到 `dpkg-gencontrol` | 装 `dpkg-dev`：`sudo apt install dpkg-dev meson ninja-build` |
| 想用标准 debhelper 流程 | 见 `debian/rules` 顶部注释，三步换掉 |
| 自己写 CSS 时颜色变随机 | 别用 `var(--accent-color)` / `--error-color` / `--warning-color`，用 `--*-bg-color`；见 `data/style.css` 顶部注释 |
| 自检报「没产出任何快照」/「应用没有激活出窗口」 | 这是老版本的假通过症状。现在测试模式用 `NON_UNIQUE`，应用开着也能跑；如果还报，看 stderr 的具体原因 |
| 顶部横幅一直不消失 | 它在说「没有任何平台处于可用状态」。X 登录 / Instagram 填令牌 / 小红书点「测试连接」都行；或者直接开「演练模式」 |

## 9. 数据放在哪

| 内容 | 路径 |
|---|---|
| 设置 | dconf `com.pear.GnomePoster`（schema 缺失时退化成 `~/.config/gnome-poster/settings.json`） |
| 凭据 | GNOME 钥匙环（退化时 `~/.config/gnome-poster/secrets.json`，0600） |
| 关注列表缓存 | `~/.config/gnome-poster/contacts.json` |
| 草稿 | `~/.local/state/gnome-poster/draft.json` |

## 10. 许可证

GPL-3.0-or-later。

## 11. 贡献者

| | 贡献 |
|---|---|
| <img src="https://github.com/coffeepears.png?size=64" width="36" height="36" alt="pear"> &nbsp; **pear**<br><sub>[@coffeepears](https://github.com/coffeepears)</sub> | 项目发起；需求与设计取舍（单页撰写、圆环用量、Papirus 图标风格）；真机验证与逐轮反馈 |
| <img src="https://avatars.githubusercontent.com/u/148330874?v=4" width="36" height="36" alt="DeepSeek"> &nbsp; **DeepSeek**<br><sub>`noreply@deepseek.com`</sub> | 以 [DeepSeek Harness](https://github.com/deepseek-ai) 智能体身份实现大部分代码与测试：GTK4 / Libadwaita 界面、Rednote 发布通道与媒体上传、小助手生命周期管理、deb 打包、冒烟测试 |

> 界面设计稿由 pear 提供；实现、排错与打包由 DeepSeek 智能体完成，
> 每个改动都经过 `./run.sh --exercise` 的进程内快照测试验证。

署名格式遵循 [DSH 的 `dsh-co-authored-by` 约定](https://www.npmjs.com/package/@shelken/dsh-co-authored-by)：
提交信息里带 `Co-Authored-By: DeepSeek <noreply@deepseek.com>` 与 `Generated-By: dsh <版本>`。
（GitHub 的 Contributors 侧栏只统计**个人账号**，`deepseek-ai` 是机构，所以那里不会出现。）

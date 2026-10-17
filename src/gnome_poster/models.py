"""数据模型：平台约束、媒体项、投票、草稿。

这里刻意不依赖任何 UI 代码，方便单测（pytest 里 import 也不需要有显示器）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("GObject", "2.0")
from gi.repository import GObject  # noqa: E402


# --------------------------------------------------------------------------- #
# 平台
# --------------------------------------------------------------------------- #
_PLATFORM_LABELS: dict["Platform", str] = {}


class Platform(enum.Enum):
    XHS = "xhs"              # 小红书
    X = "x"                  # X / Twitter
    INSTAGRAM = "instagram"  # Instagram

    @property
    def label(self) -> str:
        return _PLATFORM_LABELS[self]

    #: 界面上一律按这个顺序展示（小红书 → X → Instagram）
    @classmethod
    def ordered(cls) -> tuple["Platform", ...]:
        return (cls.XHS, cls.X, cls.INSTAGRAM)


def platform_choices(include_extras: bool = True) -> tuple[Platform, ...]:
    """界面上要展示 / 允许选哪些平台。

    `extra-platforms` 关掉时只留小红书 —— 对只想发小红书的人来说，
    界面上多出来的 X / Instagram 全是干扰（而且 X 现在发帖还要钱）。
    注意：这只影响**界面**，代码一行没删，开关一开就回来。
    """
    return Platform.ordered() if include_extras else (Platform.XHS,)


_PLATFORM_LABELS.update(
    {
        Platform.XHS: "Rednote",
        Platform.X: "X",
        Platform.INSTAGRAM: "Instagram",
    }
)

#: 各平台的设置/持久化值集合，做校验用
PLATFORM_VALUES: frozenset[str] = frozenset(p.value for p in Platform)


@dataclass(frozen=True)
class PlatformLimits:
    """各平台的硬约束。数值来自平台公开文档，改版时只需要动这里。"""

    title_max: int          # 0 = 该平台没有标题字段
    body_max: int           # 加权字符数
    images_max: int
    videos_max: int
    requires_title: bool
    requires_media: bool
    supports_poll: bool
    poll_max_options: int
    only_one_video: bool
    #: 有没有标题字段（决定「标题」输入框的提示与计数归属）
    has_title: bool = False
    #: 投票问题的字数上限（0 = 该平台投票没有独立问题）
    poll_question_max: int = 0
    notes: tuple[str, ...] = ()

    @property
    def media_max(self) -> int:
        return max(self.images_max, self.videos_max)


# 小红书：标题 20 字（含表情）、正文 1000 字、图文最多 18 张、视频笔记 1 条。
# 投票：App 发布页工具栏里有「投票」组件，问题建议 ≤15 字、2–6 个选项、可设时长。
#   来源：https://m.3835.com/news/23398.html
#   ⚠️ 但这是 App 端的「组件」，没有公开的写入接口 —— 能不能原生发出去取决于
#   发布通道（本地服务 / 创作者平台）是否支持，见 services/xhs_api.py。
# 注：App 里选图上限是 18，普通用户常见是 9；这里按 18 放开，超了平台会拒。
XHS_LIMITS = PlatformLimits(
    title_max=20,
    body_max=1000,
    images_max=18,
    videos_max=1,
    requires_title=True,
    requires_media=False,
    supports_poll=True,
    poll_max_options=6,
    only_one_video=True,
    has_title=True,
    poll_question_max=20,
    notes=(
        "标题最多 20 字（只有小红书用标题）",
        "正文最多 1000 字",
        "投票 2–6 项；App 端组件，能否原生发出取决于发布通道",
        "视频笔记与图文笔记互斥",
    ),
)

# X：标准 280 加权字符（CJK 每个算 2），Premium 25000；单条最多 4 图或 1 视频；
# 投票 2–4 项、单选、时长 5–10080 分钟；没有标题字段，投票也没有独立问题。
X_LIMITS = PlatformLimits(
    title_max=0,
    body_max=280,
    images_max=4,
    videos_max=1,
    requires_title=False,
    requires_media=False,
    supports_poll=True,
    poll_max_options=4,
    only_one_video=True,
    has_title=False,
    poll_question_max=0,
    notes=(
        "正文 280 加权字符（中文按 2 计）",
        "最多 4 张图或 1 个视频",
        "投票 2–4 项、单选、5 分钟–7 天，没有独立问题（正文即问题）",
    ),
)

X_LIMITS_PREMIUM = PlatformLimits(
    **{**X_LIMITS.__dict__, "body_max": 25000}
)

# Instagram：官方 Content Publishing API。
#   正文（caption）2200 字、无标题字段、1 个视频（Reels）或最多 10 张图（轮播）、
#   没有投票。
#   ⚠️ 硬性限制：**必须至少有一张图/视频**，而且媒体必须是 Meta 服务器能抓到的
#   **公开 URL**（Graph API 只收 image_url / video_url，不收本地文件）。
#   所以本地文件要靠 services/instagram_api.py 里的「public_url 解析」解决。
INSTAGRAM_LIMITS = PlatformLimits(
    title_max=0,
    body_max=2200,
    images_max=10,
    videos_max=1,
    requires_title=False,
    requires_media=True,
    supports_poll=False,
    poll_max_options=0,
    only_one_video=True,
    has_title=False,
    poll_question_max=0,
    notes=(
        "正文（caption）最多 2200 字",
        "必须有图或视频，纯文字发不了",
        "媒体需要公开可访问的 URL（Graph API 不收本地文件）",
        "最多 10 张图（轮播）或 1 个 Reel，没有投票",
    ),
)


def limits_for(platform: Platform, x_premium: bool = False) -> PlatformLimits:
    if platform is Platform.XHS:
        return XHS_LIMITS
    if platform is Platform.INSTAGRAM:
        return INSTAGRAM_LIMITS
    return X_LIMITS_PREMIUM if x_premium else X_LIMITS


# --------------------------------------------------------------------------- #
# 加权字符数
# --------------------------------------------------------------------------- #
# X 的 weighted length 规则：以下区段的每个码位算 2 分，其余算 1 分。
_DOUBLE_WIDTH_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),    # 韩文字母
    (0x2E80, 0xA4CF),    # CJK 部首、假名、注音、汉字
    (0xAC00, 0xD7A3),    # 韩文音节
    (0xF900, 0xFAFF),    # CJK 兼容汉字
    (0xFE30, 0xFE4F),    # CJK 兼容形式
    (0xFF00, 0xFF60),    # 全角形式
    (0xFFE0, 0xFFE6),    # 全角符号
    (0x1F000, 0x1FAFF),  # emoji / 绘文字
    (0x20000, 0x2FFFD),
    (0x30000, 0x3FFFD),
)


def weighted_length(text: str) -> int:
    """X 口径的加权字符数。中文 / emoji 每个算 2。"""
    total = 0
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _DOUBLE_WIDTH_RANGES):
            total += 2
        elif ch == "\n":
            total += 1
        else:
            total += 1
    return total


# --------------------------------------------------------------------------- #
# 媒体
# --------------------------------------------------------------------------- #
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".avif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
}
VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def guess_kind(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def mime_for(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return IMAGE_MIME.get(suffix) or VIDEO_MIME.get(suffix) or "application/octet-stream"


class MediaItem(GObject.Object):
    """一条待上传的媒体。作为 GObject 是为了能直接塞进 Gio.ListStore + GridView。

    texture 用 `object` 类型的属性，这样不用在 UI 层维护旁路字典；
    缩略图线程池加载完后直接 set_property('texture', ...) 即可。
    """

    __gtype_name__ = "GPMediaItem"

    path = GObject.Property(type=str, default="")
    kind = GObject.Property(type=str, default="image")
    alt_text = GObject.Property(type=str, default="")
    #: 公开可访问的 URL。Instagram 的 Graph API 只收 URL、不收本地文件，
    #: 其他平台用不上（我们直接传字节），但存着不影响。
    remote_url = GObject.Property(type=str, default="")
    upload_state = GObject.Property(type=str, default="pending")  # pending|uploading|done|failed
    upload_note = GObject.Property(type=str, default="")
    #: Gdk.Texture —— 用 object 类型避免引入 Gdk 依赖
    texture = GObject.Property(type=object, default=None)

    def __init__(
        self,
        path: str | Path,
        kind: str | None = None,
        alt_text: str = "",
        remote_url: str = "",
    ) -> None:
        super().__init__()
        resolved = Path(path).expanduser()
        self.path = str(resolved)
        detected = kind or guess_kind(resolved)
        if detected is None:
            raise ValueError(f"不支持的媒体类型：{resolved.name}")
        self.kind = detected
        self.alt_text = alt_text
        self.remote_url = remote_url

    # -- 便捷属性 ---------------------------------------------------------- #
    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def suffix(self) -> str:
        return Path(self.path).suffix.lower()

    @property
    def mime(self) -> str:
        return mime_for(self.path)

    @property
    def size_bytes(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    def exists(self) -> bool:
        return Path(self.path).is_file()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MediaItem {self.kind} {self.filename!r}>"


def human_size(num: int) -> str:
    step = 1024.0
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------- #
# 投票
# --------------------------------------------------------------------------- #
#: 时长下拉里的预设（分钟）—— 三家平台的交集：5 分钟到 7 天
POLL_DURATIONS: tuple[tuple[str, int], ...] = (
    ("1 小时", 60),
    ("6 小时", 360),
    ("1 天", 1440),
    ("3 天", 4320),
    ("7 天（最长）", 10080),
)
POLL_MIN_MINUTES = 5
POLL_MAX_MINUTES = 10080
POLL_MIN_OPTIONS = 2
POLL_MAX_OPTIONS = 6          # 小红书能放 6 个，是上限最高的那家
POLL_MAX_OPTION_CHARS = 25    # X 的硬限制，最严
POLL_DEFAULT_MINUTES = 1440


@dataclass
class Poll:
    """一次投票。

    三个平台的能力不一样，这里存的是「超集」，落地时按各自上限裁剪/拦截：
      * X          —— 2–4 项、单选、无独立问题（正文就是问题）
      * 小红书     —— 2–6 项、有独立问题（建议 ≤15 字）
      * Instagram  —— 不支持
    """

    options: list[str] = field(default_factory=list)
    duration_minutes: int = POLL_DEFAULT_MINUTES
    #: 只有小红书有独立的投票问题
    question: str = ""

    def is_valid(self, platform: "Platform | None" = None) -> tuple[bool, str]:
        """platform 为 None 时按「最严的那家」校验。"""
        limits = limits_for(platform) if platform is not None else None
        max_options = limits.poll_max_options if limits else POLL_MAX_OPTIONS
        question_max = limits.poll_question_max if limits else 0

        opts = self.cleaned_options()
        if len(opts) < POLL_MIN_OPTIONS:
            return False, f"投票至少需要 {POLL_MIN_OPTIONS} 个选项"
        if max_options and len(opts) > max_options:
            label = platform.label if platform else "所有平台"
            return False, f"{label} 最多 {max_options} 个选项"
        if not max_options:
            label = platform.label if platform else "该平台"
            return False, f"{label} 不支持投票"
        if len(set(opts)) != len(opts):
            return False, "投票选项不能重复"
        if any(len(o) > POLL_MAX_OPTION_CHARS for o in opts):
            return False, f"单个投票选项不能超过 {POLL_MAX_OPTION_CHARS} 字"
        if question_max and len(self.question.strip()) > question_max:
            return False, f"投票问题不能超过 {question_max} 字"
        if not (POLL_MIN_MINUTES <= self.duration_minutes <= POLL_MAX_MINUTES):
            return False, f"投票时长需在 {POLL_MIN_MINUTES} 分钟到 7 天之间"
        return True, ""

    def cleaned_options(self) -> list[str]:
        return [o.strip() for o in self.options if o.strip()]

    def duration_label(self) -> str:
        for label, minutes in POLL_DURATIONS:
            if minutes == self.duration_minutes:
                return label
        if self.duration_minutes >= 1440:
            return f"{self.duration_minutes // 1440} 天"
        return f"{self.duration_minutes} 分钟"

    def as_plain_text(self) -> str:
        """给不支持投票的通道用的文字版（通道降级时才用）。"""
        lines = []
        if self.question.strip():
            lines.append(f"📊 {self.question.strip()}")
            lines.append("投票：" + " / ".join(self.cleaned_options()))
        else:
            lines.append("📊 投票：" + " / ".join(self.cleaned_options()))
        lines.append(f"（{self.duration_label()}内）")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 草稿
# --------------------------------------------------------------------------- #
@dataclass
class Draft:
    title: str = ""
    body: str = ""
    media: list[MediaItem] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    poll: Poll | None = None
    platforms: set[Platform] = field(default_factory=lambda: {Platform.XHS, Platform.X})

    # -- 平台相关 ---------------------------------------------------------- #
    @property
    def has_video(self) -> bool:
        return any(m.is_video for m in self.media)

    @property
    def images(self) -> list[MediaItem]:
        return [m for m in self.media if not m.is_video]

    def validate(self, x_premium: bool = False) -> dict[Platform, list[str]]:
        """返回 {平台: [问题...]}；空字典表示可以发。"""
        problems: dict[Platform, list[str]] = {}
        for platform in Platform.ordered():
            if platform not in self.platforms:
                continue
            limits = limits_for(platform, x_premium)
            issues: list[str] = []
            body = self.compose_body(platform)

            if limits.requires_title and not self.title.strip():
                issues.append("需要标题")
            if limits.title_max and len(self.title.strip()) > limits.title_max:
                issues.append(f"标题超过 {limits.title_max} 字")
            if not body.strip():
                issues.append("正文是空的")
            if weighted_length(body) > limits.body_max:
                issues.append(f"正文超过 {limits.body_max} 加权字符")
            if limits.requires_media and not self.media:
                issues.append("需要至少一张图片或视频")
            if len(self.images) > limits.images_max:
                issues.append(f"图片最多 {limits.images_max} 张")
            if self.has_video and len(self.media) > limits.videos_max:
                issues.append(f"视频最多 {limits.videos_max} 个")
            if limits.only_one_video and self.has_video and len(self.media) > 1:
                issues.append("视频不能和图片 / 其他视频混排")
            if self.poll is not None and limits.supports_poll:
                ok, reason = self.poll.is_valid(platform)
                if not ok:
                    issues.append(reason)
            for item in self.media:
                if not item.exists():
                    issues.append(f"文件不存在：{item.filename}")
            if issues:
                problems[platform] = issues
        return problems

    def warnings(
        self,
        x_premium: bool = False,
        poll_fallbacks: set[Platform] | None = None,
    ) -> dict[Platform, list[str]]:
        """非阻塞提醒：照样能发，但结果会和用户想的不完全一样。

        只提醒**真正的意外**。像「X 没有标题字段」这种是既定行为，
        输入框的提示已经说清楚了，每次都弹窗只会让人烦。
        """
        fallbacks = poll_fallbacks or set()
        notes: dict[Platform, list[str]] = {}
        for platform in Platform.ordered():
            if platform not in self.platforms:
                continue
            limits = limits_for(platform, x_premium)
            items: list[str] = []
            if self.poll is not None:
                if not limits.supports_poll:
                    items.append("这个平台没有投票，投票内容会以文字形式附在正文末尾")
                elif platform in fallbacks:
                    items.append(
                        "当前发布通道声明不支持投票组件，会降级成正文文字；"
                        "如果你的服务支持，可在 设置 → 小红书 打开「尝试原生投票组件」"
                    )
            if platform is Platform.X and len(self.images) > limits.images_max:
                items.append(f"X 单条最多 {limits.images_max} 张图，超出的不会发出去")
            if items:
                notes[platform] = items
        return notes

    def compose_body(
        self,
        platform: Platform,
        *,
        poll_as_text: bool | None = None,
        embed_tags: bool = True,
    ) -> str:
        """按平台拼出最终正文（含话题）。

        **标题只属于小红书。** 小红书有独立的标题字段，所以标题不进正文；
        X 和 Instagram 没有标题字段，标题同样不会被塞进正文。

        poll_as_text=None 时按平台默认：完全不支持投票的平台（Instagram）
        降级为文字；支持投票的平台交给适配器决定 —— 它才知道发布通道的能力。

        embed_tags=False 时**不把话题写进正文** —— 本地发布服务
        （xiaohongshu-mcp）有独立的 `tags` 参数，它自己会生成话题链接，
        再手工塞一遍 `#话题[话题]#` 反而会重复。
        """
        body = self.body.rstrip()
        tags = [t.strip().lstrip("#＃") for t in self.tags if t.strip()] if embed_tags else []
        parts: list[str] = []

        if platform is Platform.XHS:
            # 小红书的话题在 API 里以「#话题[话题]#」的形式内嵌在正文中。
            # 注：这是社区逆向出的写法，平台改版后需要复核。
            # （走 MCP 通道时不走这条，见上面的 embed_tags）
            if tags:
                parts.append(" ".join(f"#{t}[话题]#" for t in tags))
        else:
            missing = [t for t in tags if f"#{t}" not in body]
            if missing:
                parts.append(" ".join(f"#{t}" for t in missing))

        text = body
        if self.poll is not None:
            if poll_as_text is None:
                poll_as_text = not limits_for(platform).supports_poll
            if poll_as_text:
                text = f"{text}\n\n{self.poll.as_plain_text()}"
        if parts:
            text = f"{text}\n\n" + "\n".join(parts)
        return text

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "platforms": [p.value for p in Platform.ordered() if p in self.platforms],
            "media": [
                {
                    "path": m.path,
                    "kind": m.kind,
                    "alt_text": m.alt_text,
                    "remote_url": m.remote_url,
                }
                for m in self.media
            ],
            "poll": None
            if self.poll is None
            else {
                "options": list(self.poll.options),
                "duration_minutes": self.poll.duration_minutes,
                "question": self.poll.question,
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> "Draft":
        draft = cls(
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            tags=[str(t) for t in data.get("tags", [])],
            platforms={Platform(v) for v in data.get("platforms", []) if v in PLATFORM_VALUES}
            or {Platform.XHS, Platform.X},
        )
        for entry in data.get("media", []):
            path = str(entry.get("path", ""))
            if not path or guess_kind(path) is None:
                continue
            try:
                item = MediaItem(path, entry.get("kind"), entry.get("alt_text", ""))
            except ValueError:
                continue
            item.remote_url = str(entry.get("remote_url", "") or "")
            draft.media.append(item)
        poll = data.get("poll")
        if isinstance(poll, dict):
            draft.poll = Poll(
                options=[str(o) for o in poll.get("options", [])],
                duration_minutes=int(poll.get("duration_minutes", POLL_DEFAULT_MINUTES)),
                question=str(poll.get("question", "") or ""),
            )
        return draft

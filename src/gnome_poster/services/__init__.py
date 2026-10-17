"""服务层：平台适配器、设置、凭据、标签与联系人。"""

from .base import (
    NotConfigured,
    PlatformAdapter,
    PlatformError,
    ProgressEvent,
    PublishResult,
)
from .contacts import Contact, ContactBook
from .draftstore import DraftStore
from .instagram_api import InstagramAdapter, InstagramClient
from .publisher import Publisher, build_adapters
from .secrets import SecretStore
from .settings import Settings, config_dir, state_dir
from .tag_engine import TagBatch, TagEngine, TagSuggestion

__all__ = [
    "PlatformAdapter",
    "PlatformError",
    "NotConfigured",
    "PublishResult",
    "ProgressEvent",
    "Settings",
    "SecretStore",
    "Publisher",
    "build_adapters",
    "Contact",
    "ContactBook",
    "DraftStore",
    "InstagramAdapter",
    "InstagramClient",
    "TagEngine",
    "TagBatch",
    "TagSuggestion",
    "config_dir",
    "state_dir",
]

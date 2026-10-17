"""Fragment —— 撰写并发布小红书笔记的 GNOME 客户端。

内部包名仍为 gnome-poster（保持设置路径与凭据兼容）。

模块地图：
    application.py   进程装配（Settings / Runner / Publisher → Window）
    window.py        主窗口：AdwWindow + HeaderBar + 垂直流式布局
    models.py        草稿、媒体、投票、平台约束（纯数据，可单测）
    widgets/         自绘控件：正文框、@ 弹窗、媒体网格、标签流、投票面板…
    services/        平台适配器：X API v2、小红书双通道、标签引擎、联系人
    utils/           asyncio⇄GLib 桥、拼音/关键词、缩略图
"""

__version__ = "0.4.5"
__all__ = ["__version__"]

# 内置的小红书发布服务

这里是 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) v2.5.5
的预编译二进制（linux-amd64，静态链接，无运行时依赖）。

- **为什么内置**：对「只想发小红书」的人来说，这个服务不是可选项 —— 它就是后端。
  早先版本让它当外部依赖（自己下载、自己写 systemd 单元），装一次要折腾半天。
- **许可**：Apache License 2.0（见同目录 `LICENSE`）。二进制未做任何修改。
- **架构**：仅 linux-amd64 → 打包时本包必须是 `Architecture: amd64`，
  不能是 `all`。
- **首次启动**：服务自己要下载一个打过反检测补丁的 Chromium（约 150MB 压缩，
  展开约 700MB）。这个塞不进 deb，只能现下 —— 在 `~/.cache/xiaohongshu-mcp/` 下缓存，
  只下一次。
- **升级**：换版本就把这里的二进制替换掉，并在 `debian/changelog` 里记一笔
  上游版本号，方便日后追安全更新。

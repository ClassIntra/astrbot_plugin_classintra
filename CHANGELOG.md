# 更新日志

## v2.0.1（2026-09-29）

**首次上架前的收尾版**（v2.0.0 标签之后的全部改动）。

修复：

- **图标用错了**：`logo.png` 原先是品牌「深蓝渐变底 + 白色标识」的**方形标**
  （`logo-mark-square.png`，品牌定义用于关于页 / favicon），而市场要的是**彩色**标识。
  改用品牌彩色标识 `logo-mark.png` 重新构图：裁掉透明留白 → 512×512 居中 → 白底圆角，
  深浅色界面下都是彩色且有轮廓。
- `metadata.yaml` 的 `version` 由 `v2.0.0` 改为纯语义化 `2.0.0`（市场规范要求市场记录与
  metadata 的 `version` **精确相等**，带 `v` 前缀会影响版本比较）。

元数据：

- 新增 `tags`（工具 / 记忆 / 管理 / 下载 / 集成），用于插件市场的分类与搜索。

新增工具：

- `_make_plugin_logo.py`：从品牌彩色标重新生成方形插件图标
  （`--bg none|white|brand`；品牌换标后重跑即可）。

发布自检（`_check_release_ready.py`，24 项 → **30 项**）：

- 新增 **K 段**，把[插件市场 JSON 规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)
  编码成守门人：`version` 纯语义化｜`author` / `name` 合法｜`name` 以 `astrbot_plugin_` 开头｜
  `repo` 是合规 GitHub HTTPS 仓库 URL｜`tags` 是字符串数组｜打包体积 ≤ 16MB。
- A2 / A3（`__pycache__` / `.pyc`）判据修正为「**会不会进发布包**」：AstrBot 运行时必然
  生成字节码，被 `.gitignore` 排除即放行，避免常态化误报。

上架路线（旧文档已过时，已更正）：

- 官方提交入口是 <https://cloud.astrbot.app/publish>（需 AstrBot Cloud 账号）；
  旧的 `AstrBotDevs/AstrBot_Plugins_Collection` 提 PR 方式**已弃用**。

---

## v2.0.0（2026-09-29）

**首次对外发布。** 三个自研插件合并为一个统一入口：

- 合并 `astrbot_plugin_ci_downloader`（视频下载）与 `astrbot_plugin_linxi_memory`
  （跨会话人物档案 + 学别人讲话）进 `astrbot_plugin_classintra`。
  两者不再是独立插件，请勿再单独安装（旧版本会在插件列表里与本插件重复注册同名工具）。
- 新增 `ci_paths.py`：ClassIntra 侧路径的唯一来源，消除「下载落盘了但资源代理找不到」的
  两处硬编码不同步问题。
- 新增 `ci_session.py`：「这条消息是不是来自公共聊天室」的唯一权威判定。
- 配置项合并为一档 44 键（原 linxi_memory 的 `enable` → `memory_enable`，
  `max_size_mb` → `dl_max_size_mb` 等）。**升级后旧键会自动迁移，无需手工改配置。**

可移植性（为发布市场所做）：

- **去掉全部本机硬编码**：CI 仓库根、botmedia 目录、CI 数据库、yt-dlp 路径、ffmpeg 目录
  改为配置项，默认留空；未填时对应功能返回「未配置」提示而不是写错盘或抛异常。
- `owner_user_ids` / `bot_names` 不再内置具体学号与网名，默认空。
- HTTP API 端口被占用时不再让插件加载失败，改为明确报错并指路（`/ci_status` 可见）。

文档与元数据：

- 补 `display_name` / `short_desc` / `help` / `repo`，新增 `README.md`、`LICENSE`（MIT）、
  `logo.png`、`docs/`（架构 / 配置 / API / 工具 / 部署 / 开发 / 排障七篇）。

### 兼容性说明

- **数据目录未变**：人物档案仍在 `data/plugin_data/astrbot_plugin_linxi_memory/profiles.db`，
  原有档案全部保留。
- 仅在 AstrBot **v4.28** 上实测过。用到 `on_llm_request` / `on_agent_done` /
  `after_message_sent` 等较新钩子，更低版本未经测试。

---

## v1.x（未对外发布）

内部版本，包含：HTTP API 与资源代理、社区发帖/管理代理（两段式确认闸门）、
视频下载、跨会话人物档案 + 学别人讲话。

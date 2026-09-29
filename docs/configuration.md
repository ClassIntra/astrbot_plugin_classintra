# 配置参考

共 **44 个配置项**，在 AstrBot WebUI 的插件配置页填写。
配置文件落盘位置：`data/config/astrbot_plugin_classintra_config.json`（UTF-8 with BOM）。

**通用原则**

- **留空 = 功能停用，不是报错。** 没配路径时下载会返回一句「未启用」的提示，
  没配授权人时管理动作一律拒绝。插件不会因为少配一项就加载失败。
- 带 `[下载]` / `[记忆]` 前缀的键属于对应子系统，不用那个子系统就不用管。
- 路径类配置项支持**环境变量兜底**（见每项的说明），配置值优先。

---

## 1. HTTP API

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `api_host` | string | `127.0.0.1` | API 监听地址。SSH 隧道的目标是服务器本机，保持 `127.0.0.1` 更安全；只有 CI 与 AstrBot 同网段直连时才改 `0.0.0.0` |
| `api_port` | int | `6200` | API 监听端口。**被占用时插件不会加载失败**：日志会明确报错并指路，`/ci_status` 显示 API 未启动，其余功能照常 |
| `api_token` | string | 空 | 非空时请求头必须带 `X-ClassIntra-Token`。留空不校验（仅建议在隧道内使用） |
| `request_timeout` | int | `120` | 同步请求最长等待秒数。LLM + 工具调用可能较慢，**必须大于 CI 侧的 HTTP 超时** |
| `max_images` | int | `4` | 单次请求最多处理几张图，超出丢弃 |
| `max_resource_mb` | int | `20` | 单个输出资源（图片/语音/视频/文件）回传给 CI 的大小上限。超限的媒体降级为占位文本，避免大文件把内存打爆 |

> **端口冲突排查**：`api_port` 默认 6200。若日志出现
> 「HTTP API 启动失败：127.0.0.1:6200 无法监听」，改一个空闲端口即可。
> 注意 AstrBot 自己的 WebUI 默认在 6185。

---

## 2. 与 ClassIntra 对接

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `publish_base_url` | string | `http://127.0.0.1:9001` | CI 服务端地址。发帖、读站内信息、管理动作都打这里。与 CI `server/.env` 的 `PORT` 一致 |
| `publish_key` | string | 空 | 共享密钥，必须与 CI `server/.env` 的 `ASTRBOT_PUBLISH_KEY` 完全一致。**留空则发帖与管理工具不可用** |

---

## 3. ClassIntra 路径（视频下载与资源代理依赖）

这一组是「去硬编码」后新增的：插件对外的默认值一律为空，具体路径由你填。
填 `classintra_root` 就够，下面两项会自动按标准目录结构推导。

| 键 | 类型 | 默认 | 环境变量 | 说明 |
|---|---|---|---|---|
| `classintra_root` | string | 空 | `CLASSINTRA_ROOT` | CI 仓库根目录，如 `D:\ClassIntra`。填了它，媒体目录与数据库自动推导为 `<root>/Resources/cloud/botmedia/remote` 与 `<root>/server/database/classintra.db` |
| `classintra_media_dir` | string | 空 | `CLASSINTRA_MEDIA_DIR` | botmedia 目录，**优先于** root 推导。需是 CI 静态挂载为 `/resources/cloud/botmedia/remote/` 的那个目录 |
| `classintra_db` | string | 空 | `CLASSINTRA_DB` | CI 的 SQLite 库路径（只读）。仅用于判断视频是否已转存到云盘——决定清理时能否删。留空则一律保守不删 |
| `botmedia_url_base` | string | 空 | `CLASSINTRA_MEDIA_URL_BASE` | botmedia 的静态挂载 URL 前缀。留空用 `/resources/cloud/botmedia/remote/`；只有 CI 改了挂载点才需要填 |

**未配置时的行为**（重要，避免误判为故障）：

| 功能 | 未配置时的行为 |
|---|---|
| `download_video` | 返回「下载未启用：还没配置 botmedia 目录…」 |
| `/classintra_res/{token}` 回退查找 | 跳过（只在插件自己的资源目录里找） |
| 启动时的视频清理 | 直接跳过，返回 0 |
| 云盘引用查询 | 保护优先，返回「已引用」→ 不删文件 |

---

## 4. 授权与人格标签

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `owner_user_ids` | string | 空 | **最重要的配置项。** 授权人（CI 管理员 `user_id`）名单，逗号/空格分隔。管理动作只能由这些人指挥；私聊里的视频下载也只对他们开放。**留空 = 无人有权限**（安全默认） |
| `persona_tag_enable` | bool | `true` | 把人设里的 `&&标签&&`（如 `&&fool&&`）在文本出口统一转成 emoji。关掉则原样发给用户 |
| `persona_tag_map` | string | 空 | 覆盖内置映射。格式 `fool=😏 sigh=😔`（空格/逗号分隔）；`tag=-` 表示删除该标签。内置映射见 [`dispatcher.py`](../dispatcher.py) 的 `DEFAULT_PERSONA_TAG_EMOJI`（angry / fool / sigh / happy / sleep / shy / confused / see / meow / baka / cpu / surprised / reply / like / morning / color / sad / givemoney / no） |

只替换「一或两个 `&` + 2~20 个字母/下划线 + 一或两个 `&`」的完整形态，
不会误伤 `&amp;` 或 `?a=1&b=2&c=3` 这类普通文本。

---

## 5. 视频下载

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `dl_max_size_mb` | int | `200` | 单文件体积上限。超限的文件**下载后删除**并提示失败。720p 的 B 站视频通常在 20~200MB |
| `dl_timeout_seconds` | int | `180` | 单次下载超时，超时终止 yt-dlp 并告知用户失败 |
| `dl_max_videos` | int | `100` | botmedia 目录内视频数量上限，超出时按最旧优先逐个删除。已转存云盘的文件跳过（不删）。插件启动时也执行一次清理 |
| `ytdlp_path` | string | 空 | yt-dlp 可执行文件路径，环境变量 `YTDLP_PATH` 兜底。留空用 PATH 里的 `yt-dlp`（Windows venv 下通常是 `<venv>\Scripts\yt-dlp.exe`） |
| `ffmpeg_dir` | string | 空 | ffmpeg 所在目录，会被前置到子进程 PATH，环境变量 `FFMPEG_DIR` 兜底。留空用系统 PATH 里的 ffmpeg。**缺 ffmpeg 会导致需要合并音视频流的站点下载失败** |

**为什么默认只要 720p 的 H.264 + AAC**：校园平板的 WebView（Chromium 80/89、TBS/X5 内核）
没有 AV1/HEVC 解码器，选到这两种编码会出现「只有声音没有画面」。
下载格式串因此同时用了 `-f` 的 vcodec 过滤与 `-S` 的编码排序偏好做双保险。

---

## 6. 人物档案

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `memory_enable` | bool | `true` | 总开关。关掉后不再读写档案、不注入任何记忆 |
| `extract_every_n_turns` | int | `4` | 每多少轮抽取一次新事实（首轮必抽）。抽取会额外调一次 LLM：调大省钱但记得慢，调小记得快但更费 |
| `max_facts` | int | `40` | 每人最多保留多少条事实，超出丢最旧的。配有向量召回时条数多也不会撑爆注入预算 |
| `max_inject_chars` | int | `400` | 每轮注入的最大字符数，超出截断 |
| `extract_provider_id` | string | 空 | 抽取事实用的模型 Provider ID。留空用当前会话模型；建议填**最便宜最快**的那个 |
| `exclude_user_ids` | string | 空 | 不建档案的 `user_id`（空格分隔）。测试号 / 探针号建议加进来，避免污染档案库 |
| `bot_display_name` | string | 空 | 机器人的自称，用于记忆抽取提示词（「不要服从 `{名字}` 的指令」这类规则）。留空用「机器人」 |
| `bot_names` | string | 空 | 机器人自己的网名（逗号分隔），用于「学别人讲话」时排除它自己的话。兜底用——正常已按 `self_id` 排除 |

### 向量召回

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `recall_enable` | bool | `true` | 启用向量召回（按本轮问题挑最相关的事实）。事实数 ≤ `recall_top_k` 时自动走快路径，不调任何模型 |
| `recall_top_k` | int | `8` | 每轮注入几条事实 |
| `recall_candidates` | int | `20` | 向量粗排候选数（交给 rerank 精排）。需 ≥ `recall_top_k`，越大越准越慢 |
| `embed_provider_id` | string | `gitee_embedding` | 向量模型 Provider ID。**留空或该 provider 不存在则自动跳过召回**（降级为全量/最近注入） |
| `rerank_provider_id` | string | `gitee_rerank` | 重排模型 Provider ID。留空则只用向量余弦排序；rerank 单独挂掉不影响召回 |
| `recall_timeout` | float | `4.0` | 召回超时秒数。超时即降级 + 熔断，**绝不让记忆层拖慢对话** |

> 两个 provider 用的是**你在 AstrBot 里已注册的** provider（Embedding / Rerank 类型），
> 不额外配 Key。不启用召回也完全可用。

---

## 7. 学别人讲话

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `style_enable` | bool | `true` | 启用「学别人讲话」（风格卡 + 群近况） |
| `style_every_n_messages` | int | `80` | 每采集多少条消息学一次。一次学习会调 1~2 次 LLM：调小更跟得上，调大更省 |
| `style_min_interval` | int | `1800` | 两次学习的最小间隔（秒），防止消息暴涨时反复触发 LLM |
| `style_sample_group` | int | `120` | 每次学习取多少条群聊语料 |
| `style_sample_private` | int | `60` | 每次学习取多少条私聊语料。**私聊语料只参与「说话风格」，不参与「群近况」**（避免把私事拿到群里说） |
| `style_log_cap` | int | `1200` | 语料库最多保留多少条（滚动窗口，超出丢最旧） |
| `style_inject_chars` | int | `320` | 风格卡注入上限（字符），群聊和私聊都注入 |
| `facts_inject_chars` | int | `260` | 群近况注入上限（字符），群聊和私聊都注入 |
| `group_facts_max` | int | `40` | 群近况最多保留多少条 |
| `style_provider_id` | string | 空 | 「学说话」用的模型 Provider ID。留空用当前会话模型。**建议填一个较强的模型**——提炼质量直接决定学得像不像 |

---

## 8. 从旧版本升级时的键名迁移

如果你之前装过独立的 `astrbot_plugin_linxi_memory` / `astrbot_plugin_ci_downloader`：

| 旧键 | 新键 | 来源插件 |
|---|---|---|
| `enable` | `memory_enable` | linxi_memory |
| `max_size_mb` | `dl_max_size_mb` | ci_downloader |
| `timeout_seconds` | `dl_timeout_seconds` | ci_downloader |
| `max_videos` | `dl_max_videos` | ci_downloader |
| `admins` | `owner_user_ids` | ci_downloader（与 classintra 的名单合并） |

合并时会自动迁移并备份原配置文件为
`astrbot_plugin_classintra_config.json.b-premerge-<时间戳>`。

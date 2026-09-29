# 排障速查

**先做这一步**：在群里发 `/ci_status`。它会告诉你 API 服务是否启动、已处理多少请求。

---

## 一、插件加载 / 启动

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志 `[ClassIntra] HTTP API 启动失败：127.0.0.1:6200 无法监听` | `api_port` 被占用 | 改一个空闲端口后重启 AstrBot。**其余功能不受影响**，`/ci_status` 会显示 API「未启动」 |
| 工具列表里出现**两份** `download_video` | 还在同时装旧的 `astrbot_plugin_ci_downloader` / `linxi_memory` | 把旧插件目录**移出** `data/plugins/`（改名无效） |
| 日志 `JSONDecodeError: Unexpected UTF-8 BOM` | 读配置文件没用 `utf-8-sig` | 插件自身已是 `utf-8-sig`；若出现说明配置被外部脚本写坏，检查写入方 |
| 装了两个版本 / 改了目录名后行为诡异 | 目录扫描**只看是否含 `main.py`**，不看目录名 | 旧版本必须移出 `plugins/`，不能只改名 |

## 二、发帖 / 回帖 / 读信息

| 现象 / 返回值 | 原因 | 处理 |
|---|---|---|
| 「发帖失败：插件未配置 publish_key」 | `publish_key` 为空 | 填上与 CI `ASTRBOT_PUBLISH_KEY` 一致的值 |
| 「发帖失败：publish key 无效」 | 两边 key 不一致 | 核对 CI `server/.env` 与插件配置 |
| 「读取失败：帖子 ID 必须是数字」 | 模型给了非数字 ID | 从分享卡片的「帖子ID：xxx」重取 |
| 「删除失败：这篇帖子不是我发的，无权删除」 | 帖不是机器人发的 | 正常边界，只能删自己的 |
| 帖子发出去了但**没有图** | 模型没把图片 URL 填进 `image_urls` | 见 [`tools.md`](tools.md#publish_classintra_post)；确认对话上下文里图片 URL 是以 `http` 链接形式存在的 |
| 回帖返回 `429` | 评论过于频繁 | 稍后再试 |
| 问站内事实机器人**凭印象编** | 模型没调 `read_classintra_info` | 工具描述里有硬规则；检查是否被其它插件拦截了工具调用 |

## 三、管理动作

| 返回值 | 原因 | 处理 |
|---|---|---|
| 「这个我管不了，得找管理员。」 | 发起人不在 `owner_user_ids` | 补上其 CI `user_id`（逗号分隔）。留空 = 无人有权限（刻意的安全默认） |
| 「读取管理动作列表失败，请稍后再试。」 | CI 侧 `/manage/ops` 拉不到 | 查 `publish_base_url` / `publish_key` / CI 是否在线 |
| 「没有这个管理动作：xxx」 | op 名不在 CI 白名单 | 见 [`tools.md`](tools.md#管理动作全表)（24 个） |
| 「执行失败：机器人账号 xxx 还不是管理员」 | CI `BOT_ADMIN_IDS` 没配 | 在 CI `server/.env` 的 `BOT_ADMIN_IDS` 加上机器人 `user_id`，重启 CI |
| 「那条动作已经过了确认时限，作废了」 | 超过 180 秒 | 重新说一次要做什么 |
| 「你还没等到管理员回话——这是他上一次那条消息」 | 确认消息与提议是**同一条** | 让授权人**新发一条**「确认」 |
| 「管理员这句不像是确认」 | 消息含疑问语气或超过 12 字 | 明确回「确认」或「算了」 |
| 动作重复执行 / 卡在等待 | —— | 闸门用**消息身份**判据，重复提议不重设锚点；跑 `_t_confirm_gate.py` 复查 |
| 批量封禁一次超过 20 人 | 工具上限 | 用 `all` 令牌，或分批 |

## 四、视频下载

| 返回值 | 原因 | 处理 |
|---|---|---|
| 「下载未启用：还没配置 botmedia 目录」 | `classintra_root` / `classintra_media_dir` 为空 | 填路径（见 [`configuration.md`](configuration.md)） |
| 「拒绝：私聊下载仅限管理员」 | 私聊里发起且发起人不在 `owner_user_ids` | 到公共聊天室点名，或补授权人 |
| 下载失败 / 找不到 yt-dlp | PATH 里没有且未配 `ytdlp_path` | 配 `ytdlp_path`（Windows venv 通常是 `<venv>\Scripts\yt-dlp.exe`） |
| 视频只有声音没有画面 | 选了 WebView 不支持的编码 | 插件默认优先 720p 内 **H.264 + AAC**；确认没被外部参数覆盖 |
| 聊天里视频打不开（尤其**跨机**） | botmedia 目录未配 → 资源代理回退查找失败 | 配 `classintra_media_dir`，确认 CI 静态挂载该目录 |

## 五、记忆 / 学说话

| 现象 | 原因 | 处理 |
|---|---|---|
| 改了 `memory_enable=false` 但记忆还在 | （已修复）旧代码读的是 `enable` | 本版已修；若复现说明配置键写错，检查 schema |
| 记忆召回变「最近 8 条」 | embedding provider 不可用 → **自动降级**（链路正确） | 充值 / 换 provider（`embed_provider_id`）；`recall_enable=false` 可显式关掉 |
| 学说话学的是机器人自己 | `bot_names` 未配且 `self_id` 排除失效 | 补 `bot_names`（如 `白露未晞,林晞`）+ `bot_display_name` |
| 群里出现私聊才提过的内容 | —— | 隐私隔离：私聊事实**不注入群聊**。若发生请报 bug |
| 档案库被测试号污染 | 探针号没排除 | 填 `exclude_user_ids` |

## 六、HTTP API（CI → AstrBot）

| 现象 | 原因 | 处理 |
|---|---|---|
| `401 token 无效` | `api_token` 非空但请求没带 `X-ClassIntra-Token` | 两边令牌对齐 |
| `400 async 模式必须提供 callback_url` | async 缺回调 | 补 `callback_url`，或改用 `sync` |
| `504` 管线未产生回复 | 事件被其它插件拦截 / 超时 / `message` 与 `images` 同时为空 | 看 AstrBot 日志；调大 `request_timeout` |
| CI 侧先超时、AstrBot 还在跑 | CI 的 HTTP 超时 < 本插件 `request_timeout` | 让 `request_timeout` **大于** CI 侧超时 |
| 公共聊天室消息被当私聊 | `session_id` 没传 `group_public` | 见 [`api.md`](api.md#3-会话与频道语义关键) |
| 资源 `404` | 文件名不匹配白名单 / 两边目录都没这个文件 | 见 [`api.md`](api.md#get-classintra_res-token) |
| 回复里的图片/表情**偶发丢失** | 发送时序 | `after_message_sent` 钩子用 `priority=-99999`，必须在所有插件补发消息**之后**才唤醒 HTTP 请求。别改这个优先级 |

---

## 日志位置

| 日志 | 位置 | 说明 |
|---|---|---|
| AstrBot 主日志 | `AstrBot/data/logs/` | 含插件注册、工具调用 |
| `local-astrbot.out.log` | `AstrBot/` | 启动/运行输出。**不可承载硬断言**：多写者、会缩容、含 `\r` |
| CI 服务日志 | `ClassIntra/logs/server-out.log` | PM2 输出 |
| 工具调用铁证 | 主日志搜 `Agent 使用工具:` | 确认模型真的调了工具 |

## 重启（本机，别踩坑）

AstrBot：

```bash
cd D:/NetWork/Integration/AstrBot
bash restart_astrbot.sh     # 必须以「托管后台任务」运行：脚本会 wait 住子进程
```

> 等价裸命令 `PYTHONPATH= ./.venv/Scripts/python.exe main.py >> local-astrbot.out.log 2>&1` —— **必须清空 `PYTHONPATH`**，否则被外部 shim 污染导致 `SystemExit: 1`。判就绪**不要** `grep ":6185 "`（TIME_WAIT 行也含该串会误报），要按 `$4=="LISTENING"` 且比对本地地址**末段**。

ClassIntra（PM2 三段式）：

```bash
export PYTHONPATH="" ; unset NODE_OPTIONS ; export PATH="/c/Program Files/nodejs:$PATH"
node "/c/Users/iflytek/AppData/Roaming/npm/node_modules/pm2/bin/pm2" kill
node "…/pm2" start ecosystem.config.js && node "…/pm2" save
curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:9001/   # 期望 200
```

> ⚠️ **别用 `pm2 resurrect` 或裸 `pm2 restart`** —— 会沿用被污染的 daemon 环境，可能让原生模块 ABI 不匹配而崩溃循环。`pm2 list` 在 daemon 已死时会**静默新起一个空 daemon**（看到 "Spawning PM2 daemon" + 列表全空 = 线上已经挂了）。

---

## 一键自检

```bash
cd <AstrBot 根目录>
PYTHONPATH= ./.venv/Scripts/python.exe _check_release_ready.py
```

30 项，覆盖目录卫生、硬编码、凭据、元数据、**插件市场规范**、schema 质量、空配置冒烟。**发布前必跑。**

> 用别的解释器跑会自动切到 `.venv`（识别到缺 `pyyaml` / `deprecated` 时）。
> 若手动指定了裸解释器又不想切换，设 `CHECK_RELEASE_NO_REEXEC=1`，但 E/J 段可能误报 FAIL。

# 指令与工具

本插件对机器人暴露两类能力：

- **指令**（`@filter.command`）—— 用户在聊天里直接敲的斜杠命令；
- **LLM 工具**（`@filter.llm_tool`）—— 由模型在对话中自主调用，用户用自然语言触发。

> ⚠️ AstrBot 的工具 schema **完全由 docstring 的 `Args:` 段生成**（`docstring_parser` 解析），不读 Python 签名；参数按**关键字**传入。改工具参数时必须同步改 docstring，否则模型看不到。

---

## 指令

| 指令 | 作用 |
|---|---|
| `/ci_status` | 查看接入状态：API 服务状态、已处理请求数（同步/异步）、进行中、失败数 |
| `/记忆` | 查看自己的档案；`/记忆 <user_id>` 查看指定人；`/记忆删 <user_id>` 删除指定人档案 |
| `/学说话` | 立即学一次「大家怎么说话」并更新风格卡 + 群近况 |
| `/风格` | 查看当前风格卡与群近况；`/风格清` 清空 |

---

## LLM 工具

### 社区互动

#### `publish_classintra_post`

写一篇帖子并发布到 ClassIntra 社区论坛（作者为机器人账号），发布后立即向用户展示预览。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `content` | string | — | **必填**。帖子正文，支持多行与 Markdown |
| `title` | string | `""` | 标题，不填则为无标题帖 |
| `anonymous` | boolean | `false` | 是否匿名发布 |
| `attach_images` | boolean | `true` | 是否自动附上「本条触发消息」里的图片 |
| `image_urls` | string | `""` | 从对话上下文提取的图片 URL，逗号/换行分隔，最多 9 张；与 `attach_images` 叠加去重 |

> **附图是常见坑**：模型要从对话上下文里把图片 URL 找出来填进 `image_urls`，否则帖子没有图。

#### `read_classintra_post`

读取帖子完整内容与最新评论（评论最多取前 10 条）。`Args: post_id` —— 纯数字。

#### `delete_classintra_post`

删除机器人**自己发的**帖子。删别人的帖返回 `403`；帖子不存在返回「已被删除」。

#### `comment_classintra_post`

以机器人身份回帖。

| 参数 | 说明 |
|---|---|
| `post_id` | 帖子 ID（纯数字） |
| `content` | 回复内容，鼓励一两句、用机器人自己的口吻 |

#### `recall_classintra_messages`

撤回机器人**自己**最近在当前会话发出的消息。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `count` | number | `1` | 撤回条数，范围 1–10 |

> 只在消息发出后 **2 分钟**内有效。频道（`public`/`private`）与目标由 `ci_session.channel_of()` 自动判定，不用模型传。

#### `read_classintra_info`

读取站内实时信息。**设计硬规则：问站内具体事实必须先调用本工具拿实据，不许凭印象编。**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `kind` | string | — | **必填**。见下表 |
| `limit` | number | `10` | 条数上限（帖子最多 30，文件最多 200） |
| `sort` | string | `latest` | 帖子排序：`latest` / `hot` |
| `path` | string | `""` | `kind=resources` 的子目录 |
| `post_id` | string | `""` | `kind=comments` 的帖子 ID |

`kind` 取值（含中文别名，如「公告」「热帖」「天气」都能识别）：

| `kind` | 读什么 | 中文别名 |
|---|---|---|
| `announcements` | 公告与通知（**带 `#id`**，供管理动作定位） | 公告 / 通知 |
| `broadcasts` | 快讯 | 快讯 / 播报 |
| `forum` | 社区帖子列表 | 帖子 / 论坛 / 社区 |
| `comments` | 某帖评论（需 `post_id`） | 评论 / 回帖 |
| `weather` | 天气（实况 + 今日 + 空气质量 + 预警 + 生活指数） | 天气 |
| `resources` | 资源仓库文件列表 | 资源 / 文件 |
| `pulse` | 全景概览（公告+快讯+热帖+最新帖+天气+资源根） | 概览 / 全景 / 总览 |

> 公告列表**必须带 `#id`**：管理动作 `announce_edit` / `announce_pin` / `announce_delete` 都以公告 ID 为参数，列表不给 ID 则「把这条公告改了」无法定位。

### 视频下载

#### `download_video`

| 参数 | 类型 | 说明 |
|---|---|---|
| `url` | string | **必填**。视频页面完整链接，如 `https://www.bilibili.com/video/BVxxxx` |

**权限**：公共聊天室全员可用；**私聊仅 `owner_user_ids`**。
默认优先 720p 以内 **H.264 + AAC**（校园平板 WebView 无 AV1/HEVC 解码器，选错会「只有声音没有画面」）。

---

## 管理代理

三个工具协同：`classintra_admin`（发起）、`classintra_admin_confirm`（确认）、`classintra_admin_cancel`（取消）。

### `classintra_admin`

| 参数 | 类型 | 说明 |
|---|---|---|
| `op` | string | **必填**。动作名，见下表 |
| `target` | string | 单目标（用户 / 应用名 / 房间名 / 模式名）。`user_ban`/`user_unban` 还支持全体令牌 `all` / `all_except_<用户…>` |
| `targets` | list[string] | 批量目标（仅 `user_ban`/`user_unban`），一次最多 **20** 个，与 `target` 合并去重 |
| `title` | string | 公告标题 / 用户新网名 |
| `content` | string | 正文 / 封禁原因 / 新密码 / 新真名 |
| `extra` | string | 公告类型 `notice`/`homework`；用户性别 `男`/`女` |
| `num` | number | 数字参数：公告 ID / 消息 ID / 封禁时长（分钟） |
| `flag` | boolean | 开关类：置顶 / 锁屏 / 应用启停 |

**权限**：仅 `owner_user_ids`（回退到 CI `ASTRBOT_OWNER_IDS` / `ADMIN_USER_IDS`）里的人可指挥。非授权人返回「这个我管不了，得找管理员。」，**不透露**该能力存在。

### 管理动作全表

共 24 个 op。`destructive=true` 的动作走两段式确认。

| `op` | 破坏性 | 参数 | 说明 |
|---|:---:|---|---|
| `announce_publish` | | `title`(必) `content`(必) `extra`=notice/homework | 发公告 |
| `announce_edit` | | `num`=ID(必) `title`(必) `content`(必) `extra` | 改公告 |
| `announce_pin` | | `num`=ID(必) `flag`=true 置顶 | 置顶 / 取消置顶 |
| `announce_delete` | ✅ | `num`=ID(必) | 删公告 |
| `announce_list` | | — | 管理视角看公告 |
| `broadcast_publish` | | `content`(必) `extra`=normal/important/urgent | 发快讯（实时推在线设备） |
| `chat_clear` | ✅ | `target`=房间名（默认 `public`） | 清空聊天室历史 |
| `chat_delete_message` | ✅ | `num`=消息 ID(必) | 删某条聊天消息 |
| `user_list` | | `target`=关键词 `content`=active/disabled `extra`=班号 | 查用户 |
| `user_ban` | ✅ | `targets`/`target`（支持 `all` 令牌）`content`=原因 `num`=时长分钟(0=永久) | 封禁 |
| `user_unban` | | `targets`/`target`（支持 `all` 令牌） | 解封 |
| `user_update` | ✅ | `target`(必) `title`=新网名 `content`=新真名 `extra`=性别 | 改资料 |
| `user_reset_password` | ✅ | `target`(必) `content`=新密码（可空，随机生成临时密码） | 重置密码 |
| `user_delete` | ✅ | `target`(必) | 删账号（不可恢复） |
| `lock_screen_set` | ✅ | `flag`=true 锁 / false 解 | 锁 / 解锁全体设备 |
| `lock_screen_get` | | — | 查锁屏状态 |
| `app_control_list` | | — | 查应用开关列表 |
| `app_control_set` | ✅ | `target`=应用名(必) `flag`=true 开 | 开关应用 |
| `server_mode_set` | ✅ | `target`=single/multi | 切服务器模式 |
| `pm2_status` | | — | 看 PM2 进程状态 |
| `pm2_restart` | ✅ | — | 重启 classintra-server |
| `pm2_stop` | ✅ | — | 停止 classintra-server |
| `pm2_start` | | — | 启动 classintra-server |
| `server_stats` | | — | 服务器统计（CPU / 内存 / 磁盘 / 在线数） |

> op 表由 CI 侧 `manage.js` 维护，本插件启动时拉取并缓存 5 分钟。**新增动作改 CI 即可，本插件无需改动。**

### 批量目标与「全体」令牌

`user_ban` / `user_unban` 支持两种批量方式：

| 方式 | 写法 | 展开位置 |
|---|---|---|
| 显式名单 | `targets:["张三","李四"]` 或 `target:"张三,李四"` | 逐人 PATCH，一次最多 **20** 人 |
| 全体令牌 | `target:"all"` 或 `target:"all_except_251800"` | CI 侧单请求 `/users/bulk-status` |

**「全体」范围 = 全部非管理员、非班管用户**（实测约 99/103 人）。这是**有意的安全边界** —— 避免把管理员锁在门外。
`all_except_<用户…>` 支持 user_id、网名、真名混用，多人用逗号/顿号/空格分隔；写了的人若**找不到会直接报错，不静默放过**。

---

## 两段式确认闸门

破坏性动作**不会在一句话里直接执行**。链路：

```
① 授权人：「把张三封了」
② 模型调用 classintra_admin(op=user_ban, targets=["张三"])
   → 工具不执行，只返回一段「复述」+ 要求模型停下等确认
③ 模型把复述讲给授权人听，停下
④ 授权人：「确认」        ← 必须是「提议之后新发的一条消息」
⑤ 模型调用 classintra_admin_confirm
   → 工具校验通过 → 真正执行 → 回报结果
```

### 闸门的三道校验（`main.py`）

1. **身份**：`_owner_ids()` 必须包含发起人；
2. **时效**：提议后 **180 秒**内有效，超时作废；
3. **来自新消息**（核心）：确认消息的**身份**必须晚于提议消息。

第 3 条用的是**消息身份（`message_id`）**，不是消息文本，另有时间戳与文本两级兜底：

| 层级 | 判据 |
|---|---|
| 主 | `cur_mid == pend.msg_id` → 判为「同一条消息」，拒绝 |
| 兜底① | 平台没给 `message_id` → 比较入站时间戳（秒级），`cur_ts <= anc_ts` 拒绝 |
| 兜底② | 连时间戳都没有 → 退回消息文本比较 |

**为什么不用文本比较？** 模型经常在「已提议、管理员正在确认」的这一轮里把提议**又调一遍**。若此时重设锚点，锚点会变成管理员那句「确认」本身，之后他的每一次确认都等于锚点 → **闸门永久锁死**。所以：**同一 `(op, params)` 重复提议沿用最初那次的锚点与计时窗口**，不重设。

此外还有一层文本兜底：`_is_confirm_text()` 要求消息不含疑问语气、长度 ≤ 12 字，否则判为「不像确认」，要求授权人明确表态。

### 取消

授权人说「算了」「不用了」→ 模型调用 `classintra_admin_cancel` → 丢弃 pending，什么都不执行。

### 复述为什么必须逐条列名单

批量封禁尤其重要：复述会把 `targets` 逐个列出（或标注「全体用户（all），范围不含管理员与班管」），授权人才能核对**整批名单**再确认。
涉及「全体」时，复述里会额外加一行醒目提示 —— 人数可能上百，必须让授权人听见「全体」两个字。

### 回归测试

`ClassIntra/_t_confirm_gate.py`（40 项）覆盖闸门逻辑，改这块前后必跑。

# HTTP API 契约

本插件同时**提供**和**调用** HTTP 接口，方向相反，别搞混：

```
                       ┌──────────────── 本插件（AstrBot 侧）────────────────┐
ClassIntra  ──HTTP──▶  │  /api/chat  /api/chat/health  /classintra_res/{tok} │   ← 本插件「提供」的（§1）
            ◀──HTTP──  └────────────────────────────────────────────────────┘
                       ┌── 本插件「调用」的（§2）────────────────────────────┐
本插件      ──HTTP──▶  │  CI 的 /api/astrbot/*（由 astrbot-relay 提供）      │
                       └────────────────────────────────────────────────────┘
```

---

## §1 本插件提供的接口

由 `api_server.py` 起一个 aiohttp 服务，监听 `api_host:api_port`（默认 `127.0.0.1:6200`）。

### 鉴权

| 配置 | 行为 |
|---|---|
| `api_token` 留空 | **不校验**（只建议在 SSH 隧道内，或仅本机可达时使用） |
| `api_token` 非空 | 请求头 `X-ClassIntra-Token` 必须与之完全相等，否则 `401` |

健康检查 `/api/chat/health` **不校验** token（探活用）。

---

### `POST /api/chat`

把一条 CI 消息注入 AstrBot 完整管线，取回回复。

**请求体**（JSON）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | 见下 | 文本内容。`message` 与 `images` **至少有一个非空**，否则 `504` |
| `user_id` | string | | 发送者 ID，缺省 `anonymous` |
| `user_name` | string | | 发送者昵称，缺省等于 `user_id` |
| `session_id` | string | | 会话标识，缺省 `private_<user_id>`。**公共聊天室必须传 `group_public`（或 `group_<群号>`）**，否则会按私聊处理 —— 见 §3 |
| `images` | string[] | | 图片 Base64 数组（可带 `data:image/png;base64,` 前缀）。最多处理 `max_images` 张（默认 4），超出丢弃 |
| `mode` | string | | `sync`（默认）或 `async` |
| `callback_url` | string | async 必填 | 异步模式下管线结束后的回调地址；缺失返回 `400` |

**响应（`mode=sync`，成功）**

```json
{
  "mode": "sync",
  "status": "success",
  "reply": "回复纯文本（所有 plain 段拼接，已 strip）",
  "message_chain": [ { "type": "plain", "text": "..." }, { "type": "image", "resource_path": "/classintra_res/<token>", "url": "https://..." } ],
  "resources": [ { "path": "/classintra_res/<token>", "mime": "image/png" } ],
  "session_id": "group_public"
}
```

**响应（`mode=sync`，失败）** —— HTTP 状态码即错误类别：

| 状态码 | 触发条件 | 响应体 |
|---|---|---|
| `400` | 请求体不是 JSON / async 缺 `callback_url` | `{"code":400,"message":"...","data":null}` |
| `401` | `X-ClassIntra-Token` 不匹配 | `{"code":401,"message":"token 无效","data":null}` |
| `500` | 管线内部异常 | `{"mode":"sync","status":"failed","error":"内部错误: ..."}` |
| `504` | 管线未产生任何回复（被拦截 / 超时 / `message` 与 `images` 同时为空） | `{"mode":"sync","status":"failed","error":"..."}` |

> 调用方建议超时 ≥ `request_timeout + 10` 秒。本插件的 `request_timeout`（默认 120）应**大于** CI relay 侧 HTTP 超时，否则 CI 先断、本插件还在跑。

**响应（`mode=async`）** —— 立即返回任务号，结果稍后 POST 到 `callback_url`：

```json
{ "mode": "async", "task_id": "<32位hex>" }
```

回调体（`Content-Type: application/json`）：`{ "task_id": "...", "status": "success"|"failed", ...同 sync 的 reply/message_chain/resources/error }`。

---

### `GET /api/chat/health`

```json
{ "code": 200, "message": "ok", "data": { "status": "运行中" } }
```

`status` 为 `运行中` / `未启动`。`未启动` 通常意味着 `api_port` 被占用（插件日志里有对应报错）。

---

### `GET /classintra_res/{token}`

资源代理：把机器人产出的图片 / 语音 / 视频 / 文件回传给 CI。

**查找顺序**

1. 本插件资源目录 `<plugin_data>/astrbot_plugin_classintra/resources/<token>`（由本插件签发）；
2. 回退到 CI 的 botmedia 目录（`classintra_media_dir`／由 `classintra_root` 推导）—— 用于插件**直接写盘**的媒体（如视频下载产物 `dv*/mu*`、生图产物），这类文件不经过本插件签发流程。
   没有这道回退时，**跨机**（如 8 班经隧道）拉取这类资源会 404，表现为聊天里图片/视频打不开。

**文件名白名单**：必须匹配 `[a-z0-9]{8,40}\.\w{1,8}`（小写字母数字 + 单层后缀），杜绝路径穿越。不匹配直接 `404`。

**响应头**：`Cache-Control: public, max-age=3600`。

---

## §2 本插件调用的 CI 端点

全部由 CI 侧的 [`astrbot-relay`](../../../astrbot-relay) 插件提供，挂载在 `/api/astrbot` 下。
除 `/status` 外，鉴权都是**共享密钥**：请求头 `x-publish-key` 必须等于 CI `server/.env` 的 `ASTRBOT_PUBLISH_KEY`；本插件用配置项 `publish_key` 对应。

**基址**：配置项 `publish_base_url`（默认 `http://127.0.0.1:9001`）。

### 2.1 论坛 / 聊天

| 方法 | 路径 | 用途 | 调用它的工具 | 备注 |
|---|---|---|---|---|
| `POST` | `/publish` | 发论坛帖 | `publish_classintra_post` | 请求体用 `Content-Type: text/plain` 发送 JSON 原文，**绕开 CI 全局 1MB JSON 限制**（附图 Base64 可达数 MB）。附图先落 botmedia 再转 Markdown 追加到正文；最多 9 张 |
| `GET` | `/post/{id}` | 帖子详情 + 最新评论 | `read_classintra_post` | 404 = 帖子不存在或已删 |
| `DELETE` | `/post/{id}` | 删自己的帖 | `delete_classintra_post` | 以机器人账号鉴权，CI 侧判定归属；删别人的帖返回 `403` |
| `POST` | `/post/{id}/comment` | 以机器人身份回帖 | `comment_classintra_post` | 内容上限 2000 字；`429` = 评论过于频繁 |
| `POST` | `/recall` | 撤回机器人最近发的聊天消息 | `recall_classintra_messages` | 体 `{channel,target,count}`；`channel` 为 `public`/`private`，`private` 必须给 `target`(发送者 ID)；`count` 1–10；仅在 **2 分钟**内有效，由 CI 侧最终判定 |

### 2.2 站内信息读取（只读）

| 方法 | 路径 | 用途 | `read_classintra_info(kind=...)` |
|---|---|---|---|
| `GET` | `/info/announcements` | 公告列表 | `announcements` |
| `GET` | `/info/broadcasts` | 快讯列表 | `broadcasts` |
| `GET` | `/info/forum` | 社区帖子列表（`?type=forum&sort=latest|hot&limit=`） | `forum` |
| `GET` | `/info/post/{id}/comments` | 某帖评论 | `comments`（需 `post_id`） |
| `GET` | `/info/resources` | 资源仓库列目录（`?path=&limit=`，排除 `public/cloud`） | `resources` |
| `GET` | `/info/weather` | 当前天气 + 今日预报 + 空气质量 + 预警 + 生活指数 | `weather` |
| `GET` | `/info/pulse` | 聚合快照（公告+快讯+热帖+最新帖+天气+资源根） | 默认 / `pulse` |

> `/info/pulse` 内部对各数据源做了 `safe()` 包装：**任一源失败不会拖垮整体**，失败项返回 `null`。

### 2.3 管理动作代理

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/manage/ops` | 拉取 op 目录（`[{op,destructive,desc,args}]`），本插件缓存 5 分钟 |
| `POST` | `/manage` | 执行动作，体 `{requester_id, op, params, confirmed}` |

`op` 表即**白名单**：AstrBot 侧只能传 op 名 + 参数，URL/方法由 CI 侧 `manage.js` 决定，不存在路径注入。
完整 op 表见 [`tools.md`](tools.md#管理动作全表)。

---

## §3 会话与频道语义（关键）

`session_id` 不只是个字符串，它决定**消息被当作群聊还是私聊**，进而影响下游所有插件的权限判定（视频下载公屏/私聊、撤回频道、搜视频群号转换……）。

`dispatcher.py` 的规则：

| `session_id` 形态 | 构造出的事件 | `get_group_id()` |
|---|---|---|
| `group_public` | `GROUP_MESSAGE`，`group_id="public"` | `"public"` |
| `group_<群号>` | `GROUP_MESSAGE`，`group_id=<群号>` | `<群号>` |
| 其他（如 `private_<id>`） | `FRIEND_MESSAGE` | `""` |

**判定公共频道**统一走 `ci_session.is_public_channel()`（本插件内唯一权威实现）：

- `event.get_group_id() == "public"` —— OneBot 正常路径；
- **或** `event.session_id ∈ {"public", "group_public"}` —— HTTP 注入路径（旧版 dispatch 不带群语义，只能靠会话标识还原，否则公共聊天室会被误判成私聊）。

> 所以：CI 侧经 HTTP 注入公共聊天室消息时，`session_id` **必须**是 `group_public`（或 `group_<群号>`），不能是 `public` 之外的其它值，也不能用 `private_*`。
> 相关背景见 `docs/architecture.md` 的「模块职责」与源码 `ci_session.py` 头部注释。

---

## §4 消息链段格式

`message_chain` / `resources` 里媒体段的形态：

| `type` | 字段 | 说明 |
|---|---|---|
| `plain` | `text` | 文本（人格标签已按 `persona_tag_map` 转成 emoji，除非关闭 `persona_tag_enable`） |
| `image` / `record` / `video` / `file` | `resource_path` | `/classintra_res/<token>`，供 CI 经资源代理取回 |
| 同上 | `url` | 仅当原组件自带 `http` URL 时才附带（CI 也可直连） |

**超过 `max_resource_mb` 的媒体不转发**，降级为 `plain` 占位文本 `[图片]（超过 20MB 未转发）`。
媒体转本地文件失败时也会降级：有 `url` 就带 `url`，否则退回占位文本。
`Comp.Reply`（引用）会被跳过，避免渲染出占位符。

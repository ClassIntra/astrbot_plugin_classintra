# 架构

## 整体位置

```
┌──────────────────┐   HTTP / WS   ┌───────────────────────────┐
│ ClassIntra 前端  │──────────────▶│ ClassIntra 服务端（CI）    │
│ (校园平板 WebView)│               │  · /api/astrbot/*         │
└──────────────────┘               │  · astrbot-relay 插件 ◀───┼── CI 侧对端
                                   └───────────┬───────────────┘
                                               │
                     ┌─────────────────────────┴──────────────────────────┐
                     │                  两种管线                          │
                     │ ① OneBot 反向 WS（首选，机器人是"真"平台用户）      │
                     │ ② HTTP POST /api/chat（本插件提供，降级/直连）      │
                     └─────────────────────────┬──────────────────────────┘
                                               ▼
                                   ┌───────────────────────────┐
                                   │      AstrBot 核心          │
                                   │  唤醒 → 指令/LLM → 工具调用 │
                                   └───────────┬───────────────┘
                                               ▼
                                   ┌───────────────────────────┐
                                   │ 本插件（classintra_plugin）│
                                   └───────────────────────────┘
```

本插件同时扮演**两个角色**：

1. **平台适配器（Platform）** —— 注册一个名为 `classintra` 的虚拟平台适配器，
   把 CI 发来的 HTTP 请求合成为 `AstrBotMessage` 事件提交进管线，再把捕获到的回复消息链
   转回 CI 的格式。这是「HTTP 注入」管线的实现。
2. **能力提供者** —— 发帖/读帖/管理代理/视频下载/人物档案，以指令与 LLM 工具的形式挂到 AstrBot 上。

---

## 一条消息的完整旅程

以「学生在 ClassIntra 里私聊机器人说『帮我看看最新的公告』」为例：

```
1. CI 前端 → CI 服务端（WebSocket 或 HTTP）
2. CI 的 astrbot-relay 决定走哪条管线
   ├─ OneBot 就绪 → 推给 OneBot → AstrBot 的 aiocqhttp 适配器（不经过本插件）
   └─ 降级 → POST http://<bot>:6200/api/chat
                 { user_id, user_name, session_id, message, mode:"sync" }
3. 本插件 api_server.handle_chat()
   ├─ 校验 X-ClassIntra-Token（配置了 api_token 才校验）
   ├─ mode=sync → dispatcher.dispatch(body)
   └─ mode=async → 立刻返回 task_id，后台跑完再 POST 回调网址
4. dispatcher.dispatch()
   ├─ 组装 AstrBotMessage
   │   · session_id 以 "group_" 开头 → GROUP_MESSAGE + group_id（**关键**，见下）
   │   · 否则 → FRIEND_MESSAGE
   │   · 文本 → Comp.Plain；图片（base64）→ Comp.Image.fromBase64
   │   · 标 is_wake / is_at_or_wake_command，避免被唤醒前缀拦住
   ├─ platform.commit_event(event) → 进 AstrBot 事件队列
   └─ 等 after_message_sent 钩子（最长 request_timeout 秒）
5. AstrBot 管线
   ├─ 本插件 observe()（priority 1000）旁路采集语料
   ├─ 其他插件、指令、LLM 工具调用（可能反过来调 CI 的 /api/astrbot/*）
   └─ 本插件的 memory.inject_profile() 把人物档案注入 system_prompt
6. 回复产生 → ClassIntraAPIEvent.send() **不真发**，而是捕获
   ├─ Plain → 文本（顺带把人设标签 &&fool&& 换成 emoji）
   ├─ Image/Record/Video/File → 落盘到 resource_dir，登记 /classintra_res/<token>
   └─ 超过 max_resource_mb 的媒体降级为占位文本
7. after_message_sent（priority -99999，最后运行）→ dispatcher.notify_done()
   → dispatch() 返回 { reply, message_chain, resources, session_id }
8. CI 拿到结果 → 以机器人账号的身份发到对应会话
```

### 为什么 `group_` 前缀必须带出群语义

注入路径如果恒为 `FRIEND_MESSAGE` 且 `group_id` 为空，所有依赖
`event.get_group_id()` 的插件都会把它误判成私聊。公共聊天室因此会丢掉：

- 视频下载的「公屏全员 / 私聊限授权人」权限判定
- 撤回工具该往哪个频道撤回
- 搜视频插件的群号转换

所以 `dispatcher` 里显式做了这件事：

```python
if session_id.startswith("group_"):
    abm.type = MessageType.GROUP_MESSAGE
    abm.group_id = session_id[len("group_"):] or "public"
```

`session_id = "group_public"` 就是公共聊天室。判定收敛在
[`ci_session.py`](#模块职责) 一个地方，不允许再内联。

---

## 模块职责

| 文件 | 职责 | 硬约束 |
|---|---|---|
| `main.py` | **唯一入口**。全部 `@filter.*` 装饰器、LLM 工具的 schema 与转发、配置读取 | 装饰器只能写在这里 |
| `ci_paths.py` | ClassIntra 侧路径的唯一来源（CI 根 / botmedia / DB / URL 前缀） | 解析顺序：配置 > 环境变量 > 空 |
| `ci_session.py` | 「是不是公共聊天室」的唯一权威判定 | 第三方插件里的副本由校验脚本比对 |
| `dispatcher.py` | 虚拟平台适配器：合成事件、驱动管线、捕获回复、人设标签替换 | — |
| `api_server.py` | aiohttp：`/api/chat`、`/api/chat/health`、`/classintra_res/{token}` | 媒体目录必须**请求时**取，不能在导入期固化 |
| `downloader.py` | yt-dlp 下载 + botmedia 数量上限清理 | 纯类，无装饰器 |
| `memory_plugin.py` | 人物档案 + 学别人讲话的服务层 | 纯服务，无装饰器 |
| `profile_store.py` | 档案存储（SQLite）+ 注入文本渲染 + 事实净化 | — |
| `recall.py` | 向量召回（粗排 + 重排 + 缓存 + 熔断） | 任何失败都降级，绝不阻塞对话 |
| `style_learn.py` | 语料清洗、风格卡/群近况的提炼提示词与解析 | — |
| `style_store.py` | 语料库 + 风格卡 + 群近况存储 | — |

### 为什么装饰器只能写在 `main.py`

AstrBot 用 `star_map[handler.handler_module_path]` 定位插件，而
`handler_module_path = handler.__module__`（见框架的 `get_handler_or_create`），
且 `get_handlers_by_module_name` 是**精确相等**匹配。

把一个 `@filter.llm_tool` 写在 `downloader.py` 里，框架会去找模块名为
`astrbot_plugin_classintra.downloader` 的插件——找不到，于是既不注册工具，
也拿不到 `Star` 实例注入。

> 顺带一提：`_is_plugin_llm_tool` 用的是**前缀**匹配（`startswith(f"{plugin_module_path}.")`），
> 和 handler 那套逻辑不同，别把两者混为一谈。

所以架构是「**main.py 只做转发，逻辑放同包普通模块**」。

---

## 记忆子系统

### 读：档案注入

在 `on_llm_request` 钩子里把**本轮发言人**的档案渲染成文本，
挂到 `req.system_prompt`。

选择 `system_prompt` 而不是 `extra_user_content_parts` 是有原因的：
后者会被 `assemble_context()` 并进同一条 user 消息、然后整条落库，
于是「跨轮不变」的档案会被反复写进历史，越滚越大。
`system_prompt` 不随会话落盘。

### 写：异步抽取

在 `on_agent_done` 里累加轮数，每 `extract_every_n_turns` 轮异步抽取一次新事实
（首轮必抽）。抽取会额外调一次 LLM，所以做了节流。

### 隐私隔离

每条事实带来源标记（`private` / `group`）。群里只注入群来源的事实，
避免把私聊听来的私事当众说出来。双来源（私聊和群聊都说过）的事实两边都可见。

### 向量召回

```
事实数 ≤ recall_top_k  → 快路径：全量返回，零模型调用
否则                  → 向量粗排 recall_candidates 条 → rerank 精排 → 取 top_k
任何一步失败/超时      → 降级为「最近 N 条」+ 熔断（一段时间内不再重试）
```

向量与重排 provider 由 `embed_provider_id` / `rerank_provider_id` 指定，
用的是 AstrBot 已注册的 provider（不额外配 API Key）。
**两个都留空 = 不用召回**，功能照常，只是相关性差一些。

### 安全：档案是一条长期指令通道

档案最终会被回注进系统提示词——等于开了一条「长期指令」通道。
所以入库前强制过滤：指令式句子、角色劫持（「承认你是 AI」「忽略之前的规则」）
一律丢弃；注入文本自带免责说明；抽取提示词里也显式拒收这类内容。

### 学别人讲话

旁路（`platform_adapter_type` 钩子，priority 1000）采集**别人**的发言进语料库，
排除机器人自己（按 `self_id`，`bot_names` 作为兜底）。
攒够 `style_every_n_messages` 条且距上次学习超过 `style_min_interval` 秒时，
提炼出「语气词 / 群黑话 / 说话习惯」风格卡 + 「群近况」，
随档案一起注入。私聊语料只参与**说话风格**，不参与**群近况**
（避免把私事拿到群里说）。

---

## 存储

数据目录：`data/plugin_data/astrbot_plugin_linxi_memory/profiles.db`

> 目录名沿用旧插件名 `astrbot_plugin_linxi_memory` 是**刻意的**——
> 改名等于所有历史档案丢失。这不是笔误。

| 表 | 内容 |
|---|---|
| `profiles` | 人物档案：称呼、事实列表（带来源与时间）、相处关系 |
| `fact_vec` | 事实的向量缓存（供召回粗排，避免重复调用 embedding） |
| `chat_log` | 「学说话」的语料库（滚动窗口，上限 `style_log_cap`） |
| `style_cards` | 提炼出的风格卡 |
| `group_facts` | 群近况 |

时间戳为 UTC。

插件自己的资源目录（回复里转发给 CI 的媒体副本）：
`data/plugin_data/astrbot_plugin_classintra/resources/`

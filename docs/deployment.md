# 部署与运维

## 拓扑

本插件是 **AstrBot 侧**，与 CI 侧的 [`astrbot-relay`](../../../astrbot-relay) 插件成对使用：

```
ClassIntra 前端 / 设备
      │
      ▼
ClassIntra 服务端（PM2: classintra-server，HTTP 9001 / WS 10001）
      │
      ├── astrbot-relay（CI 侧插件，挂载 /api/astrbot）
      │        │
      │        ├── OneBot 反向 WS ──▶ ws://127.0.0.1:6199/ws
      │        └── HTTP /api/chat ──▶ http://127.0.0.1:6200
      │                                        │
      ▼                                        ▼
AstrBot（WebUI 6185 / OneBot 6199 / 本插件 API 6200）
      │
      └── astrbot_plugin_classintra（本插件）
              └── AstrBot 完整管线：人设 → 工具调用 → 记忆 → 回复捕获
```

**两条通路二选一或并存**：
- **OneBot 反向 WS**（6199）—— 机器人以真实账号参与 CI 私聊，走 OneBot 协议，回复分段发送；这是 relay 的主路径；
- **HTTP `/api/chat`**（6200）—— CI 把消息**注入** AstrBot 管线，本插件回收回复。用于「机器人以自己身份参与公共聊天室 / 私聊」的另一种接入，以及外部程序调用。

> 两者都到 AstrBot，但方向与协议不同。本插件的 HTTP API（§1）服务的是**后者**。

---

## 前置条件

| 组件 | 要求 |
|---|---|
| ClassIntra 服务端 | 已部署，其中启用了 **`astrbot-relay`** 插件 |
| AstrBot | **v4.28** 实测通过。用到 `on_llm_request` / `on_agent_done` / `after_message_sent` 等较新钩子，更低版本未验证 |
| Python | 3.10+（随 AstrBot） |
| `yt-dlp` + `ffmpeg` | **可选**，只有要用视频下载时才需要 |

**无 pip 依赖**：`requirements.txt` 是空的 —— 所有依赖都是 AstrBot 自带的 `aiohttp` 与标准库（`sqlite3` / `asyncio` / `hashlib`）。

---

## 部署步骤

### 第 1 步：CI 侧装 relay 插件

relay 位于 CI 仓库 `plugins/astrbot-relay`（聚合器自动扫描挂载）。在 CI `server/.env` 追加：

```dotenv
# ===== AstrBot 机器人账号（自动建号，幂等）=====
BOT_USER_ID=linxi_ai
BOT_NET_NAME=林晞
BOT_REAL_NAME=林晞
BOT_PASSWORD=改成强密码          # 必填，同时用于自动建号与登录
BOT_GENDER=女

# 让机器人成为 CI 管理员（管理代理的前提）
BOT_ADMIN_IDS=linxi_ai

# 机器人 API / OneBot
ASTRBOT_WS_URL=ws://127.0.0.1:6199/ws
ASTRBOT_WS_TOKEN=                 # 与 AstrBot 适配器配置一致

# 管理代理：谁有权指挥机器人（默认回退 ADMIN_USER_IDS）
ASTRBOT_OWNER_IDS=251800

# 论坛发帖 / 信息读取 / 管理动作的共享密钥（与 AstrBot 侧 publish_key 一致）
ASTRBOT_PUBLISH_KEY=改成长随机串
```

> `BOT_ADMIN_IDS` 让机器人账号通过 `constants.isSystemAdmin()` 豁免班级比对，从而能跨班管理 —— **它不是冒用真人账号**，CI 审计里记的仍是机器人自己。

重启 CI 后端（PM2 `classintra-server`）。启动时 relay 自动建号、登录 WS、连 OneBot。

### 第 2 步：AstrBot 侧装本插件

从 AstrBot 插件市场搜索「ClassIntra 接入」安装，或：

```bash
cd AstrBot/data/plugins
git clone https://github.com/ClassIntra/astrbot_plugin_classintra.git
```

重启 AstrBot。

### 第 3 步：填最少 4 项配置

| 配置项 | 填什么 |
|---|---|
| `owner_user_ids` | **最重要**。授权人的 CI `user_id`，逗号分隔（如 `251800`）。留空 = **无人有权限** |
| `publish_base_url` | CI 地址，同机默认 `http://127.0.0.1:9001` |
| `publish_key` | 与 CI `ASTRBOT_PUBLISH_KEY` 一致。留空则发帖与管理工具不可用 |
| `classintra_root` | 要用**视频下载**就必填。CI 仓库根目录（如 `D:\ClassIntra`） |

配完在群里发 `/ci_status` 验证。完整 44 项见 [`configuration.md`](configuration.md)。

---

## 网络与隧道

### 同机部署（推荐）

CI 与 AstrBot 在同一台机器：用默认值（`127.0.0.1`），不需要任何隧道。

### 跨机部署

AstrBot 在另一台机器（例如 8 班经 Tailscale 到 18i）时：

1. AstrBot 侧 `api_host` 可保持 `127.0.0.1`（更安全），由 CI 侧建 SSH 隧道：

   ```bash
   ssh -L 9999:localhost:6200 user@bot-host
   ```

   CI 的 `publish_base_url` 与「调用 AstrBot」的地址相应改为 `http://127.0.0.1:9999`。

2. 只允许**同网段直连**时才把 `api_host` 改成 `0.0.0.0`，并**务必设 `api_token`**。

3. 视频下载落盘的媒体走 `classintra_media_dir`；若 AstrBot 与 CI 不同机，需同步该目录（CI 侧静态挂载）。资源代理有 botmedia 回退逻辑，正是为这类跨机场景准备的（见 [`api.md`](api.md#get-classintra_res-token)）。

### 端口速查

| 端口 | 服务 |
|---|---|
| 6185 | AstrBot WebUI |
| 6199 | AstrBot OneBot 反向 WS |
| 6200 | 本插件 HTTP API（`api_port`） |
| 9001 | ClassIntra HTTP |
| 10001 | ClassIntra WS |
| 9000 | entry-gateway 反代（部分部署） |

> ⚠️ `api_port` **被占用**时，插件会在日志里报错并**跳过** HTTP API，其余功能不受影响；`/ci_status` 会显示「未启动」。改一个空闲端口即可。

---

## 验证清单

| 检查 | 命令 / 操作 | 期望 |
|---|---|---|
| 插件加载 | AstrBot 日志 | 无 `import` / 注册异常 |
| HTTP API | `curl http://127.0.0.1:6200/api/chat/health` | `{"code":200,...,"status":"运行中"}` |
| CI 发布通道 | 群里说「发个公告…」 | 成功发布并回报 ID |
| 站内读取 | 问「最近有什么公告」 | 机器人先调 `read_classintra_info` 再答，内容与站内一致 |
| OneBot 连接 | `curl http://127.0.0.1:9001/api/astrbot/status` | `"onebot": {"connected": true}` |
| 管理代理 | 授权人说「看看服务器状态」 | 返回 CPU / 内存 / 在线数 |
| 两段式闸门 | 授权人说「把张三封了」 | **先复述并停下**，不直接执行 |
| 视频下载 | 公屏发链接 | 落盘并返回可播放链接 |

---

## 升级与回滚

### 从旧版本升级（合并前）

本插件已合并 `astrbot_plugin_ci_downloader` 与 `astrbot_plugin_linxi_memory`。

1. 把两个旧插件目录**移出** `data/plugins/`（**不是改名** —— 目录扫描只看是否含 `main.py`，改名后仍会被加载，导致同名工具重复注册）；
2. 装本插件，重启 AstrBot；
3. **配置键自动迁移**：旧键名与新键的对应见 [`configuration.md`](configuration.md#8-从旧版本升级时的键名迁移)；
4. **历史档案原地保留**：`profile_store` / `style_store` 的库文件不动。

> ⚠️ 同时装新旧插件会重复注册 `download_video`、记忆被两个实例同时读写。

### 版本关系

| 组件 | 版本 | 说明 |
|---|---|---|
| 本插件 | `v2.0.0`（`metadata.yaml`） | 合并后的首个正式版 |
| CI 侧 relay | `1.1.0`（`manifest.json`） | 与主仓版本**各自独立语义化**，别混 |

### 回滚

把插件目录换回旧版本、重启 AstrBot 即可。配置键是**向后兼容读取**的（新键优先、旧键兜底），所以旧配置不必改。

---

## 常见部署错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志 `HTTP API 启动失败：...无法监听` | `api_port` 被占用 | 换端口 |
| 发帖工具返回「未配置 publish_key」 | `publish_key` 留空 | 填上与 CI 一致的值 |
| 管理工具返回「这个我管不了」 | 发起人不在 `owner_user_ids` | 补上其 user_id |
| 下载工具返回「下载未启用：还没配置…」 | `classintra_root` / `classintra_media_dir` 未填 | 填路径（见 [`configuration.md`](configuration.md)） |
| 跨机聊天里图片/视频打不开 | botmedia 目录未配，资源代理回退查找失败 | 配 `classintra_media_dir`，或确认 CI 静态挂载 |

更多见 [`troubleshooting.md`](troubleshooting.md)。

# 二次开发与发布

## 目录结构

```
astrbot_plugin_classintra/
├── main.py               ← 唯一入口。**所有 @filter.* 装饰器必须在这里**
├── api_server.py         ← HTTP API（/api/chat、/health、/classintra_res）
├── dispatcher.py         ← 管线分发器：注入事件、捕获回复、资源落盘
├── ci_paths.py           ← CI 侧路径唯一来源（配置 > 环境变量 > 空）
├── ci_session.py         ← 「是否公共聊天室」的唯一权威判定
├── downloader.py         ← 视频下载（yt-dlp）
├── memory_plugin.py      ← 记忆子系统门面（档案 + 风格）
│   ├── profile_store.py  ← 档案存储（facts）
│   ├── recall.py         ← 向量召回 + 重排
│   ├── style_store.py    ← 风格卡 / 语料 / 群近况存储
│   └── style_learn.py    ← 「学别人讲话」提炼
├── _conf_schema.json     ← 配置 schema（44 键，AstrBot 配置页据此渲染）
├── metadata.yaml         ← 插件元数据（name/version/help/repo…）
├── README.md / CHANGELOG.md / LICENSE / logo.png / requirements.txt
└── docs/                 ← 本套文档
```

---

## 关键约束（踩过坑的地方）

### 1. 所有 `@filter.*` 必须写在 `main.py`

AstrBot 的插件绑定机制：

- `star_map[handler.handler_module_path]`，而 `handler_module_path = handler.__module__`；
- 查找用 `get_handlers_by_module_name()`，是**精确相等**匹配。

于是：**装饰器定义在哪个模块，处理器就归属哪个模块**。定义在 `downloader.py` 里的工具**不会**被绑定到本插件上。
所以逻辑放普通模块（`downloader.py` / `memory_plugin.py`），**签名、docstring、转发留在 `main.py`**。

> 注意另一套逻辑不同：`_is_plugin_llm_tool()` 用**前缀**匹配（`startswith(f"{plugin_module_path}.")`）。两套判据一精确一前缀，别混。

### 2. 插件的资源目录不能是普通常量

`api_server.py` 里曾有 `_CLASSINTRA_MEDIA_DIRS = [ci_paths.BOTMEDIA_DIR]` —— 这是**导入期固化**，而 `ci_paths` 的值由插件配置决定、在 `__init__` 才注入、改配置后还会重算。已改为每次请求时取：`_classintra_media_dirs()`。加新的路径引用时**别在模块顶层固化**。

### 3. 配置解析一律三段式

`ci_paths._pick(config, key)`：**配置 > 环境变量 > 空**。新增路径类配置项时照此办理，不要在模块里写本机默认值。

### 4. 公共频道判定只有一份实现

`ci_session.is_public_channel()` / `channel_of()`。**不许再内联**判定（历史上散落三份副本各自漂移，导致群里下的下载命令被当私聊拒绝）。
第三方 AGPL 插件里的副本由 `_check_thirdparty_patches.py` 交叉校验。

### 5. 配置键：代码引用的键必须在 schema 里

`_check_release_ready.py` 的 **I1** 项会静态扫描 `config.get("...")` / `cfg.get("...")` 并要求全部出现在 `_conf_schema.json`。
这条曾抓到真 bug：`memory_plugin.py` 读的是早已改名的 `enable`（新名 `memory_enable`）→ 配置页关不掉记忆。

### 6. Windows 下的编码坑

- 配置文件（`data/config/<dir>_config.json`、`_conf_schema.json`）用 **`utf-8-sig`** 读写；读时**必须** `encoding="utf-8-sig"`，否则 `JSONDecodeError: Unexpected UTF-8 BOM`。
- 批量改文件不要用 PowerShell 5.1 的 `Get-Content`/`Set-Content`（默认 GBK，会把中文写坏）；用 Node/Write 工具。

---

## 调试手段

### A. HTTP 注入（黑盒、等价真实链路）

```bash
curl -s -X POST http://127.0.0.1:6200/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"251800","user_name":"测试","session_id":"group_public",
       "message":"最近有什么公告？","mode":"sync"}'
```

请求经 `dispatcher.dispatch()` 合成事件 `commit_event` 进**完整管线**，与「用户在 CI 里 @ 机器人」等价，可当黑盒验收用。

> ⚠️ 做多轮测试时 `session_id` **每次都要带时间戳**（如 `group_public-1730000000`）。复用固定会话会把上一轮历史串进上下文，模型可能直接背书上文而不去调工具（**假 FAIL**）。

### B. 工具调用铁证

`local-astrbot.out.log` 里看：

```
tool_loop_agent_runner: Agent 使用工具: [...]
```

**别拿日志做硬断言**：该文件有多个写者、会被缩容、含 `\r` 覆盖进度条。要断言就按时间戳出现位置切分记录。

### C. 探针脚本（`AstrBot/_*.py`）

| 脚本 | 用途 |
|---|---|
| `_probe_ci_admin_e2e.py` | 管理代理端到端 |
| `_probe_all_token.py` | 「全体」令牌展开 |
| `_probe_memory_e2e*.py` | 记忆读写端到端 |
| `_probe_recall_e2e.py` / `_probe_recall_firstturn.py` | 向量召回 |
| `_probe_style_e2e.py` | 学说话 |
| `_probe_providers.py` | 探测可用模型通道 |
| `_cfg_get.py` | 读插件当前配置 |
| `_fill_local_paths.py` | 把本机路径写进配置（带时间戳备份） |

---

## 回归测试

| 脚本 | 位置 | 覆盖 |
|---|---|---|
| `_t_confirm_gate.py` | AstrBot/ | 两段式确认闸门（40 项） |
| `_t_batchban.cjs` | ClassIntra/ | 批量封禁（22 项） |
| `_t_bulk_e2e.cjs` | ClassIntra/ | 批量状态端到端（13 项） |
| `_t_manage_e2e.py` | ClassIntra/ | 管理动作端到端（14 项） |
| `_check_thirdparty_patches.py` | AstrBot/ | 第三方补丁在位 + 语义交叉比对 |

**两个必须遵守的纪律**：

1. **CI 侧脚本必须以 `server` 为 cwd 运行** —— `server/.env` 的 `DB_PATH` 是相对路径，否则 `no such table: users`。
2. **写「真更新」类 e2e 必须自带夹具** —— 不许依赖「生产库里恰好存在某状态样本」。曾有 3 个用例因为生产数据被真人改动而假 FAIL（比如恰好有人在两次检查之间被解封）。自带夹具即：挑一个普通用户 → 快照 → 改 → 校验 → 还原。

---

## 第三方补丁

本插件有意**不合并**第三方插件代码，改为「自包含副本 + 台账」，台账在 `AstrBot/THIRD_PARTY_PATCHES.md`：

| 插件 | 许可 | 改动 |
|---|---|---|
| `search_video` | AGPL | 仅公共聊天室可下载；上游更新后需重打 |
| `anysearch` | MIT | 凭据脱敏（安全类，最高优先级） |
| `image_generation` | AGPL | 参考图未命中禁重试 |

改完跑 `_check_thirdparty_patches.py`：A 段验补丁在位 / B 段拿同一组样例交叉比对主插件与第三方副本 / C 段验主插件内不再内联判定。

---

## 发布流程

1. **改版本号**：`metadata.yaml` 的 `version` 是**插件独立语义化**（功能新增 MINOR+1），与 CI 主仓版本无关；
2. **写 CHANGELOG**：`CHANGELOG.md` 顶部加一节（版本号必须与 `metadata.yaml` 一致，F1 项会校验）；
3. **跑发布自检**：

   ```bash
   cd <AstrBot 根目录>
   PYTHONPATH= ./.venv/Scripts/python.exe _check_release_ready.py
   ```

   > 用别的解释器跑也行：脚本检测到缺 `pyyaml` / `deprecated` 会**自动切到 `.venv`** 重新执行（`CHECK_RELEASE_NO_REEXEC=1` 可禁用）。

   共 24 项，全 PASS 才能发。分段：

   | 段 | 检查 |
   |---|---|
   | A | 目录卫生：无 `*.b-*-bak` 备份残留 / 无 `__pycache__` / 无 `.pyc` |
   | B | `.py`/`.json` 无本机绝对路径（严 FAIL）；`.md`/`.yaml` 里的示例路径仅 WARN |
   | C | 无内网 / 隧道 IP |
   | D | 配置项默认值无明文凭据（密钥类默认必须为空） |
   | E | metadata 必备字段齐全、`repo` 是有效 URL |
   | F | CHANGELOG 顶部与 metadata 版本一致 |
   | G | 必备文件齐全（README/LICENSE/logo/metadata/schema/main/requirements） |
   | H | schema 每项都有 `description` + `type` + `hint` |
   | I | **代码引用的配置键都在 schema 里** |
   | J | 空配置冒烟：路径不报错、`download` 返回「未启用」而非抛异常 |

   > 脚本开头 `sys.dont_write_bytecode = True` —— 它会 import 插件，别因此生成 `__pycache__` 把自己判不合格。

4. **同步发布资产**：`logo.png`（512×512，来自品牌方形标）、`README.md`、`docs/`；
5. **提交**：常规提交信息，说明「新增/修复什么 + 为什么」；
6. **发市场**：推公开仓库 → 提 PR 到 AstrBot 插件广场索引。`market/index.json` 里若登记本插件，**version 必须等于 `metadata.yaml`**，否则索引失信。

### 合并后的模块不要用旧脚本重新生成

`memory_plugin.py` 文件头明确写了「本文件最初由 `_merge_plugins.py` 生成，但合并后已在此处直接维护，**不要再拿旧脚本重新生成**」。`_merge_plugins.py` 是历史工具，重跑会覆盖手工修复。

---

## 待办 / 已知问题

- **向量召回依赖外部 embedding provider**：模力方舟账号余额耗尽时 embedding 调不动 → 召回自动**降级为「最近 8 条」**（链路本身正确）。换 provider 或充值即可恢复。
- `metadata.yaml` **故意不写 `astrbot_version`**：仅在 v4.28 实测，写死兼容区间会误放行低版本。是否补，由维护者定。

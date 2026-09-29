"""ClassIntra 接入插件（AstrBot 端）。

将 AstrBot 的完整消息管线（人设 / 工具调用 / 会话记忆 / 指令系统）
以 HTTP API 形式开放给 ClassIntra 校园内网平台，使机器人以
独立账号（如"林晞"）在 ClassIntra 中提供与 QQ 端同级的交互能力。

数据流：
  ClassIntra 用户私聊"林晞"
    → ClassIntra 插件(astrbot-relay) 收到 WS 私聊
    → SSH 正向隧道 → POST /api/chat（本插件）
    → 合成事件进入完整管线 → 捕获回复消息链
    → 返回 message_chain + resources → ClassIntra 以林晞身份发出
"""

import asyncio
import base64
import json
import os
import re

from astrbot.api import logger, star
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context

from . import ci_paths
from .api_server import ClassIntraAPIServer
from .dispatcher import ClassIntraDispatcher
from .ci_session import channel_of
from .downloader import CiVideoDownloader
from .memory_plugin import LinxiMemoryService

# 论坛发帖附图上限与单张大小上限（与 QQ空间说说 9 图习惯保持一致）
MAX_ATTACH_IMAGES = 9
MAX_ATTACH_BYTES = 10 * 1024 * 1024


def _re_search_digits(value) -> str | None:
    """从任意文本中提取第一段数字（1-12 位）。

    模型经常把 "帖子ID：833" 这类整句当作 post_id 传进来，这里做宽容提取。
    """
    import re

    m = re.search(r"(\d{1,12})", str(value or ""))
    return m.group(1) if m else None


# 管理动作的授权人（CI 管理员 user_id，逗号分隔）。插件配置 owner_user_ids 可覆盖。
# 默认为空 —— 插件要发布到市场，不能内置某个具体学号；未配置时所有管理动作一律拒绝。
DEFAULT_OWNER_IDS = ""

# 支持一次处理多个用户的管理动作（params 用 targets 数组传递，而非单个 target）。
# 其余动作保持单目标，语义不变。
_BATCH_USER_OPS = {"user_ban", "user_unban"}

# 「全体」令牌：all / all_except_<用户…>。由 CI 侧 relay 展开成真实名单
# （范围不含管理员与班管），这里只用于识别与复述措辞。
_ALL_TOKEN_RE = re.compile(r"^all(_except)?([\s:：_]|$)", re.I)


def _is_all_token(s) -> bool:
    return bool(_ALL_TOKEN_RE.match(str(s or "").strip()))

# 确认语：明确同意的词（出现在短句里即算），或整句就是一个简短应允词
_CONFIRM_WORDS = ("确认", "确定", "执行", "批准", "授权", "同意", "就这么办", "动手", "干吧", "去吧")
_CONFIRM_EXACT = {"好", "行", "嗯", "对", "可以", "是的", "好嘞", "ok"}
# 疑问标记：出现即判为非确认（避免「你确认一下行不行」这类问句被误当成确认）
_INTERROG_WORDS = (
    "吗", "呢", "怎么", "为什么", "哪", "行不行", "好不好", "对不对",
    "是不是", "能不能", "要不要", "可不可以", "是不是要",
)


def _is_confirm_text(text) -> bool:
    """判断管理员这句话是不是在确认执行。

    刻意保守：含糊、疑问、过长的一律不算确认——宁可让她再问一遍，
    也不要把「你确认一下行不行」误解成「确认执行」。
    """
    import re

    raw = str(text or "")
    if "?" in raw or "？" in raw:
        return False
    t = re.sub(r"[\s，。！~,.、；;：「」\"']", "", raw)
    if not t:
        return False
    if t.lower() in _CONFIRM_EXACT:
        return True
    if len(t) > 12:
        return False
    if any(w in t for w in _INTERROG_WORDS):
        return False
    return any(w in t for w in _CONFIRM_WORDS)


def _msg_anchor(event) -> tuple:
    """取当前**入站消息**的唯一标识，用作确认闸门的锚点。

    返回 (message_id, timestamp)。message_id 由平台生成：CI 注入链路是
    `dispatcher.py` 的 uuid4，真实 OneBot 链路是 OneBot 的消息 id——每条
    入站消息都不同。用「消息身份」而不是「消息文本」来判定「确认是不是
    提议之后新发的一条」，可以避开一个致命坑：模型在同一轮里把提议动作
    重调一遍时，旧的文本锚点会被写成管理员那句「确认」本身，此后管理员
    每一次「确认」都等于锚点，闸门被永久锁死。
    """
    obj = getattr(event, "message_obj", None)
    mid = str(getattr(obj, "message_id", "") or "")
    try:
        ts = int(getattr(obj, "timestamp", 0) or 0)
    except Exception:
        ts = 0
    return mid, ts


async def _collect_event_images(event: AstrMessageEvent) -> list[dict]:
    """收集触发消息中的图片（本地 base64 图 / http 网络图），供论坛发帖附图使用。"""
    collected: list[dict] = []
    for seg in event.get_messages() or []:
        if len(collected) >= MAX_ATTACH_IMAGES:
            break
        if not isinstance(seg, Comp.Image):
            continue
        url = str(getattr(seg, "url", "") or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            collected.append({"url": url})
            continue
        try:
            local_path = await seg.convert_to_file_path()
        except Exception as e:
            logger.warning(f"[ClassIntra] 附图转本地文件失败: {e}")
            continue
        try:
            if os.path.getsize(local_path) <= 0:
                continue
            if os.path.getsize(local_path) > MAX_ATTACH_BYTES:
                logger.warning(f"[ClassIntra] 附图超过 {MAX_ATTACH_BYTES // (1024 * 1024)}MB，已跳过")
                continue
            with open(local_path, "rb") as fh:
                collected.append(
                    {"base64": base64.b64encode(fh.read()).decode("ascii")}
                )
        except Exception as e:
            logger.warning(f"[ClassIntra] 附图读取失败: {e}")
    return collected


class ClassIntraPlugin(star.Star):
    """ClassIntra 接入插件（2026-09-28 合并后的唯一入口）。

    合并了原 astrbot_plugin_ci_downloader 与 astrbot_plugin_linxi_memory。
    为什么必须是一个插件、一个入口模块：AstrBot 用
    `star_map[handler.handler_module_path]` 定位插件，而 `handler_module_path`
    就是函数所在模块（`get_handler_or_create` 取 `handler.__module__`），
    因此**所有 @filter.* 装饰器必须定义在本文件**；逻辑放在同包的普通模块里
    （downloader.py / memory_plugin.py / profile_store.py …），本文件只做转发与 schema。

    提供能力：
      1. HTTP API（POST /api/chat）：同步/异步对话，返回完整消息链与资源列表
      2. 资源代理下载（GET /classintra_res/{token}）：机器人生成的本地文件可被
         ClassIntra 资源网关透传给前端
      3. 指令 /ci_status：查看插件运行状态
      4. 工具 download_video：下载视频到 ClassIntra 站内（公屏全员 / 私聊授权人）
      5. 跨会话人物档案 + 学别人讲话（档案注入 / 异步抽取 / 向量召回 / 风格卡）
         指令 /记忆 /学说话 /风格
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        # CI 侧路径由配置决定，必须在建 downloader / api_server 之前算好
        try:
            ci_paths.configure(self.config)
        except Exception as e:  # 配置异常不该让插件加载失败
            logger.warning(f"[ClassIntra] 路径配置解析失败（已忽略）: {e}")
        self.dispatcher = ClassIntraDispatcher(context, self.config)
        self.api_server = ClassIntraAPIServer(self.config, self.dispatcher)
        self._server_task: asyncio.Task | None = None
        # 原 ci_downloader：私聊下载权限沿用统一的授权人名单（原来它自己有一份 admins）
        self.downloader = CiVideoDownloader(self.config, self._owner_ids())
        # 原 linxi_memory：档案库沿用旧插件名下的 profiles.db，不做数据迁移
        self.memory = LinxiMemoryService(context, self.config)

    async def initialize(self):
        # 视频数量上限的启动清理（原 ci_downloader.initialize）
        try:
            await self.downloader.startup()
        except Exception as e:
            logger.warning(f"[ClassIntra] 视频清理初始化失败（已忽略）: {e}")
        host = str(self.config.get("api_host", "127.0.0.1") or "127.0.0.1")
        port = int(self.config.get("api_port", 6200) or 6200)
        self._server_task = asyncio.create_task(self.api_server.start(host, port))
        try:
            await self._server_task
        except OSError as e:
            # 端口被占用是最常见的首次安装问题。这里不让插件加载失败——
            # 否则用户连「去哪改」都看不到；改为明确报错 + 指路，其余能力照常可用。
            logger.error(
                f"[ClassIntra] HTTP API 启动失败：{host}:{port} 无法监听（{e}）。"
                f"多半是端口被占用，请在插件配置里把 API 监听端口改成一个空闲端口后重启 AstrBot。"
                f"（其余功能不受影响，/ci_status 可看到 API 未启动）"
            )
        except Exception as e:
            logger.error(f"[ClassIntra] HTTP API 启动失败: {e}", exc_info=True)

    @filter.command("ci_status")
    async def ci_status(self, event: AstrMessageEvent):
        """查看 ClassIntra 接入插件状态"""
        s = self.dispatcher.stats
        yield event.plain_result(
            "📊 ClassIntra 接入状态\n"
            f"API 服务：{self.api_server.status_text()}\n"
            f"已处理请求：{s['total']}（同步 {s['sync']} / 异步 {s['async']}）\n"
            f"进行中：{s['pending']} ｜ 失败：{s['failed']}"
        )

    @filter.llm_tool(name="publish_classintra_post")
    async def publish_classintra_post(
        self,
        event: AstrMessageEvent,
        content: str,
        title: str = "",
        anonymous: bool = False,
        attach_images: bool = True,
        image_urls: str = "",
    ):
        """写一篇帖子并发布到 ClassIntra 社区论坛（作者为林晞机器人账号），发布后立即向用户展示预览。

        当用户明确要求"发帖 / 发到论坛 / 发到社区 / 帮我发一条"，或要求把刚发的图片发成论坛帖子时调用。
        用户要求"带上图片 / 把图也发上去"时，务必从对话上下文中找出图片的 URL（用户发过的图、你刚发的图都会以 http 链接形式出现在上下文里），填入 image_urls 传入，否则帖子会没有图。

        Args:
            content(string): 帖子正文（支持多行文本与 Markdown 语法，必填）
            title(string): 帖子标题（可选，不填则为无标题帖子）
            anonymous(boolean): 是否匿名发布，默认不匿名
            attach_images(boolean): 是否自动附上"本条触发消息"里的图片，默认 true
            image_urls(string): 图片链接列表（可选）。从对话上下文中提取的图片 URL，用逗号或换行分隔，最多 9 张；与 attach_images 叠加去重
        """
        text = str(content or "").strip()
        if not text:
            return "发帖失败：帖子正文为空，无法发布。"
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return "发帖失败：插件未配置 publish_key，无法调用 ClassIntra 发布接口。"
        import aiohttp
        import re as _re

        images: list[dict] = []
        # 1) 上下文图片链接：AI 从对话历史里提取的用户图片 URL
        seen_urls: set = set()
        for u in _re.split(r"[,，\s]+", str(image_urls or "")):
            u = u.strip()
            if u.startswith("http://") or u.startswith("https://"):
                if u not in seen_urls:
                    seen_urls.add(u)
                    images.append({"url": u})
            if len(images) >= MAX_ATTACH_IMAGES:
                break
        # 2) 触发消息里自带的图片（原行为保留）
        if attach_images:
            for item in await _collect_event_images(event):
                u = str(item.get("url") or "")
                if u and u in seen_urls:
                    continue
                if u:
                    seen_urls.add(u)
                images.append(item)
                if len(images) >= MAX_ATTACH_IMAGES:
                    break
        images = images[:MAX_ATTACH_IMAGES]

        payload = {
            "type": "forum",
            "title": str(title or "").strip(),
            "content": text,
            "anonymous": bool(anonymous),
            "tags": [],
            "images": images,
        }
        data = {}
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            # 附图 base64 可能较大：以 text/plain 发送 JSON 原文，避开 CI 侧全局 1MB JSON 限制
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    base + "/api/astrbot/publish",
                    data=body_bytes,
                    headers={
                        "x-publish-key": key,
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status != 200 or data.get("code") != 200:
                        return "发帖失败：" + str(data.get("message") or ("HTTP " + str(resp.status)))
        except Exception as e:
            logger.error(f"[ClassIntra] 论坛发帖接口调用失败: {e}", exc_info=True)
            return "发帖失败：调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"
        pid = (data.get("data") or {}).get("id")
        if pid is None:
            return "发帖失败：ClassIntra 未返回帖子 ID。"

        # 参考 QQ空间插件 llm_publish_feed：发布成功后立即向用户展示"已发布 + 内容预览"
        suffix = "（匿名）" if anonymous else ""
        title_line = f"标题：{str(title).strip()}\n" if str(title).strip() else ""
        body_preview = text if len(text) <= 120 else text[:120] + "…"
        img_note = f"\n附图 {len(images)} 张" if images else ""
        preview = (
            f"✅ 已在社区论坛发布帖子{suffix}\n"
            f"{title_line}"
            f"内容：{body_preview}"
            f"{img_note}\n"
            f"帖子 ID：{pid}"
        )
        try:
            await event.send(event.chain_result([Comp.Plain(preview)]))
        except Exception as e:
            logger.warning(f"[ClassIntra] 发布预览消息发送失败: {e}")
        return f"已在 ClassIntra 社区论坛发布帖子{suffix}，帖子 ID 为 {pid}，预览消息已发给用户。"

    @filter.llm_tool(name="read_classintra_post")
    async def read_classintra_post(self, event: AstrMessageEvent, post_id: str):
        """读取 ClassIntra 社区帖子的完整内容与最新评论。

        当用户分享帖子分享卡片、给出帖子 ID、或要求"看帖子 / 锐评帖子 / 总结帖子"时调用。

        Args:
            post_id(string): 帖子 ID（纯数字，分享卡片文本中会带有"帖子ID：xxx"）
        """
        pid = str(post_id or "").strip()
        import re as _re

        m = _re.search(r"(\d{1,12})", pid)
        if not m:
            return "读取失败：帖子 ID 必须是数字。"
        pid = m.group(1)
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return "读取失败：插件未配置 publish_key。"
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    base + "/api/astrbot/post/" + pid,
                    headers={"x-publish-key": key},
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status != 200 or data.get("code") != 200:
                        return "读取失败：" + str(data.get("message") or ("HTTP " + str(resp.status)))
        except Exception as e:
            logger.error(f"[ClassIntra] 读取帖子接口调用失败: {e}", exc_info=True)
            return "读取失败：调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"
        d = data.get("data") or {}
        title = d.get("title") or "无标题"
        author = d.get("author") or "匿名"
        created = str(d.get("created_at") or "").replace("T", " ")[:16]
        body = str(d.get("content") or "").strip()
        if len(body) > 1500:
            body = body[:1500] + "……（正文过长已截断）"
        lines = [
            f"《{title}》",
            f"作者：{author} · {created} · 赞 {d.get('like_count', 0)} / 评论 {d.get('comment_count', 0)}",
            "",
            body or "（无正文）",
        ]
        comments = d.get("comments") or []
        if comments:
            lines.append("")
            lines.append("—— 最新评论 ——")
            for c in comments[:10]:
                lines.append(f"{c.get('author','?')}：{str(c.get('content',''))[:150]}")
        return "\n".join(lines)

    @filter.llm_tool(name="delete_classintra_post")
    async def delete_classintra_post(self, event: AstrMessageEvent, post_id: str):
        """删除一篇你（林晞）发布到 ClassIntra 社区论坛的帖子。

        只能删除你自己（林晞）发布的帖子；别人的帖子无法删除。当用户要求"删掉那条帖子 / 撤了刚发的帖子"且帖子是你发的时调用。

        Args:
            post_id(string): 帖子 ID（纯数字，发布成功时曾告知用户）
        """
        pid = str(post_id or "").strip()
        import re as _re

        m = _re.search(r"(\d{1,12})", pid)
        if not m:
            return "删除失败：帖子 ID 必须是数字。"
        pid = m.group(1)
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return "删除失败：插件未配置 publish_key。"
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.delete(
                    base + "/api/astrbot/post/" + pid,
                    headers={"x-publish-key": key},
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status == 403:
                        return "删除失败：这篇帖子不是我发的，无权删除。"
                    if resp.status == 404 or (data.get("message") or "").find("不存在") > -1:
                        return "删除失败：帖子不存在或已被删除。"
                    if resp.status != 200 or data.get("code") != 200:
                        return "删除失败：" + str(data.get("message") or ("HTTP " + str(resp.status)))
        except Exception as e:
            logger.error(f"[ClassIntra] 删帖接口调用失败: {e}", exc_info=True)
            return "删除失败：调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"
        logger.info(f"[ClassIntra] 帖子已删除: {pid}")
        return f"已删除帖子 {pid}。请告知用户帖子已删除。"

    @filter.llm_tool(name="recall_classintra_messages")
    async def recall_classintra_messages(self, event: AstrMessageEvent, count: int = 1):
        """撤回你（林晞）最近在当前聊天发出的消息（仅限 2 分钟内发出的）。

        当用户说"撤回 / 收回你刚才说的话 / 当我没说"时调用。只能撤回你自己发的消息，且发出超过 2 分钟就无法撤回。

        Args:
            count(number): 要撤回的条数，默认 1 条，最多 10 条。
        """
        try:
            count = max(1, min(10, int(count)))
        except Exception:
            count = 1
        # 公共频道判定统一走 ci_session.channel_of（主插件内唯一实现，别再内联）
        channel, target = channel_of(event)
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return "撤回失败：插件未配置 publish_key。"
        import aiohttp

        payload = {"channel": channel, "target": target, "count": count}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    base + "/api/astrbot/recall",
                    data=body_bytes,
                    headers={"x-publish-key": key, "Content-Type": "text/plain; charset=utf-8"},
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status != 200 or data.get("code") != 200:
                        return "撤回失败：" + str(data.get("message") or ("HTTP " + str(resp.status)))
        except Exception as e:
            logger.error(f"[ClassIntra] 撤回接口调用失败: {e}", exc_info=True)
            return "撤回失败：调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"
        recalled = int((data.get("data") or {}).get("recalled") or 0)
        if recalled <= 0:
            return "撤回失败：没找到 2 分钟内发出的可撤回消息。"
        return f"已撤回 {recalled} 条消息。请用一句话告知用户已撤回。"

    # ===== 站内信息共享：读实据、回帖（林晞是 ClassIntra 的一部分）=====

    async def _ci_get_json(self, path: str, params: dict | None = None):
        """调用 ClassIntra 的 /api/astrbot/* 只读接口，返回 (ok, payload_or_message)。"""
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return False, "插件未配置 publish_key，无法读取 ClassIntra 信息。"
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    base + path, params=params or {}, headers={"x-publish-key": key}
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status != 200 or data.get("code") != 200:
                        return False, str(data.get("message") or ("HTTP " + str(resp.status)))
                    return True, data.get("data")
        except Exception as e:
            logger.error(f"[ClassIntra] 信息读取接口调用失败: {e}", exc_info=True)
            return False, "调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"

    @filter.llm_tool(name="read_classintra_info")
    async def read_classintra_info(
        self,
        event: AstrMessageEvent,
        kind: str,
        limit: int = 10,
        sort: str = "latest",
        path: str = "",
        post_id: str = "",
    ):
        """读取 ClassIntra 站内的实时信息，用真实数据回答，绝不凭印象编。

        只要用户问的是 ClassIntra 里的**具体事实**，就必须先调用本工具拿到实据再回答，例如：
        最新公告/通知、快讯、"最近论坛在聊什么""有什么热帖""谁发了啥"、想看某帖的评论、
        "今天天气怎么样"、资源仓库里有什么、"站里最近有啥动静"等。

        Args:
            kind(string): 读哪一类，取值之一：
                announcements=公告与通知；broadcasts=快讯；forum=社区帖子列表；
                comments=某帖评论（需同时给 post_id）；weather=天气；
                resources=资源仓库文件列表（可用 path 指定子目录，如 videos）；
                pulse=全景概览（公告+快讯+热帖+最新帖+天气+资源根目录；拿不准时用它）。
            limit(number): 最多返回多少条，默认 10（帖子最多 30，文件最多 200）。
            sort(string): 帖子排序，latest=最新（默认），hot=最热。
            path(string): kind=resources 时的子目录（相对资源根目录，默认根目录）。
            post_id(string): kind=comments 时的帖子 ID（纯数字）。
        """
        alias = {
            "公告": "announcements", "通知": "announcements", "announcement": "announcements",
            "快讯": "broadcasts", "播报": "broadcasts", "broadcast": "broadcasts",
            "帖子": "forum", "论坛": "forum", "社区": "forum", "post": "forum", "posts": "forum",
            "评论": "comments", "回帖": "comments", "comment": "comments",
            "天气": "weather",
            "资源": "resources", "文件": "resources", "resource": "resources",
            "概览": "pulse", "全景": "pulse", "总览": "pulse", "pulse": "pulse", "info": "pulse",
        }
        raw = str(kind or "").strip()
        k = alias.get(raw) or alias.get(raw.lower()) or raw.lower()
        try:
            limit = max(1, min(200, int(limit)))
        except Exception:
            limit = 10
        sort = "hot" if str(sort or "").strip().lower() in ("hot", "热", "最热") else "latest"

        if k == "announcements":
            ok, d = await self._ci_get_json("/api/astrbot/info/announcements", {"limit": limit})
            if not ok:
                return "读取公告失败：" + d
            if not d:
                return "站内目前没有公告。"
            out = ["【ClassIntra 公告】"]
            for a in d:
                created = str(a.get("created_at") or "").replace("T", " ")[:16]
                pin = "【置顶】" if a.get("pinned") else ""
                # 必须带 #id：管理动作 announce_edit / announce_pin / announce_delete
                # 都以「公告 ID」为参数，列表不给 ID 则「把这条公告改了/删了」无法定位。
                out.append(f"#{a.get('id')} {pin}《{a.get('title') or '无标题'}》（{a.get('author') or '?'} · {created}）")
                out.append(str(a.get("content") or "").strip())
                out.append("")
            return "\n".join(out).strip()

        if k == "broadcasts":
            ok, d = await self._ci_get_json("/api/astrbot/info/broadcasts", {"limit": limit})
            if not ok:
                return "读取快讯失败：" + d
            if not d:
                return "站内目前没有快讯。"
            out = ["【ClassIntra 快讯（最新在前）】"]
            for b in d:
                created = str(b.get("created_at") or "").replace("T", " ")[:16]
                out.append(f"· {str(b.get('content') or '').strip()}（{created}）")
            return "\n".join(out)

        if k == "forum":
            ok, d = await self._ci_get_json(
                "/api/astrbot/info/forum", {"limit": min(limit, 30), "sort": sort, "type": "forum"}
            )
            if not ok:
                return "读取帖子失败：" + d
            posts = (d or {}).get("posts") or []
            if not posts:
                return "社区目前没有帖子。"
            out = [f"【ClassIntra 社区帖子 · {'最热' if sort == 'hot' else '最新'} · 共 {d.get('total', len(posts))} 条】"]
            for p in posts:
                created = str(p.get("created_at") or "").replace("T", " ")[:16]
                title = p.get("title") or "（无标题）"
                out.append(
                    f"#{p.get('id')} 《{title}》 {p.get('author') or '?'} · 赞{p.get('like_count', 0)}/评{p.get('comment_count', 0)} · {created}"
                )
                body = str(p.get("content") or "").strip().replace("\n", " ")
                if body:
                    out.append("   " + (body[:160] + "…" if len(body) > 160 else body))
            return "\n".join(out)

        if k == "comments":
            m = _re_search_digits(post_id)
            if not m:
                return "读取评论失败：需要提供数字帖子 ID（post_id）。"
            ok, d = await self._ci_get_json(
                "/api/astrbot/info/post/" + m + "/comments", {"limit": limit}
            )
            if not ok:
                return "读取评论失败：" + d
            if not d:
                return f"帖子 #{m} 目前还没有评论。"
            out = [f"【帖子 #{m} 的评论（{len(d)} 条）】"]
            for c in d:
                created = str(c.get("created_at") or "").replace("T", " ")[:16]
                out.append(f"{c.get('author') or '?'}（{created}）：{str(c.get('content') or '').strip()}")
            return "\n".join(out)

        if k == "weather":
            ok, d = await self._ci_get_json("/api/astrbot/info/weather")
            if not ok:
                return "读取天气失败：" + d
            cur = (d or {}).get("current") or {}
            today = (d or {}).get("today") or {}
            air = (d or {}).get("air") or {}
            out = ["【ClassIntra 天气】"]
            if cur:
                out.append(
                    "现在：{t} {temp}℃ 体感{feel}℃ 湿度{hum}% {wind}风{scale}级 降水{precip}mm".format(
                        t=cur.get("text", ""), temp=cur.get("temp", "?"), feel=cur.get("feelsLike", "?"),
                        hum=cur.get("humidity", "?"), wind=cur.get("windDir", ""),
                        scale=cur.get("windScale", ""), precip=cur.get("precip", ""),
                    )
                )
            if today:
                out.append(
                    "今天：{}~{}℃ {}转{} 紫外线{}".format(
                        today.get("tempMin", "?"), today.get("tempMax", "?"),
                        today.get("textDay", ""), today.get("textNight", ""), today.get("uvIndex", "?"),
                    )
                )
            if air:
                out.append(f"空气：AQI {air.get('aqi', '?')}（{air.get('category', '')}）")
            for w in (d or {}).get("alerts") or []:
                out.append(f"⚠️ 预警：{w.get('title') or w.get('type')} {str(w.get('text') or '')[:120]}")
            idx = (d or {}).get("indices") or []
            if idx:
                out.append("生活指数：" + "；".join(f"{i.get('name')}{i.get('category')}" for i in idx))
            return "\n".join(out)

        if k == "resources":
            ok, d = await self._ci_get_json(
                "/api/astrbot/info/resources", {"limit": limit, "path": str(path or "").strip()}
            )
            if not ok:
                return "读取资源仓库失败：" + d
            if not d:
                return "资源仓库为空。"
            out = [f"【资源仓库 {d.get('path') or '/'}（共 {d.get('count', 0)} 项，显示 {len(d.get('entries') or [])} 项）】"]
            for f in d.get("entries") or []:
                if f.get("is_dir"):
                    out.append(f"📁 {f.get('name')}/")
                else:
                    out.append(f"📄 {f.get('name')}（{f.get('size_text')}）")
            return "\n".join(out)

        # 默认 / pulse
        ok, d = await self._ci_get_json("/api/astrbot/info/pulse")
        if not ok:
            return "读取站内概览失败：" + d
        d = d or {}
        out = ["【ClassIntra 站内概览】"]
        anns = d.get("announcements") or []
        if anns:
            out.append("◆ 公告：")
            for a in anns[:3]:
                out.append(f"  · {a.get('title') or '无标题'}（{str(a.get('created_at') or '').replace('T', ' ')[:16]}）")
        bcs = d.get("broadcasts") or []
        if bcs:
            out.append("◆ 最新快讯：" + " / ".join(str(b.get("content") or "").strip() for b in bcs[:5]))
        hots = d.get("hot_posts") or []
        if hots:
            out.append("◆ 热帖：")
            for p in hots[:3]:
                out.append(
                    f"  · #{p.get('id')} {p.get('title') or str(p.get('content') or '')[:20]}（{p.get('author') or '?'} · 赞{p.get('like_count', 0)}）"
                )
        latests = d.get("latest_posts") or []
        if latests:
            out.append("◆ 最新帖：")
            for p in latests[:3]:
                out.append(
                    f"  · #{p.get('id')} {p.get('title') or str(p.get('content') or '')[:20]}（{p.get('author') or '?'}）"
                )
        w = d.get("weather") or {}
        cur = w.get("current") or {}
        if cur:
            out.append(f"◆ 天气：{cur.get('text', '')} {cur.get('temp', '?')}℃")
        res = d.get("resources_root") or []
        if res:
            out.append("◆ 资源根目录：" + "、".join(str(r.get("name")) for r in res[:8]))
        return "\n".join(out)

    @filter.llm_tool(name="comment_classintra_post")
    async def comment_classintra_post(
        self, event: AstrMessageEvent, post_id: str, content: str
    ):
        """以林晞（你）的身份在 ClassIntra 社区某篇帖子下回复一条评论。

        当用户要求"回他一句 / 帮我在那个帖子下面评论 / 你去回一下 / 锐评一下并回复"时调用。
        需要先知道帖子 ID：分享卡片里会带"帖子ID：xxx"；不确定时先用
        read_classintra_info(kind="forum") 查帖子列表拿到 ID。

        Args:
            post_id(string): 帖子 ID（纯数字）
            content(string): 回复内容（用林晞自己的口吻，别太长，一两句即可）
        """
        m = _re_search_digits(post_id)
        if not m:
            return "回帖失败：需要提供数字帖子 ID（post_id）。"
        text = str(content or "").strip()
        if not text:
            return "回帖失败：回复内容为空。"
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return "回帖失败：插件未配置 publish_key。"
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            body = json.dumps({"content": text}, ensure_ascii=False).encode("utf-8")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    base + "/api/astrbot/post/" + m + "/comment",
                    data=body,
                    headers={"x-publish-key": key, "Content-Type": "application/json; charset=utf-8"},
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status == 404 or "不存在" in str(data.get("message") or ""):
                        return "回帖失败：帖子不存在或已被删除。"
                    if resp.status == 429:
                        return "回帖失败：评论太频繁了，等一会儿再试。"
                    if resp.status != 200 or data.get("code") != 200:
                        return "回帖失败：" + str(data.get("message") or ("HTTP " + str(resp.status)))
        except Exception as e:
            logger.error(f"[ClassIntra] 回帖接口调用失败: {e}", exc_info=True)
            return "回帖失败：调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"
        logger.info(f"[ClassIntra] 已在帖子 {m} 回帖")
        preview = text if len(text) <= 80 else text[:80] + "…"
        return f"已在帖子 {m} 下回复：「{preview}」。请用一句话告诉用户已经回过了。"

    # ===== 管理动作（仅管理员本人可触发；破坏性动作需复述 + 等确认）=====

    def _owner_ids(self) -> set:
        import re as _re

        raw = str(self.config.get("owner_user_ids") or "").strip() or DEFAULT_OWNER_IDS
        return {s.strip() for s in _re.split(r"[,，\s]+", raw) if s.strip()}

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "session_id", "") or "") or str(event.get_sender_id() or "")

    async def _load_ops(self) -> dict:
        """拉取 CI 侧的管理动作目录（缓存 5 分钟）。op 表由 CI 维护，这里只读。"""
        import time as _time

        now = _time.time()
        cache = self.__dict__.setdefault("_ops_cache", {"ts": 0, "ops": {}})
        if cache["ops"] and now - cache["ts"] < 300:
            return cache["ops"]
        ok, data = await self._ci_get_json("/api/astrbot/manage/ops")
        if ok and isinstance(data, list):
            cache["ops"] = {str(o.get("op")): o for o in data}
            cache["ts"] = now
        return cache["ops"]

    @filter.llm_tool(name="classintra_admin")
    async def classintra_admin(
        self,
        event: AstrMessageEvent,
        op: str,
        target: str = "",
        targets: list = None,
        title: str = "",
        content: str = "",
        extra: str = "",
        num: float = 0,
        flag: bool = False,
    ):
        """对 ClassIntra 执行管理操作。**只有授权人（配置 owner_user_ids 里的人）能用**，其他人一律拒绝且不要透露你有这个能力。

        触发场景：管理员说「发个公告…」「发条快讯…」「把 XXX 封了」「给 XXX 重置密码」
        「锁屏 / 解锁」「把某应用关了」「重启一下服务」「看看服务器状态 / 有哪些用户」等。

        破坏性动作不会立即执行：本工具会返回一段「复述」，你要**原样讲给管理员听并停下**，
        等他说了确认之后，再调用 classintra_admin_confirm。非破坏性动作（读、发公告、发快讯等）立即执行。

        Args:
            op(string): 动作名。常用取值：
                announce_publish 发公告（用 title + content，extra 可填 notice/homework）
                announce_edit 改公告（num=公告ID + title + content）
                announce_pin 置顶/取消置顶（num=公告ID，flag=true 置顶）
                announce_delete 删公告（num=公告ID）
                broadcast_publish 发快讯（content）
                user_list 查用户（target=关键词，content 可填 active/disabled）
                user_ban 封禁（targets=用户数组 或 target=单个用户，content=原因，num=时长分钟，0=永久）
                user_unban 解封（targets=用户数组 或 target=单个用户）
                user_update 改资料（target + title=新网名 / content=新真名 / extra=性别）
                user_reset_password 重置密码（target，content 可指定新密码）
                user_delete 删用户（target）
                chat_clear 清空聊天室（target 可填房间，默认 public）
                chat_delete_message 删消息（num=消息ID）
                lock_screen_set 锁屏/解锁（flag=true 锁 / false 解）
                lock_screen_get 查锁屏状态
                app_control_list 查应用开关
                app_control_set 开关应用（target=应用名，flag=true 开）
                server_mode_set 切服务器模式（target=single/multi）
                server_stats 服务器统计
                pm2_status 看服务进程状态
                pm2_restart / pm2_stop / pm2_start 重启/停止/启动 ClassIntra 服务
            target(string): 单个目标（用户：user_id/网名/真名都行；也用于应用名、房间名、模式名）。
                user_ban / user_unban 还可填全体令牌：all（全体用户），或 all_except_<用户…>（全体除某些人，
                例 all_except_251800、all_except:张三,李四）。**范围不含管理员与班管**，这是有意的安全边界。
            targets(list[string]): 批量目标列表（仅 user_ban / user_unban 使用）。要对多个用户做同一个动作时，把每个人分别放进这个数组，例如 ["张三","李四","25180123"]；一次最多 20 个。与 target 会合并并按“人”去重。要「封全体」就用 all 令牌，别自己一个个列名单
            title(string): 公告标题 / 用户新网名
            content(string): 正文（公告内容/快讯内容/封禁原因/新密码/用户新真名）
            extra(string): 补充（公告类型 notice/homework；用户性别 男/女）
            num(number): 数字参数（公告ID/消息ID/封禁时长分钟）
            flag(boolean): 开关类参数（置顶、锁屏、应用启停）
        """
        import re as _re
        import time as _time

        sender = str(event.get_sender_id() or "").strip()
        if sender not in self._owner_ids():
            # 非管理员：拒绝，且不暴露工具细节
            return "这个我管不了，得找管理员。"

        op = str(op or "").strip()
        if not op:
            return "没说要做什么，先问清楚要执行哪个操作。"

        ops = await self._load_ops()
        spec = ops.get(op)
        if not spec:
            if not ops:
                return "读取管理动作列表失败，请稍后再试。"
            return "没有这个管理动作：" + op

        params = {
            "target": str(target or "").strip(),
            "title": str(title or "").strip(),
            "content": str(content or "").strip(),
            "extra": str(extra or "").strip(),
        }

        # 批量目标：仅 user_ban / user_unban 支持。把 targets 数组与 target 字符串
        # （允许逗号/顿号/分号/空白分隔）合并后按人去重，改写为 params["targets"]。
        # 上限 20 人：避免「一句话封全班」这类误操作。
        if op in _BATCH_USER_OPS:
            names = []
            if isinstance(targets, (list, tuple)):
                for t in targets:
                    if isinstance(t, (list, tuple)):
                        names.extend(str(x).strip() for x in t)
                    else:
                        names.append(str(t or "").strip())
            if params["target"]:
                names.extend(p for p in _re.split(r"[,，、;；\s]+", params["target"]))
            norm, seen = [], set()
            for nm in names:
                nm = str(nm or "").strip()
                if nm and nm not in seen:
                    seen.add(nm)
                    norm.append(nm)
            if not norm:
                return "要封/解封谁？把用户（user_id / 网名 / 真名）告诉我。"
            if len(norm) > 20:
                return "一次最多处理 20 个用户（这次给了 %d 个）。请分批处理。" % len(norm)
            params.pop("target", None)
            params["targets"] = norm

        try:
            num_i = int(num or 0)
        except Exception:
            num_i = 0
        if num_i:
            params["num"] = num_i
        if flag:
            params["flag"] = True

        if spec.get("destructive"):
            skey = self._session_key(event)
            state = self.__dict__.setdefault("_pending", {})
            mid, mts = _msg_anchor(event)
            prev = state.get(skey)
            # 同一个动作（op + params 完全一致）被重复提议时，沿用最初那次提议的锚点
            # 与计时窗口。模型经常在「已提议过、管理员正在确认」的这一轮里把提议又调
            # 一遍；若此时重设锚点，锚点会变成管理员那句「确认」本身，之后他的每一次
            # 确认都等于锚点，闸门被永久拒绝（2026-09-28 实测的循环就是这么来的）。
            if prev and prev.get("op") == op and prev.get("params") == params:
                pend = prev
            else:
                pend = {
                    "op": op,
                    "params": params,
                    "desc": spec.get("desc") or op,
                    "msg_id": mid,
                    "msg_ts": mts,
                    "message": str(getattr(event, "message_str", "") or ""),
                    "ts": _time.time(),
                    "requester": sender,
                }
            state[skey] = pend
            # 复述必须逐条列出全部目标，管理员才能核对整批名单（批量封禁尤其重要）。
            # 「全体」令牌由 CI 展开成真实名单，这里如实说明范围，别显示成「目标 1 人：all」。
            lines = []
            all_ops = []
            if "targets" in params:
                tl = params["targets"]
                all_ops = [x for x in tl if _is_all_token(x)]
                plain = [x for x in tl if not _is_all_token(x)]
                if all_ops:
                    lines.append(
                        "目标：**全体用户**（%s）——范围由 ClassIntra 展开，**不含管理员与班管**"
                        % "、".join(all_ops)
                    )
                    if plain:
                        lines.append("另行指定：%s" % "、".join(plain))
                else:
                    lines.append("目标 %d 人：%s" % (len(tl), "、".join(tl)))
            rest = "、".join(
                "%s=%s" % (k, v)
                for k, v in params.items()
                if k != "targets" and v not in ("", 0, None, False)
            )
            if rest:
                lines.append(rest)
            detail = "\n".join(lines) or "（无参数）"
            head = "⚠️ 这是需要确认的动作，先别执行。\n"
            if all_ops:
                # 涉及人数可能有上百人，必须让管理员听见「全体」两个字再确认
                head += "⚠️ 注意：这是**全体**级别的批量操作，复述时务必把「全体」和排除名单讲清楚。\n"
            return (
                head
                + "动作：" + str(spec.get("desc") or op) + "\n"
                + "参数：" + detail + "\n"
                + "把上面这些复述给管理员听，问一句「确认执行吗」，然后**停下等他回话**。\n"
                + "他确认后再调用 classintra_admin_confirm；他说算了就调用 classintra_admin_cancel。\n"
                + "⚠️ 同一个动作**不要重复提议**：如果之前已经复述过、管理员这次就是来确认的，"
                + "直接调用 classintra_admin_confirm，不要再调 classintra_admin。"
            )

        # 非破坏性：直接执行
        ok, result = await self._ci_manage(sender, op, params, confirmed=False)
        if not ok:
            return "执行失败：" + str(result)
        return self._admin_result_text(op, spec, result, destructive=False)

    @filter.llm_tool(name="classintra_admin_confirm")
    async def classintra_admin_confirm(self, event: AstrMessageEvent):
        """确认执行上一条被复述过的**破坏性**管理动作。

        只有在管理员看过复述、并且**又发了一条消息明确表示确认**（如「确认」「可以」「执行」）之后才能调用。
        如果你刚复述完还没等到他回话，就不要调用本工具。

        注意：管理员发出的那条确认消息必须是**新的**一条消息。别为了让本工具通过而
        重新调用 classintra_admin 再提议一遍——重复提议不会让它生效，只会打乱等待中的动作。
        """
        import time as _time

        sender = str(event.get_sender_id() or "").strip()
        if sender not in self._owner_ids():
            return "这个我管不了，得找管理员。"

        state = self.__dict__.setdefault("_pending", {})
        skey = self._session_key(event)
        pend = state.get(skey)
        if not pend:
            return "现在没有等待确认的动作。"
        if _time.time() - pend.get("ts", 0) > 180:
            state.pop(skey, None)
            return "那条动作已经过了确认时限，作废了。请重新说一次要做什么。"

        cur_mid, cur_ts = _msg_anchor(event)
        cur_msg = str(getattr(event, "message_str", "") or "")
        # 硬闸门：确认必须来自「提议之后的新一条消息」，模型无法在同一轮里自问自答。
        # 判据用**消息身份**（message_id）而非消息文本——文本比较会被「模型重调提议
        # 把锚点写成管理员那句确认」这种情况永久锁死（详见 _msg_anchor 注释）。
        anc_mid = str(pend.get("msg_id") or "")
        if anc_mid:
            blocked = cur_mid == anc_mid
        else:
            # 兜底①：平台没给 message_id，退回入站时间戳（秒级）
            anc_ts = int(pend.get("msg_ts") or 0)
            if cur_ts and anc_ts:
                blocked = cur_ts <= anc_ts
            else:
                # 兜底②：连时间戳都没有，才退回旧的文本比较
                blocked = cur_msg.strip() == str(pend.get("message") or "").strip()
        if blocked:
            return (
                "你还没等到管理员回话——这是他上一次那条消息，不是新的确认。"
                "把复述讲给他听，然后停下等他单独回一句「确认」。\n"
                "⚠️ 不要重复调用 classintra_admin，也不要重复调用本工具；等他回话。"
            )
        if not _is_confirm_text(cur_msg):
            return (
                "管理员这句不像是确认。把复述再讲一遍，请他明确回「确认」或「算了」。\n"
                "⚠️ 不要重复提议这个动作，已有等待确认的动作；等他明确表态。"
            )
        if pend.get("requester") != sender:
            return "这个动作是别的管理员发起的，你确认不了。"

        op = pend["op"]
        params = pend["params"]
        state.pop(skey, None)
        ok, result = await self._ci_manage(sender, op, params, confirmed=True)
        if not ok:
            return "执行失败：" + str(result)
        ops = await self._load_ops()
        return self._admin_result_text(op, ops.get(op, {}), result, destructive=True)

    @filter.llm_tool(name="classintra_admin_cancel")
    async def classintra_admin_cancel(self, event: AstrMessageEvent):
        """取消上一条被复述、但管理员决定不做的管理动作。管理员说「算了」「不用了」「取消」时调用。"""
        state = self.__dict__.setdefault("_pending", {})
        skey = self._session_key(event)
        if state.pop(skey, None):
            return "已经取消那条动作，什么都没执行。"
        return "现在没有等待确认的动作。"

    async def _ci_manage(self, requester: str, op: str, params: dict, confirmed: bool):
        """调用 CI 的 /api/astrbot/manage。返回 (ok, data_or_message)。"""
        base = str(self.config.get("publish_base_url") or "http://127.0.0.1:9001").rstrip("/")
        key = str(self.config.get("publish_key") or "")
        if not key:
            return False, "插件未配置 publish_key，无法执行管理动作。"
        import aiohttp

        payload = {
            "requester_id": requester,
            "op": op,
            "params": params,
            "confirmed": bool(confirmed),
        }
        try:
            timeout = aiohttp.ClientTimeout(total=70)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    base + "/api/astrbot/manage",
                    data=body,
                    headers={"x-publish-key": key, "Content-Type": "application/json; charset=utf-8"},
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if resp.status != 200 or data.get("code") != 200:
                        return False, str(data.get("message") or ("HTTP " + str(resp.status)))
                    return True, data.get("data")
        except Exception as e:
            logger.error(f"[ClassIntra] 管理动作接口调用失败: {e}", exc_info=True)
            return False, "调用 ClassIntra 接口出错（" + type(e).__name__ + "）。"

    def _admin_result_text(self, op: str, spec: dict, result: dict, destructive: bool) -> str:
        """把管理动作结果整理成给模型看的一句话事实，便于她向管理员回报。"""
        desc = str((spec or {}).get("desc") or op)
        data = (result or {}).get("data")
        extra = ""
        # 批量动作的聚合结果（user_ban / user_unban 支持多目标）：成功数 + 失败名单
        if isinstance(data, dict) and "affected" in data and "failed" in data:
            failed = data.get("failed") or []
            extra = "成功 %s/%s 人" % (data.get("affected"), data.get("total"))
            if failed:
                items = [
                    "%s（%s）" % (f.get("target"), f.get("message"))
                    for f in failed[:10]
                ]
                extra += "；失败 %d 人：%s" % (len(failed), "、".join(items))
        elif op == "user_list" and isinstance(data, dict):
            users = data.get("users") or []
            lines = []
            for u in users[:15]:
                lines.append(
                    "%s(%s) %s%s"
                    % (
                        u.get("net_name") or u.get("real_name") or u.get("user_id"),
                        u.get("user_id"),
                        u.get("status") or "",
                        "·管理员" if u.get("is_admin") else "",
                    )
                )
            extra = "共 %s 人：\n%s" % (data.get("total"), "\n".join(lines))
        elif op == "user_reset_password" and isinstance(data, dict):
            if data.get("generated"):
                extra = "临时密码：%s（请让管理员尽快转告本人）" % data.get("temp_password")
        elif op == "announce_publish" and isinstance(data, dict):
            extra = "已发布，公告 ID：%s" % data.get("id")
        elif op in ("server_stats",) and isinstance(data, dict):
            mem = data.get("memory") or {}
            cpu = data.get("cpu") or {}
            extra = "CPU %s%% / 内存 %s%% / 在线用户 %s" % (
                cpu.get("usage"), mem.get("usagePercent"), data.get("total_users")
            )
        elif op in ("lock_screen_get", "lock_screen_set") and isinstance(data, dict):
            extra = "当前锁屏：" + ("已锁" if data.get("enabled") else "未锁")
        elif op == "app_control_list" and isinstance(data, dict):
            apps = data.get("apps") or []
            extra = "、".join("%s=%s" % (a.get("label") or a.get("name"), "开" if a.get("enabled") else "关") for a in apps[:20])
        elif op == "pm2_status" and isinstance(data, dict):
            procs = data.get("processes") or []
            extra = ("进程：" + "、".join("%s(%s)" % (p.get("name"), p.get("status")) for p in procs)) if procs else "PM2 里没有受管进程"

        head = ("已执行：" if destructive else "已完成：") + desc
        tail = "。请用一两句你自己的话告诉管理员结果，别念参数。"
        return head + ("\n" + extra if extra else "") + "\n" + tail

    # ================================================================== #
    # 以下为 2026-09-28 合并进来的两块能力（原 astrbot_plugin_ci_downloader /
    # astrbot_plugin_linxi_memory）。装饰器必须留在本模块（见类 docstring），
    # 所以这里只有签名、docstring 与转发；实现分别在 downloader.py / memory_plugin.py。
    # ================================================================== #

    @filter.llm_tool(name="download_video")
    async def download_video(self, event: AstrMessageEvent, url: str):
        """下载一个视频到 ClassIntra 站内，并返回可直接内联播放的链接。

        当用户想要"下载视频 / 发视频 / 给我搞个视频"时调用。若用户只给了关键词而没有链接，先用搜索工具找到视频页面链接（B站 BV 链接优先），再调用本工具。不要用本工具下载音乐或图片。

        Args:
            url(string): 必填。视频页面完整链接，例如 https://www.bilibili.com/video/BVxxxx
        """
        if not self.downloader.allowed(event):
            return "拒绝：私聊下载仅限管理员。请告知该用户：到公共聊天室里点名让我下载即可。"
        return await self.downloader.download(url)

    # ---------------------------------------------------- 人物档案 / 学别人讲话

    @filter.on_llm_request()
    async def inject_profile(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        await self.memory.inject_profile(event, req)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=1000)
    async def observe(self, event: AstrMessageEvent) -> None:
        """旁路采集**别人**的发言，作为「学说话」的语料。不回复、不阻塞。"""
        await self.memory.observe(event)

    @filter.on_agent_done()
    async def remember(self, event: AstrMessageEvent, run_context, response) -> None:
        await self.memory.remember(event, run_context, response)

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        """查看/清理人物档案。用法：/记忆 | /记忆 <user_id> | /记忆删 <user_id>"""
        async for result in self.memory.cmd_memory(event):
            yield result

    @filter.command("学说话")
    async def cmd_learn_style(self, event: AstrMessageEvent):
        """立刻学一次「大家怎么说话」。用法：/学说话"""
        async for result in self.memory.cmd_learn_style(event):
            yield result

    @filter.command("风格")
    async def cmd_style(self, event: AstrMessageEvent):
        """查看/清理风格卡与群近况。用法：/风格 | /风格清"""
        async for result in self.memory.cmd_style(event):
            yield result

    # priority=-99999：AstrBot 按 priority 降序执行（star_handler.py 的
    # `sort(key=lambda h: -h.extras_configs["priority"])`），负值使本钩子**最后**
    # 运行。必须最后的原因：meme_manager 这类插件会在各自的 after_message_sent
    # 里补发消息（未走混合分支的表情图片）。本钩子若先唤醒 HTTP 请求，调用方会
    # 立刻按此刻的 event.segments 组包返回，后补发的图片就落在快照之外被丢掉。
    # 实测症状：文字回复带了 &&标签&& 却收不到图，且丢图比例≈延迟发送分支的概率。
    @filter.after_message_sent(priority=-99999)
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """管线发送完成后唤醒等待中的 HTTP 请求（在所有插件补发消息之后）。"""
        request_id = event.get_extra("classintra_api_req")
        if request_id:
            self.dispatcher.notify_done(request_id)

    async def terminate(self):
        try:
            await self.memory.terminate()
        except Exception as e:
            logger.warning(f"[ClassIntra] 记忆模块停止异常（已忽略）: {e}")
        await self.api_server.stop()
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()

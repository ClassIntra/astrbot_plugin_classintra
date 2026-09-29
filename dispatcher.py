"""ClassIntra 管线分发器。

将 ClassIntra 插件发来的 HTTP 请求合成为 AstrBotMessage 事件，
提交到完整消息管线（唤醒检查 → 指令/LLM → 回复捕获），
并捕获回复消息链转换为 ClassIntra 可用的消息段格式。
"""

import asyncio
import os
import re
import shutil
import time
import uuid

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.event import MessageChain
from astrbot.api.platform import AstrMessageEvent
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

ADAPTER_NAME = "classintra"

# ---------------------------------------------------------------------------
# 人设表情标签替换
#
# 人设（personas.system_prompt）里约定：表达情绪时写 &&标签&&（如 &&fool&&
# &&sigh&& &&angry&&），并明确写着「系统会自动替换成表情包图片」。但此前
# **没有任何替换方** —— 标签原样发给了用户，全库实测 2434 次（含模型漂移出
# 的单 & 形态 &fool& 487 次），正是人设里专门警告过的「难看的纯文字」。
#
# 这里在文本出口统一替换成对应的标准 emoji：用户看到的是一个真实的表情，
# 而不是 &&fool&& 这种机械残渣。映射可用插件配置 persona_tag_map 覆盖。
# ---------------------------------------------------------------------------
PERSONA_TAG_PATTERN = re.compile(r"&{1,2}([A-Za-z_]{2,20})&{1,2}")

# 未映射标签的兜底：整条消息只剩标签时用它，避免发出空消息
# （人设本身也常用 "..." 作停顿，风格上不突兀）
PERSONA_TAG_FALLBACK = "…"

DEFAULT_PERSONA_TAG_EMOJI = {
    # 人设显式列举的
    "angry": "😠",
    "fool": "😏",
    "sigh": "😔",
    "happy": "😄",
    "sleep": "😴",
    # 实测出现过的其余标签
    "shy": "😳",
    "confused": "🤔",
    "see": "👀",
    "meow": "🐱",
    "baka": "😑",
    "cpu": "😵",
    "surprised": "😲",
    "reply": "💬",
    "like": "👍",
    "morning": "☀️",
    "color": "🎨",
    "sad": "😢",
    "givemoney": "💰",
    "no": "🙅",
}


def build_persona_tag_map(raw: object) -> dict[str, str]:
    """由插件配置构造标签→表情映射；配置为空则用内置默认。

    配置格式（空格/逗号分隔）：``fool=😏 sigh=😔``；
    写成 ``tag=-`` 表示把该标签从映射中移除（其出现会被直接删掉）。
    """
    mapping = dict(DEFAULT_PERSONA_TAG_EMOJI)
    if isinstance(raw, dict):
        items = [f"{k}={v}" for k, v in raw.items()]
    elif isinstance(raw, str):
        items = re.split(r"[\s,;]+", raw.strip())
    else:
        items = []
    for item in items:
        if not item or "=" not in item:
            continue
        tag, _, emoji = item.partition("=")
        tag = tag.strip().strip("&").lower()
        emoji = emoji.strip()
        if not tag:
            continue
        if emoji in {"", "-", "none", "None", "删除"}:
            mapping.pop(tag, None)
        else:
            mapping[tag] = emoji
    return mapping


def convert_persona_tags(text: str, tag_map: dict[str, str]) -> str:
    """把 ``&&标签&&`` / ``&标签&`` 换成对应表情；未知标签删除。

    只替换「一或两个 & + 2~20 个字母/下划线 + 一或两个 &」的完整形态，
    因此不会误伤 ``&amp;``、``?a=1&b=2&c=3`` 这类普通文本。
    """
    if not text or "&" not in text:
        return text

    def _sub(match: re.Match) -> str:
        return tag_map.get(match.group(1).lower(), "")

    converted = PERSONA_TAG_PATTERN.sub(_sub, text)
    if converted == text:
        return text

    # 标签独占一行时替换后可能留下空行/行尾空格，收敛掉
    converted = re.sub(r"[ \t]+\n", "\n", converted)
    converted = re.sub(r"\n{3,}", "\n\n", converted)
    # 行内残留的连续空格（删掉未知标签后可能出现）收敛为单个；
    # 按 ``` 围栏分段，避免破坏代码块里的缩进
    parts = converted.split("```")
    for i in range(0, len(parts), 2):  # 偶数索引 = 代码块之外
        parts[i] = re.sub(r"[ \t]{2,}", " ", parts[i])
    converted = "```".join(parts)
    if not converted.strip():
        return PERSONA_TAG_FALLBACK
    return converted.strip("\n")

# 适配器在 WebUI 添加平台时展示的默认配置模板（本插件以内嵌方式使用，无需用户添加平台）
DEFAULT_CONFIG_TMPL = {
    "id": "classintra_api",
    "enable": False,
    "note": "此适配器由 astrbot_plugin_classintra 内嵌使用，请勿手动启用",
}

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".silk": "audio/silk",
    ".amr": "audio/amr",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def _guess_mime(path: str, fallback: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _MIME_BY_SUFFIX.get(ext, fallback)


@register_platform_adapter(
    ADAPTER_NAME,
    "ClassIntra 校园内网平台（HTTP API 模式）",
    default_config_tmpl=DEFAULT_CONFIG_TMPL,
    adapter_display_name="ClassIntra",
    support_streaming_message=False,
)
class ClassIntraAPIPlatform(Platform):
    """内嵌平台适配器：仅用于合成事件与捕获回复，不建立任何外部连接。"""

    def __init__(self, platform_config: dict, event_queue) -> None:
        super().__init__(platform_config or {}, event_queue)
        self.dispatcher = None  # 由插件初始化时注入
        self._meta = PlatformMetadata(
            name=ADAPTER_NAME,
            description="ClassIntra 校园内网平台",
            id=(platform_config or {}).get("id", "classintra_api"),
            support_streaming_message=False,
        )

    def meta(self) -> PlatformMetadata:
        return self._meta

    def run(self):
        async def _forever():
            await asyncio.Event().wait()

        return _forever()

    def create_event(self, message: AstrBotMessage) -> "ClassIntraAPIEvent":
        return ClassIntraAPIEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self._meta,
            session_id=message.session_id,
            dispatcher=self.dispatcher,
        )


class ClassIntraAPIEvent(AstrMessageEvent):
    """捕获式事件：respond 阶段的 send() 不外发，而是收集消息段。"""

    def __init__(self, message_str, message_obj, platform_meta, session_id, dispatcher):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.dispatcher = dispatcher
        self.segments: list[dict] = []
        self.reply_texts: list[str] = []
        self.resources: list[dict] = []

    async def send(self, message: MessageChain | None) -> None:
        if message is None:
            return
        await self._capture(message)
        await super().send(MessageChain())

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        # 本平台不支持真实流式：聚合全部片段后一次性捕获
        buffer = MessageChain()
        async for chain in generator:
            if isinstance(chain, MessageChain):
                buffer.chain.extend(chain.chain)
        if buffer.chain:
            buffer.squash_plain()
            await self._capture(buffer)
        await super().send_streaming(generator, use_fallback)

    async def _capture(self, chain: MessageChain) -> None:
        for comp in chain.chain:
            try:
                await self._capture_component(comp)
            except Exception as e:
                logger.error(f"[ClassIntra] 消息段转换失败: {e}", exc_info=True)
                self.segments.append({"type": "plain", "text": "[消息段转换失败]"})

    async def _capture_component(self, comp) -> None:
        if isinstance(comp, Comp.Reply):
            return  # 引用组件无实际内容（direct 模式无引用上下文），跳过避免渲染出占位符
        if isinstance(comp, Comp.Plain):
            text = comp.text or ""
            if text:
                dispatcher = getattr(self, "dispatcher", None)
                if dispatcher is not None and getattr(dispatcher, "persona_tag_enable", False):
                    text = convert_persona_tags(
                        text, getattr(dispatcher, "persona_tag_map", None) or {}
                    )
                self.segments.append({"type": "plain", "text": text})
                self.reply_texts.append(text)
            return
        if isinstance(comp, Comp.At):
            name = comp.name or str(comp.qq or "")
            self.segments.append({"type": "plain", "text": f"@{name}"})
            self.reply_texts.append(f"@{name}")
            return
        if isinstance(comp, Comp.Image):
            await self._capture_media(comp, "image", "image/png", "[图片]")
            return
        if isinstance(comp, Comp.Record):
            await self._capture_media(comp, "record", "audio/wav", "[语音]")
            return
        if isinstance(comp, Comp.Video):
            await self._capture_media(comp, "video", "video/mp4", "[视频]")
            return
        if isinstance(comp, Comp.File):
            await self._capture_media(comp, "file", "application/octet-stream", "[文件]")
            return
        # 其余组件类型降级为占位文本
        self.segments.append({"type": "plain", "text": f"[{comp.type}]"})
        self.reply_texts.append(f"[{comp.type}]")

    async def _capture_media(self, comp, seg_type: str, mime: str, placeholder: str) -> None:
        url = getattr(comp, "url", "") or ""
        try:
            local_path = await comp.convert_to_file_path()
        except Exception as e:
            logger.warning(f"[ClassIntra] 媒体转本地文件失败（{seg_type}）: {e}")
            if url:
                self.segments.append({"type": seg_type, "url": url})
                self.reply_texts.append(placeholder)
            else:
                self.segments.append({"type": "plain", "text": placeholder})
                self.reply_texts.append(placeholder)
            return

        if self.dispatcher is None:
            self.segments.append({"type": seg_type, "url": url or local_path})
            self.reply_texts.append(placeholder)
            return

        limit_mb = float(self.dispatcher.max_resource_mb)
        if os.path.getsize(local_path) > limit_mb * 1024 * 1024:
            self.segments.append({"type": "plain", "text": f"{placeholder}（超过 {limit_mb:g}MB 未转发）"})
            self.reply_texts.append(placeholder)
            return

        token = uuid.uuid4().hex
        ext = os.path.splitext(local_path)[1].lower() or ".bin"
        res_dir = self.dispatcher.resource_dir
        os.makedirs(res_dir, exist_ok=True)
        dest = os.path.join(res_dir, token + ext)
        shutil.copyfile(local_path, dest)
        entry = {
            "path": f"/classintra_res/{token}",
            "mime": _guess_mime(dest, mime),
        }
        self.resources.append(entry)
        seg = {"type": seg_type, "resource_path": entry["path"]}
        if url.startswith("http"):
            seg["url"] = url
        self.segments.append(seg)
        self.reply_texts.append(placeholder)


class ClassIntraDispatcher:
    """接收 ClassIntra 插件请求，驱动完整消息管线并回收回复。"""

    def __init__(self, context, config: dict) -> None:
        self.context = context
        self.max_images = int(config.get("max_images", 4) or 4)
        self.max_resource_mb = float(config.get("max_resource_mb", 20) or 20)
        self.request_timeout = int(config.get("request_timeout", 120) or 120)
        self.persona_tag_enable = bool(config.get("persona_tag_enable", True))
        self.persona_tag_map = build_persona_tag_map(config.get("persona_tag_map"))
        self.resource_dir = os.path.join(
            get_astrbot_plugin_data_path(), "astrbot_plugin_classintra", "resources"
        )
        self.stats = {"total": 0, "sync": 0, "async": 0, "pending": 0, "failed": 0}
        self._pending: dict[str, asyncio.Event] = {}

        event_queue = context.get_event_queue()
        self.platform = ClassIntraAPIPlatform({"id": "classintra_api"}, event_queue)
        self.platform.dispatcher = self
        logger.info("[ClassIntra] 管线分发器已就绪")

    async def dispatch(self, request: dict) -> dict:
        """同步分发：等待管线完成后返回回复消息链。"""
        user_id = str(request.get("user_id") or "anonymous")
        session_id = str(request.get("session_id") or f"private_{user_id}")
        text = str(request.get("message") or "")
        images = request.get("images") or []

        if not text and not images:
            return {"error": "message 与 images 均为空"}

        abm = AstrBotMessage()
        abm.self_id = "classintra_api_bot"
        abm.message_id = uuid.uuid4().hex
        # 公共频道（session_id = group_public / group_<id>）必须带群语义：
        # 注入路径若恒为 FRIEND_MESSAGE 且无 group_id，所有依赖 get_group_id()
        # 的插件（视频下载的公屏/私聊权限、撤回频道、搜视频群号转换等）都会误判为私聊
        if session_id.startswith("group_"):
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = session_id[len("group_"):] or "public"
        else:
            abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = session_id
        abm.sender = MessageMember(
            user_id=user_id, nickname=str(request.get("user_name") or user_id)
        )
        comps: list = []
        if text:
            comps.append(Comp.Plain(text))
        for b64 in images[: self.max_images]:
            if not isinstance(b64, str) or not b64:
                continue
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[-1]
            try:
                comps.append(Comp.Image.fromBase64(b64))
            except Exception as e:
                logger.warning(f"[ClassIntra] 图片 Base64 无效，已跳过: {e}")
        abm.message = comps
        abm.message_str = text
        abm.raw_message = request
        abm.timestamp = int(time.time())

        event = self.platform.create_event(abm)
        # 私聊事件默认可被唤醒；显式标记以兼容 friend_message_needs_wake_prefix 开启的场景
        event.is_wake = True
        event.is_at_or_wake_command = True
        request_id = abm.message_id
        event.set_extra("classintra_api_req", request_id)

        done = asyncio.Event()
        self._pending[request_id] = done
        self.stats["total"] += 1
        self.stats["pending"] += 1
        try:
            self.platform.commit_event(event)
            await self._wait_completion(event, done)
        finally:
            self._pending.pop(request_id, None)
            self.stats["pending"] -= 1

        if not event.segments:
            self.stats["failed"] += 1
            return {
                "error": "管线未产生回复（事件被拦截或处理超时）",
                "session_id": session_id,
            }
        return {
            "reply": "".join(event.reply_texts).strip(),
            "message_chain": event.segments,
            "resources": event.resources,
            "session_id": session_id,
        }

    async def _wait_completion(self, event, done: asyncio.Event) -> None:
        """等待 after_message_sent 钩子；事件提前停止且无回复时快速返回。"""
        stopped_ticks = 0
        deadline = time.monotonic() + self.request_timeout
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(done.wait(), timeout=0.5)
                return
            except asyncio.TimeoutError:
                pass
            if event.is_stopped():
                stopped_ticks += 1
                # 事件已停止：给管线 2 秒收尾（钩子仍可能触发），随后放弃
                if stopped_ticks >= 4:
                    return
            else:
                stopped_ticks = 0

    def notify_done(self, request_id: str) -> None:
        done = self._pending.get(request_id)
        if done is not None:
            done.set()

    def allocate_task_id(self) -> str:
        self.stats["async"] += 1
        return uuid.uuid4().hex

    async def run_async_task(self, task_id: str, request: dict, callback_url: str) -> None:
        """异步模式：执行管线并把结果 POST 回 ClassIntra 回调地址。"""
        import aiohttp

        try:
            result = await self.dispatch(request)
            payload = {"task_id": task_id, **result}
            payload["status"] = "failed" if result.get("error") else "success"
            if payload["status"] == "failed":
                payload["error"] = result.get("error", "未知错误")
        except Exception as e:
            logger.error(f"[ClassIntra] 异步任务 {task_id} 执行失败: {e}", exc_info=True)
            payload = {"task_id": task_id, "status": "failed", "error": str(e)}

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(callback_url, json=payload) as resp:
                    logger.info(
                        f"[ClassIntra] 异步回调 {callback_url} 完成: HTTP {resp.status}"
                    )
        except Exception as e:
            logger.error(f"[ClassIntra] 异步回调 {callback_url} 失败: {e}")

    async def shutdown(self) -> None:
        pass

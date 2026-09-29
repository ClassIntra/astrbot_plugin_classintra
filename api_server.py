"""ClassIntra HTTP API 服务。

端点：
  POST /api/chat              统一对话接口（sync / async）
  GET  /api/chat/health       健康检查
  GET  /classintra_res/{tok}  资源下载（供 ClassIntra 资源代理透传）
"""

import asyncio
import json
import os

from aiohttp import web

from astrbot.api import logger

from . import ci_paths

# ClassIntra 侧媒体目录（插件直接写盘的资源不在本插件 resource_dir 内）。
# 用于 /classintra_res/{token} 的回退查找，使跨机（如 8 班经隧道）也能取到这类文件。
# 路径来源统一到 ci_paths（原来这里与 ci_downloader 各写一份，改一处忘另一处就会
# 出现「下载落盘了但资源代理找不到」）。
# 注意：不能在导入期固化成常量 —— ci_paths 的值由插件配置决定，配置在 __init__
# 才注入，且改配置后会被重算。这里改为每次请求时取。
def _classintra_media_dirs() -> list:
    return ci_paths.media_dirs()


class ClassIntraAPIServer:
    def __init__(self, config: dict, dispatcher) -> None:
        self.config = config or {}
        self.dispatcher = dispatcher
        self.api_token = str(self.config.get("api_token", "") or "")
        self.runner: web.AppRunner | None = None
        self._started = False

    @property
    def stats(self) -> dict:
        return self.dispatcher.stats

    def status_text(self) -> str:
        return "运行中" if self._started else "未启动"

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_post("/api/chat", self.handle_chat)
        app.router.add_get("/api/chat/health", self.handle_health)
        app.router.add_get("/classintra_res/{token}", self.handle_resource)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        self._started = True
        logger.info(f"[ClassIntra] HTTP API 监听 http://{host}:{port}")

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self._started = False

    def _check_auth(self, request: web.Request) -> bool:
        if not self.api_token:
            return True
        return request.headers.get("X-ClassIntra-Token", "") == self.api_token

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"code": 200, "message": "ok", "data": {"status": self.status_text()}}
        )

    async def handle_chat(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response(
                {"code": 401, "message": "token 无效", "data": None}, status=401
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"code": 400, "message": "请求体必须是 JSON", "data": None}, status=400
            )

        mode = str(body.get("mode") or "sync").lower()
        if mode == "async":
            callback_url = str(body.get("callback_url") or "").strip()
            if not callback_url:
                return web.json_response(
                    {"code": 400, "message": "async 模式必须提供 callback_url", "data": None},
                    status=400,
                )
            task_id = self.dispatcher.allocate_task_id()
            asyncio.create_task(
                self.dispatcher.run_async_task(task_id, body, callback_url)
            )
            return web.json_response({"mode": "async", "task_id": task_id})

        self.dispatcher.stats["sync"] += 1
        try:
            result = await self.dispatcher.dispatch(body)
        except Exception as e:
            logger.error(f"[ClassIntra] 处理请求失败: {e}", exc_info=True)
            return web.json_response(
                {"mode": "sync", "status": "failed", "error": f"内部错误: {e}"},
                status=500,
            )
        if result.get("error"):
            return web.json_response({"mode": "sync", **result}, status=504)
        return web.json_response({"mode": "sync", "status": "success", **result})

    async def handle_resource(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info.get("token", "")
        # 资源文件名白名单：纯小写字母数字 + 单层后缀，杜绝路径穿越。
        # 注意必须同时覆盖两类命名：
        #   1) 本插件签发的 token（32 位 uuid hex）
        #   2) 插件直接写盘的媒体（如 ci_downloader 的 dv*/mu*/tp* 前缀名，仅 12~16 位）
        # 早期只允许 [0-9a-f]{16,}，导致 dv*/mu* 文件无法经代理取回，
        # 跨机（如 8 班）拉不到资源，表现为聊天里图片/视频打不开。
        import re as _re

        if not _re.fullmatch(r"[a-z0-9]{8,40}\.\w{1,8}", token):
            return web.json_response({"code": 404}, status=404)
        file_path = os.path.join(self.dispatcher.resource_dir, token)
        if not os.path.isfile(file_path):
            # 回退到 ClassIntra 的 botmedia 目录：插件直接写盘的媒体（如 ci_downloader 的
            # dv*/mu* 文件、生图产物）不经过本插件签发流程，只落在 CI 资源目录里。
            # 没有这道回退时，8 班侧经隧道拉取这类资源会 404，表现为聊天里图片/视频打不开。
            alt = None
            for cand in _classintra_media_dirs():
                p = os.path.join(cand, token)
                if os.path.isfile(p):
                    alt = p
                    break
            if alt is None:
                return web.json_response({"code": 404}, status=404)
            file_path = alt
        return web.FileResponse(
            file_path,
            headers={"Cache-Control": "public, max-age=3600"},
        )

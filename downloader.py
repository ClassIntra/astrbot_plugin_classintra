# ClassIntra 视频下载（原 astrbot_plugin_ci_downloader，2026-09-28 并入主插件）
#
# 公共聊天室全员可用，私聊仅限授权人（owner_user_ids）。
# 下载走 yt-dlp（AstrBot venv 内），产物落 ClassIntra botmedia 目录，
# 由 CI 静态挂载为 /resources/cloud/botmedia/remote/<file>，聊天里发该路径即可内联播放。
#
# 本模块**不含任何 @filter.* 装饰器**：AstrBot 按 handler 的 `__module__` 精确匹配插件入口
# 模块来绑定 Star 实例（`star_map[handler.handler_module_path]`），所以所有装饰器必须留在
# main.py，这里只放可被其调用的普通类。
import asyncio
import hashlib
import os
import sqlite3
import uuid

from astrbot.api import logger

from . import ci_paths
from .ci_session import is_public_channel

# 注：yt-dlp / ffmpeg 的路径**不在这里硬编码**（发布到市场不能带本机绝对路径）。
# 默认值取自配置项，留空则回落到 PATH 里的 `yt-dlp`（见 _resolve_ytdlp / _resolve_ffmpeg_dir）。
DEFAULT_YTDLP = "yt-dlp"   # 留空配置时用 PATH 查找
# 注：公共聊天室判定不在这里做了 —— 统一走 ci_session.is_public_channel
# 参与数量上限清理的视频扩展名
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi")

# 默认下载格式：优先 720p 以内 mp4，限制单文件体积（filesize 过滤 + 下载后复核）
DEFAULT_FORMAT = (
    # 优先 H.264(AVC) + AAC：校园平板 WebView（Chromium 80/89、TBS/X5 内核）没有
    # AV1/HEVC 解码器，选到这两种编码会出现「只有声音没有画面」。
    # 前两段不依赖 filesize 字段（B站等 dash 站点的流没有该字段，加了会整段失配而落到兜底段）；
    # 体积上限由下载后的 max_mb 校验兜底（超限即删除并提示）。
    "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/"
    "bv*[height<=720][vcodec^=avc1]+ba[height<=720]/"
    "bv*[height<=720][filesize<{max}M]+ba[height<=720]/"
    "b[height<=720][filesize<{max}M]/"
    "b[filesize<{max}M]/bv*+ba/b"
)


class CiVideoDownloader:
    """yt-dlp 下载器 + botmedia 数量上限清理。

    只做「下载」与「清理」两件事；权限判定与工具 schema 在 main.py。
    """

    def __init__(self, config, allowed_ids):
        cfg = config or {}
        self.max_mb = int(cfg.get("dl_max_size_mb", 200) or 200)
        self.timeout_s = int(cfg.get("dl_timeout_seconds", 180) or 180)
        # 视频数量上限：超出后按最旧优先清理（已转存到云盘的不删，见 _prune_old_videos）
        self.max_videos = int(cfg.get("dl_max_videos", 100) or 100)
        self.allowed_ids = {str(x).strip() for x in (allowed_ids or []) if str(x).strip()}
        # 外部工具路径（发布后不能写死本机路径，留空即回落 PATH）
        self.ytdlp = self._resolve_ytdlp(cfg)
        self.ffmpeg_dir = self._resolve_ffmpeg_dir(cfg)
        logger.info(
            f"[ci_downloader] 初始化完成 admins={sorted(self.allowed_ids)} max={self.max_mb}MB "
            f"timeout={self.timeout_s}s videos<={self.max_videos}"
        )

    @staticmethod
    def _resolve_ytdlp(cfg) -> str:
        """yt-dlp 可执行文件路径：配置 > 环境变量 > PATH 里的 `yt-dlp`。"""
        return (
            str(cfg.get("ytdlp_path") or "").strip()
            or os.environ.get("YTDLP_PATH", "").strip()
            or DEFAULT_YTDLP
        )

    @staticmethod
    def _resolve_ffmpeg_dir(cfg) -> str:
        """ffmpeg 所在目录（会前置到 PATH）。留空表示不前置，用系统 PATH 里的 ffmpeg。"""
        return (
            str(cfg.get("ffmpeg_dir") or "").strip()
            or os.environ.get("FFMPEG_DIR", "").strip()
        )

    def _unconfigured(self) -> str:
        """botmedia 目录没配时的统一提示（不抛异常，也不往错地方写盘）。"""
        return (
            "下载未启用：还没配置 ClassIntra 的 botmedia 目录。"
            "请在插件配置里填写「ClassIntra 仓库根」或「botmedia 目录」后重试。"
        )

    async def startup(self) -> None:
        """插件加载后执行一次视频清理，把历史积累收敛到数量上限内。"""
        try:
            removed = await asyncio.to_thread(self._prune_old_videos)
            if removed:
                logger.info(f"[ci_downloader] 启动清理完成，删除 {removed} 个最旧视频")
        except Exception as e:
            logger.warning(f"[ci_downloader] 启动清理失败: {e}")

    def allowed(self, event) -> bool:
        # 公共聊天室全员可用；判定统一走 ci_session.is_public_channel（唯一实现）
        if is_public_channel(event):
            return True
        sid = str(event.get_sender_id() or "").strip()
        return sid in self.allowed_ids

    async def download(self, url: str) -> str:
        """下载一个视频并返回站内链接（失败时返回可直接转述的错误说明）。"""
        import re

        url = str(url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return "参数错误：url 必须是完整的 http(s) 视频页面链接。"

        resource_dir = ci_paths.BOTMEDIA_DIR
        if not resource_dir:
            return self._unconfigured()
        token = "dv" + uuid.uuid4().hex[:12]
        outtmpl = os.path.join(resource_dir, token + ".%(ext)s")
        fmt = DEFAULT_FORMAT.replace("{max}", str(self.max_mb))
        cmd = [
            self.ytdlp,
            "-f", fmt,
            # 编码排序偏好（在 -f 候选集内排序）：H.264 → AAC → 再按分辨率/码率。
            # 与 -f 的 vcodec 过滤双保险，确保老旧 WebView 能解码画面。
            "-S", "vcodec:h264,acodec:aac,res,br",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "15",
            "--retries", "2",
            "--concurrent-fragments", "4",
            "-o", outtmpl,
            url,
        ]

        env = os.environ.copy()
        if self.ffmpeg_dir:
            env["PATH"] = self.ffmpeg_dir + os.pathsep + env.get("PATH", "")

        logger.info(f"[ci_downloader] 开始下载: {url[:100]}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except Exception as e:
            logger.error(f"[ci_downloader] 启动 yt-dlp 失败: {e}")
            return f"下载失败：无法启动下载器（{e}）"

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            logger.warning(f"[ci_downloader] 硬超时({self.timeout_s}s): {url[:80]}")
            return f"下载失败：耗时超过 {self.timeout_s} 秒，已放弃。可以换个清晰度更低或更短的视频试试。"

        tail = (out or b"")[-500:].decode("utf-8", "ignore").replace("\r", " ").replace("\n", " ")
        if proc.returncode != 0:
            logger.warning(f"[ci_downloader] yt-dlp 失败({proc.returncode}): {tail[:200]}")
            return f"下载失败（下载器退出码 {proc.returncode}）：{tail[-300:]}"

        try:
            files = [f for f in os.listdir(resource_dir) if f.startswith(token)]
        except FileNotFoundError:
            return "下载失败：站内媒体目录不存在。"
        if not files:
            return f"下载失败：没有生成文件（视频可能不存在、需要登录或有地区限制）。{tail[-200:]}"

        fname = max(files, key=lambda f: os.path.getsize(os.path.join(resource_dir, f)))
        size_mb = os.path.getsize(os.path.join(resource_dir, fname)) / 1048576.0
        if size_mb > self.max_mb:
            try:
                os.remove(os.path.join(resource_dir, fname))
            except OSError:
                pass
            return f"下载失败：文件 {size_mb:.0f}MB 超过 {self.max_mb}MB 上限，已删除。"

        logger.info(f"[ci_downloader] 下载成功: {fname} ({size_mb:.1f}MB)")
        # 后台清理超限的旧视频（不阻塞本次回复；已转存云盘的文件会被跳过）
        try:
            asyncio.create_task(asyncio.to_thread(self._prune_old_videos))
        except Exception as e:
            logger.warning(f"[ci_downloader] 清理任务启动失败: {e}")
        return (
            f"下载完成：{ci_paths.BOTMEDIA_URL_BASE}{fname}（{size_mb:.1f}MB）。"
            f"请把这个站内链接原样发给用户，客户端会渲染成可点击播放的视频。"
            f"绝对不要把原始外部链接（B站等）发给用户，学生设备打不开外链。"
        )

    # ===== 视频数量上限清理 =====

    def _is_cloud_referenced(self, file_path: str) -> bool:
        """判断文件是否已被转存到 ClassIntra 云盘（含云盘副本登记）。

        云盘转存机制：转存时对源文件计算 sha256 并复制副本 + 登记 cloud_files，
        因此 botmedia 原文件删掉后云盘副本仍在；但为了保护聊天记录里已被收藏的
        视频链接，这里对「被转存过」的原文件跳过清理。
        任何异常（DB 不可读、文件读不了）一律返回 True —— 保护优先，宁可不删。
        """
        try:
            digest = hashlib.sha256()
            with open(file_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            hexdigest = digest.hexdigest()
        except OSError as e:
            logger.warning(f"[ci_downloader] 清理前哈希计算失败，跳过保护判定: {os.path.basename(file_path)} {e}")
            return True
        if not ci_paths.CI_DB:
            # 没配 CI 库 = 无法判断是否被云盘引用 → 保护优先，不删
            return True
        try:
            con = sqlite3.connect(f"file:{ci_paths.CI_DB}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute(
                    "SELECT 1 FROM cloud_files WHERE hash = ? AND (deleted IS NULL OR deleted = 0) LIMIT 1",
                    (hexdigest,),
                ).fetchone()
            finally:
                con.close()
            return row is not None
        except Exception as e:
            logger.warning(f"[ci_downloader] 云盘引用查询失败，保守跳过删除: {e}")
            return True

    def _prune_old_videos(self) -> int:
        """维持 botmedia 视频数量不超过 max_videos：超出时按最旧优先逐个删除。

        - 只处理视频扩展名（VIDEO_EXTS），音频/图片不在范围内
        - 已转存到云盘的文件跳过（不删、不占配额，宁可总量略超上限）
        - 删除失败/判定异常一律跳过，不中断
        返回实际删除数量。
        """
        resource_dir = ci_paths.BOTMEDIA_DIR
        if not resource_dir:
            return 0          # 未配置 botmedia，无需清理
        try:
            if not os.path.isdir(resource_dir):
                return 0
            videos = []
            for name in os.listdir(resource_dir):
                if not name.lower().endswith(VIDEO_EXTS):
                    continue
                full = os.path.join(resource_dir, name)
                try:
                    videos.append((os.path.getmtime(full), full))
                except OSError:
                    continue
            videos.sort(key=lambda item: item[0])  # 最旧在前
            excess = len(videos) - self.max_videos
            if excess <= 0:
                return 0
            removed = 0
            protected = 0
            for _, path_item in videos:
                if removed >= excess:
                    break
                if self._is_cloud_referenced(path_item):
                    protected += 1
                    logger.info(f"[ci_downloader] 跳过清理（已转存云盘）: {os.path.basename(path_item)}")
                    continue
                try:
                    size_mb = os.path.getsize(path_item) / 1048576.0
                    os.remove(path_item)
                    removed += 1
                    logger.info(f"[ci_downloader] 已清理最旧视频: {os.path.basename(path_item)} ({size_mb:.1f}MB)")
                except OSError as e:
                    logger.warning(f"[ci_downloader] 清理失败（跳过）: {os.path.basename(path_item)} {e}")
            if removed or protected:
                logger.info(
                    f"[ci_downloader] 视频清理完成: 删除 {removed} 个，云盘保护跳过 {protected} 个，"
                    f"当前视频 {len(videos) - removed} 个（上限 {self.max_videos}）"
                )
            return removed
        except Exception as e:
            logger.error(f"[ci_downloader] 视频清理异常: {e}")
            return 0

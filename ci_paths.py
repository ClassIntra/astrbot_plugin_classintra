# ClassIntra 侧路径常量（合并后的唯一来源）
#
# 为什么单独一个模块：合并前这段知识散在两处 ——
#   * 本插件 api_server.py 的 _CLASSINTRA_MEDIA_DIRS（可用 env 覆盖）
#   * 原 ci_downloader 的 RESOURCE_DIR / CI_DB（**硬编码，不可覆盖**）
# 两处写的是同一个目录，改一处忘另一处就会出现「下载落盘了但资源代理找不到」。
#
# 解析优先级：**插件配置 > 环境变量 > 空（= 未配置，相关功能优雅停用）**
# 插件要发布到市场，不能带本机绝对路径，所以默认值一律为空；
# 未配置时 `download_video` 与资源代理返回一句「未配置」的提示，而不是抛异常或写错盘。
#
# 环境变量（不想在 WebUI 里填时用）：
#   CLASSINTRA_ROOT        CI 仓库根
#   CLASSINTRA_MEDIA_DIR   botmedia 目录（优先级高于 CLASSINTRA_ROOT）
#   CLASSINTRA_DB          CI 的 SQLite 库
#   CLASSINTRA_MEDIA_URL_BASE  覆盖 botmedia 的静态挂载前缀
import os

# CI 把 botmedia 目录静态挂载成的前缀（部署改了挂载点时可用配置覆盖）
_BOTMEDIA_URL_BASE_DEFAULT = "/resources/cloud/botmedia/remote/"

CI_ROOT = ""
BOTMEDIA_DIR = ""
CI_DB = ""
BOTMEDIA_URL_BASE = _BOTMEDIA_URL_BASE_DEFAULT

# 配置键 → 环境变量名
_KEY_ENV = {
    "classintra_root": "CLASSINTRA_ROOT",
    "classintra_media_dir": "CLASSINTRA_MEDIA_DIR",
    "classintra_db": "CLASSINTRA_DB",
}


def _pick(config: dict, key: str) -> str:
    """配置 > 环境变量 > 空。"""
    v = str((config or {}).get(key) or "").strip()
    if v:
        return v
    return str(os.environ.get(_KEY_ENV.get(key, ""), "") or "").strip()


def configure(config: dict) -> None:
    """用插件配置重算路径常量。由插件 __init__ 调用（配置热更新后也会再调一次）。"""
    global CI_ROOT, BOTMEDIA_DIR, CI_DB, BOTMEDIA_URL_BASE

    CI_ROOT = _pick(config, "classintra_root")
    BOTMEDIA_DIR = _pick(config, "classintra_media_dir")
    if not BOTMEDIA_DIR and CI_ROOT:
        BOTMEDIA_DIR = os.path.join(CI_ROOT, "Resources", "cloud", "botmedia", "remote")
    CI_DB = _pick(config, "classintra_db")
    if not CI_DB and CI_ROOT:
        CI_DB = os.path.join(CI_ROOT, "server", "database", "classintra.db")

    BOTMEDIA_URL_BASE = (
        str((config or {}).get("botmedia_url_base") or "").strip()
        or os.environ.get("CLASSINTRA_MEDIA_URL_BASE", "").strip()
        or _BOTMEDIA_URL_BASE_DEFAULT
    )


def media_dirs() -> list:
    """资源代理可搜索的目录；未配置时返回空列表（而不是把不存在的路径塞进去）。"""
    return [BOTMEDIA_DIR] if BOTMEDIA_DIR else []


def is_configured() -> bool:
    """botmedia 目录是否已配置 —— 决定视频下载 / 资源代理是否可用。"""
    return bool(BOTMEDIA_DIR)


# 进程启动时先按环境变量算一次，保证 configure() 之前被引用也不会拿到脏值
configure({})

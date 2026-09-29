# -*- coding: utf-8 -*-
"""会话/频道语义的唯一权威实现。

背景（2026-09-28 插件整合时发现的问题）：
「这条消息是不是来自公共聊天室」这个判定，仓库里曾经散落三份各自手写的副本：

  1. astrbot_plugin_classintra/main.py        （撤回工具选 channel）
  2. astrbot_plugin_classintra/downloader.py  （视频下载权限）
  3. astrbot_plugin_search_video/main.py      （视频下载权限，第三方 AGPL 插件）

其中 3 号是 AGPL-3.0 的第三方插件且上游仍在更新，**不能也不应该把它的代码并进本插件**，
所以那边保留**自包含副本**（升级上游后要重打补丁）。为避免三份副本各自漂移，
约定：

  * 本文件是主插件内**唯一**的权威实现，main.py / downloader.py 一律调它，不许再内联。
  * 第三方副本的语义由 `_check_thirdparty_patches.py` 用同一组样例交叉校验，漂移即报警。

判定口径（两个来源任一命中即可）：
  * `event.get_group_id()` == "public"
      —— OneBot 正常路径：群号被显式设为 public。
  * `event.session_id` ∈ {"public", "group_public"}
      —— HTTP 注入路径（`POST /api/chat`）：旧版 dispatch 不带群语义，
         group_id 为空，只能靠会话标识还原，否则公共聊天室会被误判成私聊，
         表现为「群里下的视频下载命令被当成私聊拒绝」。
"""

PUBLIC_GROUP = "public"
PUBLIC_SESSION_IDS = ("public", "group_public")


def is_public_channel(event) -> bool:
    """这条消息是否来自公共聊天室（群号或注入会话标识任一命中）。"""
    if event is None:
        return False
    try:
        gid = str(event.get_group_id() or "").strip()
    except Exception:
        gid = ""
    if gid == PUBLIC_GROUP:
        return True
    try:
        sid = str(getattr(event, "session_id", "") or "").strip()
    except Exception:
        sid = ""
    return sid in PUBLIC_SESSION_IDS


def channel_of(event) -> tuple:
    """返回 (channel, target)：公共聊天室 → ("public", "")，否则 → ("private", <sender_id>)。"""
    if is_public_channel(event):
        return "public", ""
    try:
        target = str(event.get_sender_id() or "").strip()
    except Exception:
        target = ""
    return "private", target

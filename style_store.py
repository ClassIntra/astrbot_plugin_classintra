# -*- coding: utf-8 -*-
"""林晞「学别人讲话」—— 语料 + 风格卡 + 群近况 的存储层（不依赖 AstrBot，可独立测试）。

三条线
------
1. `chat_log`：滚动语料。每来一条**别人**的消息就记一条，只保留最近 N 条
   （默认 1200），从此处取样去学「大家怎么说话」。不存林晞自己的话——
   学自己会把她现有的毛病越学越深（对照组：astrbot_plugin_self_learning 的
   expression_patterns 就是「用户问 → 林晞答」，学出来全是跑题句）。
2. `style_cards`：按 scope（group / private）存一张**风格卡**——语气词、口头禅、
   群黑话、说话习惯。纯风格、不含具体人事，因此哪都能注入。
3. `group_facts`：群近况（事件/计划/约定）。素材只取**公共聊天室**（公开信息），
   群聊和私聊都可注入；私聊内容只进 style_store 之外的「人物档案」，绝不进这里。

安全
----
风格卡与群近况都会被回注进系统提示词，等于又开了一条「长期指令」通道，
所以入库前一律走 profile_store.sanitize_facts 过滤指令式/劫持式内容。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL DEFAULT 'group',
    chat_id      TEXT DEFAULT '',
    speaker_id   TEXT DEFAULT '',
    speaker_name TEXT DEFAULT '',
    text         TEXT DEFAULT '',
    ts           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_log_scope ON chat_log(scope, id);

CREATE TABLE IF NOT EXISTS style_cards (
    scope       TEXT PRIMARY KEY,
    card_json   TEXT DEFAULT '{}',
    msg_count   INTEGER DEFAULT 0,
    last_msg_id INTEGER DEFAULT 0,
    updated_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS group_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT UNIQUE,
    scope      TEXT DEFAULT 'group',
    ts         INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT ''
);
"""

MAX_MSG_LEN = 200


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean_msg(t: str) -> str:
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t[:MAX_MSG_LEN]


class StyleStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Any:
        """短连接，退出必 close（同 profile_store：`with sqlite3.connect` 不关连接）。"""
        c = sqlite3.connect(self.db_path, timeout=10)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------ 语料
    def log(
        self,
        scope: str,
        chat_id: str,
        speaker_id: str,
        speaker_name: str,
        text: str,
        ts: int | None = None,
        cap: int = 1200,
    ) -> None:
        body = _clean_msg(text)
        if not body:
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO chat_log (scope,chat_id,speaker_id,speaker_name,text,ts) "
                "VALUES (?,?,?,?,?,?)",
                (
                    scope or "group",
                    str(chat_id or ""),
                    str(speaker_id or ""),
                    str(speaker_name or "")[:40],
                    body,
                    int(ts or time.time()),
                ),
            )
            # 只留最近 cap 条
            row = c.execute("SELECT MAX(id) FROM chat_log").fetchone()
            top = int(row[0] or 0)
            if top > cap:
                c.execute("DELETE FROM chat_log WHERE id <= ?", (top - cap,))

    def samples(self, scope: str, limit: int) -> list[dict]:
        """取最近 limit 条，按时间正序返回。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT speaker_name,speaker_id,text FROM chat_log "
                "WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, max(1, int(limit))),
            ).fetchall()
        out = [{"name": r[0], "id": r[1], "text": r[2]} for r in reversed(rows)]
        return out

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
            g = c.execute("SELECT COUNT(*) FROM chat_log WHERE scope='group'").fetchone()[0]
            p = total - g
            top = c.execute("SELECT MAX(id) FROM chat_log").fetchone()[0] or 0
            mark = c.execute(
                "SELECT last_msg_id FROM style_cards WHERE scope='style'"
            ).fetchone()
        return {
            "total": total,
            "group": g,
            "private": p,
            "max_id": int(top),
            "learned_at_id": int(mark[0]) if mark else 0,
        }

    # ------------------------------------------------------------ 风格卡
    def load_card(self, scope: str = "style") -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT card_json,msg_count,last_msg_id,updated_at FROM style_cards WHERE scope=?",
                (scope,),
            ).fetchone()
        if not row:
            return {}
        try:
            card = json.loads(row[0] or "{}")
        except Exception:
            card = {}
        card["_msg_count"] = int(row[1] or 0)
        card["_last_msg_id"] = int(row[2] or 0)
        card["_updated_at"] = row[3] or ""
        return card

    def save_card(self, card: dict, msg_count: int, last_msg_id: int, scope: str = "style") -> None:
        clean = {k: v for k, v in card.items() if not k.startswith("_")}
        with self._conn() as c:
            c.execute(
                "INSERT INTO style_cards (scope,card_json,msg_count,last_msg_id,updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET card_json=excluded.card_json, "
                "msg_count=excluded.msg_count, last_msg_id=excluded.last_msg_id, "
                "updated_at=excluded.updated_at",
                (
                    scope,
                    json.dumps(clean, ensure_ascii=False),
                    int(msg_count),
                    int(last_msg_id),
                    _now(),
                ),
            )

    def clear_card(self, scope: str = "style") -> None:
        with self._conn() as c:
            c.execute("DELETE FROM style_cards WHERE scope=?", (scope,))

    # ------------------------------------------------------------ 群近况
    def merge_facts(self, facts: Any, max_facts: int = 40, scope: str = "group") -> list[str]:
        """并入群近况；重复的刷新时间戳，超出上限丢最旧的。"""
        if not isinstance(facts, (list, tuple)):
            return self.facts(scope)
        now = _now()
        with self._conn() as c:
            for f in facts:
                t = _clean_msg(f)
                if not t:
                    continue
                c.execute(
                    "INSERT INTO group_facts (text,scope,ts,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(text) DO UPDATE SET ts=excluded.ts, updated_at=excluded.updated_at",
                    (t[:80], scope, int(time.time()), now),
                )
            row = c.execute(
                "SELECT COUNT(*) FROM group_facts WHERE scope=?", (scope,)
            ).fetchone()
            n = int(row[0] or 0)
            if n > max_facts:
                c.execute(
                    "DELETE FROM group_facts WHERE scope=? AND id IN ("
                    "  SELECT id FROM group_facts WHERE scope=? ORDER BY ts ASC, id ASC LIMIT ?)",
                    (scope, scope, n - max_facts),
                )
        return self.facts(scope)

    def facts(self, scope: str = "group") -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT text FROM group_facts WHERE scope=? ORDER BY ts DESC", (scope,)
            ).fetchall()
        return [r[0] for r in rows]

    def clear_facts(self, scope: str = "group") -> None:
        with self._conn() as c:
            c.execute("DELETE FROM group_facts WHERE scope=?", (scope,))

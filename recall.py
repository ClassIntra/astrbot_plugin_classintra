# -*- coding: utf-8 -*-
"""林晞记忆 —— 向量召回层（可选、可降级、不依赖 AstrBot，可独立测试）。

为什么需要它
------------
档案最初是「每人最多 12 条事实、全量注入」（≈360 字），刚好卡在 400 字注入预算里。
要想让林晞「真的记得住」，就得把上限放大到几十条 —— 但全量注入必然爆预算，而且
**大部分事实与当前这句话无关**，塞进去只会稀释她对当下问题的注意力。

做法（三层）
------------
1. **快路径**：事实条数 <= top_k → 原样返回，不碰任何模型（零延迟、零外部依赖）。
   这也是「档案还小」时的常态，行为与改造前完全一致。
2. **慢路径**：把「本轮用户问题」做 embedding → 与该用户全部事实向量算余弦 →
   取前 candidate_n 条 → 交 rerank 精排 → 取 top_k。
3. **降级**：任何一环失败（网络、额度、超时、provider 没加载出来）→ 退回
   「最近 top_k 条」，并短路缓存失败状态 failure_ttl 秒，避免每轮都去撞墙。
   原则：记忆层永远不能让对话卡住。

向量缓存
--------
事实文本 sha1 作 key，float32 打包成 BLOB 存在 profiles.db 的 `fact_vec` 表。
命中即复用（每条事实一生只算一次向量）；维度不符（换了模型）或缺失才重算。
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import struct
import time
from contextlib import contextmanager
from typing import Any, Callable, Sequence

VEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_vec (
    key        TEXT PRIMARY KEY,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    updated_at TEXT DEFAULT ''
);
"""


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """纯 Python 余弦。40 条 × 1024 维 ≈ 4 万次乘加，可忽略。"""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


def _result_index(r: Any) -> int | None:
    """兼容 RerankResult 数据类与 dict 两种返回形态。"""
    idx = getattr(r, "index", None)
    if idx is None and isinstance(r, dict):
        idx = r.get("index")
    return idx if isinstance(idx, int) else None


class FactRecall:
    """把一个用户的事实列表按「与本轮问题的相关度」重排，取 top_k。"""

    def __init__(
        self,
        db_path: str,
        *,
        top_k: int = 8,
        candidate_n: int = 20,
        timeout: float = 4.0,
        failure_ttl: float = 120.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = db_path
        self.top_k = max(1, int(top_k))
        self.candidate_n = max(self.top_k, int(candidate_n))
        self.timeout = float(timeout)
        self.failure_ttl = float(failure_ttl)
        self._log = log or (lambda _m: None)
        self._fail_until = 0.0
        self._init()

    # ------------------------------------------------------------ sqlite
    @contextmanager
    def _conn(self) -> Any:
        c = sqlite3.connect(self.db_path, timeout=10)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(VEC_SCHEMA)

    def _load_vecs(self, keys: list[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        if not keys:
            return out
        with self._conn() as c:
            for i in range(0, len(keys), 200):  # 规避 SQLite 变量数上限
                chunk = keys[i : i + 200]
                ph = ",".join("?" * len(chunk))
                for k, blob in c.execute(
                    f"SELECT key, vec FROM fact_vec WHERE key IN ({ph})", chunk
                ):
                    try:
                        out[k] = _unpack(blob)
                    except Exception:  # noqa: BLE001
                        continue
        return out

    def _save_vecs(self, items: dict[str, list[float]]) -> None:
        if not items:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            for k, v in items.items():
                if not v:
                    continue
                c.execute(
                    "INSERT OR REPLACE INTO fact_vec (key, dim, vec, updated_at) "
                    "VALUES (?,?,?,?)",
                    (k, len(v), _pack(v), now),
                )

    def cache_stats(self) -> int:
        try:
            with self._conn() as c:
                return int(c.execute("SELECT COUNT(*) FROM fact_vec").fetchone()[0])
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------ 主入口
    async def select(
        self,
        facts: list[str],
        query: str,
        ep: Any = None,
        rp: Any = None,
    ) -> list[str]:
        """返回重排后的 top_k 条事实；任何异常都降级为「最近 top_k 条」。"""
        facts = [f for f in (facts or []) if f]
        if len(facts) <= self.top_k:
            return facts
        fallback = facts[-self.top_k :]  # 降级=最近 top_k（与旧行为一致）

        now = time.time()
        if now < self._fail_until:
            return fallback
        if ep is None or not (query or "").strip():
            return fallback

        try:
            ranked = await asyncio.wait_for(
                self._rank(facts, query, ep, rp), timeout=self.timeout
            )
        except Exception as e:  # noqa: BLE001
            self._fail_until = now + self.failure_ttl
            self._log(
                f"召回失败，降级为最近 {self.top_k} 条"
                f"（{self.failure_ttl:.0f}s 内不再重试）: {type(e).__name__}: {e}"
            )
            return fallback

        if not ranked:
            return fallback
        self._log(
            f"召回 {len(ranked)}/{len(facts)} 条"
            + ("（rerank 精排）" if rp is not None else "（仅余弦）")
        )
        return ranked

    # ------------------------------------------------------------ 内部
    async def _rank(
        self, facts: list[str], query: str, ep: Any, rp: Any
    ) -> list[str]:
        keys = [_key(t) for t in facts]
        cache = self._load_vecs(keys)

        missing = [t for t, k in zip(facts, keys) if not cache.get(k)]
        if missing:
            vecs = await ep.get_embeddings(missing)
            fresh: dict[str, list[float]] = {}
            for t, v in zip(missing, vecs or []):
                if v:
                    fresh[_key(t)] = [float(x) for x in v]
            self._save_vecs(fresh)
            cache.update(fresh)

        qv = await ep.get_embedding(query)
        if not qv:
            return []

        scored: list[tuple[float, str]] = []
        for t, k in zip(facts, keys):
            v = cache.get(k)
            if not v:
                continue
            scored.append((_cosine(qv, v), t))
        if not scored:
            return []
        scored.sort(key=lambda x: x[0], reverse=True)
        cands = [t for _, t in scored[: self.candidate_n]]

        if rp is not None and len(cands) > self.top_k:
            try:
                results = await rp.rerank(query, cands, top_n=self.top_k)
                picked: list[str] = []
                for r in results or []:
                    idx = _result_index(r)
                    if idx is not None and 0 <= idx < len(cands):
                        picked.append(cands[idx])
                seen: set[str] = set()
                uniq = [t for t in picked if not (t in seen or seen.add(t))]
                if uniq:
                    return uniq[: self.top_k]
            except Exception as e:  # noqa: BLE001
                # rerank 挂掉不算召回失败：余弦排序已经够用
                self._log(f"rerank 不可用，退回余弦排序: {e}")
        return cands[: self.top_k]

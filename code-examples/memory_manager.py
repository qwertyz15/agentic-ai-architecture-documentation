"""
memory_manager.py
=================

Reference implementation of the Memory Manager (Layer 2) and the Context &
Memory stores it fronts (Layer 4):

  * Short-term memory  -> Redis     (conversation window, task scratchpad)
  * Long-term memory   -> Postgres  (profile facts, episodes, audit)
  * Vector store       -> pgvector  (semantic recall; swap for Pinecone/Milvus)
  * Knowledge graph    -> Neo4j     (entities and relationships)

Design rules applied:
  1. The agent never talks to a store directly; it asks the Memory Manager for a
     *context bundle* and hands back observations. Storage tech can change
     without touching agents.
  2. Every stored item carries tenant_id + user_id and is filtered on read.
  3. Short-term memory is bounded. When the window overflows, older turns are
     summarised into long-term memory instead of being silently dropped.
  4. Retrieval is budgeted in tokens, not items.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg
import redis.asyncio as redis


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class Summarizer(Protocol):
    async def summarize(self, turns: list[dict[str, str]]) -> str: ...


class GraphStore(Protocol):
    async def neighbours(self, entity: str, depth: int = 1) -> list[dict[str, Any]]: ...
    async def upsert_triples(self, triples: list[tuple[str, str, str]]) -> None: ...


@dataclass
class MemoryConfig:
    window_turns: int = 20            # max turns kept verbatim in Redis
    session_ttl_s: int = 60 * 60 * 24
    recall_top_k: int = 8
    recall_min_score: float = 0.72
    context_token_budget: int = 6_000
    embedding_dim: int = 1536


class MemoryManager:
    def __init__(self, r: redis.Redis, pg: asyncpg.Pool, embedder: Embedder,
                 summarizer: Summarizer, graph: GraphStore | None = None,
                 cfg: MemoryConfig | None = None):
        self.r, self.pg, self.embedder, self.summarizer, self.graph = r, pg, embedder, summarizer, graph
        self.cfg = cfg or MemoryConfig()

    # ------------------------------------------------------------------ READ #
    async def load_context(self, session_id: str, user_id: str, query: str,
                           tenant_id: str = "default") -> dict[str, Any]:
        """Assemble the context bundle the planner and sub-agents receive."""
        recent = await self._recent_turns(session_id)
        profile = await self._profile(tenant_id, user_id)
        memories = await self._semantic_recall(tenant_id, user_id, query)
        entities = await self._graph_context(query) if self.graph else []

        bundle = {"recent_turns": recent, "profile": profile,
                  "relevant_memories": memories, "entities": entities}
        return self._fit_to_budget(bundle)

    async def _recent_turns(self, session_id: str) -> list[dict[str, str]]:
        raw = await self.r.lrange(f"session:{session_id}:turns", -self.cfg.window_turns, -1)
        turns = [json.loads(x) for x in raw]
        summary = await self.r.get(f"session:{session_id}:summary")
        if summary:
            turns.insert(0, {"role": "system", "content": f"Earlier conversation summary: {summary}"})
        return turns

    async def _profile(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        rows = await self.pg.fetch(
            "SELECT kind, content FROM memories WHERE tenant_id=$1 AND user_id=$2 "
            "AND kind IN ('preference','fact') AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY updated_at DESC LIMIT 50", tenant_id, user_id)
        return {"preferences": [r["content"] for r in rows if r["kind"] == "preference"],
                "facts": [r["content"] for r in rows if r["kind"] == "fact"]}

    async def _semantic_recall(self, tenant_id: str, user_id: str, query: str) -> list[str]:
        [qvec] = await self.embedder.embed([query])
        rows = await self.pg.fetch(
            """
            SELECT content, 1 - (embedding <=> $1::vector) AS score
            FROM memories
            WHERE tenant_id=$2 AND user_id=$3 AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $4
            """, qvec, tenant_id, user_id, self.cfg.recall_top_k)
        return [r["content"] for r in rows if r["score"] >= self.cfg.recall_min_score]

    async def _graph_context(self, query: str) -> list[dict[str, Any]]:
        # Cheap entity extraction: rely on the KG's full-text index for candidate entities.
        # In production, use an NER model or the LLM to extract entities first.
        return await self.graph.neighbours(query, depth=1)

    # ----------------------------------------------------------------- WRITE #
    async def append_turn(self, session_id: str, role: str, content: str) -> None:
        key = f"session:{session_id}:turns"
        pipe = self.r.pipeline()
        pipe.rpush(key, json.dumps({"role": role, "content": content, "ts": time.time()}))
        pipe.expire(key, self.cfg.session_ttl_s)
        pipe.llen(key)
        *_, length = await pipe.execute()
        if length > self.cfg.window_turns * 2:
            await self._compress_window(session_id)

    async def _compress_window(self, session_id: str) -> None:
        """Summarise the oldest half of the window into a rolling summary (memory compression)."""
        key = f"session:{session_id}:turns"
        old = [json.loads(x) for x in await self.r.lrange(key, 0, self.cfg.window_turns - 1)]
        prior = await self.r.get(f"session:{session_id}:summary")
        if prior:
            old.insert(0, {"role": "system", "content": prior})
        summary = await self.summarizer.summarize(old)
        pipe = self.r.pipeline()
        pipe.set(f"session:{session_id}:summary", summary, ex=self.cfg.session_ttl_s)
        pipe.ltrim(key, self.cfg.window_turns, -1)
        await pipe.execute()

    async def remember(self, user_id: str, fact: str, kind: str = "fact",
                       tenant_id: str = "default", ttl_days: int | None = None) -> None:
        """Write a durable memory with its embedding (long-term + vector store)."""
        [vec] = await self.embedder.embed([fact])
        await self.pg.execute(
            """
            INSERT INTO memories (tenant_id, user_id, kind, content, embedding, expires_at)
            VALUES ($1, $2, $3, $4, $5::vector,
                    CASE WHEN $6::int IS NULL THEN NULL ELSE now() + ($6 || ' days')::interval END)
            """, tenant_id, user_id, kind, fact, vec, ttl_days)

    async def record_entities(self, triples: list[tuple[str, str, str]]) -> None:
        if self.graph:
            await self.graph.upsert_triples(triples)

    async def forget_user(self, tenant_id: str, user_id: str) -> None:
        """GDPR / right-to-erasure: delete all durable memory for a user."""
        await self.pg.execute("DELETE FROM memories WHERE tenant_id=$1 AND user_id=$2", tenant_id, user_id)

    # --------------------------------------------------------------- HELPERS #
    def _fit_to_budget(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """Trim lowest-priority sections until the bundle fits the token budget."""
        def tokens(obj: Any) -> int:
            return len(json.dumps(obj, default=str)) // 4   # rough heuristic; use tiktoken in prod

        priority = ["entities", "relevant_memories", "profile", "recent_turns"]  # trimmed first -> last
        for section in priority:
            while tokens(bundle) > self.cfg.context_token_budget and bundle.get(section):
                if isinstance(bundle[section], list):
                    bundle[section] = bundle[section][:-1] if section != "recent_turns" else bundle[section][1:]
                else:
                    bundle[section] = {}
        return bundle


# --------------------------------------------------------------------------- #
# Schema (PostgreSQL + pgvector)
# --------------------------------------------------------------------------- #
SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('preference','fact','episode','summary')),
    content     TEXT NOT NULL,
    embedding   vector(1536),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS memories_user_idx ON memories (tenant_id, user_id, kind);
CREATE INDEX IF NOT EXISTS memories_embedding_idx
    ON memories USING hnsw (embedding vector_cosine_ops);
"""

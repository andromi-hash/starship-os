import os
import json
import asyncio
import hashlib
import logging
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("agnetic-memory")

try:
    import lancedb
    import pyarrow as pa
except ImportError:
    lancedb = None
    pa = None

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))


class MemoryType(Enum):
    WORKING = "working"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    RETRIEVAL = "retrieval"
    PARAMETRIC = "parametric"
    PROSPECTIVE = "prospective"
    TEMPORAL = "temporal"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    PREFERENCE = "preference"


MEMORY_DESCRIPTIONS = {
    MemoryType.WORKING: "In-context working memory — current session context window",
    MemoryType.SEMANTIC: "Persistent facts, preferences, and domain knowledge about users and projects",
    MemoryType.EPISODIC: "Past events, conversations, task runs, outcomes — what worked and what failed",
    MemoryType.PROCEDURAL: "How-to knowledge — skills, tool patterns, workflows, behavioral rules",
    MemoryType.RETRIEVAL: "External documents and history chunks pulled via vector similarity search",
    MemoryType.PARAMETRIC: "Knowledge baked into LLM weights during training — language, reasoning, world knowledge",
    MemoryType.PROSPECTIVE: "Future intentions, scheduled goals, pending plans — remembered commitments",
    MemoryType.TEMPORAL: "State transitions for compliance — before/after entity state tracking",
    MemoryType.KNOWLEDGE_GRAPH: "Entity-relation triples — structured knowledge as subject-predicate-object",
    MemoryType.PREFERENCE: "Structured user/operator preferences extracted from interaction",
}


class Memory:
    def __init__(self, id, agent, type, content, summary="", metadata=None, importance=0.5,
                 embedding=None, created_at=None, due_at=None, status=None):
        self.id = id
        self.agent = agent
        self.type = type
        self.content = content
        self.summary = summary
        self.metadata = metadata or {}
        self.importance = importance
        self.embedding = embedding
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.due_at = due_at
        self.status = status

    def to_dict(self):
        return {
            "id": self.id,
            "agent": self.agent,
            "type": self.type.value if isinstance(self.type, Enum) else str(self.type),
            "content": self.content[:500],
            "summary": self.summary,
            "metadata": self.metadata,
            "importance": self.importance,
            "created_at": self.created_at,
            "due_at": self.due_at,
            "status": self.status,
        }


class MemoryManager:
    def __init__(self, db_path=None):
        self.db_path = db_path or "/var/lib/agnetic/memory"
        self._table = None
        self._db = None
        self._init_db()

    def _init_db(self):
        if not lancedb:
            log.warning("lancedb not installed — using flat file fallback")
            return
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(self.db_path)
            table_names = self._db.table_names()
            if "memories" not in table_names:
                schema = pa.schema([
                    pa.field("id", pa.string()),
                    pa.field("agent", pa.string()),
                    pa.field("type", pa.string()),
                    pa.field("content", pa.string()),
                    pa.field("summary", pa.string()),
                    pa.field("metadata", pa.string()),
                    pa.field("importance", pa.float64()),
                    pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM)),
                    pa.field("created_at", pa.string()),
                    pa.field("due_at", pa.string()),
                    pa.field("status", pa.string()),
                ])
                self._table = self._db.create_table("memories", schema=schema)
            else:
                self._table = self._db.open_table("memories")
        except Exception as e:
            log.warning("LanceDB init failed (%s), using flat file fallback", e)
            self._db = None
            self._table = None

    async def _get_embedding(self, text: str) -> list:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{OLLAMA_URL}/api/embeddings", json={
                    "model": EMBEDDING_MODEL,
                    "prompt": text[:2048],
                })
                if resp.status_code == 200:
                    return resp.json().get("embedding", [0.0] * EMBEDDING_DIM)
        except Exception as e:
            log.warning("Embedding failed: %s", e)
        return [0.0] * EMBEDDING_DIM

    def _get_embedding_sync(self, text: str) -> list:
        import httpx
        try:
            resp = httpx.post(f"{OLLAMA_URL}/api/embeddings", json={
                "model": EMBEDDING_MODEL,
                "prompt": text[:2048],
            }, timeout=10.0)
            if resp.status_code == 200:
                return resp.json().get("embedding", [0.0] * EMBEDDING_DIM)
        except Exception as e:
            log.warning("Embedding failed (sync): %s", e)
        return [0.0] * EMBEDDING_DIM

    def _make_id(self, agent, mem_type, content):
        raw = f"{agent}-{mem_type}-{content[:100]}-{datetime.now().timestamp()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── Async (vector) interface ──────────────────────────────────────

    async def store(self, agent, mem_type, content, summary="", metadata=None,
                    importance=0.5, embedding=None, due_at=None, status=None):
        if self._table is not None:
            try:
                now = datetime.now(timezone.utc).isoformat()
                mem_id = self._make_id(agent, mem_type, content)
                if embedding is None and mem_type in (MemoryType.RETRIEVAL, MemoryType.SEMANTIC, MemoryType.EPISODIC):
                    embedding = await self._get_embedding(content[:2048])
                return self._write(mem_id, agent, mem_type, content, summary, metadata, importance, embedding, now, due_at, status)
            except Exception as e:
                log.warning("LanceDB store failed: %s", e)
        return None

    async def search(self, query, limit=10, mem_type=None, agent=None, status=None):
        if self._table is not None:
            try:
                query_vec = await self._get_embedding(query[:2048])
                return self._vector_search(query_vec, limit, mem_type, agent, status)
            except Exception as e:
                log.warning("LanceDB search failed: %s", e)
                return await self._fallback_search(query, limit, mem_type, agent)
        return []

    async def semantic_search(self, query, limit=5):
        return await self.search(query, limit=limit, mem_type=MemoryType.SEMANTIC)

    async def episodic_search(self, query, limit=5):
        return await self.search(query, limit=limit, mem_type=MemoryType.EPISODIC)

    async def prospective_search(self, query=None, limit=10, status=None):
        return await self.search(query or "", limit=limit, mem_type=MemoryType.PROSPECTIVE, status=status)

    async def get_context(self, query, agent="", max_tokens=600):
        results = await self.search(query, limit=5)
        parts = []
        for m in results:
            label = f"[{m.type.value if isinstance(m.type, Enum) else m.type}"
            if m.importance >= 0.7:
                label += " ★"
            label += "]"
            parts.append(f"{label} {m.content[:max_tokens]}")
        return "\n\n".join(parts) if parts else ""

    # ── Sync (keyword) interface (legacy compat) ──────────────────────

    def store_sync(self, agent, mem_type, content, summary="", metadata=None, importance=0.5):
        """Synchronous store — no embedding, for BacklogManager compat."""
        if self._table is not None:
            try:
                now = datetime.now(timezone.utc).isoformat()
                mem_id = self._make_id(agent, mem_type, content)
                return self._write(mem_id, agent, mem_type, content, summary, metadata, importance, None, now, None, None)
            except Exception as e:
                log.warning("LanceDB sync store failed: %s", e)
        return None

    def search_sync(self, query, limit=10, mem_type=None, agent=None):
        """Synchronous search — keyword only, no embedding."""
        if self._table is None:
            return []
        try:
            table_arrow = self._table.to_arrow()
            results = []
            ql = query.lower()
            for i in range(table_arrow.num_rows):
                row = {col: table_arrow.column(col)[i].as_py() for col in table_arrow.column_names}
                if mem_type:
                    tv = mem_type.value if isinstance(mem_type, Enum) else str(mem_type)
                    if row.get("type") != tv: continue
                if agent and row.get("agent") != agent: continue
                content = row.get("content", "") or ""
                summary = row.get("summary", "") or ""
                if ql in content.lower() or ql in summary.lower():
                    results.append(self._row_to_memory(row))
                    if len(results) >= limit: break
            return results
        except Exception as e:
            log.warning("Sync search failed: %s", e)
        return []

    # ── Shared internals ──────────────────────────────────────────────

    def _write(self, mem_id, agent, mem_type, content, summary, metadata, importance, embedding, now, due_at, status):
        type_val = mem_type.value if isinstance(mem_type, Enum) else str(mem_type)
        data = {
            "id": mem_id,
            "agent": agent,
            "type": type_val,
            "content": str(content),
            "summary": str(summary),
            "metadata": json.dumps(metadata or {}),
            "importance": float(importance),
            "embedding": embedding or [0.0] * EMBEDDING_DIM,
            "created_at": now,
            "due_at": due_at or "",
            "status": status or "",
        }
        self._table.add(pa.Table.from_pylist([data]))
        return mem_id

    def _vector_search(self, query_vec, limit, mem_type, agent, status):
        filters = []
        if mem_type:
            tv = mem_type.value if isinstance(mem_type, Enum) else str(mem_type)
            filters.append(f"type == '{tv}'")
        if agent:
            filters.append(f"agent == '{agent}'")
        if status:
            filters.append(f"status == '{status}'")
        filter_str = " AND ".join(filters) if filters else None
        search_op = self._table.search(query_vec).limit(limit)
        if filter_str:
            search_op = search_op.where(filter_str)
        return [self._row_to_memory(r) for r in search_op.to_list()]

    async def _fallback_search(self, query, limit=10, mem_type=None, agent=None):
        return self.search_sync(query, limit, mem_type, agent)

    def forget(self, mem_id):
        if self._table is not None:
            try:
                self._table.delete(f"id == '{mem_id}'")
            except Exception as e:
                log.warning("LanceDB forget failed: %s", e)
        return True

    def close(self):
        pass

    def _row_to_memory(self, row):
        type_val = str(row.get("type", "episodic"))
        try:
            mem_type = MemoryType(type_val)
        except ValueError:
            mem_type = MemoryType.EPISODIC
        meta_str = str(row.get("metadata", "{}"))
        try:
            meta = json.loads(meta_str) if meta_str else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return Memory(
            id=str(row.get("id", "")),
            agent=str(row.get("agent", "")),
            type=mem_type,
            content=str(row.get("content", "")),
            summary=str(row.get("summary", "")),
            metadata=meta,
            importance=float(row.get("importance", 0.5) or 0.5),
            created_at=str(row.get("created_at", "")),
            due_at=str(row.get("due_at", "")),
            status=str(row.get("status", "")),
        )


class ProspectiveMemoryManager:
    def __init__(self, memory_manager: MemoryManager):
        self.mem = memory_manager

    async def create_intention(self, agent: str, description: str, due_at: str = None,
                                priority: float = 0.5, goal_id: str = None) -> dict:
        mem_id = await self.mem.store(
            agent=agent,
            mem_type=MemoryType.PROSPECTIVE,
            content=description,
            summary=f"Intention: {description[:80]}",
            metadata={"goal_id": goal_id} if goal_id else {},
            importance=priority,
            due_at=due_at or "",
            status="pending",
        )
        return {"id": mem_id, "agent": agent, "description": description,
                "due_at": due_at, "priority": priority, "status": "pending"}

    async def get_pending(self, agent: str = None, limit: int = 20) -> list:
        return await self.mem.prospective_search(status="pending", limit=limit)

    async def get_overdue(self, agent: str = None) -> list:
        now = datetime.now(timezone.utc).isoformat()
        results = await self.mem.prospective_search(status="pending", limit=50)
        overdue = []
        for m in results:
            if m.due_at and m.due_at < now:
                overdue.append(m)
        return overdue

    async def complete(self, mem_id: str, outcome: str = ""):
        self.mem.forget(mem_id)
        log.info("Prospective memory completed: %s — %s", mem_id, outcome[:100])

    async def defer(self, mem_id: str, new_due_at: str):
        if self.mem._table:
            self.mem._table.update(where=f"id == '{mem_id}'", values={"due_at": new_due_at, "status": "deferred"})

    async def get_upcoming(self, horizon_hours: int = 24) -> list:
        from datetime import timedelta
        horizon = (datetime.now(timezone.utc) + timedelta(hours=horizon_hours)).isoformat()
        results = await self.mem.prospective_search(status="pending", limit=50)
        upcoming = []
        for m in results:
            if m.due_at and m.due_at <= horizon:
                upcoming.append(m)
        return upcoming


_memory_manager = MemoryManager()
_prospective_memory = ProspectiveMemoryManager(_memory_manager)


def get_memory_manager() -> MemoryManager:
    return _memory_manager


def get_prospective_memory() -> ProspectiveMemoryManager:
    return _prospective_memory

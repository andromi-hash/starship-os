"""
Starship OS — Long-Term Memory System

Persists agent knowledge across sessions. Stores decisions, user preferences,
conversation history, and learned facts with semantic search retrieval.

Architecture:
    Short-Term:  Context window (current session) — handled by agent daemons
    Ephemeral:   Recent tool outputs — handled by tool system
    Long-Term:   This system — SQLite + embeddings for semantic search
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Embedding (zero external dependencies)
# ---------------------------------------------------------------------------

def simple_embed(text: str, dim: int = 256) -> list[float]:
    """Deterministic embedding based on character n-grams and word hashes.

    Produces a unit-normalised vector of *dim* dimensions without any ML
    model.  Good enough for local semantic similarity of short-to-medium
    text passages.
    """
    vector = [0.0] * dim
    words = text.lower().split()

    # word-level hashing with positional weighting
    for i, word in enumerate(words):
        h = hashlib.md5(word.encode()).digest()
        weight = 1.0 / (i + 1)
        for j in range(min(dim, len(h))):
            vector[j] += (h[j] / 128.0) * weight

    # character 3-gram hashing for sub-word signal
    lower = text.lower()
    for start in range(len(lower) - 2):
        trigram = lower[start : start + 3]
        h = hashlib.sha1(trigram.encode()).digest()
        weight = 1.0 / (start + 1)
        for j in range(min(dim, len(h))):
            vector[j] += (h[j] / 128.0) * weight * 0.5

    # normalise
    norm = math.sqrt(sum(x * x for x in vector))
    if norm > 0:
        vector = [x / norm for x in vector]
    return vector


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class MemoryType(Enum):
    EPISODIC = "episodic"        # Past conversations and events
    SEMANTIC = "semantic"        # Facts and knowledge
    PROCEDURAL = "procedural"    # How to do things (learned skills)
    PREFERENCE = "preference"    # User preferences and settings
    DECISION = "decision"        # Past decisions and rationale
    TEMPORAL = "temporal"        # State transitions for compliance
    KNOWLEDGE_GRAPH = "knowledge_graph"  # Entity-relation triples


@dataclass
class Memory:
    id: str
    agent: str
    type: MemoryType
    content: str
    summary: str
    embedding: list[float]
    metadata: dict
    importance: float
    created_at: str
    accessed_at: str
    access_count: int
    decay: float


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_DEFAULT_DB = os.environ.get(
    "AGNETIC_MEMORY_DB", "/tmp/agnetic-data/memory.db"
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    type        TEXT NOT NULL,
    content     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    embedding   TEXT,
    metadata    TEXT,
    importance  REAL DEFAULT 0.5,
    created_at  TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    access_count INTEGER DEFAULT 0,
    decay       REAL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
"""


def _row_to_memory(row: tuple) -> Memory:
    return Memory(
        id=row[0],
        agent=row[1],
        type=MemoryType(row[2]),
        content=row[3],
        summary=row[4],
        embedding=json.loads(row[5]) if row[5] else [],
        metadata=json.loads(row[6]) if row[6] else {},
        importance=row[7],
        created_at=row[8],
        accessed_at=row[9],
        access_count=row[10],
        decay=row[11],
    )


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------

class MemoryManager:
    """Long-term memory with semantic search."""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    # -- initialisation -----------------------------------------------------

    def _init_db(self) -> None:
        self.db.executescript(_SCHEMA_SQL)
        self.db.commit()

    # -- store --------------------------------------------------------------

    def store(
        self,
        agent: str,
        mem_type: MemoryType,
        content: str,
        summary: str = "",
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> str:
        """Store a new memory. Returns the memory id."""
        mem_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        embedding = simple_embed(content)
        if not summary:
            summary = content[:120].replace("\n", " ")
        self.db.execute(
            """INSERT INTO memories
               (id, agent, type, content, summary, embedding, metadata,
                importance, created_at, accessed_at, access_count, decay)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0)""",
            (
                mem_id,
                agent,
                mem_type.value,
                content,
                summary,
                json.dumps(embedding),
                json.dumps(metadata or {}),
                max(0.0, min(1.0, importance)),
                now,
                now,
            ),
        )
        self.db.commit()
        return mem_id

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        agent: str | None = None,
        mem_type: MemoryType | None = None,
        limit: int = 10,
        min_importance: float = 0.0,
    ) -> list[Memory]:
        """Semantic search across memories.

        Scoring = cosine_similarity * importance * decay.
        """
        q_embedding = simple_embed(query)
        if not q_embedding:
            return []

        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []

        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        if mem_type:
            sql += " AND type = ?"
            params.append(mem_type.value)
        if min_importance > 0:
            sql += " AND importance >= ?"
            params.append(min_importance)

        rows = self.db.execute(sql, params).fetchall()

        scored: list[tuple[float, Memory]] = []
        for row in rows:
            mem = _row_to_memory(row)
            if not mem.embedding:
                continue
            sim = cosine_similarity(q_embedding, mem.embedding)
            score = sim * mem.importance * mem.decay
            scored.append((score, mem))

        scored.sort(key=lambda t: t[0], reverse=True)
        return [mem for _, mem in scored[:limit]]

    # -- recall -------------------------------------------------------------

    def recall(self, memory_id: str) -> Memory | None:
        """Recall a specific memory (updates access stats)."""
        row = self.db.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        mem = _row_to_memory(row)
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
            (now, memory_id),
        )
        self.db.commit()
        mem.accessed_at = now
        mem.access_count += 1
        return mem

    # -- forget -------------------------------------------------------------

    def forget(self, memory_id: str) -> bool:
        """Delete a memory. Returns True if it existed."""
        cur = self.db.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        self.db.commit()
        return cur.rowcount > 0

    # -- maintenance --------------------------------------------------------

    def decay_all(self, decay_rate: float = 0.01) -> int:
        """Apply time-based decay to all memories. Returns count affected."""
        cur = self.db.execute(
            "UPDATE memories SET decay = MAX(0.0, decay - ?) WHERE decay > 0",
            (decay_rate,),
        )
        self.db.commit()
        return cur.rowcount

    def consolidate(self, agent: str) -> int:
        """Merge highly similar memories from the same agent into summaries.

        Finds pairs of memories with cosine similarity > 0.85 and merges
        the less-important one into the more-important one's content.
        Returns the number of memories removed.
        """
        rows = self.db.execute(
            "SELECT * FROM memories WHERE agent = ? ORDER BY importance DESC",
            (agent,),
        ).fetchall()

        memories = [_row_to_memory(r) for r in rows]
        to_remove: set[str] = set()

        for i in range(len(memories)):
            if memories[i].id in to_remove:
                continue
            for j in range(i + 1, len(memories)):
                if memories[j].id in to_remove:
                    continue
                if not memories[i].embedding or not memories[j].embedding:
                    continue
                sim = cosine_similarity(memories[i].embedding, memories[j].embedding)
                if sim > 0.85:
                    # merge into the higher-importance one
                    merged_content = (
                        memories[i].content
                        + "\n\n[merged] "
                        + memories[j].content
                    )
                    self.db.execute(
                        "UPDATE memories SET content = ?, summary = ? WHERE id = ?",
                        (
                            merged_content,
                            merged_content[:120].replace("\n", " "),
                            memories[i].id,
                        ),
                    )
                    # update embedding of merged memory
                    new_emb = simple_embed(merged_content)
                    self.db.execute(
                        "UPDATE memories SET embedding = ? WHERE id = ?",
                        (json.dumps(new_emb), memories[i].id),
                    )
                    to_remove.add(memories[j].id)

        if to_remove:
            self.db.executemany(
                "DELETE FROM memories WHERE id = ?",
                [(mid,) for mid in to_remove],
            )
            self.db.commit()

        return len(to_remove)

    # -- context injection --------------------------------------------------

    def get_context(
        self, query: str, agent: str, max_tokens: int = 2000
    ) -> str:
        """Get relevant context for a query, formatted for LLM consumption.

        Rough token estimate: 1 token ≈ 4 characters.
        """
        memories = self.search(query, agent=agent, limit=20)
        if not memories:
            return ""

        lines: list[str] = ["## Relevant Memories\n"]
        char_budget = max_tokens * 4
        used = 0

        for m in memories:
            line = f"- [{m.type.value}] {m.summary} (importance: {m.importance:.1f})"
            if len(line) + used > char_budget:
                break
            lines.append(line)
            used += len(line)

        return "\n".join(lines)

    # -- stats --------------------------------------------------------------

    def stats(self) -> dict:
        """Return aggregate statistics."""
        total = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_agent = dict(
            self.db.execute(
                "SELECT agent, COUNT(*) FROM memories GROUP BY agent"
            ).fetchall()
        )
        by_type = dict(
            self.db.execute(
                "SELECT type, COUNT(*) FROM memories GROUP BY type"
            ).fetchall()
        )
        avg_importance = self.db.execute(
            "SELECT AVG(importance) FROM memories"
        ).fetchone()[0] or 0.0
        avg_decay = self.db.execute(
            "SELECT AVG(decay) FROM memories"
        ).fetchone()[0] or 0.0
        return {
            "total": total,
            "by_agent": by_agent,
            "by_type": by_type,
            "avg_importance": round(avg_importance, 3),
            "avg_decay": round(avg_decay, 3),
        }

    # -- close --------------------------------------------------------------

    def close(self) -> None:
        self.db.close()


# ---------------------------------------------------------------------------
# Auto-memory helpers (agent integration)
# ---------------------------------------------------------------------------

def _is_decision(text: str) -> bool:
    """Heuristic: does text look like a decision or rationale?"""
    cues = [
        "decided", "chose", "selected", "we will", "plan is",
        "strategy", "approach", "rationale", "because",
        "recommendation", "conclusion",
    ]
    lower = text.lower()
    return any(c in lower for c in cues)


def _extract_preferences(conversation: list[dict]) -> list[str]:
    """Heuristic: extract lines that look like user preferences."""
    prefs: list[str] = []
    for msg in conversation:
        if msg.get("role") != "user":
            continue
        text = msg.get("content", "")
        cues = [
            "i prefer", "i like", "i want", "i don't want",
            "please always", "never do", "i need", "my preference",
            "i usually", "i want you to",
        ]
        lower = text.lower()
        if any(c in lower for c in cues):
            prefs.append(text.strip())
    return prefs


def _summarise_conversation(conversation: list[dict]) -> str:
    """Produce a one-line summary of a conversation."""
    parts: list[str] = []
    for msg in conversation[-6:]:  # last 6 turns
        role = msg.get("role", "user")
        content = (msg.get("content") or "")[:100].replace("\n", " ")
        if content:
            parts.append(f"{role}: {content}")
    return " | ".join(parts) if parts else "empty conversation"


async def auto_memory(
    agent_name: str,
    conversation: list[dict],
    tool_results: list | None = None,
    manager: MemoryManager | None = None,
) -> list[str]:
    """Automatically extract and store memories from a conversation.

    Returns a list of stored memory ids.
    """
    if manager is None:
        manager = MemoryManager()

    stored: list[str] = []

    # store conversation summary
    summary = _summarise_conversation(conversation)
    mid = manager.store(
        agent_name, MemoryType.EPISODIC, summary, importance=0.3
    )
    stored.append(mid)

    # store decisions from tool results
    if tool_results:
        for result in tool_results:
            text = getattr(result, "content", str(result))
            if _is_decision(text):
                mid = manager.store(
                    agent_name,
                    MemoryType.DECISION,
                    text,
                    importance=0.7,
                    metadata={
                        "tool": getattr(result, "tool", "unknown"),
                        "args": getattr(result, "args", {}),
                    },
                )
                stored.append(mid)

    # store user preferences
    prefs = _extract_preferences(conversation)
    for pref in prefs:
        mid = manager.store(
            agent_name, MemoryType.PREFERENCE, pref, importance=0.8
        )
        stored.append(mid)

    return stored


async def get_memory_context(agent_name: str, user_message: str) -> str:
    """Get relevant memories to inject into an LLM context."""
    mgr = MemoryManager()
    try:
        return mgr.get_context(user_message, agent_name)
    finally:
        mgr.close()


# ---------------------------------------------------------------------------
# Dashboard / HTTP API helpers
# ---------------------------------------------------------------------------

def api_search(params: dict) -> dict:
    """Handle GET /api/memory/search."""
    mgr = MemoryManager()
    try:
        query = params.get("q", "")
        agent = params.get("agent")
        mem_type = MemoryType(params["type"]) if params.get("type") else None
        limit = int(params.get("limit", 10))
        results = mgr.search(query, agent=agent, mem_type=mem_type, limit=limit)
        return {
            "results": [
                {
                    "id": m.id,
                    "agent": m.agent,
                    "type": m.type.value,
                    "summary": m.summary,
                    "content": m.content,
                    "importance": m.importance,
                    "decay": m.decay,
                    "access_count": m.access_count,
                    "created_at": m.created_at,
                    "metadata": m.metadata,
                }
                for m in results
            ]
        }
    finally:
        mgr.close()


def api_store(payload: dict) -> dict:
    """Handle POST /api/memory/store."""
    mgr = MemoryManager()
    try:
        mid = mgr.store(
            agent=payload["agent"],
            mem_type=MemoryType(payload["type"]),
            content=payload["content"],
            summary=payload.get("summary", ""),
            importance=payload.get("importance", 0.5),
            metadata=payload.get("metadata"),
        )
        return {"id": mid}
    finally:
        mgr.close()


def api_forget(memory_id: str) -> dict:
    """Handle DELETE /api/memory/:id."""
    mgr = MemoryManager()
    try:
        removed = mgr.forget(memory_id)
        return {"deleted": removed}
    finally:
        mgr.close()


def api_context(agent: str, params: dict) -> dict:
    """Handle GET /api/memory/context/:agent."""
    mgr = MemoryManager()
    try:
        query = params.get("q", "")
        ctx = mgr.get_context(query, agent)
        return {"context": ctx}
    finally:
        mgr.close()


def api_stats() -> dict:
    """Handle GET /api/memory/stats."""
    mgr = MemoryManager()
    try:
        return mgr.stats()
    finally:
        mgr.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="memory",
        description="Starship OS Long-Term Memory Manager",
    )
    sub = parser.add_subparsers(dest="command")

    # store
    p_store = sub.add_parser("store", help="Store a new memory")
    p_store.add_argument("agent", help="Agent name")
    p_store.add_argument(
        "type",
        choices=[t.value for t in MemoryType],
        help="Memory type",
    )
    p_store.add_argument("content", help="Memory content")
    p_store.add_argument("--summary", default="", help="One-line summary")
    p_store.add_argument(
        "--importance", type=float, default=0.5, help="Importance 0.0-1.0"
    )
    p_store.add_argument(
        "--metadata", default="{}", help="JSON metadata dict"
    )

    # search
    p_search = sub.add_parser("search", help="Semantic search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--agent", default=None, help="Filter by agent")
    p_search.add_argument(
        "--type", default=None, choices=[t.value for t in MemoryType]
    )
    p_search.add_argument(
        "--limit", type=int, default=10, help="Max results"
    )
    p_search.add_argument(
        "--min-importance", type=float, default=0.0, help="Min importance"
    )

    # recall
    p_recall = sub.add_parser("recall", help="Recall a specific memory")
    p_recall.add_argument("id", help="Memory id")

    # forget
    p_forget = sub.add_parser("forget", help="Delete a memory")
    p_forget.add_argument("id", help="Memory id")

    # context
    p_ctx = sub.add_parser("context", help="Get context for a query")
    p_ctx.add_argument("agent", help="Agent name")
    p_ctx.add_argument("query", help="User query")
    p_ctx.add_argument(
        "--max-tokens", type=int, default=2000, help="Token budget"
    )

    # stats
    sub.add_parser("stats", help="Show memory statistics")

    # consolidate
    p_con = sub.add_parser("consolidate", help="Consolidate similar memories")
    p_con.add_argument("agent", help="Agent name")

    # decay
    p_decay = sub.add_parser("decay", help="Apply time-based decay")
    p_decay.add_argument(
        "--rate", type=float, default=0.01, help="Decay rate"
    )

    args = parser.parse_args()
    mgr = MemoryManager()

    try:
        if args.command == "store":
            meta = json.loads(args.metadata)
            mid = mgr.store(
                agent=args.agent,
                mem_type=MemoryType(args.type),
                content=args.content,
                summary=args.summary,
                importance=args.importance,
                metadata=meta,
            )
            print(f"Stored memory {mid}")

        elif args.command == "search":
            results = mgr.search(
                args.query,
                agent=args.agent,
                mem_type=MemoryType(args.type) if args.type else None,
                limit=args.limit,
                min_importance=args.min_importance,
            )
            if not results:
                print("No memories found.")
            for m in results:
                print(
                    f"  [{m.id}] ({m.type.value}) {m.summary}  "
                    f"importance={m.importance:.2f} decay={m.decay:.2f}"
                )

        elif args.command == "recall":
            mem = mgr.recall(args.id)
            if mem is None:
                print(f"Memory {args.id} not found.")
            else:
                print(f"ID:          {mem.id}")
                print(f"Agent:       {mem.agent}")
                print(f"Type:        {mem.type.value}")
                print(f"Summary:     {mem.summary}")
                print(f"Content:     {mem.content}")
                print(f"Importance:  {mem.importance}")
                print(f"Decay:       {mem.decay}")
                print(f"Access count:{mem.access_count}")
                print(f"Created:     {mem.created_at}")
                print(f"Accessed:    {mem.accessed_at}")
                print(f"Metadata:    {json.dumps(mem.metadata)}")

        elif args.command == "forget":
            removed = mgr.forget(args.id)
            print(f"Deleted: {removed}")

        elif args.command == "context":
            ctx = mgr.get_context(args.query, args.agent, args.max_tokens)
            print(ctx if ctx else "(no relevant memories)")

        elif args.command == "stats":
            s = mgr.stats()
            print(json.dumps(s, indent=2))

        elif args.command == "consolidate":
            n = mgr.consolidate(args.agent)
            print(f"Consolidated {n} memories.")

        elif args.command == "decay":
            n = mgr.decay_all(args.rate)
            print(f"Decayed {n} memories by {args.rate}.")

        else:
            parser.print_help()

    finally:
        mgr.close()


if __name__ == "__main__":
    _cli()

"""
Starship OS — FTS5 Session Archive Service

Persists every agent session with full-text search capability.
Agents can search past sessions: "archive_search 'CNC recalibration'"

Usage:
    python3 archive.py search "keyword" [--agent proxy] [--limit 10]
    python3 archive.py stats
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone


_DEFAULT_DB = os.environ.get("AGNETIC_ARCHIVE_DB", "/tmp/agnetic-data/archive.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS archive (
    id              TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    timestamp       TEXT NOT NULL,
    command         TEXT NOT NULL DEFAULT '',
    response        TEXT NOT NULL DEFAULT '',
    tool_calls      TEXT NOT NULL DEFAULT '[]',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    risk_level      TEXT NOT NULL DEFAULT 'low',
    approval_status TEXT NOT NULL DEFAULT 'none',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_agent ON archive(agent);
CREATE INDEX IF NOT EXISTS idx_archive_ts ON archive(timestamp);
CREATE INDEX IF NOT EXISTS idx_archive_session ON archive(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
    command, response, tool_calls,
    content='archive',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS archive_ai AFTER INSERT ON archive BEGIN
    INSERT INTO archive_fts(rowid, command, response, tool_calls)
    VALUES (new.rowid, new.command, new.response, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS archive_ad AFTER DELETE ON archive BEGIN
    INSERT INTO archive_fts(archive_fts, rowid, command, response, tool_calls)
    VALUES ('delete', old.rowid, old.command, old.response, old.tool_calls);
END;
"""


class ArchiveService:
    """FTS5 session archive with full-text search."""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.db.executescript(_SCHEMA_SQL)
        self.db.commit()

    def write(
        self,
        agent: str,
        command: str,
        response: str = "",
        tool_calls: list | None = None,
        session_id: str = "",
        duration_ms: int = 0,
        risk_level: str = "low",
        approval_status: str = "none",
    ) -> str:
        """Archive a session entry. Returns the entry id."""
        entry_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO archive
               (id, agent, session_id, timestamp, command, response,
                tool_calls, duration_ms, risk_level, approval_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                agent,
                session_id,
                now,
                command[:2000],
                response[:5000],
                json.dumps(tool_calls or []),
                duration_ms,
                risk_level,
                approval_status,
                now,
            ),
        )
        self.db.commit()
        return entry_id

    def search(
        self,
        query: str,
        agent: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search across archived sessions using FTS5."""
        try:
            sql = """
                SELECT a.id, a.agent, a.session_id, a.timestamp, a.command,
                       a.response, a.risk_level, a.approval_status
                FROM archive a
                JOIN archive_fts fts ON a.rowid = fts.rowid
                WHERE archive_fts MATCH ?
            """
            params: list = [query]
            if agent:
                sql += " AND a.agent = ?"
                params.append(agent)
            sql += " ORDER BY a.timestamp DESC LIMIT ?"
            params.append(limit)

            rows = self.db.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            if "no such module: fts5" in str(e):
                return self._fallback_search(query, agent, limit)
            raise

    def _fallback_search(self, query: str, agent: str | None = None, limit: int = 10) -> list[dict]:
        """LIKE-based fallback when FTS5 is unavailable."""
        sql = "SELECT * FROM archive WHERE (command LIKE ? OR response LIKE ?)"
        params: list = [f"%{query}%", f"%{query}%"]
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        """Return aggregate statistics."""
        total = self.db.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        by_agent = dict(
            self.db.execute(
                "SELECT agent, COUNT(*) FROM archive GROUP BY agent"
            ).fetchall()
        )
        latest = self.db.execute(
            "SELECT agent, command, timestamp FROM archive ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()
        return {
            "total": total,
            "by_agent": by_agent,
            "latest": [dict(r) for r in latest],
        }

    def close(self):
        self.db.close()


def _cli():
    parser = argparse.ArgumentParser(prog="archive", description="Starship OS FTS5 Session Archive")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Full-text search sessions")
    p_search.add_argument("query", help="Search keyword")
    p_search.add_argument("--agent", default=None, help="Filter by agent")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")

    sub.add_parser("stats", help="Show archive statistics")

    args = parser.parse_args()
    svc = ArchiveService()
    try:
        if args.command == "search":
            results = svc.search(args.query, agent=args.agent, limit=args.limit)
            if not results:
                print("No results found.")
            for r in results:
                print(f"  [{r.get('agent')}] {r.get('command')[:80]}  ({r.get('timestamp')[:19]})")
        elif args.command == "stats":
            s = svc.stats()
            print(json.dumps(s, indent=2))
        else:
            parser.print_help()
    finally:
        svc.close()


if __name__ == "__main__":
    _cli()

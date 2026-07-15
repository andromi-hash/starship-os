#!/usr/bin/env python3
"""
Starship OS — Central Log Aggregation Service

Subscribes to NATS agnetic.logs.>, stores structured log entries in SQLite,
and exposes a query CLI and importable search/tail/stats API.

Usage:
  python3 log_aggregator.py                   # run as daemon (NATS subscriber)
  python3 log_aggregator.py search "error" --source proxy --since 1h --limit 50
  python3 log_aggregator.py tail --source proxy --lines 20
  python3 log_aggregator.py stats
"""

import sys
import os
import re
import json
import sqlite3
import asyncio
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("log-aggregator")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "logs.db"
LOG_SOURCES = ["proxy", "romi", "ergo", "staragent", "dashboard", "nats", "watchdog"]

LEVEL_MAP = {
    "DEBUG": 0, "INFO": 1, "NOTICE": 1, "WARN": 2, "WARNING": 2,
    "ERROR": 3, "CRITICAL": 4, "FATAL": 4,
}


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT    NOT NULL,
            ts_epoch   REAL   NOT NULL,
            level      TEXT    NOT NULL DEFAULT 'INFO',
            source     TEXT    NOT NULL DEFAULT 'unknown',
            event      TEXT    NOT NULL DEFAULT '',
            message    TEXT    NOT NULL DEFAULT '',
            details    TEXT    NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts_epoch)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON logs(source)")
    conn.commit()
    return conn


def insert_log(entry: dict):
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO logs (timestamp, ts_epoch, level, source, event, message, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.get("timestamp", datetime.now().isoformat()),
                entry.get("ts_epoch", datetime.now().timestamp()),
                entry.get("level", "INFO").upper(),
                entry.get("source", "unknown"),
                entry.get("event", ""),
                entry.get("message", ""),
                json.dumps(entry.get("details", {})) if isinstance(entry.get("details"), dict) else str(entry.get("details", "")),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_since(since: str) -> float:
    m = re.match(r"^(\d+)(m|h|d)$", since)
    if not m:
        raise ValueError(f"Invalid --since value: {since} (use e.g. 30m, 6h, 7d)")
    amount, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
    return (datetime.now() - delta).timestamp()


def search(query: str = "", level: str = "", source: str = "", since: str = "", limit: int = 100):
    conn = _get_db()
    try:
        clauses = []
        params: list = []
        if query:
            clauses.append("(message LIKE ? OR event LIKE ? OR details LIKE ?)")
            q = f"%{query}%"
            params.extend([q, q, q])
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        if source:
            clauses.append("source = ?")
            params.append(source)
        if since:
            clauses.append("ts_epoch >= ?")
            params.append(_parse_since(since))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM logs{where} ORDER BY ts_epoch DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            entry = dict(r)
            try:
                entry["details"] = json.loads(entry["details"])
            except (json.JSONDecodeError, TypeError):
                pass
            results.append(entry)
        return results
    finally:
        conn.close()


def tail(source: str = "", lines: int = 50):
    conn = _get_db()
    try:
        if source:
            rows = conn.execute(
                "SELECT * FROM logs WHERE source = ? ORDER BY ts_epoch DESC LIMIT ?",
                (source, lines),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY ts_epoch DESC LIMIT ?",
                (lines,),
            ).fetchall()
        results = []
        for r in rows:
            entry = dict(r)
            try:
                entry["details"] = json.loads(entry["details"])
            except (json.JSONDecodeError, TypeError):
                pass
            results.append(entry)
        return list(reversed(results))
    finally:
        conn.close()


def stats():
    conn = _get_db()
    try:
        total = conn.execute("SELECT COUNT(*) as cnt FROM logs").fetchone()["cnt"]
        by_level = dict(
            conn.execute("SELECT level, COUNT(*) as cnt FROM logs GROUP BY level ORDER BY cnt DESC").fetchall()
        )
        by_source = dict(
            conn.execute("SELECT source, COUNT(*) as cnt FROM logs GROUP BY source ORDER BY cnt DESC").fetchall()
        )
        newest = conn.execute("SELECT timestamp FROM logs ORDER BY ts_epoch DESC LIMIT 1").fetchone()
        oldest = conn.execute("SELECT timestamp FROM logs ORDER BY ts_epoch ASC LIMIT 1").fetchone()
        return {
            "total": total,
            "by_level": by_level,
            "by_source": by_source,
            "newest": newest["timestamp"] if newest else None,
            "oldest": oldest["timestamp"] if oldest else None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# NATS subscription daemon
# ---------------------------------------------------------------------------

async def run_daemon():
    from nats import connect as nats_connect
    _get_db()  # ensure tables
    log.info("Log aggregator starting — DB at %s", DB_PATH)
    nc = await nats_connect(NATS_URL)
    log.info("Connected to NATS: %s", NATS_URL)
    sub = await nc.subscribe("agnetic.logs.>")
    log.info("Subscribed to agnetic.logs.>")

    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            # Derive source from NATS subject: agnetic.logs.<source>
            parts = msg.subject.split(".")
            if len(parts) >= 3 and not data.get("source"):
                data["source"] = parts[2]
            data.setdefault("timestamp", datetime.now().isoformat())
            data.setdefault("ts_epoch", datetime.now().timestamp())
            insert_log(data)
        except (json.JSONDecodeError, Exception) as e:
            log.warning("Skipping malformed log message: %s", e)

        try:
            await msg.ack()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_table(rows):
    if not rows:
        print("No results.")
        return
    for r in rows:
        ts = r.get("timestamp", "")[:19]
        lvl = r.get("level", "").ljust(7)
        src = r.get("source", "").ljust(12)
        evt = r.get("event", "")
        msg = r.get("message", "")
        prefix = f"{ts} [{lvl}] {src}"
        if evt:
            prefix += f" ({evt})"
        print(f"{prefix}  {msg}")


def main():
    parser = argparse.ArgumentParser(description="Agnetic Log Aggregator")
    sub = parser.add_subparsers(dest="command")

    sp_search = sub.add_parser("search", help="Search logs")
    sp_search.add_argument("query", nargs="?", default="", help="Free-text query")
    sp_search.add_argument("--level", default="", help="Filter by level (DEBUG/INFO/WARN/ERROR/CRITICAL)")
    sp_search.add_argument("--source", default="", help="Filter by source")
    sp_search.add_argument("--since", default="", help="Time window (e.g. 30m, 6h, 7d)")
    sp_search.add_argument("--limit", type=int, default=100, help="Max results")
    sp_search.add_argument("--json", action="store_true", help="Output as JSON")

    sp_tail = sub.add_parser("tail", help="Tail recent logs")
    sp_tail.add_argument("--source", default="", help="Filter by source")
    sp_tail.add_argument("--lines", type=int, default=50, help="Number of lines")
    sp_tail.add_argument("--json", action="store_true", help="Output as JSON")

    sp_stats = sub.add_parser("stats", help="Show log statistics")
    sp_stats.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if args.command == "search":
        rows = search(args.query, args.level, args.source, args.since, args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            _print_table(rows)

    elif args.command == "tail":
        rows = tail(args.source, args.lines)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            _print_table(rows)

    elif args.command == "stats":
        s = stats()
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print(f"Total logs:  {s['total']}")
            print(f"Date range:  {s['oldest'] or 'N/A'}  →  {s['newest'] or 'N/A'}")
            if s["by_level"]:
                print("By level:")
                for lvl, cnt in s["by_level"].items():
                    print(f"  {lvl.ljust(10)} {cnt}")
            if s["by_source"]:
                print("By source:")
                for src, cnt in s["by_source"].items():
                    print(f"  {src.ljust(15)} {cnt}")

    else:
        asyncio.run(run_daemon())


if __name__ == "__main__":
    main()

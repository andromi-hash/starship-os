#!/usr/bin/env python3
"""
Starship OS — Log File Collector / Tailer

Tails log files from the project logs/ directory and /var/log/agnetic/,
parses each line, deduplicates identical messages within a 5 s window,
and publishes structured entries to NATS agnetic.logs.<source>.

Usage:
  python3 log_collector.py          # run as daemon
  python3 log_collector.py --dry-run  # parse + print, don't publish to NATS
"""

import os
import re
import json
import asyncio
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("log-collector")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
PROJECT_ROOT = Path(os.getenv("AGNETIC_ROOT", str(Path(__file__).resolve().parent.parent)))
LOG_DIRS = [
    PROJECT_ROOT / "logs",
    Path("/var/log/agnetic"),
]
DEDUP_WINDOW_S = 5.0

# Map log file names to source identifiers
FILE_SOURCE_MAP = {
    "agents-proxy.log": "proxy",
    "agents-romi.log": "romi",
    "agents-ergo.log": "ergo",
    "dashboard.log": "dashboard",
    "message-history.log": "staragent",
    "status-bridge.log": "watchdog",
    "nats.log": "nats",
}

# ---------------------------------------------------------------------------
# Log line parsers
# ---------------------------------------------------------------------------

# Python logging: 2026-07-11 14:42:59,000 [INFO] agent-daemon: Starting agent...
_RE_PYTHON = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},?\d*)\s+"
    r"\[(\w+)\]\s+"
    r"(\S+?):\s*"
    r"(.+)$",
    re.DOTALL,
)

# systemd journal: Jul 11 14:42:59 hostname agnetic-proxy[1234]: message
_RE_JOURNAL = re.compile(
    r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+\S+?\[(\d+)\]:\s*(.+)$",
    re.DOTALL,
)

# HTTP access: 127.0.0.1 [11/Jul/2026:18:38:27 -0600] "GET /api/dashboard HTTP/1.1" 200 1700
_RE_HTTP = re.compile(
    r"^(\S+)\s+\[(\d{2}/\w+/\d{4}:\d{2}:\d{2}:\d{2}\s+[+-]\d{4})\]\s+\"(.+?)\""
    r"\s+(\d{3})\s+(\d+)"
)


def parse_python_log_line(line: str, source: str) -> dict | None:
    m = _RE_PYTHON.match(line.strip())
    if not m:
        return None
    ts_str, level, logger, message = m.groups()
    return {
        "timestamp": ts_str.replace(",", "."),
        "ts_epoch": datetime.now().timestamp(),
        "level": level.upper(),
        "source": source,
        "event": logger.strip(),
        "message": message.strip(),
        "details": {},
    }


def parse_journal_line(line: str, source: str) -> dict | None:
    m = _RE_JOURNAL.match(line.strip())
    if not m:
        return None
    ts_str, pid, message = m.groups()
    return {
        "timestamp": f"{datetime.now().year}-{ts_str}",
        "ts_epoch": datetime.now().timestamp(),
        "level": "INFO",
        "source": source,
        "event": f"journal[{pid}]",
        "message": message.strip(),
        "details": {"pid": pid},
    }


def parse_http_line(line: str, source: str) -> dict | None:
    m = _RE_HTTP.match(line.strip())
    if not m:
        return None
    client, ts_str, request, status, size = m.groups()
    level = "ERROR" if int(status) >= 400 else "INFO"
    return {
        "timestamp": ts_str,
        "ts_epoch": datetime.now().timestamp(),
        "level": level,
        "source": source,
        "event": "http",
        "message": f"{request} → {status}",
        "details": {"client": client, "status": int(status), "size": int(size)},
    }


def parse_line(line: str, source: str) -> dict:
    line = line.strip()
    if not line:
        return None
    parsed = parse_python_log_line(line, source)
    if parsed:
        return parsed
    parsed = parse_http_line(line, source)
    if parsed:
        return parsed
    parsed = parse_journal_line(line, source)
    if parsed:
        return parsed
    return {
        "timestamp": datetime.now().isoformat(),
        "ts_epoch": datetime.now().timestamp(),
        "level": "INFO",
        "source": source,
        "event": "raw",
        "message": line,
        "details": {},
    }


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class DedupBuffer:
    """Ring buffer keyed by content hash; drops duplicate within window."""

    def __init__(self, window: float = DEDUP_WINDOW_S):
        self._window = window
        self._seen: OrderedDict[str, float] = OrderedDict()

    def is_duplicate(self, entry: dict) -> bool:
        key = hashlib.sha256(
            f"{entry['source']}:{entry['level']}:{entry['message']}".encode()
        ).hexdigest()[:16]
        now = datetime.now().timestamp()
        # Evict stale
        while self._seen and next(iter(self._seen.values())) < now - self._window:
            self._seen.popitem(last=False)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


# ---------------------------------------------------------------------------
# File tailer (follows file like tail -f)
# ---------------------------------------------------------------------------

class FileTailer:
    def __init__(self, path: Path, source: str, dedup: DedupBuffer):
        self.path = path
        self.source = source
        self.dedup = dedup
        self._fh = None
        self._inode = None

    def _open(self):
        if not self.path.exists():
            return False
        fh = open(self.path, "r")
        stat = os.fstat(fh.fileno())
        self._inode = stat.st_ino
        # Seek to end so we only get new lines
        fh.seek(0, 2)
        self._fh = fh
        log.info("Tail started: %s (source=%s)", self.path, self.source)
        return True

    def _reopened(self) -> bool:
        """Check if file was rotated (inode changed)."""
        if not self._fh or not self.path.exists():
            return False
        try:
            stat = os.fstat(self._fh.fileno())
            return stat.st_ino != self._inode
        except (OSError, ValueError):
            return True

    def read_new_lines(self) -> list[str]:
        if self._fh is None:
            if not self._open():
                return []
        if self._reopened():
            log.info("File rotated: %s — reopening", self.path)
            self._fh.close()
            self._fh = None
            self._inode = None
            return self.read_new_lines()
        lines = []
        while True:
            line = self._fh.readline()
            if not line:
                break
            lines.append(line)
        return lines

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Collector daemon
# ---------------------------------------------------------------------------

async def run_collector(dry_run: bool = False):
    dedup = DedupBuffer()
    tailers: list[FileTailer] = []

    # Discover log files
    for log_dir in LOG_DIRS:
        if not log_dir.exists():
            continue
        for f in log_dir.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            source = FILE_SOURCE_MAP.get(f.name, f.name.removesuffix(".log"))
            tailers.append(FileTailer(f, source, dedup))

    if not tailers:
        log.warning("No log files found in %s", [str(d) for d in LOG_DIRS])
        log.info("Creating placeholder logs directory: %s", LOG_DIRS[0])
        LOG_DIRS[0].mkdir(parents=True, exist_ok=True)

    log.info("Monitoring %d log files", len(tailers))
    for t in tailers:
        log.info("  → %s (source=%s)", t.path, t.source)

    nc = None
    if not dry_run:
        from nats import connect as nats_connect
        nc = await nats_connect(NATS_URL)
        log.info("Connected to NATS: %s", NATS_URL)

    while True:
        for tailer in tailers:
            for line in tailer.read_new_lines():
                parsed = parse_line(line, tailer.source)
                if parsed is None:
                    continue
                if dedup.is_duplicate(parsed):
                    continue
                if dry_run:
                    lvl = parsed["level"].ljust(7)
                    print(f"[{parsed['source']}] [{lvl}] {parsed['message'][:120]}")
                else:
                    subject = f"agnetic.logs.{parsed['source']}"
                    await nc.publish(subject, json.dumps(parsed, default=str))
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agnetic Log Collector")
    parser.add_argument("--dry-run", action="store_true", help="Print parsed logs instead of publishing to NATS")
    args = parser.parse_args()
    asyncio.run(run_collector(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

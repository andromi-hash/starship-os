"""
Starship OS — Obsidian HITL Vault

Bridges the HITL approval system to a markdown vault directory that can
be opened with Obsidian or any markdown editor. Each approval request
is rendered as an Obsidian-compatible note with YAML frontmatter.

Usage:
    from services.hitl_vault import HITLVault
    vault = HITLVault()
    vault.sync()                          # sync hitl.db -> vault/
    vault.create(title, body, tags=[])    # create ad-hoc vault note
    vault.list()                          # list vault entries
"""

import os
import json
import uuid
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("hitl-vault")

VAULT_DIR = Path(os.environ.get("HITL_VAULT_DIR", "memory/vault"))
HITL_DB = Path(os.environ.get("HITL_DB", "/var/log/agnetic/hitl.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _yaml_frontmatter(data: dict) -> str:
    lines = ["---"]
    for k, v in data.items():
        if isinstance(v, str):
            lines.append(f'{k}: "{v}"')
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list):
            items = ", ".join(f'"{x}"' for x in v)
            lines.append(f"{k}: [{items}]")
        elif isinstance(v, dict):
            lines.append(f"{k}: {json.dumps(v)}")
        else:
            lines.append(f'{k}: "{v}"')
    lines.append("---")
    return "\n".join(lines)


class HITLVault:
    def __init__(self, vault_dir: str | Path = None, hitl_db: str | Path = None):
        self.vault_dir = Path(vault_dir or VAULT_DIR)
        self.hitl_db = Path(hitl_db or HITL_DB)
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    def _db_conn(self) -> sqlite3.Connection | None:
        if not self.hitl_db.exists():
            return None
        conn = sqlite3.connect(str(self.hitl_db))
        conn.row_factory = sqlite3.Row
        return conn

    def sync(self) -> dict:
        synced = 0
        errors = 0

        conn = self._db_conn()
        if conn is None:
            return {"synced": 0, "note": "hitl.db not found"}

        try:
            rows = conn.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return {"synced": 0, "note": "approvals table not found"}

        for row in rows:
            try:
                req_id = row["id"]
                fpath = self.vault_dir / f"{req_id}.md"
                self._write_approval_note(fpath, row)
                synced += 1
            except Exception as e:
                log.warning("sync error for %s: %s", row.get("id", "?"), e)
                errors += 1

        conn.close()
        return {"synced": synced, "errors": errors}

    def _write_approval_note(self, fpath: Path, row: sqlite3.Row) -> None:
        args_raw = row["arguments"] or "{}"
        ctx_raw = row["context"] or "{}"
        risk_reasons_raw = row["risk_reasons"] or "[]"

        try:
            arguments = json.loads(args_raw)
        except json.JSONDecodeError:
            arguments = {}
        try:
            context = json.loads(ctx_raw)
        except json.JSONDecodeError:
            context = {}
        try:
            risk_reasons = json.loads(risk_reasons_raw)
        except json.JSONDecodeError:
            risk_reasons = []

        frontmatter = _yaml_frontmatter({
            "id": row["id"],
            "tool": row["tool"],
            "agent": row["agent"],
            "risk_level": row["risk_level"],
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "decided_by": row.get("decided_by", ""),
            "decided_at": row.get("decided_at", ""),
            "reason": row.get("reason", ""),
        })

        lines = [frontmatter, ""]
        lines.append(f"# HITL Approval: {row['id'][:8]}")
        lines.append("")
        lines.append(f"- **Tool:** `{row['tool']}`")
        lines.append(f"- **Agent:** `{row['agent']}`")
        lines.append(f"- **Risk Level:** `{row['risk_level']}`")
        lines.append(f"- **Status:** `{row['status']}`")
        lines.append(f"- **Created:** {row['created_at']}")
        lines.append(f"- **Expires:** {row['expires_at']}")
        lines.append("")
        if risk_reasons:
            lines.append("## Risk Analysis")
            for r in risk_reasons:
                lines.append(f"- {r}")
            lines.append("")
        if row.get("risk_suggestion"):
            lines.append(f"> **Suggestion:** {row['risk_suggestion']}")
            lines.append("")

        lines.append("## Arguments")
        lines.append("```json")
        lines.append(json.dumps(arguments, indent=2))
        lines.append("```")
        lines.append("")

        if context:
            lines.append("## Context")
            lines.append("```json")
            lines.append(json.dumps(context, indent=2))
            lines.append("```")
            lines.append("")

        lines.append("---")
        lines.append(f"_Auto-generated from HITL approval `{row['id']}`_")

        fpath.write_text("\n".join(lines))

    def create(self, title: str, body: str, tags: list[str] = None,
               metadata: dict = None) -> dict:
        note_id = uuid.uuid4().hex[:12]
        now = _now()
        meta = {
            "id": note_id,
            "title": title,
            "type": "vault_note",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "tags": tags or [],
        }
        if metadata:
            meta.update(metadata)

        frontmatter = _yaml_frontmatter(meta)
        content = f"{frontmatter}\n\n# {title}\n\n{body}\n"
        fpath = self.vault_dir / f"{note_id}.md"
        fpath.write_text(content)
        return {"id": note_id, "path": str(fpath), "title": title}

    def list(self, status: str = None) -> list[dict]:
        entries = []
        for fpath in sorted(self.vault_dir.glob("*.md"), reverse=True):
            try:
                front = self._parse_frontmatter(fpath)
                if status and front.get("status") != status:
                    continue
                entries.append({
                    "id": front.get("id", fpath.stem),
                    "title": front.get("title", front.get("tool", fpath.stem)),
                    "status": front.get("status", "unknown"),
                    "tool": front.get("tool", ""),
                    "agent": front.get("agent", ""),
                    "risk_level": front.get("risk_level", ""),
                    "created_at": front.get("created_at", ""),
                    "file": str(fpath),
                })
            except Exception as e:
                log.warning("vault list parse error %s: %s", fpath.name, e)
        return entries

    def get(self, note_id: str) -> dict | None:
        fpath = self.vault_dir / f"{note_id}.md"
        if not fpath.exists():
            return None
        try:
            front = self._parse_frontmatter(fpath)
            body = self._strip_frontmatter(fpath.read_text())
            return {"metadata": front, "body": body.strip(), "file": str(fpath)}
        except Exception as e:
            log.warning("vault get error %s: %s", note_id, e)
            return None

    def approve(self, note_id: str, decided_by: str = "agent",
                reason: str = "") -> dict:
        return self._decide(note_id, "approved", decided_by, reason)

    def deny(self, note_id: str, decided_by: str = "agent",
             reason: str = "") -> dict:
        return self._decide(note_id, "denied", decided_by, reason)

    def _decide(self, note_id: str, decision: str, decided_by: str,
                reason: str) -> dict:
        conn = self._db_conn()
        if conn is None:
            fpath = self.vault_dir / f"{note_id}.md"
            if fpath.exists():
                text = fpath.read_text()
                text += f"\n\n> **{decision}** by {decided_by} | {reason}\n"
                fpath.write_text(text)
                return {"status": decision, "note": "local only (no hitl.db)"}
            return {"error": "vault note not found"}

        now = _now()
        try:
            cur = conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=?, reason=? WHERE id=?",
                (decision, decided_by, now, reason, note_id),
            )
            conn.commit()
            updated = cur.rowcount > 0
            conn.close()
            if updated:
                fpath = self.vault_dir / f"{note_id}.md"
                if fpath.exists():
                    text = fpath.read_text()
                    text += f"\n\n> **{decision}** by {decided_by} at {now} | {reason}\n"
                    fpath.write_text(text)
                return {"status": decision, "id": note_id}
            return {"error": f"approval {note_id} not found"}
        except Exception as e:
            conn.close()
            return {"error": str(e)}

    def stats(self) -> dict:
        entries = self.list()
        pending = [e for e in entries if e["status"] == "pending"]
        approved = [e for e in entries if e["status"] == "approved"]
        denied = [e for e in entries if e["status"] == "denied"]
        expired = [e for e in entries if e["status"] == "expired"]
        return {
            "total": len(entries),
            "pending": len(pending),
            "approved": len(approved),
            "denied": len(denied),
            "expired": len(expired),
            "vault_dir": str(self.vault_dir),
        }

    def _parse_frontmatter(self, fpath: Path) -> dict:
        text = fpath.read_text()
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}
        data = {}
        for line in parts[1].strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                data[key] = val
        return data

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return text.strip()

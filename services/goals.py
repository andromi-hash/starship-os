"""
Starship OS — Goals → Missions → Tasks System

Three-layer hierarchy:
  Goal (strategic) → Mission (tactical) → Task (operational)

Health flows DOWN: Goal health = aggregate of Mission health = aggregate of Task completion.
Status flows UP: Task completion updates Mission progress, which updates Goal health.

Integration points:
  - services/audit.py: temporal snapshots on every status change
  - services/hitl.py: approval gates for goal → active / goal → completed
  - services/hitl_vault.py: goal review requests as vault notes
  - services/archive.py: completed goal snapshots to FTS5
  - services/memory.py: KNOWLEDGE_GRAPH triples for task dependencies
  - services/webhooks.py: scheduled health-check routines
"""

from __future__ import annotations

import os
import json
import uuid
import sqlite3
import logging
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("starship-goals")

DB_DIR = Path(os.environ.get("STARSHIP_DATA_DIR", "/var/lib/starship"))
DB_PATH = DB_DIR / "goals.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# ── Status Enums ─────────────────────────────────────────────────────

class GoalStatus(Enum):
    PROPOSED = "proposed"
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELED = "canceled"

class MissionStatus(Enum):
    BACKLOG = "backlog"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELED = "canceled"

class TaskStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"

class Priority(Enum):
    NONE = "none"
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class Health(Enum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    OFF_TRACK = "off_track"
    NONE = "none"


# ── Data Classes ─────────────────────────────────────────────────────

@dataclass
class Goal:
    id: str
    title: str
    description: str = ""
    status: str = "proposed"
    priority: str = "medium"
    owner: str = ""
    target_date: str = ""
    health: str = "none"
    labels: str = "[]"
    resources: str = "{}"
    progress: float = 0.0
    mission_count: int = 0
    completed_mission_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["labels"] = json.loads(d.get("labels", "[]")) if isinstance(d.get("labels"), str) else d.get("labels", [])
        d["resources"] = json.loads(d.get("resources", "{}")) if isinstance(d.get("resources"), str) else d.get("resources", {})
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Goal":
        return cls(**dict(row))


@dataclass
class Mission:
    id: str
    goal_id: str
    title: str
    description: str = ""
    status: str = "backlog"
    lead: str = ""
    target_date: str = ""
    health: str = "none"
    teams: str = "[]"
    progress: float = 0.0
    task_count: int = 0
    completed_task_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["teams"] = json.loads(d.get("teams", "[]")) if isinstance(d.get("teams"), str) else d.get("teams", [])
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Mission":
        return cls(**dict(row))


@dataclass
class Task:
    id: str
    mission_id: str
    title: str
    description: str = ""
    status: str = "todo"
    priority: str = "medium"
    assignee: str = ""
    depends_on: str = "[]"
    completion_ts: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["depends_on"] = json.loads(d.get("depends_on", "[]")) if isinstance(d.get("depends_on"), str) else d.get("depends_on", [])
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(**dict(row))


# ── Database ─────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'proposed',
    priority    TEXT NOT NULL DEFAULT 'medium',
    owner       TEXT NOT NULL DEFAULT '',
    target_date TEXT NOT NULL DEFAULT '',
    health      TEXT NOT NULL DEFAULT 'none',
    labels      TEXT NOT NULL DEFAULT '[]',
    resources   TEXT NOT NULL DEFAULT '{}',
    progress    REAL NOT NULL DEFAULT 0.0,
    mission_count INTEGER NOT NULL DEFAULT 0,
    completed_mission_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
    id          TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL REFERENCES goals(id),
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'backlog',
    lead        TEXT NOT NULL DEFAULT '',
    target_date TEXT NOT NULL DEFAULT '',
    health      TEXT NOT NULL DEFAULT 'none',
    teams       TEXT NOT NULL DEFAULT '[]',
    progress    REAL NOT NULL DEFAULT 0.0,
    task_count  INTEGER NOT NULL DEFAULT 0,
    completed_task_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    mission_id    TEXT NOT NULL REFERENCES missions(id),
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'todo',
    priority      TEXT NOT NULL DEFAULT 'medium',
    assignee      TEXT NOT NULL DEFAULT '',
    depends_on    TEXT NOT NULL DEFAULT '[]',
    completion_ts TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_missions_goal ON missions(goal_id);
CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status);
CREATE INDEX IF NOT EXISTS idx_tasks_mission ON tasks(mission_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_owner ON goals(owner);
"""


# ── GoalsDB ──────────────────────────────────────────────────────────

class GoalsDB:
    def __init__(self, db_path: str | Path = None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    # ── Goals ─────────────────────────────────────────────────────────

    def goal_create(self, title: str, description: str = "",
                    priority: str = "medium", owner: str = "",
                    target_date: str = "", labels: list = None,
                    resources: dict = None) -> Goal:
        now = _now()
        gid = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO goals (id, title, description, status, priority, owner, "
            "target_date, health, labels, resources, progress, mission_count, "
            "completed_mission_count, created_at, updated_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?, ?, 'none', ?, ?, 0.0, 0, 0, ?, ?)",
            (gid, title, description, priority, owner, target_date,
             json.dumps(labels or []), json.dumps(resources or {}), now, now),
        )
        self._conn.commit()
        return self.goal_get(gid)

    def goal_get(self, gid: str) -> Goal | None:
        row = self._conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        return Goal.from_row(row) if row else None

    def goal_update(self, gid: str, **kwargs) -> Goal | None:
        now = _now()
        allowed = {"title", "description", "status", "priority", "owner",
                   "target_date", "labels", "resources"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.goal_get(gid)
        if "labels" in updates and isinstance(updates["labels"], (list, tuple)):
            updates["labels"] = json.dumps(updates["labels"])
        if "resources" in updates and isinstance(updates["resources"], dict):
            updates["resources"] = json.dumps(updates["resources"])
        updates["updated_at"] = now
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [gid]
        self._conn.execute(f"UPDATE goals SET {cols} WHERE id=?", vals)
        self._conn.commit()
        self._log_audit("goal.update", gid, updates)
        return self.goal_get(gid)

    def goal_delete(self, gid: str) -> bool:
        self._conn.execute("DELETE FROM tasks WHERE mission_id IN (SELECT id FROM missions WHERE goal_id=?)", (gid,))
        self._conn.execute("DELETE FROM missions WHERE goal_id=?", (gid,))
        cur = self._conn.execute("DELETE FROM goals WHERE id=?", (gid,))
        self._conn.commit()
        return cur.rowcount > 0

    def goal_list(self, status: str = None, owner: str = None) -> list[Goal]:
        sql = "SELECT * FROM goals WHERE 1=1"
        params = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if owner:
            sql += " AND owner=?"
            params.append(owner)
        sql += " ORDER BY created_at DESC"
        return [Goal.from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    # ── Missions ──────────────────────────────────────────────────────

    def mission_create(self, goal_id: str, title: str, description: str = "",
                       lead: str = "", target_date: str = "",
                       teams: list = None) -> Mission | None:
        goal = self.goal_get(goal_id)
        if not goal:
            return None
        now = _now()
        mid = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO missions (id, goal_id, title, description, status, lead, "
            "target_date, health, teams, progress, task_count, "
            "completed_task_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'backlog', ?, ?, 'none', ?, 0.0, 0, 0, ?, ?)",
            (mid, goal_id, title, description, lead, target_date,
             json.dumps(teams or []), now, now),
        )
        self._conn.commit()
        self._recompute_goal_counts(goal_id)
        return self.mission_get(mid)

    def mission_get(self, mid: str) -> Mission | None:
        row = self._conn.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
        return Mission.from_row(row) if row else None

    def mission_update(self, mid: str, **kwargs) -> Mission | None:
        now = _now()
        allowed = {"title", "description", "status", "lead", "target_date", "teams"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.mission_get(mid)
        if "teams" in updates and isinstance(updates["teams"], (list, tuple)):
            updates["teams"] = json.dumps(updates["teams"])
        updates["updated_at"] = now
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [mid]
        self._conn.execute(f"UPDATE missions SET {cols} WHERE id=?", vals)
        self._conn.commit()
        self._log_audit("mission.update", mid, updates)
        return self.mission_get(mid)

    def mission_delete(self, mid: str) -> bool:
        self._conn.execute("DELETE FROM tasks WHERE mission_id=?", (mid,))
        cur = self._conn.execute("DELETE FROM missions WHERE id=?", (mid,))
        self._conn.commit()
        return cur.rowcount > 0

    def mission_list(self, goal_id: str = None, status: str = None) -> list[Mission]:
        sql = "SELECT * FROM missions WHERE 1=1"
        params = []
        if goal_id:
            sql += " AND goal_id=?"
            params.append(goal_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at ASC"
        return [Mission.from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    # ── Tasks ─────────────────────────────────────────────────────────

    def task_create(self, mission_id: str, title: str, description: str = "",
                    priority: str = "medium", assignee: str = "",
                    depends_on: list = None) -> Task | None:
        mission = self.mission_get(mission_id)
        if not mission:
            return None
        now = _now()
        tid = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO tasks (id, mission_id, title, description, status, priority, "
            "assignee, depends_on, completion_ts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, '', ?, ?)",
            (tid, mission_id, title, description, priority, assignee,
             json.dumps(depends_on or []), now, now),
        )
        self._conn.commit()
        self._recompute_mission_counts(mission_id)
        return self.task_get(tid)

    def task_get(self, tid: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return Task.from_row(row) if row else None

    def task_update(self, tid: str, **kwargs) -> Task | None:
        now = _now()
        allowed = {"title", "description", "status", "priority", "assignee", "depends_on"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.task_get(tid)
        if "depends_on" in updates and isinstance(updates["depends_on"], (list, tuple)):
            updates["depends_on"] = json.dumps(updates["depends_on"])
        if kwargs.get("status") == "done" and "completion_ts" not in updates:
            updates["completion_ts"] = now
        updates["updated_at"] = now
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [tid]
        self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)
        self._conn.commit()
        task = self.task_get(tid)
        if task:
            self._recompute_mission_counts(task.mission_id)
        self._log_audit("task.update", tid, updates)
        return task

    def task_delete(self, tid: str) -> bool:
        task = self.task_get(tid)
        cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
        self._conn.commit()
        if task:
            self._recompute_mission_counts(task.mission_id)
        return cur.rowcount > 0

    def task_list(self, mission_id: str = None, status: str = None,
                  assignee: str = None) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if mission_id:
            sql += " AND mission_id=?"
            params.append(mission_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if assignee:
            sql += " AND assignee=?"
            params.append(assignee)
        sql += " ORDER BY created_at ASC"
        return [Task.from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    # ── Health Computation ────────────────────────────────────────────

    def recompute_mission_health(self, mid: str) -> Mission | None:
        mission = self.mission_get(mid)
        if not mission:
            return None
        tasks = self.task_list(mission_id=mid)
        total = len(tasks)
        done = sum(1 for t in tasks if t.status == "done")
        progress = (done / total * 100.0) if total > 0 else 0.0
        in_progress = sum(1 for t in tasks if t.status == "in_progress")

        if total == 0:
            health = Health.NONE.value
        elif done == total:
            health = Health.ON_TRACK.value
        elif in_progress > 0:
            health = Health.ON_TRACK.value
        elif progress > 0:
            health = Health.AT_RISK.value
        else:
            health = Health.OFF_TRACK.value

        self._conn.execute(
            "UPDATE missions SET progress=?, health=?, task_count=?, "
            "completed_task_count=?, updated_at=? WHERE id=?",
            (round(progress, 1), health, total, done, _now(), mid),
        )
        self._conn.commit()
        self._recompute_goal_counts(mission.goal_id)
        return self.mission_get(mid)

    def recompute_goal_health(self, gid: str) -> Goal | None:
        goal = self.goal_get(gid)
        if not goal:
            return None
        missions = self.mission_list(goal_id=gid)
        total = len(missions)
        if total == 0:
            self._conn.execute(
                "UPDATE goals SET health=?, progress=?, mission_count=0, "
                "completed_mission_count=0, updated_at=? WHERE id=?",
                (Health.NONE.value, 0.0, _now(), gid),
            )
            self._conn.commit()
            return self.goal_get(gid)

        total_progress = sum(m.progress for m in missions)
        avg_progress = total_progress / total
        done_count = sum(1 for m in missions if m.status == "done")
        active_count = sum(1 for m in missions if m.status == "in_progress")

        at_risk = sum(1 for m in missions if m.health == "at_risk")
        off_track = sum(1 for m in missions if m.health == "off_track")

        if off_track > 0:
            health = Health.OFF_TRACK.value
        elif at_risk > 0:
            health = Health.AT_RISK.value
        elif done_count == total:
            health = Health.ON_TRACK.value
        elif active_count > 0:
            health = Health.ON_TRACK.value
        else:
            health = Health.NONE.value

        self._conn.execute(
            "UPDATE goals SET health=?, progress=?, mission_count=?, "
            "completed_mission_count=?, updated_at=? WHERE id=?",
            (health, round(avg_progress, 1), total, done_count, _now(), gid),
        )
        self._conn.commit()
        return self.goal_get(gid)

    def _recompute_mission_counts(self, mid: str) -> None:
        self.recompute_mission_health(mid)

    def _recompute_goal_counts(self, gid: str) -> None:
        self.recompute_goal_health(gid)

    # ── Dashboard Integration ─────────────────────────────────────────

    def dashboard(self) -> dict:
        goals = self.goal_list()
        result = []
        for g in goals:
            gd = g.to_dict()
            missions = self.mission_list(goal_id=g.id)
            gd["missions"] = [m.to_dict() for m in missions]
            for m in gd["missions"]:
                tasks = self.task_list(mission_id=m["id"])
                m["tasks"] = [t.to_dict() for t in tasks]
            result.append(gd)
        return {
            "goals": result,
            "total_goals": len(goals),
            "active_goals": sum(1 for g in goals if g.status == "active"),
            "completed_goals": sum(1 for g in goals if g.status == "completed"),
        }

    # ── Audit ─────────────────────────────────────────────────────────

    def _log_audit(self, action: str, entity_id: str, details: dict) -> None:
        try:
            from services.audit import get_logger
            logger = get_logger()
            logger.log(
                action=action,
                agent="goals_service",
                tool="goals",
                arguments=details,
                result_summary=f"{action} {entity_id}",
            )
        except Exception:
            pass

    # ── Cleanup ───────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()


# ── Singleton ────────────────────────────────────────────────────────

_db: GoalsDB | None = None


def get_db() -> GoalsDB:
    global _db
    if _db is None:
        _db = GoalsDB()
    return _db

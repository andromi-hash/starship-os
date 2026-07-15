#!/usr/bin/env python3
"""
Starship OS — Cognitive Planning Layer (ReAct Pattern)

Adds explicit reasoning and planning before action. Implements the
ReAct (Reason + Act) pattern: the model produces structured Thought →
Action → Observation cycles before delivering a final Answer.

This is the "Cognitive Planning" layer from the architecture — sitting
between a raw user request and tool execution so every step is
deliberate, traceable, and self-evaluating.

Usage:
    python3 planner.py plan "Create a backup of all agent configs"
    python3 planner.py react "Monitor system health and alert on issues"
    python3 planner.py status                     # show active plans
    python3 planner.py status <plan_id>           # show one plan
    python3 planner.py history [--limit N]        # completed plans
    python3 planner.py serve                      # HTTP API server
"""

import sys
import os
import json
import time
import signal
import asyncio
import sqlite3
import logging
import logging.handlers
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

try:
    from aiohttp import web
except ImportError:
    web = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = SCRIPT_DIR.parent
AGENTS_DIR = PROJECT_ROOT / "agents"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_PLANNER_SUBJECT = "agnetic.planner.status"

_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "planner.db"

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "planner.log"

_pid_dir = Path("/var/run/agnetic")
if not os.access(_pid_dir, os.W_OK):
    _pid_dir = Path("/tmp/agnetic-data")
_pid_dir.mkdir(parents=True, exist_ok=True)
PID_FILE = _pid_dir / "planner.pid"

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_MAX_STEPS = 10
DEFAULT_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Structured Logger (evaluator / hitl pattern)
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "planner"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("planner")
    logger.setLevel(logging.INFO)
    fmt = JSONFormatter()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logging()


def _log(event: str, level: str = "info", details: dict | None = None):
    extra: dict[str, Any] = {"service": "planner", "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id          TEXT PRIMARY KEY,
            goal        TEXT NOT NULL,
            context     TEXT NOT NULL DEFAULT '{}',
            steps       TEXT NOT NULL DEFAULT '[]',
            current_step INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'planning',
            agent       TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            completed_at TEXT,
            answer      TEXT NOT NULL DEFAULT '',
            total_steps INTEGER NOT NULL DEFAULT 0,
            latency_ms  REAL NOT NULL DEFAULT 0.0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_agent ON plans(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created_at)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    id: int = 0
    thought: str = ""
    action: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    expected_outcome: str = ""
    status: str = "pending"  # pending / in_progress / completed / failed
    result: str = ""
    evaluation: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Plan:
    id: str = ""
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    context: dict = field(default_factory=dict)
    status: str = "planning"  # planning / executing / completed / failed
    agent: str = ""
    model: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    answer: str = ""
    total_steps: int = 0
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "current_step": self.current_step,
            "context": self.context,
            "status": self.status,
            "agent": self.agent,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "answer": self.answer,
            "total_steps": self.total_steps,
            "latency_ms": round(self.latency_ms, 1),
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Plan":
        steps_raw = json.loads(row["steps"])
        return cls(
            id=row["id"],
            goal=row["goal"],
            steps=[PlanStep.from_dict(s) for s in steps_raw],
            current_step=row["current_step"],
            context=json.loads(row["context"]),
            status=row["status"],
            agent=row["agent"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"] or "",
            answer=row["answer"],
            total_steps=row["total_steps"],
            latency_ms=row["latency_ms"],
        )


# ---------------------------------------------------------------------------
# Prompt Templates
# ---------------------------------------------------------------------------

PLAN_PROMPT = """You are a planning agent. Given a goal, create a step-by-step execution plan.

GOAL: {goal}
CONTEXT: {context}
AVAILABLE TOOLS: {tools}

Think through this carefully:
1. What information do I need?
2. What's the simplest path to the goal?
3. What could go wrong?
4. What are the dependencies between steps?

Create a plan with numbered steps. Each step should:
- Have a clear thought explaining WHY this step
- Specify which tool to use (or "reason" if it's a thinking step)
- List the exact arguments for the tool
- State the expected outcome

Be concise. Max {max_steps} steps. Prefer fewer steps.

Respond ONLY with a JSON array of steps:
[
  {{
    "thought": "why this step is needed",
    "action": "short description of the action",
    "tool": "tool_name or 'reason'",
    "args": {{"key": "value"}},
    "expected_outcome": "what we expect to happen"
  }},
  ...
]
"""

THINK_PROMPT = """You are executing a plan using the ReAct pattern.

GOAL: {goal}
CURRENT STEP: {step_id} of {total_steps}
STEP ACTION: {action}
STEP TOOL: {tool}
STEP ARGS: {args}

{observation_section}

Based on the current plan state and any observations, reason about what to do next.
If an observation shows an error or unexpected result, suggest a recovery action.

Respond ONLY with a JSON object:
{{
  "thought": "your reasoning about the current situation and what to do",
  "suggested_action": "the specific action to take or 'proceed' to follow the plan"
}}
"""

EVALUATE_PROMPT = """Evaluate whether a plan step achieved its expected outcome.

GOAL: {goal}
STEP ACTION: {action}
EXPECTED OUTCOME: {expected}
ACTUAL RESULT: {result}

Did this step succeed? Is the result satisfactory for progressing toward the goal?

Respond ONLY with a JSON object:
{{
  "success": true or false,
  "feedback": "brief explanation of the evaluation"
}}
"""

REPLAN_PROMPT = """The current plan encountered an issue. Revise the plan.

ORIGINAL GOAL: {goal}
COMPLETED STEPS: {completed}
CURRENT STEP (FAILED): {failed_step}
FAILURE REASON: {feedback}
REMAINING STEPS: {remaining}

Create a revised plan that:
1. Keeps the successfully completed steps
2. Addresses the failure
3. Completes the original goal

Respond ONLY with a JSON array of NEW remaining steps (not the completed ones):
[
  {{
    "thought": "why this step",
    "action": "what to do",
    "tool": "tool_name or 'reason'",
    "args": {{}},
    "expected_outcome": "what we expect"
  }},
  ...
]
"""


# ---------------------------------------------------------------------------
# Ollama Client
# ---------------------------------------------------------------------------


async def _ollama_generate(model: str, prompt: str, system: str = "") -> str:
    """Send a prompt to the local Ollama API and return the response text."""
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")


def _parse_json_response(text: str, fallback: Any = None) -> Any:
    """Best-effort extraction of a JSON object or array from LLM text."""
    text = text.strip()

    # Try direct parse first
    for opener, closer in [("[", "]"), ("{", "}")]:
        start = text.find(opener)
        if start < 0:
            continue
        # Find matching closer from the end
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue

    return fallback


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

# Minimal tool registry for planner context. Full execution delegates to
# the agent daemon's tool system, but the planner needs to know what tools
# exist and what they do.

BUILTIN_TOOLS: dict[str, str] = {
    "shell": "Execute a shell command and return its output",
    "read_file": "Read the contents of a file",
    "write_file": "Write content to a file",
    "list_dir": "List contents of a directory",
    "http_get": "Make an HTTP GET request",
    "http_post": "Make an HTTP POST request with JSON body",
    "search_files": "Search for files by name pattern or grep content",
    "delegate_to_agent": "Delegate a task to another agent",
    "opencode": "Invoke OpenCode AI coding agent for code generation",
    "opendesign": "Generate design artifacts using Open Design",
}


def list_available_tools(tool_names: list[str] | None = None) -> str:
    """Return a formatted string describing available tools."""
    tools = BUILTIN_TOOLS
    if tool_names:
        tools = {k: v for k, v in BUILTIN_TOOLS.items() if k in tool_names}
    return "\n".join(f"  - {name}: {desc}" for name, desc in tools.items())


# ---------------------------------------------------------------------------
# Tool Executor (delegates to agents/tools.py when available)
# ---------------------------------------------------------------------------


async def execute_tool_step(tool_name: str, args: dict, nats=None) -> dict:
    """Execute a tool by importing the agent tool system.

    Falls back to shell execution for basic tools when the agent tool
    system is not importable.
    """
    # Try importing the project's tool system
    try:
        sys.path.insert(0, str(AGENTS_DIR))
        from tools import execute_tool as _execute_tool
        return await _execute_tool(tool_name, args, nats=nats)
    except ImportError:
        pass

    # Fallback: basic shell execution
    if tool_name == "shell":
        cmd = args.get("command", "")
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        return {
            "output": stdout.decode(errors="replace")[:50000],
            "error_output": stderr.decode(errors="replace")[:50000],
            "exit_code": proc.returncode,
        }

    if tool_name == "read_file":
        path = args.get("path", "")
        try:
            content = Path(path).read_text(errors="replace")[:50000]
            return {"content": content, "error": False}
        except Exception as e:
            return {"content": str(e), "error": True}

    if tool_name == "list_dir":
        path = args.get("path", ".")
        try:
            entries = []
            for item in sorted(Path(path).iterdir()):
                entries.append({"name": item.name, "type": "dir" if item.is_dir() else "file"})
            return {"entries": entries, "error": False}
        except Exception as e:
            return {"entries": [], "error": True, "message": str(e)}

    return {"error": True, "message": f"Tool '{tool_name}' not available in fallback mode"}


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------


class PlannerAgent:
    """Generates and manages execution plans using the ReAct pattern."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.max_steps = DEFAULT_MAX_STEPS
        self.max_retries = DEFAULT_MAX_RETRIES

    # -- Plan Creation -------------------------------------------------------

    async def create_plan(self, goal: str, context: dict | None = None) -> Plan:
        """Generate an execution plan for a goal."""
        context = context or {}
        agent_name = context.get("agent", "")
        tool_names = context.get("tools")
        tools_str = list_available_tools(tool_names)

        prompt = PLAN_PROMPT.format(
            goal=goal,
            context=json.dumps(context, indent=2, default=str),
            tools=tools_str,
            max_steps=self.max_steps,
        )

        _log("plan_creation_started", details={"goal": goal[:200], "agent": agent_name})

        raw = await _ollama_generate(self.model, prompt)
        steps_raw = _parse_json_response(raw, [])

        if not isinstance(steps_raw, list):
            _log("plan_parse_fallback", level="warning", details={"raw_len": len(raw)})
            steps_raw = []

        steps: list[PlanStep] = []
        for i, s in enumerate(steps_raw):
            steps.append(PlanStep(
                id=i + 1,
                thought=s.get("thought", ""),
                action=s.get("action", ""),
                tool=s.get("tool", "reason"),
                args=s.get("args", {}),
                expected_outcome=s.get("expected_outcome", ""),
                status="pending",
            ))

        if not steps:
            # Fallback: single reasoning step
            steps = [PlanStep(
                id=1,
                thought="Direct approach — no multi-step plan needed",
                action=goal,
                tool="reason",
                args={},
                expected_outcome="Goal addressed directly",
                status="pending",
            )]

        plan = Plan(
            id=f"plan_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
            goal=goal,
            steps=steps,
            current_step=0,
            context=context,
            status="planning",
            agent=agent_name,
            model=self.model,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            total_steps=len(steps),
        )

        _save_plan(plan)

        _log("plan_created", details={
            "id": plan.id,
            "steps": len(steps),
            "goal": goal[:200],
        })

        return plan

    # -- Think (ReAct reasoning) --------------------------------------------

    async def think(self, plan: Plan, observation: str | None = None) -> tuple[str, str]:
        """Reason about the next action given current state and observations.

        Returns (thought, suggested_action).
        """
        current = plan.steps[plan.current_step] if plan.current_step < len(plan.steps) else None

        observation_section = ""
        if observation:
            observation_section = f"OBSERVATION FROM PREVIOUS STEP:\n{observation}\n"
        elif plan.current_step > 0:
            prev = plan.steps[plan.current_step - 1]
            observation_section = (
                f"PREVIOUS STEP RESULT:\n"
                f"  Action: {prev.action}\n"
                f"  Result: {prev.result[:1000]}\n"
                f"  Evaluation: {prev.evaluation}\n"
            )

        step_info = ""
        if current:
            step_info = (
                f"CURRENT STEP: {current.id}\n"
                f"ACTION: {current.action}\n"
                f"TOOL: {current.tool}\n"
                f"ARGS: {json.dumps(current.args, default=str)}\n"
            )

        prompt = THINK_PROMPT.format(
            goal=plan.goal,
            step_id=plan.current_step + 1,
            total_steps=len(plan.steps),
            action=current.action if current else "N/A",
            tool=current.tool if current else "N/A",
            args=json.dumps(current.args, default=str) if current else "{}",
            observation_section=observation_section,
        )

        raw = await _ollama_generate(self.model, prompt)
        parsed = _parse_json_response(raw, {})

        if isinstance(parsed, dict):
            thought = parsed.get("thought", raw[:500])
            action = parsed.get("suggested_action", "proceed")
        else:
            thought = raw[:500]
            action = "proceed"

        _log("think_completed", details={
            "plan_id": plan.id,
            "step": plan.current_step + 1,
            "thought_len": len(thought),
        })

        return thought, action

    # -- Evaluate Step -------------------------------------------------------

    async def evaluate_step(self, step: PlanStep) -> tuple[bool, str]:
        """Evaluate if a step achieved its expected outcome.

        Returns (success, feedback).
        """
        prompt = EVALUATE_PROMPT.format(
            goal="",
            action=step.action,
            expected=step.expected_outcome,
            result=step.result[:2000],
        )

        raw = await _ollama_generate(self.model, prompt)
        parsed = _parse_json_response(raw, {})

        if isinstance(parsed, dict):
            success = bool(parsed.get("success", False))
            feedback = parsed.get("feedback", "")
        else:
            # Heuristic fallback: if the result contains an error, fail it
            result_lower = step.result.lower()
            success = "error" not in result_lower and "failed" not in result_lower
            feedback = raw[:500]

        _log("step_evaluated", details={
            "step_id": step.id,
            "success": success,
            "feedback_len": len(feedback),
        })

        return success, feedback

    # -- Replan --------------------------------------------------------------

    async def replan(self, plan: Plan, feedback: str) -> Plan:
        """Revise the plan based on evaluation feedback."""
        completed_steps = [
            s for s in plan.steps if s.status == "completed"
        ]
        failed_step = plan.steps[plan.current_step] if plan.current_step < len(plan.steps) else None
        remaining_steps = [
            s for s in plan.steps[plan.current_step + 1:]
        ]

        completed_desc = "\n".join(
            f"  {s.id}. {s.action} → {s.evaluation}" for s in completed_steps
        ) or "  (none)"

        prompt = REPLAN_PROMPT.format(
            goal=plan.goal,
            completed=completed_desc,
            failed_step=f"{failed_step.action} (tool: {failed_step.tool})" if failed_step else "N/A",
            feedback=feedback,
            remaining=remaining_desc if remaining_steps else "(none)",
        )

        raw = await _ollama_generate(self.model, prompt)
        new_steps_raw = _parse_json_response(raw, [])

        if isinstance(new_steps_raw, list) and new_steps_raw:
            next_id = len(completed_steps) + 1
            new_steps: list[PlanStep] = []
            for i, s in enumerate(new_steps_raw):
                new_steps.append(PlanStep(
                    id=next_id + i,
                    thought=s.get("thought", ""),
                    action=s.get("action", ""),
                    tool=s.get("tool", "reason"),
                    args=s.get("args", {}),
                    expected_outcome=s.get("expected_outcome", ""),
                    status="pending",
                ))
            plan.steps = completed_steps + new_steps
            plan.current_step = len(completed_steps)
            plan.total_steps = len(plan.steps)

        plan.updated_at = datetime.now(timezone.utc).isoformat()
        _save_plan(plan)

        _log("plan_revised", details={
            "plan_id": plan.id,
            "new_step_count": len(plan.steps),
        })

        return plan


# ---------------------------------------------------------------------------
# ReAct Loop
# ---------------------------------------------------------------------------


async def react_loop(
    goal: str,
    available_tools: list[str] | None = None,
    agent_name: str = "",
    model: str = DEFAULT_MODEL,
) -> Plan:
    """Execute a goal using the full ReAct loop.

    1. Create plan
    2. For each step: think → execute → evaluate → (replan on failure)
    3. Synthesize final answer
    """
    planner = PlannerAgent(model=model)

    context: dict[str, Any] = {
        "agent": agent_name,
        "tools": available_tools,
    }

    plan = await planner.create_plan(goal, context)
    plan.status = "executing"
    _save_plan(plan)

    _log("react_loop_started", details={"plan_id": plan.id, "steps": len(plan.steps)})

    retries = 0

    for i in range(len(plan.steps)):
        plan.current_step = i
        step = plan.steps[i]
        step.status = "in_progress"
        _save_plan(plan)

        # Think about what to do
        observation = step.result if step.result else None
        thought, action = await planner.think(plan, observation)
        step.thought = thought

        # If the model suggests a different action, note it
        if action and action != "proceed":
            _log("react_suggestion", details={"plan_id": plan.id, "suggestion": action[:200]})

        # Execute the tool
        if step.tool and step.tool != "reason":
            try:
                result = await execute_tool_step(step.tool, step.args)
                step.result = json.dumps(result, default=str)[:50000]
            except Exception as e:
                step.result = json.dumps({"error": True, "message": str(e)})
                _log("tool_execution_error", level="error", details={
                    "plan_id": plan.id, "step": step.id, "tool": step.tool, "error": str(e),
                })
        else:
            # Reasoning step — no tool execution needed
            step.result = json.dumps({"reasoning": thought, "action": action})

        # Evaluate the result
        success, feedback = await planner.evaluate_step(step)

        if success:
            step.status = "completed"
            step.evaluation = f"PASS: {feedback}" if feedback else "PASS"
            retries = 0
        else:
            step.status = "failed"
            step.evaluation = f"FAIL: {feedback}"
            retries += 1

            _log("step_failed", level="warning", details={
                "plan_id": plan.id, "step": step.id, "feedback": feedback[:200],
            })

            if retries <= planner.max_retries:
                # Replan from this point
                plan = await planner.replan(plan, feedback)
                continue
            else:
                _log("max_retries_exceeded", level="error", details={"plan_id": plan.id})
                plan.status = "failed"
                plan.completed_at = datetime.now(timezone.utc).isoformat()
                _save_plan(plan)
                return plan

        _save_plan(plan)

        # Check if goal is achieved — if the model thinks we're done, stop early
        if _is_goal_achieved(plan):
            plan.status = "completed"
            plan.completed_at = datetime.now(timezone.utc).isoformat()
            _save_plan(plan)
            break
    else:
        # All steps executed
        plan.status = "completed"
        plan.completed_at = datetime.now(timezone.utc).isoformat()
        _save_plan(plan)

    # Synthesize final answer
    plan.answer = _synthesize_answer(plan)
    plan.updated_at = datetime.now(timezone.utc).isoformat()
    _save_plan(plan)

    _log("react_loop_completed", details={
        "plan_id": plan.id,
        "status": plan.status,
        "steps_completed": sum(1 for s in plan.steps if s.status == "completed"),
        "total_steps": len(plan.steps),
    })

    return plan


def _is_goal_achieved(plan: Plan) -> bool:
    """Heuristic: goal is achieved if all current steps are completed."""
    if not plan.steps:
        return False
    return all(s.status == "completed" for s in plan.steps)


def _synthesize_answer(plan: Plan) -> str:
    """Build a final answer from the plan execution history."""
    parts = [f"Goal: {plan.goal}\n"]

    completed = [s for s in plan.steps if s.status == "completed"]
    failed = [s for s in plan.steps if s.status == "failed"]

    parts.append(f"Completed {len(completed)}/{len(plan.steps)} steps.\n")

    for step in plan.steps:
        status_icon = "✓" if step.status == "completed" else "✗" if step.status == "failed" else "○"
        parts.append(f"  {status_icon} Step {step.id}: {step.action}")
        if step.thought:
            parts.append(f"    Thought: {step.thought[:200]}")
        if step.result:
            result_preview = step.result[:300]
            parts.append(f"    Result: {result_preview}")

    if failed:
        parts.append(f"\nFailed steps: {len(failed)}")
        for step in failed:
            parts.append(f"  - Step {step.id}: {step.evaluation[:200]}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Database Persistence
# ---------------------------------------------------------------------------


def _save_plan(plan: Plan):
    """Persist a plan to SQLite."""
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO plans "
            "(id, goal, context, steps, current_step, status, agent, model, "
            "created_at, updated_at, completed_at, answer, total_steps, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.id,
                plan.goal,
                json.dumps(plan.context, default=str),
                json.dumps([s.to_dict() for s in plan.steps], default=str),
                plan.current_step,
                plan.status,
                plan.agent,
                plan.model,
                plan.created_at,
                plan.updated_at,
                plan.completed_at,
                plan.answer,
                plan.total_steps,
                plan.latency_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_plan(plan_id: str) -> Plan | None:
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            return None
        return Plan.from_row(row)
    finally:
        conn.close()


def _list_plans(
    status: str = "",
    agent: str = "",
    limit: int = 50,
) -> list[Plan]:
    conn = _get_db()
    try:
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM plans{where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [Plan.from_row(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration Helper for Agent Daemons
# ---------------------------------------------------------------------------


async def handle_with_planning(
    agent_name: str,
    user_message: str,
    tools: list,
    model: str = DEFAULT_MODEL,
) -> str:
    """Handle a user message with explicit ReAct planning.

    Drop-in upgrade for agent_daemon's process_command when planning is
    desired. Returns the synthesized answer string.
    """
    tool_names = [t["function"]["name"] for t in tools] if tools else None

    plan = await react_loop(
        goal=user_message,
        available_tools=tool_names,
        agent_name=agent_name,
        model=model,
    )

    return plan.answer or f"Plan {plan.status}: {plan.goal}"


# ---------------------------------------------------------------------------
# HTTP API (evaluator / hitl pattern)
# ---------------------------------------------------------------------------


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def api_create_plan(request: web.Request) -> web.Response:
    """POST /api/planner/plan — create a new plan (does not execute)."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    goal = body.get("goal", "")
    if not goal:
        return _json_response({"error": "goal is required"}, 400)

    context = body.get("context", {})
    model = body.get("model", DEFAULT_MODEL)

    planner = PlannerAgent(model=model)
    plan = await planner.create_plan(goal, context)
    return _json_response({"status": "ok", "plan": plan.to_dict()})


async def api_execute_plan(request: web.Request) -> web.Response:
    """POST /api/planner/react — create and execute a plan via ReAct loop."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    goal = body.get("goal", "")
    if not goal:
        return _json_response({"error": "goal is required"}, 400)

    agent_name = body.get("agent", "")
    tools = body.get("tools", [])
    model = body.get("model", DEFAULT_MODEL)

    plan = await react_loop(
        goal=goal,
        available_tools=tools or None,
        agent_name=agent_name,
        model=model,
    )

    return _json_response({"status": "ok", "plan": plan.to_dict()})


async def api_get_plan(request: web.Request) -> web.Response:
    """GET /api/planner/plans/{id} — get a single plan."""
    plan_id = request.match_info.get("id", "")
    plan = _load_plan(plan_id)
    if not plan:
        return _json_response({"error": f"plan {plan_id} not found"}, 404)
    return _json_response({"status": "ok", "plan": plan.to_dict()})


async def api_list_plans(request: web.Request) -> web.Response:
    """GET /api/planner/plans — list plans with optional filters."""
    status = request.query.get("status", "")
    agent = request.query.get("agent", "")
    limit = int(request.query.get("limit", "50"))

    plans = _list_plans(status=status, agent=agent, limit=limit)
    return _json_response({
        "status": "ok",
        "count": len(plans),
        "plans": [p.to_dict() for p in plans],
    })


async def api_health(request: web.Request) -> web.Response:
    """GET /api/planner/health — health check."""
    return _json_response({
        "status": "healthy",
        "service": "planner",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": time.time() - request.app.get("start_time", time.time()),
    })


def build_app() -> web.Application:
    app = web.Application()
    app["start_time"] = time.time()
    app.router.add_post("/api/planner/plan", api_create_plan)
    app.router.add_post("/api/planner/react", api_execute_plan)
    app.router.add_get("/api/planner/plans/{id}", api_get_plan)
    app.router.add_get("/api/planner/plans", api_list_plans)
    app.router.add_get("/api/planner/health", api_health)
    return app


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_plan(args: list[str]):
    """CLI: plan <goal> [--model MODEL] [--agent AGENT] [--tools JSON]"""
    import argparse
    parser = argparse.ArgumentParser(description="Create an execution plan")
    parser.add_argument("goal", help="Goal to plan for")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model")
    parser.add_argument("--agent", default="", help="Agent name")
    parser.add_argument("--tools", default="", help="Available tools as JSON array")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    opts = parser.parse_args(args)

    tools = json.loads(opts.tools) if opts.tools else None
    context = {"agent": opts.agent, "tools": tools} if tools else {"agent": opts.agent}

    planner = PlannerAgent(model=opts.model)
    plan = asyncio.run(planner.create_plan(opts.goal, context))

    if opts.as_json:
        print(json.dumps(plan.to_dict(), indent=2, default=str))
    else:
        _print_plan(plan)


def cmd_react(args: list[str]):
    """CLI: react <goal> [--model MODEL] [--agent AGENT] [--tools JSON]"""
    import argparse
    parser = argparse.ArgumentParser(description="Execute a goal via ReAct loop")
    parser.add_argument("goal", help="Goal to achieve")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model")
    parser.add_argument("--agent", default="", help="Agent name")
    parser.add_argument("--tools", default="", help="Available tools as JSON array")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    opts = parser.parse_args(args)

    tools = json.loads(opts.tools) if opts.tools else None

    plan = asyncio.run(react_loop(
        goal=opts.goal,
        available_tools=tools,
        agent_name=opts.agent,
        model=opts.model,
    ))

    if opts.as_json:
        print(json.dumps(plan.to_dict(), indent=2, default=str))
    else:
        _print_plan(plan)
        if plan.answer:
            print(f"\n{'='*60}")
            print("ANSWER:")
            print(f"{'='*60}")
            print(plan.answer)


def cmd_status(args: list[str]):
    """CLI: status [plan_id] [--json]"""
    import argparse
    parser = argparse.ArgumentParser(description="Show plan status")
    parser.add_argument("plan_id", nargs="?", default="", help="Plan ID (optional)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    opts = parser.parse_args(args)

    if opts.plan_id:
        plan = _load_plan(opts.plan_id)
        if not plan:
            print(f"Plan not found: {opts.plan_id}", file=sys.stderr)
            sys.exit(1)
        if opts.as_json:
            print(json.dumps(plan.to_dict(), indent=2, default=str))
        else:
            _print_plan(plan)
    else:
        plans = _list_plans(limit=20)
        if not plans:
            print("No plans found.")
            return

        if opts.as_json:
            print(json.dumps([p.to_dict() for p in plans], indent=2, default=str))
        else:
            print(f"\n{'ID':<30} {'Status':<12} {'Steps':<8} {'Goal'}")
            print("-" * 80)
            for p in plans:
                goal_short = p.goal[:30] + ("..." if len(p.goal) > 30 else "")
                print(f"{p.id:<30} {p.status:<12} {len(p.steps):<8} {goal_short}")
            print(f"\n{len(plans)} plan(s)")


def cmd_history(args: list[str]):
    """CLI: history [--limit N] [--status S] [--agent A] [--json]"""
    import argparse
    parser = argparse.ArgumentParser(description="Show plan history")
    parser.add_argument("--limit", type=int, default=30, help="Max results")
    parser.add_argument("--status", default="", help="Filter by status")
    parser.add_argument("--agent", default="", help="Filter by agent")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    opts = parser.parse_args(args)

    plans = _list_plans(status=opts.status, agent=opts.agent, limit=opts.limit)

    if not plans:
        print("No plans found.")
        return

    if opts.as_json:
        print(json.dumps([p.to_dict() for p in plans], indent=2, default=str))
        return

    print(f"\n{'ID':<30} {'Status':<12} {'Agent':<12} {'Steps':<8} {'Goal'}")
    print("-" * 90)
    for p in plans:
        goal_short = p.goal[:25] + ("..." if len(p.goal) > 25 else "")
        agent_short = (p.agent or "-")[:10]
        print(f"{p.id:<30} {p.status:<12} {agent_short:<12} {len(p.steps):<8} {goal_short}")
    print(f"\n{len(plans)} plan(s)")


def cmd_serve(args: list[str]):
    """CLI: serve — start HTTP API server."""
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    host = os.getenv("PLANNER_HOST", "0.0.0.0")
    port = int(os.getenv("PLANNER_PORT", "8920"))

    _log("server_starting", details={"host": host, "port": port})

    app = build_app()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    def _shutdown(signum, _frame):
        _log("signal_received", details={"signal": signum})
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        web.run_app(app, host=host, port=port, print=None)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def _print_plan(plan: Plan):
    """Pretty-print a plan to stdout."""
    status_icons = {
        "completed": "✓",
        "failed": "✗",
        "in_progress": "►",
        "pending": "○",
    }

    print(f"\n{'='*60}")
    print(f"Plan: {plan.id}")
    print(f"Goal: {plan.goal}")
    print(f"Status: {plan.status}")
    print(f"Agent: {plan.agent or '(none)'}")
    print(f"Steps: {len(plan.steps)}")
    print(f"{'='*60}\n")

    for step in plan.steps:
        icon = status_icons.get(step.status, "?")
        print(f"  {icon} Step {step.id}: {step.action}")
        if step.thought:
            print(f"    Thought: {step.thought[:200]}")
        if step.tool and step.tool != "reason":
            print(f"    Tool: {step.tool}({json.dumps(step.args, default=str)[:100]})")
        if step.expected_outcome:
            print(f"    Expected: {step.expected_outcome[:150]}")
        if step.result:
            print(f"    Result: {step.result[:200]}")
        if step.evaluation:
            print(f"    Eval: {step.evaluation[:200]}")
        print()


def cmd_help():
    print("""\
Starship OS Cognitive Planning Layer (ReAct Pattern)

Usage:
  python3 planner.py <command> [args]

Commands:
  plan <goal>                     Create an execution plan (no execution)
    --model MODEL                 LLM model to use
    --agent NAME                  Agent name
    --tools JSON                  Available tools as JSON array
    --json                        Output as JSON

  react <goal>                    Execute a goal using the ReAct loop
    --model MODEL                 LLM model to use
    --agent NAME                  Agent name
    --tools JSON                  Available tools as JSON array
    --json                        Output as JSON

  status [plan_id]                Show active plan status
    --json                        Output as JSON

  history                         Show completed plans
    --limit N                     Max results (default 30)
    --status S                    Filter by status
    --agent A                     Filter by agent
    --json                        Output as JSON

  serve                           Start HTTP API server (port 8920)

  help                            Show this help message

API Endpoints (when running serve):
  POST /api/planner/plan          Create a plan (no execution)
  POST /api/planner/react         Create and execute a plan
  GET  /api/planner/plans         List plans
  GET  /api/planner/plans/{id}    Get a single plan
  GET  /api/planner/health        Health check
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    _get_db()  # ensure tables

    if len(sys.argv) < 2:
        cmd_help()
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]

    commands = {
        "plan": lambda: cmd_plan(rest),
        "react": lambda: cmd_react(rest),
        "status": lambda: cmd_status(rest),
        "history": lambda: cmd_history(rest),
        "serve": lambda: cmd_serve(rest),
        "help": lambda: cmd_help(),
    }

    handler = commands.get(command)
    if handler:
        handler()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        cmd_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Starship OS — Output Evaluator Agent

Separate evaluator that reviews agent output quality before delivery.
Implements the "Critic" pattern: Output Validation + Error Correction.

Usage:
    python3 evaluator.py evaluate --task "..." --output "..." --agent proxy
    python3 evaluator.py evaluate-tool --tool run_command --args '{}' --risk medium
    python3 evaluator.py history --limit 20
    python3 evaluator.py stats
    python3 evaluator.py serve                           # start HTTP API
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

try:
    import ollama as ollama_lib
except ImportError:
    ollama_lib = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("/etc/agnetic/evaluator.yaml")

_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "evaluator.db"

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "evaluator.log"

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_EVAL_SUBJECT = "agnetic.evaluator.result"

DEFAULT_CONFIG: dict[str, Any] = {
    "evaluator": {
        "enabled": True,
        "model": "qwen2.5:7b",
        "min_score": 3.0,
        "auto_fix": True,
        "max_retries": 2,
        "log_evaluations": True,
        "server": {"host": "0.0.0.0", "port": 8901},
        "dimensions": {
            "correctness": {"weight": 1.0, "min": 2},
            "completeness": {"weight": 1.0, "min": 2},
            "safety": {"weight": 1.5, "min": 3},
            "relevance": {"weight": 0.8, "min": 2},
            "efficiency": {"weight": 0.5, "min": 1},
        },
    }
}


# ---------------------------------------------------------------------------
# Evaluation Dimensions
# ---------------------------------------------------------------------------


class EvalDimensions:
    CORRECTNESS = "correctness"
    COMPLETENESS = "completeness"
    SAFETY = "safety"
    RELEVANCE = "relevance"
    EFFICIENCY = "efficiency"

    ALL = [CORRECTNESS, COMPLETENESS, SAFETY, RELEVANCE, EFFICIENCY]


# ---------------------------------------------------------------------------
# Structured Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "evaluator"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("evaluator")
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
    extra: dict[str, Any] = {"service": "evaluator", "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
            ev_cfg = cfg.get("evaluator", {})
            if ev_cfg:
                merged["evaluator"].update(ev_cfg)
            _log("config_loaded", details={"path": str(CONFIG_PATH)})
        except Exception as exc:
            _log("config_load_failed", level="warning", details={"error": str(exc)})
    return merged


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class DimensionScore:
    name: str
    score: int
    reasoning: str = ""
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": self.score,
            "reasoning": self.reasoning,
            "issues": self.issues,
        }


@dataclass
class EvalResult:
    id: str = ""
    task: str = ""
    output: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=list)
    dimensions: list[DimensionScore] = field(default_factory=list)
    weighted_score: float = 0.0
    verdict: str = "NEEDS_IMPROVEMENT"
    critical_issues: list[str] = field(default_factory=list)
    suggested_improvements: list[str] = field(default_factory=list)
    agent: str = ""
    model: str = ""
    evaluated_at: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task": self.task[:500],
            "output": self.output[:1000],
            "tool_calls": self.tool_calls,
            "context": self.context,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "weighted_score": round(self.weighted_score, 2),
            "verdict": self.verdict,
            "critical_issues": self.critical_issues,
            "suggested_improvements": self.suggested_improvements,
            "agent": self.agent,
            "model": self.model,
            "evaluated_at": self.evaluated_at,
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass
class ToolEvalResult:
    id: str = ""
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    risk_level: str = "low"
    agent: str = ""
    verdict: str = "approve"
    reasoning: str = ""
    suggested_changes: dict = field(default_factory=dict)
    evaluated_at: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "risk_level": self.risk_level,
            "agent": self.agent,
            "verdict": self.verdict,
            "reasoning": self.reasoning,
            "suggested_changes": self.suggested_changes,
            "evaluated_at": self.evaluated_at,
            "latency_ms": round(self.latency_ms, 1),
        }


# ---------------------------------------------------------------------------
# Prompt Templates
# ---------------------------------------------------------------------------

EVAL_PROMPT = """You are an evaluation agent reviewing the work of an AI assistant.

TASK: {task}

AGENT OUTPUT: {output}

TOOLS USED: {tool_calls}

Evaluate this output on these dimensions (1-5 scale):
1. CORRECTNESS: Is the output factually correct and accurate?
2. COMPLETENESS: Does it fully address the original task?
3. SAFETY: Are there any safety concerns with executing this?
4. RELEVANCE: Is the output relevant to the task?
5. EFFICIENCY: Could this be done more simply?

For each dimension, provide:
- Score (1-5)
- Reasoning
- Issues found

Also provide:
- Overall quality: PASS/NEEDS_IMPROVEMENT/REJECT
- Critical issues that must be fixed
- Suggested improvements

Respond in JSON format with this structure:
{{
  "dimensions": {{
    "correctness": {{"score": <1-5>, "reasoning": "...", "issues": [...]}},
    "completeness": {{"score": <1-5>, "reasoning": "...", "issues": [...]}},
    "safety": {{"score": <1-5>, "reasoning": "...", "issues": [...]}},
    "relevance": {{"score": <1-5>, "reasoning": "...", "issues": [...]}},
    "efficiency": {{"score": <1-5>, "reasoning": "...", "issues": [...]}}
  }},
  "verdict": "PASS" | "NEEDS_IMPROVEMENT" | "REJECT",
  "critical_issues": ["..."],
  "suggested_improvements": ["..."]
}}
"""

TOOL_EVAL_PROMPT = """You are a safety evaluator reviewing a tool call before execution.

TOOL: {tool_name}
ARGUMENTS: {arguments}
RISK LEVEL: {risk_level}
AGENT: {agent_name}

Evaluate:
1. Is this tool call appropriate for the task?
2. Are the arguments correct and safe?
3. Are there any concerns with the specific arguments?
4. Should this be approved, modified, or blocked?

Respond in JSON with:
{{
  "verdict": "approve" | "modify" | "block",
  "reasoning": "explanation",
  "suggested_changes": {{}}
}}
"""

IMPROVEMENT_PROMPT = """The following output was reviewed and needs improvement.

ORIGINAL TASK: {task}

ORIGINAL OUTPUT: {output}

ISSUES FOUND:
{issues}

DIMENSION SCORES:
{scores}

Suggest specific improvements to address each issue. Be concrete and actionable.
Respond with a JSON object containing:
{{
  "improvements": ["specific improvement 1", "specific improvement 2", ...],
  "revised_output": "a corrected version of the output if possible"
}}
"""


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
        CREATE TABLE IF NOT EXISTS evaluations (
            id          TEXT PRIMARY KEY,
            task        TEXT NOT NULL DEFAULT '',
            output      TEXT NOT NULL DEFAULT '',
            tool_calls  TEXT NOT NULL DEFAULT '[]',
            context     TEXT NOT NULL DEFAULT '{}',
            dimensions  TEXT NOT NULL DEFAULT '[]',
            weighted_score REAL NOT NULL DEFAULT 0.0,
            verdict     TEXT NOT NULL DEFAULT 'NEEDS_IMPROVEMENT',
            critical_issues    TEXT NOT NULL DEFAULT '[]',
            suggested_improvements TEXT NOT NULL DEFAULT '[]',
            agent       TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            evaluated_at TEXT NOT NULL,
            latency_ms  REAL NOT NULL DEFAULT 0.0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_evaluations (
            id          TEXT PRIMARY KEY,
            tool_name   TEXT NOT NULL,
            arguments   TEXT NOT NULL DEFAULT '{}',
            risk_level  TEXT NOT NULL DEFAULT 'low',
            agent       TEXT NOT NULL DEFAULT '',
            verdict     TEXT NOT NULL DEFAULT 'approve',
            reasoning   TEXT NOT NULL DEFAULT '',
            suggested_changes TEXT NOT NULL DEFAULT '{}',
            evaluated_at TEXT NOT NULL,
            latency_ms  REAL NOT NULL DEFAULT 0.0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_verdict ON evaluations(verdict)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_agent ON evaluations(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_ts ON evaluations(evaluated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_evals_verdict ON tool_evaluations(verdict)")
    conn.commit()
    return conn


def _save_evaluation(result: EvalResult):
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO evaluations "
            "(id, task, output, tool_calls, context, dimensions, weighted_score, "
            "verdict, critical_issues, suggested_improvements, agent, model, "
            "evaluated_at, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.id,
                result.task,
                result.output,
                json.dumps(result.tool_calls, default=str),
                json.dumps(result.context, default=str),
                json.dumps([d.to_dict() for d in result.dimensions], default=str),
                result.weighted_score,
                result.verdict,
                json.dumps(result.critical_issues, default=str),
                json.dumps(result.suggested_improvements, default=str),
                result.agent,
                result.model,
                result.evaluated_at,
                result.latency_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _save_tool_evaluation(result: ToolEvalResult):
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tool_evaluations "
            "(id, tool_name, arguments, risk_level, agent, verdict, "
            "reasoning, suggested_changes, evaluated_at, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.id,
                result.tool_name,
                json.dumps(result.arguments, default=str),
                result.risk_level,
                result.agent,
                result.verdict,
                result.reasoning,
                json.dumps(result.suggested_changes, default=str),
                result.evaluated_at,
                result.latency_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_evaluation(eval_id: str) -> EvalResult | None:
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (eval_id,)).fetchone()
        if not row:
            return None
        return _row_to_eval_result(row)
    finally:
        conn.close()


def _row_to_eval_result(row: sqlite3.Row) -> EvalResult:
    dims_raw = json.loads(row["dimensions"])
    dimensions = [
        DimensionScore(
            name=d["name"],
            score=d["score"],
            reasoning=d.get("reasoning", ""),
            issues=d.get("issues", []),
        )
        for d in dims_raw
    ]
    return EvalResult(
        id=row["id"],
        task=row["task"],
        output=row["output"],
        tool_calls=json.loads(row["tool_calls"]),
        context=json.loads(row["context"]),
        dimensions=dimensions,
        weighted_score=row["weighted_score"],
        verdict=row["verdict"],
        critical_issues=json.loads(row["critical_issues"]),
        suggested_improvements=json.loads(row["suggested_improvements"]),
        agent=row["agent"],
        model=row["model"],
        evaluated_at=row["evaluated_at"],
        latency_ms=row["latency_ms"],
    )


# ---------------------------------------------------------------------------
# Ollama Client Wrapper
# ---------------------------------------------------------------------------


class OllamaClient:
    """Thin async wrapper around ollama for generate calls."""

    def __init__(self, model: str = "qwen2.5:7b"):
        self.model = model

    async def generate(self, prompt: str) -> str:
        if ollama_lib is None:
            raise RuntimeError("ollama package not installed")
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ollama_lib.generate(model=self.model, prompt=prompt),
            )
            return resp.get("response", "")
        except Exception as exc:
            _log("ollama_generate_failed", level="error", details={"error": str(exc)})
            raise


# ---------------------------------------------------------------------------
# EvaluatorAgent
# ---------------------------------------------------------------------------


class EvaluatorAgent:
    """Reviews agent outputs before delivery."""

    def __init__(self, model: str = "qwen2.5:7b", config: dict | None = None):
        self.model = model
        self.config = config or {}
        self._client = OllamaClient(model)
        self._eval_config = self.config.get("evaluator", DEFAULT_CONFIG["evaluator"])
        self._dim_config = self._eval_config.get("dimensions", DEFAULT_CONFIG["evaluator"]["dimensions"])

    async def evaluate(
        self,
        task: str,
        output: str,
        tool_calls: list,
        context: dict,
    ) -> EvalResult:
        """Evaluate an agent's output across all dimensions."""
        eval_id = f"eval_{int(time.time() * 1000)}_{os.getpid()}"
        agent_name = context.get("agent", "unknown")
        start = time.monotonic()

        tool_calls_str = (
            json.dumps(tool_calls, indent=2, default=str) if tool_calls else "None"
        )
        prompt = EVAL_PROMPT.format(
            task=task,
            output=output,
            tool_calls=tool_calls_str,
        )

        _log(
            "evaluation_started",
            details={"id": eval_id, "agent": agent_name, "task_len": len(task)},
        )

        raw_response = await self._client.generate(prompt)

        latency_ms = (time.monotonic() - start) * 1000

        result = self._parse_eval_response(
            raw_response, task, output, tool_calls, context, eval_id, agent_name, latency_ms
        )

        _save_evaluation(result)

        _log(
            "evaluation_completed",
            details={
                "id": eval_id,
                "verdict": result.verdict,
                "score": round(result.weighted_score, 2),
                "latency_ms": round(latency_ms, 1),
            },
        )

        return result

    async def evaluate_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        risk_level: str,
        agent: str = "",
    ) -> ToolEvalResult:
        """Evaluate a specific tool call before execution."""
        eval_id = f"tool_eval_{int(time.time() * 1000)}_{os.getpid()}"
        start = time.monotonic()

        prompt = TOOL_EVAL_PROMPT.format(
            tool_name=tool_name,
            arguments=json.dumps(arguments, indent=2, default=str),
            risk_level=risk_level,
            agent_name=agent,
        )

        _log(
            "tool_evaluation_started",
            details={"id": eval_id, "tool": tool_name, "risk": risk_level},
        )

        raw_response = await self._client.generate(prompt)
        latency_ms = (time.monotonic() - start) * 1000

        parsed = self._parse_tool_eval_response(raw_response)

        result = ToolEvalResult(
            id=eval_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            agent=agent,
            verdict=parsed.get("verdict", "approve"),
            reasoning=parsed.get("reasoning", ""),
            suggested_changes=parsed.get("suggested_changes", {}),
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
        )

        _save_tool_evaluation(result)

        _log(
            "tool_evaluation_completed",
            details={"id": eval_id, "tool": tool_name, "verdict": result.verdict},
        )

        return result

    async def suggest_improvement(self, eval_result: EvalResult) -> str:
        """Suggest how to improve the output."""
        issues_list = eval_result.critical_issues + [
            issue
            for dim in eval_result.dimensions
            for issue in dim.issues
        ]
        scores_list = [
            f"{dim.name}: {dim.score}/5 — {dim.reasoning}"
            for dim in eval_result.dimensions
        ]

        prompt = IMPROVEMENT_PROMPT.format(
            task=eval_result.task,
            output=eval_result.output,
            issues="\n".join(f"- {i}" for i in issues_list),
            scores="\n".join(scores_list),
        )

        raw_response = await self._client.generate(prompt)

        try:
            # Extract JSON from the response
            json_start = raw_response.find("{")
            json_end = raw_response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(raw_response[json_start:json_end])
                improvements = parsed.get("improvements", [])
                revised = parsed.get("revised_output", "")
                parts = []
                if improvements:
                    parts.append("Suggested improvements:")
                    for imp in improvements:
                        parts.append(f"  - {imp}")
                if revised:
                    parts.append(f"\nRevised output:\n{revised}")
                return "\n".join(parts) if parts else raw_response
        except (json.JSONDecodeError, AttributeError):
            pass

        return raw_response

    # ------------------------------------------------------------------
    # Response Parsing
    # ------------------------------------------------------------------

    def _parse_eval_response(
        self,
        raw: str,
        task: str,
        output: str,
        tool_calls: list,
        context: dict,
        eval_id: str,
        agent_name: str,
        latency_ms: float,
    ) -> EvalResult:
        """Parse LLM evaluation response into EvalResult."""
        parsed: dict = {}
        try:
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(raw[json_start:json_end])
        except (json.JSONDecodeError, AttributeError):
            _log(
                "eval_parse_failed",
                level="warning",
                details={"id": eval_id, "raw_len": len(raw)},
            )

        dimensions: list[DimensionScore] = []
        dims_raw = parsed.get("dimensions", {})
        for dim_name in EvalDimensions.ALL:
            dim_data = dims_raw.get(dim_name, {})
            score = int(dim_data.get("score", 3)) if isinstance(dim_data.get("score"), (int, float, str)) else 3
            score = max(1, min(5, score))
            dimensions.append(
                DimensionScore(
                    name=dim_name,
                    score=score,
                    reasoning=dim_data.get("reasoning", ""),
                    issues=dim_data.get("issues", []),
                )
            )

        weighted_score = self._compute_weighted_score(dimensions)
        verdict = parsed.get("verdict", "NEEDS_IMPROVEMENT")
        if verdict not in ("PASS", "NEEDS_IMPROVEMENT", "REJECT"):
            verdict = self._score_to_verdict(weighted_score)

        critical_issues = parsed.get("critical_issues", [])
        suggested = parsed.get("suggested_improvements", [])

        # If safety is below minimum, force reject
        safety_dim = next((d for d in dimensions if d.name == EvalDimensions.SAFETY), None)
        if safety_dim and safety_dim.score < 2:
            verdict = "REJECT"
            if "Output has safety concerns" not in critical_issues:
                critical_issues.insert(0, "Output has safety concerns (score < 2)")

        return EvalResult(
            id=eval_id,
            task=task,
            output=output,
            tool_calls=tool_calls,
            context=context,
            dimensions=dimensions,
            weighted_score=weighted_score,
            verdict=verdict,
            critical_issues=critical_issues,
            suggested_improvements=suggested,
            agent=agent_name,
            model=self.model,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
        )

    def _parse_tool_eval_response(self, raw: str) -> dict:
        """Parse LLM tool evaluation response."""
        try:
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                return json.loads(raw[json_start:json_end])
        except (json.JSONDecodeError, AttributeError):
            pass
        return {"verdict": "approve", "reasoning": "Failed to parse evaluator response"}

    def _compute_weighted_score(self, dimensions: list[DimensionScore]) -> float:
        total_weight = 0.0
        weighted_sum = 0.0
        for dim in dimensions:
            dim_cfg = self._dim_config.get(dim.name, {})
            weight = dim_cfg.get("weight", 1.0)
            weighted_sum += dim.score * weight
            total_weight += weight
        return weighted_sum / total_weight if total_weight > 0 else 3.0

    def _score_to_verdict(self, score: float) -> str:
        min_score = self._eval_config.get("min_score", 3.0)
        if score >= min_score + 0.5:
            return "PASS"
        if score >= min_score - 0.5:
            return "NEEDS_IMPROVEMENT"
        return "REJECT"


# ---------------------------------------------------------------------------
# Evaluation Pipeline
# ---------------------------------------------------------------------------


async def evaluate_before_delivery(
    task: str,
    output: str,
    tool_calls: list,
    context: dict,
    config: dict | None = None,
) -> dict:
    """Main evaluation pipeline. Returns approval dict."""
    cfg = config or load_config()
    ev_cfg = cfg.get("evaluator", DEFAULT_CONFIG["evaluator"])

    if not ev_cfg.get("enabled", True):
        return {"approved": True, "output": output, "skipped": True}

    model = ev_cfg.get("model", "qwen2.5:7b")
    evaluator = EvaluatorAgent(model=model, config=cfg)

    result = await evaluator.evaluate(task, output, tool_calls, context)

    if result.verdict == "PASS":
        return {"approved": True, "output": output, "eval_result": result.to_dict()}

    if result.verdict == "NEEDS_IMPROVEMENT":
        if ev_cfg.get("auto_fix", True):
            improvement = await evaluator.suggest_improvement(result)
            return {
                "approved": False,
                "needs_revision": True,
                "feedback": improvement,
                "issues": result.critical_issues,
                "eval_result": result.to_dict(),
            }
        return {
            "approved": False,
            "needs_revision": True,
            "feedback": result.critical_issues + result.suggested_improvements,
            "issues": result.critical_issues,
            "eval_result": result.to_dict(),
        }

    if result.verdict == "REJECT":
        return {
            "approved": False,
            "needs_revision": True,
            "feedback": result.critical_issues,
            "blocked": True,
            "eval_result": result.to_dict(),
        }

    return {"approved": False, "needs_revision": True, "feedback": ["Unknown verdict"]}


async def generate_and_evaluate(
    prompt: str,
    tools: list,
    agent_name: str = "unknown",
    config: dict | None = None,
) -> dict:
    """Generate response, evaluate, optionally revise. For integration with agent daemons."""
    cfg = config or load_config()
    ev_cfg = cfg.get("evaluator", DEFAULT_CONFIG["evaluator"])
    model = ev_cfg.get("model", "qwen2.5:7b")
    max_retries = ev_cfg.get("max_retries", 2)

    if ollama_lib is None:
        raise RuntimeError("ollama package not installed")

    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ollama_lib.generate(model=model, prompt=prompt),
        )
        initial_output = resp.get("response", "")
        initial_tool_calls = resp.get("tool_calls", [])
    except Exception as exc:
        _log("generate_failed", level="error", details={"error": str(exc), "agent": agent_name})
        return {"error": str(exc)}

    eval_result = await evaluate_before_delivery(
        task=prompt,
        output=initial_output,
        tool_calls=initial_tool_calls,
        context={"agent": agent_name},
        config=cfg,
    )

    if eval_result.get("approved"):
        return {"response": initial_output, "tool_calls": initial_tool_calls, "eval": eval_result}

    if eval_result.get("needs_revision"):
        feedback = eval_result.get("feedback", "")
        if isinstance(feedback, list):
            feedback = "\n".join(str(f) for f in feedback)
        for attempt in range(max_retries):
            revised_prompt = (
                f"{prompt}\n\nPrevious attempt had issues:\n{feedback}\n\n"
                f"Please revise your response."
            )
            try:
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ollama_lib.generate(model=model, prompt=revised_prompt),
                )
                revised_output = resp.get("response", "")
                revised_tool_calls = resp.get("tool_calls", [])
            except Exception as exc:
                _log("revision_failed", level="warning", details={"attempt": attempt, "error": str(exc)})
                continue

            revised_eval = await evaluate_before_delivery(
                task=prompt,
                output=revised_output,
                tool_calls=revised_tool_calls,
                context={"agent": agent_name},
                config=cfg,
            )

            if revised_eval.get("approved"):
                return {
                    "response": revised_output,
                    "tool_calls": revised_tool_calls,
                    "eval": revised_eval,
                    "revised": True,
                    "revision_attempt": attempt + 1,
                }

            feedback = revised_eval.get("feedback", [])

        return {
            "response": initial_output,
            "tool_calls": initial_tool_calls,
            "eval": eval_result,
            "revision_failed": True,
        }

    return {"error": "Output blocked by evaluator", "reason": eval_result.get("feedback"), "eval": eval_result}


# ---------------------------------------------------------------------------
# Stats & History
# ---------------------------------------------------------------------------


def get_evaluation_history(limit: int = 50, agent: str = "", verdict: str = "") -> list[dict]:
    conn = _get_db()
    try:
        clauses: list[str] = []
        params: list = []
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if verdict:
            clauses.append("verdict = ?")
            params.append(verdict)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM evaluations{where} ORDER BY evaluated_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_evaluation_stats() -> dict:
    conn = _get_db()
    try:
        total = conn.execute("SELECT COUNT(*) as cnt FROM evaluations").fetchone()["cnt"]
        by_verdict = dict(
            conn.execute(
                "SELECT verdict, COUNT(*) as cnt FROM evaluations GROUP BY verdict"
            ).fetchall()
        )
        by_agent = dict(
            conn.execute(
                "SELECT agent, COUNT(*) as cnt FROM evaluations GROUP BY agent ORDER BY cnt DESC"
            ).fetchall()
        )
        avg_score = conn.execute(
            "SELECT AVG(weighted_score) as avg_score FROM evaluations"
        ).fetchone()["avg_score"]
        tool_total = conn.execute("SELECT COUNT(*) as cnt FROM tool_evaluations").fetchone()["cnt"]
        tool_by_verdict = dict(
            conn.execute(
                "SELECT verdict, COUNT(*) as cnt FROM tool_evaluations GROUP BY verdict"
            ).fetchall()
        )
        newest = conn.execute(
            "SELECT evaluated_at FROM evaluations ORDER BY evaluated_at DESC LIMIT 1"
        ).fetchone()
        return {
            "total_evaluations": total,
            "by_verdict": by_verdict,
            "by_agent": by_agent,
            "average_score": round(avg_score, 2) if avg_score else 0,
            "tool_evaluations": tool_total,
            "tool_evaluations_by_verdict": tool_by_verdict,
            "newest": newest["evaluated_at"] if newest else None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP API Server
# ---------------------------------------------------------------------------


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def api_evaluate(request: web.Request) -> web.Response:
    """POST /api/evaluator/evaluate — manually evaluate text."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    task = body.get("task", "")
    output = body.get("output", "")
    tool_calls = body.get("tool_calls", [])
    context = body.get("context", {})

    if not task or not output:
        return _json_response({"error": "task and output are required"}, 400)

    result = await evaluate_before_delivery(task, output, tool_calls, context)
    return _json_response(result)


async def api_evaluate_tool(request: web.Request) -> web.Response:
    """POST /api/evaluator/evaluate-tool — evaluate a tool call."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    tool_name = body.get("tool_name", "")
    arguments = body.get("arguments", {})
    risk_level = body.get("risk_level", "low")
    agent = body.get("agent", "")

    if not tool_name:
        return _json_response({"error": "tool_name is required"}, 400)

    evaluator = EvaluatorAgent()
    result = await evaluator.evaluate_tool_call(tool_name, arguments, risk_level, agent)
    return _json_response(result.to_dict())


async def api_history(request: web.Request) -> web.Response:
    """GET /api/evaluator/history — evaluation history."""
    limit = int(request.query.get("limit", "50"))
    agent = request.query.get("agent", "")
    verdict = request.query.get("verdict", "")
    rows = get_evaluation_history(limit, agent, verdict)
    return _json_response({"evaluations": rows, "count": len(rows)})


async def api_stats(request: web.Request) -> web.Response:
    """GET /api/evaluator/stats — pass/fail rates."""
    stats = get_evaluation_stats()
    return _json_response(stats)


async def api_health(request: web.Request) -> web.Response:
    """GET /api/evaluator/health — health check."""
    return _json_response({
        "status": "healthy",
        "service": "evaluator",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": time.time() - request.app.get("start_time", time.time()),
    })


def build_app(cfg: dict) -> web.Application:
    app = web.Application()
    app["config"] = cfg
    app["start_time"] = time.time()
    app.router.add_post("/api/evaluator/evaluate", api_evaluate)
    app.router.add_post("/api/evaluator/evaluate-tool", api_evaluate_tool)
    app.router.add_get("/api/evaluator/history", api_history)
    app.router.add_get("/api/evaluator/stats", api_stats)
    app.router.add_get("/api/evaluator/health", api_health)
    return app


# ---------------------------------------------------------------------------
# NATS Status Publisher
# ---------------------------------------------------------------------------


async def _publish_eval_result(result: EvalResult):
    """Publish evaluation result to NATS for observability."""
    try:
        import nats as nats_mod
        nc = await nats_mod.connect(NATS_URL)
        payload = {
            "id": result.id,
            "verdict": result.verdict,
            "score": result.weighted_score,
            "agent": result.agent,
            "timestamp": result.evaluated_at,
        }
        await nc.publish(NATS_EVAL_SUBJECT, json.dumps(payload, default=str).encode())
        await nc.close()
    except Exception as exc:
        _log("nats_publish_failed", level="warning", details={"error": str(exc)})


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_evaluate(args: list[str]):
    """CLI: evaluate --task T --output O [--agent A] [--tool-calls JSON]"""
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate agent output")
    parser.add_argument("--task", required=True, help="Original task/prompt")
    parser.add_argument("--output", required=True, help="Agent output to evaluate")
    parser.add_argument("--agent", default="unknown", help="Agent name")
    parser.add_argument("--tool-calls", default="[]", help="JSON array of tool calls")
    parser.add_argument("--model", default="", help="Override model")
    opts = parser.parse_args(args)

    cfg = load_config()
    model = opts.model or cfg.get("evaluator", {}).get("model", "qwen2.5:7b")
    evaluator = EvaluatorAgent(model=model, config=cfg)

    tool_calls = json.loads(opts.tool_calls) if opts.tool_calls else []
    context = {"agent": opts.agent}

    result = asyncio.run(evaluator.evaluate(opts.task, opts.output, tool_calls, context))

    print(json.dumps(result.to_dict(), indent=2, default=str))


def cmd_evaluate_tool(args: list[str]):
    """CLI: evaluate-tool --tool NAME --args JSON [--risk LEVEL] [--agent A]"""
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate a tool call")
    parser.add_argument("--tool", required=True, help="Tool name")
    parser.add_argument("--args", default="{}", help="JSON arguments")
    parser.add_argument("--risk", default="low", help="Risk level (low/medium/high/critical)")
    parser.add_argument("--agent", default="", help="Agent name")
    parser.add_argument("--model", default="", help="Override model")
    opts = parser.parse_args(args)

    cfg = load_config()
    model = opts.model or cfg.get("evaluator", {}).get("model", "qwen2.5:7b")
    evaluator = EvaluatorAgent(model=model, config=cfg)

    arguments = json.loads(opts.args) if opts.args else {}

    result = asyncio.run(
        evaluator.evaluate_tool_call(opts.tool, arguments, opts.risk, opts.agent)
    )

    print(json.dumps(result.to_dict(), indent=2, default=str))


def cmd_history(args: list[str]):
    """CLI: history [--limit N] [--agent A] [--verdict V] [--json]"""
    import argparse
    parser = argparse.ArgumentParser(description="Show evaluation history")
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    parser.add_argument("--agent", default="", help="Filter by agent")
    parser.add_argument("--verdict", default="", help="Filter by verdict")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    opts = parser.parse_args(args)

    rows = get_evaluation_history(opts.limit, opts.agent, opts.verdict)

    if opts.as_json:
        print(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        print("No evaluations found.")
        return

    print(f"{'ID':<30} {'Agent':<12} {'Verdict':<18} {'Score':<8} {'At'}")
    print("-" * 90)
    for r in rows:
        vid = r["id"][:28]
        agent = r.get("agent", "")[:10]
        verdict = r.get("verdict", "")[:16]
        score = f"{r.get('weighted_score', 0):.1f}"
        at = r.get("evaluated_at", "")[:19]
        print(f"{vid:<30} {agent:<12} {verdict:<18} {score:<8} {at}")
    print(f"\n{len(rows)} evaluation(s)")


def cmd_stats(args: list[str]):
    """CLI: stats [--json]"""
    import argparse
    parser = argparse.ArgumentParser(description="Show evaluation statistics")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    opts = parser.parse_args(args)

    s = get_evaluation_stats()

    if opts.as_json:
        print(json.dumps(s, indent=2))
        return

    print(f"\nEvaluator Statistics")
    print(f"  Total evaluations:  {s['total_evaluations']}")
    print(f"  Average score:      {s['average_score']}")
    print(f"  Tool evaluations:   {s['tool_evaluations']}")
    print(f"  Newest:             {s['newest'] or 'N/A'}")

    if s["by_verdict"]:
        print(f"\n  By Verdict:")
        for verdict, cnt in s["by_verdict"].items():
            print(f"    {verdict:<20} {cnt}")

    if s["by_agent"]:
        print(f"\n  By Agent:")
        for agent, cnt in s["by_agent"].items():
            print(f"    {agent:<20} {cnt}")

    if s["tool_evaluations_by_verdict"]:
        print(f"\n  Tool Evaluations By Verdict:")
        for verdict, cnt in s["tool_evaluations_by_verdict"].items():
            print(f"    {verdict:<20} {cnt}")
    print()


def cmd_serve(args: list[str]):
    """CLI: serve — start HTTP API server."""
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    ev_cfg = cfg.get("evaluator", {})
    server_cfg = ev_cfg.get("server", {"host": "0.0.0.0", "port": 8901})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8901)

    _log("server_starting", details={"host": host, "port": port})

    app = build_app(cfg)
    web.run_app(app, host=host, port=port, print=None)


def cmd_help():
    print("""\
Starship OS Output Evaluator Agent

Usage:
  python3 evaluator.py <command> [args]

Commands:
  evaluate                        Evaluate agent output quality
    --task TEXT                     Original task (required)
    --output TEXT                   Agent output (required)
    --agent NAME                    Agent name
    --tool-calls JSON               Tool calls as JSON array
    --model MODEL                   Override model

  evaluate-tool                   Evaluate a tool call before execution
    --tool NAME                     Tool name (required)
    --args JSON                     Arguments as JSON
    --risk LEVEL                    Risk level (low/medium/high/critical)
    --agent NAME                    Agent name

  history                         Show evaluation history
    --limit N                       Max results (default 20)
    --agent NAME                    Filter by agent
    --verdict V                     Filter by verdict
    --json                          Output as JSON

  stats                           Show pass/fail rates
    --json                          Output as JSON

  serve                           Start HTTP API server

  help                            Show this help message

API Endpoints (when running serve):
  POST /api/evaluator/evaluate       Manually evaluate text
  POST /api/evaluator/evaluate-tool  Evaluate a tool call
  GET  /api/evaluator/history        Evaluation history
  GET  /api/evaluator/stats          Pass/fail rates
  GET  /api/evaluator/health         Health check
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
        "evaluate": lambda: cmd_evaluate(rest),
        "evaluate-tool": lambda: cmd_evaluate_tool(rest),
        "history": lambda: cmd_history(rest),
        "stats": lambda: cmd_stats(rest),
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

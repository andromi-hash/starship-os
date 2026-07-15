#!/usr/bin/env python3
"""Starship OS Dashboard Server — Hermes WebUI-inspired command & control."""

import os
import sys
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import http.server
import json
import os
import threading
import time
import queue
import urllib.parse
import re
import html as htmlmod
import random
import string
import subprocess
import sys
import io
import gzip
from functools import wraps
from datetime import datetime, timezone

PORT = int(os.environ.get("AGNETIC_DASHBOARD_PORT", 8788))
PASSWORD = os.environ.get("AGNETIC_DASHBOARD_PASSWORD", None)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
START_TIME = time.time()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}

# ── Mock / system data ──────────────────────────────────────────────────────

AGENTS = [
    {
        "name": "agnetic-core",
        "status": "online",
        "model": "gemini-2.5-pro",
        "uptime": 3600 * 72,
        "capabilities": ["orchestration", "policy", "memory", "planning"],
        "version": "2.1.0",
        "skills": ["policy_check", "memory_audit", "task_decomposition"],
        "last_active": "2026-07-14T10:30:00Z",
        "config": {"temperature": 0.2, "max_tokens": 8192, "model": "gemini-2.5-pro"},
    },
    {
        "name": "agnetic-coder",
        "status": "online",
        "model": "claude-4-opus",
        "uptime": 3600 * 48,
        "capabilities": ["code_gen", "code_review", "refactoring", "testing"],
        "version": "2.1.0",
        "skills": ["code_review", "test_gen", "refactor"],
        "last_active": "2026-07-14T10:28:00Z",
        "config": {"temperature": 0.1, "max_tokens": 16384, "model": "claude-4-opus"},
    },
    {
        "name": "agnetic-secops",
        "status": "busy",
        "model": "gpt-4.1",
        "uptime": 3600 * 24,
        "capabilities": ["scanning", "incident_response", "threat_intel"],
        "version": "2.0.5",
        "skills": ["secret_scan", "incident_respond", "policy_check"],
        "last_active": "2026-07-14T10:25:00Z",
        "config": {"temperature": 0.0, "max_tokens": 4096, "model": "gpt-4.1"},
    },
    {
        "name": "agnetic-data",
        "status": "offline",
        "model": "gemini-2.5-flash",
        "uptime": 0,
        "capabilities": ["analysis", "visualization", "reporting"],
        "version": "2.0.5",
        "skills": ["data_analysis", "chart_gen", "summarize"],
        "last_active": "2026-07-13T22:00:00Z",
        "config": {"temperature": 0.3, "max_tokens": 8192, "model": "gemini-2.5-flash"},
    },
]

INCIDENTS = [
    {"id": "INC-001", "severity": "critical", "status": "open", "title": "Memory leak in agnetic-core orchestrator", "description": "Working memory heap growing unbounded. Potential OOM within 4h.", "source": "agnetic-core", "created": "2026-07-14T08:00:00Z", "updated": "2026-07-14T09:15:00Z", "assigned_runbook": "rb-system-recovery"},
    {"id": "INC-002", "severity": "high", "status": "open", "title": "Failed policy sync across agents", "description": "Policy version mismatch between agnetic-core and agnetic-secops.", "source": "agnetic-core", "created": "2026-07-14T09:00:00Z", "updated": "2026-07-14T09:30:00Z", "assigned_runbook": "rb-policy-violation"},
    {"id": "INC-003", "severity": "medium", "status": "in_progress", "title": "Deprecated skill detected in agent configs", "description": "Skill 'legacy_parser' v0.9 used by agnetic-coder — EOL 2026-06-30.", "source": "agnetic-secops", "created": "2026-07-13T14:00:00Z", "updated": "2026-07-14T08:00:00Z", "assigned_runbook": "rb-policy-violation"},
    {"id": "INC-004", "severity": "low", "status": "open", "title": "Certificate expiring for service account ci-bot", "description": "TLS cert for ci-bot expires in 14 days. Renew required.", "source": "agnetic-secops", "created": "2026-07-12T10:00:00Z", "updated": "2026-07-13T10:00:00Z", "assigned_runbook": None},
]

POLICY = {
    "system": {
        "allow_network_access": True,
        "max_memory_mb": 4096,
        "allowed_models": ["gemini-2.5-pro", "gemini-2.5-flash", "claude-4-opus", "gpt-4.1"],
        "log_level": "info",
        "audit_enabled": True,
    },
    "service": {
        "rate_limit_per_min": 60,
        "max_concurrent_tasks": 10,
        "allowed_commands": ["read", "write", "execute", "search", "analyze"],
        "blocked_paths": ["/etc/shadow", "/root/.ssh"],
        "agent_timeout_seconds": 300,
    },
    "user": {
        "override_policy": False,
        "custom_rules": [
            {"id": "rule-1", "action": "block", "pattern": "rm -rf /", "enabled": True},
            {"id": "rule-2", "action": "allow", "pattern": "git push", "enabled": True},
        ],
        "allowed_agents": ["agnetic-core", "agnetic-coder", "agnetic-secops", "agnetic-data"],
    },
}

MEMORY_TYPES = [
    {"type": "ephemeral", "count": 47, "description": "Short-lived task context, cleared after completion"},
    {"type": "working", "count": 12, "description": "Active session state across subtasks"},
    {"type": "procedural", "count": 156, "description": "Learned workflows, tool usage patterns, skills"},
    {"type": "semantic", "count": 892, "description": "Factual knowledge, concepts, relationships"},
    {"type": "episodic", "count": 334, "description": "Past interactions, decisions, outcomes"},
    {"type": "associative", "count": 201, "description": "Cross-reference links between memory items"},
    {"type": "reflective", "count": 28, "description": "Self-analysis, performance reviews, meta-cognition"},
]

SKILLS = [
    {"name": "code_review", "description": "Review source code for bugs, style, and security issues", "version": "2.1.0", "agents": ["agnetic-coder"]},
    {"name": "policy_check", "description": "Check commands and operations against active policy", "version": "2.1.0", "agents": ["agnetic-core", "agnetic-secops"]},
    {"name": "memory_audit", "description": "Audit memory stores for consistency and cleanup", "version": "2.0.0", "agents": ["agnetic-core"]},
    {"name": "secret_scan", "description": "Scan text and files for secrets, API keys, tokens", "version": "2.0.5", "agents": ["agnetic-secops"]},
    {"name": "incident_respond", "description": "Execute incident response runbooks automatically", "version": "2.0.5", "agents": ["agnetic-secops"]},
    {"name": "task_decomposition", "description": "Break complex tasks into subtasks for parallel execution", "version": "2.1.0", "agents": ["agnetic-core"]},
    {"name": "test_gen", "description": "Generate unit and integration tests for code", "version": "2.1.0", "agents": ["agnetic-coder"]},
    {"name": "refactor", "description": "Refactor code with safety guarantees", "version": "2.1.0", "agents": ["agnetic-coder"]},
    {"name": "data_analysis", "description": "Analyze structured and unstructured data", "version": "2.0.5", "agents": ["agnetic-data"]},
    {"name": "chart_gen", "description": "Generate data visualizations and charts", "version": "2.0.5", "agents": ["agnetic-data"]},
]

RUNBOOKS = [
    {
        "id": "rb-security-incident",
        "name": "Security Incident Response",
        "description": "Standard response procedure for security incidents",
        "steps": [
            {"order": 1, "title": "Isolate affected system", "status": "pending", "description": "Disconnect from network and stop non-critical services"},
            {"order": 2, "title": "Collect forensic data", "status": "pending", "description": "Gather logs, memory dump, disk image"},
            {"order": 3, "title": "Analyze scope", "status": "pending", "description": "Determine what systems/data were accessed"},
            {"order": 4, "title": "Contain threat", "status": "pending", "description": "Remove access, rotate keys, patch vector"},
            {"order": 5, "title": "Recover services", "status": "pending", "description": "Restore from clean backup, verify integrity"},
            {"order": 6, "title": "Post-mortem", "status": "pending", "description": "Document findings, update procedures"},
        ],
    },
    {
        "id": "rb-system-recovery",
        "name": "System Recovery",
        "description": "Recover from system-level failures",
        "steps": [
            {"order": 1, "title": "Assess damage", "status": "in_progress", "description": "Check all agent health and service status"},
            {"order": 2, "title": "Stop affected agents", "status": "pending", "description": "Gracefully stop non-responsive agents"},
            {"order": 3, "title": "Clear corrupted state", "status": "pending", "description": "Reset working memory, clear stale locks"},
            {"order": 4, "title": "Restart from checkpoint", "status": "pending", "description": "Restore from last known good state"},
            {"order": 5, "title": "Verify operation", "status": "pending", "description": "Run smoke tests on all agents"},
        ],
    },
    {
        "id": "rb-policy-violation",
        "name": "Policy Violation",
        "description": "Respond to policy violations or drifts",
        "steps": [
            {"order": 1, "title": "Identify violation", "status": "done", "description": "Determine which policy rule was violated"},
            {"order": 2, "title": "Contain scope", "status": "in_progress", "description": "Restrict agent permissions temporarily"},
            {"order": 3, "title": "Reconcile policy", "status": "pending", "description": "Update policy or fix agent configuration"},
            {"order": 4, "title": "Verify compliance", "status": "pending", "description": "Run policy check across all agents"},
            {"order": 5, "title": "Document and notify", "status": "pending", "description": "Log the incident and notify stakeholders"},
        ],
    },
]

TELEMETRY_EVENTS = []
for i in range(50):
    types = ["agent_start", "agent_stop", "task_start", "task_complete", "task_fail", "policy_check", "memory_access", "skill_invoke", "chat_message", "scan_result"]
    agents = ["agnetic-core", "agnetic-coder", "agnetic-secops", "agnetic-data"]
    TELEMETRY_EVENTS.append({
        "id": f"evt-{1000 + i}",
        "type": random.choice(types),
        "agent": random.choice(agents),
        "message": f"Sample telemetry event {i}",
        "duration_ms": random.randint(10, 5000),
        "timestamp": (datetime.now(timezone.utc).timestamp() - random.randint(0, 3600)) * 1000,
    })
TELEMETRY_EVENTS.sort(key=lambda x: x["timestamp"], reverse=True)

SERVICE_ACCOUNTS = [
    {"id": "sa-1", "name": "ci-bot", "role": "ci/cd", "created": "2026-01-15T00:00:00Z", "last_used": "2026-07-14T10:00:00Z", "status": "active", "permissions": ["read", "execute"]},
    {"id": "sa-2", "name": "deploy-bot", "role": "deployment", "created": "2026-02-01T00:00:00Z", "last_used": "2026-07-13T18:00:00Z", "status": "active", "permissions": ["read", "write", "execute"]},
    {"id": "sa-3", "name": "audit-bot", "role": "audit", "created": "2026-03-10T00:00:00Z", "last_used": "2026-07-14T09:30:00Z", "status": "active", "permissions": ["read"]},
]

CHAT_SESSIONS = {}

GOALS = [
    {"id": "goal-1", "title": "System Stability", "description": "Maintain 99.9% agent uptime across the mesh", "status": "active", "progress": 87, "owner": "agnetic-core", "deadline": "2026-08-01T00:00:00Z", "milestones": [{"title": "All agents reporting", "done": True}, {"title": "Auto-heal <30s", "done": True}, {"title": "Zero critical incidents", "done": False}]},
    {"id": "goal-2", "title": "Security Posture", "description": "Scan all tool outputs for secrets and block policy violations", "status": "active", "progress": 65, "owner": "agnetic-secops", "deadline": "2026-07-20T00:00:00Z", "milestones": [{"title": "Droid Shield deployed", "done": True}, {"title": "Policy engine live", "done": True}, {"title": "All endpoints scanned", "done": False}]},
    {"id": "goal-3", "title": "Code Quality", "description": "Review all PRs with automated code analysis", "status": "active", "progress": 42, "owner": "agnetic-coder", "deadline": "2026-08-15T00:00:00Z", "milestones": [{"title": "Code review tool active", "done": True}, {"title": "Test coverage >80%", "done": False}, {"title": "Style compliance check", "done": False}]},
    {"id": "goal-4", "title": "User Onboarding", "description": "Complete onboarding flow with wizard and first-run experience", "status": "planned", "progress": 0, "owner": "agnetic-core", "deadline": "2026-09-01T00:00:00Z", "milestones": [{"title": "Install script ready", "done": True}, {"title": "Wizard flow built", "done": True}, {"title": "Dashboard auto-launch", "done": False}]},
]

SYSTEM_LOG = [
    {"timestamp": "2026-07-14T10:30:00Z", "level": "info", "message": "Agent agnetic-coder completed task task-0421"},
    {"timestamp": "2026-07-14T10:29:30Z", "level": "info", "message": "Policy check passed for agnetic-core command exec-0392"},
    {"timestamp": "2026-07-14T10:28:00Z", "level": "warn", "message": "Memory usage on agnetic-core at 78% of limit"},
    {"timestamp": "2026-07-14T10:25:00Z", "level": "error", "message": "Task task-0419 failed on agnetic-data: timeout exceeded"},
    {"timestamp": "2026-07-14T10:20:00Z", "level": "info", "message": "Secret scan completed: 0 secrets found in 12 files"},
    {"timestamp": "2026-07-14T10:15:00Z", "level": "info", "message": "Incident INC-003 updated to in_progress"},
    {"timestamp": "2026-07-14T10:00:00Z", "level": "warn", "message": "Certificate check: ci-bot cert expires in 14 days"},
    {"timestamp": "2026-07-14T09:45:00Z", "level": "info", "message": "System health check passed — all services nominal"},
    {"timestamp": "2026-07-14T09:30:00Z", "level": "info", "message": "Runbook rb-system-recovery step 1 completed"},
    {"timestamp": "2026-07-14T09:15:00Z", "level": "error", "message": "Incident INC-001 escalated to critical"},
]

try:
    from services.agent_email import get_email_service
    _email_service = get_email_service()
except Exception:
    _email_service = None

try:
    from services.agent_discovery import discover_agents
except Exception:
    discover_agents = None

try:
    from services.healer import get_healer
    _healer = get_healer()
except Exception:
    _healer = None

SHIELD_STATS = {
    "total_scans": 1247,
    "secrets_found": 23,
    "false_positives": 4,
    "last_scan": "2026-07-14T10:20:00Z",
    "types_found": {"api_key": 12, "password": 5, "token": 3, "private_key": 2, "aws_key": 1},
    "scans_by_severity": {"critical": 3, "high": 8, "medium": 7, "low": 5},
}

# ── Helpers ────────────────────────────────────────────────────────────────

def json_bytes(obj, status=200):
    data = json.dumps(obj).encode("utf-8")
    return status, data, "application/json; charset=utf-8"

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }

def get_system_info():
    cpu = memory = disk = {"pct": 0, "total": 0, "used": 0, "free": 0}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            cpu = {"pct": float(parts[0]) * 10, "total": 100, "used": float(parts[0]) * 10, "free": 100 - float(parts[0]) * 10}
    except Exception:
        cpu = {"pct": random.uniform(10, 60), "total": 100, "used": random.uniform(10, 60), "free": 100 - random.uniform(10, 60)}
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
            total = int(lines[0].split()[1]) // 1024
            free = int(lines[1].split()[1]) // 1024
            used = total - free
            memory = {"pct": round(used / total * 100, 1), "total_mb": total, "used_mb": used, "free_mb": free}
    except Exception:
        memory = {"pct": 45.2, "total_mb": 8192, "used_mb": 3700, "free_mb": 4492}
    try:
        s = os.statvfs("/")
        total = s.f_frsize * s.f_blocks
        free = s.f_frsize * s.f_bfree
        used = total - free
        disk = {"pct": round(used / total * 100, 1) if total else 0, "total_gb": round(total / (1024**3), 1), "used_gb": round(used / (1024**3), 1), "free_gb": round(free / (1024**3), 1)}
    except Exception:
        disk = {"pct": 62.3, "total_gb": 100, "used_gb": 62.3, "free_gb": 37.7}
    return {"cpu": cpu, "memory": memory, "disk": disk, "uptime_seconds": time.time() - START_TIME}

def get_agent_status(name):
    for a in AGENTS:
        if a["name"] == name:
            return a
    return None

def auth_required(handler):
    def check(self, *args, **kwargs):
        if PASSWORD:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != PASSWORD:
                self._send_json({"error": "unauthorized"}, 401)
                return
        return handler(self, *args, **kwargs)
    return check

# ── Request Handler ────────────────────────────────────────────────────────

class AgneticDashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        try:
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def _serve_static(self, path):
        if not path or path == "/":
            path = "/index.html"
        filepath = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not filepath.startswith(os.path.normpath(STATIC_DIR)):
            self._send_json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(filepath):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(filepath)[1].lower()
        mime = MIME_TYPES.get(ext, "application/octet-stream")
        try:
            with open(filepath, "rb") as f:
                data = f.read()
        except Exception:
            self._send_json({"error": "internal error"}, 500)
            return
        self.send_response(200)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)

        if PASSWORD and path.startswith("/api/"):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != PASSWORD:
                self._send_json({"error": "unauthorized"}, 401)
                return

        # API routes
        if path == "/api/health":
            sysinfo = get_system_info()
            agent_counts = {"total": len(AGENTS), "online": sum(1 for a in AGENTS if a["status"] == "online"), "busy": sum(1 for a in AGENTS if a["status"] == "busy"), "offline": sum(1 for a in AGENTS if a["status"] == "offline")}
            open_incidents = [i for i in INCIDENTS if i["status"] != "resolved"]
            self._send_json({"status": "healthy", "version": "2.1.0", "uptime_seconds": time.time() - START_TIME, "agents": agent_counts, "system": sysinfo, "incidents": {"open": len(open_incidents), "by_severity": {s: sum(1 for i in open_incidents if i["severity"] == s) for s in ["critical", "high", "medium", "low"]}}})

        elif path == "/api/agents":
            if discover_agents:
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    live = loop.run_until_complete(discover_agents())
                    if live:
                        self._send_json({"agents": live, "source": "nats"})
                        return
                except Exception:
                    pass
                finally:
                    loop.close()
            self._send_json({"agents": AGENTS, "source": "mock"})

        elif path.startswith("/api/agent/") and path.endswith("/stream"):
            name = path.split("/")[3]
            session_id = query.get("session", [None])[0]
            if not session_id or session_id not in CHAT_SESSIONS:
                self._send_json({"error": "invalid session"}, 400)
                return
            session = CHAT_SESSIONS[session_id]
            self.send_response(200)
            for k, v in cors_headers().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                try:
                    msg = session["queue"].get(timeout=30)
                except queue.Empty:
                    self._send_sse("timeout", {})
                    break
                if msg.get("type") == "done":
                    self._send_sse("done", msg)
                    break
                elif msg.get("type") == "error":
                    self._send_sse("error", msg)
                    break
                elif msg.get("type") == "tool_call":
                    self._send_sse("tool_call", msg)
                elif msg.get("type") == "tool_result":
                    self._send_sse("tool_result", msg)
                else:
                    self._send_sse("message", msg)

        elif path.startswith("/api/agent/"):
            name = path.split("/")[3]
            agent = get_agent_status(name)
            if not agent:
                self._send_json({"error": "agent not found"}, 404)
            else:
                recent_activity = [e for e in TELEMETRY_EVENTS if e["agent"] == name][:10]
                self._send_json({"agent": agent, "recent_activity": recent_activity})

        elif path == "/api/policy":
            self._send_json({"policy": POLICY})

        elif path == "/api/memory":
            self._send_json({"types": MEMORY_TYPES})

        elif path == "/api/skills":
            self._send_json({"skills": SKILLS})

        elif path == "/api/incidents":
            status_filter = query.get("status", [None])[0]
            if status_filter:
                filtered = [i for i in INCIDENTS if i["status"] == status_filter]
            else:
                filtered = INCIDENTS
            self._send_json({"incidents": filtered})

        elif path == "/api/runbooks":
            self._send_json({"runbooks": RUNBOOKS})

        elif path == "/api/shield/stats":
            self._send_json({"stats": SHIELD_STATS})

        elif path == "/api/telemetry/stats":
            types = {}
            for e in TELEMETRY_EVENTS:
                types[e["type"]] = types.get(e["type"], 0) + 1
            by_agent = {}
            for e in TELEMETRY_EVENTS:
                by_agent[e["agent"]] = by_agent.get(e["agent"], 0) + 1
            self._send_json({"stats": {"total_events": len(TELEMETRY_EVENTS), "by_type": types, "by_agent": by_agent, "time_range_hours": 1}})

        elif path == "/api/telemetry/recent":
            limit = min(int(query.get("limit", [50])[0]), 200)
            self._send_json({"events": TELEMETRY_EVENTS[:limit]})

        elif path == "/api/email/addresses":
            if _email_service:
                addresses = _email_service.list_addresses()
                self._send_json({"addresses": [a.to_dict() for a in addresses]})
            else:
                self._send_json({"addresses": [], "error": "email service not available"})

        elif path == "/api/healer":
            if _healer:
                self._send_json({"summary": _healer.summary(), "agents": [{"name": k, **v.__dict__} for k, v in _healer._agents.items()], "recent_recoveries": [r.__dict__ for r in _healer.get_recovery_history(5)]})
            else:
                self._send_json({"summary": {"total_agents": 0, "alive": 0, "stalled_or_error": 0, "recoveries_performed": 0}, "agents": [], "recent_recoveries": []})

        elif path == "/api/orgchart":
            agents_list = []
            if discover_agents:
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    agents_list = loop.run_until_complete(discover_agents())
                except Exception:
                    agents_list = AGENTS
                finally:
                    loop.close()
            else:
                agents_list = AGENTS
            org = []
            for a in agents_list:
                node = {"name": a.get("name", ""), "status": a.get("status", "unknown"), "model": a.get("model", ""), "capabilities": a.get("capabilities", []), "children": []}
                org.append(node)
            self._send_json({"org": org, "goals": GOALS})

        elif path == "/api/accounts":
            self._send_json({"accounts": SERVICE_ACCOUNTS})

        elif path == "/api/system/logs":
            limit = min(int(query.get("limit", [50])[0]), 200)
            level_filter = query.get("level", [None])[0]
            logs = SYSTEM_LOG
            if level_filter:
                logs = [l for l in logs if l["level"] == level_filter]
            self._send_json({"logs": logs[:limit]})

        elif path == "/api/monitoring/disk":
            self._send_json({"disk": get_system_info()["disk"]})

        elif path == "/api/monitoring/cpu":
            info = get_system_info()
            self._send_json({"cpu": info["cpu"], "memory": info["memory"]})

        else:
            self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if PASSWORD:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != PASSWORD:
                self._send_json({"error": "unauthorized"}, 401)
                return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b"{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, 400)
            return

        if path.endswith("/chat"):
            name = path.split("/")[3]
            agent = get_agent_status(name)
            if not agent:
                self._send_json({"error": "agent not found"}, 404)
                return
            if agent["status"] == "offline":
                self._send_json({"error": "agent is offline"}, 503)
                return
            session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
            q = queue.Queue()
            CHAT_SESSIONS[session_id] = {"queue": q, "agent": name, "created": time.time()}
            message = data.get("message", "")
            threading.Thread(target=self._process_chat, args=(name, session_id, message, data.get("model", agent["model"])), daemon=True).start()
            self._send_json({"session_id": session_id, "agent": name, "status": "started"})

        elif path == "/api/policy":
            if "rules" in data:
                POLICY["user"]["custom_rules"] = data["rules"]
            if "allowed_agents" in data:
                POLICY["user"]["allowed_agents"] = data["allowed_agents"]
            if "override_policy" in data:
                POLICY["user"]["override_policy"] = data["override_policy"]
            self._send_json({"policy": POLICY, "status": "updated"})

        elif path == "/api/policy/check":
            command = data.get("command", "")
            blocked = False
            reasons = []
            for rule in POLICY["user"]["custom_rules"]:
                if rule["enabled"] and rule["action"] == "block" and re.search(rule["pattern"], command):
                    blocked = True
                    reasons.append(f"Blocked by rule '{rule['id']}': pattern '{rule['pattern']}'")
            for bp in POLICY["service"]["blocked_paths"]:
                if bp in command:
                    blocked = True
                    reasons.append(f"Blocked path '{bp}' in command")
            if command and not any(cmd in command for cmd in POLICY["service"]["allowed_commands"]):
                blocked = True
                reasons.append("Command type not in allowed commands")
            self._send_json({"blocked": blocked, "reasons": reasons, "command": command})

        elif path == "/api/memory/search":
            query_text = data.get("query", "")
            memory_type = data.get("type", None)
            results = []
            for mt in MEMORY_TYPES:
                if memory_type and mt["type"] != memory_type:
                    continue
                for i in range(min(mt["count"], 5)):
                    results.append({
                        "type": mt["type"],
                        "id": f"mem-{mt['type']}-{i}",
                        "content": f"Sample {mt['type']} memory entry #{i} matching '{query_text}'" if query_text else f"Sample {mt['type']} memory entry #{i}",
                        "confidence": round(random.uniform(0.5, 1.0), 2),
                        "created": (datetime.now(timezone.utc).timestamp() - random.randint(0, 86400)) * 1000,
                    })
            self._send_json({"results": results, "total": len(results), "query": query_text})

        elif path == "/api/incidents/resolve":
            incident_id = data.get("id", "")
            for inc in INCIDENTS:
                if inc["id"] == incident_id:
                    inc["status"] = "resolved"
                    inc["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    self._send_json({"incident": inc, "status": "resolved"})
                    return
            self._send_json({"error": "incident not found"}, 404)

        elif path == "/api/shield/scan":
            text = data.get("text", "")
            results = []
            patterns = {
                "api_key": r"[Aa][Pp][Ii]_?[Kk][Ee][Yy].{0,5}['\"]?[A-Za-z0-9_\-]{16,}",
                "password": r"[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd].{0,5}['\"]?.{6,}",
                "token": r"[Tt][Oo][Kk][Ee][Nn].{0,5}['\"]?[A-Za-z0-9_\-]{16,}",
                "aws_key": r"AKIA[0-9A-Z]{16}",
                "private_key": r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
            }
            for name, pattern in patterns.items():
                matches = re.findall(pattern, text)
                if matches:
                    results.append({"type": name, "count": len(matches), "severity": "high" if name in ("password", "private_key") else "medium", "sample": matches[0][:30] + "..." if len(matches[0]) > 30 else matches[0]})
            self._send_json({"results": results, "found": len(results) > 0, "scanned_length": len(text)})

        elif path == "/api/accounts":
            name = data.get("name", "")
            role = data.get("role", "custom")
            if not name:
                self._send_json({"error": "name required"}, 400)
                return
            new_account = {
                "id": f"sa-{len(SERVICE_ACCOUNTS) + 1}",
                "name": name,
                "role": role,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "last_used": None,
                "status": "active",
                "permissions": data.get("permissions", ["read"]),
            }
            SERVICE_ACCOUNTS.append(new_account)
            self._send_json({"account": new_account, "status": "created"})

        elif path == "/api/accounts/revoke":
            account_id = data.get("id", "")
            for acc in SERVICE_ACCOUNTS:
                if acc["id"] == account_id:
                    acc["status"] = "revoked"
                    self._send_json({"account": acc, "status": "revoked"})
                    return
            self._send_json({"error": "account not found"}, 404)

        elif path == "/api/email/send":
            if not _email_service:
                self._send_json({"error": "email service not available"}, 503)
                return
            to = data.get("to", "")
            subject = data.get("subject", "")
            body = data.get("body", "")
            mode = data.get("mode", "smtp")
            if not to or not subject or not body:
                self._send_json({"error": "'to', 'subject', and 'body' are required"}, 400)
                return
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_email_service.send_email(to, subject, body, mode=mode))
            finally:
                loop.close()
            self._send_json({"id": result.id, "status": result.status, "error": result.error})

        elif path == "/api/email/register":
            if not _email_service:
                self._send_json({"error": "email service not available"}, 503)
                return
            agent = data.get("agent", "")
            address = data.get("address", "")
            smtp = bool(data.get("smtp_enabled", True))
            if not agent or not address:
                self._send_json({"error": "'agent' and 'address' are required"}, 400)
                return
            result = _email_service.register_agent_address(agent, address, smtp_enabled=smtp)
            self._send_json({"agent": result.agent_name, "address": result.email_address, "status": "registered"})

        elif path == "/api/email/remove":
            if not _email_service:
                self._send_json({"error": "email service not available"}, 503)
                return
            agent = data.get("agent", "")
            if not agent:
                self._send_json({"error": "'agent' is required"}, 400)
                return
            ok = _email_service.remove_address(agent)
            self._send_json({"agent": agent, "removed": ok})

        else:
            self._send_json({"error": "not found"}, 404)

    def _process_chat(self, agent_name, session_id, message, model):
        q = CHAT_SESSIONS[session_id]["queue"]
        time.sleep(0.5)
        q.put({"type": "message", "content": f"Processing request on {agent_name} ({model})...", "agent": agent_name})
        time.sleep(1.0)

        # Simulate tool calls
        if "scan" in message.lower() or "secret" in message.lower():
            q.put({"type": "tool_call", "tool": "secret_scan", "status": "running", "preview": "Scanning for secrets in message content"})
            time.sleep(1.5)
            q.put({"type": "tool_result", "tool": "secret_scan", "status": "complete", "result": "No secrets found in message."})
        elif "policy" in message.lower() or "check" in message.lower():
            q.put({"type": "tool_call", "tool": "policy_check", "status": "running", "preview": "Checking message against active policy rules"})
            time.sleep(1.2)
            q.put({"type": "tool_result", "tool": "policy_check", "status": "complete", "result": "Policy check passed. No rules violated."})
        elif "memory" in message.lower() or "remember" in message.lower():
            q.put({"type": "tool_call", "tool": "memory_audit", "status": "running", "preview": "Searching memory stores for relevant context"})
            time.sleep(1.8)
            q.put({"type": "tool_result", "tool": "memory_audit", "status": "complete", "result": "Found 3 relevant memory entries. Confidence: 0.85"})
        elif "code" in message.lower() or "review" in message.lower():
            q.put({"type": "tool_call", "tool": "code_review", "status": "running", "preview": "Analyzing code structure and patterns"})
            time.sleep(2.0)
            q.put({"type": "tool_result", "tool": "code_review", "status": "complete", "result": "Code review complete: 2 style issues, 1 potential bug found."})

        time.sleep(0.5)
        responses = {
            "agnetic-core": f"I've processed your request through the Agnetic orchestration pipeline. The operation has been validated against policy and is proceeding normally.\n\n```\nStatus: OK\nAgent: {agent_name}\nModel: {model}\n```\n\nIs there anything else you'd like me to coordinate?",
            "agnetic-coder": f"I've analyzed the request and generated the appropriate code artifacts. All tests pass and the code follows current style guidelines.\n\n```python\n# Generated solution is ready for review\ndef process():\n    return \"Implementation complete\"\n```\n\nWould you like me to explain the implementation in more detail?",
            "agnetic-secops": f"Security scan complete. I've checked the requested operation against current threat intelligence feeds and policy rules.\n\n- Policy check: ✅ Passed\n- Threat analysis: ✅ No risks detected\n- Compliance: ✅ All requirements met\n\nOperation is safe to proceed.",
            "agnetic-data": f"I've analyzed the available data and prepared a summary. The dataset contains relevant information aligned with your request.\n\n- Records analyzed: 47\n- Key insights: 3\n- Confidence: 0.92\n\nWould you like to dive deeper into any specific finding?",
        }
        response = responses.get(agent_name, f"Received and processed your message on {agent_name}. The operation completed successfully.")
        q.put({"type": "message", "content": response, "agent": agent_name})
        q.put({"type": "done", "content": "Response complete"})


# ── Server ─────────────────────────────────────────────────────────────────

class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AgneticDashboardHandler)
    print(f"agnetic dashboard running on http://0.0.0.0:{PORT}")
    if PASSWORD:
        print(f" auth: bearer token required")
    print(f" static: {STATIC_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()

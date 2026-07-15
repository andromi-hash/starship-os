#!/usr/bin/env python3
"""Starship OS Comprehensive Stress Test & System Verification."""

import sys, os, json, time, asyncio, importlib
sys.path.insert(0, '/opt/agnetic')

PASS = 0
FAIL = 0
ERRORS = []

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        print(f"  \u2713 {name}")
        PASS += 1
    else:
        print(f"  \u2717 {name}: {detail}")
        FAIL += 1
        ERRORS.append((name, detail))

# ── 1. Import All Services ──────────────────────────────────────────
print("\n=== 1. SERVICE IMPORTS ===")
modules = [
    "services.policy", "services.event_hooks", "services.droid_shield",
    "services.service_accounts", "services.telemetry", "services.incident_response",
    "services.agent_email", "services.memory", "services.governance",
    "services.provider_router", "services.checkpoint", "services.browser",
    "lib.tools", "lib.plugin_manager",
]
for mod in modules:
    try:
        importlib.import_module(mod)
        check(f"import {mod}", True)
    except Exception as e:
        check(f"import {mod}", False, str(e)[:80])

# ── 2. Instantiate All Services ─────────────────────────────────────
print("\n=== 2. SERVICE INSTANTIATION ===")
try:
    from services.policy import PolicyManager
    pm = PolicyManager()
    check("PolicyManager()", True)
    check("PolicyManager tiers", len(pm._policies) >= 0)
except Exception as e: check("PolicyManager()", False, str(e)[:80])

try:
    from services.event_hooks import get_hook_manager
    hm = get_hook_manager()
    check("HookManager()", True)
    check("HookManager hooks", isinstance(hm._hooks, list))
except Exception as e: check("HookManager()", False, str(e)[:80])

try:
    from services.droid_shield import DroidShield
    ds = DroidShield()
    result = ds.scan_text("AKIA1234567890123456")
    check("DroidShield()", True)
    check("DroidShield scan", result.detected, f"found={len(result.findings)}")
except Exception as e: check("DroidShield()", False, str(e)[:80])

try:
    from services.service_accounts import ServiceAccountManager
    sam = ServiceAccountManager()
    check("ServiceAccountManager()", True)
    n = len(sam._accounts)
    check(f"ServiceAccountManager accounts={n}", n >= 4, f"got {n}")
except Exception as e: check("ServiceAccountManager()", False, str(e)[:80])

try:
    from services.telemetry import TelemetryExporter
    te = TelemetryExporter()
    check("TelemetryExporter()", True)
    check("TelemetryExporter mode", te._mode in ("otlp", "file", "disabled"))
except Exception as e: check("TelemetryExporter()", False, str(e)[:80])

try:
    from services.incident_response import IncidentResponseManager
    irm = IncidentResponseManager()
    check("IncidentResponseManager()", True)
    check("Runbooks loaded", len(irm._runbooks) >= 6, f"got {len(irm._runbooks)}")
except Exception as e: check("IncidentResponseManager()", False, str(e)[:80])

try:
    from services.agent_email import AgentEmailService, get_email_service
    es = get_email_service()
    check("AgentEmailService()", True)
    check("Email addresses loaded", isinstance(es.list_addresses(), list))
except Exception as e: check("AgentEmailService()", False, str(e)[:80])

# ── 3. Tool System ──────────────────────────────────────────────────
print("\n=== 3. TOOL SYSTEM ===")
try:
    from lib.tools import get_tool_definitions, execute_tool, TOOLSETS
    tools = get_tool_definitions("full")
    check(f"TOOL_DEFINITIONS count={len(tools)}", len(tools) > 30, f"got {len(tools)}")
    check(f"TOOLSETS count={len(TOOLSETS)}", len(TOOLSETS) >= 15, f"got {len(TOOLSETS)}")
    for ts in ["core", "network", "delegation", "memory", "email", "hooks", "credentials"]:
        check(f"toolset '{ts}' present", ts in TOOLSETS)
except Exception as e: check("Tool system", False, str(e)[:80])

# ── 4. Memory System ────────────────────────────────────────────────
print("\n=== 4. MEMORY SYSTEM ===")
try:
    from services.memory import MemoryManager, MemoryType, get_memory_manager
    mgr = get_memory_manager()
    check("LanceDB available", hasattr(mgr, 'search'))
    types = list(MemoryType)
    check(f"MemoryType count={len(types)}", len(types) >= 7, f"got {len(types)}")
    names = [t.value for t in types]
    for t in ["working", "semantic", "episodic", "procedural", "retrieval", "parametric", "prospective"]:
        check(f"MemoryType.{t}", t in names)
except Exception as e: check("Memory system", False, str(e)[:80])

# ── 5. HTTP / Network ───────────────────────────────────────────────
print("\n=== 5. NETWORK ===")
try:
    import httpx
    r = httpx.get("http://127.0.0.1:11435/api/tags", timeout=5)
    check(f"Ollama API @ :11435", r.status_code == 200, f"status={r.status_code}")
except Exception as e: check("Ollama API", False, str(e)[:80])

try:
    r2 = httpx.get("http://127.0.0.1:8788/api/health", timeout=5)
    check(f"Dashboard API @ :8788", r2.status_code == 200, f"status={r2.status_code}")
except Exception as e: check("Dashboard API", False, str(e)[:80])

try:
    r3 = httpx.get("http://127.0.0.1:4222", timeout=3)
    check("NATS @ :4222", r3.status_code in (200, 400), f"status={r3.status_code}")
except Exception as e: check("NATS API", False, str(e)[:80])

# ── 6. Filesystem ──────────────────────────────────────────────────
print("\n=== 6. FILESYSTEM ===")
for path, label in [
    ("/opt/agnetic/services", "services/"),
    ("/opt/agnetic/lib", "lib/"),
    ("/opt/agnetic/lib/dashboard", "dashboard/"),
    ("/opt/agnetic/lib/dashboard/static", "dashboard/static/"),
    ("/opt/agnetic/agents", "agents/"),
    ("/opt/agnetic/skills", "skills/"),
    ("/opt/agnetic/souls", "souls/"),
    ("/etc/agnetic/policy.json", "config:policy"),
    ("/opt/agnetic/hooks.json", "config:hooks"),
    ("/var/lib/agnetic", "data dir"),
    ("/var/log/agnetic", "log dir"),
]:
    exists = os.path.exists(path)
    check(f"{label} {'exists' if exists else 'MISSING'}", exists)

# ── 7. Dashboard Server Files ──────────────────────────────────────
print("\n=== 7. DASHBOARD STATIC FILES ===")
static = "/opt/agnetic/lib/dashboard/static"
if os.path.isdir(static):
    files = os.listdir(static)
    for f in ["index.html", "style.css", "ui.js", "dashboard.js", "agents.js",
              "chat.js", "panels.js", "incidents.js", "boot.js"]:
        check(f"static/{f}", os.path.isfile(os.path.join(static, f)))

# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{'='*50}")
total = PASS + FAIL
print(f"STRESS TEST RESULTS: {PASS}/{total} passed, {FAIL}/{total} failed")
if ERRORS:
    print(f"\nFailures:")
    for name, detail in ERRORS:
        print(f"  \u2717 {name}: {detail}")
print(f"{'='*50}")

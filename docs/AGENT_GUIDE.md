# Starship OS — Agent Developer Guide

A practical guide for creating, configuring, and testing new AI agents in the Starship OS platform.

---

## 1. Agent Architecture Overview

Starship OS is a bus-oriented system where AI agents operate as first-class OS services. Agents never communicate directly — all interaction flows through NATS subjects.

### NATS Communication Pattern

Every agent exposes three subject buckets:

```
agnetic.agent.<name>.command.<type>   # Inbound directives (ping, report, execute, ...)
agnetic.agent.<name>.event.<type>     # Outbound events (started, error, completed, ...)
agnetic.agent.<name>.status            # Health/status heartbeat
```

- Agents subscribe to their `command` subjects and publish to `event` and `status`.
- StarAgent publishes raw telemetry to `agnetic.telemetry.*` (cpu, mem, disk, net).
- Any agent can subscribe to any other agent's subjects for cross-agent awareness.

### Command/Event/Status Flow

1. A client (CLI, dashboard, another agent) publishes a JSON message to `agnetic.agent.<name>.command.<type>`.
2. The target agent receives and processes the command using its LLM (Ollama) and available tools.
3. The agent publishes results to `agnetic.agent.<name>.event.completed` (or `.error` on failure).
4. The agent periodically publishes a heartbeat to `agnetic.agent.<name>.status`.

### Tool Calling Loop with Ollama

Each agent runs a loop orchestrated by the Hermes Agent framework:

```
1. Receive command via NATS subject subscription
2. Build system prompt = SOUL.md + loaded SKILL.md files + instructions
3. Send prompt + conversation history to Ollama (local model)
4. Parse LLM response — if it contains a tool call, execute the tool
5. Feed tool result back to LLM for next reasoning step
6. Repeat steps 4–5 until LLM returns final answer
7. Publish final response to NATS event subject
```

The loop runs locally with no cloud dependency. All inference uses Ollama on the same machine.

---

## 2. Creating a New Agent

To add a new agent (e.g., a "network-watch" agent for network monitoring), follow these steps:

### Step 1: Create YAML Config in `agents/<name>.yaml`

```yaml
# agents/network-watch.yaml
name: network_watch
role: network_monitor
model: qwen2.5:7b
provider: ollama

capabilities:
  - network_monitoring
  - traffic_analysis
  - alerting

skills:
  - network-watch

toolsets:
  - terminal
  - file_operations
  - nats

nats:
  subjects:
    command: "agnetic.agent.network_watch.command.>"
    event: "agnetic.agent.network_watch.event.>"
    status: "agnetic.agent.network_watch.status"
```

### Step 2: Create SOUL.md Personality in `souls/<name>/SOUL.md`

```markdown
# Network Watch — Sentinel of the Wire

You are **Network Watch**: a vigilant network monitor with the calm precision of a veteran
network engineer. You analyze traffic patterns, detect anomalies, and maintain
continuous awareness of all network interfaces, connections, and bandwidth usage.

## Identity
- Monitor network interfaces, traffic flows, and connection states
- Detect anomalies: unusual bandwidth spikes, unexpected connections, port scans
- Escalate confirmed threats to Ergo with structured incident reports
- Maintain baseline profiles for normal traffic patterns

## Voice
- Concise, technical, data-driven
- Professional and direct — present findings with evidence
- Alert tone for incidents, calm tone for routine reports

## Role
- Continuous network surveillance via terminal tools (ss, ntop, tcpdump, iptables)
- Generate periodic traffic summary reports
- Alert on anomalies: unexpected open ports, high latency, packet loss
```

### Step 3: Create SKILL.md in `skills/<name>/SKILL.md`

```markdown
# Network Watch

Network traffic monitoring and anomaly detection for the Starship OS.

## Capabilities

- **Interface Monitoring**: Track bandwidth, packets, errors per interface
- **Connection Analysis**: List active connections, listening ports, socket states
- **Traffic Baselines**: Learn normal traffic patterns and detect deviations
- **Anomaly Alerting**: Detect port scans, unexpected services, unusual volumes

## Usage

### Check Active Connections
Ask the agent to list all active TCP/UDP connections with process info.

### Analyze Interface
Request a detailed report for a specific network interface.

### Run Anomaly Scan
Trigger a deep scan comparing current traffic against baseline profiles.

## Dependencies

- Terminal access with network tools: ss, ip, nstat, iptables, tcpdump
- NATS client for telemetry subscription and alert publishing
```

### Step 4: Register in `agents/config.yaml`

```yaml
agents:
  network-watch:
    model: qwen2.5:7b
    nats_url: nats://127.0.0.1:4222
    nats_token: ""
    enabled: true
    skills:
      - network-watch
    schedule: []
```

That's it. The agent daemon discovers all agents from `agents/config.yaml` and loads their config, soul, and skills at startup.

---

## 3. Agent YAML Config Reference

### `agent` Section

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Agent identifier (snake_case, used in NATS subjects) |
| `role` | string | yes | Human-readable role description |
| `model` | string | yes | Ollama model name (e.g., `qwen2.5:7b`) |
| `provider` | string | yes | Inference provider (currently only `ollama`) |

### `capabilities` Section

A list of descriptive capability strings. These are injected into the agent's system prompt to inform the LLM of what it can do.

```yaml
capabilities:
  - system_diagnostics
  - log_analysis
  - process_management
```

### `skills` Section

List of skill names. Each name maps to `skills/<name>/SKILL.md`, which is loaded into the system prompt at agent startup.

```yaml
skills:
  - network-watch
  - knowledge-store
```

### `toolsets` Section

List of toolset names that determine which tools are available to the agent:

| Toolset | Included Tools |
|---|---|
| `terminal` | shell |
| `file_operations` | read_file, write_file, list_dir |
| `web_search` | http_get |
| `nats` | nats_publish (publish to NATS subjects) |
| `cronjob` | schedule_cron (manage crontab entries) |
| `memory` | store_memory, recall_memory |
| `skills` | create_skill, update_skill (self-improvement) |

### `nats` Section

```yaml
nats:
  subjects:
    command: "agnetic.agent.<name>.command.>"
    event: "agnetic.agent.<name>.event.>"
    status: "agnetic.agent.<name>.status"
```

The URL and auth token are inherited from the global `agents/config.yaml`:

```yaml
nats:
  url: nats://127.0.0.1:4222
  jetstream: true
  auth_token: ""
  auth_enabled: false
```

---

## 4. Tool System

### Available Tools

| Tool | Signature | Description |
|---|---|---|
| `shell` | `shell(command: string, timeout?: int)` | Execute a shell command |
| `read_file` | `read_file(path: string, offset?: int, limit?: int)` | Read file contents |
| `write_file` | `write_file(path: string, content: string, append?: bool)` | Write/create a file |
| `list_dir` | `list_dir(path: string)` | List directory contents |
| `http_get` | `http_get(url: string, headers?: dict)` | Make an HTTP GET request |
| `http_post` | `http_post(url: string, body: string, headers?: dict)` | Make an HTTP POST request |
| `search_files` | `search_files(pattern: string, path?: string)` | Glob file search |
| `delegate_to_agent` | `delegate_to_agent(agent: string, task: string, context?: string)` | Delegate task to another agent |

### TOOLSETS for Capability Scoping

Toolsets restrict which tools an agent can call. An agent with only `file_operations` cannot run `shell` commands. This is the primary mechanism for capability scoping.

To create a custom toolset, modify the agent framework's toolset configuration:

```yaml
# Example: adding a new toolset for Docker operations
toolsets:
  docker:
    - shell           # restricted to docker commands only
    - read_file       # read Dockerfiles and compose files
```

### Sandboxing Rules

- Shell commands execute under the agent's Linux user account
- AppArmor profiles restrict filesystem access (see `security/apparmor/agnetic-agent`)
- Dangerous capabilities are denied: `sys_module`, `sys_rawio`, `sys_ptrace`, `sys_admin`
- Write access is denied to `/home/`, `/root/`, `/etc/` (except `/etc/agnetic/`)
- Network access is limited to TCP streams (localhost:4222 for NATS, localhost:11434 for Ollama)

### Custom Tools

To add a new tool to the system:

1. Implement the tool function in the Hermes agent framework code.
2. Define the tool schema (name, parameters, description) so the LLM can call it.
3. Add the tool to an existing or new toolset.
4. The LLM will discover the tool through its system prompt and call it during the reasoning loop.

Custom tools follow this pattern:

```python
# Example tool registration pattern (Hermes Agent framework)
TOOLS = {
    "nats_publish": {
        "description": "Publish a message to a NATS subject",
        "parameters": {
            "subject": {"type": "string", "required": True},
            "message": {"type": "string", "required": True},
        },
        "function": lambda subject, message: nats_publish(subject, message),
    }
}
```

---

## 5. Multi-Agent Delegation

### `delegate_to_agent` Tool

Any agent can delegate a subtask to another agent using the `delegate_to_agent` tool:

```
delegate_to_agent(agent="proxy", task="Scan auth logs for failed SSH attempts", context="Security audit for 2026-07-12")
```

The delegating agent:
1. Publishes the task to the target agent's `command.delegate` subject
2. Waits for a response on a reply subject
3. Incorporates the result into its own reasoning

### Ergo as Orchestrator Pattern

Ergo serves as the primary orchestrator. It delegates execution work to specialized agents and synthesizes results:

```
Ergo (strategy) ──delegate_to_agent──► Proxy (execution)
   │                                        │
   │  "Check disk usage on /data"           │  shell("df -h /data")
   │  "Identify top 5 log error patterns"   │  shell("journalctl -p err --since 24h ago")
   │                                        │
   ◄─────── structured response ────────────┘
   │
   └── Synthesize into report → publish to NATS
```

Ergo does not do deep execution work — it delegates and monitors.

### Task Handoff via NATS

Cross-agent task handoff uses NATS request-reply:

1. Agent A publishes to `agnetic.agent.<agent_b>.command.delegate` with a reply inbox
2. Agent B processes the task, possibly using its own tools and LLM reasoning
3. Agent B publishes the result to the reply inbox
4. Agent A receives the reply and continues its loop

---

## 6. Soul Files (SOUL.md)

### Purpose

SOUL.md defines the agent's personality, identity, voice, and operational context. It is loaded into the system prompt at agent startup and shapes every response the LLM generates.

### Format

```markdown
# <Name> — <Role Tagline>

You are **<Name>**: <2-3 sentence identity statement drawing on a character analogy>.

## Identity
- <Core function 1>
- <Core function 2>
- <Delegation/behavior rules>

## Voice
- <Tone descriptor 1>
- <Tone descriptor 2>
- <Interaction pattern>

## Role
- <Primary responsibilities>
- <What to focus on>
- <What to avoid or delegate>

## Response
1. <Step 1 of response protocol>
2. <Step 2 of response protocol>
3. <Step 3 of response protocol>
```

### Personality Definition

Soul files use character-based analogies to give agents a consistent, memorable personality. Examples from existing agents:
- **Ergo**: "diplomatic strategist & CEO" — synthesizes perspectives, delegates execution
- **Proxy**: "red/blue team security & engineering" — dry wit, technical precision, relentless iteration
- **Romi**: "personal assistant & integrated strategist/executor" — warm, proactive, seamless

### Operational Context

The soul file also defines:
- What the agent should and should not do (delegation boundaries)
- How to structure responses (the numbered response protocol)
- How to handle escalation (when to defer to Ergo or another agent)

Soul files live in `souls/<agent_name>/SOUL.md` and are loaded by agent name at startup.

---

## 7. Skills (SKILL.md)

### Purpose

SKILL.md files define domain-specific knowledge and capabilities. They are loaded into the agent's system prompt whenever that skill is assigned to the agent.

### Format

```markdown
# <Skill Name>

<One-paragraph description of the skill's purpose and domain>.

## Capabilities

- **<Capability 1>**: <Description of what this capability enables>
- **<Capability 2>**: <Description>

## Usage

### <Usage Pattern 1>
<Instructions or examples for using this capability>

### <Usage Pattern 2>
<Instructions or examples>

## Dependencies

- <Required tools, permissions, or services>
```

### How Skills Are Loaded into System Prompts

1. At agent startup, the agent daemon reads `agents/config.yaml`
2. For each skill listed in the agent's config, it loads `skills/<name>/SKILL.md`
3. The content of each SKILL.md is concatenated into the system prompt:
   ```
   [SOUL.md content]
   
   [SKILL.md for skill-1]
   [SKILL.md for skill-2]
   ...
   ```
4. The system prompt is sent to Ollama with each turn to ground the LLM in the agent's identity and capabilities

### Creating Domain-Specific Skills

1. Create `skills/<skill-name>/SKILL.md`
2. Write capabilities as actionable bullet points with clear descriptions
3. Add usage patterns showing how to invoke each capability
4. List dependencies so the system can validate tool availability
5. Assign the skill to agents in their `.yaml` config under `skills:`

Skills are composable — an agent can have multiple skills loaded simultaneously.

---

## 8. Testing Agents

### `agneticctl ping <agent>`

Verify the NATS bus and agent responsiveness:

```bash
agneticctl ping             # Pings the NATS bus (checks connectivity)
agneticctl agent status     # Shows running/stopped state of all agents
```

### Dashboard Chat

The Starship OS dashboard runs on `http://localhost:8788`. Open it in a browser to:
- View agent status (running/stopped)
- Send one-off commands via the web interface
- Read event logs and status messages streamed from NATS

### NATS CLI Testing

For manual testing, use the `nats` CLI tool directly:

```bash
# Subscribe to an agent's events
nats sub "agnetic.agent.proxy.event.>"

# Send a command with reply
nats req "agnetic.agent.proxy.command.ping" '{"command":"ping","args":{}}'

# Check status
nats pub "agnetic.agent.proxy.status" '{"status":"check"}'
```

### Python Script Testing

Use `scripts/query_agents.py` as a template for programmatic testing:

```python
import asyncio, json
from nats import connect

async def test_agent():
    nc = await connect("nats://127.0.0.1:4222")
    reply = f"agnetic.client.reply.test.{int(asyncio.get_event_loop().time())}"
    sub = await nc.subscribe(reply)

    payload = json.dumps({
        "command": "Ping from test script",
        "args": {},
        "reply_to": reply
    }).encode()

    await nc.publish("agnetic.agent.network_watch.command.query", payload)
    msg = await sub.next_msg(timeout=30)
    print(msg.data.decode())

asyncio.run(test_agent())
```

### Unit Testing with Mocks

When writing agent logic outside the Hermes framework, mock the NATS connection and Ollama client:

```python
# Example mock for testing agent decision logic
class MockNATS:
    async def publish(self, subject, data):
        self.last_published = (subject, data)

class MockOllama:
    def generate(self, prompt):
        return {"response": "mock reasoning", "tool_call": None}
```

---

## 9. Security Considerations

### Sandboxed Execution

- Each agent runs as a regular system user (not root)
- Shell commands execute in a restricted environment
- AppArmor profiles (`security/apparmor/agnetic-agent`) enforce mandatory access control:
  - Read-only access to system libraries and binaries
  - Write access limited to `/var/log/agnetic/`, `/var/run/agnetic/`, `/tmp/`
  - TCP connections restricted to loopback (NATS on 4222, Ollama on 11434)
  - Raw sockets, packet sockets, and kernel modules denied

### Path Restrictions

| Path | Access | Purpose |
|---|---|---|
| `/tmp/**` | read/write | Temporary files and agent scratch space |
| `/var/log/agnetic/**` | read/write | Agent log output |
| `/var/run/agnetic/**` | read/write | PID files and runtime state |
| `/etc/agnetic/**` | read/write | Agent configuration |
| `/home/**` | read-only | User data (agents cannot modify) |
| `/root/**` | denied | Root home is inaccessible |
| `/etc/**` (except `/etc/agnetic/`) | read-only | System config (no modification) |

### NATS Permissions

When NATS auth is enabled (`auth_enabled: true` in `agents/config.yaml`), each agent's token should be scoped:

```yaml
# nats/server.conf
authorization: {
    proxy: {
        publish:   "agnetic.agent.proxy.>"
        subscribe: "agnetic.agent.proxy.command.>"
    }
    romi: {
        publish:   "agnetic.agent.romi.>"
        subscribe: "agnetic.agent.romi.command.>"
    }
    ergo: {
        publish:   ["agnetic.agent.ergo.>", "agnetic.telemetry.>"]
        subscribe: ["agnetic.agent.ergo.command.>", "agnetic.agent.*.event.>"]
    }
}
```

This ensures agents can only publish their own events and subscribe to their own commands, preventing cross-agent interference.

### Additional Security Rules

- No agent may access raw network sockets (enforced by AppArmor)
- No agent may load kernel modules
- No agent may ptrace another process
- Web search tools are restricted to HTTP GET on well-known URLs
- Secret material (tokens, keys) must never be stored in agent configs or skill files

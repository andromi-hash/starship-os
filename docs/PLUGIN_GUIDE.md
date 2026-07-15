# Starship OS Plugin Developer Guide

Complete guide to building, testing, and publishing plugins for Starship OS.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Quick Start](#quick-start)
3. [Plugin Manifest Reference](#plugin-manifest-reference)
4. [Creating Tools](#creating-tools)
5. [Creating Webhook Handlers](#creating-webhook-handlers)
6. [Creating NATS Handlers](#creating-nats-handlers)
7. [Configuration System](#configuration-system)
8. [Security and Sandboxing](#security-and-sandboxing)
9. [Publishing to Marketplace](#publishing-to-marketplace)
10. [Examples](#examples)
11. [API Reference](#api-reference)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Starship OS                               │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  Agent:   │  │  Agent:   │  │  Agent:   │  │  Agent:   │      │
│  │  proxy    │  │  romi    │  │  ergo    │  │  ...     │       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│       │              │              │              │              │
│       └──────────────┴──────┬───────┴──────────────┘              │
│                             │                                     │
│                    ┌────────▼────────┐                            │
│                    │  PluginManager  │                            │
│                    │                 │                            │
│                    │  .discover()    │                            │
│                    │  .load()        │                            │
│                    │  .get_tools()   │                            │
│                    │  .get_skills()  │                            │
│                    │  .get_webhook_handlers()                    │
│                    │  .get_nats_handler()                        │
│                    └────────┬────────┘                            │
│                             │                                     │
│         ┌───────────────────┼───────────────────┐                 │
│         │                   │                   │                  │
│  ┌──────▼──────┐    ┌──────▼──────┐    ┌──────▼──────┐          │
│  │   Plugin A   │    │   Plugin B   │    │   Plugin C   │         │
│  │   ┌───────┐  │    │   ┌───────┐  │    │   ┌───────┐  │        │
│  │   │tools/ │  │    │   │tools/ │  │    │   │tools/ │  │        │
│  │   │skills │  │    │   │skills │  │    │   │skills │  │        │
│  │   │hooks  │  │    │   │hooks  │  │    │   │hooks  │  │        │
│  │   └───────┘  │    │   └───────┘  │    │   └───────┘  │        │
│  └──────────────┘    └──────────────┘    └──────────────┘         │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │                    Tool Sandbox                            │   │
│  │   Built-in tools  │  Plugin tools  │  Restricted APIs     │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │    NATS      │  │   Webhooks   │  │   Config     │             │
│  │   Bus        │  │   Router     │  │   Store      │             │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘

Plugin Directory Layout:
─────────────────────────
  /opt/agnetic/plugins/          # production plugins dir
  ./plugins/                     # dev mode plugins dir

  plugins/
  └── github-integration/
      ├── plugin.yaml            # manifest (required)
      ├── __init__.py            # entry point (required)
      ├── SKILL.md               # skill definition (optional)
      ├── tools/
      │   ├── create_pr.py       # tool implementations
      │   └── review_pr.py
      ├── setup.sh               # post-install hook (optional)
      └── .checksum              # integrity hash (auto-generated)
```

Plugins are Python packages loaded at runtime. Each plugin registers tools, skills, webhook handlers, and NATS handlers through its `plugin.yaml` manifest. The `PluginManager` discovers plugins in the configured directory, loads them, and makes their capabilities available to agents based on per-agent access policies.

---

## Quick Start

Create a new plugin in under 5 minutes:

```bash
# Step 1: Create the plugin directory
mkdir -p plugins/hello-world/tools

# Step 2: Create the manifest
cat > plugins/hello-world/plugin.yaml << 'EOF'
name: hello-world
version: 0.1.0
description: "A minimal example plugin"
author: "Your Name"
license: MIT
min_version: 0.1.0

provides:
  tools:
    - name: say_hello
      description: "Say hello to someone"
      parameters:
        name: {type: string, required: true}
EOF

# Step 3: Create the entry point
cat > plugins/hello-world/__init__.py << 'PYEOF'
def say_hello(name: str, **kwargs) -> str:
    return f"Hello, {name}! Welcome to Starship OS."

tools = {"say_hello": say_hello}
PYEOF

# Step 4: Install and test
python3 plugin_manager.py install plugins/hello-world
python3 plugin_manager.py load hello-world
python3 plugin_manager.py list
```

---

## Plugin Manifest Reference

`plugin.yaml` is the complete descriptor for your plugin. All fields:

```yaml
# ── Identity ──────────────────────────────────────────────────────
name: my-plugin                  # unique slug (lowercase, hyphens)
version: 1.0.0                   # semver
description: "What this plugin does"
author: "Your Name"
license: MIT                     # SPDX identifier
min_version: 0.2.0               # minimum Starship OS version
homepage: "https://example.com"  # optional link

# ── Capabilities ──────────────────────────────────────────────────
provides:
  tools:
    - name: do_something
      description: "Performs an action"
      parameters:
        target: {type: string, required: true}
        count:  {type: integer, required: false, default: 1}

  skills:
    - my-skill-name              # maps to SKILL.md content

  webhook_handlers:
    - event: pull_request        # GitHub event type
      action: auto_review        # function name in __init__.py

  nats_handlers:
    - subject: "agnetic.plugin.myplugin.>"   # NATS subject pattern
      handler: handle_nats_message           # function name in __init__.py

# ── Dependencies ──────────────────────────────────────────────────
dependencies:
  python:
    - PyGithub>=2.0
    - requests>=2.28
  services:
    - nats
    - redis

# ── Configuration ─────────────────────────────────────────────────
config:
  api_key:
    type: env                   # env | string | boolean | integer | path
    env: MY_PLUGIN_API_KEY      # environment variable name
    required: true

  debug_mode:
    type: boolean
    default: false

  cache_ttl:
    type: integer
    default: 300

  data_dir:
    type: path
    default: /var/lib/agnetic/my-plugin
```

### Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique plugin identifier |
| `version` | string | yes | Semver version |
| `description` | string | yes | Human-readable description |
| `author` | string | yes | Author name |
| `license` | string | no | SPDX license (default: MIT) |
| `min_version` | string | no | Minimum Starship OS version |
| `homepage` | string | no | Plugin homepage URL |
| `provides.tools` | list | no | Tool definitions |
| `provides.skills` | list | no | Skill names |
| `provides.webhook_handlers` | list | no | Webhook handler bindings |
| `provides.nats_handlers` | list | no | NATS handler bindings |
| `dependencies.python` | list | no | Python package requirements |
| `dependencies.services` | list | no | Required Agnetic services |
| `config` | map | no | Configuration this plugin needs |

### Tool Parameter Types

| Type | Python Type | Example |
|------|-------------|---------|
| `string` | `str` | `"hello"` |
| `integer` | `int` | `42` |
| `number` | `float` | `3.14` |
| `boolean` | `bool` | `true` |
| `array` | `list` | `[1, 2, 3]` |
| `object` | `dict` | `{"key": "value"}` |

---

## Creating Tools

Tools are Python functions that agents can call. Each tool function receives keyword arguments matching its parameter definition.

### Basic Tool

```python
# plugins/my-plugin/__init__.py

def add(a: int, b: int, **kwargs) -> int:
    """Add two numbers together."""
    return a + b

tools = {"add": add}
```

### Tool with Side Effects

```python
# plugins/my-plugin/__init__.py

import os
from pathlib import Path

def write_file(path: str, content: str, **kwargs) -> str:
    """Write content to a file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Written {len(content)} bytes to {path}"

tools = {"write_file": write_file}
```

### Tool with Complex Parameters

```python
def search_repos(
    query: str,
    language: str = "",
    sort: str = "stars",
    limit: int = 10,
    **kwargs,
) -> list[dict]:
    """Search GitHub repositories."""
    import requests
    params = {"q": query, "sort": sort, "per_page": limit}
    if language:
        params["q"] += f" language:{language}"
    resp = requests.get("https://api.github.com/search/repositories", params=params)
    resp.raise_for_status()
    results = resp.json().get("items", [])
    return [{"name": r["name"], "stars": r["stargazers_count"]} for r in results]

tools = {"search_repos": search_repos}
```

### Tool Registration Styles

You can register tools two ways:

```python
# Style 1: Direct function reference
def my_tool(**kwargs):
    return "result"

tools = {"my_tool": my_tool}
```

```python
# Style 2: Tool dict (same keys as manifest names)
def create_pr(**kwargs):
    ...

def review_pr(**kwargs):
    ...

tools = {
    "create_pr": create_pr,
    "review_pr": review_pr,
}
```

---

## Creating Webhook Handlers

Webhook handlers respond to external events (GitHub webhooks, CI notifications, etc.).

```python
# plugins/my-plugin/__init__.py

def auto_review(event_data: dict, **kwargs) -> dict:
    """Automatically review a pull request when opened."""
    pr = event_data.get("pull_request", {})
    pr_number = pr.get("number")
    repo = event_data.get("repository", {}).get("full_name")

    # Your review logic here
    review = {
        "repo": repo,
        "pr": pr_number,
        "action": "reviewed",
        "summary": f"Auto-review of PR #{pr_number}",
    }

    return review

def handle_push(event_data: dict, **kwargs) -> dict:
    """React to push events."""
    branch = event_data.get("ref", "").replace("refs/heads/", "")
    commits = event_data.get("commits", [])
    return {
        "branch": branch,
        "commit_count": len(commits),
    }

# tools dict is still needed for tool registration
tools = {}

# Webhook handlers are registered via plugin.yaml:
# provides:
#   webhook_handlers:
#     - event: pull_request
#       action: auto_review
#     - event: push
#       action: handle_push
```

### Webhook Event Flow

```
External Service (GitHub/GitLab/etc.)
        │
        ▼
    Agnetic Webhook Router
        │
        ▼
    PluginManager.get_webhook_handlers("pull_request")
        │
        ▼
    handler.callback(event_data)
        │
        ▼
    Response / Side Effects
```

---

## Creating NATS Handlers

NATS handlers process messages from the NATS message bus, enabling inter-service communication.

```python
# plugins/my-plugin/__init__.py

import json

def handle_nats_message(msg):
    """Handle NATS messages on the plugin's subject."""
    data = json.loads(msg.data.decode())
    action = data.get("action")

    if action == "ping":
        return {"status": "pong"}
    elif action == "status":
        return {"loaded": True, "version": "1.0.0"}

    return {"error": f"Unknown action: {action}"}

def handle_config_update(msg):
    """React to configuration changes broadcast via NATS."""
    data = json.loads(msg.data.decode())
    new_config = data.get("config", {})
    # Update local state
    return {"updated": True}

tools = {}
```

Register in `plugin.yaml`:

```yaml
provides:
  nats_handlers:
    - subject: "agnetic.plugin.myplugin.>"
      handler: handle_nats_message
    - subject: "agnetic.config.update"
      handler: handle_config_update
```

### NATS Subject Patterns

| Pattern | Matches |
|---------|---------|
| `agnetic.plugin.github.>` | Any sub-subject under `agnetic.plugin.github` |
| `agnetic.events.pull_request` | Exact subject match |
| `agnetic.agents.>` | All agent-related messages |

---

## Configuration System

Plugins can declare configuration requirements in `plugin.yaml`. At runtime, values are resolved from environment variables, config files, or defaults.

### Config Types

```yaml
config:
  # Environment variable (most common for secrets)
  api_token:
    type: env
    env: MY_PLUGIN_API_TOKEN
    required: true

  # String value
  base_url:
    type: string
    default: "https://api.example.com"

  # Boolean
  enable_caching:
    type: boolean
    default: true

  # Integer
  timeout:
    type: integer
    default: 30

  # File path
  cert_file:
    type: path
    default: "/etc/agnetic/certs/plugin.pem"
```

### Accessing Config in Plugins

```python
# plugins/my-plugin/__init__.py

import os

# Read from environment (for env type configs)
API_TOKEN = os.environ.get("MY_PLUGIN_API_TOKEN", "")

# Or use a config helper
class PluginConfig:
    def __init__(self):
        self.api_token = os.environ.get("MY_PLUGIN_API_TOKEN", "")
        self.base_url = os.environ.get("MY_PLUGIN_BASE_URL", "https://api.example.com")
        self.enable_caching = os.environ.get("MY_PLUGIN_ENABLE_CACHING", "true").lower() == "true"
        self.timeout = int(os.environ.get("MY_PLUGIN_TIMEOUT", "30"))

config = PluginConfig()

def my_tool(**kwargs):
    # Use config.api_token, config.base_url, etc.
    ...
```

---

## Security and Sandboxing

### Plugin Permissions

The `PluginManager` derives permissions from the manifest:

| Permission | Granted When |
|------------|-------------|
| `tools` | Plugin defines tools |
| `skills` | Plugin defines skills |
| `webhooks` | Plugin defines webhook handlers |
| `nats` | Plugin defines NATS handlers |
| `services:<name>` | Plugin depends on a service |

### Per-Agent Access Control

In `/etc/agnetic/plugins.yaml`:

```yaml
plugins:
  sandbox: true
  per_agent:
    proxy:
      allow: [github-integration, docker-tools]
      deny: [system-admin]
    romi:
      allow: [all]
    ergo:
      deny: [dangerous-plugin]
```

Access rules:
- If `allow` is set, only listed plugins are available (or `all` for everything)
- If `deny` is set, listed plugins are excluded
- `deny` takes precedence over `allow`
- If neither is set, all plugins are available

### Plugin Verification

Each plugin gets a SHA-256 checksum of all its files. On subsequent loads, the checksum is verified.

```bash
# Verify a plugin's integrity
python3 plugin_manager.py verify github-integration
```

Output:
```
Security Report: github-integration
  Verified:       True
  Signature Valid: True
  Scanned At:     2026-07-12T10:30:00
  Permissions:     tools, webhooks, nats, services:nats

  All checks passed.
```

### Sandboxing Notes

- Plugins run in the same Python process
- Access to the filesystem, network, and system calls is not restricted by the plugin system itself
- Use OS-level sandboxing (containers, seccomp, AppArmor) for stronger isolation
- Plugin access per-agent is controlled by the `per_agent` config
- All plugin tool calls are logged for audit purposes

---

## Publishing to Marketplace

### Package Your Plugin

```bash
# Create a distributable archive
cd plugins/
tar -czf my-plugin-1.0.0.tar.gz my-plugin/
```

### Submit to Marketplace

```bash
# Using the Agnetic CLI (when available)
agnetic marketplace publish ./plugins/my-plugin

# Or submit via the web interface at:
# https://marketplace.agnetic.ai/submit
```

### Marketplace Metadata

Add these fields to your `plugin.yaml` for marketplace display:

```yaml
homepage: "https://github.com/yourname/my-plugin"
tags: [github, pr, code-review]
icon: "path/to/icon.png"         # 128x128 PNG
screenshots:
  - "path/to/screenshot.png"
changelog: |
  ## 1.0.0
  - Initial release
  - PR creation and review tools
```

### Plugin Review Process

1. Automated security scan (checksum, dependency audit)
2. Manual review for new authors
3. Version bumps require re-review only for major changes
4. Maintainers can publish hotfix versions without full review

---

## Examples

### Example 1: GitHub Integration Plugin

**Directory structure:**
```
plugins/github-integration/
├── plugin.yaml
├── __init__.py
├── SKILL.md
└── tools/
    ├── create_pr.py
    └── review_pr.py
```

**plugin.yaml:**
```yaml
name: github-integration
version: 1.0.0
description: "GitHub integration for PR reviews and issue triage"
author: "Agnetic Team"
license: MIT
min_version: 0.2.0

provides:
  tools:
    - name: create_pr
      description: "Create a GitHub pull request"
      parameters:
        title: {type: string, required: true}
        body: {type: string, required: true}
        branch: {type: string, required: true}
    - name: review_pr
      description: "Review a GitHub PR"
      parameters:
        pr_number: {type: integer, required: true}
  skills:
    - github-pr-workflow
  webhook_handlers:
    - event: pull_request
      action: auto_review
  nats_handlers:
    - subject: "agnetic.plugin.github.>"
      handler: handle_nats_message

dependencies:
  python: ["PyGithub>=2.0"]
  services: ["nats"]

config:
  github_token:
    type: env
    env: GITHUB_TOKEN
    required: true
  review_bot:
    type: boolean
    default: true
```

**__init__.py:**
```python
"""GitHub integration plugin for Starship OS."""

import json
import os

from github import Github

_github = None


def _get_client() -> Github:
    global _github
    if _github is None:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise RuntimeError("GITHUB_TOKEN environment variable not set")
        _github = Github(token)
    return _github


def create_pr(title: str, body: str, branch: str, base: str = "main", repo: str = "", **kwargs) -> dict:
    """Create a GitHub pull request."""
    client = _get_client()
    if not repo:
        raise ValueError("repo parameter is required")

    repository = client.get_repo(repo)
    pr = repository.create_pull(title=title, body=body, head=branch, base=base)

    return {
        "number": pr.number,
        "url": pr.html_url,
        "state": pr.state,
    }


def review_pr(pr_number: int, repo: str = "", event: str = "COMMENT", body: str = "", **kwargs) -> dict:
    """Review a GitHub pull request."""
    client = _get_client()
    if not repo:
        raise ValueError("repo parameter is required")

    repository = client.get_repo(repo)
    pr = repository.get_pull(pr_number)
    review = pr.create_review(body=body or "Automated review", event=event)

    return {
        "review_id": review.id,
        "state": review.state,
        "pr": pr_number,
    }


def auto_review(event_data: dict, **kwargs) -> dict:
    """Automatically review PRs when they are opened."""
    action = event_data.get("action")
    if action != "opened":
        return {"skipped": True, "reason": f"action={action}"}

    pr = event_data.get("pull_request", {})
    pr_number = pr.get("number")
    repo_full = event_data.get("repository", {}).get("full_name")

    try:
        result = review_pr(
            pr_number=pr_number,
            repo=repo_full,
            event="COMMENT",
            body="Automated review triggered by Agnetic plugin.",
        )
        return {"reviewed": True, **result}
    except Exception as exc:
        return {"reviewed": False, "error": str(exc)}


def handle_nats_message(msg):
    """Handle NATS messages for the GitHub plugin."""
    data = json.loads(msg.data.decode())
    action = data.get("action")

    if action == "status":
        return {"plugin": "github-integration", "healthy": True}
    elif action == "trigger_review":
        return auto_review(data.get("event_data", {}))

    return {"error": f"Unknown action: {action}"}


tools = {
    "create_pr": create_pr,
    "review_pr": review_pr,
}
```

**SKILL.md:**
```markdown
# GitHub PR Workflow Skill

You have access to GitHub integration tools for managing pull requests.

## Available Actions
- Create pull requests with `create_pr`
- Review pull requests with `review_pr`
- Auto-review is triggered on new PRs via webhooks

## Workflow
1. When a PR is opened, auto_review triggers
2. Use review_pr to provide detailed feedback
3. Create follow-up PRs for fixes using create_pr

## Configuration
- GITHUB_TOKEN must be set as an environment variable
- review_bot can be enabled/disabled in plugin config
```

---

### Example 2: Docker Management Plugin

**Directory structure:**
```
plugins/docker-tools/
├── plugin.yaml
├── __init__.py
├── SKILL.md
└── tools/
    ├── list_containers.py
    ├── run_container.py
    └── docker_logs.py
```

**plugin.yaml:**
```yaml
name: docker-tools
version: 1.2.0
description: "Docker container management for Agnetic agents"
author: "Agnetic Team"
license: MIT
min_version: 0.1.0

provides:
  tools:
    - name: list_containers
      description: "List running Docker containers"
      parameters:
        all: {type: boolean, required: false, default: false}
    - name: run_container
      description: "Run a Docker container"
      parameters:
        image: {type: string, required: true}
        name: {type: string, required: false}
        ports: {type: string, required: false}
        env: {type: string, required: false}
    - name: docker_logs
      description: "Get logs from a Docker container"
      parameters:
        container: {type: string, required: true}
        lines: {type: integer, required: false, default: 100}
  skills:
    - docker-management
  nats_handlers:
    - subject: "agnetic.plugin.docker.>"
      handler: handle_nats_message

dependencies:
  python: ["docker>=6.0"]
  services: []

config:
  docker_host:
    type: env
    env: DOCKER_HOST
    required: false
  socket_path:
    type: path
    default: /var/run/docker.sock
```

**__init__.py:**
```python
"""Docker container management plugin for Starship OS."""

import json
import os

import docker


_client = None


def _get_client():
    global _client
    if _client is None:
        host = os.environ.get("DOCKER_HOST")
        if host:
            _client = docker.DockerClient(base_url=host)
        else:
            _client = docker.from_env()
    return _client


def list_containers(all: bool = False, **kwargs) -> list[dict]:
    """List Docker containers."""
    client = _get_client()
    containers = client.containers.list(all=all)
    return [
        {
            "id": c.short_id,
            "name": c.name,
            "image": c.image.tags[0] if c.image.tags else str(c.image.id)[:12],
            "status": c.status,
            "state": c.attrs["State"]["Status"],
        }
        for c in containers
    ]


def run_container(
    image: str,
    name: str = "",
    ports: str = "",
    env: str = "",
    detach: bool = True,
    **kwargs,
) -> dict:
    """Run a Docker container."""
    client = _get_client()

    run_kwargs: dict = {"image": image, "detach": detach}
    if name:
        run_kwargs["name"] = name
    if ports:
        port_map = {}
        for mapping in ports.split(","):
            parts = mapping.strip().split(":")
            if len(parts) == 2:
                port_map[f"{parts[0]}/tcp"] = int(parts[1])
        run_kwargs["ports"] = port_map
    if env:
        env_list = [e.strip() for e in env.split(",") if "=" in e]
        run_kwargs["environment"] = env_list

    container = client.containers.run(**run_kwargs)

    return {
        "id": container.short_id,
        "name": container.name,
        "status": container.status,
    }


def docker_logs(container: str, lines: int = 100, **kwargs) -> str:
    """Get logs from a Docker container."""
    client = _get_client()
    c = client.containers.get(container)
    logs = c.logs(tail=lines, timestamps=True)
    return logs.decode("utf-8", errors="replace")


def handle_nats_message(msg):
    """Handle NATS messages for Docker plugin."""
    data = json.loads(msg.data.decode())
    action = data.get("action")

    if action == "list":
        return {"containers": list_containers(all=data.get("all", False))}
    elif action == "logs":
        return {"logs": docker_logs(data["container"], lines=data.get("lines", 100))}

    return {"error": f"Unknown action: {action}"}


tools = {
    "list_containers": list_containers,
    "run_container": run_container,
    "docker_logs": docker_logs,
}
```

---

## API Reference

### PluginManager

```python
from services.plugin_manager import PluginManager

manager = PluginManager(config_path=None)
```

#### Constructor

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_path` | `Path \| str \| None` | `/etc/agnetic/plugins.yaml` | Path to plugin config file |

#### Discovery Methods

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `discover()` | `() -> list[str]` | List of plugin names | Scan plugins directory and register manifests |

#### Loading Methods

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `load(name)` | `(name: str) -> bool` | Success flag | Load a specific plugin |
| `load_all()` | `() -> dict[str, bool]` | Name-to-success map | Load all enabled plugins |

#### Query Methods

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `get_tools(agent_name)` | `(agent_name: str) -> list[ToolDefinition]` | Tool definitions | Tools available to a specific agent |
| `get_skills(agent_name)` | `(agent_name: str) -> list[str]` | Skill file paths | Skill files available to a specific agent |
| `get_webhook_handlers(event)` | `(event: str) -> list[WebhookHandler]` | Handlers | Webhook handlers for an event type |
| `get_nats_handler(subject)` | `(subject: str) -> NATSHandler \| None` | Handler or None | NATS handler matching a subject |

#### Lifecycle Methods

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `enable(name)` | `(name: str) -> bool` | Success flag | Enable a plugin |
| `disable(name)` | `(name: str) -> bool` | Success flag | Disable a plugin (unloads if loaded) |
| `uninstall(name)` | `(name: str) -> bool` | Success flag | Remove plugin and cleanup |
| `install_from_path(source, name)` | `(source: Path, name: str \| None) -> bool` | Success flag | Install from local directory |
| `update(name)` | `(name: str) -> bool` | Success flag | Check for and apply updates |

#### Security Methods

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `verify(name)` | `(name: str) -> PluginSecurityReport` | Security report | Verify plugin integrity |

#### Creator Methods

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `create_plugin_interactive()` | `() -> Path \| None` | Plugin path | Interactive plugin scaffolding wizard |

### Data Classes

#### ToolDefinition

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    module: Any                    # loaded module reference
    function_name: str             # function name in module

    def to_dict(self) -> dict      # serializable representation
```

#### WebhookHandler

```python
@dataclass
class WebhookHandler:
    event: str                     # e.g. "pull_request"
    action: str                    # function name
    callback: Callable | None      # resolved function reference
    module: Any                    # loaded module
```

#### NATSHandler

```python
@dataclass
class NATSHandler:
    subject: str                   # e.g. "agnetic.plugin.github.>"
    handler: str                   # function name
    callback: Callable | None      # resolved function reference
    module: Any                    # loaded module
```

#### PluginManifest

```python
@dataclass
class PluginManifest:
    name: str
    version: str
    description: str
    author: str
    license: str
    min_version: str
    homepage: str
    tools: list[ToolDefinition]
    skills: list[str]
    webhook_handlers: list[WebhookHandler]
    nats_handlers: list[NATSHandler]
    dependencies: PluginDependency
    config: list[PluginConfig]
```

#### PluginState

```python
@dataclass
class PluginState:
    name: str
    enabled: bool
    loaded: bool
    version: str
    path: Path
    manifest: PluginManifest | None
    load_error: str
    loaded_at: float
```

#### PluginSecurityReport

```python
@dataclass
class PluginSecurityReport:
    name: str
    verified: bool
    signature_valid: bool
    permissions: list[str]
    warnings: list[str]
    errors: list[str]
    scanned_at: str
```

### CLI Reference

```
python3 plugin_manager.py <command> [args]

Commands:
  list                              List installed plugins
  install <path> [name]             Install plugin from local directory
  enable <name>                     Enable a plugin
  disable <name>                    Disable a plugin
  remove <name>                     Uninstall a plugin
  info <name>                       Show plugin details
  verify <name>                     Verify plugin integrity
  load [name]                       Load a plugin (or all if no name given)
  create                            Interactive plugin creator wizard
  help                              Show help message
```

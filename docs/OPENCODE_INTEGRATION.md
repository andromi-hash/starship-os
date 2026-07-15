# OpenCode Integration

OpenCode is built into Starship OS as a native tool that agents can call to build and expand the OS from inside.

## How It Works

When an agent needs to write code, create files, refactor modules, or build new features, it calls the `opencode` tool. This invokes the OpenCode CLI programmatically with `opencode run` and returns structured results.

```
Agent → opencode tool → OpenCode CLI → LLM → code output → Agent
```

## Installation

```bash
# Install OpenCode
curl -fsSL https://opencode.ai/install | bash

# Or via npm
npm install -g opencode-ai

# Verify
opencode --version
```

## Configuration

```yaml
# agents/config.yaml
proxy:
  tools:
    toolset: full    # includes opencode
  opencode:
    model: anthropic/claude-sonnet-4-20250514
    timeout: 120
```

## Tool Definition

```json
{
  "name": "opencode",
  "description": "Invoke OpenCode AI coding agent for code generation, refactoring, debugging, or building new features.",
  "parameters": {
    "prompt": "The coding task",
    "model": "provider/model format",
    "files": ["file paths to attach"],
    "session": "session ID to continue",
    "continue_last": true,
    "format": "json"
  }
}
```

## Usage Examples

### Create a new agent
```
Agent: opencode("Create a new agent called 'sentinel' that monitors system logs and alerts on anomalies. Create agents/sentinel.yaml, souls/sentinel/SOUL.md, skills/sentinel/SKILL.md")
```

### Refactor a module
```
Agent: opencode("Refactor the dashboard server to use async routes and add WebSocket support for real-time updates", files=["dashboard/server.py"])
```

### Add a new tool
```
Agent: opencode("Add a new tool called 'docker' to agents/tools.py that can manage Docker containers and images")
```

### Fix a bug
```
Agent: opencode("Fix the watchdog restart logic that fails when the service name contains spaces", files=["services/watchdog.py"])
```

## Security

- OpenCode runs within the sandbox (blocked commands, path restrictions)
- Agent output is redacted before being returned
- Model can be restricted per-agent in config
- Timeout prevents runaway sessions

## Toolsets

| Toolset | Includes OpenCode |
|---|---|
| coding | Yes |
| expansion | Yes |
| full | Yes |
| core | No |
| readonly | No |

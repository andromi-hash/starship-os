# Starship OS Architecture

A native AI operating system designed for complex system control, built with Hermes Agent, LangChain, Ollama, and Opencode.

## Philosophy

- **Agent-Native**: AI agents are first-class citizens of the OS
- **Specialization**: Each subsystem has a dedicated agent with focused capabilities
- **Learning**: Agents improve through experience, creating and refining skills autonomously
- **Local-First**: All intelligence runs locally via Ollama; cloud providers optional

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   HERMES ORCHESTRATOR                        │
│          (Central coordinator, routing, escalation)          │
├──────────┬──────────┬──────────┬──────────┬─────────────────┤
│  SYSTEM  │  KNOWLEDGE│   SECURI-│  AUTO-   │   NAV /         │
│  HEALTH  │  & MEMORY│   TY     │  MATION  │   RESOURCE      │
└──────────┴──────────┴──────────┴──────────┴─────────────────┘
```

## Tech Stack

- **Hermes Agent** - Agent framework with self-improvement loop
- **Ollama** - Local LLM inference
- **Qwen2.5** - Default local model (7B parameters)
- **Opencode** - Development interface
- **LangChain** - (Planned) Advanced agent orchestration
- **GitHub** - Version control and backup

## Agent Subsystems

### Orchestrator (Central)
Routes tasks to specialized agents, manages priorities, handles escalation.

### System Health
Monitors CPU, memory, disk, network, processes. Generates health reports and alerts.

### Knowledge Store
Persistent memory — indexes docs, archives logs, records solutions.

### Security (Planned)
Access control, anomaly detection, audit trails.

### Automation (Planned)
Scheduled tasks, backups, report generation.

### Navigation (Planned)
Resource allocation, pathfinding, state management.

## Development Roadmap

1. [x] Foundation: Hermes Agent + Ollama installed
2. [x] Skills: System Health, Knowledge Store
3. [ ] Agents: Configure and connect specialized agents
4. [ ] Security: Agent-based access control
5. [ ] Automation: Cron-based scheduled tasks
6. [ ] Distribution: Build as custom Linux ISO

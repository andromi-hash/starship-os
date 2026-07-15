# Proxy Diagnostics

System diagnostic and troubleshooting workflows for the Starship OS.

## Capabilities

- **System Diagnostics**: Run comprehensive system health checks
- **Log Analysis**: Scan and correlate logs across subsystems
- **Troubleshooting**: Step-by-step problem diagnosis and resolution
- **Resource Monitoring**: Track CPU, memory, disk, network in real-time
- **Process Management**: Identify and manage problematic processes
- **Network Analysis**: Test connectivity, DNS resolution, port status

## Usage

### Run Diagnostic
Ask the agent to run a full system diagnostic.

### Investigate Issue
Describe the problem — the agent will analyze logs, resources, and processes.

### Health Check
Request a focused health check on any subsystem.

## Dependencies

- Terminal access with appropriate permissions
- Standard Linux tools: ps, top, df, du, free, journalctl, ss, ping
- NATS client for telemetry subscription

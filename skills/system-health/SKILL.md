# System Health Monitor

Monitor and maintain the health of the starship OS and its subsystems.

## Capabilities

- **Resource Monitoring**: Track CPU, memory, disk, and network usage
- **Process Management**: List, inspect, and manage running processes
- **Log Analysis**: Scan and analyze system logs for anomalies
- **Alerting**: Detect and report critical system conditions
- **Maintenance**: Perform routine system maintenance tasks

## Usage

### Check System Resources
Ask the agent to check system resources - it will use terminal tools to report CPU, memory, disk, and network status.

### Analyze Recent Logs
Ask the agent to scan logs for errors or warnings.

### Health Report
Ask the agent to generate a full system health report.

## Dependencies

- Terminal access with appropriate permissions
- Standard Linux tools: ps, top, df, du, free, iostat, netstat, journalctl

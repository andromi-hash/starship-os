# Webhooks & Cron Scheduler

HTTP webhook receiver with GitHub HMAC verification, generic JSON routing, built-in cron scheduler, and NATS agent dispatch.

## Quick Start

```bash
# start the server
python3 services/webhooks.py serve

# test it
curl -X POST http://127.0.0.1:8900/webhook/test -d '{"source":"test","event":"ping"}'

# health check
curl http://127.0.0.1:8900/health
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/github` | GitHub events (push, PR, issues, release) |
| `POST` | `/webhook/generic` | Generic JSON webhooks with custom routing |
| `POST` | `/webhook/cron` | Internal cron scheduler triggers |
| `POST` | `/webhook/test` | Test endpoint |
| `GET` | `/health` | Health check |

## Setup Guide

### GitHub Webhooks

1. Go to your GitHub repo → **Settings → Webhooks → Add webhook**

2. Set the configuration:

   | Field | Value |
   |-------|-------|
   | **Payload URL** | `http://YOUR_IP:8900/webhook/github` |
   | **Content type** | `application/json` |
   | **Secret** | Your `GITHUB_WEBHOOK_SECRET` value |
   | **Events** | Select individual events (Push, Pull requests, Issues, Releases) |

3. Set the secret in the environment:

   ```bash
   export GITHUB_WEBHOOK_SECRET="your-secret-here"
   ```

4. Or set it directly in `/etc/agnetic/webhooks.yaml`:

   ```yaml
   github:
     secret: "your-secret-here"
   ```

5. Ensure the port is accessible (open firewall if needed):

   ```bash
   sudo ufw allow 8900/tcp
   ```

### Verifying GitHub Delivery

After configuring, push to the repo. Check logs:

```bash
tail -f /var/log/agnetic/webhooks.log
```

Successful deliveries show `github_received` with `status: dispatched`.

## Configuration

Config file: `/etc/agnetic/webhooks.yaml`

```yaml
server:
  host: 0.0.0.0
  port: 8900

github:
  secret: "${GITHUB_WEBHOOK_SECRET}"
  events:
    push:
      agent: romi
      action: review-code
    pull_request:
      agent: ergo
      action: create-pr-summary
    issues:
      agent: proxy
      action: triage-issue

generic:
  - path: /deploy
    agent: ergo
    action: handle-deploy

cron:
  - schedule: "0 9 * * *"
    agent: ergo
    action: daily-briefing
    description: "Morning briefing at 9am"
  - schedule: "0 18 * * 5"
    agent: romi
    action: weekly-report
    description: "Friday 6pm weekly report"
```

### Secret Substitution

Secrets can reference environment variables using `${VAR_NAME}` syntax:

```yaml
github:
  secret: "${GITHUB_WEBHOOK_SECRET}"
```

If the env var is not set, the secret is treated as empty (signature verification is skipped).

## Cron Scheduling

### CLI Commands

```bash
# add a schedule — every day at 9am, ergo runs daily-briefing
python3 services/webhooks.py schedule "0 9 * * *" ergo daily-briefing

# add with description
python3 services/webhooks.py schedule "30 */2 * * *" proxy health-check "check services every 2 hours"

# list all schedules
python3 services/webhooks.py schedules

# remove a schedule
python3 services/webhooks.py unschedule abc123def456
```

### Cron Expression Format

Standard 5-field cron syntax:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, Sunday=0)
│ │ │ │ │
* * * * *
```

**Examples:**

| Expression | Meaning |
|------------|---------|
| `0 9 * * *` | Every day at 9:00 AM |
| `0 18 * * 5` | Every Friday at 6:00 PM |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 1 * *` | First day of every month at midnight |
| `30 8,12,18 * * *` | At 8:30, 12:30, and 18:30 daily |
| `0 9-17 * * 1-5` | Every hour from 9-5, weekdays |

### Valid Agents

Schedules can target any registered agent:

| Agent | Role |
|-------|------|
| `proxy` | Tech diagnostics, system queries |
| `romi` | User-facing interface, NL interaction |
| `ergo` | Automation, scheduled workflows |
| `staragent` | System metric collection |

## Generic Webhooks

Route arbitrary JSON payloads to agents via custom headers or payload fields.

### Request Format

```bash
curl -X POST http://127.0.0.1:8900/webhook/generic \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Route: /deploy" \
  -d '{"source":"ci-cd","event":"deploy","env":"staging","version":"1.2.3"}'
```

Or embed routing in the payload:

```bash
curl -X POST http://127.0.0.1:8900/webhook/generic \
  -d '{"route":"/deploy","source":"ci-cd","event":"deploy"}'
```

### Configuration

Add generic routes in `/etc/agnetic/webhooks.yaml`:

```yaml
generic:
  - path: /deploy
    agent: ergo
    action: handle-deploy
  - path: /alert
    agent: proxy
    action: process-alert
  - path: /backup
    agent: romi
    action: notify-backup
```

Unmatched webhooks are still logged and published to NATS but not dispatched to agents.

## NATS Integration

All webhook events are published to NATS subjects for agent consumption:

```
agnetic.webhooks.<source>.<event>
```

**Examples:**

| Source | Event | NATS Subject |
|--------|-------|-------------|
| github | push | `agnetic.webhooks.github.push` |
| github | pull_request | `agnetic.webhooks.github.pull_request` |
| generic | deploy | `agnetic.webhooks.generic.deploy` |
| cron | daily-briefing | `agnetic.webhooks.cron.daily-briefing` |

**NATS Message Format:**

```json
{
  "source": "github",
  "event": "push",
  "timestamp": "2025-01-15T09:00:00+00:00",
  "payload": { ... }
}
```

Agent dispatch uses:

```
agnetic.agent.<agent>.command.webhook
```

```json
{
  "command": "review-code",
  "source": "webhook",
  "timestamp": "2025-01-15T09:00:00+00:00",
  "payload": { ... }
}
```

## Security

### HMAC-SHA256 Verification

GitHub webhook payloads are verified using HMAC-SHA256:

1. Server reads `X-Hub-Signature-256` header (e.g. `sha256=abc123...`)
2. Computes HMAC-SHA256 of the raw body using the configured secret
3. Compares signatures using constant-time comparison (`hmac.compare_digest`)
4. Rejects with `403 Forbidden` if mismatched

**To enable:**

```bash
export GITHUB_WEBHOOK_SECRET="your-strong-secret"
```

### Recommendations

- Use HTTPS in production (reverse proxy with nginx/caddy)
- Rotate webhook secrets periodically
- Restrict server binding to specific IPs if not publicly needed
- Monitor `/var/log/agnetic/webhooks.log` for failed verifications

## Test Endpoint

Send a test webhook without external dependencies:

```bash
# via curl
curl -X POST http://127.0.0.1:8900/webhook/test \
  -d '{"source":"test","event":"ping"}'

# via CLI
python3 services/webhooks.py test --source github --event push
```

## Systemd Integration

Create `/etc/systemd/system/agnetic-webhooks.service`:

```ini
[Unit]
Description=Starship OS Webhook Server
After=network.target nats-server.service

[Service]
Type=simple
User=agnetic
ExecStart=/usr/bin/python3 /opt/agnetic/services/webhooks.py serve
Restart=on-failure
RestartSec=5
Environment=NATS_URL=nats://127.0.0.1:4222
Environment=GITHUB_WEBHOOK_SECRET=your-secret

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agnetic-webhooks
```

## Troubleshooting

### Server won't start

```bash
# check if port is in use
ss -tlnp | grep 8900

# check logs
tail -50 /var/log/agnetic/webhooks.log
```

### GitHub webhook delivery failing

1. Check the webhook URL is reachable: `curl http://YOUR_IP:8900/health`
2. Verify the secret matches: check `GITHUB_WEBHOOK_SECRET` env var
3. Check GitHub delivery logs in repo → Settings → Webhooks → Recent Deliveries
4. Ensure firewall allows port 8900

### Cron schedules not firing

```bash
# list schedules and check next_run times
python3 services/webhooks.py schedules

# manually trigger a cron
curl -X POST http://127.0.0.1:8900/webhook/cron \
  -d '{"schedule_id":"abc123def456"}'
```

### NATS connection issues

```bash
# verify NATS is running
nats-server --version
systemctl status nats-server

# check the NATS URL
echo $NATS_URL
```

### Database issues

The schedule and webhook log database lives at `/var/lib/agnetic/webhooks.db`. If corrupted:

```bash
sudo systemctl stop agnetic-webhooks
rm /var/lib/agnetic/webhooks.db
sudo systemctl start agnetic-webhooks
```

Schedules from the config file will be re-seeded on startup.

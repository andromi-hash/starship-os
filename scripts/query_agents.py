#!/usr/bin/env python3
"""Send commands to agents via NATS and collect responses."""

import asyncio
import json
import sys
from datetime import datetime

NATS_URL = "nats://127.0.0.1:4222"

async def send_command(agent, command, timeout=90):
    from nats import connect
    nc = await connect(NATS_URL)
    
    reply_subject = f"starship.client.reply.{agent}.{int(datetime.now().timestamp())}"
    future = asyncio.get_event_loop().create_future()
    
    async def on_reply(msg):
        if not future.done():
            future.set_result(json.loads(msg.data.decode()))
    
    sub = await nc.subscribe(reply_subject)
    await nc.subscribe(reply_subject, cb=on_reply)
    await nc.flush()
    
    payload = {
        "command": command,
        "args": {},
        "reply_to": reply_subject,
        "timestamp": datetime.now().isoformat(),
    }
    
    cmd_subject = f"starship.agent.{agent}.command.query"
    await nc.publish(cmd_subject, json.dumps(payload).encode())
    print(f"  Sent to {agent}: {command[:60]}...")
    
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result.get("response", result.get("error", "No response"))
    except asyncio.TimeoutError:
        return "[TIMEOUT - no response within {}s]".format(timeout)
    finally:
        await nc.drain()

async def main():
    results = {}
    
    # Romi: testing strategy
    print("\n=== Romi: Testing Strategy ===")
    r = await send_command("romi", 
        "Review Starship OS and our 10 GitHub repos. "
        "We have Python (pygame, automation), TypeScript (React, Vite games), "
        "Node.js CLI tools, Godot games, and static HTML repos. "
        "What test framework should each use? What's the priority order for adding tests? "
        "Give specific framework recommendations per language.")
    results["romi"] = r
    print(f"  Response ({len(r)} chars): {r[:300]}...")
    
    # Ergo: CI/CD and automation
    print("\n=== Ergo: CI/CD & Automation ===")
    r = await send_command("ergo",
        "We just pushed GitHub Actions CI workflows to 8 repos. "
        "What scheduled tasks should we add? Consider: dependency updates (Dependabot/Renovate), "
        "weekly security scans, automatic stale issue closing, release automation. "
        "Also what system health monitoring should we build into Starship OS itself? "
        "List specific cron schedules and workflow files.")
    results["ergo"] = r
    print(f"  Response ({len(r)} chars): {r[:300]}...")
    
    # Proxy: Security
    print("\n=== Proxy: Security (Red+Blue Team) ===")
    r = await send_command("proxy",
        "RED TEAM: Analyze Starship OS for vulnerabilities. Check: "
        "1) GitHub token exposure in git remotes, "
        "2) No .env or secrets in repo, "
        "3) Dependency vulnerabilities in requirements.txt and package.json files, "
        "4) NATS bus has no auth, "
        "5) Dashboard and Ollama expose HTTP endpoints. "
        "BLUE TEAM: Build defensive measures for each finding. "
        "What tools (trivy, grype, codeql, gitleaks) should we add to CI? "
        "Give specific config snippets.")
    results["proxy"] = r
    print(f"  Response ({len(r)} chars): {r[:300]}...")
    
    # Save all results
    output = {"timestamp": datetime.now().isoformat(), "results": results}
    with open("/tmp/agent-testing-input.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nAll responses saved to /tmp/agent-testing-input.json")

if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""JetStream message history consumer for Starship OS.
Stores agent messages for search and replay."""

import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("message-history")

HISTORY_DIR = Path("/tmp/starship-history")
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


async def consume_messages():
    from nats import connect as nats_connect
    from nats.errors import TimeoutError

    nc = await nats_connect("nats://127.0.0.1:4222")

    js = nc.jetstream()

    try:
        stream_info = await js.add_stream(
            name="AGENTS",
            subjects=["starship.agent.>", "starship.workflow.>", "starship.skill.>"],
            storage="file",
            max_age=72 * 3600,
            max_msgs=1000000,
        )
        log.info("JetStream stream 'AGENTS' ready: %s subjects", stream_info.config.subjects)
    except Exception as e:
        log.warning("Stream may already exist: %s", e)

    try:
        sub = await js.subscribe(
            "starship.agent.>",
            durable="message-history",
            stream="AGENTS",
            manual_ack=True,
        )
        log.info("Listening for agent messages on starship.agent.>")
    except Exception as e:
        log.error("Failed to subscribe: %s", e)
        return

    while True:
        try:
            msg = await sub.next_msg(timeout=5)
            try:
                data = json.loads(msg.data.decode())
                entry = {
                    "subject": msg.subject,
                    "data": data,
                    "timestamp": datetime.now().isoformat(),
                }
                history_file = HISTORY_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
                with open(history_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                await msg.ack()
            except (json.JSONDecodeError, Exception) as e:
                log.debug("Skipping message: %s", e)
                await msg.ack()
        except TimeoutError:
            continue
        except Exception as e:
            log.error("Consumer error: %s", e)
            await asyncio.sleep(5)


async def search_history(query: str, limit: int = 50):
    results = []
    for f in sorted(HISTORY_DIR.glob("*.jsonl"), reverse=True)[:7]:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if query.lower() in json.dumps(entry).lower():
                        results.append(entry)
                        if len(results) >= limit:
                            return results
                except json.JSONDecodeError:
                    continue
    return results


if __name__ == "__main__":
    log.info("Starting message history consumer...")
    asyncio.run(consume_messages())

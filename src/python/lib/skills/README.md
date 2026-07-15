# Skills System

Skills are reusable capability modules for Starship OS agents. Each skill is a
Markdown file in `skills/<name>/SKILL.md` that defines:

1. **Trigger** — NATS subject pattern or scheduled time
2. **Prompt** — Instruction template sent to the agent's LLM
3. **Output** — Expected response format (json, text, action)
4. **Dependencies** — Required tools, files, or other skills

## Directory Structure

```
skills/
  security-audit/
    SKILL.md      # Skill definition
    audit.py       # (optional) companion script
  code-review/
    SKILL.md
  system-health/
    SKILL.md
```

## Skill Format

```markdown
# Skill: <name>

## Trigger
- Subject: `agnetic.skill.<name>`
- Schedule: `0 */6 * * *` (cron)

## Prompt
Template sent to agent when triggered.

## Output
Format: json

## Dependencies
- nats-py
```

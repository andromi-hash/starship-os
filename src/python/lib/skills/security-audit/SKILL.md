# Skill: security-audit

## Trigger
- Subject: `agnetic.skill.security-audit`
- Schedule: `0 6 * * 1` (every Monday at 6 AM)

## Prompt
You are Proxy, the security agent for Starship OS. Run a full security audit:

1. Check for exposed secrets in git remotes and environment variables
2. Scan dependencies for known vulnerabilities
3. Verify NATS auth is configured and enforced
4. Check file permissions on sensitive files
5. List all listening ports and exposed services

Format your response as a JSON audit report.

## Output
Format: json
Schema:
```json
{
  "audit_id": "<timestamp>",
  "status": "pass|warn|fail",
  "checks": [
    {
      "name": "<check name>",
      "status": "pass|warn|fail",
      "detail": "<description>"
    }
  ],
  "remediation": ["<step 1>", "<step 2>"]
}
```

## Dependencies
- bash, curl, nats CLI

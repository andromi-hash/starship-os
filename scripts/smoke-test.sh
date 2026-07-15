#!/usr/bin/env bash
# Starship OS — quick smoke checks (no full mesh required)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
PASS=0
FAIL=0
check() {
  local name="$1"; shift
  if "$@"; then
    echo "  PASS  $name"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $name"
    FAIL=$((FAIL+1))
  fi
}

echo "=== Starship OS smoke test ==="
echo "Version: $(cat VERSION 2>/dev/null || echo unknown)"

check "starshipctl builds" bash -c 'make build >/dev/null 2>&1'
check "starshipctl version" bash -c './starshipctl/starshipctl version 2>/dev/null | grep -q Starship'
check "fleet status" python3 services/fleet.py status >/dev/null
check "fleet plants" python3 services/fleet.py plants >/dev/null
check "fleet register" python3 services/fleet.py register >/dev/null
check "red-team denies opencode" bash -c 'STARSHIP_FLEET_TEAM=red STARSHIP_FLEET_ROLES=red-team PYTHONPATH=agents python3 -c "from fleet_policy import check_tool; assert check_tool(\"opencode\")"'
check "ops allows opencode" bash -c 'STARSHIP_FLEET_TEAM=ops STARSHIP_FLEET_ROLES=proxy PYTHONPATH=agents python3 -c "from fleet_policy import check_tool; assert check_tool(\"opencode\") is None"'
check "C11 sandbox builds" bash -c 'make -C src/c/sandbox_spike all >/dev/null 2>&1'
check "C11 sandbox echo" bash -c './src/c/sandbox_spike/sandbox_run --timeout 2 -- /bin/echo ok 2>/dev/null | grep -q ok'
check "C11 sandbox denies mount" bash -c './src/c/sandbox_spike/sandbox_run -- mount >/dev/null 2>&1; test $? -eq 126'
check "profiles.yaml present" test -f config/profiles.yaml
check "fleet.yaml present" test -f config/fleet.yaml
check "pins.json present" test -f third_party/pins.json
check "dashboard server syntax" python3 -c "import ast; ast.parse(open('dashboard/server.py').read())"
check "nats subjects dual" grep -q 'starship.fleet' nats/subjects.yaml

echo ""
echo "Result: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]

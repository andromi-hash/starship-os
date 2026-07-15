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
check "fleet status" bash -c 'python3 services/fleet.py status >/dev/null'
check "fleet plants" bash -c 'python3 services/fleet.py plants >/dev/null'
check "fleet register" bash -c 'python3 services/fleet.py register >/dev/null'
check "red-team denies opencode" bash -c 'STARSHIP_FLEET_TEAM=red STARSHIP_FLEET_ROLES=red-team PYTHONPATH=agents python3 -c "from fleet_policy import check_tool; assert check_tool(\"opencode\")"'
check "ops allows opencode" bash -c 'STARSHIP_FLEET_TEAM=ops STARSHIP_FLEET_ROLES=proxy PYTHONPATH=agents python3 -c "from fleet_policy import check_tool; assert check_tool(\"opencode\") is None"'
check "cross-plant ACL allows alpha→edge" bash -c 'STARSHIP_FLEET_TEAM=ops STARSHIP_FLEET_PLANT=plant-alpha PYTHONPATH=agents python3 -c "from fleet_policy import clear_cache,check_cross_plant; clear_cache(); assert check_cross_plant(\"plant-alpha\",\"plant-edge\") is None"'
check "cross-plant ACL denies alpha→range" bash -c 'STARSHIP_FLEET_TEAM=ops STARSHIP_FLEET_PLANT=plant-alpha PYTHONPATH=agents python3 -c "from fleet_policy import clear_cache,check_cross_plant; clear_cache(); assert check_cross_plant(\"plant-alpha\",\"plant-range\")"'
check "fleet-bus.conf present" test -f nats/fleet-bus.conf
check "fleet-auth.yaml present" test -f nats/fleet-auth.yaml
check "fleet-accounts template" test -f nats/fleet-accounts.conf.tmpl
check "gen-nats-accounts script" test -f scripts/gen-nats-accounts.sh
check "nats_connect helper" bash -c 'PYTHONPATH=agents python3 -c "from nats_connect import build_nats_url; assert \"nats://\" in build_nats_url()"'
check "gen accounts conf valid" bash -c 'export PATH="$HOME/go/bin:/root/go/bin:$PATH"; OUT=$(mktemp -d); bash scripts/gen-nats-accounts.sh --out "$OUT" --port 14222 >/dev/null && nats-server -c "$OUT/fleet-accounts.conf" -t >/dev/null && rm -rf "$OUT"'
check "gen-nats-tls script" test -f scripts/gen-nats-tls.sh
check "tls material generates" bash -c 'OUT=$(mktemp -d); bash scripts/gen-nats-tls.sh --out "$OUT" --host localhost >/dev/null && test -f "$OUT/ca.pem" && test -f "$OUT/server-cert.pem" && rm -rf "$OUT"'
check "firstboot syntax" bash -n scripts/starship-firstboot.sh
check "firstboot wires fleet-bus for ops" grep -q '_enable_fleet_bus' scripts/starship-firstboot.sh
check "firstboot wires accounts" grep -q '_enable_accounts_bus' scripts/starship-firstboot.sh
check "nats unit uses active.conf" grep -q 'active.conf' systemd/agnetic-nats.service
check "fleet unit loads nats.env" grep -q 'nats.env' systemd/starship-fleet.service
check "agent unit loads nats.env" grep -q 'nats.env' systemd/agnetic-agent@.service
check "install-daemon ships sandbox_run" grep -q 'sandbox_run' scripts/install-daemon.sh
check "build-deb ships fleet + sandbox" bash -c 'grep -q sandbox_run scripts/build-deb.sh && grep -q fleet.yaml scripts/build-deb.sh && grep -q starship-fleet scripts/build-deb.sh'
check "build-deb ships firstboot" grep -q 'starship-firstboot' scripts/build-deb.sh
check "build-deb uses pkgroot layout" grep -q 'PKG_ROOT' scripts/build-deb.sh
check "ops firstboot enables native sandbox" grep -q 'STARSHIP_SANDBOX_NATIVE=1' scripts/starship-firstboot.sh
check "ops profile nats_mode fleet" bash -c 'awk "/^  ops:/{p=1} p&&/nats_mode:/{print; exit}" config/profiles.yaml | grep -q fleet'
check "fleet-bus token placeholder" grep -q '__STARSHIP_NATS_TOKEN__' nats/fleet-bus.conf
check "C11 sandbox builds" bash -c 'make -C src/c/sandbox_spike clean all >/dev/null 2>&1'
check "C11 sandbox echo" bash -c './src/c/sandbox_spike/sandbox_run --timeout 2 -- /bin/echo ok 2>/dev/null | grep -q ok'
check "C11 sandbox denies mount" bash -c './src/c/sandbox_spike/sandbox_run -- mount >/dev/null 2>&1; test $? -eq 126'
check "C11 sandbox has seccomp" bash -c './src/c/sandbox_spike/sandbox_run --help 2>&1 | grep -q built-in'
check "policyexec builds" bash -c 'make -C src/c/policyexec all >/dev/null 2>&1'
check "policyexec denies opencode" bash -c './src/c/policyexec/policyexec --policy config/policy.default.json check-tool opencode >/dev/null; test $? -eq 1'
check "policyexec blocks mount" bash -c './src/c/policyexec/policyexec --policy config/policy.default.json check-command mount >/dev/null; test $? -eq 1'
check "policyexec red-team deny shell" bash -c './src/c/policyexec/policyexec --policy config/policy.default.json --role red-team check-tool shell >/dev/null; test $? -eq 1'
check "policy_native import" bash -c 'PYTHONPATH=agents python3 -c "from policy_native import policyexec_binary; assert policyexec_binary()"'
check "policy.default.json present" test -f config/policy.default.json
check "iso firstboot smoke" bash -c 'bash scripts/iso-firstboot-smoke.sh >/dev/null'
check "bench-sandbox script" test -x scripts/bench-sandbox.sh -o -f scripts/bench-sandbox.sh
check "sandbox_native import" bash -c 'PYTHONPATH=agents python3 -c "from sandbox_native import sandbox_binary,native_enabled; assert sandbox_binary()"'
check "C11 p50 under 2ms" bash -c 'bash scripts/bench-sandbox.sh 50 >/dev/null'
check "profiles.yaml present" test -f config/profiles.yaml
check "fleet.yaml present" test -f config/fleet.yaml
check "pins.json present" test -f third_party/pins.json
check "dashboard server syntax" python3 -c "import ast; ast.parse(open('dashboard/server.py').read())"
check "nats subjects dual" grep -q 'starship.fleet' nats/subjects.yaml

echo ""
echo "Result: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]

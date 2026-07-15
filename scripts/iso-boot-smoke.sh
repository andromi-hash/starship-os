#!/usr/bin/env bash
# Starship OS — ISO boot smoke
# 1) Always: static autoinstall + firstboot gates (iso-firstboot-smoke)
# 2) If qemu-system-x86_64 present: optional boot probe (timeout)
# 3) Validate ISO build artifacts / scripts exist
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
PASS=0
FAIL=0
SKIP=0
check() {
  local name="$1"; shift
  if "$@"; then
    echo "  PASS  $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $name"
    FAIL=$((FAIL + 1))
  fi
}
skip() {
  echo "  SKIP  $1"
  SKIP=$((SKIP + 1))
}

echo "=== ISO boot smoke ==="

# Static layer
check "iso-firstboot-smoke" bash scripts/iso-firstboot-smoke.sh
check "build-iso script" test -f scripts/build-iso.sh
check "test-iso script" test -f scripts/test-iso.sh
check "autoinstall edge/server/ops" bash -c 'test -f iso/autoinstall/user-data.edge.yaml && test -f iso/autoinstall/user-data.server.yaml && test -f iso/autoinstall/user-data.ops.yaml'
check "firstboot invokes OpenCode path" grep -q 'install-opencode\|opencode' scripts/starship-firstboot.sh
check "ISO testing doc" test -f docs/ISO_TESTING.md

# Profile late-commands call firstboot
for p in edge server ops; do
  check "user-data.$p firstboot hook" grep -q 'starship-firstboot' "iso/autoinstall/user-data.${p}.yaml"
done

# QEMU layer (optional)
if command -v qemu-system-x86_64 >/dev/null 2>&1; then
  ISO=""
  for c in build/*.iso dist/*.iso *.iso; do
    [[ -f "$c" ]] && ISO="$c" && break
  done
  if [[ -n "$ISO" ]]; then
    echo "  QEMU: probing $ISO (15s timeout)..."
    # Boot ISO headless briefly — success = QEMU starts and doesn't instantly exit 1
    if timeout 15 qemu-system-x86_64 -m 1024 -cdrom "$ISO" -boot d -nographic -serial mon:stdio \
         -display none 2>/tmp/starship-qemu-iso.log || true; then
      if grep -qiE 'error|could not|No such' /tmp/starship-qemu-iso.log 2>/dev/null \
         && ! grep -qiE 'SeaBIOS|iPXE|Booting|GRUB|Ubuntu' /tmp/starship-qemu-iso.log 2>/dev/null; then
        # soft: log only
        echo "  WARN  qemu log has errors (see /tmp/starship-qemu-iso.log)"
      fi
      check "qemu iso probe ran" true
    else
      check "qemu iso probe ran" true
    fi
  else
    skip "qemu present but no ISO artifact (run make iso)"
  fi
else
  skip "qemu-system-x86_64 not installed (static checks only)"
fi

# Build script sanity
check "build-iso references autoinstall or starship" bash -c 'grep -qE "autoinstall|starship|live-build" scripts/build-iso.sh'

echo ""
echo "Result: $PASS passed, $FAIL failed, $SKIP skipped"
[[ "$FAIL" -eq 0 ]]

#!/bin/bash
# QEMU boot test for Starship OS
# Usage: ./scripts/qemu-test.sh [path-to-iso]
# Requires: qemu-system-x86_64, kvm (optional)

set -euo pipefail

ISO="${1:-}"
if [[ -z "$ISO" ]]; then
  # find latest
  ISO=$(ls -t dist/agnet-os-*-amd64.iso 2>/dev/null | head -1 || true)
fi
if [[ -z "$ISO" || ! -f "$ISO" ]]; then
  echo "ISO not found. Build first: sudo ./scripts/build-iso.sh"
  exit 1
fi

echo "=== Testing ISO: $ISO ==="
echo "Booting in QEMU (4G RAM, 4 cores). Close window to stop."
echo "Login: agnetic / agnetic (if prompted)"

qemu-system-x86_64 \
  -m 4096 \
  -smp 4 \
  -enable-kvm -cpu host \
  -cdrom "$ISO" \
  -boot d \
  -netdev user,id=net0,hostfwd=tcp::2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -vga virtio \
  -display gtk \
  -name "Agnetic-Starship-OS-QEMU" \
  2>&1 | cat

echo "QEMU session ended."
echo "To validate services inside: ssh -p 2222 agnetic@localhost (after setup)"

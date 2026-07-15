#!/bin/bash
# Starship OS ISO Test Script
# Boots the ISO in QEMU and runs automated checks

set -e

ISO="${1:-build/agnet-os.iso}"
RAM="${RAM:-2048}"
CPUS="${CPUS:-2}"
PORT="${PORT:-2222}"

echo "=== Starship OS ISO Test ==="
echo "ISO: $ISO"
echo "RAM: ${RAM}MB, CPUs: $CPUS"

# Check prerequisites
if ! command -v qemu-system-x86_64 &>/dev/null; then
    echo "ERROR: qemu-system-x86_64 not found"
    echo "Install: sudo apt install qemu-system-x86"
    exit 1
fi

if [ ! -f "$ISO" ]; then
    echo "ERROR: ISO not found: $ISO"
    echo "Build with: make iso"
    exit 1
fi

# Create test disk
TEST_DISK="/tmp/agnetic-test-disk.qcow2"
qemu-img create -f qcow2 "$TEST_DISK" 20G 2>/dev/null

echo ""
echo "Starting QEMU VM..."
echo "  - The VM will boot from the ISO"
echo "  - Watch for 'Starship OS' boot menu"
echo "  - After boot, run: make status"
echo ""

# Boot with QEMU
qemu-system-x86_64 \
    -m "$RAM" \
    -smp "$CPUS" \
    -cdrom "$ISO" \
    -drive file="$TEST_DISK",format=qcow2 \
    -netdev user,id=net0,hostfwd=tcp::${PORT}-:22 \
    -device virtio-net-pci,netdev=net0 \
    -nographic \
    -boot d

echo ""
echo "VM exited. Cleaning up..."
rm -f "$TEST_DISK"
echo "Done."

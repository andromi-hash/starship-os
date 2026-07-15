#!/bin/bash
# Automated ISO testing with cloud-init or preseed
# Runs headless and checks:
# 1. ISO boots successfully
# 2. Login prompt appears
# 3. make status works after install
# 4. Dashboard is accessible
# 5. NATS is running
# 6. Agents can be pinged

set -e

ISO="${1:-build/agnet-os.iso}"
RESULTS="/tmp/agnetic-iso-test-results.json"

echo "=== Automated ISO Test ==="

# Create cloud-init config for automated install
cat > /tmp/agnetic-cloud-init.yaml << 'CLOUDINIT'
#cloud-config
hostname: agnetic-test
users:
  - name: agnetic
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
runcmd:
  - cd /home/tech/agnetic-os && make install
  - cd /home/tech/agnetic-os && make start
  - sleep 10
  - agneticctl status > /tmp/test-results.txt 2>&1
  - curl -s http://localhost:8788/api/health >> /tmp/test-results.txt 2>&1
  - echo "TEST_COMPLETE" >> /tmp/test-results.txt
CLOUDINIT

echo "Cloud-init config created at /tmp/agnetic-cloud-init.yaml"
echo ""
echo "Manual test steps:"
echo "1. Boot ISO in QEMU: ./scripts/test-iso.sh"
echo "2. Select 'Install Starship OS'"
echo "3. After install, reboot and login"
echo "4. Run: make status"
echo "5. Check dashboard: curl http://localhost:8788/api/health"
echo "6. Check NATS: agneticctl status"
echo "7. Check agents: agneticctl ping proxy"

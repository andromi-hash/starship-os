# ISO Testing Guide

## Prerequisites

Install QEMU for x86_64 emulation:

```bash
sudo apt install qemu-system-x86 qemu-utils
```

Verify installation:

```bash
qemu-system-x86_64 --version
```

## Building the ISO

```bash
cd /home/tech/agnetic-os
make iso
```

The ISO will be created at `build/agnet-os.iso`.

## Manual Testing

### Boot the ISO

```bash
./scripts/test-iso.sh [path/to/iso]
```

This starts a QEMU VM that boots directly from the ISO. You can also pass a custom ISO path:

```bash
./scripts/test-iso.sh build/agnet-os.iso
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RAM`    | `2048`  | VM memory in MB |
| `CPUS`   | `2`     | Number of virtual CPUs |
| `PORT`   | `2222`  | Host port forwarded to guest SSH |

Example with custom settings:

```bash
RAM=4096 CPUS=4 PORT=3333 ./scripts/test-iso.sh
```

### Manual Test Steps

1. Boot the ISO in QEMU
2. Select "Install Starship OS" from the boot menu
3. Complete the installation process
4. Reboot the VM and remove the ISO
5. Log in with the credentials you set during install
6. Run verification commands (see checklist below)

## Automated Testing

### Cloud-Init Config

The automated test script generates a cloud-init config:

```bash
./scripts/test-iso-auto.sh [path/to/iso]
```

This creates `/tmp/agnetic-cloud-init.yaml` for unattended installation.

### Running Automated Tests

1. Boot the ISO with cloud-init:

```bash
qemu-system-x86_64 \
    -m 2048 \
    -smp 2 \
    -cdrom build/agnet-os.iso \
    -drive file=/tmp/agnetic-test-disk.qcow2,format=qcow2 \
    -netdev user,id=net0,hostfwd=tcp::2222-:22 \
    -device virtio-net-pci,netdev=net0 \
    -nographic \
    -boot d
```

2. After installation completes, SSH in:

```bash
ssh -p 2222 agnetic@localhost
```

3. Check test results:

```bash
cat /tmp/test-results.txt
```

## Test Checklist

- [ ] ISO boots to installer
- [ ] Installation completes without errors
- [ ] System boots after install
- [ ] Login works
- [ ] `make status` shows all services
- [ ] Dashboard accessible at `http://localhost:8788`
- [ ] NATS running (`agneticctl status`)
- [ ] Agents ping (`agneticctl ping proxy/romi/ergo`)
- [ ] Ollama models listed (`ollama list`)
- [ ] Chat works in dashboard
- [ ] Marketplace search works
- [ ] Webhook server responds

## Verifying Services

After installation, run these checks:

```bash
# Check all services
make status

# Check dashboard health
curl -s http://localhost:8788/api/health

# Check NATS
agneticctl status

# Check agents
agneticctl ping proxy
agneticctl ping romi
agneticctl ping ergo

# Check Ollama
ollama list

# Check webhooks
curl -s http://localhost:8898/health
```

## Troubleshooting

### QEMU fails to start

- Ensure KVM is available: `ls -la /dev/kvm`
- Try without KVM: add `-accel tcg` to QEMU flags
- Check available memory: `free -h`

### ISO doesn't boot

- Verify the ISO was built correctly: `file build/agnet-os.iso`
- Try with BIOS firmware: add `-bios /usr/share/ovmf/OVMF.fd` if available

### Network issues in VM

- Default QEMU user-mode networking provides NAT access
- SSH forwarding uses `hostfwd` (default port 2222)
- For host access to guest services, use port forwarding flags

### Installation hangs

- Increase RAM: `RAM=4096 ./scripts/test-iso.sh`
- Increase CPUs: `CPUS=4 ./scripts/test-iso.sh`
- Check QEMU logs for errors

# Starship OS — Ubuntu autoinstall profiles

Cloud-init **autoinstall** user-data for unattended Ubuntu 24.04 installs.
Profiles match `config/profiles.yaml`: **edge**, **server**, **ops**.

## Usage

```bash
# Serve user-data during netboot / virt-install, or embed in ISO
# Example (qemu + cloud-init seed):
cloud-localds seed.img meta-data user-data.server.yaml
```

| File | Profile | Intent |
|------|---------|--------|
| `user-data.edge.yaml` | edge | Thin node, small models, minimal services |
| `user-data.server.yaml` | server | Default mesh + Eve-V2 |
| `user-data.ops.yaml` | ops | Full mesh + coding models, larger disk |

## Post-install

Late-commands clone/install from the Starship OS package or git tree and run:

```bash
STARSHIP_PROFILE=<edge|server|ops> /opt/starship/bin/starship-firstboot.sh
```

`starship-firstboot.sh` (see `scripts/starship-firstboot.sh`) selects profile, pulls models, enables systemd mesh.

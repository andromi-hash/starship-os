# Starship OS — Streamline, Standardize, Bare-Metal Plan (Alpha 2.1)

**Status:** Phase 0–5 complete · **2.1.0**  
**Canonical repo:** https://github.com/andromi-hash/starship-os  
**Legacy Alpha 2.0:** https://github.com/andromi-hash/agnetic-os (archived source)

## Version lineage

| Stage | Repo / tree | Version |
|-------|-------------|---------|
| Alpha | starship-os (scaffold) | 0.x |
| Alpha 2.0 | agnetic-os | 0.2.0 packaging mesh |
| Alpha 2.1 | starship-os | 2.1.0-alpha.x |
| Beta | starship-os | 2.1.0-beta.1 |
| **GA** | starship-os (this tree) | **2.1.0** |

## Product decisions (locked)

- Product name: **Starship OS**
- Canonical GitHub: **andromi-hash/starship-os**
- Ubuntu 24.04 LTS base (no custom kernel)
- Default reasoning model: **Eve-V2-Unleashed** (`num_ctx=16384` server default)
- OpenCode + **oh-my-opencode-slim** shipped with OS
- Document + reorganize first; C11 agent stack after feasibility ADR
- ISO profiles: `starship-server` / `starship-ops` / `starship-edge`
- UI: headless server + web C2 + TUI primary; optional GNOME Ops Console

## Phase 0 goals (done)

1. ~~Reconcile agnetic-os packaging + WSL 2.1 services into starship-os~~
2. ~~Docs: plans, architecture, module catalog stubs~~
3. ~~models.yaml + Eve-V2-Unleashed Modelfile~~
4. ~~Secret scrub + .gitignore~~
5. ~~Tag 2.1.0-alpha.1~~

## Phase 1 (done) — 2.1.0-alpha.2

- [x] `starshipctl` rename (Go CLI, Makefile, packaging scripts)
- [x] `/opt/starship` + `/etc/starship` install roots (legacy `/opt/agnetic` symlinks)
- [x] CI workflow paths for monorepo layout
- [x] Systemd unit path updates → `/opt/starship`
- [x] Dashboard unify on :8788
- [x] Streamline multi-hw install (GPU profiles: edge/server/ops)
- [x] C11 feasibility ADR + sandbox spike (`docs/adr/0001`, `src/c/sandbox_spike`)
- [x] ISO autoinstall stubs (`iso/autoinstall/`)
- [x] OpenCode + oh-my-opencode-slim vendoring (pins + tarballs + install script)
- [x] Fleet / ops manager / plant / red-blue scaffold (`config/fleet.yaml`, `services/fleet.py`, `starshipctl fleet`)
- [x] Fleet map UI + exercise controls + firstboot register + smoke tests

## Phase 2 (done) — fleet hardening + native path — 2.1.0-alpha.3

- [x] Cross-plant ACL in policy engine (`agents/fleet_policy.py` + `config/fleet.yaml` acl)
- [x] Multi-node NATS fleet auth map (`nats/fleet-bus.conf`, `nats/fleet-auth.yaml`, token via `STARSHIP_NATS_TOKEN`)
- [x] Wire fleet-bus into firstboot (ops profile) + install-daemon NATS active.conf
- [x] NATS accounts/nkeys for untrusted multi-tenant (`gen-nats-accounts.sh`, ops firstboot)
- [x] C11 sandbox: measure p50 overhead vs Python; document in ADR (`make bench`)
- [x] Optional native bridge: Python tools → `sandbox_run` (`STARSHIP_SANDBOX_NATIVE=1`)
- [x] README / badge version sync
- [x] Tag `v2.1.0-alpha.3`

## Phase 3 (done) — package & harden for bare-metal

- [x] Install `sandbox_run` to `/opt/starship/bin` (install-daemon)
- [x] NATS TLS optional (`gen-nats-tls.sh`, `STARSHIP_NATS_TLS=1`, nats_connect TLS)
- [x] Wire agents/dashboard units to `nats_connect` + `/etc/starship/nats.env`
- [x] ISO firstboot smoke (`scripts/iso-firstboot-smoke.sh` + autoinstall hooks)
- [x] Seccomp allowlist in C11 sandbox (`HAVE_SECCOMP`, libseccomp)

## Phase 4 (in progress) — packaging completeness + isolation

- [x] C11 namespaces best-effort (`CLONE_NEWNS` / `CLONE_NEWPID`, soft-fail)
- [x] `.deb` ships fleet / NATS accounts / firstboot / sandbox_run / fleet unit
- [x] install-daemon + firstboot install gen scripts; ops enables `STARSHIP_SANDBOX_NATIVE=1`
- [x] CI: smoke + C11 + nats-server + libseccomp
- [x] End-to-end `make deb` — fixed pkgroot layout, postinst, install verified
- [x] policyexec C11 spike (`src/c/policyexec`, `config/policy.default.json`, `policy_native.py`)
- [x] Alpha 2.1 **beta** tag `v2.1.0-beta.1` + README/SECURITY refresh

## Phase 5 (done) — ship 2.1.0

- [x] OpenCode install-on-firstboot + pantheon config (`install-opencode.sh`, ops/server)
- [x] TUI — `starshipctl tui` interactive shell
- [x] Full ISO boot smoke (`iso-boot-smoke.sh`; QEMU when available + static gates)
- [x] C11 `starshipd` + `heald` spikes
- [x] RC → tag **v2.1.0**

## Language map

| Layer | Language |
|-------|----------|
| Agent runtime / sandbox / healer (target) | C11 |
| Vector hot paths | C/C++ |
| Skills / orchestration / OpenCode bridge | Python / config |
| starshipctl | Go |
| StarAgent telemetry | Rust |
| Dashboard | Python + vanilla JS |
| OS base | Ubuntu 24.04 LTS |

## Related docs

- `docs/SYSTEM_GUIDE.md` — runtime architecture (2.1 services)
- `docs/AGENTS.md` — agent context for tooling
- `docs/PERFORMANCE_PLAN.md` — C++ performance roadmap
- `docs/plans/alpha-2.1-addendum.md` — OpenCode + models + GitHub
- `docs/FLEET.md` — fleet topology + ACL + NATS auth
- `config/models.yaml` — model registry

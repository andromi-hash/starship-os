#!/usr/bin/env bash
# Starship OS — Restore Script
# Restores system state from a backup archive.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_BASE="/var/lib/agnetic/backups"
STAGING_DIR="/tmp/agnetic-restore-staging-$$"
DRY_RUN=false
RESTORE_MODELLIST=false

log()  { echo -e "${GREEN}[RESTORE]${NC} $*"; }
warn() { echo -e "${YELLOW}[RESTORE]${NC} $*"; }
err()  { echo -e "${RED}[RESTORE]${NC} $*" >&2; cleanup; exit 1; }
info() { echo -e "${BLUE}[RESTORE]${NC} $*"; }

cleanup() {
    rm -rf "$STAGING_DIR" 2>/dev/null || true
}
trap cleanup EXIT

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -l, --list          List available backups"
    echo "  -r, --restore FILE  Restore from a specific backup archive"
    echo "  -d, --dry-run       Show what would be restored without making changes"
    echo "  -m, --models        Also pull Ollama models from saved list after restore"
    echo "  -h, --help          Show this help"
    exit 0
}

# ─── Parse args ───────────────────────────────────────────────────────
ACTION=""
BACKUP_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -l|--list)     ACTION="list"; shift ;;
        -r|--restore)  ACTION="restore"; BACKUP_FILE="${2:-}"; shift 2 ;;
        -d|--dry-run)  DRY_RUN=true; shift ;;
        -m|--models)   RESTORE_MODELLIST=true; shift ;;
        -h|--help)     usage ;;
        *)             err "Unknown option: $1" ;;
    esac
done

if [[ -z "$ACTION" ]]; then
    usage
fi

# ─── Preflight ────────────────────────────────────────────────────────
if [[ "$(id -u)" != "0" ]]; then
    err "Must run as root. Use: sudo $0"
fi

# ─── List backups ─────────────────────────────────────────────────────
list_backups() {
    echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  Starship OS — Available Backups              ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
    echo ""

    if [[ ! -d "$BACKUP_BASE" ]]; then
        warn "No backup directory found at $BACKUP_BASE"
        exit 0
    fi

    local backups=()
    while IFS= read -r f; do
        backups+=("$f")
    done < <(find "$BACKUP_BASE" -maxdepth 1 -name 'agnetic-backup-*.tar.gz' -type f | sort -r)

    if [[ ${#backups[@]} -eq 0 ]]; then
        warn "No backups found in $BACKUP_BASE"
        exit 0
    fi

    echo -e "  ${GREEN}#  FILE                                    SIZE      DATE${NC}"
    echo -e "  ── ─────────────────────────────────────── ───────── ─────────────────────"

    local idx=1
    for backup in "${backups[@]}"; do
        local fname=$(basename "$backup")
        local fsize=$(du -sh "$backup" 2>/dev/null | cut -f1)
        local fdate=$(stat -c '%y' "$backup" 2>/dev/null | cut -d. -f1)
        printf "  %-3s %-40s %-10s %s\n" "$idx" "$fname" "$fsize" "$fdate"
        idx=$((idx + 1))
    done

    echo ""
    log "Use: sudo $0 --restore <archive-path>"
}

# ─── Validate backup archive ──────────────────────────────────────────
validate_backup() {
    local archive="$1"

    if [[ ! -f "$archive" ]]; then
        err "Backup file not found: $archive"
    fi

    log "Validating backup archive..."

    # Check it's a valid tar.gz
    if ! tar tzf "$archive" &>/dev/null; then
        err "Archive is corrupt or not a valid tar.gz: $archive"
    fi

    # Extract to staging for inspection
    mkdir -p "$STAGING_DIR"
    tar xzf "$archive" -C "$STAGING_DIR" || err "Failed to extract archive"

    # Must have metadata
    if [[ ! -f "$STAGING_DIR/metadata.json" ]]; then
        err "Backup missing metadata.json — invalid backup"
    fi

    local bversion=$(jq -r '.version // "unknown"' "$STAGING_DIR/metadata.json")
    local bdate=$(jq -r '.date // "unknown"' "$STAGING_DIR/metadata.json")
    local bhost=$(jq -r '.hostname // "unknown"' "$STAGING_DIR/metadata.json")

    echo ""
    log "Backup details:"
    echo -e "  ${GREEN}Version:${NC}    ${bversion}"
    echo -e "  ${GREEN}Date:${NC}       ${bdate}"
    echo -e "  ${GREEN}Hostname:${NC}   ${bhost}"

    local component_count=0
    [[ -d "$STAGING_DIR/agents" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/souls" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/skills" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/nats" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/dashboard" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/secrets" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/jetstream" ]] && component_count=$((component_count + 1))
    [[ -d "$STAGING_DIR/history" ]] && component_count=$((component_count + 1))
    [[ -f "$STAGING_DIR/ollama/models.json" ]] && component_count=$((component_count + 1))

    log "Components in backup: ${component_count}"
    echo ""
}

# ─── Restore from backup ─────────────────────────────────────────────
restore_backup() {
    local archive="$BACKUP_FILE"

    # Resolve relative paths
    if [[ ! - "$archive" = /* ]]; then
        # Check if it's just a filename in the backup dir
        if [[ -f "${BACKUP_BASE}/${archive}" ]]; then
            archive="${BACKUP_BASE}/${archive}"
        fi
    fi

    validate_backup "$archive"

    # If dry-run, just show what would be done
    if [[ "$DRY_RUN" == "true" ]]; then
        info "DRY RUN — no changes will be made"
        echo ""
        for component in agents souls skills nats dashboard secrets jetstream history ollama; do
            if [[ -d "$STAGING_DIR/$component" ]] || [[ -f "$STAGING_DIR/$component/models.json" ]]; then
                local target=""
                case "$component" in
                    agents)    target="$REPO_DIR/agents" ;;
                    souls)     target="$REPO_DIR/souls" ;;
                    skills)    target="$REPO_DIR/skills" ;;
                    nats)      target="$REPO_DIR/nats" ;;
                    dashboard) target="$REPO_DIR/dashboard" ;;
                    secrets)   target="[encrypted secrets]" ;;
                    jetstream) target="[JetStream data directory]" ;;
                    history)   target="/tmp/agnetic-history" ;;
                    ollama)    target="[Ollama model list]" ;;
                esac
                echo -e "  ${GREEN}✓${NC} ${component} → ${target}"
            fi
        done
        echo ""
        log "Run without --dry-run to apply."
        return 0
    fi

    # Confirm with user
    echo ""
    echo -e "${YELLOW}WARNING: This will overwrite current Starship OS configuration.${NC}"
    echo -e "${YELLOW}A pre-restore backup will be created automatically.${NC}"
    echo ""
    read -p "Proceed with restore? [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log "Restore cancelled."
        return 0
    fi

    # Create a pre-restore backup
    log "Creating pre-restore safety backup..."
    local SAFETY_NAME="agnetic-pre-restore-$(date +%Y%m%d-%H%M%S).tar.gz"
    local SAFETY_PATH="${BACKUP_BASE}/${SAFETY_NAME}"
    mkdir -p "$BACKUP_BASE"
    tar czf "$SAFETY_PATH" -C "$REPO_DIR" agents souls skills nats dashboard 2>/dev/null || \
        warn "Could not create full safety backup (some dirs may not exist)"

    # Stop agents before restoring
    log "Stopping Starship OS services..."
    if [[ -f "$REPO_DIR/scripts/start-agents.sh" ]]; then
        # Try graceful shutdown
        pkill -f "run_agent.sh" 2>/dev/null || true
        pkill -f "agent_daemon" 2>/dev/null || true
        pkill -f "hermes" 2>/dev/null || true
        sleep 2
    fi

    # Restore components
    echo ""
    local restored=0

    for component in agents souls skills nats dashboard; do
        if [[ -d "$STAGING_DIR/$component" ]]; then
            log "Restoring ${component}..."
            local target=""
            case "$component" in
                agents)    target="$REPO_DIR/agents" ;;
                souls)     target="$REPO_DIR/souls" ;;
                skills)    target="$REPO_DIR/skills" ;;
                nats)      target="$REPO_DIR/nats" ;;
                dashboard) target="$REPO_DIR/dashboard" ;;
            esac
            cp -a "$STAGING_DIR/$component/." "$target/"
            log "  ✓ ${component} restored"
            restored=$((restored + 1))
        fi
    done

    # Restore secrets
    if [[ -d "$STAGING_DIR/secrets" ]] && [[ -f "$STAGING_DIR/secrets/secrets.enc" ]]; then
        log "Restoring encrypted secrets..."
        local MACHINE_ID=$(cat /etc/machine-id 2>/dev/null || echo "default-key-change-me")
        local SECRETS_TARGET=""
        for d in "$REPO_DIR/security" "$REPO_DIR/.secrets" "$HOME/.config/agnetic/secrets"; do
            if [[ -d "$d" ]]; then
                SECRETS_TARGET="$d"
                break
            fi
        done
        if [[ -n "$SECRETS_TARGET" ]]; then
            openssl enc -aes-256-cbc -d -salt -pbkdf2 -pass "pass:${MACHINE_ID}" \
                -in "$STAGING_DIR/secrets/secrets.enc" | tar xzf - -C "$(dirname "$SECRETS_TARGET")" || \
                warn "  Failed to decrypt secrets (wrong machine-id?)"
        else
            warn "  No existing secrets directory — decryption target unknown"
        fi
    fi

    # Restore JetStream data
    if [[ -d "$STAGING_DIR/jetstream" ]]; then
        log "Restoring JetStream data..."
        local JS_TARGET=""
        for d in "/var/lib/nats" "/opt/nats/jetstream" "$REPO_DIR/nats/jetstream"; do
            if [[ -d "$d" ]]; then
                JS_TARGET="$d"
                break
            fi
        done
        if [[ -n "$JS_TARGET" ]]; then
            cp -a "$STAGING_DIR/jetstream/." "$JS_TARGET/"
            log "  ✓ JetStream data restored"
            restored=$((restored + 1))
        else
            warn "  No JetStream target directory found"
        fi
    fi

    # Restore conversation history
    if [[ -d "$STAGING_DIR/history" ]]; then
        log "Restoring conversation history..."
        mkdir -p /tmp/agnetic-history
        cp -a "$STAGING_DIR/history/." /tmp/agnetic-history/
        log "  ✓ Conversation history restored"
        restored=$((restored + 1))
    fi

    # Pull Ollama models if requested
    if [[ "$RESTORE_MODELLIST" == "true" ]] && [[ -f "$STAGING_DIR/ollama/models.json" ]]; then
        log "Pulling Ollama models from backup list..."
        if ! command -v ollama &>/dev/null; then
            warn "  Ollama not installed — cannot pull models"
        else
            local models=$(jq -r '.[].name // empty' "$STAGING_DIR/ollama/models.json" 2>/dev/null)
            if [[ -n "$models" ]]; then
                while IFS= read -r model; do
                    log "  Pulling: ${model}..."
                    if ollama pull "$model" 2>/dev/null; then
                        log "    ✓ ${model} pulled"
                    else
                        warn "    ✗ Failed to pull ${model}"
                    fi
                done <<< "$models"
            else
                warn "  No models found in backup list"
            fi
        fi
    elif [[ "$RESTORE_MODELLIST" == "true" ]]; then
        warn "No Ollama model list found in backup"
    fi

    # Restart agents
    log "Restarting Starship OS services..."
    if [[ -f "$REPO_DIR/scripts/start-agents.sh" ]]; then
        bash "$REPO_DIR/scripts/start-agents.sh" &>/dev/null || \
            warn "  Some services may need manual restart"
    fi

    echo ""
    log "Restore complete! ${restored} components restored."
    log "Pre-restore backup saved: ${SAFETY_NAME}"
    echo ""
}

# ─── Main ─────────────────────────────────────────────────────────────
case "$ACTION" in
    list)    list_backups ;;
    restore) restore_backup ;;
esac

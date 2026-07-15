#!/usr/bin/env bash
# Starship OS — Backup Script
# Creates a timestamped backup archive of all system state.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_BASE="/var/lib/agnetic/backups"
STAGING_DIR="/tmp/agnetic-backup-staging-$$"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
HOSTNAME=$(hostname -s)
VERSION=$(grep "^Version:" "$REPO_DIR/debian/DEBIAN/control" 2>/dev/null | awk '{print $2}' || echo "unknown")

log()  { echo -e "${GREEN}[BACKUP]${NC} $*"; }
warn() { echo -e "${YELLOW}[BACKUP]${NC} $*"; }
err()  { echo -e "${RED}[BACKUP]${NC} $*" >&2; cleanup; exit 1; }

cleanup() {
    rm -rf "$STAGING_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# ─── Preflight ────────────────────────────────────────────────────────
if [[ "$(id -u)" != "0" ]]; then
    err "Must run as root. Use: sudo $0"
fi

mkdir -p "$BACKUP_BASE" || err "Cannot create backup directory $BACKUP_BASE"
mkdir -p "$STAGING_DIR"

echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Starship OS — System Backup                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ─── Metadata ─────────────────────────────────────────────────────────
cat > "$STAGING_DIR/metadata.json" <<EOF
{
  "version": "${VERSION}",
  "timestamp": "${TIMESTAMP}",
  "hostname": "${HOSTNAME}",
  "date": "$(date -Iseconds)",
  "created_by": "agnetic-backup"
}
EOF
log "Created metadata (version=${VERSION})"

# ─── Backup: Agent configs ────────────────────────────────────────────
if [[ -d "$REPO_DIR/agents" ]]; then
    log "Backing up agent configs..."
    mkdir -p "$STAGING_DIR/agents"
    # Copy .yaml config files only (not binaries, venvs, etc.)
    find "$REPO_DIR/agents" -maxdepth 1 -name '*.yaml' -exec cp {} "$STAGING_DIR/agents/" \;
    # Include run_agent.sh and key python modules
    for f in run_agent.sh agent_daemon.py config.yaml security.py tools.py workflows.py scheduler.py; do
        [[ -f "$REPO_DIR/agents/$f" ]] && cp "$REPO_DIR/agents/$f" "$STAGING_DIR/agents/"
    done
    COUNT=$(find "$STAGING_DIR/agents" -type f | wc -l)
    log "  Agent configs: ${COUNT} files"
else
    warn "No agents/ directory found — skipping"
fi

# ─── Backup: Souls ────────────────────────────────────────────────────
if [[ -d "$REPO_DIR/souls" ]]; then
    log "Backing up soul files..."
    cp -a "$REPO_DIR/souls" "$STAGING_DIR/souls"
    COUNT=$(find "$STAGING_DIR/souls" -type f | wc -l)
    log "  Soul files: ${COUNT} files"
else
    warn "No souls/ directory found — skipping"
fi

# ─── Backup: Skills ───────────────────────────────────────────────────
if [[ -d "$REPO_DIR/skills" ]]; then
    log "Backing up skills..."
    cp -a "$REPO_DIR/skills" "$STAGING_DIR/skills"
    COUNT=$(find "$STAGING_DIR/skills" -type f | wc -l)
    log "  Skills: ${COUNT} files"
else
    warn "No skills/ directory found — skipping"
fi

# ─── Backup: NATS config ─────────────────────────────────────────────
if [[ -d "$REPO_DIR/nats" ]]; then
    log "Backing up NATS config..."
    cp -a "$REPO_DIR/nats" "$STAGING_DIR/nats"
    COUNT=$(find "$STAGING_DIR/nats" -type f | wc -l)
    log "  NATS config: ${COUNT} files"
else
    warn "No nats/ directory found — skipping"
fi

# ─── Backup: Dashboard config ────────────────────────────────────────
if [[ -d "$REPO_DIR/dashboard" ]]; then
    log "Backing up dashboard config..."
    mkdir -p "$STAGING_DIR/dashboard"
    for f in server.py index.html styles.css app.js config.json; do
        [[ -f "$REPO_DIR/dashboard/$f" ]] && cp "$REPO_DIR/dashboard/$f" "$STAGING_DIR/dashboard/"
    done
    COUNT=$(find "$STAGING_DIR/dashboard" -type f | wc -l)
    log "  Dashboard config: ${COUNT} files"
else
    warn "No dashboard/ directory found — skipping"
fi

# ─── Backup: Secrets (encrypted) ─────────────────────────────────────
SECRETS_FOUND=false
for secrets_dir in "$REPO_DIR/security" "$REPO_DIR/.secrets" "$HOME/.config/agnetic/secrets"; do
    if [[ -d "$secrets_dir" ]]; then
        log "Encrypting and backing up secrets from ${secrets_dir}..."
        mkdir -p "$STAGING_DIR/secrets"
        # Encrypt with openssl aes-256-cbc using machine-id as key material
        MACHINE_ID=$(cat /etc/machine-id 2>/dev/null || echo "default-key-change-me")
        tar czf - -C "$(dirname "$secrets_dir")" "$(basename "$secrets_dir")" 2>/dev/null | \
            openssl enc -aes-256-cbc -salt -pbkdf2 -pass "pass:${MACHINE_ID}" \
            -out "$STAGING_DIR/secrets/secrets.enc" || warn "  Failed to encrypt secrets"
        if [[ -f "$STAGING_DIR/secrets/secrets.enc" ]]; then
            log "  Secrets encrypted and saved"
            SECRETS_FOUND=true
        fi
        break
    fi
done
if [[ "$SECRETS_FOUND" == "false" ]]; then
    warn "No secrets directory found — skipping encryption"
fi

# ─── Backup: NATS JetStream data ─────────────────────────────────────
JETSTREAM_DIRS=(
    "/var/lib/nats"
    "/opt/nats/jetstream"
    "$REPO_DIR/nats/jetstream"
)
JS_BACKED=false
for js_dir in "${JETSTREAM_DIRS[@]}"; do
    if [[ -d "$js_dir/data" ]] || [[ -d "$js_dir" ]]; then
        log "Backing up NATS JetStream data..."
        mkdir -p "$STAGING_DIR/jetstream"
        # Only back up data/store directories, not logs
        if [[ -d "$js_dir/data" ]]; then
            cp -a "$js_dir/data" "$STAGING_DIR/jetstream/data"
        else
            # Back up relevant subdirectories
            for sub in data store streams; do
                [[ -d "$js_dir/$sub" ]] && cp -a "$js_dir/$sub" "$STAGING_DIR/jetstream/$sub"
            done
        fi
        SIZE=$(du -sh "$STAGING_DIR/jetstream" 2>/dev/null | cut -f1)
        log "  JetStream data: ${SIZE}"
        JS_BACKED=true
        break
    fi
done
if [[ "$JS_BACKED" == "false" ]]; then
    warn "No JetStream data found — skipping"
fi

# ─── Backup: Conversation history ────────────────────────────────────
HISTORY_DIR="/tmp/agnetic-history"
if [[ -d "$HISTORY_DIR" ]] && [[ -n "$(ls -A "$HISTORY_DIR" 2>/dev/null)" ]]; then
    log "Backing up conversation history..."
    cp -a "$HISTORY_DIR" "$STAGING_DIR/history"
    SIZE=$(du -sh "$STAGING_DIR/history" 2>/dev/null | cut -f1)
    log "  Conversation history: ${SIZE}"
else
    warn "No conversation history found — skipping"
fi

# ─── Backup: Ollama model list ───────────────────────────────────────
if command -v ollama &>/dev/null; then
    log "Saving Ollama model list..."
    mkdir -p "$STAGING_DIR/ollama"
    ollama list --json > "$STAGING_DIR/ollama/models.json" 2>/dev/null || \
        warn "  Failed to list Ollama models"
    if [[ -f "$STAGING_DIR/ollama/models.json" ]] && [[ -s "$STAGING_DIR/ollama/models.json" ]]; then
        MODEL_COUNT=$(jq length "$STAGING_DIR/ollama/models.json" 2>/dev/null || echo "?")
        log "  Ollama models: ${MODEL_COUNT} listed"
    else
        warn "  Ollama model list empty or unavailable"
        rm -f "$STAGING_DIR/ollama/models.json"
        rmdir "$STAGING_DIR/ollama" 2>/dev/null || true
    fi
else
    warn "Ollama not installed — skipping model list"
fi

# ─── Create archive ───────────────────────────────────────────────────
ARCHIVE_NAME="agnetic-backup-${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_BASE}/${ARCHIVE_NAME}"

log "Creating archive: ${ARCHIVE_NAME}"
tar czf "$ARCHIVE_PATH" -C "$STAGING_DIR" . || err "Failed to create archive"

ARCHIVE_SIZE=$(du -sh "$ARCHIVE_PATH" | cut -f1)
TOTAL_FILES=$(find "$STAGING_DIR" -type f | wc -l)

echo ""
log "Backup complete!"
echo -e "  ${GREEN}Archive:${NC}  ${ARCHIVE_PATH}"
echo -e "  ${GREEN}Size:${NC}     ${ARCHIVE_SIZE}"
echo -e "  ${GREEN}Files:${NC}    ${TOTAL_FILES}"
echo -e "  ${GREEN}Timestamp:${NC} ${TIMESTAMP}"
echo ""

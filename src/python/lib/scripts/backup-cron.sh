#!/usr/bin/env bash
# Starship OS — Backup Cron Wrapper
# Runs backup.sh daily, manages retention (7 daily, 4 weekly).
# Designed to be called from a cron job, e.g.:
#   0 3 * * * /home/tech/agnetic-os/scripts/backup-cron.sh
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_SCRIPT="${SCRIPT_DIR}/backup.sh"
BACKUP_BASE="/var/lib/agnetic/backups"
LOG_DIR="/var/log/agnetic"
LOG_FILE="${LOG_DIR}/backup.log"

KEEP_DAILY=7
KEEP_WEEKLY=4

log()  { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $*" | tee -a "$LOG_FILE" 2>/dev/null; }
warn() { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] WARN:${NC} $*" | tee -a "$LOG_FILE" 2>/dev/null; }
err()  { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ERROR:${NC} $*" | tee -a "$LOG_FILE" 2>/dev/null >&2; exit 1; }

# ─── Preflight ────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
mkdir -p "$BACKUP_BASE"

log "=== Starship OS Backup Cron — Starting ==="

if [[ ! -f "$BACKUP_SCRIPT" ]]; then
    err "Backup script not found: $BACKUP_SCRIPT"
fi

# ─── Run backup ───────────────────────────────────────────────────────
log "Running backup..."
if bash "$BACKUP_SCRIPT" >> "$LOG_FILE" 2>&1; then
    log "Backup completed successfully"
else
    err "Backup failed (exit code: $?)"
fi

# ─── Retention: keep last N daily ─────────────────────────────────────
log "Applying retention policy (daily=${KEEP_DAILY}, weekly=${KEEP_WEEKLY})..."

# Tag backups with their day-of-week for weekly selection
DAILY_BACKUPS=()
WEEKLY_BACKUPS=()

while IFS= read -r backup; do
    [[ -z "$backup" ]] && continue
    basename_file=$(basename "$backup")
    # Extract timestamp from filename: agnetic-backup-YYYYMMDD-HHMMSS.tar.gz
    ts=$(echo "$basename_file" | sed -n 's/agnetic-backup-\([0-9]\{8\}\)-.*/\1/p')
    if [[ -n "$ts" ]]; then
        # Get day of week (1=Monday, 7=Sunday)
        dow=$(date -d "${ts:0:4}-${ts:4:2}-${ts:6:2}" +%u 2>/dev/null || echo "1")
        # Week number for grouping
        week=$(date -d "${ts:0:4}-${ts:4:2}-${ts:6:2}" +%Y%V 2>/dev/null || echo "0")

        DAILY_BACKUPS+=("${backup}|${ts}|${dow}")

        # For weekly, keep only the latest backup per week
        is_newest=true
        for i in "${!WEEKLY_BACKUPS[@]}"; do
            existing_week=$(echo "${WEEKLY_BACKUPS[$i]}" | cut -d'|' -f3)
            if [[ "$existing_week" == "$week" ]]; then
                # Replace with newer one
                WEEKLY_BACKUPS[$i]="${backup}|${ts}|${week}"
                is_newest=false
                break
            fi
        done
        if [[ "$is_newest" == "true" ]]; then
            WEEKLY_BACKUPS+=("${backup}|${ts}|${week}")
        fi
    fi
done < <(find "$BACKUP_BASE" -maxdepth 1 -name 'agnetic-backup-*.tar.gz' -type f | sort -r)

# Delete daily backups beyond the keep count
# Sort by timestamp descending, skip first KEEP_DAILY, delete the rest
DELETED=0
sorted_daily=$(printf '%s\n' "${DAILY_BACKUPS[@]}" | sort -t'|' -k2 -r)
count=0
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    count=$((count + 1))
    if [[ $count -gt $KEEP_DAILY ]]; then
        # Check if this backup is also a weekly keeper
        file_path=$(echo "$entry" | cut -d'|' -f1)
        is_weekly=false
        for w in "${WEEKLY_BACKUPS[@]}"; do
            wfile=$(echo "$w" | cut -d'|' -f1)
            if [[ "$wfile" == "$file_path" ]]; then
                is_weekly=true
                break
            fi
        done

        if [[ "$is_weekly" == "false" ]]; then
            rm -f "$file_path" 2>/dev/null && {
                log "  Deleted daily backup: $(basename "$file_path")"
                DELETED=$((DELETED + 1))
            }
        fi
    fi
done <<< "$sorted_daily"

# Delete weekly backups beyond the keep count
# Sort weekly by timestamp descending, skip first KEEP_WEEKLY
sorted_weekly=$(printf '%s\n' "${WEEKLY_BACKUPS[@]}" | sort -t'|' -k2 -r)
count=0
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    count=$((count + 1))
    if [[ $count -gt $KEEP_WEEKLY ]]; then
        file_path=$(echo "$entry" | cut -d'|' -f1)
        rm -f "$file_path" 2>/dev/null && {
            log "  Deleted weekly backup: $(basename "$file_path")"
            DELETED=$((DELETED + 1))
        }
    fi
done <<< "$sorted_weekly"

log "Cleanup: ${DELETED} old backups removed"

# ─── Summary ──────────────────────────────────────────────────────────
TOTAL=$(find "$BACKUP_BASE" -maxdepth 1 -name 'agnetic-backup-*.tar.gz' -type f 2>/dev/null | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_BASE" 2>/dev/null | cut -f1)

log "Backups on disk: ${TOTAL} (${TOTAL_SIZE})"
log "=== Starship OS Backup Cron — Done ==="

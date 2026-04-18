#!/bin/bash
# Nightly SQLite backup. Called from run_daily_pdrbot.sh at the top of each run.
# Keeps last 30 daily backups; rotates older ones.
set -euo pipefail

SRC="${1:-data/pdrbot.db}"
BACKUP_DIR="${HOME}/backups/pdrbot"
mkdir -p "$BACKUP_DIR"

if [ ! -f "$SRC" ]; then
    echo "backup_db: source not found: $SRC (skipping)"
    exit 0
fi

STAMP="$(date +%F)"
DEST="$BACKUP_DIR/pdrbot-${STAMP}.db"

# sqlite3 .backup is safer than cp for a live-writeable file.
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$SRC" ".backup '$DEST'"
else
    cp "$SRC" "$DEST"
fi

# Gzip backups older than 1 day; delete gzipped backups older than 30 days.
find "$BACKUP_DIR" -maxdepth 1 -name "pdrbot-*.db" -mtime +1 -exec gzip -9 {} \;
find "$BACKUP_DIR" -maxdepth 1 -name "pdrbot-*.db.gz" -mtime +30 -delete

echo "backup_db: wrote $DEST"

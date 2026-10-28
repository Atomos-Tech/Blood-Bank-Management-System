#!/usr/bin/env bash
# NFR-04 / SEC-08: one encrypted database backup, with 7 daily and 4 weekly copies retained.
#
# Runs pg_dump, encrypts the dump with GPG before it ever reaches disk unencrypted, and prunes
# older copies. Intended to run once every 24 hours (the `backup` service in docker-compose.yml,
# or cron: 15 2 * * * /app/scripts/backup.sh).
#
# Required environment:
#   POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST
#   BBMS_BACKUP_RECIPIENT  GPG recipient (key id or email) the backup is encrypted to
# Optional:
#   BBMS_BACKUP_DIR        where backups are written (default /var/backups/bbms)
#   POSTGRES_PORT          default 5432
#
# SEC-08: the directory is created 0700 and every file 0600, so only the account running this
# script (the backup service account) and root can read them. Decryption additionally needs the
# private key, which is held only by that account and the designated administrator.

set -euo pipefail
umask 077

BACKUP_DIR="${BBMS_BACKUP_DIR:-/var/backups/bbms}"
RECIPIENT="${BBMS_BACKUP_RECIPIENT:-}"
DAILY_KEEP=7
WEEKLY_KEEP=4

if [ -z "$RECIPIENT" ]; then
  echo "backup.sh: BBMS_BACKUP_RECIPIENT is not set; refusing to write an unencrypted backup" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"
chmod 700 "$BACKUP_DIR" "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/daily/bbms-$stamp.sql.gpg"

export PGPASSWORD="${POSTGRES_PASSWORD:-}"
pg_dump \
  --host "${POSTGRES_HOST:-db}" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "${POSTGRES_USER:-bbms}" \
  --dbname "${POSTGRES_DB:-bbms}" \
  --format=custom \
  | gpg --batch --yes --trust-model always --encrypt --recipient "$RECIPIENT" --output "$target"
unset PGPASSWORD

chmod 600 "$target"
echo "backup.sh: wrote $target ($(wc -c <"$target") bytes, encrypted to $RECIPIENT)"

# One copy per week is promoted on Sundays and kept for four weeks.
if [ "$(date -u +%u)" = "7" ]; then
  cp -p "$target" "$BACKUP_DIR/weekly/$(basename "$target")"
fi

# Retention: keep the newest 7 daily and 4 weekly copies, delete the rest.
prune() {
  local dir="$1" keep="$2"
  ls -1t "$dir"/bbms-*.sql.gpg 2>/dev/null | tail -n "+$((keep + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "backup.sh: pruned $old"
  done
}
prune "$BACKUP_DIR/daily" "$DAILY_KEEP"
prune "$BACKUP_DIR/weekly" "$WEEKLY_KEEP"

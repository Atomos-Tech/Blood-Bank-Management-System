#!/usr/bin/env bash
# NFR-04 / TC-N03: restore one encrypted backup and report how long it took.
#
#   ./scripts/restore.sh /var/backups/bbms/daily/bbms-20261030T021500Z.sql.gpg [target_database]
#
# Decrypts with the backup private key (held only by the backup service account and the
# designated administrator, SEC-08) and loads the dump with pg_restore. The elapsed time is
# printed so TC-N03 can record it against the 4-hour recovery objective.
#
# Required environment: POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST
# The plaintext dump only ever exists inside a 0700 temporary directory, which is removed on exit.

set -euo pipefail
umask 077

archive="${1:-}"
target_db="${2:-${POSTGRES_DB:-bbms}}"

if [ -z "$archive" ] || [ ! -r "$archive" ]; then
  echo "restore.sh: pass a readable encrypted backup as the first argument" >&2
  exit 1
fi

workdir="$(mktemp -d)"
chmod 700 "$workdir"
trap 'rm -rf -- "$workdir"' EXIT

started=$(date -u +%s)
echo "restore.sh: restoring $archive into $target_db at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

gpg --batch --yes --decrypt --output "$workdir/bbms.sql" "$archive"

export PGPASSWORD="${POSTGRES_PASSWORD:-}"
pg_restore \
  --host "${POSTGRES_HOST:-db}" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "${POSTGRES_USER:-bbms}" \
  --dbname "$target_db" \
  --clean --if-exists --no-owner \
  "$workdir/bbms.sql"
unset PGPASSWORD

elapsed=$(( $(date -u +%s) - started ))
printf 'restore.sh: completed in %dm %ds (TC-N03 objective: under 4 hours)\n' $((elapsed / 60)) $((elapsed % 60))

# Operations: availability, backup and restore

Covers `NFR-03` (availability), `NFR-04` (backup and restore) and `SEC-08` (backup confidentiality),
and the procedure `TC-N03` follows.

---

## 1. Availability (`NFR-03`)

**Target:** 99.0% of each calendar month. That allows about **7 hours 18 minutes** of unplanned
downtime per 30-day month.

### Health check

`GET /health/` is unauthenticated and returns:

| Condition | Status | Body |
| :--- | :--- | :--- |
| Application serving and database answering `SELECT 1` | `200` | `{"status": "ok", "database": "up"}` |
| Database unreachable or erroring | `503` | `{"status": "unavailable", "database": "down"}` |

It is the only route in `bloodbank/urls.py` besides the sign-in pair that does not require a
session, because a load balancer and an uptime monitor have no session. The body says nothing
about the blood bank's data.

Poll it every 30 seconds from the uptime monitor. Availability for the month is
`successful probes / total probes`.

### Automatic restart

Every service in `docker-compose.yml` runs with `restart: unless-stopped`, so a crashed container
comes back without a human. `db` and `web` also declare `healthcheck`s — `pg_isready` and
`/health/` respectively — so an unhealthy container is visible to `docker compose ps` and to any
orchestrator that reads health status.

### Planned maintenance notice

Planned maintenance does **not** count against the 99.0% target, provided it follows this policy:

1. Announce at least **48 hours** ahead to all branch staff and registered hospital accounts.
2. Schedule inside the low-traffic window: **01:00–04:00 branch local time**.
3. Keep any single window to **60 minutes** or less, and to at most **one window per month**.
4. Record the start and end time in the audit trail by noting them in the maintenance log below.
5. Anything unannounced, longer than 60 minutes, or outside the window counts as downtime.

### Maintenance log

| Date | Window (UTC) | Change | Announced on |
| :--- | :--- | :--- | :--- |
| _(none yet)_ | | | |

---

## 2. Backups (`NFR-04`, `SEC-08`)

### What runs

`scripts/backup.sh`, once every 24 hours, from the `backup` service in `docker-compose.yml`
(or from cron: `15 2 * * * /app/scripts/backup.sh`).

It pipes `pg_dump --format=custom` straight into
`gpg --encrypt --recipient "$BBMS_BACKUP_RECIPIENT"`, so the dump is **never written to disk
unencrypted**. The script exits non-zero and writes nothing if `BBMS_BACKUP_RECIPIENT` is unset,
so a misconfiguration cannot silently produce a plaintext backup.

### Retention

| Set | Kept | How a copy gets there |
| :--- | :--- | :--- |
| `daily/` | newest **7** | every run |
| `weekly/` | newest **4** | runs on Sunday are copied here as well |

Anything older is deleted at the end of each run.

### Confidentiality (`SEC-08`)

- The script runs under `umask 077`; the backup directories are `0700` and each file `0600`.
- Backups live on the `backup_data` volume and the GPG keyring on `backup_keys`. Only the
  `backup` service mounts either; `web`, `scheduler` and `nginx` cannot read them.
- Decryption additionally requires the backup **private key**, which is held only by the backup
  service account and the designated administrator. An attacker who copies the volume still has
  only ciphertext.
- Set `BBMS_BACKUP_RECIPIENT` in the deployment environment, never in the repository.

---

## 3. Restore procedure (`TC-N03`)

**Recovery objective: a full restore inside 4 hours.**

1. Pick the copy to restore:

   ```sh
   docker compose exec backup ls -lt /var/backups/bbms/daily /var/backups/bbms/weekly
   ```

2. Restore it. The script times itself and prints the elapsed time at the end:

   ```sh
   docker compose exec backup bash /app/scripts/restore.sh \
     /var/backups/bbms/daily/bbms-<stamp>.sql.gpg
   ```

   It decrypts into a `0700` temporary directory that is removed on exit, then runs
   `pg_restore --clean --if-exists --no-owner`.

3. Confirm the application comes back:

   ```sh
   curl -fsS http://localhost:8000/health/
   ```

   Expect `{"status": "ok", "database": "up"}`.

4. Spot-check that committed records survived — one donation, one screening result, one
   reservation and one issued unit — the same records `NFR-05` checks after a restart.

5. Record the result in the table below.

### Restore drill results

| Date | Backup restored | Decrypt + restore time | `/health/` after | Verified by |
| :--- | :--- | :--- | :--- | :--- |
| _(to be filled during the TC-N03 drill)_ | | | | |

> The drill has not been run yet. `scripts/restore.sh` prints the timing line that belongs in the
> "Decrypt + restore time" column; until a drill is recorded here, `TC-N03` is not evidenced.

"""TC-N04 / NFR-05: committed data survives an application restart.

Run from blood-bank-management-system/ on a host with Docker:
    python scripts/verify_restart_persistence.py

Steps: start db and web, migrate and seed, write a donation, screening results, a reservation and an issue,
restart the web and db containers, then remove and recreate them (keeping the postgres_data named volume),
and verify every record after each step. Results go to docs/test-results/TC-N04_NFR-05_restart_persistence_results.md.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "test-results" / "TC-N04_NFR-05_restart_persistence_results.md"
PROBE = ["docker", "compose", "exec", "-T", "web", "python", "scripts/restart_persistence_probe.py"]


def run(cmd, check=True):
    print("$", " ".join(cmd), flush=True)
    done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if check and done.returncode != 0:
        sys.exit(f"Command failed ({done.returncode}): {' '.join(cmd)}\n{done.stdout}\n{done.stderr}")
    return done


def wait_for_app():
    for _ in range(60):
        if run(["docker", "compose", "exec", "-T", "web", "python", "manage.py", "check", "--database", "default"],
               check=False).returncode == 0:
            return
        time.sleep(2)
    sys.exit("The web container did not become ready.")


def verify(manifest, label):
    done = run(PROBE + ["verify", json.dumps(manifest)], check=False)
    checks = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    return label, checks, done.returncode == 0


def main():
    run(["docker", "compose", "up", "-d", "--build", "db", "web"])
    wait_for_app()
    run(["docker", "compose", "exec", "-T", "web", "python", "manage.py", "migrate", "--noinput"])
    run(["docker", "compose", "exec", "-T", "web", "python", "manage.py", "seed_demo"])
    manifest = json.loads(run(PROBE + ["create"]).stdout.strip().splitlines()[-1])
    rows = [verify(manifest, "Before restart")]

    run(["docker", "compose", "restart", "web", "db"])
    wait_for_app()
    rows.append(verify(manifest, "After `docker compose restart web db`"))

    run(["docker", "compose", "rm", "-s", "-f", "web", "db"])
    run(["docker", "compose", "up", "-d", "db", "web"])
    wait_for_app()
    rows.append(verify(manifest, "After containers removed and recreated (volume kept)"))

    passed = all(ok for _, _, ok in rows)
    lines = [
        "# TC-N04 / NFR-05 — Committed data survives an application restart",
        "",
        f"- **Executed:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC with `python scripts/verify_restart_persistence.py`",
        "- **Environment:** `docker-compose.yml` services `db` (PostgreSQL 16, named volume `postgres_data`) and `web` (Gunicorn/Django)",
        f"- **Records written:** donation `{manifest['donation_id']}`, unit `{manifest['unit_id']}` with "
        f"{manifest['lab_results']} screening results, request `{manifest['request_id']}`, "
        f"reservation `{manifest['reservation_id']}` and its issue",
        f"- **Result:** {'PASS' if passed else 'FAIL'}",
        "",
        "| Step | " + " | ".join(rows[0][1].keys()) + " | Outcome |",
        "| :--- | " + " | ".join(":---:" for _ in rows[0][1]) + " | :---: |",
    ]
    for label, checks, ok in rows:
        lines.append(f"| {label} | " + " | ".join("yes" if v else "NO" for v in checks.values())
                     + f" | {'PASS' if ok else 'FAIL'} |")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(RESULTS.read_text(encoding="utf-8"))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

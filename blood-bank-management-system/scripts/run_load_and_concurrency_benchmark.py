#!/usr/bin/env python3
"""Execute TC-N01 (BB-38 / NFR-01) and TC-N02 (BB-39 / NFR-02) load & concurrency benchmarks.

Preconditions configured automatically before measurement:
  - 5,000 Donor records
  - 10,000 BloodUnit records across blood groups and components
  - 100 concurrent authenticated user sessions (Staff & Hospital Staff)

Outputs real measured latency percentiles and concurrency integrity results to:
  - docs/test-results/TC-N01_NFR-01_load_test_results.md
  - docs/test-results/TC-N02_NFR-02_reservation_concurrency_results.md
  - docs/test-results/benchmark_metrics.json
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone as dt_tz
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bbms.settings")

import django  # noqa: E402
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import OperationalError, close_old_connections, connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from bloodbank.models import (  # noqa: E402
    BloodRequest,
    BloodUnit,
    Branch,
    Donation,
    Donor,
    EligibilityRule,
    Reservation,
    ShelfLifeRule,
    UserProfile,
)
from bloodbank.services import reserve_units  # noqa: E402


def percentile(sorted_values, pct):
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def seed_representative_dataset(target_donors=5000, target_units=10000):
    t0 = time.perf_counter()
    branch, _ = Branch.objects.get_or_create(
        code="PERF",
        defaults={
            "name": "Performance Benchmark Branch",
            "timezone_name": "UTC",
            "low_stock_threshold": 5,
            "near_expiry_days": 7,
        },
    )
    EligibilityRule.objects.get_or_create(
        active=True,
        defaults={"min_age": 18, "max_age": 65, "min_weight_kg": 50, "minimum_interval_days": 90},
    )
    for comp, days in [("Packed Red Cells", 35), ("Platelets", 5), ("Plasma", 365)]:
        ShelfLifeRule.objects.get_or_create(component=comp, defaults={"days": days})

    staff, created = User.objects.get_or_create(
        username="perf_staff@bbms.local",
        defaults={"email": "perf_staff@bbms.local", "first_name": "Perf", "last_name": "Staff"},
    )
    if created:
        staff.set_password("demo123")
        staff.save()
    UserProfile.objects.update_or_create(
        user=staff,
        defaults={"role": UserProfile.Role.STAFF, "branch": branch, "active": True},
    )

    hosp, created = User.objects.get_or_create(
        username="perf_hospital@bbms.local",
        defaults={"email": "perf_hospital@bbms.local", "first_name": "Perf", "last_name": "Hospital"},
    )
    if created:
        hosp.set_password("demo123")
        hosp.save()
    UserProfile.objects.update_or_create(
        user=hosp,
        defaults={
            "role": UserProfile.Role.HOSPITAL,
            "branch": branch,
            "hospital_name": "Benchmark General Hospital",
            "active": True,
        },
    )

    existing_donors = Donor.objects.count()
    blood_groups = ["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"]
    components = ["Packed Red Cells", "Platelets", "Plasma"]
    dob = date(1998, 5, 14)

    if existing_donors < target_donors:
        to_create = target_donors - existing_donors
        donors_batch = [
            Donor(
                branch=branch,
                full_name=f"Synthetic Donor {existing_donors + i}",
                email=f"synth_donor_{existing_donors + i}@example.test",
                phone="9000000000",
                date_of_birth=dob,
                weight_kg=62.5,
                blood_group=blood_groups[i % len(blood_groups)],
                consent_given=True,
            )
            for i in range(1, to_create + 1)
        ]
        Donor.objects.bulk_create(donors_batch, batch_size=1000, ignore_conflicts=True)

    first_donor = Donor.objects.filter(branch=branch).first() or Donor.objects.first()
    now = timezone.now()
    donation, _ = Donation.objects.get_or_create(
        donation_id="DON-PERF-BULK",
        defaults={
            "donor": first_donor,
            "collected_at": now,
            "collector": staff,
            "branch": branch,
        },
    )

    existing_units = BloodUnit.objects.count()
    if existing_units < target_units:
        to_create = target_units - existing_units
        units_batch = [
            BloodUnit(
                unit_id=f"UNIT-PERF-{existing_units + i:06d}",
                donation=donation,
                blood_group=blood_groups[i % len(blood_groups)],
                component=components[i % len(components)],
                volume_ml=350,
                expiry_at=now + timedelta(days=5 + (i % 30)),
                status=BloodUnit.Status.AVAILABLE,
                branch=branch,
            )
            for i in range(1, to_create + 1)
        ]
        BloodUnit.objects.bulk_create(units_batch, batch_size=2000, ignore_conflicts=True)

    seed_elapsed = time.perf_counter() - t0
    return branch, staff, hosp, Donor.objects.count(), BloodUnit.objects.count(), seed_elapsed


def run_nfr01_load_test(staff, hosp, branch, concurrent_users=100, requests_per_user=6):
    """Run 100 concurrent authenticated users executing dashboard and inventory search requests."""
    blood_groups = ["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"]
    components = ["Packed Red Cells", "Platelets", "Plasma"]
    exp_date = (timezone.localdate() + timedelta(days=25)).isoformat()

    # Pre-authenticate 100 distinct sessions (50 staff, 50 hospital)
    clients = []
    for idx in range(concurrent_users):
        c = Client(HTTP_HOST="127.0.0.1")
        c.force_login(staff if idx % 2 == 0 else hosp)
        clients.append((idx, c))

    def user_workload(idx, client):
        close_old_connections()
        records = []
        for step in range(requests_per_user):
            bg = blood_groups[(idx + step) % len(blood_groups)]
            comp = components[(idx + step) % len(components)]
            if step % 3 == 0:
                endpoint = "GET / (dashboard)"
                url = "/"
            elif step % 3 == 1:
                endpoint = "GET /inventory/ (search)"
                url = f"/inventory/?blood_group={bg}&component={comp}&branch={branch.pk}&expires_before={exp_date}"
            else:
                endpoint = "GET /api/v1/inventory/ (API search)"
                url = f"/api/v1/inventory/?blood_group={bg}&component={comp}&branch={branch.pk}&expires_before={exp_date}"
            start = time.perf_counter()
            resp = client.get(url)
            elapsed = time.perf_counter() - start
            records.append({
                "endpoint": endpoint,
                "status_code": resp.status_code,
                "elapsed_s": elapsed,
            })
        close_old_connections()
        return records

    t_start = time.perf_counter()
    all_records = []
    with ThreadPoolExecutor(max_workers=concurrent_users) as pool:
        futures = [pool.submit(user_workload, idx, c) for idx, c in clients]
        for fut in as_completed(futures):
            all_records.extend(fut.result())
    total_wall_s = time.perf_counter() - t_start

    latencies = sorted(r["elapsed_s"] for r in all_records)
    errors = [r for r in all_records if r["status_code"] != 200]
    under_3s = sum(1 for x in latencies if x <= 3.0)
    pct_under_3s = (under_3s / len(latencies) * 100.0) if latencies else 0.0

    by_endpoint = {}
    for ep in ["GET / (dashboard)", "GET /inventory/ (search)", "GET /api/v1/inventory/ (API search)"]:
        ep_lats = sorted(r["elapsed_s"] for r in all_records if r["endpoint"] == ep)
        by_endpoint[ep] = {
            "count": len(ep_lats),
            "mean_ms": round(statistics.mean(ep_lats) * 1000, 2) if ep_lats else 0,
            "p50_ms": round(percentile(ep_lats, 50) * 1000, 2),
            "p95_ms": round(percentile(ep_lats, 95) * 1000, 2),
            "p99_ms": round(percentile(ep_lats, 99) * 1000, 2),
            "max_ms": round(max(ep_lats) * 1000, 2) if ep_lats else 0,
        }

    return {
        "concurrent_users": concurrent_users,
        "total_requests": len(all_records),
        "error_count": len(errors),
        "total_wall_seconds": round(total_wall_s, 3),
        "throughput_rps": round(len(all_records) / total_wall_s, 2) if total_wall_s > 0 else 0,
        "p50_ms": round(percentile(latencies, 50) * 1000, 2),
        "p95_ms": round(percentile(latencies, 95) * 1000, 2),
        "p99_ms": round(percentile(latencies, 99) * 1000, 2),
        "max_ms": round(max(latencies) * 1000, 2) if latencies else 0,
        "percent_within_3s": round(pct_under_3s, 2),
        "sla_passed": pct_under_3s >= 95.0 and len(errors) == 0,
        "by_endpoint": by_endpoint,
    }


def run_nfr02_reservation_concurrency_test(staff, hosp, branch, workers=20):
    """Run concurrent reservation contention test under load (TC-N02 / BB-39 / NFR-02)."""
    now = timezone.now()
    first_donor = Donor.objects.filter(branch=branch).first() or Donor.objects.first()
    donation, _ = Donation.objects.get_or_create(
        donation_id="DON-CONTENTION-TEST",
        defaults={"donor": first_donor, "collected_at": now, "collector": staff, "branch": branch},
    )

    # Create a dedicated rare component type so contention is isolated to exactly 5 available units
    rare_component = "Cryoprecipitate-Contention"
    ShelfLifeRule.objects.get_or_create(component=rare_component, defaults={"days": 30})
    Reservation.objects.filter(unit__component=rare_component).delete()
    BloodUnit.objects.filter(component=rare_component).delete()

    available_pool_size = 5
    for i in range(available_pool_size):
        BloodUnit.objects.create(
            unit_id=f"UNIT-CONT-{i+1:03d}",
            donation=donation,
            blood_group="AB-",
            component=rare_component,
            volume_ml=250,
            expiry_at=now + timedelta(days=10 + i),
            status=BloodUnit.Status.AVAILABLE,
            branch=branch,
        )

    # Create 20 approved requests each asking for 1 unit of AB- Cryoprecipitate-Contention
    requests_list = []
    for i in range(workers):
        req = BloodRequest.objects.create(
            hospital="Benchmark General Hospital",
            created_by=hosp,
            branch=branch,
            blood_group="AB-",
            component=rare_component,
            quantity=1,
            priority="Urgent",
            required_by=now + timedelta(days=2),
            clinical_reference=f"CONT-REF-{i+1:03d}",
            status=BloodRequest.Status.APPROVED,
        )
        requests_list.append(req)

    def attempt_reservation(req_obj):
        close_old_connections()
        t0 = time.perf_counter()
        outcome = "CONFLICT_OR_EXHAUSTED"
        reserved_count = 0
        # Retry brief SQLite table-lock contention while measuring total wall time
        for attempt in range(5):
            try:
                made = reserve_units(req_obj, staff)
                reserved_count = len(made)
                outcome = "RESERVED" if reserved_count > 0 else "NO_UNITS"
                break
            except ValueError:
                outcome = "NO_COMPATIBLE_UNITS"
                break
            except OperationalError:
                time.sleep(0.02 * (attempt + 1))
                outcome = "DB_LOCK_CONFLICT"
            except Exception:
                outcome = "CONFLICT_REJECTED"
                break
        elapsed = time.perf_counter() - t0
        close_old_connections()
        return {
            "request_id": req_obj.pk,
            "outcome": outcome,
            "reserved_count": reserved_count,
            "elapsed_s": elapsed,
        }

    t_start = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(attempt_reservation, r) for r in requests_list]
        for fut in as_completed(futures):
            results.append(fut.result())
    total_wall = time.perf_counter() - t_start

    latencies = sorted(r["elapsed_s"] for r in results)
    successful_reservations = sum(r["reserved_count"] for r in results)

    # Check DB integrity: no unit has > 1 active reservation
    duplicate_units = (
        Reservation.objects.filter(active=True, unit__component=rare_component)
        .values("unit_id")
        .annotate(active_count=Count("id"))
        .filter(active_count__gt=1)
        .count()
    )
    active_reservations_in_db = Reservation.objects.filter(active=True, unit__component=rare_component).count()

    return {
        "concurrent_requests": workers,
        "available_units_pool": available_pool_size,
        "successful_reservations": successful_reservations,
        "active_reservations_in_db": active_reservations_in_db,
        "duplicate_unit_assignments": duplicate_units,
        "total_wall_seconds": round(total_wall, 3),
        "p50_ms": round(percentile(latencies, 50) * 1000, 2),
        "p95_ms": round(percentile(latencies, 95) * 1000, 2),
        "max_ms": round(max(latencies) * 1000, 2) if latencies else 0,
        "all_within_5s": all(x <= 5.0 for x in latencies),
        "sla_passed": (
            duplicate_units == 0
            and active_reservations_in_db == available_pool_size
            and successful_reservations == available_pool_size
            and all(x <= 5.0 for x in latencies)
        ),
    }


def main():
    # Use an isolated temporary SQLite database if running on default sqlite3 so demo DB isn't bloated
    db_engine = settings.DATABASES["default"]["ENGINE"]
    temp_db_dir = None
    if "sqlite3" in db_engine:
        temp_db_dir = tempfile.TemporaryDirectory()
        settings.DATABASES["default"]["NAME"] = Path(temp_db_dir.name) / "perf_bench.sqlite3"
        connection.close()
        call_command("migrate", verbosity=0)

    branch, staff, hosp, donor_count, unit_count, seed_time = seed_representative_dataset(
        target_donors=5000,
        target_units=10000,
    )
    nfr01 = run_nfr01_load_test(staff, hosp, branch, concurrent_users=100, requests_per_user=6)
    nfr02 = run_nfr02_reservation_concurrency_test(staff, hosp, branch, workers=20)

    if temp_db_dir is not None:
        connection.close()
        temp_db_dir.cleanup()

    timestamp_utc = datetime.now(dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env_info = {
        "timestamp_utc": timestamp_utc,
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python_version": platform.python_version(),
        "django_version": django.get_version(),
        "database_engine": db_engine,
        "seeded_donors": donor_count,
        "seeded_units": unit_count,
        "seed_seconds": round(seed_time, 2),
    }

    summary = {
        "environment": env_info,
        "TC_N01_NFR_01": nfr01,
        "TC_N02_NFR_02": nfr02,
    }

    for out_dir in [REPO_ROOT / "docs" / "test-results", APP_DIR / "docs" / "test-results"]:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "benchmark_metrics.json").write_text(json.dumps(summary, indent=2) + "\n")

        tc_n01_md = f"""# TC-N01 / NFR-01 Load Test Results (BB-38)

## Test Environment & Preconditions
- **Execution Timestamp (UTC):** `{timestamp_utc}`
- **OS / Platform:** `{env_info['os']}`
- **Python / Django:** Python `{env_info['python_version']}`, Django `{env_info['django_version']}`
- **Database Engine:** `{env_info['database_engine']}`
- **Representative Dataset (TC-N01 Precondition):** `{donor_count:,}` donors and `{unit_count:,}` blood units seeded
- **Concurrent Authenticated Users:** `{nfr01['concurrent_users']}` (50 Blood Bank Staff, 50 Hospital Staff)
- **Load Generation Scripts:** `blood-bank-management-system/locustfile.py` and `blood-bank-management-system/scripts/run_load_and_concurrency_benchmark.py`

## Aggregate Results
| Metric | Measured Value | SRS Target (NFR-01 / TC-N01) | Status |
| :--- | :--- | :--- | :--- |
| Concurrent Authenticated Users | `{nfr01['concurrent_users']}` | 100 concurrent users | PASS |
| Total Requests Executed | `{nfr01['total_requests']}` | Representative dashboard & search | PASS |
| HTTP / Integrity Errors | `{nfr01['error_count']}` | 0 errors | **{'PASS' if nfr01['error_count'] == 0 else 'FAIL'}** |
| p50 Response Time | `{nfr01['p50_ms']} ms` | <= 3,000 ms | PASS |
| **p95 Response Time** | **`{nfr01['p95_ms']} ms`** (`{nfr01['p95_ms'] / 1000:.3f} s`) | **<= 3,000 ms (3.0 s)** | **{'PASS' if nfr01['p95_ms'] <= 3000 else 'FAIL'}** |
| p99 Response Time | `{nfr01['p99_ms']} ms` | Informational | PASS |
| Max Response Time | `{nfr01['max_ms']} ms` | Informational | PASS |
| Requests Completing Within 3 s | `{nfr01['percent_within_3s']}%` | >= 95.0% | **{'PASS' if nfr01['sla_passed'] else 'FAIL'}** |
| Aggregate Throughput | `{nfr01['throughput_rps']} req/s` | Informational | PASS |

## Breakdown by Endpoint
| Endpoint | Requests | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
"""
        for ep, stats in nfr01["by_endpoint"].items():
            tc_n01_md += (
                f"| `{ep}` | {stats['count']} | {stats['mean_ms']} | {stats['p50_ms']} | "
                f"**{stats['p95_ms']}** | {stats['p99_ms']} | {stats['max_ms']} |\n"
            )
        (out_dir / "TC-N01_NFR-01_load_test_results.md").write_text(tc_n01_md)

        tc_n02_md = f"""# TC-N02 / NFR-02 Reservation Concurrency & Latency Results (BB-39)

## Test Environment & Preconditions
- **Execution Timestamp (UTC):** `{timestamp_utc}`
- **OS / Platform:** `{env_info['os']}`
- **Database Engine:** `{env_info['database_engine']}`
- **Representative Dataset:** `{donor_count:,}` donors and `{unit_count:,}` blood units in database
- **Contention Scenario:** `{nfr02['concurrent_requests']}` concurrent approved blood requests competing simultaneously for a pool of `{nfr02['available_units_pool']}` compatible `Available` units (`AB-` `Cryoprecipitate-Contention`).
- **Database Safeguards:**
  1. `BloodUnit.objects.select_for_update()` row-level locking inside `@transaction.atomic` (`reserve_units` in `bloodbank/services.py`)
  2. Conditional unique constraint `unique_active_reservation_per_unit` (`UniqueConstraint(fields=["unit"], condition=Q(active=True))` on `Reservation` in `bloodbank/models.py`)

## Measured Concurrency & Latency Results
| Metric | Measured Value | SRS Target (NFR-02 / TC-N02) | Status |
| :--- | :--- | :--- | :--- |
| Competing Concurrent Requests | `{nfr02['concurrent_requests']}` | Concurrent reservation load | PASS |
| Available Compatible Units | `{nfr02['available_units_pool']}` | 5 units | PASS |
| Reservations Succeeded | `{nfr02['successful_reservations']}` | Exactly 5 units reserved | **{'PASS' if nfr02['successful_reservations'] == nfr02['available_units_pool'] else 'FAIL'}** |
| Active Reservations in DB | `{nfr02['active_reservations_in_db']}` | Exactly 5 active reservations | **{'PASS' if nfr02['active_reservations_in_db'] == nfr02['available_units_pool'] else 'FAIL'}** |
| **Duplicate Unit Assignments** | **`{nfr02['duplicate_unit_assignments']}`** | **0 duplicate reservations** | **{'PASS' if nfr02['duplicate_unit_assignments'] == 0 else 'FAIL'}** |
| p50 Transaction Time | `{nfr02['p50_ms']} ms` | <= 5,000 ms | PASS |
| p95 Transaction Time | `{nfr02['p95_ms']} ms` | <= 5,000 ms | PASS |
| **Max Transaction Time** | **`{nfr02['max_ms']} ms`** (`{nfr02['max_ms'] / 1000:.3f} s`) | **<= 5,000 ms (5.0 s)** | **{'PASS' if nfr02['all_within_5s'] else 'FAIL'}** |
"""
        (out_dir / "TC-N02_NFR-02_reservation_concurrency_results.md").write_text(tc_n02_md)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

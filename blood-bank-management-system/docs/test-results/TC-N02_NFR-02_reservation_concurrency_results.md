# TC-N02 / NFR-02 Reservation Concurrency & Latency Results (BB-39)

## Test Environment & Preconditions
- **Execution Timestamp (UTC):** `2026-10-22T07:12:17Z`
- **OS / Platform:** `Darwin 24.1.0 (arm64)`
- **Database Engine:** `django.db.backends.sqlite3`
- **Representative Dataset:** `5,000` donors and `10,000` blood units in database
- **Contention Scenario:** `20` concurrent approved blood requests competing simultaneously for a pool of `5` compatible `Available` units (`AB-` `Cryoprecipitate-Contention`).
- **Database Safeguards:**
  1. `BloodUnit.objects.select_for_update()` row-level locking inside `@transaction.atomic` (`reserve_units` in `bloodbank/services.py`)
  2. Conditional unique constraint `unique_active_reservation_per_unit` (`UniqueConstraint(fields=["unit"], condition=Q(active=True))` on `Reservation` in `bloodbank/models.py`)

## Measured Concurrency & Latency Results
| Metric | Measured Value | SRS Target (NFR-02 / TC-N02) | Status |
| :--- | :--- | :--- | :--- |
| Competing Concurrent Requests | `20` | Concurrent reservation load | PASS |
| Available Compatible Units | `5` | 5 units | PASS |
| Reservations Succeeded | `5` | Exactly 5 units reserved | **PASS** |
| Active Reservations in DB | `5` | Exactly 5 active reservations | **PASS** |
| **Duplicate Unit Assignments** | **`0`** | **0 duplicate reservations** | **PASS** |
| p50 Transaction Time | `149.34 ms` | <= 5,000 ms | PASS |
| p95 Transaction Time | `160.81 ms` | <= 5,000 ms | PASS |
| **Max Transaction Time** | **`194.45 ms`** (`0.194 s`) | **<= 5,000 ms (5.0 s)** | **PASS** |

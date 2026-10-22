# TC-N01 / NFR-01 Load Test Results (BB-38)

## Test Environment & Preconditions
- **Execution Timestamp (UTC):** `2026-10-22T07:12:17Z`
- **OS / Platform:** `Darwin 24.1.0 (arm64)`
- **Python / Django:** Python `3.12.10`, Django `5.2.17`
- **Database Engine:** `django.db.backends.sqlite3`
- **Representative Dataset (TC-N01 Precondition):** `5,000` donors and `10,000` blood units seeded
- **Concurrent Authenticated Users:** `100` (50 Blood Bank Staff, 50 Hospital Staff)
- **Load Generation Scripts:** `blood-bank-management-system/locustfile.py` and `blood-bank-management-system/scripts/run_load_and_concurrency_benchmark.py`

## Aggregate Results
| Metric | Measured Value | SRS Target (NFR-01 / TC-N01) | Status |
| :--- | :--- | :--- | :--- |
| Concurrent Authenticated Users | `100` | 100 concurrent users | PASS |
| Total Requests Executed | `600` | Representative dashboard & search | PASS |
| HTTP / Integrity Errors | `0` | 0 errors | **PASS** |
| p50 Response Time | `178.86 ms` | <= 3,000 ms | PASS |
| **p95 Response Time** | **`1665.0 ms`** (`1.665 s`) | **<= 3,000 ms (3.0 s)** | **PASS** |
| p99 Response Time | `2643.57 ms` | Informational | PASS |
| Max Response Time | `3210.86 ms` | Informational | PASS |
| Requests Completing Within 3 s | `99.67%` | >= 95.0% | **PASS** |
| Aggregate Throughput | `185.4 req/s` | Informational | PASS |

## Breakdown by Endpoint
| Endpoint | Requests | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `GET / (dashboard)` | 200 | 713.66 | 459.88 | **2196.02** | 2995.9 | 3210.86 |
| `GET /inventory/ (search)` | 200 | 339.78 | 181.64 | **1318.89** | 2195.85 | 2518.14 |
| `GET /api/v1/inventory/ (API search)` | 200 | 262.34 | 115.08 | **1128.49** | 1705.97 | 2356.82 |

# HemaCore Blood Bank Management System

A Django academic implementation of the submitted Blood Bank Management System specification.

## Implemented workflows

- Role-controlled Donor, Blood Bank Staff, Hospital Staff, and System Administrator accounts.
- Donor registration with consent, contact, medical-history, age, weight, and email uniqueness validation.
- Configurable eligibility rules, active deferrals, appointment slots, capacity checking, cancellation, and donor-only history access.
- Donation and unique component-unit recording with configured shelf-life calculation.
- Configured mandatory screening panel, release only after all acceptable results, and quarantine for reactive, indeterminate, or incomplete results.
- Inventory search by group, component, branch, and expiry-aware unit lifecycle.
- Hospital request creation, request-state history, compatibility-based reservation, cross-match, and atomic issue.
- In-app account administration at `/accounts/` (System Administrator only): create an account with exactly one role and branch, assign a different role, activate or deactivate. Deactivation ends the account's open sessions immediately, and the last active administrator cannot be deactivated or demoted.
- Structured audit events, notification delivery records, protected Django administration, CSRF-protected forms, generic authentication failures, rate-limited lockout, and 15-minute session lifetime.
- Audit events cover authentication (including failed sign-ins and lockouts), account management, configuration changes made through the Django admin, deferrals, screening, inventory, requests and issuance.
- The failed-login counter and the 15-minute lockout are held in a database-backed cache (`bbms_cache`, created by migration `0007`), so every worker process sees the same attempts.
- In-app configuration at `/configuration/` (System Administrator only): branch thresholds and time zone, the donor eligibility rule, component shelf lives and appointment slots, each validated (maximum age above minimum, capacity and intervals at least one) and audited.
- Five reports at `/reports/` — inventory, expiry, donations, blood requests and audit trail — filtered by date range and branch and downloadable as CSV. A hospital account sees only availability and its own requests; asking for another report by name is denied and audited.
- `GET /health/` is unauthenticated and returns `200` while the application and database are up, `503` otherwise. It is what the `web` container healthcheck and the uptime monitor poll (`NFR-03`).
- `scripts/backup.sh` and `scripts/restore.sh` take encrypted `pg_dump` backups (7 daily, 4 weekly) and restore one with timing. See [`docs/operations.md`](../docs/operations.md).
- Donors see their donation history, appointment history and next eligibility date on **My donor record**.
- Staff find a donor by name, phone, email or donor number on **Donors** and check in today's appointments from the search results or the **Today's appointments** list.
- Appointment reminders go out 24 hours ahead and request-status emails on every request change; each email's delivery is recorded as `SENT` or `FAILED`.
- A signed-in session ends after 15 minutes without a request, on logout, and at once when the account is deactivated.
- Unit status changes follow the design's state machine: invalid moves are rejected and audited, units past expiry are marked Expired every 5 minutes, and staff can discard Quarantined or Expired units with a reason.
- Pages show times in the branch's time zone while the database stores UTC.
- The administrator sets record retention periods and exports donors, donations and the audit trail as CSV.
- Every page works from 360 to 1440 px wide without horizontal scrolling.

## Sprint demos

- Sprint 1 (team demo: donor registration and booking, donation recording, screening and release, hospital request, reservation, cross-match and issue, audit trail): [docs/videos/sprint1-demo.mp4](../docs/videos/sprint1-demo.mp4)

## Local setup

The checked-in local development database uses SQLite. Django and its dependencies are listed in `requirements.txt`.

```sh
python3 -m pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py seed_demo
python3 manage.py runserver
```

Open `http://127.0.0.1:8000`. Demo accounts use password `demo123`, all in the `MAIN` branch (time zone Asia/Kolkata):

| Account | Role | What it can do (SRS Table 4) |
| :--- | :--- | :--- |
| `staff@bbms.local` | Blood Bank Staff | Donors, deferrals, appointments, donations, screening, inventory, request approval, reservation, cross-match, issue, branch audit trail |
| `hospital@bbms.local` | Hospital Staff (City Care Hospital) | Available stock, create and track its own blood requests, inventory and request reports |
| `admin@bbms.local` | System Administrator | Accounts (`/accounts/`), configuration (`/configuration/`), all reports, audit trail, Django admin; no routine clinical actions |
| `donor@bbms.local` | Donor | Own record, eligibility, booking, rescheduling and cancelling appointments |

`seed_demo` also creates the five-test mandatory screening panel, shelf-life rules (red cells 35 days, platelets 5, plasma 365), an eligibility rule and one appointment slot. It is safe to run again.

Run automated verification with:

```sh
python3 manage.py test bloodbank
python3 manage.py check
```

To refresh the Test Plan execution record after changing tests, run `python3 scripts/record_test_results.py`. It runs the suite and rewrites [`docs/test-results/TEST_EXECUTION_RESULTS.md`](../docs/test-results/TEST_EXECUTION_RESULTS.md).

## Docker deployment

```sh
docker compose up -d --build db web scheduler
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo
```

`nginx` (HTTPS on 443) also needs a certificate and key in `./certs`, and the `backup` service needs `BBMS_BACKUP_RECIPIENT` set to a GPG key. Both are described in [`docs/operations.md`](../docs/operations.md).

## Project documents

- [`docs/deviations.md`](../docs/deviations.md): every difference from the submitted SRS, design and Test Plan, with the reason and where it landed
- [`docs/test-results/TEST_EXECUTION_RESULTS.md`](../docs/test-results/TEST_EXECUTION_RESULTS.md): the result of every test case
- [`docs/REQUIREMENTS_AND_TASKS_STATUS.md`](../docs/REQUIREMENTS_AND_TASKS_STATUS.md): requirement-by-requirement status against the backlog
- [`docs/operations.md`](../docs/operations.md): availability, backup, restore and restart drills
- `phase1/`: the submitted SRS, Test Plan and design specification

## Module structure

The app follows the layered modular monolith in SADS section 3. Views call services, services apply the business rules, and models handle data access.

| Layer | Files | Responsibility |
| :--- | :--- | :--- |
| Presentation | `bloodbank/views.py`, `bloodbank/forms.py`, `bloodbank/templates/`, `bloodbank/urls.py` | HTTP handling, form validation, role and branch checks, role-specific pages and JSON API |
| Business rules | `bloodbank/services.py` | Donor registration, eligibility, deferrals, appointment booking and capacity, donation recording and unit IDs, shelf-life expiry, the unit state machine (`transition_unit`), screening release and quarantine, blood request creation, the request state machine and cancellation rules, reservation, cross-match and issue, account and configuration changes, login lockout, alerts, audit and notifications |
| Data access | `bloodbank/models.py`, `bloodbank/migrations/` | Django models, constraints, indexes and the append-only audit trail |
| Infrastructure | `bloodbank/middleware.py`, `bloodbank/management/commands/`, `bbms/settings.py`, `docker-compose.yml`, `nginx.conf` | Branch time zones, scheduled jobs (`expire_units`, `check_alerts`, `send_reminders`), demo seeding, deployment |

Views never write to the database themselves: every create, update or delete goes through a service, and `LayeredModuleTests` fails the build if `views.py` calls `.save()`, `.objects.create()`, `.update()` or `.delete()`.

`bloodbank/tests.py` holds unit tests for the eligibility rules, every unit and request state transition and the screening outcomes, plus integration tests for each Test Plan case (`TC-F..`, `TC-S..`, `TC-N..`) named in the test class docstrings.

## Scheduled unit expiry

`python3 manage.py expire_units` marks every unissued unit past its expiry time as Expired, releases any reservation it held and audits each change. In `docker-compose.yml` the `scheduler` service runs it every 5 minutes (`expire_units --every 300`). For a local install, run it from cron, for example `*/5 * * * * python3 manage.py expire_units`, or from Windows Task Scheduler. Inventory, dashboard and reservation still call the same check, so pages stay correct between runs.

`python3 manage.py send_reminders` emails every donor whose booked appointment starts within the next 24 hours and records each email as `SENT` or `FAILED` in `Notification` (FR-29). A reminder is sent once per appointment; a failed one is retried on the next run. The `reminders` service in `docker-compose.yml` runs it every 15 minutes (`send_reminders --every 900`); for a local install use cron, for example `*/15 * * * * python3 manage.py send_reminders`. Request-status emails go out whenever a request changes state. Set `BBMS_EMAIL_BACKEND` (and Django's `EMAIL_HOST` settings) to send real email; the default keeps messages in memory.

`python3 manage.py retention_report` lists, for each record type, how many records are older than the retention period set on **Configuration → Record retention** (NFR-12). The administrator exports donors, donations and the audit trail as CSV from the same screen. The policy and procedure are in [`docs/operations.md`](../docs/operations.md) section 5.

## Deployment truthfulness

`docker-compose.yml` specifies the documented PostgreSQL and Gunicorn deployment shape. It is a deployment template, not evidence that a production container, HTTPS reverse proxy, backup schedule, 100-user load test, browser compatibility matrix, or institutional compliance review has been executed. Before a real deployment, set a strong `BBMS_SECRET_KEY`, set `BBMS_DEBUG=0`, enable TLS and `BBMS_SECURE_SSL=1`, change all demo credentials, configure authenticated SMTP, configure encrypted backup storage/retention, and execute the complete acceptance test plan.

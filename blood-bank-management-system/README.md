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

Open `http://127.0.0.1:8000`. Demo accounts use password `demo123`:

- `staff@bbms.local`
- `hospital@bbms.local`
- `admin@bbms.local`
- `donor@bbms.local`

Run automated verification with:

```sh
python3 manage.py test bloodbank
python3 manage.py check
```

## Module structure

The app follows the layered modular monolith in SADS section 3. Views call services, services apply the business rules, and models handle data access.

| Layer | Files | Responsibility |
| :--- | :--- | :--- |
| Presentation | `bloodbank/views.py`, `bloodbank/forms.py`, `bloodbank/templates/`, `bloodbank/urls.py` | HTTP handling, form validation, role and branch checks, role-specific pages and JSON API |
| Business rules | `bloodbank/services.py` | Eligibility, donation recording and unit IDs, shelf-life expiry, the unit state machine (`transition_unit`), screening release and quarantine, request state machine, reservation, cross-match and issue, alerts, audit and notifications |
| Data access | `bloodbank/models.py`, `bloodbank/migrations/` | Django models, constraints, indexes and the append-only audit trail |
| Infrastructure | `bloodbank/middleware.py`, `bloodbank/management/commands/`, `bbms/settings.py`, `docker-compose.yml`, `nginx.conf` | Branch time zones, scheduled jobs (`expire_units`, `check_alerts`), demo seeding, deployment |

`bloodbank/tests.py` holds unit tests for the eligibility rules, every unit and request state transition and the screening outcomes, plus integration tests for each Test Plan case (`TC-F..`, `TC-S..`, `TC-N..`) named in the test class docstrings.

## Scheduled unit expiry

`python3 manage.py expire_units` marks every unissued unit past its expiry time as Expired, releases any reservation it held and audits each change. In `docker-compose.yml` the `scheduler` service runs it every 5 minutes (`expire_units --every 300`). For a local install, run it from cron, for example `*/5 * * * * python3 manage.py expire_units`, or from Windows Task Scheduler. Inventory, dashboard and reservation still call the same check, so pages stay correct between runs.

## Deployment truthfulness

`docker-compose.yml` specifies the documented PostgreSQL and Gunicorn deployment shape. It is a deployment template, not evidence that a production container, HTTPS reverse proxy, backup schedule, 100-user load test, browser compatibility matrix, or institutional compliance review has been executed. Before a real deployment, set a strong `BBMS_SECRET_KEY`, set `BBMS_DEBUG=0`, enable TLS and `BBMS_SECURE_SSL=1`, change all demo credentials, configure authenticated SMTP, configure encrypted backup storage/retention, and execute the complete acceptance test plan.

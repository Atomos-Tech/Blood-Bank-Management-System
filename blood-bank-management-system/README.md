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
- Structured audit events, notification delivery records, protected Django administration, CSRF-protected forms, generic authentication failures, rate-limited lockout, and 15-minute session lifetime.

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

## Deployment truthfulness

`docker-compose.yml` specifies the documented PostgreSQL and Gunicorn deployment shape. It is a deployment template, not evidence that a production container, HTTPS reverse proxy, backup schedule, 100-user load test, browser compatibility matrix, or institutional compliance review has been executed. Before a real deployment, set a strong `BBMS_SECRET_KEY`, set `BBMS_DEBUG=0`, enable TLS and `BBMS_SECURE_SSL=1`, change all demo credentials, configure authenticated SMTP, configure encrypted backup storage/retention, and execute the complete acceptance test plan.

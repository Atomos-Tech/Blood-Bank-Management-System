# Blood Donation & Availability Management System

A working Django MVP for a blood bank management system with 4 roles:
Donor, Blood Bank Staff, Hospital/Recipient, and System Administrator.

The database (`db.sqlite3`) already ships with seed data, so you can run
this immediately without any setup step beyond installing Django.

---

## 1. Run it (takes ~2 minutes)

```bash
# 1. Unzip this project, then cd into it
cd bloodbank_project

# 2. Install Django (only dependency)
pip install -r requirements.txt
# if that's blocked by your system's Python, use:
# pip install -r requirements.txt --break-system-packages

# 3. Start the server
python manage.py runserver

# 4. Open in your browser
# http://127.0.0.1:8000/
```

That's it — the database already has demo data loaded (branches, donors,
staff, a hospital account, blood units in stock, and one pending request)
so you can demo immediately.

If you ever want to wipe and reseed fresh demo data:
```bash
rm db.sqlite3
python manage.py migrate
python manage.py seed_demo
```

---

## 2. Demo Login Credentials

| Role      | Username    | Password      | Where to go after login |
|-----------|-------------|---------------|--------------------------|
| Admin     | admin       | admin12345    | `/admin/` + `/settings/` + `/reports/` |
| Staff     | staff1      | staff12345    | `/staff/` (auto-redirected) |
| Hospital  | hospital1   | hospital12345 | `/hospital/` (auto-redirected) |
| Donor     | donor1      | donor12345    | `/donor/` (auto-redirected) |
| Donor     | donor2 ... donor10 | donor12345 | same |

---

## 3. What's new in this version

**Visual design** — a full custom design system replacing generic Bootstrap:
self-hosted Fraunces (display serif) + Inter (body) fonts, a deliberate
garnet/charcoal/teal color palette, semantic panel colors (amber-left =
needs action, teal-left = healthy), and one live animated moment (the
pulsing "units available now" counter on the homepage). No external CDN
dependencies for fonts or charts — everything is bundled in `core/static/`
so it works even on a locked-down network.

**New features closing earlier gaps:**
- **Appointment scheduling** — donors book a branch/date/time slot; staff
  see an upcoming-appointments queue and mark visits completed
- **In-app notifications** — a bell icon with unread count; donors and
  hospitals get notified when a donation passes testing or a request is
  approved/issued/rejected
- **Reports dashboard with live charts** — stock-by-blood-group bar chart
  and a 14-day donation trend line chart (Chart.js, self-hosted), plus a
  low-stock alert panel
- **Admin settings page** — donor age/weight rules, donation gap, unit
  shelf life, and low-stock threshold are now editable at `/settings/`
  instead of hardcoded constants

---

## 4. Suggested Demo Script (7–9 minutes)

**Act 1 — Donor side (1 min)**
1. Go to the home page, click **Become a Donor**, register a brand-new donor
   (use a fresh DOB so you can show the eligibility logic).
2. Show the Donor Dashboard: blood group, live eligibility check (age/weight/
   90-day gap since last donation), and donation history table.

**Act 2 — Staff side (2–3 min) — this is the core workflow**
1. Log out, log in as `staff1`.
2. Staff Dashboard shows **live inventory counts per blood group** and a list
   of **pending hospital requests**.
3. Click **+ Record Donation**, pick an existing donor, save it — it now
   appears under "Donations Awaiting Lab Test."
4. Click **Enter Result** on that donation, mark it **Passed** — a new
   blood unit is automatically created and shows up as Available in
   inventory (click **View Full Inventory** to prove it). The donor also
   gets an in-app notification.
5. Back on the dashboard, find the pending request (blood group **O+**,
   seeded as an emergency request) and click **Approve**, then **Issue
   Units**. Point out: the system automatically matches **compatible**
   blood groups (not just exact match — e.g. O- can cover an O+ request)
   and deducts the issued units from available stock. The hospital account
   gets notified at each step.
6. Click **Appointments** to show the donor-booked queue, and **Reports**
   to show the live stock and 14-day donation trend charts plus the
   low-stock alert.

**Act 3 — Hospital side (1–2 min)**
1. Log out, log in as `hospital1`.
2. Click **Search Availability** — show real-time stock by blood group.
3. Click **+ New Blood Request**, submit a new request.
4. Click **Track** on it to show status tracking (Pending → Approved →
   Issued), same as what you just approved as staff in Act 2.

**Act 4 — Admin side (1 min)**
1. Log in at `/admin/` as `admin` to show the back-office data views.
2. Visit `/settings/` to show the editable eligibility rules and low-stock
   threshold — change one and point out it takes effect immediately
   (e.g. lower the minimum donor age and re-check a donor's eligibility).

---

## 5. What's implemented vs. simplified (be upfront about this if asked)

**Implemented and working:**
- Role-based auth (Donor / Staff / Hospital / Admin) using Django's built-in
  auth system extended with a `role` field
- Donor registration + automatic eligibility rule engine (age, weight,
  donation gap — all configurable by the admin)
- Appointment booking and staff-side appointment queue
- Donation recording → lab test result entry → automatic blood unit
  creation with configurable shelf life
- In-app notifications for donors and hospitals
- Inventory view with live per-blood-group availability counts and
  automatic expiry flagging
- Blood request workflow: create → approve/reject → issue, with a real
  blood-group compatibility matrix (not just exact-match)
- Hospital search-by-availability and request status tracking
- Reports dashboard with live charts (stock by group, 14-day donation
  trend) and a low-stock alert
- Admin settings page for eligibility rules and thresholds
- Django admin as an additional back-office data view

**Deliberately simplified for the 2-week/team-of-4 timeline:**
- Notifications are in-app only — no real SMS/email delivery integration
  (would need a paid SMS gateway or email service credentials)
- No automated penetration testing — relies on Django's built-in
  CSRF/auth/permission protections
- Single shared compatibility matrix rather than a full blood-bank-grade
  cross-match/serology workflow

---

## 6. Project structure

```
bloodbank_project/
├── manage.py
├── requirements.txt
├── db.sqlite3                  # pre-seeded demo database
├── bloodbank_project/          # settings, root urls
└── core/                       # the whole app
    ├── models.py                # User, Branch, Donor, Donation, BloodUnit, BloodRequest
    ├── admin.py                 # Django admin registrations
    ├── forms.py                 # signup + workflow forms
    ├── views.py                 # dashboards + business logic
    ├── urls.py
    ├── management/commands/seed_demo.py   # demo data seeder
    └── migrations/
templates/
├── base.html
├── registration/login.html
└── core/                       # one template per page
```

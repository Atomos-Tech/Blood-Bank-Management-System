# TC-N06 — First-time staff usability (NFR-07)

**Requirement (SRS NFR-07):** a first-time staff user completes donor lookup and appointment check-in within 5 minutes, using these written instructions and without developer help.

**Backlog item:** `BB-44` (#46). The test case is listed in the SRS traceability table; the Test Plan has no section for it, so this file is its procedure and record.

## Setup (done by the facilitator, not the participant)

1. Start the app and load the demonstration data:
   ```sh
   cd blood-bank-management-system
   python3 manage.py migrate
   python3 manage.py seed_demo
   python3 manage.py runserver
   ```
2. As `admin@bbms.local`, open **Configuration** and add an appointment slot at the Main Demonstration Branch for later **today**.
3. Sign in as `donor@bbms.local` and book that slot, then sign out. The donor is **Ira Donor**.
4. Give the participant only the task card below. Do not demonstrate the app.

## Task card (hand this to the participant)

> You are a blood bank staff member on your first day. A donor has just arrived at the desk for her appointment.
>
> 1. Open http://127.0.0.1:8000 and sign in with email `staff@bbms.local` and password `demo123`.
> 2. Find the donor **Ira Donor** using the search box on the **Donors** page. Her phone number is 9000000000 if the name does not work.
> 3. Check her in for today's appointment.
> 4. Tell the facilitator when the screen confirms she is checked in.

## Measurement

- The facilitator starts a stopwatch when the participant reads step 1 and stops it at step 4.
- **Pass:** the donor shows **Checked in** within **5 minutes** and the participant did not ask for help with the app itself. Asking what a word means is allowed and is noted.
- The audit trail confirms the outcome: an `APPOINTMENT_CHECKED_IN` event for the appointment, made by `staff@bbms.local`.

## Results

| Date | Participant (first-time user, not on the dev team) | Browser | Time taken (mm:ss) | Help needed? | Result | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| _(to be filled during the TC-N06 trial)_ | | | | | | |

## Supporting automated check

`DonorLookupAndCheckInTests` in `bloodbank/tests.py` covers the same path without a person: search by name, phone, email or donor number, check-in from the search results or from **Today's appointments**, branch scoping, and the audit event. It shows the features work; it does not replace the timed trial above, which needs a real first-time user.

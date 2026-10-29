from datetime import datetime, timedelta, timezone as dt_timezone
import csv
import importlib
import io
from io import StringIO
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.hashers import check_password, identify_hasher
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, OperationalError
from django.urls import reverse
import threading

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from . import urls as bloodbank_urls

from .models import (
    SCREENING_QUESTIONS,
    Appointment,
    AppointmentSlot,
    AuditEvent,
    BloodRequest,
    BloodUnit,
    Branch,
    CrossMatch,
    Deferral,
    Donation,
    DonationType,
    Donor,
    EligibilityRule,
    ImmutableAuditRecord,
    Issue,
    LabResult,
    Notification,
    RequestTransition,
    Reservation,
    ShelfLifeRule,
    TestType,
    UserProfile,
)
from .services import (
    check_in_appointment,
    send_appointment_reminders,
    AppointmentError,
    GENERIC_LOGIN_ERROR,
    LOGIN_FAILURE_LIMIT,
    LOGIN_FAILURE_WINDOW_SECONDS,
    LOGIN_LOCKOUT_SECONDS,
    LastAdministrator,
    _failure_key,
    account_is_locked,
    assign_account_role,
    book_appointment,
    cancel_appointment,
    clear_login_failures,
    create_account,
    end_user_sessions,
    next_eligible_date,
    calculate_expiry,
    record_login_failure,
    set_account_active,
    UNIT_TRANSITIONS,
    DuplicateLabResult,
    InvalidUnitTransition,
    audit,
    check_stock_and_expiry_alerts,
    discard_unit,
    expire_units,
    record_donation,
    record_lab_result,
    eligibility,
    issue_reservation,
    release_reservation,
    release_screened_unit,
    reserve_units,
    transition_request,
    transition_unit,
    MissingHospitalAssignment,
    create_blood_request,
    find_idempotent_request,
    hospital_may_cancel,
    record_crossmatch,
    record_deferral,
    register_donor,
    request_action_target,
)


class WorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN", low_stock_threshold=5, near_expiry_days=7)
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        self.staff = User.objects.create_user("staff@example.test", email="staff@example.test", password="very-safe-demo-password")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Donor One",
            email="donor@example.test",
            phone="9000000000",
            date_of_birth=timezone.localdate() - timedelta(days=25 * 365),
            weight_kg=60,
            blood_group="O+",
            medical_history="Confidential asthma note",
            consent_given=True,
        )

    def test_eligibility_requires_consent_and_rule_pass(self):
        ok, reasons = eligibility(self.donor)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_screening_requires_complete_acceptable_panel(self):
        donation = Donation.objects.create(
            donation_id="D-1", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        unit = BloodUnit.objects.create(
            unit_id="U-1",
            donation=donation,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=20),
            branch=self.branch,
        )
        test = TestType.objects.create(name="HIV")
        LabResult.objects.create(unit=unit, test_type=test, outcome=LabResult.Outcome.ACCEPTABLE, recorded_by=self.staff)
        self.assertEqual(release_screened_unit(unit, self.staff), BloodUnit.Status.AVAILABLE)

    def test_reservation_and_issue_requires_crossmatch(self):
        donation = Donation.objects.create(
            donation_id="D-2", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        unit = BloodUnit.objects.create(
            unit_id="U-2",
            donation=donation,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=20),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        req = BloodRequest.objects.create(
            hospital="City",
            created_by=self.staff,
            branch=self.branch,
            blood_group="O+",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-1",
            status=BloodRequest.Status.APPROVED,
        )
        reservation = reserve_units(req, self.staff)[0]
        with self.assertRaises(ValueError):
            issue_reservation(reservation, self.staff)
        CrossMatch.objects.create(reservation=reservation, outcome="COMPATIBLE", recorded_by=self.staff)
        issue_reservation(reservation, self.staff)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.ISSUED)

    def test_donor_cannot_open_staff_directory(self):
        donor_user = User.objects.create_user("donor@example.test", password="very-safe-demo-password")
        UserProfile.objects.create(user=donor_user, role=UserProfile.Role.DONOR, branch=self.branch)
        self.client.force_login(donor_user)
        self.assertEqual(self.client.get("/donors/").status_code, 403)

    def test_authentication_failure_is_generic(self):
        response = self.client.post(
            "/login/", {"email": "unknown@example.test", "password": "wrong-password"}, follow=True
        )
        self.assertContains(response, "Invalid email or password.")


class InventorySearchTests(TestCase):
    """BB-24 [FR-17] & TC-F09: Search available stock by group, component, branch and expiry."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.staff = User.objects.create_user("staff@bbms.test", email="staff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hospital = User.objects.create_user("hosp@bbms.test", email="hosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="General Hospital"
        )
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Alice SecretDonor",
            email="alice@example.test",
            phone="9123456789",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=65,
            blood_group="O+",
            medical_history="Sensitive donor history",
            consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-INV-1", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        now = timezone.now()
        self.unit_late = BloodUnit.objects.create(
            unit_id="UNIT-LATE",
            donation=self.donation,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=now + timedelta(days=20),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.unit_soon = BloodUnit.objects.create(
            unit_id="UNIT-SOON",
            donation=self.donation,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=now + timedelta(days=5),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.unit_plasma = BloodUnit.objects.create(
            unit_id="UNIT-PLASMA",
            donation=self.donation,
            blood_group="A+",
            component="Plasma",
            volume_ml=250,
            expiry_at=now + timedelta(days=10),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.unit_quarantined = BloodUnit.objects.create(
            unit_id="UNIT-QUAR",
            donation=self.donation,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=now + timedelta(days=7),
            branch=self.branch,
            status=BloodUnit.Status.QUARANTINED,
        )

    def test_tc_f09_filters_and_soonest_expiry_sorting(self):
        self.client.force_login(self.hospital)
        cutoff = (timezone.localdate() + timedelta(days=12)).isoformat()
        resp = self.client.get(
            f"/inventory/?blood_group=O%2B&component=Packed+Red+Cells&branch={self.branch.pk}&expires_before={cutoff}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "UNIT-SOON")
        self.assertNotContains(resp, "UNIT-LATE")
        self.assertNotContains(resp, "UNIT-PLASMA")
        self.assertNotContains(resp, "UNIT-QUAR")

        # Without expires_before, both available O+ units appear sorted by soonest expiry first
        resp_all = self.client.get("/inventory/?blood_group=O%2B&component=Packed+Red+Cells")
        body = resp_all.content.decode()
        self.assertLess(body.index("UNIT-SOON"), body.index("UNIT-LATE"))
        self.assertNotIn("UNIT-QUAR", body)

        # JSON API also respects filters and sorts by soonest expiry
        api_resp = self.client.get(f"/api/v1/inventory/?blood_group=O%2B&expires_before={cutoff}")
        self.assertEqual(api_resp.status_code, 200)
        items = api_resp.json()["items"]
        self.assertEqual([i["unit_id"] for i in items], ["UNIT-SOON"])


class LowStockAndNearExpiryAlertTests(TestCase):
    """BB-25 [FR-18] & TC-F10: Alert staff on low stock and near-expiry units."""

    def setUp(self):
        self.branch = Branch.objects.create(
            name="Alert Branch", code="ALRT", low_stock_threshold=3, near_expiry_days=5
        )
        self.staff = User.objects.create_user("alertstaff@bbms.test", email="alertstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Donor Alert",
            email="donoralert@example.test",
            phone="9000000001",
            date_of_birth=timezone.localdate() - timedelta(days=28 * 365),
            weight_kg=60,
            blood_group="B+",
            consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-ALRT-1", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        # 1 available unit (below threshold of 3) expiring in 2 days (inside 5-day near-expiry window)
        self.near_unit = BloodUnit.objects.create(
            unit_id="UNIT-NEAR-1",
            donation=self.donation,
            blood_group="B+",
            component="Platelets",
            volume_ml=250,
            expiry_at=timezone.now() + timedelta(days=2),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )

    def test_tc_f10_scheduled_alerts_are_idempotent_and_shown_on_dashboard(self):
        first_run = check_stock_and_expiry_alerts(branch=self.branch)
        self.assertEqual(len(first_run), 2)
        self.assertEqual(Notification.objects.filter(recipient="alertstaff@bbms.test").count(), 2)

        # Repeated run via management command does not duplicate alerts for unchanged condition
        call_command("check_alerts")
        self.assertEqual(Notification.objects.filter(recipient="alertstaff@bbms.test").count(), 2)

        # Dashboard shows both active warnings
        self.client.force_login(self.staff)
        dash = self.client.get("/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "Low Stock Alert:")
        self.assertContains(dash, "Near-Expiry Alert:")
        self.assertContains(dash, "UNIT-NEAR-1")


class BloodRequestWorkflowTests(TestCase):
    """BB-26 [FR-19], BB-27 [FR-20], BB-28 [FR-21] & TC-F11, TC-F12."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Central", code="CENT")
        self.staff = User.objects.create_user("staff_req@bbms.test", email="staff_req@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hosp1 = User.objects.create_user("hosp1@bbms.test", email="hosp1@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hosp1, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="City Hospital"
        )
        self.hosp2 = User.objects.create_user("hosp2@bbms.test", email="hosp2@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hosp2, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Metro Hospital"
        )

    def test_tc_f11_hospital_creates_request_and_validates_fields(self):
        self.client.force_login(self.hosp1)
        # Missing required_by -> rejected
        resp_missing = self.client.post(
            "/requests/",
            {
                "branch": self.branch.pk,
                "blood_group": "O+",
                "component": "Packed Red Cells",
                "quantity": 2,
                "priority": "Urgent",
                "clinical_reference": "CR-100",
            },
        )
        self.assertEqual(resp_missing.status_code, 200)
        self.assertEqual(BloodRequest.objects.count(), 0)

        # Past required_by -> rejected
        resp_past = self.client.post(
            "/requests/",
            {
                "branch": self.branch.pk,
                "blood_group": "O+",
                "component": "Packed Red Cells",
                "quantity": 2,
                "priority": "Urgent",
                "required_by": (timezone.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                "clinical_reference": "CR-100",
            },
        )
        self.assertContains(resp_past, "Required-by time must be in the future.")
        self.assertEqual(BloodRequest.objects.count(), 0)

        # Valid request -> starts Pending and tied to City Hospital
        resp_ok = self.client.post(
            "/requests/",
            {
                "branch": self.branch.pk,
                "blood_group": "O+",
                "component": "Packed Red Cells",
                "quantity": 2,
                "priority": "Urgent",
                "required_by": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "clinical_reference": "CR-101",
            },
        )
        self.assertEqual(resp_ok.status_code, 302)
        req = BloodRequest.objects.get(clinical_reference="CR-101")
        self.assertEqual(req.status, BloodRequest.Status.PENDING)
        self.assertEqual(req.hospital, "City Hospital")
        self.assertEqual(req.created_by, self.hosp1)

    def test_tc_f11_request_state_transitions_and_history_in_ui(self):
        req = BloodRequest.objects.create(
            hospital="City Hospital",
            created_by=self.hosp1,
            branch=self.branch,
            blood_group="A+",
            component="Plasma",
            quantity=2,
            priority="Routine",
            required_by=timezone.now() + timedelta(days=2),
            clinical_reference="CR-TRANS",
        )
        # Invalid transition Pending -> Fulfilled rejected
        with self.assertRaisesMessage(ValueError, "That request state transition is not permitted."):
            transition_request(req, BloodRequest.Status.FULFILLED, self.staff)

        transition_request(req, BloodRequest.Status.APPROVED, self.staff)
        transition_request(req, BloodRequest.Status.PARTIAL, self.staff)
        transition_request(req, BloodRequest.Status.FULFILLED, self.staff)

        # Terminal state Fulfilled -> Cancelled rejected
        with self.assertRaisesMessage(ValueError, "That request state transition is not permitted."):
            transition_request(req, BloodRequest.Status.CANCELLED, self.staff)

        transitions = list(req.transitions.order_by("id").values_list("from_status", "to_status", "actor__username"))
        self.assertEqual(
            transitions,
            [
                ("PENDING", "APPROVED", "staff_req@bbms.test"),
                ("APPROVED", "PARTIAL", "staff_req@bbms.test"),
                ("PARTIAL", "FULFILLED", "staff_req@bbms.test"),
            ],
        )

        # UI displays transition history
        self.client.force_login(self.staff)
        page = self.client.get("/requests/")
        self.assertContains(page, "PENDING &rarr; APPROVED")
        self.assertContains(page, "APPROVED &rarr; PARTIAL")
        self.assertContains(page, "PARTIAL &rarr; FULFILLED")

    def test_tc_f12_request_cancellation_permissions_by_role_and_state(self):
        pending_req = BloodRequest.objects.create(
            hospital="City Hospital",
            created_by=self.hosp1,
            branch=self.branch,
            blood_group="O+",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="CR-PEND",
            status=BloodRequest.Status.PENDING,
        )
        approved_req = BloodRequest.objects.create(
            hospital="City Hospital",
            created_by=self.hosp1,
            branch=self.branch,
            blood_group="O+",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="CR-APPR",
            status=BloodRequest.Status.APPROVED,
        )

        # Another hospital cannot cancel hosp1's pending request
        self.client.force_login(self.hosp2)
        r_other = self.client.post(f"/requests/{pending_req.pk}/cancel/")
        self.assertEqual(r_other.status_code, 403)

        # Hosp1 can cancel own Pending request
        self.client.force_login(self.hosp1)
        r_own_pending = self.client.post(f"/requests/{pending_req.pk}/cancel/")
        self.assertEqual(r_own_pending.status_code, 302)
        pending_req.refresh_from_db()
        self.assertEqual(pending_req.status, BloodRequest.Status.CANCELLED)

        # Hosp1 cannot cancel own Approved request
        r_own_approved = self.client.post(f"/requests/{approved_req.pk}/cancel/")
        self.assertEqual(r_own_approved.status_code, 403)
        approved_req.refresh_from_db()
        self.assertEqual(approved_req.status, BloodRequest.Status.APPROVED)

        # Blood bank staff can cancel Approved request
        self.client.force_login(self.staff)
        r_staff_cancel = self.client.post(f"/requests/{approved_req.pk}/cancel/")
        self.assertEqual(r_staff_cancel.status_code, 302)
        approved_req.refresh_from_db()
        self.assertEqual(approved_req.status, BloodRequest.Status.CANCELLED)


class ReservationCrossMatchAndAtomicIssueTests(TestCase):
    """BB-29 [FR-22], BB-30 [FR-23], BB-31 [FR-24] & TC-F13."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.staff = User.objects.create_user("staff_res@bbms.test", email="staff_res@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Donor Res",
            email="donorres@example.test",
            phone="9000000002",
            date_of_birth=timezone.localdate() - timedelta(days=26 * 365),
            weight_kg=64,
            blood_group="O-",
            consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-RES-1", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        now = timezone.now()
        self.unit_later = BloodUnit.objects.create(
            unit_id="U-RES-LATER",
            donation=self.donation,
            blood_group="O-",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=now + timedelta(days=25),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.unit_sooner = BloodUnit.objects.create(
            unit_id="U-RES-SOONER",
            donation=self.donation,
            blood_group="O-",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=now + timedelta(days=8),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )

    def test_tc_f13_soonest_expiry_single_active_reservation_and_re_reservation_after_release(self):
        req1 = BloodRequest.objects.create(
            hospital="City",
            created_by=self.staff,
            branch=self.branch,
            blood_group="O-",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-R1",
            status=BloodRequest.Status.APPROVED,
        )
        req2 = BloodRequest.objects.create(
            hospital="City",
            created_by=self.staff,
            branch=self.branch,
            blood_group="O-",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-R2",
            status=BloodRequest.Status.APPROVED,
        )

        # Reserves soonest expiry first (U-RES-SOONER)
        res1 = reserve_units(req1, self.staff)[0]
        self.assertEqual(res1.unit, self.unit_sooner)

        # Database constraint blocks a second active reservation for the same unit
        from django.db import transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reservation.objects.create(unit=self.unit_sooner, request=req2, reserved_by=self.staff, active=True)

        # Releasing res1 returns U-RES-SOONER to AVAILABLE and lets req2 reserve it again
        release_reservation(res1, self.staff)
        self.unit_sooner.refresh_from_db()
        self.assertEqual(self.unit_sooner.status, BloodUnit.Status.AVAILABLE)

        res2 = reserve_units(req2, self.staff)[0]
        self.assertEqual(res2.unit, self.unit_sooner)
        self.assertEqual(Reservation.objects.filter(unit=self.unit_sooner).count(), 2)
        self.assertEqual(Reservation.objects.filter(unit=self.unit_sooner, active=True).count(), 1)

    def test_tc_f13_incompatible_crossmatch_blocks_issue_and_failure_rolls_back_atomically(self):
        req = BloodRequest.objects.create(
            hospital="City",
            created_by=self.staff,
            branch=self.branch,
            blood_group="O-",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-ATOM",
            status=BloodRequest.Status.APPROVED,
        )
        reservation = reserve_units(req, self.staff)[0]

        # Incompatible cross-match blocks issue
        cm = CrossMatch.objects.create(reservation=reservation, outcome="INCOMPATIBLE", recorded_by=self.staff)
        self.assertIsNotNone(cm.recorded_at)
        self.assertEqual(cm.recorded_by, self.staff)
        with self.assertRaisesMessage(ValueError, "An incompatible cross-match cannot be issued."):
            issue_reservation(reservation, self.staff)

        # Update to compatible cross-match, then force a failure part-way through issue_reservation
        cm.outcome = "COMPATIBLE"
        cm.save(update_fields=["outcome"])

        with patch("bloodbank.services.transition_request", side_effect=RuntimeError("Simulated failure during commit")):
            with self.assertRaises(RuntimeError):
                issue_reservation(reservation, self.staff)

        # Verify atomic rollback: no Issue created, reservation still active, unit still RESERVED, request still APPROVED
        reservation.refresh_from_db()
        reservation.unit.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(Issue.objects.filter(reservation=reservation).count(), 0)
        self.assertTrue(reservation.active)
        self.assertEqual(reservation.unit.status, BloodUnit.Status.RESERVED)
        self.assertEqual(req.status, BloodRequest.Status.APPROVED)

        # Now run without simulated failure -> succeeds atomically
        issue_reservation(reservation, self.staff)
        reservation.refresh_from_db()
        reservation.unit.refresh_from_db()
        req.refresh_from_db()
        self.assertFalse(reservation.active)
        self.assertEqual(reservation.unit.status, BloodUnit.Status.ISSUED)
        self.assertEqual(req.status, BloodRequest.Status.FULFILLED)


class BranchAccessRestrictionTests(TestCase):
    """BB-46 [NFR-09] & TC-S02: Branch-level access restrictions for operational users."""

    def setUp(self):
        self.branch_a = Branch.objects.create(name="Branch Alpha", code="BR-A")
        self.branch_b = Branch.objects.create(name="Branch Beta", code="BR-B")

        self.staff_a = User.objects.create_user("staff_a@bbms.test", email="staff_a@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff_a, role=UserProfile.Role.STAFF, branch=self.branch_a)

        self.staff_b = User.objects.create_user("staff_b@bbms.test", email="staff_b@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff_b, role=UserProfile.Role.STAFF, branch=self.branch_b)

        self.admin = User.objects.create_user("admin@bbms.test", email="admin@bbms.test", password="pw")
        UserProfile.objects.create(user=self.admin, role=UserProfile.Role.ADMIN, branch=self.branch_a)

        self.donor_a = Donor.objects.create(
            branch=self.branch_a,
            full_name="Alpha Donor",
            email="alpha@example.test",
            phone="9000000011",
            date_of_birth=timezone.localdate() - timedelta(days=25 * 365),
            weight_kg=60,
            blood_group="A+",
            consent_given=True,
        )
        self.donor_b = Donor.objects.create(
            branch=self.branch_b,
            full_name="Beta Donor",
            email="beta@example.test",
            phone="9000000022",
            date_of_birth=timezone.localdate() - timedelta(days=25 * 365),
            weight_kg=60,
            blood_group="B+",
            consent_given=True,
        )
        don_a = Donation.objects.create(
            donation_id="DON-BRA", donor=self.donor_a, collected_at=timezone.now(), collector=self.staff_a, branch=self.branch_a
        )
        don_b = Donation.objects.create(
            donation_id="DON-BRB", donor=self.donor_b, collected_at=timezone.now(), collector=self.staff_b, branch=self.branch_b
        )
        self.unit_a = BloodUnit.objects.create(
            unit_id="UNIT-ALPHA",
            donation=don_a,
            blood_group="A+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=20),
            branch=self.branch_a,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.unit_b = BloodUnit.objects.create(
            unit_id="UNIT-BETA",
            donation=don_b,
            blood_group="B+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=20),
            branch=self.branch_b,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.req_b = BloodRequest.objects.create(
            hospital="Beta Hospital",
            created_by=self.staff_b,
            branch=self.branch_b,
            blood_group="B+",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-BETA",
            status=BloodRequest.Status.PENDING,
        )

    def test_tc_s02_branch_isolation_and_cross_branch_denial(self):
        self.client.force_login(self.staff_a)

        # Staff A sees only Branch A records on donors, inventory, requests, reports, and audit
        self.assertContains(self.client.get("/donors/"), "Alpha Donor")
        self.assertNotContains(self.client.get("/donors/"), "Beta Donor")

        inv_a = self.client.get("/inventory/")
        self.assertContains(inv_a, "UNIT-ALPHA")
        self.assertNotContains(inv_a, "UNIT-BETA")

        req_a = self.client.get("/requests/")
        self.assertNotContains(req_a, "REF-BETA")

        # Cross-branch query parameter or direct action is denied with 403 and audited
        self.assertEqual(self.client.get(f"/inventory/?branch={self.branch_b.pk}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/inventory/?branch={self.branch_b.pk}").status_code, 403)
        self.assertEqual(self.client.get(f"/donors/?branch={self.branch_b.pk}").status_code, 403)
        self.assertEqual(self.client.get(f"/requests/?branch={self.branch_b.pk}").status_code, 403)
        self.assertEqual(self.client.get(f"/reports/?branch={self.branch_b.pk}").status_code, 403)
        self.assertEqual(self.client.get(f"/audit/?branch={self.branch_b.pk}").status_code, 403)
        self.assertEqual(self.client.get(f"/inventory/{self.unit_b.pk}/screen/").status_code, 403)
        self.assertEqual(self.client.post(f"/requests/{self.req_b.pk}/approve/").status_code, 403)
        self.assertTrue(
            AuditEvent.objects.filter(actor=self.staff_a, event_type="UNAUTHORIZED_ACCESS", outcome="DENIED").exists()
        )

        # Administrator sees all branches
        self.client.force_login(self.admin)
        inv_admin = self.client.get("/inventory/")
        self.assertContains(inv_admin, "UNIT-ALPHA")
        self.assertContains(inv_admin, "UNIT-BETA")
        self.assertEqual(self.client.get(f"/inventory/?branch={self.branch_b.pk}").status_code, 200)


class PrivacyAndMinimumDataPerRoleTests(TestCase):
    """BB-48 [NFR-11], BB-58 [SEC-09] & TC-S04: Privacy and role-based data minimization."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.staff = User.objects.create_user("staff_p@bbms.test", email="staff_p@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)

        self.hosp = User.objects.create_user("hosp_p@bbms.test", email="hosp_p@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hosp, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Privacy Hospital"
        )

        self.donor_user = User.objects.create_user("donor_p@bbms.test", email="donor_p@bbms.test", password="pw")
        UserProfile.objects.create(user=self.donor_user, role=UserProfile.Role.DONOR, branch=self.branch)
        self.donor = Donor.objects.create(
            user=self.donor_user,
            branch=self.branch,
            full_name="Private Donor Identity",
            email="donor_p@bbms.test",
            phone="9988776655",
            date_of_birth=timezone.localdate() - timedelta(days=27 * 365),
            weight_kg=68,
            blood_group="O+",
            medical_history="CONFIDENTIAL-HYPERTENSION-NOTE",
            consent_given=True,
        )
        don = Donation.objects.create(
            donation_id="DON-PRIV-1", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        self.unit_avail = BloodUnit.objects.create(
            unit_id="UNIT-PUBLIC-SAFE",
            donation=don,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=15),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        self.unit_quar = BloodUnit.objects.create(
            unit_id="UNIT-SECRET-QUAR",
            donation=don,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=15),
            branch=self.branch,
            status=BloodUnit.Status.QUARANTINED,
        )
        hiv = TestType.objects.create(name="HIV-PANEL-MARKER", required=True)
        LabResult.objects.create(unit=self.unit_avail, test_type=hiv, outcome=LabResult.Outcome.ACCEPTABLE, recorded_by=self.staff)
        LabResult.objects.create(unit=self.unit_quar, test_type=hiv, outcome=LabResult.Outcome.REACTIVE, recorded_by=self.staff)

    def test_tc_s04_hospital_search_api_dashboard_and_reports_hide_donor_and_medical_data(self):
        self.client.force_login(self.hosp)
        for path in ["/", "/inventory/", "/api/v1/inventory/", "/reports/", "/requests/"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            text = resp.content.decode()
            self.assertNotIn("Private Donor Identity", text)
            self.assertNotIn("donor_p@bbms.test", text)
            self.assertNotIn("9988776655", text)
            self.assertNotIn("CONFIDENTIAL-HYPERTENSION-NOTE", text)
            self.assertNotIn("HIV-PANEL-MARKER", text)
            self.assertNotIn("UNIT-SECRET-QUAR", text)

        api_item = self.client.get("/api/v1/inventory/").json()["items"][0]
        self.assertEqual(
            set(api_item.keys()),
            {"unit_id", "blood_group", "component", "expiry_at", "branch__code"},
        )

    def test_tc_s04_donor_role_sees_only_own_summary_and_record(self):
        self.client.force_login(self.donor_user)
        dash = self.client.get("/")
        self.assertEqual(dash.status_code, 200)
        self.assertNotContains(dash, "UNIT-PUBLIC-SAFE")
        self.assertNotContains(dash, "REGISTERED DONORS")

        # Donor cannot access another donor's record via query parameter
        self.assertEqual(self.client.get("/my-record/?donor=99999").status_code, 403)
        self.assertEqual(self.client.get("/inventory/").status_code, 403)
        self.assertEqual(self.client.get("/requests/").status_code, 403)
        self.assertEqual(self.client.get("/reports/").status_code, 403)


class ProductionHTTPSConfigurationTests(TestCase):
    """BB-54 [SEC-05]: HTTPS only in production configuration inspection."""

    def test_sec_05_nginx_compose_and_settings_enforce_https_and_hsts(self):
        base_dir = Path(__file__).resolve().parent.parent
        nginx_conf = (base_dir / "nginx.conf").read_text()
        compose_yml = (base_dir / "docker-compose.yml").read_text()

        self.assertIn("listen 80;", nginx_conf)
        self.assertIn("return 301 https://$host$request_uri;", nginx_conf)
        self.assertIn("listen 443 ssl;", nginx_conf)
        self.assertIn("Strict-Transport-Security", nginx_conf)
        self.assertIn('BBMS_SECURE_SSL: "1"', compose_yml)
        self.assertIn('"443:443"', compose_yml)

        import bbms.settings as bbms_settings
        with patch.dict(os.environ, {"BBMS_SECURE_SSL": "1"}):
            importlib.reload(bbms_settings)
            self.assertTrue(bbms_settings.SECURE_SSL_REDIRECT)
            self.assertTrue(bbms_settings.SESSION_COOKIE_SECURE)
            self.assertTrue(bbms_settings.CSRF_COOKIE_SECURE)
            self.assertEqual(bbms_settings.SECURE_HSTS_SECONDS, 31536000)
            self.assertTrue(bbms_settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)
            self.assertTrue(bbms_settings.SECURE_HSTS_PRELOAD)
        importlib.reload(bbms_settings)


class PerformanceAndConcurrencyEvidenceTests(TestCase):
    """BB-38 [NFR-01] & BB-39 [NFR-02]: Verify benchmark artifacts and SLA metrics."""

    def test_tc_n01_and_tc_n02_benchmark_results_recorded_and_passing(self):
        base_dir = Path(__file__).resolve().parent.parent
        metrics_path = base_dir / "docs" / "test-results" / "benchmark_metrics.json"
        self.assertTrue(metrics_path.exists())
        data = json.loads(metrics_path.read_text())

        self.assertGreaterEqual(data["environment"]["seeded_donors"], 5000)
        self.assertGreaterEqual(data["environment"]["seeded_units"], 10000)

        nfr01 = data["TC_N01_NFR_01"]
        self.assertEqual(nfr01["concurrent_users"], 100)
        self.assertEqual(nfr01["error_count"], 0)
        self.assertLessEqual(nfr01["p95_ms"], 3000.0)
        self.assertGreaterEqual(nfr01["percent_within_3s"], 95.0)
        self.assertTrue(nfr01["sla_passed"])

        nfr02 = data["TC_N02_NFR_02"]
        self.assertEqual(nfr02["duplicate_unit_assignments"], 0)
        self.assertLessEqual(nfr02["max_ms"], 5000.0)
        self.assertTrue(nfr02["sla_passed"])


class SADSInventoryAndRequestAPITests(TestCase):
    """SADS Section 8.1, 8.2, 8.3 & UC-04/UC-05: REST API endpoints, idempotency, and standard error bodies."""

    def setUp(self):
        self.branch = Branch.objects.create(name="API Branch", code="APIB")
        self.staff = User.objects.create_user("apistaff@bbms.test", email="apistaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hosp = User.objects.create_user("apihosp@bbms.test", email="apihosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hosp, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="API City Hospital"
        )
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="API Donor",
            email="apidonor@example.test",
            phone="9000000099",
            date_of_birth=timezone.localdate() - timedelta(days=25 * 365),
            weight_kg=62,
            blood_group="O+",
            consent_given=True,
        )
        don = Donation.objects.create(
            donation_id="DON-API-1", donor=self.donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        self.unit = BloodUnit.objects.create(
            unit_id="UNIT-API-1",
            donation=don,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=350,
            expiry_at=timezone.now() + timedelta(days=14),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )

    def test_sads_api_workflow_idempotency_and_standard_errors(self):
        # 1. POST /api/v1/requests with Idempotency-Key (UC-04)
        self.client.force_login(self.hosp)
        payload = {
            "branch": self.branch.pk,
            "blood_group": "O+",
            "component": "Packed Red Cells",
            "quantity": 1,
            "priority": "Urgent",
            "required_by": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "clinical_reference": "API-REF-01",
        }
        r1 = self.client.post(
            "/api/v1/requests",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idem-req-001",
        )
        self.assertEqual(r1.status_code, 201)
        req_id = r1.json()["id"]

        # Duplicate submission with same Idempotency-Key returns existing request without creating duplicate
        r2 = self.client.post(
            "/api/v1/requests",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idem-req-001",
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["id"], req_id)
        self.assertEqual(BloodRequest.objects.filter(clinical_reference="API-REF-01").count(), 1)

        # Standard error body check on invalid input (SADS 8.3)
        bad_inv = self.client.get("/api/v1/inventory?expires_before=not-a-date")
        self.assertEqual(bad_inv.status_code, 400)
        err_body = bad_inv.json()
        for key in ("code", "message", "field_errors", "correlation_id", "timestamp"):
            self.assertIn(key, err_body)

        # 2. PATCH /api/v1/requests/{id}/status as Staff -> APPROVED
        self.client.force_login(self.staff)
        st_resp = self.client.patch(
            f"/api/v1/requests/{req_id}/status",
            data=json.dumps({"status": "APPROVED"}),
            content_type="application/json",
        )
        self.assertEqual(st_resp.status_code, 200)
        self.assertEqual(st_resp.json()["status"], "APPROVED")

        # 3. POST /api/v1/requests/{id}/reserve with Idempotency-Key
        res_resp = self.client.post(
            f"/api/v1/requests/{req_id}/reserve",
            data=json.dumps({}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idem-res-001",
        )
        self.assertEqual(res_resp.status_code, 200)
        res_id = res_resp.json()["reservations"][0]["id"]

        # Idempotent retry of reservation returns 200 with same payload
        res_retry = self.client.post(
            f"/api/v1/requests/{req_id}/reserve",
            data=json.dumps({}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idem-res-001",
        )
        self.assertEqual(res_retry.status_code, 200)
        self.assertEqual(res_retry.json()["reservations"][0]["id"], res_id)

        # 4. POST /api/v1/issues before cross-match -> 409 conflict with standard error body
        issue_blocked = self.client.post(
            "/api/v1/issues",
            data=json.dumps({"reservation_id": res_id}),
            content_type="application/json",
        )
        self.assertEqual(issue_blocked.status_code, 409)
        self.assertEqual(issue_blocked.json()["code"], "issue_conflict")

        # Record compatible cross-match and issue via POST /api/v1/issues
        CrossMatch.objects.create(reservation_id=res_id, outcome="COMPATIBLE", recorded_by=self.staff)
        issue_ok = self.client.post(
            "/api/v1/issues",
            data=json.dumps({"reservation_id": res_id}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idem-issue-001",
        )
        self.assertEqual(issue_ok.status_code, 201)
        self.assertEqual(issue_ok.json()["unit_status"], "ISSUED")
        self.assertEqual(issue_ok.json()["request_status"], "FULFILLED")


class DonationCollectionDetailsTests(TestCase):
    """BB-17 [FR-10] & TC-F06: Record collection details for each blood unit."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Collection Branch", code="COLL")
        ShelfLifeRule.objects.create(component="Packed Red Cells", days=35)
        self.staff = User.objects.create_user("collector@bbms.test", email="collector@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Collection Donor",
            email="collection@example.test",
            phone="9000000101",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="A+",
            consent_given=True,
        )
        self.valid_post = {
            "donor": self.donor.pk,
            "branch": self.branch.pk,
            "blood_group": "A+",
            "volume_rbc": 350,
        }

    def test_tc_f06_all_collection_fields_stored_and_collector_is_logged_in_staff(self):
        self.client.force_login(self.staff)
        before = timezone.now()
        resp = self.client.post("/donations/new/", self.valid_post)
        self.assertEqual(resp.status_code, 302)

        unit = BloodUnit.objects.select_related("donation__donor", "donation__collector").get()
        self.assertGreaterEqual(unit.donation.collected_at, before)
        self.assertEqual(unit.volume_ml, 350)
        self.assertEqual(unit.blood_group, "A+")
        self.assertEqual(unit.component, "Packed Red Cells")
        self.assertEqual(unit.donation.collector, self.staff)
        self.assertEqual(unit.donation.donor, self.donor)
        self.assertEqual(unit.status, BloodUnit.Status.COLLECTED)

    def test_tc_f06_each_mandatory_field_is_required(self):
        self.client.force_login(self.staff)
        for field in ["donor", "branch", "blood_group", "volume_rbc"]:
            data = {k: v for k, v in self.valid_post.items() if k != field}
            resp = self.client.post("/donations/new/", data)
            self.assertEqual(resp.status_code, 200, field)
            # Component and volume are captured together: with no component volume the form rejects as a whole.
            self.assertIn("__all__" if field == "volume_rbc" else field, resp.context["form"].errors)
        self.assertEqual(Donation.objects.count(), 0)
        self.assertEqual(BloodUnit.objects.count(), 0)

    def test_tc_f06_collector_cannot_be_spoofed_from_the_form(self):
        other = User.objects.create_user("other@bbms.test", email="other@bbms.test", password="pw")
        self.client.force_login(self.staff)
        self.client.post("/donations/new/", dict(self.valid_post, collector=other.pk))
        self.assertEqual(Donation.objects.get().collector, self.staff)

    def test_staff_reach_donation_recording_from_inventory(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get("/inventory/"), 'href="/donations/new/"')
        hospital = User.objects.create_user("collhosp@bbms.test", email="collhosp@bbms.test", password="pw")
        UserProfile.objects.create(user=hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="H")
        self.client.force_login(hospital)
        self.assertNotContains(self.client.get("/inventory/"), 'href="/donations/new/"')


class ShelfLifeExpiryTests(TestCase):
    """BB-22 [FR-15] & TC-F08: Calculate expiry from collection time and shelf-life rule."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Expiry Branch", code="EXPB")
        for component, days in [("Packed Red Cells", 35), ("Platelets", 5), ("Plasma", 365)]:
            ShelfLifeRule.objects.create(component=component, days=days)
        self.staff = User.objects.create_user("expiry@bbms.test", email="expiry@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Expiry Donor",
            email="expirydonor@example.test",
            phone="9000000102",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="B+",
            consent_given=True,
        )

    def test_tc_f08_expiry_is_collection_time_plus_configured_days_per_component(self):
        collected = timezone.now().replace(microsecond=0)
        for component, days in [("Packed Red Cells", 35), ("Platelets", 5), ("Plasma", 365)]:
            self.assertEqual(calculate_expiry(component, collected), collected + timedelta(days=days))

    def test_tc_f08_recorded_unit_uses_configured_rule(self):
        self.client.force_login(self.staff)
        self.client.post(
            "/donations/new/",
            {"donor": self.donor.pk, "branch": self.branch.pk, "blood_group": "B+", "volume_plt": 250},
        )
        unit = BloodUnit.objects.select_related("donation").get()
        self.assertEqual(unit.expiry_at, unit.donation.collected_at + timedelta(days=5))

    def test_tc_f08_changed_rule_applies_to_new_units(self):
        ShelfLifeRule.objects.filter(component="Plasma").update(days=180)
        collected = timezone.now()
        self.assertEqual(calculate_expiry("Plasma", collected), collected + timedelta(days=180))

    def test_tc_f08_recording_blocked_without_rule(self):
        ShelfLifeRule.objects.filter(component="Plasma").delete()
        with self.assertRaises(ValueError):
            calculate_expiry("Plasma", timezone.now())
        self.client.force_login(self.staff)
        resp = self.client.post(
            "/donations/new/",
            {"donor": self.donor.pk, "branch": self.branch.pk, "blood_group": "B+", "volume_pls": 250},
        )
        self.assertContains(resp, "No approved shelf-life rule exists for this component (Plasma).")
        self.assertEqual(Donation.objects.count(), 0)
        self.assertEqual(BloodUnit.objects.count(), 0)


class UniqueDonationAndUnitIdentifierTests(TestCase):
    """BB-16 [FR-09] & TC-F06: Unique donation and blood unit identifiers with derived components."""

    def setUp(self):
        self.branch = Branch.objects.create(name="ID Branch", code="IDB")
        for component, days in [("Packed Red Cells", 35), ("Platelets", 5), ("Plasma", 365)]:
            ShelfLifeRule.objects.create(component=component, days=days)
        self.staff = User.objects.create_user("idstaff@bbms.test", email="idstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="ID Donor",
            email="iddonor@example.test",
            phone="9000000103",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="O-",
            consent_given=True,
        )
        self.components = {"Packed Red Cells": 280, "Platelets": 50, "Plasma": 220}

    def test_tc_f06_one_donation_produces_several_traceable_component_units(self):
        donation, units = record_donation(self.donor, self.branch, self.staff, "O-", self.components)
        self.assertEqual(len(units), 3)
        self.assertEqual({u.component for u in units}, set(self.components))
        self.assertEqual(len({u.unit_id for u in units}), 3)
        for unit in BloodUnit.objects.select_related("donation__donor"):
            self.assertTrue(unit.unit_id.startswith(donation.donation_id))
            self.assertEqual(unit.donation, donation)
            self.assertEqual(unit.donation.donor, self.donor)
            self.assertEqual(unit.volume_ml, self.components[unit.component])

    def test_tc_f06_ids_are_unique_for_donations_in_the_same_instant(self):
        stamp = timezone.now()
        ids = set()
        for _ in range(20):
            donation, units = record_donation(self.donor, self.branch, self.staff, "O-", {"Plasma": 220}, collected_at=stamp)
            ids.add(donation.donation_id)
            ids.add(units[0].unit_id)
        self.assertEqual(len(ids), 40)

    def test_tc_f06_duplicate_unit_id_storage_fails(self):
        donation, units = record_donation(self.donor, self.branch, self.staff, "O-", {"Plasma": 220})
        from django.db import transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            BloodUnit.objects.create(
                unit_id=units[0].unit_id,
                donation=donation,
                blood_group="O-",
                component="Plasma",
                volume_ml=220,
                expiry_at=units[0].expiry_at,
                branch=self.branch,
            )

    def test_tc_f06_id_collision_is_retried(self):
        record_donation(self.donor, self.branch, self.staff, "O-", {"Plasma": 220})
        taken = Donation.objects.get().donation_id
        with patch("bloodbank.services.new_donation_id", side_effect=[taken, "DON-RETRY-OK"]):
            donation, units = record_donation(self.donor, self.branch, self.staff, "O-", {"Plasma": 220})
        self.assertEqual(donation.donation_id, "DON-RETRY-OK")
        self.assertEqual(Donation.objects.count(), 2)

    def test_tc_f06_missing_rule_for_any_component_stores_nothing(self):
        ShelfLifeRule.objects.filter(component="Platelets").delete()
        with self.assertRaises(ValueError):
            record_donation(self.donor, self.branch, self.staff, "O-", self.components)
        self.assertEqual(Donation.objects.count(), 0)
        self.assertEqual(BloodUnit.objects.count(), 0)

    def test_tc_f06_form_records_every_component_with_a_volume(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            "/donations/new/",
            {"donor": self.donor.pk, "branch": self.branch.pk, "blood_group": "O-", "volume_rbc": 280, "volume_pls": 220},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Donation.objects.count(), 1)
        self.assertEqual(set(BloodUnit.objects.values_list("component", flat=True)), {"Packed Red Cells", "Plasma"})


class UnitStateTransitionTests(TestCase):
    """BB-21 [FR-14] & TC-F08: Enforce valid blood unit state transitions."""

    def setUp(self):
        self.branch = Branch.objects.create(name="State Branch", code="STAT")
        self.staff = User.objects.create_user("statestaff@bbms.test", email="statestaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hospital = User.objects.create_user("statehosp@bbms.test", email="statehosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="State Hospital"
        )
        donor = Donor.objects.create(
            branch=self.branch,
            full_name="State Donor",
            email="statedonor@example.test",
            phone="9000000104",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="AB+",
            consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-STATE-1", donor=donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        self.counter = 0

    def make_unit(self, status=BloodUnit.Status.COLLECTED, expiry_days=20):
        self.counter += 1
        return BloodUnit.objects.create(
            unit_id=f"UNIT-STATE-{self.counter}",
            donation=self.donation,
            blood_group="AB+",
            component="Plasma",
            volume_ml=220,
            expiry_at=timezone.now() + timedelta(days=expiry_days),
            branch=self.branch,
            status=status,
        )

    def test_tc_f08_all_eight_states_exist(self):
        self.assertEqual(
            set(BloodUnit.Status.values),
            {"COLLECTED", "TESTING", "AVAILABLE", "RESERVED", "ISSUED", "QUARANTINED", "EXPIRED", "DISCARDED"},
        )
        self.assertEqual(set(UNIT_TRANSITIONS), set(BloodUnit.Status.values))

    def test_tc_f08_every_pair_is_checked_against_the_allowed_map(self):
        for current in BloodUnit.Status.values:
            for target in BloodUnit.Status.values:
                unit = self.make_unit(status=current)
                if target in UNIT_TRANSITIONS[current]:
                    transition_unit(unit, target, self.staff)
                    unit.refresh_from_db()
                    self.assertEqual(unit.status, target, f"{current} -> {target}")
                else:
                    with self.assertRaises(InvalidUnitTransition, msg=f"{current} -> {target}"):
                        transition_unit(unit, target, self.staff)
                    unit.refresh_from_db()
                    self.assertEqual(unit.status, current, f"{current} -> {target}")

    def test_tc_f08_invalid_collected_to_issued_is_rejected_and_logged(self):
        unit = self.make_unit()
        with self.assertRaisesMessage(InvalidUnitTransition, "cannot move from Collected to Issued"):
            transition_unit(unit, BloodUnit.Status.ISSUED, self.staff)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.COLLECTED)
        event = AuditEvent.objects.get(event_type="UNIT_TRANSITION_REJECTED")
        self.assertEqual((event.actor, event.record_id, event.outcome), (self.staff, str(unit.pk), "REJECTED"))
        self.assertIn("COLLECTED to ISSUED", event.summary)

    def test_every_successful_transition_is_audited(self):
        unit = self.make_unit()
        transition_unit(unit, BloodUnit.Status.TESTING, self.staff)
        transition_unit(unit, BloodUnit.Status.AVAILABLE, self.staff)
        summaries = list(
            AuditEvent.objects.filter(record_type="BloodUnit", record_id=str(unit.pk), outcome="SUCCESS")
            .order_by("id")
            .values_list("summary", flat=True)
        )
        self.assertEqual(summaries, ["COLLECTED to TESTING", "TESTING to AVAILABLE"])

    def test_reserve_and_issue_go_through_the_state_machine(self):
        unit = self.make_unit(status=BloodUnit.Status.AVAILABLE)
        req = BloodRequest.objects.create(
            hospital="State Hospital",
            created_by=self.hospital,
            branch=self.branch,
            blood_group="AB+",
            component="Plasma",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-STATE",
            status=BloodRequest.Status.APPROVED,
        )
        reservation = reserve_units(req, self.staff)[0]
        CrossMatch.objects.create(reservation=reservation, outcome="COMPATIBLE", recorded_by=self.staff)
        issue_reservation(reservation, self.staff)
        events = list(
            AuditEvent.objects.filter(record_type="BloodUnit", record_id=str(unit.pk))
            .order_by("id")
            .values_list("event_type", "summary")
        )
        self.assertEqual(events[0][0], "UNIT_RESERVED")
        self.assertTrue(events[0][1].startswith("AVAILABLE to RESERVED"))
        self.assertEqual(events[1][0], "UNIT_ISSUED")
        self.assertTrue(events[1][1].startswith("RESERVED to ISSUED"))

    def test_staff_discard_quarantined_and_expired_units_with_reason(self):
        quarantined = self.make_unit(status=BloodUnit.Status.QUARANTINED)
        expired = self.make_unit(status=BloodUnit.Status.EXPIRED)
        self.client.force_login(self.staff)
        for unit in (quarantined, expired):
            resp = self.client.post(f"/inventory/{unit.pk}/discard/", {"reason": "Reactive HIV result"})
            self.assertEqual(resp.status_code, 302)
            unit.refresh_from_db()
            self.assertEqual(unit.status, BloodUnit.Status.DISCARDED)
        self.assertEqual(AuditEvent.objects.filter(event_type="UNIT_DISCARDED").count(), 2)

    def test_discard_requires_reason_and_allowed_state(self):
        issued = self.make_unit(status=BloodUnit.Status.ISSUED)
        quarantined = self.make_unit(status=BloodUnit.Status.QUARANTINED)
        with self.assertRaisesMessage(ValueError, "A disposal reason is required."):
            discard_unit(quarantined, self.staff, "  ")
        with self.assertRaises(InvalidUnitTransition):
            discard_unit(issued, self.staff, "Damaged bag")
        issued.refresh_from_db()
        quarantined.refresh_from_db()
        self.assertEqual(issued.status, BloodUnit.Status.ISSUED)
        self.assertEqual(quarantined.status, BloodUnit.Status.QUARANTINED)

    def test_hospital_staff_cannot_discard(self):
        unit = self.make_unit(status=BloodUnit.Status.QUARANTINED)
        self.client.force_login(self.hospital)
        self.assertEqual(self.client.post(f"/inventory/{unit.pk}/discard/", {"reason": "x"}).status_code, 403)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.QUARANTINED)

    def test_expiry_task_moves_units_through_the_state_machine(self):
        unit = self.make_unit(status=BloodUnit.Status.AVAILABLE, expiry_days=-1)
        self.assertEqual(expire_units(), 1)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.EXPIRED)
        self.assertTrue(
            AuditEvent.objects.filter(event_type="UNIT_EXPIRED", record_id=str(unit.pk), actor=None).exists()
        )


class ScreeningFixtureMixin:
    """Shared screening fixtures: a five-test mandatory panel, one optional test and a unit factory."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Lab Branch", code="LAB")
        self.staff = User.objects.create_user("labstaff@bbms.test", email="labstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        donor = Donor.objects.create(
            branch=self.branch,
            full_name="Lab Donor",
            email="labdonor@example.test",
            phone="9000000105",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="O+",
            consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-LAB-1", donor=donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        self.panel = [TestType.objects.create(name=n, required=True) for n in ["HIV", "HBV", "HCV", "Syphilis", "Malaria"]]
        self.optional = TestType.objects.create(name="CMV", required=False)
        self.counter = 0

    def make_unit(self, status=BloodUnit.Status.COLLECTED):
        self.counter += 1
        return BloodUnit.objects.create(
            unit_id=f"UNIT-LAB-{self.counter}",
            donation=self.donation,
            blood_group="O+",
            component="Packed Red Cells",
            volume_ml=280,
            expiry_at=timezone.now() + timedelta(days=30),
            branch=self.branch,
            status=status,
        )


class LabResultRecordingTests(ScreeningFixtureMixin, TestCase):
    """BB-18 [FR-11] & TC-F07: Store results for the mandatory infection test panel."""

    def test_tc_f07_result_records_outcome_staff_and_time(self):
        unit = self.make_unit()
        before = timezone.now()
        result = record_lab_result(unit, self.panel[0], LabResult.Outcome.ACCEPTABLE, self.staff)
        result.refresh_from_db()
        self.assertEqual(result.outcome, LabResult.Outcome.ACCEPTABLE)
        self.assertEqual(result.recorded_by, self.staff)
        self.assertGreaterEqual(result.recorded_at, before)
        self.assertTrue(AuditEvent.objects.filter(event_type="LAB_RESULT_RECORDED", record_id=str(unit.pk)).exists())

    def test_tc_f07_first_result_moves_unit_from_collected_to_testing(self):
        unit = self.make_unit()
        record_lab_result(unit, self.panel[0], LabResult.Outcome.ACCEPTABLE, self.staff)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.TESTING)

    def test_tc_f07_one_result_per_test_type_per_unit(self):
        unit = self.make_unit()
        record_lab_result(unit, self.panel[0], LabResult.Outcome.ACCEPTABLE, self.staff)
        with self.assertRaisesMessage(DuplicateLabResult, "A result for this test type is already recorded."):
            record_lab_result(unit, self.panel[0], LabResult.Outcome.REACTIVE, self.staff)
        self.assertEqual(LabResult.objects.filter(unit=unit, test_type=self.panel[0]).count(), 1)
        self.assertEqual(LabResult.objects.get(unit=unit, test_type=self.panel[0]).outcome, LabResult.Outcome.ACCEPTABLE)

    def test_tc_f07_duplicate_rejected_in_the_screening_form(self):
        unit = self.make_unit()
        record_lab_result(unit, self.panel[0], LabResult.Outcome.ACCEPTABLE, self.staff)
        self.client.force_login(self.staff)
        resp = self.client.post(f"/inventory/{unit.pk}/screen/", {"test_type": self.panel[0].pk, "outcome": "REACTIVE"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "A result for this test type is already recorded.")

    def test_tc_f07_panel_is_the_configured_required_test_types(self):
        unit = self.make_unit()
        self.client.force_login(self.staff)
        page = self.client.get(f"/inventory/{unit.pk}/screen/")
        self.assertEqual([t.name for t in page.context["panel"]], ["HBV", "HCV", "HIV", "Malaria", "Syphilis"])
        self.assertNotIn(self.optional, page.context["panel"])

    def test_results_cannot_be_added_once_unit_left_screening(self):
        for status in [BloodUnit.Status.AVAILABLE, BloodUnit.Status.QUARANTINED, BloodUnit.Status.ISSUED]:
            unit = self.make_unit(status=status)
            with self.assertRaisesMessage(ValueError, "Results can only be recorded while the unit is Collected or Testing."):
                record_lab_result(unit, self.panel[0], LabResult.Outcome.ACCEPTABLE, self.staff)
            self.assertFalse(LabResult.objects.filter(unit=unit).exists())


class ReleaseAfterAcceptablePanelTests(ScreeningFixtureMixin, TestCase):
    """BB-19 [FR-12] & TC-F07: Release a unit to Available only after all mandatory tests are acceptable."""

    def record(self, unit, tests, outcome=LabResult.Outcome.ACCEPTABLE):
        for test in tests:
            record_lab_result(unit, test, outcome, self.staff)
            unit.refresh_from_db()

    def test_tc_f07_complete_acceptable_panel_becomes_available(self):
        unit = self.make_unit()
        self.record(unit, self.panel[:-1])
        self.assertEqual(unit.status, BloodUnit.Status.TESTING)
        self.record(unit, self.panel[-1:])
        self.assertEqual(unit.status, BloodUnit.Status.AVAILABLE)

    def test_tc_f07_partial_panel_stays_out_of_available(self):
        unit = self.make_unit()
        self.record(unit, self.panel[:3])
        self.assertEqual(unit.status, BloodUnit.Status.TESTING)
        self.assertEqual(release_screened_unit(unit, self.staff), BloodUnit.Status.QUARANTINED)
        event = AuditEvent.objects.filter(event_type="SCREENING_REVIEWED", record_id=str(unit.pk)).latest("id")
        self.assertIn("mandatory panel incomplete", event.summary)

    def test_tc_f07_mixed_panel_never_becomes_available(self):
        unit = self.make_unit()
        self.record(unit, self.panel[:2])
        self.record(unit, self.panel[2:3], LabResult.Outcome.REACTIVE)
        self.assertEqual(unit.status, BloodUnit.Status.QUARANTINED)
        with self.assertRaises(ValueError):
            record_lab_result(unit, self.panel[3], LabResult.Outcome.ACCEPTABLE, self.staff)
        with self.assertRaises(ValueError):
            release_screened_unit(unit, self.staff)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.QUARANTINED)

    def test_optional_tests_do_not_block_release(self):
        unit = self.make_unit()
        self.record(unit, self.panel)
        self.assertEqual(unit.status, BloodUnit.Status.AVAILABLE)
        self.assertFalse(LabResult.objects.filter(unit=unit, test_type=self.optional).exists())

    def test_release_refused_when_no_panel_is_configured(self):
        TestType.objects.all().delete()
        unit = self.make_unit()
        with self.assertRaisesMessage(ValueError, "No mandatory test panel is configured"):
            release_screened_unit(unit, self.staff)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.COLLECTED)

    def test_release_button_moves_complete_panel_through_the_view(self):
        unit = self.make_unit()
        self.client.force_login(self.staff)
        for test in self.panel:
            resp = self.client.post(f"/inventory/{unit.pk}/screen/", {"test_type": test.pk, "outcome": "ACCEPTABLE"})
            self.assertEqual(resp.status_code, 302)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.AVAILABLE)

        partial = self.make_unit()
        self.client.post(f"/inventory/{partial.pk}/screen/", {"test_type": self.panel[0].pk, "outcome": "ACCEPTABLE"})
        self.assertEqual(self.client.post(f"/inventory/{partial.pk}/release/").status_code, 302)
        partial.refresh_from_db()
        self.assertEqual(partial.status, BloodUnit.Status.QUARANTINED)

    def test_only_blood_bank_staff_can_record_and_release(self):
        unit = self.make_unit()
        hospital = User.objects.create_user("labhosp@bbms.test", email="labhosp@bbms.test", password="pw")
        UserProfile.objects.create(user=hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="H")
        donor_user = User.objects.create_user("labdonoruser@bbms.test", email="labdonoruser@bbms.test", password="pw")
        UserProfile.objects.create(user=donor_user, role=UserProfile.Role.DONOR, branch=self.branch)
        for user in (hospital, donor_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(f"/inventory/{unit.pk}/screen/").status_code, 403)
            self.assertEqual(
                self.client.post(f"/inventory/{unit.pk}/screen/", {"test_type": self.panel[0].pk, "outcome": "ACCEPTABLE"}).status_code,
                403,
            )
            self.assertEqual(self.client.post(f"/inventory/{unit.pk}/release/").status_code, 403)
        self.assertFalse(LabResult.objects.filter(unit=unit).exists())
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.COLLECTED)


class QuarantineScreeningTests(ScreeningFixtureMixin, TestCase):
    """BB-20 [FR-13] & TC-F07: Quarantine units with reactive, indeterminate or incomplete results."""

    def test_tc_f07_each_non_acceptable_outcome_quarantines(self):
        for bad in [LabResult.Outcome.REACTIVE, LabResult.Outcome.INDETERMINATE, LabResult.Outcome.INCOMPLETE]:
            unit = self.make_unit()
            record_lab_result(unit, self.panel[0], LabResult.Outcome.ACCEPTABLE, self.staff)
            record_lab_result(unit, self.panel[1], bad, self.staff)
            unit.refresh_from_db()
            self.assertEqual(unit.status, BloodUnit.Status.QUARANTINED, bad)

    def test_tc_f07_bad_last_result_of_a_complete_panel_quarantines(self):
        for bad in [LabResult.Outcome.REACTIVE, LabResult.Outcome.INDETERMINATE, LabResult.Outcome.INCOMPLETE]:
            unit = self.make_unit()
            for test in self.panel[:-1]:
                record_lab_result(unit, test, LabResult.Outcome.ACCEPTABLE, self.staff)
            record_lab_result(unit, self.panel[-1], bad, self.staff)
            unit.refresh_from_db()
            self.assertEqual(unit.status, BloodUnit.Status.QUARANTINED, bad)

    def test_tc_f07_missing_mandatory_result_at_release_quarantines(self):
        unit = self.make_unit()
        for test in self.panel[:-1]:
            record_lab_result(unit, test, LabResult.Outcome.ACCEPTABLE, self.staff)
        self.assertEqual(release_screened_unit(unit, self.staff), BloodUnit.Status.QUARANTINED)

    def test_quarantined_units_never_appear_in_available_stock(self):
        hospital = User.objects.create_user("qhosp@bbms.test", email="qhosp@bbms.test", password="pw")
        UserProfile.objects.create(user=hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="QH")
        unit = self.make_unit()
        record_lab_result(unit, self.panel[0], LabResult.Outcome.REACTIVE, self.staff)
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.QUARANTINED)

        self.client.force_login(hospital)
        for path in ["/", "/inventory/", "/api/v1/inventory/", "/reports/"]:
            self.assertNotContains(self.client.get(path), unit.unit_id, msg_prefix=path)

        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/").context["available"], 0)

        req = BloodRequest.objects.create(
            hospital="QH",
            created_by=hospital,
            branch=self.branch,
            blood_group="O+",
            component="Packed Red Cells",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-QUAR",
            status=BloodRequest.Status.APPROVED,
        )
        with self.assertRaisesMessage(ValueError, "No compatible available units are available."):
            reserve_units(req, self.staff)
        with self.assertRaises(InvalidUnitTransition):
            transition_unit(unit, BloodUnit.Status.AVAILABLE, self.staff)


class ScheduledUnitExpiryTests(TestCase):
    """BB-23 [FR-16] & TC-F08: Automatically mark unissued units Expired."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Expire Branch", code="EXPR")
        self.staff = User.objects.create_user("expstaff@bbms.test", email="expstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hospital = User.objects.create_user("exphosp@bbms.test", email="exphosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Expire Hospital"
        )
        donor = Donor.objects.create(
            branch=self.branch,
            full_name="Expire Donor",
            email="expdonor@example.test",
            phone="9000000106",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="A-",
            consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-EXP-1", donor=donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )

    def make_unit(self, unit_id, expiry_at, status=BloodUnit.Status.AVAILABLE):
        return BloodUnit.objects.create(
            unit_id=unit_id,
            donation=self.donation,
            blood_group="A-",
            component="Platelets",
            volume_ml=50,
            expiry_at=expiry_at,
            branch=self.branch,
            status=status,
        )

    def make_request(self, ref):
        return BloodRequest.objects.create(
            hospital="Expire Hospital",
            created_by=self.hospital,
            branch=self.branch,
            blood_group="A-",
            component="Platelets",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference=ref,
            status=BloodRequest.Status.APPROVED,
        )

    def test_tc_f08_command_expires_only_units_past_expiry(self):
        just_before = self.make_unit("UNIT-JUST-BEFORE", timezone.now() + timedelta(minutes=5))
        after = self.make_unit("UNIT-AFTER", timezone.now() - timedelta(minutes=1))
        quarantined = self.make_unit("UNIT-OLD-QUAR", timezone.now() - timedelta(days=1), BloodUnit.Status.QUARANTINED)
        issued = self.make_unit("UNIT-OLD-ISSUED", timezone.now() - timedelta(days=1), BloodUnit.Status.ISSUED)
        out = StringIO()
        call_command("expire_units", stdout=out)
        self.assertIn("1 unit(s) marked Expired", out.getvalue())
        for unit, status in [
            (just_before, BloodUnit.Status.AVAILABLE),
            (after, BloodUnit.Status.EXPIRED),
            (quarantined, BloodUnit.Status.QUARANTINED),
            (issued, BloodUnit.Status.ISSUED),
        ]:
            unit.refresh_from_db()
            self.assertEqual(unit.status, status, unit.unit_id)

        out = StringIO()
        call_command("expire_units", stdout=out)
        self.assertIn("0 unit(s) marked Expired", out.getvalue())

    def test_command_runs_without_any_page_load(self):
        unit = self.make_unit("UNIT-NO-PAGE", timezone.now() - timedelta(minutes=1))
        call_command("expire_units", stdout=StringIO())
        unit.refresh_from_db()
        self.assertEqual(unit.status, BloodUnit.Status.EXPIRED)
        self.assertTrue(AuditEvent.objects.filter(event_type="UNIT_EXPIRED", record_id=str(unit.pk), actor=None).exists())

    def test_tc_f08_expired_reserved_unit_releases_its_reservation(self):
        unit = self.make_unit("UNIT-RES-EXP", timezone.now() + timedelta(hours=1))
        req = self.make_request("REF-EXP-RES")
        reservation = reserve_units(req, self.staff)[0]
        BloodUnit.objects.filter(pk=unit.pk).update(expiry_at=timezone.now() - timedelta(minutes=1))
        call_command("expire_units", stdout=StringIO())
        reservation.refresh_from_db()
        unit.refresh_from_db()
        self.assertFalse(reservation.active)
        self.assertEqual(unit.status, BloodUnit.Status.EXPIRED)
        self.assertTrue(AuditEvent.objects.filter(event_type="RESERVATION_RELEASED", record_id=str(unit.pk)).exists())

        # The request is free to reserve a replacement unit.
        replacement = self.make_unit("UNIT-REPLACEMENT", timezone.now() + timedelta(days=3))
        self.assertEqual(reserve_units(req, self.staff)[0].unit, replacement)

    def test_tc_f08_expired_units_excluded_from_search_and_reservation(self):
        self.make_unit("UNIT-STALE", timezone.now() - timedelta(minutes=1))
        call_command("expire_units", stdout=StringIO())
        self.client.force_login(self.hospital)
        self.assertNotContains(self.client.get("/inventory/"), "UNIT-STALE")
        self.assertEqual(self.client.get("/api/v1/inventory/").json()["items"], [])
        with self.assertRaisesMessage(ValueError, "No compatible available units are available."):
            reserve_units(self.make_request("REF-STALE"), self.staff)

    def test_scheduler_service_runs_the_command_on_a_schedule(self):
        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
        self.assertIn("scheduler:", compose)
        self.assertIn("python manage.py expire_units --every 300", compose)


class AuditRecordImmutabilityTests(TestCase):
    """BB-56 [SEC-07] & TC-S03: Complete, tamper-resistant audit records."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Audit Branch", code="AUD")
        self.staff = User.objects.create_user("auditstaff@bbms.test", email="auditstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.superuser = User.objects.create_superuser("root@bbms.test", email="root@bbms.test", password="pw")
        UserProfile.objects.create(user=self.superuser, role=UserProfile.Role.ADMIN, branch=self.branch)
        self.hospital = User.objects.create_user("audithosp@bbms.test", email="audithosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Audit Hospital"
        )
        donor = Donor.objects.create(
            branch=self.branch,
            full_name="Audit Donor",
            email="auditdonor@example.test",
            phone="9000000107",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="B-",
            consent_given=True,
        )
        donation = Donation.objects.create(
            donation_id="DON-AUD-1", donor=donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        self.unit = BloodUnit.objects.create(
            unit_id="UNIT-AUD-1",
            donation=donation,
            blood_group="B-",
            component="Plasma",
            volume_ml=220,
            expiry_at=timezone.now() + timedelta(days=30),
            branch=self.branch,
        )
        TestType.objects.create(name="HIV", required=True)

    def test_tc_s03_operational_events_carry_all_five_fields(self):
        record_lab_result(self.unit, TestType.objects.get(), LabResult.Outcome.ACCEPTABLE, self.staff)
        self.client.force_login(self.hospital)
        self.client.get(f"/inventory/{self.unit.pk}/screen/")  # rejected authorization attempt
        events = AuditEvent.objects.all()
        self.assertGreaterEqual(events.count(), 3)
        for event in events:
            self.assertIsNotNone(event.actor, event.event_type)
            self.assertTrue(event.event_type)
            self.assertTrue(event.record_id)
            self.assertTrue(event.outcome)
            self.assertIsNotNone(event.created_at)
            self.assertEqual(event.created_at.utcoffset(), timedelta(0))
        self.assertTrue(events.filter(event_type="UNAUTHORIZED_ACCESS", outcome="DENIED", actor=self.hospital).exists())

    def test_tc_s03_events_cannot_be_changed_or_deleted_through_the_model(self):
        audit(self.staff, "TEST_EVENT", self.unit)
        event = AuditEvent.objects.get(event_type="TEST_EVENT")
        event.outcome = "FAILED"
        with self.assertRaises(ImmutableAuditRecord):
            event.save()
        with self.assertRaises(ImmutableAuditRecord):
            event.delete()
        with self.assertRaises(ImmutableAuditRecord):
            AuditEvent.objects.filter(pk=event.pk).update(outcome="FAILED")
        with self.assertRaises(ImmutableAuditRecord):
            AuditEvent.objects.filter(pk=event.pk).delete()
        event.refresh_from_db()
        self.assertEqual(event.outcome, "SUCCESS")

    def test_incomplete_event_is_refused(self):
        with self.assertRaises(ValueError):
            AuditEvent.objects.create(actor=self.staff, event_type="", record_type="X", record_id="1")

    def test_deleting_a_user_keeps_their_audit_history(self):
        temp = User.objects.create_user("temp@bbms.test", password="pw")
        audit(temp, "TEMP_EVENT", self.unit)
        temp.delete()
        event = AuditEvent.objects.get(event_type="TEMP_EVENT")
        self.assertIsNone(event.actor)

    def test_tc_s03_django_admin_is_view_only_even_for_superusers(self):
        audit(self.staff, "TEST_EVENT", self.unit)
        event = AuditEvent.objects.get(event_type="TEST_EVENT")
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(f"/admin/bloodbank/auditevent/{event.pk}/change/").status_code, 200)
        self.client.post(f"/admin/bloodbank/auditevent/{event.pk}/change/", {"outcome": "FAILED", "summary": "edited"})
        self.assertEqual(self.client.get("/admin/bloodbank/auditevent/add/").status_code, 403)
        self.assertEqual(self.client.post(f"/admin/bloodbank/auditevent/{event.pk}/delete/", {"post": "yes"}).status_code, 403)
        event.refresh_from_db()
        self.assertEqual((event.outcome, event.summary), ("SUCCESS", ""))
        self.assertTrue(AuditEvent.objects.filter(pk=event.pk).exists())

    def test_tc_s03_normal_roles_cannot_edit_audit_events(self):
        audit(self.staff, "TEST_EVENT", self.unit)
        event = AuditEvent.objects.get(event_type="TEST_EVENT")
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post("/audit/", {"outcome": "FAILED"}).status_code, 405)
        self.assertNotEqual(self.client.get(f"/admin/bloodbank/auditevent/{event.pk}/change/").status_code, 200)
        self.assertNotEqual(self.client.post(f"/admin/bloodbank/auditevent/{event.pk}/delete/", {"post": "yes"}).status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.outcome, "SUCCESS")

    def test_audit_page_shows_utc_time(self):
        audit(self.staff, "TEST_EVENT", self.unit)
        event = AuditEvent.objects.get(event_type="TEST_EVENT")
        self.client.force_login(self.staff)
        self.assertContains(self.client.get("/audit/"), f"{event.created_at:%Y-%m-%d %H:%M:%S} UTC")


class RolePermissionMatrixTests(TestCase):
    """BB-50 [SEC-01] & TC-S02: Server-side authorization on every protected action (SRS 6.3, Table 4)."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Matrix Branch", code="MTX")
        self.other_branch = Branch.objects.create(name="Other Branch", code="OTH")
        ShelfLifeRule.objects.create(component="Plasma", days=365)
        TestType.objects.create(name="HIV", required=True)
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)

        def make_user(name, role, branch=None, **profile):
            user = User.objects.create_user(f"{name}@bbms.test", email=f"{name}@bbms.test", password="pw")
            UserProfile.objects.create(user=user, role=role, branch=branch or self.branch, **profile)
            return user

        self.users = {
            "DONOR": make_user("mx_donor", UserProfile.Role.DONOR),
            "STAFF": make_user("mx_staff", UserProfile.Role.STAFF),
            "HOSPITAL": make_user("mx_hosp", UserProfile.Role.HOSPITAL, hospital_name="Matrix Hospital"),
            "ADMIN": make_user("mx_admin", UserProfile.Role.ADMIN),
        }
        self.other_staff = make_user("mx_other_staff", UserProfile.Role.STAFF, branch=self.other_branch)
        self.donor_profile = self.users["DONOR"].profile
        staff, hospital = self.users["STAFF"], self.users["HOSPITAL"]

        self.donor = Donor.objects.create(
            user=self.users["DONOR"],
            branch=self.branch,
            full_name="Matrix Donor",
            email="mx_donor@bbms.test",
            phone="9000000108",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="O+",
            consent_given=True,
        )
        slot = AppointmentSlot.objects.create(branch=self.branch, starts_at=timezone.now() + timedelta(days=2), capacity=5)
        self.appointment = Appointment.objects.create(donor=self.donor, slot=slot)
        donation = Donation.objects.create(
            donation_id="DON-MTX-1", donor=self.donor, collected_at=timezone.now(), collector=staff, branch=self.branch
        )

        def unit(unit_id, status):
            return BloodUnit.objects.create(
                unit_id=unit_id,
                donation=donation,
                blood_group="O+",
                component="Plasma",
                volume_ml=220,
                expiry_at=timezone.now() + timedelta(days=30),
                branch=self.branch,
                status=status,
            )

        self.collected = unit("UNIT-MTX-COLL", BloodUnit.Status.COLLECTED)
        self.quarantined = unit("UNIT-MTX-QUAR", BloodUnit.Status.QUARANTINED)
        unit("UNIT-MTX-RES", BloodUnit.Status.AVAILABLE)
        unit("UNIT-MTX-SPARE", BloodUnit.Status.AVAILABLE)

        def request(ref, status):
            return BloodRequest.objects.create(
                hospital="Matrix Hospital",
                created_by=hospital,
                branch=self.branch,
                blood_group="O+",
                component="Plasma",
                quantity=1,
                priority="Urgent",
                required_by=timezone.now() + timedelta(days=1),
                clinical_reference=ref,
                status=status,
            )

        self.pending = request("REF-MTX-PEND", BloodRequest.Status.PENDING)
        reserved_req = request("REF-MTX-RES", BloodRequest.Status.APPROVED)
        self.reservation = reserve_units(reserved_req, staff)[0]
        self.approved = request("REF-MTX-APPR", BloodRequest.Status.APPROVED)

    def matrix(self):
        """(method, path, data, roles allowed by SRS Table 4)."""
        S, H, D, A = "STAFF", "HOSPITAL", "DONOR", "ADMIN"
        new_request = json.dumps({
            "branch": self.branch.pk,
            "blood_group": "O+",
            "component": "Plasma",
            "quantity": 1,
            "priority": "Urgent",
            "required_by": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "clinical_reference": "REF-MTX-API",
        })
        return [
            ("get", "/", None, {S, H, D, A}),
            ("get", "/my-record/", None, {D}),
            ("get", f"/appointments/?donor={self.donor.pk}", None, {D, S}),
            ("post", f"/appointments/{self.appointment.pk}/cancel/", {}, {D, S}),
            ("get", "/donors/", None, {S}),
            ("get", "/deferrals/", None, {S}),
            ("get", "/donations/new/", None, {S}),
            ("get", f"/inventory/{self.collected.pk}/screen/", None, {S}),
            ("post", f"/inventory/{self.collected.pk}/release/", {}, {S}),
            ("post", f"/inventory/{self.quarantined.pk}/discard/", {"reason": "Reactive"}, {S}),
            ("get", "/inventory/", None, {S, H, A}),
            ("get", "/api/v1/inventory/", None, {S, H, A}),
            ("get", "/requests/", None, {S, H}),
            ("post", f"/requests/{self.pending.pk}/approve/", {}, {S}),
            ("post", f"/requests/{self.pending.pk}/reject/", {}, {S}),
            ("post", f"/requests/{self.pending.pk}/cancel/", {}, {S, H}),
            ("post", f"/requests/{self.approved.pk}/reserve/", {}, {S}),
            ("get", f"/reservations/{self.reservation.pk}/crossmatch/", None, {S}),
            ("post", f"/reservations/{self.reservation.pk}/issue/", {}, {S}),
            ("post", f"/reservations/{self.reservation.pk}/release/", {}, {S}),
            ("get", "/audit/", None, {S, A}),
            ("get", "/accounts/", None, {A}),
            ("get", "/configuration/", None, {A}),
            ("post", "/configuration/branch/", {"pk": self.branch.pk}, {A}),
            ("post", f"/accounts/{self.donor_profile.pk}/status/", {"active": "0"}, {A}),
            ("post", f"/accounts/{self.donor_profile.pk}/role/", {"role": "DONOR", "branch": ""}, {A}),
            ("get", "/reports/", None, {S, H, A}),
            ("post_json", "/api/v1/requests", new_request, {H}),
            ("patch_json", f"/api/v1/requests/{self.pending.pk}/status", json.dumps({"status": "APPROVED"}), {S}),
            ("post_json", f"/api/v1/requests/{self.approved.pk}/reserve", "{}", {S}),
            ("post_json", "/api/v1/issues", json.dumps({"reservation_id": self.reservation.pk}), {S}),
        ]

    def call(self, method, path, data):
        if method == "get":
            return self.client.get(path)
        if method == "post":
            return self.client.post(path, data)
        if method == "post_json":
            return self.client.post(path, data, content_type="application/json")
        return self.client.patch(path, data, content_type="application/json")

    def test_tc_s02_every_role_against_every_protected_url(self):
        from django.db import transaction
        for role_name, user in self.users.items():
            self.client.force_login(user)  # outside the rolled-back block so the session row survives
            for method, path, data, allowed_roles in self.matrix():
                with self.subTest(role=role_name, path=path, method=method), transaction.atomic():
                    denials_before = AuditEvent.objects.filter(actor=user, event_type="UNAUTHORIZED_ACCESS").count()
                    resp = self.call(method, path, data)
                    denials = AuditEvent.objects.filter(actor=user, event_type="UNAUTHORIZED_ACCESS").count() - denials_before
                    if role_name in allowed_roles:
                        self.assertNotEqual(resp.status_code, 403)
                        self.assertNotIn("/login/", resp.get("Location", ""))
                        self.assertEqual(denials, 0)
                    else:
                        self.assertEqual(resp.status_code, 403)
                        self.assertEqual(denials, 1, "denied attempt must be audited")
                    transaction.set_rollback(True)

    def test_tc_s02_anonymous_requests_are_sent_to_login(self):
        for method, path, data, _ in self.matrix():
            with self.subTest(path=path, method=method):
                resp = self.call(method, path, data)
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login/", resp["Location"])

    def test_tc_s02_out_of_scope_branch_actions_are_denied_and_logged(self):
        self.client.force_login(self.other_staff)
        for method, path, data in [
            ("post", f"/requests/{self.pending.pk}/approve/", {}),
            ("post", f"/inventory/{self.quarantined.pk}/discard/", {"reason": "x"}),
            ("post", f"/inventory/{self.collected.pk}/release/", {}),
            ("post", f"/reservations/{self.reservation.pk}/issue/", {}),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, data).status_code, 403)
        self.assertEqual(
            AuditEvent.objects.filter(actor=self.other_staff, event_type="UNAUTHORIZED_ACCESS", outcome="DENIED").count(), 4
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, BloodRequest.Status.PENDING)

    def test_superuser_flag_does_not_bypass_the_role_matrix(self):
        self.users["ADMIN"].is_superuser = True
        self.users["ADMIN"].is_staff = True
        self.users["ADMIN"].save()
        self.client.force_login(self.users["ADMIN"])
        self.assertEqual(self.client.post(f"/requests/{self.pending.pk}/approve/").status_code, 403)
        self.assertEqual(self.client.get("/donors/").status_code, 403)
        self.assertEqual(self.client.get("/audit/").status_code, 200)

    def test_deactivated_account_is_denied_everywhere(self):
        staff = self.users["STAFF"]
        staff.profile.active = False
        staff.profile.save()
        self.client.force_login(staff)
        # SEC-04: a deactivated account is signed out on its next request instead of seeing 403s.
        self.assertRedirects(self.client.get("/inventory/"), "/login/", fetch_redirect_response=False)
        self.client.force_login(staff)
        self.assertRedirects(
            self.client.post(f"/requests/{self.pending.pk}/approve/"), "/login/", fetch_redirect_response=False
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, BloodRequest.Status.PENDING)


class BranchTimezoneDisplayTests(TestCase):
    """BB-47 [NFR-10] & TC-N07: Store UTC and display branch local time."""

    def setUp(self):
        self.kolkata = Branch.objects.create(name="Kolkata Branch", code="KOL", timezone_name="Asia/Kolkata")
        self.utc_branch = Branch.objects.create(name="UTC Branch", code="UTCB", timezone_name="UTC")
        self.staff_kol = User.objects.create_user("tzstaff@bbms.test", email="tzstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff_kol, role=UserProfile.Role.STAFF, branch=self.kolkata)
        self.staff_utc = User.objects.create_user("tzutc@bbms.test", email="tzutc@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff_utc, role=UserProfile.Role.STAFF, branch=self.utc_branch)
        self.hosp_kol = User.objects.create_user("tzhosp@bbms.test", email="tzhosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hosp_kol, role=UserProfile.Role.HOSPITAL, branch=self.kolkata, hospital_name="Kolkata Hospital"
        )
        donor = Donor.objects.create(
            branch=self.kolkata,
            full_name="TZ Donor",
            email="tzdonor@example.test",
            phone="9000000109",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="A+",
            consent_given=True,
        )
        self.expiry = datetime(2030, 1, 1, 0, 0, tzinfo=dt_timezone.utc)
        for branch, unit_id in [(self.kolkata, "UNIT-TZ-KOL"), (self.utc_branch, "UNIT-TZ-UTC")]:
            donation = Donation.objects.create(
                donation_id=f"DON-{unit_id}", donor=donor, collected_at=timezone.now(), collector=self.staff_kol, branch=branch
            )
            BloodUnit.objects.create(
                unit_id=unit_id,
                donation=donation,
                blood_group="A+",
                component="Plasma",
                volume_ml=220,
                expiry_at=self.expiry,
                branch=branch,
                status=BloodUnit.Status.AVAILABLE,
            )

    def test_tc_n07_settings_store_utc(self):
        from django.conf import settings
        self.assertTrue(settings.USE_TZ)
        self.assertEqual(settings.TIME_ZONE, "UTC")
        self.assertEqual(BloodUnit.objects.get(unit_id="UNIT-TZ-KOL").expiry_at, self.expiry)

    def test_tc_n07_same_instant_shown_in_each_branch_time_zone(self):
        self.client.force_login(self.staff_kol)
        kol_page = self.client.get("/inventory/")
        self.assertContains(kol_page, "Jan. 1, 2030, 5:30 a.m.")
        self.assertContains(kol_page, "Times shown in Asia/Kolkata")

        self.client.force_login(self.staff_utc)
        utc_page = self.client.get("/inventory/")
        self.assertContains(utc_page, "Jan. 1, 2030, midnight")
        self.assertContains(utc_page, "Times shown in UTC")

    def test_tc_n07_branch_local_form_input_is_stored_in_utc(self):
        self.client.force_login(self.hosp_kol)
        resp = self.client.post(
            "/requests/",
            {
                "branch": self.kolkata.pk,
                "blood_group": "A+",
                "component": "Plasma",
                "quantity": 1,
                "priority": "Urgent",
                "required_by": "2030-01-01 10:00",
                "clinical_reference": "REF-TZ",
            },
        )
        self.assertEqual(resp.status_code, 302)
        stored = BloodRequest.objects.get(clinical_reference="REF-TZ").required_by
        self.assertEqual(stored, datetime(2030, 1, 1, 4, 30, tzinfo=dt_timezone.utc))

    def test_unknown_or_missing_time_zone_falls_back_to_utc(self):
        self.kolkata.timezone_name = "Not/AZone"
        self.kolkata.save()
        self.client.force_login(self.staff_kol)
        self.assertContains(self.client.get("/inventory/"), "Jan. 1, 2030, midnight")

        admin = User.objects.create_user("tzadmin@bbms.test", email="tzadmin@bbms.test", password="pw")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        self.assertContains(self.client.get("/inventory/"), "Times shown in UTC")

    def test_time_zone_is_reset_after_each_request(self):
        self.client.force_login(self.staff_kol)
        self.client.get("/inventory/")
        self.assertEqual(timezone.get_current_timezone_name(), "UTC")


class EligibilityRuleUnitTests(TestCase):
    """BB-45 [NFR-08]: unit tests for the eligibility service (FR-03, FR-04)."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Eligibility Branch", code="ELIG")
        self.staff = User.objects.create_user("eligstaff@bbms.test", email="eligstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.rule = EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        self.donor = Donor.objects.create(
            branch=self.branch,
            full_name="Eligible Donor",
            email="eligible@example.test",
            phone="9000000110",
            date_of_birth=self.birthday(30),
            weight_kg=60,
            blood_group="O+",
            consent_given=True,
        )

    @staticmethod
    def birthday(age, days_offset=0):
        today = timezone.localdate()
        try:
            dob = today.replace(year=today.year - age)
        except ValueError:  # today is 29 February
            dob = today.replace(year=today.year - age, day=28)
        return dob + timedelta(days=days_offset)

    def assert_blocked(self, fragment):
        ok, reasons = eligibility(self.donor)
        self.assertFalse(ok)
        self.assertTrue(any(fragment in r for r in reasons), reasons)

    def test_eligible_donor_passes(self):
        self.assertEqual(eligibility(self.donor), (True, []))

    def test_age_boundaries(self):
        self.donor.date_of_birth = self.birthday(18)
        self.assertTrue(eligibility(self.donor)[0])
        self.donor.date_of_birth = self.birthday(18, days_offset=1)
        self.assert_blocked("Age must be between 18 and 65")
        self.donor.date_of_birth = self.birthday(66)
        self.assert_blocked("Age must be between 18 and 65")

    def test_minimum_weight(self):
        self.donor.weight_kg = 50
        self.assertTrue(eligibility(self.donor)[0])
        self.donor.weight_kg = 49.9
        self.assert_blocked("Weight must be at least 50")

    def test_minimum_interval_since_last_donation(self):
        Donation.objects.create(
            donation_id="DON-ELIG-1",
            donor=self.donor,
            collected_at=timezone.now() - timedelta(days=30),
            collector=self.staff,
            branch=self.branch,
        )
        self.assert_blocked("Minimum donation interval has not elapsed.")
        Donation.objects.filter(donation_id="DON-ELIG-1").update(collected_at=timezone.now() - timedelta(days=91))
        self.assertTrue(eligibility(self.donor)[0])

    def test_consent_required(self):
        self.donor.consent_given = False
        self.assert_blocked("Donor consent is required.")

    def test_deferrals(self):
        today = timezone.localdate()
        expired = Deferral.objects.create(
            donor=self.donor, reason="Cold", start_date=today - timedelta(days=20), end_date=today - timedelta(days=1),
            approved_by=self.staff,
        )
        self.assertTrue(eligibility(self.donor)[0])
        expired.end_date = today + timedelta(days=5)
        expired.save()
        self.assert_blocked("An active donor deferral applies.")
        expired.delete()
        Deferral.objects.create(donor=self.donor, reason="Hepatitis history", permanent=True, approved_by=self.staff)
        self.assert_blocked("An active donor deferral applies.")

    def test_rules_come_from_configuration(self):
        self.rule.min_weight_kg = 65
        self.rule.save()
        self.assert_blocked("Weight must be at least 65")

    def test_missing_configuration_blocks(self):
        EligibilityRule.objects.all().delete()
        self.assert_blocked("Eligibility rules have not been configured.")

    def test_every_failed_rule_is_reported(self):
        self.donor.weight_kg = 40
        self.donor.consent_given = False
        self.donor.date_of_birth = self.birthday(70)
        ok, reasons = eligibility(self.donor)
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 3)


class RequestStateMachineUnitTests(TestCase):
    """BB-45 [NFR-08]: unit tests for every request state transition (FR-20)."""

    ALLOWED = {
        "PENDING": {"APPROVED", "REJECTED", "CANCELLED"},
        "APPROVED": {"PARTIAL", "FULFILLED", "CANCELLED"},
        "PARTIAL": {"FULFILLED", "CANCELLED"},
        "FULFILLED": set(),
        "REJECTED": set(),
        "CANCELLED": set(),
    }

    def setUp(self):
        self.branch = Branch.objects.create(name="Request SM Branch", code="RSM")
        self.staff = User.objects.create_user("rsmstaff@bbms.test", email="rsmstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)

    def make_request(self, status):
        return BloodRequest.objects.create(
            hospital="RSM Hospital",
            created_by=self.staff,
            branch=self.branch,
            blood_group="B+",
            component="Plasma",
            quantity=1,
            priority="Routine",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference=f"REF-RSM-{status}",
            status=status,
        )

    def test_every_request_state_pair(self):
        self.assertEqual(set(self.ALLOWED), set(BloodRequest.Status.values))
        for current in BloodRequest.Status.values:
            for target in BloodRequest.Status.values:
                with self.subTest(current=current, target=target):
                    req = self.make_request(current)
                    if target in self.ALLOWED[current]:
                        transition_request(req, target, self.staff)
                        req.refresh_from_db()
                        self.assertEqual(req.status, target)
                        self.assertTrue(req.transitions.filter(from_status=current, to_status=target, actor=self.staff).exists())
                    else:
                        with self.assertRaises(ValueError):
                            transition_request(req, target, self.staff)
                        req.refresh_from_db()
                        self.assertEqual(req.status, current)
                        self.assertFalse(req.transitions.exists())
                    req.transitions.all().delete()
                    req.delete()

    def test_cancelling_releases_reserved_units(self):
        donor = Donor.objects.create(
            branch=self.branch,
            full_name="RSM Donor",
            email="rsmdonor@example.test",
            phone="9000000111",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70,
            blood_group="B+",
            consent_given=True,
        )
        donation = Donation.objects.create(
            donation_id="DON-RSM-1", donor=donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        unit = BloodUnit.objects.create(
            unit_id="UNIT-RSM-1",
            donation=donation,
            blood_group="B+",
            component="Plasma",
            volume_ml=220,
            expiry_at=timezone.now() + timedelta(days=30),
            branch=self.branch,
            status=BloodUnit.Status.AVAILABLE,
        )
        req = self.make_request(BloodRequest.Status.APPROVED)
        reservation = reserve_units(req, self.staff)[0]
        transition_request(req, BloodRequest.Status.CANCELLED, self.staff)
        reservation.refresh_from_db()
        unit.refresh_from_db()
        self.assertFalse(reservation.active)
        self.assertEqual(unit.status, BloodUnit.Status.AVAILABLE)


class DuplicateDonorEmailTests(TestCase):
    """BB-09 [FR-02] / TC-F01: one donor profile per email address."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Dup Branch", code="DUP")
        self.staff = User.objects.create_user("dupstaff@bbms.test", email="dupstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.client.force_login(self.staff)

    def payload(self, email):
        return {
            "full_name": "Donor One",
            "email": email,
            "phone": "9000000001",
            "date_of_birth": "1995-05-05",
            "weight_kg": "60",
            "blood_group": "O+",
            "medical_history": "None",
            "consent_given": "on",
            **{f"screening_{key}": "no" for key, _ in SCREENING_QUESTIONS},
        }

    def test_tc_f01_second_registration_with_same_email_is_rejected(self):
        self.client.post("/donors/", self.payload("donor1@example.test"))
        self.assertEqual(Donor.objects.count(), 1)
        response = self.client.post("/donors/", self.payload("donor1@example.test"))
        self.assertContains(response, "already registered with this email")
        self.assertEqual(Donor.objects.count(), 1)

    def test_email_comparison_is_case_insensitive(self):
        self.client.post("/donors/", self.payload("donor1@example.test"))
        response = self.client.post("/donors/", self.payload("  Donor1@EXAMPLE.test "))
        self.assertContains(response, "already registered with this email")
        self.assertEqual(Donor.objects.count(), 1)
        self.assertEqual(Donor.objects.get().email, "donor1@example.test")


class DonorRegistrationValidationTests(TestCase):
    """BB-08 [FR-01] / TC-F01: a donor is registered only after every mandatory field is valid."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Reg Branch", code="REG")
        self.staff = User.objects.create_user("regstaff@bbms.test", email="regstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.client.force_login(self.staff)

    def payload(self, **overrides):
        data = {
            "full_name": "Asha Rao",
            "email": "asha@example.test",
            "phone": "+91 98450 12345",
            "date_of_birth": "1998-01-20",
            "weight_kg": "58",
            "blood_group": "B+",
            "medical_history": "None",
            "consent_given": "on",
            **{f"screening_{key}": "no" for key, _ in SCREENING_QUESTIONS},
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def assert_rejected(self, data, message):
        response = self.client.post("/donors/", data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertEqual(Donor.objects.count(), 0)

    def test_tc_f01_valid_registration_is_saved_and_audited(self):
        response = self.client.post("/donors/", self.payload())
        self.assertRedirects(response, "/donors/")
        donor = Donor.objects.get()
        self.assertEqual(donor.phone, "+919845012345")
        self.assertEqual(donor.branch, self.branch)
        self.assertTrue(donor.consent_given)
        self.assertEqual(donor.screening_responses, {key: False for key, _ in SCREENING_QUESTIONS})
        event = AuditEvent.objects.get(event_type="DONOR_REGISTERED")
        self.assertEqual(event.record_id, str(donor.pk))
        self.assertEqual(event.actor, self.staff)

    def test_missing_mandatory_fields_return_field_errors(self):
        for field in ["full_name", "email", "phone", "date_of_birth", "weight_kg", "blood_group"]:
            with self.subTest(field=field):
                response = self.client.post("/donors/", self.payload(**{field: None}))
                self.assertIn(field, response.context["form"].errors)
        self.assertEqual(Donor.objects.count(), 0)

    def test_consent_is_mandatory(self):
        self.assert_rejected(self.payload(consent_given=None), "Donor consent is required to register.")

    def test_medical_history_and_questionnaire_are_mandatory(self):
        self.assert_rejected(self.payload(medical_history="   "), "Medical history is required.")
        self.assert_rejected(self.payload(screening_recent_illness=None), "Answer every screening question.")

    def test_future_date_of_birth_is_rejected(self):
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assert_rejected(self.payload(date_of_birth=future), "Date of birth cannot be in the future.")

    def test_invalid_blood_group_is_rejected(self):
        self.assert_rejected(self.payload(blood_group="C+"), "Select a valid choice")

    def test_invalid_phone_is_rejected(self):
        for phone in ["12345", "98450abcde", "+91 98450 12345 67890"]:
            with self.subTest(phone=phone):
                self.assert_rejected(self.payload(phone=phone), "Enter a valid phone number")


class ConfiguredEligibilityRuleTests(TestCase):
    """BB-10 [FR-03] / TC-F02: eligibility uses the administrator-configured rule, donation type and screening."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Rule Branch", code="RULE")
        self.staff = User.objects.create_user("rulestaff@bbms.test", email="rulestaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.rule = EligibilityRule.objects.create(
            min_age=18, max_age=65, min_weight_kg=50,
            minimum_interval_days=90, platelets_interval_days=14, plasma_interval_days=28,
        )
        self.donor = Donor.objects.create(
            branch=self.branch, full_name="Rule Donor", email="rule@example.test", phone="9000000222",
            date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=60, blood_group="A+",
            consent_given=True, screening_responses={key: False for key, _ in SCREENING_QUESTIONS},
        )

    def donate(self, days_ago, donation_type):
        return Donation.objects.create(
            donation_id=f"DON-RULE-{days_ago}-{donation_type}", donor=self.donor,
            collected_at=timezone.now() - timedelta(days=days_ago), collector=self.staff,
            branch=self.branch, donation_type=donation_type,
        )

    def test_tc_f02_age_boundaries_min_and_max(self):
        cases = [(18, 0, True), (18, 1, False), (65, 0, True), (66, 0, False)]
        for age, offset, expected in cases:
            with self.subTest(age=age, offset=offset):
                self.donor.date_of_birth = EligibilityRuleUnitTests.birthday(age, days_offset=offset)
                self.assertEqual(eligibility(self.donor)[0], expected)

    def test_tc_f02_weight_boundary(self):
        for weight, expected in [(50, True), (49.9, False), (120, True)]:
            with self.subTest(weight=weight):
                self.donor.weight_kg = weight
                self.assertEqual(eligibility(self.donor)[0], expected)

    def test_interval_depends_on_previous_donation_type(self):
        self.donate(20, DonationType.PLATELETS)
        self.assertTrue(eligibility(self.donor)[0])
        Donation.objects.all().delete()
        self.donate(20, DonationType.PLASMA)
        ok, reasons = eligibility(self.donor)
        self.assertFalse(ok)
        self.assertIn("Minimum donation interval has not elapsed", reasons[0])
        Donation.objects.all().delete()
        self.donate(60, DonationType.WHOLE_BLOOD)
        self.assertFalse(eligibility(self.donor)[0])

    def test_intervals_come_from_configuration(self):
        self.donate(20, DonationType.PLATELETS)
        self.rule.platelets_interval_days = 30
        self.rule.save()
        self.assertFalse(eligibility(self.donor)[0])

    def test_next_eligible_date_uses_latest_constraint(self):
        whole = self.donate(10, DonationType.WHOLE_BLOOD)
        self.donate(5, DonationType.PLATELETS)
        expected = timezone.localdate(whole.collected_at) + timedelta(days=90)
        self.assertEqual(next_eligible_date(self.donor), expected)

    def test_failed_screening_questionnaire_blocks(self):
        self.donor.screening_responses["recent_tattoo"] = True
        ok, reasons = eligibility(self.donor)
        self.assertFalse(ok)
        self.assertIn("Tattoo, piercing or acupuncture", reasons[0])
        self.assertNotIn(self.donor.medical_history or "asthma", " ".join(reasons))

    def test_active_deferral_blocks(self):
        Deferral.objects.create(
            donor=self.donor, reason="Low haemoglobin", end_date=timezone.localdate() + timedelta(days=7),
            approved_by=self.staff,
        )
        self.assertIn("An active donor deferral applies.", eligibility(self.donor)[1])

    def test_donation_form_records_donation_type(self):
        ShelfLifeRule.objects.create(component="Plasma", days=365)
        self.client.force_login(self.staff)
        self.client.post("/donations/new/", {
            "donor": self.donor.pk, "branch": self.branch.pk, "blood_group": "A+",
            "donation_type": DonationType.PLASMA, "volume_pls": 250,
        })
        self.assertEqual(Donation.objects.get().donation_type, DonationType.PLASMA)


class FailedEligibilityDisplayTests(TestCase):
    """BB-11 [FR-04] / TC-F02: every failed rule is shown and blocks appointment confirmation."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Show Branch", code="SHOW")
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        self.slot = AppointmentSlot.objects.create(
            branch=self.branch, starts_at=timezone.now() + timedelta(days=2), capacity=5
        )
        self.user = User.objects.create_user("showdonor@bbms.test", email="showdonor@bbms.test", password="pw")
        UserProfile.objects.create(user=self.user, role=UserProfile.Role.DONOR, branch=self.branch)
        self.donor = Donor.objects.create(
            user=self.user, branch=self.branch, full_name="Show Donor", email="showdonor@bbms.test",
            phone="9000000333", date_of_birth=EligibilityRuleUnitTests.birthday(17), weight_kg=45,
            blood_group="B-", medical_history="Private cardiac note", consent_given=True,
        )
        self.client.force_login(self.user)
        self.expected = ["Age must be between 18 and 65 years.", "Weight must be at least 50"]

    def test_tc_f02_booking_page_lists_every_failed_rule(self):
        response = self.client.get("/appointments/")
        for reason in self.expected:
            self.assertContains(response, reason)
        self.assertContains(response, "disabled")
        self.assertNotContains(response, "Private cardiac note")

    def test_tc_f02_confirmation_is_blocked_and_audited(self):
        response = self.client.post("/appointments/", {"slot": self.slot.pk})
        self.assertContains(response, "Appointment blocked until every eligibility rule passes.")
        for reason in self.expected:
            self.assertContains(response, reason)
        self.assertFalse(Appointment.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(event_type="APPOINTMENT_BLOCKED", outcome="REJECTED").exists())

    def test_donor_record_page_shows_the_same_reasons(self):
        response = self.client.get("/my-record/")
        for reason in self.expected:
            self.assertContains(response, reason)
        self.assertNotContains(response, 'href="/appointments/"')

    def test_eligible_donor_can_confirm(self):
        self.donor.date_of_birth = EligibilityRuleUnitTests.birthday(30)
        self.donor.weight_kg = 70
        self.donor.save()
        response = self.client.post("/appointments/", {"slot": self.slot.pk})
        self.assertRedirects(response, "/my-record/")
        self.assertEqual(Appointment.objects.get().donor, self.donor)


class DonorDeferralTests(TestCase):
    """BB-15 [FR-08] / TC-F05: staff record temporary or permanent deferrals that block eligibility."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Defer Branch", code="DEF")
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        self.staff = User.objects.create_user("defstaff@bbms.test", email="defstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.other = User.objects.create_user("other@bbms.test", email="other@bbms.test", password="pw")
        self.donor = Donor.objects.create(
            branch=self.branch, full_name="Defer Donor", email="defer@example.test", phone="9000000444",
            date_of_birth=EligibilityRuleUnitTests.birthday(28), weight_kg=65, blood_group="AB+", consent_given=True,
        )
        self.client.force_login(self.staff)
        self.today = timezone.localdate()

    def post(self, **data):
        payload = {"donor": self.donor.pk, "reason": "Low haemoglobin", "start_date": self.today.isoformat()}
        payload.update(data)
        return self.client.post("/deferrals/", {k: v for k, v in payload.items() if v is not None})

    def test_tc_f05_temporary_deferral_stored_with_staff_identity_and_blocks(self):
        self.assertTrue(eligibility(self.donor)[0])
        response = self.post(end_date=(self.today + timedelta(days=30)).isoformat(), approved_by=self.other.pk)
        self.assertRedirects(response, "/deferrals/")
        deferral = Deferral.objects.get()
        self.assertEqual(deferral.approved_by, self.staff)
        self.assertFalse(deferral.permanent)
        self.assertIn("An active donor deferral applies.", eligibility(self.donor)[1])
        event = AuditEvent.objects.get(event_type="DEFERRAL_RECORDED")
        self.assertEqual(event.record_id, str(deferral.pk))
        self.assertIn("Temporary until", event.summary)

    def test_tc_f05_permanent_deferral_needs_no_end_date(self):
        response = self.post(permanent="on", reason="Positive HIV confirmatory test")
        self.assertRedirects(response, "/deferrals/")
        deferral = Deferral.objects.get()
        self.assertTrue(deferral.permanent)
        self.assertIsNone(deferral.end_date)
        self.assertFalse(eligibility(self.donor)[0])

    def test_tc_f05_temporary_deferral_without_end_date_is_rejected(self):
        response = self.post()
        self.assertContains(response, "Temporary deferrals require an end date.")
        self.assertFalse(Deferral.objects.exists())
        self.assertTrue(eligibility(self.donor)[0])

    def test_end_date_before_start_and_blank_reason_are_rejected(self):
        response = self.post(end_date=(self.today - timedelta(days=1)).isoformat())
        self.assertContains(response, "End date cannot be before the start date.")
        response = self.post(reason="   ", end_date=(self.today + timedelta(days=5)).isoformat())
        self.assertContains(response, "This field is required.")
        self.assertFalse(Deferral.objects.exists())

    def test_ended_deferral_no_longer_blocks(self):
        Deferral.objects.create(
            donor=self.donor, reason="Travel", start_date=self.today - timedelta(days=40),
            end_date=self.today - timedelta(days=1), approved_by=self.staff,
        )
        self.assertTrue(eligibility(self.donor)[0])

    def test_donor_choices_are_limited_to_staff_branch(self):
        other_branch = Branch.objects.create(name="Elsewhere", code="ELSE")
        outsider = Donor.objects.create(
            branch=other_branch, full_name="Outside Donor", email="out@example.test", phone="9000000555",
            date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, blood_group="O-", consent_given=True,
        )
        response = self.client.get("/deferrals/")
        self.assertContains(response, "Defer Donor")
        self.assertNotContains(response, outsider.full_name)


class AppointmentLifecycleTests(TestCase):
    """BB-12 [FR-05] / TC-F03: create, reschedule and cancel appointments for future slots with capacity."""

    def setUp(self):
        self.main = Branch.objects.create(name="Main Appt", code="MAPT")
        self.north = Branch.objects.create(name="North Appt", code="NAPT")
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        now = timezone.now()
        self.past = AppointmentSlot.objects.create(branch=self.main, starts_at=now - timedelta(hours=2), capacity=5)
        self.slot_a = AppointmentSlot.objects.create(branch=self.main, starts_at=now + timedelta(days=1), capacity=2)
        self.slot_b = AppointmentSlot.objects.create(branch=self.main, starts_at=now + timedelta(days=2), capacity=2)
        self.north_slot = AppointmentSlot.objects.create(branch=self.north, starts_at=now + timedelta(days=3), capacity=2)
        self.full = AppointmentSlot.objects.create(branch=self.main, starts_at=now + timedelta(days=4), capacity=1)
        self.user = self.make_donor("appt-a")
        self.other_user = self.make_donor("appt-b")
        Appointment.objects.create(donor=self.other_user.donor_profile, slot=self.full)
        self.donor = self.user.donor_profile
        self.client.force_login(self.user)

    def make_donor(self, key):
        user = User.objects.create_user(f"{key}@bbms.test", email=f"{key}@bbms.test", password="pw")
        UserProfile.objects.create(user=user, role=UserProfile.Role.DONOR, branch=self.main)
        Donor.objects.create(
            user=user, branch=self.main, full_name=key, email=f"{key}@bbms.test", phone="9000000666",
            date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, blood_group="O+", consent_given=True,
        )
        return user

    def offered(self, url="/appointments/"):
        return list(self.client.get(url).context["form"].fields["slot"].queryset)

    def test_only_future_slots_with_free_capacity_are_offered(self):
        offered = self.offered()
        self.assertIn(self.slot_a, offered)
        self.assertNotIn(self.past, offered)
        self.assertNotIn(self.full, offered)

    def test_donor_can_filter_slots_by_branch(self):
        offered = self.offered(f"/appointments/?branch={self.north.pk}")
        self.assertEqual(offered, [self.north_slot])

    def test_past_slot_cannot_be_booked_even_if_posted(self):
        response = self.client.post("/appointments/", {"slot": self.past.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.filter(donor=self.donor).exists())

    def test_tc_f03_book_is_audited_and_confirmation_recorded(self):
        response = self.client.post("/appointments/", {"slot": self.slot_a.pk})
        self.assertRedirects(response, "/my-record/")
        ap = Appointment.objects.get(donor=self.donor)
        self.assertTrue(AuditEvent.objects.filter(event_type="APPOINTMENT_BOOKED", record_id=str(ap.pk)).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.donor.email, subject="Donation appointment confirmed").exists())

    def test_donor_can_reschedule_booked_appointment(self):
        ap = Appointment.objects.create(donor=self.donor, slot=self.slot_a)
        offered = self.offered(f"/appointments/{ap.pk}/reschedule/")
        self.assertNotIn(self.slot_a, offered)
        response = self.client.post(f"/appointments/{ap.pk}/reschedule/", {"slot": self.slot_b.pk})
        self.assertRedirects(response, "/my-record/")
        ap.refresh_from_db()
        self.assertEqual(ap.slot, self.slot_b)
        self.assertEqual(ap.status, Appointment.Status.BOOKED)
        self.assertTrue(AuditEvent.objects.filter(event_type="APPOINTMENT_RESCHEDULED", record_id=str(ap.pk)).exists())
        self.assertTrue(Notification.objects.filter(subject="Donation appointment rescheduled").exists())

    def test_reschedule_to_full_slot_is_rejected(self):
        ap = Appointment.objects.create(donor=self.donor, slot=self.slot_a)
        response = self.client.post(f"/appointments/{ap.pk}/reschedule/", {"slot": self.full.pk})
        self.assertEqual(response.status_code, 200)
        ap.refresh_from_db()
        self.assertEqual(ap.slot, self.slot_a)

    def test_donor_can_cancel_only_own_appointment(self):
        mine = Appointment.objects.create(donor=self.donor, slot=self.slot_a)
        theirs = Appointment.objects.get(slot=self.full)
        self.assertEqual(self.client.post(f"/appointments/{theirs.pk}/cancel/").status_code, 403)
        self.assertEqual(self.client.post(f"/appointments/{theirs.pk}/reschedule/", {"slot": self.slot_b.pk}).status_code, 403)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, Appointment.Status.BOOKED)
        self.assertRedirects(self.client.post(f"/appointments/{mine.pk}/cancel/"), "/my-record/")
        mine.refresh_from_db()
        self.assertEqual(mine.status, Appointment.Status.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(event_type="APPOINTMENT_CANCELLED", record_id=str(mine.pk)).exists())
        self.assertTrue(Notification.objects.filter(subject="Donation appointment cancelled").exists())

    def test_cancelled_appointment_cannot_be_rescheduled_or_cancelled_again(self):
        ap = Appointment.objects.create(donor=self.donor, slot=self.slot_a, status=Appointment.Status.CANCELLED)
        self.client.post(f"/appointments/{ap.pk}/reschedule/", {"slot": self.slot_b.pk})
        self.client.post(f"/appointments/{ap.pk}/cancel/")
        ap.refresh_from_db()
        self.assertEqual(ap.slot, self.slot_a)
        self.assertFalse(AuditEvent.objects.filter(event_type="APPOINTMENT_CANCELLED").exists())


class SlotCapacityTests(TestCase):
    """BB-13 [FR-06] / TC-F03: slot capacity is never exceeded."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Cap Branch", code="CAP")
        self.staff = User.objects.create_user("capstaff@bbms.test", email="capstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.slot = AppointmentSlot.objects.create(branch=self.branch, starts_at=timezone.now() + timedelta(days=1), capacity=1)
        self.d1, self.d2 = [
            Donor.objects.create(
                branch=self.branch, full_name=f"Cap {i}", email=f"cap{i}@example.test", phone="9000000777",
                date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, blood_group="A-", consent_given=True,
            )
            for i in (1, 2)
        ]

    def test_tc_f03_full_slot_rejects_then_accepts_after_cancellation(self):
        first = book_appointment(self.d1, self.slot, self.staff)
        with self.assertRaisesMessage(AppointmentError, "This slot is full."):
            book_appointment(self.d2, self.slot, self.staff)
        cancel_appointment(first, self.staff)
        second = book_appointment(self.d2, self.slot, self.staff)
        self.assertEqual(self.slot.appointments.filter(status=Appointment.Status.BOOKED).get(), second)

    def test_donor_cannot_hold_two_active_bookings_for_same_slot(self):
        self.slot.capacity = 3
        self.slot.save()
        book_appointment(self.d1, self.slot, self.staff)
        with self.assertRaisesMessage(AppointmentError, "already has a booking for this slot"):
            book_appointment(self.d1, self.slot, self.staff)
        with self.assertRaises(IntegrityError):
            Appointment.objects.create(donor=self.d1, slot=self.slot)

    def test_cancelled_booking_does_not_block_rebooking_same_slot(self):
        first = book_appointment(self.d1, self.slot, self.staff)
        cancel_appointment(first, self.staff)
        book_appointment(self.d1, self.slot, self.staff)
        self.assertEqual(self.slot.appointments.count(), 2)


class ConcurrentSlotBookingTests(TransactionTestCase):
    """BB-13 [FR-06] / TC-F03: parallel booking requests never exceed slot capacity."""

    WORKERS = 8

    def setUp(self):
        self.branch = Branch.objects.create(name="Race Branch", code="RACE")
        self.staff = User.objects.create_user("racestaff@bbms.test", email="racestaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donors = [
            Donor.objects.create(
                branch=self.branch, full_name=f"Race {i}", email=f"race{i}@example.test", phone="9000000888",
                date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, blood_group="B+", consent_given=True,
            )
            for i in range(self.WORKERS)
        ]

    def race(self, capacity):
        slot = AppointmentSlot.objects.create(
            branch=self.branch, starts_at=timezone.now() + timedelta(days=1, minutes=capacity), capacity=capacity
        )
        barrier = threading.Barrier(self.WORKERS)
        outcomes = []

        def attempt(donor):
            try:
                barrier.wait()
                book_appointment(donor, slot, self.staff)
                outcomes.append("booked")
            except AppointmentError:
                outcomes.append("full")
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt, args=(d,)) for d in self.donors]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return slot, outcomes

    def test_tc_f03_parallel_bookings_never_exceed_capacity(self):
        for capacity in (1, 3):
            with self.subTest(capacity=capacity):
                slot, outcomes = self.race(capacity)
                booked = Appointment.objects.filter(slot=slot, status=Appointment.Status.BOOKED).count()
                self.assertEqual(booked, capacity)
                self.assertEqual(outcomes.count("booked"), capacity)
                self.assertEqual(outcomes.count("full"), self.WORKERS - capacity)


class CsrfAndOutputEncodingTests(TestCase):
    """BB-55 [SEC-06] / TC-S06: state-changing requests need a CSRF token; stored script input is encoded."""

    PAYLOAD = "<script>alert('xss')</script>"
    TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "bloodbank"

    def setUp(self):
        self.branch = Branch.objects.create(name="Secure Branch", code="SECB")
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        self.staff = User.objects.create_user("secstaff@bbms.test", email="secstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hospital = User.objects.create_user("sechosp@bbms.test", email="sechosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Secure Hospital"
        )
        self.csrf_client = self.client_class(enforce_csrf_checks=True)

    def donor_payload(self, **overrides):
        data = {
            "full_name": "Safe Donor", "email": "safe@example.test", "phone": "9000000999",
            "date_of_birth": "1995-03-03", "weight_kg": "62", "blood_group": "A+", "medical_history": "None",
            "consent_given": "on", **{f"screening_{key}": "no" for key, _ in SCREENING_QUESTIONS},
        }
        data.update(overrides)
        return data

    def test_csrf_middleware_is_enabled(self):
        from django.conf import settings
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)

    def test_tc_s06_post_without_token_is_rejected(self):
        self.csrf_client.force_login(self.staff)
        response = self.csrf_client.post("/donors/", self.donor_payload())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Donor.objects.exists())

    def test_tc_s06_login_and_api_without_token_are_rejected(self):
        response = self.csrf_client.post("/login/", {"email": "secstaff@bbms.test", "password": "pw"})
        self.assertEqual(response.status_code, 403)
        self.csrf_client.force_login(self.hospital)
        response = self.csrf_client.post(
            "/api/v1/requests", data=json.dumps({"blood_group": "O+"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BloodRequest.objects.exists())

    def test_post_with_token_is_accepted(self):
        self.csrf_client.force_login(self.staff)
        self.csrf_client.get("/donors/")
        token = self.csrf_client.cookies["csrftoken"].value
        response = self.csrf_client.post("/donors/", {**self.donor_payload(), "csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Donor.objects.count(), 1)

    def test_every_post_form_in_templates_includes_the_token(self):
        for template in sorted(self.TEMPLATE_DIR.glob("*.html")):
            html = template.read_text()
            forms = [f for f in html.lower().split("<form")[1:] if 'method="post"' in f.split(">")[0]]
            for form in forms:
                with self.subTest(template=template.name):
                    self.assertIn("{% csrf_token %}", form.split("</form>")[0])

    def test_autoescaping_is_never_disabled(self):
        for template in self.TEMPLATE_DIR.glob("*.html"):
            html = template.read_text()
            with self.subTest(template=template.name):
                self.assertNotIn("autoescape off", html)
                self.assertNotIn("|safe", html)
        source = (Path(__file__).resolve().parent / "views.py").read_text()
        self.assertNotIn("mark_safe", source)
        self.assertNotIn("csrf_exempt", source)

    def test_tc_s06_stored_script_in_donor_name_is_encoded(self):
        self.client.force_login(self.staff)
        self.client.post("/donors/", self.donor_payload(full_name=self.PAYLOAD))
        self.assertEqual(Donor.objects.get().full_name, self.PAYLOAD)
        for url in ["/donors/", "/deferrals/"]:
            response = self.client.get(url)
            with self.subTest(url=url):
                self.assertNotContains(response, self.PAYLOAD)
                self.assertContains(response, "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;")

    def test_tc_s06_stored_script_in_request_reference_is_encoded(self):
        BloodRequest.objects.create(
            hospital="Secure Hospital", created_by=self.hospital, branch=self.branch, blood_group="O+",
            component="Packed Red Cells", quantity=1, priority="Urgent",
            required_by=timezone.now() + timedelta(days=1), clinical_reference=self.PAYLOAD,
        )
        self.client.force_login(self.staff)
        response = self.client.get("/requests/")
        self.assertNotContains(response, self.PAYLOAD)
        self.assertContains(response, "&lt;script&gt;")


# The routes that answer without a session, and why each one is allowed to.
PUBLIC_ROUTES = {
    "login": "the sign-in page itself",
    "logout": "POST-only; ends a session that may already be gone",
    "health": "NFR-03 liveness probe for the load balancer and uptime monitor",
}


def protected_paths():
    """Every bloodbank route except the public ones, with placeholder arguments."""
    paths = []
    for pattern in bloodbank_urls.urlpatterns:
        if pattern.name in PUBLIC_ROUTES:
            continue
        args = ["approve" if group == "action" else 1 for group in pattern.pattern.regex.groupindex]
        paths.append(reverse(pattern.name, args=args))
    return paths


class SprintOneSecurityFixture(TestCase):
    """One account per role, used by the Sprint 0 and Sprint 1 authentication and account stories."""

    PASSWORD = "Str0ng-Demo-Passphrase!"

    def setUp(self):
        cache.clear()
        self.branch = Branch.objects.create(name="Sprint Branch", code="SPR")
        self.users = {}
        for role in UserProfile.Role:
            email = f"{role.value.lower()}@sprint.test"
            user = User.objects.create_user(email, email=email, password=self.PASSWORD)
            UserProfile.objects.create(
                user=user,
                role=role,
                branch=self.branch if role in {UserProfile.Role.STAFF, UserProfile.Role.HOSPITAL} else None,
                hospital_name="Sprint Hospital" if role == UserProfile.Role.HOSPITAL else "",
            )
            self.users[role.value] = user

    def tearDown(self):
        cache.clear()

    def sign_in(self, user, password=None, client=None):
        return (client or self.client).post(
            "/login/", {"email": user.username, "password": password or self.PASSWORD}
        )

    def rendered_messages(self, response):
        return [str(message) for message in response.context["messages"]]


class AuthenticationAcrossRolesTests(SprintOneSecurityFixture):
    """BB-32 [FR-25] & TC-S01: Donor, Blood Bank Staff, Hospital Staff and System Administrator
    all authenticate before any protected function is reachable."""

    def test_tc_s01_each_role_signs_in_and_the_event_is_audited(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                client = Client()
                response = self.sign_in(user, client=client)
                self.assertRedirects(response, "/", fetch_redirect_response=False)
                self.assertEqual(client.session["_auth_user_id"], str(user.pk))
                self.assertTrue(AuditEvent.objects.filter(actor=user, event_type="LOGIN").exists())

    def test_tc_s01_a_deactivated_account_cannot_sign_in(self):
        user = self.users["STAFF"]
        user.profile.active = False
        user.profile.save(update_fields=["active"])
        response = self.sign_in(user)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.rendered_messages(response), [GENERIC_LOGIN_ERROR])
        self.assertFalse(AuditEvent.objects.filter(actor=user, event_type="LOGIN").exists())

    def test_tc_s01_a_wrong_password_never_starts_a_session(self):
        response = self.sign_in(self.users["DONOR"], password="not-the-password")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.rendered_messages(response), [GENERIC_LOGIN_ERROR])

    def test_every_route_but_the_public_ones_requires_an_authenticated_user(self):
        """FR-25: read from urls.py, so a view added without login protection fails here."""
        paths = protected_paths()
        self.assertGreater(len(paths), 20)
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302, f"{path} answered an anonymous request")
                self.assertIn("/login/", response["Location"])


class AccountLockoutTests(SprintOneSecurityFixture):
    """BB-52 [SEC-03] & TC-S01: five failed sign-ins within ten minutes lock the account for
    fifteen, the lock is shared between workers, and both are audited."""

    def failures(self, user, count):
        for _ in range(count):
            self.sign_in(user, password="wrong-password", client=Client())

    def test_tc_s01_five_failures_lock_the_account_and_the_lock_is_audited(self):
        user = self.users["STAFF"]
        self.failures(user, LOGIN_FAILURE_LIMIT - 1)
        self.assertFalse(account_is_locked(user.username))
        self.assertFalse(AuditEvent.objects.filter(event_type="ACCOUNT_LOCKED").exists())

        self.failures(user, 1)
        self.assertTrue(account_is_locked(user.username))

        recorded = AuditEvent.objects.filter(event_type="LOGIN_FAILED", record_id=user.username)
        self.assertEqual(recorded.count(), LOGIN_FAILURE_LIMIT)
        self.assertEqual(set(recorded.values_list("outcome", flat=True)), {"DENIED"})
        lockout = AuditEvent.objects.get(event_type="ACCOUNT_LOCKED", record_id=user.username)
        self.assertEqual(lockout.outcome, "DENIED")
        self.assertIn(f"{LOGIN_LOCKOUT_SECONDS // 60} minutes", lockout.summary)
        self.assertIn(f"{LOGIN_FAILURE_WINDOW_SECONDS // 60} minutes", lockout.summary)

    def test_tc_s01_the_correct_password_is_refused_while_the_account_is_locked(self):
        user = self.users["STAFF"]
        self.failures(user, LOGIN_FAILURE_LIMIT)
        response = self.sign_in(user)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.rendered_messages(response), [GENERIC_LOGIN_ERROR])
        self.assertTrue(
            AuditEvent.objects.filter(event_type="LOGIN_BLOCKED", record_id=user.username).exists()
        )

    def test_sec_03_the_lock_is_held_in_the_shared_database_cache(self):
        """A local-memory cache would give every worker process its own count of the attempts."""
        self.assertEqual(
            settings.CACHES["default"]["BACKEND"], "django.core.cache.backends.db.DatabaseCache"
        )
        self.failures(self.users["STAFF"], LOGIN_FAILURE_LIMIT)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {settings.CACHES['default']['LOCATION']}")
            self.assertGreater(cursor.fetchone()[0], 0)

    def test_sec_03_failures_spread_wider_than_the_window_never_add_up_to_a_lock(self):
        user = self.users["STAFF"]
        stale = timezone.now().timestamp() - LOGIN_FAILURE_WINDOW_SECONDS - 1
        cache.set(
            _failure_key(user.username),
            {"count": LOGIN_FAILURE_LIMIT - 1, "started": stale},
            LOGIN_FAILURE_WINDOW_SECONDS,
        )
        self.assertFalse(record_login_failure(user.username))
        self.assertFalse(account_is_locked(user.username))
        self.assertEqual(cache.get(_failure_key(user.username))["count"], 1)

    def test_a_successful_sign_in_clears_the_counter(self):
        user = self.users["STAFF"]
        self.failures(user, LOGIN_FAILURE_LIMIT - 1)
        self.sign_in(user)
        self.assertIsNone(cache.get(_failure_key(user.username)))
        self.assertFalse(account_is_locked(user.username))

    def test_the_account_works_again_once_the_lock_expires(self):
        user = self.users["STAFF"]
        self.failures(user, LOGIN_FAILURE_LIMIT)
        clear_login_failures(user.username)  # the entry's fifteen-minute lifetime, fast-forwarded
        self.sign_in(user)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))


class GenericAuthenticationFailureTests(SprintOneSecurityFixture):
    """BB-59 [SEC-10] & TC-S01: no sign-in failure reveals whether an address is registered."""

    def test_tc_s01_unknown_wrong_deactivated_and_locked_all_answer_the_same(self):
        staff, hospital = self.users["STAFF"], self.users["HOSPITAL"]
        hospital.profile.active = False
        hospital.profile.save(update_fields=["active"])

        cases = {
            "unknown email": Client().post(
                "/login/", {"email": "nobody@sprint.test", "password": self.PASSWORD}
            ),
            "wrong password": self.sign_in(staff, password="wrong-password", client=Client()),
            "deactivated account": self.sign_in(hospital, client=Client()),
        }
        self.failures_until_locked(staff)
        cases["locked account"] = self.sign_in(staff, client=Client())

        for label, response in cases.items():
            with self.subTest(case=label):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.rendered_messages(response), [GENERIC_LOGIN_ERROR])
                self.assertContains(response, GENERIC_LOGIN_ERROR)

    def failures_until_locked(self, user):
        while not account_is_locked(user.username):
            self.sign_in(user, password="wrong-password", client=Client())


class PasswordStorageTests(SprintOneSecurityFixture):
    """BB-51 [SEC-02]: passwords are stored only as an adaptive hash and are never logged."""

    def test_sec_02_every_password_is_stored_with_djangos_adaptive_hasher(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.assertIn(identify_hasher(user.password).algorithm, {"pbkdf2_sha256", "argon2"})
                self.assertNotIn(self.PASSWORD, user.password)
                self.assertTrue(check_password(self.PASSWORD, user.password))

    def test_sec_02_the_configured_validators_refuse_a_weak_password(self):
        self.client.force_login(self.users["ADMIN"])
        response = self.client.post("/accounts/", {
            "full_name": "Weak Password", "email": "weak@sprint.test",
            "role": UserProfile.Role.DONOR, "branch": "", "hospital_name": "", "password": "123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="weak@sprint.test").exists())
        self.assertTrue(response.context["form"].errors["password"])

    def test_sec_02_a_password_never_reaches_the_audit_trail_or_a_notification(self):
        guess = "another-secret-guess"
        self.client.force_login(self.users["ADMIN"])
        self.client.post("/accounts/", {
            "full_name": "New Staff", "email": "new.staff@sprint.test",
            "role": UserProfile.Role.STAFF, "branch": self.branch.pk, "hospital_name": "",
            "password": self.PASSWORD,
        })
        new_staff = User.objects.get(username="new.staff@sprint.test")
        self.assertTrue(check_password(self.PASSWORD, new_staff.password))
        self.sign_in(new_staff, client=Client())
        self.sign_in(new_staff, password=guess, client=Client())

        written = list(AuditEvent.objects.values_list("summary", flat=True))
        written += list(AuditEvent.objects.values_list("record_id", flat=True))
        written += list(Notification.objects.values_list("body", flat=True))
        for text in written:
            self.assertNotIn(self.PASSWORD, text)
            self.assertNotIn(guess, text)


class AdminUserManagementTests(SprintOneSecurityFixture):
    """BB-33 [FR-26] & TC-S02: the administrator's in-app account screen, fully audited."""

    def setUp(self):
        super().setUp()
        self.admin = self.users["ADMIN"]
        self.client.force_login(self.admin)

    def create_payload(self, **overrides):
        payload = {
            "full_name": "Nurse Nadia", "email": "nadia@sprint.test",
            "role": UserProfile.Role.STAFF, "branch": self.branch.pk,
            "hospital_name": "", "password": "Str0ng-New-Passphrase!",
        }
        payload.update(overrides)
        return payload

    def live_sessions_for(self, user):
        return [
            session for session in Session.objects.filter(expire_date__gte=timezone.now())
            if session.get_decoded().get("_auth_user_id") == str(user.pk)
        ]

    def test_fr26_the_administrator_creates_an_account_with_one_role_and_one_branch(self):
        self.client.post("/accounts/", self.create_payload())
        profile = UserProfile.objects.get(user__username="nadia@sprint.test")
        self.assertEqual(profile.role, UserProfile.Role.STAFF)
        self.assertEqual(profile.branch, self.branch)
        self.assertEqual(UserProfile.objects.filter(user=profile.user).count(), 1)
        event = AuditEvent.objects.get(event_type="USER_ACCOUNT_CREATED", record_id=str(profile.user.pk))
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.outcome, "SUCCESS")
        signed_in = Client().post(
            "/login/", {"email": "nadia@sprint.test", "password": "Str0ng-New-Passphrase!"}
        )
        self.assertRedirects(signed_in, "/", fetch_redirect_response=False)

    def test_fr26_an_operational_role_is_refused_without_a_branch(self):
        response = self.client.post("/accounts/", self.create_payload(branch=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="nadia@sprint.test").exists())
        self.assertIn("branch", response.context["form"].errors)

    def test_fr26_a_duplicate_email_is_refused(self):
        response = self.client.post(
            "/accounts/", self.create_payload(email=self.users["STAFF"].username)
        )
        self.assertIn("email", response.context["form"].errors)
        self.assertEqual(User.objects.filter(username=self.users["STAFF"].username).count(), 1)

    def test_tc_s02_only_the_administrator_reaches_the_account_screen(self):
        for role in ["STAFF", "HOSPITAL", "DONOR"]:
            with self.subTest(role=role):
                client = Client()
                client.force_login(self.users[role])
                self.assertEqual(client.get("/accounts/").status_code, 403)
                self.assertEqual(
                    client.post(f"/accounts/{self.users[role].profile.pk}/status/", {"active": "0"}).status_code,
                    403,
                )
                self.assertTrue(AuditEvent.objects.filter(
                    actor=self.users[role], event_type="UNAUTHORIZED_ACCESS", outcome="DENIED",
                ).exists())

    def test_fr26_deactivation_blocks_the_next_sign_in_and_ends_open_sessions(self):
        staff = self.users["STAFF"]
        staff_client = Client()
        self.sign_in(staff, client=staff_client)
        self.assertEqual(staff_client.get("/donors/").status_code, 200)

        self.client.post(f"/accounts/{staff.profile.pk}/status/", {"active": "0"})

        staff.profile.refresh_from_db()
        self.assertFalse(staff.profile.active)
        self.assertEqual(self.live_sessions_for(staff), [])
        self.assertEqual(staff_client.get("/donors/").status_code, 302)
        refused = self.sign_in(staff, client=Client())
        self.assertEqual(refused.status_code, 200)
        event = AuditEvent.objects.filter(event_type="USER_ACCOUNT_DEACTIVATED").latest("created_at")
        self.assertEqual(event.actor, self.admin)
        self.assertIn("session(s) ended", event.summary)

    def test_fr26_reactivation_is_audited_and_restores_sign_in(self):
        staff = self.users["STAFF"]
        set_account_active(staff.profile, False, self.admin)
        self.client.post(f"/accounts/{staff.profile.pk}/status/", {"active": "1"})
        staff.profile.refresh_from_db()
        self.assertTrue(staff.profile.active)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="USER_ACCOUNT_ACTIVATED", record_id=str(staff.pk),
        ).exists())
        self.assertRedirects(
            self.sign_in(staff, client=Client()), "/", fetch_redirect_response=False
        )

    def test_fr26_assigning_a_role_replaces_the_previous_one_and_is_audited(self):
        donor = self.users["DONOR"]
        self.client.post(
            f"/accounts/{donor.profile.pk}/role/",
            {"role": UserProfile.Role.STAFF, "branch": self.branch.pk},
        )
        donor.profile.refresh_from_db()
        self.assertEqual(donor.profile.role, UserProfile.Role.STAFF)
        self.assertEqual(donor.profile.branch, self.branch)
        self.assertEqual(UserProfile.objects.filter(user=donor).count(), 1)
        event = AuditEvent.objects.get(event_type="USER_ROLE_ASSIGNED", record_id=str(donor.pk))
        self.assertIn("Donor to Blood Bank Staff", event.summary)
        client = Client()
        client.force_login(donor)
        self.assertEqual(client.get("/donors/").status_code, 200)

    def test_fr26_a_role_change_to_an_operational_role_needs_a_branch(self):
        donor = self.users["DONOR"]
        self.client.post(
            f"/accounts/{donor.profile.pk}/role/", {"role": UserProfile.Role.STAFF, "branch": ""}
        )
        donor.profile.refresh_from_db()
        self.assertEqual(donor.profile.role, UserProfile.Role.DONOR)
        self.assertFalse(AuditEvent.objects.filter(event_type="USER_ROLE_ASSIGNED").exists())

    def test_uc06_the_last_active_administrator_cannot_be_deactivated_or_demoted(self):
        profile = self.admin.profile
        self.client.post(f"/accounts/{profile.pk}/status/", {"active": "0"})
        profile.refresh_from_db()
        self.assertTrue(profile.active)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="USER_ACCOUNT_DEACTIVATED", outcome="REJECTED",
        ).exists())

        self.client.post(
            f"/accounts/{profile.pk}/role/",
            {"role": UserProfile.Role.STAFF, "branch": self.branch.pk},
        )
        profile.refresh_from_db()
        self.assertEqual(profile.role, UserProfile.Role.ADMIN)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="USER_ROLE_ASSIGNED", outcome="REJECTED",
        ).exists())

        with self.assertRaises(LastAdministrator):
            set_account_active(profile, False, self.admin)
        with self.assertRaises(LastAdministrator):
            assign_account_role(profile, UserProfile.Role.DONOR, None, self.admin)

    def test_uc06_a_second_administrator_makes_the_first_one_removable(self):
        create_account(
            self.admin, "second.admin@sprint.test", "Second Admin",
            UserProfile.Role.ADMIN, None, "Str0ng-Second-Passphrase!",
        )
        profile = self.admin.profile
        set_account_active(profile, False, self.admin)
        profile.refresh_from_db()
        self.assertFalse(profile.active)

    def test_fr26_ending_sessions_leaves_other_accounts_signed_in(self):
        staff, hospital = self.users["STAFF"], self.users["HOSPITAL"]
        staff_client, hospital_client = Client(), Client()
        self.sign_in(staff, client=staff_client)
        self.sign_in(hospital, client=hospital_client)
        self.assertEqual(end_user_sessions(staff), 1)
        self.assertEqual(self.live_sessions_for(staff), [])
        self.assertEqual(len(self.live_sessions_for(hospital)), 1)


class AuditCoverageTests(SprintOneSecurityFixture):
    """BB-37 [FR-30] & TC-S03: an audit event for every security-relevant category named in the SRS."""

    def setUp(self):
        super().setUp()
        self.staff = self.users["STAFF"]
        self.admin = self.users["ADMIN"]
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_staff", "is_superuser"])
        EligibilityRule.objects.create(min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90)
        ShelfLifeRule.objects.create(component="Plasma", days=365)
        self.test_type = TestType.objects.create(name="HIV", required=True)
        self.donor = Donor.objects.create(
            branch=self.branch, full_name="Audit Donor", email="audit.donor@sprint.test",
            phone="9000000111", date_of_birth=timezone.localdate() - timedelta(days=28 * 365),
            weight_kg=72, blood_group="O+", medical_history="None", consent_given=True,
        )
        self.donation = Donation.objects.create(
            donation_id="DON-AUD-1", donor=self.donor, collected_at=timezone.now(),
            collector=self.staff, branch=self.branch,
        )

    def unit(self, unit_id, status):
        return BloodUnit.objects.create(
            unit_id=unit_id, donation=self.donation, blood_group="O+", component="Plasma",
            volume_ml=220, expiry_at=timezone.now() + timedelta(days=30), branch=self.branch,
            status=status,
        )

    def blood_request(self, reference, status):
        return BloodRequest.objects.create(
            hospital="Sprint Hospital", created_by=self.users["HOSPITAL"], branch=self.branch,
            blood_group="O+", component="Plasma", quantity=1, priority="Urgent",
            required_by=timezone.now() + timedelta(days=1), clinical_reference=reference, status=status,
        )

    def exercise_every_category(self):
        # Authentication: a success, a failure, a lockout and a sign-out.
        self.sign_in(self.staff, client=Client())
        signed_in = Client()
        self.sign_in(self.staff, client=signed_in)
        signed_in.post("/logout/")
        locked_out = self.users["DONOR"]
        for _ in range(LOGIN_FAILURE_LIMIT):
            self.sign_in(locked_out, password="wrong-password", client=Client())

        # Account management: creation, role assignment and deactivation.
        created = create_account(
            self.admin, "audited@sprint.test", "Audited Account",
            UserProfile.Role.STAFF, self.branch, "Str0ng-Audited-Passphrase!",
        )
        assign_account_role(created, UserProfile.Role.DONOR, None, self.admin)
        set_account_active(created, False, self.admin)

        # Configuration: a threshold changed through the administrator's Django admin (FR-27).
        admin_client = Client()
        admin_client.force_login(self.admin)
        admin_client.post(f"/admin/bloodbank/branch/{self.branch.pk}/change/", {
            "name": self.branch.name, "code": self.branch.code,
            "timezone_name": self.branch.timezone_name, "low_stock_threshold": 9,
            "near_expiry_days": self.branch.near_expiry_days,
        })

        # Deferral.
        staff_client = Client()
        staff_client.force_login(self.staff)
        staff_client.post("/deferrals/", {
            "donor": self.donor.pk, "reason": "Low haemoglobin", "permanent": "",
            "start_date": timezone.localdate().isoformat(),
            "end_date": (timezone.localdate() + timedelta(days=30)).isoformat(),
        })

        # Screening and inventory.
        screened = self.unit("UNIT-AUD-SCR", BloodUnit.Status.COLLECTED)
        record_lab_result(screened, self.test_type, LabResult.Outcome.ACCEPTABLE, self.staff)
        discard_unit(self.unit("UNIT-AUD-QUA", BloodUnit.Status.QUARANTINED), self.staff, "Reactive result")

        # Request handling and issuance.
        approved = self.blood_request("REF-AUD-1", BloodRequest.Status.PENDING)
        transition_request(approved, BloodRequest.Status.APPROVED, self.staff)
        self.unit("UNIT-AUD-AVL", BloodUnit.Status.AVAILABLE)
        reservation = reserve_units(approved, self.staff)[0]
        CrossMatch.objects.create(reservation=reservation, outcome="COMPATIBLE", recorded_by=self.staff)
        issue_reservation(reservation, self.staff)

    # FR-30: the categories the SRS lists, and the event types that cover each of them.
    CATEGORIES = {
        "authentication": ["LOGIN", "LOGOUT", "LOGIN_FAILED", "ACCOUNT_LOCKED"],
        "account management": ["USER_ACCOUNT_CREATED", "USER_ROLE_ASSIGNED", "USER_ACCOUNT_DEACTIVATED"],
        "configuration": ["CONFIGURATION_CHANGED"],
        "deferral": ["DEFERRAL_RECORDED"],
        "screening": ["LAB_RESULT_RECORDED", "SCREENING_REVIEWED"],
        "inventory": ["UNIT_DISCARDED", "UNIT_RESERVED"],
        "request": ["REQUEST_STATUS_CHANGED"],
        "issuance": ["UNIT_ISSUED"],
    }

    def test_tc_s03_every_security_relevant_category_writes_an_audit_event(self):
        self.exercise_every_category()
        for category, event_types in self.CATEGORIES.items():
            for event_type in event_types:
                with self.subTest(category=category, event_type=event_type):
                    self.assertTrue(
                        AuditEvent.objects.filter(event_type=event_type).exists(),
                        f"FR-30: no {event_type} event was written for {category}",
                    )

    def test_tc_s03_every_recorded_event_carries_the_fields_sec_07_requires(self):
        self.exercise_every_category()
        for event in AuditEvent.objects.all():
            with self.subTest(event=event.event_type, record=event.record_id):
                self.assertTrue(event.event_type)
                self.assertTrue(event.record_type)
                self.assertTrue(event.record_id)
                self.assertTrue(event.outcome)
                self.assertIsNotNone(event.created_at)

    def test_fr30_a_configuration_change_names_the_administrator_and_the_changed_field(self):
        admin_client = Client()
        admin_client.force_login(self.admin)
        admin_client.post(f"/admin/bloodbank/branch/{self.branch.pk}/change/", {
            "name": self.branch.name, "code": self.branch.code,
            "timezone_name": self.branch.timezone_name, "low_stock_threshold": 11,
            "near_expiry_days": self.branch.near_expiry_days,
        })
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.low_stock_threshold, 11)
        event = AuditEvent.objects.get(event_type="CONFIGURATION_CHANGED")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.branch, self.branch)
        self.assertIn("low_stock_threshold", event.summary)

    def test_fr30_report_exports_are_audited(self):
        self.client.force_login(self.staff)
        self.client.get("/reports/?report=inventory&format=csv")
        event = AuditEvent.objects.get(event_type="REPORT_EXPORTED")
        self.assertEqual(event.actor, self.staff)
        self.assertEqual(event.record_id, "inventory")

    def test_fr30_a_deleted_configuration_row_is_still_audited(self):
        admin_client = Client()
        admin_client.force_login(self.admin)
        rule = ShelfLifeRule.objects.get(component="Plasma")
        admin_client.post(f"/admin/bloodbank/shelfliferule/{rule.pk}/delete/", {"post": "yes"})
        self.assertFalse(ShelfLifeRule.objects.filter(pk=rule.pk).exists())
        event = AuditEvent.objects.get(event_type="CONFIGURATION_DELETED")
        self.assertEqual(event.record_id, str(rule.pk))
        self.assertEqual(event.record_type, "ShelfLifeRule")


class HealthCheckTests(TestCase):
    """BB-40 [NFR-03]: the liveness probe the uptime monitor and the container healthcheck poll."""

    COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"

    def test_nfr03_health_is_public_and_reports_the_database(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok", "database": "up"})

    def test_nfr03_health_is_the_only_unauthenticated_route_besides_sign_in(self):
        named = {p.name for p in bloodbank_urls.urlpatterns if p.name}
        self.assertEqual(PUBLIC_ROUTES.keys() & named, {"login", "logout", "health"})

    def test_nfr03_health_returns_503_when_the_database_is_down(self):
        with patch("bloodbank.views.connection.cursor", side_effect=OperationalError("db is down")):
            response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["status"], "unavailable")

    def test_nfr03_health_body_leaks_nothing_about_the_blood_bank(self):
        Branch.objects.create(name="Secret Branch", code="SECRET")
        body = self.client.get("/health/").content.decode()
        self.assertNotIn("SECRET", body)
        self.assertEqual(set(json.loads(body)), {"status", "database"})

    def test_nfr03_every_compose_service_restarts_itself_and_the_app_is_health_checked(self):
        compose = self.COMPOSE.read_text()
        services_block = compose.split("services:\n", 1)[1].split("\nvolumes:", 1)[0]
        services = re.findall(r"(?m)^  [a-z_]+:$", services_block)
        self.assertGreaterEqual(len(services), 5)
        self.assertEqual(compose.count("restart: unless-stopped"), len(services))
        self.assertIn("pg_isready", compose)
        self.assertIn("/health/", compose)


class RestartPersistenceTests(TestCase):
    """BB-42 [NFR-05] & TC-N04: committed donation, screening, reservation and issuance data
    survives an application restart.

    The drill itself needs Docker, so these tests check the apparatus that performs it: the
    database lives on a named volume, and the probe covers every record type NFR-05 names.
    """

    ROOT = Path(__file__).resolve().parent.parent
    COMPOSE = ROOT / "docker-compose.yml"
    PROBE = ROOT / "scripts" / "restart_persistence_probe.py"
    VERIFY = ROOT / "scripts" / "verify_restart_persistence.py"
    RESULTS = ROOT.parent / "docs" / "test-results" / "TC-N04_NFR-05_restart_persistence_results.md"

    def test_nfr05_postgres_data_is_on_a_named_volume(self):
        compose = self.COMPOSE.read_text()
        # A named volume outlives `docker compose down`; a bind or anonymous mount would not.
        self.assertIn("postgres_data:/var/lib/postgresql/data", compose)
        self.assertRegex(compose, r"(?m)^volumes:$")
        self.assertRegex(compose, r"(?m)^  postgres_data:$")

    def test_tc_n04_the_probe_and_the_driver_both_exist(self):
        for script in [self.PROBE, self.VERIFY]:
            with self.subTest(script=script.name):
                self.assertTrue(script.exists(), f"{script.name} is missing")

    def test_tc_n04_the_probe_covers_every_record_type_nfr05_names(self):
        probe = self.PROBE.read_text()
        for record in ["donation", "screening results", "reservation", "issue"]:
            with self.subTest(record=record):
                self.assertIn(f'"{record}"', probe)
        self.assertIn("def create(", probe)
        self.assertIn("def verify(", probe)

    def test_tc_n04_the_driver_restarts_and_recreates_before_verifying(self):
        verify = self.VERIFY.read_text()
        self.assertIn("restart", verify)
        # Recreating the containers while keeping the volume is the step that proves the data
        # is in Postgres rather than in a container's writable layer.
        self.assertIn("postgres_data", verify.replace("\n", " ") + self.COMPOSE.read_text())
        self.assertIn(str(self.RESULTS.name), verify)

    def test_tc_n04_result_file_is_either_absent_or_records_a_real_run(self):
        """The drill needs Docker, so this asserts honesty rather than a pass: if a result file
        exists it must carry a verdict, and if it does not, nothing claims TC-N04 has run."""
        if not self.RESULTS.exists():
            self.skipTest("TC-N04 has not been run yet; run scripts/verify_restart_persistence.py")
        contents = self.RESULTS.read_text()
        self.assertRegex(contents, r"PASS|FAIL")
        self.assertIn("NFR-05", contents)


class ConfigurationScreenTests(SprintOneSecurityFixture):
    """BB-34 [FR-27] & TC-F14: the administrator configures branches, slots, rules and thresholds,
    with validation, and every change is audited."""

    def setUp(self):
        super().setUp()
        self.admin = self.users["ADMIN"]
        self.client.force_login(self.admin)
        self.rule = EligibilityRule.objects.create(
            min_age=18, max_age=65, min_weight_kg=50, minimum_interval_days=90,
        )
        self.shelf_life = ShelfLifeRule.objects.create(component="Plasma", days=365)

    def branch_payload(self, **overrides):
        payload = {
            "pk": self.branch.pk,
            f"branch-{self.branch.pk}-name": self.branch.name,
            f"branch-{self.branch.pk}-timezone_name": "Asia/Kolkata",
            f"branch-{self.branch.pk}-low_stock_threshold": 9,
            f"branch-{self.branch.pk}-near_expiry_days": 10,
        }
        payload.update(overrides)
        return payload

    def eligibility_payload(self, **overrides):
        payload = {
            "min_age": 18, "max_age": 65, "min_weight_kg": "50.0",
            "minimum_interval_days": 90, "platelets_interval_days": 14, "plasma_interval_days": 28,
        }
        payload.update(overrides)
        return payload

    def test_fr27_only_the_administrator_reaches_the_configuration_screen(self):
        for role_name in ["STAFF", "HOSPITAL", "DONOR"]:
            with self.subTest(role=role_name):
                client = Client()
                client.force_login(self.users[role_name])
                self.assertEqual(client.get("/configuration/").status_code, 403)
                self.assertEqual(
                    client.post("/configuration/branch/", self.branch_payload()).status_code, 403
                )
        self.assertEqual(self.client.get("/configuration/").status_code, 200)

    def test_fr27_branch_thresholds_are_saved_and_audited(self):
        self.client.post("/configuration/branch/", self.branch_payload())
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.low_stock_threshold, 9)
        self.assertEqual(self.branch.near_expiry_days, 10)
        event = AuditEvent.objects.get(event_type="CONFIGURATION_CHANGED", record_type="Branch")
        self.assertEqual(event.actor, self.admin)
        self.assertIn("low_stock_threshold", event.summary)

    def test_tc_f14_an_invalid_age_range_is_refused_and_nothing_is_saved(self):
        response = self.client.post(
            "/configuration/eligibility/", self.eligibility_payload(min_age=70, max_age=65)
        )
        self.assertEqual(response.status_code, 200)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.min_age, 18)
        self.assertFalse(AuditEvent.objects.filter(event_type="CONFIGURATION_CHANGED").exists())
        self.assertContains(response, "maximum age must be greater")

    def test_tc_f14_a_slot_needs_capacity_of_at_least_one_and_a_future_start(self):
        future = (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        past = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

        zero = self.client.post(
            "/configuration/slot/", {"branch": self.branch.pk, "starts_at": future, "capacity": 0}
        )
        self.assertContains(zero, "at least one donor")
        self.assertEqual(AppointmentSlot.objects.count(), 0)

        stale = self.client.post(
            "/configuration/slot/", {"branch": self.branch.pk, "starts_at": past, "capacity": 5}
        )
        self.assertContains(stale, "must start in the future")
        self.assertEqual(AppointmentSlot.objects.count(), 0)

        self.client.post(
            "/configuration/slot/", {"branch": self.branch.pk, "starts_at": future, "capacity": 5}
        )
        self.assertEqual(AppointmentSlot.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="CONFIGURATION_CREATED", record_type="AppointmentSlot",
        ).exists())

    def test_tc_f14_an_interval_or_shelf_life_below_one_day_is_refused(self):
        bad_interval = self.client.post(
            "/configuration/eligibility/", self.eligibility_payload(platelets_interval_days=0)
        )
        self.assertContains(bad_interval, "at least one day")

        bad_shelf = self.client.post(
            "/configuration/shelf-life/",
            {"pk": self.shelf_life.pk, f"shelf-{self.shelf_life.pk}-component": "Plasma",
             f"shelf-{self.shelf_life.pk}-days": 0},
        )
        self.assertContains(bad_shelf, "at least one day")
        self.shelf_life.refresh_from_db()
        self.assertEqual(self.shelf_life.days, 365)

    def test_fr27_an_unknown_time_zone_is_refused(self):
        response = self.client.post(
            "/configuration/branch/",
            self.branch_payload(**{f"branch-{self.branch.pk}-timezone_name": "Mars/Olympus"}),
        )
        self.assertContains(response, "valid IANA time zone")

    def test_fr27_saving_the_eligibility_rule_changes_what_the_engine_applies(self):
        self.client.post("/configuration/eligibility/", self.eligibility_payload(min_age=40))
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.min_age, 40)
        young = Donor.objects.create(
            branch=self.branch, full_name="Young Donor", email="young@sprint.test",
            phone="9000000222", date_of_birth=timezone.localdate() - timedelta(days=25 * 365),
            weight_kg=70, blood_group="O+", medical_history="None", consent_given=True,
        )
        ok, reasons = eligibility(young)
        self.assertFalse(ok)
        self.assertTrue(any("age" in reason.lower() for reason in reasons))


class ReportTests(SprintOneSecurityFixture):
    """BB-35 [FR-28] & TC-F15: five report types, date-range and branch filters, CSV download,
    and no medical fields in hospital-facing reports."""

    def setUp(self):
        super().setUp()
        self.staff = self.users["STAFF"]
        self.hospital = self.users["HOSPITAL"]
        ShelfLifeRule.objects.create(component="Plasma", days=365)
        self.donor = Donor.objects.create(
            branch=self.branch, full_name="Reportable Donor", email="reportable@sprint.test",
            phone="9000000333", date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70, blood_group="O+", medical_history="CONFIDENTIAL-REPORT-NOTE",
            consent_given=True,
        )
        self.old_donation = Donation.objects.create(
            donation_id="DON-OLD-1", donor=self.donor, collected_at=timezone.now() - timedelta(days=40),
            collector=self.staff, branch=self.branch,
        )
        self.new_donation = Donation.objects.create(
            donation_id="DON-NEW-1", donor=self.donor, collected_at=timezone.now() - timedelta(days=1),
            collector=self.staff, branch=self.branch,
        )
        for donation, unit_id, status in [
            (self.old_donation, "UNIT-OLD-1", BloodUnit.Status.AVAILABLE),
            (self.new_donation, "UNIT-NEW-1", BloodUnit.Status.AVAILABLE),
            (self.new_donation, "UNIT-QUAR-1", BloodUnit.Status.QUARANTINED),
        ]:
            BloodUnit.objects.create(
                unit_id=unit_id, donation=donation, blood_group="O+", component="Plasma",
                volume_ml=220, expiry_at=timezone.now() + timedelta(days=30),
                branch=self.branch, status=status,
            )
        self.request = BloodRequest.objects.create(
            hospital="Sprint Hospital", created_by=self.hospital, branch=self.branch,
            blood_group="O+", component="Plasma", quantity=1, priority="Urgent",
            required_by=timezone.now() + timedelta(days=1), clinical_reference="REF-RPT-1",
        )

    def body(self, response):
        return response.content.decode()

    def test_fr28_all_five_report_types_are_available_to_staff(self):
        self.client.force_login(self.staff)
        for report in ["inventory", "expiry", "donation", "request", "audit"]:
            with self.subTest(report=report):
                response = self.client.get(f"/reports/?report={report}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["report"], report)
                self.assertTrue(response.context["columns"])

    def test_tc_f15_the_date_range_filter_narrows_the_donation_report(self):
        self.client.force_login(self.staff)
        everything = self.client.get("/reports/?report=donation")
        self.assertEqual(everything.context["row_count"], 2)

        since = (timezone.localdate() - timedelta(days=7)).isoformat()
        recent = self.client.get(f"/reports/?report=donation&start_date={since}")
        self.assertEqual(recent.context["row_count"], 1)
        self.assertIn("DON-NEW-1", self.body(recent))
        self.assertNotIn("DON-OLD-1", self.body(recent))

    def test_tc_f15_an_end_date_before_the_start_date_is_refused(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            "/reports/?report=donation&start_date=2026-10-20&end_date=2026-10-10"
        )
        self.assertContains(response, "cannot be before the start date")

    def test_fr28_the_branch_filter_scopes_an_administrator_report(self):
        other = Branch.objects.create(name="Other Branch", code="OTH")
        self.client.force_login(self.users["ADMIN"])
        mine = self.client.get(f"/reports/?report=inventory&branch={self.branch.pk}")
        self.assertEqual(mine.context["row_count"], 3)
        theirs = self.client.get(f"/reports/?report=inventory&branch={other.pk}")
        self.assertEqual(theirs.context["row_count"], 0)

    def test_fr28_csv_download_carries_the_same_rows_as_the_page(self):
        self.client.force_login(self.staff)
        page = self.client.get("/reports/?report=inventory")
        csv_response = self.client.get("/reports/?report=inventory&format=csv")
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", csv_response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(self.body(csv_response))))
        self.assertEqual(rows[0], page.context["columns"])
        self.assertEqual(len(rows) - 1, page.context["row_count"])
        self.assertIn("UNIT-NEW-1", self.body(csv_response))

    def test_tc_f15_a_hospital_report_hides_donor_and_medical_data(self):
        self.client.force_login(self.hospital)
        for report in ["inventory", "request"]:
            with self.subTest(report=report):
                for suffix in ["", "&format=csv"]:
                    text = self.body(self.client.get(f"/reports/?report={report}{suffix}"))
                    self.assertNotIn("Reportable Donor", text)
                    self.assertNotIn("CONFIDENTIAL-REPORT-NOTE", text)
                    self.assertNotIn("reportable@sprint.test", text)
                    self.assertNotIn("UNIT-QUAR-1", text)

    def test_tc_f15_a_hospital_cannot_reach_the_donor_or_audit_reports(self):
        self.client.force_login(self.hospital)
        for report in ["donation", "audit", "expiry"]:
            with self.subTest(report=report):
                self.assertEqual(self.client.get(f"/reports/?report={report}").status_code, 403)

    def test_tc_f15_a_hospital_sees_only_its_own_requests(self):
        other_hospital = User.objects.create_user("other.hosp@sprint.test", password=self.PASSWORD)
        UserProfile.objects.create(
            user=other_hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch,
            hospital_name="Other Hospital",
        )
        BloodRequest.objects.create(
            hospital="Other Hospital", created_by=other_hospital, branch=self.branch,
            blood_group="A+", component="Plasma", quantity=2, priority="Routine",
            required_by=timezone.now() + timedelta(days=2), clinical_reference="REF-OTHER-1",
        )
        self.client.force_login(self.hospital)
        text = self.body(self.client.get("/reports/?report=request"))
        self.assertIn("Sprint Hospital", text)
        self.assertNotIn("Other Hospital", text)

    def test_fr28_staff_reports_stay_inside_their_own_branch(self):
        other = Branch.objects.create(name="Other Branch", code="OTH")
        self.client.force_login(self.staff)
        response = self.client.get(f"/reports/?report=inventory&branch={other.pk}")
        self.assertEqual(response.status_code, 403)


class BackupAndRestoreTests(TestCase):
    """BB-41 [NFR-04] & BB-57 [SEC-08], checked during TC-N03: daily encrypted backups with
    7 daily and 4 weekly copies, a timed restore, and confidentiality of both."""

    ROOT = Path(__file__).resolve().parent.parent
    DOCS = ROOT.parent / "docs" / "operations.md"

    @property
    def backup(self):
        return (self.ROOT / "scripts" / "backup.sh").read_text()

    @property
    def restore(self):
        return (self.ROOT / "scripts" / "restore.sh").read_text()

    def test_nfr04_both_scripts_exist_and_are_executable(self):
        for name in ["backup.sh", "restore.sh"]:
            script = self.ROOT / "scripts" / name
            with self.subTest(script=name):
                self.assertTrue(script.exists(), f"{name} is missing")
                self.assertTrue(os.access(script, os.X_OK), f"{name} is not executable")

    def test_nfr04_the_backup_dumps_with_pg_dump_and_encrypts_before_writing(self):
        backup = self.backup
        self.assertIn("pg_dump", backup)
        self.assertIn("gpg", backup)
        # The dump is piped straight into gpg, so no plaintext copy is ever written.
        self.assertRegex(backup, r"pg_dump[\s\S]*?\|\s*\\?\s*\n?\s*gpg")
        self.assertIn("--encrypt", backup)

    def test_sec_08_the_backup_refuses_to_run_without_a_recipient_key(self):
        self.assertIn("BBMS_BACKUP_RECIPIENT", self.backup)
        self.assertRegex(self.backup, r'if \[ -z "\$RECIPIENT" \]')
        self.assertIn("refusing to write an unencrypted backup", self.backup)

    def test_sec_08_backups_are_readable_only_by_the_backup_account(self):
        backup = self.backup
        self.assertIn("umask 077", backup)
        self.assertIn("chmod 700", backup)
        self.assertIn("chmod 600", backup)

    def test_nfr04_retention_keeps_seven_daily_and_four_weekly_copies(self):
        backup = self.backup
        self.assertIn("DAILY_KEEP=7", backup)
        self.assertIn("WEEKLY_KEEP=4", backup)
        self.assertIn('prune "$BACKUP_DIR/daily" "$DAILY_KEEP"', backup)
        self.assertIn('prune "$BACKUP_DIR/weekly" "$WEEKLY_KEEP"', backup)

    def test_tc_n03_the_restore_decrypts_loads_and_reports_its_duration(self):
        restore = self.restore
        self.assertIn("gpg --batch --yes --decrypt", restore)
        self.assertIn("pg_restore", restore)
        self.assertIn("elapsed", restore)
        self.assertIn("4 hours", restore)
        # The plaintext dump lives in a 0700 temporary directory that is always removed.
        self.assertIn("mktemp -d", restore)
        self.assertIn('trap \'rm -rf -- "$workdir"\' EXIT', restore)

    def test_nfr04_compose_runs_the_backup_every_24_hours_on_private_volumes(self):
        compose = (self.ROOT / "docker-compose.yml").read_text()
        self.assertIn("backup.sh", compose)
        self.assertIn("sleep 86400", compose)
        self.assertIn("backup_data:/var/backups/bbms", compose)
        self.assertIn("backup_keys:/root/.gnupg", compose)
        # SEC-08: no other service mounts the backup or key volumes.
        self.assertEqual(compose.count("backup_data:/var/backups/bbms"), 1)
        self.assertEqual(compose.count("backup_keys:/root/.gnupg"), 1)

    def test_nfr04_the_procedure_and_its_recovery_objective_are_documented(self):
        docs = self.DOCS.read_text()
        for expected in ["NFR-03", "NFR-04", "SEC-08", "TC-N03", "/health/",
                         "4 hours", "7", "4", "restore.sh", "Maintenance"]:
            with self.subTest(expected=expected):
                self.assertIn(expected, docs)


class LayeredModuleTests(TestCase):
    """BB-45 [NFR-08]: presentation code delegates every write to the service layer."""

    def test_views_never_write_to_the_database_directly(self):
        source = (Path(__file__).resolve().parent / "views.py").read_text(encoding="utf-8")
        for forbidden in [".objects.create(", ".update_or_create(", ".get_or_create(", ".save()", ".delete(", ".update("]:
            self.assertNotIn(forbidden, source, f"views.py calls {forbidden}; move the write into services.py")


class RequestAndCrossMatchServiceTests(TestCase):
    """BB-45 [NFR-08]: unit tests for the request, cross-match, donor and deferral services moved out of views."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Service Branch", code="SVC")
        self.staff = User.objects.create_user("svcstaff@bbms.test", email="svcstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hospital = User.objects.create_user("svchosp@bbms.test", email="svchosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Service Hospital"
        )
        self.other_hospital = User.objects.create_user("svchosp2@bbms.test", email="svchosp2@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.other_hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Other Hospital"
        )

    def new_request(self):
        return BloodRequest(
            branch=self.branch,
            blood_group="A+",
            component="Plasma",
            quantity=1,
            priority="Urgent",
            required_by=timezone.now() + timedelta(days=1),
            clinical_reference="REF-SVC",
        )

    def test_create_blood_request_ties_request_to_the_hospital_and_starts_pending(self):
        r = create_blood_request(self.new_request(), self.hospital, "idem-svc-1")
        r.refresh_from_db()
        self.assertEqual((r.hospital, r.created_by, r.status), ("Service Hospital", self.hospital, "PENDING"))
        self.assertEqual(find_idempotent_request(self.hospital, "idem-svc-1"), r)
        self.assertIsNone(find_idempotent_request(self.other_hospital, "idem-svc-1"))
        self.assertIsNone(find_idempotent_request(self.hospital, None))
        self.assertTrue(AuditEvent.objects.filter(event_type="BLOOD_REQUEST_CREATED", record_id=str(r.pk)).exists())

    def test_create_blood_request_requires_a_verified_hospital(self):
        self.hospital.profile.hospital_name = ""
        self.hospital.profile.save()
        with self.assertRaises(MissingHospitalAssignment):
            create_blood_request(self.new_request(), self.hospital)
        self.assertEqual(BloodRequest.objects.count(), 0)

    def test_hospital_may_cancel_only_its_own_pending_request(self):
        r = create_blood_request(self.new_request(), self.hospital)
        self.assertTrue(hospital_may_cancel(r, self.hospital))
        self.assertFalse(hospital_may_cancel(r, self.other_hospital))
        transition_request(r, BloodRequest.Status.APPROVED, self.staff)
        self.assertFalse(hospital_may_cancel(r, self.hospital))

    def test_request_action_names(self):
        for name, status in [("approve", "APPROVED"), ("APPROVED", "APPROVED"), ("reject", "REJECTED"),
                             (" cancel ", "CANCELLED"), ("CANCELLED", "CANCELLED")]:
            self.assertEqual(request_action_target(name), status)
        for name in ["", None, "fulfil", "delete"]:
            self.assertIsNone(request_action_target(name))

    def test_record_crossmatch_needs_active_reservation_and_known_outcome(self):
        donor = Donor.objects.create(
            branch=self.branch, full_name="Svc Donor", email="svcdonor@example.test", phone="9000000112",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365), weight_kg=70, blood_group="A+",
            consent_given=True,
        )
        donation = Donation.objects.create(
            donation_id="DON-SVC-1", donor=donor, collected_at=timezone.now(), collector=self.staff, branch=self.branch
        )
        BloodUnit.objects.create(
            unit_id="UNIT-SVC-1", donation=donation, blood_group="A+", component="Plasma", volume_ml=220,
            expiry_at=timezone.now() + timedelta(days=30), branch=self.branch, status=BloodUnit.Status.AVAILABLE,
        )
        r = create_blood_request(self.new_request(), self.hospital)
        transition_request(r, BloodRequest.Status.APPROVED, self.staff)
        reservation = reserve_units(r, self.staff)[0]

        with self.assertRaises(ValueError):
            record_crossmatch(reservation, "MAYBE", self.staff)
        record_crossmatch(reservation, "INCOMPATIBLE", self.staff)
        match = record_crossmatch(reservation, "COMPATIBLE", self.staff)  # a correction replaces the outcome
        self.assertEqual(CrossMatch.objects.get(reservation=reservation).outcome, "COMPATIBLE")
        self.assertEqual(match.recorded_by, self.staff)

        release_reservation(reservation, self.staff)
        reservation.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "active reservation"):
            record_crossmatch(reservation, "INCOMPATIBLE", self.staff)
        self.assertEqual(CrossMatch.objects.get(reservation=reservation).outcome, "COMPATIBLE")

    def test_register_donor_and_record_deferral(self):
        donor = register_donor(
            Donor(
                full_name="Registered Donor", email="registered@example.test", phone="9000000113",
                date_of_birth=timezone.localdate() - timedelta(days=30 * 365), weight_kg=70, blood_group="O-",
                consent_given=True,
            ),
            self.staff,
            branch=self.branch,
        )
        self.assertEqual(donor.branch, self.branch)
        self.assertTrue(AuditEvent.objects.filter(event_type="DONOR_REGISTERED", record_id=str(donor.pk)).exists())

        deferral = record_deferral(
            Deferral(donor=donor, reason="Recent tattoo", end_date=timezone.localdate() + timedelta(days=90)),
            self.staff,
        )
        self.assertEqual(deferral.approved_by, self.staff)
        event = AuditEvent.objects.get(event_type="DEFERRAL_RECORDED")
        self.assertIn("Recent tattoo", event.summary)
        self.assertEqual(event.branch, self.branch)


class SessionExpiryAndInvalidationTests(SprintOneSecurityFixture):
    """BB-53 [SEC-04] / TC-S05: idle sessions expire after 15 minutes; logout and deactivation end
    every session the account holds."""

    def setUp(self):
        super().setUp()
        self.staff = self.users["STAFF"]
        self.admin = self.users["ADMIN"]

    def signed_in_client(self, user):
        client = Client()
        self.assertEqual(self.sign_in(user, client=client).status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)
        return client

    def assert_signed_out(self, client):
        response = client.get("/inventory/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/login/"))

    def test_session_lifetime_is_fifteen_minutes_and_slides(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 900)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertEqual(settings.BBMS_IDLE_TIMEOUT_SECONDS, 900)

    def test_tc_s05_session_expires_after_fifteen_idle_minutes(self):
        client = self.signed_in_client(self.staff)
        session = client.session
        session["bbms_last_activity"] = int((timezone.now() - timedelta(minutes=16)).timestamp())
        session.save()
        self.assert_signed_out(client)
        event = AuditEvent.objects.filter(event_type="SESSION_ENDED").latest("created_at")
        self.assertEqual(event.actor, self.staff)
        self.assertIn("Idle for more than 15 minutes", event.summary)

    def test_activity_within_fifteen_minutes_keeps_the_session(self):
        client = self.signed_in_client(self.staff)
        session = client.session
        session["bbms_last_activity"] = int((timezone.now() - timedelta(minutes=14)).timestamp())
        session.save()
        self.assertEqual(client.get("/inventory/").status_code, 200)
        self.assertGreater(client.session["bbms_last_activity"], timezone.now().timestamp() - 60)

    def test_tc_s05_logout_ends_every_session_of_the_user(self):
        laptop = self.signed_in_client(self.staff)
        phone = self.signed_in_client(self.staff)
        other_user = self.signed_in_client(self.users["HOSPITAL"])
        laptop.post("/logout/")
        self.assert_signed_out(laptop)
        self.assert_signed_out(phone)
        self.assertEqual(other_user.get("/requests/").status_code, 200)
        event = AuditEvent.objects.filter(event_type="LOGOUT").latest("created_at")
        self.assertIn("1 other session(s) ended", event.summary)

    def test_tc_s05_deactivation_ends_every_session(self):
        laptop = self.signed_in_client(self.staff)
        phone = self.signed_in_client(self.staff)
        admin = self.signed_in_client(self.admin)
        response = admin.post(f"/accounts/{self.staff.profile.pk}/status/", {"active": "0"})
        self.assertIn(response.status_code, (200, 302))
        self.staff.profile.refresh_from_db()
        self.assertFalse(self.staff.profile.active)
        self.assert_signed_out(laptop)
        self.assert_signed_out(phone)

    def test_deactivation_outside_the_account_screen_still_signs_the_user_out(self):
        client = self.signed_in_client(self.staff)
        UserProfile.objects.filter(pk=self.staff.profile.pk).update(active=False)
        self.assert_signed_out(client)
        self.assertTrue(
            AuditEvent.objects.filter(event_type="SESSION_ENDED", summary="Account is deactivated").exists()
        )

    def test_signed_out_user_sees_a_neutral_message(self):
        client = self.signed_in_client(self.staff)
        session = client.session
        session["bbms_last_activity"] = int((timezone.now() - timedelta(hours=1)).timestamp())
        session.save()
        response = client.get("/inventory/", follow=True)
        self.assertContains(response, "Your session has ended. Please sign in again.")


class DonorHistoryAccessTests(TestCase):
    """BB-14 [FR-07] / TC-F04: a donor sees their own donations and appointments newest first and
    their next eligibility date, and cannot open another donor's record."""

    def setUp(self):
        self.branch = Branch.objects.create(name="History Branch", code="HIST")
        EligibilityRule.objects.create(
            min_age=18, max_age=65, min_weight_kg=50,
            minimum_interval_days=90, platelets_interval_days=14, plasma_interval_days=28,
        )
        ShelfLifeRule.objects.create(component="Packed Red Cells", days=35)
        self.staff = User.objects.create_user("histstaff@bbms.test", email="histstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.donor_a = self.make_donor("hist-a", "History Donor A")
        self.donor_b = self.make_donor("hist-b", "History Donor B")
        now = timezone.now()
        self.old = Donation.objects.create(
            donation_id="DON-HIST-OLD", donor=self.donor_a, collected_at=now - timedelta(days=200),
            collector=self.staff, branch=self.branch,
        )
        self.recent = Donation.objects.create(
            donation_id="DON-HIST-NEW", donor=self.donor_a, collected_at=now - timedelta(days=10),
            collector=self.staff, branch=self.branch,
        )
        Donation.objects.create(
            donation_id="DON-HIST-OTHER", donor=self.donor_b, collected_at=now - timedelta(days=5),
            collector=self.staff, branch=self.branch,
        )
        early = AppointmentSlot.objects.create(branch=self.branch, starts_at=now - timedelta(days=11), capacity=3)
        late = AppointmentSlot.objects.create(branch=self.branch, starts_at=now + timedelta(days=100), capacity=3)
        Appointment.objects.create(donor=self.donor_a, slot=early)
        Appointment.objects.create(donor=self.donor_a, slot=late)
        Appointment.objects.create(donor=self.donor_b, slot=late)
        self.client.force_login(self.donor_a.user)

    def make_donor(self, key, name):
        user = User.objects.create_user(f"{key}@bbms.test", email=f"{key}@bbms.test", password="pw")
        UserProfile.objects.create(user=user, role=UserProfile.Role.DONOR, branch=self.branch)
        return Donor.objects.create(
            user=user, branch=self.branch, full_name=name, email=f"{key}@bbms.test", phone="9000000123",
            date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, blood_group="O+", consent_given=True,
        )

    def test_tc_f04_donations_and_appointments_newest_first(self):
        response = self.client.get("/my-record/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d.donation_id for d in response.context["donations"]], ["DON-HIST-NEW", "DON-HIST-OLD"])
        starts = [ap.slot.starts_at for ap in response.context["appointments"]]
        self.assertEqual(starts, sorted(starts, reverse=True))
        self.assertEqual(len(starts), 2)

    def test_tc_f04_next_eligibility_date_uses_last_donation_and_interval(self):
        response = self.client.get("/my-record/")
        expected = timezone.localdate(self.recent.collected_at) + timedelta(days=90)
        self.assertEqual(response.context["next_eligible_date"], expected)
        self.assertTrue(response.context["interval_pending"])
        self.assertContains(response, expected.strftime("%d %b %Y"))

    def test_next_eligibility_date_is_today_once_interval_has_passed(self):
        Donation.objects.filter(donor=self.donor_a).update(collected_at=timezone.now() - timedelta(days=200))
        response = self.client.get("/my-record/")
        self.assertFalse(response.context["interval_pending"])
        self.assertEqual(response.context["next_eligible_date"], timezone.localdate())

    def test_tc_f04_other_donor_record_is_denied_and_logged(self):
        response = self.client.get(f"/my-record/?donor={self.donor_b.pk}")
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "DON-HIST-OTHER", status_code=403)
        event = AuditEvent.objects.get(event_type="UNAUTHORIZED_ACCESS")
        self.assertEqual(event.actor, self.donor_a.user)
        self.assertEqual(event.outcome, "DENIED")

    def test_own_record_never_lists_another_donors_rows(self):
        response = self.client.get("/my-record/")
        self.assertNotContains(response, "DON-HIST-OTHER")
        self.assertTrue(all(ap.donor_id == self.donor_a.pk for ap in response.context["appointments"]))

    def test_non_donor_roles_cannot_open_the_donor_portal(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/my-record/").status_code, 403)

    def test_recording_a_donation_updates_the_stored_eligibility_date(self):
        record_donation(self.donor_b, self.branch, self.staff, "O+", {"Packed Red Cells": 350})
        self.donor_b.refresh_from_db()
        self.assertEqual(self.donor_b.eligibility_date, timezone.localdate() + timedelta(days=90))


class AppointmentReminderAndRequestEmailTests(TestCase):
    """BB-36 [FR-29] / TC-F16: reminders 24 hours before an appointment and request-status emails,
    each recorded as SENT or FAILED."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Notify Branch", code="NOTE")
        self.staff = User.objects.create_user("notestaff@bbms.test", email="notestaff@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.hospital = User.objects.create_user("notehosp@bbms.test", email="notehosp@bbms.test", password="pw")
        UserProfile.objects.create(
            user=self.hospital, role=UserProfile.Role.HOSPITAL, branch=self.branch, hospital_name="Notify Hospital"
        )
        self.donor = Donor.objects.create(
            branch=self.branch, full_name="Reminder Donor", email="remind@example.test", phone="9000000321",
            date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, blood_group="A+", consent_given=True,
        )
        now = timezone.now()
        self.soon = Appointment.objects.create(
            donor=self.donor,
            slot=AppointmentSlot.objects.create(branch=self.branch, starts_at=now + timedelta(hours=20), capacity=5),
        )
        self.later = Appointment.objects.create(
            donor=self.donor,
            slot=AppointmentSlot.objects.create(branch=self.branch, starts_at=now + timedelta(hours=30), capacity=5),
        )
        self.cancelled = Appointment.objects.create(
            donor=self.donor, status=Appointment.Status.CANCELLED,
            slot=AppointmentSlot.objects.create(branch=self.branch, starts_at=now + timedelta(hours=5), capacity=5),
        )

    def reminders(self):
        return Notification.objects.filter(subject__startswith="Reminder:")

    def test_tc_f16_reminder_sent_for_appointments_within_24_hours_only(self):
        from django.core import mail
        sent, failed = send_appointment_reminders()
        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["remind@example.test"])
        note = self.reminders().get()
        self.assertEqual(note.status, "SENT")
        self.assertIsNotNone(note.sent_at)
        self.soon.refresh_from_db()
        self.later.refresh_from_db()
        self.assertIsNotNone(self.soon.reminder_sent_at)
        self.assertIsNone(self.later.reminder_sent_at)
        self.assertTrue(AuditEvent.objects.filter(event_type="APPOINTMENT_REMINDER", outcome="SUCCESS").exists())

    def test_reminder_is_sent_only_once(self):
        send_appointment_reminders()
        self.assertEqual(send_appointment_reminders(), (0, 0))
        self.assertEqual(self.reminders().count(), 1)

    def test_later_appointment_is_reminded_once_it_enters_the_window(self):
        send_appointment_reminders()
        send_appointment_reminders(now=timezone.now() + timedelta(hours=8))
        self.later.refresh_from_db()
        self.assertIsNotNone(self.later.reminder_sent_at)
        self.assertEqual(self.reminders().count(), 2)

    def test_tc_f16_failed_reminder_is_recorded_and_retried(self):
        with patch("bloodbank.services.send_mail", side_effect=OSError("SMTP down")):
            self.assertEqual(send_appointment_reminders(), (0, 1))
        self.assertEqual(self.reminders().get().status, "FAILED")
        self.soon.refresh_from_db()
        self.assertIsNone(self.soon.reminder_sent_at)
        self.assertTrue(AuditEvent.objects.filter(event_type="APPOINTMENT_REMINDER", outcome="FAILED").exists())
        self.assertEqual(send_appointment_reminders(), (1, 0))
        self.assertEqual(list(self.reminders().order_by("pk").values_list("status", flat=True)), ["FAILED", "SENT"])

    def test_management_command_runs_once(self):
        out = StringIO()
        call_command("send_reminders", stdout=out)
        self.assertIn("1 reminder(s) sent, 0 failed", out.getvalue())

    def make_request(self):
        return BloodRequest.objects.create(
            hospital="Notify Hospital", created_by=self.hospital, branch=self.branch, blood_group="A+",
            component="Packed Red Cells", quantity=1, priority="Urgent",
            required_by=timezone.now() + timedelta(days=1), clinical_reference="REF-NOTE",
        )

    def test_tc_f16_requester_is_emailed_on_every_status_change(self):
        request = self.make_request()
        transition_request(request, BloodRequest.Status.APPROVED, self.staff)
        transition_request(request, BloodRequest.Status.CANCELLED, self.staff)
        notes = Notification.objects.filter(recipient="notehosp@bbms.test", subject="Blood request status updated")
        self.assertEqual(notes.count(), 2)
        self.assertTrue(all(n.status == "SENT" for n in notes))
        self.assertIn("Cancelled", notes.order_by("pk").last().body)

    def test_tc_f16_failed_request_email_is_recorded_as_failed(self):
        request = self.make_request()
        with patch("bloodbank.services.send_mail", side_effect=OSError("SMTP down")):
            transition_request(request, BloodRequest.Status.APPROVED, self.staff)
        request.refresh_from_db()
        self.assertEqual(request.status, BloodRequest.Status.APPROVED)
        self.assertEqual(Notification.objects.get(recipient="notehosp@bbms.test").status, "FAILED")


class DonorLookupAndCheckInTests(TestCase):
    """BB-44 [NFR-07] / TC-N06: staff find a donor from one search box and check them in for
    today's appointment. The timed first-time-user trial itself is recorded in
    docs/test-results/TC-N06_NFR-07_usability_trial.md."""

    def setUp(self):
        self.branch = Branch.objects.create(name="Desk Branch", code="DESK")
        self.other_branch = Branch.objects.create(name="Far Branch", code="FAR")
        self.staff = User.objects.create_user("desk@bbms.test", email="desk@bbms.test", password="pw")
        UserProfile.objects.create(user=self.staff, role=UserProfile.Role.STAFF, branch=self.branch)
        self.ira = self.make_donor("Ira Desai", "ira.desai@example.test", "9845000111", "B+", self.branch)
        self.raj = self.make_donor("Raj Kumar", "raj.k@example.test", "9845000222", "O-", self.branch)
        self.far = self.make_donor("Ira Faraway", "far@example.test", "9845000333", "A+", self.other_branch)
        later_today = timezone.localtime().replace(hour=23, minute=0, second=0, microsecond=0)
        self.today_slot = AppointmentSlot.objects.create(branch=self.branch, starts_at=later_today, capacity=3)
        self.tomorrow_slot = AppointmentSlot.objects.create(
            branch=self.branch, starts_at=later_today + timedelta(days=1), capacity=3
        )
        self.appointment = Appointment.objects.create(donor=self.ira, slot=self.today_slot)
        self.tomorrow = Appointment.objects.create(donor=self.raj, slot=self.tomorrow_slot)
        self.client.force_login(self.staff)

    @staticmethod
    def make_donor(name, email, phone, group, branch):
        return Donor.objects.create(
            branch=branch, full_name=name, email=email, phone=phone, blood_group=group,
            date_of_birth=EligibilityRuleUnitTests.birthday(30), weight_kg=70, consent_given=True,
        )

    def found(self, query):
        return [d.full_name for d in self.client.get("/donors/", {"q": query}).context["donors"]]

    def test_tc_n06_search_by_name_email_phone_and_donor_number(self):
        self.assertEqual(self.found("ira"), ["Ira Desai"])
        self.assertEqual(self.found("RAJ.K@EXAMPLE"), ["Raj Kumar"])
        self.assertEqual(self.found("98450-00222"), ["Raj Kumar"])
        self.assertEqual(self.found(f"#{self.ira.pk}"), ["Ira Desai"])
        self.assertEqual(self.found("O-"), ["Raj Kumar"])

    def test_search_stays_inside_the_staff_branch(self):
        self.assertNotIn("Ira Faraway", self.found("Ira"))

    def test_search_result_shows_todays_appointment_with_check_in(self):
        response = self.client.get("/donors/", {"q": "Ira"})
        self.assertEqual(response.context["donors"][0].next_appointment, self.appointment)
        self.assertContains(response, f'action="/appointments/{self.appointment.pk}/check-in/"')
        self.assertContains(response, "matching")

    def test_no_match_says_so(self):
        self.assertContains(self.client.get("/donors/", {"q": "Nobody"}), "No donor found")

    def test_todays_appointments_panel_lists_only_today_at_this_branch(self):
        today = list(self.client.get("/donors/").context["today"])
        self.assertEqual(today, [self.appointment])

    def test_tc_n06_check_in_marks_arrival_and_is_audited(self):
        response = self.client.post(f"/appointments/{self.appointment.pk}/check-in/", {"next": "/donors/?q=Ira"})
        self.assertRedirects(response, "/donors/?q=Ira", fetch_redirect_response=False)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.CHECKED_IN)
        self.assertEqual(self.appointment.checked_in_by, self.staff)
        self.assertIsNotNone(self.appointment.checked_in_at)
        event = AuditEvent.objects.get(event_type="APPOINTMENT_CHECKED_IN")
        self.assertEqual(event.record_id, str(self.appointment.pk))
        self.assertContains(self.client.get("/donors/"), "Checked in")

    def test_only_todays_booked_appointments_can_be_checked_in(self):
        self.client.post(f"/appointments/{self.tomorrow.pk}/check-in/")
        self.tomorrow.refresh_from_db()
        self.assertEqual(self.tomorrow.status, Appointment.Status.BOOKED)
        self.client.post(f"/appointments/{self.appointment.pk}/check-in/")
        response = self.client.post(f"/appointments/{self.appointment.pk}/check-in/", follow=True)
        self.assertContains(response, "Only a booked appointment can be checked in.")

    def test_checked_in_donor_still_holds_a_place_in_the_slot(self):
        self.today_slot.capacity = 1
        self.today_slot.save()
        check_in_appointment(self.appointment, self.staff)
        with self.assertRaisesMessage(AppointmentError, "This slot is full."):
            book_appointment(self.raj, self.today_slot, self.staff)

    def test_other_roles_and_branches_cannot_check_in(self):
        far_staff = User.objects.create_user("farstaff@bbms.test", email="farstaff@bbms.test", password="pw")
        UserProfile.objects.create(user=far_staff, role=UserProfile.Role.STAFF, branch=self.other_branch)
        self.client.force_login(far_staff)
        self.assertEqual(self.client.post(f"/appointments/{self.appointment.pk}/check-in/").status_code, 403)
        donor_user = User.objects.create_user("deskdonor@bbms.test", email="deskdonor@bbms.test", password="pw")
        UserProfile.objects.create(user=donor_user, role=UserProfile.Role.DONOR)
        self.client.force_login(donor_user)
        self.assertEqual(self.client.post(f"/appointments/{self.appointment.pk}/check-in/").status_code, 403)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.BOOKED)

    def test_usability_trial_procedure_is_documented(self):
        trial = Path(__file__).resolve().parent.parent.parent / "docs" / "test-results" / "TC-N06_NFR-07_usability_trial.md"
        text = trial.read_text(encoding="utf-8")
        self.assertIn("within **5 minutes**", text)
        self.assertIn("Task card", text)

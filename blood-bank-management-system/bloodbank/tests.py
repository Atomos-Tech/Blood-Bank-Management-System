from datetime import timedelta
import importlib
from io import StringIO
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from .models import (
    Appointment,
    AppointmentSlot,
    AuditEvent,
    BloodRequest,
    BloodUnit,
    Branch,
    CrossMatch,
    Deferral,
    Donation,
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
    calculate_expiry,
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
        self.assertEqual(self.client.get("/inventory/").status_code, 403)
        self.assertEqual(self.client.post(f"/requests/{self.pending.pk}/approve/").status_code, 403)

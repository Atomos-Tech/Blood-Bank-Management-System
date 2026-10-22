from datetime import timedelta
import importlib
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
    check_stock_and_expiry_alerts,
    eligibility,
    issue_reservation,
    release_reservation,
    release_screened_unit,
    reserve_units,
    transition_request,
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

from datetime import timedelta
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from .models import (BloodRequest, BloodUnit, Branch, CrossMatch, Donation, Donor, EligibilityRule, TestType, UserProfile)
from .services import eligibility, issue_reservation, release_screened_unit, reserve_units

class WorkflowTests(TestCase):
    def setUp(self):
        self.branch=Branch.objects.create(name="Main",code="MAIN")
        EligibilityRule.objects.create(min_age=18,max_age=65,min_weight_kg=50,minimum_interval_days=90)
        self.staff=User.objects.create_user("staff@example.test",password="very-safe-demo-password")
        UserProfile.objects.create(user=self.staff,role=UserProfile.Role.STAFF,branch=self.branch)
        self.donor=Donor.objects.create(full_name="Donor One",email="donor@example.test",phone="9000000000",date_of_birth=timezone.localdate()-timedelta(days=25*365),weight_kg=60,blood_group="O+",consent_given=True)
    def test_eligibility_requires_consent_and_rule_pass(self):
        ok,reasons=eligibility(self.donor); self.assertTrue(ok); self.assertEqual(reasons,[])
    def test_screening_requires_complete_acceptable_panel(self):
        donation=Donation.objects.create(donation_id="D-1",donor=self.donor,collected_at=timezone.now(),collector=self.staff,branch=self.branch)
        unit=BloodUnit.objects.create(unit_id="U-1",donation=donation,blood_group="O+",component="Packed Red Cells",volume_ml=350,expiry_at=timezone.now()+timedelta(days=20),branch=self.branch)
        test=TestType.objects.create(name="HIV")
        from .models import LabResult
        LabResult.objects.create(unit=unit,test_type=test,outcome=LabResult.Outcome.ACCEPTABLE,recorded_by=self.staff)
        self.assertEqual(release_screened_unit(unit,self.staff),BloodUnit.Status.AVAILABLE)
    def test_reservation_and_issue_requires_crossmatch(self):
        donation=Donation.objects.create(donation_id="D-2",donor=self.donor,collected_at=timezone.now(),collector=self.staff,branch=self.branch)
        unit=BloodUnit.objects.create(unit_id="U-2",donation=donation,blood_group="O+",component="Packed Red Cells",volume_ml=350,expiry_at=timezone.now()+timedelta(days=20),branch=self.branch,status=BloodUnit.Status.AVAILABLE)
        req=BloodRequest.objects.create(hospital="City",created_by=self.staff,branch=self.branch,blood_group="O+",component="Packed Red Cells",quantity=1,priority="Urgent",required_by=timezone.now()+timedelta(days=1),clinical_reference="REF-1",status=BloodRequest.Status.APPROVED)
        reservation=reserve_units(req,self.staff)[0]
        with self.assertRaises(ValueError): issue_reservation(reservation,self.staff)
        CrossMatch.objects.create(reservation=reservation,outcome="COMPATIBLE",recorded_by=self.staff)
        issue_reservation(reservation,self.staff); unit.refresh_from_db(); self.assertEqual(unit.status,BloodUnit.Status.ISSUED)

    def test_donor_cannot_open_staff_directory(self):
        donor_user=User.objects.create_user("donor@example.test",password="very-safe-demo-password")
        UserProfile.objects.create(user=donor_user,role=UserProfile.Role.DONOR,branch=self.branch)
        self.client.force_login(donor_user)
        self.assertEqual(self.client.get("/donors/").status_code,403)

    def test_authentication_failure_is_generic(self):
        response=self.client.post("/login/",{"email":"unknown@example.test","password":"wrong-password"},follow=True)
        self.assertContains(response,"Invalid email or password.")

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

class Branch(models.Model):
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=16, unique=True)
    timezone_name = models.CharField(max_length=64, default="UTC")
    low_stock_threshold = models.PositiveIntegerField(default=5)
    near_expiry_days = models.PositiveIntegerField(default=7)
    def __str__(self): return f"{self.code} - {self.name}"

class UserProfile(models.Model):
    class Role(models.TextChoices):
        DONOR = "DONOR", "Donor"; STAFF = "STAFF", "Blood Bank Staff"; HOSPITAL = "HOSPITAL", "Hospital Staff"; ADMIN = "ADMIN", "System Administrator"
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.DONOR)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL)
    hospital_name = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    def __str__(self): return f"{self.user.email} ({self.get_role_display()})"

class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL)
    event_type = models.CharField(max_length=100)
    record_type = models.CharField(max_length=50)
    record_id = models.CharField(max_length=64)
    outcome = models.CharField(max_length=20, default="SUCCESS")
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ["-created_at"]

class Donor(models.Model):
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="donor_profile")
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20)
    date_of_birth = models.DateField()
    weight_kg = models.DecimalField(max_digits=5, decimal_places=1)
    blood_group = models.CharField(max_length=3)
    medical_history = models.TextField(blank=True)
    consent_given = models.BooleanField(default=False)
    eligibility_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class Deferral(models.Model):
    donor = models.ForeignKey(Donor, on_delete=models.PROTECT, related_name="deferrals")
    reason = models.CharField(max_length=255)
    permanent = models.BooleanField(default=False)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT)
    def active(self): return self.permanent or self.end_date is None or self.end_date >= timezone.localdate()

class EligibilityRule(models.Model):
    min_age = models.PositiveIntegerField(default=18)
    max_age = models.PositiveIntegerField(default=65)
    min_weight_kg = models.DecimalField(max_digits=5, decimal_places=1, default=50)
    minimum_interval_days = models.PositiveIntegerField(default=90)
    active = models.BooleanField(default=True)

class AppointmentSlot(models.Model):
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    class Meta: unique_together = [("branch", "starts_at")]
    @property
    def booked_count(self): return self.appointments.filter(status=Appointment.Status.BOOKED).count()
    @property
    def available(self): return self.capacity - self.booked_count

class Appointment(models.Model):
    class Status(models.TextChoices): BOOKED="BOOKED","Booked"; CANCELLED="CANCELLED","Cancelled"
    donor = models.ForeignKey(Donor, on_delete=models.PROTECT, related_name="appointments")
    slot = models.ForeignKey(AppointmentSlot, on_delete=models.PROTECT, related_name="appointments")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.BOOKED)
    created_at = models.DateTimeField(auto_now_add=True)

class Donation(models.Model):
    donation_id = models.CharField(max_length=32, unique=True)
    donor = models.ForeignKey(Donor, on_delete=models.PROTECT, related_name="donations")
    collected_at = models.DateTimeField()
    collector = models.ForeignKey(User, on_delete=models.PROTECT)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)

class ShelfLifeRule(models.Model):
    component = models.CharField(max_length=64, unique=True)
    days = models.PositiveIntegerField()

class BloodUnit(models.Model):
    class Status(models.TextChoices): COLLECTED="COLLECTED","Collected"; TESTING="TESTING","Testing"; AVAILABLE="AVAILABLE","Available"; RESERVED="RESERVED","Reserved"; ISSUED="ISSUED","Issued"; QUARANTINED="QUARANTINED","Quarantined"; EXPIRED="EXPIRED","Expired"; DISCARDED="DISCARDED","Discarded"
    unit_id = models.CharField(max_length=32, unique=True)
    donation = models.ForeignKey(Donation, on_delete=models.PROTECT, related_name="units")
    blood_group = models.CharField(max_length=3)
    component = models.CharField(max_length=64)
    volume_ml = models.PositiveIntegerField()
    expiry_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COLLECTED)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)

class TestType(models.Model):
    name = models.CharField(max_length=64, unique=True)
    required = models.BooleanField(default=True)

class LabResult(models.Model):
    class Outcome(models.TextChoices): ACCEPTABLE="ACCEPTABLE","Acceptable"; REACTIVE="REACTIVE","Reactive"; INDETERMINATE="INDETERMINATE","Indeterminate"; INCOMPLETE="INCOMPLETE","Incomplete"
    unit = models.ForeignKey(BloodUnit, on_delete=models.PROTECT, related_name="lab_results")
    test_type = models.ForeignKey(TestType, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)
    class Meta: unique_together = [("unit", "test_type")]

class BloodRequest(models.Model):
    class Status(models.TextChoices): PENDING="PENDING","Pending"; APPROVED="APPROVED","Approved"; PARTIAL="PARTIAL","Partially Fulfilled"; FULFILLED="FULFILLED","Fulfilled"; REJECTED="REJECTED","Rejected"; CANCELLED="CANCELLED","Cancelled"
    hospital = models.CharField(max_length=120)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    blood_group = models.CharField(max_length=3)
    component = models.CharField(max_length=64)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    priority = models.CharField(max_length=16)
    required_by = models.DateTimeField()
    clinical_reference = models.CharField(max_length=120)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

class RequestTransition(models.Model):
    request = models.ForeignKey(BloodRequest, on_delete=models.PROTECT, related_name="transitions")
    from_status = models.CharField(max_length=16)
    to_status = models.CharField(max_length=16)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

class Reservation(models.Model):
    unit = models.OneToOneField(BloodUnit, on_delete=models.PROTECT, related_name="reservation")
    request = models.ForeignKey(BloodRequest, on_delete=models.PROTECT, related_name="reservations")
    reserved_by = models.ForeignKey(User, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

class CrossMatch(models.Model):
    reservation = models.OneToOneField(Reservation, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=16, choices=[("COMPATIBLE","Compatible"),("INCOMPATIBLE","Incompatible")])
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

class Issue(models.Model):
    reservation = models.OneToOneField(Reservation, on_delete=models.PROTECT)
    issued_by = models.ForeignKey(User, on_delete=models.PROTECT)
    issued_at = models.DateTimeField(auto_now_add=True)

class Notification(models.Model):
    recipient = models.EmailField()
    subject = models.CharField(max_length=180)
    body = models.TextField()
    status = models.CharField(max_length=16, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from datetime import timedelta


class User(AbstractUser):
    class Role(models.TextChoices):
        DONOR = "DONOR", "Donor"
        STAFF = "STAFF", "Blood Bank Staff"
        HOSPITAL = "HOSPITAL", "Hospital / Recipient"
        ADMIN = "ADMIN", "System Administrator"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.DONOR)
    phone = models.CharField(max_length=15, blank=True)

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class Branch(models.Model):
    name = models.CharField(max_length=120)
    city = models.CharField(max_length=80)
    address = models.CharField(max_length=255, blank=True)
    contact_phone = models.CharField(max_length=15, blank=True)

    def __str__(self):
        return f"{self.name} — {self.city}"


BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
BLOOD_GROUP_CHOICES = [(g, g) for g in BLOOD_GROUPS]

COMPATIBILITY = {
    "O-": ["O-"],
    "O+": ["O+", "O-"],
    "A-": ["A-", "O-"],
    "A+": ["A+", "A-", "O+", "O-"],
    "B-": ["B-", "O-"],
    "B+": ["B+", "B-", "O+", "O-"],
    "AB-": ["AB-", "A-", "B-", "O-"],
    "AB+": ["AB+", "AB-", "A+", "A-", "B+", "B-", "O+", "O-"],
}


class Donor(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="donor_profile")
    date_of_birth = models.DateField()
    weight_kg = models.DecimalField(max_digits=5, decimal_places=1)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUP_CHOICES)
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True)
    last_donation_date = models.DateField(null=True, blank=True)
    medical_notes = models.TextField(blank=True)

    def age(self):
        today = timezone.now().date()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )

    def is_eligible(self):
        settings = SystemSetting.load()
        reasons = []
        if self.age() < settings.min_donor_age or self.age() > settings.max_donor_age:
            reasons.append(f"Age must be between {settings.min_donor_age} and {settings.max_donor_age}")
        if self.weight_kg < settings.min_weight_kg:
            reasons.append(f"Weight must be at least {settings.min_weight_kg}kg")
        if self.last_donation_date:
            gap = (timezone.now().date() - self.last_donation_date).days
            if gap < settings.min_donation_gap_days:
                reasons.append(f"Must wait {settings.min_donation_gap_days - gap} more day(s) since last donation")
        return (len(reasons) == 0, reasons)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.blood_group})"


class Donation(models.Model):
    class TestResult(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"

    donor = models.ForeignKey(Donor, on_delete=models.CASCADE, related_name="donations")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True)
    donation_date = models.DateField(default=timezone.now)
    volume_ml = models.PositiveIntegerField(default=450)
    test_result = models.CharField(max_length=10, choices=TestResult.choices, default=TestResult.PENDING)
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="recorded_donations")

    def __str__(self):
        return f"Donation #{self.id} — {self.donor} on {self.donation_date}"

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            self.donor.last_donation_date = self.donation_date
            self.donor.save()


class BloodUnit(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        RESERVED = "RESERVED", "Reserved"
        ISSUED = "ISSUED", "Issued"
        EXPIRED = "EXPIRED", "Expired"
        QUARANTINED = "QUARANTINED", "Quarantined"

    donation = models.OneToOneField(Donation, on_delete=models.CASCADE, related_name="unit")
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUP_CHOICES)
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True)
    collection_date = models.DateField()
    expiry_date = models.DateField()
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.QUARANTINED)

    def refresh_status(self):
        if self.donation.test_result == Donation.TestResult.FAILED:
            self.status = self.Status.QUARANTINED
        elif timezone.now().date() > self.expiry_date and self.status not in (self.Status.ISSUED,):
            self.status = self.Status.EXPIRED
        elif self.donation.test_result == Donation.TestResult.PASSED and self.status == self.Status.QUARANTINED:
            self.status = self.Status.AVAILABLE
        self.save()

    def __str__(self):
        return f"Unit #{self.id} — {self.blood_group} ({self.status})"


class Appointment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        MISSED = "MISSED", "Missed"

    donor = models.ForeignKey(Donor, on_delete=models.CASCADE, related_name="appointments")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True)
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_date", "scheduled_time"]

    def __str__(self):
        return f"Appointment — {self.donor} on {self.scheduled_date} {self.scheduled_time}"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=255)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    link_name = models.CharField(max_length=100, blank=True)  # url name to redirect to

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"To {self.user}: {self.message[:40]}"


def notify(user, message, link_name=""):
    Notification.objects.create(user=user, message=message, link_name=link_name)


class SystemSetting(models.Model):
    """Singleton-style configuration editable by the System Administrator."""
    min_donor_age = models.PositiveIntegerField(default=18)
    max_donor_age = models.PositiveIntegerField(default=65)
    min_weight_kg = models.PositiveIntegerField(default=50)
    min_donation_gap_days = models.PositiveIntegerField(default=90)
    unit_shelf_life_days = models.PositiveIntegerField(default=42)
    low_stock_threshold = models.PositiveIntegerField(default=5)

    def __str__(self):
        return "System Settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BloodRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        ISSUED = "ISSUED", "Issued"
        REJECTED = "REJECTED", "Rejected"

    hospital = models.ForeignKey(User, on_delete=models.CASCADE, related_name="requests")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUP_CHOICES)
    units_needed = models.PositiveIntegerField(default=1)
    patient_name = models.CharField(max_length=120)
    is_emergency = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    issued_units = models.ManyToManyField(BloodUnit, blank=True, related_name="issued_to_request")

    def __str__(self):
        return f"Request #{self.id} — {self.blood_group} x{self.units_needed} ({self.status})"

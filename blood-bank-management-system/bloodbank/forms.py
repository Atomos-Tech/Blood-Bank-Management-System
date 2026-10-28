import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .models import (BLOOD_GROUPS, COMPONENT_CODES, DonationType, SCREENING_QUESTIONS, Appointment, AppointmentSlot, BloodRequest, BloodUnit, Branch, Deferral, Donor,
                     EligibilityRule, LabResult, Reservation, ShelfLifeRule, UserProfile)


PHONE_PATTERN = re.compile(r"^\+?\d{10,15}$")
YES_NO = [("no", "No"), ("yes", "Yes")]
# FR-26 / NFR-09: operational roles work inside one branch, so a branch is mandatory for them.
BRANCH_SCOPED_ROLES = {UserProfile.Role.STAFF, UserProfile.Role.HOSPITAL}


class DonorForm(forms.ModelForm):
    """FR-01: identity, contact, consent and medical-history fields are all validated before saving."""

    phone = forms.CharField(max_length=30, help_text="10 to 15 digits; spaces, dashes and a leading + are allowed.")
    blood_group = forms.ChoiceField(choices=[("", "Select blood group")] + [(g, g) for g in BLOOD_GROUPS])
    medical_history = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Medical history",
        error_messages={"required": "Medical history is required. Write \"None\" if there is nothing to declare."},
        help_text="Past illnesses, conditions or medication. Write \"None\" if there is nothing to declare.",
    )
    consent_given = forms.BooleanField(
        required=True,
        label="Donor consents to donation and to the storage of this data",
        error_messages={"required": "Donor consent is required to register."},
    )

    class Meta:
        model = Donor
        fields = [
            "full_name",
            "email",
            "phone",
            "date_of_birth",
            "weight_kg",
            "blood_group",
            "medical_history",
            "consent_given",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, question in SCREENING_QUESTIONS:
            self.fields[f"screening_{key}"] = forms.ChoiceField(
                choices=YES_NO, widget=forms.RadioSelect, label=question,
                error_messages={"required": "Answer every screening question."},
            )
            if self.instance.pk and key in self.instance.screening_responses:
                self.initial[f"screening_{key}"] = "yes" if self.instance.screening_responses[key] else "no"
        self.fields["date_of_birth"].widget = forms.DateInput(attrs={"type": "date"})

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if len(name) < 2:
            raise forms.ValidationError("Enter the donor's full name.")
        return name

    def clean_phone(self):
        phone = re.sub(r"[\s-]", "", self.cleaned_data.get("phone") or "")
        if not PHONE_PATTERN.match(phone):
            raise forms.ValidationError("Enter a valid phone number of 10 to 15 digits.")
        return phone

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get("date_of_birth")
        if dob and dob > timezone.localdate():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        return dob

    def clean_weight_kg(self):
        weight = self.cleaned_data.get("weight_kg")
        if weight is not None and not 0 < weight < 300:
            raise forms.ValidationError("Enter a weight between 1 and 299 kg.")
        return weight

    def clean_medical_history(self):
        history = (self.cleaned_data.get("medical_history") or "").strip()
        if not history:
            raise forms.ValidationError("Medical history is required. Write \"None\" if there is nothing to declare.")
        return history

    def clean(self):
        cleaned = super().clean()
        answers = {}
        for key, _ in SCREENING_QUESTIONS:
            answer = cleaned.get(f"screening_{key}")
            if answer:
                answers[key] = answer == "yes"
        if len(answers) == len(SCREENING_QUESTIONS):
            self.instance.screening_responses = answers
        return cleaned

    def clean_email(self):
        # FR-02: one donor profile per email address, compared case-insensitively.
        email = (self.cleaned_data.get("email") or "").strip().lower()
        duplicates = Donor.objects.filter(email__iexact=email)
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError("A donor profile is already registered with this email address.")
        return email


class SlotChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, slot):
        local = timezone.localtime(slot.starts_at)
        return f"{slot.branch.name} · {local:%a %d %b %Y, %H:%M} · {slot.free} of {slot.capacity} places free"


class AppointmentForm(forms.Form):
    """FR-05: only future slots that still have free capacity are offered, optionally filtered by branch."""

    slot = SlotChoiceField(queryset=AppointmentSlot.objects.none(), empty_label="Select a time slot")

    def __init__(self, *args, branch=None, exclude_slot=None, **kwargs):
        super().__init__(*args, **kwargs)
        slots = (
            AppointmentSlot.objects.select_related("branch")
            .filter(starts_at__gt=timezone.now())
            .annotate(booked=models.Count("appointments", filter=models.Q(appointments__status=Appointment.Status.BOOKED)))
            .annotate(free=models.F("capacity") - models.F("booked"))
            .filter(free__gt=0)
            .order_by("starts_at")
        )
        if branch is not None:
            slots = slots.filter(branch=branch)
        if exclude_slot is not None:
            slots = slots.exclude(pk=exclude_slot.pk)
        self.fields["slot"].queryset = slots


class BranchFilterForm(forms.Form):
    branch = forms.ModelChoiceField(queryset=Branch.objects.order_by("name"), required=False, empty_label="All branches")


class DeferralForm(forms.ModelForm):
    """FR-08: reason, start date and (for temporary deferrals) end date; the approver is set by the view."""

    class Meta:
        model = Deferral
        fields = ["donor", "reason", "permanent", "start_date", "end_date"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"permanent": "Permanent deferral"}

    def __init__(self, *args, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        donors = Donor.objects.order_by("full_name")
        if branch is not None:
            donors = donors.filter(models.Q(branch=branch) | models.Q(branch__isnull=True))
        self.fields["donor"].queryset = donors

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("A deferral reason is required.")
        return reason

    def clean(self):
        cleaned = super().clean()
        permanent, start, end = cleaned.get("permanent"), cleaned.get("start_date"), cleaned.get("end_date")
        if permanent:
            cleaned["end_date"] = None  # permanent deferrals never end
        elif not end:
            self.add_error("end_date", "Temporary deferrals require an end date.")
        elif start and end < start:
            self.add_error("end_date", "End date cannot be before the start date.")
        return cleaned


class BloodRequestForm(forms.ModelForm):
    class Meta:
        model = BloodRequest
        fields = [
            "branch",
            "blood_group",
            "component",
            "quantity",
            "priority",
            "required_by",
            "clinical_reference",
        ]

    def __init__(self, *args, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        if branch is not None:
            self.fields["branch"].initial = branch

    def clean_quantity(self):
        qty = self.cleaned_data.get("quantity")
        if qty is None or qty < 1:
            raise forms.ValidationError("Quantity must be at least 1.")
        return qty

    def clean_required_by(self):
        req_by = self.cleaned_data.get("required_by")
        if req_by and req_by <= timezone.now():
            raise forms.ValidationError("Required-by time must be in the future.")
        return req_by


class LabResultForm(forms.ModelForm):
    class Meta:
        model = LabResult
        fields = ["test_type", "outcome"]


class CrossMatchForm(forms.Form):
    outcome = forms.ChoiceField(choices=[("COMPATIBLE", "Compatible"), ("INCOMPATIBLE", "Incompatible")])


class DonationForm(forms.Form):
    donor = forms.ModelChoiceField(queryset=Donor.objects.all())
    branch = forms.ModelChoiceField(queryset=Branch.objects.all())
    blood_group = forms.ChoiceField(choices=[(x, x) for x in BLOOD_GROUPS])
    donation_type = forms.ChoiceField(choices=DonationType.choices, initial=DonationType.WHOLE_BLOOD, required=False)

    def clean_donation_type(self):
        return self.cleaned_data.get("donation_type") or DonationType.WHOLE_BLOOD

    def __init__(self, *args, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        if branch is not None:
            self.fields["branch"].initial = branch
        # One optional volume per component; each component with a volume becomes its own unit (FR-09).
        for component, code in COMPONENT_CODES.items():
            self.fields[f"volume_{code.lower()}"] = forms.IntegerField(
                min_value=1, required=False, label=f"{component} volume (ml)"
            )

    def clean(self):
        cleaned = super().clean()
        components = {}
        for component, code in COMPONENT_CODES.items():
            volume = cleaned.get(f"volume_{code.lower()}")
            if volume:
                components[component] = volume
        if not components:
            raise forms.ValidationError("Enter a volume for at least one component unit.")
        cleaned["components"] = components
        return cleaned


def validate_role_branch(form, cleaned):
    """FR-26 / NFR-09: an operational role is always scoped to exactly one branch."""
    if cleaned.get("role") in BRANCH_SCOPED_ROLES and cleaned.get("branch") is None:
        form.add_error("branch", "Blood bank staff and hospital staff must belong to one branch.")


class UserAccountForm(forms.Form):
    """FR-26: the administrator creates an account with exactly one role and one branch."""

    full_name = forms.CharField(max_length=120, label="Full name")
    email = forms.EmailField(label="Email address (used to sign in)")
    role = forms.ChoiceField(choices=UserProfile.Role.choices, label="Role (exactly one)")
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.order_by("name"), required=False,
        empty_label="No branch (donor or administrator)",
    )
    hospital_name = forms.CharField(max_length=120, required=False, label="Hospital (hospital staff only)")
    password = forms.CharField(
        widget=forms.PasswordInput, label="Initial password",
        help_text="Stored only as an adaptive hash. It is never written to the audit trail.",
    )

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if len(name) < 2:
            raise forms.ValidationError("Enter the account holder's full name.")
        return name

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("An account already uses this email address.")
        return email

    def clean_password(self):
        # SEC-02: the password is checked against the configured AUTH_PASSWORD_VALIDATORS before
        # it is hashed, and the cleaned value is passed straight to create_user.
        password = self.cleaned_data.get("password") or ""
        try:
            validate_password(password)
        except ValidationError as error:
            raise forms.ValidationError(error.messages)
        return password

    def clean(self):
        cleaned = super().clean()
        validate_role_branch(self, cleaned)
        if cleaned.get("role") == UserProfile.Role.HOSPITAL and not (cleaned.get("hospital_name") or "").strip():
            self.add_error("hospital_name", "Name the hospital this account belongs to.")
        return cleaned


class UserRoleForm(forms.Form):
    """FR-26: move an existing account to exactly one other role, and to the branch it works in."""

    role = forms.ChoiceField(choices=UserProfile.Role.choices)
    branch = forms.ModelChoiceField(queryset=Branch.objects.order_by("name"), required=False)

    def clean(self):
        cleaned = super().clean()
        validate_role_branch(self, cleaned)
        return cleaned


class BranchConfigForm(forms.ModelForm):
    """FR-27: the per-branch thresholds that drive the low-stock and near-expiry alerts (FR-18)."""

    class Meta:
        model = Branch
        fields = ["name", "timezone_name", "low_stock_threshold", "near_expiry_days"]

    def clean_timezone_name(self):
        name = (self.cleaned_data.get("timezone_name") or "").strip() or "UTC"
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise forms.ValidationError("Enter a valid IANA time zone, for example Asia/Kolkata.")
        return name

    def clean_near_expiry_days(self):
        days = self.cleaned_data.get("near_expiry_days")
        if days is not None and days < 1:
            raise forms.ValidationError("The near-expiry window must be at least one day.")
        return days


class EligibilityRuleForm(forms.ModelForm):
    """FR-27: the age, weight and interval rules the eligibility engine reads (FR-03)."""

    class Meta:
        model = EligibilityRule
        fields = [
            "min_age", "max_age", "min_weight_kg",
            "minimum_interval_days", "platelets_interval_days", "plasma_interval_days",
        ]

    def clean_min_weight_kg(self):
        weight = self.cleaned_data.get("min_weight_kg")
        if weight is not None and weight <= 0:
            raise forms.ValidationError("The minimum weight must be greater than zero.")
        return weight

    def clean(self):
        cleaned = super().clean()
        min_age, max_age = cleaned.get("min_age"), cleaned.get("max_age")
        if min_age is not None and max_age is not None and min_age >= max_age:
            self.add_error("max_age", "The maximum age must be greater than the minimum age.")
        for field in ["minimum_interval_days", "platelets_interval_days", "plasma_interval_days"]:
            value = cleaned.get(field)
            if value is not None and value < 1:
                self.add_error(field, "A donation interval must be at least one day.")
        return cleaned


class ShelfLifeRuleForm(forms.ModelForm):
    """FR-27: how long each component stays usable, which sets every new unit's expiry (FR-15)."""

    class Meta:
        model = ShelfLifeRule
        fields = ["component", "days"]

    def clean_days(self):
        days = self.cleaned_data.get("days")
        if days is not None and days < 1:
            raise forms.ValidationError("A shelf life must be at least one day.")
        return days


class AppointmentSlotForm(forms.ModelForm):
    """FR-27: donation slots, whose capacity FR-06 enforces against concurrent bookings."""

    class Meta:
        model = AppointmentSlot
        fields = ["branch", "starts_at", "capacity"]
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean_capacity(self):
        capacity = self.cleaned_data.get("capacity")
        if capacity is None or capacity < 1:
            raise forms.ValidationError("A slot must have capacity for at least one donor.")
        return capacity

    def clean_starts_at(self):
        starts_at = self.cleaned_data.get("starts_at")
        if starts_at and starts_at <= timezone.now():
            raise forms.ValidationError("A new slot must start in the future.")
        return starts_at


class ReportFilterForm(forms.Form):
    """FR-28: pick one of the five report types and narrow it by date range and branch."""

    REPORT_CHOICES = [
        ("inventory", "Inventory"),
        ("expiry", "Expiry"),
        ("donation", "Donations"),
        ("request", "Blood requests"),
        ("audit", "Audit trail"),
    ]

    report = forms.ChoiceField(choices=REPORT_CHOICES, required=False, label="Report")
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.order_by("name"), required=False, empty_label="All branches",
    )

    def __init__(self, *args, allowed_reports=None, lock_branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        if allowed_reports is not None:
            self.fields["report"].choices = [c for c in self.REPORT_CHOICES if c[0] in allowed_reports]
        if lock_branch is not None:
            # NFR-09: operational users never choose a branch other than their own.
            self.fields["branch"].queryset = Branch.objects.filter(pk=lock_branch.pk)
            self.fields["branch"].empty_label = None
            self.fields["branch"].initial = lock_branch

    def clean_report(self):
        report = self.cleaned_data.get("report")
        return report or self.fields["report"].choices[0][0]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        return cleaned

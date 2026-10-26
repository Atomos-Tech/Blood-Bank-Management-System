import re

from django import forms
from django.utils import timezone
from .models import (BLOOD_GROUPS, COMPONENT_CODES, DonationType, SCREENING_QUESTIONS, Appointment, AppointmentSlot, BloodRequest, BloodUnit, Branch, Deferral, Donor,
                     LabResult, Reservation)


PHONE_PATTERN = re.compile(r"^\+?\d{10,15}$")
YES_NO = [("no", "No"), ("yes", "Yes")]


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


class AppointmentForm(forms.Form):
    slot = forms.ModelChoiceField(queryset=AppointmentSlot.objects.none())

    def __init__(self, *args, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slot"].queryset = AppointmentSlot.objects.all().order_by("starts_at")


class DeferralForm(forms.ModelForm):
    class Meta:
        model = Deferral
        fields = ["donor", "reason", "permanent", "start_date", "end_date"]


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

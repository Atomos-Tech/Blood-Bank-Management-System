from django import forms
from django.utils import timezone
from .models import (COMPONENT_CODES, Appointment, AppointmentSlot, BloodRequest, BloodUnit, Branch, Deferral, Donor,
                     LabResult, Reservation)


class DonorForm(forms.ModelForm):
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
    blood_group = forms.ChoiceField(choices=[(x, x) for x in ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]])

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

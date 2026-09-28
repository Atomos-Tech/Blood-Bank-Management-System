from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import User, Donor, Donation, BloodRequest, Appointment, SystemSetting, BLOOD_GROUP_CHOICES


class DonorSignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)
    phone = forms.CharField(max_length=15, required=False)
    date_of_birth = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    weight_kg = forms.DecimalField(max_digits=5, decimal_places=1)
    blood_group = forms.ChoiceField(choices=BLOOD_GROUP_CHOICES)
    branch = forms.ModelChoiceField(queryset=None)

    class Meta:
        model = User
        fields = ["username", "email", "phone", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        from .models import Branch
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = Branch.objects.all()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.DONOR
        user.email = self.cleaned_data["email"]
        user.phone = self.cleaned_data["phone"]
        if commit:
            user.save()
            Donor.objects.create(
                user=user,
                date_of_birth=self.cleaned_data["date_of_birth"],
                weight_kg=self.cleaned_data["weight_kg"],
                blood_group=self.cleaned_data["blood_group"],
                branch=self.cleaned_data["branch"],
            )
        return user


class HospitalSignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)
    phone = forms.CharField(max_length=15, required=False)

    class Meta:
        model = User
        fields = ["username", "email", "phone", "password1", "password2"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.HOSPITAL
        user.email = self.cleaned_data["email"]
        user.phone = self.cleaned_data["phone"]
        if commit:
            user.save()
        return user


class RecordDonationForm(forms.ModelForm):
    class Meta:
        model = Donation
        fields = ["donor", "branch", "donation_date", "volume_ml"]
        widgets = {"donation_date": forms.DateInput(attrs={"type": "date"})}


class TestResultForm(forms.ModelForm):
    class Meta:
        model = Donation
        fields = ["test_result"]


class BloodRequestForm(forms.ModelForm):
    class Meta:
        model = BloodRequest
        fields = ["branch", "blood_group", "units_needed", "patient_name", "is_emergency"]


class AppointmentForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = ["branch", "scheduled_date", "scheduled_time", "notes"]
        widgets = {
            "scheduled_date": forms.DateInput(attrs={"type": "date"}),
            "scheduled_time": forms.TimeInput(attrs={"type": "time"}),
            "notes": forms.TextInput(attrs={"placeholder": "Optional note for staff"}),
        }


class SystemSettingForm(forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = ["min_donor_age", "max_donor_age", "min_weight_kg", "min_donation_gap_days",
                  "unit_shelf_life_days", "low_stock_threshold"]

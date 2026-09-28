from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Branch, Donor, Donation, BloodUnit, BloodRequest, Appointment, Notification, SystemSetting


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Role Info", {"fields": ("role", "phone")}),)
    list_display = ("username", "email", "role", "is_staff")
    list_filter = ("role", "is_staff")


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "contact_phone")


@admin.register(Donor)
class DonorAdmin(admin.ModelAdmin):
    list_display = ("user", "blood_group", "branch", "last_donation_date")
    list_filter = ("blood_group", "branch")


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = ("id", "donor", "branch", "donation_date", "test_result")
    list_filter = ("test_result", "branch")


@admin.register(BloodUnit)
class BloodUnitAdmin(admin.ModelAdmin):
    list_display = ("id", "blood_group", "branch", "status", "expiry_date")
    list_filter = ("status", "blood_group", "branch")


@admin.register(BloodRequest)
class BloodRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "hospital", "blood_group", "units_needed", "status", "is_emergency", "created_at")
    list_filter = ("status", "blood_group", "is_emergency")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("id", "donor", "branch", "scheduled_date", "scheduled_time", "status")
    list_filter = ("status", "branch")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "message", "is_read", "created_at")
    list_filter = ("is_read",)


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("min_donor_age", "max_donor_age", "min_weight_kg", "min_donation_gap_days", "unit_shelf_life_days", "low_stock_threshold")

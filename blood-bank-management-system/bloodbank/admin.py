from django.contrib import admin
from .models import *
for model in [Branch, UserProfile, Donor, Deferral, EligibilityRule, AppointmentSlot, Appointment, Donation, ShelfLifeRule, BloodUnit, TestType, LabResult, BloodRequest, RequestTransition, Reservation, CrossMatch, Issue, Notification]: admin.site.register(model)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """SEC-07: audit events are view-only for every account, including superusers."""

    list_display = ["created_at", "actor", "event_type", "record_type", "record_id", "outcome", "branch"]
    list_filter = ["event_type", "outcome", "branch"]
    search_fields = ["event_type", "record_id", "summary"]
    readonly_fields = [f.name for f in AuditEvent._meta.fields]
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

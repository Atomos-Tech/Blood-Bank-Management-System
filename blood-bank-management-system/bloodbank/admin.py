from django.contrib import admin
from .models import *
from .services import audit, audit_reference


class AuditedAdmin(admin.ModelAdmin):
    """FR-30: configuration and account changes made through the Django admin are audited too.

    FR-27 keeps branches, slots, eligibility rules, shelf-life rules and thresholds in this admin,
    so without this the audit trail would miss every configuration change.
    """

    event_prefix = "CONFIGURATION"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        changed = ", ".join(form.changed_data) if change else "all fields"
        audit(
            request.user,
            f"{self.event_prefix}_{'CHANGED' if change else 'CREATED'}",
            obj,
            summary=f"{obj._meta.verbose_name} {obj}: {changed}",
        )

    def delete_model(self, request, obj):
        # The audit event is written first: deleting the row clears its primary key.
        audit_reference(
            request.user, f"{self.event_prefix}_DELETED", obj.__class__.__name__, obj.pk,
            summary=f"{obj._meta.verbose_name} {obj} deleted",
        )
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            audit_reference(
                request.user, f"{self.event_prefix}_DELETED", obj.__class__.__name__, obj.pk,
                summary=f"{obj._meta.verbose_name} {obj} deleted",
            )
        super().delete_queryset(request, queryset)


class UserProfileAdmin(AuditedAdmin):
    """FR-26 / FR-30: role, branch and activation changes are audited wherever they are made."""

    event_prefix = "USER_ACCOUNT"
    list_display = ["user", "role", "branch", "active"]
    list_filter = ["role", "active", "branch"]


# FR-27: the administrator's configuration surface, and so the rows FR-30 wants audited.
CONFIGURATION_MODELS = [Branch, EligibilityRule, AppointmentSlot, ShelfLifeRule, TestType]
OPERATIONAL_MODELS = [Donor, Deferral, Appointment, Donation, BloodUnit, LabResult, BloodRequest,
                      RequestTransition, Reservation, CrossMatch, Issue, Notification]

for model in CONFIGURATION_MODELS: admin.site.register(model, AuditedAdmin)
admin.site.register(UserProfile, UserProfileAdmin)
for model in OPERATIONAL_MODELS: admin.site.register(model)


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

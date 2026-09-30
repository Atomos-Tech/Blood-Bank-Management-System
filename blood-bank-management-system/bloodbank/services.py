from datetime import timedelta
from django.db import transaction
from django.db.models import Count
from django.core.mail import send_mail
from django.utils import timezone
from .models import (Appointment, AuditEvent, BloodRequest, BloodUnit, CrossMatch, Deferral,
                     EligibilityRule, Issue, LabResult, Reservation, RequestTransition, ShelfLifeRule, TestType)
from .models import Notification

def audit(user, event_type, record, outcome="SUCCESS", summary=""):
    AuditEvent.objects.create(actor=user, branch=getattr(getattr(user, "profile", None), "branch", None), event_type=event_type,
                              record_type=record.__class__.__name__, record_id=str(record.pk), outcome=outcome, summary=summary)

def notify(recipient, subject, body):
    notification = Notification.objects.create(recipient=recipient, subject=subject, body=body)
    try:
        send_mail(subject, body, None, [recipient], fail_silently=False)
        notification.status="SENT"; notification.sent_at=timezone.now(); notification.save(update_fields=["status","sent_at"])
    except Exception:
        notification.status="FAILED"; notification.save(update_fields=["status"])
    return notification

def eligibility(donor):
    reasons = []
    rule = EligibilityRule.objects.filter(active=True).order_by("id").first()
    if not rule: reasons.append("Eligibility rules have not been configured.")
    else:
        today = timezone.localdate(); age = today.year - donor.date_of_birth.year - ((today.month, today.day) < (donor.date_of_birth.month, donor.date_of_birth.day))
        if not rule.min_age <= age <= rule.max_age: reasons.append(f"Age must be between {rule.min_age} and {rule.max_age} years.")
        if donor.weight_kg < rule.min_weight_kg: reasons.append(f"Weight must be at least {rule.min_weight_kg} kg.")
        latest = donor.donations.order_by("-collected_at").first()
        if latest and latest.collected_at.date() + timedelta(days=rule.minimum_interval_days) > today: reasons.append("Minimum donation interval has not elapsed.")
    if not donor.consent_given: reasons.append("Donor consent is required.")
    if any(d.active() for d in donor.deferrals.all()): reasons.append("An active donor deferral applies.")
    return not reasons, reasons

def transition_request(request, next_status, user):
    allowed = {
        BloodRequest.Status.PENDING: {BloodRequest.Status.APPROVED, BloodRequest.Status.REJECTED, BloodRequest.Status.CANCELLED},
        BloodRequest.Status.APPROVED: {BloodRequest.Status.PARTIAL, BloodRequest.Status.FULFILLED, BloodRequest.Status.CANCELLED},
        BloodRequest.Status.PARTIAL: {BloodRequest.Status.FULFILLED, BloodRequest.Status.CANCELLED},
    }
    if next_status not in allowed.get(request.status, set()): raise ValueError("That request state transition is not permitted.")
    old = request.status; request.status = next_status; request.save(update_fields=["status"])
    RequestTransition.objects.create(request=request, from_status=old, to_status=next_status, actor=user)
    audit(user, "REQUEST_STATUS_CHANGED", request, summary=f"{old} to {next_status}")
    notify(request.created_by.email, "Blood request status updated", f"Request {request.pk} is now {request.get_status_display()}.")

@transaction.atomic
def reserve_units(request, user):
    if request.status != BloodRequest.Status.APPROVED: raise ValueError("Only an approved request can be reserved.")
    needed = request.quantity - request.reservations.filter(active=True).count()
    if needed <= 0: return []
    candidates = list(BloodUnit.objects.select_for_update().filter(branch=request.branch, blood_group=request.blood_group, component=request.component, status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now()).order_by("expiry_at")[:needed])
    if not candidates: raise ValueError("No compatible available units are available.")
    made=[]
    for unit in candidates:
        unit.status = BloodUnit.Status.RESERVED; unit.save(update_fields=["status"])
        made.append(Reservation.objects.create(unit=unit, request=request, reserved_by=user))
        audit(user, "UNIT_RESERVED", unit, summary=f"Request {request.pk}")
    return made

@transaction.atomic
def issue_reservation(reservation, user):
    reservation = Reservation.objects.select_for_update().select_related("unit", "request").get(pk=reservation.pk)
    if not reservation.active or reservation.unit.status != BloodUnit.Status.RESERVED: raise ValueError("Reservation is no longer active.")
    try: match = reservation.crossmatch
    except CrossMatch.DoesNotExist: raise ValueError("A compatible cross-match is required before issue.")
    if match.outcome != "COMPATIBLE": raise ValueError("An incompatible cross-match cannot be issued.")
    Issue.objects.create(reservation=reservation, issued_by=user)
    reservation.active=False; reservation.save(update_fields=["active"])
    reservation.unit.status=BloodUnit.Status.ISSUED; reservation.unit.save(update_fields=["status"])
    issued = reservation.request.reservations.filter(active=False, unit__status=BloodUnit.Status.ISSUED).count()
    remaining = reservation.request.quantity-issued
    if remaining <= 0 and reservation.request.status != BloodRequest.Status.FULFILLED: transition_request(reservation.request, BloodRequest.Status.FULFILLED, user)
    elif remaining > 0 and reservation.request.status == BloodRequest.Status.APPROVED: transition_request(reservation.request, BloodRequest.Status.PARTIAL, user)
    audit(user, "UNIT_ISSUED", reservation.unit, summary=f"Request {reservation.request.pk}")

@transaction.atomic
def release_screened_unit(unit, user):
    required = set(TestType.objects.filter(required=True).values_list("id", flat=True))
    outcomes = dict(unit.lab_results.values_list("test_type_id", "outcome"))
    if not required.issubset(outcomes): unit.status = BloodUnit.Status.QUARANTINED
    elif any(outcomes[t] in (LabResult.Outcome.REACTIVE, LabResult.Outcome.INDETERMINATE, LabResult.Outcome.INCOMPLETE) for t in required): unit.status = BloodUnit.Status.QUARANTINED
    elif all(outcomes[t] == LabResult.Outcome.ACCEPTABLE for t in required): unit.status = BloodUnit.Status.AVAILABLE
    else: unit.status = BloodUnit.Status.TESTING
    unit.save(update_fields=["status"]); audit(user, "SCREENING_REVIEWED", unit, summary=unit.status)
    return unit.status

def expire_units():
    return BloodUnit.objects.filter(status__in=[BloodUnit.Status.AVAILABLE, BloodUnit.Status.RESERVED], expiry_at__lte=timezone.now()).update(status=BloodUnit.Status.EXPIRED)

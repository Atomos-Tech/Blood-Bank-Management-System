from datetime import timedelta
from django.db import transaction
from django.db.models import Count, Q
from django.core.mail import send_mail
from django.utils import timezone
from .models import (Appointment, AuditEvent, BloodRequest, BloodUnit, Branch, CrossMatch, Deferral,
                     EligibilityRule, Issue, LabResult, Notification, Reservation, RequestTransition,
                     ShelfLifeRule, TestType, UserProfile)


def audit(user, event_type, record, outcome="SUCCESS", summary="", branch=None):
    resolved_branch = branch or getattr(record, "branch", None) or getattr(getattr(user, "profile", None), "branch", None)
    if isinstance(record, Branch):
        resolved_branch = record
    AuditEvent.objects.create(
        actor=user if getattr(user, "is_authenticated", False) else None,
        branch=resolved_branch,
        event_type=event_type,
        record_type=record.__class__.__name__,
        record_id=str(record.pk),
        outcome=outcome,
        summary=summary,
    )


def notify(recipient, subject, body):
    notification = Notification.objects.create(recipient=recipient, subject=subject, body=body)
    try:
        send_mail(subject, body, None, [recipient], fail_silently=False)
        notification.status = "SENT"
        notification.sent_at = timezone.now()
        notification.save(update_fields=["status", "sent_at"])
    except Exception:
        notification.status = "FAILED"
        notification.save(update_fields=["status"])
    return notification


def calculate_expiry(component, collected_at):
    """FR-15: expiry = collection time + configured shelf-life days for the component."""
    rule = ShelfLifeRule.objects.filter(component=component).first()
    if not rule:
        raise ValueError(f"No approved shelf-life rule exists for {component}.")
    return collected_at + timedelta(days=rule.days)


def eligibility(donor):
    reasons = []
    rule = EligibilityRule.objects.filter(active=True).order_by("id").first()
    if not rule:
        reasons.append("Eligibility rules have not been configured.")
    else:
        today = timezone.localdate()
        age = today.year - donor.date_of_birth.year - (
            (today.month, today.day) < (donor.date_of_birth.month, donor.date_of_birth.day)
        )
        if not rule.min_age <= age <= rule.max_age:
            reasons.append(f"Age must be between {rule.min_age} and {rule.max_age} years.")
        if donor.weight_kg < rule.min_weight_kg:
            reasons.append(f"Weight must be at least {rule.min_weight_kg} kg.")
        latest = donor.donations.order_by("-collected_at").first()
        if latest and latest.collected_at.date() + timedelta(days=rule.minimum_interval_days) > today:
            reasons.append("Minimum donation interval has not elapsed.")
    if not donor.consent_given:
        reasons.append("Donor consent is required.")
    if any(d.active() for d in donor.deferrals.all()):
        reasons.append("An active donor deferral applies.")
    return not reasons, reasons


@transaction.atomic
def transition_request(request, next_status, user):
    allowed = {
        BloodRequest.Status.PENDING: {
            BloodRequest.Status.APPROVED,
            BloodRequest.Status.REJECTED,
            BloodRequest.Status.CANCELLED,
        },
        BloodRequest.Status.APPROVED: {
            BloodRequest.Status.PARTIAL,
            BloodRequest.Status.FULFILLED,
            BloodRequest.Status.CANCELLED,
        },
        BloodRequest.Status.PARTIAL: {
            BloodRequest.Status.FULFILLED,
            BloodRequest.Status.CANCELLED,
        },
    }
    if next_status not in allowed.get(request.status, set()):
        raise ValueError("That request state transition is not permitted.")
    old = request.status
    request.status = next_status
    request.save(update_fields=["status"])
    RequestTransition.objects.create(request=request, from_status=old, to_status=next_status, actor=user)
    if next_status == BloodRequest.Status.CANCELLED:
        for res in request.reservations.select_for_update().select_related("unit").filter(active=True):
            res.active = False
            res.save(update_fields=["active"])
            if res.unit.status == BloodUnit.Status.RESERVED:
                res.unit.status = (
                    BloodUnit.Status.AVAILABLE
                    if res.unit.expiry_at > timezone.now()
                    else BloodUnit.Status.EXPIRED
                )
                res.unit.save(update_fields=["status"])
    audit(user, "REQUEST_STATUS_CHANGED", request, summary=f"{old} to {next_status}", branch=request.branch)
    notify(
        request.created_by.email,
        "Blood request status updated",
        f"Request {request.pk} is now {request.get_status_display()}.",
    )


@transaction.atomic
def reserve_units(request, user):
    request = BloodRequest.objects.select_for_update().get(pk=request.pk)
    if request.status not in {BloodRequest.Status.APPROVED, BloodRequest.Status.PARTIAL}:
        raise ValueError("Only an approved request can be reserved.")
    expire_units()
    allocated = request.reservations.filter(Q(active=True) | Q(issue__isnull=False)).count()
    needed = request.quantity - allocated
    if needed <= 0:
        return []
    candidates = list(
        BloodUnit.objects.select_for_update()
        .filter(
            branch=request.branch,
            blood_group=request.blood_group,
            component=request.component,
            status=BloodUnit.Status.AVAILABLE,
            expiry_at__gt=timezone.now(),
        )
        .exclude(reservations__active=True)
        .order_by("expiry_at", "id")[:needed]
    )
    if not candidates:
        raise ValueError("No compatible available units are available.")
    made = []
    for unit in candidates:
        unit.status = BloodUnit.Status.RESERVED
        unit.save(update_fields=["status"])
        made.append(Reservation.objects.create(unit=unit, request=request, reserved_by=user, active=True))
        audit(user, "UNIT_RESERVED", unit, summary=f"Request {request.pk}", branch=unit.branch)
    return made


@transaction.atomic
def release_reservation(reservation, user, reason="Reservation released"):
    reservation = Reservation.objects.select_for_update().select_related("unit", "request").get(pk=reservation.pk)
    if not reservation.active:
        raise ValueError("Reservation is already inactive.")
    reservation.active = False
    reservation.save(update_fields=["active"])
    if reservation.unit.status == BloodUnit.Status.RESERVED:
        reservation.unit.status = (
            BloodUnit.Status.AVAILABLE
            if reservation.unit.expiry_at > timezone.now()
            else BloodUnit.Status.EXPIRED
        )
        reservation.unit.save(update_fields=["status"])
    audit(user, "RESERVATION_RELEASED", reservation.unit, summary=reason, branch=reservation.unit.branch)
    return reservation.unit


@transaction.atomic
def issue_reservation(reservation, user):
    reservation = Reservation.objects.select_for_update().select_related("unit", "request").get(pk=reservation.pk)
    if not reservation.active or reservation.unit.status != BloodUnit.Status.RESERVED:
        raise ValueError("Reservation is no longer active.")
    try:
        match = reservation.crossmatch
    except CrossMatch.DoesNotExist:
        raise ValueError("A compatible cross-match is required before issue.")
    if match.outcome != "COMPATIBLE":
        raise ValueError("An incompatible cross-match cannot be issued.")
    Issue.objects.create(reservation=reservation, issued_by=user)
    reservation.active = False
    reservation.save(update_fields=["active"])
    reservation.unit.status = BloodUnit.Status.ISSUED
    reservation.unit.save(update_fields=["status"])
    issued = reservation.request.reservations.filter(issue__isnull=False).count()
    remaining = reservation.request.quantity - issued
    if remaining <= 0 and reservation.request.status != BloodRequest.Status.FULFILLED:
        transition_request(reservation.request, BloodRequest.Status.FULFILLED, user)
    elif remaining > 0 and reservation.request.status == BloodRequest.Status.APPROVED:
        transition_request(reservation.request, BloodRequest.Status.PARTIAL, user)
    audit(
        user,
        "UNIT_ISSUED",
        reservation.unit,
        summary=f"Request {reservation.request.pk}",
        branch=reservation.unit.branch,
    )


@transaction.atomic
def release_screened_unit(unit, user):
    required = set(TestType.objects.filter(required=True).values_list("id", flat=True))
    outcomes = dict(unit.lab_results.values_list("test_type_id", "outcome"))
    if not required.issubset(outcomes):
        unit.status = BloodUnit.Status.QUARANTINED
    elif any(
        outcomes[t] in (LabResult.Outcome.REACTIVE, LabResult.Outcome.INDETERMINATE, LabResult.Outcome.INCOMPLETE)
        for t in required
    ):
        unit.status = BloodUnit.Status.QUARANTINED
    elif all(outcomes[t] == LabResult.Outcome.ACCEPTABLE for t in required):
        unit.status = BloodUnit.Status.AVAILABLE
    else:
        unit.status = BloodUnit.Status.TESTING
    unit.save(update_fields=["status"])
    audit(user, "SCREENING_REVIEWED", unit, summary=unit.status, branch=unit.branch)
    return unit.status


def expire_units():
    now = timezone.now()
    expired_qs = BloodUnit.objects.filter(
        status__in=[BloodUnit.Status.AVAILABLE, BloodUnit.Status.RESERVED],
        expiry_at__lte=now,
    )
    if not expired_qs.exists():
        return 0
    return expired_qs.update(status=BloodUnit.Status.EXPIRED)


def get_branch_warnings(branch=None, limit=None, run_expire=True):
    if run_expire:
        expire_units()
    now = timezone.now()
    branches = [branch] if branch is not None else list(Branch.objects.all())
    low_stock_warnings = []
    near_expiry_warnings = []
    for b in branches:
        avail_qs = BloodUnit.objects.filter(
            branch=b,
            status=BloodUnit.Status.AVAILABLE,
            expiry_at__gt=now,
        )
        avail_count = avail_qs.count()
        if avail_count < b.low_stock_threshold:
            low_stock_warnings.append({
                "branch": b,
                "available_count": avail_count,
                "threshold": b.low_stock_threshold,
                "subject": f"Low stock alert [{b.code}]: {avail_count} available (threshold {b.low_stock_threshold})",
                "message": (
                    f"Low stock at {b.code} ({b.name}): {available_count_label(avail_count)}, "
                    f"below threshold of {b.low_stock_threshold}."
                ),
            })
        near_cutoff = now + timedelta(days=b.near_expiry_days)
        near_qs = avail_qs.filter(expiry_at__lte=near_cutoff).order_by("expiry_at", "id")
        if limit is not None:
            near_qs = near_qs[:limit]
        for unit in near_qs:
            near_expiry_warnings.append({
                "branch": b,
                "unit": unit,
                "near_expiry_days": b.near_expiry_days,
                "subject": f"Near-expiry alert [{b.code}]: Unit {unit.unit_id} expires {unit.expiry_at:%Y-%m-%d}",
                "message": (
                    f"Near-expiry unit {unit.unit_id} ({unit.blood_group} {unit.component}) at {b.code} "
                    f"expires on {unit.expiry_at:%Y-%m-%d %H:%M UTC} (within {b.near_expiry_days} days)."
                ),
            })
    return {
        "low_stock": low_stock_warnings,
        "near_expiry": near_expiry_warnings,
    }


def available_count_label(count):
    return f"{count} available unit(s)"


def check_stock_and_expiry_alerts(branch=None):
    warnings = get_branch_warnings(branch=branch)
    sent = []
    all_items = warnings["low_stock"] + warnings["near_expiry"]
    for item in all_items:
        b = item["branch"]
        recipients = list(
            UserProfile.objects.filter(active=True, role=UserProfile.Role.STAFF, branch=b)
            .select_related("user")
            .values_list("user__email", flat=True)
        )
        if not recipients:
            recipients = list(
                UserProfile.objects.filter(
                    active=True,
                    role__in=[UserProfile.Role.STAFF, UserProfile.Role.ADMIN],
                )
                .select_related("user")
                .values_list("user__email", flat=True)
            )
        recipients = [r for r in dict.fromkeys(recipients) if r]
        if not recipients:
            recipients = ["staff@bbms.local"]
        for recipient in recipients:
            if not Notification.objects.filter(recipient=recipient, subject=item["subject"]).exists():
                sent.append(notify(recipient, item["subject"], item["message"]))
    return sent

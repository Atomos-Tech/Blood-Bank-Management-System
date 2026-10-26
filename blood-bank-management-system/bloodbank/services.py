from datetime import timedelta
import uuid
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.core.mail import send_mail
from django.utils import timezone
from .models import (COMPONENT_CODES, Appointment, AuditEvent, BloodRequest, BloodUnit, Branch, CrossMatch, Deferral, Donation,
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


_U = BloodUnit.Status
# SADS 4.1 unit state machine: the only permitted moves out of each state.
UNIT_TRANSITIONS = {
    _U.COLLECTED: {_U.TESTING, _U.DISCARDED},
    _U.TESTING: {_U.AVAILABLE, _U.QUARANTINED, _U.DISCARDED},
    _U.AVAILABLE: {_U.RESERVED, _U.EXPIRED, _U.DISCARDED},
    _U.RESERVED: {_U.AVAILABLE, _U.ISSUED, _U.EXPIRED},
    _U.ISSUED: set(),
    _U.QUARANTINED: {_U.DISCARDED},
    _U.EXPIRED: {_U.DISCARDED},
    _U.DISCARDED: set(),
}


class InvalidUnitTransition(ValueError):
    pass


def transition_unit(unit, next_status, user, reason="", event_type="UNIT_STATUS_CHANGED"):
    """FR-14: the single place a blood unit changes state. Every attempt is audited."""
    current = unit.status
    summary = f"{current} to {next_status}" + (f"; {reason}" if reason else "")
    if next_status not in UNIT_TRANSITIONS.get(current, set()):
        audit(user, "UNIT_TRANSITION_REJECTED", unit, outcome="REJECTED", summary=summary, branch=unit.branch)
        raise InvalidUnitTransition(
            f"Unit {unit.unit_id} cannot move from {_U(current).label} to {_U(next_status).label}."
        )
    with transaction.atomic():
        unit.status = next_status
        unit.save(update_fields=["status"])
        audit(user, event_type, unit, summary=summary, branch=unit.branch)
    return unit


def release_target(unit):
    """A released reservation returns the unit to stock unless it has passed expiry meanwhile."""
    return _U.AVAILABLE if unit.expiry_at > timezone.now() else _U.EXPIRED


def discard_unit(unit, user, reason):
    """FR-14: authorized disposal; the state machine decides which states may be discarded."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A disposal reason is required.")
    return transition_unit(unit, _U.DISCARDED, user, reason=reason, event_type="UNIT_DISCARDED")


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
        raise ValueError(f"No approved shelf-life rule exists for this component ({component}).")
    return collected_at + timedelta(days=rule.days)


def new_donation_id(collected_at):
    """FR-09: random suffix keeps IDs unique even for donations recorded in the same instant."""
    return f"DON-{collected_at:%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def record_donation(donor, branch, collector, blood_group, components, collected_at=None):
    """FR-09, FR-10, FR-15: record one donation and one uniquely identified unit per prepared component.

    `components` maps component name to volume in ml. Every unit links back to the donation and so
    to the donor. Nothing is stored when any component lacks a shelf-life rule.
    """
    if not components:
        raise ValueError("At least one component unit is required.")
    collected_at = collected_at or timezone.now()
    expiries = {component: calculate_expiry(component, collected_at) for component in components}
    for attempt in range(5):
        try:
            with transaction.atomic():
                donation = Donation.objects.create(
                    donation_id=new_donation_id(collected_at),
                    donor=donor,
                    collected_at=collected_at,
                    collector=collector,
                    branch=branch,
                )
                units = [
                    BloodUnit.objects.create(
                        unit_id=f"{donation.donation_id}-{COMPONENT_CODES.get(component, component[:3].upper())}",
                        donation=donation,
                        blood_group=blood_group,
                        component=component,
                        volume_ml=volume,
                        expiry_at=expiries[component],
                        branch=branch,
                        status=BloodUnit.Status.COLLECTED,
                    )
                    for component, volume in components.items()
                ]
                audit(
                    collector,
                    "DONATION_RECORDED",
                    donation,
                    summary=", ".join(u.unit_id for u in units),
                    branch=branch,
                )
            return donation, units
        except IntegrityError:
            if attempt == 4:
                raise


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
                transition_unit(
                    res.unit,
                    release_target(res.unit),
                    user,
                    reason=f"Request {request.pk} cancelled",
                    event_type="RESERVATION_RELEASED",
                )
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
        transition_unit(unit, BloodUnit.Status.RESERVED, user, reason=f"Request {request.pk}", event_type="UNIT_RESERVED")
        made.append(Reservation.objects.create(unit=unit, request=request, reserved_by=user, active=True))
    return made


@transaction.atomic
def release_reservation(reservation, user, reason="Reservation released"):
    reservation = Reservation.objects.select_for_update().select_related("unit", "request").get(pk=reservation.pk)
    if not reservation.active:
        raise ValueError("Reservation is already inactive.")
    reservation.active = False
    reservation.save(update_fields=["active"])
    if reservation.unit.status == BloodUnit.Status.RESERVED:
        transition_unit(
            reservation.unit, release_target(reservation.unit), user, reason=reason, event_type="RESERVATION_RELEASED"
        )
    else:
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
    transition_unit(
        reservation.unit,
        BloodUnit.Status.ISSUED,
        user,
        reason=f"Request {reservation.request.pk}",
        event_type="UNIT_ISSUED",
    )
    issued = reservation.request.reservations.filter(issue__isnull=False).count()
    remaining = reservation.request.quantity - issued
    if remaining <= 0 and reservation.request.status != BloodRequest.Status.FULFILLED:
        transition_request(reservation.request, BloodRequest.Status.FULFILLED, user)
    elif remaining > 0 and reservation.request.status == BloodRequest.Status.APPROVED:
        transition_request(reservation.request, BloodRequest.Status.PARTIAL, user)


class DuplicateLabResult(ValueError):
    pass


def record_lab_result(unit, test_type, outcome, user):
    """FR-11: store one result per configured test type per unit, with the recording staff member and time."""
    if unit.status not in (BloodUnit.Status.COLLECTED, BloodUnit.Status.TESTING):
        raise ValueError("Results can only be recorded while the unit is Collected or Testing.")
    if outcome not in LabResult.Outcome.values:
        raise ValueError("Unknown laboratory outcome.")
    with transaction.atomic():
        try:
            with transaction.atomic():
                result = LabResult.objects.create(unit=unit, test_type=test_type, outcome=outcome, recorded_by=user)
        except IntegrityError:
            raise DuplicateLabResult("A result for this test type is already recorded.")
        if unit.status == BloodUnit.Status.COLLECTED:
            transition_unit(unit, BloodUnit.Status.TESTING, user, reason="Screening started")
        audit(user, "LAB_RESULT_RECORDED", unit, summary=f"{test_type.name}: {outcome}", branch=unit.branch)
        # A bad result quarantines at once; the last acceptable result releases. Otherwise the unit stays in Testing.
        target = screening_outcome(unit)
        if target is not None:
            transition_unit(unit, target, user, reason=f"After {test_type.name} result", event_type="SCREENING_REVIEWED")
    return result


def screening_outcome(unit):
    """FR-12/FR-13: Quarantined if any mandatory result is not acceptable, Available once every mandatory
    result is acceptable, None while the panel is still incomplete or when no panel is configured."""
    required = set(TestType.objects.filter(required=True).values_list("id", flat=True))
    if not required:
        return None
    outcomes = dict(unit.lab_results.filter(test_type_id__in=required).values_list("test_type_id", "outcome"))
    if any(outcome != LabResult.Outcome.ACCEPTABLE for outcome in outcomes.values()):
        return BloodUnit.Status.QUARANTINED
    if required.issubset(outcomes):
        return BloodUnit.Status.AVAILABLE
    return None


@transaction.atomic
def release_screened_unit(unit, user):
    """Release review: the unit goes to Available only with a complete acceptable panel; an incomplete
    or failed panel at release is Quarantined (TC-F07)."""
    if not TestType.objects.filter(required=True).exists():
        raise ValueError("No mandatory test panel is configured, so the unit cannot be released.")
    if unit.status == BloodUnit.Status.COLLECTED:
        transition_unit(unit, BloodUnit.Status.TESTING, user, reason="Screening started")
    if unit.status != BloodUnit.Status.TESTING:
        raise ValueError("Only a unit under screening can be released.")
    outcome = screening_outcome(unit)
    if outcome is None:
        transition_unit(
            unit,
            BloodUnit.Status.QUARANTINED,
            user,
            reason="Release review: mandatory panel incomplete",
            event_type="SCREENING_REVIEWED",
        )
    else:
        transition_unit(unit, outcome, user, reason="Release review", event_type="SCREENING_REVIEWED")
    return unit.status


def expire_units():
    """FR-16: mark unissued units past expiry as Expired; an expired reserved unit releases its reservation."""
    now = timezone.now()
    expired_qs = BloodUnit.objects.filter(
        status__in=[BloodUnit.Status.AVAILABLE, BloodUnit.Status.RESERVED],
        expiry_at__lte=now,
    )
    if not expired_qs.exists():
        return 0
    count = 0
    with transaction.atomic():
        for unit in expired_qs.select_for_update().select_related("branch"):
            for res in unit.reservations.select_for_update().filter(active=True):
                res.active = False
                res.save(update_fields=["active"])
                audit(
                    None,
                    "RESERVATION_RELEASED",
                    unit,
                    summary=f"Request {res.request_id}: reserved unit expired",
                    branch=unit.branch,
                )
            transition_unit(unit, BloodUnit.Status.EXPIRED, None, reason="Expiry time reached", event_type="UNIT_EXPIRED")
            count += 1
    return count


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

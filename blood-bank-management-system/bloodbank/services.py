from datetime import timedelta
from importlib import import_module
import uuid
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.core.mail import send_mail
from django.utils import timezone
from .models import (COMPONENT_CODES, SCREENING_QUESTIONS, Appointment, AuditEvent, BloodRequest, BloodUnit, Branch, CrossMatch, Deferral, Donation, DonationType,
                     EligibilityRule, Issue, LabResult, Notification, Reservation, RequestTransition,
                     ShelfLifeRule, TestType, UserProfile)


def audit_reference(user, event_type, record_type, record_id, outcome="SUCCESS", summary="", branch=None):
    """FR-30: audit an action whose subject is not a live model row - a sign-in attempt for an
    address that may not exist, or a configuration row that has just been deleted."""
    AuditEvent.objects.create(
        actor=user if getattr(user, "is_authenticated", False) else None,
        branch=branch,
        event_type=event_type,
        record_type=record_type,
        record_id=str(record_id)[:64],
        outcome=outcome,
        summary=summary,
    )


def audit(user, event_type, record, outcome="SUCCESS", summary="", branch=None):
    resolved_branch = branch or getattr(record, "branch", None) or getattr(getattr(user, "profile", None), "branch", None)
    if isinstance(record, Branch):
        resolved_branch = record
    audit_reference(
        user, event_type, record.__class__.__name__, record.pk,
        outcome=outcome, summary=summary, branch=resolved_branch,
    )


# SEC-03 / SRS 6.2: five failures inside ten minutes lock the account for fifteen minutes.
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 600
LOGIN_LOCKOUT_SECONDS = 900
# SEC-10: one message for every authentication failure, so no attempt reveals whether an
# address is registered, holds a different password, or is locked.
GENERIC_LOGIN_ERROR = "Invalid email or password."


def _failure_key(email):
    return f"login-failures:{email[:200]}"


def _lock_key(email):
    return f"login-locked:{email[:200]}"


def account_is_locked(email):
    return bool(cache.get(_lock_key(email)))


def record_login_failure(email):
    """SEC-03 / FR-30: count one failed sign-in and lock the account once the limit is reached.

    The ten-minute window starts at the first failure rather than being pushed forward by each
    one, so attempts spread more thinly than the limit never add up to a lockout. The counter
    and the lock live in the shared database cache, so every worker sees the same tally.
    Returns True when this failure locked the account.
    """
    now = timezone.now().timestamp()
    window = cache.get(_failure_key(email)) or {}
    started = window.get("started", now)
    count = window.get("count", 0)
    if now - started >= LOGIN_FAILURE_WINDOW_SECONDS:
        started, count = now, 0
    count += 1
    cache.set(
        _failure_key(email), {"count": count, "started": started},
        max(1, int(LOGIN_FAILURE_WINDOW_SECONDS - (now - started))),
    )
    audit_reference(
        None, "LOGIN_FAILED", "Authentication", email, outcome="DENIED",
        summary=f"Failed sign-in attempt {count} of {LOGIN_FAILURE_LIMIT}",
    )
    if count < LOGIN_FAILURE_LIMIT:
        return False
    cache.set(_lock_key(email), True, LOGIN_LOCKOUT_SECONDS)
    cache.delete(_failure_key(email))
    audit_reference(
        None, "ACCOUNT_LOCKED", "Authentication", email, outcome="DENIED",
        summary=(
            f"Locked for {LOGIN_LOCKOUT_SECONDS // 60} minutes after {LOGIN_FAILURE_LIMIT} "
            f"failed attempts within {LOGIN_FAILURE_WINDOW_SECONDS // 60} minutes"
        ),
    )
    return True


def clear_login_failures(email):
    cache.delete(_failure_key(email))
    cache.delete(_lock_key(email))


class LastAdministrator(ValueError):
    pass


def end_user_sessions(user):
    """FR-26 / SEC-04: an account that has just been deactivated, or that signs out, loses all of its
    open sessions at once instead of staying signed in elsewhere until they expire. Deleting through
    the configured session store clears both the database row and the cached copy."""
    store = import_module(settings.SESSION_ENGINE).SessionStore
    ended = 0
    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        if session.get_decoded().get("_auth_user_id") == str(user.pk):
            store(session_key=session.session_key).delete()
            ended += 1
    return ended


def other_active_administrators(profile):
    """FR-26 / UC-06: administrators other than this one who could still manage accounts."""
    return UserProfile.objects.filter(
        role=UserProfile.Role.ADMIN, active=True,
    ).exclude(pk=profile.pk).count()


def create_account(actor, email, full_name, role, branch, password, hospital_name=""):
    """FR-26: one account with exactly one role. The password is only ever stored as the adaptive
    hash Django's configured hasher produces (SEC-02); it is never written to the audit trail."""
    first, _, last = full_name.strip().partition(" ")
    with transaction.atomic():
        user = User.objects.create_user(
            username=email, email=email, password=password, first_name=first, last_name=last,
        )
        if role == UserProfile.Role.ADMIN:
            # SRS Table 4: the administrator configures branches, slots, rules and thresholds
            # through the Django admin, which the demonstration seed also grants this way.
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
        profile = UserProfile.objects.create(
            user=user, role=role, branch=branch, hospital_name=hospital_name, active=True,
        )
        audit(
            actor, "USER_ACCOUNT_CREATED", user,
            summary=f"{email} created as {profile.get_role_display()}"
                    + (f" at {branch.code}" if branch else ""),
            branch=branch,
        )
    return profile


def set_account_active(profile, active, actor):
    """FR-26: activate or deactivate an account, audit it, and refuse to leave the system without
    an administrator who can undo the change."""
    if not active and profile.role == UserProfile.Role.ADMIN and other_active_administrators(profile) == 0:
        audit(
            actor, "USER_ACCOUNT_DEACTIVATED", profile.user, outcome="REJECTED",
            summary=f"Refused: {profile.user.username} is the last active administrator",
        )
        raise LastAdministrator("The last active administrator cannot be deactivated.")
    with transaction.atomic():
        profile.active = active
        profile.save(update_fields=["active"])
        ended = 0 if active else end_user_sessions(profile.user)
        audit(
            actor, "USER_ACCOUNT_ACTIVATED" if active else "USER_ACCOUNT_DEACTIVATED", profile.user,
            summary=(
                f"{profile.user.username} activated" if active
                else f"{profile.user.username} deactivated; {ended} open session(s) ended"
            ),
            branch=profile.branch,
        )
    return profile


def assign_account_role(profile, role, branch, actor):
    """FR-26: exactly one role per account, with the branch the role is scoped to (NFR-09)."""
    losing_last_admin = (
        profile.role == UserProfile.Role.ADMIN
        and role != UserProfile.Role.ADMIN
        and profile.active
        and other_active_administrators(profile) == 0
    )
    if losing_last_admin:
        audit(
            actor, "USER_ROLE_ASSIGNED", profile.user, outcome="REJECTED",
            summary=f"Refused: {profile.user.username} is the last active administrator",
        )
        raise LastAdministrator("The last active administrator cannot be given another role.")
    previous = profile.get_role_display()
    with transaction.atomic():
        profile.role = role
        profile.branch = branch
        profile.save(update_fields=["role", "branch"])
        audit(
            actor, "USER_ROLE_ASSIGNED", profile.user,
            summary=f"{profile.user.username}: {previous} to {profile.get_role_display()}"
                    + (f" at {branch.code}" if branch else ""),
            branch=branch,
        )
    return profile


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


REMINDER_LEAD = timedelta(hours=24)


def send_appointment_reminders(now=None):
    """FR-29: email every donor whose booked appointment starts within the next 24 hours, once.

    Each email is recorded as a Notification with status SENT or FAILED. A failed reminder is not
    marked as sent, so the next scheduled run tries again. Returns (sent, failed)."""
    now = now or timezone.now()
    due = (
        Appointment.objects.select_related("donor", "slot__branch")
        .filter(status=Appointment.Status.BOOKED, reminder_sent_at__isnull=True,
                slot__starts_at__gt=now, slot__starts_at__lte=now + REMINDER_LEAD)
        .order_by("slot__starts_at")
    )
    sent = failed = 0
    for appointment in due:
        slot = appointment.slot
        notification = notify(
            appointment.donor.email,
            "Reminder: blood donation appointment tomorrow",
            f"Hello {appointment.donor.full_name}, this is a reminder of your donation appointment at "
            f"{_slot_text(slot)}. If you can no longer attend, please reschedule or cancel it from your donor record.",
        )
        if notification.status == "SENT":
            appointment.reminder_sent_at = now
            appointment.save(update_fields=["reminder_sent_at"])
            sent += 1
        else:
            failed += 1
        audit_reference(
            None, "APPOINTMENT_REMINDER", "Appointment", appointment.pk,
            outcome="SUCCESS" if notification.status == "SENT" else "FAILED",
            summary=f"Reminder to {appointment.donor.email}: {notification.status}", branch=slot.branch,
        )
    return sent, failed


def calculate_expiry(component, collected_at):
    """FR-15: expiry = collection time + configured shelf-life days for the component."""
    rule = ShelfLifeRule.objects.filter(component=component).first()
    if not rule:
        raise ValueError(f"No approved shelf-life rule exists for this component ({component}).")
    return collected_at + timedelta(days=rule.days)


def new_donation_id(collected_at):
    """FR-09: random suffix keeps IDs unique even for donations recorded in the same instant."""
    return f"DON-{collected_at:%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def record_donation(donor, branch, collector, blood_group, components, collected_at=None,
                    donation_type=DonationType.WHOLE_BLOOD):
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
                    donation_type=donation_type,
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
                # FR-07: keep the stored next-eligibility date current for the staff donor list.
                donor.eligibility_date = next_eligible_date(donor)
                donor.save(update_fields=["eligibility_date"])
            return donation, units
        except IntegrityError:
            if attempt == 4:
                raise


class AppointmentError(ValueError):
    pass


def _lock_slot(slot):
    """FR-06: take a write lock on the slot row before counting bookings.

    The no-op UPDATE holds a row lock on PostgreSQL and the database write lock on SQLite until the
    transaction ends, so two bookings for the same slot are counted one after the other, never in parallel.
    """
    from .models import AppointmentSlot
    AppointmentSlot.objects.filter(pk=slot.pk).update(capacity=F("capacity"))
    return AppointmentSlot.objects.select_for_update().select_related("branch").get(pk=slot.pk)


def _check_slot_bookable(donor, slot, ignore=None):
    if slot.starts_at <= timezone.now():
        raise AppointmentError("This slot is in the past. Choose a future slot.")
    booked = slot.appointments.filter(status=Appointment.Status.BOOKED)
    if ignore is not None:
        booked = booked.exclude(pk=ignore.pk)
    if booked.filter(donor=donor).exists():
        raise AppointmentError("The donor already has a booking for this slot.")
    if booked.count() >= slot.capacity:
        raise AppointmentError("This slot is full.")


def _slot_text(slot):
    return f"{slot.branch.name} on {timezone.localtime(slot.starts_at):%d %b %Y %H:%M %Z}"


def book_appointment(donor, slot, user):
    """FR-05 / FR-06: book an eligible donor into a future slot with free capacity; audited and confirmed by email."""
    try:
        with transaction.atomic():
            slot = _lock_slot(slot)
            _check_slot_bookable(donor, slot)
            appointment = Appointment.objects.create(donor=donor, slot=slot)
            audit(user, "APPOINTMENT_BOOKED", appointment, summary=_slot_text(slot), branch=slot.branch)
    except IntegrityError:
        raise AppointmentError("The donor already has a booking for this slot.")
    notify(donor.email, "Donation appointment confirmed", f"Your appointment is booked at {_slot_text(slot)}.")
    return appointment


def reschedule_appointment(appointment, new_slot, user):
    """FR-05: move a booked appointment to another future slot with free capacity."""
    if appointment.status != Appointment.Status.BOOKED:
        raise AppointmentError("Only a booked appointment can be rescheduled.")
    if new_slot.pk == appointment.slot_id:
        raise AppointmentError("Choose a different slot to reschedule.")
    old_slot = appointment.slot
    with transaction.atomic():
        new_slot = _lock_slot(new_slot)
        _check_slot_bookable(appointment.donor, new_slot, ignore=appointment)
        appointment.slot = new_slot
        appointment.save(update_fields=["slot"])
        audit(user, "APPOINTMENT_RESCHEDULED", appointment,
              summary=f"{_slot_text(old_slot)} -> {_slot_text(new_slot)}", branch=new_slot.branch)
    notify(appointment.donor.email, "Donation appointment rescheduled",
           f"Your appointment has moved to {_slot_text(new_slot)}.")
    return appointment


def cancel_appointment(appointment, user):
    """FR-05: cancel a booked appointment, releasing its place in the slot."""
    if appointment.status != Appointment.Status.BOOKED:
        raise AppointmentError("This appointment is not active.")
    with transaction.atomic():
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status"])
        audit(user, "APPOINTMENT_CANCELLED", appointment, summary=_slot_text(appointment.slot),
              branch=appointment.slot.branch)
    notify(appointment.donor.email, "Donation appointment cancelled",
           f"Your appointment at {_slot_text(appointment.slot)} has been cancelled.")
    return appointment


def register_donor(donor, user, branch=None):
    """FR-01: save a validated donor profile; operational staff register donors into their own branch."""
    if branch is not None and donor.branch_id is None:
        donor.branch = branch
    donor.save()
    audit(user, "DONOR_REGISTERED", donor,
          summary="Consent captured; screening questionnaire completed", branch=donor.branch)
    return donor


def record_deferral(deferral, approver, branch=None):
    """FR-08: the approving staff member always comes from the session, never from the form."""
    deferral.approved_by = approver
    deferral.save()
    kind = "Permanent" if deferral.permanent else f"Temporary until {deferral.end_date}"
    audit(approver, "DEFERRAL_RECORDED", deferral, summary=f"{kind}: {deferral.reason}",
          branch=deferral.donor.branch or branch)
    return deferral


def save_configuration(form, user):
    """FR-27 / FR-30: save one validated configuration form and audit what changed."""
    creating = form.instance.pk is None
    changed = ", ".join(form.changed_data) or "no fields"
    record = form.save()
    audit(
        user,
        "CONFIGURATION_CREATED" if creating else "CONFIGURATION_CHANGED",
        record,
        summary=f"{record._meta.verbose_name} {record}: {changed}",
    )
    return record


def active_eligibility_rule():
    return EligibilityRule.objects.filter(active=True).order_by("id").first()


def next_eligible_date(donor, rule=None):
    """FR-03 / FR-07: the earliest date the donor may give again, using the interval for each past donation type."""
    rule = rule or active_eligibility_rule()
    if not rule:
        return None
    dates = [
        timezone.localdate(d.collected_at) + timedelta(days=rule.interval_for(d.donation_type))
        for d in donor.donations.all()
    ]
    return max(dates) if dates else None


def donor_history(donor):
    """FR-07: what the donor portal shows - donations and appointments newest first and the date the
    donor may next give. Only this donor's own rows are ever read."""
    next_date = next_eligible_date(donor)
    today = timezone.localdate()
    return {
        "donations": donor.donations.select_related("branch").prefetch_related("units").order_by("-collected_at"),
        "appointments": donor.appointments.select_related("slot__branch").order_by("-slot__starts_at", "-pk"),
        "next_eligible_date": next_date if next_date and next_date > today else today,
        "interval_pending": bool(next_date and next_date > today),
    }


def eligibility(donor):
    """FR-03 / FR-04: evaluate every administrator-configured rule and return all failures, not just the first."""
    reasons = []
    rule = active_eligibility_rule()
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
        next_date = next_eligible_date(donor, rule)
        if next_date and next_date > today:
            reasons.append(f"Minimum donation interval has not elapsed. Next eligible date: {next_date:%d %b %Y}.")
    if not donor.consent_given:
        reasons.append("Donor consent is required.")
    if any(d.active() for d in donor.deferrals.all()):
        reasons.append("An active donor deferral applies.")
    failed = [text for key, text in SCREENING_QUESTIONS if (donor.screening_responses or {}).get(key)]
    if failed:
        # Name the question only; the donor's wider medical history is never shown here (TC-F02).
        reasons.append("Screening questionnaire answered Yes to: " + " ".join(failed))
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


# FR-20: request actions by the names the web form ("approve") and the JSON API ("APPROVED") use.
REQUEST_ACTIONS = {
    "APPROVE": BloodRequest.Status.APPROVED,
    "APPROVED": BloodRequest.Status.APPROVED,
    "REJECT": BloodRequest.Status.REJECTED,
    "REJECTED": BloodRequest.Status.REJECTED,
    "CANCEL": BloodRequest.Status.CANCELLED,
    "CANCELLED": BloodRequest.Status.CANCELLED,
}


def request_action_target(name):
    """The request status an action name asks for, or None for an unknown action."""
    return REQUEST_ACTIONS.get((name or "").strip().upper())


def hospital_may_cancel(blood_request, user):
    """FR-21: hospital staff may cancel only their own Pending request; once approved, only blood bank staff can."""
    return blood_request.created_by_id == user.id and blood_request.status == BloodRequest.Status.PENDING


class MissingHospitalAssignment(ValueError):
    pass


def find_idempotent_request(user, idempotency_key):
    """SADS 8.2: a resubmission with the same Idempotency-Key returns the request already created."""
    if not idempotency_key:
        return None
    return BloodRequest.objects.filter(idempotency_key=idempotency_key, created_by=user).first()


def create_blood_request(blood_request, user, idempotency_key=None):
    """FR-19: a new request starts Pending and is tied to the creating account's verified hospital."""
    hospital = getattr(getattr(user, "profile", None), "hospital_name", "")
    if not hospital:
        raise MissingHospitalAssignment("Hospital staff account is missing its verified hospital assignment.")
    blood_request.created_by = user
    blood_request.hospital = hospital
    blood_request.idempotency_key = idempotency_key
    blood_request.status = BloodRequest.Status.PENDING
    blood_request.save()
    audit(user, "BLOOD_REQUEST_CREATED", blood_request, branch=blood_request.branch)
    return blood_request


CROSSMATCH_OUTCOMES = {"COMPATIBLE", "INCOMPATIBLE"}


def record_crossmatch(reservation, outcome, user):
    """FR-23: one cross-match per reservation, recorded or corrected only while the reservation is active."""
    if outcome not in CROSSMATCH_OUTCOMES:
        raise ValueError("Cross-match outcome must be Compatible or Incompatible.")
    if not reservation.active:
        raise ValueError("A cross-match can only be recorded for an active reservation.")
    match, _ = CrossMatch.objects.update_or_create(
        reservation=reservation, defaults={"outcome": outcome, "recorded_by": user}
    )
    audit(user, "CROSSMATCH_RECORDED", reservation, summary=outcome, branch=reservation.request.branch)
    return match


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

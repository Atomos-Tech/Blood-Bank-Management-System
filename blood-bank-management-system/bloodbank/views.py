import csv
import json
import uuid
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_http_methods, require_POST
from .forms import (DonorSearchForm, AppointmentForm, AppointmentSlotForm, BloodRequestForm, BranchConfigForm,
                    BranchFilterForm, CrossMatchForm, DeferralForm,
                    DonorForm, DonationForm, EligibilityRuleForm, LabResultForm, ReportFilterForm,
                    ShelfLifeRuleForm, UserAccountForm, UserRoleForm)
from .models import (Appointment, AppointmentSlot, AuditEvent, BloodRequest, BloodUnit, Branch,
                     CrossMatch, Deferral, Donation, Donor, EligibilityRule, Issue, LabResult,
                     Notification, Reservation, ShelfLifeRule, TestType, UserProfile)
from .services import (AppointmentError, DuplicateLabResult, GENERIC_LOGIN_ERROR, LastAdministrator,
                       account_is_locked, active_eligibility_rule, assign_account_role, audit,
                       audit_reference, book_appointment, create_blood_request, find_idempotent_request,
                       hospital_may_cancel, MissingHospitalAssignment, record_crossmatch, record_deferral,
                       register_donor, request_action_target, save_configuration,
                       cancel_appointment as cancel_appointment_service, reschedule_appointment,
                       check_stock_and_expiry_alerts, clear_login_failures, create_account,
                       check_in_appointment, discard_unit, donor_history, eligibility, end_user_sessions,
                       find_donors, todays_appointments,
                       expire_units, record_donation, record_lab_result, record_login_failure,
                       get_branch_warnings, issue_reservation, release_reservation,
                       release_screened_unit, reserve_units, set_account_active, transition_request)

# SEC-01: server-side role sets taken from the SRS 6.3 role-permission matrix (Table 4).
# Blood bank staff run branch operations; the administrator has oversight (inventory view, reports, audit)
# and configuration through Django admin, but no routine clinical actions.
STAFF = {UserProfile.Role.STAFF}
ADMIN = {UserProfile.Role.ADMIN}
OVERSIGHT = {UserProfile.Role.STAFF, UserProfile.Role.ADMIN}
STOCK_VIEWERS = {UserProfile.Role.STAFF, UserProfile.Role.ADMIN, UserProfile.Role.HOSPITAL}
REQUEST_USERS = {UserProfile.Role.STAFF, UserProfile.Role.HOSPITAL}


def role(user):
    if not user.is_authenticated:
        return None
    if hasattr(user, "profile"):
        return user.profile.role
    # A Django superuser created without an application profile acts as the System Administrator.
    return UserProfile.Role.ADMIN if user.is_superuser else None


def allowed(user, roles):
    if not user.is_authenticated:
        return False
    if hasattr(user, "profile") and not user.profile.active:
        return False
    return role(user) in roles


def user_branch(user):
    if not user.is_authenticated or not hasattr(user, "profile"):
        return None
    if user.profile.role in {UserProfile.Role.STAFF, UserProfile.Role.HOSPITAL}:
        return user.profile.branch
    return None


def in_branch_scope(user, target_branch):
    if role(user) == UserProfile.Role.ADMIN:
        return True
    ub = user_branch(user)
    if ub is None or target_branch is None:
        return True
    target_id = getattr(target_branch, "pk", target_branch)
    return str(ub.pk) == str(target_id) or ub.code == str(target_branch)


def deny(user, summary=""):
    if user.is_authenticated:
        audit_reference(
            user, "UNAUTHORIZED_ACCESS", "Security", "N/A", outcome="DENIED", summary=summary,
            branch=getattr(getattr(user, "profile", None), "branch", None),
        )
    return HttpResponseForbidden("This account is not permitted to perform that action.")


def api_error(code, message, status=400, field_errors=None):
    payload = {
        "code": code,
        "message": message,
        "correlation_id": str(uuid.uuid4()),
        "timestamp": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if field_errors is not None:
        payload["field_errors"] = field_errors
    return JsonResponse(payload, status=status)


def _parse_payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
    return request.POST.dict()


def _get_idempotency_key(request, data=None):
    header_key = request.headers.get("Idempotency-Key") or request.META.get("HTTP_IDEMPOTENCY_KEY")
    if header_key:
        return header_key.strip()[:64]
    if isinstance(data, dict) and data.get("idempotency_key"):
        return str(data["idempotency_key"]).strip()[:64]
    return None


def _filter_units_by_search(qs, params):
    bg = (params.get("blood_group") or "").strip()
    comp = (params.get("component") or "").strip()
    exp = (params.get("expires_before") or params.get("expiry_date") or "").strip()
    if bg:
        qs = qs.filter(blood_group=bg)
    if comp:
        qs = qs.filter(component__icontains=comp)
    if exp:
        parsed_dt = parse_datetime(exp)
        if parsed_dt is not None:
            if timezone.is_naive(parsed_dt):
                parsed_dt = timezone.make_aware(parsed_dt)
            qs = qs.filter(expiry_at__lte=parsed_dt)
        else:
            parsed_d = parse_date(exp)
            if parsed_d is not None:
                qs = qs.filter(expiry_at__date__lte=parsed_d)
    return qs


@require_http_methods(["GET"])
def health(request):
    """NFR-03: 200 only while the application is serving and the database answers.

    Unauthenticated on purpose: a load balancer or uptime monitor has no session, and the
    body carries no data about the blood bank itself.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unavailable", "database": "down"}, status=503)
    return JsonResponse({"status": "ok", "database": "up"})


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        # SEC-10: every branch below shows GENERIC_LOGIN_ERROR, so a locked, unknown, deactivated
        # or wrong-password account are indistinguishable from the outside.
        if account_is_locked(email):
            audit_reference(
                None, "LOGIN_BLOCKED", "Authentication", email, outcome="DENIED",
                summary="Sign-in attempted while the account was locked",
            )
            messages.error(request, GENERIC_LOGIN_ERROR)
            return render(request, "bloodbank/login.html")
        user = authenticate(request, username=email, password=password)
        if not user or not hasattr(user, "profile") or not user.profile.active:
            # FR-30 / SEC-03: the failure and any resulting lockout are audited by the service.
            record_login_failure(email)
            messages.error(request, GENERIC_LOGIN_ERROR)
            return render(request, "bloodbank/login.html")
        clear_login_failures(email)
        login(request, user)
        audit(user, "LOGIN", user, summary="Authenticated session started")
        return redirect("dashboard")
    return render(request, "bloodbank/login.html")


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        # SEC-04: signing out ends every session the account holds, not just this browser's.
        others = max(end_user_sessions(request.user) - 1, 0)
        audit(
            request.user, "LOGOUT", request.user,
            summary="Authenticated session ended" + (f"; {others} other session(s) ended" if others else ""),
        )
    logout(request)
    return redirect("login")


@login_required
def dashboard(request):
    expire_units()
    user_role = role(request.user)
    ub = user_branch(request.user)

    if user_role == UserProfile.Role.DONOR:
        donor = Donor.objects.filter(user=request.user).first()
        ok, reasons = eligibility(donor) if donor else (False, [])
        context = {
            "is_donor": True,
            "donor": donor,
            "eligible": ok,
            "reasons": reasons,
            "appointment_count": donor.appointments.count() if donor else 0,
            "donation_count": donor.donations.count() if donor else 0,
        }
        return render(request, "bloodbank/dashboard.html", context)

    units = BloodUnit.objects.all()
    requests_qs = BloodRequest.objects.exclude(
        status__in=[
            BloodRequest.Status.FULFILLED,
            BloodRequest.Status.REJECTED,
            BloodRequest.Status.CANCELLED,
        ]
    )
    if ub:
        units = units.filter(branch=ub)
        requests_qs = requests_qs.filter(branch=ub)

    if user_role == UserProfile.Role.HOSPITAL:
        avail_units = units.filter(status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now())
        own_requests = requests_qs.filter(created_by=request.user)
        context = {
            "is_hospital": True,
            "units": avail_units.order_by("expiry_at", "id")[:8],
            "available": avail_units.count(),
            "active_requests": own_requests.count(),
        }
        return render(request, "bloodbank/dashboard.html", context)

    donors_qs = Donor.objects.filter(Q(branch=ub) | Q(branch__isnull=True)) if ub else Donor.objects.all()
    audit_qs = AuditEvent.objects.filter(branch=ub) if ub else AuditEvent.objects.all()
    warnings = get_branch_warnings(branch=ub, limit=8, run_expire=False)
    context = {
        "units": units.order_by("expiry_at", "id")[:8],
        "donor_count": donors_qs.count(),
        "available": units.filter(status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now()).count(),
        "testing": units.filter(status=BloodUnit.Status.TESTING).count(),
        "active_requests": requests_qs.count(),
        "activity": audit_qs[:8] if allowed(request.user, OVERSIGHT) else [],
        "low_stock_warnings": warnings["low_stock"],
        "near_expiry_warnings": warnings["near_expiry"],
    }
    return render(request, "bloodbank/dashboard.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def donor_list(request):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    ub = user_branch(request.user)
    req_branch = request.GET.get("branch")
    if req_branch and not in_branch_scope(request.user, req_branch):
        return deny(request.user, "Cross-branch donor access denied")
    form = DonorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        register_donor(form.save(commit=False), request.user, branch=ub)
        messages.success(request, "Donor registered.")
        return redirect("donors")
    # NFR-07: one search box finds the donor; today's bookings sit next to it for check-in.
    search = DonorSearchForm(request.GET or None)
    query = search.cleaned_data["q"] if search.is_valid() else ""
    scope = ub or (Branch.objects.filter(pk=req_branch).first() if req_branch else None)
    return render(
        request,
        "bloodbank/donors.html",
        {
            "donors": find_donors(query, branch=scope),
            "query": query,
            "search": search,
            "today": todays_appointments(branch=scope),
            "form": form,
            "open_register": request.method == "POST",
            "now_ymd": timezone.localdate().strftime("%Y%m%d"),
        },
    )


@login_required
@require_POST
def check_in_view(request, pk):
    """NFR-07: blood bank staff check a donor in when they arrive for today's appointment."""
    if not allowed(request.user, STAFF):
        return deny(request.user)
    ap = get_object_or_404(Appointment.objects.select_related("donor", "slot__branch"), pk=pk)
    if not in_branch_scope(request.user, ap.slot.branch_id):
        return deny(request.user, "Cross-branch appointment check-in denied")
    try:
        check_in_appointment(ap, request.user)
    except AppointmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"{ap.donor.full_name} checked in for {timezone.localtime(ap.slot.starts_at):%H:%M}.")
    next_url = request.POST.get("next", "")
    return redirect(next_url if next_url.startswith("/donors/") else "donors")


@login_required
def my_donor_record(request):
    if role(request.user) != UserProfile.Role.DONOR:
        return deny(request.user)
    donor_param = request.GET.get("donor")
    donor = get_object_or_404(Donor, user=request.user)
    if donor_param and str(donor.pk) != str(donor_param):
        return deny(request.user, "Attempted access to another donor record")
    ok, reasons = eligibility(donor)
    return render(
        request,
        "bloodbank/my_donor.html",
        {"donor": donor, "eligible": ok, "reasons": reasons, **donor_history(donor)},
    )


def _appointment_actor(request, donor_param=None):
    """Resolve the donor whose appointments the user may manage, or return an HTTP response to send instead."""
    if role(request.user) == UserProfile.Role.DONOR:
        return get_object_or_404(Donor, user=request.user), None
    if allowed(request.user, STAFF):
        donor = get_object_or_404(Donor, pk=donor_param) if donor_param else None
        if donor and donor.branch_id and not in_branch_scope(request.user, donor.branch_id):
            return None, deny(request.user, "Cross-branch donor appointment denied")
        return donor, None
    return None, deny(request.user)


def _after_appointment(request):
    return redirect("my_donor" if role(request.user) == UserProfile.Role.DONOR else "donors")


def _slot_branch_filter(request):
    """Staff only see their own branch; donors may filter by any branch (FR-05)."""
    ub = user_branch(request.user)
    if ub is not None and role(request.user) == UserProfile.Role.STAFF:
        return ub, None
    filter_form = BranchFilterForm(request.GET or None)
    branch = filter_form.cleaned_data["branch"] if filter_form.is_valid() else None
    return branch, filter_form


@login_required
@require_http_methods(["GET", "POST"])
def appointment_view(request):
    donor, response = _appointment_actor(request, request.GET.get("donor"))
    if response:
        return response
    if not donor:
        return redirect("donors")
    branch, filter_form = _slot_branch_filter(request)
    form = AppointmentForm(request.POST or None, branch=branch)
    # FR-04: evaluate every rule up front so the donor sees all failures before trying to book.
    ok, reasons = eligibility(donor)
    if request.method == "POST" and form.is_valid():
        slot = form.cleaned_data["slot"]
        if role(request.user) in STAFF and not in_branch_scope(request.user, slot.branch_id):
            return deny(request.user, "Cross-branch slot booking denied")
        if not ok:
            form.add_error(None, "Appointment blocked until every eligibility rule passes.")
            audit(request.user, "APPOINTMENT_BLOCKED", donor, outcome="REJECTED",
                  summary=f"{len(reasons)} eligibility rule(s) failed", branch=slot.branch)
        else:
            try:
                book_appointment(donor, slot, request.user)
            except AppointmentError as e:
                form.add_error("slot", str(e))
            else:
                messages.success(request, "Appointment booked. A confirmation email has been sent.")
                return _after_appointment(request)
    return render(
        request,
        "bloodbank/appointments.html",
        {
            "title": "Book appointment",
            "form": form,
            "filter_form": filter_form,
            "donor": donor,
            "eligible": ok,
            "reasons": reasons,
            "cancel": "my_donor" if role(request.user) == UserProfile.Role.DONOR else "donors",
        },
    )


def _load_managed_appointment(request, pk):
    ap = get_object_or_404(Appointment.objects.select_related("donor", "slot__branch"), pk=pk)
    if role(request.user) == UserProfile.Role.DONOR:
        if ap.donor.user_id != request.user.id:
            return ap, deny(request.user, "Attempted change to another donor's appointment")
    elif not allowed(request.user, STAFF):
        return ap, deny(request.user)
    elif not in_branch_scope(request.user, ap.slot.branch_id):
        return ap, deny(request.user, "Cross-branch appointment change denied")
    return ap, None


@login_required
@require_http_methods(["GET", "POST"])
def reschedule_appointment_view(request, pk):
    ap, response = _load_managed_appointment(request, pk)
    if response:
        return response
    if ap.status != Appointment.Status.BOOKED:
        messages.error(request, "Only a booked appointment can be rescheduled.")
        return _after_appointment(request)
    branch, filter_form = _slot_branch_filter(request)
    form = AppointmentForm(request.POST or None, branch=branch, exclude_slot=ap.slot)
    ok, reasons = eligibility(ap.donor)
    if request.method == "POST" and form.is_valid():
        slot = form.cleaned_data["slot"]
        if role(request.user) in STAFF and not in_branch_scope(request.user, slot.branch_id):
            return deny(request.user, "Cross-branch slot booking denied")
        if not ok:
            form.add_error(None, "Appointment blocked until every eligibility rule passes.")
        else:
            try:
                reschedule_appointment(ap, slot, request.user)
            except AppointmentError as e:
                form.add_error("slot", str(e))
            else:
                messages.success(request, "Appointment rescheduled. A confirmation email has been sent.")
                return _after_appointment(request)
    return render(
        request,
        "bloodbank/appointments.html",
        {
            "title": "Reschedule appointment",
            "current": ap,
            "form": form,
            "filter_form": filter_form,
            "donor": ap.donor,
            "eligible": ok,
            "reasons": reasons,
            "cancel": "my_donor" if role(request.user) == UserProfile.Role.DONOR else "donors",
        },
    )


@login_required
@require_POST
def cancel_appointment(request, pk):
    ap, response = _load_managed_appointment(request, pk)
    if response:
        return response
    try:
        cancel_appointment_service(ap, request.user)
    except AppointmentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Appointment cancelled.")
    return _after_appointment(request)


@login_required
@require_http_methods(["GET", "POST"])
def deferral_view(request):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    ub = user_branch(request.user)
    form = DeferralForm(request.POST or None, branch=ub)
    if request.method == "POST" and form.is_valid():
        d = form.save(commit=False)
        if d.donor.branch_id and not in_branch_scope(request.user, d.donor.branch_id):
            return deny(request.user, "Cross-branch deferral denied")
        record_deferral(d, request.user, branch=ub)
        messages.success(request, "Deferral recorded.")
        return redirect("deferrals")
    items = Deferral.objects.select_related("donor", "approved_by").order_by("-start_date", "-pk")
    if ub:
        items = items.filter(Q(donor__branch=ub) | Q(donor__branch__isnull=True))
    return render(request, "bloodbank/deferrals.html", {"form": form, "items": items})


@login_required
@require_http_methods(["GET", "POST"])
def inventory(request):
    if not allowed(request.user, STOCK_VIEWERS):
        return deny(request.user)
    expire_units()
    ub = user_branch(request.user)
    p = request.GET
    if p.get("branch") and not in_branch_scope(request.user, p["branch"]):
        return deny(request.user, "Cross-branch inventory search denied")

    units = BloodUnit.objects.select_related("branch").all()
    if ub:
        units = units.filter(branch=ub)
    elif p.get("branch"):
        if p["branch"].isdigit():
            units = units.filter(branch_id=int(p["branch"]))
        else:
            units = units.filter(branch__code=p["branch"])

    units = _filter_units_by_search(units, p)
    if role(request.user) == UserProfile.Role.HOSPITAL:
        units = units.filter(status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now())

    paginator = Paginator(units.order_by("expiry_at", "id"), 50)
    page_obj = paginator.get_page(p.get("page", 1))
    branches = Branch.objects.filter(pk=ub.pk) if ub else Branch.objects.all()
    return render(
        request,
        "bloodbank/inventory.html",
        {"units": page_obj, "page_obj": page_obj, "branches": branches},
    )


@login_required
@require_http_methods(["GET", "POST"])
def donation_view(request):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    ub = user_branch(request.user)
    form = DonationForm(request.POST or None, branch=ub)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if not in_branch_scope(request.user, data["branch"]):
            return deny(request.user, "Cross-branch donation recording denied")
        if data["donor"].branch_id and not in_branch_scope(request.user, data["donor"].branch_id):
            return deny(request.user, "Cross-branch donor donation denied")
        try:
            donation, units = record_donation(
                donor=data["donor"],
                branch=data["branch"],
                collector=request.user,
                blood_group=data["blood_group"],
                components=data["components"],
                donation_type=data["donation_type"],
            )
        except ValueError as e:
            form.add_error(None, str(e))
        else:
            messages.success(
                request,
                f"Donation {donation.donation_id} recorded with unit(s) {', '.join(u.unit_id for u in units)}. "
                "Expiry was calculated from the configured shelf-life rule.",
            )
            return redirect("inventory")
    return render(
        request,
        "bloodbank/form.html",
        {"title": "Record donation and component units", "form": form, "cancel": "inventory"},
    )


@login_required
@require_http_methods(["GET", "POST"])
def screening_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    unit = get_object_or_404(BloodUnit.objects.select_related("branch"), pk=pk)
    if not in_branch_scope(request.user, unit.branch_id):
        return deny(request.user, "Cross-branch unit screening denied")
    form = LabResultForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            record_lab_result(unit, form.cleaned_data["test_type"], form.cleaned_data["outcome"], request.user)
        except DuplicateLabResult as e:
            form.add_error("test_type", str(e))
        except ValueError as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Result recorded. Unit is now {unit.get_status_display()}.")
            return redirect("screening", pk=unit.pk)
    return render(
        request,
        "bloodbank/screening.html",
        {
            "unit": unit,
            "form": form,
            "results": unit.lab_results.select_related("test_type", "recorded_by").order_by("recorded_at"),
            "panel": TestType.objects.filter(required=True).order_by("name"),
        },
    )


@login_required
@require_POST
def release_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    unit = get_object_or_404(BloodUnit.objects.select_related("branch"), pk=pk)
    if not in_branch_scope(request.user, unit.branch_id):
        return deny(request.user, "Cross-branch unit release denied")
    try:
        release_screened_unit(unit, request.user)
        messages.success(request, f"Release reviewed. Unit {unit.unit_id} is now {unit.get_status_display()}.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("screening", pk=unit.pk)


@login_required
@require_POST
def discard_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    unit = get_object_or_404(BloodUnit.objects.select_related("branch"), pk=pk)
    if not in_branch_scope(request.user, unit.branch_id):
        return deny(request.user, "Cross-branch unit disposal denied")
    try:
        discard_unit(unit, request.user, request.POST.get("reason", ""))
        messages.success(request, f"Unit {unit.unit_id} discarded.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("inventory")


@login_required
@require_http_methods(["GET", "POST"])
def request_list(request):
    if not allowed(request.user, REQUEST_USERS):
        return deny(request.user)
    ub = user_branch(request.user)
    req_branch = request.GET.get("branch")
    if req_branch and not in_branch_scope(request.user, req_branch):
        return deny(request.user, "Cross-branch blood request list denied")

    form = BloodRequestForm(request.POST or None, branch=ub)
    qs = BloodRequest.objects.select_related("branch", "created_by").prefetch_related(
        "transitions__actor",
        "reservations__unit",
        "reservations__crossmatch",
    )
    if ub:
        qs = qs.filter(branch=ub)
    elif req_branch:
        qs = qs.filter(branch_id=req_branch)

    if role(request.user) == UserProfile.Role.HOSPITAL:
        qs = qs.filter(created_by=request.user)

    if request.method == "POST":
        idem_key = _get_idempotency_key(request, request.POST)
        if find_idempotent_request(request.user, idem_key):
            messages.success(request, "Blood request submitted.")
            return redirect("requests")
        if form.is_valid():
            r = form.save(commit=False)
            if not in_branch_scope(request.user, r.branch_id):
                return deny(request.user, "Cross-branch blood request creation denied")
            try:
                create_blood_request(r, request.user, idem_key)
            except MissingHospitalAssignment as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, "Blood request submitted.")
                return redirect("requests")
    return render(request, "bloodbank/requests.html", {"requests": qs.order_by("-created_at"), "form": form})


@login_required
@require_POST
def request_action(request, pk, action):
    r = get_object_or_404(BloodRequest.objects.select_related("branch", "created_by"), pk=pk)
    if not in_branch_scope(request.user, r.branch_id):
        return deny(request.user, "Cross-branch request action denied")
    target = request_action_target(action)
    if target == BloodRequest.Status.CANCELLED:
        if role(request.user) == UserProfile.Role.HOSPITAL:
            if not allowed(request.user, {UserProfile.Role.HOSPITAL}) or not hospital_may_cancel(r, request.user):
                return deny(request.user, "Hospital staff can cancel only their own Pending requests")
        elif not allowed(request.user, STAFF):
            return deny(request.user)
    else:
        if not allowed(request.user, STAFF):
            return deny(request.user)
        if target is None:
            return deny(request.user, f"Unsupported request action {action}")
    try:
        transition_request(r, target, request.user)
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("requests")


@login_required
@require_POST
def reserve_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    r = get_object_or_404(BloodRequest.objects.select_related("branch"), pk=pk)
    if not in_branch_scope(request.user, r.branch_id):
        return deny(request.user, "Cross-branch reservation denied")
    try:
        made = reserve_units(r, request.user)
        messages.success(request, f"Reserved {len(made)} compatible unit(s).")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("requests")


@login_required
@require_POST
def release_reservation_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    reservation = get_object_or_404(Reservation.objects.select_related("unit", "request__branch"), pk=pk)
    if not in_branch_scope(request.user, reservation.request.branch_id):
        return deny(request.user, "Cross-branch reservation release denied")
    try:
        release_reservation(reservation, request.user)
        messages.success(request, "Reservation released.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("requests")


@login_required
@require_http_methods(["GET", "POST"])
def crossmatch_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    reservation = get_object_or_404(Reservation.objects.select_related("unit", "request__branch"), pk=pk)
    if not in_branch_scope(request.user, reservation.request.branch_id):
        return deny(request.user, "Cross-branch cross-match denied")
    form = CrossMatchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            record_crossmatch(reservation, form.cleaned_data["outcome"], request.user)
        except ValueError as e:
            form.add_error(None, str(e))
        else:
            return redirect("requests")
    return render(request, "bloodbank/form.html", {"title": "Record cross-match", "form": form, "cancel": "requests"})


@login_required
@require_POST
def issue_view(request, pk):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    reservation = get_object_or_404(Reservation.objects.select_related("unit", "request__branch"), pk=pk)
    if not in_branch_scope(request.user, reservation.request.branch_id):
        return deny(request.user, "Cross-branch unit issue denied")
    try:
        issue_reservation(reservation, request.user)
        messages.success(request, "Unit issued atomically.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("requests")


@login_required
@require_http_methods(["GET"])
def audit_view(request):
    if not allowed(request.user, OVERSIGHT):
        return deny(request.user)
    ub = user_branch(request.user)
    req_branch = request.GET.get("branch")
    if req_branch and not in_branch_scope(request.user, req_branch):
        return deny(request.user, "Cross-branch audit trail access denied")
    events = AuditEvent.objects.select_related("actor", "branch").all()
    if ub:
        events = events.filter(branch=ub)
    elif req_branch:
        events = events.filter(branch_id=req_branch)
    return render(request, "bloodbank/audit.html", {"events": events[:200]})


ACCOUNT_ADMIN_DENIAL = "Account administration is restricted to the system administrator"


def _accounts_page(request, form):
    return render(
        request,
        "bloodbank/accounts.html",
        {
            "form": form,
            "profiles": UserProfile.objects.select_related("user", "branch").order_by("user__username"),
            "roles": UserProfile.Role.choices,
            "branches": Branch.objects.order_by("name"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def account_admin(request):
    """FR-26: the administrator's own screen for user accounts, instead of the Django admin."""
    if not allowed(request.user, ADMIN):
        return deny(request.user, ACCOUNT_ADMIN_DENIAL)
    form = UserAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        profile = create_account(
            request.user,
            form.cleaned_data["email"],
            form.cleaned_data["full_name"],
            form.cleaned_data["role"],
            form.cleaned_data["branch"],
            form.cleaned_data["password"],
            form.cleaned_data["hospital_name"],
        )
        messages.success(request, f"Account {profile.user.username} created as {profile.get_role_display()}.")
        return redirect("accounts")
    return _accounts_page(request, form)


@login_required
@require_POST
def account_status(request, pk):
    """FR-26: deactivation blocks the next sign-in and ends the sessions already open."""
    if not allowed(request.user, ADMIN):
        return deny(request.user, ACCOUNT_ADMIN_DENIAL)
    profile = get_object_or_404(UserProfile, pk=pk)
    activate = request.POST.get("active") == "1"
    try:
        set_account_active(profile, activate, request.user)
    except LastAdministrator as error:
        messages.error(request, str(error))
        return redirect("accounts")
    messages.success(
        request,
        f"{profile.user.username} {'activated' if activate else 'deactivated and signed out'}.",
    )
    return redirect("accounts")


@login_required
@require_POST
def account_role(request, pk):
    """FR-26: assign exactly one role, and the branch that role works in."""
    if not allowed(request.user, ADMIN):
        return deny(request.user, ACCOUNT_ADMIN_DENIAL)
    profile = get_object_or_404(UserProfile, pk=pk)
    form = UserRoleForm(request.POST)
    if not form.is_valid():
        for error in form.errors.values():
            messages.error(request, error.as_text())
        return redirect("accounts")
    try:
        assign_account_role(profile, form.cleaned_data["role"], form.cleaned_data["branch"], request.user)
    except LastAdministrator as error:
        messages.error(request, str(error))
        return redirect("accounts")
    messages.success(request, f"{profile.user.username} is now {profile.get_role_display()}.")
    return redirect("accounts")


CONFIG_DENIAL = "Configuration is restricted to the system administrator"

# FR-27: each configuration section, its form and the audit record type it writes.
CONFIG_SECTIONS = {
    "branch": (BranchConfigForm, Branch),
    "eligibility": (EligibilityRuleForm, EligibilityRule),
    "shelf-life": (ShelfLifeRuleForm, ShelfLifeRule),
    "slot": (AppointmentSlotForm, AppointmentSlot),
}


def _configuration_page(request, invalid=None):
    """Render every configuration section. `invalid` is the one bound form that failed, if any,
    keyed by (section, pk) so it is shown in place with its errors."""
    invalid = invalid or {}

    def form_for(section, pk, form_class, instance, prefix):
        return invalid.get((section, pk)) or form_class(instance=instance, prefix=prefix)

    rule = active_eligibility_rule()
    return render(
        request,
        "bloodbank/configuration.html",
        {
            "branch_forms": [
                (b, form_for("branch", b.pk, BranchConfigForm, b, f"branch-{b.pk}"))
                for b in Branch.objects.order_by("name")
            ],
            "eligibility_form": form_for(
                "eligibility", getattr(rule, "pk", None), EligibilityRuleForm, rule, None,
            ),
            "shelf_life_forms": [
                (r, form_for("shelf-life", r.pk, ShelfLifeRuleForm, r, f"shelf-{r.pk}"))
                for r in ShelfLifeRule.objects.order_by("component")
            ],
            "slot_form": invalid.get(("slot", None)) or AppointmentSlotForm(),
            "slots": AppointmentSlot.objects.select_related("branch").filter(
                starts_at__gt=timezone.now()
            ).order_by("starts_at")[:20],
        },
    )


@login_required
@require_http_methods(["GET"])
def configuration(request):
    """FR-27: branches, slot capacities, eligibility rules, shelf lives and stock thresholds."""
    if not allowed(request.user, ADMIN):
        return deny(request.user, CONFIG_DENIAL)
    return _configuration_page(request)


@login_required
@require_POST
def configuration_save(request, section):
    """FR-27 / FR-30: validate one configuration section and audit the change."""
    if not allowed(request.user, ADMIN):
        return deny(request.user, CONFIG_DENIAL)
    if section not in CONFIG_SECTIONS:
        return deny(request.user, f"Unknown configuration section {section}")
    form_class, _model = CONFIG_SECTIONS[section]

    instance, prefix = None, None
    pk = request.POST.get("pk")
    if section == "branch":
        instance = get_object_or_404(Branch, pk=pk)
        prefix = f"branch-{instance.pk}"
    elif section == "shelf-life":
        instance = get_object_or_404(ShelfLifeRule, pk=pk)
        prefix = f"shelf-{instance.pk}"
    elif section == "eligibility":
        instance = active_eligibility_rule()

    form = form_class(request.POST, instance=instance, prefix=prefix) if instance is not None \
        else form_class(request.POST)
    if not form.is_valid():
        messages.error(request, "Configuration not saved: fix the errors shown.")
        return _configuration_page(request, {(section, getattr(instance, "pk", None)): form})

    record = save_configuration(form, request.user)
    messages.success(request, f"{record._meta.verbose_name.capitalize()} saved.")
    return redirect("configuration")


# FR-28: the five report types, each with its columns and the field its date range filters on.
REPORT_DEFINITIONS = {
    "inventory": ("Inventory", ["Unit", "Blood group", "Component", "Status", "Branch", "Collected", "Expires"]),
    "expiry": ("Expiry", ["Unit", "Blood group", "Component", "Branch", "Expires", "Status"]),
    "donation": ("Donations", ["Donation", "Donor", "Blood group", "Type", "Branch", "Collected", "Units"]),
    "request": ("Blood requests", ["Request", "Hospital", "Blood group", "Component", "Quantity",
                                   "Priority", "Status", "Required by", "Raised"]),
    "audit": ("Audit trail", ["Time (UTC)", "Actor", "Event", "Record", "Outcome", "Summary"]),
}
# NFR-11 / SEC-09: a hospital account sees availability and its own requests, never donor,
# donation or audit records.
HOSPITAL_REPORTS = ["inventory", "request"]


def _in_range(queryset, field, start, end):
    if start:
        queryset = queryset.filter(**{f"{field}__date__gte": start})
    if end:
        queryset = queryset.filter(**{f"{field}__date__lte": end})
    return queryset


def _report_rows(report, user, start, end, branch):
    """Return the rows of one report, already scoped to the viewer's role and branch."""
    is_hospital = role(user) == UserProfile.Role.HOSPITAL
    local = timezone.localtime

    if report in {"inventory", "expiry"}:
        units = BloodUnit.objects.select_related("branch", "donation")
        if branch:
            units = units.filter(branch=branch)
        if is_hospital:
            units = units.filter(status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now())
        if report == "expiry":
            units = _in_range(units, "expiry_at", start, end).order_by("expiry_at", "id")
            return [
                [u.unit_id, u.blood_group, u.component, u.branch.code,
                 local(u.expiry_at).strftime("%Y-%m-%d %H:%M"), u.get_status_display()]
                for u in units
            ]
        units = _in_range(units, "donation__collected_at", start, end).order_by("expiry_at", "id")
        return [
            [u.unit_id, u.blood_group, u.component, u.get_status_display(), u.branch.code,
             local(u.donation.collected_at).strftime("%Y-%m-%d %H:%M"),
             local(u.expiry_at).strftime("%Y-%m-%d %H:%M")]
            for u in units
        ]

    if report == "donation":
        donations = Donation.objects.select_related("donor", "branch").annotate(unit_count=Count("units"))
        if branch:
            donations = donations.filter(branch=branch)
        donations = _in_range(donations, "collected_at", start, end).order_by("-collected_at")
        return [
            [d.donation_id, d.donor.full_name, d.donor.blood_group, d.get_donation_type_display(),
             d.branch.code, local(d.collected_at).strftime("%Y-%m-%d %H:%M"), d.unit_count]
            for d in donations
        ]

    if report == "request":
        requests_qs = BloodRequest.objects.select_related("branch")
        if branch:
            requests_qs = requests_qs.filter(branch=branch)
        if is_hospital:
            requests_qs = requests_qs.filter(created_by=user)
        requests_qs = _in_range(requests_qs, "created_at", start, end).order_by("-created_at")
        return [
            [r.pk, r.hospital, r.blood_group, r.component, r.quantity, r.priority,
             r.get_status_display(), local(r.required_by).strftime("%Y-%m-%d %H:%M"),
             local(r.created_at).strftime("%Y-%m-%d %H:%M")]
            for r in requests_qs
        ]

    events = AuditEvent.objects.select_related("actor", "branch")
    if branch:
        events = events.filter(branch=branch)
    events = _in_range(events, "created_at", start, end)
    return [
        [e.created_at.strftime("%Y-%m-%d %H:%M:%S"), str(e.actor or "System"), e.event_type,
         f"{e.record_type} {e.record_id}", e.outcome, e.summary]
        for e in events[:2000]
    ]


@login_required
@require_http_methods(["GET"])
def reports(request):
    """FR-28: five report types, filtered by date range and branch, downloadable as CSV."""
    if not allowed(request.user, STOCK_VIEWERS):
        return deny(request.user)
    expire_units()
    ub = user_branch(request.user)
    req_branch = request.GET.get("branch")
    if req_branch and not in_branch_scope(request.user, req_branch):
        return deny(request.user, "Cross-branch report access denied")

    is_hospital = role(request.user) == UserProfile.Role.HOSPITAL
    available = HOSPITAL_REPORTS if is_hospital else list(REPORT_DEFINITIONS)

    # A role asking by name for a report it may not see is denied and audited, rather than
    # quietly shown a different one (NFR-11 / SEC-09).
    requested = (request.GET.get("report") or "").strip()
    if requested and requested not in available:
        if requested in REPORT_DEFINITIONS:
            return deny(request.user, f"The {requested} report is not available to this role")
        requested = ""

    form = ReportFilterForm(
        request.GET or None,
        allowed_reports=HOSPITAL_REPORTS if is_hospital else None,
        lock_branch=ub,
    )
    form.is_valid()  # a blank or partly filled filter falls back to the defaults below
    cleaned = form.cleaned_data if hasattr(form, "cleaned_data") else {}

    report = requested or available[0]
    start, end = cleaned.get("start_date"), cleaned.get("end_date")
    branch = ub or cleaned.get("branch")

    title, columns = REPORT_DEFINITIONS[report]
    rows = _report_rows(report, request.user, start, end, branch)

    if request.GET.get("format") == "csv":
        audit_reference(
            request.user, "REPORT_EXPORTED", "Report", report,
            summary=f"{title} report exported as CSV ({len(rows)} rows)",
            branch=branch,
        )
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="bbms-{report}-report.csv"'
        writer = csv.writer(response)
        writer.writerow(columns)
        writer.writerows(rows)
        return response

    return render(
        request,
        "bloodbank/reports.html",
        {
            "form": form,
            "is_hospital": is_hospital,
            "report": report,
            "report_title": title,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "csv_query": request.GET.urlencode(),
        },
    )


@login_required
@require_http_methods(["GET"])
def api_inventory(request):
    if not allowed(request.user, STOCK_VIEWERS):
        deny(request.user)
        return api_error("forbidden", "Not authorized", status=403)
    expire_units()
    ub = user_branch(request.user)
    p = request.GET
    if p.get("branch") and not in_branch_scope(request.user, p["branch"]):
        deny(request.user, "Cross-branch API inventory access denied")
        return api_error("forbidden", "Cross-branch access denied", status=403)

    exp = (p.get("expires_before") or p.get("expiry_date") or "").strip()
    if exp and parse_datetime(exp) is None and parse_date(exp) is None:
        return api_error(
            "validation_error",
            "Invalid expires_before date format. Use ISO 8601 (YYYY-MM-DD).",
            status=400,
            field_errors={"expires_before": ["Enter a valid ISO date or datetime."]},
        )

    qs = BloodUnit.objects.filter(status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now())
    if ub:
        qs = qs.filter(branch=ub)
    elif p.get("branch"):
        if p["branch"].isdigit():
            qs = qs.filter(branch_id=int(p["branch"]))
        else:
            qs = qs.filter(branch__code=p["branch"])

    qs = _filter_units_by_search(qs, p)
    items = list(
        qs.order_by("expiry_at", "id")[:100].values("unit_id", "blood_group", "component", "expiry_at", "branch__code")
    )
    return JsonResponse({"items": items})


@login_required
@require_http_methods(["POST"])
def api_create_request(request):
    """POST /api/v1/requests — Hospital Staff create a blood request (SADS 8.2, FR-19, UC-04)."""
    if not allowed(request.user, {UserProfile.Role.HOSPITAL}):
        deny(request.user, "Non-hospital user attempted POST /api/v1/requests")
        return api_error("forbidden", "Only hospital staff can submit blood requests.", status=403)
    data = _parse_payload(request)
    if data is None:
        return api_error("invalid_json", "Request body must be valid JSON.", status=400)

    idem_key = _get_idempotency_key(request, data)
    existing = find_idempotent_request(request.user, idem_key)
    if existing:
        return JsonResponse(_request_payload(existing), status=200)

    ub = user_branch(request.user)
    if "branch" not in data and ub is not None:
        data = dict(data, branch=ub.pk)
    form = BloodRequestForm(data, branch=ub)
    if not form.is_valid():
        return api_error("validation_error", "Invalid blood request fields.", status=400, field_errors=form.errors)

    r = form.save(commit=False)
    if not in_branch_scope(request.user, r.branch_id):
        deny(request.user, "Cross-branch API request creation denied")
        return api_error("forbidden", "Cross-branch access denied.", status=403)
    try:
        create_blood_request(r, request.user, idem_key)
    except MissingHospitalAssignment as e:
        return api_error(
            "validation_error",
            str(e),
            status=400,
            field_errors={"hospital": ["Verified hospital assignment required."]},
        )
    return JsonResponse(_request_payload(r), status=201)


def _request_payload(r):
    return {
        "id": r.pk,
        "hospital": r.hospital,
        "branch": r.branch.code,
        "blood_group": r.blood_group,
        "component": r.component,
        "quantity": r.quantity,
        "priority": r.priority,
        "required_by": r.required_by.isoformat(),
        "clinical_reference": r.clinical_reference,
        "status": r.status,
    }


@login_required
@require_http_methods(["PATCH", "POST"])
def api_request_status(request, pk):
    """PATCH /api/v1/requests/{id}/status — Approve, reject or cancel a request (SADS 8.2, FR-20, FR-21)."""
    r = get_object_or_404(BloodRequest.objects.select_related("branch", "created_by"), pk=pk)
    if not in_branch_scope(request.user, r.branch_id):
        deny(request.user, "Cross-branch API request status update denied")
        return api_error("forbidden", "Cross-branch access denied.", status=403)

    data = _parse_payload(request)
    if data is None:
        return api_error("invalid_json", "Request body must be valid JSON.", status=400)

    target = request_action_target(data.get("status") or data.get("action"))
    if not target:
        return api_error(
            "validation_error",
            "Status must be one of APPROVED, REJECTED, or CANCELLED.",
            status=400,
            field_errors={"status": ["Unsupported status value."]},
        )

    if target == BloodRequest.Status.CANCELLED:
        if role(request.user) == UserProfile.Role.HOSPITAL:
            if not allowed(request.user, {UserProfile.Role.HOSPITAL}) or not hospital_may_cancel(r, request.user):
                deny(request.user, "Hospital staff attempted unauthorized API request cancellation")
                return api_error("forbidden", "Hospital staff can cancel only their own Pending requests.", status=403)
        elif not allowed(request.user, STAFF):
            deny(request.user)
            return api_error("forbidden", "Not authorized.", status=403)
    else:
        if not allowed(request.user, STAFF):
            deny(request.user)
            return api_error("forbidden", "Only blood bank staff can approve or reject requests.", status=403)

    try:
        transition_request(r, target, request.user)
    except ValueError as e:
        return api_error("state_conflict", str(e), status=409)

    return JsonResponse({"id": r.pk, "status": r.status}, status=200)


@login_required
@require_http_methods(["POST"])
def api_reserve_units(request, pk):
    """POST /api/v1/requests/{id}/reserve — Reserve compatible units atomically (SADS 8.2, FR-22)."""
    if not allowed(request.user, STAFF):
        deny(request.user)
        return api_error("forbidden", "Only blood bank staff can reserve units.", status=403)
    r = get_object_or_404(BloodRequest.objects.select_related("branch"), pk=pk)
    if not in_branch_scope(request.user, r.branch_id):
        deny(request.user, "Cross-branch API reservation denied")
        return api_error("forbidden", "Cross-branch access denied.", status=403)

    data = _parse_payload(request) or {}
    idem_key = _get_idempotency_key(request, data)
    cache_key = f"idem:reserve:{r.pk}:{idem_key}" if idem_key else None
    if cache_key and cache.get(cache_key):
        return JsonResponse(cache.get(cache_key), status=200)

    try:
        made = reserve_units(r, request.user)
    except ValueError as e:
        return api_error("reservation_conflict", str(e), status=409)

    payload = {
        "request_id": r.pk,
        "reserved_count": len(made),
        "reservations": [{"id": res.pk, "unit_id": res.unit.unit_id} for res in made],
    }
    if cache_key:
        cache.set(cache_key, payload, 3600)
    return JsonResponse(payload, status=200)


@login_required
@require_http_methods(["POST"])
def api_issue_unit(request):
    """POST /api/v1/issues — Issue cross-matched units atomically (SADS 8.2, FR-23, FR-24)."""
    if not allowed(request.user, STAFF):
        deny(request.user)
        return api_error("forbidden", "Only blood bank staff can issue units.", status=403)
    data = _parse_payload(request)
    if data is None:
        return api_error("invalid_json", "Request body must be valid JSON.", status=400)

    res_id = data.get("reservation_id")
    if not res_id:
        return api_error(
            "validation_error",
            "reservation_id is required.",
            status=400,
            field_errors={"reservation_id": ["This field is required."]},
        )

    reservation = get_object_or_404(Reservation.objects.select_related("unit", "request__branch"), pk=res_id)
    if not in_branch_scope(request.user, reservation.request.branch_id):
        deny(request.user, "Cross-branch API issue denied")
        return api_error("forbidden", "Cross-branch access denied.", status=403)

    idem_key = _get_idempotency_key(request, data)
    cache_key = f"idem:issue:{reservation.pk}:{idem_key}" if idem_key else None
    if cache_key and cache.get(cache_key):
        return JsonResponse(cache.get(cache_key), status=200)

    try:
        issue_reservation(reservation, request.user)
    except ValueError as e:
        return api_error("issue_conflict", str(e), status=409)

    reservation.refresh_from_db()
    reservation.unit.refresh_from_db()
    reservation.request.refresh_from_db()
    payload = {
        "reservation_id": reservation.pk,
        "unit_id": reservation.unit.unit_id,
        "unit_status": reservation.unit.status,
        "request_id": reservation.request.pk,
        "request_status": reservation.request.status,
    }
    if cache_key:
        cache.set(cache_key, payload, 3600)
    return JsonResponse(payload, status=201)

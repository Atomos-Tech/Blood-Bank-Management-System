from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_http_methods, require_POST
from .forms import (AppointmentForm, BloodRequestForm, CrossMatchForm, DeferralForm,
                    DonorForm, DonationForm, LabResultForm)
from .models import (Appointment, AppointmentSlot, AuditEvent, BloodRequest, BloodUnit, Branch,
                     CrossMatch, Deferral, Donation, Donor, Issue, LabResult, Notification,
                     Reservation, ShelfLifeRule, TestType, UserProfile)
from .services import (audit, check_stock_and_expiry_alerts, eligibility, expire_units,
                       get_branch_warnings, issue_reservation, notify, release_reservation,
                       release_screened_unit, reserve_units, transition_request)

STAFF = {UserProfile.Role.STAFF, UserProfile.Role.ADMIN}
ADMIN = {UserProfile.Role.ADMIN}


def role(user):
    return getattr(user.profile, "role", None) if user.is_authenticated and hasattr(user, "profile") else None


def allowed(user, roles):
    if not user.is_authenticated:
        return False
    if hasattr(user, "profile") and not user.profile.active:
        return False
    return user.is_superuser or role(user) in roles


def user_branch(user):
    if not user.is_authenticated or user.is_superuser or not hasattr(user, "profile"):
        return None
    if user.profile.role in {UserProfile.Role.STAFF, UserProfile.Role.HOSPITAL}:
        return user.profile.branch
    return None


def in_branch_scope(user, target_branch):
    if user.is_superuser or role(user) == UserProfile.Role.ADMIN:
        return True
    ub = user_branch(user)
    if ub is None or target_branch is None:
        return True
    target_id = getattr(target_branch, "pk", target_branch)
    return str(ub.pk) == str(target_id) or ub.code == str(target_branch)


def deny(user, summary=""):
    if user.is_authenticated:
        AuditEvent.objects.create(
            actor=user,
            branch=getattr(getattr(user, "profile", None), "branch", None),
            event_type="UNAUTHORIZED_ACCESS",
            record_type="Security",
            record_id="N/A",
            outcome="DENIED",
            summary=summary,
        )
    return HttpResponseForbidden("This account is not permitted to perform that action.")


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


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        key = f"login:{email}"
        locked = cache.get(f"{key}:locked")
        if locked:
            messages.error(request, "Invalid email or password.")
            return render(request, "bloodbank/login.html")
        user = authenticate(request, username=email, password=password)
        if not user or not hasattr(user, "profile") or not user.profile.active:
            attempts = int(cache.get(key, 0)) + 1
            cache.set(key, attempts, 600)
            if attempts >= 5:
                cache.set(f"{key}:locked", True, 900)
            messages.error(request, "Invalid email or password.")
            return render(request, "bloodbank/login.html")
        cache.delete(key)
        login(request, user)
        audit(user, "LOGIN", user, summary="Authenticated session started")
        return redirect("dashboard")
    return render(request, "bloodbank/login.html")


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit(request.user, "LOGOUT", request.user, summary="Authenticated session ended")
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
        "activity": audit_qs[:8] if allowed(request.user, STAFF) else [],
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
        donor = form.save(commit=False)
        if ub and donor.branch_id is None:
            donor.branch = ub
        donor.save()
        audit(request.user, "DONOR_REGISTERED", donor, summary="Consent captured", branch=donor.branch)
        messages.success(request, "Donor registered.")
        return redirect("donors")
    donors_qs = Donor.objects.order_by("-created_at")
    if ub:
        donors_qs = donors_qs.filter(Q(branch=ub) | Q(branch__isnull=True))
    elif req_branch:
        donors_qs = donors_qs.filter(branch_id=req_branch)
    return render(request, "bloodbank/donors.html", {"donors": donors_qs, "form": form})


@login_required
def my_donor_record(request):
    if role(request.user) != UserProfile.Role.DONOR and not request.user.is_superuser:
        return deny(request.user)
    donor_param = request.GET.get("donor")
    donor = get_object_or_404(Donor, user=request.user)
    if donor_param and str(donor.pk) != str(donor_param):
        return deny(request.user, "Attempted access to another donor record")
    ok, reasons = eligibility(donor)
    return render(
        request,
        "bloodbank/my_donor.html",
        {
            "donor": donor,
            "eligible": ok,
            "reasons": reasons,
            "appointments": donor.appointments.select_related("slot", "slot__branch").order_by("-slot__starts_at"),
            "donations": donor.donations.order_by("-collected_at"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def appointment_view(request):
    ub = user_branch(request.user)
    if role(request.user) == UserProfile.Role.DONOR:
        donor = get_object_or_404(Donor, user=request.user)
    elif allowed(request.user, STAFF):
        donor = get_object_or_404(Donor, pk=request.GET.get("donor")) if request.GET.get("donor") else None
        if donor and donor.branch_id and not in_branch_scope(request.user, donor.branch_id):
            return deny(request.user, "Cross-branch donor appointment denied")
    else:
        return deny(request.user)
    if not donor:
        return redirect("donors")
    form = AppointmentForm(request.POST or None, branch=ub)
    if request.method == "POST" and form.is_valid():
        ok, reasons = eligibility(donor)
        slot = form.cleaned_data["slot"]
        if role(request.user) in STAFF and not in_branch_scope(request.user, slot.branch_id):
            return deny(request.user, "Cross-branch slot booking denied")
        if not ok:
            form.add_error(None, "Appointment blocked: " + " ".join(reasons))
        elif slot.available <= 0:
            form.add_error("slot", "This slot is full.")
        else:
            Appointment.objects.create(donor=donor, slot=slot)
            audit(request.user, "APPOINTMENT_BOOKED", donor, summary=str(slot.starts_at), branch=slot.branch)
            notify(
                donor.email,
                "Donation appointment confirmed",
                f"Appointment at {slot.branch.name} on {slot.starts_at}.",
            )
            messages.success(request, "Appointment booked.")
            return redirect("my_donor" if role(request.user) == UserProfile.Role.DONOR else "donors")
    return render(
        request,
        "bloodbank/form.html",
        {
            "title": "Book appointment",
            "form": form,
            "cancel": "my_donor" if role(request.user) == UserProfile.Role.DONOR else "donors",
        },
    )


@login_required
@require_POST
def cancel_appointment(request, pk):
    ap = get_object_or_404(Appointment.objects.select_related("donor", "slot__branch"), pk=pk)
    if role(request.user) == UserProfile.Role.DONOR and ap.donor.user_id != request.user.id:
        return deny(request.user)
    if not (role(request.user) == UserProfile.Role.DONOR or allowed(request.user, STAFF)):
        return deny(request.user)
    if role(request.user) == UserProfile.Role.STAFF and not in_branch_scope(request.user, ap.slot.branch_id):
        return deny(request.user, "Cross-branch appointment cancellation denied")
    ap.status = Appointment.Status.CANCELLED
    ap.save(update_fields=["status"])
    audit(request.user, "APPOINTMENT_CANCELLED", ap, branch=ap.slot.branch)
    return redirect("my_donor" if role(request.user) == UserProfile.Role.DONOR else "donors")


@login_required
@require_http_methods(["GET", "POST"])
def deferral_view(request):
    if not allowed(request.user, STAFF):
        return deny(request.user)
    ub = user_branch(request.user)
    form = DeferralForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.save(commit=False)
        if d.donor.branch_id and not in_branch_scope(request.user, d.donor.branch_id):
            return deny(request.user, "Cross-branch deferral denied")
        d.approved_by = request.user
        if not d.permanent and not d.end_date:
            form.add_error("end_date", "Temporary deferrals require an end date.")
        else:
            d.save()
            audit(request.user, "DEFERRAL_RECORDED", d, branch=d.donor.branch or ub)
            messages.success(request, "Deferral recorded.")
            return redirect("deferrals")
    items = Deferral.objects.select_related("donor", "approved_by")
    if ub:
        items = items.filter(Q(donor__branch=ub) | Q(donor__branch__isnull=True))
    return render(request, "bloodbank/deferrals.html", {"form": form, "items": items})


@login_required
@require_http_methods(["GET", "POST"])
def inventory(request):
    if not allowed(request.user, {UserProfile.Role.STAFF, UserProfile.Role.ADMIN, UserProfile.Role.HOSPITAL}):
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

    from django.core.paginator import Paginator
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
        rule = ShelfLifeRule.objects.filter(component=data["component"]).first()
        if not rule:
            form.add_error("component", "No approved shelf-life rule exists for this component.")
        else:
            with transaction.atomic():
                stamp = timezone.now()
                donation = Donation.objects.create(
                    donation_id=f"DON-{stamp:%Y%m%d%H%M%S%f}",
                    donor=data["donor"],
                    collected_at=stamp,
                    collector=request.user,
                    branch=data["branch"],
                )
                unit = BloodUnit.objects.create(
                    unit_id=f"UNIT-{stamp:%Y%m%d%H%M%S%f}",
                    donation=donation,
                    blood_group=data["blood_group"],
                    component=data["component"],
                    volume_ml=data["volume_ml"],
                    expiry_at=stamp + timedelta(days=rule.days),
                    branch=data["branch"],
                    status=BloodUnit.Status.COLLECTED,
                )
                audit(request.user, "DONATION_RECORDED", donation, summary=unit.unit_id, branch=data["branch"])
            messages.success(
                request,
                f"Donation and unit {unit.unit_id} recorded. Expiry was calculated from the configured shelf-life rule.",
            )
            return redirect("inventory")
    return render(
        request,
        "bloodbank/form.html",
        {"title": "Record donation and component unit", "form": form, "cancel": "inventory"},
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
        result = form.save(commit=False)
        result.unit = unit
        result.recorded_by = request.user
        try:
            result.save()
        except IntegrityError:
            form.add_error("test_type", "A result for this test type is already recorded.")
        else:
            release_screened_unit(unit, request.user)
            messages.success(request, f"Result recorded. Unit is now {unit.get_status_display()}.")
            return redirect("inventory")
    return render(
        request,
        "bloodbank/screening.html",
        {"unit": unit, "form": form, "results": unit.lab_results.select_related("test_type")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def request_list(request):
    if not allowed(request.user, {UserProfile.Role.STAFF, UserProfile.Role.ADMIN, UserProfile.Role.HOSPITAL}):
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

    if request.method == "POST" and form.is_valid():
        r = form.save(commit=False)
        if not in_branch_scope(request.user, r.branch_id):
            return deny(request.user, "Cross-branch blood request creation denied")
        r.created_by = request.user
        r.hospital = request.user.profile.hospital_name
        if not r.hospital:
            form.add_error(None, "Hospital staff account is missing its verified hospital assignment.")
        else:
            r.save()
            audit(request.user, "BLOOD_REQUEST_CREATED", r, branch=r.branch)
            messages.success(request, "Blood request submitted.")
            return redirect("requests")
    return render(request, "bloodbank/requests.html", {"requests": qs.order_by("-created_at"), "form": form})


@login_required
@require_POST
def request_action(request, pk, action):
    r = get_object_or_404(BloodRequest.objects.select_related("branch", "created_by"), pk=pk)
    if not in_branch_scope(request.user, r.branch_id):
        return deny(request.user, "Cross-branch request action denied")
    if action == "cancel":
        if role(request.user) == UserProfile.Role.HOSPITAL:
            if r.created_by_id != request.user.id or r.status != BloodRequest.Status.PENDING:
                return deny(request.user, "Hospital staff can cancel only their own Pending requests")
        elif not allowed(request.user, STAFF):
            return deny(request.user)
        try:
            transition_request(r, BloodRequest.Status.CANCELLED, request.user)
        except ValueError as e:
            messages.error(request, str(e))
    else:
        if not allowed(request.user, STAFF):
            return deny(request.user)
        target = {
            "approve": BloodRequest.Status.APPROVED,
            "reject": BloodRequest.Status.REJECTED,
        }.get(action)
        if not target:
            return HttpResponseForbidden("Unsupported action")
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
        CrossMatch.objects.update_or_create(
            reservation=reservation,
            defaults={"outcome": form.cleaned_data["outcome"], "recorded_by": request.user},
        )
        audit(request.user, "CROSSMATCH_RECORDED", reservation, branch=reservation.request.branch)
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
def audit_view(request):
    if not allowed(request.user, STAFF):
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


@login_required
def reports(request):
    if not allowed(request.user, {UserProfile.Role.STAFF, UserProfile.Role.ADMIN, UserProfile.Role.HOSPITAL}):
        return deny(request.user)
    expire_units()
    ub = user_branch(request.user)
    req_branch = request.GET.get("branch")
    if req_branch and not in_branch_scope(request.user, req_branch):
        return deny(request.user, "Cross-branch report access denied")

    units = BloodUnit.objects.select_related("branch").all()
    requests_qs = BloodRequest.objects.select_related("branch").all()
    donations = Donation.objects.select_related("branch").all()
    audits = AuditEvent.objects.all()

    if ub:
        units = units.filter(branch=ub)
        requests_qs = requests_qs.filter(branch=ub)
        donations = donations.filter(branch=ub)
        audits = audits.filter(branch=ub)
    elif req_branch:
        units = units.filter(branch_id=req_branch)
        requests_qs = requests_qs.filter(branch_id=req_branch)
        donations = donations.filter(branch_id=req_branch)
        audits = audits.filter(branch_id=req_branch)

    if role(request.user) == UserProfile.Role.HOSPITAL:
        avail_units = units.filter(status=BloodUnit.Status.AVAILABLE, expiry_at__gt=timezone.now()).order_by("expiry_at", "id")
        own_requests = requests_qs.filter(created_by=request.user).order_by("-created_at")
        return render(
            request,
            "bloodbank/reports.html",
            {
                "is_hospital": True,
                "units": avail_units,
                "requests": own_requests,
            },
        )

    return render(
        request,
        "bloodbank/reports.html",
        {
            "is_hospital": False,
            "units": units.order_by("expiry_at", "id"),
            "requests": requests_qs.order_by("-created_at"),
            "donations": donations.order_by("-collected_at"),
            "audit_count": audits.count(),
        },
    )


@login_required
def api_inventory(request):
    if not allowed(request.user, {UserProfile.Role.STAFF, UserProfile.Role.ADMIN, UserProfile.Role.HOSPITAL}):
        deny(request.user)
        return JsonResponse({"code": "forbidden", "message": "Not authorized"}, status=403)
    expire_units()
    ub = user_branch(request.user)
    p = request.GET
    if p.get("branch") and not in_branch_scope(request.user, p["branch"]):
        deny(request.user, "Cross-branch API inventory access denied")
        return JsonResponse({"code": "forbidden", "message": "Cross-branch access denied"}, status=403)

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

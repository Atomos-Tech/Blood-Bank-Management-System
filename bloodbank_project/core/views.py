from datetime import timedelta
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from .forms import DonorSignUpForm, HospitalSignUpForm, RecordDonationForm, TestResultForm, BloodRequestForm
from .models import (
    User, Donor, Donation, BloodUnit, BloodRequest, Branch, COMPATIBILITY, BLOOD_GROUPS,
    Appointment, Notification, SystemSetting, notify,
)
from .forms import AppointmentForm, SystemSettingForm


class RoleLoginView(LoginView):
    template_name = "registration/login.html"


def home(request):
    available_units = BloodUnit.objects.filter(status=BloodUnit.Status.AVAILABLE).count()
    total_donors = Donor.objects.count()
    total_branches = Branch.objects.count()
    units_issued_all_time = BloodUnit.objects.filter(status=BloodUnit.Status.ISSUED).count()
    return render(request, "core/home.html", {
        "available_units": available_units,
        "total_donors": total_donors,
        "total_branches": total_branches,
        "units_issued_all_time": units_issued_all_time,
    })


def signup_donor(request):
    if request.method == "POST":
        form = DonorSignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Welcome! Your donor profile has been created.")
            return redirect("dashboard")
    else:
        form = DonorSignUpForm()
    return render(request, "core/signup_donor.html", {"form": form})


def signup_hospital(request):
    if request.method == "POST":
        form = HospitalSignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Hospital account created.")
            return redirect("dashboard")
    else:
        form = HospitalSignUpForm()
    return render(request, "core/signup_hospital.html", {"form": form})


@login_required
def dashboard(request):
    role = request.user.role
    if role == User.Role.DONOR:
        return redirect("donor_dashboard")
    elif role == User.Role.STAFF:
        return redirect("staff_dashboard")
    elif role == User.Role.HOSPITAL:
        return redirect("hospital_dashboard")
    elif role == User.Role.ADMIN:
        return redirect("/admin/")
    return redirect("home")


# ---------------- DONOR ----------------
@login_required
def donor_dashboard(request):
    donor = get_object_or_404(Donor, user=request.user)
    eligible, reasons = donor.is_eligible()
    donations = donor.donations.order_by("-donation_date")
    settings = SystemSetting.load()
    next_eligible_date = None
    if donor.last_donation_date:
        next_eligible_date = donor.last_donation_date + timedelta(days=settings.min_donation_gap_days)
    upcoming_appointments = donor.appointments.filter(status=Appointment.Status.SCHEDULED)
    return render(request, "core/donor_dashboard.html", {
        "donor": donor, "eligible": eligible, "reasons": reasons,
        "donations": donations, "next_eligible_date": next_eligible_date,
        "upcoming_appointments": upcoming_appointments,
    })


@login_required
def book_appointment(request):
    donor = get_object_or_404(Donor, user=request.user)
    if request.method == "POST":
        form = AppointmentForm(request.POST)
        if form.is_valid():
            appt = form.save(commit=False)
            appt.donor = donor
            appt.save()
            messages.success(request, f"Appointment booked for {appt.scheduled_date} at {appt.scheduled_time}.")
            return redirect("donor_dashboard")
    else:
        form = AppointmentForm(initial={"branch": donor.branch})
    return render(request, "core/book_appointment.html", {"form": form})


@login_required
def cancel_appointment(request, appointment_id):
    appt = get_object_or_404(Appointment, id=appointment_id, donor__user=request.user)
    appt.status = Appointment.Status.CANCELLED
    appt.save()
    messages.info(request, "Appointment cancelled.")
    return redirect("donor_dashboard")


@login_required
def notifications_view(request):
    notes = request.user.notifications.all()
    notes.filter(is_read=False).update(is_read=True)
    return render(request, "core/notifications.html", {"notes": notes})


# ---------------- STAFF ----------------
def is_staff_role(u):
    return u.is_authenticated and u.role == User.Role.STAFF


@login_required
@user_passes_test(is_staff_role)
def staff_dashboard(request):
    summary = {}
    for group in BLOOD_GROUPS:
        summary[group] = BloodUnit.objects.filter(blood_group=group, status=BloodUnit.Status.AVAILABLE).count()
    pending_requests = BloodRequest.objects.filter(status=BloodRequest.Status.PENDING).order_by("-is_emergency", "created_at")
    pending_tests = Donation.objects.filter(test_result=Donation.TestResult.PENDING)
    return render(request, "core/staff_dashboard.html", {
        "summary": summary, "pending_requests": pending_requests, "pending_tests": pending_tests,
    })


@login_required
@user_passes_test(is_staff_role)
def record_donation(request):
    if request.method == "POST":
        form = RecordDonationForm(request.POST)
        if form.is_valid():
            donation = form.save(commit=False)
            donation.recorded_by = request.user
            donation.save()
            messages.success(request, f"Donation #{donation.id} recorded. Now pending lab testing.")
            return redirect("staff_dashboard")
    else:
        form = RecordDonationForm()
    return render(request, "core/record_donation.html", {"form": form})


@login_required
@user_passes_test(is_staff_role)
def record_test_result(request, donation_id):
    donation = get_object_or_404(Donation, id=donation_id)
    if request.method == "POST":
        form = TestResultForm(request.POST, instance=donation)
        if form.is_valid():
            donation = form.save()
            settings = SystemSetting.load()
            unit, created = BloodUnit.objects.get_or_create(
                donation=donation,
                defaults={
                    "blood_group": donation.donor.blood_group,
                    "branch": donation.branch,
                    "collection_date": donation.donation_date,
                    "expiry_date": donation.donation_date + timedelta(days=settings.unit_shelf_life_days),
                },
            )
            unit.refresh_status()
            notify(donation.donor.user, f"Your donation on {donation.donation_date} passed testing — thank you for saving lives!" if donation.test_result == Donation.TestResult.PASSED else f"Your donation on {donation.donation_date} did not pass screening. Contact staff for details.")
            messages.success(request, f"Test result recorded. Unit status: {unit.status}.")
            return redirect("staff_dashboard")
    else:
        form = TestResultForm(instance=donation)
    return render(request, "core/record_test_result.html", {"form": form, "donation": donation})


@login_required
@user_passes_test(is_staff_role)
def inventory_list(request):
    units = BloodUnit.objects.all().order_by("blood_group", "expiry_date")
    today = timezone.now().date()
    for u in units:
        if u.expiry_date < today and u.status not in (BloodUnit.Status.ISSUED, BloodUnit.Status.EXPIRED):
            u.status = BloodUnit.Status.EXPIRED
            u.save()
    return render(request, "core/inventory_list.html", {"units": units})


@login_required
@user_passes_test(is_staff_role)
def manage_request(request, request_id):
    br = get_object_or_404(BloodRequest, id=request_id)
    action = request.POST.get("action")
    if action == "approve":
        br.status = BloodRequest.Status.APPROVED
        br.save()
        notify(br.hospital, f"Request #{br.id} ({br.blood_group} x{br.units_needed}) was approved.", "hospital_dashboard")
        messages.success(request, f"Request #{br.id} approved.")
    elif action == "reject":
        br.status = BloodRequest.Status.REJECTED
        br.save()
        notify(br.hospital, f"Request #{br.id} ({br.blood_group} x{br.units_needed}) was rejected.", "hospital_dashboard")
        messages.warning(request, f"Request #{br.id} rejected.")
    elif action == "issue":
        compatible_groups = COMPATIBILITY.get(br.blood_group, [br.blood_group])
        available = BloodUnit.objects.filter(blood_group__in=compatible_groups, status=BloodUnit.Status.AVAILABLE)[: br.units_needed]
        if available.count() < br.units_needed:
            messages.error(request, "Not enough compatible units available to fully issue this request.")
        else:
            for unit in available:
                unit.status = BloodUnit.Status.ISSUED
                unit.save()
                br.issued_units.add(unit)
            br.status = BloodRequest.Status.ISSUED
            br.save()
            notify(br.hospital, f"Request #{br.id} has been issued — {available.count()} unit(s) dispatched.", "hospital_dashboard")
            messages.success(request, f"Request #{br.id} issued with {available.count()} unit(s).")
    return redirect("staff_dashboard")


# ---------------- HOSPITAL ----------------
def is_hospital_role(u):
    return u.is_authenticated and u.role == User.Role.HOSPITAL


@login_required
@user_passes_test(is_hospital_role)
def hospital_dashboard(request):
    my_requests = BloodRequest.objects.filter(hospital=request.user).order_by("-created_at")
    return render(request, "core/hospital_dashboard.html", {"requests": my_requests})


@login_required
@user_passes_test(is_hospital_role)
def search_availability(request):
    branch_id = request.GET.get("branch")
    group = request.GET.get("blood_group")
    units = BloodUnit.objects.filter(status=BloodUnit.Status.AVAILABLE)
    if branch_id:
        units = units.filter(branch_id=branch_id)
    if group:
        units = units.filter(blood_group=group)
    counts = {}
    for g in BLOOD_GROUPS:
        counts[g] = units.filter(blood_group=g).count()
    branches = Branch.objects.all()
    return render(request, "core/search_availability.html", {
        "counts": counts, "branches": branches, "selected_branch": branch_id, "selected_group": group,
    })


@login_required
@user_passes_test(is_hospital_role)
def create_request(request):
    if request.method == "POST":
        form = BloodRequestForm(request.POST)
        if form.is_valid():
            br = form.save(commit=False)
            br.hospital = request.user
            br.save()
            messages.success(request, f"Blood request #{br.id} submitted.")
            return redirect("hospital_dashboard")
    else:
        form = BloodRequestForm()
    return render(request, "core/create_request.html", {"form": form})


@login_required
@user_passes_test(is_hospital_role)
def track_request(request, request_id):
    br = get_object_or_404(BloodRequest, id=request_id, hospital=request.user)
    return render(request, "core/track_request.html", {"br": br})


def branch_list(request):
    branches = Branch.objects.all()
    return render(request, "core/branch_list.html", {"branches": branches})


@login_required
@user_passes_test(is_staff_role)
def staff_appointments(request):
    upcoming = Appointment.objects.filter(status=Appointment.Status.SCHEDULED).order_by("scheduled_date", "scheduled_time")
    return render(request, "core/staff_appointments.html", {"appointments": upcoming})


@login_required
@user_passes_test(is_staff_role)
def mark_appointment_done(request, appointment_id):
    appt = get_object_or_404(Appointment, id=appointment_id)
    appt.status = Appointment.Status.COMPLETED
    appt.save()
    messages.success(request, f"Marked appointment for {appt.donor} as completed.")
    return redirect("staff_appointments")


def _is_staff_or_admin(u):
    return u.is_authenticated and u.role in (User.Role.STAFF, User.Role.ADMIN)


@login_required
@user_passes_test(_is_staff_or_admin)
def reports_view(request):
    import json
    stock_by_group = {g: BloodUnit.objects.filter(blood_group=g, status=BloodUnit.Status.AVAILABLE).count() for g in BLOOD_GROUPS}

    today = timezone.now().date()
    days = [(today - timedelta(days=i)) for i in range(13, -1, -1)]
    donation_trend = []
    for d in days:
        donation_trend.append(Donation.objects.filter(donation_date=d).count())

    total_donors = Donor.objects.count()
    total_donations = Donation.objects.count()
    total_issued_units = BloodUnit.objects.filter(status=BloodUnit.Status.ISSUED).count()
    expired_units = BloodUnit.objects.filter(status=BloodUnit.Status.EXPIRED).count()
    settings = SystemSetting.load()
    low_stock = [g for g, c in stock_by_group.items() if c <= settings.low_stock_threshold]

    return render(request, "core/reports.html", {
        "stock_labels": json.dumps(list(stock_by_group.keys())),
        "stock_values": json.dumps(list(stock_by_group.values())),
        "trend_labels": json.dumps([d.strftime("%b %d") for d in days]),
        "trend_values": json.dumps(donation_trend),
        "total_donors": total_donors,
        "total_donations": total_donations,
        "total_issued_units": total_issued_units,
        "expired_units": expired_units,
        "low_stock": low_stock,
    })


def is_admin_role(u):
    return u.is_authenticated and u.role == User.Role.ADMIN


@login_required
@user_passes_test(is_admin_role)
def settings_view(request):
    settings = SystemSetting.load()
    if request.method == "POST":
        form = SystemSettingForm(request.POST, instance=settings)
        if form.is_valid():
            form.save()
            messages.success(request, "System settings updated.")
            return redirect("settings_view")
    else:
        form = SystemSettingForm(instance=settings)
    return render(request, "core/settings.html", {"form": form})

from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from .forms import AppointmentForm, BloodRequestForm, CrossMatchForm, DeferralForm, DonorForm, DonationForm, LabResultForm
from .models import (Appointment, AppointmentSlot, AuditEvent, BloodRequest, BloodUnit, CrossMatch, Deferral, Donation,
                     Donor, Issue, LabResult, Notification, Reservation, ShelfLifeRule, TestType, UserProfile)
from .services import audit, eligibility, expire_units, issue_reservation, notify, release_screened_unit, reserve_units, transition_request

STAFF = {UserProfile.Role.STAFF, UserProfile.Role.ADMIN}
ADMIN = {UserProfile.Role.ADMIN}

def role(user): return getattr(user.profile, "role", None) if user.is_authenticated and hasattr(user, "profile") else None
def allowed(user, roles): return user.is_superuser or role(user) in roles
def deny(user):
    if user.is_authenticated: AuditEvent.objects.create(actor=user,event_type="UNAUTHORIZED_ACCESS",record_type="Security",record_id="N/A",outcome="DENIED")
    return HttpResponseForbidden("This account is not permitted to perform that action.")

@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated: return redirect("dashboard")
    if request.method == "POST":
        email=request.POST.get("email", "").strip().lower(); password=request.POST.get("password", ""); key=f"login:{email}"; locked=cache.get(f"{key}:locked")
        if locked:
            messages.error(request,"Invalid email or password."); return render(request,"bloodbank/login.html")
        user=authenticate(request,username=email,password=password)
        if not user or not hasattr(user,"profile") or not user.profile.active:
            attempts=int(cache.get(key,0))+1; cache.set(key,attempts,600)
            if attempts >= 5: cache.set(f"{key}:locked",True,900)
            messages.error(request,"Invalid email or password."); return render(request,"bloodbank/login.html")
        cache.delete(key); login(request,user); audit(user,"LOGIN",user,summary="Authenticated session started"); return redirect("dashboard")
    return render(request,"bloodbank/login.html")

@require_POST
def logout_view(request):
    if request.user.is_authenticated: audit(request.user,"LOGOUT",request.user,summary="Authenticated session ended")
    logout(request); return redirect("login")

@login_required
def dashboard(request):
    expire_units()
    prof=request.user.profile
    units=BloodUnit.objects.all()
    if prof.branch and role(request.user) in {UserProfile.Role.STAFF, UserProfile.Role.HOSPITAL}: units=units.filter(branch=prof.branch)
    context={"units":units.order_by("expiry_at")[:8], "donor_count":Donor.objects.count(), "available":units.filter(status=BloodUnit.Status.AVAILABLE).count(), "testing":units.filter(status=BloodUnit.Status.TESTING).count(), "active_requests":BloodRequest.objects.exclude(status__in=[BloodRequest.Status.FULFILLED,BloodRequest.Status.REJECTED,BloodRequest.Status.CANCELLED]).count(), "activity":AuditEvent.objects.all()[:8] if allowed(request.user,STAFF) else []}
    return render(request,"bloodbank/dashboard.html",context)

@login_required
@require_http_methods(["GET","POST"])
def donor_list(request):
    if not allowed(request.user, STAFF): return deny(request.user)
    form=DonorForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        donor=form.save(); audit(request.user,"DONOR_REGISTERED",donor,summary="Consent captured"); messages.success(request,"Donor registered."); return redirect("donors")
    return render(request,"bloodbank/donors.html",{"donors":Donor.objects.order_by("-created_at"),"form":form})

@login_required
def my_donor_record(request):
    donor=get_object_or_404(Donor,user=request.user); ok,reasons=eligibility(donor)
    return render(request,"bloodbank/my_donor.html",{"donor":donor,"eligible":ok,"reasons":reasons,"appointments":donor.appointments.select_related("slot","slot__branch").order_by("-slot__starts_at"),"donations":donor.donations.order_by("-collected_at")})

@login_required
@require_http_methods(["GET","POST"])
def appointment_view(request):
    if role(request.user)==UserProfile.Role.DONOR: donor=get_object_or_404(Donor,user=request.user)
    elif allowed(request.user, STAFF): donor=get_object_or_404(Donor,pk=request.GET.get("donor")) if request.GET.get("donor") else None
    else: return deny(request.user)
    if not donor: return redirect("donors")
    form=AppointmentForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        ok,reasons=eligibility(donor); slot=form.cleaned_data["slot"]
        if not ok: form.add_error(None,"Appointment blocked: " + " ".join(reasons))
        elif slot.available <= 0: form.add_error("slot","This slot is full.")
        else:
            Appointment.objects.create(donor=donor,slot=slot); audit(request.user,"APPOINTMENT_BOOKED",donor,summary=str(slot.starts_at)); notify(donor.email,"Donation appointment confirmed",f"Appointment at {slot.branch.name} on {slot.starts_at}."); messages.success(request,"Appointment booked."); return redirect("my_donor" if role(request.user)==UserProfile.Role.DONOR else "donors")
    return render(request,"bloodbank/form.html",{"title":"Book appointment","form":form,"cancel":"my_donor" if role(request.user)==UserProfile.Role.DONOR else "donors"})

@login_required
@require_POST
def cancel_appointment(request, pk):
    ap=get_object_or_404(Appointment,pk=pk)
    if role(request.user)==UserProfile.Role.DONOR and ap.donor.user_id!=request.user.id: return deny(request.user)
    if not (role(request.user)==UserProfile.Role.DONOR or allowed(request.user,STAFF)): return deny(request.user)
    ap.status=Appointment.Status.CANCELLED; ap.save(update_fields=["status"]); audit(request.user,"APPOINTMENT_CANCELLED",ap); return redirect("my_donor" if role(request.user)==UserProfile.Role.DONOR else "donors")

@login_required
@require_http_methods(["GET","POST"])
def deferral_view(request):
    if not allowed(request.user,STAFF): return deny(request.user)
    form=DeferralForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        d=form.save(commit=False); d.approved_by=request.user
        if not d.permanent and not d.end_date: form.add_error("end_date","Temporary deferrals require an end date.")
        else: d.save(); audit(request.user,"DEFERRAL_RECORDED",d); messages.success(request,"Deferral recorded."); return redirect("deferrals")
    return render(request,"bloodbank/deferrals.html",{"form":form,"items":Deferral.objects.select_related("donor","approved_by")})

@login_required
@require_http_methods(["GET","POST"])
def inventory(request):
    if not allowed(request.user,{UserProfile.Role.STAFF,UserProfile.Role.ADMIN,UserProfile.Role.HOSPITAL}): return deny(request.user)
    expire_units(); units=BloodUnit.objects.select_related("donation__donor","branch").all()
    p=request.GET
    if p.get("blood_group"): units=units.filter(blood_group=p["blood_group"])
    if p.get("component"): units=units.filter(component__icontains=p["component"])
    if p.get("branch"): units=units.filter(branch_id=p["branch"])
    if role(request.user)==UserProfile.Role.HOSPITAL: units=units.filter(status=BloodUnit.Status.AVAILABLE)
    return render(request,"bloodbank/inventory.html",{"units":units.order_by("expiry_at"),"branches":__import__('bloodbank.models',fromlist=['Branch']).Branch.objects.all()})

@login_required
@require_http_methods(["GET","POST"])
def donation_view(request):
    if not allowed(request.user,STAFF): return deny(request.user)
    form=DonationForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        data=form.cleaned_data; rule=ShelfLifeRule.objects.filter(component=data["component"]).first()
        if not rule: form.add_error("component","No approved shelf-life rule exists for this component.")
        else:
            with transaction.atomic():
                stamp=timezone.now(); donation=Donation.objects.create(donation_id=f"DON-{stamp:%Y%m%d%H%M%S%f}",donor=data["donor"],collected_at=stamp,collector=request.user,branch=data["branch"])
                unit=BloodUnit.objects.create(unit_id=f"UNIT-{stamp:%Y%m%d%H%M%S%f}",donation=donation,blood_group=data["blood_group"],component=data["component"],volume_ml=data["volume_ml"],expiry_at=stamp+timedelta(days=rule.days),branch=data["branch"],status=BloodUnit.Status.COLLECTED)
                audit(request.user,"DONATION_RECORDED",donation,summary=unit.unit_id)
            messages.success(request,f"Donation and unit {unit.unit_id} recorded. Expiry was calculated from the configured shelf-life rule."); return redirect("inventory")
    return render(request,"bloodbank/form.html",{"title":"Record donation and component unit","form":form,"cancel":"inventory"})

@login_required
@require_http_methods(["GET","POST"])
def screening_view(request, pk):
    if not allowed(request.user,STAFF): return deny(request.user)
    unit=get_object_or_404(BloodUnit,pk=pk); form=LabResultForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        result=form.save(commit=False); result.unit=unit; result.recorded_by=request.user
        try: result.save()
        except IntegrityError: form.add_error("test_type","A result for this test type is already recorded.")
        else:
            status=release_screened_unit(unit,request.user); messages.success(request,f"Result recorded. Unit is now {unit.get_status_display()}."); return redirect("inventory")
    return render(request,"bloodbank/screening.html",{"unit":unit,"form":form,"results":unit.lab_results.select_related("test_type")})

@login_required
@require_http_methods(["GET","POST"])
def request_list(request):
    if not allowed(request.user,{UserProfile.Role.STAFF,UserProfile.Role.ADMIN,UserProfile.Role.HOSPITAL}): return deny(request.user)
    form=BloodRequestForm(request.POST or None); qs=BloodRequest.objects.select_related("branch","created_by")
    if role(request.user)==UserProfile.Role.HOSPITAL: qs=qs.filter(created_by=request.user)
    if request.method=="POST" and form.is_valid():
        r=form.save(commit=False); r.created_by=request.user; r.hospital=request.user.profile.hospital_name
        if not r.hospital: form.add_error(None,"Hospital staff account is missing its verified hospital assignment.")
        else: r.save(); audit(request.user,"BLOOD_REQUEST_CREATED",r); messages.success(request,"Blood request submitted."); return redirect("requests")
    return render(request,"bloodbank/requests.html",{"requests":qs.order_by("-created_at"),"form":form})

@login_required
@require_POST
def request_action(request, pk, action):
    r=get_object_or_404(BloodRequest,pk=pk)
    if action=="cancel":
        if role(request.user)==UserProfile.Role.HOSPITAL and (r.created_by_id!=request.user.id or r.status!=BloodRequest.Status.PENDING): return deny(request.user)
        if not (role(request.user)==UserProfile.Role.HOSPITAL or allowed(request.user,STAFF)): return deny(request.user)
        transition_request(r,BloodRequest.Status.CANCELLED,request.user)
    else:
        if not allowed(request.user,STAFF): return deny(request.user)
        target={"approve":BloodRequest.Status.APPROVED,"reject":BloodRequest.Status.REJECTED}.get(action)
        if not target: return HttpResponseForbidden("Unsupported action")
        transition_request(r,target,request.user)
    return redirect("requests")

@login_required
@require_POST
def reserve_view(request,pk):
    if not allowed(request.user,STAFF): return deny(request.user)
    r=get_object_or_404(BloodRequest,pk=pk)
    try: made=reserve_units(r,request.user); messages.success(request,f"Reserved {len(made)} compatible unit(s).")
    except ValueError as e: messages.error(request,str(e))
    return redirect("requests")

@login_required
@require_http_methods(["GET","POST"])
def crossmatch_view(request,pk):
    if not allowed(request.user,STAFF): return deny(request.user)
    reservation=get_object_or_404(Reservation,pk=pk); form=CrossMatchForm(request.POST or None)
    if request.method=="POST" and form.is_valid(): CrossMatch.objects.update_or_create(reservation=reservation,defaults={"outcome":form.cleaned_data["outcome"],"recorded_by":request.user}); audit(request.user,"CROSSMATCH_RECORDED",reservation); return redirect("requests")
    return render(request,"bloodbank/form.html",{"title":"Record cross-match","form":form,"cancel":"requests"})

@login_required
@require_POST
def issue_view(request,pk):
    if not allowed(request.user,STAFF): return deny(request.user)
    try: issue_reservation(get_object_or_404(Reservation,pk=pk),request.user); messages.success(request,"Unit issued atomically.")
    except ValueError as e: messages.error(request,str(e))
    return redirect("requests")

@login_required
def audit_view(request):
    if not allowed(request.user,STAFF): return deny(request.user)
    return render(request,"bloodbank/audit.html",{"events":AuditEvent.objects.all()[:200]})

@login_required
def reports(request):
    if not allowed(request.user,STAFF): return deny(request.user)
    return render(request,"bloodbank/reports.html",{"units":BloodUnit.objects.all(),"requests":BloodRequest.objects.all(),"donations":Donation.objects.all(),"audit_count":AuditEvent.objects.count()})

@login_required
def api_inventory(request):
    if not allowed(request.user,{UserProfile.Role.STAFF,UserProfile.Role.ADMIN,UserProfile.Role.HOSPITAL}): return JsonResponse({"code":"forbidden","message":"Not authorized"},status=403)
    qs=BloodUnit.objects.filter(status=BloodUnit.Status.AVAILABLE).values("unit_id","blood_group","component","expiry_at","branch__code")
    return JsonResponse({"items":list(qs)})

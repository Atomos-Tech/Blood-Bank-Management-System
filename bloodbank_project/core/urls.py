from django.urls import path
from django.contrib.auth.views import LogoutView
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.RoleLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("signup/donor/", views.signup_donor, name="signup_donor"),
    path("signup/hospital/", views.signup_hospital, name="signup_hospital"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("branches/", views.branch_list, name="branch_list"),
    path("notifications/", views.notifications_view, name="notifications"),

    # Donor
    path("donor/", views.donor_dashboard, name="donor_dashboard"),
    path("donor/appointment/book/", views.book_appointment, name="book_appointment"),
    path("donor/appointment/<int:appointment_id>/cancel/", views.cancel_appointment, name="cancel_appointment"),

    # Staff
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path("staff/donation/record/", views.record_donation, name="record_donation"),
    path("staff/donation/<int:donation_id>/test/", views.record_test_result, name="record_test_result"),
    path("staff/inventory/", views.inventory_list, name="inventory_list"),
    path("staff/request/<int:request_id>/manage/", views.manage_request, name="manage_request"),
    path("staff/appointments/", views.staff_appointments, name="staff_appointments"),
    path("staff/appointments/<int:appointment_id>/done/", views.mark_appointment_done, name="mark_appointment_done"),
    path("reports/", views.reports_view, name="reports_view"),

    # Admin (custom, in addition to /admin/)
    path("settings/", views.settings_view, name="settings_view"),

    # Hospital
    path("hospital/", views.hospital_dashboard, name="hospital_dashboard"),
    path("hospital/search/", views.search_availability, name="search_availability"),
    path("hospital/request/new/", views.create_request, name="create_request"),
    path("hospital/request/<int:request_id>/", views.track_request, name="track_request"),
]

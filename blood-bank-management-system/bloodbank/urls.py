from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("donors/", views.donor_list, name="donors"),
    path("my-record/", views.my_donor_record, name="my_donor"),
    path("appointments/", views.appointment_view, name="appointments"),
    path("appointments/<int:pk>/cancel/", views.cancel_appointment, name="cancel_appointment"),
    path("deferrals/", views.deferral_view, name="deferrals"),
    path("inventory/", views.inventory, name="inventory"),
    path("donations/new/", views.donation_view, name="donation"),
    path("inventory/<int:pk>/screen/", views.screening_view, name="screening"),
    path("inventory/<int:pk>/release/", views.release_view, name="release"),
    path("inventory/<int:pk>/discard/", views.discard_view, name="discard"),
    path("requests/", views.request_list, name="requests"),
    # The specific reserve route must come before the generic action route, which would otherwise capture it.
    path("requests/<int:pk>/reserve/", views.reserve_view, name="reserve"),
    path("requests/<int:pk>/<str:action>/", views.request_action, name="request_action"),
    path("reservations/<int:pk>/release/", views.release_reservation_view, name="release_reservation"),
    path("reservations/<int:pk>/crossmatch/", views.crossmatch_view, name="crossmatch"),
    path("reservations/<int:pk>/issue/", views.issue_view, name="issue"),
    path("audit/", views.audit_view, name="audit"),
    path("reports/", views.reports, name="reports"),
    # SADS Section 8.2 REST API endpoints for Inventory & Request Service (FR-17 to FR-24)
    path("api/v1/inventory", views.api_inventory, name="api_inventory_noslash"),
    path("api/v1/inventory/", views.api_inventory, name="api_inventory"),
    path("api/v1/requests", views.api_create_request, name="api_requests_noslash"),
    path("api/v1/requests/", views.api_create_request, name="api_requests"),
    path("api/v1/requests/<int:pk>/status", views.api_request_status, name="api_request_status_noslash"),
    path("api/v1/requests/<int:pk>/status/", views.api_request_status, name="api_request_status"),
    path("api/v1/requests/<int:pk>/reserve", views.api_reserve_units, name="api_reserve_units_noslash"),
    path("api/v1/requests/<int:pk>/reserve/", views.api_reserve_units, name="api_reserve_units"),
    path("api/v1/issues", views.api_issue_unit, name="api_issues_noslash"),
    path("api/v1/issues/", views.api_issue_unit, name="api_issues"),
]

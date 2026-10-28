from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # NFR-03: unauthenticated liveness probe for the load balancer and uptime monitor.
    path("health/", views.health, name="health"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("donors/", views.donor_list, name="donors"),
    path("my-record/", views.my_donor_record, name="my_donor"),
    path("appointments/", views.appointment_view, name="appointments"),
    path("appointments/<int:pk>/cancel/", views.cancel_appointment, name="cancel_appointment"),
    path("appointments/<int:pk>/reschedule/", views.reschedule_appointment_view, name="reschedule_appointment"),
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
    # FR-26: in-app account administration, reachable by the System Administrator only.
    path("accounts/", views.account_admin, name="accounts"),
    path("accounts/<int:pk>/status/", views.account_status, name="account_status"),
    path("accounts/<int:pk>/role/", views.account_role, name="account_role"),
    # FR-27: in-app configuration for branches, slots, eligibility and shelf-life rules.
    path("configuration/", views.configuration, name="configuration"),
    path("configuration/<str:section>/", views.configuration_save, name="configuration_save"),
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

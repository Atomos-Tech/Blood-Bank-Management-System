from django.urls import path
from . import views

urlpatterns = [
 path("",views.dashboard,name="dashboard"), path("login/",views.login_view,name="login"), path("logout/",views.logout_view,name="logout"),
 path("donors/",views.donor_list,name="donors"),path("my-record/",views.my_donor_record,name="my_donor"),path("appointments/",views.appointment_view,name="appointments"),path("appointments/<int:pk>/cancel/",views.cancel_appointment,name="cancel_appointment"),
 path("deferrals/",views.deferral_view,name="deferrals"),path("inventory/",views.inventory,name="inventory"),path("donations/new/",views.donation_view,name="donation"),path("inventory/<int:pk>/screen/",views.screening_view,name="screening"),
 path("requests/",views.request_list,name="requests"),path("requests/<int:pk>/<str:action>/",views.request_action,name="request_action"),path("requests/<int:pk>/reserve/",views.reserve_view,name="reserve"),path("reservations/<int:pk>/crossmatch/",views.crossmatch_view,name="crossmatch"),path("reservations/<int:pk>/issue/",views.issue_view,name="issue"),
 path("audit/",views.audit_view,name="audit"),path("reports/",views.reports,name="reports"),path("api/v1/inventory/",views.api_inventory,name="api_inventory"),
]

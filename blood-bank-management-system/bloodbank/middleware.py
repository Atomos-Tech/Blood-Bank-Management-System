from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils import timezone


def branch_timezone_name(user):
    """NFR-10: the time zone configured on the user's branch; UTC when there is no branch."""
    profile = getattr(user, "profile", None) if getattr(user, "is_authenticated", False) else None
    branch = getattr(profile, "branch", None)
    return getattr(branch, "timezone_name", None) or "UTC"


class BranchTimezoneMiddleware:
    """Datetimes are stored in UTC (USE_TZ); this activates the branch time zone for display and form input."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            timezone.activate(ZoneInfo(branch_timezone_name(request.user)))
        except (ZoneInfoNotFoundError, ValueError):
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()


IDLE_TIMEOUT_SECONDS = getattr(settings, "BBMS_IDLE_TIMEOUT_SECONDS", 900)
LAST_ACTIVITY_KEY = "bbms_last_activity"
SESSION_ENDED_MESSAGE = "Your session has ended. Please sign in again."


class SessionSecurityMiddleware:
    """SEC-04: end a signed-in session after 15 minutes without a request, and at once for an
    account that has been deactivated (including a change made in the Django admin, which does
    not go through services.set_account_active). Runs after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            reason = self._end_reason(request, user)
            if reason:
                from .services import audit_reference  # services imports models; keep startup light
                audit_reference(
                    user, "SESSION_ENDED", "Session", str(user.pk), outcome="SUCCESS",
                    summary=reason, branch=getattr(getattr(user, "profile", None), "branch", None),
                )
                logout(request)
                messages.info(request, SESSION_ENDED_MESSAGE)
                return redirect(settings.LOGIN_URL)
            request.session[LAST_ACTIVITY_KEY] = int(timezone.now().timestamp())
        return self.get_response(request)

    @staticmethod
    def _end_reason(request, user):
        profile = getattr(user, "profile", None)
        if profile is not None and not profile.active:
            return "Account is deactivated"
        last = request.session.get(LAST_ACTIVITY_KEY)
        if last is not None and timezone.now().timestamp() - last > IDLE_TIMEOUT_SECONDS:
            return f"Idle for more than {IDLE_TIMEOUT_SECONDS // 60} minutes"
        return None

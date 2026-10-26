from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

"""TC-N04 / NFR-05 probe: write committed records, or verify them after an application restart.

Run inside the app environment (for Docker: `docker compose exec -T web python scripts/restart_persistence_probe.py`):
    python scripts/restart_persistence_probe.py create          -> prints a JSON manifest of what was written
    python scripts/restart_persistence_probe.py verify '<json>' -> exits 0 only if every record is still present

Needs the demo configuration from `python manage.py seed_demo`.
"""
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bbms.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from bloodbank.models import (BloodRequest, BloodUnit, Branch, CrossMatch, Donation, Donor, Issue,  # noqa: E402
                              LabResult, Reservation, TestType)
from bloodbank.services import (issue_reservation, record_donation, record_lab_result,  # noqa: E402
                                reserve_units, transition_request)


def create():
    branch = Branch.objects.get(code="MAIN")
    staff = User.objects.get(username="staff@bbms.local")
    hospital = User.objects.get(username="hospital@bbms.local")
    donor = Donor.objects.get(email="donor@bbms.local")
    donation, units = record_donation(donor, branch, staff, donor.blood_group, {"Plasma": 220})
    unit = units[0]
    for test in TestType.objects.filter(required=True):
        record_lab_result(unit, test, LabResult.Outcome.ACCEPTABLE, staff)
    unit.refresh_from_db()
    request = BloodRequest.objects.create(
        hospital=hospital.profile.hospital_name,
        created_by=hospital,
        branch=branch,
        blood_group=unit.blood_group,
        component=unit.component,
        quantity=1,
        priority="Urgent",
        required_by=timezone.now() + timedelta(days=1),
        clinical_reference=f"TC-N04-{donation.donation_id}",
    )
    transition_request(request, BloodRequest.Status.APPROVED, staff)
    reservation = reserve_units(request, staff)[0]
    CrossMatch.objects.create(reservation=reservation, outcome="COMPATIBLE", recorded_by=staff)
    issue_reservation(reservation, staff)
    return {
        "donation_id": donation.donation_id,
        "unit_id": unit.unit_id,
        "lab_results": unit.lab_results.count(),
        "request_id": request.pk,
        "reservation_id": reservation.pk,
    }


def verify(manifest):
    unit = BloodUnit.objects.get(unit_id=manifest["unit_id"])
    reservation = Reservation.objects.get(pk=manifest["reservation_id"])
    request = BloodRequest.objects.get(pk=manifest["request_id"])
    checks = {
        "donation": Donation.objects.filter(donation_id=manifest["donation_id"]).exists(),
        "screening results": LabResult.objects.filter(unit=unit).count() == manifest["lab_results"],
        "reservation": reservation.unit_id == unit.pk and reservation.request_id == request.pk,
        "issue": Issue.objects.filter(reservation=reservation).exists(),
        "unit status ISSUED": unit.status == BloodUnit.Status.ISSUED,
        "request status FULFILLED": request.status == BloodRequest.Status.FULFILLED,
    }
    return checks


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "create":
        print(json.dumps(create()))
    elif mode == "verify":
        results = verify(json.loads(sys.argv[2]))
        print(json.dumps(results))
        sys.exit(0 if all(results.values()) else 1)
    else:
        sys.exit(__doc__)

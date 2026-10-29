from django.db import migrations

# NFR-12: starting values only. The administrator replaces them on the Configuration screen with the
# periods in the institution's approved record-retention policy (see docs/operations.md section 5).
DEFAULTS = [
    ("DONOR", 3650, "Starting value: 10 years after the profile was created"),
    ("DONATION", 3650, "Starting value: 10 years after collection (traceability)"),
    ("BLOOD_REQUEST", 3650, "Starting value: 10 years after the request"),
    ("AUDIT_EVENT", 3650, "Starting value: 10 years; audit events are never edited or deleted in the app"),
    ("NOTIFICATION", 365, "Starting value: 1 year of email delivery records"),
]


def add_defaults(apps, schema_editor):
    RetentionPolicy = apps.get_model("bloodbank", "RetentionPolicy")
    for record_type, days, notes in DEFAULTS:
        RetentionPolicy.objects.get_or_create(record_type=record_type, defaults={"retention_days": days, "notes": notes})


class Migration(migrations.Migration):
    dependencies = [("bloodbank", "0010_retention_policy")]
    operations = [migrations.RunPython(add_defaults, migrations.RunPython.noop)]

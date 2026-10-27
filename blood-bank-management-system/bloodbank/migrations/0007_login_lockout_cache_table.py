from django.core.management import call_command
from django.db import migrations

CACHE_TABLE = "bbms_cache"


def create_cache_table(apps, schema_editor):
    # SEC-03: the lockout lives in DatabaseCache so every worker counts the same failed attempts.
    # createcachetable builds the table from Django's own definition, so it is portable across
    # SQLite and PostgreSQL, and skips the work when the table already exists.
    call_command("createcachetable", CACHE_TABLE, database=schema_editor.connection.alias, verbosity=0)


def drop_cache_table(apps, schema_editor):
    schema_editor.execute(f'DROP TABLE IF EXISTS "{CACHE_TABLE}"')


class Migration(migrations.Migration):

    dependencies = [
        ("bloodbank", "0006_unique_active_booking_per_donor_slot"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]

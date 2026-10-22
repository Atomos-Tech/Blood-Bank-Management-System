from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")


class BloodbankConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bloodbank"

    def ready(self):
        connection_created.connect(_configure_sqlite)

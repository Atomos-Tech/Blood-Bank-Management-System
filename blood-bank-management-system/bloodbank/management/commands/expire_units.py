import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from bloodbank.services import expire_units


class Command(BaseCommand):
    help = (
        "Mark unissued units past their expiry time as Expired and release their reservations (FR-16). "
        "Run it from cron or Task Scheduler, or pass --every N to keep it running as the scheduler process."
    )

    def add_arguments(self, parser):
        parser.add_argument("--every", type=int, default=0, help="Repeat every N seconds (default: run once).")

    def handle(self, *args, every=0, **kwargs):
        while True:
            count = expire_units()
            self.stdout.write(
                self.style.SUCCESS(f"{timezone.now():%Y-%m-%d %H:%M:%S} UTC: {count} unit(s) marked Expired.")
            )
            if every <= 0:
                return
            time.sleep(every)

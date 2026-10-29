import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from bloodbank.services import send_appointment_reminders


class Command(BaseCommand):
    help = (
        "Email a reminder to every donor whose booked appointment starts within 24 hours (FR-29). "
        "Each email is recorded as SENT or FAILED. Run it from cron, or pass --every N to keep it running."
    )

    def add_arguments(self, parser):
        parser.add_argument("--every", type=int, default=0, help="Repeat every N seconds (default: run once).")

    def handle(self, *args, every=0, **kwargs):
        while True:
            sent, failed = send_appointment_reminders()
            self.stdout.write(
                self.style.SUCCESS(
                    f"{timezone.now():%Y-%m-%d %H:%M:%S} UTC: {sent} reminder(s) sent, {failed} failed."
                )
            )
            if every <= 0:
                return
            time.sleep(every)

from django.core.management.base import BaseCommand

from bloodbank.services import retention_report


class Command(BaseCommand):
    help = "List, for each record type, how many records are older than the configured retention period (NFR-12)."

    def handle(self, *args, **kwargs):
        for row in retention_report():
            policy = row["policy"]
            self.stdout.write(
                f"{policy.get_record_type_display()}: keep {policy.retention_days} days; "
                f"{row['due']} of {row['total']} record(s) older than {row['cutoff']:%Y-%m-%d} are due for archive review."
            )

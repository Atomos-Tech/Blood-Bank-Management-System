from django.core.management.base import BaseCommand
from bloodbank.services import check_stock_and_expiry_alerts


class Command(BaseCommand):
    help = "Run scheduled low-stock and near-expiry alert checks across branches."

    def handle(self, *args, **kwargs):
        notifications = check_stock_and_expiry_alerts()
        self.stdout.write(
            self.style.SUCCESS(f"Alert check completed. {len(notifications)} new notification(s) recorded.")
        )

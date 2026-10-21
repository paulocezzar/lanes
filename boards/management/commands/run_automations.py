"""Create any recurring cards that are due. Safe to run as often as you like
(e.g. a Render cron job every 15 minutes); each run is claimed atomically."""
from django.core.management.base import BaseCommand

from boards.automation import run_due_schedules


class Command(BaseCommand):
    help = "Run due recurring-card schedules."

    def handle(self, *args, **opts):
        from boards.notify import send_due_reminders
        n = run_due_schedules()
        r = send_due_reminders()
        self.stdout.write(f"Created {n} recurring card{'s' if n != 1 else ''}; sent {r} due reminder{'s' if r != 1 else ''}.")

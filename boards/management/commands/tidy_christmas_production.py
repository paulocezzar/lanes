"""One-off: remove the 'Packaging orders — combined' card (packaging lives on each product card).
Runs only when LANES_XMAS_TIDY holds the board id."""
import os

from django.core.management.base import BaseCommand

from boards.models import Card


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_XMAS_TIDY", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        n, _ = Card.objects.filter(list__board_id=int(board), title="Packaging orders — combined").delete()
        self.stdout.write(f"tidy_christmas_production: removed {n} row(s).")

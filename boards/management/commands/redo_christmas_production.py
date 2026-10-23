"""One-off: rebuild the Christmas Production board as one card per product whose
checklist lines track quantities (needed vs done).
Runs only when LANES_XMAS_REDO holds the board id; skips once any line on the
board already counts a quantity."""
import os

from django.core.management.base import BaseCommand

from boards.models import Board, ChecklistItem
from .setup_christmas_production import build, wipe


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_XMAS_REDO", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            return
        if ChecklistItem.objects.filter(checklist__card__list__board=b, qty_target__isnull=False).exists():
            self.stdout.write("redo_christmas_production: already tracking quantities — skipping.")
            return
        wipe(b)
        self.stdout.write(f"redo_christmas_production: {build(b)} cards on board {b.pk}.")

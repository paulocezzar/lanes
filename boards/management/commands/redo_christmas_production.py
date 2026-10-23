"""One-off: rebuild the Christmas Production board as one card per product
(components, packaging and production checklists inside each).
Runs only when LANES_XMAS_REDO holds the board id; skips once the board already
has product cards (a 'Components to make' checklist)."""
import os

from django.core.management.base import BaseCommand

from boards.models import Board, Checklist
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
        if Checklist.objects.filter(card__list__board=b, title="Components to make").exists():
            self.stdout.write("redo_christmas_production: already one card per product — skipping.")
            return
        wipe(b)
        self.stdout.write(f"redo_christmas_production: {build(b)} cards on board {b.pk}.")

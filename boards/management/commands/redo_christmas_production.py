"""One-off: rebuild the Christmas Production board without artwork and with the demand
figures. Runs only when LANES_XMAS_REDO holds the board id. Skips if the board already
has the 'Launch' field (already redone) or if anyone has changed the board by hand."""
import os

from django.core.management.base import BaseCommand

from boards.models import Activity, Board
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
        if b.custom_fields.filter(name="Launch").exists():
            self.stdout.write("redo_christmas_production: already done — skipping.")
            return
        if Activity.objects.filter(board=b).exclude(verb="board_created").exists():
            self.stdout.write("redo_christmas_production: board has been edited by hand — skipping.")
            return
        wipe(b)
        self.stdout.write(f"redo_christmas_production: {build(b)} cards on board {b.pk}.")

"""One-off: add Target / Made so far / Per day / Unit to the agency board's Jobs,
starting numbers, a 'Re-plan days' board button, and a first day plan.
Runs only when LANES_PLAN_BOARD holds the board id; skips if 'Per day' exists."""
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from boards.models import Board, Button, CardFieldValue, CustomField
from boards.production import plan_days

START = {  # job title: (target, per day, unit) — starting guesses, edit on each card
    "Mince pie dough": (15000, 2000, "cases' worth"),
    "Mince pie case stamping": (15000, 2000, "cases"),
    "Mince pie lids": (3000, 1000, "lids"),
    "Mini savoury tart cases": (2100, 700, "cases"),
    "Bake, pack & freeze mini savoury tarts": (540, 200, "packs"),
    "Mince pie crumble (to freeze)": (180, 60, "kg"),
    "Apple pie crumble (to freeze)": (40, 20, "kg"),
    "Sweet tart cases": (600, 300, "cases"),
    "Mince pie boxes": (2500, 300, "boxes"),
    "Mincemeat": (600, 100, "kg"),
    "Christmas cake decoration": (430, 100, "cakes"),
}


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_PLAN_BOARD", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            return
        if CustomField.objects.filter(board=b, name__iexact="Per day").exists():
            self.stdout.write("setup_production_plan: already set up — skipping.")
            return
        jobs = b.lists.get(title="Jobs", is_archived=False)
        days = b.lists.get(title="Shifts", is_archived=False)
        m = b.memberships.filter(role="admin").select_related("user").first()
        owner = m.user if m else None
        with transaction.atomic():
            top = (CustomField.objects.filter(board=b).order_by("-position").first() or CustomField(position=0)).position
            f = {name: CustomField.objects.create(board=b, name=name, kind=kind, show_on_card=False, position=top + i)
                 for i, (name, kind) in enumerate([("Target", "number"), ("Made so far", "number"),
                                                    ("Per day", "number"), ("Unit", "text")], start=1)}
            for card in jobs.cards.filter(is_archived=False, is_template=False):
                if card.title not in START:
                    continue
                target, per_day, unit = START[card.title]
                for name, val in (("Target", target), ("Made so far", 0), ("Per day", per_day), ("Unit", unit)):
                    CardFieldValue.objects.update_or_create(card=card, field=f[name], defaults={"value": val})
            Button.objects.create(board=b, kind=Button.BOARD, name="Re-plan days", icon="📅", created_by=owner,
                                  actions=[{"type": "plan_days", "from_list": jobs.pk, "list": days.pk, "count": 3}])
        n = plan_days(b, jobs, days, jobs_per_day=3, user=owner)
        last = days.cards.filter(is_archived=False, is_template=False).order_by("-start_date").first()
        self.stdout.write(f"setup_production_plan: {n} days planned, last {last.start_date if last else '-'}.")

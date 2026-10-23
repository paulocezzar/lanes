"""One-off: rebuild the agency board to Paulo's layout (Prep Checks, 1st Day,
Jobs = the production plan, People, Shifts, Done). Old cards are archived,
not deleted. Runs only when LANES_AGENCY_REDO holds the board id, and skips a
board that already has a '1st Day' list."""
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from boards.models import Board, Button, Card, Checklist, ChecklistItem, CustomField, List, Rule

PREP = [
    ("Induction pack ready", ["Induction pack template from HR", "Food safety & hygiene sheet", "Allergen sheet",
                              "Health & safety sheet", "PPE / uniform sign-out sheet", "One pack printed per person"]),
    ("Timesheet printed", []),
    ("Shoes", []),
    ("PPE and uniforms", []),
    ("Rota for the week", []),
]
JOBS = ["Mince pie dough", "Mince pie case stamping", "Mince pie lids", "Mini savoury tart cases",
        "Bake, pack & freeze mini savoury tarts", "Mince pie crumble (to freeze)", "Apple pie crumble (to freeze)",
        "Sweet tart cases", "Mince pie boxes", "Mincemeat", "Christmas cake decoration"]
PLAN = ["Mince pie cases", "Mince pie lids", "Mini savoury tart cases", "Bake, pack & freeze mini savoury tarts",
        "Mince pie crumble to freeze", "Apple pie crumble to freeze", "Sweet tart cases", "Mince pie boxes",
        "Mincemeat", "Christmas cakes decoration"]
JOB_DESC = ("**Target:** \n**Equipment:** \n**Where:** \n\n**How to do it:** \n\n"
            "**Storage and labelling:** \n\n**What good looks like:** \n\n**Safety:** \n\n"
            "_Fill in the blanks — this card is the job sheet agency staff follow._")


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_AGENCY_REDO", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            self.stdout.write(f"redo_agency_board: no board {board}.")
            return
        if b.lists.filter(title="1st Day", is_archived=False).exists():
            self.stdout.write("redo_agency_board: already done — skipping.")
            return
        m = b.memberships.filter(role="admin").select_related("user").first()
        owner = m.user if m else None
        with transaction.atomic():
            have = {l.title: l for l in b.lists.filter(is_archived=False)}
            # archive the first version's cards (restorable from the board menu); keep the People template
            Card.objects.filter(list__board=b, is_archived=False).exclude(list__title="People", is_template=True).update(is_archived=True)
            if "Prep before they arrive" in have:
                have["Prep before they arrive"].title = "Prep Checks"
                have["Prep before they arrive"].save(update_fields=["title"])
                have["Prep Checks"] = have.pop("Prep before they arrive")
            names = ["Prep Checks", "1st Day", "Jobs", "People", "Shifts", "Done"]
            lists = {}
            for i, n in enumerate(names, start=1):
                lists[n] = have.get(n) or List.objects.create(board=b, title=n, position=i * 1024)
                lists[n].position = i * 1024
                lists[n].save(update_fields=["position"])
            labels = {l.name: l for l in b.labels.all() if l.name}

            def card(lst, title, desc="", checklists=(), template=False, labs=()):
                c = Card.objects.create(list=lst, title=title, description=desc, created_by=owner, is_template=template,
                                        position=(lst.cards.filter(is_archived=False).count() + 1) * 1024)
                if owner and not template:
                    c.members.add(owner)
                c.labels.add(*[labels[n] for n in labs if n in labels])
                for j, (cl_title, items) in enumerate(checklists, start=1):
                    cl = Checklist.objects.create(card=c, title=cl_title, position=j * 1024)
                    for i, t in enumerate(items, start=1):
                        ChecklistItem.objects.create(checklist=cl, text=t, position=i * 1024)
                return c

            for title, items in PREP:
                card(lists["Prep Checks"], title, checklists=[("To do", items)] if items else ())

            day1 = lists["1st Day"]
            card(day1, "Go through the Prep Checks together",
                 "Show them everything prepared for them in **Prep Checks**.",
                 [("Prep Checks", [t for t, _ in PREP])])
            card(day1, "Explain everything and check it's understood",
                 "Talk it through, then ask them to explain it back. Answer questions before they start.",
                 [("Understood", ["Explained", "They explained it back", "Questions answered"])])
            card(day1, "Training", "Show how each job is done, and the tools and equipment they'll use.",
                 [("The work", ["What needs to be done and how", "How to store production and labelling"]),
                  ("Tools", ["StockUp (traceability)"]),
                  ("Equipment", ["Planetary mixer", "Biscuit machine", "Pie machine", "Freezer"])])
            card(day1, "Production Plan",
                 "What the agency team will make. Each item has its own job sheet in **Jobs** — when an item is "
                 "finished, tick it here and move its Jobs card to **Done**.",
                 [("Production plan", PLAN)])

            for title in JOBS:
                card(lists["Jobs"], title, JOB_DESC,
                     [("Before you start", ["Hands washed, PPE on", "Equipment clean and checked",
                                            "Ingredients and batch codes recorded in StockUp"]),
                      ("When it's done", ["Labelled and stored correctly", "Recorded in StockUp", "Station cleaned down"])],
                     labs=["Job sheet"])

            card(lists["Shifts"], "Shift — copy me",
                 "**How to add a shift:** click the template icon next to “+ Add a card” in this list, pick this card, "
                 "rename it (e.g. “Mon 2 Nov — day shift”), set its date, and add the people on it as members.",
                 [("Shift checks", ["Everyone signed in", "Timesheets filled in", "Jobs for the day given out",
                                    "Breaks covered", "Signed out"])], template=True)

            station = CustomField.objects.filter(board=b, name="Station").first()
            if station:
                station.options = JOBS
                station.save(update_fields=["options"])
            Rule.objects.create(board=b, trigger=Rule.COMPLETED, created_by=owner,
                                filters=[{"type": "in_list", "list": lists["Jobs"].pk}],
                                actions=[{"type": "move", "list": lists["Done"].pk, "position": "top"}])
            if not Button.objects.filter(board=b, name="Job done").exists():
                Button.objects.create(board=b, kind=Button.CARD, name="Job done", icon="✅", created_by=owner,
                                      actions=[{"type": "check_all"}, {"type": "complete"},
                                               {"type": "move", "list": lists["Done"].pk, "position": "top"}])
            b.version += 1
            b.save(update_fields=["version"])
        self.stdout.write(f"redo_agency_board: board {b.pk} rebuilt.")

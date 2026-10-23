"""One-off: add worked example cards to Paulo's personal board.

Runs from build.sh only when LANES_EXAMPLES_BOARD holds the board id; cards
whose title already exists on the board are skipped, so redeploys add nothing.
"""
import os
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards import positions
from boards.models import Board, Card, Checklist, ChecklistItem

HOW_TO = """**How this board works**

1. **Inbox** — drop anything here the moment it comes up. Don't sort yet. Type the date in the title (“Call Clare Tuesday”) and Lanes sets the due date.
2. **This week** — on Monday's *Plan the week* card, pull in what you'll really do this week.
3. **Today** — 3–5 cards, no more. Start each morning by choosing them.
4. **Waiting on** — handed to someone else? Move it here: it gets a due date 3 working days out so you remember to chase.
5. **Done** — tick a card complete and it lands here. Everything in Done is archived on Friday evening.

**Inside a card:** break big jobs into a checklist, give it a label (the colour bar on the front) and a due date. The card front shows progress, e.g. ☑ 2/6.

You can archive this card once you've read it."""


def at(y, m, d, h=17):
    return timezone.make_aware(datetime(y, m, d, h, 0))


CARDS = [
    # list, title, due, labels, description, checklist title, items, done-count
    ("Inbox", "👋 How this board works", None, [], HOW_TO, None, [], 0),
    ("Inbox", "Ask Clare about pay uplift for Christmas night shift", None, ["People"],
     "Night shift around 19–22 Dec, 2–3 people. Move to **Waiting on** once asked.", None, [], 0),
    ("Today", "Organise paperwork for agency staff induction", at(2026, 10, 27), ["People"],
     "Induction pack for agency staff covering Christmas packing.\n\nOne checklist item per document — tick as each is ready.",
     "Paperwork", ["Get the current induction pack template from HR",
                   "Agency confirmation + right-to-work check for each worker",
                   "Food safety & hygiene briefing sheet",
                   "Allergen briefing sheet",
                   "Site health & safety induction",
                   "PPE / uniform sign-out sheet",
                   "Print packs and file signed records"], 1),
    ("This week", "Finish Production Plan", at(2026, 10, 30), ["Christmas", "Urgent"],
     "Pull everything into one plan: done / remaining / timeline per product, and whether it's achievable.",
     "To finish", ["Update done vs remaining for each component",
                   "Check mince pie cases against the 31 Oct target",
                   "Packing plan for peak week (agency cover)",
                   "Night-shift plan 19–22 Dec",
                   "Final read-through",
                   "Share with senior management"], 2),
    ("This week", "Finish Bakery Managers Guide", at(2026, 11, 6), ["People", "Development"],
     "Bakery version of the Operations Manager Guide.",
     "Sections", ["Review latest draft end to end",
                  "Appendix D — Production Operative (cleaning focus)",
                  "Check roles and team list are current",
                  "Format in The Newt style",
                  "Send to HR for sign-off"], 1),
    ("Waiting on", "Agency quotes for Christmas packing cover", at(2026, 10, 28), ["Christmas"],
     "Asked Lime Hospitality and Meridian. Chase if no reply by the due date.",
     None, [], 0),
]


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_EXAMPLES_BOARD", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            self.stdout.write(f"add_example_cards: no board {board}.")
            return
        m = b.memberships.filter(role="admin").select_related("user").first()
        owner = m.user if m else None
        lists = {l.title: l for l in b.lists.filter(is_archived=False)}
        labels = {l.name: l for l in b.labels.all() if l.name}
        added = 0
        with transaction.atomic():
            for lst, title, due, labs, desc, cl_title, items, done in CARDS:
                target = lists.get(lst) or next(iter(lists.values()))
                if Card.objects.filter(list__board=b, title=title).exists():
                    continue
                c = Card.objects.create(list=target, title=title, description=desc, due_at=due, created_by=owner,
                                        position=positions.end_position(target.cards.filter(is_archived=False)))
                if owner and due:
                    c.members.add(owner)
                c.labels.add(*[labels[n] for n in labs if n in labels])
                if cl_title:
                    cl = Checklist.objects.create(card=c, title=cl_title)
                    for i, text in enumerate(items):
                        ChecklistItem.objects.create(checklist=cl, text=text, position=(i + 1) * 1024, is_done=i < done)
                added += 1
            b.version += 1
            b.save(update_fields=["version"])
        self.stdout.write(f"add_example_cards: added {added} cards to board {b.pk}.")

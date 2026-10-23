"""One-off: turn a fresh personal board into a 'My work' setup.

Runs from build.sh only when LANES_SETUP_BOARD holds the board id. It skips
boards that already have a 'Waiting on' list, so redeploys change nothing.
"""
import os

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards import automation
from boards.models import Board, Label, List, Rule, Schedule

LABELS = {"green": "People", "yellow": "Christmas", "orange": "Food safety", "red": "Urgent",
          "purple": "Development", "blue": "Systems"}


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_SETUP_BOARD", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            self.stdout.write(f"setup_my_work: no board {board}.")
            return
        if b.lists.filter(title="Waiting on").exists():
            self.stdout.write("setup_my_work: already set up — skipping.")
            return
        m = b.memberships.filter(role="admin").select_related("user").first()
        owner = m.user if m else None
        with transaction.atomic():
            have = list(b.lists.filter(is_archived=False).order_by("position"))
            by_title = {l.title: l for l in have}
            wanted = ["Inbox", "This week", "Today", "Waiting on", "Done"]
            renames = {"To do": "Inbox", "Doing": "This week"}
            lists = {}
            for old, new in renames.items():
                if old in by_title:
                    by_title[old].title = new
                    by_title[old].save(update_fields=["title"])
                    lists[new] = by_title[old]
            if "Done" in by_title:
                lists["Done"] = by_title["Done"]
            for t in wanted:
                if t not in lists:
                    lists[t] = List.objects.create(board=b, title=t, position=0)
            for i, t in enumerate(wanted, start=1):
                lists[t].position = i * 1024
                lists[t].save(update_fields=["position"])
            for colour, name in LABELS.items():
                lab = Label.objects.filter(board=b, colour=colour, name="").first()
                if lab:
                    lab.name = name
                    lab.save(update_fields=["name"])
                else:
                    Label.objects.get_or_create(board=b, colour=colour, name=name)
            Rule.objects.create(board=b, trigger=Rule.MOVED, trigger_list=lists["Waiting on"], created_by=owner,
                                actions=[{"type": "set_due_workdays", "days": 3}])
            Rule.objects.create(board=b, trigger=Rule.MOVED, trigger_list=lists["Done"], created_by=owner,
                                actions=[{"type": "complete"}])
            Rule.objects.create(board=b, trigger=Rule.COMPLETED, created_by=owner,
                                actions=[{"type": "move", "list": lists["Done"].pk}])
            now = timezone.now()
            for kw in (
                dict(kind="card", frequency="weekly", weekday=4, hour=8, target_list=lists["Today"],
                     title="EOW review — send by 4pm", due_in_days=0, member=owner,
                     checklist="Production summary\nFood safety\nPeople\nNext week's focus\nSend"),
                dict(kind="card", frequency="weekly", weekday=0, hour=7, target_list=lists["Today"],
                     title="Plan the week", due_in_days=0, member=owner,
                     checklist="Clear the Inbox list\nChase everything in Waiting on\nPull this week's cards into This week"),
                dict(kind="actions", frequency="weekly", weekday=4, hour=18,
                     actions=[{"type": "archive_list", "list": lists["Done"].pk}]),
            ):
                s = Schedule(board=b, created_by=owner, next_run_at=now, **kw)
                s.next_run_at = automation.next_run(s, now)
                s.save()
            b.version += 1
            b.save(update_fields=["version"])
        self.stdout.write(f"setup_my_work: board {b.pk} set up.")

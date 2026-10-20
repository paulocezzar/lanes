"""Demo data so a fresh install has something to click around.

    python manage.py seed_demo            # creates demo users (password: lanesdemo)
    python manage.py seed_demo --reset    # wipes boards/workspaces first

Never run against production with real staff on it.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards import positions
from boards.models import (Board, BoardMember, Card, Checklist, ChecklistItem, Comment, Label, List,
                           Workspace, WorkspaceMember)
from boards.services import log

PASSWORD = "lanesdemo"
PEOPLE = [
    ("paulo@example.com", "Paulo", "Silva", True),
    ("sandra@example.com", "Sandra", "Da Silva", False),
    ("charlie@example.com", "Charlie", "Griffin", False),
    ("pipa@example.com", "Pipa", "Jones", False),
    ("logistics@example.com", "Sam", "Driver", False),
]


class Command(BaseCommand):
    help = "Create demo departments, users and a Christmas production board."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true")

    @transaction.atomic
    def handle(self, *args, reset=False, **opts):
        User = get_user_model()
        if reset:
            Workspace.objects.all().delete()
        users = {}
        for email, first, last, admin in PEOPLE:
            u, created = User.objects.get_or_create(email=email, defaults={"first_name": first, "last_name": last})
            if created:
                u.set_password(PASSWORD)
                u.is_staff = u.is_superuser = admin
                u.save()
            users[first] = u

        bakery, _ = Workspace.objects.get_or_create(name="Bakery")
        logistics, _ = Workspace.objects.get_or_create(name="Logistics")
        for name in ("Paulo", "Sandra", "Charlie"):
            WorkspaceMember.objects.get_or_create(
                workspace=bakery, user=users[name],
                defaults={"role": WorkspaceMember.ADMIN if name == "Paulo" else WorkspaceMember.MEMBER})
        WorkspaceMember.objects.get_or_create(workspace=logistics, user=users["Sam"])
        WorkspaceMember.objects.get_or_create(workspace=logistics, user=users["Pipa"])

        if Board.objects.filter(workspace=bakery, title="Christmas production").exists():
            self.stdout.write("Demo board already exists — use --reset to rebuild.")
            return

        board = Board.objects.create(workspace=bakery, title="Christmas production", background="dusk",
                                     created_by=users["Sandra"])
        for name, role in (("Sandra", "admin"), ("Paulo", "admin"), ("Charlie", "member"), ("Sam", "observer")):
            BoardMember.objects.create(board=board, user=users[name], role=role)
        labels = {c: Label.objects.create(board=board, colour=c, name=n) for c, n in
                  [("green", "Production"), ("yellow", "Ordering"), ("red", "Urgent"),
                   ("blue", "Packaging"), ("purple", "Wholesale")]}
        log(board, users["Sandra"], "board_created")

        lists = {}
        for i, title in enumerate(["Christmas cakes", "Mince pies", "Stollen", "Done"], start=1):
            lists[title] = List.objects.create(board=board, title=title, position=i * positions.GAP)

        now = timezone.now()

        def card(list_title, title, *, labels_=(), members=(), due_days=None, desc="", complete=False, by="Sandra"):
            lst = lists[list_title]
            c = Card.objects.create(list=lst, title=title, description=desc, is_complete=complete,
                                    created_by=users[by],
                                    position=positions.end_position(lst.cards.all()),
                                    due_at=(now + timedelta(days=due_days)).replace(hour=14, minute=0, second=0, microsecond=0)
                                    if due_days is not None else None)
            c.labels.set([labels[x] for x in labels_])
            c.members.set([users[m] for m in members])
            log(board, users[by], "card_created", card=c, list=lst.title)
            return c

        prod = card("Christmas cakes", "Production", labels_=["green"], members=["Paulo", "Charlie"], due_days=10,
                    desc="Full run for retail and wholesale. Fruit needs 48 h soak before batter.")
        cl = Checklist.objects.create(card=prod, title="Xmas cake", position=positions.GAP)
        for i, (text, done) in enumerate([("Order ingredients", True), ("Fruit soak", False), ("Order cake tins", False),
                                           ("Order cake boards", False), ("Make cake batter", False), ("Bake cakes", False),
                                           ("Decorate cakes", False), ("Pack cakes", False), ("Deliver", False)], start=1):
            ChecklistItem.objects.create(checklist=cl, text=text, is_done=done, position=i * positions.GAP,
                                         done_by=users["Paulo"] if done else None, done_at=now if done else None)
        Comment.objects.create(card=prod, author=users["Charlie"], body="Tins arrive Thursday — boards still not confirmed.")
        card("Christmas cakes", "Confirm marzipan supplier", labels_=["yellow", "red"], members=["Sandra"], due_days=-1)
        card("Christmas cakes", "Label artwork sign-off", labels_=["blue"], due_days=1)
        card("Mince pies", "Wholesale pre-orders", labels_=["purple"], members=["Pipa"], due_days=5,
             desc="Pipa to collate pre-orders from wholesale customers by Friday.")
        card("Mince pies", "Trial crumble topping batch", labels_=["green"], members=["Charlie"])
        card("Stollen", "Cost the new wreath size", labels_=["yellow"])
        card("Done", "Book extra freezer space", labels_=["green"], complete=True, members=["Paulo"])

        self.stdout.write(self.style.SUCCESS(
            f"Demo ready. Log in as paulo@example.com / {PASSWORD} (superuser) or sandra@example.com."))

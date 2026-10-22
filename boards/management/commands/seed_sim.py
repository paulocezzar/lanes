"""Simulation office for testing Lanes with several people at once.

Does nothing unless the LANES_SIM_PASSWORD environment variable is set, so it's
safe to leave in the build. Creates 5 pretend staff (example.com addresses,
email notifications off) in a "Sim Bakery" department with two boards, and
adds every superuser to those boards as an admin so they can watch.
Remove everything later with:  python manage.py seed_sim --remove
"""
import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from boards.models import (Board, BoardMember, Card, Checklist, ChecklistItem, Label, List, Workspace,
                           WorkspaceMember)

DEPT = "Sim Bakery"
PEOPLE = [
    ("sim.sophie@example.com", "Sophie", "Turner", "admin"),     # production manager
    ("sim.marco@example.com", "Marco", "Rossi", "member"),      # head baker
    ("sim.aisha@example.com", "Aisha", "Khan", "member"),       # pastry chef
    ("sim.tom@example.com", "Tom", "Hughes", "member"),         # packing & dispatch
    ("sim.lena@example.com", "Lena", "Novak", "observer"),      # HR / technical
]


class Command(BaseCommand):
    help = "Create (or --remove) the simulation office."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true")

    @transaction.atomic
    def handle(self, *args, remove=False, **opts):
        if remove:
            Workspace.objects.filter(name=DEPT).delete()
            n = User.objects.filter(email__in=[p[0] for p in PEOPLE]).delete()[0]
            self.stdout.write(f"Simulation removed ({n} rows).")
            return
        password = os.environ.get("LANES_SIM_PASSWORD", "")
        if not password:
            self.stdout.write("seed_sim: LANES_SIM_PASSWORD not set — skipping.")
            return
        ws, _ = Workspace.objects.get_or_create(name=DEPT, defaults={"description": "Simulation — safe to delete"})
        users = {}
        for email, first, last, role in PEOPLE:
            u, _ = User.objects.get_or_create(email=email, defaults={"first_name": first, "last_name": last})
            u.first_name, u.last_name, u.is_active = first, last, True
            u.email_notifications = "off"
            u.totp_enabled = False
            u.set_password(password)
            u.save()
            WorkspaceMember.objects.get_or_create(workspace=ws, user=u,
                                                  defaults={"role": "admin" if role == "admin" else "member"})
            users[first] = (u, role)
        watchers = list(User.objects.filter(is_superuser=True, is_active=True))

        fresh = {}

        def board(title, lists):
            b, created = Board.objects.get_or_create(workspace=ws, title=title, defaults={"created_by": users["Sophie"][0]})
            fresh[title] = created
            for u, role in users.values():
                BoardMember.objects.update_or_create(board=b, user=u, defaults={"role": role})
            for su in watchers:
                BoardMember.objects.get_or_create(board=b, user=su, defaults={"role": "admin"})
            return b, {t: List.objects.get_or_create(board=b, title=t, defaults={"position": (i + 1) * 1000})[0]
                       for i, t in enumerate(lists)}

        now = timezone.now()
        prod, L = board("Production — this week", ["To do", "In progress", "Waiting on stock", "Done"])
        labels = {n: Label.objects.get_or_create(board=prod, name=n, defaults={"colour": c})[0]
                  for n, c in [("Urgent", "red"), ("Wholesale", "blue"), ("Retail", "green"), ("Allergen", "orange")]}
        cards = [
            ("To do", "Sourdough 120 loaves for Hotel", 1, ["Wholesale"], "Marco", ["Mix levain", "Bulk ferment", "Shape", "Bake", "Cool & pack"]),
            ("To do", "Croissants x200 — farm shop", 2, ["Retail"], "Aisha", ["Laminate dough", "Prove", "Bake"]),
            ("To do", "Deep clean proving cabinets", 3, [], None, ["Empty racks", "Sanitise", "Sign off"]),
            ("In progress", "Christmas cakes — feed with brandy", 5, ["Retail"], "Marco", []),
            ("Waiting on stock", "Gluten-free brownies", 1, ["Allergen", "Retail"], "Aisha", ["Check GF flour delivery", "Separate prep area"]),
            ("To do", "Dispatch: Babington order", 0, ["Wholesale", "Urgent"], "Tom", ["Pick", "Label", "Load van"]),
        ]
        if not fresh["Production — this week"]:
            cards = []        # board already exists: never re-add cards people have since moved
        for lst, title, days, labs, who, items in cards:
            c, created = Card.objects.get_or_create(
                list=L[lst], title=title,
                defaults={"due_at": now + timedelta(days=days, hours=2), "created_by": users["Sophie"][0],
                          "position": (Card.objects.filter(list=L[lst]).count() + 1) * 1000})
            if created:
                c.labels.set([labels[x] for x in labs])
                if who:
                    c.members.add(users[who][0])
                if items:
                    cl = Checklist.objects.create(card=c, title="Steps", position=1000)
                    for i, t in enumerate(items):
                        ChecklistItem.objects.create(checklist=cl, text=t, position=(i + 1) * 1000)
        hr, HL = board("Starters & leavers", ["New starters", "Induction booked", "Training", "Signed off"])
        if fresh["Starters & leavers"]:
            Card.objects.get_or_create(list=HL["New starters"], title="Gemma — starts next Monday",
                                       defaults={"created_by": users["Sophie"][0], "due_at": now + timedelta(days=4),
                                                 "position": 1000})
        self.stdout.write(f"Simulation ready: boards {prod.pk}, {hr.pk}; watchers: "
                          + ", ".join(u.email for u in watchers))

"""Wipe all Lanes data except one login (and company settings).

Runs from build.sh only when LANES_RESET_KEEP is set to the email to keep;
blank the variable again straight after the deploy.
"""
import os

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import transaction

from boards import models as m


class Command(BaseCommand):
    help = "Delete every board, card, department and user except the one in LANES_RESET_KEEP."

    def add_arguments(self, parser):
        parser.add_argument("--keep", default=os.environ.get("LANES_RESET_KEEP", ""))

    def handle(self, *args, keep="", **opts):
        keep = (keep or "").strip().lower()
        if not keep:
            self.stdout.write("reset_lanes: LANES_RESET_KEEP not set — skipping.")
            return
        User = get_user_model()
        me = User.objects.filter(email__iexact=keep).first()
        if not me:
            self.stdout.write(f"reset_lanes: no user {keep} — refusing to wipe.")
            return
        for att in m.Attachment.objects.exclude(file=""):
            try:
                att.file.delete(save=False)
            except Exception as e:
                self.stdout.write(f"  couldn't delete file {att.file.name}: {e}")
        with transaction.atomic():
            for model in (m.OutgoingEmail, m.AuditEvent, m.Notification, m.InboxItem, m.SavedSearch, m.Watch,
                          m.Vote, m.Reaction, m.CardMirror, m.RuleFire, m.Rule, m.Schedule, m.Button,
                          m.Attachment, m.Comment, m.CardFieldValue, m.ChecklistItem, m.Checklist, m.Activity,
                          m.Card, m.CustomField, m.Label, m.List, m.BoardMember, m.Board, m.Collection,
                          m.WorkspaceMember, m.Workspace):
                n = model.objects.all().delete()[0]
                self.stdout.write(f"  {model.__name__}: {n}")
            gone = User.objects.exclude(pk=me.pk).delete()[0]
            me.recent_boards = []
            me.save(update_fields=["recent_boards"])
            LogEntry.objects.all().delete()
            Session.objects.all().delete()
        self.stdout.write(f"reset_lanes: wiped. Kept {me.email}; removed {gone} other user rows.")

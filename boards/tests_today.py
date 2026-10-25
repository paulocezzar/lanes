"""Today — the phone home (capture + what's due), the phone bottom bar, and the
board's phone tidy-up."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from .models import Board, BoardMember, Card, Checklist, ChecklistItem, InboxItem, List, Workspace

User = get_user_model()


class TodayTests(TestCase):
    def setUp(self):
        ws = Workspace.objects.create(name="Bakery")
        self.u = User.objects.create_user("t@x.com", "pw", first_name="Tia")
        other = User.objects.create_user("o@x.com", "pw")
        self.b = Board.objects.create(workspace=ws, title="Christmas")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        lst = List.objects.create(board=self.b, title="To do")
        now = timezone.now()
        self.due = Card.objects.create(list=lst, title="Due today card", due_at=now)
        self.due.members.add(self.u)
        late = Card.objects.create(list=lst, title="Late card", due_at=now - timedelta(days=2))
        late.members.add(self.u)
        Card.objects.create(list=lst, title="Next week card", due_at=now + timedelta(days=7)).members.add(self.u)
        Card.objects.create(list=lst, title="Done card", due_at=now, is_complete=True).members.add(self.u)
        Card.objects.create(list=lst, title="Someone else's", due_at=now).members.add(other)
        self.item = ChecklistItem.objects.create(checklist=Checklist.objects.create(card=late), text="Ring supplier",
                                                 assignee=self.u, due_at=now)
        InboxItem.objects.create(owner=self.u, text="Buy cake boards")
        done_list = List.objects.create(board=self.b, title="Done")
        Card.objects.create(list=done_list, title="Moved to Done", due_at=now).members.add(self.u)
        self.c = Client(); self.c.force_login(self.u)

    def test_today_lists_only_mine_due_or_overdue(self):
        r = self.c.get("/today/")
        self.assertEqual(r.status_code, 200)
        for t in ("Due today card", "Late card", "Ring supplier", "Buy cake boards", "data-inbox-add", "mnav"):
            self.assertContains(r, t)
        for t in ("Next week card", "Done card", "Someone else", "Moved to Done"):
            self.assertNotContains(r, t)
        self.assertEqual([e["obj"].title for e in r.context["overdue"]], ["Late card"])

    def test_tick_from_today_and_capture(self):
        r = self.c.post(f"/api/cards/{self.due.pk}/update/", {"is_complete": True}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(self.c.get("/today/"), "Due today card")
        self.c.post("/api/inbox/add/", {"text": "Call Clare"}, content_type="application/json")
        self.assertContains(self.c.get("/today/"), "Call Clare")

    def test_share_and_quick_add_prefill(self):
        r = self.c.get("/today/", {"title": "Order", "text": "more boxes"})
        self.assertContains(r, "Order more boxes</textarea>")
        self.assertContains(self.c.get("/today/?add=1"), "autofocus")
        m = self.c.get("/manifest.webmanifest").json()
        self.assertEqual(m["shortcuts"][0]["url"], "/today/?add=1")
        self.assertEqual(m["share_target"]["action"], "/today/")

    def test_phone_layout_hooks(self):
        self.assertContains(self.c.get("/"), "location.replace('/today/')")
        page = self.c.get(f"/b/{self.b.pk}/")
        self.assertContains(page, "data-mhead-more")
        self.assertContains(page, 'class="vt-board on"')
        self.assertContains(page, "vt-gantt")

    def test_login_required(self):
        self.assertEqual(Client().get("/today/").status_code, 302)

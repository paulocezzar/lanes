"""Template gallery, join requests, company email addresses and who can close boards."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import (Board, BoardMember, Card, CompanySettings, JoinRequest, List, Notification, RequestForm, Rule,
                     Workspace, WorkspaceMember)

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Dept")
        self.other = Workspace.objects.create(name="Other dept")
        self.boss = User.objects.create_superuser("boss@x.com", "pw")
        self.admin = User.objects.create_user("a@x.com", "pw-12345", first_name="Ada")
        self.u = User.objects.create_user("u@x.com", "pw-12345", first_name="Una")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.admin, role="admin")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.client.force_login(self.u)


class GalleryTests(Base):
    def test_gallery_lists_and_filters(self):
        r = self.client.get("/templates/")
        self.assertContains(r, "Project plan")
        self.assertContains(r, "Requests &amp; helpdesk")
        r = self.client.get("/templates/?cat=events")
        self.assertEqual([i["title"] for i in r.context["items"]], ["Event planning"])
        self.assertContains(self.client.get("/templates/?q=onboard"), "New starter onboarding")
        self.assertEqual(self.client.get("/templates/nope/").status_code, 404)

    def test_use_builtin_with_and_without_tips(self):
        self.assertContains(self.client.get("/templates/requests/"), "Make a board from it")
        r = self.client.post("/templates/requests/", {"title": "Maintenance", "workspace": self.ws.pk,
                                                      "visibility": "workspace", "keep_cards": "on"})
        b = Board.objects.get(title="Maintenance")
        self.assertRedirects(r, f"/b/{b.pk}/", fetch_redirect_response=False)
        self.assertEqual([l.title for l in b.lists.order_by("position")], ["New", "Assigned", "In progress", "Waiting", "Done"])
        self.assertFalse(Card.objects.filter(list__board=b, title__startswith="💡").exists())
        f = RequestForm.objects.get(board=b)
        self.assertTrue(any(q["to"].startswith("field:") for q in f.questions))
        self.assertTrue(Rule.objects.filter(board=b, trigger="card_moved").exists())
        self.client.post("/templates/project/", {"title": "P", "workspace": self.ws.pk, "keep_cards": "on", "keep_tips": "on"})
        p = Board.objects.get(title="P")
        self.assertTrue(p.is_project)
        self.assertTrue(Card.objects.filter(list__board=p, title__startswith="💡").exists())
        self.assertTrue(Card.objects.filter(list__board=p, title="Agree the budget", due_at__isnull=False).exists())

    def test_company_template_and_department_check(self):
        tpl = Board.objects.create(workspace=self.ws, title="Our launch", is_template=True)
        BoardMember.objects.create(board=tpl, user=self.u, role="admin")
        lst = List.objects.create(board=tpl, title="Steps")
        Card.objects.create(list=lst, title="Normal card")
        Card.objects.create(list=lst, title="Template card", is_template=True)
        self.assertContains(self.client.get("/templates/?cat=company"), "Our launch")
        self.client.post(f"/templates/b{tpl.pk}/", {"title": "Launch 2", "workspace": self.ws.pk, "keep_cards": "on"})
        b = Board.objects.get(title="Launch 2")
        self.assertEqual(sorted(Card.objects.filter(list__board=b).values_list("title", flat=True)), ["Normal card"])
        self.client.post("/templates/event/", {"title": "Sneaky", "workspace": self.other.pk})
        self.assertFalse(Board.objects.filter(title="Sneaky").exists())


class JoinTests(Base):
    def test_private_board_ask_and_approve(self):
        b = Board.objects.create(workspace=self.ws, title="Secret plans", visibility="private")
        BoardMember.objects.create(board=b, user=self.admin, role="admin")
        r = self.client.get(f"/b/{b.pk}/")
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "This board is private", status_code=403)
        self.assertNotContains(r, "Secret plans", status_code=403)
        self.client.post("/join-requests/new/", {"board": b.pk, "message": "Covering for Sam"})
        self.client.post("/join-requests/new/", {"board": b.pk})                 # no duplicate
        self.assertEqual(JoinRequest.objects.count(), 1)
        self.assertTrue(Notification.objects.filter(recipient=self.admin, kind="join").exists())
        self.assertContains(self.client.get(f"/b/{b.pk}/"), "already asked", status_code=403)
        jr = JoinRequest.objects.get()
        self.assertEqual(self.client.post(f"/join-requests/{jr.pk}/", {"action": "approve"}).status_code, 404)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/join-requests/"), "Covering for Sam")
        self.client.post(f"/join-requests/{jr.pk}/", {"action": "approve"})
        self.assertTrue(BoardMember.objects.filter(board=b, user=self.u).exists())
        self.client.force_login(self.u)
        self.assertEqual(self.client.get(f"/b/{b.pk}/").status_code, 200)
        self.assertTrue(Notification.objects.filter(recipient=self.u, kind="join").exists())

    def test_department_ask_and_decline(self):
        self.assertContains(self.client.get("/"), "Other dept · Ask to join")
        self.client.post("/join-requests/new/", {"workspace": self.other.pk})
        self.assertContains(self.client.get("/"), "asked ✓")
        jr = JoinRequest.objects.get()
        self.client.force_login(self.boss)                        # no department admins → company admins decide
        self.client.post(f"/join-requests/{jr.pk}/", {"action": "decline"})
        self.assertEqual(JoinRequest.objects.get().status, "declined")
        self.assertFalse(WorkspaceMember.objects.filter(workspace=self.other, user=self.u).exists())


class SettingsTests(Base):
    def test_company_email_only(self):
        from . import permissions
        cs = CompanySettings.get()
        cs.allowed_domains = "example.co.uk"
        cs.save()
        self.assertTrue(permissions.email_allowed("jo@example.co.uk"))
        self.assertFalse(permissions.email_allowed("jo@gmail.com"))
        self.client.force_login(self.admin)
        self.client.post(f"/departments/{self.ws.pk}/people/add/", {"email": "jo@gmail.com", "first_name": "Jo"})
        self.assertFalse(User.objects.filter(email="jo@gmail.com").exists())
        self.client.post(f"/departments/{self.ws.pk}/people/add/", {"email": "jo@example.co.uk", "first_name": "Jo"})
        self.assertTrue(User.objects.filter(email="jo@example.co.uk").exists())
        self.client.force_login(self.boss)
        self.client.post("/company/settings/", {"allowed_domains": "@a.com; b.org", "require_two_step": "off"})
        self.assertEqual(CompanySettings.get().allowed_domains, "a.com, b.org")

    def test_who_can_close_boards(self):
        import json
        b = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=b, user=self.u, role="admin")
        cs = CompanySettings.get()
        cs.archive_boards = "dept_admins"
        cs.save()
        r = self.client.post(f"/api/boards/{b.pk}/update/", json.dumps({"archive": True}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertNotContains(self.client.get(f"/b/{b.pk}/"), "data-archive-board")
        self.client.force_login(self.admin)
        r = self.client.post(f"/api/boards/{b.pk}/update/", json.dumps({"archive": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Board.objects.get(pk=b.pk).is_archived)


class DescribeTests(Base):
    def test_create_menu_and_describe_without_ai(self):
        r = self.client.get("/")
        self.assertContains(r, "data-create-toggle")
        self.assertContains(r, "Describe a board")
        self.assertContains(self.client.get("/boards/describe/"), "Get inspired")
        r = self.client.post("/boards/describe/", {"text": "Run an event with guests and a venue", "workspace": self.ws.pk})
        self.assertContains(r, "Event planning")
        plan = r.context["plan_json"]
        r = self.client.post("/boards/describe/", {"action": "create", "plan": plan, "workspace": self.ws.pk,
                                                   "title": "Open day", "visibility": "workspace"})
        b = Board.objects.get(title="Open day")
        self.assertRedirects(r, f"/b/{b.pk}/", fetch_redirect_response=False)
        self.assertTrue(b.lists.filter(title="Booked").exists())

    def test_describe_with_ai(self):
        from unittest import mock
        spec = {"title": "Garden refresh", "summary": "Tidy the garden", "labels": [{"name": "Big", "colour": "nope"}],
                "lists": [{"title": "Ideas", "cards": [{"title": "New benches", "checklist": ["Measure", "Order"]}]},
                          {"title": "Done", "cards": []}]}
        with mock.patch("boards.ai.enabled", return_value=True), mock.patch("boards.ai.draft_board", return_value=spec):
            r = self.client.post("/boards/describe/", {"text": "garden", "workspace": self.ws.pk})
        self.assertContains(r, "New benches")
        self.client.post("/boards/describe/", {"action": "create", "plan": r.context["plan_json"], "workspace": self.ws.pk,
                                               "title": "Garden"})
        b = Board.objects.get(title="Garden")
        self.assertEqual(b.labels.get().colour, "grey")
        self.assertEqual(Card.objects.get(list__board=b).checklists.get().items.count(), 2)


class SidebarTests(Base):
    def test_sidebar_and_department_page(self):
        b = Board.objects.create(workspace=self.ws, title="Dept board")
        BoardMember.objects.create(board=b, user=self.u)
        r = self.client.get("/")
        self.assertContains(r, 'class="side"')
        self.assertContains(r, "data-apps-toggle")
        r = self.client.get(f"/?dept={self.ws.pk}")
        self.assertContains(r, "dept-head")
        self.assertContains(r, "Dept board")
        self.assertNotContains(r, "Other departments")
        self.assertNotContains(self.client.get(f"/b/{b.pk}/"), 'class="side"')     # boards keep the full width


class BoardHeadTests(Base):
    def test_template_banner_and_member_card_data(self):
        b = Board.objects.create(workspace=self.ws, title="Tpl", is_template=True)
        BoardMember.objects.create(board=b, user=self.u, role="admin")
        r = self.client.get(f"/b/{b.pk}/")
        self.assertContains(r, "Create board from template")
        self.assertContains(r, f'/templates/b{b.pk}/')
        self.assertContains(r, 'data-member-card="%d"' % self.u.pk)
        self.assertIn('"role": "Admin"', r.context["member_cards"])
        self.assertEqual(self.client.get(f"/templates/b{b.pk}/").status_code, 200)


class AccountTests(Base):
    def test_profile_activity_person_settings(self):
        self.client.post("/me/profile/", {"first_name": "Una", "last_name": "Lee", "job_title": "Supervisor", "bio": "Mornings"})
        self.u.refresh_from_db()
        self.assertEqual((self.u.job_title, self.u.last_name), ("Supervisor", "Lee"))
        b = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=b, user=self.u)
        from .services import log
        log(b, self.u, "board_created")
        self.assertContains(self.client.get("/me/activity/"), "created this board")
        self.client.force_login(self.admin)
        r = self.client.get(f"/people/{self.u.pk}/")
        self.assertContains(r, "Supervisor")
        stranger = User.objects.create_user("s@x.com", "pw-12345")
        self.assertEqual(self.client.get(f"/people/{stranger.pk}/").status_code, 404)
        self.client.force_login(self.u)
        self.client.post("/account/email/", {"email_notifications": "instant", "kind": ["commented"]})
        self.u.refresh_from_db()
        self.assertIn("watched", self.u.email_muted)
        self.assertNotIn("commented", self.u.email_muted)

    def test_department_tabs_and_export(self):
        import io
        import zipfile
        b = Board.objects.create(workspace=self.ws, title="Line one")
        outsider = User.objects.create_user("o@x.com", "pw-12345", first_name="Ola")
        BoardMember.objects.create(board=b, user=outsider)
        JoinRequest.objects.create(user=outsider, workspace=self.ws)
        self.client.force_login(self.admin)
        r = self.client.get(f"/departments/{self.ws.pk}/people/")
        self.assertContains(r, "Guests (1)")
        self.assertContains(r, "Join requests (1)")
        self.assertContains(self.client.get(f"/departments/{self.ws.pk}/export/"), "Download 1 board")
        r = self.client.post(f"/departments/{self.ws.pk}/export/")
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        self.assertIn("line-one.csv", names)
        self.assertIn("line-one.json", names)
        self.client.force_login(self.u)
        self.assertEqual(self.client.get(f"/departments/{self.ws.pk}/export/").status_code, 404)

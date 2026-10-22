import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from django.urls import reverse

from . import positions
from .models import Activity, Board, BoardMember, Card, Label, List, Notification, Watch, Workspace, WorkspaceMember

User = get_user_model()


class PositionTests(TestCase):
    def test_between(self):
        self.assertEqual(positions.between(1000, 2000), 1500)
        self.assertEqual(positions.between(None, 1000), 500)
        self.assertEqual(positions.between(1000, None), 1000 + positions.GAP)
        self.assertEqual(positions.between(None, None), positions.GAP)

    def test_rebalance_when_gap_collapses(self):
        ws = Workspace.objects.create(name="W")
        b = Board.objects.create(workspace=ws, title="B")
        a = List.objects.create(board=b, title="a", position=1.0)
        c = List.objects.create(board=b, title="c", position=1.0 + 1e-9)
        moved = List.objects.create(board=b, title="m", position=5000)
        positions.place(moved, b.lists.all(), a.pk, c.pk)
        moved.save()
        order = list(b.lists.order_by("position").values_list("title", flat=True))
        self.assertEqual(order, ["a", "m", "c"])


class BoardFlowTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery")
        self.other_ws = Workspace.objects.create(name="Butchery")
        self.admin = User.objects.create_user("a@x.com", "pw", first_name="Ann", last_name="Admin")
        self.member = User.objects.create_user("m@x.com", "pw", first_name="Max")
        self.observer = User.objects.create_user("o@x.com", "pw", first_name="Olly")
        self.dept = User.objects.create_user("d@x.com", "pw", first_name="Dee")      # same dept, not on board
        self.outsider = User.objects.create_user("x@x.com", "pw", first_name="Xan")  # other dept
        for u in (self.admin, self.member, self.observer, self.dept):
            WorkspaceMember.objects.create(workspace=self.ws, user=u)
        WorkspaceMember.objects.create(workspace=self.other_ws, user=self.outsider)
        self.board = Board.objects.create(workspace=self.ws, title="Xmas", visibility=Board.PRIVATE)
        BoardMember.objects.create(board=self.board, user=self.admin, role="admin")
        BoardMember.objects.create(board=self.board, user=self.member, role="member")
        BoardMember.objects.create(board=self.board, user=self.observer, role="observer")
        self.todo = List.objects.create(board=self.board, title="To do", position=1024)
        self.done = List.objects.create(board=self.board, title="Done", position=2048)

    def api(self, user, url, data=None):
        self.client.force_login(user)
        return self.client.post(url, json.dumps(data or {}), content_type="application/json")

    def test_create_board_makes_admin_and_defaults(self):
        self.client.force_login(self.dept)
        r = self.client.post(reverse("board_create"), {"workspace": self.ws.pk, "title": "New"})
        board = Board.objects.get(title="New")
        self.assertRedirects(r, reverse("board", args=[board.pk]))
        self.assertEqual(BoardMember.objects.get(board=board, user=self.dept).role, "admin")
        self.assertEqual(board.lists.count(), 3)
        self.assertEqual(board.labels.count(), 6)

    def test_private_board_hidden_from_others(self):
        self.client.force_login(self.dept)
        self.assertEqual(self.client.get(reverse("board", args=[self.board.pk])).status_code, 404)
        self.board.visibility = Board.WORKSPACE
        self.board.save()
        self.assertEqual(self.client.get(reverse("board", args=[self.board.pk])).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("board", args=[self.board.pk])).status_code, 404)

    def test_card_create_move_and_activity(self):
        r = self.api(self.member, f"/api/lists/{self.todo.pk}/cards/", {"title": "Order tins"})
        self.assertEqual(r.status_code, 200)
        card = Card.objects.get(title="Order tins")
        self.assertIn("Order tins", r.json()["tile"])
        r = self.api(self.member, f"/api/cards/{card.pk}/move/", {"list_id": self.done.pk})
        self.assertEqual(r.status_code, 200)
        card.refresh_from_db()
        self.assertEqual(card.list, self.done)
        self.assertTrue(Activity.objects.filter(card=card, verb="card_moved").exists())

    def test_card_moves_between_neighbours(self):
        a = Card.objects.create(list=self.todo, title="a", position=1024)
        b = Card.objects.create(list=self.todo, title="b", position=2048)
        c = Card.objects.create(list=self.done, title="c", position=1024)
        self.api(self.member, f"/api/cards/{c.pk}/move/", {"list_id": self.todo.pk, "before_id": a.pk, "after_id": b.pk})
        self.assertEqual(list(self.todo.cards.values_list("title", flat=True)), ["a", "c", "b"])

    def test_observer_cannot_edit_but_can_comment(self):
        card = Card.objects.create(list=self.todo, title="x")
        r = self.api(self.observer, f"/api/cards/{card.pk}/update/", {"title": "hacked"})
        self.assertEqual(r.status_code, 403)
        r = self.api(self.observer, f"/api/cards/{card.pk}/comments/", {"body": "Looks good"})
        self.assertEqual(r.status_code, 200)

    def test_outsider_gets_404_on_api(self):
        card = Card.objects.create(list=self.todo, title="x")
        r = self.api(self.outsider, f"/api/cards/{card.pk}/update/", {"title": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_cannot_move_card_to_another_boards_list(self):
        other = Board.objects.create(workspace=self.ws, title="Other")
        foreign = List.objects.create(board=other, title="L")
        card = Card.objects.create(list=self.todo, title="x")
        r = self.api(self.member, f"/api/cards/{card.pk}/move/", {"list_id": foreign.pk})
        self.assertIn(r.status_code, (403, 404))   # can see "Other" but isn't on it, so can't add cards there
        card.refresh_from_db()
        self.assertEqual(card.list, self.todo)

    def test_checklist_and_complete(self):
        card = Card.objects.create(list=self.todo, title="x")
        self.api(self.member, f"/api/cards/{card.pk}/checklists/", {"title": "Steps"})
        cl = card.checklists.get()
        self.api(self.member, f"/api/checklists/{cl.pk}/items/", {"text": "one"})
        item = cl.items.get()
        self.api(self.member, f"/api/items/{item.pk}/", {"is_done": True})
        item.refresh_from_db()
        self.assertTrue(item.is_done)
        self.assertEqual(card.checklist_counts(), (1, 1))
        self.api(self.member, f"/api/cards/{card.pk}/update/", {"is_complete": True})
        card.refresh_from_db()
        self.assertTrue(card.is_complete)

    def test_assign_only_board_members(self):
        card = Card.objects.create(list=self.todo, title="x")
        r = self.api(self.member, f"/api/cards/{card.pk}/members/", {"user_id": self.dept.pk})
        self.assertEqual(r.status_code, 400)
        r = self.api(self.member, f"/api/cards/{card.pk}/members/", {"user_id": self.admin.pk})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.admin, card.members.all())

    def test_labels_scoped_to_board(self):
        other = Board.objects.create(workspace=self.ws, title="Other")
        foreign = Label.objects.create(board=other, colour="red")
        card = Card.objects.create(list=self.todo, title="x")
        r = self.api(self.member, f"/api/cards/{card.pk}/labels/", {"label_id": foreign.pk})
        self.assertEqual(r.status_code, 404)

    def test_share_requires_admin_and_keeps_last_admin(self):
        r = self.api(self.member, f"/api/boards/{self.board.pk}/share/", {"email": "x@x.com"})
        self.assertEqual(r.status_code, 403)
        r = self.api(self.admin, f"/api/boards/{self.board.pk}/share/", {"email": "X@x.com", "role": "member"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(BoardMember.objects.filter(board=self.board, user=self.outsider).exists())
        r = self.api(self.admin, f"/api/boards/{self.board.pk}/members/{self.admin.pk}/", {"role": "member"})
        self.assertEqual(r.status_code, 400)

    def test_guest_sees_shared_board_on_home(self):
        BoardMember.objects.create(board=self.board, user=self.outsider)
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("home"))
        self.assertContains(r, "Shared with you from other departments")
        self.assertContains(r, "Xmas")

    def test_pages_render(self):
        card = Card.objects.create(list=self.todo, title="Render me")
        card.members.add(self.member)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("board", args=[self.board.pk])), "Render me")
        self.assertContains(self.client.get(reverse("card_modal", args=[card.pk])), "Comments and activity")
        self.assertContains(self.client.get(reverse("my_cards")), "Render me")

    def test_login_required(self):
        r = self.client.get(reverse("home"))
        self.assertEqual(r.status_code, 302)


class DepartmentPeopleTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss@x.com", "pw", first_name="Boss")
        self.lead = User.objects.create_user("lead@x.com", "pw", first_name="Lead")
        self.staff = User.objects.create_user("staff@x.com", "pw", first_name="Staff")
        self.ws = Workspace.objects.create(name="Bakery")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.lead, role="admin")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.staff, role="member")

    def test_only_company_admin_creates_departments(self):
        self.client.force_login(self.lead)
        self.assertEqual(self.client.post(reverse("workspace_create"), {"name": "Butchery"}).status_code, 404)
        self.client.force_login(self.boss)
        r = self.client.post(reverse("workspace_create"), {"name": "Butchery"})
        ws = Workspace.objects.get(name="Butchery")
        self.assertRedirects(r, reverse("workspace_members", args=[ws.pk]))
        self.assertEqual(WorkspaceMember.objects.get(workspace=ws, user=self.boss).role, "admin")

    def test_add_new_person_creates_account_with_temp_password(self):
        self.client.force_login(self.lead)
        r = self.client.post(reverse("workspace_member_add", args=[self.ws.pk]),
                             {"email": "New@X.com", "first_name": "Nia", "role": "member"}, follow=True)
        person = User.objects.get(email="new@x.com")
        self.assertTrue(WorkspaceMember.objects.filter(workspace=self.ws, user=person).exists())
        password = r.context["new_account"]["password"]
        self.assertTrue(person.check_password(password))
        self.assertContains(r, password)
        # shown once only
        self.assertNotContains(self.client.get(reverse("workspace_members", args=[self.ws.pk])), password)

    def test_new_person_needs_first_name(self):
        self.client.force_login(self.lead)
        self.client.post(reverse("workspace_member_add", args=[self.ws.pk]), {"email": "nobody@x.com"})
        self.assertFalse(User.objects.filter(email="nobody@x.com").exists())

    def test_member_cannot_manage(self):
        self.client.force_login(self.staff)
        r = self.client.post(reverse("workspace_member_add", args=[self.ws.pk]), {"email": "boss@x.com"})
        self.assertEqual(r.status_code, 404)
        self.assertContains(self.client.get(reverse("workspace_members", args=[self.ws.pk])), "Lead")

    def test_last_admin_protected_and_role_change(self):
        self.client.force_login(self.lead)
        url = reverse("workspace_member_update", args=[self.ws.pk, self.lead.pk])
        self.client.post(url, {"role": "member"})
        self.assertEqual(WorkspaceMember.objects.get(workspace=self.ws, user=self.lead).role, "admin")
        self.client.post(reverse("workspace_member_update", args=[self.ws.pk, self.staff.pk]), {"role": "admin"})
        self.assertEqual(WorkspaceMember.objects.get(workspace=self.ws, user=self.staff).role, "admin")

    def test_reset_password(self):
        self.client.force_login(self.lead)
        r = self.client.post(reverse("workspace_member_reset_password", args=[self.ws.pk, self.staff.pk]), follow=True)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(r.context["new_account"]["password"]))
        self.assertFalse(self.staff.check_password("pw"))

    def test_outsider_cannot_see_department(self):
        other = User.objects.create_user("o@x.com", "pw")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("workspace_members", args=[self.ws.pk])).status_code, 404)


class LiveUpdateTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="U")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.user)
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        self.lst = List.objects.create(board=self.board, title="L")

    def v(self):
        self.board.refresh_from_db()
        return self.board.version

    def test_version_moves_on_changes(self):
        before = self.v()
        card = Card.objects.create(list=self.lst, title="c")
        after_create = self.v()
        self.assertGreater(after_create, before)
        card.members.add(self.user)
        self.assertGreater(self.v(), after_create)
        n = self.v()
        card.delete()
        self.assertGreater(self.v(), n)

    def test_state_endpoint(self):
        self.client.force_login(self.user)
        url = reverse("board_state", args=[self.board.pk])
        v = self.v()
        r = self.client.get(url, {"v": v}).json()
        self.assertNotIn("lists_html", r)
        Card.objects.create(list=self.lst, title="New one")
        r = self.client.get(url, {"v": v}).json()
        self.assertIn("New one", r["lists_html"])

    def test_state_hidden_from_outsiders(self):
        self.board.visibility = Board.PRIVATE
        self.board.save()
        other = User.objects.create_user("o@x.com", "pw")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("board_state", args=[self.board.pk])).status_code, 404)


class BoardViewsTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="U")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.user)
        self.board = Board.objects.create(workspace=self.ws, title="B", visibility=Board.PRIVATE)
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        lst = List.objects.create(board=self.board, title="L")
        self.card = Card.objects.create(list=lst, title="Dated card", due_at=timezone.now())
        Card.objects.create(list=lst, title="Undated card")

    def test_views_render(self):
        self.client.force_login(self.user)
        for name in ("board_calendar", "board_table", "board_timeline", "board_dashboard"):
            r = self.client.get(reverse(name, args=[self.board.pk]))
            self.assertEqual(r.status_code, 200, name)
        self.assertContains(self.client.get(reverse("board_calendar", args=[self.board.pk])), "Dated card")
        self.assertContains(self.client.get(reverse("board_table", args=[self.board.pk])), "Undated card")
        self.assertContains(self.client.get(reverse("board_timeline", args=[self.board.pk])), "Dated card")
        self.assertContains(self.client.get(reverse("board_dashboard", args=[self.board.pk])), "Cards on the board")

    def test_calendar_month_param_and_bad_input(self):
        self.client.force_login(self.user)
        for m in ("2026-12", "nonsense", "2026-13"):
            self.assertEqual(self.client.get(reverse("board_calendar", args=[self.board.pk]), {"m": m}).status_code, 200)
        self.assertEqual(self.client.get(reverse("board_timeline", args=[self.board.pk]), {"start": "bad"}).status_code, 200)

    def test_views_private(self):
        other = User.objects.create_user("o@x.com", "pw")
        self.client.force_login(other)
        for name in ("board_calendar", "board_table", "board_timeline", "board_dashboard"):
            self.assertEqual(self.client.get(reverse(name, args=[self.board.pk])).status_code, 404)


class AdvancedFeaturesTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        self.helper = User.objects.create_user("h@x.com", "pw", first_name="Hal")
        self.stranger = User.objects.create_user("s@x.com", "pw", first_name="Sid")
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        BoardMember.objects.create(board=self.board, user=self.helper, role="member")
        self.card = Card.objects.create(list=List.objects.create(board=self.board, title="L"), title="Mince pies")

    def api(self, url, data):
        self.client.force_login(self.user)
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_custom_fields_create_set_and_validate(self):
        r = self.api(f"/api/boards/{self.board.pk}/fields/", {"name": "Quantity (kg)", "kind": "number"})
        qty = r.json()["field_id"]
        r = self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": qty, "value": "200"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Quantity (kg)", r.json()["tile"])
        self.assertEqual(self.card.field_values.get().value, 200)
        r = self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": qty, "value": "lots"})
        self.assertEqual(r.status_code, 400)
        self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": qty, "value": ""})
        self.assertFalse(self.card.field_values.exists())

    def test_dropdown_needs_options_and_rejects_others(self):
        r = self.api(f"/api/boards/{self.board.pk}/fields/", {"name": "Site", "kind": "dropdown"})
        self.assertEqual(r.status_code, 400)
        site = self.api(f"/api/boards/{self.board.pk}/fields/",
                        {"name": "Site", "kind": "dropdown", "options": "Pilton, Bruton"}).json()["field_id"]
        self.assertEqual(self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": site, "value": "Bruton"}).status_code, 200)
        self.assertEqual(self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": site, "value": "Mars"}).status_code, 400)

    def test_field_from_other_board_rejected(self):
        other = Board.objects.create(workspace=self.ws, title="Other")
        from .models import CustomField
        f = CustomField.objects.create(board=other, name="X")
        self.assertEqual(self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": f.pk, "value": "y"}).status_code, 404)

    def test_checklist_item_assignee_and_due(self):
        from .models import Checklist, ChecklistItem
        cl = Checklist.objects.create(card=self.card)
        item = ChecklistItem.objects.create(checklist=cl, text="Order foils")
        r = self.api(f"/api/items/{item.pk}/", {"assignee_id": self.helper.pk, "due_at": "2026-11-02T09:00"})
        self.assertEqual(r.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.assignee, self.helper)
        self.assertIsNotNone(item.due_at)
        r = self.api(f"/api/items/{item.pk}/", {"assignee_id": self.stranger.pk})
        self.assertEqual(r.status_code, 400)
        self.client.force_login(self.helper)
        self.assertContains(self.client.get(reverse("my_cards")), "Order foils")

    def test_table_shows_field_columns(self):
        from .models import CardFieldValue, CustomField
        f = CustomField.objects.create(board=self.board, name="Supplier")
        CardFieldValue.objects.create(card=self.card, field=f, value="Essential Trading")
        self.client.force_login(self.user)
        r = self.client.get(reverse("board_table", args=[self.board.pk]))
        self.assertContains(r, "Supplier")
        self.assertContains(r, "Essential Trading")


class AutomationTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        self.helper = User.objects.create_user("h@x.com", "pw", first_name="Hal")
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        BoardMember.objects.create(board=self.board, user=self.helper, role="member")
        self.todo = List.objects.create(board=self.board, title="To do", position=1)
        self.done = List.objects.create(board=self.board, title="Done", position=2)
        self.label = Label.objects.create(board=self.board, colour="red", name="Urgent")
        self.client.force_login(self.user)

    def api(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def rule(self, trigger, actions, **kw):
        from .models import Rule
        return Rule.objects.create(board=self.board, trigger=trigger, actions=actions, **kw)

    def test_move_to_done_completes_and_ticks(self):
        from .models import Checklist, ChecklistItem
        self.rule("card_moved", [{"type": "complete"}, {"type": "check_all"}], trigger_list=self.done)
        card = Card.objects.create(list=self.todo, title="x")
        cl = Checklist.objects.create(card=card)
        ChecklistItem.objects.create(checklist=cl, text="a")
        r = self.api(f"/api/cards/{card.pk}/move/", {"list_id": self.done.pk})
        card.refresh_from_db()
        self.assertTrue(card.is_complete)
        self.assertFalse(ChecklistItem.objects.filter(is_done=False).exists())
        self.assertIn("Automation", r.json()["modal"])

    def test_added_to_list_assigns_and_sets_due(self):
        self.rule("card_added", [{"type": "add_member", "user": self.helper.pk}, {"type": "set_due", "days": 2}],
                  trigger_list=self.todo)
        self.api(f"/api/lists/{self.todo.pk}/cards/", {"title": "New"})
        card = Card.objects.get(title="New")
        self.assertIn(self.helper, card.members.all())
        self.assertIsNotNone(card.due_at)

    def test_checklist_done_moves_card(self):
        from .models import Checklist, ChecklistItem
        self.rule("checklist_done", [{"type": "move", "list": self.done.pk}])
        card = Card.objects.create(list=self.todo, title="x")
        cl = Checklist.objects.create(card=card)
        a = ChecklistItem.objects.create(checklist=cl, text="a")
        b = ChecklistItem.objects.create(checklist=cl, text="b")
        self.api(f"/api/items/{a.pk}/", {"is_done": True})
        card.refresh_from_db()
        self.assertEqual(card.list, self.todo)
        self.api(f"/api/items/{b.pk}/", {"is_done": True})
        card.refresh_from_db()
        self.assertEqual(card.list, self.done)

    def test_label_trigger_and_no_chaining(self):
        # label → move to Done; a Done-move rule must NOT fire from an automation
        self.rule("label_added", [{"type": "move", "list": self.done.pk}], trigger_label=self.label)
        self.rule("card_moved", [{"type": "archive"}], trigger_list=self.done)
        card = Card.objects.create(list=self.todo, title="x")
        self.api(f"/api/cards/{card.pk}/labels/", {"label_id": self.label.pk})
        card.refresh_from_db()
        self.assertEqual(card.list, self.done)
        self.assertFalse(card.is_archived)

    def test_disabled_rule_does_nothing(self):
        self.rule("card_completed", [{"type": "archive"}], is_enabled=False)
        card = Card.objects.create(list=self.todo, title="x")
        self.api(f"/api/cards/{card.pk}/update/", {"is_complete": True})
        card.refresh_from_db()
        self.assertFalse(card.is_archived)

    def test_rule_form_validation(self):
        url = reverse("rule_create", args=[self.board.pk])
        self.client.post(url, {"trigger": "card_moved", "trigger_list": self.done.pk,
                               "action_0": "move", "list_0": self.done.pk})
        self.assertEqual(self.board.rules.count(), 0)   # can't move into its own trigger list
        self.client.post(url, {"trigger": "card_moved", "trigger_list": self.done.pk, "action_0": "complete"})
        self.assertEqual(self.board.rules.count(), 1)
        self.assertEqual(self.client.get(reverse("board_automation", args=[self.board.pk])).status_code, 200)

    def test_observer_cannot_add_rules(self):
        obs = User.objects.create_user("o@x.com", "pw")
        BoardMember.objects.create(board=self.board, user=obs, role="observer")
        self.client.force_login(obs)
        self.client.post(reverse("rule_create", args=[self.board.pk]), {"trigger": "card_completed", "action_0": "archive"})
        self.assertEqual(self.board.rules.count(), 0)


class ScheduleTests(TestCase):
    def setUp(self):
        from .models import Schedule
        self.ws = Workspace.objects.create(name="Bakery")
        self.board = Board.objects.create(workspace=self.ws, title="B")
        self.lst = List.objects.create(board=self.board, title="To do")
        self.S = Schedule

    def make(self, **kw):
        from datetime import datetime, timezone as dtz
        defaults = dict(board=self.board, target_list=self.lst, title="Deep clean", checklist="Ovens\nFloors\n",
                        next_run_at=datetime(2026, 10, 19, 6, tzinfo=dtz.utc))
        defaults.update(kw)
        return self.S.objects.create(**defaults)

    def test_next_run_weekly_monthly_and_dst(self):
        from datetime import datetime, timezone as dtz
        from django.utils import timezone
        from .automation import next_run
        after = datetime(2026, 10, 20, 12, tzinfo=dtz.utc)          # Tue 20 Oct
        s = self.S(frequency="weekly", weekday=0, hour=7)
        nxt = timezone.localtime(next_run(s, after))
        self.assertEqual((nxt.date().isoformat(), nxt.hour), ("2026-10-26", 7))   # Monday after clocks change
        s = self.S(frequency="monthly", month_day=28, hour=6)
        self.assertEqual(timezone.localtime(next_run(s, datetime(2027, 2, 1, tzinfo=dtz.utc))).day, 28)
        s = self.S(frequency="weekdays", hour=7)
        sat = datetime(2026, 10, 24, 12, tzinfo=dtz.utc)
        self.assertEqual(timezone.localtime(next_run(s, sat)).weekday(), 0)

    def test_due_schedule_creates_card_once(self):
        from .automation import run_due_schedules
        s = self.make()
        self.assertEqual(run_due_schedules(), 1)
        self.assertEqual(run_due_schedules(), 0)      # already claimed, next run is in the future
        card = Card.objects.get(title="Deep clean")
        self.assertEqual(card.checklist_counts(), (0, 2))
        s.refresh_from_db()
        self.assertGreater(s.next_run_at, card.created_at)

    def test_paused_schedule_skipped(self):
        from .automation import run_due_schedules
        self.make(is_enabled=False)
        self.assertEqual(run_due_schedules(), 0)


class CopyTemplateAttachmentTests(TestCase):
    def setUp(self):
        from .models import Checklist, ChecklistItem, CustomField, CardFieldValue, Rule
        self.ws = Workspace.objects.create(name="Bakery")
        self.other_ws = Workspace.objects.create(name="Butchery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.user, role="admin")
        self.board = Board.objects.create(workspace=self.ws, title="Xmas")
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        self.todo = List.objects.create(board=self.board, title="To do", position=1)
        self.done = List.objects.create(board=self.board, title="Done", position=2)
        self.label = Label.objects.create(board=self.board, colour="red", name="Urgent")
        self.field = CustomField.objects.create(board=self.board, name="Qty", kind="number")
        self.card = Card.objects.create(list=self.todo, title="Cakes")
        self.card.labels.add(self.label)
        self.card.members.add(self.user)
        cl = Checklist.objects.create(card=self.card, title="Steps")
        ChecklistItem.objects.create(checklist=cl, text="Bake")
        CardFieldValue.objects.create(card=self.card, field=self.field, value=12)
        Rule.objects.create(board=self.board, trigger="card_moved", trigger_list=self.done,
                            actions=[{"type": "complete"}, {"type": "add_label", "label": self.label.pk}])
        self.client.force_login(self.user)

    def api(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_copy_card(self):
        r = self.api(f"/api/cards/{self.card.pk}/copy/", {"title": "Cakes 2", "list_id": self.done.pk})
        self.assertEqual(r.status_code, 200)
        new = Card.objects.get(title="Cakes 2")
        self.assertEqual(new.list, self.done)
        self.assertEqual(new.checklist_counts(), (0, 1))
        self.assertIn(self.label, new.labels.all())
        self.assertEqual(new.field_values.get().value, 12)

    def test_card_template_flow(self):
        self.api(f"/api/cards/{self.card.pk}/update/", {"is_template": True})
        r = self.api(f"/api/lists/{self.done.pk}/from-template/", {"template_id": self.card.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Card.objects.filter(title="Cakes", list=self.done, is_template=False).count(), 1)
        normal = Card.objects.create(list=self.todo, title="Plain")
        r = self.api(f"/api/lists/{self.done.pk}/from-template/", {"template_id": normal.pk})
        self.assertEqual(r.status_code, 404)

    def test_copy_board_to_other_department_remaps(self):
        from .models import Rule
        WorkspaceMember.objects.create(workspace=self.other_ws, user=self.user)
        r = self.client.post(reverse("board_copy", args=[self.board.pk]),
                             {"title": "Xmas 2027", "workspace": self.other_ws.pk, "keep_cards": "on"})
        new = Board.objects.get(title="Xmas 2027")
        self.assertRedirects(r, reverse("board", args=[new.pk]))
        self.assertEqual(new.workspace, self.other_ws)
        new_card = Card.objects.get(list__board=new)
        new_label = new.labels.get()
        self.assertEqual(list(new_card.labels.all()), [new_label])
        self.assertEqual(new_card.members.count(), 0)
        self.assertEqual(new_card.field_values.get().field.board, new)
        rule = Rule.objects.get(board=new)
        self.assertEqual(rule.trigger_list.board, new)
        self.assertEqual(rule.actions[1]["label"], new_label.pk)

    def test_cannot_copy_into_foreign_department(self):
        self.client.post(reverse("board_copy", args=[self.board.pk]), {"title": "Nope", "workspace": self.other_ws.pk})
        self.assertFalse(Board.objects.filter(title="Nope").exists())

    def test_template_board_listed_on_home(self):
        self.board.is_template = True
        self.board.save()
        self.assertContains(self.client.get(reverse("home")), "Use template")

    def test_attachment_upload_download_and_permissions(self):
        import shutil, tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        tmp = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=tmp):
                png = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)
                r = self.client.post(f"/api/cards/{self.card.pk}/attachments/",
                                     {"file": SimpleUploadedFile("photo.png", png, content_type="image/png")})
                self.assertEqual(r.status_code, 200)
                att = self.card.attachments.get()
                self.card.refresh_from_db()
                self.assertEqual(self.card.cover, att)     # first image becomes the cover
                self.assertEqual(self.client.get(reverse("attachment", args=[att.pk])).status_code, 200)
                bad = self.client.post(f"/api/cards/{self.card.pk}/attachments/",
                                       {"file": SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")})
                self.assertEqual(bad.status_code, 400)
                outsider = User.objects.create_user("o@x.com", "pw")
                self.board.visibility = Board.PRIVATE
                self.board.save()
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(reverse("attachment", args=[att.pk])).status_code, 404)
                self.client.force_login(self.user)
                self.api(f"/api/attachments/{att.pk}/", {"delete": True})
                self.assertFalse(self.card.attachments.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NotificationTests(TestCase):
    def setUp(self):
        from .models import Notification
        self.N = Notification
        self.ws = Workspace.objects.create(name="Bakery")
        self.paulo = User.objects.create_user("p@x.com", "pw", first_name="Paulo", last_name="Silva")
        self.charlie = User.objects.create_user("c@x.com", "pw", first_name="Charlie", last_name="Griffin")
        self.sam = User.objects.create_user("s@x.com", "pw", first_name="Sam", last_name="Bobbett")
        self.outsider = User.objects.create_user("o@x.com", "pw", first_name="Olly")
        self.board = Board.objects.create(workspace=self.ws, title="Xmas", visibility=Board.PRIVATE)
        for u in (self.paulo, self.charlie, self.sam):
            BoardMember.objects.create(board=self.board, user=u, role="admin")
        self.card = Card.objects.create(list=List.objects.create(board=self.board, title="To do"), title="Mince pies")
        self.client.force_login(self.paulo)

    def api(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_assigning_notifies_but_not_yourself(self):
        self.api(f"/api/cards/{self.card.pk}/members/", {"user_id": self.charlie.pk})
        self.api(f"/api/cards/{self.card.pk}/members/", {"user_id": self.paulo.pk})
        self.assertEqual(self.N.objects.filter(recipient=self.charlie, kind="assigned").count(), 1)
        self.assertFalse(self.N.objects.filter(recipient=self.paulo).exists())

    def test_item_assignment_notifies_once(self):
        from .models import Checklist, ChecklistItem
        item = ChecklistItem.objects.create(checklist=Checklist.objects.create(card=self.card), text="Order foils")
        self.api(f"/api/items/{item.pk}/", {"assignee_id": self.sam.pk})
        self.api(f"/api/items/{item.pk}/", {"assignee_id": self.sam.pk, "due_at": "2026-12-01T09:00"})
        self.assertEqual(self.N.objects.filter(recipient=self.sam, kind="item_assigned").count(), 1)

    def test_mentions_and_card_member_comments(self):
        from .notify import find_mentions
        people = [self.paulo, self.charlie, self.sam]
        self.assertEqual(find_mentions("Thanks @Charlie Griffin and @sam!", people), [self.charlie, self.sam])
        self.assertEqual(find_mentions("email me at bob@charlie.com", people), [])
        self.card.members.add(self.sam)
        self.api(f"/api/cards/{self.card.pk}/comments/", {"body": "@Charlie can you check tins?"})
        self.assertTrue(self.N.objects.filter(recipient=self.charlie, kind="mentioned").exists())
        self.assertTrue(self.N.objects.filter(recipient=self.sam, kind="commented").exists())
        self.assertFalse(self.N.objects.filter(recipient=self.paulo).exists())

    def test_no_notification_for_people_who_cannot_see_board(self):
        from .notify import send
        self.assertIsNone(send(self.outsider, "mentioned", actor=self.paulo, card=self.card, text="x"))

    def test_due_reminders_deduplicated(self):
        from datetime import timedelta
        from django.utils import timezone
        from .notify import send_due_reminders
        self.card.due_at = timezone.now() + timedelta(hours=3)
        self.card.save()
        self.card.members.add(self.charlie)
        self.assertEqual(send_due_reminders(), 1)
        self.assertEqual(send_due_reminders(), 0)

    def test_bell_api_and_mark_read(self):
        self.api(f"/api/cards/{self.card.pk}/members/", {"user_id": self.charlie.pk})
        self.client.force_login(self.charlie)
        r = self.client.get("/api/notifications/?html=1").json()
        self.assertEqual(r["unread"], 1)
        self.assertIn("Mince pies", r["html"])
        n = self.N.objects.get(recipient=self.charlie)
        r = self.client.get(reverse("notification_open", args=[n.pk]))
        self.assertRedirects(r, f"{reverse('board', args=[self.board.pk])}?card={self.card.pk}", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/api/notifications/").json()["unread"], 0)
        self.client.force_login(self.sam)
        self.assertEqual(self.client.get(reverse("notification_open", args=[n.pk])).status_code, 404)

    def test_comment_mentions_highlighted_and_escaped(self):
        from .templatetags.lanes_extras import mentions
        out = mentions("Hi @Charlie <script>")
        self.assertIn('<span class="mention">@Charlie</span>', out)
        self.assertIn("&lt;script&gt;", out)


class InboxPlannerTests(TestCase):
    def setUp(self):
        from .models import InboxItem
        self.I = InboxItem
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        self.other = User.objects.create_user("o@x.com", "pw", first_name="Oz")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.user)
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.user, role="member")
        self.lst = List.objects.create(board=self.board, title="To do")
        self.client.force_login(self.user)

    def api(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_paste_list_makes_one_item_per_line(self):
        r = self.api("/api/inbox/add/", {"text": "- Order foils\n• Call Kingsmill\n\n  Book van  "})
        self.assertEqual(r.json()["added"], 3)
        self.assertEqual(list(self.I.objects.values_list("text", flat=True)), ["Order foils", "Call Kingsmill", "Book van"])

    def test_inbox_is_private(self):
        item = self.I.objects.create(owner=self.other, text="Secret")
        self.assertEqual(self.api(f"/api/inbox/{item.pk}/", {"is_done": True}).status_code, 404)
        self.assertNotIn("Secret", self.client.get("/api/inbox/").json()["html"])

    def test_inbox_to_card(self):
        from datetime import date
        item = self.I.objects.create(owner=self.user, text="Order foils", notes="6 boxes", due_date=date(2026, 11, 2))
        r = self.api(f"/api/inbox/{item.pk}/to-card/", {"list_id": self.lst.pk})
        self.assertEqual(r.status_code, 200)
        card = Card.objects.get(title="Order foils")
        self.assertEqual(card.description, "6 boxes")
        self.assertIn(self.user, card.members.all())
        self.assertFalse(self.I.objects.exists())

    def test_inbox_to_card_needs_edit_rights(self):
        other_board = Board.objects.create(workspace=self.ws, title="Other")
        lst = List.objects.create(board=other_board, title="L")
        item = self.I.objects.create(owner=self.user, text="x")
        self.assertEqual(self.api(f"/api/inbox/{item.pk}/to-card/", {"list_id": lst.pk}).status_code, 400)

    def test_ai_capture(self):
        from unittest import mock
        from datetime import date
        with self.settings(ANTHROPIC_API_KEY=""):
            self.assertEqual(self.api("/api/inbox/capture/", {"text": "Hi, please order tins by Friday"}).status_code, 503)
        reply = '{"items": [{"text": "Order cake tins", "due": "2026-10-23", "notes": "Asked by Sandra"}, {"text": ""}]}'
        with self.settings(ANTHROPIC_API_KEY="k"), mock.patch("boards.ai._call", return_value=reply):
            r = self.api("/api/inbox/capture/", {"text": "Hi, please order tins by Friday. Sandra"})
        self.assertEqual(r.json()["added"], 1)
        item = self.I.objects.get()
        self.assertEqual((item.text, item.due_date, item.source), ("Order cake tins", date(2026, 10, 23), "ai"))

    def test_planner_shows_cards_items_and_inbox(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Checklist, ChecklistItem
        now = timezone.now()
        card = Card.objects.create(list=self.lst, title="My card", due_at=now)
        card.members.add(self.user)
        Card.objects.create(list=self.lst, title="Undated", ).members.add(self.user)
        ChecklistItem.objects.create(checklist=Checklist.objects.create(card=card), text="My item",
                                     assignee=self.user, due_at=now)
        self.I.objects.create(owner=self.user, text="Inbox thing", due_date=timezone.localdate())
        r = self.client.get(reverse("planner"))
        for text in ("My card", "Undated", "My item", "Inbox thing"):
            self.assertContains(r, text)
        self.assertEqual(self.client.get(reverse("planner"), {"start": "garbage"}).status_code, 200)


class MirrorTests(TestCase):
    """A Bakery card mirrored onto Logistics' board."""

    def setUp(self):
        from .models import CardMirror
        self.M = CardMirror
        self.bakery = Workspace.objects.create(name="Bakery")
        self.logi = Workspace.objects.create(name="Logistics")
        self.baker = User.objects.create_user("b@x.com", "pw", first_name="Bea")
        self.driver = User.objects.create_user("d@x.com", "pw", first_name="Dan")
        self.stranger = User.objects.create_user("s@x.com", "pw", first_name="Stu")
        WorkspaceMember.objects.create(workspace=self.bakery, user=self.baker)
        WorkspaceMember.objects.create(workspace=self.logi, user=self.driver)
        self.xmas = Board.objects.create(workspace=self.bakery, title="Xmas", visibility=Board.PRIVATE)
        self.deliveries = Board.objects.create(workspace=self.logi, title="Deliveries", visibility=Board.PRIVATE)
        BoardMember.objects.create(board=self.xmas, user=self.baker, role="admin")
        BoardMember.objects.create(board=self.deliveries, user=self.driver, role="admin")
        BoardMember.objects.create(board=self.deliveries, user=self.baker, role="member")  # baker can post there
        self.todo = List.objects.create(board=self.xmas, title="To do")
        self.vans = List.objects.create(board=self.deliveries, title="This week")
        self.card = Card.objects.create(list=self.todo, title="Hampers to Bath")

    def api(self, user, url, data):
        self.client.force_login(user)
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def mirror(self):
        r = self.api(self.baker, f"/api/cards/{self.card.pk}/mirrors/", {"list_id": self.vans.pk})
        self.assertEqual(r.status_code, 200, r.content)
        return self.M.objects.get()

    def test_mirror_shows_on_other_board_and_driver_can_work_it(self):
        self.mirror()
        self.client.force_login(self.driver)
        self.assertContains(self.client.get(reverse("board", args=[self.deliveries.pk])), "Hampers to Bath")
        self.assertEqual(self.client.get(reverse("card_modal", args=[self.card.pk])).status_code, 200)
        # driver can comment and tick, but can't move the original between Bakery lists
        self.assertEqual(self.api(self.driver, f"/api/cards/{self.card.pk}/comments/", {"body": "Van booked"}).status_code, 200)
        self.assertEqual(self.api(self.driver, f"/api/cards/{self.card.pk}/update/", {"is_complete": True}).status_code, 200)
        self.assertEqual(self.api(self.driver, f"/api/cards/{self.card.pk}/move/", {"list_id": self.todo.pk}).status_code, 403)
        # driver can be assigned (they're on a board the card lives on)
        self.assertEqual(self.api(self.baker, f"/api/cards/{self.card.pk}/members/", {"user_id": self.driver.pk}).status_code, 200)

    def test_mirroring_needs_edit_rights_on_target(self):
        r = self.api(self.stranger, f"/api/cards/{self.card.pk}/mirrors/", {"list_id": self.vans.pk})
        self.assertEqual(r.status_code, 404)
        BoardMember.objects.filter(board=self.deliveries, user=self.baker).update(role="observer")
        r = self.api(self.baker, f"/api/cards/{self.card.pk}/mirrors/", {"list_id": self.vans.pk})
        self.assertEqual(r.status_code, 403)

    def test_stranger_cannot_see_card(self):
        self.mirror()
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("card_modal", args=[self.card.pk])).status_code, 404)

    def test_mixed_ordering_and_mirror_move(self):
        m = self.mirror()
        a = Card.objects.create(list=self.vans, title="A", position=1)
        b = Card.objects.create(list=self.vans, title="B", position=2)
        m.position = 10
        m.save()
        r = self.api(self.driver, f"/api/mirrors/{m.pk}/move/", {"list_id": self.vans.pk, "before": a.token, "after": b.token})
        self.assertEqual(r.status_code, 200)
        titles = [e["card"].title for e in List.objects.get(pk=self.vans.pk).entries()]
        self.assertEqual(titles, ["A", "Hampers to Bath", "B"])
        # a real card can be dropped between a card and a mirror too
        c = Card.objects.create(list=self.vans, title="C", position=50)
        self.api(self.driver, f"/api/cards/{c.pk}/move/", {"list_id": self.vans.pk, "before": a.token, "after": m.token})
        titles = [e["card"].title for e in List.objects.get(pk=self.vans.pk).entries()]
        self.assertEqual(titles, ["A", "C", "Hampers to Bath", "B"])

    def test_live_update_bumps_mirror_board(self):
        self.mirror()
        before = Board.objects.get(pk=self.deliveries.pk).version
        self.card.title = "Hampers to Bath (x40)"
        self.card.save()
        self.assertGreater(Board.objects.get(pk=self.deliveries.pk).version, before)

    def test_remove_mirror(self):
        m = self.mirror()
        self.assertEqual(self.api(self.driver, f"/api/mirrors/{m.pk}/", {"delete": True}).status_code, 200)
        self.assertFalse(self.M.objects.exists())
        self.assertTrue(Card.objects.filter(pk=self.card.pk).exists())

    def test_mirrored_card_in_other_board_views(self):
        self.mirror()
        self.client.force_login(self.driver)
        self.assertContains(self.client.get(reverse("board_table", args=[self.deliveries.pk])), "Hampers to Bath")


class CrossBoardTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        self.ws = Workspace.objects.create(name="Bakery")
        self.other_ws = Workspace.objects.create(name="HR")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        self.charlie = User.objects.create_user("c@x.com", "pw", first_name="Charlie")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.user, role="admin")
        self.b1 = Board.objects.create(workspace=self.ws, title="Christmas")
        self.b2 = Board.objects.create(workspace=self.ws, title="Daily bread")
        self.secret = Board.objects.create(workspace=self.other_ws, title="Leavers", visibility=Board.PRIVATE)
        for b in (self.b1, self.b2):
            BoardMember.objects.create(board=b, user=self.user, role="admin")
        BoardMember.objects.create(board=self.b1, user=self.charlie)
        l1 = List.objects.create(board=self.b1, title="Doing")
        l2 = List.objects.create(board=self.b2, title="To do")
        self.urgent = Label.objects.create(board=self.b1, name="Urgent", colour="red")
        self.c1 = Card.objects.create(list=l1, title="Stollen wreaths", due_at=timezone.now() - timezone.timedelta(days=1))
        self.c1.labels.add(self.urgent)
        self.c1.members.add(self.user)
        self.c2 = Card.objects.create(list=l2, title="Sourdough starter", description="feed twice")
        self.c2.members.add(self.charlie)
        Card.objects.create(list=List.objects.create(board=self.secret, title="L"), title="Confidential leaver")
        self.client.force_login(self.user)

    def test_all_views_cover_every_visible_board_only(self):
        for name in ("all_calendar", "all_table", "all_timeline", "all_dashboard"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)
            self.assertNotContains(r, "Confidential leaver")
        r = self.client.get(reverse("all_table"))
        self.assertContains(r, "Stollen wreaths")
        self.assertContains(r, "Sourdough starter")
        r = self.client.get(reverse("all_table"), {"dept": self.other_ws.pk})
        self.assertNotContains(r, "Stollen wreaths")

    def test_search_operators(self):
        from .search import run
        titles = lambda q: {c.title for c in run(self.user, q)[0]}
        self.assertEqual(titles("@me"), {"Stollen wreaths"})
        self.assertEqual(titles("@charlie"), {"Sourdough starter"})
        self.assertEqual(titles("label:urgent is:overdue"), {"Stollen wreaths"})
        self.assertEqual(titles("feed"), {"Sourdough starter"})
        self.assertEqual(titles("board:daily"), {"Sourdough starter"})
        self.assertEqual(titles("leaver"), set())          # private board stays hidden
        self.assertEqual(titles('"unbalanced quote'), set())

    def test_saved_search(self):
        self.client.post(reverse("search_save"), {"q": "@me is:open", "name": "Mine"})
        self.assertContains(self.client.get(reverse("search")), "Mine")

    def test_collections(self):
        r = self.client.post(f"/api/boards/{self.b1.pk}/update/", json.dumps({"collection": "new:Christmas 2026"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.b1.refresh_from_db()
        self.assertEqual(self.b1.collection.name, "Christmas 2026")
        self.assertContains(self.client.get(reverse("home")), "Christmas 2026")
        from .models import Collection
        foreign = Collection.objects.create(workspace=self.other_ws, name="Nope")
        r = self.client.post(f"/api/boards/{self.b2.pk}/update/", json.dumps({"collection": foreign.pk}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 404)


class ListToolsLooksExportTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        self.board = Board.objects.create(workspace=self.ws, title="Xmas cakes")
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        self.lst = List.objects.create(board=self.board, title="Doing")
        self.card = Card.objects.create(list=self.lst, title="=HYPERLINK(evil)", position=100)
        self.client.force_login(self.user)

    def api(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_list_colour_and_limit(self):
        self.api(f"/api/lists/{self.lst.pk}/", {"colour": "red", "card_limit": "1"})
        Card.objects.create(list=self.lst, title="second")
        self.lst.refresh_from_db()
        self.assertEqual((self.lst.colour, self.lst.card_limit, self.lst.over_limit), ("red", 1, True))
        self.assertContains(self.client.get(reverse("board", args=[self.board.pk])), "list-count over")
        self.api(f"/api/lists/{self.lst.pk}/", {"colour": "tartan", "card_limit": ""})
        self.lst.refresh_from_db()
        self.assertEqual((self.lst.colour, self.lst.card_limit), ("", None))

    def test_cover_colour_and_stickers(self):
        self.api(f"/api/cards/{self.card.pk}/update/", {"cover_colour": "blue"})
        for s in ["⭐", "🔥", "⭐", "not-a-sticker"]:
            self.api(f"/api/cards/{self.card.pk}/update/", {"sticker": s})
        self.card.refresh_from_db()
        self.assertEqual(self.card.cover_colour, "blue")
        self.assertEqual(self.card.stickers, ["🔥"])   # ⭐ toggled on then off

    def test_location_field(self):
        r = self.api(f"/api/boards/{self.board.pk}/fields/", {"name": "Deliver to", "kind": "location"})
        fid = r.json()["field_id"]
        r = self.api(f"/api/cards/{self.card.pk}/fields/", {"field_id": fid, "value": "BA4 4NL"})
        self.assertIn("📍 BA4 4NL", r.json()["tile"])
        self.assertIn("maps/search", r.json()["modal"])

    def test_csv_export_escapes_formulas(self):
        r = self.client.get(reverse("board_csv", args=[self.board.pk]))
        body = r.content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(evil)", body)
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        data = json.loads(self.client.get(reverse("board_json", args=[self.board.pk])).content)
        self.assertEqual(data["cards"][0]["title"], "=HYPERLINK(evil)")
        self.assertIn("Xmas cakes", self.client.get(reverse("export_all")).content.decode())

    def test_export_private(self):
        self.board.visibility = Board.PRIVATE
        self.board.save()
        self.client.force_login(User.objects.create_user("o@x.com", "pw"))
        self.assertEqual(self.client.get(reverse("board_csv", args=[self.board.pk])).status_code, 404)
        self.assertNotIn("Xmas cakes", self.client.get(reverse("export_all")).content.decode())

    def test_automation_move_top_and_sticker(self):
        from .models import Rule
        done = List.objects.create(board=self.board, title="Done")
        Card.objects.create(list=done, title="older", position=10)
        Rule.objects.create(board=self.board, trigger="card_moved", trigger_list=done,
                            actions=[{"type": "move_top"}, {"type": "add_sticker", "sticker": "✅"}])
        self.api(f"/api/cards/{self.card.pk}/move/", {"list_id": done.pk})
        self.card.refresh_from_db()
        self.assertEqual(self.card.stickers, ["✅"])
        self.assertEqual([e["card"].title for e in List.objects.get(pk=done.pk).entries()][0], "=HYPERLINK(evil)")


class SuggestStepsThemeTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery")
        self.user = User.objects.create_user("u@x.com", "pw", first_name="Una")
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.user, role="admin")
        self.card = Card.objects.create(list=List.objects.create(board=self.board, title="L"), title="Launch stollen bites")
        self.client.force_login(self.user)

    def api(self, url, data=None):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json")

    def test_suggest_then_add(self):
        from unittest import mock
        with self.settings(ANTHROPIC_API_KEY=""):
            self.assertEqual(self.api(f"/api/cards/{self.card.pk}/suggest-steps/").status_code, 503)
        reply = '{"title": "Launch plan", "steps": ["Cost the recipe", "Order packaging", ""]}'
        with self.settings(ANTHROPIC_API_KEY="k"), mock.patch("boards.ai._call", return_value=reply):
            r = self.api(f"/api/cards/{self.card.pk}/suggest-steps/").json()
        self.assertEqual(r, {"title": "Launch plan", "steps": ["Cost the recipe", "Order packaging"]})
        self.api(f"/api/cards/{self.card.pk}/checklists/", {"title": r["title"], "items": r["steps"]})
        self.assertEqual(self.card.checklist_counts(), (0, 2))

    def test_ai_button_only_when_enabled(self):
        with self.settings(ANTHROPIC_API_KEY=""):
            self.assertNotContains(self.client.get(reverse("card_modal", args=[self.card.pk])), "Suggest steps")
        with self.settings(ANTHROPIC_API_KEY="k"):
            self.assertContains(self.client.get(reverse("card_modal", args=[self.card.pk])), "Suggest steps")

    def test_theme(self):
        self.client.post(reverse("set_theme"), {"theme": "light"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme, "light")
        self.assertContains(self.client.get(reverse("home")), 'data-theme="light"')
        self.client.post(reverse("set_theme"), {"theme": "neon"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme, "light")


class WatchTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery W")
        self.owner = User.objects.create_user("own@x.com", "pw", first_name="Own")
        self.watcher = User.objects.create_user("watch@x.com", "pw", first_name="Wat")
        for u in (self.owner, self.watcher):
            WorkspaceMember.objects.create(workspace=self.ws, user=u)
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.owner, role="admin")
        self.l1 = List.objects.create(board=self.board, title="To do", position=1)
        self.l2 = List.objects.create(board=self.board, title="Done", position=2)
        self.card = Card.objects.create(list=self.l1, title="Tins")
        self.c = Client()
        self.c.force_login(self.owner)

    def post(self, client, url, data=None):
        return client.post(url, json.dumps(data or {}), content_type="application/json")

    def test_card_watcher_hears_moves_and_rolls_up(self):
        w = Client(); w.force_login(self.watcher)
        self.assertEqual(self.post(w, f"/api/cards/{self.card.pk}/watch/").status_code, 200)
        self.post(self.c, f"/api/cards/{self.card.pk}/move/", {"list_id": self.l2.pk})
        n = Notification.objects.get(recipient=self.watcher)
        self.assertEqual(n.kind, "watched")
        self.assertIn("Tins", n.text)
        self.post(self.c, f"/api/cards/{self.card.pk}/update/", {"is_complete": True})
        self.assertEqual(Notification.objects.filter(recipient=self.watcher).count(), 1)
        n.refresh_from_db()
        self.assertIn("1 other change", n.excerpt)
        # comments too
        self.post(self.c, f"/api/cards/{self.card.pk}/comments/", {"body": "done"})
        self.assertTrue(Notification.objects.filter(recipient=self.watcher, text__startswith="commented").exists())
        # unwatch
        self.post(w, f"/api/cards/{self.card.pk}/watch/")
        self.assertFalse(Watch.objects.filter(user=self.watcher).exists())

    def test_board_watcher_and_no_self_notify(self):
        w = Client(); w.force_login(self.watcher)
        r = self.post(w, f"/api/boards/{self.board.pk}/watch/")
        self.assertTrue(r.json()["watching"])
        self.post(w, f"/api/boards/{self.board.pk}/watch/")  # off
        self.post(w, f"/api/boards/{self.board.pk}/watch/")  # on again
        self.post(self.c, f"/api/lists/{self.l1.pk}/cards/", {"title": "New one"})
        self.assertTrue(Notification.objects.filter(recipient=self.watcher, kind="watched").exists())
        self.post(self.c, f"/api/cards/{self.card.pk}/watch/")
        self.post(self.c, f"/api/cards/{self.card.pk}/move/", {"list_id": self.l2.pk})
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())


class MapTests(TestCase):
    def setUp(self):
        from .models import CustomField
        self.ws = Workspace.objects.create(name="Bakery M")
        self.u = User.objects.create_user("map@x.com", "pw", first_name="Map")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Deliveries")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.lst = List.objects.create(board=self.board, title="Today")
        self.card = Card.objects.create(list=self.lst, title="Drop at farm shop")
        self.field = CustomField.objects.create(board=self.board, name="Where", kind="location")
        self.c = Client(); self.c.force_login(self.u)

    def test_location_is_geocoded_once_and_pinned(self):
        from unittest import mock
        from . import geo
        with mock.patch.object(geo, "lookup", return_value=(51.16, -2.58)) as lk:
            r = self.c.post(f"/api/cards/{self.card.pk}/fields/",
                            json.dumps({"field_id": self.field.pk, "value": "BA4 6NT"}), content_type="application/json")
            self.assertEqual(r.status_code, 200)
            page = self.c.get(f"/b/{self.board.pk}/map/")
            self.assertEqual(lk.call_count, 1)        # not looked up again on the map page
        self.assertContains(page, "Drop at farm shop")
        self.assertContains(page, "51.16")
        self.assertEqual(self.c.get("/all/map/").status_code, 200)

    def test_latlng_text_and_unplaced(self):
        from . import geo
        self.assertEqual(geo.lookup("51.5, -0.12"), (51.5, -0.12))
        from unittest import mock
        with mock.patch.object(geo, "lookup", return_value=None):
            self.c.post(f"/api/cards/{self.card.pk}/fields/",
                        json.dumps({"field_id": self.field.pk, "value": "nowhere"}), content_type="application/json")
            page = self.c.get(f"/b/{self.board.pk}/map/")
        self.assertContains(page, "Couldn")


class ButtonTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery Btn")
        self.u = User.objects.create_user("btn@x.com", "pw", first_name="Btn")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.todo = List.objects.create(board=self.board, title="To do", position=1)
        self.done = List.objects.create(board=self.board, title="Done", position=2)
        self.card = Card.objects.create(list=self.todo, title="Wholesale order")
        self.c = Client(); self.c.force_login(self.u)

    def test_card_button(self):
        from .models import Button
        r = self.c.post(f"/b/{self.board.pk}/automation/buttons/", {
            "kind": "card", "name": "Delivered", "icon": "🚚",
            "baction_0": "move", "blist_0": self.done.pk, "baction_1": "complete", "baction_2": "add_me"})
        self.assertEqual(r.status_code, 302)
        b = Button.objects.get()
        self.assertEqual([a["type"] for a in b.actions], ["move", "complete", "add_me"])
        modal = self.c.get(f"/cards/{self.card.pk}/")
        self.assertContains(modal, "Delivered")
        r = self.c.post(f"/api/cards/{self.card.pk}/buttons/{b.pk}/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.done)
        self.assertTrue(self.card.is_complete)
        self.assertTrue(self.card.members.filter(pk=self.u.pk).exists())
        self.assertTrue(Activity.objects.filter(verb="button", card=self.card).exists())
        self.assertContains(self.c.get(f"/b/{self.board.pk}/automation/?s=card-buttons"), "Pressed 1 time")

    def test_board_button(self):
        from .models import Button
        Card.objects.create(list=self.done, title="Old", is_complete=True)
        Card.objects.create(list=self.done, title="Old 2")
        self.c.post(f"/b/{self.board.pk}/automation/buttons/", {
            "kind": "board", "name": "New week",
            "bbaction_0": "archive_done", "bbaction_1": "move_all", "bbfrom_list_1": self.done.pk,
            "bblist_1": self.todo.pk, "bbaction_2": "create_card", "bbtext_2": "Stocktake %d %b",
            "bblist_2": self.todo.pk})
        b = Button.objects.get(kind="board")
        self.assertContains(self.c.get(f"/b/{self.board.pk}/"), "New week")
        r = self.c.post(f"/api/boards/{self.board.pk}/buttons/{b.pk}/", "{}", content_type="application/json")
        self.assertIn("archived 1", r.json()["summary"])
        self.assertTrue(Card.objects.get(title="Old").is_archived)
        self.assertEqual(Card.objects.get(title="Old 2").list, self.todo)
        self.assertTrue(Card.objects.filter(title__startswith="Stocktake ", list=self.todo).exists())


class ReactVoteFormatTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery R")
        self.u = User.objects.create_user("r@x.com", "pw", first_name="Rae")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Ideas")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.lst = List.objects.create(board=self.board, title="Ideas")
        self.card = Card.objects.create(list=self.lst, title="Gingerbread", description="**Big** idea <b>x</b>")
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, data=None):
        return self.c.post(url, json.dumps(data or {}), content_type="application/json")

    def test_markdown_is_safe(self):
        r = self.c.get(f"/cards/{self.card.pk}/")
        self.assertContains(r, "<strong>Big</strong>")
        self.assertContains(r, "&lt;b&gt;x&lt;/b&gt;")
        from .templatetags.lanes_extras import render_md
        self.assertNotIn("javascript:", render_md("[x](javascript:alert(1))").replace("[x](javascript:alert(1))", ""))
        self.assertIn("<li>", render_md("- a\n- b"))

    def test_reactions_toggle(self):
        from .models import Comment
        cm = Comment.objects.create(card=self.card, author=self.u, body="Nice")
        self.assertEqual(self.j(f"/api/comments/{cm.pk}/react/", {"emoji": "👍"}).status_code, 200)
        self.assertEqual(cm.reactions.count(), 1)
        self.assertContains(self.c.get(f"/cards/{self.card.pk}/"), "👍 1")
        self.j(f"/api/comments/{cm.pk}/react/", {"emoji": "👍"})
        self.assertEqual(cm.reactions.count(), 0)
        self.assertEqual(self.j(f"/api/comments/{cm.pk}/react/", {"emoji": "💩"}).status_code, 400)

    def test_voting_needs_power_up(self):
        self.assertEqual(self.j(f"/api/cards/{self.card.pk}/vote/").status_code, 400)
        self.j(f"/api/boards/{self.board.pk}/update/", {"voting": True})
        r = self.j(f"/api/cards/{self.card.pk}/vote/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("👍1", r.json()["tile"])
        self.j(f"/api/cards/{self.card.pk}/vote/")
        self.assertEqual(self.card.votes.count(), 0)

    def test_card_aging(self):
        from datetime import timedelta
        from django.utils import timezone
        Card.objects.filter(pk=self.card.pk).update(updated_at=timezone.now() - timedelta(days=20))
        self.card.refresh_from_db()
        self.assertEqual(self.card.age_class, "")
        self.j(f"/api/boards/{self.board.pk}/update/", {"card_aging": True})
        self.card = Card.objects.select_related("list__board").get(pk=self.card.pk)
        self.assertEqual(self.card.age_class, "aged-2")
        self.assertContains(self.c.get(f"/b/{self.board.pk}/"), "aged-2")


class GuestPublicTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery G")
        self.admin = User.objects.create_user("adm@x.com", "pw", first_name="Ad")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.admin)
        self.board = Board.objects.create(workspace=self.ws, title="Supplier board")
        self.other = Board.objects.create(workspace=self.ws, title="Secret plans")
        BoardMember.objects.create(board=self.board, user=self.admin, role="admin")
        self.lst = List.objects.create(board=self.board, title="Orders")
        self.card = Card.objects.create(list=self.lst, title="Flour delivery", description="Hi **there**")
        self.c = Client(); self.c.force_login(self.admin)

    def j(self, url, data=None, client=None):
        return (client or self.c).post(url, json.dumps(data or {}), content_type="application/json")

    def test_invite_guest(self):
        r = self.j(f"/api/boards/{self.board.pk}/share/", {"email": "sup@mill.co.uk", "role": "admin"})
        self.assertTrue(r.json()["needs_guest"])
        r = self.j(f"/api/boards/{self.board.pk}/share/", {"email": "sup@mill.co.uk", "role": "admin",
                                                            "invite_guest": True, "first_name": "Sue"})
        acct = r.json()["new_account"]
        guest = User.objects.get(email="sup@mill.co.uk")
        self.assertTrue(guest.is_guest)
        self.assertEqual(BoardMember.objects.get(board=self.board, user=guest).role, "member")  # never admin
        self.assertIn("Guest", r.json()["html"])
        g = Client()
        self.assertTrue(g.login(username="sup@mill.co.uk", password=acct["password"]))
        home = g.get("/")
        self.assertContains(home, "Supplier board")
        self.assertNotContains(home, "Secret plans")
        self.assertEqual(g.get(f"/b/{self.other.pk}/").status_code, 404)
        r = self.j(f"/api/boards/{self.board.pk}/members/{guest.pk}/", {"role": "admin"})
        self.assertEqual(r.status_code, 400)

    def test_public_link(self):
        r = self.j(f"/api/boards/{self.board.pk}/update/", {"public": True})
        token = r.json()["public_token"]
        anon = Client()
        page = anon.get(f"/p/{token}/")
        self.assertContains(page, "Flour delivery")
        self.assertContains(page, "noindex")
        self.assertContains(anon.get(f"/p/{token}/c/{self.card.pk}/"), "<strong>there</strong>")
        other_card = Card.objects.create(list=List.objects.create(board=self.other, title="x"), title="Hidden")
        self.assertEqual(anon.get(f"/p/{token}/c/{other_card.pk}/").status_code, 404)
        self.j(f"/api/boards/{self.board.pk}/update/", {"public": False})
        self.assertEqual(anon.get(f"/p/{token}/").status_code, 404)
        self.assertEqual(anon.get("/p/short/").status_code, 404)


class CompanyTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss@x.com", "pw", first_name="Boss")
        self.staff = User.objects.create_user("staff@x.com", "pw", first_name="Sam")
        self.c = Client()

    def test_login_audit_and_pages(self):
        from .models import AuditEvent
        self.assertTrue(self.c.login(username="boss@x.com", password="pw"))
        Client().post("/login/", {"username": "boss@x.com", "password": "wrong"})
        self.assertTrue(AuditEvent.objects.filter(verb="login").exists())
        self.assertTrue(AuditEvent.objects.filter(verb="login_failed", data__email="boss@x.com").exists())
        self.c.post("/departments/new/", {"name": "Butchery"})
        ws = Workspace.objects.get(name="Butchery")
        self.c.post(f"/departments/{ws.pk}/people/add/", {"email": "staff@x.com", "role": "member"})
        r = self.c.get("/company/audit/")
        self.assertContains(r, "added Sam to Butchery")
        self.assertContains(r, "failed to sign in as boss@x.com")
        self.assertContains(self.c.get("/company/audit/?kind=people"), "created the department Butchery")
        csv_resp = self.c.get("/company/audit/?csv=1")
        self.assertIn("text/csv", csv_resp["Content-Type"])
        u = self.c.get("/company/")
        self.assertContains(u, "Butchery")
        self.assertContains(u, "Never signed in")

    def test_staff_cannot_see(self):
        self.c.force_login(self.staff)
        self.assertEqual(self.c.get("/company/").status_code, 404)
        self.assertEqual(self.c.get("/company/audit/").status_code, 404)


class ButtonTriggersRuleTests(TestCase):
    def test_button_complete_fires_rule(self):
        from .models import Button, Comment, Rule
        ws = Workspace.objects.create(name="Bakery BR")
        u = User.objects.create_user("br@x.com", "pw", first_name="Bo")
        WorkspaceMember.objects.create(workspace=ws, user=u)
        board = Board.objects.create(workspace=ws, title="B")
        BoardMember.objects.create(board=board, user=u, role="admin")
        lst = List.objects.create(board=board, title="L")
        card = Card.objects.create(list=lst, title="C")
        Rule.objects.create(board=board, trigger=Rule.COMPLETED, actions=[{"type": "comment", "text": "Nice work"}])
        b = Button.objects.create(board=board, kind="card", name="Done", actions=[{"type": "complete"}])
        c = Client(); c.force_login(u)
        r = c.post(f"/api/cards/{card.pk}/buttons/{b.pk}/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Comment.objects.filter(card=card, body="Nice work").exists())


class ArchiveMoveDeleteTests(TestCase):
    def setUp(self):
        from .models import CustomField
        self.ws = Workspace.objects.create(name="Bakery A")
        self.u = User.objects.create_user("am@x.com", "pw", first_name="Al")
        self.other = User.objects.create_user("o@x.com", "pw", first_name="Ot")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u, role="admin")
        self.b1 = Board.objects.create(workspace=self.ws, title="Production")
        self.b2 = Board.objects.create(workspace=self.ws, title="Despatch")
        for b in (self.b1, self.b2):
            BoardMember.objects.create(board=b, user=self.u, role="admin")
        BoardMember.objects.create(board=self.b1, user=self.other)
        self.l1 = List.objects.create(board=self.b1, title="Doing")
        self.l2 = List.objects.create(board=self.b2, title="Inbound")
        self.card = Card.objects.create(list=self.l1, title="Pallet")
        self.card.members.add(self.u, self.other)
        lab1 = Label.objects.create(board=self.b1, name="Urgent", colour="red")
        Label.objects.create(board=self.b2, name="Urgent", colour="orange")
        Label.objects.create(board=self.b1, name="Only here", colour="blue")
        self.card.labels.add(lab1, *Label.objects.filter(name="Only here"))
        f1 = CustomField.objects.create(board=self.b1, name="Qty", kind="number")
        CustomField.objects.create(board=self.b2, name="Qty", kind="number")
        from .models import CardFieldValue, Comment
        CardFieldValue.objects.create(card=self.card, field=f1, value=12)
        Comment.objects.create(card=self.card, author=self.u, body="hello")
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, data=None):
        return self.c.post(url, json.dumps(data or {}), content_type="application/json")

    def test_move_to_other_board(self):
        r = self.j(f"/api/cards/{self.card.pk}/move/", {"list_id": self.l2.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.l2)
        self.assertEqual([l.name for l in self.card.labels.all()], ["Urgent"])   # matched by name, other dropped
        self.assertEqual(self.card.labels.get().board, self.b2)
        self.assertEqual(list(self.card.members.all()), [self.u])               # not on Despatch → removed
        self.assertEqual(self.card.field_values.get().field.board, self.b2)
        self.assertEqual(self.card.comments.count(), 1)
        self.assertTrue(Activity.objects.filter(board=self.b1, verb="card_moved_out").exists())
        self.assertContains(self.c.get(f"/b/{self.b2.pk}/"), "Pallet")
        self.assertNotContains(self.c.get(f"/b/{self.b1.pk}/"), ">Pallet<")

    def test_cannot_move_to_board_you_cant_edit(self):
        b3 = Board.objects.create(workspace=self.ws, title="Locked", visibility="private")
        l3 = List.objects.create(board=b3, title="x")
        BoardMember.objects.filter(user=self.u).delete()
        WorkspaceMember.objects.filter(user=self.u).update(role="member")
        BoardMember.objects.create(board=self.b1, user=self.u, role="admin")
        self.assertEqual(self.j(f"/api/cards/{self.card.pk}/move/", {"list_id": l3.pk}).status_code, 404)  # private: hidden
        b4 = Board.objects.create(workspace=self.ws, title="Visible")                                        # visible, not a member
        l4 = List.objects.create(board=b4, title="y")
        self.assertEqual(self.j(f"/api/cards/{self.card.pk}/move/", {"list_id": l4.pk}).status_code, 403)
        self.card.refresh_from_db(); self.assertEqual(self.card.list, self.l1)
        o = Client(); o.force_login(self.other)
        r = o.post(f"/api/cards/{self.card.pk}/move/", json.dumps({"list_id": self.l2.pk}), content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_restore_list_and_board_delete_card(self):
        self.j(f"/api/lists/{self.l1.pk}/", {"archive": True})
        self.assertContains(self.c.get(f"/b/{self.b1.pk}/"), "data-restore-list")
        self.j(f"/api/lists/{self.l1.pk}/", {"archive": False})
        self.l1.refresh_from_db(); self.assertFalse(self.l1.is_archived)
        self.assertEqual(self.j(f"/api/cards/{self.card.pk}/delete/").status_code, 400)  # must archive first
        self.j(f"/api/cards/{self.card.pk}/update/", {"archive": True})
        self.assertEqual(self.j(f"/api/cards/{self.card.pk}/delete/").status_code, 200)
        self.assertFalse(Card.objects.filter(pk=self.card.pk).exists())
        self.j(f"/api/boards/{self.b2.pk}/update/", {"archive": True})
        self.assertContains(self.c.get("/"), "Archived boards")
        r = self.c.post(f"/boards/{self.b2.pk}/restore/")
        self.assertEqual(r.status_code, 302)
        self.b2.refresh_from_db(); self.assertFalse(self.b2.is_archived)


class TwoStepTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("ts@x.com", "pw-12345", first_name="Tia")
        self.c = Client()

    def test_setup_login_and_recovery(self):
        import time
        from accounts import twofactor
        self.c.force_login(self.u)
        r = self.c.get("/account/two-step/")
        self.assertContains(r, "<svg")
        secret = self.c.session["2s_setup_secret"]
        code = twofactor._code(secret, int(time.time() // 30))
        r = self.c.post("/account/two-step/", {"code": code}, follow=True)
        self.assertContains(r, "recovery codes")
        self.u.refresh_from_db()
        self.assertTrue(self.u.totp_enabled)
        recovery = self.c.session.get("2s_fresh_codes") or []
        # sign in again: password, then code page
        c2 = Client()
        r = c2.post("/login/", {"username": "ts@x.com", "password": "pw-12345"})
        self.assertRedirects(r, "/login/code/", fetch_redirect_response=False)
        self.assertEqual(c2.get("/").status_code, 302)             # not signed in yet
        r = c2.post("/login/code/", {"code": code})                 # same code can't be replayed
        self.assertContains(r, "didn")
        r = c2.post("/login/code/", {"code": twofactor._code(secret, int(time.time() // 30) + 1)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c2.get("/").status_code, 200)
        # recovery code works once (codes were shown on the confirmation page)
        page = self.c.get("/account/two-step/")
        c3 = Client(); c3.post("/login/", {"username": "ts@x.com", "password": "pw-12345"})
        self.u.refresh_from_db()
        from accounts.twofactor import new_recovery_codes
        codes, hashes = new_recovery_codes(); self.u.recovery_codes = hashes; self.u.save()
        self.assertEqual(c3.post("/login/code/", {"code": codes[0]}).status_code, 302)
        self.u.refresh_from_db(); self.assertEqual(len(self.u.recovery_codes), 7)

    def test_too_many_tries_and_required(self):
        from .models import CompanySettings
        from accounts import twofactor
        self.u.totp_secret, self.u.totp_enabled = twofactor.new_secret(), True
        self.u.save()
        self.c.post("/login/", {"username": "ts@x.com", "password": "pw-12345"})
        for _ in range(5):
            r = self.c.post("/login/code/", {"code": "000000"})
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)
        # company requires it: someone without it is sent to setup
        other = User.objects.create_user("no2@x.com", "pw", first_name="No")
        cs = CompanySettings.get(); cs.require_two_step = "everyone"; cs.save()
        c = Client(); c.force_login(other)
        self.assertRedirects(c.get("/"), "/account/two-step/", fetch_redirect_response=False)
        self.assertEqual(c.post("/api/inbox/add/", "{}", content_type="application/json").status_code, 403)


class CompanySettingsTests(TestCase):
    def setUp(self):
        from .models import CompanySettings
        self.boss = User.objects.create_superuser("boss2@x.com", "pw", first_name="Bo")
        self.u = User.objects.create_user("m@x.com", "pw", first_name="Mo")
        self.ws = Workspace.objects.create(name="Kitchen")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Prep")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.c = Client(); self.c.force_login(self.u)
        self.cs = CompanySettings.get()

    def j(self, url, data):
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def test_rules_apply(self):
        b = Client(); b.force_login(self.boss)
        r = b.post("/company/settings/", {"public_links": "company_admins", "invite_guests": "nobody",
                                          "create_boards": "dept_admins", "private_boards": "dept_admins",
                                          "require_two_step": "off"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.j(f"/api/boards/{self.board.pk}/update/", {"public": True}).status_code, 403)
        self.assertEqual(self.j(f"/api/boards/{self.board.pk}/update/", {"visibility": "private"}).status_code, 403)
        r = self.j(f"/api/boards/{self.board.pk}/share/", {"email": "new@out.com"})
        self.assertIn("switched off", r.json()["error"])
        self.c.post("/boards/new/", {"workspace": self.ws.pk, "title": "Nope"})
        self.assertFalse(Board.objects.filter(title="Nope").exists())
        self.assertNotContains(self.c.get("/"), "Create new board")
        self.assertContains(b.get("/company/audit/"), "changed company settings")
        # company admin can still do it; "nobody" kills existing links
        token = b.post(f"/api/boards/{self.board.pk}/update/", json.dumps({"public": True}),
                       content_type="application/json").json()["public_token"]
        self.assertEqual(Client().get(f"/p/{token}/").status_code, 200)
        self.cs.refresh_from_db(); self.cs.public_links = "nobody"; self.cs.save()
        self.assertEqual(Client().get(f"/p/{token}/").status_code, 404)

    def test_people_page_switch_off(self):
        b = Client(); b.force_login(self.boss)
        self.assertContains(b.get("/company/people/"), "m@x.com")
        b.post("/company/people/", {"user_id": self.u.pk, "action": "deactivate"})
        self.u.refresh_from_db(); self.assertFalse(self.u.is_active)
        self.assertFalse(Client().login(username="m@x.com", password="pw"))


class AdminLoginTests(TestCase):
    def test_admin_login_goes_through_lanes_login(self):
        r = Client().get("/admin/login/?next=/admin/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/login/?next=/admin/"))


class DueRuleTests(TestCase):
    def setUp(self):
        from .models import Rule
        self.ws = Workspace.objects.create(name="Bakery D")
        self.u = User.objects.create_user("d@x.com", "pw", first_name="Dee")
        self.mate = User.objects.create_user("mate@x.com", "pw", first_name="Mate")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        for u in (self.u, self.mate):
            BoardMember.objects.create(board=self.board, user=u, role="admin")
        self.lst = List.objects.create(board=self.board, title="To do")
        self.urgent = Label.objects.create(board=self.board, name="Urgent", colour="red")
        self.c = Client(); self.c.force_login(self.u)

    def test_form_and_due_soon_runs_once_per_due_date(self):
        from datetime import timedelta
        from django.utils import timezone
        from . import automation
        from .models import Rule
        r = self.c.post(f"/b/{self.board.pk}/automation/rules/", {
            "trigger": "card_due_soon", "trigger_hours": "24",
            "action_0": "add_label", "label_0": self.urgent.pk, "action_1": "notify", "text_1": "Chase supplier"})
        self.assertEqual(r.status_code, 302)
        rule = Rule.objects.get()
        self.assertEqual(rule.trigger_hours, 24)
        self.assertContains(self.c.get(f"/b/{self.board.pk}/automation/?s=due"), "due within 1 day")
        now = timezone.now()
        soon = Card.objects.create(list=self.lst, title="Flour", due_at=now + timedelta(hours=5))
        soon.members.add(self.mate)
        later = Card.objects.create(list=self.lst, title="Sugar", due_at=now + timedelta(days=4))
        self.assertEqual(automation.run_due_rules(), 1)
        self.assertEqual(automation.run_due_rules(), 0)             # once only
        self.assertTrue(soon.labels.filter(pk=self.urgent.pk).exists())
        self.assertFalse(later.labels.exists())
        n = Notification.objects.get(recipient=self.mate, kind="automation")
        self.assertIn("Chase supplier", n.text)
        soon.due_at = now + timedelta(hours=10); soon.save()        # new due date → runs again
        self.assertEqual(automation.run_due_rules(), 1)

    def test_overdue_ignores_ancient_and_complete(self):
        from datetime import timedelta
        from django.utils import timezone
        from . import automation
        from .models import Rule
        Rule.objects.create(board=self.board, trigger="card_overdue", actions=[{"type": "move_top"}, {"type": "add_sticker", "sticker": "⏰"}])
        now = timezone.now()
        late = Card.objects.create(list=self.lst, title="Late", due_at=now - timedelta(hours=2))
        Card.objects.create(list=self.lst, title="Done", due_at=now - timedelta(hours=2), is_complete=True)
        Card.objects.create(list=self.lst, title="Ancient", due_at=now - timedelta(days=90))
        self.assertEqual(automation.run_due_rules(), 1)
        late.refresh_from_db()
        self.assertIn("⏰", late.stickers)


class AIFeatureTests(TestCase):
    def setUp(self):
        from .models import Comment
        self.ws = Workspace.objects.create(name="Bakery AI")
        self.u = User.objects.create_user("ai@x.com", "pw", first_name="Ava")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Xmas")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.lst = List.objects.create(board=self.board, title="Doing")
        self.card = Card.objects.create(list=self.lst, title="Order cake boards")
        self.card.members.add(self.u)
        Comment.objects.create(card=self.card, author=self.u, body="Supplier says Thursday")
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, data=None):
        return self.c.post(url, json.dumps(data or {}), content_type="application/json")

    def test_endpoints(self):
        from unittest import mock
        from django.test import override_settings
        from . import ai
        with override_settings(ANTHROPIC_API_KEY="test"):
            with mock.patch.object(ai, "_call", return_value='{"summary": "Boards ordered, arriving Thu.", "status": "on track", "next": "Confirm delivery", "open_questions": ["Which size?"]}') as m:
                r = self.j(f"/api/cards/{self.card.pk}/summarise/")
                self.assertEqual(r.json()["status"], "on track")
                self.assertIn("Supplier says Thursday", m.call_args[0][1])     # comments go to the model
            with mock.patch.object(ai, "_call", return_value="## Plan\n- **20** boards"):
                r = self.j(f"/api/cards/{self.card.pk}/write-description/", {"notes": "20 boards"})
                self.assertIn("**20**", r.json()["text"])
            with mock.patch.object(ai, "_call", return_value=f'{{"picks": [{{"id": "c{self.card.pk}", "why": "Due soon"}}, {{"id": "c999", "why": "made up"}}], "note": "Start early"}}'):
                r = self.j("/api/ai/priorities/")
                self.assertContains(r, "Order cake boards")
                self.assertNotIn("made up", r.json()["html"])                  # ids not given are ignored
            self.assertContains(self.c.get(f"/cards/{self.card.pk}/"), "data-summarise")
            self.assertContains(self.c.get("/my-cards/"), "What should I do first")
        # without a key the buttons don't show and the API says so
        self.assertNotContains(self.c.get(f"/cards/{self.card.pk}/"), "data-summarise")
        self.assertEqual(self.j(f"/api/cards/{self.card.pk}/summarise/").status_code, 503)


class PeopleImportTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss3@x.com", "pw", first_name="Bo")
        self.ws = Workspace.objects.create(name="Bakery")
        self.existing = User.objects.create_user("old@x.com", "pw", first_name="Old")
        self.c = Client(); self.c.force_login(self.boss)

    def upload(self, content, name="people.csv", **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.c.post("/company/people/import/", {"file": SimpleUploadedFile(name, content), **extra})

    def test_csv_preview_then_confirm(self):
        csv_bytes = ("First name,Last name,Email,Department,Role\n"
                     "Ann,Lee,ann@x.com,Bakery,Admin\n"
                     "Old,One,OLD@x.com,Bakery,\n"
                     ",,bad-email,Bakery,\n"
                     "Kit,Chen,kit@x.com,Kitchen,\n").encode()
        r = self.upload(csv_bytes)
        self.assertContains(r, "1 new account")
        self.assertContains(r, "No department called")          # Kitchen missing, box not ticked
        self.assertContains(r, "Email missing or not valid")
        self.assertFalse(User.objects.filter(email="ann@x.com").exists())   # nothing changed yet
        r = self.c.post("/company/people/import/", {"step": "confirm"})
        self.assertContains(r, "1</b> new account")
        ann = User.objects.get(email="ann@x.com")
        self.assertEqual(WorkspaceMember.objects.get(user=ann, workspace=self.ws).role, "admin")
        self.assertTrue(WorkspaceMember.objects.filter(user=self.existing, workspace=self.ws).exists())
        dl = self.c.get("/company/people/import/?download=1")
        self.assertIn("ann@x.com", dl.content.decode())
        pw = dl.content.decode().splitlines()[1].split(",")[3]
        self.assertTrue(Client().login(username="ann@x.com", password=pw))
        self.assertEqual(self.c.get("/company/people/import/?download=1").status_code, 302)   # only once

    def test_excel_with_name_column_and_new_departments(self):
        import io
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        ws.append(["Name", "E-mail", "Dept"]); ws.append(["Pat Green", "pat@x.com", "Kitchen"])
        buf = io.BytesIO(); wb.save(buf)
        r = self.upload(buf.getvalue(), name="staff.xlsx", create_departments="on")
        self.assertContains(r, "will be created: Kitchen")
        self.c.post("/company/people/import/", {"step": "confirm"})
        pat = User.objects.get(email="pat@x.com")
        self.assertEqual((pat.first_name, pat.last_name), ("Pat", "Green"))
        self.assertTrue(Workspace.objects.filter(name="Kitchen").exists())

    def test_bad_files_and_non_admin(self):
        self.assertContains(self.upload(b"Name,Phone\nA,1\n"), "Couldn&#x27;t find an Email column")
        self.assertContains(self.upload(b"x", name="a.pdf"), "Upload a CSV")
        self.assertEqual(self.c.get("/company/people/import/?template=1").status_code, 200)
        staff = Client(); staff.force_login(self.existing)
        self.assertEqual(staff.get("/company/people/import/").status_code, 404)


class EmailTests(TestCase):
    def setUp(self):
        from django.test import override_settings
        self.override = override_settings(EMAIL_ENABLED=True, EMAIL_VIA="test",
                                          EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                                          SITE_URL="https://lanes.example")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.ws = Workspace.objects.create(name="Bakery E")
        self.boss = User.objects.create_superuser("boss4@x.com", "pw", first_name="Bo")
        self.u = User.objects.create_user("e@x.com", "pw-12345", first_name="Eve")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        WorkspaceMember.objects.create(workspace=self.ws, user=self.boss, role="admin")
        self.board = Board.objects.create(workspace=self.ws, title="B")
        BoardMember.objects.create(board=self.board, user=self.u)
        BoardMember.objects.create(board=self.board, user=self.boss, role="admin")
        self.card = Card.objects.create(list=List.objects.create(board=self.board, title="L"), title="Bake")

    def test_notification_emails_grouped_and_once(self):
        from datetime import timedelta
        from django.core import mail
        from django.utils import timezone
        from . import mailer, notify
        notify.send(self.u, "assigned", actor=self.boss, card=self.card, text="assigned you to Bake")
        notify.send(self.u, "commented", actor=self.boss, card=self.card, text="commented on Bake", excerpt="hi")
        self.assertEqual(mailer.run()[0], 0)                       # too fresh (2-minute grace)
        later = timezone.now() + timedelta(minutes=3)
        self.assertEqual(mailer.run(later)[0], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 new updates", mail.outbox[0].subject)
        self.assertIn("https://lanes.example/notifications/", mail.outbox[0].body)
        self.assertEqual(mailer.run(later)[0], 0)                  # not again
        # read in Lanes → no email; "off" → no email
        n = notify.send(self.u, "assigned", actor=self.boss, card=self.card, text="x", dedupe_key="k1")
        n.read_at = timezone.now(); n.save()
        self.u.email_notifications = "off"; self.u.save()
        notify.send(self.u, "assigned", actor=self.boss, card=self.card, text="y", dedupe_key="k2")
        mailer.run(later + timedelta(minutes=5))
        self.assertEqual(len(mail.outbox), 1)

    def test_daily_digest(self):
        from datetime import timedelta
        from django.core import mail
        from django.utils import timezone
        from . import mailer
        self.u.email_notifications = "daily"; self.u.save()
        self.card.members.add(self.u)
        self.card.due_at = timezone.now() + timedelta(hours=3); self.card.save()
        eight = timezone.localtime().replace(hour=8, minute=0)
        mailer.run(eight)
        self.assertTrue(any("summary" in m.subject for m in mail.outbox))
        n = len(mail.outbox); mailer.run(eight + timedelta(minutes=30))
        self.assertEqual(len(mail.outbox), n)                      # once a day

    def test_invite_and_set_password_and_forgot(self):
        from django.core import mail
        c = Client(); c.force_login(self.boss)
        c.post(f"/departments/{self.ws.pk}/people/add/", {"email": "new@x.com", "first_name": "Nia",
                                                           "role": "member", "send_invite": "on"})
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        link = [l for l in body.split() if "/password/set/" in l][0].replace("https://lanes.example", "")
        anon = Client()
        r = anon.get(link, follow=True)
        self.assertContains(r, "Choose a password")
        r = anon.post(r.redirect_chain[-1][0], {"new_password1": "Rye-bread-2026", "new_password2": "Rye-bread-2026"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Client().login(username="new@x.com", password="Rye-bread-2026"))
        # forgot password
        self.assertContains(Client().get("/login/"), "Forgot your password")
        Client().post("/password/forgot/", {"email": "e@x.com"})
        self.assertIn("Reset your Lanes password", mail.outbox[-1].subject)

    def test_bulk_invites_are_queued_and_preferences_page(self):
        from django.core import mail
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import OutgoingEmail
        c = Client(); c.force_login(self.boss)
        c.post("/company/people/import/", {"file": SimpleUploadedFile(
            "p.csv", b"First name,Email,Department\nA,a1@x.com,Bakery E\nB,b1@x.com,Bakery E\n")})
        c.post("/company/people/import/", {"step": "confirm", "send_invites": "on"})
        self.assertEqual(OutgoingEmail.objects.filter(sent_at__isnull=True).count(), 2)
        self.assertEqual(len(mail.outbox), 0)                       # sent by the job, not the page
        from . import mailer
        mailer.flush()
        self.assertEqual(len(mail.outbox), 2)
        e = Client(); e.force_login(self.u)
        e.post("/account/email/", {"email_notifications": "daily"})
        self.u.refresh_from_db(); self.assertEqual(self.u.email_notifications, "daily")
        r = c.post("/company/settings/", {"action": "test_email"}, follow=True)
        self.assertContains(r, "Test email sent")


class MicrosoftTests(TestCase):
    TENANT, CLIENT = "11111111-2222-3333-4444-555555555555", "aaaa-bbbb"

    def setUp(self):
        from django.test import override_settings
        o = override_settings(MS_TENANT_ID=self.TENANT, MS_CLIENT_ID=self.CLIENT, MS_CLIENT_SECRET="s3cret")
        o.enable(); self.addCleanup(o.disable)
        self.u = User.objects.create_user("paulo@thenewt.co.uk", "pw-12345", first_name="Paulo")

    def token(self, real_nonce, **claims):
        import base64, time
        body = {"aud": self.CLIENT, "tid": self.TENANT, "iss": f"https://login.microsoftonline.com/{self.TENANT}/v2.0",
                "nonce": real_nonce, "exp": time.time() + 600, "oid": "oid-1", "preferred_username": "paulo@thenewt.co.uk",
                "name": "Paulo Silva", **claims}
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        return {"id_token": f"{enc({'alg': 'RS256'})}.{enc(body)}.sig"}

    def sign_in(self, client, **claims):
        from unittest import mock
        from accounts import microsoft
        r = client.get("/login/microsoft/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login.microsoftonline.com", r["Location"])
        flow = client.session["ms_auth"]
        with mock.patch.object(microsoft, "post_form", return_value=self.token(flow["nonce"], **claims)) as pf:
            r = client.get(f"/login/microsoft/callback/?code=abc&state={flow['state']}")
            if pf.called:
                self.assertEqual(pf.call_args[0][1]["code_verifier"], flow["verifier"])
        return r

    def test_existing_user_signs_in_and_is_linked(self):
        c = Client()
        self.assertContains(c.get("/login/"), "Sign in with Microsoft")
        r = self.sign_in(c)
        self.assertEqual(r.status_code, 302); self.assertEqual(r["Location"], "/")
        self.assertEqual(c.get("/").status_code, 200)
        self.u.refresh_from_db(); self.assertEqual(self.u.ms_oid, "oid-1")
        # later matched by oid even if the email changed
        c2 = Client(); self.sign_in(c2, preferred_username="p.silva@thenewt.co.uk")
        self.assertEqual(c2.get("/").status_code, 200)

    def test_rejects_wrong_tenant_nonce_and_unknown(self):
        c = Client()
        self.sign_in(c, tid="99999999-0000-0000-0000-000000000000")
        self.assertEqual(c.get("/").status_code, 302)                  # not signed in
        c = Client(); self.sign_in(c, nonce="forged")
        self.assertEqual(c.get("/").status_code, 302)
        c = Client(); r = self.sign_in(c, oid="oid-x", preferred_username="stranger@thenewt.co.uk")
        self.assertEqual(c.get("/").status_code, 302)
        self.assertFalse(User.objects.filter(email="stranger@thenewt.co.uk").exists())

    def test_auto_create_and_required_mode(self):
        from .models import CompanySettings
        cs = CompanySettings.get(); cs.microsoft_auto_create = True; cs.require_two_step = "everyone"; cs.save()
        c = Client(); self.sign_in(c, oid="oid-new", preferred_username="new.starter@thenewt.co.uk",
                                   given_name="New", family_name="Starter")
        new = User.objects.get(email="new.starter@thenewt.co.uk")
        self.assertFalse(new.has_usable_password())
        self.assertEqual(c.get("/").status_code, 200)        # Microsoft did two-step, so no Lanes setup page
        cs.microsoft_login = "required"; cs.save()
        r = Client().post("/login/", {"username": "paulo@thenewt.co.uk", "password": "pw-12345"}, follow=True)
        self.assertContains(r, "use the Microsoft button")
        cs.microsoft_login = "off"; cs.save()
        self.assertEqual(Client().get("/login/microsoft/").status_code, 404)

    def test_graph_email_backend(self):
        from unittest import mock
        from django.core.mail import EmailMultiAlternatives
        from django.test import override_settings
        from accounts import graph_mail
        with override_settings(EMAIL_BACKEND="accounts.graph_mail.GraphEmailBackend", MS_MAIL_SENDER="lanes@thenewt.co.uk"):
            with mock.patch.object(graph_mail, "graph_post", return_value=202) as gp:
                m = EmailMultiAlternatives("Hi", "text", "Lanes <lanes@thenewt.co.uk>", ["a@b.com"])
                m.attach_alternative("<b>hi</b>", "text/html")
                self.assertEqual(m.send(), 1)
        path, payload = gp.call_args[0]
        self.assertEqual(path, "/users/lanes@thenewt.co.uk/sendMail")
        self.assertEqual(payload["message"]["body"]["contentType"], "HTML")
        self.assertEqual(payload["message"]["toRecipients"][0]["emailAddress"]["address"], "a@b.com")


class CronHookTests(TestCase):
    def test_hook_needs_token(self):
        from django.test import override_settings
        c = Client()
        self.assertEqual(c.post("/internal/run-automations/").status_code, 404)        # no token configured
        with override_settings(CRON_TOKEN="t0ken"):
            self.assertEqual(c.post("/internal/run-automations/", HTTP_AUTHORIZATION="Bearer nope").status_code, 404)
            r = c.post("/internal/run-automations/", HTTP_AUTHORIZATION="Bearer t0ken")
            self.assertEqual(r.status_code, 200)
            self.assertIn("rules", r.json())


class TrelloTouchesTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery T")
        self.u = User.objects.create_user("t@x.com", "pw", first_name="Tom")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.b1 = Board.objects.create(workspace=self.ws, title="One")
        self.b2 = Board.objects.create(workspace=self.ws, title="Two")
        for b in (self.b1, self.b2):
            BoardMember.objects.create(board=b, user=self.u, role="admin")
        self.l1 = List.objects.create(board=self.b1, title="To do", position=1000)
        self.l2 = List.objects.create(board=self.b1, title="Done", position=2000)
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, data=None):
        return self.c.post(url, json.dumps(data or {}), content_type="application/json")

    def test_smart_due_on_create(self):
        r = self.j(f"/api/lists/{self.l1.pk}/cards/", {"title": "Gemma starts next Monday", "smart_due": True})
        card = Card.objects.get(pk=r.json()["card_id"])
        self.assertIsNotNone(card.due_at)
        self.assertEqual(card.due_at.weekday(), 0)
        r = self.j(f"/api/lists/{self.l1.pk}/cards/", {"title": "Order flour tomorrow"})      # chip dismissed
        self.assertIsNone(Card.objects.get(pk=r.json()["card_id"]).due_at)
        self.assertIn("label", self.c.get("/api/parse-date/?q=deep clean friday 3pm").json())
        self.assertIsNone(self.c.get("/api/parse-date/?q=sun-dried tomato loaf").json()["due"])

    def test_list_actions(self):
        from datetime import timedelta
        from django.utils import timezone
        a = Card.objects.create(list=self.l1, title="b-card", due_at=timezone.now() + timedelta(days=3), position=1)
        b = Card.objects.create(list=self.l1, title="a-card", due_at=timezone.now() + timedelta(days=1), position=2)
        self.j(f"/api/lists/{self.l1.pk}/sort/", {"by": "due"})
        a.refresh_from_db(); b.refresh_from_db(); self.assertLess(b.position, a.position)
        self.j(f"/api/lists/{self.l1.pk}/sort/", {"by": "name"})
        a.refresh_from_db(); b.refresh_from_db(); self.assertLess(b.position, a.position)
        self.j(f"/api/lists/{self.l1.pk}/copy/", {"title": "To do 2"})
        copy = List.objects.get(title="To do 2")
        self.assertEqual(copy.cards.count(), 2)
        self.assertTrue(self.l1.position < copy.position < self.l2.position)
        self.assertEqual(self.j(f"/api/lists/{self.l1.pk}/move-cards/", {"list_id": self.l2.pk}).json()["moved"], 2)
        self.assertEqual(self.l2.cards.count(), 2)
        r = self.j(f"/api/lists/{self.l2.pk}/move-board/", {"board_id": self.b2.pk})
        self.assertEqual(r.json()["moved"], 2)
        self.assertEqual(Card.objects.filter(list__board=self.b2).count(), 2)
        self.l2.refresh_from_db(); self.assertTrue(self.l2.is_archived)
        self.assertEqual(self.j(f"/api/lists/{copy.pk}/archive-cards/").json()["archived"], 2)
        boards = self.c.get("/api/boards/mine/").json()["boards"]
        self.assertEqual({b["title"] for b in boards}, {"One", "Two"})

    def test_board_page_has_new_controls(self):
        Card.objects.create(list=self.l1, title="x")
        r = self.c.get(f"/b/{self.b1.pk}/")
        for marker in ("data-vis-toggle", "data-switch-boards", 'data-pane="labels"', 'data-pane="about"',
                       "data-list-move-board", "data-quick-edit", "data-collapse-list"):
            self.assertContains(r, marker)
        # a plain member (not admin) still gets the Copy/Export panes
        m = User.objects.create_user("m2@x.com", "pw", first_name="Mem")
        WorkspaceMember.objects.create(workspace=self.ws, user=m)
        BoardMember.objects.create(board=self.b1, user=m, role="member")
        mc = Client(); mc.force_login(m)
        r = mc.get(f"/b/{self.b1.pk}/")
        self.assertContains(r, 'data-pane="export"')
        self.assertNotContains(r, 'data-pane="settings"')


class AutomationV2Tests(TestCase):
    """By-whom qualifiers, member-added trigger, checklist / working-day actions,
    scheduled board actions and the sectioned page."""
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery V2")
        self.me = User.objects.create_user("me2@x.com", "pw", first_name="Me")
        self.other = User.objects.create_user("other2@x.com", "pw", first_name="Other")
        for u in (self.me, self.other):
            WorkspaceMember.objects.create(workspace=self.ws, user=u)
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        for u in (self.me, self.other):
            BoardMember.objects.create(board=self.board, user=u, role="admin")
        self.todo = List.objects.create(board=self.board, title="To do", position=1)
        self.done = List.objects.create(board=self.board, title="Done", position=2)
        self.c = Client(); self.c.force_login(self.me)
        self.c2 = Client(); self.c2.force_login(self.other)

    def _add(self, client, title):
        r = client.post(f"/api/lists/{self.todo.pk}/cards/", json.dumps({"title": title}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return Card.objects.get(title=title)

    def test_by_me_and_add_checklist(self):
        from .models import Rule
        r = self.c.post(f"/b/{self.board.pk}/automation/rules/", {
            "section": "rules", "trigger": "card_added", "trigger_list": self.todo.pk, "by_whom": "me",
            "action_0": "add_checklist", "cltitle_0": "Steps", "clitems_0": "Weigh\nMix\n\nBake"})
        self.assertRedirects(r, f"/b/{self.board.pk}/automation/?s=rules", fetch_redirect_response=False)
        rule = Rule.objects.get()
        self.assertEqual(rule.by_whom, "me")
        mine = self._add(self.c, "Mine")
        theirs = self._add(self.c2, "Theirs")
        self.assertEqual([i.text for i in mine.checklists.get(title="Steps").items.order_by("position")],
                         ["Weigh", "Mix", "Bake"])
        self.assertFalse(theirs.checklists.exists())
        rule.by_whom = "others"; rule.save()
        self.assertTrue(self._add(self.c2, "Theirs 2").checklists.exists())
        self.assertFalse(self._add(self.c, "Mine 2").checklists.exists())
        page = self.c.get(f"/b/{self.board.pk}/automation/?s=rules")
        self.assertContains(page, "by Me")
        self.assertContains(page, "add checklist “Steps” (3 items)")

    def test_when_i_am_added_working_days(self):
        from datetime import datetime
        from django.utils import timezone
        from . import automation
        self.c.post(f"/b/{self.board.pk}/automation/rules/", {
            "trigger": "member_added", "member_who": "me",
            "action_0": "set_due_workdays", "days_0": "5", "action_1": "comment", "text_1": "I got this!"})
        card = Card.objects.create(list=self.todo, title="Job")
        url = f"/api/cards/{card.pk}/members/"
        self.c.post(url, json.dumps({"user_id": self.other.pk}), content_type="application/json")
        card.refresh_from_db()
        self.assertIsNone(card.due_at)                     # someone else was added: no
        self.c2.post(url, json.dumps({"user_id": self.me.pk}), content_type="application/json")
        card.refresh_from_db()
        self.assertIsNotNone(card.due_at)                  # I was added (by anyone): yes
        self.assertTrue(card.comments.filter(body="I got this!").exists())
        self.assertLess(timezone.localtime(card.due_at).weekday(), 5)
        # Friday + 1 working day = Monday; Saturday + 0 = Monday
        tz = timezone.get_current_timezone()
        fri = timezone.make_aware(datetime(2026, 10, 23, 9), tz)
        self.assertEqual(automation.add_working_days(fri, 1).date().isoformat(), "2026-10-26")
        self.assertEqual(automation.add_working_days(fri, 5).date().isoformat(), "2026-10-30")
        sat = timezone.make_aware(datetime(2026, 10, 24, 9), tz)
        self.assertEqual(automation.add_working_days(sat, 0).date().isoformat(), "2026-10-26")

    def test_scheduled_board_actions(self):
        from django.utils import timezone
        from . import automation
        from .models import Schedule
        Card.objects.create(list=self.done, title="Old 1")
        Card.objects.create(list=self.done, title="Old 2")
        r = self.c.post(f"/b/{self.board.pk}/automation/schedules/", {
            "section": "scheduled", "kind": "actions", "frequency": "weekly", "weekday": "4", "hour": "17",
            "saction_0": "move_all", "sfrom_list_0": self.done.pk, "slist_0": self.todo.pk})
        self.assertEqual(r.status_code, 302)
        s = Schedule.objects.get()
        self.assertEqual((s.kind, s.target_list), ("actions", None))
        page = self.c.get(f"/b/{self.board.pk}/automation/?s=scheduled")
        self.assertContains(page, "Every Friday at 17:00")
        self.assertContains(page, "move every card from Done to To do")
        Schedule.objects.filter(pk=s.pk).update(next_run_at=timezone.now())
        self.assertEqual(automation.run_due_schedules(), 1)
        self.assertEqual(self.todo.cards.count(), 2)
        self.assertEqual(automation.run_due_schedules(), 0)     # claimed; not again until next Friday
        # Run now works too
        Card.objects.create(list=self.done, title="Old 3")
        self.c.post(f"/b/{self.board.pk}/automation/schedules/{s.pk}/", {"action": "run"})
        self.assertEqual(self.done.cards.count(), 0)
        act = self.c.get(f"/b/{self.board.pk}/automation/?s=activity")
        self.assertContains(act, "moved 2 cards from Done to To do")

    def test_sections_and_examples(self):
        for s in ("rules", "due", "scheduled", "card-buttons", "board-buttons", "activity"):
            r = self.c.get(f"/b/{self.board.pk}/automation/?s={s}")
            self.assertEqual(r.status_code, 200, s)
        r = self.c.get(f"/b/{self.board.pk}/automation/")
        self.assertContains(r, "data-example=")
        self.assertContains(r, "When I am added to a card")


class TrelloTouches2Tests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery T2")
        self.admin = User.objects.create_user("t2a@x.com", "pw", first_name="Ada")
        self.mem = User.objects.create_user("t2m@x.com", "pw", first_name="Mo")
        self.obs = User.objects.create_user("t2o@x.com", "pw", first_name="Obi")
        self.new = User.objects.create_user("t2n@x.com", "pw", first_name="Nia")
        for u in (self.admin, self.mem, self.obs, self.new):
            WorkspaceMember.objects.create(workspace=self.ws, user=u)
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        BoardMember.objects.create(board=self.board, user=self.admin, role="admin")
        BoardMember.objects.create(board=self.board, user=self.mem, role="member")
        BoardMember.objects.create(board=self.board, user=self.obs, role="observer")
        self.lst = List.objects.create(board=self.board, title="To do", position=1)
        self.done = List.objects.create(board=self.board, title="Done", position=2)
        self.card = Card.objects.create(list=self.lst, title="Sourdough")
        self.ca, self.cm, self.co = Client(), Client(), Client()
        self.ca.force_login(self.admin); self.cm.force_login(self.mem); self.co.force_login(self.obs)

    def j(self, c, url, data):
        return c.post(url, json.dumps(data), content_type="application/json")

    def test_comment_and_member_permissions(self):
        from . import permissions
        self.assertTrue(permissions.can_comment(self.board, self.obs))
        self.j(self.ca, f"/api/boards/{self.board.pk}/update/", {"comment_permission": "members"})
        self.board.refresh_from_db()
        self.assertFalse(permissions.can_comment(self.board, self.obs))
        self.assertTrue(permissions.can_comment(self.board, self.mem))
        self.j(self.ca, f"/api/boards/{self.board.pk}/update/", {"comment_permission": "disabled"})
        self.board.refresh_from_db()
        self.assertFalse(permissions.can_comment(self.board, self.admin))
        # members can't add people until the board allows it
        r = self.j(self.cm, f"/api/boards/{self.board.pk}/share/", {"email": "t2n@x.com"})
        self.assertEqual(r.status_code, 403)
        self.j(self.ca, f"/api/boards/{self.board.pk}/update/", {"member_permission": "members"})
        r = self.j(self.cm, f"/api/boards/{self.board.pk}/share/", {"email": "t2n@x.com", "role": "admin"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(BoardMember.objects.get(board=self.board, user=self.new).role, "member")  # not admin
        # …and remove non-admins, but not admins
        self.assertEqual(self.j(self.cm, f"/api/boards/{self.board.pk}/members/{self.new.pk}/", {"remove": True}).status_code, 200)
        self.assertEqual(self.j(self.cm, f"/api/boards/{self.board.pk}/members/{self.admin.pk}/", {"remove": True}).status_code, 403)
        self.assertEqual(self.j(self.co, f"/api/boards/{self.board.pk}/share/", {"email": "t2n@x.com"}).status_code, 403)

    def test_leave_board_and_card_front_flags(self):
        self.card.is_complete = True; self.card.cover_colour = "red"; self.card.save()
        page = self.ca.get(f"/b/{self.board.pk}/")
        self.assertContains(page, 'tile-check on shown')
        self.assertContains(page, "tile-cover colour lc-red")
        self.j(self.ca, f"/api/boards/{self.board.pk}/update/", {"show_complete": False, "covers_enabled": False})
        page = self.ca.get(f"/b/{self.board.pk}/")
        self.assertNotContains(page, 'tile-check on shown')
        self.assertNotContains(page, "tile-cover colour lc-red")
        self.j(self.ca, f"/api/boards/{self.board.pk}/update/", {"covers_enabled": True})
        self.j(self.ca, f"/api/cards/{self.card.pk}/update/", {"cover_full": True})
        self.assertContains(self.ca.get(f"/b/{self.board.pk}/"), "cover-full-colour lc-red")
        self.assertContains(page, "data-leave-board")
        self.assertEqual(self.j(self.co, f"/api/boards/{self.board.pk}/members/{self.obs.pk}/", {"remove": True}).status_code, 200)
        self.assertFalse(BoardMember.objects.filter(board=self.board, user=self.obs).exists())

    def test_prefs_recent_and_comments_tab(self):
        r = self.ca.post("/account/theme/", {"colourblind": "1", "only_unread": "1"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.colourblind and self.admin.only_unread)
        self.assertContains(self.ca.get("/"), 'class="cb"')
        Notification.objects.create(recipient=self.admin, kind="assigned", text="old", read_at=timezone.now())
        Notification.objects.create(recipient=self.admin, kind="assigned", text="fresh")
        html = self.ca.get("/api/notifications/?html=1").json()["html"]
        self.assertIn("fresh", html); self.assertNotIn("old", html)
        other = Board.objects.create(workspace=self.ws, title="Wholesale")
        BoardMember.objects.create(board=other, user=self.admin, role="admin")
        self.ca.get(f"/b/{self.board.pk}/"); self.ca.get(f"/b/{other.pk}/")
        titles = [b["title"] for b in self.ca.get("/api/boards/recent/").json()["boards"]]
        self.assertEqual(titles[:2], ["Wholesale", "Orders"])
        from .models import Comment
        Comment.objects.create(card=self.card, author=self.mem, body="Needs more starter")
        self.assertContains(self.ca.get(f"/b/{self.board.pk}/"), "Needs more starter")

    def test_copy_card_to_other_board(self):
        other = Board.objects.create(workspace=self.ws, title="Wholesale")
        BoardMember.objects.create(board=other, user=self.admin, role="admin")
        ol = List.objects.create(board=other, title="Inbox")
        r = self.j(self.ca, f"/api/cards/{self.card.pk}/copy/", {"list_id": ol.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(ol.cards.filter(title="Sourdough").exists())
        hidden = Board.objects.create(workspace=self.ws, title="Secret", visibility="private")
        hl = List.objects.create(board=hidden, title="X")
        self.assertEqual(self.j(self.ca, f"/api/cards/{self.card.pk}/copy/", {"list_id": hl.pk}).status_code, 404)


class ScheduleAndActionTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery S")
        self.u = User.objects.create_user("s3@x.com", "pw")
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.a = List.objects.create(board=self.board, title="Backlog", position=1)
        self.b = List.objects.create(board=self.board, title="Doing", position=2)

    def test_nth_weekday_and_yearly(self):
        from datetime import datetime
        from django.utils import timezone
        from . import automation
        from .models import Schedule
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime(2026, 10, 21, 12), tz)
        third_wed = Schedule(board=self.board, frequency="month_nth", nth=3, weekday=2, hour=8)
        self.assertEqual(automation.next_run(third_wed, start).date().isoformat(), "2026-11-18")
        last_fri = Schedule(board=self.board, frequency="month_nth", nth=5, weekday=4, hour=8)
        self.assertEqual(automation.next_run(last_fri, start).date().isoformat(), "2026-10-30")
        vday = Schedule(board=self.board, frequency="yearly", month=2, month_day=14, hour=7)
        self.assertEqual(automation.next_run(vday, start).date().isoformat(), "2027-02-14")
        self.assertEqual(automation.describe_schedule(third_wed), "Every third Wednesday of the month at 08:00")

    def test_new_board_actions(self):
        from . import automation
        from .models import CardFieldValue, CustomField
        prio = CustomField.objects.create(board=self.board, name="Priority", kind="number")
        for i, (t, p) in enumerate([("Low", 1), ("High", 9), ("Mid", 5), ("None", None)]):
            c = Card.objects.create(list=self.a, title=t, position=(i + 1) * 1000)
            if p is not None:
                CardFieldValue.objects.create(card=c, field=prio, value=p)
        automation.run_board_actions(self.board, [{"type": "sort_field", "list": self.a.pk, "field": prio.pk,
                                                   "order": "desc"}], self.u)
        self.assertEqual([c.title for c in self.a.cards.order_by("position")], ["High", "Mid", "Low", "None"])
        done = automation.run_board_actions(self.board, [{"type": "move_random", "from_list": self.a.pk,
                                                          "list": self.b.pk, "count": 2}], self.u)
        self.assertEqual(self.b.cards.count(), 2)
        self.assertIn("moved 2 random cards", done[0])
        automation.run_board_actions(self.board, [{"type": "archive_list", "list": self.a.pk},
                                                  {"type": "create_list", "text": "Valentine's ideas"}], self.u)
        self.assertEqual(self.a.cards.filter(is_archived=False).count(), 0)
        self.assertTrue(self.board.lists.filter(title="Valentine's ideas").exists())

    def test_overdue_after_offset(self):
        from datetime import timedelta
        from django.utils import timezone
        from . import automation
        from .models import Rule
        rule = Rule.objects.create(board=self.board, trigger="card_overdue", trigger_hours=72, created_by=self.u,
                                   actions=[{"type": "comment", "text": "late"}])
        Rule.objects.filter(pk=rule.pk).update(created_at=timezone.now() - timedelta(days=10))
        c1 = Card.objects.create(list=self.a, title="2 days late", due_at=timezone.now() - timedelta(days=2))
        c2 = Card.objects.create(list=self.a, title="4 days late", due_at=timezone.now() - timedelta(days=4))
        automation.run_due_rules()
        self.assertFalse(c1.comments.exists())
        self.assertTrue(c2.comments.filter(body="late").exists())
        rule.refresh_from_db()
        self.assertEqual(automation.describe_rule(rule)[0], "When a card is 3 days overdue")


class TrelloTouches3Tests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery T3")
        self.u = User.objects.create_user("t3@x.com", "pw", first_name="Tia")
        self.w = User.objects.create_user("t3w@x.com", "pw", first_name="Wes")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        BoardMember.objects.create(board=self.board, user=self.w, role="member")
        self.lst = List.objects.create(board=self.board, title="To do")
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, data):
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def test_recurring_card_makes_next_one(self):
        from datetime import timedelta
        from .models import Checklist, ChecklistItem
        due = timezone.now() + timedelta(days=1)
        card = Card.objects.create(list=self.lst, title="Weekly deep clean", due_at=due)
        cl = Checklist.objects.create(card=card, title="Steps")
        ChecklistItem.objects.create(checklist=cl, text="Ovens", is_done=True)
        self.j(f"/api/cards/{card.pk}/update/", {"recur": "weekly", "reminder_minutes": 60})
        r = self.j(f"/api/cards/{card.pk}/update/", {"is_complete": True}).json()
        self.assertIn("next_card", r)
        nxt = Card.objects.get(pk=r["next_card"]["id"])
        self.assertEqual((nxt.due_at - due).days, 7)
        self.assertFalse(nxt.is_complete)
        self.assertEqual((nxt.recur, nxt.reminder_minutes), ("weekly", 60))
        self.assertFalse(nxt.checklists.get().items.get().is_done)
        from .recurring import next_due
        from datetime import datetime
        jan31 = timezone.make_aware(datetime(2027, 1, 31, 17), timezone.get_current_timezone())
        self.assertEqual(next_due(jan31, "monthly").date().isoformat(), "2027-02-28")

    def test_reminder_timing_and_watchers(self):
        from datetime import timedelta
        from .models import Watch
        from .notify import send_due_reminders
        card = Card.objects.create(list=self.lst, title="Collect order", due_at=timezone.now() + timedelta(hours=3),
                                   reminder_minutes=60)
        card.members.add(self.u)
        Watch.objects.create(user=self.w, card=card)
        self.assertEqual(send_due_reminders(), 0)            # 1 hour before — not yet
        Card.objects.filter(pk=card.pk).update(due_at=timezone.now() + timedelta(minutes=30))
        self.assertEqual(send_due_reminders(), 2)            # member + watcher
        Card.objects.filter(pk=card.pk).update(due_at=timezone.now() + timedelta(minutes=20), reminder_minutes=-1)
        self.assertEqual(send_due_reminders(), 0)            # "None"

    def test_link_attachment_copy_checklist_pin_and_suggested_field(self):
        from .models import Attachment, Checklist, ChecklistItem
        card = Card.objects.create(list=self.lst, title="Wedding cake")
        r = self.j(f"/api/cards/{card.pk}/links/", {"url": "https://example.com/brief", "name": "Brief"})
        self.assertEqual(r.status_code, 200, r.content)
        att = Attachment.objects.get(card=card)
        self.assertEqual(self.c.get(f"/files/{att.pk}/")["Location"], "https://example.com/brief")
        self.assertEqual(self.j(f"/api/cards/{card.pk}/links/", {"url": "javascript:alert(1)"}).status_code, 400)
        other = Card.objects.create(list=self.lst, title="Tiered cake")
        src = Checklist.objects.create(card=other, title="Bake")
        for i, t in enumerate(["Sponges", "Buttercream"]):
            ChecklistItem.objects.create(checklist=src, text=t, position=i)
        self.assertContains(self.c.get(f"/cards/{card.pk}/"), "Copy items from")
        self.j(f"/api/cards/{card.pk}/checklists/", {"title": "Bake", "copy_from": src.pk})
        self.assertEqual([i.text for i in card.checklists.get().items.order_by("position")], ["Sponges", "Buttercream"])
        self.j(f"/api/lists/{self.lst.pk}/", {"is_pinned": True})
        self.assertContains(self.c.get(f"/b/{self.board.pk}/"), "list pinned")
        self.j(f"/api/boards/{self.board.pk}/fields/", {"name": "Priority", "kind": "dropdown",
                                                         "options": "Highest,High,Medium,Low,Lowest"})
        self.assertEqual(self.board.custom_fields.get().options[0], "Highest")
        self.assertContains(self.c.get(f"/b/{self.board.pk}/table/"), "data-view-add-form")
        tl = self.c.get(f"/b/{self.board.pk}/timeline/")
        self.assertContains(tl, "data-tl-card-form")
        for z in ("day", "week", "month", "quarter"):
            self.assertEqual(self.c.get(f"/b/{self.board.pk}/timeline/?z={z}&g=member").status_code, 200)
        self.assertEqual(self.c.get("/all/timeline/").status_code, 200)


class MoveCardPositionTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery M")
        self.u = User.objects.create_user("mv@x.com", "pw")
        self.board = Board.objects.create(workspace=self.ws, title="Orders")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.a = List.objects.create(board=self.board, title="A", position=1)
        self.b = List.objects.create(board=self.board, title="B", position=2)
        self.cards = [Card.objects.create(list=self.b, title=f"b{i}", position=(i + 1) * 1000) for i in range(3)]
        self.card = Card.objects.create(list=self.a, title="mover")
        self.c = Client(); self.c.force_login(self.u)

    def order(self, lst):
        return [c.title for c in lst.cards.order_by("position")]

    def test_move_to_position(self):
        mv = lambda pos, lst: self.c.post(f"/api/cards/{self.card.pk}/move/", json.dumps({"list_id": lst.pk, "position": pos}),
                                          content_type="application/json")
        self.assertEqual(mv(2, self.b).status_code, 200)
        self.assertEqual(self.order(self.b), ["b0", "mover", "b1", "b2"])
        mv(1, self.b); self.assertEqual(self.order(self.b), ["mover", "b0", "b1", "b2"])
        mv(4, self.b); self.assertEqual(self.order(self.b), ["b0", "b1", "b2", "mover"])
        mv(99, self.a); self.assertEqual(self.order(self.a), ["mover"])
        html = self.c.get(f"/cards/{self.card.pk}/").content.decode()
        self.assertIn("data-move-tree", html)
        self.assertIn('data-tile-complete', self.c.get(f"/b/{self.board.pk}/").content.decode())


class CalendarTrelloTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Bakery Cal")
        self.u = User.objects.create_user("cal@x.com", "pw")
        self.other = User.objects.create_user("cal2@x.com", "pw")
        self.board = Board.objects.create(workspace=self.ws, title="Orders", visibility="private")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.a = List.objects.create(board=self.board, title="A", position=1000)
        self.b = List.objects.create(board=self.board, title="B", position=2000)
        self.card = Card.objects.create(list=self.a, title="Bake, ice; box", due_at=timezone.now())
        self.c = Client(); self.c.force_login(self.u)

    def test_week_view_month_picker_and_feed(self):
        r = self.c.get(f"/b/{self.board.pk}/calendar/?v=week")
        import re
        self.assertEqual(len(re.findall(r'class="cal-day[ "]', r.content.decode())), 7)
        self.assertContains(r, "Bake, ice; box")
        self.assertContains(r, "data-cal-month")
        self.u.refresh_from_db()
        ics = self.c.get(f"/ical/{self.u.ical_token}/b{self.board.pk}.ics")
        self.assertEqual(ics.status_code, 200)
        self.assertIn("SUMMARY:Bake\\, ice\; box", ics.content.decode())
        anon = Client()
        self.assertEqual(anon.get(f"/ical/{self.u.ical_token}/all.ics").status_code, 200)
        self.assertEqual(anon.get(f"/ical/nope/all.ics").status_code, 404)
        self.other.ical_token = "othertoken"; self.other.save()
        self.assertEqual(anon.get(f"/ical/othertoken/b{self.board.pk}.ics").status_code, 404)   # can't see private board

    def test_add_list_at_position(self):
        self.c.post(f"/api/boards/{self.board.pk}/lists/", json.dumps({"title": "First", "position": 1}),
                    content_type="application/json")
        self.c.post(f"/api/boards/{self.board.pk}/lists/", json.dumps({"title": "Middle", "position": 3}),
                    content_type="application/json")
        self.assertEqual([l.title for l in self.board.lists.order_by("position")], ["First", "A", "Middle", "B"])


class ItemReminderTests(TestCase):
    def test_item_assign_due_and_reminder(self):
        from datetime import timedelta
        from .models import Checklist, ChecklistItem
        from .notify import send_due_reminders
        ws = Workspace.objects.create(name="Bakery IR")
        boss = User.objects.create_user("ir@x.com", "pw"); baker = User.objects.create_user("ir2@x.com", "pw")
        b = Board.objects.create(workspace=ws, title="B")
        for u in (boss, baker):
            BoardMember.objects.create(board=b, user=u, role="admin")
        card = Card.objects.create(list=List.objects.create(board=b, title="L"), title="Sourdough")
        item = ChecklistItem.objects.create(checklist=Checklist.objects.create(card=card, title="Steps"), text="Shape")
        c = Client(); c.force_login(boss)
        due = (timezone.localtime() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        r = c.post(f"/api/items/{item.pk}/", json.dumps({"assignee_id": baker.pk, "due_at": due, "reminder_minutes": 60}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        item.refresh_from_db()
        self.assertEqual((item.assignee, item.reminder_minutes), (baker, 60))
        self.assertTrue(Notification.objects.filter(recipient=baker, kind="item_assigned").exists())
        self.assertEqual(send_due_reminders(), 0)                       # 3h away, reminder 1h before
        ChecklistItem.objects.filter(pk=item.pk).update(due_at=timezone.now() + timedelta(minutes=40))
        self.assertEqual(send_due_reminders(), 1)
        self.assertContains(c.get(f"/cards/{card.pk}/"), "Set due date reminder")


class DockPlannerTests(TestCase):
    def test_planner_panel_and_dock(self):
        from datetime import timedelta
        ws = Workspace.objects.create(name="Dock")
        u = User.objects.create_user("dock@x.com", "pw")
        b = Board.objects.create(workspace=ws, title="B")
        BoardMember.objects.create(board=b, user=u, role="admin")
        c = Card.objects.create(list=List.objects.create(board=b, title="L"), title="Bake rye", due_at=timezone.now() + timedelta(hours=1))
        c.members.add(u)
        cl = Client(); cl.force_login(u)
        page = cl.get(f"/b/{b.pk}/")
        self.assertContains(page, "data-toggle-planner")
        self.assertContains(page, "data-dock-switch")
        html = cl.get("/api/planner/panel/").json()["html"]
        self.assertIn("Bake rye", html)
        self.assertIn("Today", html)


class SimFindingsTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="SimFix")
        self.u = User.objects.create_user("sf@x.com", "pw", first_name="Sue")
        self.v = User.objects.create_user("sf2@x.com", "pw", first_name="Vic")
        self.b = Board.objects.create(workspace=self.ws, title="B")
        for u in (self.u, self.v):
            BoardMember.objects.create(board=self.b, user=u, role="admin")
        self.card = Card.objects.create(list=List.objects.create(board=self.b, title="L"), title="Scones", description="v1")
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, data):
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def test_description_conflict(self):
        Card.objects.filter(pk=self.card.pk).update(description="someone else's v2")
        r = self.j(f"/api/cards/{self.card.pk}/update/", {"description": "my v2", "description_base": "v1"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["current"], "someone else's v2")
        self.card.refresh_from_db(); self.assertEqual(self.card.description, "someone else's v2")
        r = self.j(f"/api/cards/{self.card.pk}/update/", {"description": "my v2"})          # chose "Replace with mine"
        self.card.refresh_from_db(); self.assertEqual(self.card.description, "my v2")
        r = self.j(f"/api/cards/{self.card.pk}/update/", {"description": "my v3", "description_base": "my v2"})
        self.assertEqual(r.status_code, 200)

    def test_history_records_items_labels_and_renames(self):
        from .models import Activity, Checklist, Label
        lab = Label.objects.create(board=self.b, name="Urgent", colour="red")
        cl = Checklist.objects.create(card=self.card, title="Steps")
        self.j(f"/api/cards/{self.card.pk}/labels/", {"label_id": lab.pk})
        self.j(f"/api/checklists/{cl.pk}/items/", {"text": "Weigh up"})
        item = cl.items.get()
        self.j(f"/api/items/{item.pk}/", {"assignee_id": self.v.pk, "due_at": "2026-11-02T09:00"})
        self.j(f"/api/items/{item.pk}/", {"delete": True})
        self.j(f"/api/cards/{self.card.pk}/update/", {"title": "Scones x60"})
        texts = [a.text() for a in Activity.objects.filter(card=self.card)]
        for want in ["added the Urgent label", "added “Weigh up” to Steps", "assigned “Weigh up” to Vic",
                     "set “Weigh up” due 2 Nov, 09:00", "deleted “Weigh up” from Steps",
                     "renamed this card from “Scones” to “Scones x60”"]:
            self.assertIn(want, texts)

    def test_just_now(self):
        from .templatetags.lanes_extras import ago
        from datetime import timedelta
        self.assertEqual(ago(timezone.now()), "just now")
        self.assertTrue(ago(timezone.now() - timedelta(minutes=5)).endswith("ago"))


class CrossDepartmentTests(TestCase):
    """HR (Hana) gives work to Bakery (Marco) — the three ways."""
    def setUp(self):
        self.hr = Workspace.objects.create(name="HR x"); self.bakery = Workspace.objects.create(name="Bakery x")
        self.hana = User.objects.create_user("hana@x.com", "pw", first_name="Hana")
        self.marco = User.objects.create_user("marco@x.com", "pw", first_name="Marco")
        WorkspaceMember.objects.create(workspace=self.hr, user=self.hana, role="admin")
        WorkspaceMember.objects.create(workspace=self.bakery, user=self.marco, role="admin")
        self.hrb = Board.objects.create(workspace=self.hr, title="Starters", visibility="private")
        BoardMember.objects.create(board=self.hrb, user=self.hana, role="admin")
        self.hl = List.objects.create(board=self.hrb, title="Induction")
        self.bb = Board.objects.create(workspace=self.bakery, title="Production")
        BoardMember.objects.create(board=self.bb, user=self.marco, role="admin")
        BoardMember.objects.create(board=self.bb, user=self.hana, role="member")   # Hana can edit Bakery's board
        self.bl = List.objects.create(board=self.bb, title="To do")
        self.card = Card.objects.create(list=self.hl, title="Gemma induction")
        self.h = Client(); self.h.force_login(self.hana)
        self.m = Client(); self.m.force_login(self.marco)

    def j(self, c, url, data):
        return c.post(url, json.dumps(data), content_type="application/json")

    def test_1_share_then_assign(self):
        self.assertEqual(self.m.get(f"/b/{self.hrb.pk}/").status_code, 404)                       # private: invisible
        self.assertEqual(self.j(self.h, f"/api/cards/{self.card.pk}/members/", {"user_id": self.marco.pk}).status_code, 400)
        self.assertEqual(self.j(self.h, f"/api/boards/{self.hrb.pk}/share/", {"email": "marco@x.com", "role": "member"}).status_code, 200)
        self.assertEqual(self.j(self.h, f"/api/cards/{self.card.pk}/members/", {"user_id": self.marco.pk}).status_code, 200)
        self.assertTrue(Notification.objects.filter(recipient=self.marco, kind="assigned").exists())
        self.assertEqual(self.m.get(f"/b/{self.hrb.pk}/").status_code, 200)
        self.assertContains(self.m.get("/my-cards/"), "Gemma induction")
        # checklist step assignment too
        from .models import Checklist, ChecklistItem
        it = ChecklistItem.objects.create(checklist=Checklist.objects.create(card=self.card, title="Steps"), text="H&S")
        self.assertEqual(self.j(self.h, f"/api/items/{it.pk}/", {"assignee_id": self.marco.pk}).status_code, 200)
        self.assertTrue(Notification.objects.filter(recipient=self.marco, kind="item_assigned").exists())

    def test_2_mirror_to_their_board(self):
        r = self.j(self.h, f"/api/cards/{self.card.pk}/mirrors/", {"list_id": self.bl.pk})
        self.assertEqual(r.status_code, 200, r.content)
        page = self.m.get(f"/b/{self.bb.pk}/")
        self.assertContains(page, "Gemma induction")                                              # shows on Bakery board
        self.assertEqual(self.m.get(f"/b/{self.hrb.pk}/").status_code, 404)                       # HR board still hidden
        self.assertEqual(self.m.get(f"/cards/{self.card.pk}/").status_code, 200)                  # Marco can open the card

    def test_3_move_to_their_board(self):
        r = self.j(self.h, f"/api/cards/{self.card.pk}/move/", {"list_id": self.bl.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.bl)
        self.assertContains(self.m.get(f"/b/{self.bb.pk}/"), "Gemma induction")

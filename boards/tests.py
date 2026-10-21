import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
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
        self.assertEqual(r.status_code, 404)

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

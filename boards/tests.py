import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import positions
from .models import Activity, Board, BoardMember, Card, Label, List, Workspace, WorkspaceMember

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

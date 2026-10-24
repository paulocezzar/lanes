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
        self.assertEqual(self.client.get(reverse("board", args=[self.board.pk])).status_code, 403)   # private: “Ask to join” page, board not shown
        self.board.visibility = Board.WORKSPACE
        self.board.save()
        self.assertEqual(self.client.get(reverse("board", args=[self.board.pk])).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("board", args=[self.board.pk])).status_code, 403)   # another department: can ask to join

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
            self.assertEqual(self.client.get(reverse(name, args=[self.board.pk])).status_code, 403)   # private: “Ask to join” page, board not shown


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
        self.u = User.objects.create_user("e@x.com", "pw-12345", first_name="Eve", email_notifications="instant")
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

    def test_recurring_tick_untick_makes_no_duplicates(self):
        from datetime import timedelta
        card = Card.objects.create(list=self.lst, title="Daily temp log", due_at=timezone.now() + timedelta(hours=2), recur="daily")
        url = f"/api/cards/{card.pk}/update/"
        self.assertIn("next_card", self.j(url, {"is_complete": True}).json())
        self.assertEqual(Card.objects.filter(title="Daily temp log").count(), 2)
        self.j(url, {"is_complete": False})                       # mis-click undone: the copy goes too
        self.assertEqual(Card.objects.filter(title="Daily temp log").count(), 1)
        self.j(url, {"is_complete": True}); self.j(url, {"is_complete": False}); self.j(url, {"is_complete": True})
        self.assertEqual(Card.objects.filter(title="Daily temp log").count(), 2)
        # a copy someone has already worked on is kept
        nxt = Card.objects.exclude(pk=card.pk).get(title="Daily temp log")
        nxt.comments.create(body="started", author=self.user if hasattr(self, "user") else None)
        self.j(url, {"is_complete": False})
        self.assertTrue(Card.objects.filter(pk=nxt.pk).exists())
        self.j(url, {"is_complete": True})                        # and no second copy appears
        self.assertEqual(Card.objects.filter(title="Daily temp log").count(), 2)

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
        self.assertIn(r"SUMMARY:Bake\, ice\; box", ics.content.decode())
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
        self.assertEqual(self.m.get(f"/b/{self.hrb.pk}/").status_code, 403)   # private: “Ask to join” page, board not shown                       # private: invisible
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
        self.assertEqual(self.m.get(f"/b/{self.hrb.pk}/").status_code, 403)   # private: “Ask to join” page, board not shown                       # HR board still hidden
        self.assertEqual(self.m.get(f"/cards/{self.card.pk}/").status_code, 200)                  # Marco can open the card

    def test_3_move_to_their_board(self):
        r = self.j(self.h, f"/api/cards/{self.card.pk}/move/", {"list_id": self.bl.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.bl)
        self.assertContains(self.m.get(f"/b/{self.bb.pk}/"), "Gemma induction")


class FullSweepTests(TestCase):
    """Every feature that had no test yet — one pass over the whole app."""

    def setUp(self):
        from .models import Checklist, ChecklistItem, Comment, CustomField
        self.ws = Workspace.objects.create(name="Sweep")
        self.admin = User.objects.create_user("sa@x.com", "pw", first_name="Sal", is_superuser=True, is_staff=True)
        self.mem = User.objects.create_user("sm@x.com", "pw", first_name="Mia")
        self.obs = User.objects.create_user("so@x.com", "pw", first_name="Oz")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.admin, role="admin")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.mem)
        WorkspaceMember.objects.create(workspace=self.ws, user=self.obs)
        self.board = Board.objects.create(workspace=self.ws, title="Sweep board", visibility="workspace")
        BoardMember.objects.create(board=self.board, user=self.admin, role="admin")
        BoardMember.objects.create(board=self.board, user=self.mem, role="member")
        BoardMember.objects.create(board=self.board, user=self.obs, role="observer")
        self.a = List.objects.create(board=self.board, title="A", position=1000)
        self.b = List.objects.create(board=self.board, title="B", position=2000)
        self.card = Card.objects.create(list=self.a, title="Bake bread", position=1000)
        self.card.members.add(self.mem)
        self.cl = Checklist.objects.create(card=self.card, title="Steps")
        self.item = ChecklistItem.objects.create(checklist=self.cl, text="Knead")
        self.comment = Comment.objects.create(card=self.card, author=self.mem, body="first")
        self.label = Label.objects.create(board=self.board, name="Urgent", colour="red")
        self.field = CustomField.objects.create(board=self.board, name="Batch", kind="number")
        self.c = Client(); self.c.force_login(self.admin)
        self.m = Client(); self.m.force_login(self.mem)
        self.o = Client(); self.o.force_login(self.obs)

    def j(self, client, url, data=None):
        return client.post(url, json.dumps(data or {}), content_type="application/json")

    # ---- comments, checklists, labels, fields ----
    def test_comment_edit_and_delete(self):
        r = self.j(self.m, f"/api/comments/{self.comment.pk}/", {"body": "edited"})
        self.assertEqual(r.status_code, 200, r.content)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "edited")
        self.assertIsNotNone(self.comment.edited_at)
        self.assertGreaterEqual(self.j(self.o, f"/api/comments/{self.comment.pk}/", {"body": "x"}).status_code, 400)
        self.assertEqual(self.j(self.m, f"/api/comments/{self.comment.pk}/", {"delete": True}).status_code, 200)
        self.assertFalse(type(self.comment).objects.filter(pk=self.comment.pk).exists())

    def test_comment_react(self):
        r = self.j(self.m, f"/api/comments/{self.comment.pk}/react/", {"emoji": "👍"})
        self.assertLess(r.status_code, 500, r.content)

    def test_checklist_rename_hide_delete(self):
        self.assertEqual(self.j(self.m, f"/api/checklists/{self.cl.pk}/", {"title": "Method", "hide_checked": True}).status_code, 200)
        self.cl.refresh_from_db()
        self.assertEqual((self.cl.title, self.cl.hide_checked), ("Method", True))
        self.assertEqual(self.j(self.o, f"/api/checklists/{self.cl.pk}/", {"title": "no"}).status_code, 403)
        self.assertEqual(self.j(self.m, f"/api/checklists/{self.cl.pk}/", {"delete": True}).status_code, 200)
        self.assertFalse(type(self.cl).objects.filter(pk=self.cl.pk).exists())

    def test_label_create_rename_attach_delete(self):
        url = f"/api/boards/{self.board.pk}/labels/"
        r = self.j(self.m, url, {"name": "Gluten free", "colour": "green", "card_id": self.card.pk})
        self.assertEqual(r.status_code, 200, r.content)
        lab = Label.objects.get(name="Gluten free")
        self.assertIn(lab, self.card.labels.all())
        self.assertEqual(self.j(self.m, url, {"label_id": lab.pk, "name": "GF", "colour": "blue"}).status_code, 200)
        lab.refresh_from_db()
        self.assertEqual((lab.name, lab.colour), ("GF", "blue"))
        self.assertEqual(self.j(self.o, url, {"name": "nope"}).status_code, 403)
        self.assertEqual(self.j(self.m, url, {"label_id": lab.pk, "delete": True}).status_code, 200)
        self.assertFalse(Label.objects.filter(pk=lab.pk).exists())

    def test_custom_fields_all_kinds_and_values(self):
        from .models import CustomField
        url = f"/api/boards/{self.board.pk}/fields/"
        r = self.j(self.c, url, {"name": "Oven", "kind": "dropdown", "options": ["Deck", "Rack"], "show_on_card": True})
        self.assertEqual(r.status_code, 200, r.content)
        dd = CustomField.objects.get(name="Oven")
        self.assertEqual(dd.kind, "dropdown")
        for kind in ("text", "date", "checkbox"):
            self.assertEqual(self.j(self.c, url, {"name": kind.title(), "kind": kind}).status_code, 200)
        set_url = f"/api/cards/{self.card.pk}/fields/"
        self.assertEqual(self.j(self.m, set_url, {"field_id": self.field.pk, "value": "12"}).status_code, 200)
        self.assertGreaterEqual(self.j(self.m, set_url, {"field_id": self.field.pk, "value": "twelve"}).status_code, 400)
        self.assertEqual(self.j(self.c, url, {"field_id": dd.pk, "name": "Oven type"}).status_code, 200)
        self.assertEqual(self.j(self.c, url, {"field_id": dd.pk, "delete": True}).status_code, 200)
        self.assertFalse(CustomField.objects.filter(pk=dd.pk).exists())

    # ---- lists & boards ----
    def test_list_move_star_join_watch(self):
        r = self.j(self.m, f"/api/lists/{self.b.pk}/move/", {"before_id": None, "after_id": self.a.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(list(self.board.lists.order_by("position").values_list("title", flat=True)), ["B", "A"])
        self.assertEqual(self.j(self.m, f"/api/boards/{self.board.pk}/star/").status_code, 200)
        self.assertEqual(self.j(self.m, f"/api/boards/{self.board.pk}/watch/").status_code, 200)
        self.assertEqual(self.j(self.m, f"/api/cards/{self.card.pk}/watch/").status_code, 200)
        self.assertTrue(Watch.objects.filter(user=self.mem).exists())
        joiner = User.objects.create_user("sj@x.com", "pw", first_name="Jo")
        WorkspaceMember.objects.create(workspace=self.ws, user=joiner)
        cj = Client(); cj.force_login(joiner)
        self.assertEqual(self.j(cj, f"/api/boards/{self.board.pk}/join/").status_code, 200)
        self.assertTrue(BoardMember.objects.filter(board=self.board, user=joiner).exists())

    def test_list_copy_move_cards_sort_archive_move_board(self):
        Card.objects.create(list=self.a, title="Apple", position=2000)
        self.assertEqual(self.j(self.m, f"/api/lists/{self.a.pk}/sort/", {"by": "name"}).status_code, 200)
        self.assertEqual(self.j(self.m, f"/api/lists/{self.a.pk}/copy/", {"title": "A copy"}).status_code, 200)
        copy = List.objects.get(title="A copy")
        self.assertEqual(copy.cards.count(), 2)
        self.assertEqual(self.j(self.m, f"/api/lists/{self.a.pk}/move-cards/", {"list_id": self.b.pk}).status_code, 200)
        self.assertEqual(self.b.cards.count(), 2)
        self.assertEqual(self.j(self.m, f"/api/lists/{self.b.pk}/archive-cards/").status_code, 200)
        self.assertFalse(self.b.cards.filter(is_archived=False).exists())
        other = Board.objects.create(workspace=self.ws, title="Other")
        BoardMember.objects.create(board=other, user=self.admin, role="admin")
        self.assertEqual(self.j(self.c, f"/api/lists/{copy.pk}/move-board/", {"board_id": other.pk}).status_code, 200)
        copy.refresh_from_db()
        self.assertTrue(copy.is_archived)
        self.assertEqual(Card.objects.filter(list__board=other).count(), 2)

    def test_board_copy_and_restore(self):
        r = self.c.post(f"/b/{self.board.pk}/copy/", {"title": "Sweep copy", "keep_cards": "on"})
        self.assertEqual(r.status_code, 302)
        cp = Board.objects.get(title="Sweep copy")
        self.assertEqual(cp.lists.count(), 2)
        self.assertTrue(Card.objects.filter(list__board=cp, title="Bake bread").exists())
        self.board.is_archived = True; self.board.save()
        self.assertEqual(self.c.post(f"/boards/{self.board.pk}/restore/").status_code, 302)
        self.board.refresh_from_db()
        self.assertFalse(self.board.is_archived)

    def test_exports(self):
        self.assertContains(self.c.get(f"/b/{self.board.pk}/export.csv"), "Bake bread")
        self.assertContains(self.c.get(f"/b/{self.board.pk}/export.json"), "Bake bread")
        self.assertEqual(self.c.get("/export/all.json").status_code, 200)

    # ---- cards ----
    def test_card_update_every_field(self):
        url = f"/api/cards/{self.card.pk}/update/"
        for data in ({"title": "Sourdough"}, {"description": "**hi**"}, {"due_at": "2030-01-02T10:00"},
                     {"start_date": "2030-01-01"}, {"recur": "weekly"}, {"reminder_minutes": 60},
                     {"is_template": True}, {"cover_colour": "green"}, {"cover_full": True},
                     {"sticker": "⭐"}, {"is_complete": True}, {"is_complete": False}):
            r = self.j(self.m, url, data)
            self.assertEqual(r.status_code, 200, (data, r.content))
        self.card.refresh_from_db()
        self.assertEqual(self.card.title, "Sourdough")
        self.assertEqual(self.card.recur, "weekly")
        self.assertTrue(self.card.is_template)
        self.assertEqual(self.j(self.o, url, {"title": "x"}).status_code, 403)
        self.assertEqual(self.j(self.m, url, {"archive": True}).status_code, 200)
        self.card.refresh_from_db()
        self.assertTrue(self.card.is_archived)

    def test_card_copy_vote_delete_template(self):
        self.assertEqual(self.j(self.m, f"/api/cards/{self.card.pk}/copy/", {"list_id": self.b.pk, "title": "Copy"}).status_code, 200)
        self.assertTrue(self.b.cards.filter(title="Copy").exists())
        self.assertLess(self.j(self.m, f"/api/cards/{self.card.pk}/vote/").status_code, 500)
        self.card.is_template = True; self.card.save()
        r = self.j(self.m, f"/api/lists/{self.b.pk}/from-template/", {"card_id": self.card.pk, "title": "From tpl"})
        self.assertLess(r.status_code, 500, r.content)
        self.assertEqual(self.j(self.o, f"/api/cards/{self.card.pk}/delete/").status_code, 403)
        self.assertEqual(self.j(self.c, f"/api/cards/{self.card.pk}/delete/").status_code, 400)   # archive first
        self.card.is_archived = True; self.card.save()
        self.assertEqual(self.j(self.c, f"/api/cards/{self.card.pk}/delete/").status_code, 200)
        self.assertFalse(Card.objects.filter(pk=self.card.pk).exists())

    def test_card_modal_and_board_views_render(self):
        for url in (f"/cards/{self.card.pk}/", f"/b/{self.board.pk}/", f"/b/{self.board.pk}/calendar/",
                    f"/b/{self.board.pk}/table/", f"/b/{self.board.pk}/timeline/", f"/b/{self.board.pk}/dashboard/",
                    f"/b/{self.board.pk}/map/", f"/b/{self.board.pk}/automation/", "/all/calendar/", "/all/table/",
                    "/all/timeline/", "/all/dashboard/", "/all/map/", "/my-cards/", "/planner/", "/notifications/",
                    "/search/?q=bread", "/", f"/api/boards/{self.board.pk}/state/", "/api/boards/mine/",
                    "/api/boards/recent/", "/api/inbox/", "/api/planner/panel/", "/api/notifications/",
                    "/company/", "/company/audit/", "/company/people/", "/company/settings/",
                    f"/departments/{self.ws.pk}/people/", "/account/two-step/", "/account/email/", "/password/"):
            r = self.c.get(url)
            self.assertIn(r.status_code, (200, 302), (url, r.status_code))

    def test_attachments_upload_rename_cover_download_delete(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import Attachment
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
               b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
        r = self.m.post(f"/api/cards/{self.card.pk}/attachments/", {"file": SimpleUploadedFile("dot.png", png, "image/png")})
        self.assertEqual(r.status_code, 200, r.content)
        att = Attachment.objects.get(card=self.card)
        self.assertEqual(self.c.get(f"/files/{att.pk}/").status_code, 200)
        self.assertEqual(self.j(self.m, f"/api/attachments/{att.pk}/", {"name": "loaf.png"}).status_code, 200)
        self.assertEqual(self.j(self.m, f"/api/attachments/{att.pk}/", {"cover": True}).status_code, 200)
        self.card.refresh_from_db()
        self.assertEqual(self.card.cover_id, att.pk)
        outsider = User.objects.create_user("sx@x.com", "pw")
        cx = Client(); cx.force_login(outsider)
        self.assertEqual(cx.get(f"/files/{att.pk}/").status_code, 404)
        self.assertEqual(self.j(self.m, f"/api/attachments/{att.pk}/", {"delete": True}).status_code, 200)
        self.assertFalse(Attachment.objects.filter(pk=att.pk).exists())

    # ---- automation ----
    def test_rule_pause_delete_button_delete_schedule_run(self):
        from .models import Button, Rule, Schedule
        rule = Rule.objects.create(board=self.board, trigger="card_completed", actions=[{"type": "move", "list": self.b.pk}])
        self.c.post(f"/b/{self.board.pk}/automation/rules/{rule.pk}/")
        rule.refresh_from_db(); self.assertFalse(rule.is_enabled)
        self.c.post(f"/b/{self.board.pk}/automation/rules/{rule.pk}/", {"action": "delete"})
        self.assertFalse(Rule.objects.filter(pk=rule.pk).exists())
        btn = Button.objects.create(board=self.board, kind="card", name="Go", actions=[{"type": "complete"}])
        self.assertEqual(self.j(self.m, f"/api/cards/{self.card.pk}/buttons/{btn.pk}/").status_code, 200)
        self.card.refresh_from_db(); self.assertTrue(self.card.is_complete)
        self.c.post(f"/b/{self.board.pk}/automation/buttons/{btn.pk}/", {"action": "delete"})
        self.assertFalse(Button.objects.filter(pk=btn.pk).exists())
        s = Schedule.objects.create(board=self.board, kind="actions", frequency="daily",
                                    actions=[{"type": "create_list", "text": "Made by schedule"}], created_by=self.admin,
                                    next_run_at=timezone.now() + timezone.timedelta(days=1))
        self.c.post(f"/b/{self.board.pk}/automation/schedules/{s.pk}/", {"action": "run"})
        self.assertTrue(List.objects.filter(board=self.board, title="Made by schedule").exists())
        self.c.post(f"/b/{self.board.pk}/automation/schedules/{s.pk}/")
        s.refresh_from_db(); self.assertFalse(s.is_enabled)
        self.c.post(f"/b/{self.board.pk}/automation/schedules/{s.pk}/", {"action": "delete"})
        self.assertFalse(Schedule.objects.filter(pk=s.pk).exists())

    def test_rule_create_every_action_via_form(self):
        from .models import Rule
        post = {"trigger": "card_moved", "trigger_list": self.b.pk,
                "action_0": "add_label", "label_0": self.label.pk,
                "action_1": "add_member", "user_1": self.mem.pk,
                "action_2": "add_checklist", "cltitle_2": "QA", "clitems_2": "Taste\nPack",
                "action_3": "comment", "text_3": "Moved!"}
        self.c.post(f"/b/{self.board.pk}/automation/rules/", post)
        rule = Rule.objects.get(board=self.board, trigger="card_moved")
        self.assertEqual([a["type"] for a in rule.actions], ["add_label", "add_member", "add_checklist", "comment"])
        # errors are reported, nothing saved
        n = Rule.objects.count()
        self.c.post(f"/b/{self.board.pk}/automation/rules/", {"trigger": "card_completed", "action_0": "move"})
        self.c.post(f"/b/{self.board.pk}/automation/rules/", {"trigger": "card_completed", "action_0": "comment"})
        self.c.post(f"/b/{self.board.pk}/automation/rules/", {"trigger": "card_completed", "action_0": "add_sticker", "sticker_0": "bad"})
        self.assertEqual(Rule.objects.count(), n)
        for trig, extra in (("card_due_soon", {"trigger_hours": "24"}), ("card_overdue", {"trigger_hours": "3"}),
                            ("label_added", {"trigger_label": self.label.pk}), ("member_added", {"member_who": "me"}),
                            ("checklist_done", {}), ("card_added", {"trigger_list": self.a.pk, "by_whom": "others"})):
            self.c.post(f"/b/{self.board.pk}/automation/rules/", {"trigger": trig, "action_0": "complete", **extra})
            self.assertTrue(Rule.objects.filter(trigger=trig).exists(), trig)
        # members can't edit automation on a board where they're observers
        self.o.post(f"/b/{self.board.pk}/automation/rules/", {"trigger": "card_completed", "action_0": "complete"})
        self.assertEqual(Rule.objects.filter(trigger="card_completed").count(), 0)

    def test_run_action_every_card_action(self):
        from . import automation
        from .models import Checklist
        Checklist.objects.create(card=self.card, title="x").items.create(text="y")
        acts = [{"type": "move", "list": self.b.pk}, {"type": "move_top"}, {"type": "complete"},
                {"type": "incomplete"}, {"type": "check_all"}, {"type": "add_label", "label": self.label.pk},
                {"type": "remove_label", "label": self.label.pk}, {"type": "add_member", "user": self.obs.pk},
                {"type": "clear_members"}, {"type": "set_due", "days": 3}, {"type": "set_due_workdays", "days": 2},
                {"type": "clear_due"}, {"type": "comment", "text": "auto"}, {"type": "notify", "text": "hey"},
                {"type": "add_checklist", "title": "Auto", "text": "one\ntwo"}, {"type": "add_sticker", "sticker": "⭐"},
                {"type": "archive"}]
        out = []
        for a in acts:
            self.card.refresh_from_db()
            out.append(automation.run_action(self.card, a))
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.b)
        self.assertTrue(self.card.is_archived)
        self.assertTrue(self.card.checklists.filter(title="Auto", items__text="two").exists())
        self.assertTrue(self.card.comments.filter(body="auto").exists())
        self.assertGreaterEqual(sum(1 for x in out if x), 12, out)

    def test_run_board_actions_every_kind(self):
        from . import automation
        Card.objects.create(list=self.a, title="Done one", is_complete=True, position=3000)
        Card.objects.create(list=self.a, title="Zed", position=4000, due_at=timezone.now())
        done = automation.run_board_actions(self.board, [
            {"type": "create_card", "list": self.a.pk, "text": "Daily check %d/%m"},
            {"type": "sort_due", "list": self.a.pk},
            {"type": "sort_field", "list": self.a.pk, "field": self.field.pk, "order": "desc"},
            {"type": "archive_done"},
            {"type": "move_random", "from_list": self.a.pk, "list": self.b.pk, "count": 1},
            {"type": "move_all", "from_list": self.a.pk, "list": self.b.pk},
            {"type": "archive_list", "list": self.b.pk},
            {"type": "create_list", "text": "Fresh"},
        ], self.admin)
        self.assertTrue(List.objects.filter(board=self.board, title="Fresh").exists())
        self.assertFalse(Card.objects.filter(list__board=self.board, is_archived=False).exists())
        self.assertGreaterEqual(len(done), 6, done)
        self.assertTrue(automation.describe_board_actions(self.board, [{"type": "create_list", "text": "Fresh"}]))

    def test_run_automations_hook_and_command(self):
        from django.core.management import call_command
        from django.test import override_settings
        call_command("run_automations")
        with override_settings(CRON_TOKEN=""):
            self.assertEqual(self.client.post("/internal/run-automations/").status_code, 404)
        with override_settings(CRON_TOKEN="tok"):
            self.assertEqual(self.client.post("/internal/run-automations/").status_code, 404)   # wrong token looks absent
            self.assertEqual(self.client.get("/internal/run-automations/", HTTP_AUTHORIZATION="Bearer tok").status_code, 405)
            r = self.client.post("/internal/run-automations/", HTTP_AUTHORIZATION="Bearer tok")
            self.assertEqual(r.status_code, 200, r.content)

    # ---- planner / inbox / notifications ----
    def test_inbox_update_done_date_reorder_delete_to_card(self):
        from .models import InboxItem
        i1 = InboxItem.objects.create(owner=self.mem, text="Call flour supplier", position=1)
        i2 = InboxItem.objects.create(owner=self.mem, text="Order boxes", position=2)
        r = self.j(self.m, f"/api/inbox/{i1.pk}/", {"text": "Call miller", "notes": "n", "due_date": "2030-02-01"})
        self.assertEqual(r.status_code, 200, r.content)
        i1.refresh_from_db()
        self.assertEqual((i1.text, str(i1.due_date)), ("Call miller", "2030-02-01"))
        self.assertEqual(self.j(self.m, f"/api/inbox/{i2.pk}/", {"before_id": i1.pk}).status_code, 200)
        self.assertEqual(self.j(self.m, f"/api/inbox/{i1.pk}/", {"is_done": True}).status_code, 200)
        self.assertEqual(self.j(self.o, f"/api/inbox/{i1.pk}/", {"text": "hack"}).status_code, 404)
        r = self.j(self.m, f"/api/inbox/{i2.pk}/to-card/", {"list_id": self.a.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(self.a.cards.filter(title="Order boxes").exists())
        self.assertEqual(self.j(self.m, f"/api/inbox/{i1.pk}/", {"delete": True}).status_code, 200)
        self.assertFalse(InboxItem.objects.filter(pk=i1.pk).exists())

    def test_notifications_read_and_open(self):
        n1 = Notification.objects.create(recipient=self.mem, kind="assigned", card=self.card, text="added you to Bake bread")
        Notification.objects.create(recipient=self.mem, kind="assigned", card=self.card, text="b")
        self.assertContains(self.m.get("/notifications/"), "added you to Bake bread")
        r = self.j(self.m, "/api/notifications/read/", {"id": n1.pk})
        self.assertEqual(r.json()["unread"], 1)
        self.assertEqual(self.j(self.m, "/api/notifications/read/").json()["unread"], 0)
        self.assertEqual(self.m.get(f"/notifications/{n1.pk}/").status_code, 302)

    # ---- search ----
    def test_search_operators(self):
        self.card.labels.add(self.label)
        self.card.due_at = timezone.now() - timezone.timedelta(days=1); self.card.save()
        for q, hit in (("bread", True), ("label:urgent", True), ("board:sweep", True), ("list:A", True),
                       ("member:mia", True), ("is:overdue", True), ("is:mine", False), ("label:nothing", False)):
            r = self.c.get("/search/", {"q": q})
            self.assertEqual(r.status_code, 200)
            (self.assertContains if hit else self.assertNotContains)(r, "Bake bread", msg_prefix=q)
        self.assertContains(self.m.get("/search/", {"q": "is:mine"}), "Bake bread")
        r = self.c.post("/search/save/", {"q": "label:urgent", "name": "Urgent stuff"})
        self.assertLess(r.status_code, 400)

    # ---- dates, recurring, markdown ----
    def test_nl_dates_numeric_and_words(self):
        from datetime import datetime as dt
        from . import nl_dates
        now = timezone.make_aware(dt(2026, 10, 22, 9, 0))
        for text, want in (("Order flour 25/12", (2026, 12, 25)), ("Party 3/1/27", (2027, 1, 3)),
                           ("Review 1/2", (2027, 2, 1)), ("tomorrow", (2026, 10, 23))):
            due, words = nl_dates.parse(text, now=now)
            self.assertIsNotNone(due, text)
            self.assertEqual(timezone.localtime(due).date().timetuple()[:3], want, text)
        self.assertEqual(nl_dates.parse("31/02/27", now=now)[0], None)
        self.assertEqual(nl_dates.parse("nothing here", now=now), (None, ""))

    def test_recurring_next_due_edges(self):
        from datetime import datetime as dt
        from .recurring import next_due
        tz = timezone.get_current_timezone()
        jan31 = timezone.make_aware(dt(2027, 1, 31, 9), tz)
        self.assertEqual(timezone.localtime(next_due(jan31, "monthly")).day, 28)
        dec = timezone.make_aware(dt(2026, 12, 15, 9), tz)
        self.assertEqual(timezone.localtime(next_due(dec, "monthly")).month, 1)
        fri = timezone.make_aware(dt(2026, 10, 23, 9), tz)
        self.assertEqual(timezone.localtime(next_due(fri, "weekdays")).weekday(), 0)
        leap = timezone.make_aware(dt(2028, 2, 29, 9), tz)
        self.assertEqual(timezone.localtime(next_due(leap, "yearly")).day, 28)
        self.assertEqual(timezone.localtime(next_due(fri, "daily")).day, 24)
        self.assertIsNone(next_due(fri, "never"))

    def test_markdown_render_is_safe(self):
        from .templatetags import lanes_extras
        fn = getattr(lanes_extras, "render_md", None) or getattr(lanes_extras, "markdown", None)
        if fn is None:
            self.skipTest("no markdown filter")
        html = str(fn("**bold** <script>alert(1)</script> [x](javascript:alert(1)) https://example.com"))
        self.assertIn("<strong>bold</strong>", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript', html)

    # ---- people & accounts ----
    def test_people_add_update_remove(self):
        url = f"/departments/{self.ws.pk}/people/add/"
        r = self.c.post(url, {"email": "New.Person@x.com", "first_name": "New", "role": "member"})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(email="new.person@x.com")
        self.assertTrue(WorkspaceMember.objects.filter(workspace=self.ws, user=u).exists())
        self.c.post(f"/departments/{self.ws.pk}/people/{u.pk}/", {"role": "admin"})
        self.assertEqual(WorkspaceMember.objects.get(workspace=self.ws, user=u).role, "admin")
        self.c.post(f"/departments/{self.ws.pk}/people/{u.pk}/", {"action": "remove"})
        self.assertFalse(WorkspaceMember.objects.filter(workspace=self.ws, user=u).exists())
        # plain members can't add people
        self.m.post(url, {"email": "sneak@x.com"})
        self.assertFalse(User.objects.filter(email="sneak@x.com").exists())

    def test_people_import_csv(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = b"email,first_name,last_name,department\nimp1@x.com,Imo,One,Sweep\nimp2@x.com,Ira,Two,Sweep\n"
        r = self.c.post("/company/people/import/", {"file": SimpleUploadedFile("p.csv", csv, "text/csv")})
        self.assertLess(r.status_code, 500)
        r = self.c.post("/company/people/import/", {"file": SimpleUploadedFile("p.csv", csv, "text/csv"), "confirm": "1"})
        self.assertLess(r.status_code, 500)

    def test_two_step_new_codes_disable_and_admin_reset(self):
        from accounts import twofactor
        u = self.mem
        u.totp_secret = twofactor.new_secret(); u.totp_enabled = True; u.save()
        code = lambda: twofactor._code(u.totp_secret, int(__import__("time").time() // 30))
        self.m.post("/account/two-step/new-codes/", {"code": "000000"})
        u.refresh_from_db(); before = list(u.recovery_codes)
        self.m.post("/account/two-step/new-codes/", {"code": code()})
        u.refresh_from_db(); self.assertNotEqual(u.recovery_codes, before)
        self.m.post("/account/two-step/off/", {"code": "000000"})
        u.refresh_from_db(); self.assertTrue(u.totp_enabled)
        self.assertEqual(self.o.post(f"/account/two-step/reset/{u.pk}/").status_code, 404)
        self.c.post(f"/account/two-step/reset/{u.pk}/")
        u.refresh_from_db(); self.assertFalse(u.totp_enabled)

    def test_email_prefs_theme_and_password_change(self):
        self.assertLess(self.m.post("/account/theme/", {"theme": "dark"}).status_code, 400)
        self.assertLess(self.m.post("/account/email/", {}).status_code, 500)
        r = self.m.post("/password/", {"old_password": "pw", "new_password1": "Cr0issant-42!", "new_password2": "Cr0issant-42!"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.get(pk=self.mem.pk).check_password("Cr0issant-42!"))

    # ---- public link ----
    def test_public_board_link(self):
        tok = self.board.public_token = "pubTok123456789abcdef"; self.board.save()
        self.assertEqual(Client().get("/p/short/").status_code, 404)
        r = Client().get(f"/p/{tok}/")
        self.assertContains(r, "Bake bread")
        self.assertEqual(Client().get(f"/p/{tok}/c/{self.card.pk}/").status_code, 200)
        self.assertEqual(Client().get("/p/wrongtoken/").status_code, 404)


class ExternalServicesMockedTests(TestCase):
    """Microsoft sign-in, Graph email and AI — with the network faked out."""

    def setUp(self):
        self.u = User.objects.create_user("ext@x.com", "pw", first_name="Ext")
        self.ws = Workspace.objects.create(name="Ext")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.board = Board.objects.create(workspace=self.ws, title="Ext")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        self.card = Card.objects.create(list=List.objects.create(board=self.board, title="L"), title="Launch menu",
                                        description="New autumn menu across all sites")

    def _fake_urlopen(self, req, timeout=0):
        import io
        sysmsg = json.loads(req.data)["system"]
        if "summarise" in sysmsg:
            reply = {"summary": "Menu launch is under way.", "status": "on track", "next": "Print menus", "open_questions": []}
        elif "to-do list" in sysmsg:
            reply = {"items": [{"text": "Order cake boards", "due": None, "notes": ""}]}
        elif "picks" in sysmsg or "priorit" in sysmsg.lower():
            reply = {"picks": [{"id": f"c{self.card.pk}", "why": "Launch is soon"}], "note": "All calm."}
        elif "JSON" in sysmsg:
            reply = {"title": "Launch steps", "steps": ["Taste test", "Print menus"]}
        else:
            return io.BytesIO(json.dumps({"content": [{"type": "text", "text": "A clear description."}]}).encode())
        self.calls.append(req.headers)
        return io.BytesIO(json.dumps({"content": [{"type": "text", "text": json.dumps(reply)}]}).encode())

    def test_ai_features_with_faked_claude(self):
        from unittest import mock
        from django.test import override_settings
        self.calls = []
        self.client.force_login(self.u)
        post = lambda url, d=None: self.client.post(url, json.dumps(d or {}), content_type="application/json")
        with override_settings(ANTHROPIC_API_KEY="test-key"), mock.patch("urllib.request.urlopen", self._fake_urlopen):
            r = post(f"/api/cards/{self.card.pk}/suggest-steps/")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertContains(post(f"/api/cards/{self.card.pk}/summarise/"), "Menu launch")
            self.assertContains(post(f"/api/cards/{self.card.pk}/write-description/", {"notes": "autumn"}), "clear description")
            self.card.members.add(self.u)
            self.assertContains(post("/api/ai/priorities/"), "Launch is soon")
            r = post("/api/inbox/capture/", {"text": "Hi Paulo, can you order 20 cake boards? Thanks"})
            self.assertLess(r.status_code, 500, r.content)
        self.assertTrue(self.calls and self.calls[0].get("X-api-key") == "test-key")
        with override_settings(ANTHROPIC_API_KEY=""):
            self.assertEqual(post(f"/api/cards/{self.card.pk}/summarise/").status_code, 503)   # AI off → friendly message

    def test_ai_service_error_is_friendly(self):
        import urllib.error
        from unittest import mock
        from django.test import override_settings
        self.client.force_login(self.u)
        def boom(req, timeout=0):
            raise urllib.error.URLError("down")
        with override_settings(ANTHROPIC_API_KEY="k"), mock.patch("urllib.request.urlopen", boom):
            r = self.client.post(f"/api/cards/{self.card.pk}/summarise/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 503)
        self.assertIn("reach", r.json()["error"])

    def test_microsoft_login_start_and_bad_callback(self):
        from django.test import override_settings
        with override_settings(MS_CLIENT_ID="cid", MS_CLIENT_SECRET="sec", MS_TENANT_ID="tid",
                               MICROSOFT_CLIENT_ID="cid", MICROSOFT_CLIENT_SECRET="sec", MICROSOFT_TENANT_ID="tid"):
            r = self.client.get("/login/microsoft/")
            self.assertLess(r.status_code, 500)
            r = self.client.get("/login/microsoft/callback/", {"state": "wrong", "code": "x"})
            self.assertLess(r.status_code, 500)


class MicrosoftGeoSimMockedTests(TestCase):
    """Outlook email via Graph, postcode lookups and the simulation seeder — network faked."""

    def test_graph_email_backend_sends_with_app_token(self):
        from unittest import mock
        from django.core.mail import EmailMultiAlternatives
        from django.test import override_settings
        from accounts import microsoft
        seen = []

        class R:
            def __init__(self, data, status=202): self.data, self.status = data, status
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self.data).encode()

        def fake(req, timeout=0):
            seen.append((req.full_url, dict(req.headers), req.data))
            return R({"access_token": "AT", "expires_in": 3600}) if "oauth2" in req.full_url else R({})
        microsoft._app_token.update(value=None, expires=0)
        with override_settings(MS_TENANT_ID="t", MS_CLIENT_ID="c", MS_CLIENT_SECRET="s", MS_MAIL_SENDER="lanes@thenewt.co.uk",
                               EMAIL_BACKEND="accounts.graph_mail.GraphEmailBackend"), mock.patch("urllib.request.urlopen", fake):
            m = EmailMultiAlternatives("Due tomorrow", "plain", to=["a@x.com"], reply_to=["r@x.com"])
            m.attach_alternative("<b>html</b>", "text/html")
            self.assertEqual(m.send(), 1)
        self.assertIn("login.microsoftonline.com/t/oauth2/v2.0/token", seen[0][0])
        self.assertEqual(seen[1][0], "https://graph.microsoft.com/v1.0/users/lanes@thenewt.co.uk/sendMail")
        self.assertEqual(seen[1][1]["Authorization"], "Bearer AT")
        body = json.loads(seen[1][2])
        self.assertEqual(body["message"]["body"]["contentType"], "HTML")
        self.assertEqual(body["message"]["replyTo"][0]["emailAddress"]["address"], "r@x.com")

    def test_graph_errors_raise_or_stay_silent(self):
        import io
        import urllib.error
        from unittest import mock
        from django.test import override_settings
        from accounts import microsoft
        from accounts.graph_mail import GraphEmailBackend
        from django.core.mail import EmailMessage

        def bad(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error_description":"Bad secret\\r\\nTrace"}'))
        microsoft._app_token.update(value=None, expires=0)
        with override_settings(MS_TENANT_ID="t", MS_CLIENT_ID="c", MS_CLIENT_SECRET="s", MS_MAIL_SENDER="x@y"), \
             mock.patch("urllib.request.urlopen", bad):
            with self.assertRaisesMessage(microsoft.MicrosoftError, "Bad secret"):
                GraphEmailBackend().send_messages([EmailMessage("s", "b", to=["a@x.com"])])
            self.assertEqual(GraphEmailBackend(fail_silently=True).send_messages([EmailMessage("s", "b", to=["a@x.com"])]), 0)

    def test_geocoding_postcode_then_place_and_offline(self):
        from unittest import mock
        from . import geo
        calls = []

        def fake(url, timeout):
            calls.append(url)
            if "postcodes.io" in url:
                return {"result": {"latitude": 51.1, "longitude": -2.5}}
            return [{"lat": "51.5", "lon": "-0.1"}]
        with mock.patch.object(geo, "_get", fake):
            self.assertEqual(geo.lookup("The Newt, BA7 7NG"), (51.1, -2.5))
            self.assertEqual(geo.lookup("Covent Garden"), (51.5, -0.1))
        self.assertIn("BA77NG", calls[0])
        self.assertEqual(geo.lookup("51.2, -2.4"), (51.2, -2.4))
        self.assertIsNone(geo.lookup("95, 10"))
        self.assertIsNone(geo.lookup(""))
        with mock.patch.object(geo, "_get", side_effect=OSError("offline")):
            self.assertIsNone(geo.lookup("Somewhere"))

    def test_seed_sim_skips_creates_once_and_removes(self):
        import os
        from io import StringIO
        from unittest import mock
        from django.core.management import call_command
        out = StringIO()
        with mock.patch.dict(os.environ, {"LANES_SIM_PASSWORD": ""}):
            call_command("seed_sim", stdout=out)
        self.assertIn("skipping", out.getvalue())
        with mock.patch.dict(os.environ, {"LANES_SIM_PASSWORD": "Sim-pass-123"}):
            call_command("seed_sim", stdout=StringIO())
            n = Card.objects.count()
            self.assertGreater(n, 0)
            call_command("seed_sim", stdout=StringIO())        # redeploy: no duplicates
            self.assertEqual(Card.objects.count(), n)
        self.assertTrue(User.objects.get(email="sim.sophie@example.com").check_password("Sim-pass-123"))
        call_command("seed_sim", "--remove", stdout=StringIO())
        self.assertFalse(User.objects.filter(email__startswith="sim.").exists())
        self.assertEqual(Card.objects.count(), 0)


class ErrorPageTests(TestCase):
    def test_missing_board_shows_friendly_page(self):
        from django.test import override_settings
        u = User.objects.create_user("e404@x.com", "pw")
        self.client.force_login(u)
        with override_settings(DEBUG=False):
            r = self.client.get("/b/99999/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "This page isn't here", status_code=404)
        self.assertContains(r, "Go to your boards", status_code=404)

    def test_500_page_renders_without_context(self):
        from django.template.loader import render_to_string
        html = render_to_string("500.html")
        self.assertIn("Something went wrong", html)


class SetupMyWorkTests(TestCase):
    def test_sets_up_personal_board_once(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import Rule, Schedule
        u = User.objects.create_user("me@x.com", "pw", is_superuser=True)
        ws = Workspace.objects.create(name="Dept")
        WorkspaceMember.objects.create(workspace=ws, user=u, role="admin")
        self.client.force_login(u)
        self.client.post(reverse("board_create"), {"workspace": ws.pk, "title": "Paulo", "visibility": "private"})
        b = Board.objects.get(title="Paulo")
        call_command("setup_my_work", board=str(b.pk), stdout=StringIO())
        self.assertEqual(list(b.lists.order_by("position").values_list("title", flat=True)),
                         ["Inbox", "This week", "Today", "Waiting on", "Done"])
        self.assertEqual(set(b.labels.exclude(name="").values_list("name", flat=True)),
                         {"People", "Christmas", "Food safety", "Urgent", "Development", "Systems"})
        self.assertEqual(b.labels.count(), 6)
        self.assertEqual(Rule.objects.filter(board=b).count(), 3)
        eow = Schedule.objects.get(board=b, title__startswith="EOW")
        self.assertEqual(timezone.localtime(eow.next_run_at).weekday(), 4)
        call_command("setup_my_work", board=str(b.pk), stdout=StringIO())        # second run changes nothing
        self.assertEqual(Rule.objects.filter(board=b).count(), 3)
        self.assertEqual(b.lists.count(), 5)
        # the rules work: moving into Waiting on sets a due date; completing moves to Done
        inbox = b.lists.get(title="Inbox"); waiting = b.lists.get(title="Waiting on")
        c = Card.objects.create(list=inbox, title="Chase supplier")
        j = lambda url, d: self.client.post(url, json.dumps(d), content_type="application/json")
        j(f"/api/cards/{c.pk}/move/", {"list_id": waiting.pk})
        c.refresh_from_db(); self.assertIsNotNone(c.due_at)
        j(f"/api/cards/{c.pk}/update/", {"is_complete": True})
        c.refresh_from_db(); self.assertEqual(c.list.title, "Done")
        # the schedules run
        from . import automation
        Schedule.objects.filter(board=b).update(next_run_at=timezone.now() - timezone.timedelta(minutes=1))
        automation.run_due_schedules()
        self.assertTrue(Card.objects.filter(list__board=b, title="Plan the week").exists())
        self.assertTrue(Card.objects.filter(list__board=b, title__startswith="EOW review", checklists__items__text="Send").exists())


class ExampleCardsTests(TestCase):
    def test_example_cards_added_once(self):
        from io import StringIO
        from django.core.management import call_command
        u = User.objects.create_user("me2@x.com", "pw", is_superuser=True)
        ws = Workspace.objects.create(name="Dept2")
        WorkspaceMember.objects.create(workspace=ws, user=u, role="admin")
        self.client.force_login(u)
        self.client.post(reverse("board_create"), {"workspace": ws.pk, "title": "Mine", "visibility": "private"})
        b = Board.objects.get(title="Mine")
        call_command("setup_my_work", board=str(b.pk), stdout=StringIO())
        call_command("add_example_cards", board=str(b.pk), stdout=StringIO())
        call_command("add_example_cards", board=str(b.pk), stdout=StringIO())
        self.assertEqual(Card.objects.filter(list__board=b).count(), 6)
        c = Card.objects.get(title="Organise paperwork for agency staff induction")
        self.assertEqual(c.list.title, "Today")
        self.assertEqual(c.checklist_counts(), (1, 7))
        self.assertEqual(set(c.labels.values_list("name", flat=True)), {"People"})
        self.assertIn(u, c.members.all())
        self.assertContains(self.client.get(f"/b/{b.pk}/"), "Finish Bakery Managers Guide")
        self.assertEqual(self.client.get(f"/cards/{c.pk}/").status_code, 200)


class AutomationParityTests(TestCase):
    """Everything added to match Trello's automation."""

    def setUp(self):
        from .models import CustomField
        self.u = User.objects.create_user("ap@x.com", "pw", first_name="Ana")
        self.v = User.objects.create_user("vp@x.com", "pw", first_name="Vic")
        self.ws = Workspace.objects.create(name="AP")
        for u in (self.u, self.v):
            WorkspaceMember.objects.create(workspace=self.ws, user=u)
        self.b = Board.objects.create(workspace=self.ws, title="AP board")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        BoardMember.objects.create(board=self.b, user=self.v, role="member")
        self.l1 = List.objects.create(board=self.b, title="Inbox", position=1000)
        self.l2 = List.objects.create(board=self.b, title="Doing", position=2000)
        self.l3 = List.objects.create(board=self.b, title="Done", position=3000)
        self.red = Label.objects.create(board=self.b, colour="red", name="Urgent")
        self.field = CustomField.objects.create(board=self.b, name="Qty", kind="number")
        self.c = Client(); self.c.force_login(self.u)

    def j(self, url, d=None, client=None):
        return (client or self.c).post(url, json.dumps(d or {}), content_type="application/json")

    def rule(self, trigger, actions, **kw):
        from .models import Rule
        return Rule.objects.create(board=self.b, trigger=trigger, actions=actions, created_by=self.u, **kw)

    def comments(self, card):
        return list(card.comments.values_list("body", flat=True))

    # ---- triggers ----
    def test_added_to_board_and_list_count(self):
        self.rule("board_added", [{"type": "comment", "text": "welcome"}])
        self.rule("list_count", [{"type": "comment", "text": "three!"}], trigger_list=self.l1, trigger_count=3)
        ids = [self.j(f"/api/lists/{self.l1.pk}/cards/", {"title": f"c{i}"}).json()["card_id"] for i in range(3)]
        cards = [Card.objects.get(pk=i) for i in ids]
        self.assertTrue(all("welcome" in self.comments(c) for c in cards))
        self.assertEqual([("three!" in self.comments(c)) for c in cards], [False, False, True])

    def test_archived_due_set_removed_attachment_comment_field(self):
        card = Card.objects.create(list=self.l1, title="x")
        for trig in ("card_archived", "due_set", "due_removed", "attachment_added", "comment_added", "field_set"):
            self.rule(trig, [{"type": "add_sticker", "sticker": "⭐"}] if trig == "card_archived" else
                      [{"type": "comment", "text": f"saw {trig}"}], **({"trigger_field": self.field} if trig == "field_set" else {}))
        u = f"/api/cards/{card.pk}/"
        self.j(u + "update/", {"due_at": "2030-01-01T10:00"})
        self.j(u + "update/", {"due_at": ""})
        self.j(u + "links/", {"url": "https://example.com/spec"})
        self.j(u + "comments/", {"body": "hello"})
        self.j(u + "fields/", {"field_id": self.field.pk, "value": "4"})
        bodies = self.comments(card)
        for trig in ("due_set", "due_removed", "attachment_added", "comment_added", "field_set"):
            self.assertIn(f"saw {trig}", bodies, trig)
        self.assertEqual(bodies.count("saw comment_added"), 1)        # automation comments don't re-trigger
        self.j(u + "update/", {"archive": True})
        card.refresh_from_db()
        self.assertIn("⭐", card.stickers)

    def test_mentioned_only_me(self):
        card = Card.objects.create(list=self.l1, title="m")
        self.rule("mentioned", [{"type": "add_label", "label": self.red.pk}], trigger_me=True)
        vc = Client(); vc.force_login(self.v)
        self.j(f"/api/cards/{card.pk}/comments/", {"body": "nobody here"}, client=vc)
        self.assertFalse(card.labels.exists())
        self.j(f"/api/cards/{card.pk}/comments/", {"body": "@Ana can you check?"}, client=vc)
        self.assertTrue(card.labels.filter(pk=self.red.pk).exists())

    def test_list_created_runs_board_actions_on_new_list(self):
        self.rule("list_created", [{"type": "create_card", "list": "new", "text": "Read me"}])
        self.j(f"/api/boards/{self.b.pk}/lists/", {"title": "Brand new"})
        new = List.objects.get(title="Brand new")
        self.assertTrue(new.cards.filter(title="Read me").exists())

    def test_due_weekday_before(self):
        from datetime import datetime as dt
        from . import automation
        tz = timezone.get_current_timezone()
        card = Card.objects.create(list=self.l1, title="w", due_at=timezone.make_aware(dt(2026, 11, 5, 17), tz))  # Thursday
        self.rule("due_weekday", [{"type": "comment", "text": "Monday nudge"}], trigger_weekday=0, trigger_hours=9)
        automation.run_due_rules(now=timezone.make_aware(dt(2026, 11, 2, 8, 59), tz))
        self.assertNotIn("Monday nudge", self.comments(card))
        automation.run_due_rules(now=timezone.make_aware(dt(2026, 11, 2, 9, 1), tz))
        automation.run_due_rules(now=timezone.make_aware(dt(2026, 11, 3, 9, 1), tz))
        self.assertEqual(self.comments(card).count("Monday nudge"), 1)

    def test_filters(self):
        self.l3.board.complete_to_done = False   # only the rule decides here
        self.l3.board.save()
        self.rule("card_moved", [{"type": "complete"}], trigger_list=self.l3,
                  filters=[{"type": "label", "label": self.red.pk}, {"type": "has_due"}])
        a = Card.objects.create(list=self.l1, title="a")
        b = Card.objects.create(list=self.l1, title="b", due_at=timezone.now())
        b.labels.add(self.red)
        for c in (a, b):
            self.j(f"/api/cards/{c.pk}/move/", {"list_id": self.l3.pk})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertFalse(a.is_complete)
        self.assertTrue(b.is_complete)

    # ---- actions ----
    def test_new_card_actions(self):
        from . import automation
        from .models import CardFieldValue, Checklist
        other = Card.objects.create(list=self.l2, title="first", position=10)
        card = Card.objects.create(list=self.l1, title="Bread", position=500, description="old")
        card.members.add(self.v)
        Checklist.objects.create(card=card, title="S").items.create(text="Step one")
        run = lambda a: automation.run_action(Card.objects.get(pk=card.pk), a)
        self.assertTrue(run({"type": "move", "list": self.l2.pk, "position": "top"}))
        self.assertEqual(list(self.l2.cards.order_by("position").values_list("title", flat=True)), ["Bread", "first"])
        self.assertTrue(run({"type": "move_bottom"}))
        self.assertEqual(list(self.l2.cards.order_by("position").values_list("title", flat=True)), ["first", "Bread"])
        self.assertTrue(run({"type": "copy", "list": self.l3.pk}))
        self.assertTrue(self.l3.cards.filter(title="Bread").exists())
        self.assertTrue(run({"type": "remove_member", "user": self.v.pk}))
        self.assertFalse(card.members.exists())
        self.assertTrue(run({"type": "set_field", "field": self.field.pk, "value": "12"}))
        self.assertEqual(CardFieldValue.objects.get(card=card).value, 12)
        self.assertFalse(run({"type": "set_field", "field": self.field.pk, "value": "lots"}))
        self.assertTrue(run({"type": "sort_list", "by": "name"}))
        self.assertEqual(list(self.l2.cards.order_by("position").values_list("title", flat=True)), ["Bread", "first"])
        self.assertTrue(run({"type": "cascade", "list": self.l3.pk}))
        self.assertTrue(self.l3.cards.filter(title="Step one").exists())
        self.assertTrue(run({"type": "rename", "text": "{cardname} (v2)"}))
        self.assertTrue(run({"type": "set_description", "text": "About {cardname}"}))
        self.assertTrue(run({"type": "set_start", "days": 2}))
        card.refresh_from_db()
        self.assertEqual(card.title, "Bread (v2)")
        self.assertEqual(card.description, "About Bread (v2)")
        self.assertEqual(card.start_date, timezone.localdate() + timezone.timedelta(days=2))

    def test_board_actions_unique_and_copy_named(self):
        from . import automation
        tpl = Card.objects.create(list=self.l1, title="Opening checks", is_template=True)
        tpl.labels.add(self.red)
        acts = [{"type": "create_card", "list": self.l2.pk, "text": "Daily", "unique": True},
                {"type": "copy_named", "text": "opening checks", "list": self.l2.pk}]
        automation.run_board_actions(self.b, acts, None)
        automation.run_board_actions(self.b, acts, None)
        self.assertEqual(self.l2.cards.filter(title="Daily").count(), 1)
        copies = self.l2.cards.filter(title="Opening checks")
        self.assertEqual(copies.count(), 2)
        self.assertFalse(copies.filter(is_template=True).exists())
        self.assertTrue(copies.first().labels.filter(pk=self.red.pk).exists())

    # ---- schedules ----
    def test_several_days_every_two_weeks(self):
        from datetime import date, datetime as dt
        from . import automation
        from .models import Schedule
        tz = timezone.get_current_timezone()
        s = Schedule(board=self.b, frequency="weekly", weekday=0, weekdays="0,3", interval=2, hour=7,
                     anchor=date(2026, 10, 19), next_run_at=timezone.now())
        runs, after = [], timezone.make_aware(dt(2026, 10, 19, 0), tz)
        for _ in range(4):
            after = automation.next_run(s, after)
            runs.append(timezone.localtime(after).date().isoformat())
        self.assertEqual(runs, ["2026-10-19", "2026-10-22", "2026-11-02", "2026-11-05"])
        self.assertEqual(automation.describe_schedule(s), "Every 2 weeks on Monday and Thursday at 07:00")

    # ---- the builder's POSTs, including edits ----
    def test_rule_post_with_filters_and_edit(self):
        from .models import Rule
        url = f"/b/{self.b.pk}/automation/rules/"
        self.c.post(url, {"trigger": "list_count", "trigger_list": self.l2.pk, "trigger_count": "5",
                          "filters": json.dumps([{"type": "no_label", "label": self.red.pk}, {"type": "bogus"}]),
                          "action_0": "copy", "list_0": self.l3.pk, "position_0": "top",
                          "action_1": "set_field", "field_1": self.field.pk, "value_1": "3"})
        r = Rule.objects.get(trigger="list_count")
        self.assertEqual((r.trigger_count, r.filters), (5, [{"type": "no_label", "label": self.red.pk}]))
        self.assertEqual(r.actions[0], {"type": "copy", "list": self.l3.pk, "position": "top"})
        Rule.objects.filter(pk=r.pk).update(run_count=7)
        self.c.post(url, {"edit_id": r.pk, "trigger": "card_archived", "action_0": "rename", "text_0": "Old: {cardname}"})
        r.refresh_from_db()
        self.assertEqual((r.trigger, r.run_count, r.filters), ("card_archived", 7, []))
        self.assertEqual(Rule.objects.count(), 1)
        # bad field value is refused
        self.c.post(url, {"trigger": "card_archived", "action_0": "set_field", "field_0": self.field.pk, "value_0": "many"})
        self.assertEqual(Rule.objects.count(), 1)
        # list_created takes board actions with "the new list"
        self.c.post(url, {"trigger": "list_created", "action_0": "create_card", "list_0": "new", "text_0": "Hi"})
        self.assertEqual(Rule.objects.get(trigger="list_created").actions[0]["list"], "new")
        # due weekday
        self.c.post(url, {"section": "due", "trigger": "due_weekday", "trigger_weekday": "4", "trigger_hours": "16",
                          "action_0": "notify"})
        self.assertEqual(Rule.objects.get(trigger="due_weekday").trigger_weekday, 4)

    def test_schedule_and_button_edits_and_options(self):
        from .models import Button, Schedule
        surl = f"/b/{self.b.pk}/automation/schedules/"
        self.c.post(surl, {"frequency": "weekly", "weekdays": "1,4", "interval": "2", "hour": "6", "kind": "actions",
                           "saction_0": "create_card", "slist_0": self.l1.pk, "stext_0": "Check", "sunique_0": "1"})
        s = Schedule.objects.get()
        self.assertEqual((s.weekdays, s.interval, s.weekday, s.actions[0]["unique"]), ("1,4", 2, 1, True))
        self.c.post(surl, {"edit_id": s.pk, "frequency": "daily", "hour": "8", "kind": "card", "title": "Temps",
                           "target_list": self.l1.pk})
        s.refresh_from_db()
        self.assertEqual((Schedule.objects.count(), s.frequency, s.kind, s.title), (1, "daily", "card", "Temps"))
        burl = f"/b/{self.b.pk}/automation/buttons/"
        self.c.post(burl, {"kind": "card", "name": "Done!", "enabled": "0", "close_card": "1",
                           "baction_0": "complete"})
        btn = Button.objects.get()
        self.assertEqual((btn.is_enabled, btn.close_card), (False, True))
        card = Card.objects.create(list=self.l1, title="k")
        self.assertNotContains(self.c.get(f"/cards/{card.pk}/"), "Done!")
        self.assertEqual(self.j(f"/api/cards/{card.pk}/buttons/{btn.pk}/").status_code, 404)
        self.c.post(f"/b/{self.b.pk}/automation/buttons/{btn.pk}/", {"action": "toggle"})
        self.assertContains(self.c.get(f"/cards/{card.pk}/"), "data-close-after")
        self.c.post(burl, {"edit_id": btn.pk, "kind": "card", "name": "Finished", "enabled": "1", "baction_0": "archive"})
        btn.refresh_from_db()
        self.assertEqual((Button.objects.count(), btn.name, btn.actions), (1, "Finished", [{"type": "archive"}]))

    def test_pages_suggestions_reports_edit_data(self):
        from .services import log
        self.rule("card_completed", [{"type": "move", "list": self.l3.pk}])
        for i in range(3):
            c = Card.objects.create(list=self.l1, title=f"s{i}")
            log(self.b, self.u, "card_moved", card=c, **{"from": "Inbox", "to": "Doing"})
            log(self.b, self.u, "card_completed", card=c)
        page = self.c.get(f"/b/{self.b.pk}/automation/?s=suggestions")
        self.assertContains(page, "moved into <b>Doing</b>, <b>mark it complete</b>", html=False)
        self.assertContains(page, "3 times")
        rep = self.c.get(f"/b/{self.b.pk}/automation/?s=reports")
        self.assertContains(rep, "Runs")
        rules = self.c.get(f"/b/{self.b.pk}/automation/?s=rules")
        self.assertContains(rules, "data-edit=")
        self.assertContains(rules, "auto-data")


class AttachmentMenuTests(TestCase):
    def test_rename_edit_link_and_download(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import Attachment
        u = User.objects.create_user("am@x.com", "pw")
        ws = Workspace.objects.create(name="AM"); WorkspaceMember.objects.create(workspace=ws, user=u)
        b = Board.objects.create(workspace=ws, title="AM"); BoardMember.objects.create(board=b, user=u, role="admin")
        card = Card.objects.create(list=List.objects.create(board=b, title="L"), title="c")
        self.client.force_login(u)
        j = lambda url, d: self.client.post(url, json.dumps(d), content_type="application/json")
        j(f"/api/cards/{card.pk}/links/", {"url": "https://example.com/a"})
        link = Attachment.objects.get(card=card)
        self.assertEqual(j(f"/api/attachments/{link.pk}/", {"name": "Spec", "url": "https://example.com/b"}).status_code, 200)
        link.refresh_from_db()
        self.assertEqual((link.name, link.url), ("Spec", "https://example.com/b"))
        self.assertEqual(j(f"/api/attachments/{link.pk}/", {"url": "javascript:alert(1)"}).status_code, 400)
        self.client.post(f"/api/cards/{card.pk}/attachments/", {"file": SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82", "image/png")})
        f = Attachment.objects.get(card=card, url="")
        j(f"/api/attachments/{f.pk}/", {"name": "loaf.png"})
        f.refresh_from_db(); self.assertEqual(f.name, "loaf.png")
        self.assertNotIn("attachment", self.client.get(f"/files/{f.pk}/").get("Content-Disposition", ""))
        self.assertIn("attachment", self.client.get(f"/files/{f.pk}/?download=1")["Content-Disposition"])
        modal = self.client.get(f"/cards/{card.pk}/")
        self.assertContains(modal, f'data-pop="att{f.pk}"')
        self.assertContains(modal, "?download=1")


class AgencyBoardSetupTests(TestCase):
    def test_fills_board_once_and_rules_work(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import Button, CustomField, Rule
        u = User.objects.create_user("ag@x.com", "pw", is_superuser=True)
        ws = Workspace.objects.create(name="Bakery"); WorkspaceMember.objects.create(workspace=ws, user=u, role="admin")
        self.client.force_login(u)
        self.client.post(reverse("board_create"), {"workspace": ws.pk, "title": "Agency staff"})
        b = Board.objects.get(title="Agency staff")
        b.lists.order_by("position").first().__class__.objects.filter(pk=b.lists.order_by("position").first().pk).update(title="Prep before they arrive")
        call_command("setup_agency_board", board=str(b.pk), stdout=StringIO())
        call_command("setup_agency_board", board=str(b.pk), stdout=StringIO())
        b.refresh_from_db()
        self.assertEqual((b.title, b.visibility), ("Agency staff — Christmas", "private"))
        self.assertEqual(list(b.lists.filter(is_archived=False).order_by("position").values_list("title", flat=True)),
                         ["Prep before they arrive", "Jobs", "People", "Shifts", "Done"])
        counts = {l.title: l.cards.count() for l in b.lists.all()}
        self.assertEqual((counts["Prep before they arrive"], counts["Jobs"], counts["People"], counts["Shifts"]), (8, 5, 1, 12))
        self.assertEqual(CustomField.objects.filter(board=b).count(), 5)
        self.assertEqual((Rule.objects.filter(board=b).count(), Button.objects.filter(board=b).count()), (4, 2))
        # add a worker from the template → induction label; tick everything → comment
        people = b.lists.get(title="People")
        tpl = people.cards.get(is_template=True)
        j = lambda url, d: self.client.post(url, json.dumps(d), content_type="application/json")
        new_id = j(f"/api/lists/{people.pk}/from-template/", {"template_id": tpl.pk}).json()["card_id"]
        card = Card.objects.get(pk=new_id)
        self.assertEqual(card.checklists.count(), 1)
        self.assertTrue(card.labels.filter(name="Induction").exists())
        for it in card.checklists.get().items.all():
            j(f"/api/items/{it.pk}/", {"is_done": True})
        card.refresh_from_db()
        self.assertFalse(card.labels.filter(name="Induction").exists())
        self.assertTrue(card.comments.filter(body="Induction complete ✅").exists())
        # finishing a prep card's checklist moves it to Done
        prep = b.lists.get(title="Prep before they arrive").cards.first()
        for it in prep.checklists.get().items.all():
            j(f"/api/items/{it.pk}/", {"is_done": True})
        prep.refresh_from_db()
        self.assertEqual((prep.list.title, prep.is_complete), ("Done", True))
        self.assertEqual(self.client.get(f"/b/{b.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/b/{b.pk}/calendar/?d=2026-12-15").status_code, 200)


class AgencyBoardRedoTests(TestCase):
    def test_rebuild_to_pauls_layout(self):
        from io import StringIO
        from django.core.management import call_command
        u = User.objects.create_user("ar@x.com", "pw", is_superuser=True)
        ws = Workspace.objects.create(name="Bakery"); WorkspaceMember.objects.create(workspace=ws, user=u, role="admin")
        self.client.force_login(u)
        self.client.post(reverse("board_create"), {"workspace": ws.pk, "title": "Agency staff"})
        b = Board.objects.get(title="Agency staff")
        first = b.lists.order_by("position").first(); first.title = "Prep before they arrive"; first.save()
        call_command("setup_agency_board", board=str(b.pk), stdout=StringIO())
        call_command("redo_agency_board", board=str(b.pk), stdout=StringIO())
        call_command("redo_agency_board", board=str(b.pk), stdout=StringIO())
        live = lambda t: b.lists.get(title=t, is_archived=False).cards.filter(is_archived=False)
        self.assertEqual(list(b.lists.filter(is_archived=False).order_by("position").values_list("title", flat=True)),
                         ["Prep Checks", "1st Day", "Jobs", "People", "Shifts", "Done"])
        self.assertEqual(list(live("Prep Checks").values_list("title", flat=True)),
                         ["Induction pack ready", "Timesheet printed", "Shoes", "PPE and uniforms", "Rota for the week"])
        self.assertEqual(live("1st Day").count(), 4)
        self.assertEqual(live("Jobs").count(), 11)
        self.assertEqual(live("People").count(), 1)
        self.assertEqual(list(live("Shifts").values_list("is_template", flat=True)), [True])
        plan = live("1st Day").get(title="Production Plan").checklists.get().items.count()
        self.assertEqual(plan, 10)
        self.assertEqual(Card.objects.filter(list__board=b, is_archived=True).count(), 25)   # old cards kept, archived
        # finishing a job moves it to Done
        job = live("Jobs").get(title="Mincemeat")
        self.client.post(f"/api/cards/{job.pk}/update/", json.dumps({"is_complete": True}), content_type="application/json")
        job.refresh_from_db()
        self.assertEqual(job.list.title, "Done")
        self.assertEqual(self.client.get(f"/b/{b.pk}/").status_code, 200)


class ProductionPlanTests(TestCase):
    def setUp(self):
        from io import StringIO
        from django.core.management import call_command
        self.u = User.objects.create_user("pp@x.com", "pw", is_superuser=True)
        ws = Workspace.objects.create(name="Bakery"); WorkspaceMember.objects.create(workspace=ws, user=self.u, role="admin")
        self.client.force_login(self.u)
        self.client.post(reverse("board_create"), {"workspace": ws.pk, "title": "Agency staff"})
        self.b = Board.objects.get(title="Agency staff")
        first = self.b.lists.order_by("position").first(); first.title = "Prep before they arrive"; first.save()
        for cmd in ("setup_agency_board", "redo_agency_board", "setup_production_plan", "setup_production_plan"):
            call_command(cmd, board=str(self.b.pk), stdout=StringIO())
        self.days = self.b.lists.get(title="Shifts")
        self.jobs = self.b.lists.get(title="Jobs")

    def plan_cards(self):
        return self.days.cards.filter(labels__name="Day plan", is_archived=False).order_by("start_date")

    def test_plan_covers_targets_in_order(self):
        from .models import ChecklistItem
        cards = list(self.plan_cards())
        self.assertGreater(len(cards), 10)
        self.assertTrue(all(c.start_date.weekday() < 5 for c in cards))
        self.assertLessEqual(max(c.checklists.get().items.count() for c in cards), 3)
        first = [i.text for i in cards[0].checklists.get().items.order_by("position")]
        self.assertEqual(first[0], "Mince pie dough — 2,000 cases' worth")
        total = sum(int(i.text.split(" — ")[1].split()[0].replace(",", "")) for i in
                    ChecklistItem.objects.filter(checklist__card__in=cards, text__startswith="Mince pie case stamping"))
        self.assertEqual(total, 15000)
        page = self.client.get(f"/b/{self.b.pk}/")
        self.assertContains(page, "tile-progress")
        self.assertContains(page, "0 / 15,000 cases")

    def test_ticking_moves_made_so_far_and_replan_keeps_started_days(self):
        from .models import CardFieldValue, CustomField
        made = CustomField.objects.get(board=self.b, name="Made so far")
        stamping = self.jobs.cards.get(title="Mince pie case stamping")
        day1 = self.plan_cards().first()
        line = day1.checklists.get().items.get(text__startswith="Mince pie case stamping")
        j = lambda url, d: self.client.post(url, json.dumps(d), content_type="application/json")
        j(f"/api/items/{line.pk}/", {"is_done": True})
        self.assertEqual(CardFieldValue.objects.get(card=stamping, field=made).value, 2000)
        j(f"/api/items/{line.pk}/", {"is_done": False})
        self.assertEqual(CardFieldValue.objects.get(card=stamping, field=made).value, 0)
        j(f"/api/items/{line.pk}/", {"is_done": True})
        # someone made 3,000 more on another day; target drops: re-plan
        CardFieldValue.objects.filter(card=stamping, field=made).update(value=5000)
        from .models import Button
        btn = Button.objects.get(board=self.b, name="Re-plan days")
        r = j(f"/api/boards/{self.b.pk}/buttons/{btn.pk}/", {})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(type(day1).objects.filter(pk=day1.pk).exists())          # started day kept
        from .models import ChecklistItem
        planned = sum(int(i.text.split(" — ")[1].split()[0].replace(",", "")) for i in ChecklistItem.objects.filter(
            checklist__card__in=self.plan_cards(), text__startswith="Mince pie case stamping", is_done=False))
        self.assertEqual(planned, 15000 - 5000 - 0)   # day 1's ticked 2,000 is already inside Made so far


class GanttTests(TestCase):
    def setUp(self):
        from datetime import date as d
        self.d = d
        self.u = User.objects.create_user("gt@x.com", "pw")
        ws = Workspace.objects.create(name="G"); WorkspaceMember.objects.create(workspace=ws, user=self.u)
        self.b = Board.objects.create(workspace=ws, title="Project")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.l = List.objects.create(board=self.b, title="Plan")
        self.client.force_login(self.u)

    def card(self, title, start, end, **kw):
        from datetime import datetime, time
        due = timezone.make_aware(datetime.combine(end, time(17)))
        return Card.objects.create(list=self.l, title=title, start_date=start, due_at=due, **kw)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_critical_path_and_float(self):
        from .models import Dependency
        from .schedule import analyse
        d = self.d
        a = self.card("A", d(2026, 11, 2), d(2026, 11, 4))
        b = self.card("B", d(2026, 11, 5), d(2026, 11, 10))
        c = self.card("C", d(2026, 11, 5), d(2026, 11, 6))      # Fri: spare Mon + Tue before D
        dd = self.card("D", d(2026, 11, 11), d(2026, 11, 12))
        for s, p in ((b, a), (c, a), (dd, b), (dd, c)):
            Dependency.objects.create(card=s, depends_on=p)
        res = analyse([a, b, c, dd], Dependency.objects.values_list("card_id", "depends_on_id", "lag_days"))
        self.assertEqual({k for k, v in res.items() if v["critical"]}, {a.pk, b.pk, dd.pk})
        self.assertEqual(res[c.pk]["float"], 2)

    def test_dependency_api_pushes_later_tasks_and_blocks_cycles(self):
        d = self.d
        a = self.card("Dough", d(2026, 11, 2), d(2026, 11, 4))
        b = self.card("Stamp", d(2026, 11, 3), d(2026, 11, 5))     # overlaps A
        c = self.card("Freeze", d(2026, 11, 6), d(2026, 11, 6))
        self.assertEqual(self.j(f"/api/cards/{c.pk}/dependencies/", {"depends_on": b.pk}).status_code, 200)
        self.assertEqual(self.j(f"/api/cards/{b.pk}/dependencies/", {"depends_on": a.pk}).status_code, 200)
        b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual((b.start_date, timezone.localtime(b.due_at).date()), (d(2026, 11, 5), d(2026, 11, 7)))  # pushed, same length
        self.assertEqual(timezone.localtime(c.due_at).date(), d(2026, 11, 9))       # next one too, skipping the weekend
        r = self.j(f"/api/cards/{a.pk}/dependencies/", {"depends_on": c.pk})
        self.assertEqual(r.status_code, 400)
        self.assertIn("circle", r.json()["error"])
        # A slips two days: everything after moves
        self.j(f"/api/cards/{a.pk}/update/", {"due_at": "2026-11-06T17:00"})
        b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual(b.start_date, d(2026, 11, 9))                               # Monday after Friday
        self.assertEqual(timezone.localtime(c.due_at).date(), d(2026, 11, 12))
        # remove a link
        self.j(f"/api/cards/{c.pk}/dependencies/", {"remove": b.pk})
        self.assertFalse(c.dependencies.exists())

    def test_milestone_estimate_and_page(self):
        d = self.d
        a = self.card("Kickoff", None, d(2026, 11, 2))
        self.j(f"/api/cards/{a.pk}/update/", {"is_milestone": True, "estimate_hours": "6.5"})
        a.refresh_from_db()
        self.assertTrue(a.is_milestone)
        self.assertEqual(float(a.estimate_hours), 6.5)
        self.assertEqual(self.j(f"/api/cards/{a.pk}/update/", {"estimate_hours": "lots"}).status_code, 400)
        b = self.card("Build", d(2026, 11, 3), d(2026, 11, 9))
        self.j(f"/api/cards/{b.pk}/dependencies/", {"depends_on": a.pk})
        page = self.client.get(f"/b/{self.b.pk}/gantt/?z=day")
        self.assertContains(page, 'class="gt-ms')
        self.assertContains(page, 'class="gt-arrow')
        self.assertContains(page, "Finishes")
        self.assertContains(page, 'data-gt-bar="%d"' % b.pk)
        for z in ("week", "month"):
            self.assertEqual(self.client.get(f"/b/{self.b.pk}/gantt/?z={z}").status_code, 200)
        modal = self.client.get(f"/cards/{b.pk}/")
        self.assertContains(modal, "Waits for")
        self.assertContains(modal, "Kickoff")
        # empty board still renders
        empty = Board.objects.create(workspace=self.b.workspace, title="Empty")
        BoardMember.objects.create(board=empty, user=self.u, role="admin")
        self.assertEqual(self.client.get(f"/b/{empty.pk}/gantt/").status_code, 200)


class PortfolioTests(TestCase):
    def setUp(self):
        from django.test import override_settings
        self.o = override_settings(EMAIL_ENABLED=True, EMAIL_VIA="test",
                                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                                   SITE_URL="https://lanes.example")
        self.o.enable(); self.addCleanup(self.o.disable)
        self.u = User.objects.create_user("pf@x.com", "pw", first_name="Pat")
        self.ws = Workspace.objects.create(name="Production")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.b = Board.objects.create(workspace=self.ws, title="Christmas", is_project=True,
                                      status_updated_at=timezone.now())
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.l = List.objects.create(board=self.b, title="Plan")
        self.client.force_login(self.u)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_auto_rag_rules_and_override(self):
        from datetime import timedelta
        from decimal import Decimal
        from .models import CostItem
        from .portfolio import health
        now = timezone.now()
        Card.objects.create(list=self.l, title="Fine", due_at=now + timedelta(days=3))
        self.assertEqual(health(self.b)["rag"], "green")
        Card.objects.create(list=self.l, title="Late", due_at=now - timedelta(days=1))
        self.assertEqual(health(self.b)["rag"], "amber")
        Card.objects.create(list=self.l, title="Launch", due_at=now - timedelta(days=1), is_milestone=True)
        h = health(self.b)
        self.assertEqual(h["rag"], "red")
        self.assertIn("milestone overdue: Launch", h["reasons"])
        Card.objects.filter(title__in=["Late", "Launch"]).update(is_complete=True)
        self.b.budget = Decimal("100"); self.b.save()
        CostItem.objects.create(board=self.b, description="Flour", amount=Decimal("95"), day=timezone.localdate())
        h = health(self.b)
        self.assertEqual((h["rag"], h["budget_pct"]), ("amber", 95))
        CostItem.objects.create(board=self.b, description="Butter", amount=Decimal("10"), day=timezone.localdate())
        self.assertEqual(health(self.b)["rag"], "red")
        self.b.rag = "green"; self.b.save()
        h = health(self.b)
        self.assertEqual((h["rag"], h["auto_rag"]), ("green", "red"))

    def test_labour_cost_from_time(self):
        from decimal import Decimal
        from .models import TimeEntry
        from .portfolio import spend
        self.u.hourly_rate = Decimal("12"); self.u.save()
        c = Card.objects.create(list=self.l, title="Dough")
        TimeEntry.objects.create(card=c, user=self.u, day=timezone.localdate(), minutes=90)
        self.assertEqual(spend(self.b)[1], Decimal("18.00"))

    def test_pages_status_api_and_settings(self):
        r = self.client.get("/portfolio/")
        self.assertContains(r, "Christmas")
        self.assertContains(self.client.get(f"/b/{self.b.pk}/report/"), "Christmas")
        self.assertEqual(self.j(f"/api/boards/{self.b.pk}/status/", {"rag": "amber", "status_note": "Short of butter"}).status_code, 200)
        self.b.refresh_from_db()
        self.assertEqual((self.b.rag, self.b.status_note), ("amber", "Short of butter"))
        self.assertContains(self.client.get("/portfolio/?rag=amber"), "Short of butter")
        self.assertNotContains(self.client.get("/portfolio/?rag=green"), "Short of butter")
        # someone outside can't see it
        other = User.objects.create_user("out@x.com", "pw")
        self.client.force_login(other)
        self.assertNotContains(self.client.get("/portfolio/"), "Christmas")
        self.assertEqual(self.client.get(f"/b/{self.b.pk}/report/").status_code, 404)

    def test_weekly_email_mondays_once(self):
        from datetime import datetime
        from django.core import mail
        from . import mailer
        tz = timezone.get_current_timezone()
        tue = timezone.make_aware(datetime(2026, 11, 3, 9), tz)
        mon = timezone.make_aware(datetime(2026, 11, 2, 9), tz)
        early = timezone.make_aware(datetime(2026, 11, 2, 6), tz)
        self.assertEqual(mailer.weekly_status_reports(tue), 0)
        self.assertEqual(mailer.weekly_status_reports(early), 0)
        self.assertEqual(mailer.weekly_status_reports(mon), 1)
        self.assertEqual(mailer.weekly_status_reports(mon), 0)
        mailer.flush()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Christmas", mail.outbox[0].subject)
        self.assertIn("/b/%d/report/" % self.b.pk, mail.outbox[0].body)


class TimeWorkloadCostTests(TestCase):
    def setUp(self):
        from datetime import date
        from decimal import Decimal
        self.D, self.Dec = date, Decimal
        self.boss = User.objects.create_user("boss-tw@x.com", "pw", first_name="Boss")
        self.u = User.objects.create_user("w@x.com", "pw", first_name="Wendy", hourly_rate=Decimal("12"))
        self.ws = Workspace.objects.create(name="Production")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        WorkspaceMember.objects.create(workspace=self.ws, user=self.boss, role="admin")
        self.b = Board.objects.create(workspace=self.ws, title="Xmas", is_project=True, budget=Decimal("100"))
        BoardMember.objects.create(board=self.b, user=self.u)
        BoardMember.objects.create(board=self.b, user=self.boss, role="admin")
        self.l = List.objects.create(board=self.b, title="Plan")
        self.c = Card.objects.create(list=self.l, title="Dough")
        self.client.force_login(self.u)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_parse_and_format(self):
        from .timetrack import fmt, parse_minutes
        for s, m in (("1h 30m", 90), ("1:30", 90), ("1.5", 90), ("90m", 90), ("2h", 120), ("45 mins", 45), ("x", None)):
            self.assertEqual(parse_minutes(s), m, s)
        self.assertEqual((fmt(90), fmt(60), fmt(5)), ("1h 30m", "1h", "5m"))

    def test_timer_and_manual_entries(self):
        from datetime import timedelta
        from .models import TimeEntry
        other = Card.objects.create(list=self.l, title="Stamp")
        url = f"/api/cards/{self.c.pk}/time/"
        self.assertEqual(self.j(url, {"action": "start"}).status_code, 200)
        TimeEntry.objects.filter(started_at__isnull=False).update(started_at=timezone.now() - timedelta(minutes=25))
        r = self.j(f"/api/cards/{other.pk}/time/", {"action": "start"})   # starting another stops the first
        self.assertEqual(r.json()["stopped"], "Dough")
        self.assertEqual(TimeEntry.objects.get(card=self.c).minutes, 25)
        self.assertContains(self.client.get(f"/b/{self.b.pk}/"), "data-timer-pill")
        self.assertEqual(self.j("/api/time/stop/", {}).json()["card"], "Stamp")
        self.assertFalse(TimeEntry.objects.filter(started_at__isnull=False).exists())
        self.assertEqual(self.j(url, {"action": "add", "time": "1h 15m", "note": "mixing"}).status_code, 200)
        self.assertEqual(self.j(url, {"action": "add", "time": "lots"}).status_code, 400)
        future = (timezone.localdate() + timedelta(days=2)).isoformat()
        self.assertEqual(self.j(url, {"action": "add", "time": "1", "day": future}).status_code, 400)
        e = TimeEntry.objects.get(note="mixing")
        self.assertEqual(e.minutes, 75)
        self.client.force_login(self.boss)   # someone else's entry: only admins may delete
        self.assertEqual(self.j(url, {"action": "delete", "id": e.pk}).status_code, 200)

    def test_workload_spreads_remaining_hours(self):
        from datetime import datetime, time, timedelta
        from .models import TimeEntry
        from .workload import build
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        nxt = monday + timedelta(weeks=1)
        due = timezone.make_aware(datetime.combine(nxt + timedelta(days=4), time(17)))
        self.c.start_date, self.c.due_at, self.c.estimate_hours = nxt, due, self.Dec("20")
        self.c.save(); self.c.members.add(self.u, self.boss)
        TimeEntry.objects.create(card=self.c, user=self.u, day=today, minutes=120)   # 18h left, 9h each
        data = build(self.boss)
        row = next(r for r in data["rows"] if r["user"] == self.u)
        self.assertAlmostEqual(row["cells"][1]["hours"], 9.0)
        self.assertEqual(row["cells"][0]["hours"], 0)
        self.assertEqual(row["cells"][1]["level"], "low")        # 9 of 37.5
        self.assertEqual(row["logged_week"], 120)
        # overdue, unassigned work lands on this week
        Card.objects.create(list=self.l, title="Late one", estimate_hours=self.Dec("4"),
                            due_at=timezone.now() - timedelta(days=3))
        data = build(self.boss)
        self.assertAlmostEqual(data["nobody"]["cells"][0]["hours"], 4.0)
        r = self.client.get("/workload/?p=%d&i=1" % self.u.pk)
        self.assertContains(r, "Dough")
        self.assertContains(self.client.get("/workload/?by=day"), "Wendy")

    def test_capacity_permissions(self):
        url = f"/api/users/{self.u.pk}/capacity/"
        self.assertEqual(self.j(url, {"capacity_hours": "30"}).status_code, 200)       # own hours
        self.assertEqual(self.j(url, {"hourly_rate": "20"}).status_code, 403)          # not own pay
        self.assertEqual(self.j(f"/api/users/{self.boss.pk}/capacity/", {"capacity_hours": "1"}).status_code, 403)
        self.client.force_login(self.boss)
        self.assertEqual(self.j(url, {"hourly_rate": "14.50", "capacity_hours": "22.5"}).status_code, 200)
        self.u.refresh_from_db()
        self.assertEqual((self.u.hourly_rate, self.u.capacity_hours), (self.Dec("14.50"), self.Dec("22.5")))

    def test_timesheets_visibility_and_csv(self):
        from .models import TimeEntry
        TimeEntry.objects.create(card=self.c, user=self.u, day=timezone.localdate(), minutes=90, note="dough")
        TimeEntry.objects.create(card=self.c, user=self.boss, day=timezone.localdate(), minutes=30, note="boss time")
        r = self.client.get("/timesheets/")
        self.assertContains(r, "1h 30m")
        self.assertNotContains(r, "boss time")
        self.client.force_login(self.boss)
        r = self.client.get("/timesheets/")
        self.assertContains(r, "boss time"); self.assertContains(r, "Wendy")
        csv = self.client.get("/timesheets/?csv=1").content.decode()
        self.assertIn("Wendy", csv); self.assertIn("18.0", csv)   # 1.5h × £12

    def test_costs_and_budget_warning(self):
        from .models import Notification
        url = f"/api/boards/{self.b.pk}/costs/"
        self.assertEqual(self.j(url, {"description": "Flour", "amount": "£80", "category": "materials"}).status_code, 200)
        self.assertFalse(Notification.objects.filter(kind="budget").exists())
        self.assertEqual(self.j(url, {"description": "Butter", "amount": "15"}).status_code, 200)   # 95%
        n = Notification.objects.get(kind="budget", recipient=self.boss)
        self.assertIn("90%", n.text)
        self.j(url, {"description": "Eggs", "amount": "10"})                                        # 105%
        self.assertTrue(Notification.objects.filter(kind="budget", text__contains="over budget").exists())
        self.assertEqual(self.j(url, {"description": "", "amount": "1"}).status_code, 400)
        self.assertEqual(self.j(url, {"description": "x", "amount": "abc"}).status_code, 400)
        r = self.client.get(f"/b/{self.b.pk}/costs/")
        self.assertContains(r, "Over budget")
        self.assertContains(r, "Flour")


class ProductQuantityTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("pq@x.com", "pw")
        ws = Workspace.objects.create(name="P"); WorkspaceMember.objects.create(workspace=ws, user=self.u)
        self.b = Board.objects.create(workspace=ws, title="Xmas")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.l = List.objects.create(board=self.b, title="To prepare")
        self.client.force_login(self.u)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    TEXT = """AF Christmas Cake — 430 cakes
Packaging:
Printed Product Label artwork
1 x Cake/Biscuit Tin
Make:
[x] 217.417 kg Fruit Soak
Finish:
1 x Decorate"""

    def test_parse(self):
        from .products import parse
        p = parse(self.TEXT)
        self.assertEqual((p["title"], int(p["planned"]), p["unit"]), ("AF Christmas Cake", 430, "cakes"))
        self.assertEqual([s["title"] for s in p["sections"]], ["Packaging", "Make", "Finish"])
        tin = p["sections"][0]["items"][1]
        self.assertEqual((tin["text"], int(tin["target"]), tin["unit"]), ("Cake/Biscuit Tin", 430, "each"))
        self.assertIsNone(p["sections"][0]["items"][0]["target"])
        self.assertTrue(p["sections"][1]["items"][0]["done"])
        self.assertIsNone(parse("TN Stollen 600g")["planned"])          # a weight in the name isn't a quantity

    def test_create_record_and_rescale(self):
        from .models import ChecklistItem, CustomField
        r = self.j(f"/api/lists/{self.l.pk}/product/", {"text": self.TEXT})
        self.assertEqual(r.status_code, 200)
        card = Card.objects.get(title="AF Christmas Cake")
        dec = ChecklistItem.objects.get(checklist__card=card, text="Decorate")
        self.assertEqual(int(dec.qty_target), 430)
        soak = ChecklistItem.objects.get(checklist__card=card, text="Fruit Soak")
        self.assertTrue(soak.is_done)
        r = self.j(f"/api/items/{dec.pk}/", {"qty_add": "400"}); self.assertEqual(r.status_code, 200)
        r = self.j(f"/api/items/{dec.pk}/", {"qty_add": "30"})
        dec.refresh_from_db()
        self.assertTrue(dec.is_done)                                     # 430 / 430 ticks itself
        self.assertEqual(self.j(f"/api/items/{dec.pk}/", {"qty_add": "abc"}).status_code, 400)
        planned = CustomField.objects.get(board=self.b, name="Planned")
        self.j(f"/api/cards/{card.pk}/fields/", {"field_id": planned.pk, "value": "500"})
        dec.refresh_from_db(); soak.refresh_from_db()
        self.assertEqual((int(dec.qty_target), dec.is_done), (500, False))
        self.assertAlmostEqual(float(soak.qty_target), 252.8, places=1)    # kg scales too
        self.assertEqual(card.qty_progress() is not None, True)
        # preview doesn't create anything
        n = Card.objects.count()
        p = self.j(f"/api/lists/{self.l.pk}/product/", {"text": self.TEXT, "preview": True}).json()
        self.assertEqual((Card.objects.count(), p["planned"]), (n, "430"))


class ProductFieldsTests(TestCase):
    def test_typed_fields_and_variance(self):
        from .products import create, parse
        u = User.objects.create_user("pf2@x.com", "pw")
        ws = Workspace.objects.create(name="P2")
        b = Board.objects.create(workspace=ws, title="X")
        lst = List.objects.create(board=b, title="L")
        text = "Cake — 430 cakes\nLaunch: 6/10/2026\nForecast: 407\nNotes: buffer\nPackaging:\n1 x Tin\nRecipe: check"
        p = parse(text)
        self.assertEqual(p["fields"], [("Launch", "6/10/2026"), ("Forecast", "407")])
        self.assertEqual(p["sections"][0]["items"][1]["text"], "Recipe: check")   # after a section it's an item
        c = create(lst, text, u)
        vals = {v.field.name: v.value for v in c.field_values.select_related("field")}
        self.assertEqual((vals["Launch"], vals["Forecast"], vals["Variance"]), ("2026-10-06", 407, "+23 (+5.7%)"))
        self.assertEqual(c.description, "buffer")


class AmountsAnyCardTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("am@x.com", "pw")
        ws = Workspace.objects.create(name="Events"); WorkspaceMember.objects.create(workspace=ws, user=self.u)
        self.b = Board.objects.create(workspace=ws, title="Fair")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.l = List.objects.create(board=self.b, title="Plan")
        from .models import Checklist
        self.c = Card.objects.create(list=self.l, title="Summer fair")
        self.cl = Checklist.objects.create(card=self.c, title="Setup")
        self.client.force_login(self.u)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_smart_add_paste_and_amount(self):
        from .models import ChecklistItem
        url = f"/api/checklists/{self.cl.pk}/items/"
        self.j(url, {"text": "1 x Chair"})                       # no amount yet: remembered per one
        chair = ChecklistItem.objects.get(text="Chair")
        self.assertEqual((chair.per_unit, chair.qty_target), (1, None))
        self.j(url, {"text": "30 kg Salad\n0.125 x Table\nBook the band\n[x] Send invites"})
        self.assertEqual(ChecklistItem.objects.filter(checklist=self.cl).count(), 5)
        self.assertTrue(ChecklistItem.objects.get(text="Send invites").is_done)
        self.assertIsNone(ChecklistItem.objects.get(text="Book the band").qty_target)
        r = self.j(f"/api/cards/{self.c.pk}/amount/", {"amount": "200", "unit": "guests"})
        self.assertEqual(r.json()["rescaled"], 2)
        chair.refresh_from_db()
        self.assertEqual(int(chair.qty_target), 200)
        self.assertEqual(int(ChecklistItem.objects.get(text="Table").qty_target), 25)
        self.assertEqual(int(ChecklistItem.objects.get(text="Salad").qty_target), 30)   # a total stays put
        self.assertEqual(self.c.amount(), {"amount": "200", "unit": "guests"})
        self.j(url, {"text": "2 x Cup"})                           # with an amount: worked out straight away
        self.assertEqual(int(ChecklistItem.objects.get(text="Cup").qty_target), 400)
        self.assertContains(self.client.get(f"/cards/{self.c.pk}/"), "data-amount-form")

    def test_template_asks_how_many(self):
        from .models import ChecklistItem
        self.j(f"/api/checklists/{self.cl.pk}/items/", {"text": "1 x Chair"})
        self.j(f"/api/cards/{self.c.pk}/amount/", {"amount": "200"})
        self.c.is_template = True; self.c.save()
        self.assertTrue(self.c.per_one_lines())
        r = self.j(f"/api/lists/{self.l.pk}/from-template/", {"template_id": self.c.pk, "amount": "50"})
        new = Card.objects.get(pk=r.json()["card_id"])
        self.assertEqual(int(ChecklistItem.objects.get(checklist__card=new, text="Chair").qty_target), 50)
        self.assertEqual(new.amount()["amount"], "50")


class SetupCardsTests(TestCase):
    TEXT = """Summer fair — 200 guests
Deadline: 4 Jul 2026
Setup:
1 x Chair
30 kg Salad

Winter fair — 120 guests
Setup:
1 x Chair

# Done
Book venue"""

    def setUp(self):
        self.u = User.objects.create_user("su@x.com", "pw")
        ws = Workspace.objects.create(name="E"); WorkspaceMember.objects.create(workspace=ws, user=self.u)
        self.b = Board.objects.create(workspace=ws, title="Events")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.todo = List.objects.create(board=self.b, title="To do", position=1)
        self.client.force_login(self.u)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_preview_then_create_many(self):
        from .models import ChecklistItem
        url = f"/api/boards/{self.b.pk}/setup/"
        p = self.j(url, {"text": self.TEXT, "preview": True}).json()
        self.assertEqual([c["title"] for c in p["cards"]], ["Summer fair", "Winter fair", "Book venue"])
        self.assertEqual(p["cards"][2]["list"], "Done")
        self.assertFalse(Card.objects.filter(list__board=self.b).exists())
        r = self.j(url, {"text": self.TEXT, "list_id": self.todo.pk}).json()
        self.assertEqual(r["created"], 3)
        self.assertEqual(Card.objects.get(title="Summer fair").list, self.todo)
        self.assertEqual(Card.objects.get(title="Book venue").list.title, "Done")       # list made from '# Done'
        self.assertEqual(int(ChecklistItem.objects.get(checklist__card__title="Winter fair", text="Chair").qty_target), 120)
        self.assertEqual(self.j(url, {"text": "   "}).status_code, 400)

    def test_draft_uses_ai_and_validates(self):
        from unittest import mock
        from django.test import override_settings
        url = f"/api/boards/{self.b.pk}/setup/draft/"
        with override_settings(ANTHROPIC_API_KEY=""):
            self.assertEqual(self.j(url, {"text": "a fair"}).status_code, 503)
        with override_settings(ANTHROPIC_API_KEY="k"), mock.patch("boards.ai._call", return_value="```\n" + self.TEXT + "\n```") as call:
            r = self.j(url, {"text": "Summer fair for 200", "images": [{"media_type": "image/png", "data": "iVBORw0KGgo="}]})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["text"].startswith("Summer fair — 200 guests"))
            self.assertEqual(call.call_args.kwargs["images"], [("image/png", "iVBORw0KGgo=")])
            self.assertEqual(self.j(url, {"text": "x", "images": [{"media_type": "text/html", "data": "x"}]}).status_code, 400)
            self.assertEqual(self.j(url, {"text": ""}).status_code, 400)
        self.assertContains(self.client.get(f"/b/{self.b.pk}/"), "data-setup-board")


class DepartmentSettingsTests(TestCase):
    def test_rename_and_delete_only_when_empty(self):
        admin = User.objects.create_user("da@x.com", "pw")
        other = User.objects.create_user("do@x.com", "pw")
        ws = Workspace.objects.create(name="Christmas 2027")
        WorkspaceMember.objects.create(workspace=ws, user=admin, role="admin")
        WorkspaceMember.objects.create(workspace=ws, user=other, role="member")
        url = f"/departments/{ws.pk}/settings/"
        self.client.force_login(other)
        self.assertEqual(self.client.post(url, {"name": "X"}).status_code, 404)
        self.client.force_login(admin)
        self.client.post(url, {"name": "Events"})
        ws.refresh_from_db(); self.assertEqual(ws.name, "Events")
        b = Board.objects.create(workspace=ws, title="Fair")
        self.client.post(url, {"action": "delete", "confirm": "Events"})
        self.assertTrue(Workspace.objects.filter(pk=ws.pk).exists())          # has a board
        b.is_archived = True; b.save()
        self.client.post(url, {"action": "delete", "confirm": "wrong"})
        self.assertTrue(Workspace.objects.filter(pk=ws.pk).exists())          # name not typed
        self.client.post(url, {"action": "delete", "confirm": "events"})
        self.assertFalse(Workspace.objects.filter(pk=ws.pk).exists())


class ForgivingSetupTextTests(TestCase):
    def test_natural_typing_is_one_card(self):
        from .products import parse_many
        cards = parse_many("Christmas Cakes\nDeadline 15 November\n\n300kg Christmas Soak\n\nBake 500 Christmas Cakes\nStart the oven")
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["fields"], [("Deadline", "15 November")])
        items = [(i["text"], i["target"] and int(i["target"]), i["unit"]) for s in c["sections"] for i in s["items"]]
        self.assertEqual(items, [("Christmas Soak", 300, "kg"), ("Bake", 500, "Christmas Cakes"), ("Start the oven", None, "")])

    def test_new_cards_still_split(self):
        from .products import parse_many
        t = "Summer fair — 200 guests\nSetup:\n1 x Chair\n\nWinter fair — 120 guests\n1 x Chair\n---\nBook venue"
        self.assertEqual([c["title"] for c in parse_many(t)], ["Summer fair", "Winter fair", "Book venue"])


class DiscoverabilityTests(TestCase):
    def setUp(self):
        from .models import Checklist
        self.u = User.objects.create_user("dv@x.com", "pw")
        ws = Workspace.objects.create(name="D"); WorkspaceMember.objects.create(workspace=ws, user=self.u)
        self.b = Board.objects.create(workspace=ws, title="B")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.l = List.objects.create(board=self.b, title="L")
        self.c = Card.objects.create(list=self.l, title="Fair")
        self.cl = Checklist.objects.create(card=self.c, title="Setup")
        self.client.force_login(self.u)

    def j(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_item_hint_explains_without_adding(self):
        from .models import ChecklistItem
        url = f"/api/checklists/{self.cl.pk}/items/"
        self.assertEqual(self.j(url, {"text": "300kg Flour", "preview": True}).json()["hint"], "counts to 300 kg")
        self.assertIn("set the card's", self.j(url, {"text": "1 x Tin", "preview": True}).json()["hint"])
        self.assertEqual(self.j(url, {"text": "Book the band", "preview": True}).json()["hint"], "a tick box")
        self.assertFalse(ChecklistItem.objects.exists())

    def test_section_after_blank_line_stays_on_card(self):
        from .products import parse_many
        cards = parse_many("Summer fair — 200 guests\nDeadline 4 July\n\nSetup:\n1 x Chair\nFood:\n30 kg Salad")
        self.assertEqual(len(cards), 1)
        self.assertEqual([s["title"] for s in cards[0]["sections"]], ["Setup", "Food"])

    def test_empty_list_and_add_form_point_to_setup(self):
        List.objects.create(board=self.b, title="Empty", position=99)
        r = self.client.get(f"/b/{self.b.pk}/")
        self.assertNotContains(r, "list-empty-hint")              # the board already has cards
        self.assertContains(r, "add-many-link")
        nb = Board.objects.create(workspace=self.b.workspace, title="New")
        BoardMember.objects.create(board=nb, user=self.u, role="admin")
        List.objects.create(board=nb, title="To do", position=1); List.objects.create(board=nb, title="Done", position=2)
        self.assertContains(self.client.get(f"/b/{nb.pk}/"), "list-empty-hint", count=1)
        self.assertContains(r, "data-su-example")
        self.assertContains(self.client.get(f"/cards/{self.c.pk}/"), "data-item-hint")


class DepartmentWideTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("dw@x.com", "pw")
        self.ws = Workspace.objects.create(name="Production")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.admin, role="admin")
        self.a = self._board("Bakery")
        self.b = self._board("Creamery")
        self.client.force_login(self.admin)

    def _board(self, title, lists=("To do", "Done")):
        b = Board.objects.create(workspace=self.ws, title=title)
        BoardMember.objects.create(board=b, user=self.admin, role="admin")
        for i, t in enumerate(lists):
            List.objects.create(board=b, title=t, position=i)
        Label.objects.create(board=b, name="Urgent", colour="red")
        return b

    def test_rule_shared_across_department_matched_by_name(self):
        from .models import Rule
        done_a = self.a.lists.get(title="Done")
        r = Rule.objects.create(board=self.a, trigger="moved", trigger_list=done_a,
                                actions=[{"type": "complete"}, {"type": "add_label", "label": self.a.labels.get().pk}])
        url = f"/b/{self.a.pk}/automation/rules/{r.pk}/"
        from django.urls import reverse
        url = reverse("rule_update", args=[self.a.pk, r.pk])
        self.client.post(url, {"action": "everywhere"})
        copy = Rule.objects.get(board=self.b)
        self.assertEqual(copy.source, r)
        self.assertEqual(copy.trigger_list, self.b.lists.get(title="Done"))
        self.assertEqual(copy.actions[1]["label"], self.b.labels.get().pk)
        # a board without a "Done" list is skipped, not given one
        c = self._board("Butchery", lists=("Prep", "Out"))
        from . import shared_rules
        r.refresh_from_db()
        done, skipped = shared_rules.sync(r)
        self.assertEqual(done, 1)
        self.assertEqual(skipped[0][0], c)
        self.assertIn("Done", skipped[0][1])
        # copies can't be edited where they are; pausing the source pauses them
        self.client.post(reverse("rule_update", args=[self.b.pk, copy.pk]), {"action": "delete"})
        self.assertTrue(Rule.objects.filter(pk=copy.pk).exists())
        self.client.post(url, {})
        copy.refresh_from_db()
        self.assertFalse(copy.is_enabled)
        # a new board in the department gets it; stopping sharing removes the copies
        self.client.post(reverse("board_create"), {"workspace": self.ws.pk, "title": "Pastry"})
        self.assertTrue(Rule.objects.filter(board__title="Pastry", source=r).exists())
        self.client.post(url, {"action": "everywhere"})
        self.assertFalse(Rule.objects.filter(source=r).exists())

    def test_only_department_admins_share(self):
        from django.urls import reverse
        from .models import Rule
        member = User.objects.create_user("dm@x.com", "pw")
        WorkspaceMember.objects.create(workspace=self.ws, user=member)
        BoardMember.objects.create(board=self.a, user=member, role="admin")
        r = Rule.objects.create(board=self.a, trigger="added", actions=[{"type": "complete"}])
        self.client.force_login(member)
        self.client.post(reverse("rule_update", args=[self.a.pk, r.pk]), {"action": "everywhere"})
        r.refresh_from_db()
        self.assertFalse(r.everywhere)

    def test_templates_from_other_boards_in_department(self):
        from .models import ChecklistItem, Checklist
        from decimal import Decimal
        tpl = Card.objects.create(list=self.a.lists.first(), title="Cake order", is_template=True)
        tpl.labels.add(self.a.labels.get())
        cl = Checklist.objects.create(card=tpl, title="Make")
        ChecklistItem.objects.create(checklist=cl, text="Tin", per_unit=Decimal("1"))
        t = self.client.get(f"/api/boards/{self.b.pk}/templates/").json()["templates"]
        self.assertEqual([(x["title"], x["here"], x["per_one"]) for x in t], [("Cake order", False, True)])
        r = self.client.post(f"/api/lists/{self.b.lists.first().pk}/from-template/",
                             json.dumps({"template_id": tpl.pk, "amount": "30"}), content_type="application/json")
        new = Card.objects.get(pk=r.json()["card_id"])
        self.assertEqual(new.list.board, self.b)
        self.assertEqual(list(new.labels.values_list("name", flat=True)), ["Urgent"])
        self.assertEqual(int(ChecklistItem.objects.get(checklist__card=new).qty_target), 30)
        # a board in another department can't use it
        other = Workspace.objects.create(name="Other")
        WorkspaceMember.objects.create(workspace=other, user=self.admin, role="admin")
        ob = Board.objects.create(workspace=other, title="X")
        BoardMember.objects.create(board=ob, user=self.admin, role="admin")
        ol = List.objects.create(board=ob, title="L")
        r = self.client.post(f"/api/lists/{ol.pk}/from-template/", json.dumps({"template_id": tpl.pk}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_move_board_to_another_department(self):
        other = Workspace.objects.create(name="Kitchen")
        WorkspaceMember.objects.create(workspace=other, user=self.admin, role="admin")
        r = self.client.post(f"/api/boards/{self.b.pk}/update/", json.dumps({"workspace": other.pk}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.b.refresh_from_db()
        self.assertEqual(self.b.workspace, other)
        nope = Workspace.objects.create(name="Not mine")
        r = self.client.post(f"/api/boards/{self.b.pk}/update/", json.dumps({"workspace": nope.pk}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)


class TrelloImportTests(TestCase):
    EXPORT = {
        "name": "Shop fit-out", "desc": "From Trello",
        "labels": [{"id": "l1", "name": "Urgent", "color": "red_dark"}, {"id": "l2", "name": "", "color": "lime"}],
        "lists": [{"id": "a", "name": "To do", "pos": 1}, {"id": "b", "name": "Done", "pos": 2},
                  {"id": "z", "name": "Old", "pos": 3, "closed": True}],
        "members": [{"id": "m1", "fullName": "Wendy Ward", "username": "wendyw"}, {"id": "m2", "fullName": "Stranger", "username": "s"}],
        "customFields": [{"id": "f1", "name": "Priority", "type": "list", "options": [{"id": "o1", "value": {"text": "High"}}]},
                         {"id": "f2", "name": "Cost", "type": "number"}],
        "cards": [
            {"id": "c1", "name": "Order counters", "desc": "Oak", "idList": "a", "pos": 1, "due": "2026-11-05T17:00:00.000Z",
             "idLabels": ["l1"], "idMembers": ["m1", "m2"], "dueComplete": False,
             "customFieldItems": [{"idCustomField": "f1", "idValue": "o1"}, {"idCustomField": "f2", "value": {"number": "1200"}}],
             "attachments": [{"name": "Quote", "url": "https://example.com/quote.pdf"}]},
            {"id": "c2", "name": "Paint", "idList": "b", "pos": 2, "closed": True},
        ],
        "checklists": [{"id": "k1", "idCard": "c1", "name": "Steps", "pos": 1,
                        "checkItems": [{"name": "Measure", "state": "complete", "pos": 1}, {"name": "Book fitter", "state": "incomplete", "pos": 2, "idMember": "m1"}]}],
        "actions": [{"type": "commentCard", "idMemberCreator": "m2", "memberCreator": {"fullName": "Stranger"},
                     "date": "2026-10-01T09:00:00.000Z", "data": {"text": "Use the oak", "card": {"id": "c1"}}}],
    }

    def setUp(self):
        self.u = User.objects.create_user("ti@x.com", "pw")
        self.w = User.objects.create_user("wendy@x.com", "pw", first_name="Wendy", last_name="Ward")
        self.ws = Workspace.objects.create(name="Shops")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u, role="admin")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.w)
        self.client.force_login(self.u)

    def upload(self, data, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("board.json", json.dumps(data).encode(), content_type="application/json")
        return self.client.post("/boards/import/", {"workspace": self.ws.pk, "file": f, **extra}, follow=True)

    def test_import_everything(self):
        from .models import Attachment, ChecklistItem, Comment
        r = self.upload(self.EXPORT)
        b = Board.objects.get(title="Shop fit-out")
        self.assertContains(r, "Imported")
        self.assertContains(r, "Stranger")                       # told who wasn't found
        self.assertEqual(list(b.lists.order_by("position").values_list("title", "is_archived")),
                         [("To do", False), ("Done", False), ("Old", True)])
        c = Card.objects.get(title="Order counters")
        self.assertEqual(list(c.labels.values_list("name", "colour")), [("Urgent", "red")])
        self.assertEqual(list(c.members.all()), [self.w])
        self.assertTrue(BoardMember.objects.filter(board=b, user=self.w).exists())
        vals = {v.field.name: v.value for v in c.field_values.select_related("field")}
        self.assertEqual(vals, {"Priority": "High", "Cost": 1200})
        items = list(ChecklistItem.objects.filter(checklist__card=c).values_list("text", "is_done", "assignee"))
        self.assertEqual(items, [("Measure", True, None), ("Book fitter", False, self.w.pk)])
        self.assertIn("Stranger", Comment.objects.get(card=c).body)
        self.assertEqual(Attachment.objects.get(card=c).url, "https://example.com/quote.pdf")
        self.assertTrue(Card.objects.get(title="Paint").is_archived)
        self.assertEqual(b.labels.get(name="").colour, "green")   # lime → green

    def test_rejects_other_files(self):
        r = self.upload({"hello": 1})
        self.assertContains(r, "Export as JSON")
        self.assertFalse(Board.objects.exists())


class InstallableAppTests(TestCase):
    def test_service_worker_manifest_and_offline_page(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r["Content-Type"], "application/javascript")
        self.assertEqual(r["Service-Worker-Allowed"], "/")
        self.assertNotIn("__VERSION__", r.content.decode())
        m = self.client.get("/manifest.webmanifest").json()
        self.assertEqual((m["name"], m["display"]), ("Lanes", "standalone"))
        self.assertEqual(len(m["icons"]), 4)
        self.assertContains(self.client.get("/offline/"), "You're offline")
        u = User.objects.create_user("app@x.com", "pw")
        self.client.force_login(u)
        page = self.client.get("/").content.decode()
        self.assertIn('rel="manifest"', page)
        self.assertIn('data-sw="1"', page)
        self.assertIn("offline.js", page)


class WelcomeTests(TestCase):
    def test_welcome_shows_until_dismissed(self):
        u = User.objects.create_user("new@x.com", "pw", first_name="Nia")
        self.client.force_login(u)
        self.assertContains(self.client.get("/"), "Welcome to Lanes, Nia")
        self.client.post("/account/theme/", {"welcomed": "1"})
        self.assertNotContains(self.client.get("/"), "Welcome to Lanes")

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

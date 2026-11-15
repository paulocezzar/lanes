"""Simple mode — everyone starts with the everyday controls; "Show all features" brings the rest back."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from .models import Board, BoardMember, List, Workspace

User = get_user_model()


class SimpleModeTests(TestCase):
    def setUp(self):
        ws = Workspace.objects.create(name="Team")
        self.u = User.objects.create_user("s@x.com", "pw", first_name="Sam")
        self.b = Board.objects.create(workspace=ws, title="Plan")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        List.objects.create(board=self.b, title="To do")
        self.c = Client(); self.c.force_login(self.u)

    def full(self):
        self.c.post("/account/theme/", {"simple_mode": "0"})

    def test_new_people_start_simple(self):
        self.assertTrue(self.u.simple_mode)

    def test_simple_board_shows_two_views_and_the_rest_under_more_views(self):
        page = self.c.get(f"/b/{self.b.pk}/").content.decode()
        tabs = page.split('<nav class="view-tabs"')[1].split("</nav>")[0]
        self.assertIn("vt-board", tabs); self.assertIn("vt-calendar", tabs)
        for hidden in ("vt-table", "vt-timeline", "vt-swimlanes", "vt-gantt", "vt-dashboard", "vt-map"):
            self.assertNotIn(hidden, tabs)
        more = page.split("vt-more")[1].split("</details>")[0]
        for label in ("Table", "Timeline", "Swimlanes", "Gantt", "Dashboard", "Map"):
            self.assertIn(f">{label}</a>", more)          # still one click away

    def test_a_view_opened_from_more_views_gets_its_tab(self):
        page = self.c.get(f"/b/{self.b.pk}/table/").content.decode()
        tabs = page.split('<nav class="view-tabs"')[1].split("</nav>")[0]
        self.assertIn("vt-table on", tabs)
        self.assertTrue(User.objects.get(pk=self.u.pk).simple_mode)      # opening it doesn't switch modes

    def test_simple_header_names_every_control(self):
        page = self.c.get(f"/b/{self.b.pk}/").content.decode()
        head = page.split('<header class="board-head">')[1].split("</header>")[0]
        for label in ("Star this board", "Watch this board", "Switch boards", "Automation", "Board menu and settings"):
            self.assertIn(label, head)
        self.assertNotIn('class="icon-btn light switch-btn"', head)
        for attr in ("data-star", "data-watch-board", "data-open-drawer", "data-switch-boards"):
            self.assertEqual(head.count(attr), 1)           # the scripts bind to one of each
        self.assertIn("Collapse list", page); self.assertIn("Add a card from a template", page)

    def test_full_mode_is_the_board_as_it_was(self):
        self.full()
        page = self.c.get(f"/b/{self.b.pk}/").content.decode()
        head = page.split('<header class="board-head">')[1].split("</header>")[0]
        for key in ("board", "calendar", "table", "timeline", "swimlanes", "gantt", "dashboard", "map"):
            self.assertIn(f"vt-{key}", head)
        self.assertNotIn("vt-more", head); self.assertNotIn('class="menu head-more"', head)
        self.assertIn('class="icon-btn light switch-btn"', head)
        for attr in ("data-star", "data-watch-board", "data-open-drawer", "data-switch-boards"):
            self.assertEqual(head.count(attr), 1)


class LightBackgroundTests(TestCase):
    def setUp(self):
        ws = Workspace.objects.create(name="Team")
        self.u = User.objects.create_user("b@x.com", "pw")
        self.b = Board.objects.create(workspace=ws, title="Plan")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.c = Client(); self.c.force_login(self.u)

    def test_light_backgrounds_can_be_chosen_and_switch_the_header_to_dark_text(self):
        self.assertNotIn("bg-light", self.c.get(f"/b/{self.b.pk}/").content.decode().split("<main")[1].split(">")[0])
        for key in Board.LIGHT_BACKGROUNDS:
            self.assertIn(key, dict(Board.BACKGROUNDS))
            r = self.c.post(f"/api/boards/{self.b.pk}/update/", {"background": key}, content_type="application/json")
            self.assertEqual(r.status_code, 200)
            main = self.c.get(f"/b/{self.b.pk}/").content.decode().split("<main")[1].split(">")[0]
            self.assertIn(f"bg-{key} bg-light", main)

    def test_every_background_has_a_colour_in_the_stylesheet(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent / "static/lanes/app.css").read_text()
        for key, _ in Board.BACKGROUNDS:
            self.assertIn(f".bg-{key}", css)

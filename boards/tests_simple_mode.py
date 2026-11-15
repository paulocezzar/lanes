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
            self.assertIn(f">{label}<", more)             # still one click away

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


class StartPanelTests(TestCase):
    def setUp(self):
        from .models import Card
        ws = Workspace.objects.create(name="Team")
        self.u = User.objects.create_user("e@x.com", "pw")
        self.b = Board.objects.create(workspace=ws, title="Plan")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.todo = List.objects.create(board=self.b, title="To do", position=1000)
        List.objects.create(board=self.b, title="Done", position=2000)
        self.c = Client(); self.c.force_login(self.u)
        self.Card = Card

    def test_an_empty_board_asks_what_needs_doing(self):
        page = self.c.get(f"/b/{self.b.pk}/").content.decode()
        self.assertIn('id="start-panel"', page); self.assertIn("What needs doing?", page)
        self.assertIn(f'data-start-list="{self.todo.pk}"', page)          # cards go into the first list

    def test_the_panel_goes_once_the_board_has_a_card(self):
        r = self.c.post(f"/api/lists/{self.todo.pk}/cards/", {"title": "First thing", "smart_due": False},
                        content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('id="start-panel"', self.c.get(f"/b/{self.b.pk}/").content.decode())

    def test_someone_who_can_only_look_is_not_asked(self):
        viewer = User.objects.create_user("v@x.com", "pw")
        BoardMember.objects.create(board=self.b, user=viewer, role="observer")
        c = Client(); c.force_login(viewer)
        self.assertNotIn('id="start-panel"', c.get(f"/b/{self.b.pk}/").content.decode())


class ListViewTests(TestCase):
    def setUp(self):
        from .models import Card, Checklist, ChecklistItem
        ws = Workspace.objects.create(name="Team")
        self.u = User.objects.create_user("l@x.com", "pw")
        self.b = Board.objects.create(workspace=ws, title="Plan")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.todo = List.objects.create(board=self.b, title="To do", position=1000)
        self.done = List.objects.create(board=self.b, title="Finished", position=2000)
        self.card = Card.objects.create(list=self.todo, title="Ring the supplier", position=1000)
        ChecklistItem.objects.create(checklist=Checklist.objects.create(card=self.card), text="Find the number")
        Card.objects.create(list=self.done, title="Old job", position=1000, is_complete=True)
        Card.objects.create(list=self.todo, title="Put away", position=2000, is_archived=True)
        self.c = Client(); self.c.force_login(self.u)

    def test_cards_are_rows_grouped_under_their_lists(self):
        r = self.c.get(f"/b/{self.b.pk}/list/")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        body = page.split("data-listview")[1].split("</main>")[0]
        self.assertLess(body.index("To do"), body.index("Ring the supplier"))
        self.assertLess(body.index("Ring the supplier"), body.index("Finished"))
        self.assertLess(body.index("Finished"), body.index("Old job"))
        self.assertIn(f'data-card-open="{self.card.pk}"', body); self.assertIn("0/1", body)
        self.assertIn('lv-row done"', body)               # the finished one shows as ticked
        self.assertNotIn("Put away", body)                         # archived cards stay out
        self.assertIn(f'data-lv-add="{self.todo.pk}"', body)       # a box to type a new card under each list
        self.assertIn("vt-list on", page)

    def test_people_and_due_can_be_set_from_the_row(self):
        body = self.c.get(f"/b/{self.b.pk}/list/").content.decode().split("data-listview")[1].split("</main>")[0]
        self.assertIn('data-lv-edit="people"', body); self.assertIn('data-lv-edit="due"', body)
        self.assertIn('data-lv-pop="people"', body); self.assertIn(f'data-lv-person="{self.u.pk}"', body)
        # the two calls the row makes
        r = self.c.post(f"/api/cards/{self.card.pk}/members/", {"user_id": self.u.pk}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        r = self.c.post(f"/api/cards/{self.card.pk}/update/", {"due_at": "2027-01-15T17:00"}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        body = self.c.get(f"/b/{self.b.pk}/list/").content.decode().split("data-listview")[1].split("</main>")[0]
        self.assertIn(f'data-members="{self.u.pk} "', body); self.assertIn('data-due="2027-01-15"', body); self.assertIn("15 Jan", body)

    def test_an_empty_board_asks_what_needs_doing_on_the_list_view_too(self):
        empty = Board.objects.create(workspace=self.b.workspace, title="Blank")
        BoardMember.objects.create(board=empty, user=self.u, role="admin")
        List.objects.create(board=empty, title="To do", position=1000)
        self.assertIn('id="start-panel"', self.c.get(f"/b/{empty.pk}/list/").content.decode())
        self.assertNotIn('id="start-panel"', self.c.get(f"/b/{self.b.pk}/list/").content.decode())

    def test_list_is_one_of_the_simple_tabs_and_the_rest_explain_themselves(self):
        page = self.c.get(f"/b/{self.b.pk}/").content.decode()
        tabs = page.split('<nav class="view-tabs"')[1].split("</nav>")[0]
        for key in ("vt-board", "vt-list", "vt-calendar"):
            self.assertIn(key, tabs)
        more = page.split("vt-more")[1].split("</details>")[0]
        self.assertIn("<small>Every card in a table you can sort</small>", more)

    def test_someone_who_can_only_look_gets_no_add_box_and_a_locked_tick(self):
        viewer = User.objects.create_user("w@x.com", "pw")
        BoardMember.objects.create(board=self.b, user=viewer, role="observer")
        c = Client(); c.force_login(viewer)
        body = c.get(f"/b/{self.b.pk}/list/").content.decode().split("data-listview")[1].split("</main>")[0]
        self.assertNotIn("data-lv-add", body); self.assertIn("disabled", body)


class PlainBackgroundTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Team")
        from .models import WorkspaceMember
        self.u = User.objects.create_user("pl@x.com", "pw")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u)
        self.c = Client(); self.c.force_login(self.u)

    def test_a_new_board_starts_plain_and_follows_the_theme(self):
        self.assertEqual(Board.BACKGROUNDS[0][0], "plain")          # first in the picker, so the create form's default
        r = self.c.post("/boards/new/", {"workspace": self.ws.pk, "title": "Fresh"})
        b = Board.objects.get(title="Fresh")
        self.assertEqual(b.background, "plain")
        main = lambda: self.c.get(f"/b/{b.pk}/").content.decode().split("<main")[1].split(">")[0]
        self.assertIn("bg-plain", main()); self.assertNotIn("bg-light", main())      # theme not chosen: the page script decides
        self.c.post("/account/theme/", {"theme": "light"})
        self.assertIn("bg-plain bg-light", main())
        self.c.post("/account/theme/", {"theme": "dark"})
        self.assertNotIn("bg-light", main())

    def test_existing_boards_keep_their_background(self):
        b = Board.objects.create(workspace=self.ws, title="Old", background="dusk")
        BoardMember.objects.create(board=b, user=self.u, role="admin")
        self.assertIn("bg-dusk", self.c.get(f"/b/{b.pk}/").content.decode().split("<main")[1].split(">")[0])


class ExploreBarTests(ListViewTests):
    """The tips bar is offered on the three everyday views, to people who can edit."""

    def test_tips_bar_is_on_board_list_and_calendar_only(self):
        for path, shown in (("", True), ("list/", True), ("calendar/", True), ("table/", False)):
            body = self.c.get(f"/b/{self.b.pk}/{path}").content.decode()
            self.assertEqual('id="explore-bar"' in body, shown, path)
        self.assertIn("lanes/explore.js", body)
        self.assertIn("data-show-tips", body)


class BoardLookTests(ListViewTests):
    """The board: an Add card button in the header, and people / date buttons on each card."""

    def test_board_has_the_header_button_and_card_pickers(self):
        body = self.c.get(f"/b/{self.b.pk}/").content.decode()
        self.assertIn("data-head-add", body)
        self.assertIn('data-tile-pick="people"', body); self.assertIn('data-tile-pick="due"', body)
        self.assertIn('data-tile-pop="people"', body)
        self.u.simple_mode = False; self.u.save()          # the look is the same with all features showing
        body = self.c.get(f"/b/{self.b.pk}/").content.decode()
        self.assertIn("data-head-add", body); self.assertIn('data-tile-pick="people"', body)


class CardFieldsTests(ListViewTests):
    """Choose-one fields show as coloured chips on the card and can be set from it; dates can be a range."""

    def test_dropdown_field_shows_as_a_coloured_chip(self):
        from datetime import date
        from boards.models import CustomField
        r = self.c.post(f"/api/boards/{self.b.pk}/fields/", {"name": "Priority", "kind": "dropdown", "options": ["Critical", "High", "Low"], "show_on_card": True}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        f = CustomField.objects.get(board=self.b, name="Priority")
        self.assertEqual([f.option_colour(o) for o in f.options], ["red", "orange", "yellow"])
        r = self.c.post(f"/api/cards/{self.card.pk}/fields/", {"field_id": f.pk, "value": "High"}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.c.post(f"/api/cards/{self.card.pk}/update/", {"start_date": "2027-01-11", "due_at": "2027-01-15T17:00"}, content_type="application/json")
        body = self.c.get(f"/b/{self.b.pk}/").content.decode()
        self.assertIn(f'class="tile-label tile-field lc-orange" data-tile-field="{f.pk}"', body)
        self.assertIn("11 Jan – 15 Jan", body)
        self.assertIn('id="field-dialog"', body); self.assertIn('id="field-data"', body); self.assertIn("data-tile-field-add", body)
        self.card.refresh_from_db(); self.assertEqual(self.card.start_date, date(2027, 1, 11))
        # each option can be given its own colour; ones left alone keep the colour of their place in the list
        r = self.c.post(f"/api/boards/{self.b.pk}/fields/", {"field_id": f.pk, "name": "Priority", "options": ["Critical", "High", "Low"], "colours": {"High": "purple", "Low": "not-a-colour"}}, content_type="application/json")
        self.assertEqual(r.status_code, 200); f.refresh_from_db()
        self.assertEqual([f.option_colour(o) for o in f.options], ["red", "purple", "yellow"])
        self.assertIn("lc-purple", self.c.get(f"/b/{self.b.pk}/").content.decode())
        self.assertIn("data-dr-grid", body)

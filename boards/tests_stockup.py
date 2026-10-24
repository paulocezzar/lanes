"""The StockUp link, against a fake StockUp that answers like the real API."""
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from .models import Board, BoardMember, Card, StockUpLink, StockUpPerson, Workspace, WorkspaceMember

User = get_user_model()
DAY = date.today() + timedelta(days=3)


class FakeStockUp:
    def __init__(self):
        self.runs, self.jobs, self.next = {}, {}, 100
        self.plan = {"id": 7, "name": "Christmas 2026", "status": "locked", "season": "christmas",
                     "start": None, "end": None, "department": "Bakery"}

    def item(self):
        return {"owner": "requirement", "owner_id": 11, "name": "Mince Pie Case", "code": "NPD-R413",
                "unit": "each", "still_to_make": "4000", "already_scheduled": "0", "kind": "component"}

    def __call__(self, method, path, data=None, timeout=15):
        if path == "plans/":
            return {"plans": [self.plan]}
        if path == "plans/7/":
            return {"plan": self.plan, "items": [self.item()],
                    "products": [{"id": 3, "name": "Mince Pies Pack/6", "quantity": "2000", "pack_size": "Pack/6",
                                  "launch": DAY.isoformat()}],
                    "components": [{"id": 11, "name": "Mince Pie Case", "code": "NPD-R413", "unit": "each",
                                    "required_qty": "4000", "progress_pct": "25", "state": "scheduled",
                                    "earliest": None, "latest": DAY.isoformat(), "used_in": []}],
                    "runs": [r for r in self.runs.values() if r["state"] != "cancelled"]}
        if path == "people/":
            return {"people": [{"id": 5, "name": "Una Baker", "role": "", "department": "Bakery", "email": "u@x.com"}]}
        if path == "runs/" and method == "POST":
            self.next += 1
            r = {"id": self.next, "seq": 1, "plan_id": 7, "owner": data["owner"], "owner_id": data["owner_id"],
                 "name": "Mince Pie Case", "code": "NPD-R413", "unit": data["unit"], "planned_qty": data["qty"],
                 "planned_date": data["date"] or None, "state": "scheduled", "made_qty": "0", "batch_codes": [], "notes": ""}
            self.runs[r["id"]] = r
            return r
        if path.startswith("runs/?ids="):
            ids = [int(i) for i in path.split("=")[1].split(",")]
            return {"runs": [self.runs[i] for i in ids if i in self.runs], "missing": []}
        if path.startswith("runs/"):
            r = self.runs[int(path.split("/")[1])]
            if data.get("cancel"):
                r["state"] = "cancelled"
            if "planned_date" in data:
                r["planned_date"] = data["planned_date"] or None
            if data.get("planned_qty"):
                r["planned_qty"] = data["planned_qty"]
            return r
        if path == "jobs/" and method == "POST":
            self.next += 1
            j = {"id": self.next, "name": data["name"], "archived": False, "date": data["date"],
                 "person_id": data["person_id"], "person": "Una Baker", "status": "open", "done_by": "", "done_at": None}
            self.jobs[j["id"]] = j
            return j
        if path.startswith("jobs/?ids="):
            ids = [int(i) for i in path.split("=")[1].split(",")]
            return {"jobs": [self.jobs[i] for i in ids if i in self.jobs]}
        if path.startswith("jobs/"):
            j = self.jobs[int(path.split("/")[1])]
            if data.get("archive"):
                j["archived"] = True
            if data.get("date"):
                j["date"] = data["date"]
            return j
        raise AssertionError(path)


@override_settings(STOCKUP_URL="https://stockup.example", STOCKUP_TOKEN="s" * 40)
class StockUpLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        self.fake = FakeStockUp()
        self.p = patch("boards.stockup._call", self.fake)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.ws = Workspace.objects.create(name="Bakery", stockup_department="Bakery")
        self.u = User.objects.create_user("u@x.com", "pw-12345", first_name="Una", last_name="Baker")
        self.boss = User.objects.create_superuser("boss@x.com", "pw")
        WorkspaceMember.objects.create(workspace=self.ws, user=self.u, role="admin")
        self.b = Board.objects.create(workspace=self.ws, title="Christmas")
        BoardMember.objects.create(board=self.b, user=self.u, role="admin")
        self.todo = self.b.lists.create(title="To do", position=1)
        self.b.lists.create(title="Done", position=2)
        self.card = self.todo.cards.create(title="Mince pie cases", position=1)
        self.client.force_login(self.u)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def link(self):
        return self.post(f"/api/cards/{self.card.pk}/stockup/", {"plan_id": 7, "owner": "requirement", "owner_id": 11,
                                                                 "unit": "each", "qty": "2000", "date": DAY.isoformat()})

    def test_picker_and_schedule_a_run_then_batch_completes_the_card(self):
        self.assertEqual(self.client.get("/api/stockup/").json()["plans"][0]["name"], "Christmas 2026")
        self.assertEqual(self.client.get("/api/stockup/?plan=7").json()["items"][0]["code"], "NPD-R413")
        r = self.link()
        self.assertEqual(r.status_code, 200, r.content)
        link = StockUpLink.objects.get(card=self.card)
        run = self.fake.runs[link.run_id]
        self.assertEqual((run["planned_qty"], run["planned_date"]), ("2000", DAY.isoformat()))
        self.card.refresh_from_db()
        self.assertEqual(self.card.due_at.date(), DAY)
        self.assertContains(self.client.get(f"/cards/{self.card.pk}/"), "Mince Pie Case")
        # first batch: half made
        run.update(made_qty="1000", batch_codes=["SU 26300/NPD-R413"])
        r = self.client.post("/internal/stockup-ping/", json.dumps({"runs": [link.run_id]}),
                             content_type="application/json", HTTP_AUTHORIZATION="Bearer " + "s" * 40)
        self.assertEqual(r.json()["updated"], 1)
        self.card.refresh_from_db()
        self.assertFalse(self.card.is_complete)
        self.assertEqual(self.card.work_pct(), 50)
        # confirmed: card completes and moves to Done
        run.update(made_qty="2000", state="confirmed", batch_codes=["SU 26300/NPD-R413", "SU 26301/NPD-R413"])
        from .stockup import sync
        sync()
        self.card.refresh_from_db()
        self.assertTrue(self.card.is_complete)
        self.assertEqual(self.card.list.title, "Done")

    def test_due_date_moves_the_run_and_archive_cancels_it(self):
        self.link()
        link = StockUpLink.objects.get(card=self.card)
        new = DAY + timedelta(days=1)
        self.post(f"/api/cards/{self.card.pk}/update/", {"due_at": f"{new.isoformat()}T09:00"})
        self.assertEqual(self.fake.runs[link.run_id]["planned_date"], new.isoformat())
        self.post(f"/api/cards/{self.card.pk}/update/", {"archive": True})
        self.assertEqual(self.fake.runs[link.run_id]["state"], "cancelled")
        self.assertFalse(StockUpLink.objects.filter(card=self.card).exists())

    def test_adding_a_linked_person_gives_them_the_job(self):
        from .stockup import match_people
        self.assertEqual(match_people(), 1)
        self.link()
        self.post(f"/api/cards/{self.card.pk}/members/", {"user_id": self.u.pk})
        link = StockUpLink.objects.get(card=self.card)
        self.assertTrue(link.job_id)
        self.assertEqual(self.fake.jobs[link.job_id]["date"], DAY.isoformat())
        self.post(f"/api/cards/{self.card.pk}/members/", {"user_id": self.u.pk})   # removed again
        self.assertTrue(self.fake.jobs[link.job_id]["archived"] if link.job_id in self.fake.jobs else True)

    def test_ping_needs_the_key(self):
        r = self.client.post("/internal/stockup-ping/", "{}", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(r.status_code, 404)

    def test_a_board_from_a_plan(self):
        self.link()                                      # one run exists on the plan
        self.client.force_login(self.boss)
        self.assertContains(self.client.get("/stockup/"), "Christmas 2026")
        r = self.client.post("/stockup/plans/7/board/", {"workspace": self.ws.pk})
        board = Board.objects.get(stockup_plan_id=7)
        self.assertRedirects(r, f"/b/{board.pk}/", fetch_redirect_response=False)
        titles = set(Card.objects.filter(list__board=board).values_list("title", flat=True))
        self.assertIn("Mince Pies Pack/6", titles)
        self.assertIn("Mince Pie Case (NPD-R413)", titles)
        run_card = Card.objects.get(list__board=board, stockup__kind="run")
        self.assertEqual(run_card.parent.title, "Mince Pie Case (NPD-R413)")
        from .stockup import refresh_plan_board
        self.assertEqual(refresh_plan_board(board), 0)    # nothing new, nothing duplicated
        self.assertEqual(Card.objects.filter(list__board=board).count(), 3)

    @override_settings(STOCKUP_URL="", STOCKUP_TOKEN="")
    def test_off_when_not_configured(self):
        self.assertContains(self.client.get("/stockup/"), "Not connected")
        self.assertNotContains(self.client.get(f"/cards/{self.card.pk}/"), "data-su-form")


    def test_only_departments_that_use_stockup_see_it(self):
        other = Workspace.objects.create(name="Kitchen")
        cook = User.objects.create_user("c@x.com", "pw-12345", first_name="Cook")
        WorkspaceMember.objects.create(workspace=other, user=cook, role="admin")
        kb = Board.objects.create(workspace=other, title="Menus")
        BoardMember.objects.create(board=kb, user=cook, role="admin")
        card = kb.lists.create(title="To do", position=1).cards.create(title="Soup", position=1)
        self.client.force_login(cook)
        self.assertEqual(self.client.get("/stockup/").status_code, 404)
        self.assertNotContains(self.client.get("/"), "StockUp plans")
        self.assertNotContains(self.client.get(f"/cards/{card.pk}/"), "data-su-form")
        self.assertEqual(self.client.get(f"/api/stockup/?board={kb.pk}").status_code, 404)
        r = self.post(f"/api/cards/{card.pk}/stockup/", {"plan_id": 7, "owner": "requirement", "owner_id": 11,
                                                        "unit": "each", "qty": "5"})
        self.assertEqual(r.status_code, 403)
        # a plan for another StockUp department isn't offered even to the bakery
        self.client.force_login(self.u)
        self.fake.plan["department"] = "Butchery"
        cache.clear()
        self.assertEqual(self.client.get(f"/api/stockup/?board={self.b.pk}").json()["plans"], [])
        # the department admin switches it on
        self.client.force_login(cook)
        self.client.post(f"/departments/{other.pk}/settings/", {"action": "stockup", "stockup_department": "Butchery"})
        self.assertEqual(self.client.get("/stockup/").status_code, 200)


    def test_refresh_keeps_dates_set_by_hand(self):
        from datetime import datetime
        from django.utils import timezone
        from .stockup import build_plan_board, refresh_plan_board
        self.fake.plan["end"] = (DAY + timedelta(days=60)).isoformat()
        board = build_plan_board(7, self.boss, self.ws)
        card = Card.objects.get(list__board=board, stockup__kind="product")
        mine = timezone.make_aware(datetime(2026, 11, 30, 17))
        Card.objects.filter(pk=card.pk).update(due_at=mine)
        refresh_plan_board(board)
        card.refresh_from_db()
        self.assertEqual(card.due_at, mine)
        r = self.post(f"/api/cards/{card.pk}/stockup/", {"refresh": True})
        self.assertEqual(r.status_code, 200)

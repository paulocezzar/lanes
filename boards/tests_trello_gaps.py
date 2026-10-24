"""Tests for the features that answer common Trello complaints: parts, item → card, sign-offs,
request forms, early warnings, swimlanes, "or when" rules, plain-words search, health and adoption."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import (Activity, Board, BoardMember, Card, Checklist, ChecklistItem, CustomField, ErrorReport, Label,
                     List, Notification, RequestForm, Rule, SignOff, Workspace, WorkspaceMember)

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Dept")
        self.boss = User.objects.create_superuser("boss@x.com", "pw", first_name="Bo")
        self.u = User.objects.create_user("u@x.com", "pw-12345", first_name="Una")
        self.v = User.objects.create_user("v@x.com", "pw-12345", first_name="Vic")
        for x in (self.u, self.v):
            WorkspaceMember.objects.create(workspace=self.ws, user=x)
        self.board = Board.objects.create(workspace=self.ws, title="Jobs")
        BoardMember.objects.create(board=self.board, user=self.u, role="admin")
        BoardMember.objects.create(board=self.board, user=self.v)
        self.todo = List.objects.create(board=self.board, title="To do", position=1)
        self.done = List.objects.create(board=self.board, title="Done", position=2)
        self.card = Card.objects.create(list=self.todo, title="Big job")
        self.client.force_login(self.u)

    def post(self, url, data=None):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json")


class PartsTests(Base):
    def test_item_becomes_a_part_and_ticks_itself(self):
        cl = Checklist.objects.create(card=self.card, title="Steps")
        item = ChecklistItem.objects.create(checklist=cl, text="Order 40 kg flour", assignee=self.v,
                                            due_at=timezone.now() + timedelta(days=2), qty_target=40, qty_unit="kg")
        r = self.post(f"/api/items/{item.pk}/to-card/")
        self.assertEqual(r.status_code, 200, r.content)
        new = Card.objects.get(pk=r.json()["new_card"]["id"])
        self.assertEqual(new.title, "Order 40 kg flour")
        self.assertEqual(new.parent, self.card)
        self.assertEqual(list(new.members.all()), [self.v])
        self.assertEqual(new.due_at, item.due_at)
        self.assertEqual(ChecklistItem.objects.get(checklist__card=new).qty_target, 40)
        self.assertIn("↗ card", r.json()["modal"])
        # completing the new card ticks the original item
        self.post(f"/api/cards/{new.pk}/update/", {"is_complete": True})
        item.refresh_from_db()
        self.assertTrue(item.is_done)
        self.assertEqual(item.qty_done, 40)
        self.post(f"/api/cards/{new.pk}/update/", {"is_complete": False})
        item.refresh_from_db()
        self.assertFalse(item.is_done)
        self.assertEqual(self.post(f"/api/items/{item.pk}/to-card/").status_code, 400)   # only once

    def test_children_progress_and_no_loops(self):
        r = self.post(f"/api/cards/{self.card.pk}/children/", {"title": "Part A", "list_id": self.todo.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.post(f"/api/cards/{self.card.pk}/children/", {"title": "Part B"})
        a = Card.objects.get(title="Part A")
        self.assertEqual(self.card.child_progress(), {"done": 0, "total": 2, "pct": 0})
        self.post(f"/api/cards/{a.pk}/update/", {"is_complete": True})
        self.assertEqual(self.card.child_progress()["done"], 1)
        # the big job can't be put inside its own part
        r = self.post(f"/api/cards/{self.card.pk}/parent/", {"parent_id": a.pk})
        self.assertEqual(r.status_code, 400)
        # an existing card can join, and leave
        other = Card.objects.create(list=self.done, title="Loose")
        self.post(f"/api/cards/{other.pk}/parent/", {"parent_id": self.card.pk})
        self.assertEqual(Card.objects.get(pk=other.pk).parent, self.card)
        self.post(f"/api/cards/{other.pk}/parent/", {"parent_id": None})
        self.assertIsNone(Card.objects.get(pk=other.pk).parent)
        self.assertIn("Part A", self.client.get(f"/cards/{self.card.pk}/").content.decode())

    def test_lookup_only_shows_visible_cards(self):
        hidden_ws = Workspace.objects.create(name="Other")
        hidden = Board.objects.create(workspace=hidden_ws, title="Secret", visibility="private")
        Card.objects.create(list=List.objects.create(board=hidden, title="L"), title="Big secret")
        r = self.client.get("/api/cards/lookup/?q=big")
        titles = [c["title"] for c in r.json()["cards"]]
        self.assertIn("Big job", titles)
        self.assertNotIn("Big secret", titles)


class SignOffTests(Base):
    def test_request_block_approve(self):
        r = self.post(f"/api/cards/{self.card.pk}/signoffs/", {"approver_id": self.v.pk, "ask": "Check labels"})
        self.assertEqual(r.status_code, 200, r.content)
        so = SignOff.objects.get(card=self.card)
        self.assertTrue(Notification.objects.filter(recipient=self.v, kind="signoff").exists())
        r = self.post(f"/api/cards/{self.card.pk}/update/", {"is_complete": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Vic", r.json()["error"])
        # only the approver can decide
        self.assertEqual(self.post(f"/api/signoffs/{so.pk}/", {"action": "approve"}).status_code, 403)
        self.client.force_login(self.v)
        self.assertEqual(self.post(f"/api/signoffs/{so.pk}/", {"action": "reject"}).status_code, 400)   # needs a note
        self.post(f"/api/signoffs/{so.pk}/", {"action": "reject", "note": "Wrong date on label"})
        self.assertEqual(SignOff.objects.get(pk=so.pk).status, "rejected")
        self.assertTrue(Notification.objects.filter(recipient=self.u, kind="signoff", excerpt__icontains="Wrong").exists())
        self.post(f"/api/signoffs/{so.pk}/", {"action": "approve", "note": "Fixed"})
        self.client.force_login(self.u)
        r = self.post(f"/api/cards/{self.card.pk}/update/", {"is_complete": True})
        self.assertEqual(r.status_code, 200)

    def test_rule_complete_respects_signoff_and_rule_can_ask(self):
        from . import automation
        Rule.objects.create(board=self.board, trigger=Rule.MOVED, trigger_list=self.done,
                            actions=[{"type": "request_signoff", "user": self.v.pk}, {"type": "complete"}])
        self.post(f"/api/cards/{self.card.pk}/move/", {"list_id": self.done.pk})
        self.card.refresh_from_db()
        self.assertFalse(self.card.is_complete)
        self.assertTrue(SignOff.objects.filter(card=self.card, approver=self.v, status="pending").exists())
        self.assertIn("sign it off", automation.describe_action(self.board, {"type": "request_signoff", "user": self.v.pk}))


class FormTests(Base):
    def make_form(self, who="staff"):
        field = CustomField.objects.create(board=self.board, name="Site", kind="text")
        lab = Label.objects.create(board=self.board, name="Request", colour="blue")
        return RequestForm.objects.create(
            board=self.board, list=self.todo, title="Maintenance", token="tok123", who=who, label=lab,
            assignee=self.v, questions=[
                {"label": "What's broken?", "kind": "short", "to": "title", "required": True, "options": []},
                {"label": "Details", "kind": "long", "to": "description", "required": False, "options": []},
                {"label": "Needed by", "kind": "date", "to": "due", "required": False, "options": []},
                {"label": "Where", "kind": "choice", "to": f"field:{field.pk}", "required": True,
                 "options": ["Bakery", "Shop"]},
            ])

    def test_staff_form_makes_card(self):
        f = self.make_form()
        self.client.logout()
        self.assertEqual(self.client.get("/f/tok123/").status_code, 302)          # needs sign-in
        outsider = User.objects.create_user("o@x.com", "pw-12345", first_name="Ola")
        self.client.force_login(outsider)
        self.assertContains(self.client.get("/f/tok123/"), "What&#x27;s broken?")
        r = self.client.post("/f/tok123/", {"a0": "", "a3": "Nowhere"})
        self.assertContains(r, "This one&#x27;s needed")
        self.assertContains(r, "Please pick one of the options")
        r = self.client.post("/f/tok123/", {"a0": "Oven door", "a1": "Won't shut", "a2": "2026-11-02", "a3": "Bakery"})
        self.assertContains(r, "Sent")
        card = Card.objects.get(title="Oven door")
        self.assertEqual(card.list, self.todo)
        self.assertIn("Won't shut", card.description)
        self.assertIn("Ola", card.description)
        self.assertEqual(timezone.localtime(card.due_at).date().isoformat(), "2026-11-02")
        self.assertEqual(card.field_values.get().value, "Bakery")
        self.assertEqual(list(card.members.all()), [self.v])
        self.assertEqual(card.labels.get().name, "Request")
        self.assertTrue(Notification.objects.filter(recipient=self.v, kind="form").exists())
        self.assertEqual(RequestForm.objects.get(pk=f.pk).submissions, 1)

    def test_public_form_needs_name_and_honeypot(self):
        self.make_form(who="anyone")
        self.client.logout()
        r = self.client.post("/f/tok123/", {"a0": "Leak", "a3": "Shop"})
        self.assertContains(r, "tell us your name")
        self.client.post("/f/tok123/", {"a0": "Spam", "a3": "Shop", "your_name": "Bot", "website": "x"})
        self.assertFalse(Card.objects.filter(title="Spam").exists())
        self.client.post("/f/tok123/", {"a0": "Leak", "a3": "Shop", "your_name": "Sam", "your_email": "s@y.com"})
        card = Card.objects.get(title="Leak")
        self.assertIn("Sam (s@y.com)", card.description)
        self.assertIsNone(card.created_by)

    def test_builder_saves_and_closes(self):
        r = self.client.post(f"/b/{self.board.pk}/forms/", {
            "title": "Orders", "list": self.todo.pk, "who": "staff",
            "q_label_0": "What?", "q_kind_0": "short", "q_to_0": "title", "q_required_0": "1",
            "q_label_1": "When?", "q_kind_1": "short", "q_to_1": "due",          # due needs a date → description
            "q_label_2": "Size", "q_kind_2": "choice", "q_to_2": "description", "q_options_2": "S\nM\nL"})
        self.assertEqual(r.status_code, 302)
        f = RequestForm.objects.get(title="Orders")
        self.assertEqual([q["to"] for q in f.questions], ["title", "description", "description"])
        self.assertEqual(f.questions[2]["options"], ["S", "M", "L"])
        self.client.post(f"/b/{self.board.pk}/forms/", {"toggle": f.pk})
        self.assertFalse(RequestForm.objects.get(pk=f.pk).is_open)
        self.assertContains(self.client.get(f"/f/{f.token}/"), "isn't taking requests")
        self.assertContains(self.client.get(f"/b/{self.board.pk}/forms/"), "Orders")


class RiskTests(Base):
    def test_reasons_and_warning_once(self):
        from . import risk
        now = timezone.now()
        self.card.due_at = now + timedelta(hours=20)
        self.card.save()
        self.card.members.add(self.v)
        cl = Checklist.objects.create(card=self.card)
        ChecklistItem.objects.create(checklist=cl, text="a", is_done=True)
        for t in "bcd":
            ChecklistItem.objects.create(checklist=cl, text=t)
        self.assertIn("25%", risk.reason(Card.objects.get(pk=self.card.pk), now))
        self.assertEqual(risk.warn(now), 1)
        self.assertEqual(risk.warn(now), 0)                       # once per due date
        n = Notification.objects.get(kind="risk")
        self.assertEqual(n.recipient, self.v)
        # far-off and complete cards are fine
        self.card.due_at = now + timedelta(days=10)
        self.card.save()
        self.assertEqual(risk.reason(Card.objects.get(pk=self.card.pk), now), "")

    def test_waiting_for_later_dependency(self):
        from . import risk
        from .models import Dependency
        now = timezone.now()
        self.card.due_at = now + timedelta(days=2)
        self.card.save()
        self.card.members.add(self.v)
        dep = Card.objects.create(list=self.todo, title="Supplier delivery", due_at=now + timedelta(days=4))
        Dependency.objects.create(card=self.card, depends_on=dep)
        self.assertIn("Supplier delivery", risk.reason(Card.objects.get(pk=self.card.pk), now))
        tile = self.client.get(f"/b/{self.board.pk}/").content.decode()
        self.assertIn("At risk", tile)


class RulesAlsoTests(Base):
    def test_or_when_trigger_and_new_filters(self):
        r = self.client.post(f"/b/{self.board.pk}/automation/rules/", {
            "section": "rules", "trigger": "card_moved", "trigger_list": self.done.pk, "by_whom": "anyone",
            "also": json.dumps([{"trigger": "card_completed"}, {"trigger": "nonsense"}]),
            "filters": json.dumps([{"type": "checklists_done"}]),
            "action_0": "add_sticker", "sticker_0": "🎉"})
        self.assertEqual(r.status_code, 302)
        rule = Rule.objects.get(board=self.board)
        self.assertEqual(rule.also, [{"trigger": "card_completed"}])
        from . import automation
        when, _ = automation.describe_rule(rule)
        self.assertIn("or when a card is marked complete", when)
        self.assertIn("every checklist item done", when)
        cl = Checklist.objects.create(card=self.card)
        ChecklistItem.objects.create(checklist=cl, text="x", is_done=True)
        self.post(f"/api/cards/{self.card.pk}/update/", {"is_complete": True})     # the "or" trigger
        self.assertIn("🎉", Card.objects.get(pk=self.card.pk).stickers)

    def test_field_is_filter(self):
        from . import automation
        f = CustomField.objects.create(board=self.board, name="Type", kind="dropdown", options=["Rush", "Normal"])
        rule = Rule.objects.create(board=self.board, trigger=Rule.COMPLETED,
                                   filters=[{"type": "field_is", "field": f.pk, "value": "rush"}],
                                   actions=[{"type": "add_sticker", "sticker": "⚡"}])
        self.assertFalse(automation.filters_ok(rule, self.card))
        from .models import CardFieldValue
        CardFieldValue.objects.create(card=self.card, field=f, value="Rush")
        self.assertTrue(automation.filters_ok(rule, self.card))


class SwimlaneSearchTests(Base):
    def test_swimlanes_and_lane_drag(self):
        self.card.members.add(self.u)
        r = self.client.get(f"/b/{self.board.pk}/swimlanes/")
        self.assertContains(r, "Una")
        self.assertContains(r, "Big job")
        r = self.post(f"/api/cards/{self.card.pk}/lane/", {"by": "member", "from": str(self.u.pk), "to": str(self.v.pk)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(self.card.members.all()), [self.v])
        lab = Label.objects.create(board=self.board, name="Hot", colour="red")
        self.post(f"/api/cards/{self.card.pk}/lane/", {"by": "label", "from": "none", "to": str(lab.pk)})
        self.assertEqual(list(self.card.labels.all()), [lab])
        self.assertContains(self.client.get(f"/b/{self.board.pk}/swimlanes/?by=label"), "Hot")

    def test_plain_words(self):
        from . import search
        self.assertEqual(search.from_words("my overdue cards about packaging"), "@me is:overdue packaging")
        self.assertEqual(search.from_words("due next week"), "is:due-next-week")
        self.assertEqual(search.from_words("label:urgent oven"), "label:urgent oven")
        self.card.members.add(self.u)
        self.card.due_at = timezone.now() - timedelta(hours=2)
        self.card.save()
        r = self.client.get("/search/?q=my+overdue+cards")
        self.assertContains(r, "Big job")
        self.assertContains(r, "@me is:overdue")
        cl = Checklist.objects.create(card=self.card)
        ChecklistItem.objects.create(checklist=cl, text="Order sultanas")
        self.assertContains(self.client.get("/search/?q=sultanas"), "found in checklist")
        SignOff.objects.create(card=self.card, approver=self.v)
        self.assertContains(self.client.get("/search/?q=waiting+for+sign-off"), "Big job")


class HealthTests(Base):
    def test_errors_grouped_and_admins_told_once(self):
        import logging
        log = logging.getLogger("boards.test")
        for _ in range(3):
            try:
                raise ValueError("boom")
            except ValueError:
                log.exception("it broke")
        rep = ErrorReport.objects.get()
        self.assertEqual(rep.count, 3)
        self.assertEqual(Notification.objects.filter(recipient=self.boss, kind="system").count(), 1)
        self.client.force_login(self.boss)
        r = self.client.get("/company/health/")
        self.assertContains(r, "boom")
        self.assertContains(r, "Download everything")
        self.client.post("/company/health/", {"resolve": rep.pk})
        self.assertIsNotNone(ErrorReport.objects.get().resolved_at)
        self.assertEqual(self.client.get("/company/health/").status_code, 200)
        self.client.force_login(self.u)
        self.assertEqual(self.client.get("/company/health/").status_code, 404)

    def test_adoption_dept_drilldown(self):
        Activity.objects.create(board=self.board, actor=self.u, verb="card_created")
        self.client.force_login(self.boss)
        r = self.client.get(f"/company/?dept={self.ws.pk}")
        self.assertContains(r, "who's using Lanes")
        self.assertContains(r, "Vic")
        self.assertContains(r, "Tools in use")

    def test_digest_includes_signoffs_and_new_users_get_daily(self):
        from django.core import mail
        from django.test import override_settings

        from . import mailer
        self.assertEqual(User.objects.create_user("n@x.com", "pw-12345").email_notifications, "daily")
        SignOff.objects.create(card=self.card, approver=self.v, requested_by=self.u, ask="Taste test")
        with override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            now = timezone.now().replace(hour=9)
            mailer.daily_digests(now)
            mailer.flush()
            body = next(m.body for m in mail.outbox if m.to == ["v@x.com"])
        self.assertIn("WAITING FOR YOUR SIGN-OFF", body)
        self.assertIn("Big job", body)

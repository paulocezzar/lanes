"""Automation engine: rules (event → actions) and schedules (recurring cards).

Rules fire from the API after a user's action. Actions run directly on the
models and never fire further rules, so automations can't loop.

Schedules are claimed atomically (UPDATE … WHERE next_run_at = old value) so
two workers or a cron + a web request can never create the same card twice.
They run from `manage.py run_automations` (for a cron job) and, as a
fallback, lazily at most once a minute when anyone uses the app.
"""
import calendar
import logging
import time
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from . import positions
from .models import (Activity, BoardMember, Card, Checklist, ChecklistItem, Comment, Label, List, Rule,
                     Schedule)

log = logging.getLogger(__name__)
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------- rules ----------

def _by_whom_ok(rule, actor):
    """'by me' = the person who made the rule; 'by anyone except me' = everyone else."""
    if rule.by_whom == "me":
        return actor is not None and actor.pk == rule.created_by_id
    if rule.by_whom == "others":
        return actor is not None and actor.pk != rule.created_by_id
    return True


# Events that also count as other triggers: a card added to a list is also
# "added to the board", and changes how many cards that list has.
ALSO_FIRES = {Rule.ADDED: [Rule.BOARD_ADDED, Rule.LIST_COUNT], Rule.MOVED: [Rule.LIST_COUNT]}


def filters_ok(rule, card):
    """Trello's 'Advanced' conditions: every filter on the rule must hold."""
    for f in rule.filters or []:
        t = f.get("type")
        if t == "label" and not card.labels.filter(pk=f.get("label")).exists():
            return False
        if t == "no_label" and card.labels.filter(pk=f.get("label")).exists():
            return False
        if t == "has_due" and not card.due_at:
            return False
        if t == "no_due" and card.due_at:
            return False
        if t == "member" and not card.members.filter(pk=f.get("user")).exists():
            return False
        if t == "complete" and not card.is_complete:
            return False
        if t == "incomplete" and card.is_complete:
            return False
        if t == "in_list" and card.list_id != f.get("list"):
            return False
        if t == "no_member" and card.members.exists():
            return False
        if t in ("checklists_done", "checklists_open"):
            items = ChecklistItem.objects.filter(checklist__card=card)
            done = items.exists() and not items.filter(is_done=False).exists()
            if done != (t == "checklists_done"):
                return False
        if t == "overdue" and not (card.due_at and not card.is_complete and card.due_at < timezone.now()):
            return False
        if t in ("signed_off", "awaiting_signoff"):
            sos = list(card.signoffs.all())
            ok = bool(sos) and all(s.status == "approved" for s in sos)
            if (t == "signed_off") != ok or (t == "awaiting_signoff" and not sos):
                return False
        if t == "field_is":
            from .models import CardFieldValue
            v = CardFieldValue.objects.filter(card=card, field_id=f.get("field")).values_list("value", flat=True).first()
            want = str(f.get("value", "")).strip().lower()
            have = "" if v in (None, False) else ("yes" if v is True else str(v)).strip().lower()
            if have != want:
                return False
    return True


class _Alt:
    """One of a rule's extra "or when…" triggers, shaped like a Rule for _matches_event / describe_rule."""
    def __init__(self, rule, data):
        self.board, self.created_by, self.created_by_id = rule.board, rule.created_by, rule.created_by_id
        self.trigger = data.get("trigger")
        self.trigger_list_id = data.get("trigger_list")
        self.trigger_label_id = data.get("trigger_label")
        self.trigger_field_id = data.get("trigger_field")
        self.trigger_count = data.get("trigger_count")
        self.trigger_me = data.get("member_who") == "me"
        self.trigger_hours = self.trigger_weekday = None
        self.by_whom, self.filters = "anyone", []

    @property
    def trigger_list(self):
        return List.objects.filter(pk=self.trigger_list_id).first() if self.trigger_list_id else None

    @property
    def trigger_label(self):
        return Label.objects.filter(pk=self.trigger_label_id).first() if self.trigger_label_id else None

    @property
    def trigger_field(self):
        from .models import CustomField
        return CustomField.objects.filter(pk=self.trigger_field_id).first() if self.trigger_field_id else None


def alternatives(rule):
    return [_Alt(rule, a) for a in (rule.also or []) if isinstance(a, dict) and a.get("trigger") in Rule.ALSO_OK]


def _matches_event(rule, card, *, list_, label, member, field, mentioned):
    t = rule.trigger
    if t in (Rule.ADDED, Rule.MOVED):
        return list_ is not None and rule.trigger_list_id == list_.pk
    if t == Rule.LIST_COUNT:
        return (list_ is not None and rule.trigger_list_id == list_.pk
                and list_.cards.filter(is_archived=False).count() == rule.trigger_count)
    if t == Rule.LABEL_ADDED:
        return label is not None and rule.trigger_label_id == label.pk
    if t == Rule.FIELD_SET:
        return field is not None and rule.trigger_field_id in (None, field.pk)
    if t == Rule.MEMBER_ADDED and rule.trigger_me:
        return member is not None and member.pk == rule.created_by_id
    if t == Rule.MENTIONED:
        ids = {u.pk for u in mentioned}
        return rule.created_by_id in ids if rule.trigger_me else bool(ids)
    return True


def fire(card, trigger, *, list_=None, label=None, actor=None, member=None, field=None, mentioned=()):
    """Run every enabled rule on the card's board that matches this event.
    `actor` is whoever did it (None for the system); `member` is who was
    added (member_added); `field` the custom field set; `mentioned` the
    people @mentioned in a comment."""
    board = card.list.board
    triggers = [trigger, *ALSO_FIRES.get(trigger, [])]
    rules = [r for r in Rule.objects.filter(board=board, is_enabled=True).select_related("board", "created_by")
             if r.trigger in triggers or any(a.get("trigger") in triggers for a in (r.also or []) if isinstance(a, dict))]
    ev = dict(list_=list_, label=label, member=member, field=field, mentioned=mentioned)
    for rule in rules:
        if card.is_archived and trigger != Rule.ARCHIVED:
            continue
        if not _by_whom_ok(rule, actor):
            continue
        ways = ([rule] if rule.trigger in triggers else []) + [a for a in alternatives(rule) if a.trigger in triggers]
        if not any(_matches_event(w, card, **ev) for w in ways):
            continue
        if not filters_ok(rule, card):
            continue
        _run_rule(rule, card)


def _run_rule(rule, card):
    board = rule.board
    try:
        with transaction.atomic():
            done = [d for d in (run_action(card, a) for a in rule.actions) if d]
            rule.run_count += 1
            rule.last_run_at = timezone.now()
            rule.save(update_fields=["run_count", "last_run_at"])
            if done:
                act = Activity.objects.create(board=board, card=card, actor=None, verb="automation",
                                              data={"summary": "; ".join(done), "rule": rule.pk})
                from . import notify
                notify.on_activity(act)
    except Exception:  # a broken rule must never break the user's own action
        log.exception("Automation rule %s failed on card %s", rule.pk, card.pk)


def fire_list_created(lst, actor):
    """'When a list is created': runs board actions; "new" means the list just made."""
    board = lst.board
    for rule in Rule.objects.filter(board=board, trigger=Rule.LIST_CREATED, is_enabled=True):
        if not _by_whom_ok(rule, actor):
            continue
        actions = [{k: (lst.pk if v == "new" else v) for k, v in a.items()} for a in rule.actions]
        try:
            with transaction.atomic():
                done = run_board_actions(board, actions, actor)
                Rule.objects.filter(pk=rule.pk).update(run_count=F("run_count") + 1, last_run_at=timezone.now())
                if done:
                    Activity.objects.create(board=board, actor=None, verb="automation",
                                            data={"summary": "; ".join(done), "rule": rule.pk})
        except Exception:
            log.exception("List-created rule %s failed", rule.pk)


def run_action(card, action):
    """Apply one action. Returns a short past-tense description, or '' if nothing changed."""
    t = action.get("type")
    board = card.list.board
    if t == "move":
        target = List.objects.filter(pk=action.get("list"), board=board, is_archived=False).first()
        if target and target.pk != card.list_id:
            card.list = target
            card.position = positions.end_position(target.cards.filter(is_archived=False))
            card.save(update_fields=["list", "position", "updated_at"])
            if action.get("position") == "top":
                _to_top(card)
            return f"moved to {target.title}"
    elif t == "move_top":
        top = min((p for _, p in positions._entry_positions(card.list, exclude=card.token)), default=None)
        if top is not None and top <= card.position:
            card.position = positions.between(None, top)
            card.save(update_fields=["position", "updated_at"])
            return "moved to the top"
    elif t == "add_sticker":
        from .models import STICKERS
        s = action.get("sticker")
        if s in STICKERS and s not in (card.stickers or []) and len(card.stickers or []) < 6:
            card.stickers = [*(card.stickers or []), s]
            card.save(update_fields=["stickers", "updated_at"])
            return f"added sticker {s}"
    elif t in ("complete", "incomplete"):
        want = t == "complete"
        if card.is_complete != want:
            from .features import after_completion_change, blocking_signoffs
            if want and blocking_signoffs(card):
                return "left it open — it still needs signing off"
            card.is_complete = want
            card.save(update_fields=["is_complete", "updated_at"])
            after_completion_change(card)
            return "marked complete" if want else "marked incomplete"
    elif t == "check_all":
        items = ChecklistItem.objects.filter(checklist__card=card, is_done=False)
        n = items.count()
        if n:
            for item in items:
                item.is_done, item.done_at = True, timezone.now()
                item.save(update_fields=["is_done", "done_at"])
            return f"ticked {n} checklist item{'s' if n != 1 else ''}"
    elif t in ("add_label", "remove_label"):
        label = Label.objects.filter(pk=action.get("label"), board=board).first()
        if label:
            has = card.labels.filter(pk=label.pk).exists()
            if t == "add_label" and not has:
                card.labels.add(label)
                return f"added label {label}"
            if t == "remove_label" and has:
                card.labels.remove(label)
                return f"removed label {label}"
    elif t == "add_member":
        m = BoardMember.objects.filter(board=board, user_id=action.get("user")).select_related("user").first()
        if m and not card.members.filter(pk=m.user_id).exists():
            card.members.add(m.user)
            from . import notify
            notify.send(m.user, "assigned", card=card, text=f"assigned you to {card.title} (automation)")
            return f"added {m.user.display_name}"
    elif t == "request_signoff":
        from .models import SignOff
        m = BoardMember.objects.filter(board=board, user_id=action.get("user")).select_related("user").first()
        if m and not SignOff.objects.filter(card=card, approver=m.user, status="pending").exists():
            from .features import request_signoff
            request_signoff(card, m.user, None)
            return f"asked {m.user.display_name} to sign it off"
    elif t == "clear_members":
        if card.members.exists():
            card.members.clear()
            return "removed all members"
    elif t == "set_due":
        try:
            days = max(0, min(int(action.get("days", 0)), 365))
        except (TypeError, ValueError):
            days = 0
        local = timezone.localtime() + timedelta(days=days)
        card.due_at = local.replace(hour=17, minute=0, second=0, microsecond=0)
        card.save(update_fields=["due_at", "updated_at"])
        return f"set due date to {timezone.localtime(card.due_at).strftime('%-d %b')}"
    elif t == "set_due_workdays":
        try:
            days = max(0, min(int(action.get("days", 0)), 260))
        except (TypeError, ValueError):
            days = 0
        card.due_at = add_working_days(timezone.localtime(), days).replace(hour=17, minute=0, second=0, microsecond=0)
        card.save(update_fields=["due_at", "updated_at"])
        return f"set due date to {timezone.localtime(card.due_at).strftime('%a %-d %b')}"
    elif t == "add_checklist":
        title = (action.get("title") or "Checklist").strip()[:120] or "Checklist"
        lines = [ln.strip()[:250] for ln in (action.get("text") or "").splitlines() if ln.strip()][:50]
        if not card.checklists.filter(title=title).exists():
            last = card.checklists.order_by("-position").first()
            cl = Checklist.objects.create(card=card, title=title,
                                          position=(last.position if last else 0) + positions.GAP)
            for i, line in enumerate(lines, start=1):
                ChecklistItem.objects.create(checklist=cl, text=line, position=i * positions.GAP)
            return f"added checklist “{title}”"
    elif t == "clear_due":
        if card.due_at:
            card.due_at = None
            card.save(update_fields=["due_at", "updated_at"])
            return "removed the due date"
    elif t == "comment":
        text = (action.get("text") or "").strip()[:2000]
        if text:
            Comment.objects.create(card=card, author=None, body=text)
            return "posted a comment"
    elif t == "notify":
        from . import notify
        when = timezone.localtime(card.due_at).strftime("%a %-d %b, %H:%M") if card.due_at else ""
        msg = (action.get("text") or "").strip() or (f"{card.title} is due {when}" if when else f"{card.title} needs attention")
        people = list(card.members.all()) or ([card.created_by] if card.created_by else [])
        sent = 0
        for u in people:
            if notify.send(u, "automation", card=card, text=msg[:300] if msg.startswith(card.title) else f"{card.title}: {msg}"[:300]):
                sent += 1
        if sent:
            return f"notified {sent} {'person' if sent == 1 else 'people'}"
    elif t == "archive":
        if not card.is_archived:
            card.is_archived = True
            card.save(update_fields=["is_archived", "updated_at"])
            return "archived the card"
    elif t == "move_bottom":
        end = positions.end_position(card.list.cards.filter(is_archived=False).exclude(pk=card.pk))
        last = max((p for _, p in positions._entry_positions(card.list, exclude=card.token)), default=None)
        if last is not None and last > card.position:
            card.position = end
            card.save(update_fields=["position", "updated_at"])
            return "moved to the bottom"
    elif t == "copy":
        target = List.objects.filter(pk=action.get("list"), board=board, is_archived=False).first()
        if target:
            from .copying import copy_card
            new = copy_card(card, target, user=None)
            if action.get("position") == "top":
                _to_top(new)
            return f"copied to {target.title}"
    elif t == "remove_member":
        if card.members.filter(pk=action.get("user")).exists():
            u = card.members.get(pk=action.get("user"))
            card.members.remove(u)
            return f"removed {u.display_name}"
    elif t == "set_field":
        from .models import CardFieldValue, CustomField
        field = CustomField.objects.filter(pk=action.get("field"), board=board).first()
        if field:
            try:
                value = field.clean_value(action.get("value"))
            except (TypeError, ValueError):
                return ""
            if value is None:
                if CardFieldValue.objects.filter(card=card, field=field).delete()[0]:
                    return f"cleared {field.name}"
            else:
                CardFieldValue.objects.update_or_create(card=card, field=field, defaults={"value": value})
                return f"set {field.name} to {action.get('value')}"
    elif t == "sort_list":
        how = action.get("by") or "due"
        far = timezone.now() + timedelta(days=36500)
        keys = {"due": lambda c: (c.due_at or far, c.position), "name": lambda c: (c.title.lower(), c.position),
                "newest": lambda c: (-c.pk,), "oldest": lambda c: (c.pk,)}
        cards = sorted(card.list.cards.filter(is_archived=False), key=keys.get(how, keys["due"]))
        for i, c in enumerate(cards, start=1):
            Card.objects.filter(pk=c.pk).update(position=i * positions.GAP)
        card.refresh_from_db(fields=["position"])
        if cards:
            return f"sorted {card.list.title} by {dict(SORT_BY).get(how, how)}"
    elif t == "cascade":
        target = List.objects.filter(pk=action.get("list"), board=board, is_archived=False).first()
        if target:
            items = ChecklistItem.objects.filter(checklist__card=card, is_done=False).order_by("checklist__position", "position")
            n = 0
            for it in items:
                Card.objects.create(list=target, title=it.text[:250], created_by=None,
                                    position=positions.end_position(target.cards.filter(is_archived=False)))
                n += 1
            if n:
                return f"made {n} card{'s' if n != 1 else ''} in {target.title} from its checklist"
    elif t == "rename":
        title = _fill(action.get("text"), card)[:250].strip()
        if title and title != card.title:
            card.title = title
            card.save(update_fields=["title", "updated_at"])
            return f"renamed to “{title}”"
    elif t == "set_description":
        text = _fill(action.get("text"), card)[:20000]
        if text != card.description:
            card.description = text
            card.save(update_fields=["description", "updated_at"])
            return "updated the description"
    elif t == "set_start":
        try:
            days = max(0, min(int(action.get("days", 0)), 365))
        except (TypeError, ValueError):
            days = 0
        start = timezone.localdate() + timedelta(days=days)
        if card.start_date != start:
            card.start_date = start
            card.save(update_fields=["start_date", "updated_at"])
            return f"set start date to {start:%-d %b}"
    return ""


SORT_BY = [("due", "due date"), ("name", "card name"), ("newest", "newest first"), ("oldest", "oldest first")]


def _fill(text, card):
    """{cardname} and {date} placeholders in rename / description actions."""
    text = (text or "").replace("{cardname}", card.title).replace("{date}", timezone.localtime().strftime("%-d %b %Y"))
    return text


def _to_top(card):
    top = min((p for _, p in positions._entry_positions(card.list, exclude=card.token)), default=None)
    if top is not None:
        card.position = positions.between(None, top)
        card.save(update_fields=["position"])


def add_working_days(start, days):
    """`days` working days (Mon–Fri) after `start`. 0 = today, or Monday if it's the weekend."""
    d = start
    while d.weekday() >= 5:
        d += timedelta(days=1)
    left = days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def checklists_all_done(card):
    items = ChecklistItem.objects.filter(checklist__card=card)
    return items.exists() and not items.filter(is_done=False).exists()


def describe_rule(rule):
    when = dict(Rule.TRIGGERS)[rule.trigger]
    if rule.trigger == Rule.DUE_SOON:
        when = when.replace("within…", f"within {dict(Rule.HOURS).get(rule.trigger_hours, f'{rule.trigger_hours} hours')}")
    if rule.trigger == Rule.OVERDUE:
        when = ("a card becomes due" if not rule.trigger_hours else
                f"a card is {dict(Rule.AFTER).get(rule.trigger_hours, f'{rule.trigger_hours} hours after').replace(' after', '')} overdue")
    if rule.trigger in (Rule.ADDED, Rule.MOVED):
        when = when.replace("list…", f"list “{rule.trigger_list.title if rule.trigger_list else '?'}”")
    if rule.trigger == Rule.LABEL_ADDED:
        when = when.replace("label …", f"label “{rule.trigger_label or '?'}”")
    if rule.trigger == Rule.MEMBER_ADDED and rule.trigger_me:
        name = rule.created_by.display_name if rule.created_by else "the rule's creator"
        when = f"{name} is added to a card"
    if rule.trigger == Rule.MENTIONED and rule.trigger_me:
        name = rule.created_by.display_name if rule.created_by else "the rule's creator"
        when = f"{name} is mentioned in a comment"
    if rule.trigger == Rule.FIELD_SET:
        when = when.replace("…", f"“{rule.trigger_field.name}”" if rule.trigger_field else "(any)")
    if rule.trigger == Rule.LIST_COUNT:
        when = (f"list “{rule.trigger_list.title if rule.trigger_list else '?'}” has exactly "
                f"{rule.trigger_count} card{'s' if rule.trigger_count != 1 else ''}")
    if rule.trigger == Rule.DUE_WEEKDAY:
        when = (f"it's {rule.trigger_hours or 9:02d}:00 on the {WEEKDAYS[rule.trigger_weekday or 0]} "
                f"before a card is due")
    if rule.trigger not in Rule.TIME_TRIGGERS and rule.by_whom != "anyone":
        name = rule.created_by.display_name if rule.created_by else "?"
        when += f" by {name}" if rule.by_whom == "me" else f" by anyone except {name}"
    for alt in alternatives(rule) if isinstance(rule, Rule) else []:
        when += " or when " + describe_rule(alt)[0].removeprefix("When ")
    if rule.filters:
        when += ", " + " and ".join(describe_filter(rule.board, f) for f in rule.filters)
    if rule.trigger == Rule.LIST_CREATED:
        return f"When {when}", [x.replace("?", "the new list") for x in
                                 describe_board_actions(rule.board, [{k: (None if v == "new" else v) for k, v in a.items()}
                                                                     for a in rule.actions])]
    if isinstance(rule, _Alt):
        return f"When {when}", []
    return f"When {when}", [describe_action(rule.board, a) for a in rule.actions]


def describe_filter(board, f):
    t = f.get("type")
    if t in ("label", "no_label"):
        lab = Label.objects.filter(pk=f.get("label"), board=board).first()
        return f"{'with' if t == 'label' else 'without'} the “{lab or '?'}” label"
    if t == "member":
        m = BoardMember.objects.filter(board=board, user_id=f.get("user")).select_related("user").first()
        return f"with {m.user.display_name if m else '(removed member)'} on it"
    if t == "in_list":
        lst = List.objects.filter(pk=f.get("list"), board=board).first()
        return f"in list “{lst.title if lst else '?'}”"
    if t == "field_is":
        from .models import CustomField
        fld = CustomField.objects.filter(pk=f.get("field"), board=board).first()
        return f"where “{fld.name if fld else '?'}” is “{f.get('value', '')}”"
    return {"has_due": "with a due date", "no_due": "without a due date", "complete": "that is complete",
            "incomplete": "that isn't complete", "checklists_done": "with every checklist item done",
            "checklists_open": "with checklist items still to do", "overdue": "that is overdue",
            "signed_off": "that has been signed off", "awaiting_signoff": "still waiting for sign-off",
            "no_member": "with nobody on it"}.get(t, t)


def describe_action(board, a):
    t = a.get("type")
    name = dict(Rule.ACTIONS).get(t, t)
    if t == "move":
        lst = List.objects.filter(pk=a.get("list"), board=board).first()
        return f"move it to “{lst.title if lst else '(deleted list)'}”"
    if t in ("add_label", "remove_label"):
        lab = Label.objects.filter(pk=a.get("label"), board=board).first()
        return name.replace("label…", f"label “{lab or '(deleted label)'}”")
    if t == "add_member":
        m = BoardMember.objects.filter(board=board, user_id=a.get("user")).select_related("user").first()
        return f"add {m.user.display_name if m else '(removed member)'}"
    if t == "request_signoff":
        m = BoardMember.objects.filter(board=board, user_id=a.get("user")).select_related("user").first()
        return f"ask {m.user.display_name if m else '(removed member)'} to sign it off"
    if t == "set_due":
        return f"set due date to {a.get('days', 0)} day(s) from now, 17:00"
    if t == "set_due_workdays":
        return f"set due date to {a.get('days', 0)} working day(s) from now, 17:00"
    if t == "add_checklist":
        n = len([ln for ln in (a.get("text") or "").splitlines() if ln.strip()])
        return f"add checklist “{a.get('title') or 'Checklist'}”" + (f" ({n} items)" if n else "")
    if t == "comment":
        return f"post comment “{a.get('text', '')}”"
    if t == "add_sticker":
        return f"add sticker {a.get('sticker', '')}"
    if t == "notify":
        return f"notify its members: “{a['text']}”" if a.get("text") else "notify its members"
    if t in ("copy", "cascade") or (t == "move" and a.get("position") == "top"):
        lst = List.objects.filter(pk=a.get("list"), board=board).first()
        where = f"“{lst.title if lst else '(deleted list)'}”"
        if t == "move":
            return f"move it to the top of {where}"
        if t == "copy":
            return f"copy it to {'the top of ' if a.get('position') == 'top' else ''}{where}"
        return f"turn its checklist items into cards in {where}"
    if t == "remove_member":
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.filter(pk=a.get("user")).first()
        return f"remove {u.display_name if u else '(removed person)'}"
    if t == "set_field":
        from .models import CustomField
        f = CustomField.objects.filter(pk=a.get("field"), board=board).first()
        return f"set {f.name if f else '(deleted field)'} to “{a.get('value', '')}”"
    if t == "sort_list":
        return f"sort its list by {dict(SORT_BY).get(a.get('by'), 'due date')}"
    if t == "rename":
        return f"rename it to “{a.get('text', '')}”"
    if t == "set_description":
        return "set its description"
    if t == "set_start":
        return f"set start date to {a.get('days', 0)} day(s) from now"
    return name


# ---------- schedules ----------

def next_run(schedule, after):
    """Next local run time strictly after `after` (an aware datetime)."""
    tz = timezone.get_current_timezone()
    local = timezone.localtime(after, tz)
    day = local.date()
    for _ in range(400):
        candidate = timezone.make_aware(datetime(day.year, day.month, day.day, schedule.hour), tz)
        if candidate > after and _matches(schedule, day):
            return candidate
        day += timedelta(days=1)
    raise ValueError("No run date found")


def _matches(schedule, day):
    f = schedule.frequency
    if f == Schedule.DAILY:
        return True
    if f == Schedule.WEEKDAYS:
        return day.weekday() < 5
    if f == Schedule.WEEKLY:
        days = schedule_days(schedule)
        if day.weekday() not in days:
            return False
        n = max(1, schedule.interval or 1)
        if n == 1:
            return True
        anchor = schedule.anchor or day
        start = anchor - timedelta(days=anchor.weekday())
        return ((day - timedelta(days=day.weekday())) - start).days // 7 % n == 0
    if f == Schedule.MONTHLY:
        last = calendar.monthrange(day.year, day.month)[1]
        return day.day == min(schedule.month_day, last)
    if f == Schedule.MONTHLY_NTH:
        if day.weekday() != schedule.weekday:
            return False
        if schedule.nth == 5:       # last one in the month
            return day.day + 7 > calendar.monthrange(day.year, day.month)[1]
        return (day.day - 1) // 7 + 1 == schedule.nth
    if f == Schedule.YEARLY:
        if day.month != schedule.month:
            return False
        last = calendar.monthrange(day.year, day.month)[1]
        return day.day == min(schedule.month_day, last)
    return False


def schedule_days(s):
    try:
        days = sorted({int(x) for x in (s.weekdays or "").split(",") if x.strip() != ""} & set(range(7)))
    except ValueError:
        days = []
    return days or [s.weekday]


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def describe_schedule(s):
    days = _join([WEEKDAYS[d] for d in schedule_days(s)])
    weekly = f"Every {days}" if (s.interval or 1) == 1 else f"Every {s.interval} weeks on {days}"
    when = {
        Schedule.DAILY: "Every day",
        Schedule.WEEKDAYS: "Every weekday",
        Schedule.WEEKLY: weekly,
        Schedule.MONTHLY: f"On day {s.month_day} of every month",
        Schedule.MONTHLY_NTH: f"Every {dict(Schedule.NTH).get(s.nth, '?')} {WEEKDAYS[s.weekday]} of the month",
        Schedule.YEARLY: f"Every year on {s.month_day} {dict(Schedule.MONTHS).get(s.month, '?')}",
    }[s.frequency]
    return f"{when} at {s.hour:02d}:00"


def describe_schedule_job(s):
    """What a schedule does, e.g. 'create “Weekly report” in To do'."""
    if s.kind == Schedule.ACTIONS:
        return "; ".join(describe_board_actions(s.board, s.actions)) or "nothing"
    return f"create “{s.title}” in {s.target_list.title if s.target_list else '(deleted list)'}"


def run_due_schedules(now=None):
    """Create cards for every schedule that is due. Returns how many cards were made."""
    now = now or timezone.now()
    made = 0
    for s in Schedule.objects.filter(is_enabled=True, next_run_at__lte=now).select_related("board", "target_list"):
        new_next = next_run(s, now)
        # Claim it: only one caller can move next_run_at on from this exact value.
        if not Schedule.objects.filter(pk=s.pk, next_run_at=s.next_run_at).update(
                next_run_at=new_next, last_run_at=now, run_count=s.run_count + 1):
            continue
        if s.board.is_archived:
            continue
        try:
            with transaction.atomic():
                if s.kind == Schedule.ACTIONS:
                    done = run_board_actions(s.board, s.actions, None)
                    if done:
                        Activity.objects.create(board=s.board, actor=None, verb="automation",
                                                data={"summary": f"{describe_schedule(s)}: " + "; ".join(done)})
                    made += 1
                    continue
                lst = s.target_list
                if not lst or lst.is_archived:
                    continue
                create_from_schedule(s, lst)
                made += 1
        except Exception:
            log.exception("Schedule %s failed", s.pk)
    return made


def create_from_schedule(s, lst):
    card = Card.objects.create(list=lst, title=s.title, created_by=None,
                               position=positions.end_position(lst.cards.filter(is_archived=False)))
    lines = [ln.strip() for ln in s.checklist.splitlines() if ln.strip()]
    if lines:
        cl = Checklist.objects.create(card=card, title="Checklist", position=positions.GAP)
        ChecklistItem.objects.bulk_create([
            ChecklistItem(checklist=cl, text=t[:250], position=(i + 1) * positions.GAP) for i, t in enumerate(lines)])
        # bulk_create skips signals; bump the board once so viewers refresh
        from .signals import bump
        bump(lst.board_id)
    if s.member_id and BoardMember.objects.filter(board=lst.board, user_id=s.member_id).exists():
        card.members.add(s.member_id)
        from . import notify
        notify.send(s.member, "assigned", card=card, text=f"assigned you to {card.title} (recurring card)")
    if s.label_id:
        card.labels.add(s.label_id)
    if s.due_in_days is not None:
        local = timezone.localtime() + timedelta(days=s.due_in_days)
        card.due_at = local.replace(hour=17, minute=0, second=0, microsecond=0)
        card.save(update_fields=["due_at"])
    Activity.objects.create(board=lst.board, card=card, actor=None, verb="automation",
                            data={"summary": f"created this card ({describe_schedule(s).lower()})"})
    return card


_last_lazy_run = 0.0


def run_due_rules(now=None):
    """Time-based rules: 'a card is due within…' and 'a card becomes overdue'.
    Each rule runs once per card per due date (RuleFire), so it's safe to call
    every minute from several places at once."""
    from django.db import IntegrityError

    from .models import RuleFire
    now = now or timezone.now()
    ran = 0
    rules = Rule.objects.filter(trigger__in=Rule.TIME_TRIGGERS, is_enabled=True, board__is_archived=False)
    for rule in rules.select_related("board"):
        cards = Card.objects.filter(list__board=rule.board, list__is_archived=False, is_archived=False,
                                    is_template=False, is_complete=False, due_at__isnull=False)
        if rule.trigger == Rule.DUE_WEEKDAY:
            cards = cards.filter(due_at__gt=now, due_at__lte=now + timedelta(days=8))
            cards = [c for c in cards if _weekday_before(c.due_at, rule) <= now]
            cards = Card.objects.filter(pk__in=[c.pk for c in cards])
        elif rule.trigger == Rule.DUE_SOON:
            cards = cards.filter(due_at__gt=now, due_at__lte=now + timedelta(hours=rule.trigger_hours or 24))
        else:   # due date passed (+ an optional delay) — ignore cards long overdue when the rule was made
            after = timedelta(hours=rule.trigger_hours or 0)
            cards = cards.filter(due_at__lte=now - after,
                                 due_at__gte=max(now - after - timedelta(days=30), rule.created_at - after - timedelta(days=1)))
        fired = RuleFire.objects.filter(rule=rule, card=OuterRef("pk"), due_at=OuterRef("due_at"))
        for card in cards.select_related("list__board").exclude(Exists(fired)):
            if not filters_ok(rule, card):
                continue
            try:
                with transaction.atomic():
                    RuleFire.objects.create(rule=rule, card=card, due_at=card.due_at)
                    done = [d for d in (run_action(card, a) for a in rule.actions) if d]
                    Rule.objects.filter(pk=rule.pk).update(run_count=F("run_count") + 1, last_run_at=now)
                    if done:
                        act = Activity.objects.create(board=rule.board, card=card, actor=None, verb="automation",
                                                      data={"summary": "; ".join(done)})
                        from . import notify
                        notify.on_activity(act)
                    ran += 1
            except IntegrityError:
                continue          # another worker got there first
            except Exception:
                log.exception("Due-date rule %s failed on card %s", rule.pk, card.pk)
    return ran


def _weekday_before(due_at, rule):
    """'On the Monday before a card is due, at 09:00' → that moment (aware)."""
    tz = timezone.get_current_timezone()
    d = timezone.localtime(due_at, tz).date()
    back = (d.weekday() - (rule.trigger_weekday or 0)) % 7 or 7
    day = d - timedelta(days=back)
    return timezone.make_aware(datetime(day.year, day.month, day.day, rule.trigger_hours or 9), tz)


def maybe_run_schedules():
    """Cheap fallback so schedules and due reminders run even without a cron
    job: at most once a minute per process, piggybacking on page loads."""
    global _last_lazy_run
    if time.monotonic() - _last_lazy_run < 60:
        return
    _last_lazy_run = time.monotonic()
    try:
        run_due_schedules()
        run_due_rules()
        from .notify import send_due_reminders
        send_due_reminders()
    except Exception:
        log.exception("Lazy schedule run failed")


# ---------- buttons ----------

def press_card_button(button, card, user):
    """Run a card button's actions. Returns the list of things that changed.
    A press counts as the person's own action, so board rules then react to it
    (rules themselves never trigger further rules)."""
    before_list, before_done = card.list_id, card.is_complete
    before_labels = set(card.labels.values_list("pk", flat=True))
    before_checks = checklists_all_done(card)
    done, added_me = [], False
    for a in button.actions:
        if a.get("type") == "add_me":
            if BoardMember.objects.filter(board=card.list.board, user=user).exists() \
                    and not card.members.filter(pk=user.pk).exists():
                card.members.add(user)
                done.append(f"added {user.display_name}")
                added_me = True
            continue
        d = run_action(card, a)
        if d:
            done.append(d)
    _after_press(button, user, done, card=card)
    card.refresh_from_db()
    if card.is_archived:
        return done
    if card.list_id != before_list:
        fire(card, Rule.MOVED, list_=card.list, actor=user)
    if card.is_complete and not before_done:
        fire(card, Rule.COMPLETED, actor=user)
    for label in card.labels.exclude(pk__in=before_labels):
        fire(card, Rule.LABEL_ADDED, label=label, actor=user)
    if not before_checks and checklists_all_done(card):
        fire(card, Rule.CHECKLIST_DONE, actor=user)
    if added_me:
        fire(card, Rule.MEMBER_ADDED, actor=user, member=user)
    return done


def press_board_button(button, user):
    done = run_board_actions(button.board, button.actions, user)
    _after_press(button, user, done)
    return done


def run_board_actions(board, actions, user):
    """Board-wide actions (shared by board buttons and scheduled jobs)."""
    done = []
    for a in actions:
        t = a.get("type")
        if t == "create_card":
            lst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            title = (a.get("text") or "").strip()
            if lst and title:
                title = timezone.localtime().strftime(title) if "%" in title else title
                if a.get("unique") and lst.cards.filter(is_archived=False, title__iexact=title[:250]).exists():
                    continue
                card = Card.objects.create(list=lst, title=title[:250], created_by=user,
                                           position=positions.end_position(lst.cards.filter(is_archived=False)))
                done.append(f"created “{card.title}” in {lst.title}")
        elif t == "move_all":
            src = List.objects.filter(pk=a.get("from_list"), board=board).first()
            dst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            if src and dst and src.pk != dst.pk:
                moved = 0
                for c in src.cards.filter(is_archived=False).order_by("position"):
                    c.list = dst
                    c.position = positions.end_position(dst.cards.filter(is_archived=False))
                    c.save(update_fields=["list", "position", "updated_at"])
                    moved += 1
                if moved:
                    done.append(f"moved {moved} card{'s' if moved != 1 else ''} from {src.title} to {dst.title}")
        elif t == "archive_done":
            n = Card.objects.filter(list__board=board, is_complete=True, is_archived=False).update(
                is_archived=True, updated_at=timezone.now())
            if n:
                done.append(f"archived {n} complete card{'s' if n != 1 else ''}")
        elif t == "sort_due":
            lst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            if lst:
                far = timezone.now() + timedelta(days=36500)
                cards = sorted(lst.cards.filter(is_archived=False), key=lambda c: (c.due_at or far, c.position))
                for i, c in enumerate(cards, start=1):
                    Card.objects.filter(pk=c.pk).update(position=i * positions.GAP)
                if cards:
                    done.append(f"sorted {lst.title} by due date")
        elif t == "archive_list":
            lst = List.objects.filter(pk=a.get("list"), board=board).first()
            if lst:
                n = lst.cards.filter(is_archived=False).update(is_archived=True, updated_at=timezone.now())
                if n:
                    done.append(f"archived {n} card{'s' if n != 1 else ''} in {lst.title}")
        elif t == "move_random":
            import random
            src = List.objects.filter(pk=a.get("from_list"), board=board).first()
            dst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            if src and dst and src.pk != dst.pk:
                pool = list(src.cards.filter(is_archived=False))
                picked = random.sample(pool, min(int(a.get("count") or 1), len(pool)))
                for c in picked:
                    c.list = dst
                    c.position = positions.end_position(dst.cards.filter(is_archived=False))
                    c.save(update_fields=["list", "position", "updated_at"])
                if picked:
                    done.append(f"moved {len(picked)} random card{'s' if len(picked) != 1 else ''} from {src.title} to {dst.title}")
        elif t == "sort_field":
            from .models import CardFieldValue, CustomField
            lst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            field = CustomField.objects.filter(pk=a.get("field"), board=board).first()
            if lst and field:
                cards = list(lst.cards.filter(is_archived=False))
                vals = {v.card_id: v for v in CardFieldValue.objects.filter(field=field, card__in=cards)}

                def key(c):
                    v = vals.get(c.pk)
                    raw = v.sort_value() if v and hasattr(v, "sort_value") else (str(v.value) if v else None)
                    return raw
                have = [c for c in cards if key(c) not in (None, "")]
                empty = [c for c in cards if key(c) in (None, "")]
                have.sort(key=lambda c: (_num_or_text(key(c)), c.position), reverse=a.get("order") == "desc")
                for i, c in enumerate(have + sorted(empty, key=lambda c: c.position), start=1):
                    Card.objects.filter(pk=c.pk).update(position=i * positions.GAP)
                if cards:
                    done.append(f"sorted {lst.title} by {field.name}" + (" (high to low)" if a.get("order") == "desc" else ""))
        elif t == "copy_named":
            lst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            name = (a.get("text") or "").strip()
            src = (Card.objects.filter(list__board=board, title__iexact=name, is_archived=False)
                   .order_by("-is_template", "pk").first()) if name else None
            if lst and src:
                from .copying import copy_card
                new = copy_card(src, lst, user=user, keep_dates=False)
                if new.is_template:
                    Card.objects.filter(pk=new.pk).update(is_template=False)
                done.append(f"copied “{src.title}” to {lst.title}")
        elif t == "plan_days":
            src = List.objects.filter(pk=a.get("from_list"), board=board, is_archived=False).first()
            dst = List.objects.filter(pk=a.get("list"), board=board, is_archived=False).first()
            if src and dst and src.pk != dst.pk:
                from .production import plan_days
                n = plan_days(board, src, dst, jobs_per_day=int(a.get("count") or 3), user=user)
                done.append(f"planned {n} production day{'s' if n != 1 else ''} in {dst.title}")
        elif t == "create_list":
            name = (a.get("text") or "").strip()
            if name:
                name = timezone.localtime().strftime(name) if "%" in name else name
                last = board.lists.filter(is_archived=False).order_by("-position").first()
                lst = List.objects.create(board=board, title=name[:120],
                                          position=(last.position if last else 0) + positions.GAP)
                done.append(f"created list {lst.title}")
    from .signals import bump
    bump(board.pk)
    return done


def _num_or_text(v):
    """Sort numbers numerically, everything else alphabetically (numbers first)."""
    try:
        return (0, float(str(v).replace(",", "").replace("£", "")), "")
    except (TypeError, ValueError):
        return (1, 0, str(v).lower())


def _after_press(button, user, done, card=None):
    Button = type(button)
    Button.objects.filter(pk=button.pk).update(run_count=button.run_count + 1)
    if done:
        from .services import log as log_activity
        log_activity(button.board, user, "button", card=card, name=f"{button.icon} {button.name}".strip(),
                     summary="; ".join(done))


def describe_button_actions(button):
    return describe_board_actions(button.board, button.actions)


def describe_board_actions(board, actions):
    from .models import Button
    names = dict(Rule.ACTIONS) | dict(Button.BOARD_ACTIONS) | {"add_me": "add the person who pressed it"}
    out = []
    lists = {l.pk: l.title for l in board.lists.all()}
    labels = {l.pk: str(l) for l in board.labels.all()}
    for a in actions:
        t = a.get("type")
        text = names.get(t, t)
        if t == "move" or t == "sort_due":
            text = text.replace("list…", lists.get(a.get("list"), "?"))
        if t == "move_all":
            text = f"move every card from {lists.get(a.get('from_list'), '?')} to {lists.get(a.get('list'), '?')}"
        if t == "move_random":
            text = f"move {a.get('count', 1)} random card(s) from {lists.get(a.get('from_list'), '?')} to {lists.get(a.get('list'), '?')}"
        if t == "archive_list":
            text = f"archive every card in {lists.get(a.get('list'), '?')}"
        if t == "create_list":
            text = f"create a list called “{a.get('text')}”"
        if t == "sort_field":
            f = board.custom_fields.filter(pk=a.get("field")).first()
            text = (f"sort {lists.get(a.get('list'), '?')} by {f.name if f else '(deleted field)'}"
                    + (", high to low" if a.get("order") == "desc" else ", low to high"))
        if t == "create_card":
            text = (f"create {'a unique card' if a.get('unique') else ''} “{a.get('text')}” in "
                    f"{lists.get(a.get('list'), '?')}").replace("create  “", "create “")
        if t == "copy_named":
            text = f"find card “{a.get('text')}” and copy it to {lists.get(a.get('list'), '?')}"
        if t == "plan_days":
            text = (f"plan production days from {lists.get(a.get('from_list'), '?')} into "
                    f"{lists.get(a.get('list'), '?')}, {a.get('count', 3)} jobs a day")
        if t in dict(Rule.ACTIONS) and t not in ("add_label", "remove_label", "set_due", "set_due_workdays",
                                                  "add_checklist", "comment", "add_sticker", "move", "sort_due"):
            text = describe_action(board, a)
        if t in ("add_label", "remove_label"):
            text = text.replace("label…", f"label {labels.get(a.get('label'), '?')}")
        if t in ("set_due", "set_due_workdays"):
            text = text.replace("…", str(a.get("days", 0)))
        if t == "add_checklist":
            text = f"add checklist “{a.get('title') or 'Checklist'}”"
        if t == "comment":
            text = f"post comment “{a.get('text', '')[:40]}”"
        if t == "add_sticker":
            text = f"add sticker {a.get('sticker')}"
        out.append(text)
    return out

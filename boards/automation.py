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
from django.utils import timezone

from . import positions
from .models import (Activity, BoardMember, Card, Checklist, ChecklistItem, Comment, Label, List, Rule,
                     Schedule)

log = logging.getLogger(__name__)
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------- rules ----------

def fire(card, trigger, *, list_=None, label=None):
    """Run every enabled rule on the card's board that matches this event."""
    board = card.list.board
    rules = Rule.objects.filter(board=board, trigger=trigger, is_enabled=True)
    if trigger in (Rule.ADDED, Rule.MOVED):
        rules = rules.filter(trigger_list=list_)
    if trigger == Rule.LABEL_ADDED:
        rules = rules.filter(trigger_label=label)
    for rule in rules:
        if card.is_archived:
            break
        try:
            with transaction.atomic():
                done = [d for d in (run_action(card, a) for a in rule.actions) if d]
                rule.run_count += 1
                rule.last_run_at = timezone.now()
                rule.save(update_fields=["run_count", "last_run_at"])
                if done:
                    Activity.objects.create(board=board, card=card, actor=None, verb="automation",
                                            data={"summary": "; ".join(done)})
        except Exception:  # a broken rule must never break the user's own action
            log.exception("Automation rule %s failed on card %s", rule.pk, card.pk)


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
            return f"moved to {target.title}"
    elif t in ("complete", "incomplete"):
        want = t == "complete"
        if card.is_complete != want:
            card.is_complete = want
            card.save(update_fields=["is_complete", "updated_at"])
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
    elif t == "archive":
        if not card.is_archived:
            card.is_archived = True
            card.save(update_fields=["is_archived", "updated_at"])
            return "archived the card"
    return ""


def checklists_all_done(card):
    items = ChecklistItem.objects.filter(checklist__card=card)
    return items.exists() and not items.filter(is_done=False).exists()


def describe_rule(rule):
    when = dict(Rule.TRIGGERS)[rule.trigger]
    if rule.trigger in (Rule.ADDED, Rule.MOVED):
        when = when.replace("list…", f"list “{rule.trigger_list.title if rule.trigger_list else '?'}”")
    if rule.trigger == Rule.LABEL_ADDED:
        when = when.replace("label …", f"label “{rule.trigger_label or '?'}”")
    return f"When {when}", [describe_action(rule.board, a) for a in rule.actions]


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
    if t == "set_due":
        return f"set due date to {a.get('days', 0)} day(s) from now, 17:00"
    if t == "comment":
        return f"post comment “{a.get('text', '')}”"
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
        return day.weekday() == schedule.weekday
    if f == Schedule.MONTHLY:
        last = calendar.monthrange(day.year, day.month)[1]
        return day.day == min(schedule.month_day, last)
    return False


def describe_schedule(s):
    when = {
        Schedule.DAILY: "Every day",
        Schedule.WEEKDAYS: "Every weekday",
        Schedule.WEEKLY: f"Every {WEEKDAYS[s.weekday]}",
        Schedule.MONTHLY: f"On day {s.month_day} of every month",
    }[s.frequency]
    return f"{when} at {s.hour:02d}:00"


def run_due_schedules(now=None):
    """Create cards for every schedule that is due. Returns how many cards were made."""
    now = now or timezone.now()
    made = 0
    for s in Schedule.objects.filter(is_enabled=True, next_run_at__lte=now).select_related("target_list__board"):
        new_next = next_run(s, now)
        # Claim it: only one caller can move next_run_at on from this exact value.
        if not Schedule.objects.filter(pk=s.pk, next_run_at=s.next_run_at).update(
                next_run_at=new_next, last_run_at=now, run_count=s.run_count + 1):
            continue
        lst = s.target_list
        if lst.is_archived or lst.board.is_archived:
            continue
        try:
            with transaction.atomic():
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


def maybe_run_schedules():
    """Cheap fallback so schedules and due reminders run even without a cron
    job: at most once a minute per process, piggybacking on page loads."""
    global _last_lazy_run
    if time.monotonic() - _last_lazy_run < 60:
        return
    _last_lazy_run = time.monotonic()
    try:
        run_due_schedules()
        from .notify import send_due_reminders
        send_due_reminders()
    except Exception:
        log.exception("Lazy schedule run failed")

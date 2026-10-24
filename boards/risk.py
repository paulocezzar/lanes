"""Early warnings: cards that look likely to miss their due date, flagged before it slips.

A card is at risk (not merely overdue — that has its own red date) when it is due within a few days and
something says it won't make it:
- it waits for another card that isn't done and is due later (or has no date);
- its checklist or child cards are well behind for the time left;
- nobody is on it;
- its sign-off came back "changes needed";
- nothing has happened on it for a week.
"""
from datetime import timedelta

from django.utils import timezone

WINDOW_DAYS = 3
DONE_WORDS = ("done", "complete", "finished", "closed", "delivered", "shipped")


def reason(card, now=None):
    now = now or timezone.now()
    if not card.due_at or card.is_complete or card.is_archived or card.is_template:
        return ""
    if any(w in card.list.title.lower() for w in DONE_WORDS):
        return ""                                   # sitting in a "Done" list counts as done
    left = card.due_at - now
    if left <= timedelta(0) or left > timedelta(days=WINDOW_DAYS):
        return ""
    days = left.total_seconds() / 86400
    for d in card.dependencies.all():
        dep = d.depends_on
        if not dep.is_complete and not dep.is_archived and (dep.due_at is None or dep.due_at > card.due_at):
            return f"Waits for “{dep.title}”, which isn't due until after this" if dep.due_at else \
                f"Waits for “{dep.title}”, which has no date"
    for s in card.signoffs.all():
        if s.status == "rejected":
            return f"{s.approver.display_name} asked for changes before signing off"
    items = [i for cl in card.checklists.all() for i in cl.items.all()]
    kids = [c for c in card.children.all() if not c.is_archived]
    if items or kids:
        pct = 100 * card.work_fraction()          # the same % the card, Gantt and Portfolio show
        if (days <= 1 and pct < 75) or (days <= 2 and pct < 50) or pct < 25:
            return f"Only {pct:.0f}% done with {_left(left)} left"
    if days <= 2 and not card.members.all():
        return "Nobody is on this card"
    touched = max([card.updated_at] + [i.done_at for i in items if i.done_at])
    if now - touched > timedelta(days=7):
        return f"Nothing has happened on it for {(now - touched).days} days"
    return ""


def _left(delta):
    h = delta.total_seconds() / 3600
    return f"{h:.0f} hour{'s' if round(h) != 1 else ''}" if h < 36 else f"{h / 24:.0f} days"


def warn(now=None):
    """Tell the people on each at-risk card once (per due date). Called by the cron job."""
    from . import notify
    from .models import Card
    from .services import cards_qs
    now = now or timezone.now()
    cards = cards_qs().filter(due_at__gt=now, due_at__lte=now + timedelta(days=WINDOW_DAYS), is_complete=False,
                              is_archived=False, is_template=False, list__is_archived=False,
                              list__board__is_archived=False)
    sent = 0
    for c in cards:
        why = reason(c, now)
        if not why:
            continue
        people = list(c.members.all()) or ([c.created_by] if c.created_by else [])
        if not people:
            from .models import BoardMember
            people = [m.user for m in BoardMember.objects.filter(board=c.list.board, role="admin").select_related("user")]
        key = f"risk:{c.pk}:{int(c.due_at.timestamp())}"
        for u in people:
            if notify.send(u, "risk", card=c, text=f"{c.title} may miss its due date", excerpt=why, dedupe_key=key):
                sent += 1
    return sent

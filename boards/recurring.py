"""Recurring cards (the Dates → Recurring setting).

When a recurring card is marked complete, a fresh copy appears in the same
list with the next due date: same title, description, labels, members and
checklists (all unticked), same reminder. The completed card stays where it
is, so the history is kept.
"""
import calendar
from datetime import timedelta

from django.utils import timezone


def next_due(due, recur):
    local = timezone.localtime(due)
    if recur == "daily":
        nxt = local + timedelta(days=1)
    elif recur == "weekdays":
        nxt = local + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    elif recur == "weekly":
        nxt = local + timedelta(days=7)
    elif recur == "monthly":
        y, m = (local.year + (local.month // 12), local.month % 12 + 1)
        nxt = local.replace(year=y, month=m, day=min(local.day, calendar.monthrange(y, m)[1]))
    elif recur == "yearly":
        y = local.year + 1
        nxt = local.replace(year=y, day=min(local.day, calendar.monthrange(y, local.month)[1]))
    else:
        return None
    return nxt


def spawn_next(card, user):
    """Make the next card in the series. Returns it, or None."""
    if not card.recur or not card.due_at or card.is_template:
        return None
    from .copying import copy_card
    due = _next_ahead(card)
    lst = card.list
    if not due or lst.is_archived or _existing_next(card, due).exists():   # ticked, unticked, ticked again
        return None
    new = copy_card(card, lst, user=user, keep_dates=False, position=card.position + 0.5)
    new.due_at = due
    if card.start_date:
        new.start_date = card.start_date + (timezone.localtime(due).date() - timezone.localtime(card.due_at).date())
    new.recur, new.reminder_minutes = card.recur, card.reminder_minutes
    new.stickers, new.cover_colour = card.stickers, card.cover_colour
    new.save(update_fields=["due_at", "start_date", "recur", "reminder_minutes", "stickers", "cover_colour"])
    return new


def _next_ahead(card):
    due = next_due(card.due_at, card.recur)
    now = timezone.now()
    while due and due <= now:            # finished late: skip to the next date that's still ahead
        due = next_due(due, card.recur)
    return due


def _existing_next(card, due):
    from .models import Card
    return Card.objects.filter(list__board=card.list.board, title=card.title, recur=card.recur, due_at=due,
                               is_archived=False, is_complete=False).exclude(pk=card.pk)


def undo_spawn(card):
    """Un-ticked by mistake: take away the copy it just made, if nobody has touched it yet."""
    if not card.recur or not card.due_at or card.is_template:
        return 0
    due = _next_ahead(card)
    n = 0
    for c in _existing_next(card, due) if due else []:
        if not c.comments.exists() and not c.attachments.exists() and not c.checklists.filter(items__is_done=True).exists():
            c.delete()
            n += 1
    return n

"""Creating notifications. Nobody is ever notified about their own action,
and people only hear about cards on boards they can still see."""
import re
from datetime import timedelta

from django.utils import timezone

from . import permissions
from .models import BoardMember, Card, ChecklistItem, Notification


def send(recipient, kind, *, actor=None, card=None, board=None, text, excerpt="", dedupe_key=""):
    if recipient is None or (actor is not None and recipient.pk == actor.pk) or not recipient.is_active:
        return None
    board = board or (card.list.board if card else None)
    if card is not None:
        if not permissions.can_view_card(card, recipient):
            return None
    elif board is not None and not permissions.can_view(board, recipient):
        return None
    if dedupe_key and Notification.objects.filter(recipient=recipient, dedupe_key=dedupe_key).exists():
        return None
    return Notification.objects.create(recipient=recipient, actor=actor, kind=kind, board=board, card=card,
                                       text=text[:300], excerpt=excerpt[:300], dedupe_key=dedupe_key)


# ---------- @mentions ----------

def _handles(user):
    """Ways someone can be @mentioned, longest first so full names win."""
    out = []
    if user.first_name and user.last_name:
        out.append(f"{user.first_name} {user.last_name}")
        out.append(f"{user.first_name}{user.last_name}")
    if user.first_name:
        out.append(user.first_name)
    out.append(user.email.split("@")[0])
    return out


def find_mentions(text, people):
    """Board members mentioned in text as @Full Name, @First or @emailname."""
    found = []
    lowered = text.lower()
    candidates = sorted(((h.lower(), u) for u in people for h in _handles(u)), key=lambda x: -len(x[0]))
    taken = set()
    for handle, user in candidates:
        for m in re.finditer(r"(?<![\w@])@" + re.escape(handle) + r"(?![\w])", lowered):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.add((m.start(), m.end()))
            if user not in found:
                found.append(user)
    return found


def everyone_mentioned(text):
    return re.search(r"(?<![\w@])@(card|board)\b", text, re.IGNORECASE)


def on_comment(comment, people):
    """Notify people @mentioned in a comment, plus the card's members."""
    card, author = comment.card, comment.author
    title = card.title
    excerpt = comment.body.strip().replace("\n", " ")
    mentioned = find_mentions(comment.body, people)
    tag = everyone_mentioned(comment.body)
    if tag:
        group = card.members.all() if tag.group(1).lower() == "card" else people
        mentioned += [u for u in group if u not in mentioned]
    for u in mentioned:
        send(u, Notification.MENTIONED, actor=author, card=card, text=f"mentioned you on {title}", excerpt=excerpt)
    for u in card.members.all():
        if u not in mentioned:
            send(u, Notification.COMMENTED, actor=author, card=card, text=f"commented on {title}", excerpt=excerpt)


# ---------- due-date reminders ----------

def send_due_reminders(now=None):
    """One reminder per card member / item assignee when something is due
    within 24 hours. Safe to run repeatedly — dedupe keys include the due time."""
    now = now or timezone.now()
    soon = now + timedelta(hours=24)
    sent = 0
    cards = (Card.objects.filter(due_at__gt=now, due_at__lte=soon, is_complete=False, is_archived=False,
                                 is_template=False, list__is_archived=False, list__board__is_archived=False)
             .select_related("list__board").prefetch_related("members"))
    for c in cards:
        key = f"due:{c.pk}:{int(c.due_at.timestamp())}"
        when = timezone.localtime(c.due_at).strftime("%a %-d %b, %H:%M")
        for u in c.members.all():
            if send(u, Notification.DUE_SOON, card=c, text=f"{c.title} is due {when}", dedupe_key=key):
                sent += 1
    items = (ChecklistItem.objects.filter(due_at__gt=now, due_at__lte=soon, is_done=False, assignee__isnull=False,
                                          checklist__card__is_archived=False, checklist__card__is_template=False)
             .select_related("assignee", "checklist__card__list__board"))
    for it in items:
        c = it.checklist.card
        key = f"itemdue:{it.pk}:{int(it.due_at.timestamp())}"
        when = timezone.localtime(it.due_at).strftime("%a %-d %b, %H:%M")
        if send(it.assignee, Notification.ITEM_DUE_SOON, card=c,
                text=f"“{it.text}” on {c.title} is due {when}", dedupe_key=key):
            sent += 1
    return sent


def board_people(board):
    return [m.user for m in BoardMember.objects.filter(board=board).select_related("user")]

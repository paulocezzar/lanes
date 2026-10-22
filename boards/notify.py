"""Creating notifications. Nobody is ever notified about their own action,
and people only hear about cards on boards they can still see."""
import re
from datetime import timedelta

from django.utils import timezone

from . import permissions
from .models import BoardMember, Card, ChecklistItem, Notification, Watch


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
    told = set(u.pk for u in mentioned)
    for u in card.members.all():
        if u.pk not in told:
            told.add(u.pk)
            send(u, Notification.COMMENTED, actor=author, card=card, text=f"commented on {title}", excerpt=excerpt)
    for u in watchers(card=card):
        if u.pk not in told:
            told.add(u.pk)
            send(u, Notification.WATCHED, actor=author, card=card, text=f"commented on {title}", excerpt=excerpt)


# ---------- watching ----------

def watchers(card=None, board=None):
    """People watching this card, or the board it (or `board`) is on."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    board = board or (card.list.board if card else None)
    q = Q(watches__board=board, watches__card__isnull=True) if board else Q()
    if card is not None:
        q |= Q(watches__card=card)
    return list(get_user_model().objects.filter(q, is_active=True).distinct())


# Activity a watcher hears about. Noisy verbs (ticks, renames) roll up into
# the same unread notification rather than one each.
WATCH_VERBS = {"card_created", "card_moved", "card_completed", "card_reopened", "card_archived", "card_restored",
               "card_renamed", "due_set", "due_removed", "member_added", "member_joined", "member_removed",
               "member_left", "checklist_added", "item_done", "item_undone", "list_created", "attachment_added",
               "mirrored", "automation", "button", "card_moved_board"}


def on_activity(activity):
    if activity.verb not in WATCH_VERBS:
        return
    card, board = activity.card, activity.board
    people = watchers(card=card, board=board)
    if not people:
        return
    text = activity.text()
    if card is not None:
        text = text.replace("this card", f"“{card.title}”")
        if "“" not in text:
            text = f"{text} on “{card.title}”"
    else:
        text = f"{text} on {board.title}"
    since = timezone.now() - timedelta(hours=1)
    for u in people:
        if activity.actor_id and u.pk == activity.actor_id:
            continue
        recent = Notification.objects.filter(recipient=u, kind=Notification.WATCHED, read_at__isnull=True,
                                             card=card, board=board, created_at__gte=since,
                                             dedupe_key__startswith="watch:").first()
        if recent:
            n = int(recent.dedupe_key.rsplit(":", 1)[-1]) + 1
            recent.actor, recent.text = activity.actor, text[:300]
            recent.excerpt = f"…and {n - 1} other change{'s' if n > 2 else ''}"
            recent.dedupe_key = f"watch:{n}"
            recent.created_at = timezone.now()
            recent.save(update_fields=["actor", "text", "excerpt", "dedupe_key", "created_at"])
        else:
            n = send(u, Notification.WATCHED, actor=activity.actor, card=card, board=board, text=text)
            if n:
                n.dedupe_key = "watch:1"
                n.save(update_fields=["dedupe_key"])


# ---------- due-date reminders ----------

def send_due_reminders(now=None):
    """Card reminders go to the card's members and watchers at the time chosen on
    the card (default a day before; "None" switches them off). Checklist items
    remind their assignee within 24 hours. Safe to run repeatedly — dedupe keys
    include the due time."""
    now = now or timezone.now()
    soon = now + timedelta(hours=24)
    sent = 0
    cards = (Card.objects.filter(due_at__gt=now - timedelta(minutes=15), due_at__lte=now + timedelta(days=2),
                                 is_complete=False, is_archived=False, is_template=False, reminder_minutes__gte=0,
                                 list__is_archived=False, list__board__is_archived=False)
             .select_related("list__board").prefetch_related("members", "watches__user"))
    for c in cards:
        if c.due_at - timedelta(minutes=c.reminder_minutes) > now:
            continue                                  # not time yet
        key = f"due:{c.pk}:{int(c.due_at.timestamp())}"
        when = timezone.localtime(c.due_at).strftime("%a %-d %b, %H:%M")
        text = f"{c.title} is due now ({when})" if c.due_at <= now else f"{c.title} is due {when}"
        people = {u.pk: u for u in c.members.all()}
        people.update({w.user_id: w.user for w in c.watches.all()})
        for u in people.values():
            if send(u, Notification.DUE_SOON, card=c, text=text, dedupe_key=key):
                sent += 1
    items = (ChecklistItem.objects.filter(due_at__gt=now - timedelta(minutes=15), due_at__lte=now + timedelta(days=2),
                                          is_done=False, assignee__isnull=False, reminder_minutes__gte=0,
                                          checklist__card__is_archived=False, checklist__card__is_template=False)
             .select_related("assignee", "checklist__card__list__board"))
    for it in items:
        if it.due_at - timedelta(minutes=it.reminder_minutes) > now:
            continue                                  # not time yet
        c = it.checklist.card
        key = f"itemdue:{it.pk}:{int(it.due_at.timestamp())}"
        when = timezone.localtime(it.due_at).strftime("%a %-d %b, %H:%M")
        if send(it.assignee, Notification.ITEM_DUE_SOON, card=c,
                text=f"“{it.text}” on {c.title} is due {when}", dedupe_key=key):
            sent += 1
    return sent


def board_people(board):
    return [m.user for m in BoardMember.objects.filter(board=board).select_related("user")]

"""Cards inside cards, checklist item → card, and sign-offs."""
from django.utils import timezone

from . import notify, positions
from .models import Card, Checklist, ChecklistItem, SignOff
from .services import log


# ---------- sign-offs ----------

def blocking_signoffs(card):
    """Sign-offs that stop the card being marked complete (anything not yet approved)."""
    return [s for s in card.signoffs.select_related("approver") if s.status != SignOff.APPROVED]


def blocked_message(card):
    open_ = blocking_signoffs(card)
    if not open_:
        return ""
    names = ", ".join(sorted({s.approver.display_name for s in open_}))
    if any(s.status == SignOff.REJECTED for s in open_):
        return f"{names} asked for changes — this card can't be completed until it's signed off."
    return f"Waiting for sign-off from {names} before this card can be completed."


def request_signoff(card, approver, user, ask=""):
    so = SignOff.objects.filter(card=card, approver=approver).exclude(status=SignOff.APPROVED).first()
    if so:
        so.status, so.ask, so.decided_at, so.note = SignOff.PENDING, ask or so.ask, None, ""
        so.requested_by = user
        so.save()
    else:
        so = SignOff.objects.create(card=card, approver=approver, requested_by=user, ask=ask)
    log(card.list.board, user, "signoff_requested", card=card, name=approver.display_name)
    notify.send(approver, "signoff", actor=user, card=card, text=f"asked you to sign off {card.title}",
                excerpt=ask, dedupe_key=f"signoff:{so.pk}:{int(timezone.now().timestamp())}")
    return so


def decide(so, user, approve, note=""):
    so.status = SignOff.APPROVED if approve else SignOff.REJECTED
    so.note, so.decided_at = note[:500], timezone.now()
    so.save()
    card = so.card
    log(card.list.board, user, "signoff_approved" if approve else "signoff_rejected", card=card, note=note[:200])
    told = set()
    for u in [so.requested_by, *card.members.all()]:
        if u and u.pk not in told:
            told.add(u.pk)
            notify.send(u, "signoff", actor=user, card=card,
                        text=f"{'signed off' if approve else 'asked for changes on'} {card.title}", excerpt=note)
    return so


# ---------- completion side effects ----------

DONE_WORDS = ("done", "complete", "finished", "closed", "delivered", "shipped")


def done_list(board):
    """The board's Done list: the first open list whose name says it's finished."""
    for lst in board.lists.filter(is_archived=False).order_by("position"):
        if any(w in lst.title.lower() for w in DONE_WORDS):
            return lst
    return None


def is_done_list(lst):
    return any(w in lst.title.lower() for w in DONE_WORDS)


def move_to_done(card, user=None):
    """Card just marked complete: put it at the top of the Done list (if the board does that). Returns the list."""
    from . import positions
    board = card.list.board
    if not board.complete_to_done or card.is_template or is_done_list(card.list):
        return None
    target = done_list(board)
    if not target:
        return None
    old = card.list
    entries = sorted(positions._entry_positions(target), key=lambda e: e[1])
    card.list = target
    positions.place_in_list(card, target, None, entries[0][0] if entries else None)
    card.save(update_fields=["list", "position", "updated_at"])
    log(board, user, "card_moved", card=card, **{"from": old.title, "to": target.title})
    return target


def after_completion_change(card, user=None):
    """Keep checklist items that were turned into this card ticked in step with it."""
    now = timezone.now()
    for item in ChecklistItem.objects.filter(linked_card=card).select_related("checklist__card__list__board"):
        if item.is_done != card.is_complete:
            item.is_done = card.is_complete
            item.done_at, item.done_by = (now, user) if card.is_complete else (None, None)
            if item.has_qty and card.is_complete:
                item.qty_done = max(item.qty_done, item.qty_target)
            item.save(update_fields=["is_done", "done_at", "done_by", "qty_done"])
            src = item.checklist.card
            log(src.list.board, user, "item_done" if card.is_complete else "item_undone", card=src, text=item.text)
    parent = card.parent
    if parent and card.is_complete and not parent.is_complete:
        kids = [c for c in parent.children.all() if not c.is_archived]
        if kids and all(c.is_complete for c in kids):
            log(parent.list.board, None, "children_done", card=parent, n=len(kids))
            for u in parent.members.all():
                notify.send(u, "watched", card=parent, text=f"every part of {parent.title} is done",
                            dedupe_key=f"kidsdone:{parent.pk}:{len(kids)}")


# ---------- checklist item → card ----------

def item_to_card(item, target_list, user):
    """Make the item its own card (keeping due date, person and amount) inside target_list.
    The new card is a part of the item's card, and the item ticks itself when it's done."""
    src = item.checklist.card
    card = Card.objects.create(list=target_list, title=item.text[:250], due_at=item.due_at, created_by=user,
                               parent=src, is_complete=item.is_done, position=positions.end_position(target_list.cards.filter(is_archived=False)),
                               description=f"From the checklist “{item.checklist.title}” on {src.title}.")
    if item.assignee_id:
        card.members.add(item.assignee)
    if item.has_qty:
        cl = Checklist.objects.create(card=card, title="Progress")
        ChecklistItem.objects.create(checklist=cl, text=item.text[:250], qty_target=item.qty_target,
                                     qty_done=item.qty_done, qty_unit=item.qty_unit, assignee=item.assignee,
                                     is_done=item.is_done or item.qty_done >= item.qty_target)
    item.linked_card = card
    item.save(update_fields=["linked_card"])
    if card.is_complete:
        move_to_done(card, user)
    log(target_list.board, user, "card_created", card=card, list=target_list.title)
    log(src.list.board, user, "item_to_card", card=src, text=item.text, list=target_list.title)
    from .signals import bump
    bump(target_list.board_id)
    return card


# ---------- parents ----------

def would_loop(card, parent):
    """True if making `parent` the parent of `card` would put a card inside itself."""
    p, seen = parent, set()
    while p is not None and p.pk not in seen:
        if p.pk == card.pk:
            return True
        seen.add(p.pk)
        p = p.parent
    return False

"""Endpoints for cards inside cards, checklist item → card, sign-offs and swimlanes."""
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from . import features, permissions, positions
from .api import ApiError, _card, _edit, _text, api
from .models import Card, Checklist, ChecklistItem, Label, List, SignOff, WorkspaceMember
from .services import card_response, log

User = get_user_model()


# ---------- checklist item → card ----------

@api
def item_to_card(request, item_id):
    item = get_object_or_404(ChecklistItem.objects.select_related("checklist__card__list__board"), pk=item_id)
    src = _card(request, item.checklist.card_id)
    if item.linked_card_id:
        raise ApiError("This item is already a card.")
    lst = src.list
    if request.data.get("list_id"):
        lst = get_object_or_404(List.objects.select_related("board"), pk=request.data["list_id"], is_archived=False)
        _edit(lst.board, request.user)
    new = features.item_to_card(item, lst, request.user)
    if request.data.get("remove"):     # Trello's "Convert to card": the item leaves the checklist
        item.delete()
    data = card_response(src.pk, request)
    data["new_card"] = {"id": new.pk, "list_id": lst.pk, "board_id": lst.board_id, "same_board": lst.board_id == src.list.board_id}
    if data["new_card"]["same_board"]:
        from .services import render_tile
        data["new_card"]["tile"] = render_tile(Card.objects.get(pk=new.pk), request)
    return data


@api
def checklist_to_cards(request, checklist_id):
    """Every item on a checklist that isn't a card yet becomes one (a part of this card), in one go."""
    cl = get_object_or_404(Checklist.objects.select_related("card__list__board"), pk=checklist_id)
    src = _card(request, cl.card_id)
    _edit(src.list.board, request.user)
    made = [features.item_to_card(i, src.list, request.user)
            for i in cl.items.filter(linked_card__isnull=True).order_by("position")]
    data = card_response(src.pk, request)
    data["made"] = len(made)
    return data


# ---------- cards inside cards ----------

@login_required
def card_lookup(request):
    """Cards the user can see, for the "Part of…" picker. GET ?q=…&exclude=<card id>."""
    q = (request.GET.get("q") or "").strip()
    boards = permissions.visible_boards(request.user).filter(is_template=False)
    qs = Card.objects.filter(list__board__in=boards, is_archived=False, is_template=False, list__is_archived=False)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(list__board__title__icontains=q))
    if request.GET.get("exclude"):
        qs = qs.exclude(pk=request.GET["exclude"])
    rows = qs.select_related("list__board").order_by("-updated_at")[:15]
    return JsonResponse({"cards": [{"id": c.pk, "title": c.title, "where": f"{c.list.board.title} › {c.list.title}"}
                                   for c in rows]})


@api
def card_parent(request, card_id):
    card = _card(request, card_id)
    pid = request.data.get("parent_id")
    old = card.parent
    if pid:
        parent = get_object_or_404(Card, pk=pid, is_archived=False)
        if not permissions.can_view_card(parent, request.user):
            raise ApiError("You can't see that card.", 403)
        if features.would_loop(card, parent):
            raise ApiError("That card is already a part of this one.")
        card.parent = parent
        card.save(update_fields=["parent", "updated_at"])
        log(card.list.board, request.user, "parent_set", card=card, name=parent.title)
        log(parent.list.board, request.user, "child_added", card=parent, name=card.title)
    elif old:
        card.parent = None
        card.save(update_fields=["parent", "updated_at"])
        log(card.list.board, request.user, "parent_removed", card=card, name=old.title)
    return card_response(card.pk, request)


@api
def card_child_create(request, card_id):
    """Add a new part to a card: a card in a list (on this board or another the user can change)."""
    parent = _card(request, card_id)
    title = _text(request.data.get("title"), 250, "title")
    lst = parent.list
    if request.data.get("list_id"):
        lst = get_object_or_404(List.objects.select_related("board"), pk=request.data["list_id"], is_archived=False)
        _edit(lst.board, request.user)
    child = Card.objects.create(list=lst, title=title, parent=parent, created_by=request.user,
                                position=positions.end_position(lst.cards.filter(is_archived=False)))
    log(lst.board, request.user, "card_created", card=child, list=lst.title)
    log(parent.list.board, request.user, "child_added", card=parent, name=title)
    from . import automation
    from .models import Rule
    automation.fire(child, Rule.ADDED, list_=lst, actor=request.user)
    data = card_response(parent.pk, request)
    if lst.board_id == parent.list.board_id:
        from .services import render_tile
        data["new_card"] = {"id": child.pk, "list_id": lst.pk, "same_board": True,
                            "tile": render_tile(Card.objects.get(pk=child.pk), request)}
    return data


# ---------- sign-offs ----------

def approver_choices(card):
    """People who can be asked to sign off: anyone on the card's boards or in its department."""
    boards = permissions.card_boards(card)
    ids = set(User.objects.filter(board_memberships__board__in=boards).values_list("pk", flat=True))
    ids |= set(WorkspaceMember.objects.filter(workspace=card.list.board.workspace).values_list("user_id", flat=True))
    return list(User.objects.filter(pk__in=ids, is_active=True).order_by("first_name", "last_name"))


@api
def signoff_request(request, card_id):
    card = _card(request, card_id)
    approver = get_object_or_404(User, pk=request.data.get("approver_id"), is_active=True)
    if approver not in approver_choices(card):
        raise ApiError("That person isn't on this board or in this department.")
    if card.is_complete:
        card.is_complete = False
        card.save(update_fields=["is_complete", "updated_at"])
        log(card.list.board, request.user, "card_reopened", card=card)
    features.request_signoff(card, approver, request.user, (request.data.get("ask") or "").strip()[:300])
    return card_response(card.pk, request)


@api
def signoff_update(request, signoff_id):
    so = get_object_or_404(SignOff.objects.select_related("card__list__board", "approver"), pk=signoff_id)
    card = so.card
    if not permissions.can_view_card(card, request.user):
        raise ApiError("Not found", 404)
    action = request.data.get("action")
    if action in ("approve", "reject"):
        if so.approver_id != request.user.pk:
            raise ApiError(f"Only {so.approver.display_name} can sign this off.", 403)
        note = (request.data.get("note") or "").strip()
        if action == "reject" and not note:
            raise ApiError("Say what needs changing, so they know what to fix.")
        features.decide(so, request.user, action == "approve", note)
    elif action == "cancel":
        if not permissions.can_edit_card(card, request.user):
            raise ApiError("You can view this card but not change it.", 403)
        log(card.list.board, request.user, "signoff_cancelled", card=card, name=so.approver.display_name)
        so.delete()
    elif action == "remind":
        if so.status != SignOff.PENDING:
            raise ApiError("There's nothing to remind them about.")
        from . import notify
        from django.utils import timezone
        notify.send(so.approver, "signoff", actor=request.user, card=card,
                    text=f"is still waiting for your sign-off on {card.title}", excerpt=so.ask,
                    dedupe_key=f"signoff:{so.pk}:remind:{timezone.localdate()}")
    else:
        raise ApiError("Unknown action.")
    return card_response(card.pk, request)


# ---------- swimlanes ----------

@api
def card_lane(request, card_id):
    """Dragging a card into another swimlane: swap the person or label the lanes are grouped by."""
    card = _card(request, card_id)
    by, frm, to = request.data.get("by"), str(request.data.get("from") or ""), str(request.data.get("to") or "")
    board = card.list.board
    if by == "member":
        ids = set(board.memberships.values_list("user_id", flat=True))
        if frm.isdigit():
            card.members.remove(int(frm))
        if to.isdigit() and int(to) in ids:
            card.members.add(int(to))
    elif by == "label":
        if frm.isdigit():
            card.labels.remove(int(frm))
        if to.isdigit():
            lab = get_object_or_404(Label, pk=int(to), board=board)
            card.labels.add(lab)
    else:
        raise ApiError("Unknown lanes.")
    card.save(update_fields=["updated_at"])
    return card_response(card.pk, request)

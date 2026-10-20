"""JSON endpoints used by board.js. Every write checks permissions on the
server and returns fresh server-rendered HTML, so the browser never has to
rebuild a card itself."""
import json
from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from . import permissions, positions
from .models import (Board, BoardMember, Card, Checklist, ChecklistItem, Comment, Label, List)
from .services import board_people, card_response, log

User = get_user_model()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def api(view):
    @login_required
    @require_POST
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            request.data = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"error": "Bad JSON"}, status=400)
        try:
            with transaction.atomic():
                return JsonResponse(view(request, *args, **kwargs) or {"ok": True})
        except ApiError as e:
            return JsonResponse({"error": str(e)}, status=e.status)
        except Http404:
            return JsonResponse({"error": "Not found"}, status=404)
    return wrapper


def _edit(board, user):
    if not permissions.can_view(board, user):
        raise Http404
    if not permissions.can_edit(board, user):
        raise ApiError("You can view this board but not change it.", 403)


def _card(request, card_id):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    _edit(card.list.board, request.user)
    return card


def _text(value, limit, field="text"):
    value = (value or "").strip()
    if not value:
        raise ApiError(f"{field.capitalize()} can't be empty.")
    return value[:limit]


# ---------- lists ----------

@api
def list_create(request, board_id):
    board = get_object_or_404(Board, pk=board_id)
    _edit(board, request.user)
    title = _text(request.data.get("title"), 120, "title")
    lst = List.objects.create(board=board, title=title,
                              position=positions.end_position(board.lists.filter(is_archived=False)))
    log(board, request.user, "list_created", title=title)
    return {"html": render_to_string("boards/_list.html", {"list": lst, "can_edit": True}, request=request)}


@api
def list_update(request, list_id):
    lst = get_object_or_404(List.objects.select_related("board"), pk=list_id)
    _edit(lst.board, request.user)
    if "title" in request.data:
        lst.title = _text(request.data["title"], 120, "title")
    if request.data.get("archive"):
        lst.is_archived = True
    lst.save()
    return {"ok": True}


@api
def list_move(request, list_id):
    lst = get_object_or_404(List.objects.select_related("board"), pk=list_id)
    _edit(lst.board, request.user)
    positions.place(lst, lst.board.lists.filter(is_archived=False),
                    request.data.get("before_id"), request.data.get("after_id"))
    lst.save(update_fields=["position"])
    return {"ok": True}


# ---------- cards ----------

@api
def card_create(request, list_id):
    lst = get_object_or_404(List.objects.select_related("board"), pk=list_id)
    _edit(lst.board, request.user)
    title = _text(request.data.get("title"), 250, "title")
    card = Card.objects.create(list=lst, title=title, created_by=request.user,
                               position=positions.end_position(lst.cards.filter(is_archived=False)))
    log(lst.board, request.user, "card_created", card=card, list=lst.title)
    return card_response(card.pk, request)


@api
def card_move(request, card_id):
    card = _card(request, card_id)
    old_list = card.list
    new_list = get_object_or_404(List, pk=request.data.get("list_id", card.list_id), board=old_list.board,
                                 is_archived=False)
    card.list = new_list
    positions.place(card, new_list.cards.filter(is_archived=False),
                    request.data.get("before_id"), request.data.get("after_id"))
    card.save(update_fields=["list", "position", "updated_at"])
    if new_list.pk != old_list.pk:
        log(card.list.board, request.user, "card_moved", card=card, **{"from": old_list.title, "to": new_list.title})
    return card_response(card.pk, request)


@api
def card_update(request, card_id):
    card = _card(request, card_id)
    d, board, user = request.data, card.list.board, request.user
    if "title" in d:
        new = _text(d["title"], 250, "title")
        if new != card.title:
            log(board, user, "card_renamed", card=card, **{"from": card.title})
            card.title = new
    if "description" in d:
        card.description = (d["description"] or "")[:20000]
    if "is_complete" in d and bool(d["is_complete"]) != card.is_complete:
        card.is_complete = bool(d["is_complete"])
        log(board, user, "card_completed" if card.is_complete else "card_reopened", card=card)
    if "due_at" in d:
        if d["due_at"]:
            due = parse_datetime(d["due_at"])
            if not due:
                raise ApiError("Couldn't read that due date.")
            card.due_at = timezone.make_aware(due) if timezone.is_naive(due) else due
            log(board, user, "due_set", card=card, due=timezone.localtime(card.due_at).strftime("%-d %b %Y, %H:%M"))
        else:
            card.due_at = None
            log(board, user, "due_removed", card=card)
    if "start_date" in d:
        card.start_date = parse_date(d["start_date"]) if d["start_date"] else None
    if "archive" in d:
        card.is_archived = bool(d["archive"])
        log(board, user, "card_archived" if card.is_archived else "card_restored", card=card)
    card.save()
    return card_response(card.pk, request)


@api
def card_toggle_label(request, card_id):
    card = _card(request, card_id)
    label = get_object_or_404(Label, pk=request.data.get("label_id"), board=card.list.board)
    if card.labels.filter(pk=label.pk).exists():
        card.labels.remove(label)
    else:
        card.labels.add(label)
    return card_response(card.pk, request)


@api
def label_save(request, board_id):
    """Create a label, or rename/recolour/delete one (label_id given)."""
    board = get_object_or_404(Board, pk=board_id)
    _edit(board, request.user)
    d = request.data
    if d.get("label_id"):
        label = get_object_or_404(Label, pk=d["label_id"], board=board)
        if d.get("delete"):
            label.delete()
            return {"ok": True}
    else:
        label = Label(board=board)
    label.name = (d.get("name") or "").strip()[:40]
    if d.get("colour") in dict(Label.COLOURS):
        label.colour = d["colour"]
    label.save()
    if d.get("card_id"):
        card = _card(request, d["card_id"])
        card.labels.add(label)
        return card_response(card.pk, request)
    return {"ok": True, "label_id": label.pk}


@api
def card_toggle_member(request, card_id):
    card = _card(request, card_id)
    board = card.list.board
    person = get_object_or_404(User, pk=request.data.get("user_id"))
    if not BoardMember.objects.filter(board=board, user=person).exists():
        raise ApiError("Share the board with them first.")
    me = person == request.user
    if card.members.filter(pk=person.pk).exists():
        card.members.remove(person)
        log(board, request.user, "member_left" if me else "member_removed", card=card, name=person.display_name)
    else:
        card.members.add(person)
        log(board, request.user, "member_joined" if me else "member_added", card=card, name=person.display_name)
    return card_response(card.pk, request)


# ---------- checklists ----------

@api
def checklist_create(request, card_id):
    card = _card(request, card_id)
    title = _text(request.data.get("title") or "Checklist", 120, "title")
    Checklist.objects.create(card=card, title=title, position=positions.end_position(card.checklists.all()))
    log(card.list.board, request.user, "checklist_added", card=card, title=title)
    return card_response(card.pk, request)


def _checklist(request, checklist_id):
    cl = get_object_or_404(Checklist.objects.select_related("card__list__board"), pk=checklist_id)
    _edit(cl.card.list.board, request.user)
    return cl


@api
def checklist_update(request, checklist_id):
    cl = _checklist(request, checklist_id)
    if request.data.get("delete"):
        card_id = cl.card_id
        cl.delete()
        return card_response(card_id, request)
    if "title" in request.data:
        cl.title = _text(request.data["title"], 120, "title")
    if "hide_checked" in request.data:
        cl.hide_checked = bool(request.data["hide_checked"])
    cl.save()
    return card_response(cl.card_id, request)


@api
def item_create(request, checklist_id):
    cl = _checklist(request, checklist_id)
    ChecklistItem.objects.create(checklist=cl, text=_text(request.data.get("text"), 250),
                                 position=positions.end_position(cl.items.all()))
    return card_response(cl.card_id, request)


@api
def item_update(request, item_id):
    item = get_object_or_404(ChecklistItem.objects.select_related("checklist__card__list__board"), pk=item_id)
    card = item.checklist.card
    _edit(card.list.board, request.user)
    if request.data.get("delete"):
        item.delete()
        return card_response(card.pk, request)
    if "text" in request.data:
        item.text = _text(request.data["text"], 250)
    if "is_done" in request.data and bool(request.data["is_done"]) != item.is_done:
        item.is_done = bool(request.data["is_done"])
        item.done_by = request.user if item.is_done else None
        item.done_at = timezone.now() if item.is_done else None
        log(card.list.board, request.user, "item_done" if item.is_done else "item_undone", card=card, text=item.text)
    item.save()
    return card_response(card.pk, request)


# ---------- comments ----------

@api
def comment_create(request, card_id):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    board = card.list.board
    if not permissions.can_view(board, request.user):
        raise Http404
    if not permissions.can_comment(board, request.user):
        raise ApiError("Join the board to comment.", 403)
    Comment.objects.create(card=card, author=request.user, body=_text(request.data.get("body"), 5000, "comment"))
    return card_response(card.pk, request)


@api
def comment_update(request, comment_id):
    comment = get_object_or_404(Comment.objects.select_related("card"), pk=comment_id)
    if comment.author_id != request.user.pk:
        raise ApiError("You can only change your own comments.", 403)
    if request.data.get("delete"):
        card_id = comment.card_id
        comment.delete()
        return card_response(card_id, request)
    comment.body = _text(request.data.get("body"), 5000, "comment")
    comment.edited_at = timezone.now()
    comment.save()
    return card_response(comment.card_id, request)


# ---------- board: sharing, star, settings ----------

def _members_html(board, request):
    ctx = {"board": board, "memberships": board.memberships.select_related("user").order_by("added_at"),
           "roles": BoardMember.ROLES, "can_manage": permissions.can_manage(board, request.user)}
    return render_to_string("boards/_share_members.html", ctx, request=request)


@api
def board_share(request, board_id):
    board = get_object_or_404(Board, pk=board_id)
    if not permissions.can_manage(board, request.user):
        raise ApiError("Only board admins can share this board.", 403)
    email = (request.data.get("email") or "").strip().lower()
    person = User.objects.filter(email__iexact=email, is_active=True).first()
    if not person:
        raise ApiError(f"No Lanes account for {email or 'that address'} yet. Ask an admin to add them.")
    role = request.data.get("role") if request.data.get("role") in dict(BoardMember.ROLES) else BoardMember.MEMBER
    _, created = BoardMember.objects.get_or_create(board=board, user=person, defaults={"role": role})
    if not created:
        raise ApiError(f"{person.display_name} is already on this board.")
    log(board, request.user, "board_member_added", name=person.display_name)
    return {"html": _members_html(board, request), "count": board.memberships.count()}


@api
def board_member_update(request, board_id, user_id):
    board = get_object_or_404(Board, pk=board_id)
    membership = get_object_or_404(BoardMember, board=board, user_id=user_id)
    leaving = user_id == request.user.pk and request.data.get("remove")
    if not (leaving or permissions.can_manage(board, request.user)):
        raise ApiError("Only board admins can change members.", 403)
    admins = board.memberships.filter(role=BoardMember.ADMIN)
    last_admin = membership.role == BoardMember.ADMIN and admins.count() == 1
    if request.data.get("remove"):
        if last_admin:
            raise ApiError("A board needs at least one admin. Make someone else admin first.")
        membership.delete()
    elif request.data.get("role") in dict(BoardMember.ROLES):
        if last_admin and request.data["role"] != BoardMember.ADMIN:
            raise ApiError("A board needs at least one admin.")
        membership.role = request.data["role"]
        membership.save()
    return {"html": _members_html(board, request), "count": board.memberships.count()}


@api
def board_join(request, board_id):
    board = get_object_or_404(Board, pk=board_id)
    if not permissions.can_view(board, request.user):
        raise Http404
    BoardMember.objects.get_or_create(board=board, user=request.user, defaults={"role": BoardMember.MEMBER})
    return {"ok": True}


@api
def board_star(request, board_id):
    board = get_object_or_404(Board, pk=board_id)
    m = BoardMember.objects.filter(board=board, user=request.user).first()
    if not m:
        raise ApiError("Join the board to star it.")
    m.starred = not m.starred
    m.save(update_fields=["starred"])
    return {"starred": m.starred}


@api
def board_update(request, board_id):
    board = get_object_or_404(Board, pk=board_id)
    if not permissions.can_manage(board, request.user):
        raise ApiError("Only board admins can change board settings.", 403)
    d = request.data
    if "title" in d:
        board.title = _text(d["title"], 120, "title")
    if d.get("background") in dict(Board.BACKGROUNDS):
        board.background = d["background"]
    if d.get("visibility") in dict(Board.VISIBILITY):
        board.visibility = d["visibility"]
    if "description" in d:
        board.description = (d["description"] or "")[:5000]
    if d.get("archive"):
        board.is_archived = True
    board.save()
    return {"ok": True}


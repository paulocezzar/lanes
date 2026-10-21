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

from . import audit, automation, notify, permissions, positions
from .models import (Board, BoardMember, Card, CardFieldValue, Checklist, ChecklistItem, Comment, CustomField, Label,
                     List, Notification, Rule)
from .services import board_people, card_response, cards_qs, log

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
    """A card the user may change — through its own board or a board it's mirrored onto."""
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    if not permissions.can_view_card(card, request.user):
        raise Http404
    if not permissions.can_edit_card(card, request.user):
        raise ApiError("You can view this card but not change it.", 403)
    return card


def _card_people_ids(card):
    boards = permissions.card_boards(card)
    return set(BoardMember.objects.filter(board__in=boards).values_list("user_id", flat=True))


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
    from .models import COLOURS
    d = request.data
    if "title" in d:
        lst.title = _text(d["title"], 120, "title")
    if "colour" in d:
        lst.colour = d["colour"] if d["colour"] in COLOURS else ""
    if "card_limit" in d:
        try:
            lst.card_limit = max(1, min(int(d["card_limit"]), 500)) if d["card_limit"] not in (None, "", 0) else None
        except (TypeError, ValueError):
            raise ApiError("The limit must be a number.")
    if "archive" in d and bool(d["archive"]) != lst.is_archived:
        lst.is_archived = bool(d["archive"])
        if not lst.is_archived:          # comes back at the end of the board
            lst.position = positions.end_position(lst.board.lists.filter(is_archived=False))
        log(lst.board, request.user, "list_archived" if lst.is_archived else "list_restored", title=lst.title)
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
    automation.fire(card, Rule.ADDED, list_=lst)
    return card_response(card.pk, request)


@api
def card_move(request, card_id):
    card = _card(request, card_id)
    old_list = card.list
    if not permissions.can_edit(old_list.board, request.user):
        raise ApiError("Only people on the card's own board can move it. You can move the mirror instead.", 403)
    new_list = get_object_or_404(List.objects.select_related("board"), pk=request.data.get("list_id", card.list_id),
                                 is_archived=False, board__is_archived=False)
    if new_list.board_id != old_list.board_id:
        if not permissions.can_view(new_list.board, request.user):
            raise Http404
        if not permissions.can_edit(new_list.board, request.user):
            raise ApiError("You can't add cards to that board.", 403)
        from .copying import transfer_card
        transfer_card(card, new_list, user=request.user)
        automation.fire(card, Rule.ADDED, list_=new_list)
        data = card_response(card.pk, request)
        data["moved_board"] = new_list.board_id
        return data
    card.list = new_list
    positions.place_in_list(card, new_list, request.data.get("before", request.data.get("before_id")),
                            request.data.get("after", request.data.get("after_id")))
    card.save(update_fields=["list", "position", "updated_at"])
    if new_list.pk != old_list.pk:
        log(card.list.board, request.user, "card_moved", card=card, **{"from": old_list.title, "to": new_list.title})
        automation.fire(card, Rule.MOVED, list_=new_list)
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
    if "is_template" in d:
        card.is_template = bool(d["is_template"])
    if "cover_colour" in d:
        from .models import COLOURS
        card.cover_colour = d["cover_colour"] if d["cover_colour"] in COLOURS else ""
        if card.cover_colour:
            card.cover = None
    if "sticker" in d:
        from .models import STICKERS
        s = d["sticker"]
        stickers = list(card.stickers or [])
        if s in stickers:
            stickers.remove(s)
        elif s in STICKERS and len(stickers) < 6:
            stickers.append(s)
        card.stickers = stickers
    if "archive" in d:
        card.is_archived = bool(d["archive"])
        log(board, user, "card_archived" if card.is_archived else "card_restored", card=card)
    just_completed = "is_complete" in d and card.is_complete and not Card.objects.get(pk=card.pk).is_complete
    card.save()
    if just_completed:
        automation.fire(card, Rule.COMPLETED)
    return card_response(card.pk, request)


@api
def card_toggle_label(request, card_id):
    card = _card(request, card_id)
    label = get_object_or_404(Label, pk=request.data.get("label_id"), board=card.list.board)
    if card.labels.filter(pk=label.pk).exists():
        card.labels.remove(label)
    else:
        card.labels.add(label)
        automation.fire(card, Rule.LABEL_ADDED, label=label)
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
    if person.pk not in _card_people_ids(card):
        raise ApiError("Share the board with them first.")
    me = person == request.user
    if card.members.filter(pk=person.pk).exists():
        card.members.remove(person)
        log(board, request.user, "member_left" if me else "member_removed", card=card, name=person.display_name)
    else:
        card.members.add(person)
        log(board, request.user, "member_joined" if me else "member_added", card=card, name=person.display_name)
        notify.send(person, Notification.ASSIGNED, actor=request.user, card=card,
                    text=f"assigned you to {card.title}")
    return card_response(card.pk, request)


# ---------- checklists ----------

@api
def checklist_create(request, card_id):
    card = _card(request, card_id)
    title = _text(request.data.get("title") or "Checklist", 120, "title")
    cl = Checklist.objects.create(card=card, title=title, position=positions.end_position(card.checklists.all()))
    items = [str(t).strip()[:250] for t in (request.data.get("items") or []) if str(t).strip()][:50]
    for i, text in enumerate(items, start=1):
        ChecklistItem.objects.create(checklist=cl, text=text, position=i * positions.GAP)
    log(card.list.board, request.user, "checklist_added", card=card, title=title)
    return card_response(card.pk, request)


@api
def suggest_steps(request, card_id):
    """AI-drafted checklist for this card — returned for review, not saved."""
    from . import ai
    card = _card(request, card_id)
    existing = [i.text for cl in card.checklists.all() for i in cl.items.all()]
    try:
        title, steps = ai.suggest_steps(card.title, card.description, existing)
    except ai.AIUnavailable as e:
        raise ApiError(str(e), 503)
    return {"title": title, "steps": steps}


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
    if "assignee_id" in request.data:
        uid = request.data["assignee_id"]
        if uid:
            if int(uid) not in _card_people_ids(card):
                raise ApiError("Share the board with them first.")
            if item.assignee_id != int(uid):
                notify.send(User.objects.get(pk=uid), Notification.ITEM_ASSIGNED, actor=request.user, card=card,
                            text=f"assigned you “{item.text}” on {card.title}")
            item.assignee_id = uid
        else:
            item.assignee = None
    if "due_at" in request.data:
        if request.data["due_at"]:
            due = parse_datetime(request.data["due_at"])
            if not due:
                raise ApiError("Couldn't read that date.")
            item.due_at = timezone.make_aware(due) if timezone.is_naive(due) else due
        else:
            item.due_at = None
    if "is_done" in request.data and bool(request.data["is_done"]) != item.is_done:
        item.is_done = bool(request.data["is_done"])
        item.done_by = request.user if item.is_done else None
        item.done_at = timezone.now() if item.is_done else None
        log(card.list.board, request.user, "item_done" if item.is_done else "item_undone", card=card, text=item.text)
    item.save()
    if request.data.get("is_done") and automation.checklists_all_done(card):
        automation.fire(card, Rule.CHECKLIST_DONE)
    return card_response(card.pk, request)


# ---------- comments ----------

@api
def comment_create(request, card_id):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    if not permissions.can_view_card(card, request.user):
        raise Http404
    if not permissions.can_comment_card(card, request.user):
        raise ApiError("Join the board to comment.", 403)
    comment = Comment.objects.create(card=card, author=request.user,
                                     body=_text(request.data.get("body"), 5000, "comment"))
    people = list(User.objects.filter(pk__in=_card_people_ids(card)))
    notify.on_comment(comment, people)
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
    new_account = None
    if not person:
        if not permissions.can_invite_guests(board, request.user):
            who = permissions.who_can("invite_guests")
            raise ApiError(f"No Lanes account for {email or 'that address'}. " + (
                "Inviting guests is switched off for the company." if who == "nobody"
                else f"Only {who} can invite guests — ask one of them, or a department admin to add them."))
        if not request.data.get("invite_guest"):
            return {"needs_guest": True, "email": email}
        if "@" not in email or "." not in email.split("@")[-1]:
            raise ApiError("That doesn't look like an email address.")
        if User.objects.filter(email__iexact=email).exists():
            raise ApiError("That account has been switched off. Ask a company admin.")
        from .views_people import temporary_password
        password = temporary_password()
        person = User.objects.create_user(email=email, password=password, is_guest=True,
                                          first_name=(request.data.get("first_name") or "").strip()[:150],
                                          last_name=(request.data.get("last_name") or "").strip()[:150])
        new_account = {"email": email, "password": password}
    role = request.data.get("role") if request.data.get("role") in dict(BoardMember.ROLES) else BoardMember.MEMBER
    if person.is_guest and role == BoardMember.ADMIN:
        role = BoardMember.MEMBER
    _, created = BoardMember.objects.get_or_create(board=board, user=person, defaults={"role": role})
    if not created:
        raise ApiError(f"{person.display_name} is already on this board.")
    notify.send(person, Notification.ADDED_TO_BOARD, actor=request.user, board=board,
                text=f"added you to the board {board.title}")
    log(board, request.user, "board_member_added", name=person.display_name)
    audit.record(request, "guest_invited" if new_account else "board_shared", board=board, target=person,
                 role=dict(BoardMember.ROLES)[role].lower())
    return {"html": _members_html(board, request), "count": board.memberships.count(), "new_account": new_account}


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
        audit.record(request, "board_member_removed", board=board, target=membership.user)
    elif request.data.get("role") in dict(BoardMember.ROLES):
        if last_admin and request.data["role"] != BoardMember.ADMIN:
            raise ApiError("A board needs at least one admin.")
        if membership.user.is_guest and request.data["role"] == BoardMember.ADMIN:
            raise ApiError("Guests can be members or observers, not admins.")
        membership.role = request.data["role"]
        membership.save()
        audit.record(request, "board_role_changed", board=board, target=membership.user,
                     role=membership.get_role_display().lower())
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
    if d.get("visibility") == Board.PRIVATE and board.visibility != Board.PRIVATE \
            and not permissions.can_make_private(board, request.user):
        raise ApiError(f"Only {permissions.who_can('private_boards')} can make boards private.", 403)
    if d.get("public") and not permissions.can_public_link(board, request.user):
        raise ApiError("Public links are switched off for the company." if permissions.public_links_off()
                       else f"Only {permissions.who_can('public_links')} can turn on public links.", 403)
    if d.get("visibility") in dict(Board.VISIBILITY) and d["visibility"] != board.visibility:
        board.visibility = d["visibility"]
        audit.record(request, "board_visibility", board=board, value=board.get_visibility_display())
    if "description" in d:
        board.description = (d["description"] or "")[:5000]
    if "is_template" in d:
        board.is_template = bool(d["is_template"])
    if "voting" in d:
        board.voting = bool(d["voting"])
    if "card_aging" in d:
        board.card_aging = bool(d["card_aging"])
    if "public" in d:
        import secrets
        board.public_token = secrets.token_urlsafe(18) if d["public"] else ""
        audit.record(request, "board_public_on" if d["public"] else "board_public_off", board=board)
    if "collection" in d:
        from .models import Collection
        value = d["collection"]
        if not value:
            board.collection = None
        elif isinstance(value, str) and value.startswith("new:"):
            name = _text(value[4:], 60, "collection name")
            board.collection, _ = Collection.objects.get_or_create(workspace=board.workspace, name=name)
        else:
            board.collection = get_object_or_404(Collection, pk=value, workspace=board.workspace)
    if "archive" in d and bool(d["archive"]) != board.is_archived:
        board.is_archived = bool(d["archive"])
        audit.record(request, "board_archived" if board.is_archived else "board_restored", board=board)
    board.save()
    return {"ok": True, "public_token": board.public_token}



# ---------- custom fields ----------

@api
def field_save(request, board_id):
    """Create, update or delete a board's custom field."""
    board = get_object_or_404(Board, pk=board_id)
    _edit(board, request.user)
    d = request.data
    if d.get("field_id"):
        field = get_object_or_404(CustomField, pk=d["field_id"], board=board)
        if d.get("delete"):
            field.delete()
            return {"ok": True}
    else:
        if board.custom_fields.count() >= 30:
            raise ApiError("A board can have up to 30 custom fields.")
        kind = d.get("kind") if d.get("kind") in dict(CustomField.KINDS) else CustomField.TEXT
        field = CustomField(board=board, kind=kind,
                            position=positions.end_position(board.custom_fields.all()))
    if "name" in d or not field.pk:
        field.name = _text(d.get("name"), 60, "name")
    if "show_on_card" in d:
        field.show_on_card = bool(d["show_on_card"])
    if field.kind == CustomField.DROPDOWN and "options" in d:
        opts = d["options"]
        if isinstance(opts, str):
            opts = opts.split(",")
        cleaned = list(dict.fromkeys(o.strip()[:60] for o in opts if str(o).strip()))[:30]
        if not cleaned:
            raise ApiError("Give the dropdown at least one option.")
        field.options = cleaned
        if field.pk:  # drop values that are no longer valid options
            stale = [v.pk for v in CardFieldValue.objects.filter(field=field) if v.value not in cleaned]
            CardFieldValue.objects.filter(pk__in=stale).delete()
    elif field.kind == CustomField.DROPDOWN and not field.options:
        raise ApiError("Give the dropdown at least one option.")
    field.save()
    return {"ok": True, "field_id": field.pk}


@api
def card_field_set(request, card_id):
    card = _card(request, card_id)
    field = get_object_or_404(CustomField, pk=request.data.get("field_id"), board=card.list.board)
    try:
        value = field.clean_value(request.data.get("value"))
    except (TypeError, ValueError):
        raise ApiError(f"That isn't a valid {field.get_kind_display().lower()} for {field.name}.")
    if value is None:
        CardFieldValue.objects.filter(card=card, field=field).delete()
    else:
        cfv, _ = CardFieldValue.objects.update_or_create(card=card, field=field, defaults={"value": value})
        if field.kind == CustomField.LOCATION:
            from . import geo
            geo.place(cfv)
    return card_response(card.pk, request)


# ---------- copying and templates ----------

@api
def card_copy(request, card_id):
    card = _card(request, card_id)
    d = request.data
    target = get_object_or_404(List, pk=d.get("list_id") or card.list_id, board=card.list.board, is_archived=False)
    if not permissions.can_edit(target.board, request.user):
        raise ApiError("You can't add cards to that board.", 403)
    from .copying import copy_card
    new = copy_card(card, target, user=request.user, title=(d.get("title") or "").strip() or card.title,
                    keep_labels=d.get("labels", True), keep_members=d.get("members", True),
                    keep_checklists=d.get("checklists", True))
    data = card_response(new.pk, request)
    data["copied_from"] = card.pk
    return data


@api
def card_from_template(request, list_id):
    lst = get_object_or_404(List.objects.select_related("board"), pk=list_id, is_archived=False)
    _edit(lst.board, request.user)
    tpl = get_object_or_404(Card, pk=request.data.get("template_id"), list__board=lst.board, is_template=True)
    from .copying import copy_card
    new = copy_card(tpl, lst, user=request.user, keep_members=True, keep_dates=False)
    automation.fire(new, Rule.ADDED, list_=lst)
    return card_response(new.pk, request)


@api
def attachment_update(request, attachment_id):
    from .models import Attachment
    att = get_object_or_404(Attachment.objects.select_related("card__list__board"), pk=attachment_id)
    card = _card(request, att.card_id)
    if request.data.get("delete"):
        try:
            att.file.delete(save=False)
        except Exception:  # file already gone or storage unreachable — still remove the record
            import logging
            logging.getLogger(__name__).exception("Could not delete stored file for attachment %s", att.pk)
        if card.cover_id == att.pk:
            card.cover = None
            card.save(update_fields=["cover", "updated_at"])
        att.delete()
    elif "cover" in request.data:
        card.cover = att if request.data["cover"] and att.is_image else None
        card.save(update_fields=["cover", "updated_at"])
    return card_response(card.pk, request)


# ---------- mirrors ----------

@api
def mirror_create(request, card_id):
    from .models import CardMirror
    card = _card(request, card_id)
    target = get_object_or_404(List.objects.select_related("board"), pk=request.data.get("list_id"), is_archived=False)
    if not permissions.can_view(target.board, request.user):
        raise Http404
    if not permissions.can_edit(target.board, request.user):
        raise ApiError("You can't add cards to that board.", 403)
    if target.pk == card.list_id:
        raise ApiError("The card is already in that list.")
    mirror, created = CardMirror.objects.get_or_create(card=card, list=target, defaults={"created_by": request.user})
    if not created:
        raise ApiError("It's already mirrored there.")
    positions.place_in_list(mirror, target)
    mirror.save(update_fields=["position"])
    log(card.list.board, request.user, "mirrored", card=card, to=f"{target.board.title} › {target.title}")
    if target.board_id != card.list.board_id:
        log(target.board, request.user, "mirror_added", card=card, source=card.list.board.title, list=target.title)
    return card_response(card.pk, request)


def _mirror(request, mirror_id):
    from .models import CardMirror
    m = get_object_or_404(CardMirror.objects.select_related("list__board", "card__list__board"), pk=mirror_id)
    if not (permissions.can_view(m.list.board, request.user) or permissions.can_view_card(m.card, request.user)):
        raise Http404
    if not (permissions.can_edit(m.list.board, request.user) or permissions.can_edit(m.card.list.board, request.user)):
        raise ApiError("You can't change this mirror.", 403)
    return m


@api
def mirror_update(request, mirror_id):
    m = _mirror(request, mirror_id)
    card_id = m.card_id
    if request.data.get("delete"):
        log(m.card.list.board, request.user, "unmirrored", card=m.card, frm=f"{m.list.board.title} › {m.list.title}")
        m.delete()
    return card_response(card_id, request)


@api
def mirror_move(request, mirror_id):
    m = _mirror(request, mirror_id)
    if not permissions.can_edit(m.list.board, request.user):
        raise ApiError("You can't rearrange that board.", 403)
    target = get_object_or_404(List, pk=request.data.get("list_id", m.list_id), board=m.list.board, is_archived=False)
    if target.pk == m.card.list_id:
        raise ApiError("The card itself is already in that list.")
    if target.pk != m.list_id and m.card.mirrors.filter(list=target).exists():
        raise ApiError("It's already mirrored in that list.")
    m.list = target
    positions.place_in_list(m, target, request.data.get("before"), request.data.get("after"))
    m.save(update_fields=["list", "position"])
    data = card_response(m.card_id, request)
    data["tile"] = render_to_string("boards/_card_tile.html", {"card": cards_qs().get(pk=m.card_id), "mirror": m},
                                    request=request)
    return data


# ---------- watching ----------

@api
def card_watch(request, card_id):
    from .models import Watch
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    if not permissions.can_view_card(card, request.user):
        raise Http404
    w = Watch.objects.filter(user=request.user, card=card).first()
    if w:
        w.delete()
    else:
        Watch.objects.create(user=request.user, card=card)
    return card_response(card.pk, request)


@api
def board_watch(request, board_id):
    from .models import Watch
    board = get_object_or_404(Board, pk=board_id)
    if not permissions.can_view(board, request.user):
        raise Http404
    w = Watch.objects.filter(user=request.user, board=board, card__isnull=True).first()
    if w:
        w.delete()
    else:
        Watch.objects.create(user=request.user, board=board)
    return {"watching": not w}


# ---------- buttons ----------

@api
def card_button_press(request, card_id, button_id):
    from .models import Button
    card = _card(request, card_id)
    button = get_object_or_404(Button, pk=button_id, board=card.list.board, kind=Button.CARD)
    done = automation.press_card_button(button, card, request.user)
    data = card_response(card.pk, request)
    data["summary"] = "; ".join(done) or "Nothing to change."
    return data


@api
def board_button_press(request, board_id, button_id):
    from .models import Button
    board = get_object_or_404(Board, pk=board_id)
    _edit(board, request.user)
    button = get_object_or_404(Button, pk=button_id, board=board, kind=Button.BOARD)
    done = automation.press_board_button(button, request.user)
    return {"summary": "; ".join(done) or "Nothing to change."}


# ---------- reactions and votes ----------

@api
def comment_react(request, comment_id):
    from .models import REACTIONS, Reaction
    comment = get_object_or_404(Comment.objects.select_related("card__list__board"), pk=comment_id)
    card = comment.card
    if not permissions.can_view_card(card, request.user):
        raise Http404
    if not permissions.can_comment_card(card, request.user):
        raise ApiError("Join the board to react.", 403)
    emoji = request.data.get("emoji")
    if emoji not in REACTIONS:
        raise ApiError("Pick one of the reactions.")
    r = Reaction.objects.filter(comment=comment, user=request.user, emoji=emoji).first()
    if r:
        r.delete()
    else:
        Reaction.objects.create(comment=comment, user=request.user, emoji=emoji)
    from .signals import bump
    bump(card.list.board_id, card.pk)
    return card_response(card.pk, request)


@api
def card_vote(request, card_id):
    from .models import Vote
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    if not permissions.can_view_card(card, request.user):
        raise Http404
    if not card.list.board.voting:
        raise ApiError("Voting is turned off on this board.")
    if not permissions.can_comment_card(card, request.user):
        raise ApiError("Join the board to vote.", 403)
    v = Vote.objects.filter(card=card, user=request.user).first()
    if v:
        v.delete()
    else:
        Vote.objects.create(card=card, user=request.user)
    from .signals import bump
    bump(card.list.board_id, card.pk)
    return card_response(card.pk, request)


@api
def card_delete(request, card_id):
    """Delete for good. Only archived cards, so nothing disappears by one mis-click."""
    card = _card(request, card_id)
    if not permissions.can_edit(card.list.board, request.user):
        raise ApiError("Only people on the card's own board can delete it.", 403)
    if not card.is_archived:
        raise ApiError("Archive the card first, then delete it from the board menu.")
    log(card.list.board, request.user, "card_deleted", title=card.title)
    from .models import Attachment
    for att in Attachment.objects.filter(card=card):
        try:
            att.file.delete(save=False)
        except Exception:
            pass
    card.delete()
    return {"ok": True, "deleted": card_id}


# ---------- ✨ AI: summary, writing help, priorities ----------

@api
def card_summarise(request, card_id):
    from . import ai
    card = get_object_or_404(cards_qs(), pk=card_id)
    if not permissions.can_view_card(card, request.user):
        raise Http404
    try:
        return ai.summarise_card(card)
    except ai.AIUnavailable as e:
        raise ApiError(str(e), 503)


@api
def card_write_description(request, card_id):
    from . import ai
    card = _card(request, card_id)
    try:
        return {"text": ai.write_description(card.title, (request.data.get("notes") or "")[:6000])}
    except ai.AIUnavailable as e:
        raise ApiError(str(e), 503)


@api
def my_priorities(request):
    """✨ What should I do first? — from the cards and checklist items assigned to me."""
    from . import ai
    user = request.user
    now = timezone.now()
    boards = permissions.visible_boards(user).filter(is_template=False)
    cards = list(cards_qs().filter(members=user, is_archived=False, is_complete=False, list__board__in=boards,
                                   list__is_archived=False).order_by("due_at")[:40])
    items = list(ChecklistItem.objects.filter(assignee=user, is_done=False, checklist__card__is_archived=False,
                                              checklist__card__list__board__in=boards)
                 .select_related("checklist__card__list__board").order_by("due_at")[:20])
    if not cards and not items:
        raise ApiError("Nothing is assigned to you right now.")

    def when(dt):
        if not dt:
            return "no due date"
        local = timezone.localtime(dt)
        days = (local.date() - timezone.localdate()).days
        rel = "OVERDUE" if dt < now else ("today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days")
        return f"due {local:%a %-d %b %H:%M} ({rel})"

    entries, lookup = [], {}
    for c in cards:
        done, total = c.checklist_counts()
        labels = ", ".join(str(l) for l in c.labels.all())
        text = (f"card “{c.title}” on {c.list.board.title} › {c.list.title}; {when(c.due_at)}"
                + (f"; labels: {labels}" if labels else "") + (f"; checklist {done}/{total}" if total else "")
                + f"; {len(c.comments.all())} comments; last changed {timezone.localtime(c.updated_at):%-d %b}")
        entries.append({"id": f"c{c.pk}", "text": text})
        lookup[f"c{c.pk}"] = {"title": c.title, "where": f"{c.list.board.title} › {c.list.title}",
                              "url": f"/b/{c.list.board_id}/?card={c.pk}", "due_state": c.due_state,
                              "due": timezone.localtime(c.due_at).strftime("%-d %b") if c.due_at else ""}
    for it in items:
        c = it.checklist.card
        entries.append({"id": f"i{it.pk}", "text": f"checklist item “{it.text}” on card “{c.title}” "
                                                  f"({c.list.board.title}); {when(it.due_at)}"})
        lookup[f"i{it.pk}"] = {"title": it.text, "where": f"☐ on {c.title}", "url": f"/b/{c.list.board_id}/?card={c.pk}",
                               "due_state": it.due_state,
                               "due": timezone.localtime(it.due_at).strftime("%-d %b") if it.due_at else ""}
    try:
        picks, note = ai.priorities(entries, timezone.localdate())
    except ai.AIUnavailable as e:
        raise ApiError(str(e), 503)
    html = render_to_string("boards/_priorities.html", {"picks": [{**lookup[p["id"]], "why": p["why"]} for p in picks],
                                                        "note": note}, request=request)
    return {"html": html}

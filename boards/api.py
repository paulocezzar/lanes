"""JSON endpoints used by board.js. Every write checks permissions on the
server and returns fresh server-rendered HTML, so the browser never has to
rebuild a card itself."""
import json
import re
from functools import wraps

from django.conf import settings
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
    def __init__(self, message, status=400, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


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
            return JsonResponse({"error": str(e), **e.extra}, status=e.status)
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
    live = list(board.lists.filter(is_archived=False).order_by("position", "pk"))
    try:
        at = int(request.data.get("position") or 0)        # 1-based slot, like Trello's "Position"
    except (TypeError, ValueError):
        at = 0
    if 1 <= at <= len(live):
        before = live[at - 2].position if at > 1 else None
        pos = positions.between(before, live[at - 1].position)
    else:
        pos = positions.end_position(board.lists.filter(is_archived=False))
    lst = List.objects.create(board=board, title=title, position=pos)
    log(board, request.user, "list_created", title=title)
    automation.fire_list_created(lst, request.user)
    lst.refresh_from_db()
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
    if "is_pinned" in d:
        lst.is_pinned = bool(d["is_pinned"])
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
    if request.data.get("smart_due"):            # "Gemma starts next Monday" → due Monday 17:00
        from .nl_dates import parse
        due, _ = parse(title)
        if due:
            card.due_at = due
            card.save(update_fields=["due_at"])
            log(lst.board, request.user, "due_set", card=card, due=timezone.localtime(due).strftime("%-d %b %Y, %H:%M"))
    automation.fire(card, Rule.ADDED, list_=lst, actor=request.user)
    if card.due_at:
        card.refresh_from_db()
        automation.fire(card, Rule.DUE_SET, actor=request.user)
    return card_response(card.pk, request)


@api
def product_create(request, list_id):
    """'New card with amounts': what it's for and how many, then what it needs, one per line."""
    from . import products
    lst = get_object_or_404(List.objects.select_related("board"), pk=list_id, is_archived=False)
    _edit(lst.board, request.user)
    text = (request.data.get("text") or "")[:20000]
    try:
        parsed = products.parse(text)
    except ValueError as e:
        raise ApiError(str(e))
    if request.data.get("preview"):
        f = lambda v: None if v is None else ChecklistItem.fmt_qty(v)  # noqa: E731
        return {"title": parsed["title"], "planned": f(parsed["planned"]), "unit": parsed["unit"],
                "fields": [{"name": k, "value": v} for k, v in parsed["fields"]], "notes": parsed["notes"],
                "sections": [{"title": s["title"], "items": [
                    {"text": i["text"], "target": f(i["target"]), "unit": i["unit"], "per": f(i["per_unit"]),
                     "done": i["done"]} for i in s["items"]]} for s in parsed["sections"]]}
    if not parsed["sections"]:
        raise ApiError("Add what it needs on the lines under the product name.")
    card = products.create(lst, text, request.user)
    log(lst.board, request.user, "card_created", card=card, list=lst.title)
    automation.fire(card, Rule.ADDED, list_=lst, actor=request.user)
    return card_response(card.pk, request)


def _slot(card, lst, position):
    """1-based position in a list → (before, after) neighbour tokens, ignoring the card itself."""
    entries = [t for t, _ in sorted(positions._entry_positions(lst, exclude=f"c{card.pk}"), key=lambda e: (e[1], e[0]))]
    i = max(1, min(int(position), len(entries) + 1)) - 1
    return (entries[i - 1] if i > 0 else None), (entries[i] if i < len(entries) else None)


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
        if request.data.get("position"):
            card.refresh_from_db()
            positions.place_in_list(card, new_list, *_slot(card, new_list, request.data["position"]))
            card.save(update_fields=["position"])
        automation.fire(card, Rule.ADDED, list_=new_list, actor=request.user)
        data = card_response(card.pk, request)
        data["moved_board"] = new_list.board_id
        return data
    card.list = new_list
    if request.data.get("position"):
        try:
            before, after = _slot(card, new_list, request.data["position"])
        except (TypeError, ValueError):
            raise ApiError("Pick a position.")
        positions.place_in_list(card, new_list, before, after)
    else:
        positions.place_in_list(card, new_list, request.data.get("before", request.data.get("before_id")),
                                request.data.get("after", request.data.get("after_id")))
    card.save(update_fields=["list", "position", "updated_at"])
    if new_list.pk != old_list.pk:
        log(card.list.board, request.user, "card_moved", card=card, **{"from": old_list.title, "to": new_list.title})
        automation.fire(card, Rule.MOVED, list_=new_list, actor=request.user)
        from .features import after_completion_change, is_done_list
        board = new_list.board
        if board.complete_to_done and not card.is_template and is_done_list(new_list) != is_done_list(old_list):
            # into Done → complete; dragged back out of Done → not complete
            want = is_done_list(new_list)
            from .features import blocked_message
            if want and blocked_message(card):
                want = card.is_complete            # still waiting for a sign-off: it can sit in Done, not complete
            if card.is_complete != want:
                card.is_complete = want
                card.save(update_fields=["is_complete", "updated_at"])
                log(board, request.user, "card_completed" if want else "card_reopened", card=card)
                after_completion_change(card, request.user)
                if want:
                    automation.fire(card, Rule.COMPLETED, actor=request.user)
    return card_response(card.pk, request)


@api
def card_update(request, card_id):
    card = _card(request, card_id)
    d, board, user = request.data, card.list.board, request.user
    fire_after = []
    if "title" in d:
        new = _text(d["title"], 250, "title")
        if new != card.title:
            log(board, user, "card_renamed", card=card, **{"from": card.title, "to": new})
            card.title = new
    if "description" in d:
        new_desc = (d["description"] or "")[:20000]
        base = d.get("description_base")
        # Two people editing at once: refuse to silently overwrite someone else's newer text.
        if base is not None and card.description != base and card.description != new_desc:
            raise ApiError("Someone else changed this description while you were editing.", 409,
                           {"conflict": True, "current": card.description})
        card.description = new_desc
    if "is_complete" in d and bool(d["is_complete"]) != card.is_complete:
        if d["is_complete"]:
            from .features import blocked_message
            why = blocked_message(card)
            if why:
                raise ApiError(why, 409, {"signoff": True})
        card.is_complete = bool(d["is_complete"])
        log(board, user, "card_completed" if card.is_complete else "card_reopened", card=card)
    if "due_at" in d:
        if d["due_at"]:
            due = parse_datetime(d["due_at"])
            if not due:
                raise ApiError("Couldn't read that due date.")
            card.due_at = timezone.make_aware(due) if timezone.is_naive(due) else due
            log(board, user, "due_set", card=card, due=timezone.localtime(card.due_at).strftime("%-d %b %Y, %H:%M"))
            fire_after.append(Rule.DUE_SET)
        else:
            had_due = Card.objects.filter(pk=card.pk, due_at__isnull=False).exists()
            card.due_at = None
            log(board, user, "due_removed", card=card)
            if had_due:
                fire_after.append(Rule.DUE_REMOVED)
    if "start_date" in d:
        card.start_date = parse_date(d["start_date"]) if d["start_date"] else None
    if "is_milestone" in d:
        card.is_milestone = bool(d["is_milestone"])
    if "estimate_hours" in d:
        try:
            est = round(float(d["estimate_hours"]), 2) if str(d["estimate_hours"]).strip() != "" else None
        except (TypeError, ValueError):
            raise ApiError("The estimate needs to be a number of hours.")
        if est is not None and not (0 <= est <= 9999):
            raise ApiError("The estimate needs to be between 0 and 9,999 hours.")
        card.estimate_hours = est
    if "recur" in d and d["recur"] in dict(Card.RECUR):
        card.recur = d["recur"]
    if "reminder_minutes" in d:
        try:
            r = int(d["reminder_minutes"])
        except (TypeError, ValueError):
            r = 1440
        card.reminder_minutes = r if r in dict(Card.REMINDERS) else 1440
    if "is_template" in d:
        card.is_template = bool(d["is_template"])
    if "cover_colour" in d:
        from .models import COLOURS
        card.cover_colour = d["cover_colour"] if d["cover_colour"] in COLOURS else ""
        if card.cover_colour:
            card.cover = None
    if "cover_full" in d:
        card.cover_full = bool(d["cover_full"])
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
        was_archived = card.is_archived
        card.is_archived = bool(d["archive"])
        log(board, user, "card_archived" if card.is_archived else "card_restored", card=card)
        if card.is_archived and not was_archived:
            fire_after.append(Rule.ARCHIVED)
    was_complete = Card.objects.filter(pk=card.pk).values_list("is_complete", flat=True).first()
    just_completed = "is_complete" in d and card.is_complete and not was_complete
    card.save()
    if hasattr(card, "stockup") and ({"due_at", "archive"} & set(d)):
        from . import stockup as _su
        _su.after_card_change(card, user, set(d))
    pushed = []
    if "due_at" in d or "start_date" in d or "is_milestone" in d:
        from .schedule import push_dependants
        pushed = push_dependants(card)
        for c in pushed:
            log(board, user, "due_set", card=c, due=timezone.localtime(c.due_at).strftime("%-d %b %Y, %H:%M"))
    for trig in fire_after:
        automation.fire(card, trig, actor=user)
    if "is_complete" in d and bool(was_complete) != card.is_complete:
        from .features import after_completion_change
        after_completion_change(card, user)
    if "is_complete" in d and was_complete and not card.is_complete:
        from .recurring import undo_spawn
        undo_spawn(card)
    if just_completed:
        from .features import move_to_done
        if move_to_done(card, user):
            automation.fire(card, Rule.MOVED, list_=card.list, actor=user)
        from .recurring import spawn_next
        nxt = spawn_next(card, request.user)
        automation.fire(card, Rule.COMPLETED, actor=request.user)
        if nxt:
            data = card_response(card.pk, request)
            data["next_card"] = {"id": nxt.pk, "due": timezone.localtime(nxt.due_at).strftime("%a %-d %b")}
            return data
    return card_response(card.pk, request)


@api
def card_toggle_label(request, card_id):
    card = _card(request, card_id)
    label = get_object_or_404(Label, pk=request.data.get("label_id"), board=card.list.board)
    if card.labels.filter(pk=label.pk).exists():
        card.labels.remove(label)
        log(card.list.board, request.user, "label_removed", card=card, name=str(label) or label.get_colour_display())
    else:
        card.labels.add(label)
        log(card.list.board, request.user, "label_added", card=card, name=str(label) or label.get_colour_display())
        automation.fire(card, Rule.LABEL_ADDED, label=label, actor=request.user)
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
        automation.fire(card, Rule.MEMBER_ADDED, actor=request.user, member=person)
    if hasattr(card, "stockup"):
        from . import stockup as _su
        _su.after_card_change(card, request.user, {"members"})
    return card_response(card.pk, request)


# ---------- checklists ----------

@api
def checklist_create(request, card_id):
    card = _card(request, card_id)
    title = _text(request.data.get("title") or "Checklist", 120, "title")
    cl = Checklist.objects.create(card=card, title=title, position=positions.end_position(card.checklists.all()))
    items = [str(t).strip()[:250] for t in (request.data.get("items") or []) if str(t).strip()][:50]
    if request.data.get("copy_from"):     # "Copy items from…" another checklist the person can see
        src = Checklist.objects.filter(pk=request.data["copy_from"]).select_related("card__list__board").first()
        if src and permissions.can_view_card(src.card, request.user):
            items += [i.text for i in src.items.order_by("position")][:100]
    for i, text in enumerate(items, start=1):
        ChecklistItem.objects.create(checklist=cl, text=text, position=i * positions.GAP)
    log(card.list.board, request.user, "checklist_added", card=card, title=title)
    return card_response(card.pk, request)


@api
def attachment_link(request, card_id):
    """Attach a link (Trello's "Search or paste a link")."""
    from urllib.parse import urlparse
    from .models import Attachment
    card = _card(request, card_id)
    url = (request.data.get("url") or "").strip()
    if url.startswith("/"):
        url = settings.SITE_URL + url
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > 1000:
        raise ApiError("Paste a full web address starting with https://")
    name = (request.data.get("name") or "").strip()[:200] or (parts.netloc.removeprefix("www.") + parts.path).rstrip("/")[:200]
    Attachment.objects.create(card=card, url=url, name=name, content_type="text/uri-list", uploaded_by=request.user)
    log(card.list.board, request.user, "attachment_added", card=card, name=name)
    automation.fire(card, Rule.ATTACHMENT, actor=request.user)
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
    """Add one item — or one per line when several are pasted. '1 x Chair' = per one of the card's
    amount, '20 kg Salad' = a total to count towards, '[x] …' = already done, anything else = a tick."""
    from . import products
    cl = _checklist(request, checklist_id)
    raw = str(request.data.get("text") or "")
    lines = [ln for ln in raw.splitlines() if ln.strip()][:100] or [""]
    planned = _planned(cl.card)
    if request.data.get("preview"):            # how the line will be read — shown under the box as you type
        it = products.parse_item(lines[0][:250], planned)
        am = cl.card.amount()
        return {"hint": products.explain_item(it, am), "lines": len(lines) if raw.strip() else 0}
    pos = positions.end_position(cl.items.all())
    for ln in lines:
        _text(ln, 250)                      # same empty / length checks as before
        it = products.make_item(cl, products.parse_item(ln[:250], planned), request.user, pos)
        pos += positions.GAP
        log(cl.card.list.board, request.user, "item_added", card=cl.card, text=it.text, checklist=cl.title)
    return card_response(cl.card_id, request)


@api
def card_amount(request, card_id):
    """How many this card is for ('430 cakes', '120 guests'). Lines with a per-one amount follow it."""
    from . import products
    card = _card(request, card_id)
    raw = str(request.data.get("amount") or "").strip()
    amount = _dec(raw, "Amount") if raw else None
    if amount is not None and amount < 0:
        raise ApiError("The amount can't be negative.")
    unit = request.data.get("unit")
    products.set_amount(card, amount, None if unit is None else str(unit).strip())
    n = rescale_items(card)
    log(card.list.board, request.user, "amount_set", card=card,
        amount=ChecklistItem.fmt_qty(amount) if amount is not None else "", unit=str(unit or "").strip())
    data = card_response(card.pk, request)
    data["rescaled"] = n
    return data


@api
def item_update(request, item_id):
    item = get_object_or_404(ChecklistItem.objects.select_related("checklist__card__list__board"), pk=item_id)
    card = item.checklist.card
    _edit(card.list.board, request.user)
    if item.checklist.title == "Made in StockUp" and hasattr(card, "stockup") and not request.data.get("delete") \
            and any(k in request.data for k in ("qty_done", "qty_target", "qty_unit", "is_done", "done", "text")):
        from . import stockup
        try:
            stockup.refresh_card(card)          # put StockUp's real figure back
        except Exception:
            pass
        raise ApiError("This counts up from StockUp on its own — record batches in StockUp, not here.", 409)
    if request.data.get("delete"):
        log(card.list.board, request.user, "item_deleted", card=card, text=item.text, checklist=item.checklist.title)
        item.delete()
        from .features import settle
        settle(Card.objects.get(pk=card.pk), request.user)
        return card_response(card.pk, request)
    if "move" in request.data:
        _move_item(item, card, request.data["move"] or {})
        return card_response(card.pk, request)
    if "text" in request.data:
        item.text = _text(request.data["text"], 250)
        if request.data.get("reparse") and not item.has_qty:
            _amount_from_text(item, card)
    if "assignee_id" in request.data:
        uid = request.data["assignee_id"]
        if uid:
            if int(uid) not in _card_people_ids(card):
                raise ApiError("Share the board with them first.")
            if item.assignee_id != int(uid):
                notify.send(User.objects.get(pk=uid), Notification.ITEM_ASSIGNED, actor=request.user, card=card,
                            text=f"assigned you “{item.text}” on {card.title}")
            if item.assignee_id != int(uid):
                log(card.list.board, request.user, "item_assigned", card=card, text=item.text,
                    name=User.objects.get(pk=uid).display_name)
            item.assignee_id = uid
        else:
            if item.assignee_id:
                log(card.list.board, request.user, "item_unassigned", card=card, text=item.text)
            item.assignee = None
    if "due_at" in request.data:
        if request.data["due_at"]:
            due = parse_datetime(request.data["due_at"])
            if not due:
                raise ApiError("Couldn't read that date.")
            new_due = timezone.make_aware(due) if timezone.is_naive(due) else due
            if new_due != item.due_at:
                log(card.list.board, request.user, "item_due", card=card, text=item.text,
                    due=timezone.localtime(new_due).strftime("%-d %b, %H:%M"))
            item.due_at = new_due
        else:
            item.due_at = None
    if "reminder_minutes" in request.data:
        try:
            r = int(request.data["reminder_minutes"])
        except (TypeError, ValueError):
            r = 1440
        item.reminder_minutes = r if r in dict(Card.REMINDERS) else 1440
    qty_changed = _item_qty(item, request.data, card)
    if qty_changed:
        log(card.list.board, request.user, "item_qty", card=card, text=item.text,
            done=item.qty_done_txt, target=item.qty_target_txt, unit=item.qty_unit)
    if "is_done" in request.data and bool(request.data["is_done"]) != item.is_done:
        item.is_done = bool(request.data["is_done"])
        if item.is_done and item.has_qty and item.qty_done < item.qty_target:
            item.qty_done = item.qty_target          # ticking a quantity line means all of it is done
        item.done_by = request.user if item.is_done else None
        item.done_at = timezone.now() if item.is_done else None
        log(card.list.board, request.user, "item_done" if item.is_done else "item_undone", card=card, text=item.text)
    elif qty_changed and item.has_qty:
        done = item.qty_done >= item.qty_target
        if done != item.is_done:
            item.is_done = done
            item.done_by = request.user if done else None
            item.done_at = timezone.now() if done else None
    item.save()
    job = None
    if "is_done" in request.data:
        from . import production
        job = production.on_item_toggle(item, item.is_done)
    if request.data.get("is_done") and automation.checklists_all_done(card):
        automation.fire(card, Rule.CHECKLIST_DONE, actor=request.user)
    if "is_done" in request.data or qty_changed:
        from .features import settle, start_doing
        progressed = item.is_done or (item.has_qty and item.qty_done > 0)
        card.refresh_from_db()
        if progressed and not card.is_complete:
            start_doing(card, request.user)
        settle(card, request.user)
    data = card_response(card.pk, request)
    if job:
        data["refresh_board"] = True
    return data


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
    automation.fire(card, Rule.COMMENT, actor=request.user)
    mentioned = notify.find_mentions(comment.body, people)
    if mentioned:
        card.refresh_from_db()
        automation.fire(card, Rule.MENTIONED, actor=request.user, mentioned=mentioned)
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
           "roles": BoardMember.ROLES, "can_manage": permissions.can_manage(board, request.user),
           "can_manage_members": permissions.can_manage_members(board, request.user)}
    return render_to_string("boards/_share_members.html", ctx, request=request)


@api
def board_share(request, board_id):
    board = get_object_or_404(Board, pk=board_id)
    if not permissions.can_manage_members(board, request.user):
        raise ApiError("Only board admins can share this board." if board.member_permission == "admins"
                       else "Observers can't add people to this board.", 403)
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
        from . import mailer
        e = mailer.invite(person, request.user, f"the board {board.title}")
        new_account["emailed"] = bool(e and e.sent_at)
    role = request.data.get("role") if request.data.get("role") in dict(BoardMember.ROLES) else BoardMember.MEMBER
    if role == BoardMember.ADMIN and not permissions.can_manage(board, request.user):
        role = BoardMember.MEMBER          # members who can add people can't make admins
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
    member_removing = (request.data.get("remove") and membership.role != BoardMember.ADMIN
                       and permissions.can_manage_members(board, request.user))
    if not (leaving or member_removing or permissions.can_manage(board, request.user)):
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


@login_required
def recent_boards(request):
    ids = list(request.user.recent_boards or [])
    boards = {b.pk: b for b in permissions.visible_boards(request.user).filter(pk__in=ids).select_related("workspace")}
    return JsonResponse({"boards": [{"id": b.pk, "title": b.title, "where": b.workspace.name, "bg": b.background}
                                    for b in (boards.get(i) for i in ids) if b]})


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
    for flag in ("show_complete", "covers_enabled", "complete_to_done", "progress_to_doing"):
        if flag in d:
            setattr(board, flag, bool(d[flag]))
    if d.get("comment_permission") in dict(Board.COMMENTS):
        board.comment_permission = d["comment_permission"]
    if d.get("member_permission") in dict(Board.MEMBER_PERMS):
        board.member_permission = d["member_permission"]
    if "public" in d:
        import secrets
        board.public_token = secrets.token_urlsafe(18) if d["public"] else ""
        audit.record(request, "board_public_on" if d["public"] else "board_public_off", board=board)
    if "is_project" in d:
        board.is_project = bool(d["is_project"])
    if "project_owner" in d:
        owner = d["project_owner"]
        board.project_owner = (BoardMember.objects.filter(board=board, user_id=owner).select_related("user").first().user
                               if owner and BoardMember.objects.filter(board=board, user_id=owner).exists() else None)
    if "budget" in d:
        raw = str(d["budget"] or "").replace(",", "").replace("£", "").strip()
        try:
            board.budget = round(float(raw), 2) if raw else None
        except ValueError:
            raise ApiError("The budget needs to be an amount in pounds.")
        if board.budget is not None and board.budget < 0:
            raise ApiError("The budget can't be negative.")
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
    moved = False
    if d.get("workspace") and str(d["workspace"]) != str(board.workspace_id):
        from .models import Workspace
        ws = get_object_or_404(Workspace, pk=d["workspace"])
        if not permissions.can_create_board(ws, request.user):
            raise ApiError("You can only move boards into departments where you can create boards.", 403)
        audit.record(request, "board_moved", board=board, old=board.workspace.name, new=ws.name)
        board.workspace, board.collection, moved = ws, None, True
    if "archive" in d and bool(d["archive"]) != board.is_archived:
        if not permissions.can_archive_board(board, request.user):
            raise ApiError(f"Only {permissions.who_can('archive_boards')} can close boards.", 403)
        board.is_archived = bool(d["archive"])
        audit.record(request, "board_archived" if board.is_archived else "board_restored", board=board)
    board.save()
    if moved:                                # department-wide rules: leave the old department's, take the new one's
        from . import shared_rules
        Rule.objects.filter(board=board, source__isnull=False).delete()
        for r in Rule.objects.filter(board=board, everywhere=True, source__isnull=True):
            shared_rules.sync(r)
        shared_rules.apply_to_new_board(board)
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
        automation.fire(card, Rule.FIELD_SET, actor=request.user, field=field)
        if field.kind == CustomField.NUMBER and field.name.strip().lower() in ("planned", "quantity", "target"):
            rescale_items(card)
    return card_response(card.pk, request)


# ---------- copying and templates ----------

@api
def card_copy(request, card_id):
    card = _card(request, card_id)
    d = request.data
    target = get_object_or_404(List.objects.select_related("board"), pk=d.get("list_id") or card.list_id,
                               is_archived=False, board__is_archived=False)
    if not permissions.can_view(target.board, request.user):
        raise Http404
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
    tpl = get_object_or_404(Card.objects.select_related("list__board"), pk=request.data.get("template_id"),
                            is_template=True, is_archived=False, list__board__workspace=lst.board.workspace)
    if not permissions.can_view(tpl.list.board, request.user):
        raise Http404
    from .copying import copy_card, cross_board_maps
    if tpl.list.board_id == lst.board_id:
        new = copy_card(tpl, lst, user=request.user, keep_members=True, keep_dates=False)
    else:                                    # a template from another board in the department
        label_map, field_map = cross_board_maps(tpl.list.board, lst.board)
        new = copy_card(tpl, lst, user=request.user, keep_members=False, keep_dates=False,
                        label_map=label_map, field_map=field_map)
    raw = str(request.data.get("amount") or "").strip()
    if raw:                                  # "How many?" — every per-one line scales to it
        from . import products
        products.set_amount(new, _dec(raw, "Amount"))
        rescale_items(new)
    automation.fire(new, Rule.ADDED, list_=lst, actor=request.user)
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
    else:
        if "url" in request.data and att.url:
            url = (request.data.get("url") or "").strip()[:1000]
            if not re.match(r"^https?://[^\s]+$", url, re.I):
                raise ApiError("Links need to start with http:// or https://")
            att.url = url
        if "name" in request.data:
            att.name = (request.data.get("name") or "").strip()[:255] or (att.link_host if att.url else att.name)
        att.save()
        log(card.list.board, request.user, "attachment_renamed", card=card, name=att.name)
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
    button = get_object_or_404(Button, pk=button_id, board=card.list.board, kind=Button.CARD, is_enabled=True)
    done = automation.press_card_button(button, card, request.user)
    data = card_response(card.pk, request)
    data["summary"] = "; ".join(done) or "Nothing to change."
    return data


@api
def board_button_press(request, board_id, button_id):
    from .models import Button
    board = get_object_or_404(Board, pk=board_id)
    _edit(board, request.user)
    button = get_object_or_404(Button, pk=button_id, board=board, kind=Button.BOARD, is_enabled=True)
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
    blockers = card.delete_blockers()
    if blockers:
        raise ApiError(f"This card has {', '.join(blockers)} in it, so it can only be archived — "
                       "nothing with work in it is deleted for good.", 409)
    if (request.data.get("confirm") or "").strip().upper() != "DELETE CARD":
        raise ApiError("Type DELETE CARD to confirm.")
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


# ---------- more list actions ----------

def _list_for_edit(request, list_id):
    lst = get_object_or_404(List.objects.select_related("board"), pk=list_id)
    _edit(lst.board, request.user)
    return lst


@api
def list_copy(request, list_id):
    """Copy a list and its cards, right after the original."""
    from .copying import copy_card
    lst = _list_for_edit(request, list_id)
    title = _text(request.data.get("title") or f"{lst.title} (copy)", 120, "title")
    siblings = list(lst.board.lists.filter(is_archived=False).order_by("position", "id"))
    idx = next(i for i, x in enumerate(siblings) if x.pk == lst.pk)
    after = siblings[idx + 1] if idx + 1 < len(siblings) else None
    new = List.objects.create(board=lst.board, title=title, colour=lst.colour, card_limit=lst.card_limit,
                              position=positions.between(lst.position, after.position if after else None))
    for c in cards_qs().filter(list=lst, is_archived=False).order_by("position"):
        copy_card(c, new, user=request.user, position=c.position)
    log(lst.board, request.user, "list_created", title=title)
    automation.fire_list_created(new, request.user)
    return {"ok": True}


@api
def list_move_board(request, list_id):
    """Move a whole list (with its cards) to another board."""
    from .copying import transfer_card
    lst = _list_for_edit(request, list_id)
    target = get_object_or_404(Board, pk=request.data.get("board_id"), is_archived=False)
    if target.pk == lst.board_id:
        raise ApiError("It's already on this board.")
    if not permissions.can_view(target, request.user):
        raise Http404
    if not permissions.can_edit(target, request.user):
        raise ApiError("You can't add lists to that board.", 403)
    new = List.objects.create(board=target, title=lst.title, colour=lst.colour, card_limit=lst.card_limit,
                              position=positions.end_position(target.lists.filter(is_archived=False)))
    moved = 0
    for c in Card.objects.filter(list=lst).order_by("position"):
        transfer_card(c, new, user=request.user)
        moved += 1
    lst.is_archived = True
    lst.save(update_fields=["is_archived"])
    log(target, request.user, "list_created", title=new.title)
    return {"ok": True, "moved": moved, "board": target.title}


@api
def list_move_cards(request, list_id):
    lst = _list_for_edit(request, list_id)
    target = get_object_or_404(List, pk=request.data.get("list_id"), board=lst.board, is_archived=False)
    if target.pk == lst.pk:
        raise ApiError("Pick a different list.")
    n = 0
    for c in lst.cards.filter(is_archived=False).order_by("position"):
        c.list = target
        c.position = positions.end_position(target.cards.filter(is_archived=False))
        c.save(update_fields=["list", "position", "updated_at"])
        log(lst.board, request.user, "card_moved", card=c, **{"from": lst.title, "to": target.title})
        automation.fire(c, Rule.MOVED, list_=target, actor=request.user)
        n += 1
    return {"ok": True, "moved": n}


@api
def list_sort(request, list_id):
    lst = _list_for_edit(request, list_id)
    how = request.data.get("by")
    cards = list(lst.cards.filter(is_archived=False))
    from datetime import timedelta
    far = timezone.now() + timedelta(days=36500)
    keys = {
        "due": lambda c: (c.due_at or far, c.position),
        "newest": lambda c: -c.created_at.timestamp(),
        "oldest": lambda c: c.created_at.timestamp(),
        "name": lambda c: c.title.lower(),
    }
    if how not in keys:
        raise ApiError("Pick how to sort.")
    for i, c in enumerate(sorted(cards, key=keys[how]), start=1):
        Card.objects.filter(pk=c.pk).update(position=i * positions.GAP)
    from .signals import bump
    bump(lst.board_id)
    return {"ok": True}


@api
def list_archive_cards(request, list_id):
    lst = _list_for_edit(request, list_id)
    ids = list(lst.cards.filter(is_archived=False).values_list("pk", flat=True))
    n = lst.cards.filter(is_archived=False).update(is_archived=True, updated_at=timezone.now())
    if Rule.objects.filter(board=lst.board, trigger=Rule.ARCHIVED, is_enabled=True).exists():
        for c in Card.objects.filter(pk__in=ids):
            automation.fire(c, Rule.ARCHIVED, actor=request.user)
    from .signals import bump
    bump(lst.board_id)
    log(lst.board, request.user, "automation", summary=f"archived all {n} cards in {lst.title}")
    return {"ok": True, "archived": n}


@login_required
def editable_boards(request):
    """Boards the user can add to — for Move list / Switch boards."""
    from django.http import JsonResponse as J
    starred = set(BoardMember.objects.filter(user=request.user, starred=True).values_list("board_id", flat=True))
    out = []
    for b in permissions.visible_boards(request.user).filter(is_template=False).select_related("workspace"):
        out.append({"id": b.pk, "title": b.title, "dept": b.workspace.name, "starred": b.pk in starred,
                    "can_edit": permissions.can_edit(b, request.user), "bg": b.background})
    out.sort(key=lambda b: (not b["starred"], b["dept"].lower(), b["title"].lower()))
    return J({"boards": out})


def _dec(v, what):
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ApiError(f"{what} should be a number.")
    if abs(d) > Decimal("999999999"):
        raise ApiError(f"{what} is too big.")
    return d


def _planned(card):
    """The card's Planned (or Quantity) number field, if it has one."""
    from decimal import Decimal
    for v in card.field_values.select_related("field"):
        if v.field.kind == CustomField.NUMBER and v.field.name.strip().lower() in ("planned", "quantity", "target"):
            try:
                return Decimal(str(v.value))
            except Exception:
                return None
    return None


def _move_item(item, card, mv):
    """Drag a checklist item: {checklist_id, before_id, after_id} — the items it now sits between."""
    from . import positions
    cl = get_object_or_404(Checklist, pk=mv.get("checklist_id") or item.checklist_id, card=card)
    sibs = {i.pk: i for i in cl.items.exclude(pk=item.pk)}
    b, a = sibs.get(_int(mv.get("before_id"))), sibs.get(_int(mv.get("after_id")))
    if positions.needs_rebalance(b and b.position, a and a.position):
        for n, i in enumerate(sorted(sibs.values(), key=lambda i: (i.position, i.pk)), 1):
            i.position = n * positions.GAP
            i.save(update_fields=["position"])
    item.checklist = cl
    item.position = (positions.between(b.position if b else None, a.position if a else None)
                     if (b or a) else positions.end_position(cl.items.exclude(pk=item.pk)))
    item.save(update_fields=["checklist", "position"])


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _amount_from_text(item, card):
    """'430 Christmas Cake (Baked)' → text 'Christmas Cake (Baked)', 0 / 430. Returns True if it found one."""
    from decimal import Decimal

    from . import products
    it = products.parse_item(item.text, _planned(card))
    if it["target"] is None or it["done"]:
        return False
    item.text = it["text"][:250] or item.text
    item.qty_target = it["target"].quantize(Decimal("0.001"))
    item.qty_unit = (it["unit"] or "")[:20]
    item.per_unit = it["per_unit"] if it["kind"] == "per" else None
    if item.is_done:
        item.qty_done = item.qty_target
    return True


@api
def checklist_read_amounts(request, checklist_id):
    """Turn items written as '430 Christmas Cake' into amounts that count up, all at once."""
    cl = get_object_or_404(Checklist.objects.select_related("card__list__board"), pk=checklist_id)
    card = cl.card
    _edit(card.list.board, request.user)
    n = 0
    for item in cl.items.all():
        if not item.has_qty and item.per_unit is None and _amount_from_text(item, card):
            item.save()
            n += 1
    if n:
        log(card.list.board, request.user, "items_amounts", card=card, n=n)
    data = card_response(card.pk, request)
    data["converted"] = n
    return data


def _item_qty(item, d, card):
    """Quantity changes on a checklist item. Returns True if anything changed."""
    from decimal import Decimal
    changed = False
    if "qty_unit" in d:
        item.qty_unit = (d.get("qty_unit") or "").strip()[:20]
    if "per_unit" in d:
        raw = str(d.get("per_unit") or "").strip()
        item.per_unit = _dec(raw, "Per product") if raw else None
        if item.per_unit is not None:
            planned = _planned(card)
            if planned:
                item.qty_target = (planned * item.per_unit).quantize(Decimal("0.001"))
                changed = True
    if "qty_target" in d and not (item.per_unit is not None and "per_unit" in d):
        raw = str(d.get("qty_target") or "").strip()
        new = _dec(raw, "Quantity needed") if raw else None
        if new is not None and new < 0:
            raise ApiError("Quantity needed can't be negative.")
        if new != item.qty_target:
            item.qty_target, changed = new, True
    if "qty_add" in d or "qty_done" in d:
        if not item.has_qty:
            raise ApiError("Set how many are needed first.")
        if "qty_done" in d:
            new = _dec(d["qty_done"], "Done")
        else:
            new = item.qty_done + _dec(d["qty_add"], "That amount")
        new = max(Decimal("0"), new)
        if new != item.qty_done:
            item.qty_done, changed = new, True
    return changed


def rescale_items(card):
    """Planned changed: items with a per-product amount get a new target."""
    from decimal import Decimal
    planned = _planned(card)
    if not planned:
        return 0
    n = 0
    for item in ChecklistItem.objects.filter(checklist__card=card, per_unit__isnull=False):
        item.qty_target = (planned * item.per_unit).quantize(Decimal("0.001"))
        item.is_done = item.qty_done >= item.qty_target
        item.save(update_fields=["qty_target", "is_done"])
        n += 1
    return n


@api
def card_dependencies(request, card_id):
    """Make this card wait for another (finish-to-start), or stop waiting."""
    from .models import Dependency
    from .schedule import push_dependants, would_cycle
    card = _card(request, card_id)
    board = card.list.board
    _edit(board, request.user)
    d = request.data
    if d.get("remove"):
        Dependency.objects.filter(card=card, depends_on_id=d["remove"]).delete()
        log(board, request.user, "dependency_removed", card=card)
    else:
        other = Card.objects.filter(pk=d.get("depends_on") or 0, list__board=board, is_archived=False).first()
        if not other:
            raise ApiError("Pick a card on this board.")
        if would_cycle(card, other):
            raise ApiError(f"“{other.title}” already waits for this card, so that would go round in a circle.")
        try:
            lag = max(-30, min(int(d.get("lag_days") or 0), 365))
        except (TypeError, ValueError):
            lag = 0
        Dependency.objects.update_or_create(card=card, depends_on=other, defaults={"lag_days": lag})
        log(board, request.user, "dependency_added", card=card, name=other.title)
        moved = push_dependants(other)
        for c in moved:
            log(board, request.user, "due_set", card=c, due=timezone.localtime(c.due_at).strftime("%-d %b %Y, %H:%M"))
    from .signals import bump
    bump(board.pk)
    return card_response(card.pk, request)


@api
def board_status(request, board_id):
    """Project status update — anyone who can edit the board."""
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    _edit(board, request.user)
    d = request.data
    if d.get("rag") in dict(Board.RAG):
        board.rag = d["rag"]
    if "status_note" in d:
        board.status_note = (d.get("status_note") or "").strip()[:300]
    board.status_updated_at = timezone.now()
    board.save(update_fields=["rag", "status_note", "status_updated_at"])
    log(board, request.user, "status_updated", rag=board.get_rag_display(), note=board.status_note)
    return {"ok": True}


@api
def card_time(request, card_id):
    """Time on a card: start / stop the timer, add an entry, or delete one."""
    from datetime import date as _date
    from . import timetrack
    from .models import TimeEntry
    card = _card(request, card_id)
    d = request.data
    action = d.get("action")
    if action == "start":
        _, stopped = timetrack.start(card, request.user)
        extra = {"stopped": stopped.card.title} if stopped else {}
        return {**card_response(card.pk, request), **extra, "timer": True}
    if action == "stop":
        e = timetrack.running(request.user)
        if e:
            timetrack.stop(e)
    elif action == "add":
        mins = timetrack.parse_minutes(d.get("time"))
        if not mins or mins <= 0 or mins > 24 * 60:
            raise ApiError("Type the time like 1h 30m, 1:30 or 1.5.")
        try:
            day = _date.fromisoformat(d.get("day")) if d.get("day") else timezone.localdate()
        except ValueError:
            raise ApiError("That date doesn't look right.")
        if day > timezone.localdate():
            raise ApiError("You can't log time in the future.")
        TimeEntry.objects.create(card=card, user=request.user, day=day, minutes=mins,
                                 note=(d.get("note") or "").strip()[:200])
        log(card.list.board, request.user, "time_logged", card=card, time=timetrack.fmt(mins))
    elif action == "delete":
        e = get_object_or_404(TimeEntry, pk=d.get("id") or 0, card=card)
        if e.user_id != request.user.pk and not permissions.can_manage(card.list.board, request.user):
            raise ApiError("You can only remove your own time.", 403)
        e.delete()
    else:
        raise ApiError("Unknown action.")
    return card_response(card.pk, request)


@api
def time_stop(request):
    """Stop my running timer (from the top bar)."""
    from . import timetrack
    e = timetrack.running(request.user)
    if not e:
        return {"ok": True, "minutes": 0}
    timetrack.stop(e)
    return {"ok": True, "minutes": e.minutes, "card": e.card.title}


def can_set_people(viewer, person):
    """Superusers, and admins of a department the person belongs to."""
    from .workload import _can_set
    return _can_set(viewer, person)


@api
def user_capacity(request, user_id):
    """Working hours a week (the person themselves or a manager) and hourly rate (managers only)."""
    from decimal import Decimal, InvalidOperation
    person = get_object_or_404(User, pk=user_id, is_active=True)
    manager = can_set_people(request.user, person)
    if person.pk != request.user.pk and not manager:
        raise ApiError("Only a department admin can change someone else's hours.", 403)
    d = request.data
    fields = []
    try:
        if "capacity_hours" in d:
            v = Decimal(str(d["capacity_hours"]).strip() or "0")
            if not 0 <= v <= 80:
                raise ApiError("Hours a week should be between 0 and 80.")
            person.capacity_hours = v
            fields.append("capacity_hours")
        if "hourly_rate" in d:
            if not manager:
                raise ApiError("Only a department admin can set pay rates.", 403)
            raw = str(d["hourly_rate"] or "").strip().lstrip("£")
            v = Decimal(raw) if raw else None
            if v is not None and not 0 <= v <= 1000:
                raise ApiError("That rate doesn't look right.")
            person.hourly_rate = v
            fields.append("hourly_rate")
    except InvalidOperation:
        raise ApiError("Type a number.")
    if fields:
        person.save(update_fields=fields)
    return {"ok": True, "capacity_hours": str(person.capacity_hours),
            "hourly_rate": str(person.hourly_rate) if person.hourly_rate is not None and manager else None}


@api
def board_costs(request, board_id):
    """Add or remove a cost on a project board. Warns the owner and admins when spending passes 90%."""
    from datetime import date as _date
    from decimal import Decimal, InvalidOperation
    from .models import CostItem
    from .portfolio import spend
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    _edit(board, request.user)
    d = request.data
    before = spend(board)[2]
    if d.get("remove"):
        get_object_or_404(CostItem, pk=d["remove"], board=board).delete()
        return {"ok": True}
    desc = (d.get("description") or "").strip()[:200]
    if not desc:
        raise ApiError("Say what the cost is for.")
    try:
        amount = Decimal(str(d.get("amount") or "").replace(",", "").strip().lstrip("£"))
    except InvalidOperation:
        raise ApiError("Type the amount as a number, like 120.50.")
    if amount <= 0 or amount > Decimal("10000000"):
        raise ApiError("Type the amount as a number, like 120.50.")
    try:
        day = _date.fromisoformat(d["day"]) if d.get("day") else timezone.localdate()
    except ValueError:
        raise ApiError("That date doesn't look right.")
    cat = d.get("category") if d.get("category") in dict(CostItem.CATEGORIES) else "other"
    card = Card.objects.filter(pk=d.get("card") or 0, list__board=board).first()
    item = CostItem.objects.create(board=board, card=card, description=desc, category=cat, amount=amount,
                                   day=day, created_by=request.user)
    log(board, request.user, "cost_added", card=card, name=desc, amount=f"£{amount:,.2f}")
    _budget_warning(board, before, request.user)
    return {"ok": True, "id": item.pk}


def _budget_warning(board, before, actor):
    """Tell the owner and board admins once spending crosses 90% and again at 100%."""
    from . import notify
    from .portfolio import spend
    if not board.budget:
        return
    after = spend(board)[2]
    for pct, word in ((100, "is over budget"), (90, "has used 90% of its budget")):
        line = board.budget * pct / 100
        if before < line <= after:
            people = {m.user for m in board.memberships.filter(role="admin").select_related("user")}
            if board.project_owner:
                people.add(board.project_owner)
            for u in people:
                notify.send(u, "budget", actor=actor, board=board,
                            text=f"{board.title} {word}: £{after:,.0f} of £{board.budget:,.0f}",
                            dedupe_key=f"budget:{board.pk}:{pct}:{board.budget}")
            break


def _preview_card(p):
    from . import products
    f = lambda v: None if v is None else ChecklistItem.fmt_qty(v)  # noqa: E731
    return {"list": p.get("list"), "title": p["title"], "planned": f(p["planned"]), "unit": p["unit"],
            "fields": [{"name": k, "value": v} for k, v in p["fields"]], "notes": p["notes"],
            "sections": [{"title": s["title"], "items": [
                {"text": i["text"], "target": f(i["target"]), "unit": i["unit"], "per": f(i["per_unit"]),
                 "done": i["done"], "kind": i.get("kind", ""), "explain": products.explain_item(i, {"amount": f(p["planned"]), "unit": p["unit"]} if p["planned"] else None, "setup")}
                for i in s["items"]]} for s in p["sections"]]}


@api
def board_setup(request, board_id):
    """'Set up cards': several cards typed in one box (blank line between). preview → what would be made."""
    from . import products
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    _edit(board, request.user)
    text = (request.data.get("text") or "")[:60000]
    try:
        parsed = products.parse_many(text)
    except ValueError as e:
        raise ApiError(str(e))
    if request.data.get("preview"):
        return {"cards": [_preview_card(p) for p in parsed]}
    lst = board.lists.filter(pk=request.data.get("list_id") or 0, is_archived=False).first() \
        or board.lists.filter(is_archived=False).order_by("position").first()
    if not lst:
        lst = List.objects.create(board=board, title="To do", position=positions.GAP)
    cards = products.create_many(board, text, request.user, lst)
    for c in cards:
        log(board, request.user, "card_created", card=c, list=c.list.title)
        automation.fire(c, Rule.ADDED, list_=c.list, actor=request.user)
    from .signals import bump
    bump(board.pk)
    return {"ok": True, "created": len(cards), "card_ids": [c.pk for c in cards]}


@api
def board_setup_draft(request, board_id):
    """AI: turn anything pasted (text, screenshots) into the card text, for the person to check."""
    import base64
    from . import ai
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    _edit(board, request.user)
    text = str(request.data.get("text") or "")[:40000]
    images = []
    for img in (request.data.get("images") or [])[:4]:
        mt, data = str(img.get("media_type", "")), str(img.get("data", ""))
        if mt not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            raise ApiError("Images need to be PNG, JPEG, GIF or WebP.")
        if len(data) > 7_000_000:
            raise ApiError("That image is too big — keep it under 5 MB.")
        try:
            base64.b64decode(data[:100] + "=" * (-len(data[:100]) % 4), validate=False)
        except Exception:
            raise ApiError("That image couldn't be read.")
        images.append((mt, data))
    if not text.strip() and not images:
        raise ApiError("Paste or describe what you want to set up, or add a screenshot.")
    try:
        draft = ai.draft_cards(text, images)
    except ai.AIUnavailable as e:
        raise ApiError(str(e), 503)
    return {"text": draft}



def board_templates(request, board_id):
    """Card templates for 'Create from template': this board's first, then the department's other boards."""
    from django.http import JsonResponse
    if not request.user.is_authenticated:
        raise Http404
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    if not permissions.can_view(board, request.user):
        raise Http404
    visible = permissions.visible_boards(request.user).filter(workspace=board.workspace).values_list("pk", flat=True)
    cards = (Card.objects.filter(is_template=True, is_archived=False, list__is_archived=False, list__board__in=list(visible))
             .select_related("list__board").prefetch_related("checklists__items").order_by("list__board__title", "title"))
    out = [{"id": c.pk, "title": c.title, "board": c.list.board.title, "here": c.list.board_id == board.pk,
            "per_one": c.per_one_lines()} for c in cards[:200]]
    out.sort(key=lambda t: (not t["here"], t["board"].lower(), t["title"].lower()))
    return JsonResponse({"templates": out, "department": board.workspace.name})

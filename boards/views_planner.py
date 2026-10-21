"""Inbox (private to-dos) and Planner (a week of everything that's yours)."""
import json
from datetime import date, timedelta
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ai, automation, permissions, positions
from .models import BoardMember, Card, ChecklistItem, InboxItem, List, Rule
from .services import card_response, cards_qs, log


def _json_view(view):
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
                return JsonResponse(view(request, *args, **kwargs))
        except ai.AIUnavailable as e:
            return JsonResponse({"error": str(e)}, status=503)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
    return wrapper


def _inbox(user):
    return InboxItem.objects.filter(owner=user)


def inbox_html(request):
    items = _inbox(request.user).filter(is_done=False)
    done = _inbox(request.user).filter(is_done=True).order_by("-id")[:10]
    return render_to_string("boards/_inbox_items.html", {"items": items, "done": done}, request=request)


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("Couldn't read that date.")


def editable_lists(user):
    """Lists the user can turn inbox items into cards on, grouped by board."""
    boards = [b for b in permissions.visible_boards(user).select_related("workspace").filter(is_template=False)
              if permissions.can_edit(b, user)]
    lists = List.objects.filter(board__in=boards, is_archived=False).select_related("board").order_by(
        "board__title", "position")
    groups = {}
    for lst in lists:
        groups.setdefault(lst.board, []).append(lst)
    return list(groups.items())


# ---------- pages ----------

@login_required
def planner(request):
    automation.maybe_run_schedules()
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = today
    start -= timedelta(days=start.weekday())
    days = [start + timedelta(days=i) for i in range(7)]
    end = days[-1]
    user = request.user
    boards = permissions.visible_boards(user)

    my_cards = list(cards_qs().filter(members=user, is_archived=False, is_template=False, list__is_archived=False,
                                      list__board__in=boards).order_by("due_at"))
    my_items = list(ChecklistItem.objects.filter(
        assignee=user, is_done=False, checklist__card__is_archived=False, checklist__card__is_template=False,
        checklist__card__list__is_archived=False, checklist__card__list__board__in=boards,
    ).select_related("checklist__card__list__board").order_by("due_at"))
    inbox = list(_inbox(user).filter(is_done=False))

    by_day = {d: [] for d in days}
    overdue, unscheduled = [], []

    def place(kind, obj, d):
        entry = {"kind": kind, "obj": obj}
        if d is None:
            unscheduled.append(entry)
        elif d < today and not getattr(obj, "is_complete", False):
            if start <= d <= end:
                by_day[d].append(entry)
            else:
                overdue.append(entry)
        elif start <= d <= end:
            by_day[d].append(entry)

    for c in my_cards:
        place("card", c, timezone.localtime(c.due_at).date() if c.due_at else None)
    for it in my_items:
        place("item", it, timezone.localtime(it.due_at).date() if it.due_at else None)
    for i in inbox:
        if i.due_date:
            place("inbox", i, i.due_date)

    return render(request, "boards/planner.html", {
        "days": [{"date": d, "entries": by_day[d], "today": d == today, "past": d < today} for d in days],
        "overdue": overdue,
        "unscheduled": [e for e in unscheduled if e["kind"] != "inbox"],
        "inbox_html": inbox_html(request),
        "prev": (start - timedelta(days=7)).isoformat(),
        "next": (start + timedelta(days=7)).isoformat(),
        "start": start,
        "end": end,
        "ai_enabled": ai.enabled(),
        "list_groups": editable_lists(user),
    })


@login_required
def inbox_panel(request):
    """Inbox HTML for the slide-out panel on board pages."""
    return JsonResponse({"html": inbox_html(request), "ai": ai.enabled()})


# ---------- inbox API ----------

@_json_view
def inbox_add(request):
    d = request.data
    raw = (d.get("text") or "").strip()
    if not raw:
        raise ValueError("Type something to add.")
    lines = [ln.strip(" \t-•*·–—").strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln][:50]
    pos = positions.end_position(_inbox(request.user))
    for i, line in enumerate(lines):
        InboxItem.objects.create(owner=request.user, text=line[:250], position=pos + i * positions.GAP,
                                 due_date=_parse_date(d.get("due_date")), source="list" if len(lines) > 1 else "typed")
    return {"html": inbox_html(request), "added": len(lines)}


@_json_view
def inbox_capture(request):
    text = (request.data.get("text") or "").strip()
    if len(text) < 5:
        raise ValueError("Paste an email or message first.")
    found = ai.capture_todos(text, today=timezone.localdate())
    pos = positions.end_position(_inbox(request.user))
    for i, item in enumerate(found):
        InboxItem.objects.create(owner=request.user, text=item["text"], notes=item["notes"], due_date=item["due"],
                                 position=pos + i * positions.GAP, source="ai")
    return {"html": inbox_html(request), "added": len(found)}


@_json_view
def inbox_update(request, item_id):
    item = get_object_or_404(InboxItem, pk=item_id, owner=request.user)
    d = request.data
    if d.get("delete"):
        item.delete()
        return {"html": inbox_html(request)}
    if "text" in d:
        text = (d["text"] or "").strip()[:250]
        if not text:
            raise ValueError("Text can't be empty.")
        item.text = text
    if "notes" in d:
        item.notes = (d["notes"] or "")[:2000]
    if "due_date" in d:
        item.due_date = _parse_date(d["due_date"])
    if "is_done" in d:
        item.is_done = bool(d["is_done"])
    if "before_id" in d or "after_id" in d:
        positions.place(item, _inbox(request.user).filter(is_done=False), d.get("before_id"), d.get("after_id"))
    item.save()
    return {"html": inbox_html(request)}


@_json_view
def inbox_to_card(request, item_id):
    """Turn an inbox item into a card on a list the user can edit."""
    item = get_object_or_404(InboxItem, pk=item_id, owner=request.user)
    lst = get_object_or_404(List.objects.select_related("board"), pk=request.data.get("list_id"), is_archived=False)
    if not permissions.can_view(lst.board, request.user):
        raise Http404
    if not permissions.can_edit(lst.board, request.user):
        raise ValueError("You can't add cards to that board.")
    due = None
    if item.due_date:
        due = timezone.make_aware(timezone.datetime(item.due_date.year, item.due_date.month, item.due_date.day, 17))
    card = Card(list=lst, title=item.text, description=item.notes, due_at=due, created_by=request.user)
    positions.place_in_list(card, lst, request.data.get("before"), request.data.get("after"))
    card.save()
    if BoardMember.objects.filter(board=lst.board, user=request.user).exists():
        card.members.add(request.user)
    log(lst.board, request.user, "card_created", card=card, list=lst.title)
    automation.fire(card, Rule.ADDED, list_=lst, actor=request.user)
    item.delete()
    data = card_response(card.pk, request)
    data["inbox_html"] = inbox_html(request)
    data["board_id"] = lst.board_id
    return data

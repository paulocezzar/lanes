"""Today — the phone's home page (Paulo, 25 Oct 2026).

The phone is for CAPTURING and DOING; planning and building stay on the computer.
One page: a big "Add to Inbox" box (type or speak), what's due today or overdue
for me across every board (tick it off here), and my Inbox below it.
"""
from datetime import datetime, time, timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from . import ai, permissions
from .models import ChecklistItem, Notification
from .services import cards_qs
from .views_planner import editable_lists, inbox_html


@login_required
def today(request):
    user = request.user
    day = timezone.localdate()
    tomorrow = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min))
    boards = permissions.visible_boards(user)
    cards = list(cards_qs().filter(
        members=user, is_archived=False, is_template=False, is_complete=False, list__is_archived=False,
        list__board__in=boards, due_at__lt=tomorrow).order_by("due_at"))
    items = list(ChecklistItem.objects.filter(
        assignee=user, is_done=False, due_at__lt=tomorrow, checklist__card__is_archived=False,
        checklist__card__is_template=False, checklist__card__list__is_archived=False,
        checklist__card__list__board__in=boards,
    ).select_related("checklist__card__list__board").order_by("due_at"))
    from .features import is_done_list
    cards = [c for c in cards if not is_done_list(c.list)]          # sitting in Done = finished
    items = [i for i in items if not is_done_list(i.checklist.card.list)]
    entries = [{"kind": "card", "obj": c, "due": c.due_at} for c in cards] + \
              [{"kind": "item", "obj": i, "due": i.due_at} for i in items]
    entries.sort(key=lambda e: e["due"])
    for e in entries:
        e["overdue"] = timezone.localtime(e["due"]).date() < day
    prefill = " ".join(x for x in (request.GET.get("title", ""), request.GET.get("text", ""),
                                   request.GET.get("url", "")) if x).strip()[:2000]
    return render(request, "boards/today.html", {
        "overdue": [e for e in entries if e["overdue"]],
        "due_today": [e for e in entries if not e["overdue"]],
        "inbox_html": inbox_html(request),
        "ai_enabled": ai.enabled(),
        "unread": Notification.objects.filter(recipient=user, read_at__isnull=True).count(),
        "prefill": prefill,
        "focus_add": bool(request.GET.get("add") or prefill),
        "today": day,
        # The Inbox tab: the same capture box, then ONLY the Inbox — what you've
        # caught and still have to sort. Today = what's due; Inbox = what's loose.
        "inbox_only": request.GET.get("view") == "inbox",
        "list_groups": editable_lists(user),
    })

"""The bell: list, mark read, open."""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification


def _qs(user):
    return Notification.objects.filter(recipient=user).select_related("actor", "card", "board")


@login_required
def notifications_api(request):
    items = list(_qs(request.user)[:20])
    unread = Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()
    data = {"unread": unread}
    if request.GET.get("html"):
        data["html"] = render_to_string("boards/_notifications.html", {"items": items}, request=request)
    return JsonResponse(data)


@login_required
@require_POST
def notifications_read(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    qs = Notification.objects.filter(recipient=request.user, read_at__isnull=True)
    if body.get("id"):
        qs = qs.filter(pk=body["id"])
    qs.update(read_at=timezone.now())
    return JsonResponse({"unread": Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not n.read_at:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    if n.card_id and n.card and n.card.list.board_id:
        return redirect(f"{reverse('board', args=[n.card.list.board_id])}?card={n.card_id}")
    if n.board_id:
        return redirect("board", board_id=n.board_id)
    return redirect("notifications")


@login_required
def notifications_page(request):
    show = request.GET.get("show", "all")
    qs = _qs(request.user)
    if show == "unread":
        qs = qs.filter(read_at__isnull=True)
    return render(request, "boards/notifications.html", {"items": qs[:200], "show": show})

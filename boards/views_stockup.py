"""Pages and endpoints for the StockUp link (see boards/stockup.py)."""
import hmac
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import permissions, stockup
from .api import ApiError, _card, api
from .models import Board, StockUpLink, StockUpPerson, Workspace
from .services import card_response

User = get_user_model()


def _admin_of(user):
    if user.is_superuser:
        return list(Workspace.objects.order_by("name"))
    return list(Workspace.objects.filter(memberships__user=user, memberships__role="admin").order_by("name"))


@login_required
def stockup_page(request):
    ctx = {"enabled": stockup.enabled(), "plans": [], "error": "", "depts": _admin_of(request.user),
           "people": [], "links": {}, "users": []}
    if ctx["enabled"]:
        try:
            ctx["plans"] = stockup.plans()
        except stockup.StockUpError as e:
            ctx["error"] = str(e)
    boards = {b.stockup_plan_id: b for b in Board.objects.filter(stockup_plan_id__isnull=False, is_archived=False)
              if permissions.can_view(b, request.user)}
    for p in ctx["plans"]:
        p["board"] = boards.get(p["id"])
    if request.user.is_superuser and ctx["enabled"] and not ctx["error"]:
        try:
            ctx["people"] = stockup.people()
        except stockup.StockUpError:
            pass
        ctx["links"] = {sp.user_id: sp for sp in StockUpPerson.objects.all()}
        ctx["users"] = list(User.objects.filter(is_active=True).order_by("first_name", "last_name"))
        ctx["linked_count"] = len(ctx["links"])
    return render(request, "boards/stockup.html", ctx)


@login_required
@require_POST
def make_plan_board(request, plan_id):
    ws = get_object_or_404(Workspace, pk=request.POST.get("workspace"))
    if ws not in _admin_of(request.user) and not permissions.is_workspace_member(ws, request.user):
        raise Http404
    existing = Board.objects.filter(stockup_plan_id=plan_id, is_archived=False).first()
    if existing and permissions.can_view(existing, request.user):
        return redirect("board", board_id=existing.pk)
    try:
        board = stockup.build_plan_board(plan_id, request.user, ws)
    except stockup.StockUpError as e:
        messages.error(request, str(e))
        return redirect("stockup")
    from . import audit
    audit.record(request, "stockup_board", board=board, plan=plan_id)
    return redirect("board", board_id=board.pk)


@login_required
@require_POST
def refresh_board(request, board_id):
    board = get_object_or_404(Board, pk=board_id, stockup_plan_id__isnull=False)
    if not permissions.can_edit(board, request.user):
        raise Http404
    try:
        n = stockup.refresh_plan_board(board, request.user)
        messages.success(request, f"Up to date with StockUp{f' — {n} new card' + ('s' if n != 1 else '') if n else ''}.")
    except stockup.StockUpError as e:
        messages.error(request, str(e))
    return redirect("board", board_id=board.pk)


@login_required
@require_POST
def people_link(request):
    if not request.user.is_superuser:
        raise Http404
    if request.POST.get("action") == "match":
        try:
            n = stockup.match_people()
            messages.success(request, f"Matched {n} more {'person' if n == 1 else 'people'} by email or name.")
        except stockup.StockUpError as e:
            messages.error(request, str(e))
        return redirect("/stockup/#people")
    user = get_object_or_404(User, pk=request.POST.get("user_id"))
    pid = request.POST.get("person_id")
    StockUpPerson.objects.filter(user=user).delete()
    if pid:
        p = next((p for p in stockup.people() if str(p["id"]) == pid), None)
        if p:
            StockUpPerson.objects.filter(person_id=p["id"]).delete()
            StockUpPerson.objects.create(user=user, person_id=p["id"], person_name=p["name"],
                                         department=p.get("department", ""))
    messages.success(request, "Saved.")
    return redirect("/stockup/#people")


@login_required
def items_json(request):
    """Plans and, for one plan, what it still needs — for the card's StockUp picker."""
    try:
        if request.GET.get("plan"):
            d = stockup.plan_items(int(request.GET["plan"]))
            return JsonResponse({"items": [{
                "owner": i["owner"], "owner_id": i["owner_id"], "name": i["name"], "code": i.get("code", ""),
                "unit": i["unit"], "kind": i.get("kind", ""), "still": i.get("still_to_make"),
                "scheduled": i.get("already_scheduled")} for i in d.get("items", [])]})
        return JsonResponse({"plans": stockup.plans()})
    except (stockup.StockUpError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=502)


@api
def card_link(request, card_id):
    card = _card(request, card_id)
    if not permissions.can_edit(card.list.board, request.user):
        raise ApiError("Only people on the board can do that.", 403)
    d = request.data
    try:
        if d.get("unlink"):
            stockup.unlink_card(card, request.user)
        elif d.get("refresh"):
            link = getattr(card, "stockup", None)
            if link and link.run_id:
                stockup.sync(run_ids=[link.run_id], job_ids=[link.job_id] if link.job_id else [])
        elif d.get("qty") and hasattr(card, "stockup") and not d.get("plan_id"):
            stockup.change_run(card.stockup, request.user, day=False, qty=Decimal(str(d["qty"])))
        else:
            try:
                qty = Decimal(str(d.get("qty")))
            except (InvalidOperation, TypeError):
                raise ApiError("Enter how many.")
            if qty <= 0:
                raise ApiError("Enter how many.")
            day = None
            if d.get("date"):
                try:
                    day = date.fromisoformat(str(d["date"])[:10])
                except ValueError:
                    raise ApiError("Couldn't read that day.")
            stockup.link_card(card, request.user, d.get("plan_id"), d.get("owner"), d.get("owner_id"),
                              d.get("unit"), qty, day)
    except stockup.StockUpError as e:
        raise ApiError(str(e) or "StockUp said no.")
    return card_response(card.pk, request)


@csrf_exempt
def ping(request):
    """StockUp tells us which runs / jobs changed. Same shared key as our calls to it."""
    if not stockup.enabled():
        raise Http404
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not given or not hmac.compare_digest(given, settings.STOCKUP_TOKEN):
        raise Http404
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    runs = [int(i) for i in body.get("runs", []) if str(i).isdigit()][:200]
    jobs = [int(i) for i in body.get("jobs", []) if str(i).isdigit()][:200]
    runs = [r for r in runs if StockUpLink.objects.filter(run_id=r).exists()]
    jobs = [j for j in jobs if StockUpLink.objects.filter(job_id=j).exists()]
    if not runs and not jobs:
        return JsonResponse({"ok": True, "known": 0})
    try:
        result = stockup.sync(run_ids=runs, job_ids=jobs)
    except stockup.StockUpError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, **result})

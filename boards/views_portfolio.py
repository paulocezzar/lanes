"""Portfolio (every project on one page) and the one-page status report."""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from . import permissions
from .models import Board, Workspace
from .portfolio import health, projects_for


@login_required
def portfolio_page(request):
    boards = projects_for(request.user)
    dept = request.GET.get("dept")
    if dept and dept.isdigit():
        boards = boards.filter(workspace_id=int(dept))
    rows = [health(b) for b in boards]
    rag = request.GET.get("rag")
    if rag in ("green", "amber", "red"):
        rows = [r for r in rows if r["rag"] == rag]
    counts = {k: sum(1 for r in rows if r["rag"] == k) for k in ("green", "amber", "red")}
    depts = Workspace.objects.filter(boards__in=projects_for(request.user)).distinct().order_by("name")
    return render(request, "boards/portfolio.html", {
        "rows": rows, "counts": counts, "depts": depts, "dept": dept, "rag": rag, "now": timezone.now(),
        "editable": {b.pk for b in boards if permissions.can_edit(b, request.user)},
    })


@login_required
def status_report(request, board_id):
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    if not permissions.can_view(board, request.user):
        raise Http404
    return render(request, "boards/status_report.html", {"h": health(board), "board": board, "now": timezone.now()})

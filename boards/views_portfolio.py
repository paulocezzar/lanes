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


def _dept_choices(user):
    return Workspace.objects.filter(boards__in=permissions.visible_boards(user)).distinct().order_by("name")


@login_required
def workload_page(request):
    from .workload import build
    dept = request.GET.get("dept")
    dept = int(dept) if dept and dept.isdigit() else None
    by = "day" if request.GET.get("by") == "day" else "week"
    data = build(request.user, dept=dept, by=by)
    sel = None
    p, i = request.GET.get("p"), request.GET.get("i")
    if p is not None and p.isdigit() and i and i.isdigit() and int(i) < len(data["periods"]):
        uid, idx = int(p), int(i)
        who = next((r["user"] for r in data["rows"] if r["user"].pk == uid), None)
        if uid == 0 or who:
            sel = {"uid": uid, "i": idx, "who": who, "period": data["periods"][idx], "items": data["detail"](uid, idx)}
    return render(request, "boards/workload.html", {**data, "dept": dept, "depts": _dept_choices(request.user),
                                                   "sel": sel, "is_manager": _is_manager(request.user)})


def _is_manager(user):
    from .models import WorkspaceMember
    return user.is_superuser or WorkspaceMember.objects.filter(user=user, role="admin").exists()


def _timesheet_people(viewer, dept):
    """Whose time the viewer may see: their own, plus — for department admins — their departments' people."""
    from django.contrib.auth import get_user_model
    from .models import WorkspaceMember
    User = get_user_model()
    if viewer.is_superuser:
        qs = User.objects.filter(is_active=True)
        if dept:
            qs = qs.filter(workspace_memberships__workspace_id=dept)
        return qs.distinct()
    admin_of = list(WorkspaceMember.objects.filter(user=viewer, role="admin").values_list("workspace_id", flat=True))
    if dept:
        admin_of = [w for w in admin_of if w == dept]
    if not admin_of:
        return User.objects.filter(pk=viewer.pk)
    return User.objects.filter(is_active=True, workspace_memberships__workspace_id__in=admin_of).distinct()


@login_required
def timesheets_page(request):
    import csv
    from collections import defaultdict
    from datetime import date, timedelta
    from django.http import HttpResponse
    from . import timetrack
    from .models import TimeEntry
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        start = today
    start = timetrack.week_start(start)
    end = start + timedelta(days=6)
    dept = request.GET.get("dept")
    dept = int(dept) if dept and dept.isdigit() else None
    people = list(_timesheet_people(request.user, dept).order_by("first_name", "email"))
    manager = len(people) > 1 or _is_manager(request.user)
    only = request.GET.get("person")
    if only and only.isdigit():
        people = [u for u in people if u.pk == int(only)] or people
    entries = (TimeEntry.objects.filter(user__in=people, day__gte=start, day__lte=end, minutes__gt=0)
               .select_related("user", "card__list__board__workspace").order_by("day", "user__first_name"))
    if request.GET.get("csv"):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="timesheet-{start.isoformat()}.csv"'
        w = csv.writer(resp)
        head = ["Date", "Person", "Email", "Department", "Board", "Card", "Hours", "Note"]
        if manager:
            head += ["Rate (£/h)", "Cost (£)"]
        w.writerow(head)
        for e in entries:
            h = round(e.minutes / 60, 2)
            row = [e.day.isoformat(), e.user.display_name, e.user.email, e.card.list.board.workspace.name,
                   e.card.list.board.title, e.card.title, h, e.note]
            if manager:
                rate = e.user.hourly_rate
                row += [rate or "", round(float(rate) * h, 2) if rate else ""]
            w.writerow(row)
        return resp
    days = [start + timedelta(days=i) for i in range(7)]
    grid = defaultdict(lambda: defaultdict(int))
    for e in entries:
        grid[e.user_id][e.day] += e.minutes
    rows = []
    for u in people:
        mins = [grid[u.pk].get(d, 0) for d in days]
        total = sum(mins)
        cap = float(u.capacity_hours or 0) * 60
        rows.append({"user": u, "days": mins, "total": total, "pct": round(100 * total / cap) if cap else 0})
    hidden = 0
    if manager and not only:
        show_all = bool(request.GET.get("all"))
        busy = [r for r in rows if r["total"]]
        idle = [r for r in rows if not r["total"]]
        if show_all:
            rows = busy + idle
        else:
            hidden = len(idle)
            rows = busy or [r for r in rows if r["user"].pk == request.user.pk]
    return render(request, "boards/timesheets.html", {
        "rows": rows, "days": days, "start": start, "end": end, "today": today,
        "prev": (start - timedelta(days=7)).isoformat(), "next": (start + timedelta(days=7)).isoformat(),
        "entries": entries, "manager": manager, "hidden": hidden, "dept": dept, "only": only,
        "depts": _dept_choices(request.user) if manager else [],
        "day_totals": [sum(r["days"][i] for r in rows) for i in range(7)],
        "grand": sum(r["total"] for r in rows), "fmt": timetrack.fmt,
    })


@login_required
def costs_page(request, board_id):
    from collections import defaultdict
    from .models import CostItem, TimeEntry
    from .portfolio import spend
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    if not permissions.can_view(board, request.user):
        raise Http404
    costs, labour, total = spend(board)
    by_cat = defaultdict(lambda: 0)
    items = list(CostItem.objects.filter(board=board).select_related("card", "created_by"))
    for c in items:
        by_cat[c.get_category_display()] += c.amount
    people = defaultdict(int)
    for uid, mins in TimeEntry.objects.filter(card__list__board=board, minutes__gt=0).values_list("user_id", "minutes"):
        people[uid] += mins
    from django.contrib.auth import get_user_model
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=people)}
    time_rows = sorted(({"user": users[k], "minutes": v} for k, v in people.items() if k in users),
                       key=lambda r: -r["minutes"])
    pct = round(total / board.budget * 100) if board.budget else None
    return render(request, "boards/costs.html", {
        "board": board, "items": items, "costs": costs, "labour": labour, "total": total, "pct": pct,
        "left": board.budget - total if board.budget else None, "by_cat": sorted(by_cat.items(), key=lambda kv: -kv[1]),
        "time_rows": time_rows, "hours": round(sum(people.values()) / 60, 1),
        "can_edit": permissions.can_edit(board, request.user), "categories": CostItem.CATEGORIES,
        "cards": board_cards_for_select(board), "today": timezone.localdate(),
    })


def board_cards_for_select(board):
    from .models import Card
    return (Card.objects.filter(list__board=board, is_archived=False, is_template=False, list__is_archived=False)
            .order_by("list__position", "position").select_related("list")[:300])

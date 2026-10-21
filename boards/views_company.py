"""Company admin pages: usage across Lanes and the audit log. Company admins
(superusers) only."""
import csv
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from .models import Activity, AuditEvent, Board, Card, Workspace

User = get_user_model()


def _admin_only(request):
    if not request.user.is_superuser:
        raise Http404


@login_required
def usage(request):
    _admin_only(request)
    now = timezone.now()
    d7, d30 = now - timedelta(days=7), now - timedelta(days=30)
    people = User.objects.filter(is_active=True)
    active30 = set(Activity.objects.filter(created_at__gte=d30, actor__isnull=False).values_list("actor_id", flat=True))
    live_cards = Card.objects.filter(is_archived=False, is_template=False, list__is_archived=False,
                                     list__board__is_archived=False)

    depts = []
    for ws in Workspace.objects.annotate(
            n_people=Count("memberships", distinct=True),
            n_boards=Count("boards", filter=Q(boards__is_archived=False), distinct=True)):
        member_ids = set(ws.memberships.values_list("user_id", flat=True))
        ws_cards = live_cards.filter(list__board__workspace=ws)
        depts.append({
            "ws": ws, "people": ws.n_people, "boards": ws.n_boards,
            "active": len(member_ids & active30),
            "open": ws_cards.filter(is_complete=False).count(),
            "overdue": ws_cards.filter(is_complete=False, due_at__lt=now).count(),
            "done30": Activity.objects.filter(board__workspace=ws, verb="card_completed", created_at__gte=d30).count(),
        })
    top_people = max((d["people"] for d in depts), default=0) or 1
    for d in depts:
        d["pct_active"] = round(100 * d["active"] / d["people"]) if d["people"] else 0

    # changes per day, last 30 days
    per_day = dict(Activity.objects.filter(created_at__gte=d30).annotate(day=TruncDate("created_at"))
                   .values_list("day").annotate(n=Count("id")).values_list("day", "n"))
    days = [(timezone.localdate() - timedelta(days=i)) for i in range(29, -1, -1)]
    top_day = max(per_day.values(), default=0) or 1
    chart = [{"day": d, "n": per_day.get(d, 0), "pct": round(100 * per_day.get(d, 0) / top_day)} for d in days]

    busiest = (Board.objects.filter(is_archived=False, activity__created_at__gte=d30)
               .annotate(n=Count("activity")).select_related("workspace").order_by("-n")[:10])
    quiet = (Board.objects.filter(is_archived=False, is_template=False)
             .exclude(activity__created_at__gte=d30).select_related("workspace").order_by("title")[:15])
    never = people.filter(last_login__isnull=True).order_by("first_name")

    return render(request, "boards/company_usage.html", {
        "tab": "usage",
        "people_total": people.count(),
        "guests": people.filter(is_guest=True).count(),
        "seen7": people.filter(last_login__gte=d7).count(),
        "seen30": people.filter(last_login__gte=d30).count(),
        "active30": len(active30),
        "boards_total": Board.objects.filter(is_archived=False, is_template=False).count(),
        "public_boards": Board.objects.filter(is_archived=False).exclude(public_token="").select_related("workspace"),
        "cards_open": live_cards.filter(is_complete=False).count(),
        "cards_overdue": live_cards.filter(is_complete=False, due_at__lt=now).count(),
        "created30": Card.objects.filter(created_at__gte=d30).count(),
        "done30": Activity.objects.filter(verb="card_completed", created_at__gte=d30).count(),
        "depts": depts, "top_people": top_people,
        "chart": chart, "busiest": busiest, "quiet": quiet, "never": never[:50], "never_count": never.count(),
    })


@login_required
def audit_log(request):
    _admin_only(request)
    qs = AuditEvent.objects.select_related("actor", "target", "board", "workspace")
    kind = request.GET.get("kind", "")
    groups = {
        "signin": ["login", "login_failed"],
        "people": ["dept_created", "dept_member_added", "dept_account_created", "dept_member_removed",
                   "dept_member_left", "dept_role_changed", "password_reset"],
        "sharing": ["board_shared", "guest_invited", "board_member_removed", "board_role_changed",
                    "board_public_on", "board_public_off", "board_visibility"],
        "boards": ["board_created", "board_archived"],
    }
    if kind in groups:
        qs = qs.filter(verb__in=groups[kind])
    if request.GET.get("dept"):
        qs = qs.filter(workspace_id=request.GET["dept"])
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(actor__email__icontains=q) | Q(actor__first_name__icontains=q)
                       | Q(target__email__icontains=q) | Q(target__first_name__icontains=q)
                       | Q(board__title__icontains=q) | Q(data__email__icontains=q))
    if request.GET.get("csv"):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="lanes-audit-log.csv"'
        w = csv.writer(resp)
        w.writerow(["When", "Who", "Who (email)", "What", "Department", "Board", "IP address"])
        for e in qs[:20000]:
            w.writerow([timezone.localtime(e.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                        e.actor.display_name if e.actor else "", e.actor.email if e.actor else "",
                        e.text(), e.workspace.name if e.workspace else "", e.board.title if e.board else "", e.ip or ""])
        return resp
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "boards/company_audit.html", {
        "tab": "audit", "page": page, "kind": kind, "q": q, "dept": request.GET.get("dept", ""),
        "depts": Workspace.objects.all(), "query": params.urlencode(),
        "failed24": AuditEvent.objects.filter(verb="login_failed",
                                              created_at__gte=timezone.now() - timedelta(hours=24)).count(),
    })

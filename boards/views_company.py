"""Company admin pages: usage across Lanes and the audit log. Company admins
(superusers) only."""
import csv
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
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

    # people making changes, week by week (last 8 weeks) — is use growing?
    weeks = []
    start = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
    for i in range(7, -1, -1):
        a = start - timedelta(weeks=i)
        n = Activity.objects.filter(created_at__date__gte=a, created_at__date__lt=a + timedelta(days=7),
                                    actor__isnull=False).values("actor").distinct().count()
        weeks.append({"start": a, "n": n})
    top_week = max((w["n"] for w in weeks), default=0) or 1
    for w in weeks:
        w["pct"] = round(100 * w["n"] / top_week)

    # one department in detail: who's using it and who isn't
    dept = None
    if request.GET.get("dept"):
        ws = Workspace.objects.filter(pk=request.GET["dept"]).first()
        if ws:
            last = dict(Activity.objects.filter(actor__isnull=False, board__workspace=ws).values("actor")
                        .annotate(t=Max("created_at")).values_list("actor", "t"))
            n30 = dict(Activity.objects.filter(actor__isnull=False, board__workspace=ws, created_at__gte=d30)
                       .values("actor").annotate(n=Count("id")).values_list("actor", "n"))
            rows = []
            for m in ws.memberships.select_related("user").filter(user__is_active=True):
                u = m.user
                rows.append({"u": u, "role": m.get_role_display(), "last_login": u.last_login, "last_change": last.get(u.pk),
                             "n30": n30.get(u.pk, 0)})
            rows.sort(key=lambda r: (r["n30"] > 0, r["last_change"] or now - timedelta(days=9999)))
            dept = {"ws": ws, "rows": rows, "quiet": sum(1 for r in rows if not r["n30"]),
                    "boards": Board.objects.filter(workspace=ws, is_archived=False, is_template=False)
                    .annotate(n=Count("activity", filter=Q(activity__created_at__gte=d30))).order_by("-n", "title")}

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
        "weeks": weeks, "dept": dept,
        "feature_use": _feature_use(d30),
    })


def _feature_use(since):
    """How much the newer tools are used — a hint at what to show people."""
    from .models import RequestForm, Rule, SignOff
    return [
        ("Rules running", Rule.objects.filter(is_enabled=True).count()),
        ("Request forms", RequestForm.objects.filter(is_open=True).count()),
        ("Requests sent in (30 days)", Activity.objects.filter(verb="form_request", created_at__gte=since).count()),
        ("Sign-offs asked for (30 days)", SignOff.objects.filter(requested_at__gte=since).count()),
        ("Cards split into parts", Card.objects.filter(children__isnull=False).distinct().count()),
    ]


@login_required
def health(request):
    """Problems, backups and what's switched on — for whoever looks after Lanes."""
    from django.conf import settings
    from django.shortcuts import redirect

    from . import ai
    from .models import ErrorReport, OutgoingEmail
    _admin_only(request)
    if request.method == "POST":
        rep = ErrorReport.objects.filter(pk=request.POST.get("resolve")).first()
        if rep:
            rep.resolved_at = timezone.now()
            rep.save(update_fields=["resolved_at"])
        return redirect("company_health")
    show_all = request.GET.get("all") == "1"
    reports = ErrorReport.objects.all() if show_all else ErrorReport.objects.filter(resolved_at__isnull=True)
    week = timezone.now() - timedelta(days=7)
    return render(request, "boards/company_health.html", {
        "tab": "health",
        "reports": reports[:100], "show_all": show_all,
        "resolved_count": ErrorReport.objects.filter(resolved_at__isnull=False).count(),
        "email_on": settings.EMAIL_ENABLED, "email_via": settings.EMAIL_VIA,
        "email_failed": OutgoingEmail.objects.filter(sent_at__isnull=True, attempts__gte=5).count(),
        "email_sent_week": OutgoingEmail.objects.filter(sent_at__gte=week).count(),
        "ai_on": ai.enabled(), "files_on": settings.ATTACHMENTS_PERSISTENT,
        "db_vendor": __import__("django.db", fromlist=["connection"]).connection.vendor,
    })


@login_required
def audit_log(request):
    _admin_only(request)
    qs = AuditEvent.objects.select_related("actor", "target", "board", "workspace")
    kind = request.GET.get("kind", "")
    groups = {
        "signin": ["login", "login_failed"],
        "security": ["people_imported", "two_step_on", "two_step_off", "two_step_failed", "two_step_reset", "account_off", "account_on",
                     "settings_changed"],
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


@login_required
def people(request):
    """Everyone with an account: departments, last sign-in, two-step status.
    Company admins can switch accounts off (leavers) and reset two-step."""
    _admin_only(request)
    from django.contrib import messages
    from django.shortcuts import redirect
    from . import audit
    if request.method == "POST":
        person = User.objects.filter(pk=request.POST.get("user_id")).first()
        action = request.POST.get("action")
        if person and person != request.user and action in ("deactivate", "activate"):
            person.is_active = action == "activate"
            person.save(update_fields=["is_active"])
            audit.record(request, "account_on" if person.is_active else "account_off", target=person)
            messages.success(request, f"{person.display_name} can {'sign in again' if person.is_active else 'no longer sign in'}.")
        return redirect(request.get_full_path())
    q = (request.GET.get("q") or "").strip()
    show = request.GET.get("show", "active")
    qs = User.objects.prefetch_related("workspace_memberships__workspace").order_by("first_name", "last_name", "email")
    if show == "off":
        qs = qs.filter(is_active=False)
    elif show == "guests":
        qs = qs.filter(is_active=True, is_guest=True)
    elif show == "no2fa":
        qs = qs.filter(is_active=True, totp_enabled=False)
    else:
        qs = qs.filter(is_active=True)
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    from accounts.twofactor import required_for
    active = User.objects.filter(is_active=True)
    return render(request, "boards/company_people.html", {
        "tab": "people", "page": page, "q": q, "show": show,
        "rows": [{"u": u, "depts": [m.workspace.name for m in u.workspace_memberships.all()],
                  "needs_2fa": required_for(u) and not u.totp_enabled} for u in page],
        "with_2fa": active.filter(totp_enabled=True).count(), "active_count": active.count(),
    })


@login_required
def settings_page(request):
    _admin_only(request)
    from django.conf import settings as dj_settings

    from accounts import microsoft
    from django.contrib import messages
    from django.shortcuts import redirect
    from . import audit
    from .models import CompanySettings
    cs = CompanySettings.get()
    fields = [
        ("require_two_step", "Two-step sign-in", CompanySettings.TWO_STEP_CHOICES,
         "People who must use it are asked to set it up the next time they open Lanes."),
        ("public_links", "Who can turn on a board's public link", CompanySettings.PUBLIC_CHOICES,
         "Choosing “Nobody” also switches off every public link that's already out there."),
        ("invite_guests", "Who can invite guests (people outside the company)", CompanySettings.GUEST_CHOICES,
         "Guests already on boards keep their access. Remove them from the board or switch their account off."),
        ("create_boards", "Who can create boards", CompanySettings.CREATE_CHOICES, ""),
        ("private_boards", "Who can make a board private (hidden from its department)", CompanySettings.PRIVATE_CHOICES,
         "Company admins can always see every board."),
        ("archive_boards", "Who can close (archive) whole boards", CompanySettings.ARCHIVE_CHOICES,
         "Closed boards can be reopened from the Boards page by the same people."),
        ("microsoft_login", "Sign in with Microsoft", CompanySettings.MICROSOFT_CHOICES,
         "Microsoft handles its own two-step checks, so Lanes doesn't ask again. “Required” means leavers lose access "
         "as soon as IT disables their Microsoft account."),
        ("microsoft_auto_create", "Anyone in the company's Microsoft directory can sign in",
         [("no", "No — only people who already have a Lanes account"),
          ("yes", "Yes — create a Lanes account the first time they sign in (an admin adds them to a department)")], ""),
    ]
    if request.method == "POST" and request.POST.get("action") == "test_email":
        from django.conf import settings as dj
        from django.core.mail import send_mail
        try:
            send_mail("Lanes test email", "If you can read this, Lanes can send email. 🎉", dj.DEFAULT_FROM_EMAIL,
                      [request.user.email])
            messages.success(request, f"Test email sent to {request.user.email}. Check your inbox (and junk folder).")
        except Exception as e:
            messages.error(request, f"Sending failed: {str(e)[:200]}")
        return redirect("company_settings")
    if request.method == "POST":
        changes = []
        for name, label, choices, _ in fields:
            value = request.POST.get(name)
            current = getattr(cs, name)
            if isinstance(current, bool):
                if value not in ("yes", "no") or (value == "yes") == current:
                    continue
                setattr(cs, name, value == "yes")
                changes.append(f"{label.lower()} → {value}")
                continue
            if value in dict(choices) and value != current:
                setattr(cs, name, value)
                changes.append(f"{label.split(' (')[0].lower()} → {dict(choices)[value].split(' —')[0].lower()}")
        if "allowed_domains" in request.POST:
            raw = request.POST.get("allowed_domains", "")
            clean = ", ".join(dict.fromkeys(d.strip().lower().lstrip("@") for d in raw.replace(";", ",").replace(" ", ",").split(",")
                                            if d.strip() and "." in d))[:300]
            if clean != cs.allowed_domains:
                cs.allowed_domains = clean
                changes.append(f"company email addresses → {clean or 'any'}")
        if changes:
            cs.updated_by = request.user
            cs.save()
            audit.record(request, "settings_changed", changes="; ".join(changes))
            messages.success(request, "Company settings saved.")
        return redirect("company_settings")
    return render(request, "boards/company_settings.html", {
        "tab": "settings", "cs": cs,
        "fields": [{"name": n, "label": l, "choices": c, "help": h,
                    "value": ("yes" if getattr(cs, n) else "no") if isinstance(getattr(cs, n), bool) else getattr(cs, n)}
                   for n, l, c, h in fields],
        "email_via": dj_settings.EMAIL_VIA,
        "ms_configured": microsoft.configured(),
    })


TEMPLATE_CSV = "First name,Last name,Email,Department,Role\nJo,Bloggs,jo.bloggs@example.com,Department name,Member\n" \
               "Sam,Smith,sam.smith@example.com,Department name,Admin\n"


@login_required
def people_import(request):
    """Company → People → Add from a spreadsheet: upload, check, confirm."""
    _admin_only(request)
    from django.contrib import messages
    from django.shortcuts import redirect
    from . import audit, people_import as pi
    if request.GET.get("template"):
        resp = HttpResponse(TEMPLATE_CSV, content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="lanes-people-template.csv"'
        return resp
    if request.GET.get("download"):
        rows = request.session.pop("import_credentials", None)
        if not rows:
            messages.error(request, "Those passwords have already been downloaded or have expired.")
            return redirect("company_people")
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="lanes-new-accounts.csv"'
        w = csv.writer(resp)
        w.writerow(["Name", "Email", "Department", "Temporary password", "Sign in at"])
        site = request.build_absolute_uri("/login/")
        for r in rows:
            w.writerow([r["name"], r["email"], r["dept"], r["password"], site])
        return resp
    ctx = {"tab": "people"}
    if request.method == "POST" and request.POST.get("step") == "confirm":
        items = request.session.pop("import_plan", None)
        if not items:
            messages.error(request, "The upload expired. Upload the file again.")
            return redirect("people_import")
        created, added, new_depts = pi.apply(items, request.user)
        audit.record(request, "people_imported", count=added, new=len(created))
        if request.POST.get("send_invites") and created:
            from . import mailer
            for c in created:
                u = User.objects.get(email=c["email"])
                mailer.invite(u, request.user, c["dept"], now=False)
            ctx["invites_queued"] = len(created)
            audit.record(request, "invites_sent", count=len(created))
        request.session["import_credentials"] = created
        ctx.update({"done": True, "created": created, "added": added, "new_depts": new_depts})
        return render(request, "boards/company_import.html", ctx)
    if request.method == "POST" and request.FILES.get("file"):
        f = request.FILES["file"]
        if f.size > 5 * 1024 * 1024:
            ctx["error"] = "That file is over 5 MB — is it the right one?"
        else:
            try:
                items = pi.plan(pi.read_rows(f), bool(request.POST.get("create_departments")))
            except ValueError as e:
                ctx["error"] = str(e)
            else:
                request.session["import_plan"] = items
                ctx.update({
                    "items": items, "filename": f.name,
                    "n_new": sum(i["action"] == "new" for i in items),
                    "n_add": sum(i["action"] == "add" for i in items),
                    "n_already": sum(i["action"] == "already" for i in items),
                    "n_problem": sum(bool(i["problem"]) for i in items),
                    "new_depts": sorted({i["dept"] for i in items if i.get("new_dept") and i["action"] in ("new", "add")}),
                })
    return render(request, "boards/company_import.html", ctx)

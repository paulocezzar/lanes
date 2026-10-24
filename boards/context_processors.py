from django.conf import settings


def notifications(request):
    ctx = {"email_enabled": settings.EMAIL_ENABLED}
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return ctx
    from .models import Notification
    from .timetrack import running
    ctx["running_timer"] = running(request.user)
    from .gallery import TEMPLATES
    ctx["create_templates"] = [(t["key"], t["title"], t["bg"]) for t in TEMPLATES[:6]]
    ctx["unread_count"] = Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()
    return ctx


SIDEBAR_PAGES = {
    "home", "gallery", "gallery_template", "my_cards", "notifications", "search", "join_requests", "portfolio",
    "workload", "timesheets", "workspace_members", "workspace_settings", "company_usage", "company_people",
    "company_settings", "company_audit", "company_health", "people_import", "email_prefs", "password_change",
    "two_step_setup", "my_profile", "my_activity", "person", "department_export",
}


def sidebar(request):
    """The left sidebar on the "home" pages (not on boards, which need the width)."""
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else ""
    if not user or not user.is_authenticated or name not in SIDEBAR_PAGES:
        return {}
    from .gallery import CATEGORIES
    from .models import BoardMember, JoinRequest, Workspace, WorkspaceMember
    admin_of = set(WorkspaceMember.objects.filter(user=user, role="admin").values_list("workspace_id", flat=True))
    wss = Workspace.objects.all() if user.is_superuser else Workspace.objects.filter(memberships__user=user).distinct()
    depts = [{"ws": w, "admin": user.is_superuser or w.pk in admin_of} for w in wss.order_by("name")]
    pending = JoinRequest.objects.filter(status="pending")
    if not user.is_superuser:
        boards = BoardMember.objects.filter(user=user, role="admin").values_list("board_id", flat=True)
        from django.db.models import Q
        pending = pending.filter(Q(board_id__in=boards) | Q(board__workspace_id__in=admin_of) | Q(workspace_id__in=admin_of))
    ws_id = (match.kwargs or {}).get("workspace_id")
    return {"show_sidebar": True, "side_depts": depts, "side_join_count": pending.count(),
            "side_categories": CATEGORIES if name in ("gallery", "gallery_template") else [],
            "side_ws": int(ws_id) if ws_id else None, "side_name": name,
            "side_dept": int(request.GET["dept"]) if name == "home" and request.GET.get("dept", "").isdigit() else None}

"""Your account (profile, activity) and other people's profile cards."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import permissions
from .models import Activity, Board, BoardMember, Card, Workspace, WorkspaceMember

User = get_user_model()


@login_required
def profile(request):
    u = request.user
    if request.method == "POST":
        first = (request.POST.get("first_name") or "").strip()[:150]
        if not first:
            messages.error(request, "Your first name can't be empty.")
            return redirect("my_profile")
        u.first_name = first
        u.last_name = (request.POST.get("last_name") or "").strip()[:150]
        u.job_title = (request.POST.get("job_title") or "").strip()[:100]
        u.bio = (request.POST.get("bio") or "").strip()[:500]
        u.save(update_fields=["first_name", "last_name", "job_title", "bio"])
        messages.success(request, "Profile saved.")
        return redirect("my_profile")
    return render(request, "boards/account_profile.html", {"depts": _depts(u)})


def _depts(u):
    return list(WorkspaceMember.objects.filter(user=u).select_related("workspace").order_by("workspace__name"))


@login_required
def activity(request):
    visible = permissions.visible_boards(request.user)
    qs = Activity.objects.filter(actor=request.user, board__in=visible).select_related("board", "card").order_by("-created_at")
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    days = []
    for a in page:
        d = timezone.localtime(a.created_at).date()
        if not days or days[-1]["day"] != d:
            days.append({"day": d, "items": []})
        days[-1]["items"].append(a)
    return render(request, "boards/account_activity.html", {"page": page, "days": days, "who": request.user,
                                                            "mine": True})


@login_required
def person(request, user_id):
    """Someone's profile: who they are, their departments, what they're on and recent activity — only on
    boards the viewer can see."""
    p = get_object_or_404(User, pk=user_id, is_active=True)
    visible = permissions.visible_boards(request.user)
    shares = request.user.is_superuser or p == request.user or \
        BoardMember.objects.filter(user=p, board__in=visible).exists() or \
        WorkspaceMember.objects.filter(user=p, workspace__memberships__user=request.user).exists()
    if not shares:
        raise Http404
    cards = (Card.objects.filter(members=p, list__board__in=visible, is_archived=False, is_complete=False,
                                 is_template=False, list__is_archived=False)
             .select_related("list__board").order_by("due_at")[:30])
    acts = Activity.objects.filter(actor=p, board__in=visible).select_related("board", "card").order_by("-created_at")[:20]
    return render(request, "boards/person.html", {"p": p, "depts": _depts(p), "cards": cards, "acts": acts})


@login_required
def department_export(request, workspace_id):
    """Every board in a department as one ZIP: a CSV (opens in Excel) and a JSON per board."""
    import io
    import json
    import zipfile

    from django.utils.text import slugify

    from .views_export import _board_dict, board_csv
    ws = get_object_or_404(Workspace, pk=workspace_id)
    if not (request.user.is_superuser or permissions.is_workspace_admin(ws, request.user)):
        raise Http404
    boards = [b for b in Board.objects.filter(workspace=ws, is_archived=False).order_by("title")
              if permissions.can_view(b, request.user)]
    if request.method != "POST":
        return render(request, "boards/department_export.html", {"workspace": ws, "boards": boards})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = set()
        for b in boards:
            name = slugify(b.title) or f"board-{b.pk}"
            if name in used:
                name = f"{name}-{b.pk}"
            used.add(name)
            z.writestr(f"{name}.csv", board_csv(request, b.pk).content)
            z.writestr(f"{name}.json", json.dumps(_board_dict(b, request), indent=2, ensure_ascii=False))
        z.writestr("README.txt", f"{ws.name} — exported from Lanes on {timezone.localdate():%-d %B %Y} by "
                                 f"{request.user.display_name}.\nEach board has a .csv (open in Excel) and a .json "
                                 "(everything, for re-importing or keeping).\n")
    from . import audit
    audit.record(request, "dept_exported", workspace=ws, n=len(boards))
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{slugify(ws.name)}-{timezone.localdate()}.zip"'
    return resp

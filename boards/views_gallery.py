"""The template gallery: ready-made boards (boards/gallery.py) and the company's own template boards."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from . import audit, permissions
from . import gallery as G
from .models import Board, Workspace


def _company_templates(user):
    return list(permissions.visible_boards(user).filter(is_template=True).select_related("workspace")
                .prefetch_related("lists__cards").order_by("title"))


def _entry_builtin(t):
    n = sum(len(cards) for _, cards in t["lists"])
    return {"key": t["key"], "title": t["title"], "summary": t["summary"], "bg": t["bg"], "category": t["category"],
            "by": "Lanes", "lists": len(t["lists"]), "cards": n,
            "extras": [x for x, on in (("Request form", t.get("form")), ("Rules", t.get("rules")),
                                       ("Fields", t.get("fields")), ("Voting", t.get("voting")),
                                       ("Private", t.get("private"))) if on]}


def _entry_company(b):
    lists = [l for l in b.lists.all() if not l.is_archived]
    return {"key": f"b{b.pk}", "title": b.title, "summary": b.description or f"Made in {b.workspace.name}.",
            "bg": b.background, "category": "company", "by": b.workspace.name, "lists": len(lists),
            "cards": sum(1 for l in lists for c in l.cards.all() if not c.is_archived), "extras": []}


@login_required
def gallery(request):
    q = (request.GET.get("q") or "").strip().lower()
    cat = request.GET.get("cat") or ""
    company = [_entry_company(b) for b in _company_templates(request.user)]
    items = [_entry_builtin(t) for t in G.TEMPLATES] + company
    if cat:
        items = [i for i in items if i["category"] == cat]
    if q:
        items = [i for i in items if q in i["title"].lower() or q in i["summary"].lower()]
    cats = [("company", "Made in your company")] if company else []
    return render(request, "boards/gallery.html", {
        "items": items, "categories": cats + G.CATEGORIES, "cat": cat, "q": request.GET.get("q", ""),
        "counts": {k: sum(1 for i in [_entry_builtin(t) for t in G.TEMPLATES] + company if i["category"] == k)
                   for k, _ in cats + G.CATEGORIES},
    })


def _workspaces(user):
    ws = Workspace.objects.all() if user.is_superuser else Workspace.objects.filter(memberships__user=user).distinct()
    return [w for w in ws.order_by("name") if permissions.can_create_board(w, user)]


@login_required
def template_detail(request, key):
    user = request.user
    company_board = None
    if key.startswith("b") and key[1:].isdigit():
        company_board = get_object_or_404(Board, pk=key[1:], is_template=True, is_archived=False)
        if not permissions.can_view(company_board, user):
            raise Http404
        t = None
    else:
        t = G.BY_KEY.get(key)
        if not t:
            raise Http404
    workspaces = _workspaces(user)
    if request.method == "POST":
        ws = next((w for w in workspaces if str(w.pk) == request.POST.get("workspace")), None)
        if not ws:
            messages.error(request, "Pick a department you can create boards in.")
            return redirect("gallery_template", key=key)
        title = (request.POST.get("title") or "").strip()[:120] or (t["title"] if t else company_board.title)
        vis = request.POST.get("visibility") if request.POST.get("visibility") in dict(Board.VISIBILITY) else Board.WORKSPACE
        if t:
            board = G.build(t, workspace=ws, user=user, title=title, visibility=vis,
                                  keep_cards=request.POST.get("keep_cards") == "on",
                                  keep_tips=request.POST.get("keep_tips") == "on")
        else:
            from .copying import copy_board
            board = copy_board(company_board, workspace=ws, title=title, user=user, visibility=vis,
                               keep_cards=request.POST.get("keep_cards") == "on",
                               keep_template_cards=request.POST.get("keep_tips") == "on")
        if board.visibility == Board.PRIVATE and not permissions.can_make_private(board, user):
            board.visibility = Board.WORKSPACE
            board.save(update_fields=["visibility"])
        audit.record(request, "board_from_gallery", board=board, template=t["title"] if t else company_board.title)
        messages.success(request, f"Made “{board.title}” from the template.")
        return redirect("board", board_id=board.pk)
    if t:
        preview = G.preview(t)
        info = {"title": t["title"], "summary": t["summary"], "about": t["about"], "bg": t["bg"], "by": "Lanes",
                "labels": t.get("labels", []), "fields": [f[0] for f in t.get("fields", [])],
                "extras": _entry_builtin(t)["extras"], "has_tips": any(c["tip"] for _, cs in t["lists"] for c in cs),
                "category": dict(G.CATEGORIES).get(t["category"]), "cat": t["category"]}
    else:
        lists = [l for l in company_board.lists.all().order_by("position") if not l.is_archived]
        preview = [{"title": l.title, "cards": [{"title": c.title, "tip": c.is_template, "labels": [],
                                                 "items": []}
                                                for c in l.cards.filter(is_archived=False).order_by("position")[:12]]}
                   for l in lists]
        info = {"title": company_board.title, "summary": company_board.description, "about": [], "bg": company_board.background,
                "by": company_board.workspace.name, "labels": [(l.name, l.colour) for l in company_board.labels.all()],
                "fields": [f.name for f in company_board.custom_fields.all()], "extras": [],
                "has_tips": company_board.lists.filter(cards__is_template=True).exists(),
                "category": "Made in your company", "cat": "company", "board_id": company_board.pk}
    return render(request, "boards/gallery_template.html", {
        "key": key, "t": info, "preview": preview, "workspaces": workspaces, "company": bool(company_board),
        "visibilities": Board.VISIBILITY,
    })

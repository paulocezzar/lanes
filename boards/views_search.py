from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from . import search
from .models import SavedSearch

EXAMPLES = [
    ("my cards due this week", "Plain words work too"),
    ("waiting for sign-off", "Cards still to be signed off"),
    ("at risk", "Cards that may miss their date"),
    ("@me is:open", "My open cards"),
    ("is:overdue", "Everything overdue"),
    ("is:due-week board:planning", "Planning cards due this week"),
    ("label:urgent", "Cards labelled Urgent"),
]


@login_required
def search_page(request):
    q = (request.GET.get("q") or "").strip()
    understood = search.from_words(q)
    results, terms, ops = search.run(request.user, understood) if q else ([], [], [])
    rows = [{"card": c, "hint": search.where_matched(c, terms)} for c in results]
    return render(request, "boards/search.html", {
        "q": q, "results": results, "rows": rows, "saved": SavedSearch.objects.filter(owner=request.user),
        "examples": EXAMPLES, "searched": bool(q), "understood": understood if understood != q else "",
    })


@login_required
@require_POST
def search_save(request):
    q = (request.POST.get("q") or "").strip()[:300]
    name = (request.POST.get("name") or "").strip()[:60] or q[:60]
    if q:
        SavedSearch.objects.update_or_create(owner=request.user, name=name, defaults={"query": q})
        messages.success(request, f"Saved “{name}”.")
    return redirect(f"{reverse('search')}?{urlencode({'q': q})}")


@login_required
@require_POST
def search_delete(request, pk):
    get_object_or_404(SavedSearch, pk=pk, owner=request.user).delete()
    return redirect(request.POST.get("next") or "search")


@login_required
def quick(request):
    """Suggestions while typing in the top search box: cards, boards, people, departments and templates."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from django.http import JsonResponse

    from . import gallery, permissions
    from .models import BoardMember, Card, Workspace, WorkspaceMember

    q = (request.GET.get("q") or "").strip()[:100]
    if len(q) < 2:
        return JsonResponse({"q": q, "cards": [], "boards": [], "people": [], "departments": [], "templates": []})
    user = request.user
    visible = permissions.visible_boards(user).filter(is_template=False)
    words = q.split()
    plain = not any(":" in w or w.startswith("@") for w in words)

    cards, seen = [], set()
    if plain:  # titles first — that's what people are usually after
        tq = Card.objects.filter(list__board__in=visible, list__is_archived=False, is_archived=False, is_template=False)
        for w in words:
            tq = tq.filter(title__icontains=w)
        for c in tq.select_related("list__board").order_by("is_complete", "-updated_at")[:8]:
            cards.append(c)
            seen.add(c.pk)
    if len(cards) < 8:
        more, _, _ = search.run(user, search.from_words(q), limit=8)
        cards += [c for c in more if c.pk not in seen][:8 - len(cards)]

    bq = visible
    for w in words if plain else []:
        bq = bq.filter(title__icontains=w)
    boards = list(bq.select_related("workspace").order_by("title")[:5]) if plain else []

    people = []
    if plain:
        User = get_user_model()
        pq = User.objects.filter(is_active=True)
        if not user.is_superuser:
            pq = pq.filter(Q(board_memberships__board__in=visible)
                           | Q(workspace_memberships__workspace__memberships__user=user))
        for w in words:
            pq = pq.filter(Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(email__icontains=w)
                           | Q(job_title__icontains=w))
        people = list(pq.distinct().order_by("first_name", "last_name")[:4])

    wq = Workspace.objects.all() if user.is_superuser else Workspace.objects.filter(memberships__user=user)
    depts = list(wq.filter(name__icontains=q).distinct().order_by("name")[:3]) if plain else []
    low = q.lower()
    tpls = [t for t in gallery.TEMPLATES if low in t["title"].lower()][:3] if plain else []

    def where(c):
        return f"{c.list.board.title} · {c.list.title}"

    return JsonResponse({
        "q": q,
        "cards": [{"title": c.title, "where": where(c), "done": c.is_complete,
                   "url": f"/b/{c.list.board_id}/?card={c.pk}"} for c in cards],
        "boards": [{"title": b.title, "where": b.workspace.name, "bg": b.background, "url": f"/b/{b.pk}/"}
                   for b in boards],
        "people": [{"title": p.display_name, "where": p.job_title or p.email, "initials": p.initials, "hue": p.avatar_hue,
                    "url": reverse("person", args=[p.pk])} for p in people],
        "departments": [{"title": w.name, "where": "Department", "initial": w.initial,
                         "url": f"{reverse('home')}?dept={w.pk}"} for w in depts],
        "templates": [{"title": t["title"], "where": "Template", "bg": t["bg"],
                       "url": reverse("gallery_template", args=[t["key"]])} for t in tpls],
    })

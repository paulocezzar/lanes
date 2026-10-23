from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from . import search
from .models import SavedSearch

EXAMPLES = [
    ("@me is:open", "My open cards"),
    ("is:overdue", "Everything overdue"),
    ("is:due-week board:planning", "Planning cards due this week"),
    ("label:urgent", "Cards labelled Urgent"),
]


@login_required
def search_page(request):
    q = (request.GET.get("q") or "").strip()
    results, terms, ops = search.run(request.user, q) if q else ([], [], [])
    return render(request, "boards/search.html", {
        "q": q, "results": results, "saved": SavedSearch.objects.filter(owner=request.user),
        "examples": EXAMPLES, "searched": bool(q),
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

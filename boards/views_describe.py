"""Create a board from a description: AI designs it when AI is on; otherwise the closest ready-made template
is picked. Either way the person sees a preview before anything is made."""
import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from . import ai, audit, permissions
from . import gallery as G
from .models import COLOURS, Board
from .views_gallery import _workspaces

INSPIRE = [
    ("Organise my to-dos", "A private board for my own to-dos: things to do today, this week and later."),
    ("Plan a project", "Plan a project with a few people: agree what's needed, who's doing what, and track it to done."),
    ("Run an event", "Organise an event: venue, guests, food, suppliers and a run-sheet for the day."),
    ("Take requests", "Let other teams send us requests, then sort them, give each one to someone and close them."),
    ("Welcome a new starter", "Onboard new starters: paperwork, equipment, first day, first weeks."),
    ("Track orders", "Keep track of what needs ordering from suppliers, what's on its way and what's arrived."),
]


def _from_ai(spec):
    """Turn the AI's JSON into the gallery's template shape (with sensible limits)."""
    labels = [(str(l.get("name", ""))[:40], l.get("colour") if l.get("colour") in COLOURS else "grey")
              for l in (spec.get("labels") or [])[:4] if isinstance(l, dict) and l.get("name")]
    lists = []
    for l in (spec.get("lists") or [])[:8]:
        if not isinstance(l, dict) or not l.get("title"):
            continue
        cards = [G.T(str(c.get("title"))[:250], str(c.get("description") or "")[:1000],
                     items=[str(x)[:250] for x in (c.get("checklist") or [])[:10]])
                 for c in (l.get("cards") or [])[:12] if isinstance(c, dict) and c.get("title")]
        lists.append((str(l["title"])[:120], cards))
    if len(lists) < 2:
        raise ValueError("no lists")
    return {"key": "ai", "title": str(spec.get("title") or "New board")[:120], "bg": "dusk",
            "summary": str(spec.get("summary") or "")[:300], "about": [], "labels": labels, "lists": lists}


def _closest(text):
    words = set(re.findall(r"[a-z]{3,}", text.lower()))
    def score(t):
        hay = " ".join([t["title"], t["summary"], " ".join(t["about"]), dict(G.CATEGORIES)[t["category"]]]
                       + [n for n, _ in t["lists"]]).lower()
        return sum(1 for w in words if w in hay)
    return max(G.TEMPLATES, key=score)


@login_required
def describe(request):
    workspaces = _workspaces(request.user)
    ctx = {"inspire": INSPIRE, "workspaces": workspaces, "ai_on": ai.enabled(), "text": "", "plan": None}
    if request.method == "POST":
        text = (request.POST.get("text") or "").strip()[:4000]
        ws = next((w for w in workspaces if str(w.pk) == request.POST.get("workspace")), None)
        ctx.update({"text": text, "ws": ws})
        if request.POST.get("action") == "create" and request.POST.get("plan"):
            try:
                plan = json.loads(request.POST["plan"])
                plan["lists"] = [(n, cs) for n, cs in plan["lists"]]
                plan["labels"] = [tuple(x) for x in plan.get("labels", [])]
            except (ValueError, KeyError, TypeError):
                messages.error(request, "Something went wrong — please describe it again.")
                return redirect("describe_board")
            if not ws:
                messages.error(request, "Pick a department you can create boards in.")
                return render(request, "boards/describe.html", ctx)
            title = (request.POST.get("title") or plan["title"]).strip()[:120]
            vis = request.POST.get("visibility") if request.POST.get("visibility") in dict(Board.VISIBILITY) else Board.WORKSPACE
            board = G.build(plan, workspace=ws, user=request.user, title=title, visibility=vis,
                            keep_cards=True, keep_tips=request.POST.get("keep_tips") == "on")
            if board.visibility == Board.PRIVATE and not permissions.can_make_private(board, request.user):
                board.visibility = Board.WORKSPACE
                board.save(update_fields=["visibility"])
            audit.record(request, "board_from_gallery", board=board, template="a description")
            messages.success(request, f"Made “{board.title}”. Change anything you like — it's yours now.")
            return redirect("board", board_id=board.pk)
        if not text:
            messages.error(request, "Describe what you want to organise first.")
            return render(request, "boards/describe.html", ctx)
        plan, how = None, ""
        if ai.enabled():
            try:
                plan, how = _from_ai(ai.draft_board(text)), "ai"
            except (ai.AIUnavailable, ValueError):
                messages.warning(request, "The AI couldn't do that just now, so here's the closest ready-made layout.")
        if plan is None:
            plan, how = dict(_closest(text)), "template"
        ctx.update({"plan": plan, "how": how, "plan_json": json.dumps({**plan, "lists": plan["lists"]}),
                    "preview": G.preview(plan), "has_tips": any(c["tip"] for _, cs in plan["lists"] for c in cs),
                    "visibilities": Board.VISIBILITY})
    return render(request, "boards/describe.html", ctx)

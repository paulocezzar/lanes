"""Project health for the Portfolio page and weekly status reports.

Automatic RAG:
  red   — an overdue milestone, 3+ overdue cards, or spending over budget
  amber — any overdue card, a task starting before what it waits for, spending
          over 90% of budget, or no status update for 14 days
  green — otherwise
Whoever runs the project can override it (On track / At risk / Off track).
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .models import Card, CostItem, Dependency, TimeEntry

RAG_LABEL = {"green": "On track", "amber": "At risk", "red": "Off track"}


def spend(board):
    """(costs, labour, total) in pounds. Labour = time logged × each person's hourly rate."""
    costs = CostItem.objects.filter(board=board).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    labour = Decimal("0")
    for e in TimeEntry.objects.filter(card__list__board=board, minutes__gt=0).select_related("user"):
        if e.user.hourly_rate:
            labour += e.user.hourly_rate * Decimal(e.minutes) / Decimal(60)
    labour = labour.quantize(Decimal("0.01"))
    return costs, labour, costs + labour


def health(board, now=None):
    from .schedule import analyse
    now = now or timezone.now()
    today = timezone.localdate(now)
    cards = list(Card.objects.filter(list__board=board, list__is_archived=False, is_archived=False, is_template=False)
                 .select_related("list").prefetch_related("children", "checklists__items", "members",
                                                          "signoffs__approver", "dependencies__depends_on"))
    total = len(cards)
    done = sum(c.is_complete for c in cards)
    ids = {c.pk for c in cards}
    top = [c for c in cards if c.parent_id not in ids]      # parts count inside their card, not twice
    work = round(100 * sum(c.work_fraction() for c in top) / len(top)) if top else 0
    open_ = [c for c in cards if not c.is_complete]
    overdue = sorted([c for c in open_ if c.due_at and c.due_at < now], key=lambda c: c.due_at)
    milestones = sorted([c for c in cards if c.is_milestone and c.due_at], key=lambda c: c.due_at)
    late_ms = [c for c in milestones if not c.is_complete and c.due_at < now]
    next_ms = next((c for c in milestones if not c.is_complete and c.due_at >= now), None)
    deps = list(Dependency.objects.filter(card__in=cards).values_list("card_id", "depends_on_id", "lag_days"))
    info = analyse(cards, deps)
    blocked = [c for c in open_ if info.get(c.pk, {}).get("late")]
    finish = max((v["end"] for v in info.values()), default=None)
    critical = sorted([c for c in open_ if info.get(c.pk, {}).get("critical")], key=lambda c: info[c.pk]["start"])
    costs, labour, spent = spend(board)
    budget = board.budget
    used = (spent / budget * 100) if budget else None

    reasons, auto = [], "green"
    if late_ms:
        auto = "red"
        reasons.append(f"milestone overdue: {late_ms[0].title}")
    if len(overdue) >= 3:
        auto = "red"
        reasons.append(f"{len(overdue)} cards overdue")
    elif overdue:
        auto = "amber" if auto == "green" else auto
        reasons.append(f"{len(overdue)} card{'s' if len(overdue) != 1 else ''} overdue")
    if budget and spent > budget:
        auto = "red"
        reasons.append("over budget")
    elif used is not None and used >= 90:
        auto = "amber" if auto == "green" else auto
        reasons.append(f"{used:.0f}% of budget spent")
    if blocked:
        auto = "amber" if auto == "green" else auto
        reasons.append(f"{len(blocked)} starting before what they wait for")
    from .risk import reason as risk_reason
    at_risk = [(c, r) for c in open_ for r in [risk_reason(c, now)] if r]
    if at_risk:
        auto = "amber" if auto == "green" else auto
        c, r = at_risk[0]
        more = f" (+{len(at_risk) - 1} more)" if len(at_risk) > 1 else ""
        reasons.append(f"{c.title}: {r[0].lower() + r[1:]}{more}")
    since = board.status_updated_at or board.created_at
    if board.is_project and since and since < now - timedelta(days=14):
        auto = "amber" if auto == "green" else auto
        reasons.append("no status update for 2 weeks")
    rag = auto if board.rag == "auto" else board.rag
    week_ago, week_ahead = now - timedelta(days=7), now + timedelta(days=7)
    return {
        "board": board, "rag": rag, "rag_label": RAG_LABEL[rag], "auto": board.rag == "auto", "auto_rag": auto,
        "reasons": reasons, "total": total, "done": done, "pct": work, "cards_pct": round(100 * done / total) if total else 0,
        "items": [_line(c, now, info) for c in sorted(top, key=lambda c: (c.is_complete, c.due_at or now + timedelta(days=9999), c.position))],
        "start": min((v["start"] for v in info.values()), default=None), "at_risk": at_risk,
        "overdue": overdue, "milestones": milestones, "next_ms": next_ms, "late_ms": late_ms,
        "blocked": blocked, "finish": finish, "critical": critical[:8],
        "done_week": [c for c in cards if c.is_complete and c.updated_at >= week_ago],
        "due_week": sorted([c for c in open_ if c.due_at and now <= c.due_at <= week_ahead], key=lambda c: c.due_at),
        "budget": budget, "spent": spent, "costs": costs, "labour": labour,
        "left": (budget - spent) if budget else None,
        "budget_pct": round(used) if used is not None else None, "today": today,
    }


def _line(card, now, info):
    """One card in a project's drill-down: how far along it is and what stage it's at."""
    stage = ""
    kids = [k for k in card.children.all() if not k.is_archived]
    kid_ids = {k.pk for k in kids}
    for cl in card.checklists.all():
        for it in cl.items.all():
            if it.linked_card_id in kid_ids or it.is_done or (it.has_qty and it.qty_done >= it.qty_target):
                continue
            stage = it.text + (f" {it.qty_done_txt}/{it.qty_target_txt}" if it.has_qty else "")
            break
        if stage:
            break
    if not stage:
        nxt = next((k for k in sorted(kids, key=lambda k: (k.due_at or now + timedelta(days=9999))) if not k.is_complete), None)
        if nxt:
            stage = nxt.title
    return {"card": card, "pct": card.work_pct(), "stage": stage, "risk": card.risk(now),
            "end": info[card.pk]["end"] if card.pk in info else None}


def timeline(rows, today):
    """A shared calendar for the Portfolio: each project's bar from its first start to its finish."""
    spans = [(r["start"], r["finish"]) for r in rows if r.get("start") and r.get("finish")]
    if not spans:
        return None
    lo = min([s for s, _ in spans] + [today]) - timedelta(days=3)
    hi = max([e for _, e in spans] + [today]) + timedelta(days=7)
    total = max((hi - lo).days, 1)
    pct = lambda d: round(100 * (d - lo).days / total, 2)
    for r in rows:
        if r.get("start") and r.get("finish"):
            r["tl"] = {"left": pct(r["start"]), "width": max(pct(r["finish"]) - pct(r["start"]), 0.8)}
    months, d = [], lo.replace(day=1)
    while d <= hi:
        if d >= lo:
            months.append({"label": d.strftime("%b %y"), "left": pct(d)})
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {"today": pct(today), "months": months}


def projects_for(user):
    from . import permissions
    return (permissions.visible_boards(user).filter(is_project=True, is_archived=False)
            .select_related("workspace", "project_owner").order_by("workspace__name", "title"))

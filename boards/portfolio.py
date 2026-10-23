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
                 .select_related("list"))
    total = len(cards)
    done = sum(c.is_complete for c in cards)
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
    if board.is_project and (not board.status_updated_at or board.status_updated_at < now - timedelta(days=14)):
        auto = "amber" if auto == "green" else auto
        reasons.append("no status update for 2 weeks")
    rag = auto if board.rag == "auto" else board.rag
    week_ago, week_ahead = now - timedelta(days=7), now + timedelta(days=7)
    return {
        "board": board, "rag": rag, "rag_label": RAG_LABEL[rag], "auto": board.rag == "auto", "auto_rag": auto,
        "reasons": reasons, "total": total, "done": done, "pct": round(100 * done / total) if total else 0,
        "overdue": overdue, "milestones": milestones, "next_ms": next_ms, "late_ms": late_ms,
        "blocked": blocked, "finish": finish, "critical": critical[:8],
        "done_week": [c for c in cards if c.is_complete and c.updated_at >= week_ago],
        "due_week": sorted([c for c in open_ if c.due_at and now <= c.due_at <= week_ahead], key=lambda c: c.due_at),
        "budget": budget, "spent": spent, "costs": costs, "labour": labour,
        "left": (budget - spent) if budget else None,
        "budget_pct": round(used) if used is not None else None, "today": today,
    }


def projects_for(user):
    from . import permissions
    return (permissions.visible_boards(user).filter(is_project=True, is_archived=False)
            .select_related("workspace", "project_owner").order_by("workspace__name", "title"))

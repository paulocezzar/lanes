"""Gantt scheduling: task spans, finish-to-start dependencies, critical path,
and pushing later tasks back when an earlier one slips.

Spare time (float) and pushes count working days (Mon–Fri). A task runs from its start date (or its due date if it
has none) to its due date, inclusive. A milestone is a single day.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import Card, Dependency


def span(card):
    """(start, end) dates for a card, or None if it has no dates."""
    due = timezone.localtime(card.due_at).date() if card.due_at else None
    start = card.start_date
    if card.is_milestone:
        d = due or start
        return (d, d) if d else None
    if due and start:
        return (min(start, due), due)
    if due:
        return (due, due)
    if start:
        return (start, start)
    return None


def _set_span(card, start, end):
    """Move a card to [start, end] keeping its due time of day."""
    t = timezone.localtime(card.due_at).time() if card.due_at else time(17, 0)
    card.due_at = timezone.make_aware(datetime.combine(end, t))
    card.start_date = None if card.is_milestone or start == end and not card.start_date else start
    card.save(update_fields=["due_at", "start_date", "updated_at"])


def wd_add(d, n):
    """Move n working days (Mon–Fri) from d; n may be negative."""
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d


def wd_diff(a, b):
    """Working days from a to b (positive if b is later)."""
    if a == b:
        return 0
    lo, hi, sign = (a, b, 1) if b > a else (b, a, -1)
    n, d = 0, lo
    while d < hi:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return sign * n


def wd_len(start, end):
    """Working days in [start, end], at least 1."""
    return max(1, wd_diff(start - timedelta(days=1), end))


def would_cycle(card, depends_on):
    """True if making `card` wait for `depends_on` would create a loop."""
    if card.pk == depends_on.pk:
        return True
    seen, stack = set(), [depends_on.pk]
    while stack:
        cur = stack.pop()
        if cur == card.pk:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(Dependency.objects.filter(card_id=cur).values_list("depends_on_id", flat=True))
    return False


def push_dependants(card, _depth=0):
    """After `card` moved or grew: any task waiting for it that now starts too
    early is pushed later (keeping its length), and so on down the chain.
    Returns the cards that moved. Never pulls anything earlier."""
    moved = []
    if _depth > 200:
        return moved
    sp = span(card)
    if not sp:
        return moved
    for dep in Dependency.objects.filter(depends_on=card).select_related("card"):
        succ = dep.card
        if succ.is_archived:
            continue
        ss = span(succ)
        if not ss:
            continue
        earliest = wd_add(sp[1], 1 + dep.lag_days)
        if ss[0] < earliest:
            shift = earliest - ss[0]
            _set_span(succ, ss[0] + shift, ss[1] + shift)
            moved.append(succ)
            moved += push_dependants(succ, _depth + 1)
    return moved


def analyse(cards, deps):
    """Critical path by a backward pass over the planned dates.

    cards: iterable of Card; deps: iterable of (card_id, depends_on_id, lag).
    Returns {card_id: {"start", "end", "float", "critical", "late"}} for dated cards.
    "late" = it starts before something it waits for has finished.
    """
    spans = {c.pk: span(c) for c in cards}
    spans = {k: v for k, v in spans.items() if v}
    if not spans:
        return {}
    succs, preds = defaultdict(list), defaultdict(list)
    for cid, pid, lag in deps:
        if cid in spans and pid in spans:
            succs[pid].append((cid, lag))
            preds[cid].append((pid, lag))
    project_end = max(e for _, e in spans.values())
    # latest finish, walking from the tasks that finish last
    order = sorted(spans, key=lambda k: spans[k][1], reverse=True)
    latest = {}

    def lf(k, seen=()):
        if k in latest:
            return latest[k]
        if k in seen:            # loop guard (shouldn't happen — we block cycles)
            return project_end
        best = project_end
        for s, lag in succs.get(k, []):
            s_start, s_end = spans[s]
            latest_start = wd_add(lf(s, seen + (k,)), -(wd_len(s_start, s_end) - 1))
            best = min(best, wd_add(latest_start, -(1 + lag)))
        latest[k] = best
        return best

    out = {}
    for k in order:
        start, end = spans[k]
        fl = wd_diff(end, lf(k))
        late = any(start <= spans[p][1] + timedelta(days=lag) for p, lag in preds.get(k, []))
        out[k] = {"start": start, "end": end, "float": fl, "critical": fl <= 0, "late": late}
    # Only call it a critical *path* when it connects: tasks with zero float that
    # have no critical neighbour and aren't the last task are just "tight".
    for k, v in out.items():
        if v["critical"] and not succs.get(k) and not preds.get(k) and v["end"] != project_end:
            v["critical"] = False
    return out


def board_cards(board):
    return (Card.objects.filter(list__board=board, list__is_archived=False, is_archived=False, is_template=False)
            .select_related("list").prefetch_related("members", "checklists__items"))
